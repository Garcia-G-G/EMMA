"""Step 40.2: the landing shows only what the local brain does today.

Each scene of the landing (_landing/index.html, the landing's own repo checked
out inside this one) carries the exact sentences it shows:

    <section class="scene" data-tool="set_volume"
             data-utterance="Baja el volumen al veinte."
             data-utterance-en="Set the volume to twenty." ...>

A scene without ``hidden`` must be decided locally, by that tool, in both
languages. Scenes the brain can't serve yet stay in the page with ``hidden``;
`python scripts/landing_scenes.py` says which of them would now pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.slots import Vocab
from core.brain_host import build_index, thresholds_from_settings, tool_docs, vocab
from tests.brain_slot_fixtures import APPS, CONNECTIONS, NOTES, PAGES, PLAYLISTS

LANDING = Path(__file__).resolve().parents[1] / "_landing" / "index.html"
EMBEDDER = Embedder()
pytestmark = [
    pytest.mark.skipif(not LANDING.exists(), reason="_landing/ not checked out"),
    pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing"),
]

_SCENE = re.compile(r"<section\b(?=[^>]*\bclass=\"[^\"]*\bscene\b)[^>]*>")


def scenes(html: str) -> list[dict[str, str | bool]]:
    out: list[dict[str, str | bool]] = []
    for tag in _SCENE.findall(html):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if "data-tool" in attrs:
            out.append({**attrs, "hidden": bool(re.search(r"\shidden(\s|>|=)", tag))})
    return out


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("brain") / "brain.db")
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    v = Vocab(apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
              playlists=base.playlists, connections=base.connections, contacts=base.contacts)
    yield Brain(tool_docs(available_only=False), v, thresholds_from_settings(), index, EMBEDDER)
    index.close()


def test_the_landing_has_a_day_of_scenes() -> None:
    found = scenes(LANDING.read_text(encoding="utf-8"))
    assert len(found) >= 4, "no scenes with data-tool/data-utterance found"
    assert sum(not s["hidden"] for s in found) >= 3


def test_every_visible_scene_is_served_locally(brain: Brain) -> None:
    failures = []
    for s in scenes(LANDING.read_text(encoding="utf-8")):
        if s["hidden"]:
            continue
        for key in ("data-utterance", "data-utterance-en"):
            d = brain.decide(str(s[key]))
            if d.verdict != "local" or d.chosen != s["data-tool"]:
                failures.append(f"{s[key]!r}: {d.verdict}/{d.reason} chose {d.chosen}, expected {s['data-tool']}")
    assert not failures, "visible scenes the brain doesn't serve (hide them):\n" + "\n".join(failures)
