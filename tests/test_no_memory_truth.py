"""Step 42 Part 0 — while memory can't store, Emma never says she noted a memory."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from config.settings import settings
from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.processor import BrainProcessor
from core.brain.respond import _NO_MEMORY, _NOT_YET
from core.brain.slots import Vocab
from core.brain_host import build_index, thresholds_from_settings, tool_docs
from tests.test_brain_processor import FakeHost, _say

EMBEDDER = Embedder()
pytestmark = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    assert settings.VOICE_BACKEND == "cascade"
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("brain") / "brain.db")
    # The live brain: only tools available here, so memory_tool is absent (LLM-backed).
    yield Brain(tool_docs(available_only=True), Vocab(), thresholds_from_settings(), index, EMBEDDER)
    index.close()


@pytest.mark.parametrize("text", [
    "Recuerda que soy alérgico a los mariscos.", "Acuérdate de que trabajo los sábados.",
    "Remember that my favourite colour is green.", "¿Qué sabes de mí?",
])
@pytest.mark.asyncio
async def test_a_memory_request_gets_the_truth(brain: Brain, text: str) -> None:
    host = FakeHost()
    said = await _say(BrainProcessor(lambda: brain.decide, host=host), text)
    assert host.calls == []
    assert said and said[0] in _NO_MEMORY["es"] + _NO_MEMORY["en"], said
    assert not any(w in said[0].lower() for w in ("apunt", "anot", "noted", "saved it"))


@pytest.mark.asyncio
async def test_other_deferred_requests_keep_the_usual_line(brain: Brain) -> None:
    said = await _say(BrainProcessor(lambda: brain.decide, host=FakeHost()),
                      "Busca vuelos baratos para el próximo mes.")
    assert said and said[0] in _NOT_YET["es"], said
