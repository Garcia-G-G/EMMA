"""Prompt 24: EventKit calendar reads (actions/calendar_store) + the tool layer.

EventKit objects are PyObjC proxies; we fake the few methods calendar_store
actually calls (start/end NSDate, title, calendar, etc.) so the marshalling and
filtering are tested without a live store. The fakes mimic Objective-C selector
names (camelCase) on purpose — hence the file-level N802 waiver.
"""

# ruff: noqa: N802

from __future__ import annotations

import datetime as dt

import pytest

from actions import calendar_store as cs


class _NSDate:
    def __init__(self, ts: float):
        self._ts = ts

    def timeIntervalSince1970(self) -> float:
        return self._ts


class _Cal:
    def __init__(self, title: str):
        self._t = title

    def title(self) -> str:
        return self._t


class _Event:
    def __init__(
        self, title, start, end, *, all_day=False, cal="Calendar", loc=None, notes=None, ident="id1"
    ):
        self._title, self._start, self._end = title, start, end
        self._all_day, self._cal, self._loc, self._notes, self._id = all_day, cal, loc, notes, ident

    def title(self):
        return self._title

    def startDate(self):
        return _NSDate(self._start.timestamp())

    def endDate(self):
        return _NSDate(self._end.timestamp())

    def isAllDay(self):
        return self._all_day

    def calendar(self):
        return _Cal(self._cal)

    def location(self):
        return self._loc

    def notes(self):
        return self._notes

    def eventIdentifier(self):
        return self._id


class _Store:
    def __init__(self, events, calendars=("Calendar",)):
        self._events = events
        self._cals = [_Cal(c) for c in calendars]
        self.last_calendars_arg = "unset"

    def predicateForEventsWithStartDate_endDate_calendars_(self, s, e, cals):
        self.last_calendars_arg = cals
        return ("pred", s, e, cals)

    def eventsMatchingPredicate_(self, pred):
        return self._events

    def calendarsForEntityType_(self, entity):
        return self._cals


def _evt(title, h_start, h_end, **kw):
    base = dt.datetime(2026, 6, 8, 0, 0)
    return _Event(title, base + dt.timedelta(hours=h_start), base + dt.timedelta(hours=h_end), **kw)


@pytest.fixture
def authorized(monkeypatch):
    monkeypatch.setattr(cs, "is_authorized", lambda: True)


class TestFetchRange:
    @pytest.mark.asyncio
    async def test_zero_events(self, monkeypatch, authorized):
        monkeypatch.setattr(cs, "_store_singleton", lambda: _Store([]))
        out = await cs.fetch_range(dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 9))
        assert out == []

    @pytest.mark.asyncio
    async def test_one_event_marshals_fully(self, monkeypatch, authorized):
        ev = _evt("Standup", 9, 10, loc="Zoom", notes="daily", ident="UID-1")
        monkeypatch.setattr(cs, "_store_singleton", lambda: _Store([ev]))
        out = await cs.fetch_range(dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 9))
        assert len(out) == 1
        e = out[0]
        assert e["id"] == "UID-1" and e["title"] == "Standup"
        assert e["start_iso"] == "2026-06-08T09:00:00" and e["end_iso"] == "2026-06-08T10:00:00"
        assert e["location"] == "Zoom" and e["notes"] == "daily" and e["calendar"] == "Calendar"
        assert e["all_day"] is False

    @pytest.mark.asyncio
    async def test_many_events_sorted_by_start(self, monkeypatch, authorized):
        store = _Store([_evt("Late", 15, 16), _evt("Early", 8, 9), _evt("Mid", 12, 13)])
        monkeypatch.setattr(cs, "_store_singleton", lambda: store)
        out = await cs.fetch_range(dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 9))
        assert [e["title"] for e in out] == ["Early", "Mid", "Late"]

    @pytest.mark.asyncio
    async def test_calendar_filter_passes_matching_objects(self, monkeypatch, authorized):
        store = _Store([_evt("X", 9, 10)], calendars=("Work", "Personal"))
        monkeypatch.setattr(cs, "_store_singleton", lambda: store)
        await cs.fetch_range(dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 9), calendars=["Work"])
        assert [c.title() for c in store.last_calendars_arg] == ["Work"]

    @pytest.mark.asyncio
    async def test_unauthorized_raises(self, monkeypatch):
        monkeypatch.setattr(cs, "is_authorized", lambda: False)
        with pytest.raises(cs.CalendarAuthError):
            await cs.fetch_range(dt.datetime(2026, 6, 8), dt.datetime(2026, 6, 9))


