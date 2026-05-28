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

import sqlite_vec
import structlog

from config.settings import settings
from memory import embeddings

log = structlog.get_logger("emma.memory.long_term")

# Semantic thresholds (cosine similarity). sqlite-vec returns cosine
# *distance* = 1 - similarity, so sim >= X  <=>  distance <= 1 - X.
# Dedup threshold. Spec said 0.92, but measured cosine sims for genuine
# paraphrases under text-embedding-3-small run 0.52-0.86 (e.g. neovim
# paraphrases=0.82), while distinct facts top out ~0.64 (Zed vs neovim).
# 0.92 merges almost nothing; 0.75 sits in the gap — merges paraphrases,
# keeps Zed!=neovim. See PR notes (CP6) for the full measurement.
_DEDUP_MIN_SIM = 0.75  # near-duplicate => bump existing instead of inserting
# Recall floor. text-embedding-3-small yields LOW absolute cosine sims
# (short query vs full sentence, cross-language), so the spec's 0.40 cut
# correctly-ranked top hits. 0.20 keeps the relevant fact (always ranked
# #1 in testing) while still rejecting true noise. See CP5 in PR notes.
_RECALL_MIN_SIM = 0.20
_EMBED_DIMS = embeddings.EMBED_DIMS

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS facts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    content        TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'general',
    confidence     REAL NOT NULL DEFAULT 0.7,
    source         TEXT NOT NULL DEFAULT 'explicit',
    created_at     REAL NOT NULL,
    last_seen_at   REAL NOT NULL,
    times_observed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
CREATE INDEX IF NOT EXISTS idx_facts_conf ON facts(confidence DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
    embedding FLOAT[{_EMBED_DIMS}] distance_metric=cosine
);
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
    times_observed: int = 1


def _ensure_times_observed(conn: sqlite3.Connection) -> None:
    """Add the times_observed column to a pre-semantic-era facts table.

    ``CREATE TABLE IF NOT EXISTS`` won't alter an existing table, so DBs
    created before this column existed need a one-time ALTER. Idempotent.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "times_observed" not in cols:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN times_observed INTEGER NOT NULL DEFAULT 1"
        )


def _connect() -> sqlite3.Connection:
    path = Path(settings.MEMORY_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # sqlite-vec is a loadable extension; every connection that touches
    # facts_vec must load it first.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA)
    _ensure_times_observed(conn)
    return conn


def _serialize(vec: list[float]) -> bytes:
    """Pack a float vector into sqlite-vec's binary format."""
    return sqlite_vec.serialize_float32(vec)


