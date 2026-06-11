"""Episodic memory — a durable, queryable audit log of ACTIONS Emma took.

The layer between ``core.session_memory`` (process-local conversation ring) and
``memory.long_term`` (durable FACTS): every state-changing tool writes a row to
an ``actions`` table in the SAME memory DB (``settings.MEMORY_DB_PATH``, new
table only). Two consumers share it:

1. "¿Qué hiciste el martes?" — Garcia querying the past (``query_by_date``).
2. ``undo_last_action`` — reversing the last fixable action, via the per-tool
   "reverse blueprint" captured at record time.

Rows are kept FOREVER — the audit trail is the whole point; nothing auto-prunes.
We open our own lightweight connection (same DB, WAL like long_term) but skip the
sqlite-vec extension — the actions table never embeds.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core.session_memory import _SECRETISH, _sanitize_args

log = structlog.get_logger("emma.episodic")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            REAL NOT NULL,
  tool_name     TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  result_json   TEXT,
  user_speech   TEXT,
  reverse_kind  TEXT,
  reverse_json  TEXT,
  reversed_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts DESC);
"""

_RESULT_CAP = 4096  # bytes of result_json
_ROW_CAP = 16 * 1024  # combined args_json + reverse_json (B3); over → manual

REVERSE_KINDS = ("inverse_call", "restore_text", "noop", "manual")


@dataclass(frozen=True)
class ActionRecord:
    id: int
    ts: float
    tool_name: str
    args: dict[str, Any]
    result: Any
    user_speech: str
    reverse_kind: str
    reverse: dict[str, Any] | None
    reversed_at: float | None


def _connect() -> sqlite3.Connection:
    path = Path(settings.MEMORY_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # coexist with long_term's writers
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def _sanitize_result(result: Any) -> Any:
    """Drop secretish keys + the internal blueprint marker from a result dict."""
    if isinstance(result, dict):
        return {
            k: v
            for k, v in result.items()
            if k != "_reverse_blueprint" and not any(s in k.lower() for s in _SECRETISH)
        }
    return result


def _row_to_record(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=row["id"],
        ts=row["ts"],
        tool_name=row["tool_name"],
        args=json.loads(row["args_json"] or "{}"),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        user_speech=row["user_speech"] or "",
        reverse_kind=row["reverse_kind"] or "noop",
        reverse=json.loads(row["reverse_json"]) if row["reverse_json"] else None,
        reversed_at=row["reversed_at"],
    )


# ---- reverse-blueprint helpers (tools build these into ToolResult.data) ------


def blueprint_inverse(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """A reciprocal tool reverses this action (create_note → delete_note)."""
    return {"kind": "inverse_call", "tool": tool, "args": args, "describe": ""}


def blueprint_restore_text(path: str, before: str) -> dict[str, Any]:
    """The pre-edit on-disk content is captured; undo writes it back."""
    return {"kind": "restore_text", "path": path, "before": before}


def blueprint_manual(hint: str) -> dict[str, Any]:
    """Reversible only with Garcia's intervention; ``hint`` tells him the step."""
    return {"kind": "manual", "hint": hint}


def blueprint_noop() -> dict[str, Any]:
    """Intrinsically irreversible (a sent message, a posted tweet) — audit only."""
    return {"kind": "noop"}


# ---- write ------------------------------------------------------------------


def _record_sync(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    user_speech: str,
    reverse: dict[str, Any] | None,
) -> int:
    now = time.time()
    args_json = json.dumps(_sanitize_args(args or {}), ensure_ascii=False, default=str)
    result_json = json.dumps(_sanitize_result(result), ensure_ascii=False, default=str)
    if len(result_json.encode("utf-8")) > _RESULT_CAP:
        result_json = json.dumps({"_truncated": True}, ensure_ascii=False)

    reverse = reverse or blueprint_noop()
    reverse_kind = reverse.get("kind", "noop")
    reverse_json = json.dumps(reverse, ensure_ascii=False, default=str)
    # B3: a restore_text blob (or any reverse) that blows the row cap downgrades
    # to manual — point Garcia at the OS-level escape hatch instead.
    if len((args_json + reverse_json).encode("utf-8")) > _ROW_CAP:
        reverse = blueprint_manual(
            reverse.get("hint")
            or "El backup es muy grande para guardar; usa Time Machine para revertirlo."
        )
        reverse_kind = "manual"
        reverse_json = json.dumps(reverse, ensure_ascii=False)

    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO actions "
            "(ts, tool_name, args_json, result_json, user_speech, reverse_kind, reverse_json, reversed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (now, tool_name, args_json, result_json, (user_speech or "")[:60], reverse_kind, reverse_json),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


async def record(
    tool_name: str,
    args: dict[str, Any],
    result: Any = None,
    user_speech: str = "",
    reverse: dict[str, Any] | None = None,
) -> int:
    """Append an action row (args/result sanitized). Returns the row id."""
    return await asyncio.to_thread(_record_sync, tool_name, args, result, user_speech, reverse)


# ---- read -------------------------------------------------------------------


def _query_by_date_sync(d: date, limit: int) -> list[ActionRecord]:
    start = datetime(d.year, d.month, d.day).timestamp()
    end = start + 86400
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM actions WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def _recent_sync(within_s: float, limit: int) -> list[ActionRecord]:
    horizon = time.time() - within_s
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM actions WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (horizon, limit)
        ).fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


def _last_undoable_sync() -> ActionRecord | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM actions WHERE reverse_kind != 'noop' AND reversed_at IS NULL "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def _get_sync(action_id: int) -> ActionRecord | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def _mark_reversed_sync(action_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE actions SET reversed_at = ? WHERE id = ?", (time.time(), action_id))
        conn.commit()
    finally:
        conn.close()


async def query_by_date(d: date, limit: int = 20) -> list[ActionRecord]:
    return await asyncio.to_thread(_query_by_date_sync, d, limit)


async def recent(within_s: float = 1800, limit: int = 20) -> list[ActionRecord]:
    return await asyncio.to_thread(_recent_sync, within_s, limit)


async def last_undoable() -> ActionRecord | None:
    return await asyncio.to_thread(_last_undoable_sync)


async def get(action_id: int) -> ActionRecord | None:
    return await asyncio.to_thread(_get_sync, action_id)


async def mark_reversed(action_id: int) -> None:
    await asyncio.to_thread(_mark_reversed_sync, action_id)
