"""calendar_events fixes: hidden launch when Calendar is closed, TTL cache to
collapse the startup triple-fetch, and warning debounce (the -600 spam)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.proactive import calendar_events as ce

RAW = "2026-6-4-9-0|2026-6-4-10-0|Standup|UID-1\n"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test starts with cold cache and untouched launch/warn flags."""
    monkeypatch.setattr(ce, "_cache", None)
    monkeypatch.setattr(ce, "_launch_attempted", False)
    monkeypatch.setattr(ce, "_warned_not_running", False)


class TestHiddenLaunch:
    @pytest.mark.asyncio
    async def test_launches_calendar_hidden_when_not_running(self, monkeypatch):
        running = AsyncMock(side_effect=[False, True])  # closed, then up after launch
        launch = AsyncMock()
        monkeypatch.setattr(ce.macos, "app_is_running", running)
        monkeypatch.setattr(ce.macos, "launch_app", launch)
        monkeypatch.setattr(ce.macos, "osascript", AsyncMock(return_value=RAW))

        events = await ce.today_events_raw()

        launch.assert_awaited_once_with("Calendar", warmup_s=3.0, background=True)
        assert len(events) == 1
        assert events[0].title == "Standup"

    @pytest.mark.asyncio
    async def test_launch_attempted_only_once_per_run(self, monkeypatch):
        """If Garcia quits Calendar, Emma must not relaunch it on every poll."""
        monkeypatch.setattr(ce.macos, "app_is_running", AsyncMock(return_value=False))
        launch = AsyncMock()
        monkeypatch.setattr(ce.macos, "launch_app", launch)
        osa = AsyncMock(return_value=RAW)
        monkeypatch.setattr(ce.macos, "osascript", osa)

        assert await ce.today_events_raw() == []
        monkeypatch.setattr(ce, "_cache", None)  # bypass TTL for the second poll
        assert await ce.today_events_raw() == []

        assert launch.await_count == 1
        osa.assert_not_awaited()  # never runs AppleScript against a dead app


class TestTTLCache:
    @pytest.mark.asyncio
    async def test_concurrent_calls_share_one_fetch(self, monkeypatch):
        """The three startup proactivities must not fire 3 AppleScripts."""
        monkeypatch.setattr(ce.macos, "app_is_running", AsyncMock(return_value=True))
        osa = AsyncMock(return_value=RAW)
        monkeypatch.setattr(ce.macos, "osascript", osa)

        results = await asyncio.gather(
            ce.today_events_raw(), ce.today_events_raw(), ce.today_events_raw()
        )

        assert osa.await_count == 1
        assert all(len(r) == 1 for r in results)

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, monkeypatch):
        monkeypatch.setattr(ce.macos, "app_is_running", AsyncMock(return_value=True))
        osa = AsyncMock(return_value=RAW)
        monkeypatch.setattr(ce.macos, "osascript", osa)

        await ce.today_events_raw()
        stamp, events = ce._cache
        monkeypatch.setattr(ce, "_cache", (stamp - ce._CACHE_TTL_S - 1, events))
        await ce.today_events_raw()

        assert osa.await_count == 2

    @pytest.mark.asyncio
    async def test_callers_get_independent_copies(self, monkeypatch):
        monkeypatch.setattr(ce.macos, "app_is_running", AsyncMock(return_value=True))
        monkeypatch.setattr(ce.macos, "osascript", AsyncMock(return_value=RAW))

        a = await ce.today_events_raw()
        b = await ce.today_events_raw()
        a.clear()
        assert len(b) == 1  # mutating one caller's list must not corrupt the cache


class TestSlowScanHandling:
    """Garcia's main calendar takes ~40s to scan via AppleScript (measured
    2026-06-04) — the fetch timeout must exceed it, and failures must be
    cached so a bad poll doesn't re-grind Calendar 60s later."""

    def test_fetch_timeout_covers_measured_scan(self):
        assert ce._FETCH_TIMEOUT_S >= 90.0

    def test_cache_ttl_keeps_scan_duty_cycle_low(self):
        assert ce._CACHE_TTL_S >= 300.0

    @pytest.mark.asyncio
    async def test_failure_is_cached_until_ttl(self, monkeypatch):
        monkeypatch.setattr(ce.macos, "app_is_running", AsyncMock(return_value=True))
        osa = AsyncMock(side_effect=ce.macos.AppleScriptError("app_dialog_blocked: timeout"))
        monkeypatch.setattr(ce.macos, "osascript", osa)

        assert await ce.today_events_raw() == []
        assert await ce.today_events_raw() == []  # served from cache, no re-grind

        assert osa.await_count == 1
