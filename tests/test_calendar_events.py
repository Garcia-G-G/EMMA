"""calendar_events (proactive reader): Prompt 24 migrated reads to EventKit.

The AppleScript hidden-launch + 90 s timeout path is gone (EventKit reads the
indexed store directly). What remains under test: the 10-min TTL cache that
collapses the startup triple-fetch, failure caching, and CalEvent marshalling.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from actions import calendar_store
from core.proactive import calendar_events as ce


def _raw(title="Standup", uid="UID-1"):
    base = dt.datetime(2026, 6, 4, 9, 0)
    return [{"start": base, "end": base + dt.timedelta(hours=1), "title": title, "id": uid}]


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(ce, "_cache", None)


class TestEventKitRead:
    @pytest.mark.asyncio
    async def test_marshals_to_calevent(self, monkeypatch):
        monkeypatch.setattr(ce.calendar_store, "fetch_range", AsyncMock(return_value=_raw()))
        events = await ce.today_events_raw()
        assert len(events) == 1
        assert events[0].title == "Standup" and events[0].uid == "UID-1"
        assert isinstance(events[0].start, dt.datetime) and isinstance(events[0].end, dt.datetime)

    @pytest.mark.asyncio
    async def test_unauthorized_returns_empty(self, monkeypatch):
        async def deny(start, end, calendars=None):
            raise calendar_store.CalendarAuthError("nope")

        monkeypatch.setattr(ce.calendar_store, "fetch_range", deny)
        assert await ce.today_events_raw() == []


class TestTTLCache:
    @pytest.mark.asyncio
    async def test_concurrent_calls_share_one_fetch(self, monkeypatch):
        """The three startup proactivities must not fire 3 EventKit queries."""
        fetch = AsyncMock(return_value=_raw())
        monkeypatch.setattr(ce.calendar_store, "fetch_range", fetch)
        results = await asyncio.gather(
            ce.today_events_raw(), ce.today_events_raw(), ce.today_events_raw()
        )
        assert fetch.await_count == 1
        assert all(len(r) == 1 for r in results)

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, monkeypatch):
        fetch = AsyncMock(return_value=_raw())
        monkeypatch.setattr(ce.calendar_store, "fetch_range", fetch)
        await ce.today_events_raw()
        stamp, events = ce._cache
        monkeypatch.setattr(ce, "_cache", (stamp - ce._CACHE_TTL_S - 1, events))
        await ce.today_events_raw()
        assert fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_callers_get_independent_copies(self, monkeypatch):
        monkeypatch.setattr(ce.calendar_store, "fetch_range", AsyncMock(return_value=_raw()))
        a = await ce.today_events_raw()
        b = await ce.today_events_raw()
        a.clear()
        assert len(b) == 1  # mutating one caller's list must not corrupt the cache

    def test_cache_ttl_keeps_query_duty_cycle_low(self):
        assert ce._CACHE_TTL_S >= 300.0

    @pytest.mark.asyncio
    async def test_failure_is_cached_until_ttl(self, monkeypatch):
        fetch = AsyncMock(side_effect=RuntimeError("eventkit blew up"))
        monkeypatch.setattr(ce.calendar_store, "fetch_range", fetch)
        assert await ce.today_events_raw() == []
        assert await ce.today_events_raw() == []  # served from cache, no re-query
        assert fetch.await_count == 1
