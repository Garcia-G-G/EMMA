"""Process-local session memory: a ring buffer of conversation events (21-B24).

Two consumers:
- The confirmation invariant (B24): "a destructive ``confirmed=True`` call is
  only valid if Garcia actually SPOKE after the tool asked its question."
  Event ORDER is the source of truth — never durations (a real "sí" can land
  200 ms after the question).
- Anaphora resolution (B29): "otra vez" / "lo de hace rato" resolve against
  the completed-action tail.

Deliberately NOT persisted: the buffer lives and dies with one daemon run.
Timestamps are ``time.monotonic()`` (ordering-safe across clock changes).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

_MAX_EVENTS = 200
ACTION_TTL_S = 1800.0  # 30 min — "lo de hace rato", not "lo del martes"

# Arg keys whose VALUES never enter the log (same spirit as core/redaction).
_SECRETISH = ("password", "secret", "token", "key", "credential")
_VALUE_CAP = 120


@dataclass(frozen=True)
class SessionEvent:
    """One conversation event. role ∈ user|assistant|tool."""

    role: str
    kind: str
    content: str
    ts: float


_events: deque[SessionEvent] = deque(maxlen=_MAX_EVENTS)
_lock = threading.Lock()


def push_event(role: str, kind: str, content: str = "") -> SessionEvent:
    ev = SessionEvent(role=role, kind=kind, content=content, ts=time.monotonic())
    with _lock:
        _events.append(ev)
    return ev


def recent(k: int = 20) -> list[SessionEvent]:
    """The newest ``k`` events, newest LAST."""
    with _lock:
        return list(_events)[-k:]


def clear() -> None:
    with _lock:
        _events.clear()


def last_user_turn_ts() -> float | None:
    """Timestamp of the most recent real user speech, or None.

    Both signals count: ``speech_started`` (VAD onset — arrives immediately,
    what the confirmation invariant needs) and ``speech`` (the transcription,
    which can lag the model's own reaction to the audio).
    """
    with _lock:
        for ev in reversed(_events):
            if ev.role == "user" and ev.kind in ("speech", "speech_started"):
                return ev.ts
    return None


def tool_completed_since_last_user_turn() -> bool:
    """True if a tool finished AFTER the user last spoke (24.6, injection guard).

    The prompt-injection-to-action signature: the user says something benign
    ("lee esta página"), a READ tool then runs and returns attacker content, and
    the LLM — in the same turn, with no fresh user voice — tries to fire a
    destructive ``confirmed=True``. If any tool completed since the last user
    turn, a cold confirmation is NOT genuine user intent and must be refused.
    The legit "borra X, sí" preemptive flow has NO tool between the user's words
    and the destructive call, so it stays allowed.
    """
    with _lock:
        for ev in reversed(_events):
            if ev.role == "user" and ev.kind in ("speech", "speech_started"):
                return False  # reached the last user turn first → nothing ran since
            if ev.role == "tool" and ev.kind == "completed":
                return True
    return False


def last_user_speech_text() -> str:
    """Content of the most recent user speech event ("" if none)."""
    with _lock:
        for ev in reversed(_events):
            if ev.role == "user" and ev.kind == "speech":
                return ev.content
    return ""


def last_user_speech_ts() -> float | None:
    """Timestamp of the last TRANSCRIBED user speech (22-B31)."""
    with _lock:
        for ev in reversed(_events):
            if ev.role == "user" and ev.kind == "speech":
                return ev.ts
    return None


def last_assistant_speech_ts() -> float | None:
    """When Emma last finished saying something (22-B31 continuity check)."""
    with _lock:
        for ev in reversed(_events):
            if ev.role == "assistant" and ev.kind == "speech":
                return ev.ts
    return None


def recent_messages_for_llm(k: int = 20) -> list[dict[str, str]]:
    """The conversational tail in OpenAI chat shape, oldest first (22-B31).

    Only real speech rides along — VAD onsets, confidence flags and tool
    bookkeeping stay internal. This is what makes a fresh Pipecat
    micro-session open WARM: the LLM sees the thread, not turn 1.
    """
    out: list[dict[str, str]] = []
    with _lock:
        for ev in _events:
            if ev.kind == "speech" and ev.role in ("user", "assistant") and ev.content:
                out.append({"role": ev.role, "content": ev.content})
    return out[-k:]


def last_user_turn_low_confidence() -> bool:
    """True when the NEWEST user event is a low-confidence flag (21-B28).

    The speech tap pushes ("user","speech") then ("user","low_confidence")
    for suspicious transcripts, so a reversed scan hitting the flag first
    means the latest turn was flagged; hitting clean speech first means not.
    """
    with _lock:
        for ev in reversed(_events):
            if ev.role == "user":
                return ev.kind == "low_confidence"
    return False


def last_tool_request_confirmation_ts(tool_name: str) -> float | None:
    """When ``tool_name`` last asked a requires_confirmation question.

    Requests already consumed by a granted confirmation don't count — each
    confirmed call needs a FRESH question + a fresh user turn after it.
    """
    req_kind = f"requires_confirmation:{tool_name}"
    consumed_kind = f"confirmation_consumed:{tool_name}"
    with _lock:
        for ev in reversed(_events):
            if ev.role != "tool":
                continue
            if ev.kind == consumed_kind:
                return None  # newest marker is a consumption → no pending request
            if ev.kind == req_kind:
                return ev.ts
    return None


def consume_confirmation_request(tool_name: str) -> None:
    """Mark the pending question for ``tool_name`` as answered."""
    push_event("tool", f"confirmation_consumed:{tool_name}")


# ---- completed actions (B29: anaphora) --------------------------------------


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if any(s in k.lower() for s in _SECRETISH):
            continue
        if isinstance(v, str) and len(v) > _VALUE_CAP:
            v = v[:_VALUE_CAP] + "…"
        out[k] = v
    return out


def record_completed_action(name: str, args: dict[str, Any], user_text: str = "") -> None:
    payload = json.dumps(
        {"name": name, "args": _sanitize_args(args), "user_text": user_text[:_VALUE_CAP]},
        ensure_ascii=False,
        default=str,
    )
    push_event("tool", "completed", payload)


def recent_completed_actions(within_s: float = ACTION_TTL_S) -> list[dict[str, Any]]:
    """Completed tool actions inside the TTL window, newest LAST."""
    horizon = time.monotonic() - within_s
    out: list[dict[str, Any]] = []
    with _lock:
        for ev in _events:
            if ev.role == "tool" and ev.kind == "completed" and ev.ts >= horizon:
                try:
                    out.append(json.loads(ev.content))
                except json.JSONDecodeError:
                    continue
    return out
