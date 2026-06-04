"""Playback half of the voice harness loopback (19.7-VAH2.3).

Plays a cached WAV at Emma: through the BlackHole virtual cable when
``device_substr`` names it, or through the default speakers (the noisier
speaker→mic fallback) when empty. Blocks until playback finishes.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from core.audio_devices import find_device_index


class PlaybackError(RuntimeError):
    """Friendly, actionable playback failure."""


def play(path: str | Path, device_substr: str = "") -> float:
    """Play ``path`` (16-bit PCM WAV) on the matching output device.

    Returns the clip duration in seconds. Raises :class:`PlaybackError` when
    the device can't be found — never an opaque PortAudio stacktrace.
    """
    import sounddevice as sd  # lazy: PortAudio touches CoreAudio on import

    p = Path(path)
    if not p.is_file():
        raise PlaybackError(f"No existe el audio {p} — corre --prewarm primero.")

    device: int | None = None
    if device_substr:
        device = find_device_index(device_substr, kind="output")
        if device is None:
            raise PlaybackError(
                f"No encontré un dispositivo de salida que contenga '{device_substr}'. "
                "¿Está instalado BlackHole? (brew install blackhole-2ch)"
            )

    with wave.open(str(p), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)

    sd.play(samples, samplerate=rate, device=device)
    sd.wait()
    return len(samples) / float(rate)
