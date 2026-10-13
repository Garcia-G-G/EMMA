"""BRAIN-1 Part 7 — shadow mode: decide beside the live path, log, execute nothing."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core import brain_shadow
from core.brain import log as brain_log
from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.slots import Vocab
from core.brain_host import (
    build_index,
    labelled_examples,
    thresholds_from_settings,
    tool_docs,
    vocab,
)
from core.brain_shadow import Shadow
from emma.brain import report
from tests.brain_slot_fixtures import APPS, CONNECTIONS, NOTES, PAGES, PLAYLISTS

EMBEDDER = Embedder()
needs_model = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    docs = tool_docs()
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("b") / "brain.db")
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    v = Vocab(apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
              playlists=base.playlists, connections=base.connections, contacts=base.contacts)
    yield Brain(docs, v, thresholds_from_settings(), index, EMBEDDER)
    index.close()


async def _turn(s: Shadow, text: str, live_tool: str | None) -> None:
    s.on_speech_started()
    task = s.on_transcript(text)
    if live_tool:
        s.on_tool(live_tool, {"confirmed": True, "x": "y"})
    assert task is not None
    await task


@needs_model
def test_a_session_of_the_140_logs_one_comparison_per_turn(
    brain: Brain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live baseline here is the label's first tool (Realtime can't run: no key)."""
    from tools import registry

    def no_dispatch(*a: Any, **k: Any) -> Any:
        raise AssertionError("shadow mode executed a tool")

    monkeypatch.setattr(registry, "dispatch", no_dispatch)
    log_path = tmp_path / "brain_decisions.jsonl"
    s = Shadow(brain, log_path)
    items = labelled_examples()

    async def session() -> None:
        for ex in items:
            live = next((t for t in ex.accept_tools if t != "__none__"), None)
            await _turn(s, ex.text, live)
        s.close()

    asyncio.run(session())
    rows = brain_log.read(log_path)
    assert len(rows) == len(items)
    for r, ex in zip(rows, items, strict=True):
        assert r["utterance"] == ex.text
        assert {"brain_verdict", "brain_tool", "realtime_tool", "agree", "features", "session"} <= set(r)
        assert "confirmed" not in (r["realtime_args"] or {})
    local = [r for r in rows if r["brain_verdict"] == "local"]
    assert local and all(r["agree"] for r in local), [r for r in local if not r["agree"]]
    text = report(rows)
    assert f"turns logged: {len(items)}" in text and "defer reasons:" in text


def test_a_decision_that_lands_late_keeps_its_own_turn(tmp_path: Path) -> None:
    class Slow:
        def decide(self, text: str) -> Any:
            import time

            from core.brain.decision import Decision

            time.sleep(0.2)
            return Decision(text, text, "defer", "test")

    log_path = tmp_path / "log.jsonl"
    s = Shadow(Slow(), log_path)  # type: ignore[arg-type]

    async def run() -> None:
        s.on_speech_started()
        t1 = s.on_transcript("primera frase")
        s.on_speech_started()  # the user talks again before the decision lands
        s.on_tool("open_application", {"name": "Spotify"})
        assert t1 is not None
        await t1
        s.close()

    asyncio.run(run())
    rows = brain_log.read(log_path)
    assert [r["utterance"] for r in rows] == ["primera frase"]
    assert rows[0]["turn"] == 1 and rows[0]["realtime_tool"] is None


def test_no_brain_yet_is_still_a_record(tmp_path: Path) -> None:
    s = Shadow(None, tmp_path / "log.jsonl")
    asyncio.run(_turn(s, "abre spotify", "open_application"))
    s.close()
    [r] = brain_log.read(tmp_path / "log.jsonl")
    assert (r["verdict"], r["reason"], r["agree"]) == ("defer", "brain_not_ready", None)


