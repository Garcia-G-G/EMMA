"""BrainProcessor — the brain as a Pipecat node between STT and TTS (VOICE-1).

Receives finished transcripts, decides with ``Brain.decide`` (the same call
shadow mode made), and pushes what Emma says as a ``TTSSpeakFrame``. Pipecat-
aware, still platform-free: everything that touches the machine (the decision
log, session memory) is a callable the host hands in, so ``core/brain`` keeps
its import rule.

Turns are serialised: a transcript that lands while the previous one is still
being decided waits its turn instead of racing it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from core.brain.decision import Decision
from core.brain.respond import Responder, language

log = structlog.get_logger("emma.brain.processor")

Decide = Callable[[str], Decision]
# (decision, what was said, extra fields) -> None. The host writes the log line.
Record = Callable[[Decision, str, dict[str, object]], None]


def _not_ready(text: str) -> Decision:
    return Decision(utterance=text, normalized="", verdict="defer", reason="brain_not_ready")


@dataclass
class TurnTiming:
    """Monotonic marks for one turn, for the latency record (VOICE-1 Part 6)."""

    transcript: float = 0.0
    decided: float = 0.0
    spoken: float = 0.0
    extra: dict[str, float] = field(default_factory=dict)

    def as_ms(self) -> dict[str, float]:
        out = {"decide_ms": round((self.decided - self.transcript) * 1000, 1)}
        if self.spoken:
            out["to_speak_ms"] = round((self.spoken - self.transcript) * 1000, 1)
        return out


class BrainProcessor(FrameProcessor):
    def __init__(
        self,
        decide: Callable[[], Decide | None],
        record: Record | None = None,
        responder: Responder | None = None,
    ) -> None:
        super().__init__()
        self._decide = decide  # a getter: the brain may finish loading mid-session
        self._record = record
        self.responder = responder or Responder()
        self._lock = asyncio.Lock()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (frame.text or "").strip()
            await self.push_frame(frame, direction)  # taps downstream still see the words
            if text:
                await self._turn(text)
            return
        await self.push_frame(frame, direction)

    async def _turn(self, text: str) -> None:
        async with self._lock:
            timing = TurnTiming(transcript=time.monotonic())
            decide = self._decide()
            if decide is None:
                d = _not_ready(text)
            else:
                try:
                    d = await asyncio.to_thread(decide, text)
                except Exception as exc:  # a failing brain is a logged defer, never silence
                    log.warning("brain_decide_failed", error=str(exc))
                    d = Decision(utterance=text, normalized="", verdict="defer",
                                 reason=f"brain_error:{type(exc).__name__}")
            timing.decided = time.monotonic()
            said, extra = await self.respond(d)
            if said:
                await self.say(said)
                timing.spoken = time.monotonic()
            extra = {**extra, "spoken": said, "latency": timing.as_ms()}
            if self._record is not None:
                try:
                    self._record(d, said, extra)
                except Exception as exc:
                    log.warning("brain_record_failed", error=str(exc))

    async def respond(self, d: Decision) -> tuple[str, dict[str, object]]:
        """What to say for ``d``, plus fields for the log. Part 1: templates only."""
        lang = language(d.utterance)
        if d.verdict in ("refuse", "clarify", "none"):
            return self.responder.say(d, lang), {"path": "template"}
        # local (until execution is on) and defer: she can't do it alone yet.
        return self.responder.not_yet(lang), {"path": "not_yet"}

    async def say(self, text: str) -> None:
        await self.push_frame(TTSSpeakFrame(text))
