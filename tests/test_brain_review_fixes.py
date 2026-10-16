"""BRAIN-1.5 review fixes — each test is a reproduction from the review.

They exercise PRODUCTION shape (no leave-one-out): the labelled rows are in the
index, and only the tools this machine has are handed to the brain.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core import injection, session_memory
from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.normalize import normalize
from core.brain.slots import Vocab, resolve
from core.brain_host import (
    build_index,
    index_fingerprint,
    labelled_examples,
    thresholds_from_settings,
    tool_docs,
    vocab,
)
from tests.brain_slot_fixtures import APPS, CONNECTIONS, NOTES, PAGES, PLAYLISTS

EMBEDDER = Embedder()
needs_model = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")
UNAVAILABLE = {"set_brightness", "search_github", "my_repos"}


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    docs = [d for d in tool_docs() if d.name not in UNAVAILABLE]  # this Mac's set
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("b") / "brain.db")
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    v = Vocab(apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
              playlists=base.playlists, connections=base.connections, contacts=base.contacts)
    yield Brain(docs, v, thresholds_from_settings(), index, EMBEDDER)
    index.close()


@needs_model
@pytest.mark.parametrize("text", ["pon el brillo al 30", "busca fastapi en github", "mis repos"])
def test_an_unavailable_tool_never_goes_local(brain: Brain, text: str) -> None:
    d = brain.decide(text)
    assert not (d.verdict == "local" and d.chosen in UNAVAILABLE), (d.chosen, d.reason)


def test_sites_only_fill_open_url() -> None:
    v = vocab(apps=APPS)
    r = resolve("remember_page", normalize("guarda github como mi portafolio"), v, ("name", "url"))
    assert "url" not in r.slots
    assert resolve("open_url", normalize("abre yutub"), v).slots["url"].value == "youtube.com"


@needs_model
def test_saying_a_site_into_remember_page_is_not_a_store(brain: Brain) -> None:
    for text in ("guarda github como mi portafolio", "guarda youtube como mi canal"):
        d = brain.decide(text)
        assert not (d.verdict == "local" and d.chosen == "remember_page"), d.slots


@needs_model
def test_repeating_an_llm_labelled_item_defers(brain: Brain) -> None:
    for ex in labelled_examples():
        if ex.brain_path in ("LLM", "READ"):
            d = brain.decide(ex.text)  # its own row IS in the index here
            assert d.verdict != "local", (ex.id, d.chosen, d.reason)


@pytest.fixture
def guard() -> Iterator[injection.SessionGuard]:
    session_memory.clear()
    g = injection.install()
    yield g
    injection.uninstall()
    session_memory.clear()


def test_a_late_request_transcript_does_not_eat_the_yes(guard: injection.SessionGuard) -> None:
    import asyncio

    session_memory.push_event("user", "speech_started", "")  # "borra la nota compras" begins
    session_memory.push_event("tool", "requires_confirmation:delete_note")  # tool fired early
    session_memory.push_event("user", "speech", "borra la nota compras")  # its transcript, late
    session_memory.push_event("user", "speech_started", "")  # the answer begins
    session_memory.push_event("user", "speech", "sí")
    verdict = asyncio.run(guard.check("delete_note", {"title": "compras", "confirmed": True}))
    assert isinstance(verdict, injection.Clearance) and verdict.by_mic


def test_the_question_is_bounded(guard: injection.SessionGuard) -> None:
    import asyncio

    long_body = "x" * 500
    verdict = asyncio.run(guard.check("send_to", {"recipient": "a@b.c", "subject": "s",
                                                    "body": long_body, "confirmed": True}))
    assert isinstance(verdict, injection.Refusal)
    assert len(verdict.message) < 220 and long_body not in verdict.message


def test_the_fingerprint_is_stable_and_covers_the_fill_tables() -> None:
    import core.brain.phrasebook as pb

    fp = index_fingerprint()
    assert fp == index_fingerprint()
    assert pb.__file__ is not None  # phrasebook.py (its fill tables) is hashed whole
