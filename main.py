"""
Servidor principal: FastAPI + Twilio Media Streams + ElevenLabs TTS/STT.

Flujo:
  POST /call  →  Twilio llama a tu teléfono
  GET  /twiml →  TwiML conecta la llamada al WebSocket
  WS   /media-stream → pipeline: audio μ-law → STT → Claude → TTS → audio μ-law
"""
import asyncio
import base64
import json
import math
import os
import re
from dataclasses import dataclass, field

# load_dotenv() DEBE ir antes de importar módulos locales que lean os.environ
from dotenv import load_dotenv
load_dotenv()

import httpx
from contextlib import asynccontextmanager
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from agent import close_mcp, get_agent_response, init_mcp
from audio_utils import compute_rms, generate_thinking_tone, mulaw_decode, mulaw_encode, mulaw_to_wav

# ---------------------------------------------------------------------------
# Clientes externos
# ---------------------------------------------------------------------------
_account_sid = os.environ["TWILIO_ACCOUNT_SID"]
_auth_token = os.environ["TWILIO_AUTH_TOKEN"]

# Twilio acepta tanto Account SID (AC...) como API Key SID (SK...) + Secret
if _account_sid.startswith("SK"):
    # API Key auth: necesita TWILIO_ACCOUNT_SID_REAL (AC...) en el .env
    _real_sid = os.environ.get("TWILIO_ACCOUNT_SID_REAL", "")
    twilio = TwilioClient(_account_sid, _auth_token, _real_sid)
else:
    twilio = TwilioClient(_account_sid, _auth_token)

elevenlabs = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])

# ---------------------------------------------------------------------------
# Sesión por llamada: trackea tokens y costos
# ---------------------------------------------------------------------------
@dataclass
class CallSession:
    chat_id: int | None
    phone_number: str
    call_sid: str = ""
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    tts_chars: int = 0
    stt_audio_seconds: float = 0.0
    call_duration_seconds: float = 0.0  # duración real de la llamada

# call_sid → CallSession (se llena al iniciar la llamada)
_pending_calls: dict[str, CallSession] = {}

# Precios aproximados en USD
_CLAUDE_INPUT_PRICE  = 3.00 / 1_000_000   # por token
_CLAUDE_OUTPUT_PRICE = 15.00 / 1_000_000  # por token
_TTS_PRICE_PER_CHAR  = 0.15 / 1_000       # ElevenLabs turbo_v2_5
_STT_PRICE_PER_SEC   = 0.40 / 3_600       # ElevenLabs Scribe v1 ($0.40/hora)
# Twilio cobra por minuto redondeado hacia arriba; varía por destino.
# Colombia móvil ≈ $0.048/min, Colombia fijo ≈ $0.024/min.
# Usamos $0.048/min como estimado conservador para celulares.
_TWILIO_PRICE_PER_SEC = 0.048 / 60        # $0.048/min → por segundo


def _md_float(value: float, fmt: str = ".4f") -> str:
    """Escapa el punto decimal de un float para MarkdownV2 ('.' → '\\.')."""
    return format(value, fmt).replace(".", "\\.")


