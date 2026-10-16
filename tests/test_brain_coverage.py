"""Step 43 Part 1 — closed vocabularies that get filled, and frames for every reversible tool."""

from __future__ import annotations

from typing import Any

import pytest

from core import brain_shadow
from core.brain.brain import Brain
from core.brain.normalize import normalize
from core.brain.phrasebook import expansions
from core.brain.slots import TOOLS, Vocab, resolve, split_clitics
from core.brain_host import tool_docs, vocab
from tests.brain_slot_fixtures import APPS
from tools import music, notes_tool

DOCS = {d.name: d for d in tool_docs(available_only=False)}

# Phrasings that SHOULD stay unexplained: a time the tool can't take yet (Part 3),
# a negation, or a speech-recognition mangling. Everything else must resolve.
EXPECTED_UNEXPLAINED = {
    "next_track": {"no me gusta esta, la siguiente", "I don't like this, skip", "siguiente canson"},
    "unmute": {"let it play sound", "anmiut"},
    "list_timers": {"que taimers tengo"},
    "describe_capabilities": {"que puedes aser"},
    "what_did_you_do": {
        "qué hiciste el lunes", "qué hiciste la semana pasada", "qué hiciste por mí el lunes",
        "qué hiciste por mí la semana pasada", "qué hiciste esta semana", "lo que hiciste el lunes",
        "lo que hiciste la semana pasada", "what did you do el lunes",
        "what did you do la semana pasada", "what did you do this week", "what did you do on monday",
        "what did you do for me el lunes", "what did you do for me la semana pasada",
        "que isiste aller",
    },
    "pause": {"pausa lo que suena", "poz la musica"},
    "resume": {"play"},
    "now_playing": set(), "toggle_ide_terminal": set(), "open_in_ide": set(), "find_file": set(),
}


def test_every_reversible_tool_has_a_frame() -> None:
    # start_timer: Step 43 Part 3. describe_capabilities: Step 43.1, once it told the truth.
    missing = sorted(d.name for d in DOCS.values() if d.reversible and d.name not in TOOLS)
    assert missing == []


@pytest.mark.parametrize("tool", sorted(EXPECTED_UNEXPLAINED))
def test_frames_explain_the_phrasebook(tool: str) -> None:
    v = vocab(apps=APPS)
    bad = set()
    for _lang, text in expansions(tool):
        r = resolve(tool, normalize(split_clitics(text)[0]), v, required=DOCS[tool].required)
        if r.missing or r.leftover:
            bad.add(text)
    assert bad == EXPECTED_UNEXPLAINED[tool]


def test_a_time_the_tool_cant_take_is_never_absorbed() -> None:
    r = resolve("what_did_you_do", normalize("qué hiciste ayer"), vocab())
    assert r.leftover == ("ayer",)


@pytest.mark.asyncio
async def test_listings_never_launch_notes_or_music(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_running(app: str) -> bool:
        return False

    async def boom(*a: Any, **k: Any) -> str:
        raise AssertionError("osascript must not run when the app is closed")

    monkeypatch.setattr(notes_tool.macos, "app_is_running", not_running)
    monkeypatch.setattr(notes_tool.macos, "osascript", boom)
    monkeypatch.setattr(music, "spotify_token_status", lambda: "missing")
    assert await notes_tool.note_titles() == ()
    assert await music.playlist_names() == ()


@pytest.mark.asyncio
async def test_note_titles_dedupe_and_skip_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.disambiguation import FIELD_SEP

    async def running(app: str) -> bool:
        return True

    async def rows(*a: Any, **k: Any) -> str:
        return "\n".join(f"id{i}{FIELD_SEP}2026-10-01{FIELD_SEP}{t}" for i, t in
                         enumerate(["Compras", "Ideas", "Compras", ""]))

    monkeypatch.setattr(notes_tool.macos, "app_is_running", running)
    monkeypatch.setattr(notes_tool.macos, "osascript", rows)
    assert await notes_tool.note_titles() == ("Compras", "Ideas")


def test_refresh_fills_the_live_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake() -> tuple[tuple[str, ...], tuple[str, ...]]:
        return ("Compras",), ("Focus",)

    brain = Brain(list(DOCS.values()), Vocab(), _th())
    monkeypatch.setattr("core.brain_host.listings", fake)
    monkeypatch.setattr(brain_shadow, "_brain", brain)
    brain_shadow.refresh_listings().join(5)
    assert (brain.vocab.notes, brain.vocab.playlists) == (("Compras",), ("Focus",))


def _th() -> Any:
    from core.brain_host import thresholds_from_settings

    return thresholds_from_settings()


@pytest.mark.asyncio
async def test_changing_a_note_refreshes_the_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import cascade
    from tools.base import ToolResult

    calls: list[str] = []

    async def dispatch(tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, None, "Borrada.", False)

    monkeypatch.setattr("tools.registry.dispatch", dispatch)
    monkeypatch.setattr(cascade.session_memory, "record_completed_action", lambda *a, **k: None)
    monkeypatch.setattr(brain_shadow, "refresh_listings", lambda: calls.append("refresh"))
    host = cascade._Host()
    await host.execute("open_application", {"name": "Safari"})
    await host.execute("delete_note", {"title": "Compras", "confirmed": True})
    assert calls == ["refresh"]
