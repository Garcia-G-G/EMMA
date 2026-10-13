"""VOICE-1 Part 3 — local execution safety: master plan rules 3, 6 and 9, enforced in code."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.tests.utils import run_test

from config.settings import Settings, settings
from core.brain.decision import Decision, SlotScalar, SlotValue
from core.brain.embed import Embedder
from core.brain.phrases import is_correction
from core.brain.processor import BrainProcessor, Outcome
from core.brain.respond import _CORRECTED, SPEAKS_RESULT
from core.brain.sinks import is_sink
from tools import availability, registry
from tools.base import tool

ROOT = Path(__file__).resolve().parent.parent

# Reviewed, not accidental: changing this set is a decision (rule 3). Each tool
# carries its one-line reason in its decorator (`reversible_because`).
REVERSIBLE = {
    "close_current_tab", "current_datetime_speak", "current_time", "describe_capabilities",
    "find_file", "list_browser_tabs", "list_notes", "list_timers", "list_today", "mute",
    "my_repos", "next_event", "next_track", "now_playing", "open_application", "open_in_ide",
    "open_my_page", "pause", "play_playlist", "play_track", "read_note", "recall_last_action",
    "repeat_last", "resume", "set_brightness", "set_volume", "start_timer", "today_events",
    "toggle_ide_terminal", "unmute", "what_did_you_do",
}


def _entries() -> dict[str, Any]:
    registry._discover()
    return {e.name: e for e in registry.get_registry().values()}


# ---- rule 3: only reversible tools run without a spoken yes --------------------------------


def test_the_reversible_set_is_exactly_the_reviewed_one() -> None:
    entries = _entries()
    got = {n for n, e in entries.items() if e.reversible}
    assert got == REVERSIBLE
    for n in got:
        e = entries[n]
        assert e.reversible_because, n
        assert not e.destructive, n
        assert not is_sink(n), f"{n} is a sink: it asks anyway, marking it reversible misleads"


def test_reversible_needs_a_reason_and_cannot_be_destructive() -> None:
    with pytest.raises(ValueError, match="reason"):
        tool(reversible=True)
    with pytest.raises(ValueError, match="destructive"):
        tool(destructive=True, reversible=True, reversible_because="x")


EMBEDDER = Embedder()
needs_model = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")


@pytest.fixture(scope="module")
def live_brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    from core.brain.brain import Brain
    from core.brain_host import build_index, thresholds_from_settings, tool_docs, vocab
    from tests.brain_slot_fixtures import APPS, NOTES

    index = build_index(EMBEDDER, tmp_path_factory.mktemp("b") / "brain.db")
    yield Brain(tool_docs(), vocab(apps=APPS, notes=NOTES), thresholds_from_settings(), index,
                EMBEDDER)
    index.close()


@needs_model
def test_every_local_decision_asks_unless_its_tool_is_reversible(live_brain: Any) -> None:
    from core.brain_host import labelled_examples

    entries = _entries()
    local = [d for d in (live_brain.decide(e.text) for e in labelled_examples())
             if d.verdict == "local"]
    assert local
    for d in local:
        e = entries[d.chosen]
        should_ask = e.destructive or is_sink(d.chosen) or not e.reversible
        assert d.needs_confirmation == should_ask, (d.utterance, d.chosen)
    assert any(not d.needs_confirmation for d in local)  # the reversible path is exercised
    assert any(d.needs_confirmation for d in local)


# ---- rule 9: argument completeness cannot be switched off -----------------------------------


def test_no_setting_can_disable_argument_completeness(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.brain_host import thresholds_from_settings

    for var in ("BRAIN_REQUIRE_SLOTS", "REQUIRE_SLOTS", "BRAIN_SLOTS"):
        monkeypatch.setenv(var, "false")
    s = Settings()
    assert not any("REQUIRE" in f and "SLOT" in f for f in type(s).model_fields)
    monkeypatch.setattr("config.settings.settings", s)
    assert thresholds_from_settings().require_slots is True


def test_the_live_loader_refuses_a_brain_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from core import brain_host, brain_shadow

    real = brain_host.thresholds_from_settings
    monkeypatch.setattr(brain_host, "thresholds_from_settings",
                        lambda: dataclasses.replace(real(), require_slots=False))
    monkeypatch.setattr(brain_shadow, "_brain", None)
    brain_shadow._load_brain()
    assert brain_shadow.loaded() is None  # load failed closed: no brain, every turn defers


# ---- availability: never a tool this machine can't run, never an LLM ------------------------


LLM_IMPORTS = ("openai", "anthropic", "groq", "ollama", "memory.embeddings", "memory.reflection",
               "memory.long_term", "core.coding_agent")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            found.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module)
            found.update(f"{n.module}.{a.name}" for a in n.names)  # from memory import long_term
    return found


def _hits(mods: set[str], prefixes: Sequence[str]) -> bool:
    return any(m == p or m.startswith(p + ".") for m in mods for p in prefixes)


def test_llm_backed_tool_modules_are_derived_not_remembered() -> None:
    """Re-derive the set from imports, transitively through other tool modules."""
    # diagnostics imports the OpenAI client only for a models.retrieve ping:
    # metadata, no inference — so it (and dev/git_tool through it) stay available.
    not_inference = {"diagnostics"}
    tool_files = {p.stem: p for p in (ROOT / "tools").glob("*.py") if p.stem not in not_inference}
    direct = {name for name, p in tool_files.items() if _hits(_imports(p), LLM_IMPORTS)}
    backed = set(direct)
    changed = True
    while changed:
        changed = False
        for name, p in tool_files.items():
            if name not in backed and _hits(_imports(p), [f"tools.{b}" for b in backed]):
                backed.add(name)
                changed = True
    assert backed == set(availability.LLM_BACKED_MODULES)


def test_the_cascade_offers_no_llm_backed_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    names = {e.name for e in registry.available_tools()}
    for gone in ("remember_fact", "recall_facts", "search_web", "describe_screen",
                 "deep_research", "delegate_to_codex"):
        assert gone not in names, gone
    assert "open_application" in names


def test_the_brain_only_sees_tools_this_machine_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.brain_host import tool_docs

    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    available = {e.name for e in registry.available_tools()}
    docs = {d.name for d in tool_docs(available_only=True)}
    assert docs == available
    assert "remember_fact" not in docs


def test_the_live_loader_builds_from_available_tools_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import brain_host, brain_shadow

    asked: list[bool] = []
    real = brain_host.tool_docs

    def spy(available_only: bool = False) -> Any:
        asked.append(available_only)
        return real(available_only)

    monkeypatch.setattr(brain_host, "tool_docs", spy)
    monkeypatch.setattr(brain_shadow, "_brain", None)
    monkeypatch.setattr("core.brain.embed.Embedder.available", lambda self: False)
    brain_shadow._load_brain()
    assert asked == [True]
    monkeypatch.setattr(brain_shadow, "_brain", None)


def test_no_core_module_imports_an_llm_client_outside_the_allowlist() -> None:
    """VOICE-1: no LLM anywhere. The exceptions are named, and each is unreachable here."""
    allowed = {
        # The Realtime path, selected only by VOICE_BACKEND=realtime.
        "core/conversation.py", "core/realtime_playback.py", "core/realtime_session_audit.py",
        # The coding agent behind delegate_to_codex — codex_tool is LLM-backed,
        # so unavailable on the cascade.
        "core/coding_agent.py",
        # Proactive briefings: _synthesize_es returns "" on the cascade (tested below).
        "core/proactive/proactivities.py",
    }
    offenders = {
        str(p.relative_to(ROOT)) for p in (ROOT / "core").rglob("*.py")
        if _hits(_imports(p), ("openai", "anthropic", "groq", "ollama", "pipecat.services.openai",
                               "memory.embeddings", "memory.reflection"))
    }
    assert offenders == allowed
    cascade_src = (ROOT / "core" / "cascade.py").read_text(encoding="utf-8")
    assert "openai" not in cascade_src.lower().replace("no openai", "")


@pytest.mark.asyncio
async def test_proactive_synthesis_never_calls_a_model_on_the_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.proactive import proactivities

    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-" + "x" * 40)

    class Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("constructed an OpenAI client on the cascade")

    monkeypatch.setattr("openai.AsyncOpenAI", Boom)
    assert await proactivities._synthesize_es("hola") == ""


# ---- every executed turn is logged; corrections are the wrong-rate ---------------------------


def _local(text: str, tool_name: str, **slots: SlotScalar) -> Decision:
    return Decision(utterance=text, normalized=text, verdict="local", reason="ok", chosen=tool_name,
                    slots={k: SlotValue(v, "t", 1.0, str(v)) for k, v in slots.items()})


class Host:
    def __init__(self, message: str = "ok") -> None:
        self.message = message
        self.calls: list[str] = []

    async def execute(self, t: str, args: dict[str, SlotScalar]) -> Outcome:
        self.calls.append(t)
        return Outcome(True, self.message)

    def asked(self, t: str) -> None: ...

    def facts(self) -> Sequence[str]:
        return ()


async def _say(proc: BrainProcessor, text: str) -> list[str]:
    down, _ = await run_test(proc, frames_to_send=[TranscriptionFrame(text, "u", "t")])
    return [f.text for f in down if isinstance(f, TTSSpeakFrame)]


@pytest.mark.parametrize("text", ["no, eso no", "cancela", "otra vez", "no", "eso no era",
                                  "that's wrong", "undo that", "try again"])
def test_correction_phrases(text: str) -> None:
    assert is_correction(text)


@pytest.mark.parametrize("text", ["abre Safari otra vez por favor ahora mismo ya", "sí",
                                  "abre Spotify", "", "pon la de nuevo de Bad Bunny en Spotify"])
def test_not_corrections(text: str) -> None:
    assert not is_correction(text)


@pytest.mark.asyncio
async def test_a_correction_after_an_executed_turn_is_logged_against_it() -> None:
    records: list[dict[str, object]] = []

    def decide(t: str) -> Decision:
        return _local(t, "open_application", name="Safari") if "abre" in t else \
            Decision(t, t, "none", "smalltalk")

    proc = BrainProcessor(lambda: decide, host=Host(),
                          record=lambda d, s, e: records.append({"verdict": d.verdict, **e}))
    await _say(proc, "abre Safari")
    said = await _say(proc, "no, eso no")
    assert said[0] in _CORRECTED["es"]
    assert records[0]["executed"] is True and "correction_of" not in records[0]
    assert records[1]["correction_of"] == {"tool": "open_application", "utterance": "abre Safari"}
    # Only the very next utterance counts.
    await _say(proc, "no")
    assert "correction_of" not in records[2]

    from emma.brain import report

    rows = [{"backend": "cascade", **r} for r in records]
    text = report(rows)
    assert "executed 1   corrections 1" in text
    assert "correction: 'abre Safari' -> open_application" in text


@pytest.mark.asyncio
async def test_a_no_with_nothing_executed_is_not_a_correction() -> None:
    records: list[dict[str, object]] = []
    proc = BrainProcessor(lambda: (lambda t: Decision(t, t, "none", "smalltalk")), host=Host(),
                          record=lambda d, s, e: records.append(e))
    await _say(proc, "no")
    assert "correction_of" not in records[0]


@pytest.mark.asyncio
async def test_answer_tools_speak_their_result_not_a_preamble() -> None:
    assert "current_time" in SPEAKS_RESULT
    proc = BrainProcessor(lambda: (lambda t: _local(t, "current_time")),
                          host=Host("Son las 16:47."))
    assert await _say(proc, "qué hora es") == ["Son las 16:47."]


