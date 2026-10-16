"""VOICE-1 Part 2 — BrainProcessor executes locally, confirms by mic, defers to the Thinker."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from typing import Any

import pytest
from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.tests.utils import run_test

from core import cascade, injection, session_memory
from core.brain.confirm import Pending
from core.brain.confirm import question as confirm_question
from core.brain.decision import Decision, SlotScalar, SlotValue
from core.brain.processor import BrainProcessor, Outcome
from core.brain.respond import _NOT_YET, Responder
from core.brain.thinker import NoThinker, ThinkerResult, Turn, rank_facts
from tools.base import RegisteredTool, ToolResult


def _local(text: str, tool: str, confirm: bool = False, **slots: SlotScalar) -> Decision:
    return Decision(
        utterance=text, normalized=text, verdict="local", reason="ok", chosen=tool,
        slots={k: SlotValue(v, "test", 1.0, str(v)) for k, v in slots.items()},
        needs_confirmation=confirm,
    )


class FakeHost:
    def __init__(self, outcome: Outcome | None = None, facts: Sequence[str] = ()) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.asked_for: list[str] = []
        self._pending: Pending | None = None
        self._outcome = outcome or Outcome(True, "ok")
        self._facts = list(facts)

    async def execute(self, tool: str, args: dict[str, SlotScalar]) -> Outcome:
        self.calls.append((tool, dict(args)))
        if args.get("confirmed"):
            self._pending = None  # the guard settles on the spoken yes
        return self._outcome

    def asked(self, pending: Pending) -> None:
        self.asked_for.append(pending.tool)
        self._pending = pending

    def pending(self) -> Pending | None:
        return self._pending

    def settle(self) -> None:
        self._pending = None

    def facts(self) -> Sequence[str]:
        return self._facts


async def _say(proc: BrainProcessor, text: str) -> list[str]:
    down, _ = await run_test(proc, frames_to_send=[TranscriptionFrame(text, "u", "t")])
    return [f.text for f in down if isinstance(f, TTSSpeakFrame)]


@pytest.mark.asyncio
async def test_local_turn_executes_through_the_host_and_acks() -> None:
    host = FakeHost()
    records: list[dict[str, object]] = []
    proc = BrainProcessor(lambda: (lambda t: _local(t, "open_application", name="Safari")),
                          host=host, record=lambda d, s, e: records.append(e))
    said = await _say(proc, "Emma, abre Safari")
    assert host.calls == [("open_application", {"name": "Safari"})]
    assert said and "Safari" in said[0]
    assert records[0]["path"] == "local" and records[0]["executed"] is True


@pytest.mark.asyncio
async def test_a_failed_tool_is_never_acked_as_done() -> None:
    host = FakeHost(Outcome(False, "No encontré Safari."))
    proc = BrainProcessor(lambda: (lambda t: _local(t, "open_application", name="Safari")),
                          host=host)
    assert await _say(proc, "abre Safari") == ["No encontré Safari."]


@pytest.mark.asyncio
async def test_risky_asks_one_short_question_then_yes_executes() -> None:
    host = FakeHost()
    decisions = {"borra la nota compras": _local("borra la nota compras", "delete_note",
                                                 confirm=True, title="Compras")}
    proc = BrainProcessor(
        lambda: (lambda t: decisions.get(t, Decision(t, t, "none", "smalltalk"))), host=host)
    q = await _say(proc, "borra la nota compras")
    assert q == ["¿Borrar la nota Compras?"] and host.calls == [] and host.asked_for == ["delete_note"]
    await _say(proc, "sí")
    assert host.calls == [("delete_note", {"title": "Compras", "confirmed": True})]


@pytest.mark.asyncio
async def test_no_cancels_with_a_template_and_runs_nothing() -> None:
    host = FakeHost()
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "borra" in t else Decision(t, t, "none", "x")),
                          host=host, responder=Responder())
    await _say(proc, "borra la nota compras")
    said = await _say(proc, "no")
    assert host.calls == [] and said and said[0] in ("Va, no lo hago.", "Cancelado.",
                                                     "Listo, lo dejo.", "Entendido, nada.")


@pytest.mark.asyncio
async def test_anything_but_yes_or_no_closes_the_question_and_is_a_new_turn() -> None:
    host = FakeHost()
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(
        lambda: (lambda t: d if "borra" in t else _local(t, "open_application", name="Safari")),
        host=host)
    await _say(proc, "borra la nota compras")
    await _say(proc, "abre Safari")
    assert host.calls == [("open_application", {"name": "Safari"})]


def test_the_question_is_short_and_names_the_action() -> None:
    assert confirm_question("delete_note", {"title": "Compras"}, "es") == "¿Borrar la nota Compras?"
    assert confirm_question("delete_note", {"title": "Compras"}, "en") == "Delete the note Compras?"


# ---- the model-less confirmation path, end to end through dispatch + the guard ------------


@pytest.fixture
def fake_delete(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from tools import registry

    ran: list[dict[str, Any]] = []

    async def delete_note(title: str, confirmed: bool = False) -> ToolResult:
        if not confirmed:
            return ToolResult(False, None, f"¿Borro {title}?", requires_confirmation=True)
        ran.append({"title": title})
        return ToolResult(True, None, f"Borré {title}.")

    entry = RegisteredTool(name="delete_note", fn=delete_note, description="", parameters={},
                           destructive=True)
    real = registry.get_tool
    monkeypatch.setattr(registry, "get_tool",
                        lambda n: entry if n == "delete_note" else real(n))
    injection.install()
    yield ran
    injection.uninstall()


def _user(text: str) -> None:
    """What _UserTurnTap does for one spoken turn: VAD onset, then the words."""
    session_memory.push_event("user", "speech_started", "")
    session_memory.push_event("user", "speech", text)


@pytest.mark.asyncio
async def test_spoken_yes_deletes_through_the_real_guard(fake_delete: list[dict[str, Any]]) -> None:
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "borra" in t else Decision(t, t, "none", "x")),
                          host=cascade._Host())
    _user("borra la nota compras")
    assert await _say(proc, "borra la nota compras") == ["¿Borrar la nota Compras?"]
    await asyncio.sleep(0.01)
    _user("sí")
    said = await _say(proc, "sí")
    assert fake_delete == [{"title": "Compras"}]
    assert said and "Compras" in said[0]


@pytest.mark.asyncio
async def test_a_yes_without_a_new_utterance_is_refused_by_the_guard(
    fake_delete: list[dict[str, Any]],
) -> None:
    """The brain armed the question, but no NEW onset followed it: dispatch refuses."""
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "borra" in t else Decision(t, t, "none", "x")),
                          host=cascade._Host())
    _user("borra la nota compras, sí")  # the "sí" was in the same breath as the request
    await _say(proc, "borra la nota compras")
    said = await _say(proc, "sí")  # no session-memory onset after the question
    assert fake_delete == []
    assert said and said[0].startswith("¿")  # the guard asks again


@pytest.mark.asyncio
async def test_spoken_no_runs_nothing(fake_delete: list[dict[str, Any]]) -> None:
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "borra" in t else Decision(t, t, "none", "x")),
                          host=cascade._Host())
    _user("borra la nota compras")
    await _say(proc, "borra la nota compras")
    _user("no")
    await _say(proc, "no")
    assert fake_delete == []


# ---- defer -> Thinker ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defer_goes_to_the_thinker_with_the_whole_turn() -> None:
    seen: list[Turn] = []

    class Spy:
        async def think(self, turn: Turn) -> ThinkerResult:
            seen.append(turn)
            return ThinkerResult("pensado")

    host = FakeHost(facts=["mi café favorito es el cortado", "trabajo desde casa los viernes",
                           "soy alérgico a los mariscos"])
    calls = {"n": 0}

    def decide(t: str) -> Decision:
        calls["n"] += 1
        if calls["n"] == 1:
            return _local(t, "open_application", name="Safari")
        return Decision(t, t, "defer", "novel")

    proc = BrainProcessor(lambda: decide, host=host, thinker=Spy())
    await _say(proc, "abre Safari")
    said = await _say(proc, "pídeme un café como siempre")
    assert said == ["pensado"]
    turn = seen[0]
    assert turn.transcript == "pídeme un café como siempre"
    assert turn.decision.verdict == "defer" and turn.decision.reason == "novel"
    assert turn.facts == ("mi café favorito es el cortado",)
    assert [c.tool for c in turn.context] == ["open_application"]


@pytest.mark.asyncio
async def test_no_thinker_says_not_yet_and_logs_the_decision() -> None:
    records: list[tuple[Decision, dict[str, object]]] = []
    proc = BrainProcessor(lambda: (lambda t: Decision(t, t, "defer", "disagree")),
                          record=lambda d, s, e: records.append((d, e)))
    said = await _say(proc, "resume mis correos")
    assert said[0] in _NOT_YET["es"]
    d, extra = records[0]
    assert d.reason == "disagree" and extra["path"] == "thinker"
    assert extra["thinker"] == "NoThinker" and extra["defer_reason"] == "disagree"


def test_not_yet_varies_and_never_sounds_like_an_error() -> None:
    for lang in ("es", "en"):
        lines = _NOT_YET[lang]
        assert 4 <= len(lines) <= 6 and len(set(lines)) == len(lines)
        for line in lines:
            low = line.lower()
            assert not any(w in low for w in ("no entend", "error", "didn't understand",
                                              "didn't get", "falló"))
    r = Responder()
    picks = [r.not_yet("es") for _ in range(6)]
    assert all(a != b for a, b in itertools.pairwise(picks))  # never twice in a row


@pytest.mark.asyncio
async def test_no_thinker_uses_the_utterance_language() -> None:
    t = await NoThinker().think(Turn("open my emails and summarize them",
                                     Decision("x", "x", "defer", "novel")))
    assert t.text in _NOT_YET["en"] and t.tool is None


def test_facts_are_ranked_by_relevance_not_confidence() -> None:
    facts = ["vivo con dos gatos", "mi editor es Zed", "prefiero el café sin azúcar"]
    assert rank_facts("hazme un café", facts) == ("prefiero el café sin azúcar",)
    assert rank_facts("abre el editor", facts) == ("mi editor es Zed",)
    assert rank_facts("hola", facts) == ()
    assert rank_facts("x", []) == ()


@pytest.mark.asyncio
async def test_a_session_ending_tool_ends_after_its_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import registry

    async def shutdown_emma() -> ToolResult:
        return ToolResult(True, None, "Hasta luego.", ends_session=True)

    entry = RegisteredTool(name="shutdown_emma", fn=shutdown_emma, description="", parameters={})
    monkeypatch.setattr(registry, "get_tool", lambda n: entry)
    host = cascade._Host()
    out = await host.execute("shutdown_emma", {})
    assert out.success and host.end_after_speech is True
