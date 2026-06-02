"""Microphone capture, energy-based VAD, and playback via sounddevice.

All audio in this module is 16 kHz mono signed-int16 PCM. Wake word
(openWakeWord) and Whisper both expect that format; ElevenLabs is
requested at the same rate so playback can be a straight passthrough.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Final

import numpy as np
import sounddevice as sd
import structlog

from config.settings import settings

log = structlog.get_logger("emma.audio")

SAMPLE_RATE: Final = 16000
FRAME_DURATION_MS: Final = 32
FRAME_SAMPLES: Final = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 512 samples


async def mic_stream() -> AsyncGenerator[bytes, None]:
    """Yield 16 kHz mono int16 PCM frames from the default input device."""
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))  # type: ignore[call-overload]

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
    """Open the mic, capture until ~`VAD_SILENCE_MS` of silence, return PCM.

    Returns empty bytes if no speech is detected within
    ``VAD_SPEECH_START_S`` (so a stray wake-word false-positive doesn't
    hold the mic open for the full ``VAD_MAX_UTTERANCE_S``).
    """
    buf = bytearray()
    silent_ms = 0
    speech_started = False
    start_time = time.monotonic()
    deadline = start_time + settings.VAD_MAX_UTTERANCE_S
    speech_deadline = start_time + settings.VAD_SPEECH_START_S

    gen = mic_stream()
    try:
        async for frame in gen:
            now = time.monotonic()
            if now > deadline:
                log.info("vad_timeout")
                break
            if not speech_started and now > speech_deadline:
                log.info("vad_no_speech_started")
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
    """Stream 16 kHz mono int16 PCM chunks straight to the output device.

    Cancellation-aware: on barge-in, ``CancelledError`` triggers
    ``stream.abort()`` which discards the buffered audio and silences
    the speaker immediately (no 50 ms drain). The exception is
    re-raised so the caller knows playback was interrupted.
    """
    stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=0,
    )
    stream.start()
    interrupted = False
    try:
        async for chunk in chunks:
            if not chunk:
                continue
            await asyncio.to_thread(stream.write, chunk)
    except asyncio.CancelledError:
        interrupted = True
        with contextlib.suppress(Exception):
            stream.abort()
        raise
    finally:
        if not interrupted:
            await asyncio.to_thread(time.sleep, 0.05)
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()


async def listen_for_speech(
    *,
    rms_threshold: float,
    consecutive_frames_required: int,
    blanking_ms: int,
) -> None:
    """Return when the mic registers `consecutive_frames_required` frames
    above `rms_threshold`, after ignoring the first `blanking_ms` of audio.

    Intended to run concurrently with ``play_audio_stream`` so the user
    can barge in over Emma's TTS. Opens its own ``RawInputStream`` that
    coexists with the output stream (input + output on macOS PortAudio
    is the default duplex case - no conflict).

    Cancel-safe: the input stream is stopped and closed in ``finally``.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status_listen", status=str(status))
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))  # type: ignore[call-overload]

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        callback=_cb,
    )
    stream.start()
    started = time.monotonic()
    consecutive = 0
    blanked_high_rms_frames = 0
    blanking_done = False
    try:
        while True:
            frame = await queue.get()
            elapsed_ms = (time.monotonic() - started) * 1000
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
            rms = math.sqrt(float(np.mean(samples * samples))) if samples.size else 0.0

            if elapsed_ms < blanking_ms:
                if rms > rms_threshold:
                    blanked_high_rms_frames += 1
                continue

            if not blanking_done:
                if blanked_high_rms_frames > 0:
                    log.debug(
                        "barge_in_skipped_blanking",
                        frames=blanked_high_rms_frames,
                        blanking_ms=blanking_ms,
                    )
                blanking_done = True

            if rms > rms_threshold:
                consecutive += 1
                if consecutive >= consecutive_frames_required:
                    return
            else:
                consecutive = 0
    finally:
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()


def play_wake_chime() -> None:
    """Two-tone rising chime for wake acknowledgment. Feels premium."""
    vol = 0.15
    r = SAMPLE_RATE
    n1 = int(r * 0.08)
    n2 = int(r * 0.12)
    gap = np.zeros(int(r * 0.03), dtype=np.int16)
    t1 = np.arange(n1) / r
    t2 = np.arange(n2) / r
    fade1 = np.minimum(np.linspace(0, 1, n1), np.linspace(1, 0, n1))
    fade2 = np.minimum(np.linspace(0, 1, n2), np.linspace(1, 0, n2))
    tone1 = (np.sin(2 * np.pi * 587.33 * t1) * fade1 * vol * 32767).astype(np.int16)
    tone2 = (np.sin(2 * np.pi * 880.0 * t2) * fade2 * vol * 32767).astype(np.int16)
    waveform = np.concatenate([tone1, gap, tone2])
    sd.play(waveform, r, blocking=True)
