"""The main loop must stop on request_shutdown, even when a session returns
normally (Pipecat swallows the task cancellation during an active session, so a
cancel alone would spin back to wake-word listening)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import core.orchestrator as orch


@pytest.fixture(autouse=True)
def _clear_shutdown():
    orch._shutdown.clear()
    yield
    orch._shutdown.clear()


def test_request_shutdown_sets_event():
    assert not orch._shutdown.is_set()
    orch.request_shutdown()
    assert orch._shutdown.is_set()


def test_main_loop_exits_after_session_when_shutdown_requested():
    calls = {"n": 0}

    async def fake_one_session():
        # Simulate a session that returns normally (cancel was swallowed by
        # Pipecat) but a shutdown was requested during it.
        calls["n"] += 1
        orch.request_shutdown()

    with (
        patch.object(orch, "_one_session", new=fake_one_session),
        patch("tools.browser.shutdown_browser", new=AsyncMock()),
    ):
        asyncio.run(orch.main_loop())

    assert orch._shutdown.is_set()
    assert calls["n"] == 1  # exited after exactly one session, did not loop back


def test_main_loop_loops_until_shutdown():
    calls = {"n": 0}

    async def fake_one_session():
        calls["n"] += 1
        if calls["n"] >= 3:
            orch.request_shutdown()

    with (
        patch.object(orch, "_one_session", new=fake_one_session),
        patch("tools.browser.shutdown_browser", new=AsyncMock()),
    ):
        asyncio.run(orch.main_loop())

    assert calls["n"] == 3
