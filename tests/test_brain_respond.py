"""BRAIN-1 Part 6 — template responses, confirmation and clarification, no LLM."""

from __future__ import annotations

import itertools
import random
import string

import pytest

from core.brain.decision import Decision, SlotValue
from core.brain.respond import (
    _ACTION,
    _TOOL,
    Responder,
    answer_clarification,
    answer_confirmation,
    language,
)
from core.brain_host import labelled_examples

L1_L2_TOOLS = sorted({
    t for e in labelled_examples() if e.brain_path in ("L1", "L2")
    for t in e.accept_tools if t in _TOOL
})


def _local(tool: str, utterance: str = "Hey Emma, x", confirm: bool = False,
           **slots: object) -> Decision:
    return Decision(
        utterance=utterance, normalized=utterance.lower(), verdict="local", reason="ok",
        chosen=tool, needs_confirmation=confirm,
        slots={k: SlotValue(v, "test", 1.0, str(v)) for k, v in slots.items()},  # type: ignore[arg-type]
    )


def test_every_routed_l1_l2_tool_has_4_to_6_phrasings_per_language() -> None:
    assert len(L1_L2_TOOLS) >= 30
    for tool in L1_L2_TOOLS:
        for lang in ("es", "en"):
            n = len(_TOOL[tool][lang])
            assert 4 <= n <= 6, f"{tool}/{lang}: {n} phrasings"


def test_templates_only_name_slots_the_tool_has() -> None:
    for tool, by_lang in _TOOL.items():
        for lang, options in by_lang.items():
            for t in options:
                fields = {f for _, f, _, _ in string.Formatter().parse(t) if f}
                assert all(f.isidentifier() for f in fields), (tool, lang, t)


def test_no_immediate_repeat() -> None:
    r = Responder(random.Random(7))
    d = _local("open_application", name="Spotify")
    said = [r.say(d, "es") for _ in range(60)]
    assert all(a != b for a, b in itertools.pairwise(said))
    assert len(set(said)) >= 4  # the variation is real


def test_language_follows_the_utterance() -> None:
    assert language("Hey Emma, abre la app de música") == "es"
    assert language("Hey Emma, search the web for the weather in San José tomorrow.") == "en"
    r = Responder(random.Random(1))
    assert "Spotify" in r.say(_local("open_application", "Emma, open spotify", name="Spotify"))


def test_confirmation_names_the_concrete_action() -> None:
    r = Responder(random.Random(3))
    q = r.say(_local("delete_note", "borra mi nota compras", confirm=True, title="Compras"), "es")
    assert "Borrar la nota Compras" in q and "delete_note" not in q
    q = r.say(_local("run_command", "corre uptime", confirm=True, command="uptime"), "es")
    assert "uptime" in q


def test_noise_says_nothing_and_defer_is_not_ours() -> None:
    r = Responder()
    noise = Decision("KMO", "kmo", "none", "noise:single_token")
    assert r.say(noise) == ""
    with pytest.raises(ValueError):
        r.say(Decision("abre eso", "abre eso", "defer", "anaphora"))


def test_refusals_are_one_calm_sentence() -> None:
    r = Responder(random.Random(5))
    for reason in ("safety:destroy_home", "safety:secret_aloud", "unsupported:phone_call"):
        line = r.say(Decision("x", "x", "refuse", reason), "es")
        assert line.count(".") == 1 and len(line) < 40, line


def test_clarify_asks_by_verb() -> None:
    r = Responder(random.Random(2))
    assert "busc" in r.say(Decision("Hey Emma, búscame algo.", "búscame algo", "clarify",
                                    "vague:no_object"), "es").lower()
    close_q = r.say(Decision("Hey Emma, cierra todo.", "cierra todo", "clarify",
                             "vague:no_object"), "es").lower()
    assert "cierr" in close_q or "cerrar" in close_q


# ---- the confirmation turn (the brain's ONLY confirmed=True) ------------------------------


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        ("sí", "execute"), ("Sí, bórrala", "execute"), ("dale", "execute"), ("yes", "execute"),
        ("no", "cancel"), ("mejor no", "cancel"), ("cancela", "cancel"), ("no, por favor", "cancel"),
        ("¿qué hora es?", "unclear"), ("borra la nota compras, sí", "unclear"),
    ],
)
def test_confirmation_turn(answer: str, kind: str) -> None:
    pending = _local("delete_note", confirm=True, title="Compras")
    out = answer_confirmation(pending, answer)
    assert out.kind == kind
    assert out.confirmed is (kind == "execute")
    assert out.tool == "delete_note" and out.slots == {"title": "Compras"}


def test_confirmation_needs_a_pending_question() -> None:
    with pytest.raises(ValueError):
        answer_confirmation(_local("open_application", name="Spotify"), "sí")


# ---- the clarification turn ---------------------------------------------------------------

OPTS = ["Juan Pérez", "Juan García (oficina)", "Juana"]


@pytest.mark.parametrize(
    ("answer", "index"),
    [
        ("el primero", 0), ("la segunda", 1), ("el 3", 2), ("the last one", 2),
        ("Juan Pérez", 0), ("el de la oficina", 1), ("García", 1),
        ("Juan", None),  # two Juans equally close: ask again, don't guess
        ("el séptimo", None), ("ninguno de esos", None),
    ],
)
def test_clarification_turn(answer: str, index: int | None) -> None:
    assert answer_clarification(OPTS, answer) == index


def test_options_are_numbered() -> None:
    line = Responder(random.Random(4)).options(OPTS, "es")
    assert "1. Juan Pérez" in line and "3. Juana" in line


def test_every_risky_l1_l2_tool_has_an_action_phrase() -> None:
    from core.brain.sinks import is_sink
    from core.brain_host import tool_docs

    docs = {d.name: d for d in tool_docs()}
    risky = [t for t in L1_L2_TOOLS if docs[t].destructive or is_sink(t)]
    missing = [t for t in risky if t not in _ACTION]
    assert not missing, f"confirmation would read a tool name: {missing}"
