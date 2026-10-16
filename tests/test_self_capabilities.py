"""Step 43.1 Part 1 — "¿Qué puedes hacer?" says only what is true on this machine."""

from __future__ import annotations

import re
from typing import Any

import pytest

from config.settings import settings
from core import cascade, injection, runtime, session_memory
from core.brain import thinker
from core.brain.decision import Decision
from core.brain.processor import BrainProcessor
from core.brain.slots import TOOLS
from core.brain_host import servable_tools, tool_docs
from tests.test_brain_processor import _local, _say, _user
from tools import self_tool

# What the LLM-free cascade can't do, in both languages, plus any tool count.
# (Memory came off this list in Step 42: it runs on the Mac now.)
FORBIDDEN = re.compile(r"web|internet|pantalla|screen|herramientas|\btools\b|\d", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _cascade(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    monkeypatch.setattr(thinker, "WIRED", False)
    yield
    runtime.set_spoken_lang("es")


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("full", [False, True])
def test_the_cascade_answer_claims_nothing_it_cannot_do(lang: str, full: bool) -> None:
    said, _ = self_tool.capabilities_answer(servable_tools(), lang, full=full)
    assert not FORBIDDEN.search(said), said
    assert said.startswith("Puedo" if lang == "es" else "I can")


def test_servable_is_framed_and_available_without_a_thinker() -> None:
    available = {d.name for d in tool_docs(available_only=True)}
    assert servable_tools() == frozenset(available & set(TOOLS))


def test_a_wired_thinker_adds_what_it_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thinker, "WIRED", True)
    assert servable_tools() == frozenset(d.name for d in tool_docs(available_only=True))


def test_every_framed_tool_is_grouped_or_deliberately_unsaid() -> None:
    grouped = set().union(*(g[2] for g in self_tool._GROUPS))
    assert set(TOOLS) - grouped - self_tool._NOT_SAID == set()


def test_short_answer_offers_more_and_the_full_one_does_not() -> None:
    every = frozenset(t for g in self_tool._GROUPS for t in g[2])
    short, more = self_tool.capabilities_answer(every, "es", full=False)
    full, again = self_tool.capabilities_answer(every, "es", full=True)
    assert more and short.endswith("¿Quieres que te diga más?") and short.count(",") <= 3
    assert not again and len(full) > len(short)


def test_nothing_servable_says_so() -> None:
    said, more = self_tool.capabilities_answer(frozenset(), "en", full=False)
    assert said == "Right now there's nothing I can do on my own here." and not more


@pytest.mark.asyncio
async def test_a_yes_to_more_gets_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    every = frozenset(t for g in self_tool._GROUPS for t in g[2])
    monkeypatch.setattr("core.brain_host.servable_tools", lambda: every)
    injection.install()
    try:
        d = _local("qué puedes hacer", "describe_capabilities")
        proc = BrainProcessor(
            lambda: (lambda t: d if "puedes" in t else Decision(t, t, "none", "smalltalk")),
            host=cascade._Host())
        _user("qué puedes hacer")
        first = await _say(proc, "qué puedes hacer")
        assert first and first[0].endswith("¿Quieres que te diga más?")
        _user("sí")
        second = await _say(proc, "sí")
        assert second and "más?" not in second[0] and len(second[0]) > len(first[0])
    finally:
        injection.uninstall()
        session_memory.clear()
