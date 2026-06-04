"""Shared today's-events fetch for the calendar proactivities.

``tools/calendar_tool.py`` only surfaces start-time + title (its `_parse_events`
drops end dates and uids). The proactive calendar features need **end times**
(conflict detection) and a stable **uid** (fire meeting-prep once per event), so
this helper runs its own AppleScript and returns richer records. It reuses
``actions.macos.osascript`` and never edits the calendar tool.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass

import structlog

from actions import macos

log = structlog.get_logger("emma.proactive.calendar")

# Calendar.app's AppleScript `whose start date ≥ …` scan is O(all events):
# measured 57 s across Garcia's 5 calendars (39.6 s on the main one alone,
# 2026-06-04). 90 s covers it with headroom. The durable fix is EventKit
# (indexed, ms-fast) — needs the Calendars TCC pane added to the permissions
# bootstrap, so it's a follow-up prompt, not a hotfix.
_FETCH_TIMEOUT_S = 90.0

# The startup proactivities (meeting prep, conflicts, quiet hours) all call
# today_events_raw() within the same second — one fetch serves them all.
# 10 min TTL (failures cached too) keeps the heavy scan at ≤6 runs/hour
# instead of grinding Calendar on every 60 s proactive tick.
_CACHE_TTL_S = 600.0
_cache: tuple[float, list[CalEvent]] | None = None
_fetch_lock = asyncio.Lock()

# Calendar.app must be running to answer AppleScript (error -600 otherwise).
# We launch it hidden at most once per Emma run: if Garcia quits it afterwards,
# that's his call — polls then skip quietly instead of relaunching every 60 s.
_launch_attempted = False
_warned_not_running = False


@dataclass
class CalEvent:
    start: dt.datetime
    end: dt.datetime
    title: str
    uid: str


def _date_setter(var: str, d: dt.datetime) -> str:
    """AppleScript building an absolute date in `var` (locale-independent)."""
    return (
        f"set {var} to (current date)\n"
        f"set year of {var} to {d.year}\n"
        f"set month of {var} to {d.month}\n"
        f"set day of {var} to {d.day}\n"
        f"set hours of {var} to {d.hour}\n"
        f"set minutes of {var} to {d.minute}\n"
        f"set seconds of {var} to {d.second}\n"
    )


def _stamp(prefix: str, var: str) -> str:
    return (
        f'(year of {var}) & "-" & (month of {var} as integer) & "-" & (day of {var}) '
        f'& "-" & (hours of {var}) & "-" & (minutes of {var})'
    )


def _parse(raw: str) -> list[CalEvent]:
    events: list[CalEvent] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.count("|") < 3:
            continue
        s_raw, e_raw, title, uid = line.split("|", 3)
        try:
            sy, smo, sd, sh, smi = (int(x) for x in s_raw.split("-"))
            ey, emo, ed, eh, emi = (int(x) for x in e_raw.split("-"))
            start = dt.datetime(sy, smo, sd, sh, smi)
            end = dt.datetime(ey, emo, ed, eh, emi)
        except (ValueError, TypeError):
            continue
        events.append(CalEvent(start=start, end=end, title=title.strip(), uid=uid.strip()))
    events.sort(key=lambda e: e.start)
    return events


async def _ensure_calendar_running() -> bool:
    """True if Calendar.app is up (launching it hidden, once per run, if not)."""
    global _launch_attempted, _warned_not_running
    if await macos.app_is_running("Calendar"):
        return True
    if not _launch_attempted:
        _launch_attempted = True
        log.info("calendar_launching_hidden")
        await macos.launch_app("Calendar", warmup_s=3.0, background=True)
        if await macos.app_is_running("Calendar"):
            return True
    if not _warned_not_running:
        _warned_not_running = True
        log.warning("calendar_not_running_skipped", hint="open Calendar.app to re-enable")
    else:
        log.debug("calendar_not_running_skipped")
    return False


async def today_events_raw() -> list[CalEvent]:
    """Today's calendar events with start, end, title, and uid. [] on failure.

    Results are cached for ``_CACHE_TTL_S`` and concurrent callers share one
    fetch, so the three startup proactivities cost a single AppleScript run.
    """
    global _cache
    async with _fetch_lock:
        if _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
            return list(_cache[1])
        if not await _ensure_calendar_running():
            return []
        events = await _fetch_today_events()
        _cache = (time.monotonic(), events)
        return list(events)


async def _fetch_today_events() -> list[CalEvent]:
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    script = (
        'tell application "Calendar"\n'
        + _date_setter("startB", start)
        + _date_setter("endB", end)
        + 'set out to ""\n'
        "repeat with cal in calendars\n"
        "  repeat with ev in (every event of cal whose start date ≥ startB and start date ≤ endB)\n"
        "    set sd to start date of ev\n"
        "    set ed to end date of ev\n"
        f'    set out to out & {_stamp("s", "sd")} & "|" & {_stamp("e", "ed")} '
        '& "|" & (summary of ev) & "|" & (uid of ev) & linefeed\n'
        "  end repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    try:
        raw = await macos.osascript(script, timeout_s=_FETCH_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        log.warning("calendar_fetch_failed", error=str(exc))
        return []
    return _parse(raw)