class TestAuthStatus:
    @pytest.mark.parametrize(
        "status,ok", [(0, False), (2, False), (3, True), (4, True), (5, False)]
    )
    def test_is_authorized_maps_status(self, monkeypatch, status, ok):
        class _EK:
            @staticmethod
            def authorizationStatusForEntityType_(entity):
                return status

        monkeypatch.setattr(cs, "_cls", lambda name: _EK)
        assert cs.authorization_status() == status
        assert cs.is_authorized() is ok

    def test_status_probe_error_is_denied(self, monkeypatch):
        def _boom(name):
            raise RuntimeError("no eventkit")

        monkeypatch.setattr(cs, "_cls", _boom)
        assert cs.authorization_status() == 2
        assert cs.is_authorized() is False


class TestToolLayer:
    @pytest.mark.asyncio
    async def test_today_events_formats_spoken(self, monkeypatch):
        from tools import calendar_tool as ct

        async def fake_fetch(start, end, calendars=None):
            return [
                {
                    "start": dt.datetime(2026, 6, 8, 9, 0),
                    "start_iso": "2026-06-08T09:00:00",
                    "title": "Standup",
                    "location": "Zoom",
                }
            ]

        monkeypatch.setattr(ct.calendar_store, "fetch_range", fake_fetch)
        r = await ct.today_events()
        assert r.success
        assert "09:00 — Standup (Zoom)" in r.user_message
        assert r.data["events"][0]["iso"] == "2026-06-08T09:00:00"

    @pytest.mark.asyncio
    async def test_today_events_empty(self, monkeypatch):
        from tools import calendar_tool as ct

        async def fake_fetch(start, end, calendars=None):
            return []

        monkeypatch.setattr(ct.calendar_store, "fetch_range", fake_fetch)
        r = await ct.today_events()
        assert r.success and "nada" in r.user_message.lower()

    @pytest.mark.asyncio
    async def test_auth_denied_friendly_message(self, monkeypatch):
        from tools import calendar_tool as ct

        async def deny(start, end, calendars=None):
            raise ct.calendar_store.CalendarAuthError("nope")

        monkeypatch.setattr(ct.calendar_store, "fetch_range", deny)
        r = await ct.today_events()
        assert r.success is False
        assert "permiso de Calendarios" in r.user_message

    @pytest.mark.asyncio
    async def test_next_event_picks_earliest(self, monkeypatch):
        from tools import calendar_tool as ct

        async def fake_fetch(start, end, calendars=None):
            return [
                {
                    "start": dt.datetime(2026, 6, 8, 9, 0),
                    "start_iso": "x",
                    "title": "First",
                    "location": "",
                },
                {
                    "start": dt.datetime(2026, 6, 8, 14, 0),
                    "start_iso": "y",
                    "title": "Second",
                    "location": "",
                },
            ]

        monkeypatch.setattr(ct.calendar_store, "fetch_range", fake_fetch)
        r = await ct.next_event()
        assert "First" in r.user_message

    @pytest.mark.asyncio
    async def test_events_in_range_bad_iso(self):
        from tools import calendar_tool as ct

        r = await ct.events_in_range("not-a-date", "also-bad")
        assert r.success is False and "ISO" in r.user_message
