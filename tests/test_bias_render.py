"""Phase 19.5-A — keyword-list bias rendering behind a config flag."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core import vocabulary


class TestBiasRender:
    def test_prompt_mode_space_joined_and_bounded(self):
        out = vocabulary.bias_render("prompt", 100)
        assert len(out) <= 100
        assert ", " not in out  # space-joined, not comma list

    def test_prompt_mode_byte_identical_to_legacy(self):
        # No regression vs the pre-19.5 expression.
        legacy = " ".join(vocabulary.bias_words())[:500]
        assert vocabulary.bias_render("prompt", 500) == legacy

    def test_keywords_mode_prefix_and_bound(self):
        out = vocabulary.bias_render("keywords", 200)
        assert out.startswith("Keywords: ")
        assert len(out) <= 200
        assert ", " in out  # comma-separated keyword list (real dict has many)

    def test_keywords_mode_never_exceeds_budget_even_when_union_overflows(self, monkeypatch):
        # Flood the word pool; the renderer must still bound to budget.
        monkeypatch.setattr(
            vocabulary, "bias_words", lambda: [f"VocabWord{i:03d}" for i in range(300)]
        )
        out = vocabulary.bias_render("keywords", 250)
        assert out.startswith("Keywords: ")
        assert len(out) <= 250


class TestSessionPropertiesIntegration:
    @pytest.mark.asyncio
    async def test_default_settings_no_regression(self, monkeypatch):
        import core.conversation as conv

        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        sp = await conv._build_session_properties()
        tr = sp.audio.input.transcription
        assert tr.model == "whisper-1"
        assert tr.prompt == " ".join(vocabulary.bias_words())[:500]  # byte-for-byte

    @pytest.mark.asyncio
    async def test_keywords_flag_switches_model_and_format(self, monkeypatch):
        import core.conversation as conv

        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        with (
            patch.object(conv.settings, "REALTIME_TRANSCRIPTION_MODEL", "gpt-realtime-whisper"),
            patch.object(conv.settings, "REALTIME_BIAS_MODE", "keywords"),
        ):
            sp = await conv._build_session_properties()
        tr = sp.audio.input.transcription
        assert tr.model == "gpt-realtime-whisper"
        assert tr.prompt.startswith("Keywords: ")
        assert len(tr.prompt) <= 500
