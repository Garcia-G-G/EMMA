"""Apple Calendar via AppleScript: read today's agenda, next event, create events."""

from __future__ import annotations

import datetime as dt

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.calendar")

# Calendar's AppleScript "whose start date ≥ ..." scans can be slow.
_CAL_TIMEOUT_S = 20.0


def _date_setter(var: str, d: dt.datetime) -> str:
    """AppleScript that builds an absolute date in `var` (locale-independent)."""
    return (
        f"set {var} to (current date)\n"
        f"set year of {var} to {d.year}\n"
        f"set month of {var} to {d.month}\n"
        f"set day of {var} to {d.day}\n"
        f"set hours of {var} to {d.hour}\n"
        f"set minutes of {var} to {d.minute}\n"
        f"set seconds of {var} to {d.second}\n"
    )


def _parse_events(raw: str) -> list[dict]:
    """Parse 'Y-M-D-h-m|summary|location' lines into sorted event dicts."""
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        stamp = parts[0]
        summary = parts[1] if len(parts) > 1 else ""
        location = parts[2] if len(parts) > 2 else ""
        try:
            y, mo, d, h, mi = (int(x) for x in stamp.split("-"))
            when = dt.datetime(y, mo, d, h, mi)
        except (ValueError, TypeError):
            continue
        label = f"{h:02d}:{mi:02d} — {summary}"
        if location:
            label += f" ({location})"
        events.append(
            {"iso": when.isoformat(), "title": summary, "location": location, "label": label, "_dt": when}
        )
    events.sort(key=lambda e: e["_dt"])
    for e in events:
        e.pop("_dt", None)
    return events


async def _fetch_between(start: dt.datetime, end: dt.datetime) -> list[dict]:
    script = (
        'tell application "Calendar"\n'
        + _date_setter("startB", start)
        + _date_setter("endB", end)
        + "set out to \"\"\n"
        "repeat with cal in calendars\n"
        "  repeat with ev in (every event of cal whose start date ≥ startB and start date ≤ endB)\n"
        "    set sd to start date of ev\n"
        '    set loc to ""\n'
        "    try\n"
        "      set loc to location of ev\n"
        "    end try\n"
        '    set out to out & (year of sd) & "-" & (month of sd as integer) & "-" & (day of sd) & "-" & (hours of sd) & "-" & (minutes of sd) & "|" & (summary of ev) & "|" & loc & linefeed\n'
        "  end repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    raw = await macos.osascript(script, timeout_s=_CAL_TIMEOUT_S)
    return _parse_events(raw)


@tool()
async def today_events() -> ToolResult:
    """Lista los eventos del calendario de hoy (hora, título, lugar)."""
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    try:
        events = await _fetch_between(start, end)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer el calendario: {exc}", False)
    if not events:
        return ToolResult(True, {"events": []}, "No tienes nada en el calendario hoy.", False)
    spoken = "; ".join(e["label"] for e in events)
    return ToolResult(True, {"events": events}, f"Hoy tienes: {spoken}.", False)


@tool()
async def next_event() -> ToolResult:
    """Dice cuál es tu próximo evento del calendario."""
    now = dt.datetime.now()
    end = now + dt.timedelta(days=14)
    try:
        events = await _fetch_between(now, end)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer el calendario: {exc}", False)
    if not events:
        return ToolResult(True, {"event": None}, "No tienes eventos próximos.", False)
    nxt = events[0]
    return ToolResult(True, {"event": nxt}, f"Tu próximo evento: {nxt['label']}.", False)


@tool()
async def events_in_range(start_iso: str, end_iso: str) -> ToolResult:
    """Lista los eventos del calendario entre dos fechas ISO (start_iso, end_iso)."""
    try:
        start = dt.datetime.fromisoformat(start_iso)
        end = dt.datetime.fromisoformat(end_iso)
    except ValueError:
        return ToolResult(False, None, "Las fechas deben estar en formato ISO.", False)
    try:
        events = await _fetch_between(start, end)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer el calendario: {exc}", False)
    spoken = "; ".join(e["label"] for e in events) or "nada"
    return ToolResult(True, {"events": events}, f"En ese rango: {spoken}.", False)


@tool(destructive=True)
async def create_event(
    title: str,
    start_iso: str,
    duration_min: int = 60,
    location: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Crea un evento en el calendario. Pide confirmación antes de crearlo.

    `start_iso` es la fecha/hora de inicio en formato ISO; `duration_min`
    la duración en minutos; `location` es opcional.
    """
    try:
        start = dt.datetime.fromisoformat(start_iso)
    except ValueError:
        return ToolResult(False, None, "La fecha de inicio debe estar en formato ISO.", False)
    if not confirmed:
        return ToolResult(
            True,
            {"title": title, "start": start.isoformat(), "duration_min": duration_min},
            f"¿Creo el evento '{title}' el {start.strftime('%d/%m a las %H:%M')}?",
            True,
        )
    end = start + dt.timedelta(minutes=int(duration_min))
    t = macos.esc_applescript(title)
    loc = macos.esc_applescript(location)
    script = (
        'tell application "Calendar"\n'
        + _date_setter("startD", start)
        + _date_setter("endD", end)
        + "tell calendar 1\n"
        f'  make new event with properties {{summary:"{t}", start date:startD, end date:endD, location:"{loc}"}}\n'
        "end tell\n"
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_CAL_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear el evento: {exc}", False)
    return ToolResult(True, {"title": title}, f"Listo, creé '{title}'.", False)


@tool(destructive=True)
async def delete_event(title: str, date: str = "", confirmed: bool = False) -> ToolResult:
    """Borra evento(s) del calendario cuyo título es `title`. Pide confirmación.

    `date` es opcional y solo se usa para la pregunta de confirmación.
    """
    if not confirmed:
        when = f" del {date}" if date else ""
        return ToolResult(
            True,
            {"title": title, "date": date},
            f"¿Borro el evento '{title}'{when}?",
            True,
        )
    t = macos.esc_applescript(title)
    script = (
        'tell application "Calendar"\n'
        "set deletedCount to 0\n"
        "repeat with cal in calendars\n"
        f'  repeat with ev in (every event of cal whose summary is "{t}")\n'
        "    delete ev\n"
        "    set deletedCount to deletedCount + 1\n"
        "  end repeat\n"
        "end repeat\n"
        "return deletedCount\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=15.0, on_error="No pude borrar el evento"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    try:
        n = int((out or "0").strip())
    except ValueError:
        n = 0
    if n == 0:
        return ToolResult(True, {"deleted": 0}, f"No encontré ningún evento '{title}'.", False)
    return ToolResult(True, {"deleted": n}, f"Listo, borré '{title}'.", False)
