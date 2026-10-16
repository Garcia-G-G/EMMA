"""Step 43 Part 2 — one pending confirmation, one question, in the turn's language."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from config.settings import Settings
from core import cascade, injection, runtime
from core.brain.confirm import Pending, question
from core.brain.decision import Decision
from core.brain.gate import Thresholds
from core.brain.processor import BrainProcessor
from core.brain_host import thresholds_from_settings
from tests.test_brain_processor import _local, _say, _user, fake_delete  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_lang() -> Any:
    yield
    runtime.set_spoken_lang("es")


def test_threshold_defaults_are_the_settings_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert Thresholds() == Thresholds(
        knn_min=s.BRAIN_KNN_MIN, margin_min=s.BRAIN_MARGIN_MIN,
        max_content_tokens=s.BRAIN_MAX_CONTENT_TOKENS, slot_floor=s.BRAIN_SLOT_FLOOR,
        relaxed_agreement=s.BRAIN_RELAXED_AGREEMENT, bm25_single_min=s.BRAIN_BM25_SINGLE_MIN,
        bm25_single_gap=s.BRAIN_BM25_SINGLE_GAP, knn_single_min=s.BRAIN_KNN_SINGLE_MIN,
    )
    assert thresholds_from_settings().require_slots


def test_the_question_names_the_action_in_both_languages_and_stays_short() -> None:
    assert question("delete_note", {"title": "Compras"}, "es") == "¿Borrar la nota Compras?"
    assert question("delete_note", {"title": "Compras"}, "en") == "Delete the note Compras?"
    long = question("run_command", {"command": "x" * 500, "confirmed": True}, "en")
    assert len(long) < 120 and "confirmed" not in long


@pytest.mark.asyncio
async def test_the_guard_asks_in_the_language_of_the_turn() -> None:
    guard = injection.install()
    try:
        runtime.set_spoken_lang("en")
        r = await guard.check("delete_note", {"title": "Compras", "confirmed": True})
        assert isinstance(r, injection.Refusal) and r.ask
        assert r.message == "Delete the note Compras?"
        assert guard.pending == Pending("delete_note", {"title": "Compras"}, "en",
                                        guard.pending.asked_at if guard.pending else 0.0)
    finally:
        injection.uninstall()


@pytest.mark.asyncio
async def test_the_brain_sets_the_spoken_language_from_the_transcript() -> None:
    proc = BrainProcessor(lambda: (lambda t: Decision(t, t, "none", "smalltalk")))
    await _say(proc, "what time is it")
    assert runtime.get_spoken_lang() == "en"
    await _say(proc, "qué hora es")
    assert runtime.get_spoken_lang() == "es"


@pytest.mark.asyncio
async def test_brain_question_is_the_guards_pending_and_a_yes_settles_it(
    fake_delete: list[dict[str, Any]],  # noqa: F811
) -> None:
    d = _local("delete the note compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "delete" in t else Decision(t, t, "none", "x")),
                          host=cascade._Host())
    _user("delete the note compras")
    assert await _say(proc, "delete the note compras") == ["Delete the note Compras?"]
    guard = injection.current()
    assert guard is not None and guard.pending is not None
    assert (guard.pending.tool, guard.pending.lang) == ("delete_note", "en")
    await asyncio.sleep(0.01)
    _user("yes")
    await _say(proc, "yes")
    assert fake_delete == [{"title": "Compras"}]
    assert guard.pending is None


@pytest.mark.asyncio
async def test_a_no_settles_the_one_pending_question(
    fake_delete: list[dict[str, Any]],  # noqa: F811
) -> None:
    d = _local("borra la nota compras", "delete_note", confirm=True, title="Compras")
    proc = BrainProcessor(lambda: (lambda t: d if "borra" in t else Decision(t, t, "none", "x")),
                          host=cascade._Host())
    _user("borra la nota compras")
    await _say(proc, "borra la nota compras")
    _user("no")
    await _say(proc, "no")
    guard = injection.current()
    assert fake_delete == [] and guard is not None and guard.pending is None
