"""EMMA-OBVIOUS Part 5 — repeat_last + reflection blackout (session-level, no DB)."""

from __future__ import annotations

import pytest

from core import session_memory
from memory import reflection
from tools.session_actions_tool import repeat_last


@pytest.fixture(autouse=True)
def _clean():
    session_memory.clear()
    reflection._suppress_until = 0.0
    yield
    session_memory.clear()
    reflection._suppress_until = 0.0


@pytest.mark.asyncio
async def test_repeat_last_speaks_verbatim() -> None:
    session_memory.push_event("assistant", "speech", "Son las tres y cuarto de la tarde.")
    res = await repeat_last()
    assert res.success
    # spoken verbatim — NOT regenerated/reworded
    assert res.user_message == "Son las tres y cuarto de la tarde."
    assert res.data["repeated"] == "Son las tres y cuarto de la tarde."


@pytest.mark.asyncio
async def test_repeat_last_returns_most_recent_turn() -> None:
    session_memory.push_event("assistant", "speech", "Primera respuesta.")
    session_memory.push_event("user", "speech", "otra cosa")
    session_memory.push_event("assistant", "speech", "Segunda respuesta.")
    res = await repeat_last()
    assert res.user_message == "Segunda respuesta."


@pytest.mark.asyncio
async def test_repeat_last_nothing_yet() -> None:
    res = await repeat_last()
    assert res.success and res.data["repeated"] is None


def test_last_assistant_speech_text_accessor() -> None:
    assert session_memory.last_assistant_speech_text() == ""
    session_memory.push_event("assistant", "speech", "hola")
    assert session_memory.last_assistant_speech_text() == "hola"


def test_reflection_suppress_blackout() -> None:
    assert reflection._is_suppressed() is False
    reflection.suppress(30)
    assert reflection._is_suppressed() is True


@pytest.mark.asyncio
async def test_reflection_rechecks_blackout_after_classify(monkeypatch) -> None:
    # TOCTOU: if the blackout fires DURING classify (i.e. forget_last_turn ran while
    # this reflection was mid-flight), the fact must NOT be written back.
    from memory.short_term import Turn

    writes: list = []

    async def fake_remember(*a, **k):
        writes.append(a)

    async def fake_classify(_v):
        reflection.suppress(30)  # blackout fires while classifying
        return "personal"

    async def fake_reflect_once(_w):
        return [{"content": "x", "kind": "general", "confidence": 0.8}]

    monkeypatch.setattr(reflection.long_term, "remember", fake_remember)
    monkeypatch.setattr(reflection, "_classify_sensitivity", fake_classify)
    monkeypatch.setattr(reflection, "reflect_once", fake_reflect_once)
    await reflection.reflect_async([Turn(user_text="hi", assistant_text="ok", timestamp=0.0)])
    assert writes == []  # the re-check caught the blackout before the write
