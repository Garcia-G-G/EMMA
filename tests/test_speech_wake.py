"""Vosk always-on wake engine — pure matcher + engine dispatch (no audio/model)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core import speech_wake as sw
from core import wake_word


@pytest.mark.parametrize("text", ["hey emma", "oye emma", "hola emma", "emma", "EY EMMA", "óye émma"])
def test_matches_wake_accepts_phrases(text: str) -> None:
    assert sw.matches_wake(text) is True


@pytest.mark.parametrize("text", ["", "[unk]", "hola que tal", "tema general", "llama"])
def test_matches_wake_rejects_non_wake(text: str) -> None:
    assert sw.matches_wake(text) is False


def test_normalize_strips_accents_and_case() -> None:
    assert sw._normalize("ÓYE ÉMMA") == "oye emma"


def test_grammar_contains_phrases_and_unk() -> None:
    import json

    g = json.loads(sw._GRAMMAR)
    assert "hey emma" in g and "[unk]" in g


@pytest.mark.asyncio
async def test_dispatch_routes_vosk_engine(monkeypatch) -> None:
    monkeypatch.setattr(wake_word.settings, "WAKE_WORD_ENGINE", "vosk")
    called = AsyncMock()
    monkeypatch.setattr(sw, "listen", called)
    await wake_word.listen_for_wake_word()
    called.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_engine_falls_back_to_openwakeword(monkeypatch) -> None:
    monkeypatch.setattr(wake_word.settings, "WAKE_WORD_ENGINE", "nonsense")
    fallback = AsyncMock()
    monkeypatch.setattr(wake_word, "_listen_openwakeword", fallback)
    await wake_word.listen_for_wake_word()
    fallback.assert_awaited_once()
