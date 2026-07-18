"""Tests for memory.long_term and memory.short_term."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from memory.short_term import append_turn, clear, last_turns, size


class TestShortTerm:
    def setup_method(self) -> None:
        clear()

    def test_append_and_retrieve(self) -> None:
        append_turn("Hola", "Qué onda")
        assert size() == 1
        turns = last_turns(1)
        assert len(turns) == 1
        assert turns[0].user_text == "Hola"
        assert turns[0].assistant_text == "Qué onda"

    def test_empty_turns_ignored(self) -> None:
        append_turn("", "")
        assert size() == 0
        append_turn("  ", "  ")
        assert size() == 0

    def test_ring_buffer_cap(self) -> None:
        for i in range(60):
            append_turn(f"user {i}", f"bot {i}")
        assert size() == 50

    def test_last_turns_order(self) -> None:
        append_turn("first", "a")
        append_turn("second", "b")
        append_turn("third", "c")
        turns = last_turns(2)
        assert len(turns) == 2
        assert turns[0].user_text == "second"
        assert turns[1].user_text == "third"

    def test_last_turns_zero(self) -> None:
        append_turn("x", "y")
        assert last_turns(0) == []

    def test_clear(self) -> None:
        append_turn("x", "y")
        clear()
        assert size() == 0


class TestLongTerm:
    @pytest.fixture(autouse=True)
    def _tmp_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_memory.db"
        self._patcher = patch("memory.long_term.settings")
        mock_settings = self._patcher.start()
        mock_settings.MEMORY_DB_PATH = db_path
        mock_settings.MEMORY_PRIMING_TOP_N = 10
        yield
        self._patcher.stop()

    def test_remember_and_recall(self) -> None:
        from memory.long_term import _recall_sync, _remember_sync

        fid = _remember_sync("Garcia likes tacos", "preference", 0.9, "explicit")
        assert fid > 0
        facts = _recall_sync(None, 10)
        assert len(facts) == 1
        assert facts[0].content == "Garcia likes tacos"

    def test_forget_recent_removes_only_recent(self) -> None:
        import time

        from memory import long_term
        from memory.long_term import _connect, _count_sync, _remember_sync

        _remember_sync("just learned this", "general", 0.8, "reflection")
        # an old fact, well outside the recent window, must survive
        with _connect() as conn:
            conn.execute(
                "INSERT INTO facts (content, kind, confidence, source, created_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?)",
                ("old fact", "general", 0.8, "reflection", time.time() - 10_000, time.time() - 10_000),
            )
        assert _count_sync() == 2
        removed = asyncio.get_event_loop().run_until_complete(long_term.forget_recent(120))
        assert removed == 1  # only the fresh one
        assert _count_sync() == 1

    def test_forget_last_turn_purges_and_blacks_out_reflection(self) -> None:
        from memory import long_term, reflection
        from memory.long_term import _remember_sync
        from tools.memory_tool import forget_last_turn

        reflection._suppress_until = 0.0
        _remember_sync("Garcia just said something private", "general", 0.7, "reflection")
        res = asyncio.get_event_loop().run_until_complete(forget_last_turn())
        assert res.success and res.data["removed"] >= 1
        # the in-flight reflection from the purged turn will be swallowed
        assert reflection._is_suppressed() is True
        assert asyncio.get_event_loop().run_until_complete(long_term.count()) == 0
        reflection._suppress_until = 0.0

    def test_dedup_on_same_content(self) -> None:
        from memory.long_term import _count_sync, _remember_sync

        _remember_sync("Garcia is a dev", "fact", 0.7, "reflection")
        _remember_sync("Garcia is a dev", "fact", 0.9, "explicit")
        assert _count_sync() == 1

    def test_forget(self) -> None:
        from memory.long_term import _count_sync, _forget_sync, _remember_sync

        _remember_sync("allergic to shrimp", "fact", 1.0, "explicit")
        removed = _forget_sync("shrimp")
        assert removed == 1
        assert _count_sync() == 0

    def test_recall_with_query(self) -> None:
        from memory.long_term import _recall_sync, _remember_sync

        _remember_sync("Garcia likes tacos", "preference", 0.9, "explicit")
        _remember_sync("Garcia has a cat named Luna", "fact", 0.8, "explicit")
        facts = _recall_sync("cat", 10)
        assert len(facts) == 1
        assert "cat" in facts[0].content

    def test_priming_block_empty(self) -> None:
        from memory.long_term import priming_block

        block = asyncio.get_event_loop().run_until_complete(priming_block())
        assert block == ""

    def test_priming_block_with_facts(self) -> None:
        from memory.long_term import _remember_sync, priming_block

        _remember_sync("Garcia speaks Spanish", "language", 0.9, "explicit")
        block = asyncio.get_event_loop().run_until_complete(priming_block())
        assert "Garcia speaks Spanish" in block
