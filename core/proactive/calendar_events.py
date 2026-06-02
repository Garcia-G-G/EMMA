"""Shared today's-events fetch for the calendar proactivities.

``tools/calendar_tool.py`` only surfaces start-time + title (its `_parse_events`
drops end dates and uids). The proactive calendar features need **end times**
(conflict detection) and a stable **uid** (fire meeting-prep once per event), so
this helper runs its own AppleScript and returns richer records. It reuses
``actions.macos.osascript`` and never edits the calendar tool.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import structlog

from actions import macos

log = structlog.get_logger("emma.proactive.calendar")

_CAL_TIMEOUT_S = 20.0


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


async def today_events_raw() -> list[CalEvent]:
    """Today's calendar events with start, end, title, and uid. [] on failure."""
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
        raw = await macos.osascript(script, timeout_s=_CAL_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        log.warning("calendar_fetch_failed", error=str(exc))
        return []
    return _parse(raw)