def test_a_failing_brain_is_a_logged_defer_not_an_exception(tmp_path: Path) -> None:
    class Broken:
        def decide(self, text: str) -> Any:
            raise RuntimeError("boom")

    s = Shadow(Broken(), tmp_path / "log.jsonl")  # type: ignore[arg-type]
    asyncio.run(_turn(s, "abre spotify", None))
    s.close()
    [r] = brain_log.read(tmp_path / "log.jsonl")
    assert r["reason"] == "brain_error:RuntimeError"


def test_a_vad_onset_with_no_words_logs_nothing(tmp_path: Path) -> None:
    s = Shadow(None, tmp_path / "log.jsonl")
    s.on_speech_started()  # a cough
    s.on_speech_started()
    s.close()
    assert brain_log.read(tmp_path / "log.jsonl") == []


def test_report_on_an_empty_log() -> None:
    assert "no shadow turns yet" in report([])


def test_the_live_speech_tap_feeds_the_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL _UserSpeechTap drives the hooks: VAD onset, then the transcript."""
    from pipecat.frames.frames import TranscriptionFrame, UserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    import core.conversation as conv

    calls: list[object] = []

    class Spy:
        brain = object()

        def on_speech_started(self) -> None:
            calls.append("start")

        def on_transcript(self, text: str) -> None:
            calls.append(("transcript", text))

    monkeypatch.setattr(brain_shadow, "_shadow", Spy())

    async def run() -> None:
        tap = conv._UserSpeechTap()
        await tap.process_frame(UserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await tap.process_frame(TranscriptionFrame("abre spotify", "u", "t"), FrameDirection.UPSTREAM)

    asyncio.run(run())
    assert calls == ["start", ("transcript", "abre spotify")]


@needs_model
def test_decision_latency_p95_under_100ms(brain: Brain) -> None:
    """Pinned: < 100 ms p95 on this Mac. Best of 3 rounds — a busy machine can't fail it."""
    texts = [e.text for e in labelled_examples()]
    brain.decide("warm up")
    best = float("inf")
    for _ in range(3):
        ms = sorted(brain.decide(t).timings["total_ms"] for t in texts)
        best = min(best, ms[int(len(ms) * 0.95)])
        if best < 100:
            break
    assert best < 100, f"decision p95 {best:.1f} ms"


# ---- a fresh install whose embedder download failed (BRAIN-1.5 Part 4) ------------------


def _no_model_brain(tmp_path: Path) -> Brain:
    missing = Embedder(tmp_path / "no-model")
    return Brain(tool_docs(), vocab(apps=APPS), thresholds_from_settings(), None, missing)


def test_without_the_embedder_tool_requests_defer_and_say_why(tmp_path: Path) -> None:
    brain = _no_model_brain(tmp_path)
    d = brain.decide("Emma, abre Safari")
    assert (d.verdict, d.reason) == ("defer", "embedder_missing")
    assert any(f.name == "knn_status" and f.value == "embedder_missing" for f in d.features)


def test_without_the_embedder_the_closed_lists_still_answer(tmp_path: Path) -> None:
    brain = _no_model_brain(tmp_path)
    assert brain.decide("Hey Emma, borra mi carpeta home completa.").verdict == "refuse"
    assert brain.decide("Hey Emma, ¿cómo estás?").verdict == "none"
    assert brain.decide("Hey Emma, búscame algo.").verdict == "clarify"


def test_a_missing_embedder_is_logged_once_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structlog.testing import capture_logs

    import core.brain.embed as embed_mod

    monkeypatch.setattr(embed_mod, "MODEL_DIR", tmp_path / "no-model")
    monkeypatch.setattr(embed_mod.Embedder.__init__, "__defaults__",
                        (tmp_path / "no-model", 2))
    monkeypatch.setattr(brain_shadow, "_brain", None)
    with capture_logs() as logs:
        brain_shadow._load_brain()
        loaded = brain_shadow._brain
        assert loaded is not None
        for _ in range(5):
            assert loaded.decide("Emma, abre Safari").reason == "embedder_missing"
    missing = [e for e in logs if e.get("event") == "brain_embedder_missing"]
    assert len(missing) == 1, logs
    monkeypatch.setattr(brain_shadow, "_brain", None)
