"""22-B32: phase-aware barge-in — the opener always finishes; the body yields."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from core.echo_gate import EchoGateFilter, SpeechPhase

LOUD = (np.ones(480, dtype=np.int16) * 25_000).tobytes()  # rms ≫ 18000
QUIET = (np.ones(480, dtype=np.int16) * 500).tobytes()
ZEROS = b"\x00" * len(LOUD)


def _run(coro):
    return asyncio.run(coro)


class TestSpeechPhaseMachine:
    def test_initial_state_is_none(self):
        assert SpeechPhase().current() is None

    def test_first_utterance_is_opener(self):
        p = SpeechPhase()
        p.on_bot_started()
        assert p.current() == "opener"

    def test_word_count_graduates_to_body(self):
        p = SpeechPhase()
        p.on_bot_started()
        p.on_bot_text("hola garcia dime qué necesitas ahora mismo por favor")  # 9 words
        assert p.current() == "body"

    def test_elapsed_time_graduates_to_body(self, monkeypatch):
        import core.echo_gate as eg

        p = SpeechPhase()
        p.on_bot_started()
        monkeypatch.setattr(eg.time, "monotonic", lambda: p._opener_started_at + 2.0)
        assert p.current() == "body"

    def test_second_utterance_is_body_immediately(self):
        p = SpeechPhase()
        p.on_bot_started()
        p.on_bot_stopped()
        assert p.current() is None  # silent between utterances
        p.on_bot_started()
        assert p.current() == "body"  # opener already done


class TestOpenerGate:
    def _gate(self, phase: str | None):
        return EchoGateFilter(tail_ms=600, barge_in_rms=18_000.0, phase_provider=lambda: phase)

    def test_opener_blocks_even_loud_speech(self):
        g = self._gate("opener")
        g.set_bot_speaking(True)
        out = _run(g.filter(LOUD))
        assert out == ZEROS  # no barge-in during the opener, period

    def test_opener_buffers_loud_and_releases_after(self):
        g = EchoGateFilter(tail_ms=600, barge_in_rms=18_000.0)
        phase = {"v": "opener"}
        g._phase_provider = lambda: phase["v"]
        g.set_bot_speaking(True)
        assert _run(g.filter(LOUD)) == ZEROS  # buffered, not dropped
        phase["v"] = "body"
        out = _run(g.filter(LOUD))  # loud during body barges in AND releases buffer
        assert out == LOUD + LOUD  # buffered chunk prepended to the live one

    def test_opener_does_not_buffer_quiet_echo(self):
        g = self._gate("opener")
        g.set_bot_speaking(True)
        _run(g.filter(QUIET))
        assert g._opener_buffer == []

    def test_buffer_capped_at_two_seconds(self):
        g = self._gate("opener")
        g.set_bot_speaking(True)
        big = (np.ones(24_000, dtype=np.int16) * 25_000).tobytes()  # 1 s per chunk
        for _ in range(4):
            _run(g.filter(big))
        assert g._opener_buffer_bytes <= 2 * 24_000 * 2

    def test_body_phase_keeps_normal_barge_in(self):
        g = self._gate("body")
        g.set_bot_speaking(True)
        assert _run(g.filter(LOUD)) == LOUD  # barge-in works
        assert _run(g.filter(QUIET)) == ZEROS  # echo still gated

    def test_no_provider_keeps_legacy_behavior(self):
        g = EchoGateFilter(tail_ms=600, barge_in_rms=18_000.0)
        g.set_bot_speaking(True)
        assert _run(g.filter(LOUD)) == LOUD
        assert _run(g.filter(QUIET)) == ZEROS


class TestProcessorWiring:
    @pytest.mark.asyncio
    async def test_prompt_carries_opener_rule(self, monkeypatch):
        from unittest.mock import AsyncMock

        import core.conversation as conv

        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        text = await conv._build_instructions()
        assert "first sentence after wake" in text
        assert "8 words is the hard limit" in text
