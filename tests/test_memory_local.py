"""Step 42 Part 2 — memory vectors are local; old OpenAI vectors migrate, facts never lost."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
import sqlite_vec

import memory.long_term as lt
from memory import embeddings


def _old_store(path: Path, n: int = 3) -> None:
    """A store as Step 40 left it: 1536-dim OpenAI vectors, no model recorded."""
    c = sqlite3.connect(str(path))
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.executescript(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'general', confidence REAL NOT NULL DEFAULT 0.7, "
        "source TEXT NOT NULL DEFAULT 'explicit', created_at REAL NOT NULL, "
        "last_seen_at REAL NOT NULL);"
        "CREATE VIRTUAL TABLE facts_vec USING vec0(embedding FLOAT[1536] distance_metric=cosine);")
    for i in range(n):
        c.execute("INSERT INTO facts (content, kind, confidence, source, created_at, last_seen_at) "
                  "VALUES (?, 'preference', 0.9, 'reflection', ?, ?)", (f"fact {i}", time.time(), time.time()))
        c.execute("INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
                  (i + 1, sqlite_vec.serialize_float32([0.1] * 1536)))
    c.commit()
    c.close()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memory.db"
    monkeypatch.setattr(lt.settings, "MEMORY_DB_PATH", path)
    lt._migrated.discard(str(path))
    return path


def test_old_vectors_are_backed_up_dropped_and_rebuilt_at_the_local_size(db: Path) -> None:
    _old_store(db)
    with lt._connect() as conn:
        facts = [r["content"] for r in conn.execute("SELECT content FROM facts ORDER BY id")]
        meta = dict(conn.execute("SELECT key, value FROM memory_meta").fetchall())
        assert conn.execute("SELECT count(*) FROM facts_vec").fetchone()[0] == 0
        conn.execute("INSERT INTO facts_vec(rowid, embedding) VALUES (1, ?)",
                     (lt._serialize([0.1] * embeddings.EMBED_DIMS),))  # the new size fits
    assert facts == ["fact 0", "fact 1", "fact 2"]  # never touched
    assert meta == {"embed_model": embeddings.EMBED_MODEL, "embed_dim": str(embeddings.EMBED_DIMS)}
    backups = list(db.parent.glob("memory.db.backup-*-pre-local-vectors"))
    assert len(backups) == 1 and oct(backups[0].stat().st_mode & 0o777) == "0o600"
    with sqlite3.connect(str(backups[0])) as b:
        assert b.execute("SELECT count(*) FROM facts").fetchone()[0] == 3


def test_migration_is_idempotent(db: Path) -> None:
    _old_store(db)
    with lt._connect():
        pass
    lt._migrated.discard(str(db))  # a new process
    with lt._connect():
        pass
    assert len(list(db.parent.glob("memory.db.backup-*"))) == 1


def test_a_fresh_store_records_the_model_and_needs_no_backup(db: Path) -> None:
    with lt._connect() as conn:
        assert conn.execute("SELECT value FROM memory_meta WHERE key='embed_model'").fetchone()[0] \
            == embeddings.EMBED_MODEL
    assert not list(db.parent.glob("memory.db.backup-*"))


@pytest.mark.skipif(not embeddings.available(), reason="memory embedder not installed")
def test_backfill_embeds_every_fact_locally(db: Path) -> None:
    _old_store(db)
    done, total = asyncio.run(lt.backfill_embeddings())
    assert (done, total) == (3, 3)
    assert asyncio.run(lt.backfill_embeddings()) == (0, 0)


def test_without_the_model_a_fact_is_still_kept(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(text: str) -> list[float]:
        raise embeddings.EmbedderMissingError("not installed")

    monkeypatch.setattr(embeddings, "embed_sync", missing)
    rid = asyncio.run(lt.remember("prefiero Zed para programar", kind="preference"))
    assert rid > 0
    assert [f.content for f in asyncio.run(lt.recall(limit=5))] == ["prefiero Zed para programar"]
    assert asyncio.run(lt.backfill_embeddings())[1] >= 1  # waiting for the model, not lost


@pytest.mark.skipif(not embeddings.available(), reason="memory embedder not installed")
def test_recall_answers_only_above_the_floor(db: Path) -> None:
    for text in ("mi perro se llama Toby", "prefiero Zed para programar", "soy vegetariano"):
        asyncio.run(lt.remember(text))
    # The floor is strict on purpose (0 wrong on the set): a close paraphrase passes
    # (0.79); "what's my dog's name?" (0.73) is the structured path's job (Part 3).
    assert [f.content for f in asyncio.run(lt.recall("el nombre de mi perro"))] == ["mi perro se llama Toby"]
    assert asyncio.run(lt.recall("¿cuál es mi canción favorita?")) == []
