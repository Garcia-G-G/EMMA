"""Apple Calendar: READS via EventKit (indexed, ms-fast), WRITES via AppleScript.

Prompt 24 killed the AppleScript dead-path on reads: ``today_events`` /
``next_event`` / ``events_in_range`` scanned ``whose start date ≥ …`` O(all
events) — ~57 s on Garcia's calendars (``ERRORS-TO-FIX.md`` §1b) — and timed out.
They now go through :mod:`actions.calendar_store` (EventKit predicate, ~60 ms).

``create_event`` / ``delete_event`` stay on AppleScript: they're single, fast
operations (``make new event`` / delete-by-uid, never the slow scan), and
EventKit writes don't persist from Emma's non-bundled Python process (TCC grants
reads but not writes without an app-bundle usage description — verified in 24).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from actions import calendar_store, macos
from memory import episodic
from tools.base import ToolResult, tool
from tools.disambiguation import FIELD_SEP, ISO_DATE_HANDLER, disambiguate, parse_matches

log = structlog.get_logger("emma.tools.calendar")

# AppleScript timeout for the WRITE paths only (single ops; reads use EventKit).
_CAL_TIMEOUT_S = 20.0

# Spoken when calendar access isn't granted (reads need the Calendars TCC pane,
# requested at install — see core/permissions.py).
_AUTH_HINT = (
    "No tengo permiso de Calendarios. Ábrelo en Configuración del Sistema → "
    "Privacidad y Seguridad → Calendarios y activa Emma."
)


def _label(ev: dict[str, Any]) -> str:
    """'HH:MM — title (location)' for one EventKit event dict."""
    start: dt.datetime | None = ev.get("start")
    hhmm = start.strftime("%H:%M") if start else "??:??"
    label = f"{hhmm} — {ev.get('title', '')}"
    if ev.get("location"):
        label += f" ({ev['location']})"
    return label


def _to_tool_event(ev: dict[str, Any]) -> dict[str, Any]:
    """EventKit dict → the shape the voice layer already expects."""
    return {
        "iso": ev.get("start_iso", ""),
        "title": ev.get("title", ""),
        "location": ev.get("location") or "",
        "label": _label(ev),
    }


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


async def _fetch_between(start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    """EventKit read → the voice layer's event dicts (sorted by start)."""
    raw = await calendar_store.fetch_range(start, end)
    return [_to_tool_event(e) for e in raw]


@tool()
async def today_events() -> ToolResult:
    """Lista los eventos del calendario de hoy (hora, título, lugar)."""
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    try:
        events = await _fetch_between(start, end)
    except calendar_store.CalendarAuthError:
        return ToolResult(False, None, _AUTH_HINT, False)
    except Exception as exc:
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
    except calendar_store.CalendarAuthError:
        return ToolResult(False, None, _AUTH_HINT, False)
    except Exception as exc:
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
    except calendar_store.CalendarAuthError:
        return ToolResult(False, None, _AUTH_HINT, False)
    except Exception as exc:
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
    # 28.1-A1: undo a creation by deleting it (delete_event resolves by title).
    reverse = episodic.blueprint_inverse("delete_event", {"title": title})
    return ToolResult(True, {"title": title, "_reverse_blueprint": reverse}, f"Listo, creé '{title}'.", False)


async def _read_event_snapshot(uid: str) -> dict[str, Any] | None:
    """Read an event's start/end/location by uid (for the delete_event snapshot)."""
    u = macos.esc_applescript(uid)
    script = (
        ISO_DATE_HANDLER + 'tell application "Calendar"\n'
        "  repeat with cal in calendars\n"
        f'    repeat with ev in (every event of cal whose uid is "{u}")\n'
        "      set loc to \"\"\n"
        "      try\n        set loc to (location of ev)\n      end try\n"
        f'      return (my isoDate(start date of ev)) & "{FIELD_SEP}" & '
        f'(my isoDate(end date of ev)) & "{FIELD_SEP}" & loc\n'
        "    end repeat\n"
        "  end repeat\n"
        '  return ""\n'
        "end tell"
    )
    try:
        raw = (await macos.osascript(script, timeout_s=_CAL_TIMEOUT_S)).strip()
    except macos.AppleScriptError:
        return None
    parts = raw.split(FIELD_SEP)
    if len(parts) < 2 or not parts[0]:
        return None
    try:
        start = dt.datetime.fromisoformat(parts[0])
        end = dt.datetime.fromisoformat(parts[1])
        dur = max(1, int((end - start).total_seconds() // 60))
    except ValueError:
        return None
    return {"start_iso": parts[0], "duration_min": dur, "location": parts[2] if len(parts) > 2 else ""}


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

    # 28.1-A2: snapshot the event BEFORE deleting so undo can recreate it.
    snap = await _read_event_snapshot(chosen.id)
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
    if snap:
        reverse = episodic.blueprint_inverse("create_event", {"title": chosen.title, **snap})
    else:
        reverse = episodic.blueprint_manual(
            "Revísalo manualmente en Calendar; no pude guardar una copia antes de borrar."
        )
    return ToolResult(
        True,
        {"deleted": 1, "title": chosen.title, "_reverse_blueprint": reverse},
        f"Listo, borré '{chosen.title}'.",
        False,
    )
