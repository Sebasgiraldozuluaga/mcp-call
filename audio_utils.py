"""
Utilidades de audio: decodificación μ-law, VAD y conversión a WAV.
Twilio Media Streams entrega audio en formato G.711 μ-law, 8 kHz, mono.
"""
import io
import wave
import numpy as np


def mulaw_decode(mulaw_bytes: bytes) -> bytes:
    """Decodifica bytes μ-law de 8 bits a PCM lineal de 16 bits."""
    mulaw = np.frombuffer(mulaw_bytes, dtype=np.uint8).astype(np.int32)
    mulaw = ~mulaw & 0xFF
    sign = (mulaw >> 7) & 1
    exponent = (mulaw >> 4) & 0x07
    mantissa = mulaw & 0x0F
    magnitude = ((mantissa << 1) | 1) << (exponent + 2)
    samples = np.where(sign == 1, -magnitude, magnitude).astype(np.int16)
    return samples.tobytes()


def compute_rms(pcm_bytes: bytes) -> float:
    """Calcula la energía RMS de muestras PCM de 16 bits (usado para VAD)."""
    if len(pcm_bytes) < 2:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples ** 2)))


def mulaw_to_wav(mulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """Convierte bytes μ-law crudos a un archivo WAV (PCM 16 bits) en memoria."""
    pcm = mulaw_decode(mulaw_bytes)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def mulaw_encode(pcm_bytes: bytes) -> bytes:
    """Codifica PCM de 16 bits con signo a G.711 μ-law de 8 bits."""
    BIAS = 132   # 0x84
    CLIP = 32635

    s = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.int32)
    sign = (s < 0).astype(np.uint8) * 0x80
    s = np.clip(np.abs(s), 0, CLIP) + BIAS

    # Exponente: bits necesarios para representar s, menos 3, acotado a [0, 7]
    exp = np.floor(np.log2(s.astype(np.float32))).astype(np.int32) - 3
    exp = np.clip(exp, 0, 7)

    mantissa = (s >> (exp + 3)) & 0x0F
    encoded = (~(sign | (exp << 4) | mantissa)).astype(np.uint8)
    return encoded.tobytes()
