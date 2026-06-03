"""Apple Calendar via AppleScript: read today's agenda, next event, create events."""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from actions import macos
from tools.base import ToolResult, tool
from tools.disambiguation import FIELD_SEP, ISO_DATE_HANDLER, disambiguate, parse_matches

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


def _parse_events(raw: str) -> list[dict[str, Any]]:
    """Parse 'Y-M-D-h-m|summary|location' lines into sorted event dicts."""
    events: list[dict[str, Any]] = []
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
            {
                "iso": when.isoformat(),
                "title": summary,
                "location": location,
                "label": label,
                "_dt": when,
            }
        )
    events.sort(key=lambda e: e["_dt"])
    for e in events:
        e.pop("_dt", None)
    return events


async def _fetch_between(start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    script = (
        'tell application "Calendar"\n'
        + _date_setter("startB", start)
        + _date_setter("endB", end)
        + 'set out to ""\n'
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


def _enumerate_events_script(title_esc: str, limit: int) -> str:
    """Enumerate events by summary → ``uid‖startISO‖summary‖`` per match.

    Enumeration only — never deletes (Bug 19.2-B2). The disambiguation date is
    the event's start date (more meaningful than a modification date here).
    """
    return (
        ISO_DATE_HANDLER + 'tell application "Calendar"\n'
        '  set out to ""\n'
        "  set k to 0\n"
        "  repeat with cal in calendars\n"
        f'    repeat with ev in (every event of cal whose summary is "{title_esc}")\n'
        "      set out to out & (uid of ev) & "
        f'"{FIELD_SEP}" & (my isoDate(start date of ev)) & '
        f'"{FIELD_SEP}" & (summary of ev) & "{FIELD_SEP}" & linefeed\n'
        "      set k to k + 1\n"
        f"      if k ≥ {int(limit)} then exit repeat\n"
        "    end repeat\n"
        f"    if k ≥ {int(limit)} then exit repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )


@tool(destructive=True)
async def delete_event(
    title: str, date: str = "", index: int | None = None, confirmed: bool = False
) -> ToolResult:
    """Borra UN evento del calendario cuyo título es `title`. Pide confirmación.

    Si hay varios con el mismo nombre, los enumera (con fecha) y pide cuál (por
    número) — nunca borra todos (Bug 19.2-B2). `date` es informativo.
    """
    t = macos.esc_applescript(title)
    try:
        raw = await macos.osascript(_enumerate_events_script(t, limit=25), timeout_s=_CAL_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer el calendario: {exc}", False)
    matches = parse_matches(raw)

    chosen, response = disambiguate(matches, index, noun="evento", title=title)
    if response is not None:
        return response
    assert chosen is not None

    if not confirmed:
        when = f" ({chosen.when})" if chosen.when else (f" del {date}" if date else "")
        return ToolResult(
            True,
            {"title": chosen.title, "uid": chosen.id},
            f"¿Borro el evento '{chosen.title}'{when}?",
            True,
        )

    uid = macos.esc_applescript(chosen.id)
    script = (
        'tell application "Calendar"\n'
        "  repeat with cal in calendars\n"
        f'    delete (every event of cal whose uid is "{uid}")\n'
        "  end repeat\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_CAL_TIMEOUT_S, on_error="No pude borrar el evento"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True, {"deleted": 1, "title": chosen.title}, f"Listo, borré '{chosen.title}'.", False
    )
