"""Step 43 Part 4 — a Thinker's tool call runs like a brain decision, or not at all.

No LLM: ``FakeThinker`` returns whatever the test says. Its proposal goes through
``Brain.admit`` (the gate's argument and risk rules), then the same confirmation
and the same Host as a local decision.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.brain.brain import Brain
from core.brain.decision import Decision, SlotScalar
from core.brain.processor import BrainProcessor
from core.brain.slots import Vocab
from core.brain.thinker import ThinkerResult, Turn
from core.brain_host import thresholds_from_settings, tool_docs
from tests.test_brain_processor import FakeHost, _say

BRAIN = Brain(tool_docs(available_only=False), Vocab(), thresholds_from_settings())


class FakeThinker:
    def __init__(self, tool: str | None, **args: SlotScalar) -> None:
        self.result = ThinkerResult("lo pienso", tool, dict(args))
        self.turns: list[Turn] = []

    async def think(self, turn: Turn) -> ThinkerResult:
        self.turns.append(turn)
        return self.result


def _proc(thinker: FakeThinker, host: FakeHost, brain: Brain | None = BRAIN) -> BrainProcessor:
    defer = lambda t: Decision(t, t, "defer", "novel")  # noqa: E731 — every turn reaches the thinker
    return BrainProcessor(lambda: defer, host=host, thinker=thinker,
                          admit=lambda: brain.admit if brain else None)


@pytest.mark.asyncio
async def test_a_reversible_proposal_executes_through_the_host() -> None:
    host = FakeHost()
    said = await _say(_proc(FakeThinker("set_volume", percent=30), host), "algo raro con el volumen")
    assert host.calls == [("set_volume", {"percent": 30})]
    assert said and "30" in said[0]


@pytest.mark.asyncio
async def test_a_risky_proposal_asks_first_then_a_yes_runs_it() -> None:
    host = FakeHost()
    proc = _proc(FakeThinker("delete_note", title="Compras"), host)
    assert await _say(proc, "quita esa nota de compras") == ["¿Borrar la nota Compras?"]
    assert host.calls == [] and host.asked_for == ["delete_note"]
    await _say(proc, "sí")
    assert host.calls == [("delete_note", {"title": "Compras", "confirmed": True})]


@pytest.mark.parametrize(("tool", "args"), [
    ("delete_note", {}),  # required title missing
    ("delete_note", {"title": "  "}),  # empty is missing
    ("set_volume", {"percent": 30, "loudly": True}),  # an argument the tool doesn't take
    ("delete_note", {"title": "Compras", "confirmed": True}),  # only a spoken yes confirms
    ("no_such_tool", {}),
    ("__llm__", {}),
])
@pytest.mark.asyncio
async def test_an_inadmissible_proposal_runs_nothing(tool: str, args: dict[str, Any]) -> None:
    host = FakeHost()
    said = await _say(_proc(FakeThinker(tool, **args), host), "haz algo")
    assert host.calls == [] and host.asked_for == []
    assert said == ["lo pienso"]


@pytest.mark.asyncio
async def test_no_brain_loaded_means_the_thinker_cannot_act() -> None:
    host = FakeHost()
    said = await _say(_proc(FakeThinker("set_volume", percent=30), host, brain=None), "volumen")
    assert host.calls == [] and said == ["lo pienso"]


@pytest.mark.asyncio
async def test_the_thinker_gets_ranked_facts() -> None:
    host = FakeHost(facts=["mi café favorito es el cortado", "trabajo los viernes"])
    t = FakeThinker(None)
    await _say(_proc(t, host), "qué café me gusta")
    assert t.turns and t.turns[0].facts == ("mi café favorito es el cortado",)
