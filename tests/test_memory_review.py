"""Prompt 25-C: the supersession review CLI + undo round-trip."""

from __future__ import annotations

import asyncio
import time

import pytest

import memory.long_term as lt
from emma.memory import review


@pytest.fixture
def tmp_mem(tmp_path, monkeypatch):
    monkeypatch.setattr(lt.settings, "MEMORY_DB_PATH", tmp_path / "m.db")
    with lt._connect():
        pass
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _seed_supersession() -> tuple[int, int]:
    """Insert an old (superseded) fact + its replacement; return (old, new)."""
    now = time.time()
    with lt._connect() as conn:
        old = int(
            conn.execute(
                "INSERT INTO facts (content, kind, confidence, source, created_at, "
                "last_seen_at, times_observed, superseded_at) "
                "VALUES ('Garcia prefiere VSCode', 'preference', 0.9, 'test', ?, ?, 1, ?)",
                (now, now, now),
            ).lastrowid
        )
        new = int(
            conn.execute(
                "INSERT INTO facts (content, kind, confidence, source, created_at, "
                "last_seen_at, times_observed, supersedes) "
                "VALUES ('Garcia prefiere Zed', 'preference', 0.9, 'test', ?, ?, 1, ?)",
                (now, now, old),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
            (new, lt._serialize([0.1] * lt._EMBED_DIMS)),
        )
    return old, new


def test_review_prints_stats_and_pairs(tmp_mem, capsys):
    _seed_supersession()
    rc = asyncio.run(review._run(undo_id=None, limit=20))
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 active fact(s), 1 superseded" in out
    assert "Garcia prefiere VSCode" in out and "Garcia prefiere Zed" in out


def test_undo_reactivates_old_and_removes_new(tmp_mem, capsys):
    old, new = _seed_supersession()
    rc = asyncio.run(review._run(undo_id=new, limit=20))
    assert rc == 0
    with lt._connect() as conn:
        old_row = conn.execute("SELECT superseded_at FROM facts WHERE id = ?", (old,)).fetchone()
        new_row = conn.execute("SELECT 1 FROM facts WHERE id = ?", (new,)).fetchone()
        vec_row = conn.execute("SELECT 1 FROM facts_vec WHERE rowid = ?", (new,)).fetchone()
    assert old_row["superseded_at"] is None  # reactivated
    assert new_row is None and vec_row is None  # replacement + its embedding gone


def test_undo_on_non_supersession_is_noop(tmp_mem, capsys):
    rc = asyncio.run(review._run(undo_id=99999, limit=20))
    assert rc == 1
    assert "not a supersession" in capsys.readouterr().out
