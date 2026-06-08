"""Prompt 25-A: context-aware semantic priming with a flat fallback."""

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
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _seed(content: str, confidence: float) -> None:
    now = time.time()
    with lt._connect() as conn:
        conn.execute(
            "INSERT INTO facts (content, kind, confidence, source, created_at, "
            "last_seen_at, times_observed) VALUES (?, 'general', ?, 'test', ?, ?, 1)",
            (content, confidence, now, now),
        )


def test_ranks_by_confidence_times_sim(tmp_mem, monkeypatch):
    low_conf_high_sim = Fact(1, "A", "general", 0.3, "test", 0.0, 0.0)  # 0.3*0.9 = 0.27
    high_conf_low_sim = Fact(2, "B", "general", 0.9, "test", 0.0, 0.0)  # 0.9*0.5 = 0.45
    monkeypatch.setattr(lt.embeddings, "embed", AsyncMock(return_value=[0.1] * lt._EMBED_DIMS))
    monkeypatch.setattr(
        lt,
        "_recall_vec_ranked_sync",
        lambda qv, k: [(low_conf_high_sim, 0.9), (high_conf_low_sim, 0.5)],
    )
    block = asyncio.run(lt.priming_block(top_n=2, context="editor"))
    assert block.index("- B") < block.index("- A")  # 0.45 outranks 0.27


def test_embed_failure_falls_back_to_flat(tmp_mem, monkeypatch):
    _seed("Garcia toma café por las mañanas", 0.9)
    monkeypatch.setattr(lt.embeddings, "embed", AsyncMock(side_effect=RuntimeError("net down")))
    block = asyncio.run(lt.priming_block(top_n=5, context="cualquier cosa"))
    assert "Garcia toma café por las mañanas" in block


def test_no_context_uses_flat_path(tmp_mem, monkeypatch):
    _seed("Garcia vive en Monterrey", 0.9)
    called = {"semantic": False}

    def _spy(qv, k):
        called["semantic"] = True
        return []

    monkeypatch.setattr(lt, "_recall_vec_ranked_sync", _spy)
    block = asyncio.run(lt.priming_block(top_n=5, context=None))
    assert "Garcia vive en Monterrey" in block
    assert called["semantic"] is False
