"""Echo gate filter for Pipecat's LocalAudioTransport.

On a MacBook with built-in mic + speakers, the mic picks up everything
the speakers emit. The Realtime API's server-side VAD interprets the
echo as user speech and fires ``speech_started``, which truncates
Emma's response after 1-2 words (a self-interruption loop).

While the bot is speaking, this filter suppresses mic audio UNLESS
the input energy exceeds a threshold — a close-range human voice is
significantly louder than speaker echo reflected back into the mic.
This preserves barge-in: Garcia can interrupt Emma by speaking up,
but quiet echo from the speakers is silenced.

After the bot stops speaking, a short tail period continues the gate
to let residual echo decay.
"""
from __future__ import annotations

import math
import time

import numpy as np

from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame


class EchoGateFilter(BaseAudioFilter):
    """Suppresses echo while allowing loud barge-in speech."""

    def __init__(self, tail_ms: int = 500, barge_in_rms: float = 2000.0):
        self._tail_s = tail_ms / 1000.0
        self._barge_in_rms = barge_in_rms
        self._bot_speaking = False
        self._bot_stopped_at: float = 0.0
        self._sample_rate = 24000

    async def start(self, sample_rate: int):
        self._sample_rate = sample_rate

    async def stop(self):
        pass

    async def process_frame(self, frame: FilterControlFrame):
        pass

    def set_bot_speaking(self, speaking: bool):
        if speaking:
            self._bot_speaking = True
        else:
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()

    def _is_gating(self) -> bool:
        if self._bot_speaking:
            return True
        return time.monotonic() - self._bot_stopped_at < self._tail_s

    async def filter(self, audio: bytes) -> bytes:
        if not self._is_gating():
            return audio
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return b"\x00" * len(audio)
        rms = math.sqrt(float(np.mean(samples * samples)))
        if rms >= self._barge_in_rms:
            return audio
        return b"\x00" * len(audio)
