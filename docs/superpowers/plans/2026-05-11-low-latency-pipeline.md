# Low-Latency Voice Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reducir la latencia percibida del agente de voz de 7–11s a ~3–4s y eliminar el silencio absoluto mientras procesa.

**Architecture:** Tres mejoras en paralelo: (1) feedback auditivo inmediato con tono local + "un momento" via TTS, (2) Claude con streaming de texto que alimenta un chunker de oraciones, (3) TTS streaming por chunk hacia Twilio sin esperar la respuesta completa.

**Tech Stack:** Python 3.11+, FastAPI, Anthropic SDK (streaming), ElevenLabs SDK (streaming), asyncio.Queue, Twilio Media Streams WebSocket.

---

## File Structure

| Archivo | Cambios |
|---|---|
| `main.py` | Reducir `SILENCE_CHUNKS`, reescribir `handle_speech`, agregar `send_tts_streaming` |
| `agent.py` | Agregar `get_agent_response_streaming`, mantener `get_agent_response` intacto |
| `tests/test_chunker.py` | Nuevo — tests del chunker de oraciones |
| `tests/test_agent_streaming.py` | Nuevo — tests de `get_agent_response_streaming` con mocks |

---

## Task 1: Reducir VAD silence y conectar tono de pensamiento

**Files:**
- Modify: `main.py:178` (`SILENCE_CHUNKS`)
- Modify: `main.py:304-335` (`handle_speech`)

- [ ] **Step 1: Cambiar SILENCE_CHUNKS**

En `main.py` línea 178, cambiar:
```python
SILENCE_CHUNKS = 70   # 70 × 20 ms = 1.4 s de silencio → procesar
```
a:
```python
SILENCE_CHUNKS = 50   # 50 × 20 ms = 1.0 s de silencio → procesar
```

- [ ] **Step 2: Agregar import de generate_thinking_tone**

En `main.py` línea 32, cambiar:
```python
from audio_utils import compute_rms, mulaw_decode, mulaw_encode, mulaw_to_wav
```
a:
```python
from audio_utils import compute_rms, generate_thinking_tone, mulaw_decode, mulaw_encode, mulaw_to_wav
```

- [ ] **Step 3: Agregar helper para enviar tono local**

Después de la función `send_tts` (línea ~501 de `main.py`), agregar:

```python
async def send_thinking_tone(websocket: WebSocket, stream_sid: str | None, stop: asyncio.Event | None = None) -> None:
    """Envía el tono do-mi-sol local en loop mientras se procesa.
    
    Llama a generate_thinking_tone() localmente (sin red) y envía los chunks
    a Twilio. Repite el tono en loop hasta que stop se setee.
    """
    if not stream_sid:
        return
    tone_bytes = generate_thinking_tone()
    try:
        while True:
            for i in range(0, len(tone_bytes), CHUNK_BYTES):
                if stop and stop.is_set():
                    return
                chunk = tone_bytes[i: i + CHUNK_BYTES]
                await websocket.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                })
            # Pausa de 0.3s entre repeticiones del tono
            for _ in range(15):  # 15 × 20ms = 300ms
                if stop and stop.is_set():
                    return
                await asyncio.sleep(0.02)
    except Exception:
        pass
```

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: reduce VAD silence to 1s, add send_thinking_tone helper"
```

---

## Task 2: Chunker de oraciones

**Files:**
- Create: `tests/test_chunker.py`
- Modify: `agent.py` (agregar función `_chunk_text`)

- [ ] **Step 1: Crear archivo de tests**

Crear `tests/__init__.py` vacío si no existe, luego crear `tests/test_chunker.py`:

```python
"""Tests para el chunker de oraciones de streaming TTS."""
import pytest
from agent import _chunk_text


def test_corte_en_punto_con_minimo_chars():
    buffer = "El total es de cien pesos."
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El total es de cien pesos."]
    assert resto == ""


def test_no_corta_si_muy_corto():
    buffer = "Hola."
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == "Hola."


