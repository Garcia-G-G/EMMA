"""BrainProcessor — the brain as a Pipecat node between STT and TTS (VOICE-1).

Receives finished transcripts and, per turn:

1. ``Brain.decide(transcript)`` — the same call shadow mode made.
2. ``local``: if the decision needs confirmation, speak the short question
   ("¿Borrar la nota Compras?") and arm the closed yes/no lists for the NEXT
   transcript; otherwise execute through the ``Host`` and speak the template.
3. ``refuse | clarify | none``: speak the template.
4. ``defer``: hand the turn to the ``Thinker`` (VOICE-1: ``NoThinker``).

Pipecat-aware, still platform-free. Everything that touches the machine —
running a tool, telling the injection guard a question was asked, reading
memory facts, writing the log — is the ``Host``'s job, handed in by
``core.cascade``. That is the seam that keeps ``core/brain`` from importing
``tools``. Execution goes through ``tools.registry.dispatch`` on the host side,
so every injection defense applies, including mic-only confirmation: a
``confirmed=True`` from here still needs the spoken "sí" the guard can see.

Turns are serialised: a transcript that lands while the previous one is still
being decided waits its turn instead of racing it.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import structlog
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from core import runtime
from core.brain.confirm import Pending, question
from core.brain.decision import Decision, SlotScalar
from core.brain.phrases import is_correction
from core.brain.respond import (
    SPEAKS_RESULT,
    Responder,
    answer_confirmation,
    language,
)
from core.brain.thinker import (
    CONTEXT_TURNS,
    ContextTurn,
    NoThinker,
    Thinker,
    Turn,
    one_line,
    rank_facts,
)

log = structlog.get_logger("emma.brain.processor")

Decide = Callable[[str], Decision]
# (decision, what was said, extra fields) -> None. The host writes the log line.
Record = Callable[[Decision, str, dict[str, object]], None]


@dataclass(frozen=True)
class Outcome:
    """A tool run, as plain data: did it work, what it said, did it ask first."""

    success: bool
    message: str
    asks: bool = False  # the tool (or the guard) wants a spoken yes before acting


class Host(Protocol):
    async def execute(self, tool: str, args: dict[str, SlotScalar]) -> Outcome: ...

    def asked(self, pending: Pending) -> None:
        """Emma just asked: this is now THE pending confirmation (the guard owns it)."""

    def pending(self) -> Pending | None:
        """The question waiting for a yes — asked by the brain, the guard or a tool."""

    def settle(self) -> None:
        """The answer came (yes, no, or something else): nothing is pending any more."""

    def facts(self) -> Sequence[str]:
        """Memory facts (non-secret), confidence-ordered. Ranked per turn here."""


# What a clitic on a music verb ("reanúdala", "páusala") can refer to.
MEDIA_TOOLS = frozenset({"play_track", "play_playlist", "resume", "pause", "next_track"})


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
        host: Host | None = None,
        thinker: Thinker | None = None,
    ) -> None:
        super().__init__()
        self._decide = decide  # a getter: the brain may finish loading mid-session
        self._record = record
        self.responder = responder or Responder()
        self._host = host
        self._thinker = thinker or NoThinker(self.responder)
        self._lock = asyncio.Lock()
        self._last_executed: tuple[str, str] | None = None  # (tool, utterance), one turn back
        self._context: deque[ContextTurn] = deque(maxlen=CONTEXT_TURNS)

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
            # Tools, templates and the guard's questions speak the speaker's language.
            runtime.set_spoken_lang(language(text))
            # The wrong-rate signal: the utterance right after an executed turn
            # is a closed-list correction ("no, eso no", "cancela", "otra vez").
            corrects = self._last_executed if is_correction(text) else None
            self._last_executed = None
            extra: dict[str, Any]
            try:
                if corrects is not None and d.verdict != "local":
                    said, extra = self.responder.corrected(language(text)), {"path": "corrected"}
                else:
                    said, extra = await self.respond(d)
            except Exception as exc:  # never leave the user in silence
                log.warning("brain_respond_failed", error=str(exc))
                said, extra = self.responder.not_yet(language(text)), {
                    "path": "error", "error": type(exc).__name__}
            if said:
                await self.say(said)
                timing.spoken = time.monotonic()
            tool = extra.get("tool") if extra.get("executed") else None
            if tool:
                self._last_executed = (str(tool), text)
            self._context.append(ContextTurn(str(tool) if tool else None,
                                             one_line(str(extra.get("result") or said))))
            extra = {**extra, "spoken": said, "latency": timing.as_ms()}
            if corrects is not None:
                extra["correction_of"] = {"tool": corrects[0], "utterance": corrects[1]}
            if self._record is not None:
                try:
                    self._record(d, said, extra)
                except Exception as exc:
                    log.warning("brain_record_failed", error=str(exc))

    async def respond(self, d: Decision) -> tuple[str, dict[str, Any]]:
        """What to say for ``d`` (after acting on it), plus fields for the log."""
        lang = language(d.utterance)
        pending = self._host.pending() if self._host is not None else None
        if pending is not None:
            outcome = answer_confirmation(pending.tool, pending.slots, d.utterance)
            if outcome.kind == "execute":
                # The guard settles it when it clears the spoken yes.
                return await self._execute(pending.tool, {**outcome.slots, "confirmed": True},
                                           lang, path="confirmed")
            assert self._host is not None
            self._host.settle()
            if outcome.kind == "cancel":
                return self.responder.cancelled(lang), {"path": "cancelled", "tool": pending.tool}
            # Not a yes or a no: the window closes, and this is a new request.
            log.info("confirmation_abandoned", tool=pending.tool)

        if d.verdict in ("refuse", "clarify", "none"):
            return self.responder.say(d, lang), {"path": "template"}
        if d.verdict == "local" and d.anaphor and not self._has_referent(d):
            # "Reanúdala" with nothing played this session: ask, don't drop or guess.
            d = replace(d, verdict="clarify", reason="anaphora:no_referent")
            return self.responder.say(d, lang), {"path": "template", "tool": d.chosen}
        if d.verdict == "local" and d.chosen and self._host is not None:
            slots = {k: v.value for k, v in d.slots.items()}
            if d.needs_confirmation:
                return self._ask(d, slots, lang), {"path": "confirm_ask", "tool": d.chosen}
            return await self._execute(d.chosen, slots, lang, path="local")
        return await self._think(d)

    def _has_referent(self, d: Decision) -> bool:
        """Only a music verb resolves today, against the session's last music action."""
        return d.chosen in MEDIA_TOOLS and any(t.tool in MEDIA_TOOLS for t in self._context)

    def _ask(self, d: Decision, slots: dict[str, SlotScalar], lang: runtime.SpokenLang) -> str:
        assert d.chosen is not None and self._host is not None
        self._host.asked(Pending(d.chosen, slots, lang, time.monotonic()))
        return question(d.chosen, slots, lang)

    async def _execute(
        self, tool: str, args: dict[str, SlotScalar], lang: str, path: str
    ) -> tuple[str, dict[str, Any]]:
        assert self._host is not None
        out = await self._host.execute(tool, args)
        extra: dict[str, Any] = {"path": path, "tool": tool, "executed": out.success,
                                 "result": one_line(out.message, 160)}
        if out.asks and not out.success:
            # The guard or the tool wants a yes first (e.g. a tainted sink): it
            # opened the pending question; we say it, the next transcript answers.
            return out.message, {**extra, "path": f"{path}:asked"}
        if not out.success:
            return out.message, extra  # never "Listo" over a failure
        if tool in SPEAKS_RESULT and out.message:
            return out.message, extra  # the result is the answer; a preamble would be all she said
        slots = {k: v for k, v in args.items() if k not in ("confirmed", "cancelled")}
        return self.responder.ack(tool, slots, "en" if lang == "en" else "es"), extra

    async def _think(self, d: Decision) -> tuple[str, dict[str, Any]]:
        facts: tuple[str, ...] = ()
        if self._host is not None:
            try:
                facts = rank_facts(d.utterance, list(self._host.facts()))
            except Exception as exc:
                log.warning("facts_unavailable", error=str(exc))
        turn = Turn(transcript=d.utterance, decision=d, context=tuple(self._context), facts=facts)
        result = await self._thinker.think(turn)
        extra: dict[str, Any] = {"path": "thinker", "thinker": type(self._thinker).__name__,
                                 "defer_reason": d.reason, "facts": len(facts),
                                 "context_turns": len(turn.context)}
        return result.text, extra

    async def say(self, text: str) -> None:
        await self.push_frame(TTSSpeakFrame(text))

