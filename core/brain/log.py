"""Append-only JSONL log of every brain decision: ``~/.emma/brain_decisions.jsonl``.

One line per decision, no exceptions — "no record, no decision" (BRAIN-1 rule 2).
Every string is passed through ``core.redaction`` first, so a secret spoken in a
transcript never lands on disk. Rotates to ``.1`` at 50 MB (one generation).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from core.brain.decision import Decision
from core.redaction import redact

DEFAULT_PATH = Path.home() / ".emma" / "brain_decisions.jsonl"
ROTATE_BYTES = 50 * 1024 * 1024

_LOCK = threading.Lock()

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _scrub(v: object) -> JsonValue:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, bool | int | float) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _scrub(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_scrub(x) for x in v]
    return redact(str(v))


def write(
    decision: Decision,
    path: Path | None = None,
    extra: dict[str, object] | None = None,
    rotate_bytes: int = ROTATE_BYTES,
) -> dict[str, JsonValue]:
    """Append one decision (plus ``extra`` fields, e.g. the Realtime comparison).

    Returns the record as written (redacted), so callers and tests see exactly
    what is on disk.
    """
    target = path or DEFAULT_PATH
    rec: dict[str, object] = {"ts": round(time.time(), 3), **decision.to_record()}
    if extra:
        rec.update(extra)
    clean = _scrub(rec)
    assert isinstance(clean, dict)
    line = json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.stat().st_size + len(line) > rotate_bytes:
                os.replace(target, target.with_name(target.name + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
    return clean


def read(path: Path | None = None) -> list[dict[str, JsonValue]]:
    """Every record in the current log file (not the rotated one)."""
    target = path or DEFAULT_PATH
    if not target.exists():
        return []
    out: list[dict[str, JsonValue]] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out.append(obj)
    return out
