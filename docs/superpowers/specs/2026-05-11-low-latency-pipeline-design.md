# Design: Pipeline de Baja Latencia para Agente de Voz

**Fecha:** 2026-05-11  
**Proyecto:** mcp-call — Agente de voz ISERV (Twilio + ElevenLabs + Claude)

---

## Problema

El agente tarda 7–11 segundos en silencio absoluto entre que el usuario termina de hablar y escucha la primera respuesta. Esto provoca que los usuarios crean que la llamada se cortó y interrumpan o cuelguen.

**Desglose de latencia actual:**
| Etapa | Tiempo |
|---|---|
| VAD (silencio antes de procesar) | 1.4s |
| STT ElevenLabs Scribe | ~1–1.5s |
| Claude turno 1 (decide tool) | ~1–2s |
| SQL via MCP Postgres | ~0.5–2s |
| Claude turno 2 (genera respuesta) | ~1–2s |
| TTS ElevenLabs (genera todo antes de enviar) | ~1–2s |
| **Total percibido** | **~7–11s de silencio** |

Adicionalmente, `generate_thinking_tone` en `audio_utils.py` existe pero nunca se usa.

---

## Solución: Pipeline de Baja Latencia (Opción C)

### Arquitectura

```
Usuario habla
    │
    ▼ VAD: 1.0s silencio (reducido de 1.4s)
STT (ElevenLabs Scribe)
    │
    ▼
[<10ms] tono do-mi-sol local (generate_thinking_tone)
    +   TTS "un momento..." lanzado en background (ElevenLabs)
    │
    ▼
Claude streaming con tools
    │
    ├─ tool_use detectado → tono do-mi-sol en loop mientras espera SQL
    │
    ▼
Chunker de oraciones (stream de texto → segmentos para TTS)
    │  Corte en: . ? ! con ≥15 chars  |  , ; con ≥30 chars
    │
    ▼ chunk 1 ──► TTS streaming ElevenLabs ──► Twilio (audio inmediato)
    ▼ chunk 2 ──► TTS streaming ...
```

### Latencia percibida esperada

| Evento | Antes | Después |
|---|---|---|
| Usuario termina de hablar | 1.4s VAD | 1.0s VAD |
| Primer sonido al usuario | ~7–11s | <0.1s (tono local) |
| "Un momento" en voz | nunca | ~1s |
| Primera oración de respuesta | ~7–11s total | ~3–4s total |

---

## Cambios por archivo

### `main.py`

1. **`SILENCE_CHUNKS`**: `70 → 50` (1.4s → 1.0s de silencio VAD)

2. **`handle_speech`** — nuevo flujo:
   - Tras STT exitoso: reproducir tono local inmediatamente (síncrono, local)
   - Lanzar TTS de "un momento..." en background (task asyncio)
   - Llamar `get_agent_response_streaming()` que produce chunks via Queue
   - Consumidor de Queue: `send_tts_streaming()` envía a Twilio en tiempo real
   - Barge-in sigue funcionando: revisa `tts_stop` entre chunks

3. **`send_tts_streaming()`** — nuevo helper:
   - Recibe `asyncio.Queue[str | None]` (None = sentinel de fin)
   - Para cada chunk de texto: llama `elevenlabs.text_to_speech.stream()`
   - Envía bytes a Twilio conforme llegan (sin `b"".join`)
   - Revisa `tts_stop` antes de cada chunk de audio

### `agent.py`

4. **`get_agent_response_streaming(user_text, history, text_queue)`** — nueva función:
   - Usa `async_client.messages.stream()` con tools
   - Al detectar `tool_use`: pone sentinel especial en queue para activar tono de espera
   - Acumula texto en buffer, detecta cortes:
     - `.` `?` `!` con buffer ≥ 15 chars → flush chunk
     - `,` `;` con buffer ≥ 30 chars → flush chunk
   - Al finalizar: flush del buffer restante + `None` sentinel
   - Actualiza `history` y retorna `(input_tokens, output_tokens)`

---

## Reglas de corte de oraciones (Chunker)

```python
CHUNK_HARD_PUNCT = frozenset('.?!')   # corte fuerte, mínimo 15 chars
CHUNK_SOFT_PUNCT = frozenset(',;')    # corte suave, mínimo 30 chars
CHUNK_MIN_HARD   = 15
CHUNK_MIN_SOFT   = 30
```

El chunker no corta dentro de números (`$1.234`) gracias a que `format_for_tts` ya los convierte a palabras antes de enviar a TTS. El texto que va a TTS ya está pre-procesado.

---

## Compatibilidad

- `get_agent_response()` se mantiene sin cambios para no romper nada existente.
- `generate_thinking_tone()` en `audio_utils.py` ya existe; solo se conecta.
- El barge-in (`tts_stop`, `playing`, `bargein_count`) sigue funcionando igual.

---

## Lo que NO cambia

- System prompt de Claude
- STT pipeline (ElevenLabs Scribe)
- Correcciones post-STT (`_STT_CORRECTIONS`)
- Lógica de sesión, costos, Telegram summary
- MCP Postgres connection

---

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Chunk cortado en medio de número | `format_for_tts` convierte números a palabras antes de TTS |
| "Un momento" se superpone con respuesta | La task de "un momento" se cancela si el agente responde antes de que termine |
| TTS streaming más lento en latencia inicial | `eleven_turbo_v2_5` tiene <400ms de TTFB en streaming; aceptable |
| Barge-in durante tono de pensamiento | Tono local se corta igual con `tts_stop` |
