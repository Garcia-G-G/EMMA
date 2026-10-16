"""Long-term memory: a small fact store at ``settings.MEMORY_DB_PATH``.

Facts are short, durable statements about the user (preferences, names,
recurring patterns) written either explicitly via the
``remember_fact`` tool or implicitly by the reflection step at the end
of each turn. They are read back into every turn's system prompt via
:func:`priming_block` so Emma starts each session knowing what she
already knows.

Storage: SQLite via stdlib ``sqlite3`` wrapped in ``asyncio.to_thread``.
``facts`` holds the text; ``facts_vec`` (sqlite-vec) holds a local embedding per
fact from memory's own model (Step 42: no OpenAI, nothing leaves the Mac);
``memory_meta`` records which model made those vectors, so a model change
re-embeds instead of mixing spaces (``_migrate_vectors``).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite_vec
import structlog

from config.settings import settings
from core import redaction
from memory import embeddings

log = structlog.get_logger("emma.memory.long_term")

# Thresholds (cosine similarity) for memory's local model, measured on
# tests/memory_recall_set.py (Step 42). sqlite-vec returns cosine *distance*.
# Dedup: a contradiction can embed at 0.962 ("talla 42" / "talla 43"); at 0.97
# no contradiction and no distinct fact merges (8/20 restatements do). Structured
# facts dedup exactly by (subject, attribute, value) instead (Step 42 Part 3).
_DEDUP_MIN_SIM = 0.97
# Recall floor for an ANSWER: at 0.77 no unanswerable question got a fact on the
# set (Part 1, cosine gate tuned for 0 wrong).
_RECALL_MIN_SIM = 0.77
# Ranking floor for priming context (orders facts for a model; answers nothing).
_RANK_MIN_SIM = 0.30
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
    times_observed INTEGER NOT NULL DEFAULT 1,
    vault_ref      TEXT,
    superseded_at  REAL,
    supersedes     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
CREATE INDEX IF NOT EXISTS idx_facts_conf ON facts(confidence DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
    embedding FLOAT[{_EMBED_DIMS}] distance_metric=cosine
);
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_migrated: set[str] = set()  # database paths already checked this process


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
    vault_ref: str | None = None  # Keychain label when the value is Secret-tier
    superseded_at: float | None = None  # set when a contradicting fact replaced it
    supersedes: int | None = None  # id of the fact this one replaced


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns to a pre-existing facts table that lacks them.

    ``CREATE TABLE IF NOT EXISTS`` won't alter an existing table, so older
    DBs need one-time ALTERs. Idempotent.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "times_observed" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN times_observed INTEGER NOT NULL DEFAULT 1")
    if "vault_ref" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN vault_ref TEXT")
    # Prompt 25: supersession (conflict-resolution). superseded_at = when this
    # fact was replaced; supersedes = the id of the fact this one replaced.
    if "superseded_at" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN superseded_at REAL")
    if "supersedes" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN supersedes INTEGER")


def _connect() -> sqlite3.Connection:
    path = Path(settings.MEMORY_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Reflection runs as a fire-and-forget background task and can write
    # concurrently with explicit `remember()` tool calls (each on its own
    # `to_thread` worker + connection). Without these PRAGMAs the second
    # writer gets an immediate "database is locked" and silently drops the
    # fact. WAL lets readers/writers coexist; busy_timeout makes a competing
    # writer wait instead of erroring.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # sqlite-vec is a loadable extension; every connection that touches
    # facts_vec must load it first.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if str(path) not in _migrated:
        _migrate_vectors(conn, path)
        _migrated.add(str(path))
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def _migrate_vectors(conn: sqlite3.Connection, path: Path) -> None:
    """Make ``facts_vec`` hold this model's vectors, never another's (Step 42).

    A store whose vectors came from another model (the OpenAI 1536-dim ones, or
    none recorded) gets: a backup copy of the file (0600), the vector table
    dropped and recreated at this model's size, and the model recorded. Facts
    are never touched; their vectors are re-made locally by
    :func:`backfill_embeddings` (at boot). Idempotent.
    """
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_meta'").fetchone()
    model = conn.execute(
        "SELECT value FROM memory_meta WHERE key='embed_model'").fetchone() if has_meta else None
    has_vec = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='facts_vec'").fetchone() is not None
    if model is not None and model[0] == embeddings.EMBED_MODEL:
        return
    if has_vec:
        n = int(conn.execute("SELECT count(*) FROM facts").fetchone()[0])
        if n:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = path.with_name(f"{path.name}.backup-{stamp}-pre-local-vectors")
            with sqlite3.connect(str(backup)) as dst:
                conn.backup(dst)
            backup.chmod(0o600)
            log.info("memory_vectors_backup", path=str(backup), facts=n)
        conn.execute("DROP TABLE facts_vec")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES ('embed_model', ?)",
                 (embeddings.EMBED_MODEL,))
    conn.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES ('embed_dim', ?)",
                 (str(embeddings.EMBED_DIMS),))
    conn.commit()
    log.info("memory_vectors_migrated", model=embeddings.EMBED_MODEL, dim=embeddings.EMBED_DIMS)


def _serialize(vec: list[float]) -> bytes:
    """Pack a float vector into sqlite-vec's binary format."""
    data: bytes = sqlite_vec.serialize_float32(vec)
    return data


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
        vault_ref=row["vault_ref"] if "vault_ref" in keys else None,
        superseded_at=row["superseded_at"] if "superseded_at" in keys else None,
        supersedes=row["supersedes"] if "supersedes" in keys else None,
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
        return int(cur.lastrowid or 0)


