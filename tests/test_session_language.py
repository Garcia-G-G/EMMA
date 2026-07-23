"""B20 (19.6): the session greeting language is pinned from user_profile
BEFORE the model hears anything — never English-first for a Spanish user."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core.conversation as conv


@pytest.fixture(autouse=True)
def _no_memory(monkeypatch):
    monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))


def _profile(lang: str):
    return {
        "display_name": "",
        "full_name": "",
        "github_username": "",
        "linkedin": "",
        "website": "",
        "preferred_lang": lang,
    }


class TestSessionLanguagePin:
    @pytest.mark.asyncio
    async def test_spanish_profile_pins_spanish_greeting(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("es"))
        text = await conv._build_instructions()
        assert "first sentence MUST be in Spanish" in text

    @pytest.mark.asyncio
    async def test_english_profile_pins_english_greeting(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("en"))
        text = await conv._build_instructions()
        assert "first sentence MUST be in English" in text

    @pytest.mark.asyncio
    async def test_empty_profile_defaults_to_spanish(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile(""))
        text = await conv._build_instructions()
        assert "first sentence MUST be in Spanish" in text

    @pytest.mark.asyncio
    async def test_directive_comes_before_personality(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("es"))
        text = await conv._build_instructions()
        assert text.index("# Session language") < text.index("# Personality")

    @pytest.mark.asyncio
    async def test_per_turn_mirroring_rule_survives(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("es"))
        text = await conv._build_instructions()
        assert "If unsure, default to Spanish" in text
        # the mirroring rule survives; the user's name is now parameterized (Part 1)
        assert "SAME language" in text and "just spoke" in text
        assert "the user" in text


class TestContextSeed:
    def test_spanish_seed_message(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("es"))
        msgs = conv._session_seed_messages()
        assert len(msgs) == 1
        assert "Spanish" in msgs[0]["content"]

    def test_english_profile_has_no_spanish_seed(self, monkeypatch):
        monkeypatch.setattr(conv.dictionary, "user_profile", lambda: _profile("en"))
        assert conv._session_seed_messages() == []
