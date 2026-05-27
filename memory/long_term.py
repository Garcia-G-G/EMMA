"""Long-term memory: a small fact store at ``settings.MEMORY_DB_PATH``.

Facts are short, durable statements about Garcia (preferences, names,
recurring patterns) written either explicitly via the
``remember_fact`` tool or implicitly by the reflection step at the end
of each turn. They are read back into every turn's system prompt via
:func:`priming_block` so Emma starts each session knowing what she
already knows.

Storage: SQLite via stdlib ``sqlite3`` wrapped in ``asyncio.to_thread``
for the rare async caller. Schema is a single ``facts`` table; we keep
it intentionally flat (no embeddings, no FTS) because Phase 03's goal
is "Emma stops being a stranger", not full retrieval-augmented
generation. A Postgres backend can be added when
``settings.POSTGRES_DSN`` is set; today the SQLite path is the only
one wired up.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from config.settings import settings

log = structlog.get_logger("emma.memory.long_term")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'general',
    confidence    REAL NOT NULL DEFAULT 0.7,
    source        TEXT NOT NULL DEFAULT 'explicit',
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
CREATE INDEX IF NOT EXISTS idx_facts_conf ON facts(confidence DESC);
"""


@dataclass(frozen=True)
class Fact:
    id: int
    content: str
    kind: str
    confidence: float
    source: str
    created_at: float
    last_seen_at: float


def _connect() -> sqlite3.Connection:
    path = Path(settings.MEMORY_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        confidence=row["confidence"],
        source=row["source"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


# ---------- sync core (the actual SQL) -----------------------------------


def _remember_sync(content: str, kind: str, confidence: float, source: str) -> int:
    now = time.time()
    content = content.strip()
    if not content:
        return -1
    with _connect() as conn:
        # If we already have this exact content, refresh last_seen_at and
        # bump confidence toward 1.0 rather than inserting a duplicate.
        existing = conn.execute(
            "SELECT id, confidence FROM facts WHERE content = ?",
            (content,),
        ).fetchone()
        if existing:
            new_conf = min(1.0, max(existing["confidence"], confidence))
            conn.execute(
                "UPDATE facts SET last_seen_at = ?, confidence = ?, source = ? WHERE id = ?",
                (now, new_conf, source, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO facts (content, kind, confidence, source, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (content, kind, confidence, source, now, now),
        )
        return int(cur.lastrowid)


def _recall_sync(query: str | None, limit: int) -> list[Fact]:
    with _connect() as conn:
        if query:
            rows = conn.execute(
                """
                SELECT * FROM facts
                WHERE content LIKE ? OR kind LIKE ?
                ORDER BY confidence DESC, last_seen_at DESC
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM facts
                ORDER BY confidence DESC, last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [_row_to_fact(r) for r in rows]


def _forget_sync(content_or_id: str | int) -> int:
    with _connect() as conn:
        if isinstance(content_or_id, int):
            cur = conn.execute("DELETE FROM facts WHERE id = ?", (content_or_id,))
        else:
            cur = conn.execute(
                "DELETE FROM facts WHERE content = ? OR content LIKE ?",
                (content_or_id, f"%{content_or_id}%"),
            )
        return int(cur.rowcount)


def _count_sync() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()
    return int(row["n"]) if row else 0


# ---------- async wrappers (for the orchestrator + tool layer) ----------


async def remember(
    content: str, *, kind: str = "general", confidence: float = 0.7, source: str = "explicit"
) -> int:
    return await asyncio.to_thread(_remember_sync, content, kind, confidence, source)


async def recall(query: str | None = None, *, limit: int = 25) -> list[Fact]:
    return await asyncio.to_thread(_recall_sync, query, limit)


async def forget(content_or_id: str | int) -> int:
    return await asyncio.to_thread(_forget_sync, content_or_id)


async def count() -> int:
    return await asyncio.to_thread(_count_sync)


# ---------- system-prompt priming ---------------------------------------


async def priming_block(top_n: int | None = None) -> str:
    """Return a short block of known facts to inject into the system prompt.

    Empty string when the store has nothing yet - keeps the prompt
    clean for fresh installs.
    """
    n = top_n if top_n is not None else settings.MEMORY_PRIMING_TOP_N
    facts = await recall(limit=n)
    if not facts:
        return ""
    lines = [f"- {f.content}" for f in facts]
    return "WHAT YOU ALREADY KNOW ABOUT GARCIA (long-term memory):\n" + "\n".join(lines)


def initialize() -> None:
    """Force schema creation at startup. Idempotent; safe to call repeatedly."""
    try:
        with _connect():
            pass
    except Exception as exc:
        log.error("memory_initialize_failed", error=str(exc))


def known_kinds() -> Iterable[str]:
    """Suggested ``kind`` taxonomy. Soft - any string is accepted."""
    return (
        "name",  # the user's name, family, friends
        "preference",  # likes / dislikes
        "habit",  # recurring pattern
        "fact",  # a specific datum (address, allergy, birthday)
        "language",  # language preference
        "tool_use",  # how they like Emma to use tools
        "general",  # catch-all
    )