def _remember_with_vec_sync(
    content: str,
    kind: str,
    confidence: float,
    source: str,
    vec: list[float],
    vault_ref: str | None = None,
) -> int:
    """Semantic upsert: bump the nearest fact if cosine sim >= _DEDUP_MIN_SIM,
    else insert a new fact + its embedding. `vec` is pre-computed by the
    async caller (embedding is a network call; SQL stays in this thread).

    Secret-tier facts (``vault_ref`` set) skip dedup entirely: their content
    is a near-identical placeholder, so cosine dedup would wrongly merge two
    different secrets into one row.
    """
    now = time.time()
    qv = _serialize(vec)
    with _connect() as conn:
        if vault_ref is None:
            nearest = conn.execute(
                "SELECT rowid, distance FROM facts_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
                (qv,),
            ).fetchone()
            # Only merge into an ACTIVE, non-secret fact. The vec0 MATCH ranks
            # across every row, so the nearest neighbour can be a superseded
            # fact (bumping it would resurrect dead state and drop the new
            # fact) or a secret placeholder (folding a personal fact into a
            # secret row). Re-hydrate the candidate and skip dedup for those.
            if nearest is not None and (1.0 - float(nearest["distance"])) >= _DEDUP_MIN_SIM:
                rid = int(nearest["rowid"])
                eligible = conn.execute(
                    "SELECT 1 FROM facts WHERE id = ? "
                    "AND vault_ref IS NULL AND superseded_at IS NULL",
                    (rid,),
                ).fetchone()
            else:
                eligible = None
            if nearest is not None and eligible is not None:
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
            "last_seen_at, times_observed, vault_ref) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (content, kind, confidence, source, now, now, vault_ref),
        )
        rid = int(cur.lastrowid or 0)
        conn.execute("INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)", (rid, qv))
        return rid


def _nearest_active_fact_sync(query_vec: bytes) -> tuple[Fact, float] | None:
    """The nearest NON-secret, NON-superseded fact to ``query_vec`` + its cosine
    similarity. None if the store is empty or the nearest is secret/superseded."""
    with _connect() as conn:
        knn = conn.execute(
            "SELECT rowid, distance FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
            (query_vec,),
        ).fetchall()
        for row in knn:
            frow = conn.execute(
                "SELECT * FROM facts WHERE id = ? AND vault_ref IS NULL AND superseded_at IS NULL",
                (row["rowid"],),
            ).fetchone()
            if frow is not None:
                return _row_to_fact(frow), 1.0 - float(row["distance"])
    return None


def _supersede_insert_sync(
    old_id: int, content: str, kind: str, confidence: float, source: str,
    vec: list[float] | None = None,
) -> int:
    """Mark ``old_id`` superseded and insert ``content`` as its replacement.

    Never deletes — the old fact stays on disk with ``superseded_at`` set so the
    review CLI can show (and undo) the change. The replacement records
    ``supersedes = old_id``.
    """
    now = time.time()
    with _connect() as conn:
        conn.execute("UPDATE facts SET superseded_at = ? WHERE id = ?", (now, old_id))
        cur = conn.execute(
            "INSERT INTO facts (content, kind, confidence, source, created_at, "
            "last_seen_at, times_observed, supersedes) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (content, kind, confidence, source, now, now, old_id),
        )
        rid = int(cur.lastrowid or 0)
        if vec is not None:
            conn.execute(
                "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)", (rid, _serialize(vec))
            )
    return rid