def _row_to_fact(row: sqlite3.Row) -> Fact:
    keys = row.keys()
    return Fact(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        confidence=row["confidence"],
        source=row["source"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        times_observed=row["times_observed"] if "times_observed" in keys else 1,
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


def _remember_with_vec_sync(
    content: str, kind: str, confidence: float, source: str, vec: list[float]
) -> int:
    """Semantic upsert: bump the nearest fact if cosine sim >= _DEDUP_MIN_SIM,
    else insert a new fact + its embedding. `vec` is pre-computed by the
    async caller (embedding is a network call; SQL stays in this thread).
    """
    now = time.time()
    qv = _serialize(vec)
    with _connect() as conn:
        nearest = conn.execute(
            "SELECT rowid, distance FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
            (qv,),
        ).fetchone()
        if nearest is not None and (1.0 - float(nearest["distance"])) >= _DEDUP_MIN_SIM:
            rid = int(nearest["rowid"])
            conn.execute(
                "UPDATE facts SET times_observed = times_observed + 1, "
                "confidence = MIN(confidence + 0.05, 1.0), last_seen_at = ?, source = ? "
                "WHERE id = ?",
                (now, source, rid),
            )
            return rid
        cur = conn.execute(
            "INSERT INTO facts (content, kind, confidence, source, created_at, "
            "last_seen_at, times_observed) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (content, kind, confidence, source, now, now),
        )
        rid = int(cur.lastrowid)
        conn.execute("INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)", (rid, qv))
        return rid


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


def _recall_vec_sync(query_vec: bytes, limit: int) -> list[Fact]:
    """Semantic recall: KNN over facts_vec, filtered by cosine similarity.

    KNN runs on facts_vec alone (vec0 requires LIMIT/k on its own query;
    a JOIN+LIMIT is rejected), then we hydrate the Fact rows by id.
    """
    with _connect() as conn:
        knn = conn.execute(
            "SELECT rowid, distance FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_vec, limit),
        ).fetchall()
        out: list[Fact] = []
        for row in knn:
            sim = 1.0 - float(row["distance"])  # cosine distance -> similarity
            if sim < _RECALL_MIN_SIM:
                continue
            frow = conn.execute(
                "SELECT * FROM facts WHERE id = ?", (row["rowid"],)
            ).fetchone()
            if frow is not None:
                out.append(_row_to_fact(frow))
    return out


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
    """Store a fact, deduplicating semantically (cosine >= _DEDUP_MIN_SIM).

    Embeds the content (async), then upserts: a near-duplicate bumps the
    existing fact's confidence/observation count instead of inserting a row.
    """
    content = content.strip()
    if not content:
        return -1
    vec = await embeddings.embed(content)
    return await asyncio.to_thread(
        _remember_with_vec_sync, content, kind, confidence, source, vec
    )


async def recall(query: str | None = None, *, limit: int = 5) -> list[Fact]:
    """Recall facts relevant to `query` via semantic (cosine) search.

    With no query, returns the highest-confidence facts (used by
    :func:`priming_block`) — that path stays embedding-free. With a
    query, embeds it and runs a vec0 KNN, keeping only matches with
    cosine similarity >= ``_RECALL_MIN_SIM``.
    """
    if not query or not query.strip():
        return await asyncio.to_thread(_recall_sync, None, limit)
    qvec = await embeddings.embed(query)
    return await asyncio.to_thread(_recall_vec_sync, _serialize(qvec), limit)


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


# ---------- embedding backfill ------------------------------------------


def _missing_vec_rows_sync() -> list[tuple[int, str]]:
    """Facts that have no row in facts_vec yet (need embedding)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT f.id, f.content FROM facts f "
            "LEFT JOIN facts_vec v ON v.rowid = f.id "
            "WHERE v.rowid IS NULL ORDER BY f.id"
        ).fetchall()
    return [(int(r["id"]), r["content"]) for r in rows]


def _insert_vecs_sync(items: list[tuple[int, list[float]]]) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
            [(rid, _serialize(vec)) for rid, vec in items],
        )


async def backfill_embeddings(batch_size: int = 10) -> tuple[int, int]:
    """Embed every fact lacking a facts_vec entry. Returns (done, total).

    Batched to avoid one giant API spike. Idempotent: a fully-embedded
    store backfills nothing.
    """
    missing = await asyncio.to_thread(_missing_vec_rows_sync)
    total = len(missing)
    if total == 0:
        log.info("memory_backfill_progress", done=0, total=0)
        return (0, 0)
    done = 0
    for start in range(0, total, batch_size):
        chunk = missing[start : start + batch_size]
        embedded: list[tuple[int, list[float]]] = []
        for rid, content in chunk:
            try:
                vec = await embeddings.embed(content)
                embedded.append((rid, vec))
            except Exception as exc:
                log.warning("memory_backfill_embed_failed", id=rid, error=str(exc))
        if embedded:
            await asyncio.to_thread(_insert_vecs_sync, embedded)
        done += len(embedded)
        log.info("memory_backfill_progress", done=done, total=total)
    return (done, total)


# ---------- one-shot paraphrase consolidation ---------------------------


def _consolidate_sync(
    facts: list[Fact], vecs: dict[int, list[float]], threshold: float
) -> dict:
    """Union-find cluster by cosine >= threshold, collapse each cluster."""
    from collections import defaultdict

    ids = [f.id for f in facts if f.id in vecs]
    by_id = {f.id: f for f in facts}
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a_idx in range(len(ids)):
        for b_idx in range(a_idx + 1, len(ids)):
            ia, ib = ids[a_idx], ids[b_idx]
            if embeddings.cosine(vecs[ia], vecs[ib]) >= threshold:
                union(ia, ib)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        clusters[find(i)].append(i)

    before = len(facts)
    collapsed: list[dict] = []
    with _connect() as conn:
        for members in clusters.values():
            if len(members) < 2:
                continue
            # keeper: highest confidence, tie-break oldest (lowest id)
            keeper = sorted(members, key=lambda i: (-by_id[i].confidence, i))[0]
            others = [i for i in members if i != keeper]
            total_obs = sum(by_id[i].times_observed for i in members)
            new_conf = min(by_id[keeper].confidence + 0.05 * (len(members) - 1), 1.0)
            conn.execute(
                "UPDATE facts SET confidence = ?, times_observed = ? WHERE id = ?",
                (new_conf, total_obs, keeper),
            )
            conn.executemany("DELETE FROM facts WHERE id = ?", [(i,) for i in others])
            conn.executemany("DELETE FROM facts_vec WHERE rowid = ?", [(i,) for i in others])
            log.info(
                "cluster_collapsed",
                kept_id=keeper,
                kept_content=by_id[keeper].content,
                deleted_ids=others,
                count=len(members),
            )
            collapsed.append(
                {"kept_id": keeper, "deleted_ids": others, "count": len(members)}
            )
        after = int(conn.execute("SELECT count(*) FROM facts").fetchone()[0])
    return {"before": before, "after": after, "collapsed": collapsed}


async def consolidate_paraphrases(threshold: float = _DEDUP_MIN_SIM) -> dict:
    """One-shot: collapse near-duplicate fact clusters (cosine >= threshold).

    Keeps the highest-confidence representative per cluster (tie -> oldest),
    sums times_observed into it, bumps its confidence by 0.05*(n-1), and
    deletes the rest from both facts and facts_vec. Returns
    ``{'before', 'after', 'collapsed': [...]}``.
    """
    facts = await asyncio.to_thread(_recall_sync, None, 100_000)
    if len(facts) < 2:
        return {"before": len(facts), "after": len(facts), "collapsed": []}
    vecs: dict[int, list[float]] = {}
    for f in facts:
        try:
            vecs[f.id] = await embeddings.embed(f.content)
        except Exception as exc:
            log.warning("consolidate_embed_failed", id=f.id, error=str(exc))
    return await asyncio.to_thread(_consolidate_sync, facts, vecs, threshold)


def initialize() -> None:
    """Create schema (incl. facts_vec) + backfill embeddings at startup.

    Stays synchronous because ``emma/__main__.py`` calls it before the
    event loop starts. The embedding backfill is async, so we drive it
    with ``asyncio.run`` when no loop is running. If a loop *is* running
    (e.g. inside a test), the schema is still created synchronously and
    callers should ``await backfill_embeddings()`` themselves.
    """
    try:
        with _connect():
            pass
    except Exception as exc:
        log.error("memory_initialize_failed", error=str(exc))
        return
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if running:
        log.info("memory_backfill_deferred_loop_running")
        return
    try:
        asyncio.run(backfill_embeddings())
    except Exception as exc:
        log.error("memory_backfill_failed", error=str(exc))


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
