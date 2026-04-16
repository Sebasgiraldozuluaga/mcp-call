"""
Utilidades de audio: decodificación μ-law, VAD y conversión a WAV.
Twilio Media Streams entrega audio en formato G.711 μ-law, 8 kHz, mono.
"""
import io
import wave
import numpy as np


def generate_thinking_tone(sample_rate: int = 8000) -> bytes:
    """Genera un tono de 'pensando' en formato μ-law 8 kHz.

    Produce tres notas suaves y ascendentes (do-mi-sol) que se usan
    como feedback auditivo mientras el agente procesa la consulta.
    """
    notes = [523, 659, 784]   # Do5, Mi5, Sol5 (Hz)
    note_dur = 0.12            # segundos por nota
    gap_dur  = 0.06            # silencio entre notas
    fade_samples = int(sample_rate * 0.015)  # fade-in/out de 15 ms

    segments: list[np.ndarray] = []
    for freq in notes:
        n = int(sample_rate * note_dur)
        t = np.linspace(0, note_dur, n, endpoint=False)
        # Onda senoidal suave con volumen al 40 %
        wave_pcm = (np.sin(2 * np.pi * freq * t) * 0.40 * 32767).astype(np.int16)
        # Fade-in y fade-out para evitar clicks
        fade = np.linspace(0, 1, fade_samples)
        wave_pcm[:fade_samples] = (wave_pcm[:fade_samples] * fade).astype(np.int16)
        wave_pcm[-fade_samples:] = (wave_pcm[-fade_samples:] * fade[::-1]).astype(np.int16)
        segments.append(wave_pcm)
        # Silencio entre notas
        segments.append(np.zeros(int(sample_rate * gap_dur), dtype=np.int16))

    pcm = np.concatenate(segments).tobytes()
    return mulaw_encode(pcm)


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
    """Convierte bytes μ-law 8 kHz a WAV PCM 16 bits a 16 kHz.

    El upsampleo 8 kHz → 16 kHz y la normalización de amplitud mejoran
    significativamente la precisión de los modelos STT, que están entrenados
    principalmente con audio a 16 kHz.
    """
    pcm = mulaw_decode(mulaw_bytes)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    # ── Upsampleo lineal 8 kHz → 16 kHz ──────────────────────────────────────
    n_original = len(samples)
    n_upsampled = n_original * 2
    samples = np.interp(
        np.linspace(0, n_original - 1, n_upsampled),
        np.arange(n_original),
        samples,
    )

    # ── Normalización de amplitud (hasta 85 % del rango) ─────────────────────
    peak = np.abs(samples).max()
    if peak > 0:
        samples = samples * (0.85 * 32767.0 / peak)

    pcm_out = samples.astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)   # ElevenLabs Scribe prefiere 16 kHz
        wf.writeframes(pcm_out)
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
