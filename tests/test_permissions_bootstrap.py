"""Tests for the install-time permission bootstrap.

osascript / say / sleep are mocked so the walkthrough runs fast and silent
and never pops real dialogs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import permissions
from core.permissions import _AUTOMATION_APPS, _MANUAL_PANES


@pytest.fixture(autouse=True)
def _restore_loop():
    # asyncio.run() leaves the loop policy with no current loop (py3.12),
    # which breaks sibling tests using get_event_loop(). Restore one.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


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


def test_bootstrap_pings_every_automation_app_and_opens_every_pane() -> None:
    fake_exec, calls = _fake_subprocess()
    with (
        patch("core.permissions.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("core.permissions.asyncio.sleep", new=AsyncMock(return_value=None)),
        patch("core.permissions._say") as say,
        patch("core.permissions._open_settings") as open_settings,
        patch("core.permissions.check_microphone", return_value=True),
    ):
        results = asyncio.run(permissions.bootstrap())

    # two osascript invocations per automation app: `launch application` then
    # the data-model query (the two-stage TCC trigger from Prompt 15.8 Bug A).
    assert len(calls) == 2 * len(_AUTOMATION_APPS)
    for app in _AUTOMATION_APPS:
        assert results[f"Automation:{app}"] == "dialog_shown"

    # one Settings pane opened per manual permission, in order
    opened = [c.args[0] for c in open_settings.call_args_list]
    assert opened == [pane for pane, _ in _MANUAL_PANES]

    # spoken context fired (mic + each app + each pane + closing line)
    assert say.call_count >= len(_AUTOMATION_APPS) + len(_MANUAL_PANES)
    assert results["Microphone"] == "granted"


def test_bootstrap_reports_microphone_denied() -> None:
    fake_exec, _ = _fake_subprocess()
    with (
        patch("core.permissions.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("core.permissions.asyncio.sleep", new=AsyncMock(return_value=None)),
        patch("core.permissions._say"),
        patch("core.permissions._open_settings"),
        patch("core.permissions.check_microphone", return_value=False),
    ):
        results = asyncio.run(permissions.bootstrap())
    assert results["Microphone"] == "denied_or_pending"


def test_check_command_exits_zero_even_when_probes_false() -> None:
    from emma.permissions import main

    with (
        patch("core.permissions.check_microphone", return_value=False),
        patch("core.permissions.check_accessibility", return_value=False),
        patch("core.permissions.check_automation", return_value=False),
    ):
        rc = main(["check"])
    assert rc == 0


def test_automation_app_list_covers_macos_tool_apps() -> None:
    # Guard the convention: the 7 Prompt-15 apps plus Music (music.py) and
    # Terminal (dev.py) must all be in the canonical bootstrap list.
    for app in (
        "Calendar",
        "Mail",
        "Messages",
        "Notes",
        "Reminders",
        "Safari",
        "Finder",
        "Music",
        "Terminal",
    ):
        assert app in _AUTOMATION_APPS
