"""Porcupine wake-word listener for the custom "Hey Emma" keyword.

The Porcupine SDK is synchronous; we feed it from a sounddevice callback
running on the portaudio thread and signal the asyncio loop on detection.
"""
from __future__ import annotations

import asyncio
import struct

import pvporcupine
import sounddevice as sd
import structlog

from config.settings import settings
from core.audio import play_tone

log = structlog.get_logger("emma.wake_word")


async def listen_for_wake_word() -> None:
    """Block until "Hey Emma" is detected, then return. Plays an ack tone."""
    porcupine = pvporcupine.create(
        access_key=settings.PICOVOICE_ACCESS_KEY,
        keyword_paths=[settings.WAKE_WORD_PATH],
    )
    frame_length = porcupine.frame_length
    sample_rate = porcupine.sample_rate

    detected = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        pcm = struct.unpack_from("h" * frame_length, bytes(indata))  # type: ignore[arg-type]
        if porcupine.process(list(pcm)) >= 0:
            loop.call_soon_threadsafe(detected.set)

    stream = sd.RawInputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_length,
        callback=_cb,
    )
    stream.start()
    try:
        await detected.wait()
    finally:
        stream.stop()
        stream.close()
        porcupine.delete()

    play_tone()
