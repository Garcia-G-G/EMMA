"""Regression tests for the Prompt 15.8 permission-bootstrap fixes.

Covers the three root-cause bugs:
  * Bug A — `_ping_automation` must `launch application` THEN run a data-model
    query (two osascript calls, in order) so TCC fires for closed apps.
  * Bug B — `_say` must be BLOCKING (`subprocess.run` with a timeout), not
    fire-and-forget `subprocess.Popen`, so Spanish phrases don't overlap.
  * Coverage — every `_AUTOMATION_APPS` entry needs a `_AUTOMATION_QUERIES`
    entry so the probe is a real data-model query, not the UI-only fallback.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import permissions


@pytest.fixture(autouse=True)
def _restore_loop():
    # asyncio.run() leaves the loop policy with no current loop (py3.12),
    # which breaks sibling tests using get_event_loop(). Restore one — same
    # guard as tests/test_permissions_bootstrap.py.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# --- Bug B: _say is blocking, with a timeout -------------------------------


def test_say_uses_blocking_run_with_timeout() -> None:
    """`_say` must call subprocess.run (blocking) with a timeout kwarg."""
    with (
        patch("core.permissions.subprocess.run") as mock_run,
        patch("core.permissions.subprocess.Popen") as mock_popen,
    ):
        permissions._say("hola")

    assert mock_run.called, "_say must use subprocess.run (blocking)"
    assert not mock_popen.called, "_say must NOT use subprocess.Popen (non-blocking)"

    _args, kwargs = mock_run.call_args
    assert "timeout" in kwargs, "blocking _say must pass a timeout"
    assert kwargs["timeout"] > 0


def test_say_keeps_monica_voice() -> None:
    """Voice must remain 'Mónica' (constraint)."""
    with patch("core.permissions.subprocess.run") as mock_run:
        permissions._say("hola")
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["say", "-v", "Mónica"]


# --- Bug A: _ping_automation launches then queries -------------------------


def test_ping_automation_launches_then_queries() -> None:
    """Two osascript calls, in order: `launch application` then the query."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"3", b""))
    proc.returncode = 0

    with patch(
        "core.permissions.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ) as mock_exec:

        async def t() -> tuple[str, str]:
            return await permissions._ping_automation("Calendar")

        app, status = asyncio.run(t())

    assert app == "Calendar"
    assert status == "dialog_shown"
    assert mock_exec.call_count == 2, "must issue exactly two osascript calls"

    # call args are ("/usr/bin/osascript", "-e", <script>)
    first_script = mock_exec.call_args_list[0].args[2]
    second_script = mock_exec.call_args_list[1].args[2]
    assert first_script == 'launch application "Calendar"', first_script
    assert second_script == 'tell application "Calendar" to count calendars', second_script
    # ordering: launch must come before the data-model query
    assert first_script.startswith("launch application")
    assert second_script.startswith("tell application")


def test_ping_automation_reports_denied_on_minus_1743() -> None:
    """A -1743 stderr maps to the 'denied' status."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b"error: ... (-1743)"))
    proc.returncode = 1

    with patch(
        "core.permissions.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):

        async def t() -> tuple[str, str]:
            return await permissions._ping_automation("Mail")

        _app, status = asyncio.run(t())

    assert status == "denied"


# --- Coverage: every automation app has a data-model query -----------------


def test_every_automation_app_has_a_query() -> None:
    missing = [a for a in permissions._AUTOMATION_APPS if a not in permissions._AUTOMATION_QUERIES]
    assert not missing, f"_AUTOMATION_APPS entries without a query: {missing}"


def test_dwell_constant_is_at_least_four_seconds() -> None:
    """Per-app dwell must give the user >=4s to click Allow (constraint)."""
    assert permissions._DWELL_AFTER_DIALOG_S >= 4.0
