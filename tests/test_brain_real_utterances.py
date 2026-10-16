"""Step 43 Part 0 — utterances that failed for real (Step 40.2), pinned so they stay fixed.

``scripts/spike_cascade/data/labelled_real.json`` has the same shape as the 140
(``labelled.json``) but is NOT in the exam or the index: it is a regression set.
Each L1/L2 row must go local, to an accepted tool, with no confirmation when the
tool is reversible.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.brain.brain import Brain
from core.brain.decision import Decision
from core.brain.embed import Embedder
from core.brain.index import Example
from core.brain.processor import BrainProcessor
from core.brain.slots import Vocab
from core.brain_host import (
    build_index,
    labelled_examples,
    thresholds_from_settings,
    tool_docs,
    vocab,
)
from tests.brain_slot_fixtures import APPS, CONNECTIONS, NOTES, PAGES, PLAYLISTS
from tests.test_brain_processor import FakeHost, _say

REAL = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "spike_cascade"
    / "data"
    / "labelled_real.json"
)
EMBEDDER = Embedder()
pytestmark = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")
ROWS = labelled_examples(REAL)


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("brain") / "brain.db")
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    v = Vocab(
        apps=base.apps,
        aliases=base.aliases,
        pages=PAGES,
        notes=base.notes,
        playlists=base.playlists,
        connections=base.connections,
        contacts=base.contacts,
    )
    yield Brain(tool_docs(available_only=False), v, thresholds_from_settings(), index, EMBEDDER)
    index.close()


@pytest.mark.parametrize("ex", ROWS, ids=lambda e: e.id)
def test_real_utterance_goes_local(brain: Brain, ex: Example) -> None:
    d: Decision = brain.decide(ex.text)
    assert ex.brain_path in ("L1", "L2")
    assert (d.verdict, d.chosen) == ("local", ex.accept_tools[0]), (d.reason, d.features)
    assert not d.needs_confirmation, "pause/resume are reversible: they run without a yes"


@pytest.mark.asyncio
async def test_reanudala_resumes_what_was_playing(brain: Brain) -> None:
    host = FakeHost()
    proc = BrainProcessor(lambda: brain.decide, host=host)
    await _say(proc, "Pon algo tranquilo.")
    await _say(proc, "Reanúdala.")
    assert [c[0] for c in host.calls] == ["play_track", "resume"]


@pytest.mark.asyncio
async def test_reanudala_with_nothing_playing_asks_instead_of_dropping(brain: Brain) -> None:
    host = FakeHost()
    proc = BrainProcessor(lambda: brain.decide, host=host)
    said = await _say(proc, "Reanúdala.")
    assert host.calls == []
    assert said and said[0].endswith("?"), said
