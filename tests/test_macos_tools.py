"""Tests for the AppleScript-backed macOS tools.

osascript is mocked (patched on actions.macos.osascript) so these run
without touching real Calendar/Mail/etc. Each test exercises one tool's
output parsing or its destructive-confirmation gate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_loop():
    # asyncio.run() leaves the loop policy with no current loop (py3.12),
    # which breaks sibling tests using get_event_loop(). Restore one.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _run(coro):
    return asyncio.run(coro)


def _mock_osascript(return_value: str = ""):
    return patch("actions.macos.osascript", new=AsyncMock(return_value=return_value))


# ---------- read-path parsing ----------


def test_today_events_parses() -> None:
    from tools.calendar_tool import today_events

    raw = "2026-5-28-9-5|Standup|Office\n2026-5-28-14-0|Lunch|"
    with _mock_osascript(raw):
        res = _run(today_events())
    assert res.success
    events = res.data["events"]
    assert events[0]["label"] == "09:05 — Standup (Office)"
    assert events[1]["label"] == "14:00 — Lunch"


def test_list_unread_parses() -> None:
    from tools.mail_tool import list_unread

    raw = "anna@example.com|Dinner tonight\nbob@example.com|Re: budget"
    with _mock_osascript(raw):
        res = _run(list_unread())
    assert res.success
    msgs = res.data["messages"]
    assert msgs[0] == {"from": "anna@example.com", "subject": "Dinner tonight"}
    assert len(msgs) == 2


def test_create_note_uses_html_body_and_creates_folder() -> None:
    # Followup fix: HTML body keeps the title clean; folder is created on demand.
    from tools.notes_tool import create_note

    captured = AsyncMock(return_value="")
    with patch("actions.macos.osascript", new=captured):
        res = _run(create_note(title="Idea", body="cuerpo", folder="Feedback"))
    assert res.success
    script = captured.call_args[0][0]
    assert "<div>Idea</div><div>cuerpo</div>" in script  # clean title, separate body
    assert 'if not (exists folder "Feedback")' in script  # create-on-demand


def test_read_note_falls_back_to_contains() -> None:
    # Exact title misses (legacy flattened note) → contains fallback resolves it.
    from tools.notes_tool import read_note

    # 1st enumerate (exact) → empty; 2nd (contains) → one match; 3rd → body read.
    seq = ["", "id1‖2026-06-02T10:00:00‖Errores de Emma Bitácora‖p", "el cuerpo"]
    osa = AsyncMock(side_effect=seq)
    with patch("actions.macos.osascript", new=osa):
        res = _run(read_note(title="Errores de Emma"))
    assert res.success
    assert res.data["body"] == "el cuerpo"
    assert "contains" in osa.call_args_list[1][0][0]  # 2nd call used a contains match


def test_list_notes_parses() -> None:
    from tools.notes_tool import list_notes

    # New 19.2-B4 enumeration shape: id‖modification_date‖title‖preview
    raw = (
        "id1‖2026-06-02T08:00:00‖Shopping‖milk and eggs\n"
        "id2‖2026-06-02T09:30:00‖Blog ideas‖post about X"
    )
    with _mock_osascript(raw):
        res = _run(list_notes())
    assert res.success
    titles = [n["title"] for n in res.data["notes"]]
    assert titles == ["Shopping", "Blog ideas"]
    assert res.data["notes"][0]["modification_date"] == "2026-06-02T08:00:00"


def test_reminders_list_today_filters_to_today() -> None:
    from tools.reminders_tool import list_today

    today = dt.date.today()
    raw = (
        f"Call dentist|{today.year}-{today.month}-{today.day}\nPay rent|2000-1-1\nSomeday task|none"
    )
    with _mock_osascript(raw):
        res = _run(list_today())
    assert res.success
    assert res.data["reminders"] == ["Call dentist"]


def test_find_recent_parses_basenames() -> None:
    from tools.finder_tool import find_recent

    raw = "/Users/go/Documents/budget_2026.xlsx\n/Users/go/notes.txt"
    with _mock_osascript(raw):
        res = _run(find_recent("budget"))
    assert res.success
    names = [f["name"] for f in res.data["files"]]
    assert names == ["budget_2026.xlsx", "notes.txt"]
    assert res.data["files"][0]["path"] == "/Users/go/Documents/budget_2026.xlsx"


def test_current_url() -> None:
    from tools.safari_tool import current_url

    with _mock_osascript("https://example.com/page"):
        res = _run(current_url())
    assert res.success
    assert res.data["url"] == "https://example.com/page"


def test_list_folder_parses() -> None:
    from tools.finder_tool import list_folder

    raw = "2026-05-20 14:30|report.pdf\n2026-05-19 10:00|photo.jpg"
    with _mock_osascript(raw):
        res = _run(list_folder("~/Documents"))
    assert res.success
    entries = res.data["entries"]
    assert entries[0] == {"name": "report.pdf", "modified": "2026-05-20 14:30"}
    assert len(entries) == 2


def test_recent_threads_parses() -> None:
    from tools.messages_tool import recent_threads

    with _mock_osascript("iMessage;-;+15551234567\niMessage;-;anna@example.com"):
        res = _run(recent_threads())
    assert res.success
    assert len(res.data["threads"]) == 2


# ---------- destructive confirmation gate ----------


def test_send_to_requires_confirmation_first() -> None:
    from tools.mail_tool import send_to

    with _mock_osascript() as m:
        res = _run(send_to("anna@example.com", "Hi", "hello"))
        assert res.requires_confirmation is True
        m.assert_not_called()  # no AppleScript runs before confirmation


def test_send_to_sends_when_confirmed() -> None:
    from tools.mail_tool import send_to

    with _mock_osascript() as m:
        res = _run(send_to("anna@example.com", "Hi", "hello", confirmed=True))
        assert res.success is True
        assert res.requires_confirmation is False
        m.assert_awaited_once()


def test_send_imessage_requires_confirmation_first() -> None:
    from tools.messages_tool import send_imessage

    with _mock_osascript() as m:
        res = _run(send_imessage("+15551234567", "on my way"))
        assert res.requires_confirmation is True
        m.assert_not_called()


def test_create_event_requires_confirmation_first() -> None:
    from tools.calendar_tool import create_event

    with _mock_osascript() as m:
        res = _run(create_event("Dentist", "2026-06-01T15:00:00"))
        assert res.requires_confirmation is True
        m.assert_not_called()


def test_move_item_requires_confirmation_first() -> None:
    from tools.finder_tool import move_item

    with _mock_osascript() as m:
        res = _run(move_item("~/a.txt", "~/b.txt"))
        assert res.requires_confirmation is True
        m.assert_not_called()


def test_applescript_error_surfaces_friendly_message() -> None:
    from actions.macos import AppleScriptError
    from tools.calendar_tool import today_events

    with patch("actions.macos.osascript", new=AsyncMock(side_effect=AppleScriptError("boom"))):
        res = _run(today_events())
    assert res.success is False
    assert "calendario" in res.user_message.lower()
