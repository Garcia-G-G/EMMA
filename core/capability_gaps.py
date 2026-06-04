"""Capability-gap ledger — records when Emma *tried* an action but couldn't
actually accomplish the user's goal.

This is NOT a crash log. It captures the softer, more useful class of failure:
the tool ran without raising, yet the real-world effect never happened —
"Spotify has no active device", "I don't have an IDE configured", "couldn't
find that repo", a tool call dropped mid-stream, a 20s timeout. These are the
gaps worth closing to make Emma more capable.

Each gap is appended as one JSON line to ``~/.emma/capability_gaps.jsonl`` so it
can be reviewed later ("what couldn't Emma do this week?") and turned into
concrete improvements.

Security: the user-facing message is passed through :func:`core.redaction.redact`
and raw ``data`` *values* are never written — only the set of keys. Tool
argument values are never seen here (the caller passes ``args_keys`` only).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core.redaction import redact

log = structlog.get_logger("emma.capability_gaps")

_LEDGER: Path = settings.EMMA_HOME / "capability_gaps.jsonl"

# HIGH-CONFIDENCE phrases that, even on a success=True result, signal an unmet
# goal (an obstacle the user must clear). Kept deliberately narrow: broad hints
# like "no" / "no encontré" / "abra " produced false positives because tools
# that ECHO content (remember_*, list_*, read_*) repeat those words in benign
# messages. Matched case-insensitively against the user-facing message.
_OBSTACLE_HINTS: tuple[str, ...] = (
    "no tiene ningún dispositivo",
    "no active device",
    "no hay ningún dispositivo",
    "abra spotify",
    "open spotify",
    "necesito un",  # "necesito un editor instalado..."
    "i need a ",
    "instala spotify",
    "no tengo un ide",
    "no tengo configurado",
)

# Tools whose SUCCESS message echoes user/world content (note titles, facts,
# search results) and so must never be flagged by the obstacle heuristic. A
# real failure still records via success=False. Matched by name prefix.
_ECHO_PREFIXES: tuple[str, ...] = (
    "remember_",
    "list_",
    "search_",
    "read_",
    "get_",
    "describe_",
)


def _is_echo_tool(name: str) -> bool:
    return name.startswith(_ECHO_PREFIXES)


def _classify(success: bool, message: str, *, timed_out: bool, errored: bool) -> str:
    """Coarse category for the gap, so the ledger is filterable."""
    if timed_out:
        return "timeout"
    if errored:
        return "exception"
    low = message.lower()
    if "dispositivo" in low or "device" in low:
        return "no_active_device"
    if "instal" in low or "install" in low or "no tengo un ide" in low or "editor" in low:
        return "not_installed_or_configured"
    if "no encontr" in low or "no tengo una" in low or "not found" in low:
        return "not_found"
    if "no pude abrir" in low or "no respond" in low or "no responded" in low:
        return "action_failed"
    if not success:
        return "reported_failure"
    return "soft_obstacle"


def is_gap(success: bool, message: str | None) -> bool:
    """True when this result represents an unmet goal worth recording."""
    if not success:
        return True
    if not message:
        return False
    low = message.lower()
    return any(hint in low for hint in _OBSTACLE_HINTS)


def record(
    *,
    name: str,
    args_keys: list[str],
    success: bool,
    user_message: str | None,
    data: Any = None,
    elapsed_ms: int,
    timed_out: bool = False,
    errored: bool = False,
) -> None:
    """Append a capability gap to the ledger, if this result is in fact a gap.

    Defensive by contract: never raises into the tool-dispatch path. Any failure
    to write is swallowed (and logged at debug), because losing a gap record must
    never break a live conversation.
    """
    try:
        message = user_message or ""
        if not (timed_out or errored):
            # A success message from an echo tool (it repeats note titles / facts /
            # search hits) must never be heuristically flagged — only a real
            # success=False failure from it counts.
            if success and _is_echo_tool(name):
                return
            if not is_gap(success, message):
                return  # the tool did its job — nothing to learn here

        data_keys: list[str] = []
        if isinstance(data, dict):
            data_keys = list(data.keys())

        record_obj = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": name,
            "args_keys": args_keys,
            "success": success,
            "category": _classify(success, message, timed_out=timed_out, errored=errored),
            "message": redact(message),
            "data_keys": data_keys,
            "elapsed_ms": elapsed_ms,
        }
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_obj, ensure_ascii=False) + "\n")
        log.info(
            "capability_gap_recorded",
            tool=name,
            category=record_obj["category"],
            success=success,
        )
    except Exception as exc:  # pragma: no cover - belt and suspenders
        log.debug("capability_gap_record_failed", error=str(exc))