def test_corte_en_coma_con_minimo_30_chars():
    buffer = "El proveedor cables y accesorios, tiene facturas pendientes"
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El proveedor cables y accesorios,"]
    assert resto == " tiene facturas pendientes"


def test_no_corta_coma_si_menos_30_chars():
    buffer = "Hola, cómo estás"
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == "Hola, cómo estás"


def test_multiples_oraciones():
    buffer = "El total es de cien pesos. Hay tres facturas pendientes."
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["El total es de cien pesos.", " Hay tres facturas pendientes."]
    assert resto == ""


def test_interrogacion():
    buffer = "¿Qué proyecto tiene más gasto este mes?"
    chunks, resto = _chunk_text(buffer)
    assert chunks == ["¿Qué proyecto tiene más gasto este mes?"]
    assert resto == ""


def test_sin_puntuacion():
    buffer = "Este es un texto sin puntuación final"
    chunks, resto = _chunk_text(buffer)
    assert chunks == []
    assert resto == buffer


def test_flush_fuerza_todo():
    buffer = "Texto sin puntuación"
    chunks, resto = _chunk_text(buffer, flush=True)
    assert chunks == ["Texto sin puntuación"]
    assert resto == ""


def test_flush_vacio():
    chunks, resto = _chunk_text("", flush=True)
    assert chunks == []
    assert resto == ""
```

- [ ] **Step 2: Ejecutar tests para verificar que fallan**

```bash
cd /home/sebas/mcp-call
python -m pytest tests/test_chunker.py -v
```
Esperado: `ImportError` o `FAILED` porque `_chunk_text` no existe aún.

- [ ] **Step 3: Implementar `_chunk_text` en `agent.py`**

Agregar al inicio de `agent.py` después de los imports existentes:

```python
# Chunker de oraciones para streaming TTS
_CHUNK_HARD_PUNCT = frozenset('.?!')   # corte fuerte, mínimo 15 chars
_CHUNK_SOFT_PUNCT = frozenset(',;')    # corte suave, mínimo 30 chars
_CHUNK_MIN_HARD   = 15
_CHUNK_MIN_SOFT   = 30


def _chunk_text(buffer: str, flush: bool = False) -> tuple[list[str], str]:
    """Divide el buffer en chunks listos para TTS y retorna el resto.

    Corta en:
    - . ? !  si el buffer hasta ese punto tiene >= 15 chars
    - , ;    si el buffer hasta ese punto tiene >= 30 chars

    Si flush=True, retorna todo el buffer como un chunk (aunque no tenga puntuación).

    Returns:
        (chunks, resto) donde chunks es lista de segmentos listos para TTS
        y resto es el texto que aún no tiene suficiente puntuación.
    """
    chunks: list[str] = []
    pos = 0
    start = 0

    while pos < len(buffer):
        ch = buffer[pos]
        segment_len = pos - start + 1

        if ch in _CHUNK_HARD_PUNCT and segment_len >= _CHUNK_MIN_HARD:
            chunks.append(buffer[start:pos + 1])
            start = pos + 1
        elif ch in _CHUNK_SOFT_PUNCT and segment_len >= _CHUNK_MIN_SOFT:
            chunks.append(buffer[start:pos + 1])
            start = pos + 1

        pos += 1

    resto = buffer[start:]

    if flush and resto.strip():
        chunks.append(resto)
        resto = ""

    return chunks, resto
