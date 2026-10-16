"""The one pending confirmation and the one question that opens it (Step 43 Part 2).

Before this, three things tracked "Emma asked, waiting for a yes": the
processor's pending decision, the injection guard's timestamps and a session
event, and two builders wrote the question (the guard's only in Spanish). Now
the session guard owns a single ``Pending``; the brain reads and settles it
through its ``Host``, and every question comes from ``question``, in the
language of the turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from core.brain.decision import SlotScalar
from core.brain.respond import action_phrase
from core.runtime import SpokenLang

_SPOKEN_VALUES = 3  # a long body (possibly attacker text) is never read aloud as "the short question"
_SPOKEN_CHARS = 60


@dataclass(frozen=True)
class Pending:
    """One question waiting for a spoken yes: what runs on that yes, and when it was asked."""

    tool: str
    slots: Mapping[str, SlotScalar] = field(default_factory=dict)  # the real args, minus confirmed
    lang: SpokenLang = "es"
    asked_at: float = 0.0  # time.monotonic() when the question was spoken


def question(
    tool: str, slots: Mapping[str, SlotScalar], lang: SpokenLang, now: datetime | None = None
) -> str:
    """Short, names the action, answerable with one word: "¿Borrar la nota Compras?".

    ``now``: the clock a moment is said against ("mañana"); the real one by default."""
    shown = {k: (s if len(s := str(v)) <= _SPOKEN_CHARS else s[: _SPOKEN_CHARS - 3] + "...")
             for k, v in list(slots.items())[:_SPOKEN_VALUES] if k not in ("confirmed", "cancelled")}
    action = action_phrase(tool, shown, lang, now)
    return f"{action}?" if lang == "en" else f"¿{action}?"
