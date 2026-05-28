"""Tests for the semantic memory upgrade (embeddings + vec0 recall/dedup).

These hit the real OpenAI embeddings API (text-embedding-3-small), so they
skip when no OPENAI_API_KEY is configured. Each test runs its async body in
a single asyncio.run() and resets the embeddings client so the AsyncOpenAI
singleton is never reused across event loops.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import settings as _real_settings

pytestmark = pytest.mark.skipif(
    not _real_settings.OPENAI_API_KEY,
    reason="needs OPENAI_API_KEY for text-embedding-3-small",
)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path):
    import memory.embeddings as embeddings_mod

    db_path = tmp_path / "sem_memory.db"
    patcher = patch("memory.long_term.settings")
    mock_settings = patcher.start()
    mock_settings.MEMORY_DB_PATH = db_path
    mock_settings.MEMORY_PRIMING_TOP_N = 10
    embeddings_mod._client = None  # fresh client per test loop
    # create schema (facts + facts_vec) without the async backfill
    from memory.long_term import _connect

    with _connect():
        pass
    yield
    patcher.stop()
    embeddings_mod._client = None
    # asyncio.run() leaves the loop policy with no current loop (py3.12),
    # which breaks sibling tests that still use the deprecated
    # get_event_loop().run_until_complete() pattern. Restore a fresh one.
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_embed_and_cosine() -> None:
    from memory.embeddings import cosine, embed

    async def body() -> None:
        v1 = await embed("Garcia loves tacos al pastor")
        v2 = await embed("Garcia loves tacos al pastor")
        v3 = await embed("the weather in Tokyo is rainy")
        assert len(v1) == 1536
        assert cosine(v1, v2) > 0.99  # identical text
        assert cosine(v1, v3) < cosine(v1, v2)  # unrelated text less similar

    asyncio.run(body())


def test_recall_paraphrased_query() -> None:
    from memory.long_term import recall, remember

    async def body() -> None:
        await remember("Garcia prefiere Zed como editor de código", kind="preference")
        results = await recall("cuál es mi editor favorito", limit=3)
        assert results, "paraphrased query returned nothing"
        assert any("Zed" in f.content for f in results)

    asyncio.run(body())


def test_dedup_near_duplicate_no_new_row() -> None:
    from memory.long_term import _count_sync, remember

    async def body() -> None:
        a = await remember("Garcia usa neovim como editor principal", kind="preference")
        b = await remember("El editor principal de Garcia es neovim", kind="preference")
        assert a == b, "near-duplicate created a new row instead of bumping"

    asyncio.run(body())
    assert _count_sync() == 1


def test_consolidate_collapses_cluster() -> None:
    from memory.embeddings import embed
    from memory.long_term import _connect, _count_sync, _serialize, consolidate_paraphrases

    async def body() -> dict:
        # Insert a synthetic cluster directly (bypassing dedup) to simulate
        # pre-existing paraphrase pollution, then consolidate.
        paraphrases = [
            "Garcia tiene un perro llamado Max",
            "El perro de Garcia se llama Max",
            "Garcia's dog is named Max",
        ]
        now = time.time()
        with _connect() as conn:
            for text in paraphrases:
                vec = await embed(text)
                cur = conn.execute(
                    "INSERT INTO facts (content, kind, confidence, source, "
                    "created_at, last_seen_at, times_observed) "
                    "VALUES (?, 'fact', 0.8, 'test', ?, ?, 1)",
                    (text, now, now),
                )
                conn.execute(
                    "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, _serialize(vec)),
                )
        assert _count_sync() == 3
        return await consolidate_paraphrases()

    res = asyncio.run(body())
    assert res["before"] == 3
    assert res["after"] == 1, f"expected 1 fact after consolidation, got {res['after']}"
    assert len(res["collapsed"]) == 1
    assert res["collapsed"][0]["count"] == 3
    # kept row aggregated the observation counts
    from memory.long_term import _recall_sync

    kept = _recall_sync(None, 10)
    assert len(kept) == 1
    assert kept[0].times_observed == 3
