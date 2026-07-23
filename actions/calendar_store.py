"""EventKit calendar READS — indexed, ms-fast, zero new pip (Prompt 24).

Binds EventKit dynamically through ``objc.loadBundle`` (pyobjc-core only; no
``pyobjc-framework-EventKit`` package). This replaces the AppleScript
``whose start date ≥ …`` scan, which was O(all events) — measured ~57 s across
the user's calendars (``ERRORS-TO-FIX.md`` §1b) — with
``predicateForEventsWithStartDate:endDate:calendars:`` against Apple's indexed
store (measured ~60 ms for a 7-day range, regardless of calendar size).

READS ONLY. EventKit *writes* from a non-bundled interpreter silently fail:
macOS TCC grants a bare ``python`` reads but rejects writes without an app
bundle carrying ``NSCalendarsFullAccessUsageDescription`` (``saveEvent:`` returns
success yet nothing persists — verified). So ``create_event`` / ``delete_event``
stay on AppleScript in ``tools/calendar_tool.py`` (single, fast operations that
were never the slow scan). This module is the single EventKit entry point.

EventKit enum ints (stable Apple constants):
  EKEntityTypeEvent = 0
  EKAuthorizationStatus: 0 notDetermined · 1 restricted · 2 denied ·
                         3 authorized · 4 fullAccess · 5 writeOnly
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import structlog

log = structlog.get_logger("emma.calendar_store")

_EK_ENTITY_EVENT = 0
_READ_OK_STATUS = (3, 4)  # authorized, fullAccess
_CALL_TIMEOUT_S = 5.0  # EventKit answers in ms; this only catches a runaway

_EVENTKIT_NS: dict[str, Any] = {}
_store: Any = None


class CalendarAuthError(Exception):
    """Calendar access is not granted (denied / not-determined / restricted)."""


def _eventkit() -> dict[str, Any]:
    if not _EVENTKIT_NS:
        import objc

        objc.loadBundle("EventKit", _EVENTKIT_NS, "/System/Library/Frameworks/EventKit.framework")
    return _EVENTKIT_NS


def _cls(name: str) -> Any:
    ek = _eventkit()
    if name in ek:
        return ek[name]
    import objc

    return objc.lookUpClass(name)


def _store_singleton() -> Any:
    global _store
    if _store is None:
        _store = _cls("EKEventStore").alloc().init()
    return _store


def authorization_status() -> int:
    """Raw EKAuthorizationStatus int for events (2/denied on any probe error)."""
    try:
        return int(_cls("EKEventStore").authorizationStatusForEntityType_(_EK_ENTITY_EVENT))
    except Exception as exc:
        log.warning("calendar_auth_status_failed", error=str(exc))
        return 2


def is_authorized() -> bool:
    """True if Emma can READ the calendar (authorized or fullAccess)."""
    return authorization_status() in _READ_OK_STATUS


def list_calendars() -> list[str]:
    """Display titles of the user's calendars (synchronous, cheap). [] on error."""
    try:
        cals = _store_singleton().calendarsForEntityType_(_EK_ENTITY_EVENT)
        return [str(c.title()) for c in (cals or [])]
    except Exception as exc:
        log.warning("calendar_list_failed", error=str(exc))
        return []


def _to_nsdate(d: dt.datetime) -> Any:
    from Foundation import NSDate

    return NSDate.dateWithTimeIntervalSince1970_(d.timestamp())


def _from_nsdate(nsdate: Any) -> dt.datetime | None:
    if nsdate is None:
        return None
    return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970())


def _marshal(ev: Any) -> dict[str, Any]:
    """One EKEvent → a plain dict (no PyObjC proxies escape this module)."""
    start = _from_nsdate(ev.startDate())
    end = _from_nsdate(ev.endDate())
    loc = ev.location()
    notes = ev.notes()
    cal = ev.calendar()
    return {
        "id": str(ev.eventIdentifier() or ""),
        "title": str(ev.title() or ""),
        "start": start,
        "end": end,
        "start_iso": start.isoformat() if start else "",
        "end_iso": end.isoformat() if end else "",
        "all_day": bool(ev.isAllDay()),
        "calendar": str(cal.title()) if cal else "",
        "location": str(loc) if loc else None,
        "notes": str(notes) if notes else None,
    }


def _fetch_sync(
    start: dt.datetime, end: dt.datetime, calendars: list[str] | None
) -> list[dict[str, Any]]:
    store = _store_singleton()
    cal_objs = None
    if calendars:
        wanted = {c.lower() for c in calendars}
        cal_objs = [
            c
            for c in (store.calendarsForEntityType_(_EK_ENTITY_EVENT) or [])
            if str(c.title()).lower() in wanted
        ] or None
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(start), _to_nsdate(end), cal_objs
    )
    events = store.eventsMatchingPredicate_(predicate)
    out = [_marshal(e) for e in (events or [])]
    out.sort(key=lambda e: e["start"] or dt.datetime.max)
    return out


async def fetch_range(
    start: dt.datetime, end: dt.datetime, calendars: list[str] | None = None
) -> list[dict[str, Any]]:
    """Events overlapping ``[start, end]`` as plain dicts, sorted by start.

    Raises :class:`CalendarAuthError` if calendar access isn't granted. Runs the
    EventKit call off the event loop (it's a sync C call) with a 5 s safety wall.
    """
    if not is_authorized():
        raise CalendarAuthError("calendar access not granted")
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _fetch_sync, start, end, calendars), timeout=_CALL_TIMEOUT_S
    )
