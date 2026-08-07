"""Tests for the install-time permission bootstrap.

osascript / say / sleep / the permission probes are mocked so the walkthrough
runs fast and silent and never pops real dialogs. The bootstrap waits on the
user's grant (polling each probe) rather than a fixed timer (LAUNCH-2.1 Item 2),
so tests shrink the ceiling and mock the probes to a granted/denied state.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import permissions
from core.permissions import _AUTOMATION_APPS, _MANUAL_PANES


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _fast_and_no_tty(monkeypatch):
    # Never touch a real /dev/tty in tests; keep the no-probe dwell tiny.
    monkeypatch.setattr(permissions, "_open_tty", lambda: None)
    monkeypatch.setattr(permissions, "_NO_PROBE_DWELL_S", 0.02)
    monkeypatch.setattr(permissions, "_GRANT_CEILING_S", 0.05)


def _fake_subprocess():
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0
    proc.kill = MagicMock()
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return proc

    return fake_exec, calls


def _run_bootstrap(*, mic=True, panes_granted=True):
    fake_exec, calls = _fake_subprocess()
    probe = MagicMock(return_value=panes_granted)
    with (
        patch("core.permissions.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("core.permissions.asyncio.sleep", new=AsyncMock(return_value=None)),
        patch("core.permissions._say") as say,
        patch("core.permissions._open_settings") as open_settings,
        patch("core.permissions.check_microphone", return_value=mic),
        patch("core.permissions.check_accessibility_ax", probe),
        patch("core.permissions.check_screen_recording", probe),
        patch("core.permissions.check_calendar", probe),
    ):
        results = asyncio.run(permissions.bootstrap())
    return results, calls, say, open_settings


def test_bootstrap_pings_every_app_opens_every_pane_and_advances_on_grant():
    results, calls, say, open_settings = _run_bootstrap(mic=True, panes_granted=True)

    # two osascript invocations per automation app (launch + data-model query).
    assert len(calls) == 2 * len(_AUTOMATION_APPS)
    for app in _AUTOMATION_APPS:
        assert results[f"Automation:{app}"] == "dialog_shown"

    # one Settings pane opened per manual permission, in order.
    opened = [c.args[0] for c in open_settings.call_args_list]
    assert opened == [pane for pane, _, _ in _MANUAL_PANES]

    # granted the instant the probe passed → 'granted' for the probeable panes.
    assert results["Microphone"] == "granted"
    assert results["Accessibility"] == "granted"
    assert results["ScreenCapture"] == "granted"
    assert results["Calendars"] == "granted"
    # Full Disk Access has no probe → fixed dwell then a benign no-probe status.
    assert results["AllFiles"] == "opened_no_probe"

    # spoke a "why" line for mic + each app + each pane (before its dialog).
    assert say.call_count >= 1 + len(_AUTOMATION_APPS) + len(_MANUAL_PANES)


def test_bootstrap_reports_missing_when_not_granted():
    # mic denied + panes never granted → they time out (skipped), summary flags them.
    results, _, say, _ = _run_bootstrap(mic=False, panes_granted=False)
    assert results["Microphone"] == "timeout"
    assert results["Accessibility"] == "timeout"
    # the closing spoken line acknowledges pending permissions (not "all granted").
    spoken = " ".join(str(c.args[0]) for c in say.call_args_list)
    assert "pendiente" in spoken.lower()


@pytest.mark.asyncio
async def test_await_grant_returns_granted_instantly(monkeypatch):
    monkeypatch.setattr(permissions, "_GRANT_CEILING_S", 5.0)
    slept = AsyncMock()
    monkeypatch.setattr(permissions.asyncio, "sleep", slept)
    status = await permissions._await_grant(lambda: True)
    assert status == "granted"
    slept.assert_not_awaited()  # advanced on the grant, never slept


@pytest.mark.asyncio
async def test_await_grant_times_out_when_never_granted(monkeypatch):
    monkeypatch.setattr(permissions.asyncio, "sleep", AsyncMock())
    status = await permissions._await_grant(lambda: False, ceiling_s=0.03, poll_s=0.001)
    assert status == "timeout"


@pytest.mark.asyncio
async def test_await_grant_skips_on_keypress(monkeypatch):
    monkeypatch.setattr(permissions.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(permissions, "_skip_pressed", lambda tty: True)
    status = await permissions._await_grant(lambda: False, ceiling_s=5.0, tty=object())
    assert status == "skipped"


def test_check_command_exits_zero_even_when_probes_false():
    from emma.permissions import main

    with (
        patch("core.permissions.check_microphone", return_value=False),
        patch("core.permissions.check_accessibility_ax", return_value=False),
        patch("core.permissions.check_automation", return_value=False),
    ):
        rc = main(["check"])
    assert rc == 0


def test_automation_app_list_covers_macos_tool_apps():
    for app in ("Calendar", "Mail", "Messages", "Notes", "Reminders",
                "Safari", "Finder", "Music", "Terminal"):
        assert app in _AUTOMATION_APPS
