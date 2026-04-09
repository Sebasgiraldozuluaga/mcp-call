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
import os

# load_dotenv() DEBE ir antes de importar módulos locales que lean os.environ
from dotenv import load_dotenv
load_dotenv()

import httpx
from contextlib import asynccontextmanager
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from agent import close_mcp, get_agent_response, init_mcp
from audio_utils import compute_rms, mulaw_decode, mulaw_encode, mulaw_to_wav

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
# Parámetros de VAD (Voice Activity Detection)
# ---------------------------------------------------------------------------
SILENCE_THRESHOLD = 400   # Energía RMS por debajo de este valor = silencio
SILENCE_CHUNKS = 75       # 75 chunks × 20 ms = 1.5 s de silencio → procesar
MIN_SPEECH_CHUNKS = 10    # Ignorar buffers < 200 ms (ruido)
CHUNK_BYTES = 160         # 160 bytes = 20 ms a 8 kHz μ-law

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranca el MCP al iniciar y lo cierra al apagar."""
    await init_mcp()
    yield
    await close_mcp()

app = FastAPI(title="Call MCP Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints HTTP
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "Call MCP Agent corriendo"}


@app.post("/call")
async def start_call():
    """Inicia una llamada saliente a YOUR_PHONE_NUMBER."""
    server_url = os.environ["SERVER_URL"]
    call = twilio.calls.create(
        url=f"{server_url}/twiml",
        to=os.environ["YOUR_PHONE_NUMBER"],
        from_=os.environ["TWILIO_PHONE_NUMBER"],
    )
    print(f"Llamada iniciada: {call.sid}")
    return JSONResponse({"call_sid": call.sid, "status": call.status})


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_response():
    """Devuelve TwiML que conecta la llamada a nuestro WebSocket."""
    server_url = os.environ["SERVER_URL"]
    ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"{ws_url}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


# ---------------------------------------------------------------------------
# WebSocket: pipeline de audio en tiempo real
# ---------------------------------------------------------------------------
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket conectado")

    stream_sid: str | None = None
    audio_buffer = bytearray()
    conversation_history: list = []
    silent_chunks = 0
    speaking = False
    busy = asyncio.Event()  # Evita procesar mientras el agente responde

    async def handle_speech(captured: bytes):
        """Transcribe, consulta al agente y responde con TTS."""
        try:
            user_text = await transcribe(captured)
            if not user_text or len(user_text.strip()) < 3:
                return
            print(f"Usuario: {user_text}")
            response_text = await get_agent_response(user_text, conversation_history)
            print(f"Agente:  {response_text}")
            await send_tts(websocket, stream_sid, response_text)
        except Exception as e:
            print(f"Error en handle_speech: {e}")
        finally:
            busy.clear()

    try:
        async for raw in websocket.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            # ── Inicio del stream ──────────────────────────────────────────
            if event == "start":
                stream_sid = data["start"]["streamSid"]
                print(f"Stream iniciado: {stream_sid}")
                await send_tts(
                    websocket, stream_sid,
                    "Hola! Soy tu asistente con acceso a la base de datos. ¿En qué te puedo ayudar?"
                )

            # ── Chunks de audio entrante ───────────────────────────────────
            elif event == "media" and not busy.is_set():
                mulaw_chunk = base64.b64decode(data["media"]["payload"])
                pcm_chunk = mulaw_decode(mulaw_chunk)
                rms = compute_rms(pcm_chunk)

                if rms > SILENCE_THRESHOLD:
                    # El usuario está hablando
                    speaking = True
                    silent_chunks = 0
                    audio_buffer.extend(mulaw_chunk)

                elif speaking:
                    # Silencio tras voz detectada
                    silent_chunks += 1
                    audio_buffer.extend(mulaw_chunk)

                    if silent_chunks >= SILENCE_CHUNKS:
                        if len(audio_buffer) > MIN_SPEECH_CHUNKS * CHUNK_BYTES:
                            # Audio válido → procesar
                            captured = bytes(audio_buffer)
                            audio_buffer.clear()
                            speaking = False
                            silent_chunks = 0
                            busy.set()
                            asyncio.create_task(handle_speech(captured))
                        else:
                            # Demasiado corto → descartar
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
        print("WebSocket desconectado")


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


async def send_tts(websocket: WebSocket, stream_sid: str | None, text: str):
    """Convierte texto a voz y lo envía a Twilio en formato μ-law.

    Intenta ElevenLabs primero; si falla (plan free / créditos), usa gTTS.
    """
    if not stream_sid or not text:
        return
    try:
        # ElevenLabs genera directamente en ulaw_8000 (compatible con Twilio)
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

    # Enviar en chunks de 20 ms
    for i in range(0, len(audio_bytes), CHUNK_BYTES):
        chunk = audio_bytes[i: i + CHUNK_BYTES]
        await websocket.send_json({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode()},
        })

    # Marcar fin para saber cuándo termina la reproducción
    await websocket.send_json({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": "tts_end"},
    })


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe audio μ-law usando ElevenLabs Scribe (STT)."""
    wav_bytes = mulaw_to_wav(audio_bytes)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"model_id": "scribe_v1", "language_code": "es"},
        )
        response.raise_for_status()
        return response.json().get("text", "")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
