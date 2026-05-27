"""Short-term session memory.

In-process turn log for the current Emma session. Lives only in memory
(no disk write); it's used by the reflection step in
:mod:`memory.reflection` to feed gpt-4o-mini a small window of the
recent conversation when extracting durable facts.

The orchestrator already keeps a 10-turn rolling history in
``core.orchestrator._one_turn``; this module is a *separate* log
focused on the reflection input. It does not affect the LLM context.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

log = structlog.get_logger("emma.memory.short_term")


@dataclass(frozen=True)
class Turn:
    user_text: str
    assistant_text: str
    timestamp: float


_SESSION: list[Turn] = []
_MAX_SESSION = 50  # ring-buffer cap for the in-process log


def append_turn(user_text: str, assistant_text: str) -> None:
    """Append the just-completed turn to the session log."""
    if not user_text.strip() and not assistant_text.strip():
        return
    _SESSION.append(Turn(user_text.strip(), assistant_text.strip(), time.time()))
    if len(_SESSION) > _MAX_SESSION:
        del _SESSION[: len(_SESSION) - _MAX_SESSION]


def last_turns(n: int = 4) -> list[Turn]:
    """Return the last `n` turns of the current session (oldest first)."""
    if n <= 0:
        return []
    return list(_SESSION[-n:])


def clear() -> None:
    _SESSION.clear()


def size() -> int:
    return len(_SESSION)
