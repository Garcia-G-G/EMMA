"""Supersession helpers (Prompt 25-B) after Step 42.

The gpt-4o-mini contradiction classifier is gone: no model decides that a new
fact replaces an old one, and similarity never does either (Step 42 measured
that no embedder tells a restatement from a changed value). Structured facts
supersede deterministically (memory.structured, Part 3). These tests keep the
SQL helpers and read filters honest, against a real tmp DB. No network.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import memory.long_term as lt
from memory.long_term import Fact


@pytest.fixture
def tmp_mem(tmp_path, monkeypatch):
    monkeypatch.setattr(lt.settings, "MEMORY_DB_PATH", tmp_path / "m.db")
    with lt._connect():
        pass
    # network-free embed (the supersede decision is stubbed independently)
    monkeypatch.setattr(lt.embeddings, "embed", AsyncMock(return_value=[0.1] * lt._EMBED_DIMS))
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _seed(content: str, *, confidence: float = 0.9, vec: list[float] | None = None) -> int:
    now = time.time()
    with lt._connect() as conn:
        cur = conn.execute(
            "INSERT INTO facts (content, kind, confidence, source, created_at, "
            "last_seen_at, times_observed) VALUES (?, 'preference', ?, 'test', ?, ?, 1)",
            (content, confidence, now, now),
        )
        rid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
            (rid, lt._serialize(vec or [0.1] * lt._EMBED_DIMS)),
        )
    return rid


def _superseded_at(fid: int):
    with lt._connect() as conn:
        return conn.execute("SELECT superseded_at FROM facts WHERE id = ?", (fid,)).fetchone()[0]


def _fact(fid: int, content: str, conf: float = 0.9) -> Fact:
    return Fact(fid, content, "preference", conf, "test", 0.0, 0.0)


class TestMigration:
    def test_columns_idempotent(self, tmp_mem):
        with lt._connect():  # second connect re-runs _ensure_columns
            pass
        with lt._connect() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(facts)")]
        assert "superseded_at" in cols and "supersedes" in cols


class TestSupersedeInsert:
    def test_marks_old_and_links_new(self, tmp_mem):
        old = _seed("the user prefiere VSCode")
        new = lt._supersede_insert_sync(
            old, "the user prefiere Zed", "preference", 0.9, "test", [0.2] * lt._EMBED_DIMS
        )
        assert _superseded_at(old) is not None
        with lt._connect() as conn:
            row = conn.execute("SELECT supersedes FROM facts WHERE id = ?", (new,)).fetchone()
            has_vec = conn.execute("SELECT 1 FROM facts_vec WHERE rowid = ?", (new,)).fetchone()
        assert row["supersedes"] == old and has_vec is not None


class TestReadFilters:
    def test_recall_excludes_superseded(self, tmp_mem):
        keep = _seed("the user prefiere Zed")
        gone = _seed("the user prefiere VSCode")
        with lt._connect() as conn:
            conn.execute("UPDATE facts SET superseded_at = ? WHERE id = ?", (time.time(), gone))
        active = lt._recall_sync(None, 50)
        assert [f.id for f in active] == [keep]


class TestNoSimilaritySupersede:
    def test_a_contradiction_by_similarity_alone_keeps_both(self, tmp_mem):
        half = lt._EMBED_DIMS // 2  # cosine ~0.95 with the new fact's vector: close, not equal
        old = _seed("the user prefiere VSCode", confidence=0.9, vec=[0.1] * half + [0.2] * half)
        new = asyncio.run(lt.remember("the user prefiere Zed", kind="preference", confidence=0.9))
        assert new != old and _superseded_at(old) is None
        assert {f.content for f in asyncio.run(lt.recall(limit=20))} == {
            "the user prefiere VSCode", "the user prefiere Zed"}

    def test_no_model_client_in_long_term(self):
        assert not hasattr(lt, "_classify_contradiction") and not hasattr(lt, "_get_client")
