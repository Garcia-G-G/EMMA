"""Prompt 25-B: reflection conflict-resolution (supersede on contradiction).

The supersede DECISION (nearest-in-band + gpt-4o-mini contradiction) is unit-
tested by stubbing the nearest lookup and the classifier; the SQL helpers and
read-path filtering run against a real tmp DB. No network.
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
        old = _seed("Garcia prefiere VSCode")
        new = lt._supersede_insert_sync(
            old, "Garcia prefiere Zed", "preference", 0.9, "test", [0.2] * lt._EMBED_DIMS
        )
        assert _superseded_at(old) is not None
        with lt._connect() as conn:
            row = conn.execute("SELECT supersedes FROM facts WHERE id = ?", (new,)).fetchone()
            has_vec = conn.execute("SELECT 1 FROM facts_vec WHERE rowid = ?", (new,)).fetchone()
        assert row["supersedes"] == old and has_vec is not None


class TestReadFilters:
    def test_recall_excludes_superseded(self, tmp_mem):
        keep = _seed("Garcia prefiere Zed")
        gone = _seed("Garcia prefiere VSCode")
        with lt._connect() as conn:
            conn.execute("UPDATE facts SET superseded_at = ? WHERE id = ?", (time.time(), gone))
        active = lt._recall_sync(None, 50)
        assert [f.id for f in active] == [keep]


class TestRememberSupersedePath:
    def test_contradiction_in_band_supersedes(self, tmp_mem, monkeypatch):
        old = _seed("Garcia prefiere VSCode", confidence=0.9)
        monkeypatch.setattr(
            lt, "_nearest_active_fact_sync", lambda qv: (_fact(old, "Garcia prefiere VSCode"), 0.6)
        )
        monkeypatch.setattr(lt, "_classify_contradiction", AsyncMock(return_value=True))
        new = asyncio.run(lt.remember("Garcia prefiere Zed", kind="preference", confidence=0.9))
        assert _superseded_at(old) is not None
        active = asyncio.run(lt.recall(limit=20))
        assert [f.content for f in active] == ["Garcia prefiere Zed"]
        assert new != old

    def test_publishes_fact_superseded_event(self, tmp_mem, monkeypatch):
        from core import events_bus

        old = _seed("Garcia prefiere VSCode", confidence=0.9)
        monkeypatch.setattr(
            lt, "_nearest_active_fact_sync", lambda qv: (_fact(old, "Garcia prefiere VSCode"), 0.6)
        )
        monkeypatch.setattr(lt, "_classify_contradiction", AsyncMock(return_value=True))

        async def _go():
            q = events_bus.subscribe()
            await lt.remember("Garcia prefiere Zed", kind="preference", confidence=0.9)
            events_bus.unsubscribe(q)
            return [q.get_nowait() for _ in range(q.qsize())]

        events = asyncio.run(_go())
        assert any(e["type"] == "fact_superseded" and e["old_id"] == old for e in events)

    def test_classifier_false_keeps_both(self, tmp_mem, monkeypatch):
        # orthogonal vectors so the fall-through dedup INSERTS instead of merging
        e0 = [1.0] + [0.0] * (lt._EMBED_DIMS - 1)
        e1 = [0.0, 1.0] + [0.0] * (lt._EMBED_DIMS - 2)
        old = _seed("Garcia entrena en las mañanas", vec=e0)
        monkeypatch.setattr(lt.embeddings, "embed", AsyncMock(return_value=e1))
        monkeypatch.setattr(
            lt,
            "_nearest_active_fact_sync",
            lambda qv: (_fact(old, "Garcia entrena en las mañanas"), 0.6),
        )
        monkeypatch.setattr(lt, "_classify_contradiction", AsyncMock(return_value=False))
        asyncio.run(lt.remember("Garcia prefiere Zed", kind="preference", confidence=0.9))
        assert _superseded_at(old) is None
        active = asyncio.run(lt.recall(limit=20))
        assert len(active) == 2

    def test_out_of_band_never_classifies(self, tmp_mem, monkeypatch):
        old = _seed("Garcia prefiere VSCode")
        # sim 0.9 >= dedup floor → out of supersede band; classifier must NOT run
        monkeypatch.setattr(
            lt, "_nearest_active_fact_sync", lambda qv: (_fact(old, "Garcia prefiere VSCode"), 0.9)
        )
        classifier = AsyncMock(return_value=True)
        monkeypatch.setattr(lt, "_classify_contradiction", classifier)
        asyncio.run(lt.remember("Garcia prefiere Zed", kind="preference", confidence=0.9))
        classifier.assert_not_awaited()
        assert _superseded_at(old) is None

    def test_low_confidence_does_not_supersede(self, tmp_mem, monkeypatch):
        old = _seed("Garcia prefiere VSCode", confidence=0.9)
        monkeypatch.setattr(
            lt,
            "_nearest_active_fact_sync",
            lambda qv: (_fact(old, "Garcia prefiere VSCode", 0.9), 0.6),
        )
        classifier = AsyncMock(return_value=True)
        monkeypatch.setattr(lt, "_classify_contradiction", classifier)
        # 0.5 < 0.9 * 0.8 → confidence guard blocks supersession (and the classifier)
        asyncio.run(lt.remember("Garcia prefiere Zed", kind="preference", confidence=0.5))
        classifier.assert_not_awaited()
        assert _superseded_at(old) is None


class TestClassifierSafety:
    def test_classifier_error_returns_false(self, monkeypatch):
        def _boom():
            raise RuntimeError("no client")

        monkeypatch.setattr(lt, "_get_client", _boom)
        assert asyncio.run(lt._classify_contradiction("a", "b")) is False
