"""Phase 19.2 bug-fix sweep — dispatch/unit coverage for B1…B7.

Live Apple-app behaviour (delete-one, launch-then-play, tab close, terminal
toggle) was verified by hand during development; these tests pin the LOGIC
(disambiguation, ordering, script shape, prompt content) without needing the
apps, per the prompt's "dispatch-level tests are acceptable" allowance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.base import ToolResult

# --- B2: no silent multi-delete (the non-skippable, data-loss bug) -----------

# Two matches sharing a title, in the enumerator's id‖when‖title‖preview shape.
_TWO_NOTES = "id1‖2026-06-02T10:00:00‖Dup‖a\nid2‖2026-06-02T10:01:00‖Dup‖b"
_TWO_EVENTS = "uid1‖2026-06-02T10:00:00‖Junta‖\nuid2‖2026-06-02T18:00:00‖Junta‖"
_TWO_REMS = "r1‖‖Comprar‖\nr2‖‖Comprar‖"


class TestB2NotesNoMultiDelete:
    @pytest.mark.asyncio
    async def test_ambiguous_delete_asks_and_deletes_nothing(self, monkeypatch):
        from tools import notes_tool

        enum = AsyncMock(return_value=_TWO_NOTES)
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(notes_tool.macos, "osascript", enum)
        monkeypatch.setattr(notes_tool.macos, "osascript_or_friendly", act)

        r = await notes_tool.delete_note("Dup")
        assert r.requires_confirmation is True
        assert len(r.data["matches"]) == 2
        act.assert_not_called()  # crucially: NO delete issued

    @pytest.mark.asyncio
    async def test_index_deletes_exactly_one_by_id(self, monkeypatch):
        from tools import notes_tool

        monkeypatch.setattr(notes_tool.macos, "osascript", AsyncMock(return_value=_TWO_NOTES))
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(notes_tool.macos, "osascript_or_friendly", act)

        r = await notes_tool.delete_note("Dup", index=2, confirmed=True)
        assert r.success is True
        act.assert_called_once()
        script = act.call_args[0][0]
        assert "id2" in script and "id1" not in script  # targets the chosen id only

    @pytest.mark.asyncio
    async def test_no_repeat_delete_anti_pattern_in_source(self):
        # The B2 anti-pattern (repeat … delete over matches) must be gone.
        import pathlib

        for fn in ("notes_tool.py", "calendar_tool.py", "reminders_tool.py"):
            src = pathlib.Path("tools", fn).read_text()
            # crude but effective: no "delete" inside a "repeat with … in (… whose …)"
            assert "delete ev\n" not in src, f"{fn} still loops delete"
            assert "delete nt\n" not in src, f"{fn} still loops delete"


class TestB2CalendarNoMultiDelete:
    @pytest.mark.asyncio
    async def test_ambiguous_delete_event_asks_and_deletes_nothing(self, monkeypatch):
        from tools import calendar_tool

        monkeypatch.setattr(calendar_tool.macos, "osascript", AsyncMock(return_value=_TWO_EVENTS))
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(calendar_tool.macos, "osascript_or_friendly", act)

        r = await calendar_tool.delete_event("Junta")
        assert r.requires_confirmation is True
        assert len(r.data["matches"]) == 2
        act.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_deletes_one_event_by_uid(self, monkeypatch):
        from tools import calendar_tool

        monkeypatch.setattr(calendar_tool.macos, "osascript", AsyncMock(return_value=_TWO_EVENTS))
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(calendar_tool.macos, "osascript_or_friendly", act)

        r = await calendar_tool.delete_event("Junta", index=1, confirmed=True)
        assert r.success is True
        assert "uid1" in act.call_args[0][0]


class TestB2RemindersNoMultiComplete:
    @pytest.mark.asyncio
    async def test_ambiguous_complete_asks_and_acts_on_nothing(self, monkeypatch):
        from tools import reminders_tool

        monkeypatch.setattr(reminders_tool.macos, "osascript", AsyncMock(return_value=_TWO_REMS))
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(reminders_tool.macos, "osascript_or_friendly", act)

        r = await reminders_tool.complete_reminder("Comprar")
        assert r.requires_confirmation is True
        assert len(r.data["matches"]) == 2
        act.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_completes_one_reminder_by_id(self, monkeypatch):
        from tools import reminders_tool

        monkeypatch.setattr(reminders_tool.macos, "osascript", AsyncMock(return_value=_TWO_REMS))
        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(reminders_tool.macos, "osascript_or_friendly", act)

        r = await reminders_tool.complete_reminder("Comprar", index=2, confirmed=True)
        assert r.success is True
        assert "r2" in act.call_args[0][0]


# --- B1: fast, clean loop-back after a session timeout -----------------------


class TestB1SessionReset:
    @pytest.mark.asyncio
    async def test_timeout_resets_and_loops_back_to_wake(self, monkeypatch):
        from core import orchestrator

        listen = AsyncMock()
        monkeypatch.setattr(orchestrator, "listen_for_wake_word", listen)
        monkeypatch.setattr(
            orchestrator.conversation, "run_session", AsyncMock(side_effect=TimeoutError())
        )
        monkeypatch.setattr(orchestrator.asyncio, "sleep", AsyncMock())  # skip the real waits
        monkeypatch.setattr(orchestrator, "_simulate_crash", False)

        # Two iterations: the timeout in #1 must not stop #2 from listening again.
        await orchestrator._one_session()
        await orchestrator._one_session()
        assert listen.await_count == 2


# --- B3: launch the music app before issuing AppleScript transport -----------


class TestB3SpotifyLaunch:
    @pytest.mark.asyncio
    async def test_play_track_launches_spotify_before_applescript(self, monkeypatch):
        from tools import music

        order: list[str] = []

        async def running(app):
            order.append(f"running?{app}")
            return False

        async def launch(app, warmup_s=1.5):
            order.append(f"launch:{app}")

        def runas(script):
            order.append("applescript")
            return ""

        monkeypatch.setattr(music, "_music_app", lambda: "Spotify")
        monkeypatch.setattr(music.macos, "app_is_running", running)
        monkeypatch.setattr(music.macos, "launch_app", launch)
        monkeypatch.setattr(music.macos, "run_applescript", runas)
        monkeypatch.setattr(music, "_spotify_search_uri", lambda q: "spotify:track:xyz")

        r = await music.play_track("bad bunny")
        assert r.success is True
        assert "launch:Spotify" in order
        assert order.index("launch:Spotify") < order.index("applescript")


# --- B4: list_notes carries ISO modification dates ---------------------------


class TestB4NotesMetadata:
    @pytest.mark.asyncio
    async def test_list_notes_populates_modification_date(self, monkeypatch):
        from tools import notes_tool

        fixture = "id9‖2026-06-02T08:15:00‖Pendientes‖primera linea de cuerpo"
        monkeypatch.setattr(notes_tool.macos, "osascript", AsyncMock(return_value=fixture))

        r = await notes_tool.list_notes(query="Pend")
        note = r.data["notes"][0]
        assert note["modification_date"] == "2026-06-02T08:15:00"
        assert note["preview"] == "primera linea de cuerpo"

    @pytest.mark.asyncio
    async def test_read_note_returns_body(self, monkeypatch):
        from tools import notes_tool

        monkeypatch.setattr(
            notes_tool.macos,
            "osascript",
            AsyncMock(side_effect=["id9‖2026-06-02T08:15:00‖N‖p", "cuerpo completo"]),
        )
        r = await notes_tool.read_note("N")
        assert r.success is True
        assert r.data["body"] == "cuerpo completo"


# --- B5: direct-AppleScript tab close ----------------------------------------


class TestB5CloseTab:
    @pytest.mark.asyncio
    async def test_safari_uses_direct_applescript(self, monkeypatch):
        from tools import user_browser

        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(user_browser.macos, "osascript_or_friendly", act)
        r = await user_browser.close_current_tab(browser="Safari")
        assert r.success is True
        assert (
            act.call_args[0][0] == 'tell application "Safari" to close current tab of front window'
        )

    @pytest.mark.asyncio
    async def test_chrome_uses_active_tab_applescript(self, monkeypatch):
        from tools import user_browser

        act = AsyncMock(return_value=(True, ""))
        monkeypatch.setattr(user_browser.macos, "osascript_or_friendly", act)
        await user_browser.close_current_tab(browser="Google Chrome")
        assert "close active tab of front window" in act.call_args[0][0]


# --- B6: toggle IDE integrated terminal --------------------------------------


class TestB6ToggleTerminal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ide,keys",
        [("Cursor", "Ctrl+`"), ("VS Code", "Ctrl+`"), ("Zed", "Cmd+J")],
    )
    async def test_each_ide_sends_expected_shortcut(self, monkeypatch, ide, keys):
        from tools import ide_actions

        ks = AsyncMock(return_value=ToolResult(True, {}, "ok", False))
        monkeypatch.setattr(ide_actions, "app_keystroke", ks)
        r = await ide_actions.toggle_ide_terminal(ide=ide)
        assert r.success is True
        ks.assert_awaited_once_with(ide, keys, confirmed=True)

    @pytest.mark.asyncio
    async def test_unknown_ide_is_declined(self, monkeypatch):
        from tools import ide_actions

        monkeypatch.setattr(ide_actions, "app_keystroke", AsyncMock())
        r = await ide_actions.toggle_ide_terminal(ide="Nano")
        assert r.success is False


# --- B7: vague-search guard present in the system prompt ----------------------


class TestB7VagueSearchGuard:
    @pytest.mark.asyncio
    async def test_instructions_contain_vague_guard(self, monkeypatch):
        import core.conversation as conv

        # priming_block reads SQLite; stub it so the test is hermetic.
        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        text = await conv._build_instructions()
        assert "# Vague search guard (mandatory)" in text
        assert "search_github" in text and "search_web" in text
        assert "fewer than 2 distinct content words" in text
