"""Shared today's-events fetch for the calendar proactivities.

``tools/calendar_tool.py`` only surfaces start-time + title; the proactive
features need **end times** (conflict detection) and a stable **uid** (fire
meeting-prep once per event), so this helper returns richer records.

Prompt 24: reads now go through :mod:`actions.calendar_store` (EventKit,
indexed, ~60 ms) instead of the AppleScript ``whose start date ≥ …`` scan that
took ~57 s and needed a 90 s timeout + a hidden Calendar.app launch. EventKit
reads the store directly — no GUI app, no timeout band-aid. The 30 s/10 min TTL
cache stays so a tight proactive loop doesn't re-query needlessly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass

import structlog

from actions import calendar_store

log = structlog.get_logger("emma.proactive.calendar")

# EventKit is ms-fast, but the three startup proactivities (meeting prep,
# conflicts, quiet hours) still fire within the same second — one fetch serves
# them all. 10 min TTL (failures cached too) keeps the duty cycle low.
_CACHE_TTL_S = 600.0
_cache: tuple[float, list[CalEvent]] | None = None
_fetch_lock = asyncio.Lock()


@dataclass
class CalEvent:
    start: dt.datetime
    end: dt.datetime
    title: str
    uid: str


async def today_events_raw() -> list[CalEvent]:
    """Today's calendar events with start, end, title, and uid. [] on failure.

    Cached for ``_CACHE_TTL_S``; concurrent callers share one fetch, so the
    startup proactivities cost a single EventKit query.
    """
    global _cache
    async with _fetch_lock:
        if _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
            return list(_cache[1])
        events = await _fetch_today_events()
        _cache = (time.monotonic(), events)
        return list(events)


async def _fetch_today_events() -> list[CalEvent]:
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    try:
        raw = await calendar_store.fetch_range(start, end)
    except calendar_store.CalendarAuthError:
        log.warning("calendar_unauthorized", hint="grant Calendars in Settings → Privacy")
        return []
    except Exception as exc:
        log.warning("calendar_fetch_failed", error=str(exc))
        return []
    out: list[CalEvent] = []
    for e in raw:
        s, en = e.get("start"), e.get("end")
        if s is None or en is None:
            continue
        out.append(CalEvent(start=s, end=en, title=e.get("title", ""), uid=e.get("id", "")))
    return out