async def _send_call_summary(session: CallSession) -> None:
    """Envía resumen de tokens y costo estimado al chat de Telegram que inició la llamada."""
    print(f"[Telegram] chat_id={session.chat_id} telegram_app={telegram_app is not None}")
    if not session.chat_id or not telegram_app:
        return

    input_tokens  = session.claude_input_tokens
    output_tokens = session.claude_output_tokens
    tts_chars     = session.tts_chars
    stt_seconds   = session.stt_audio_seconds
    twilio_mins   = math.ceil(session.call_duration_seconds / 60)
    twilio_secs   = session.call_duration_seconds

    cost_input  = input_tokens  * _CLAUDE_INPUT_PRICE
    cost_output = output_tokens * _CLAUDE_OUTPUT_PRICE
    cost_tts    = tts_chars     * _TTS_PRICE_PER_CHAR
    cost_stt    = stt_seconds   * _STT_PRICE_PER_SEC
    cost_twilio = twilio_mins   * 0.048
    total       = cost_input + cost_output + cost_tts + cost_stt + cost_twilio

    mins_str = str(twilio_mins).replace("-", "\\-")
    secs_str = f"{twilio_secs:.0f}".replace("-", "\\-")

    msg = (
        f"📞 *Llamada finalizada* — `{session.phone_number}`\n\n"
        f"🤖 *Claude Sonnet 4\\.6*\n"
        f"  • Input: `{input_tokens:,}` tokens \\(\\~\\${_md_float(cost_input)}\\)\n"
        f"  • Output: `{output_tokens:,}` tokens \\(\\~\\${_md_float(cost_output)}\\)\n\n"
        f"🎤 *ElevenLabs STT \\(Scribe v1\\)*\n"
        f"  • Audio: `{_md_float(stt_seconds, '.1f')}s` \\(\\~\\${_md_float(cost_stt)}\\)\n\n"
        f"🔊 *ElevenLabs TTS \\(Turbo v2\\.5\\)*\n"
        f"  • Caracteres: `{tts_chars:,}` \\(\\~\\${_md_float(cost_tts)}\\)\n\n"
        f"📱 *Twilio \\(llamada saliente Colombia móvil\\)*\n"
        f"  • Duración: `{secs_str}s` → {mins_str} min facturado \\(\\~\\${_md_float(cost_twilio)}\\)\n\n"
        f"💰 *Costo total estimado: \\~\\${_md_float(total)} USD*"
    )
    try:
        await telegram_app.bot.send_message(
            chat_id=session.chat_id,
            text=msg,
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        print(f"[Telegram] Error enviando resumen: {e}")


# ---------------------------------------------------------------------------
# Telegram Bot — solo Sebastian puede usar /call
# ---------------------------------------------------------------------------
TELEGRAM_ALLOWED_ID = int(os.environ.get("TELEGRAM_ALLOWED_ID", "0"))

telegram_app: Application | None = None


async def _cmd_call(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja /call [número] desde Telegram. Ejemplo: /call +573001234567"""
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ALLOWED_ID:
        await update.message.reply_text("No tienes permiso para usar este comando.")
        return

    # Determinar número destino
    if context.args:
        phone_number = context.args[0]
    else:
        phone_number = os.environ.get("YOUR_PHONE_NUMBER", "")
        if not phone_number:
            await update.message.reply_text(
                "Uso: /call +573001234567\nO configura YOUR_PHONE_NUMBER en el entorno."
            )
            return

    try:
        server_url = os.environ["SERVER_URL"]
        call = twilio.calls.create(
            url=f"{server_url}/twiml",
            to=phone_number,
            from_=os.environ["TWILIO_PHONE_NUMBER"],
        )
        _pending_calls[call.sid] = CallSession(
            chat_id=update.effective_chat.id,
            phone_number=phone_number,
            call_sid=call.sid,
        )
        await update.message.reply_text(f"Llamando a {phone_number}... ({call.sid})")
    except Exception as e:
        await update.message.reply_text(f"Error al iniciar la llamada: {e}")


# ---------------------------------------------------------------------------
# Parámetros de VAD (Voice Activity Detection)
# ---------------------------------------------------------------------------
SILENCE_THRESHOLD = 500   # RMS para detectar voz
BARGEIN_THRESHOLD = 1200  # RMS para barge-in (solo voz clara interrumpe)
BARGEIN_CONFIRM = 2       # Chunks consecutivos para confirmar barge-in (40 ms)
SILENCE_CHUNKS = 50       # 50 × 20 ms = 1.0 s de silencio → procesar
MIN_SPEECH_CHUNKS = 12    # Ignorar buffers < 240 ms (ruido / golpes)
CHUNK_BYTES = 160         # 160 bytes = 20 ms a 8 kHz μ-law

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranca el MCP y el bot de Telegram al iniciar; los cierra al apagar."""
    global telegram_app

    await init_mcp()

    # Iniciar Telegram bot si hay token configurado
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tg_token:
        telegram_app = (
            Application.builder()
            .token(tg_token)
            .build()
        )
        telegram_app.add_handler(CommandHandler("call", _cmd_call))
        await telegram_app.initialize()
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        print("[Telegram] Bot iniciado en modo polling.")
    else:
        print("[Telegram] TELEGRAM_BOT_TOKEN no configurado, bot desactivado.")

    yield

    await close_mcp()
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

app = FastAPI(title="Call MCP Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints HTTP
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "Call MCP Agent corriendo"}


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Recibe updates de Telegram y los procesa."""
    if not telegram_app:
        return JSONResponse({"ok": False})
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})


@app.post("/call")
async def start_call():
    """Inicia una llamada saliente a YOUR_PHONE_NUMBER."""
    server_url = os.environ["SERVER_URL"]
    phone_number = os.environ["YOUR_PHONE_NUMBER"]
    call = twilio.calls.create(
        url=f"{server_url}/twiml",
        to=phone_number,
        from_=os.environ["TWILIO_PHONE_NUMBER"],
    )
    _pending_calls[call.sid] = CallSession(
        chat_id=TELEGRAM_ALLOWED_ID,
        phone_number=phone_number,
        call_sid=call.sid,
    )
    print(f"Llamada iniciada: {call.sid}")
    return JSONResponse({"call_sid": call.sid, "status": call.status})


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_response(request: Request):
    """Devuelve TwiML que conecta la llamada a nuestro WebSocket."""
    server_url = os.environ["SERVER_URL"]
    ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")

    # Twilio envía CallSid en POST (form) o GET (query params)
    call_sid = ""
    if request.method == "POST":
        form = await request.form()
        call_sid = form.get("CallSid", "")
    if not call_sid:
        call_sid = request.query_params.get("CallSid", "")

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"{ws_url}/media-stream?call_sid={call_sid}")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


# ---------------------------------------------------------------------------
# WebSocket: pipeline de audio en tiempo real
# ---------------------------------------------------------------------------
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket, call_sid: str = Query("")):
    await websocket.accept()
    print("WebSocket conectado")

    # Recuperar o crear sesión vinculada a esta llamada
    print(f"[WebSocket] call_sid='{call_sid}' pending_keys={list(_pending_calls.keys())}")
    session: CallSession = _pending_calls.pop(call_sid, None) or CallSession(
        chat_id=TELEGRAM_ALLOWED_ID, phone_number=os.environ.get("YOUR_PHONE_NUMBER", "desconocido"), call_sid=call_sid
    )

    stream_sid: str | None = None
    call_start_time: float = 0.0
    audio_buffer = bytearray()
    conversation_history: list = []
    silent_chunks = 0
    speaking = False
    bargein_count = 0         # Chunks consecutivos fuertes durante TTS
    busy = asyncio.Event()    # True mientras procesa (STT + agente + TTS)
    playing = asyncio.Event() # True solo durante la reproducción de TTS
    tts_stop = asyncio.Event()# Señal para que send_tts corte el stream inmediatamente
    current_task: asyncio.Task | None = None
    task_gen = 0  # Contador de generación: evita que un finally obsoleto limpie busy
    ws_open = True            # False cuando el WebSocket se cierra

    async def handle_speech(captured: bytes, gen: int):
        """Transcribe, consulta al agente y responde con TTS."""
        nonlocal session
        try:
            session.stt_audio_seconds += len(captured) / 8000.0

            user_text = await transcribe(captured)
            if not user_text or len(user_text.strip()) < 3:
                return
            print(f"Usuario: {user_text}")

            response_text, in_tok, out_tok = await get_agent_response(user_text, conversation_history)
            session.claude_input_tokens += in_tok
            session.claude_output_tokens += out_tok
            print(f"Agente:  {response_text}")

            if not ws_open:
                return
            tts_stop.clear()
            playing.set()
            session.tts_chars += len(response_text)
            await send_tts(websocket, stream_sid, response_text, tts_stop)

        except asyncio.CancelledError:
            print("[barge-in] Respuesta del agente cancelada por el usuario")
            raise
        except Exception as e:
            print(f"Error en handle_speech: {e}")
        finally:
            playing.clear()
            if task_gen == gen:
                busy.clear()

    try:
        async for raw in websocket.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            # ── Inicio del stream ──────────────────────────────────────────
            if event == "start":
                stream_sid = data["start"]["streamSid"]
                # Twilio siempre incluye callSid en el evento start.
                # Si el WebSocket llegó con call_sid vacío, recuperamos la sesión real aquí.
                twilio_call_sid = data["start"].get("callSid", "")
                if twilio_call_sid and twilio_call_sid != session.call_sid:
                    recovered = _pending_calls.pop(twilio_call_sid, None)
                    if recovered:
                        session = recovered
                        print(f"[Sesión] Recuperada desde evento start: {twilio_call_sid}")
                call_start_time = asyncio.get_event_loop().time()
                print(f"Stream iniciado: {stream_sid}")
                greeting = "Hola! Soy tu asistente con acceso a la base de datos. ¿En qué te puedo ayudar?"
                session.tts_chars += len(greeting)
                await send_tts(websocket, stream_sid, greeting)

            # ── Chunks de audio entrante ───────────────────────────────────
            elif event == "media":
                mulaw_chunk = base64.b64decode(data["media"]["payload"])
                pcm_chunk = mulaw_decode(mulaw_chunk)
                rms = compute_rms(pcm_chunk)

                # ── Barge-in: usuario habla mientras el agente reproduce TTS ──
                if playing.is_set():
                    if rms > BARGEIN_THRESHOLD:
                        bargein_count += 1
                    else:
                        bargein_count = 0

                    if bargein_count >= BARGEIN_CONFIRM:
                        task_gen += 1
                        tts_stop.set()   # Detiene send_tts en el chunk actual
                        if current_task and not current_task.done():
                            current_task.cancel()
                        playing.clear()
                        busy.clear()
                        bargein_count = 0
                        if stream_sid:
                            await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                        print("[barge-in] Usuario interrumpió — escuchando nueva pregunta")
                        speaking = True
                        silent_chunks = 0
                        audio_buffer.clear()

                # ── VAD: captura voz cuando no estamos ocupados ni reproduciendo ──
                if not busy.is_set() and not playing.is_set():
                    if rms > SILENCE_THRESHOLD:
                        speaking = True
                        silent_chunks = 0
                        audio_buffer.extend(mulaw_chunk)

                    elif speaking:
                        silent_chunks += 1
                        audio_buffer.extend(mulaw_chunk)

                        if silent_chunks >= SILENCE_CHUNKS:
                            if len(audio_buffer) > MIN_SPEECH_CHUNKS * CHUNK_BYTES:
                                captured = bytes(audio_buffer)
                                audio_buffer.clear()
                                speaking = False
                                silent_chunks = 0
                                busy.set()
                                current_task = asyncio.create_task(
                                    handle_speech(captured, task_gen)
                                )
                            else:
                                audio_buffer.clear()
                                speaking = False
                                silent_chunks = 0

            # ── Fin del stream ─────────────────────────────────────────────
            elif event == "stop":
                print("Stream terminado")
                break

    except Exception as e:
        print(f"Error WebSocket: {e}")
    finally:
        ws_open = False
        if current_task and not current_task.done():
            current_task.cancel()
        if call_start_time:
            session.call_duration_seconds = asyncio.get_event_loop().time() - call_start_time
        print("WebSocket desconectado")
        print(
            f"[Sesión] Claude in={session.claude_input_tokens} out={session.claude_output_tokens} "
            f"| TTS chars={session.tts_chars} | STT s={session.stt_audio_seconds:.1f} "
            f"| duración={session.call_duration_seconds:.0f}s"
        )
        await _send_call_summary(session)


# ---------------------------------------------------------------------------
# Helpers: TTS y STT
# ---------------------------------------------------------------------------
def _gtts_to_mulaw(text: str) -> bytes:
    """Genera audio con gTTS (Google TTS, gratis) y lo convierte a μ-law 8 kHz."""
    import io as _io
    from gtts import gTTS
    from pydub import AudioSegment

    tts = gTTS(text=text, lang="es")
    mp3_buf = _io.BytesIO()
    tts.write_to_fp(mp3_buf)
    mp3_buf.seek(0)

    # MP3 → PCM 16 bits, 8 kHz, mono  (pydub usa ffmpeg internamente)
    audio = AudioSegment.from_mp3(mp3_buf)
    audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)
    return mulaw_encode(audio.raw_data)


async def send_tts(
    websocket: WebSocket,
    stream_sid: str | None,
    text: str,
    stop: asyncio.Event | None = None,
):
    """Convierte texto a voz y lo envía a Twilio en chunks de 20 ms.

    stop: evento que, al setearse, corta el envío inmediatamente (barge-in).
    """
    if not stream_sid or not text:
        return
    try:
        audio_gen = elevenlabs.text_to_speech.convert(
            text=text,
            voice_id=os.environ["ELEVENLABS_VOICE_ID"],
            model_id="eleven_turbo_v2_5",
            output_format="ulaw_8000",
        )
        audio_bytes = b"".join(audio_gen)
        print("TTS: ElevenLabs")
    except Exception as e:
        print(f"ElevenLabs no disponible ({type(e).__name__}), usando gTTS...")
        audio_bytes = await asyncio.to_thread(_gtts_to_mulaw, text)

    try:
        for i in range(0, len(audio_bytes), CHUNK_BYTES):
            # Revisar barge-in antes de cada chunk — corte inmediato
            if stop and stop.is_set():
                print("[barge-in] send_tts cortado en chunk", i // CHUNK_BYTES)
                return
            chunk = audio_bytes[i: i + CHUNK_BYTES]
            await websocket.send_json({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            })

        if not (stop and stop.is_set()):
            await websocket.send_json({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "tts_end"},
            })
    except Exception:
        pass


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



# Correcciones post-STT: mapea variantes fonéticas → término correcto.
# ElevenLabs confunde palabras por su sonido, no por su ortografía.
# Cada entrada: (regex_pattern, reemplazo) — se aplican en orden.
_STT_CORRECTIONS = [
    # nómina / nomina — se confunde con "mina", "nominal", "minima", "numero"
    (r'\b(no[mn]i?na[sz]?|nominas?|nomima[sz]?|n[oó]minas?)\b', 'nómina'),
    # quincena — se confunde con "quincenal", "quincenera"
    (r'\bquincen[ae]r?a?\b', 'quincena'),
    # factura/s — generalmente bien pero a veces "fractura"
    (r'\bfracturas\b', 'facturas'),
    (r'\bfractura\b', 'factura'),
    # cotización — se confunde con "comunicación", "cotisación"
    (r'\bcoti[sz]aci[oó]n\b', 'cotización'),
    # requerimiento — se confunde con "requerimentos", "requerimineto"
    (r'\brequeri?mi?en?tos\b', 'requerimientos'),
    (r'\brequeri?mi?en?to\b', 'requerimiento'),
    # presupuesto — relativamente estable
    (r'\bpresupues?tos?\b', 'presupuesto'),
    # proveedor — se confunde con "proveerdor"
    (r'\bproveerdor\b', 'proveedor'),
    # Proyectos con nombres fonéticamente difíciles
    (r'\b(hause[sz]|house[sz]|haus)\b', 'Houzez'),
    (r'\b(faun[ao]|farna|fauna)\b', 'Fauna'),
    (r'\b(sia[ck]|saika|cítrica|citrica|citrika)\b', 'Citrika'),
    (r'\b(yagua|jagua|chagua|jagüa)\b', 'Jaggua'),
    (r'\b(kiux|kiuks|quiux|kux)\b', 'Kyux'),
    (r'\b(siticen|zitizen|citizen|sitizen)\b', 'Zitizen'),
    (r'\b(piamon[nt]e?|piamonte)\b', 'Piamonte'),
    # orden de compra
    (r'\borden\s+de?\s+compras?\b', 'orden de compra'),
    # centro de costos
    (r'\bcentro\s+de?\s+costos?\b', 'centro de costos'),
]
_STT_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in _STT_CORRECTIONS]


def _fix_transcription(text: str) -> str:
    """Aplica correcciones fonéticas post-STT para términos del dominio ISERV."""
    for pattern, replacement in _STT_RE:
        text = pattern.sub(replacement, text)
    return text


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe audio μ-law usando ElevenLabs Scribe con corrección de dominio."""
    wav_bytes = mulaw_to_wav(audio_bytes)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"model_id": "scribe_v1", "language_code": "es"},
            )
            response.raise_for_status()
            text = response.json().get("text", "").strip()
            text = _fix_transcription(text)
            print(f"STT: {text}")
            return text
    except Exception as e:
        print(f"STT falló: {e}")
        return ""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