def _recall_sync(query: str | None, limit: int) -> list[Fact]:
    with _connect() as conn:
        if query:
            rows = conn.execute(
                """
                SELECT * FROM facts
                WHERE vault_ref IS NULL AND superseded_at IS NULL
                  AND (content LIKE ? OR kind LIKE ?)
                ORDER BY confidence DESC, last_seen_at DESC
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM facts
                WHERE vault_ref IS NULL AND superseded_at IS NULL
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
                "SELECT * FROM facts WHERE id = ? AND vault_ref IS NULL AND superseded_at IS NULL",
                (row["rowid"],),
            ).fetchone()
            if frow is not None:
                out.append(_row_to_fact(frow))
    return out


def _recall_vec_ranked_sync(query_vec: bytes, knn_limit: int) -> list[tuple[Fact, float]]:
    """Like ``_recall_vec_sync`` but returns (fact, similarity) pairs so the
    caller can rank by ``confidence * sim`` (used by the semantic priming block)."""
    out: list[tuple[Fact, float]] = []
    with _connect() as conn:
        knn = conn.execute(
            "SELECT rowid, distance FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_vec, knn_limit),
        ).fetchall()
        for row in knn:
            sim = 1.0 - float(row["distance"])
            if sim < _RANK_MIN_SIM:
                continue
            frow = conn.execute(
                "SELECT * FROM facts WHERE id = ? AND vault_ref IS NULL AND superseded_at IS NULL",
                (row["rowid"],),
            ).fetchone()
            if frow is not None:
                out.append((_row_to_fact(frow), sim))
    return out


def _forget_sync(content_or_id: str | int) -> int:
    with _connect() as conn:
        if isinstance(content_or_id, int):
            cur = conn.execute("DELETE FROM facts WHERE id = ?", (content_or_id,))
            conn.execute("DELETE FROM facts_vec WHERE rowid = ?", (content_or_id,))
        else:
            cur = conn.execute(
                "DELETE FROM facts WHERE content = ? OR content LIKE ?",
                (content_or_id, f"%{content_or_id}%"),
            )
        return int(cur.rowcount)


def _forget_semantic_sync(query_vec: bytes, k: int = 8) -> int:
    """Delete the SINGLE best-matching non-secret fact (cosine >= _RECALL_MIN_SIM).

    Destructive deletes are deliberately conservative. Deleting *every* fact
    above recall's permissive 0.20 floor wiped many loosely-related facts in
    testing (one "olvida lo del editor" removed 7 facts, including unrelated
    ones), because that floor is tuned for *ranked* recall, not for a one-shot
    delete. So we embed the query like recall does but remove only the nearest
    match — "forget X" deletes the one fact X most clearly refers to; the user
    can repeat to remove more. Secret-tier facts are never touched (only
    ``forget_secret`` removes those), and the fact's embedding is deleted
    alongside it so facts_vec never orphans.
    """
    with _connect() as conn:
        knn = conn.execute(
            "SELECT rowid, distance FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_vec, k),
        ).fetchall()
        for r in knn:  # nearest-first
            if (1.0 - float(r["distance"])) < _RECALL_MIN_SIM:
                break
            rid = int(r["rowid"])
            row = conn.execute(
                "SELECT id FROM facts WHERE id = ? AND vault_ref IS NULL AND superseded_at IS NULL",
                (rid,),
            ).fetchone()
            if row is None:
                continue  # secret-tier, superseded, or already gone — try next
            conn.execute("DELETE FROM facts WHERE id = ?", (rid,))
            conn.execute("DELETE FROM facts_vec WHERE rowid = ?", (rid,))
            return 1
    return 0


def _count_sync() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()
    return int(row["n"]) if row else 0


# ---------- async wrappers (for the orchestrator + tool layer) ----------


async def remember(
    content: str,
    *,
    kind: str = "general",
    confidence: float = 0.7,
    source: str = "explicit",
    vault_ref: str | None = None,
) -> int:
    """Store a fact, deduplicating semantically (cosine >= _DEDUP_MIN_SIM).

    Embeds the content on this Mac, then upserts: a near-duplicate bumps the
    existing fact's confidence/observation count instead of inserting a row.
    Without the memory model installed, the fact is still kept (exact-text
    dedup, no vector); the next boot's backfill embeds it.

    If ``vault_ref`` is set, the fact is Secret-tier: the value lives in
    Keychain under that label, and we store ONLY a placeholder as content —
    so the secret never lands in ``memory.db``, never gets embedded (which
    would ship it to the embedding API), and never reaches the priming block.
    """
    if vault_ref:
        content = f"(stored as secret: {vault_ref})"
    content = content.strip()
    if not content:
        return -1
    # LAUNCH-11 Part 2: credential shapes never reach the DB or a vector. vault_ref
    # is the deliberate path for a secret; this is the guard for the accidental one.
    content = redaction.redact(content)
    try:
        vec = await embeddings.embed(content)
    except embeddings.EmbedderMissingError:
        log.warning("memory_embedder_missing", stored="without_vector")
        return await asyncio.to_thread(_remember_sync, content, kind, confidence, source)
    # Contradictions are decided by structure (same subject + attribute), not by
    # similarity: Step 42 measured that no embedder tells a restatement from a
    # changed value. See memory.structured (Step 42 Part 3).
    return await asyncio.to_thread(
        _remember_with_vec_sync, content, kind, confidence, source, vec, vault_ref
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
    qvec = await embeddings.embed(redaction.redact(query))  # egress: don't ship a dictated secret
    return await asyncio.to_thread(_recall_vec_sync, _serialize(qvec), limit)


async def forget(content_or_id: str | int) -> int:
    """Delete facts by id (exact row) or by content (semantic).

    An integer id deletes that exact row. A string is embedded and matched with
    the same vec0 KNN + ``_RECALL_MIN_SIM`` floor as :func:`recall`, so
    "olvida lo del editor" removes the fact "¿qué editor uso?" would have found —
    keyword ``LIKE`` (the old behavior) matched nothing for paraphrases.
    Secret-tier facts are never touched. Returns the number of facts deleted.
    """
    if isinstance(content_or_id, int):
        return await asyncio.to_thread(_forget_sync, content_or_id)
    text = str(content_or_id).strip()
    if not text:
        return 0
    vec = await embeddings.embed(redaction.redact(text))  # egress: don't ship a dictated secret
    return await asyncio.to_thread(_forget_semantic_sync, _serialize(vec))


def _forget_recent_sync(cutoff: float) -> int:
    with _connect() as conn:
        # Only auto-LEARNED (reflection) facts — "borra lo que acabo de decir" purges
        # what Emma inferred this turn, NOT an explicit remember_fact the user
        # deliberately set seconds ago (e.g. "recuérdame que soy alérgico").
        rows = conn.execute(
            "SELECT id FROM facts WHERE created_at >= ? AND source = 'reflection'", (cutoff,)
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        for i in ids:
            conn.execute("DELETE FROM facts WHERE id = ?", (i,))
            conn.execute("DELETE FROM facts_vec WHERE rowid = ?", (i,))
        return len(ids)


async def forget_recent(within_s: float) -> int:
    """Delete every fact created within the last ``within_s`` seconds.

    Backs "borra lo que acabo de decir": purge whatever Emma just learned in the
    recent turn(s). Deletes ALL tiers created in the window (a secret placeholder
    row too — its orphaned Keychain ``fact_*`` entry is unreferenced and harmless).
    Returns the number of facts removed.
    """
    cutoff = time.time() - max(0.0, float(within_s))
    return await asyncio.to_thread(_forget_recent_sync, cutoff)


async def count() -> int:
    return await asyncio.to_thread(_count_sync)


# ---------- supersession review (Prompt 25-C) ---------------------------


def _stats_sync() -> tuple[int, int]:
    with _connect() as conn:
        active = int(
            conn.execute("SELECT COUNT(*) FROM facts WHERE superseded_at IS NULL").fetchone()[0]
        )
        superseded = int(
            conn.execute("SELECT COUNT(*) FROM facts WHERE superseded_at IS NOT NULL").fetchone()[0]
        )
    return active, superseded


def _supersessions_sync(limit: int) -> list[dict[str, Any]]:
    """The most-recent supersession pairs (replacement joined to the fact it
    replaced), newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT n.id AS new_id, n.content AS new_content, "
            "o.id AS old_id, o.content AS old_content, o.superseded_at AS at "
            "FROM facts n JOIN facts o ON n.supersedes = o.id "
            "WHERE n.supersedes IS NOT NULL ORDER BY o.superseded_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _undo_supersede_sync(new_id: int) -> bool:
    """Revert ONE supersession: reactivate the old fact, delete the replacement
    (and its embedding). Returns False if ``new_id`` isn't a replacement."""
    with _connect() as conn:
        row = conn.execute("SELECT supersedes FROM facts WHERE id = ?", (new_id,)).fetchone()
        if row is None or row["supersedes"] is None:
            return False
        old_id = int(row["supersedes"])
        conn.execute("UPDATE facts SET superseded_at = NULL WHERE id = ?", (old_id,))
        conn.execute("DELETE FROM facts WHERE id = ?", (new_id,))
        conn.execute("DELETE FROM facts_vec WHERE rowid = ?", (new_id,))
    return True


async def stats() -> tuple[int, int]:
    """(active_facts, superseded_facts)."""
    return await asyncio.to_thread(_stats_sync)


async def recent_supersessions(limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_supersessions_sync, limit)


async def undo_supersede(new_id: int) -> bool:
    return await asyncio.to_thread(_undo_supersede_sync, new_id)


# ---------- system-prompt priming ---------------------------------------


async def _priming_facts(n: int, context: str | None) -> list[Fact]:
    """Top-N facts to prime the prompt with. When ``context`` is given (warm
    sessions carry recent turns), rank semantically by ``confidence * sim`` so a
    paraphrase of what the user is talking about surfaces; otherwise — and on any
    embedding failure — fall back to flat confidence order (Prompt 25-A)."""
    if context and context.strip():
        try:
            qvec = await embeddings.embed(context)
            ranked = await asyncio.to_thread(
                _recall_vec_ranked_sync, _serialize(qvec), max(n * 2, 20)
            )
            if ranked:
                ranked.sort(key=lambda fs: fs[0].confidence * fs[1], reverse=True)
                return [f for f, _ in ranked[:n]]
        except Exception as exc:
            log.warning("priming_semantic_failed", error=str(exc))
    return await recall(limit=n)


async def priming_block(top_n: int | None = None, context: str | None = None) -> str:
    """Return a short block of known facts to inject into the system prompt.

    Empty string when the store has nothing yet - keeps the prompt
    clean for fresh installs. ``context`` (recent conversation) enables
    paraphrase-aware semantic ranking; without it, flat confidence order.
    """
    n = top_n if top_n is not None else settings.MEMORY_PRIMING_TOP_N
    facts = await _priming_facts(n, context)
    if not facts:
        return ""
    lines = [f"- {f.content}" for f in facts]
    # Keep the generic user label consistently cased so prompt substitution
    # catches it and no private identity can reach another user's session.
    return "WHAT YOU ALREADY KNOW ABOUT the user (long-term memory):\n" + "\n".join(lines)


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

    Local (memory's own model): no network. Idempotent: a fully-embedded store
    backfills nothing. Without the model installed it embeds nothing and says so.
    """
    missing = await asyncio.to_thread(_missing_vec_rows_sync)
    total = len(missing)
    if total == 0:
        log.info("memory_backfill_progress", done=0, total=0)
        return (0, 0)
    if not embeddings.available():
        log.warning("memory_backfill_waiting_for_model", missing=total)
        return (0, total)
    done = 0
    for start in range(0, total, batch_size):
        chunk = missing[start : start + batch_size]
        embedded: list[tuple[int, list[float]]] = []
        for rid, content in chunk:
            try:
                vec = await embeddings.embed(content)
                embedded.append((rid, vec))
            except Exception as exc:
                log.warning("memory_backfill_embed_failed", id=rid, error=type(exc).__name__)
        if embedded:
            await asyncio.to_thread(_insert_vecs_sync, embedded)
        done += len(embedded)
        log.info("memory_backfill_progress", done=done, total=total)
    return (done, total)


# ---------- one-shot paraphrase consolidation ---------------------------


def _consolidate_sync(
    facts: list[Fact], vecs: dict[int, list[float]], threshold: float
) -> dict[str, Any]:
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
    collapsed: list[dict[str, Any]] = []
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
            collapsed.append({"kept_id": keeper, "deleted_ids": others, "count": len(members)})
        after = int(conn.execute("SELECT count(*) FROM facts").fetchone()[0])
    return {"before": before, "after": after, "collapsed": collapsed}


async def consolidate_paraphrases(threshold: float = _DEDUP_MIN_SIM) -> dict[str, Any]:
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
    # Step 42: the backfill is local (memory's own model), so it runs on every
    # backend and never touches the network.
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
