"""Quiet hours + DND + calendar-aware silence.

A proactive event is demoted one priority step per active suppressor (quiet
window, in-meeting): SPEAK → NOTIFY → AMBIENT → SILENT. ``URGENT`` always passes
through untouched — the user opted in to ALWAYS receive those.
"""

from __future__ import annotations

import datetime as dt
import re

from config.settings import settings
from core.proactive.types import Priority

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _parse_windows(raw: str) -> list[tuple[dt.time, dt.time]]:
    if not raw:
        return []
    out: list[tuple[dt.time, dt.time]] = []
    for part in raw.split(","):
        m = _WINDOW_RE.match(part)
        if not m:
            continue
        a = dt.time(int(m.group(1)) % 24, int(m.group(2)) % 60)
        b = dt.time(int(m.group(3)) % 24, int(m.group(4)) % 60)
        out.append((a, b))
    return out


def _in_window(now: dt.time, a: dt.time, b: dt.time) -> bool:
    if a <= b:
        return a <= now <= b
    return now >= a or now <= b  # window wraps midnight


def in_quiet_hours(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    for a, b in _parse_windows(settings.PROACTIVE_QUIET_HOURS):
        if _in_window(now.time(), a, b):
            return True
    return False


async def in_meeting_now() -> bool:
    """True if a calendar event covers the current minute (don't speak over a
    real call). Best-effort: any failure returns False (fail open to delivery)."""
    from core.proactive.calendar_events import today_events_raw

    try:
        events = await today_events_raw()
    except Exception:
        return False
    now = dt.datetime.now()
    return any(ev.start <= now <= ev.end for ev in events)


def adjust_priority(p: Priority, quiet: bool, in_call: bool) -> Priority:
    """Demote one step per active suppressor (max two). URGENT bypasses."""
    if p == Priority.URGENT:
        return p
    steps = int(quiet) + int(in_call)
    if steps == 0:
        return p
    return Priority(max(int(Priority.SILENT), int(p) - steps))
