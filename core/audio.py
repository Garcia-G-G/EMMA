"""Microphone capture, energy-based VAD, and playback via sounddevice.

All audio in this module is 16 kHz mono signed-int16 PCM. Wake word
(Porcupine) and Whisper both expect that format; ElevenLabs is requested
at the same rate so playback can be a straight passthrough.
"""
from __future__ import annotations

import asyncio
import io
import math
import time
import wave
from collections.abc import AsyncIterator
from typing import Final

import numpy as np
import sounddevice as sd
import structlog

from config.settings import settings

log = structlog.get_logger("emma.audio")

SAMPLE_RATE: Final = 16000
FRAME_DURATION_MS: Final = 32
FRAME_SAMPLES: Final = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 512 samples


async def mic_stream() -> AsyncIterator[bytes]:
    """Yield 16 kHz mono int16 PCM frames from the default input device."""
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))  # type: ignore[arg-type]

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        callback=_cb,
    )
    stream.start()
    try:
        while True:
            yield await queue.get()
    finally:
        stream.stop()
        stream.close()


async def capture_until_silence() -> bytes:
    """Open the mic, capture until ~`VAD_SILENCE_MS` of silence, return PCM."""
    buf = bytearray()
    silent_ms = 0
    speech_started = False
    deadline = time.monotonic() + settings.VAD_MAX_UTTERANCE_S

    gen = mic_stream()
    try:
        async for frame in gen:
            if time.monotonic() > deadline:
                log.info("vad_timeout")
                break
            buf.extend(frame)
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
            rms = math.sqrt(float(np.mean(samples * samples))) if samples.size else 0.0
            if rms > settings.VAD_ENERGY_THRESHOLD:
                speech_started = True
                silent_ms = 0
            else:
                silent_ms += FRAME_DURATION_MS
            if speech_started and silent_ms >= settings.VAD_SILENCE_MS:
                break
    finally:
        await gen.aclose()
    return bytes(buf) if speech_started else b""


async def play_audio_stream(chunks: AsyncIterator[bytes]) -> None:
    """Stream 16 kHz mono int16 PCM chunks straight to the output device."""
    stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=0,
    )
    stream.start()
    try:
        async for chunk in chunks:
            if not chunk:
                continue
            await asyncio.to_thread(stream.write, chunk)
    finally:
        # Drain briefly so the tail of the utterance isn't cut off.
        await asyncio.to_thread(time.sleep, 0.05)
        stream.stop()
        stream.close()


def play_tone(freq_hz: float = 880.0, duration_ms: int = 150, volume: float = 0.18) -> None:
    """Play a short non-blocking sine pip used as wake acknowledgment."""
    n = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    fade = np.minimum(np.linspace(0.0, 1.0, n), np.linspace(1.0, 0.0, n))
    waveform = (np.sin(2 * np.pi * freq_hz * t) * fade * volume * 32767).astype(np.int16)
    sd.play(waveform, SAMPLE_RATE, blocking=False)


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw 16 kHz mono int16 PCM in a WAV container (for Whisper API)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()