```

- [ ] **Step 4: Ejecutar tests para verificar que pasan**

```bash
cd /home/sebas/mcp-call
python -m pytest tests/test_chunker.py -v
```
Esperado: todos `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_chunker.py tests/__init__.py
git commit -m "feat: add sentence chunker _chunk_text for streaming TTS"
```

---

## Task 3: `get_agent_response_streaming` en `agent.py`

**Files:**
- Modify: `agent.py` (agregar nueva función)
- Create: `tests/test_agent_streaming.py`

- [ ] **Step 1: Escribir tests con mocks**

Crear `tests/test_agent_streaming.py`:

```python
"""Tests para get_agent_response_streaming con mocks de Anthropic."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_streaming_sin_tools_produce_chunks():
    """Respuesta simple sin tool calls produce al menos un chunk de texto."""
    from agent import get_agent_response_streaming

    texto_completo = "El total de facturas es de cien mil pesos. Hay tres proveedores activos."
    chunks_recibidos = []
    sentinel_recibido = False

    async def mock_queue_put(item):
        nonlocal sentinel_recibido
        if item is None:
            sentinel_recibido = True
        elif isinstance(item, str):
            chunks_recibidos.append(item)

    queue = AsyncMock()
    queue.put = mock_queue_put

    # Mock del stream de Anthropic
    mock_event_text = MagicMock()
    mock_event_text.type = "content_block_delta"
    mock_event_text.delta = MagicMock()
    mock_event_text.delta.type = "text_delta"
    mock_event_text.delta.text = texto_completo

    mock_final = MagicMock()
    mock_final.usage = MagicMock(input_tokens=100, output_tokens=20)
    mock_final.stop_reason = "end_turn"
    mock_final.content = [MagicMock(type="text", text=texto_completo)]

    async def mock_stream_iter():
        yield mock_event_text

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_ctx)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_stream_ctx.__aiter__ = lambda self: mock_stream_iter()
    mock_stream_ctx.get_final_message = AsyncMock(return_value=mock_final)

    with patch("agent.async_client") as mock_client:
        mock_client.messages.stream.return_value = mock_stream_ctx
        history = []
        in_tok, out_tok = await get_agent_response_streaming("cuántas facturas hay", history, queue)

    assert sentinel_recibido, "Debe enviarse None al final como sentinel"
    assert in_tok == 100
    assert out_tok == 20
    assert len(history) == 2  # user + assistant
```

- [ ] **Step 2: Ejecutar tests para verificar que fallan**

```bash
cd /home/sebas/mcp-call
python -m pytest tests/test_agent_streaming.py -v
```
Esperado: `ImportError` porque `get_agent_response_streaming` no existe aún.

- [ ] **Step 3: Implementar `get_agent_response_streaming` en `agent.py`**

Agregar al final de `agent.py`, antes del último bloque de comentarios:

```python
# Sentinel especial para indicar que hay tool_use en progreso
_TOOL_USE_SENTINEL = "__TOOL_USE__"


async def get_agent_response_streaming(
    user_text: str,
    history: list,
    text_queue: asyncio.Queue,
) -> tuple[int, int]:
    """Consulta Claude en modo streaming y envía chunks de texto a text_queue.

    Mientras Claude genera texto, lo acumula en un buffer y lo corta en
    chunks usando _chunk_text (pausas naturales: . ? ! , ;).

    Señales enviadas a text_queue:
    - str: chunk de texto listo para TTS
    - _TOOL_USE_SENTINEL: Claude está ejecutando una tool (activar tono de espera)
    - None: fin de la respuesta (sentinel)

    Returns:
        (input_tokens, output_tokens)
    """
    t_start = time.perf_counter()
    print(f"\n{'='*60}")
    print(f"[Agente streaming] Pregunta: {user_text!r}")

    history.append({"role": "user", "content": user_text})

    buffer = ""
    total_input_tokens = 0
    total_output_tokens = 0
    full_text = ""
    tool_in_progress = False

    try:
        # Primera llamada: puede incluir tool_use
        messages = list(history)
        
        while True:
            async with async_client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=_mcp_tools,
                messages=messages,
            ) as stream:
                async for event in stream:
                    if not hasattr(event, "type"):
                        continue

                    if event.type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block and getattr(block, "type", "") == "tool_use":
                            # Flush buffer acumulado antes del tool
                            if buffer.strip():
                                chunks, buffer = _chunk_text(buffer, flush=True)
                                for chunk in chunks:
                                    await text_queue.put(format_for_tts(chunk))
                            tool_in_progress = True
                            await text_queue.put(_TOOL_USE_SENTINEL)

                    elif event.type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if not delta:
                            continue
                        if getattr(delta, "type", "") == "text_delta":
                            text_piece = delta.text
                            buffer += text_piece
                            full_text += text_piece
                            # Intentar cortar en chunks
                            chunks, buffer = _chunk_text(buffer)
                            for chunk in chunks:
                                await text_queue.put(format_for_tts(chunk))

                final_msg = await stream.get_final_message()

            if hasattr(final_msg, "usage") and final_msg.usage:
                total_input_tokens += getattr(final_msg.usage, "input_tokens", 0)
                total_output_tokens += getattr(final_msg.usage, "output_tokens", 0)

            # Si hay tool_use, ejecutar y continuar
            if final_msg.stop_reason == "tool_use":
                tool_in_progress = False
                # Procesar tool_use blocks con el tool_runner de Anthropic
                # Construir tool_results manualmente ejecutando cada tool
                tool_results = []
                for block in final_msg.content:
                    if not hasattr(block, "type") or block.type != "tool_use":
                        continue
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id
                    print(f"  [tool_use streaming] {tool_name}")
                    # Buscar la tool en _mcp_tools
                    tool_fn = next((t for t in _mcp_tools if t.name == tool_name), None)
                    if tool_fn is None:
                        result_content = f"Error: tool '{tool_name}' not found"
                    else:
                        try:
                            result = await tool_fn(**tool_input)
                            result_content = str(result)
                            print(f"  [tool_result] {result_content[:200]}")
                        except Exception as e:
                            result_content = f"Error ejecutando {tool_name}: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_content,
                    })

                # Actualizar messages para la siguiente iteración
                messages = list(history[:-1]) + [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": final_msg.content},
                    {"role": "user", "content": tool_results},
                ]
                continue  # siguiente iteración del while con tool results

            else:
                # end_turn: respuesta completa
                break

        # Flush del buffer restante
        if buffer.strip():
            chunks, _ = _chunk_text(buffer, flush=True)
            for chunk in chunks:
                await text_queue.put(format_for_tts(chunk))

        # Actualizar historial con respuesta completa
        history.append({"role": "assistant", "content": final_msg.content})

        t_total = time.perf_counter() - t_start
        print(f"[Agente streaming] Completado ({t_total:.2f}s) tokens in={total_input_tokens} out={total_output_tokens}")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"[Agente streaming] Error: {e}")
        await text_queue.put("Lo siento, hubo un error procesando tu solicitud.")
    finally:
        await text_queue.put(None)  # sentinel de fin

    return total_input_tokens, total_output_tokens
```

- [ ] **Step 4: Ejecutar tests**

```bash
cd /home/sebas/mcp-call
python -m pytest tests/test_agent_streaming.py tests/test_chunker.py -v
```
Esperado: todos `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_agent_streaming.py
git commit -m "feat: add get_agent_response_streaming with sentence chunker"
```

---

## Task 4: `send_tts_streaming` en `main.py`

**Files:**
- Modify: `main.py` (agregar función después de `send_thinking_tone`)

- [ ] **Step 1: Implementar `send_tts_streaming`**

Agregar después de `send_thinking_tone` en `main.py`:

```python
async def send_tts_streaming(
    websocket: WebSocket,
    stream_sid: str | None,
    text_queue: asyncio.Queue,
    stop: asyncio.Event | None = None,
    thinking_stop: asyncio.Event | None = None,
) -> int:
    """Consume chunks de text_queue y los convierte a TTS streaming hacia Twilio.

    Detiene el tono de pensamiento cuando llega el primer chunk de texto.
    Soporta barge-in via stop event.

    Returns:
        Total de caracteres enviados a TTS (para billing).
    """
    if not stream_sid:
        return 0

    total_chars = 0
    thinking_stopped = False

    while True:
        try:
            item = await asyncio.wait_for(text_queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[send_tts_streaming] Timeout esperando chunk")
            break

        if item is None:
            # Sentinel de fin
            break

        if item == "__TOOL_USE__":
            # Claude está ejecutando tool; el tono de pensamiento ya está corriendo
            continue

        if stop and stop.is_set():
            # Barge-in: drenar la queue y salir
            while not text_queue.empty():
                text_queue.get_nowait()
            break

        # Primer chunk de texto real: detener tono de pensamiento
        if not thinking_stopped and thinking_stop:
            thinking_stop.set()
            thinking_stopped = True

        chunk_text = item
        total_chars += len(chunk_text)

        try:
            audio_gen = elevenlabs.text_to_speech.stream(
                text=chunk_text,
                voice_id=os.environ["ELEVENLABS_VOICE_ID"],
                model_id="eleven_turbo_v2_5",
                output_format="ulaw_8000",
            )
            for audio_chunk in audio_gen:
                if stop and stop.is_set():
                    break
                if not audio_chunk:
                    continue
                await websocket.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(audio_chunk).decode()},
                })
        except Exception as e:
            print(f"[send_tts_streaming] Error TTS chunk: {e}")
            # Fallback: gTTS para este chunk
            try:
                audio_bytes = await asyncio.to_thread(_gtts_to_mulaw, chunk_text)
                for i in range(0, len(audio_bytes), CHUNK_BYTES):
                    if stop and stop.is_set():
                        break
                    chunk = audio_bytes[i: i + CHUNK_BYTES]
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": base64.b64encode(chunk).decode()},
                    })
            except Exception as e2:
                print(f"[send_tts_streaming] Fallback gTTS falló: {e2}")

    # Enviar mark de fin si no hubo barge-in
    if not (stop and stop.is_set()):
        try:
            await websocket.send_json({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "tts_end"},
            })
        except Exception:
            pass

    return total_chars
```

- [ ] **Step 2: Verificar sintaxis**

```bash
cd /home/sebas/mcp-call
python -c "import main; print('OK')"
```
Esperado: `OK` sin errores.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add send_tts_streaming with barge-in support"
```

---

## Task 5: Reescribir `handle_speech` en `main.py`

**Files:**
- Modify: `main.py:304-335` (función `handle_speech`)

- [ ] **Step 1: Actualizar imports en `main.py`**

Verificar que al inicio de `main.py` esté el import de `get_agent_response_streaming`:

```python
from agent import close_mcp, get_agent_response, get_agent_response_streaming, init_mcp
```

- [ ] **Step 2: Reemplazar la función `handle_speech`**

Reemplazar la función `handle_speech` completa (líneas ~304-335) con:

```python
    async def handle_speech(captured: bytes, gen: int):
        """Transcribe, consulta al agente y responde con TTS streaming."""
        nonlocal session
        try:
            session.stt_audio_seconds += len(captured) / 8000.0

            user_text = await transcribe(captured)
            if not user_text or len(user_text.strip()) < 3:
                return
            print(f"Usuario: {user_text}")

            if not ws_open or not stream_sid:
                return

            # ── Feedback inmediato: tono local + "un momento" en background ──
            thinking_stop = asyncio.Event()
            tts_stop.clear()
            playing.set()

            # Lanzar tono de pensamiento local (instantáneo, sin red)
            thinking_task = asyncio.create_task(
                send_thinking_tone(websocket, stream_sid, thinking_stop)
            )

            # Lanzar TTS de "un momento" en background mientras Claude procesa
            async def play_um():
                """Genera y reproduce 'un momento' via TTS en background."""
                try:
                    um_bytes = await asyncio.to_thread(
                        lambda: b"".join(elevenlabs.text_to_speech.convert(
                            text="Un momento.",
                            voice_id=os.environ["ELEVENLABS_VOICE_ID"],
                            model_id="eleven_turbo_v2_5",
                            output_format="ulaw_8000",
                        ))
                    )
                    # Solo reproducir si el tono de pensamiento aún no fue detenido
                    # (es decir, el agente no respondió antes de que esto termine)
                    if not thinking_stop.is_set() and not tts_stop.is_set():
                        thinking_stop.set()  # detener tono local
                        await asyncio.sleep(0.05)  # pequeño gap
                        for i in range(0, len(um_bytes), CHUNK_BYTES):
                            if tts_stop.is_set():
                                break
                            chunk = um_bytes[i: i + CHUNK_BYTES]
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(chunk).decode()},
                            })
                        thinking_stop.set()  # asegurar que está seteado
                except Exception as e:
                    print(f"[un momento TTS] Error: {e}")
                    thinking_stop.set()

            um_task = asyncio.create_task(play_um())

            # ── Lanzar agente streaming ──
            text_queue: asyncio.Queue = asyncio.Queue()
            agent_task = asyncio.create_task(
                get_agent_response_streaming(user_text, conversation_history, text_queue)
            )

            # ── Consumir chunks y enviar TTS streaming ──
            session.tts_chars += await send_tts_streaming(
                websocket, stream_sid, text_queue,
                stop=tts_stop,
                thinking_stop=thinking_stop,
            )

            # Obtener tokens del agente
            try:
                in_tok, out_tok = await asyncio.wait_for(agent_task, timeout=60.0)
                session.claude_input_tokens += in_tok
                session.claude_output_tokens += out_tok
            except asyncio.TimeoutError:
                print("[handle_speech] Timeout esperando agente")
            except asyncio.CancelledError:
                pass

            # Cancelar tareas de feedback si aún corren
            for t in (thinking_task, um_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        except asyncio.CancelledError:
            print("[barge-in] Respuesta del agente cancelada por el usuario")
            raise
        except Exception as e:
            print(f"Error en handle_speech: {e}")
        finally:
            playing.clear()
            if task_gen == gen:
                busy.clear()
```

- [ ] **Step 3: Verificar sintaxis y arranque**

```bash
cd /home/sebas/mcp-call
python -c "import main; print('OK')"
```
Esperado: `OK`.

- [ ] **Step 4: Ejecutar todos los tests**

```bash
cd /home/sebas/mcp-call
python -m pytest tests/ -v
```
Esperado: todos `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: rewrite handle_speech with streaming pipeline and thinking tone"
```

---

## Task 6: Smoke test manual y ajuste de barge-in

**Files:**
- Modify: `main.py` (ajuste de `BARGEIN_THRESHOLD` si necesario)

- [ ] **Step 1: Arrancar el servidor localmente**

```bash
cd /home/sebas/mcp-call
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Verificar en logs: `[MCP] Total herramientas cargadas: N` sin errores.

- [ ] **Step 2: Verificar logs de latencia**

Al hacer una llamada de prueba, verificar en logs:
- `[Agente streaming] Pregunta:` aparece rápido
- `[tool_use streaming]` aparece con SQL query
- Chunks de texto aparecen antes de que la respuesta completa esté lista
- `[Agente streaming] Completado (X.XXs)` al final

- [ ] **Step 3: Verificar que el tono suena y se detiene**

En logs debe aparecer:
```
[un momento TTS] ...  (o que thinking_stop se setea antes)
```
Si el tono de pensamiento suena demasiado largo, reducir el timeout de `play_um` o ajustar el delay.

- [ ] **Step 4: Commit final**

```bash
git add main.py
git commit -m "chore: smoke test adjustments for streaming pipeline"
```

---

## Resumen de cambios

| Archivo | Qué cambia |
|---|---|
| `main.py` | `SILENCE_CHUNKS` 70→50, nuevo `send_thinking_tone`, nuevo `send_tts_streaming`, `handle_speech` reescrita, import actualizado |
| `agent.py` | Nuevo `_chunk_text`, `_CHUNK_*` constantes, `_TOOL_USE_SENTINEL`, `get_agent_response_streaming` |
| `tests/test_chunker.py` | Nuevo — 8 tests del chunker |
| `tests/test_agent_streaming.py` | Nuevo — test de streaming con mocks |
