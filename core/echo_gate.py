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
from collections.abc import Callable

import numpy as np
import structlog
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame

log = structlog.get_logger("emma.echo_gate")

# Garcia's buffered over-the-opener speech is capped at 2 s of 24 kHz mono
# int16 — enough for "Emma, espera" without replaying a whole monologue.
_OPENER_BUFFER_CAP_BYTES = 2 * 24_000 * 2


class SpeechPhase:
    """Tracks Emma's speech phase: ``opener`` | ``body`` | None (22-B32).

    The OPENER is the first utterance after a session opens (or resumes via
    a continuation seed) — it must never be cut, or Emma stalls mid-greeting
    and the conversation derails. It graduates to BODY when EITHER ≥ 8 words
    have been spoken OR 1.5 s have elapsed (the OR matters: word count alone
    would block forever on short greetings). Everything after the opener
    utterance is body and fully interruptible. A new pipeline session
    constructs a fresh instance, which IS the reset.
    """

    OPENER_MAX_S = 1.5
    OPENER_MAX_WORDS = 8

    def __init__(self) -> None:
        self._speaking = False
        self._opener_done = False
        self._opener_started_at = 0.0
        self._opener_words = 0

    def on_bot_started(self) -> None:
        self._speaking = True
        if not self._opener_done and self._opener_started_at == 0.0:
            self._opener_started_at = time.monotonic()

    def on_bot_text(self, text: str) -> None:
        if not self._opener_done:
            self._opener_words += len(text.split())

    def on_bot_stopped(self) -> None:
        self._speaking = False
        self._opener_done = True  # the opener is at most ONE utterance

    def current(self) -> str | None:
        if not self._speaking:
            return None
        if self._opener_done:
            return "body"
        if self._opener_words >= self.OPENER_MAX_WORDS:
            return "body"
        if time.monotonic() - self._opener_started_at >= self.OPENER_MAX_S:
            return "body"
        return "opener"


class EchoGateFilter(BaseAudioFilter):
    """Suppresses echo while allowing loud barge-in speech."""

    def __init__(
        self,
        tail_ms: int = 500,
        barge_in_rms: float = 2000.0,
        phase_provider: Callable[[], str | None] | None = None,
    ):
        self._tail_s = tail_ms / 1000.0
        self._barge_in_rms = barge_in_rms
        self._bot_speaking = False
        self._bot_stopped_at: float = 0.0
        self._sample_rate = 24000
        self._gate_logged = False
        # 22-B32: while phase_provider() == "opener" the gate is FULLY closed
        # (no barge-in), but loud speech is buffered and released afterwards.
        self._phase_provider = phase_provider
        self._opener_buffer: list[bytes] = []
        self._opener_buffer_bytes = 0

    async def start(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate

    async def stop(self) -> None:
        pass

    async def process_frame(self, frame: FilterControlFrame) -> None:
        pass

    def set_bot_speaking(self, speaking: bool) -> None:
        if speaking and not self._bot_speaking:
            self._bot_speaking = True
            self._gate_logged = False
            log.debug("echo_gate_on")
        elif not speaking and self._bot_speaking:
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()
            log.debug("echo_gate_tail_start", tail_ms=int(self._tail_s * 1000))

    def _is_gating(self) -> bool:
        if self._bot_speaking:
            return True
        if self._bot_stopped_at == 0.0:
            return False
        elapsed = time.monotonic() - self._bot_stopped_at
        if elapsed < self._tail_s:
            return True
        if not self._gate_logged:
            self._gate_logged = True
            log.debug("echo_gate_off", elapsed_ms=int(elapsed * 1000))
        return False

    def _rms(self, audio: bytes) -> float:
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return 0.0
        return math.sqrt(float(np.mean(samples * samples)))

    def _release_opener_buffer(self) -> bytes:
        if not self._opener_buffer:
            return b""
        released = b"".join(self._opener_buffer)
        self._opener_buffer.clear()
        self._opener_buffer_bytes = 0
        log.debug("opener_buffer_released", bytes=len(released))
        return released

    async def filter(self, audio: bytes) -> bytes:
        phase = self._phase_provider() if self._phase_provider else None

        # Opener phase (22-B32): the gate is FULLY closed — Emma always
        # finishes her first sentence. Garcia's loud speech is BUFFERED, not
        # dropped, and dispatched right after the opener ends.
        if phase == "opener":
            rms = self._rms(audio)
            if (
                rms >= self._barge_in_rms
                and self._opener_buffer_bytes + len(audio) <= _OPENER_BUFFER_CAP_BYTES
            ):
                self._opener_buffer.append(audio)
                self._opener_buffer_bytes += len(audio)
            return b"\x00" * len(audio)

        released = self._release_opener_buffer()

        if not self._is_gating():
            return released + audio
        rms = self._rms(audio)
        if rms == 0.0:
            return released + b"\x00" * len(audio)
        if rms >= self._barge_in_rms:
            log.debug("echo_gate_barge_in", rms=int(rms))
            return released + audio
        return released + b"\x00" * len(audio)
