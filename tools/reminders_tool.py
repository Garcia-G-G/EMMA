"""Apple Reminders via AppleScript: today's list, add, complete."""

from __future__ import annotations

import datetime as dt

import structlog

from actions import macos
from memory import episodic
from tools.base import ToolResult, tool
from tools.disambiguation import FIELD_SEP, ISO_DATE_HANDLER, disambiguate, parse_matches

log = structlog.get_logger("emma.tools.reminders")

_REM_TIMEOUT_S = 15.0


def _date_setter(var: str, d: dt.datetime) -> str:
    return (
        f"set {var} to (current date)\n"
        f"set year of {var} to {d.year}\n"
        f"set month of {var} to {d.month}\n"
        f"set day of {var} to {d.day}\n"
        f"set hours of {var} to {d.hour}\n"
        f"set minutes of {var} to {d.minute}\n"
        f"set seconds of {var} to 0\n"
    )


@tool()
async def list_today() -> ToolResult:
    """Lista los recordatorios pendientes que vencen hoy."""
    script = (
        'tell application "Reminders"\n'
        'set out to ""\n'
        "repeat with r in (reminders whose completed is false)\n"
        '  set dd to "none"\n'
        "  try\n"
        "    set theDate to due date of r\n"
        '    set dd to (year of theDate as string) & "-" & (month of theDate as integer) & "-" & (day of theDate)\n'
        "  end try\n"
        '  set out to out & (name of r) & "|" & dd & linefeed\n'
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    try:
        raw = await macos.osascript(script, timeout_s=_REM_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer los recordatorios: {exc}", False)
    today = dt.date.today()
    todays: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, _, dd = line.partition("|")
        if dd.strip() == "none":
            continue
        try:
            y, mo, d = (int(x) for x in dd.strip().split("-"))
            if dt.date(y, mo, d) == today:
                todays.append(name.strip())
        except (ValueError, TypeError):
            continue
    if not todays:
        return ToolResult(True, {"reminders": []}, "No tienes recordatorios para hoy.", False)
    return ToolResult(True, {"reminders": todays}, f"Para hoy: {'; '.join(todays)}.", False)


@tool()
async def add_reminder(
    title: str, due_iso: str = "", list_name: str = "Reminders", confirmed: bool = False
) -> ToolResult:
    """Agrega un recordatorio. `due_iso` (ISO) es opcional; `list_name` es la lista destino."""
    t = macos.esc_applescript(title)
    ln = macos.esc_applescript(list_name)
    if due_iso:
        try:
            due = dt.datetime.fromisoformat(due_iso)
        except ValueError:
            return ToolResult(False, None, "La fecha debe estar en formato ISO.", False)
        props = f'{{name:"{t}", due date:dueD}}'
        make = _date_setter("dueD", due) + f"make new reminder with properties {props}"
    else:
        make = f'make new reminder with properties {{name:"{t}"}}'
    script = f'tell application "Reminders"\ntell list "{ln}"\n{make}\nend tell\nend tell'
    try:
        await macos.osascript(script, timeout_s=_REM_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear el recordatorio: {exc}", False)
    # 28.1-B2: undo a creation by deleting it.
    reverse = episodic.blueprint_inverse("delete_reminder", {"title": title})
    return ToolResult(
        True, {"title": title, "_reverse_blueprint": reverse}, f"Listo, te recordaré: {title}.", False
    )


def _enumerate_reminders_script(title_esc: str, limit: int, completed: bool = False) -> str:
    """Enumerate reminders by name + completed-state → ``id‖dueISO‖name‖`` per match.

    Enumeration only (Bug 19.2-B2). Due date may be missing — guarded to "".
    """
    state = "true" if completed else "false"
    return (
        ISO_DATE_HANDLER + 'tell application "Reminders"\n'
        '  set out to ""\n'
        "  set k to 0\n"
        f'  repeat with r in (reminders whose name is "{title_esc}" and completed is {state})\n'
        '    set dwhen to ""\n'
        "    try\n"
        "      set dwhen to my isoDate(due date of r)\n"
        "    end try\n"
        "    set out to out & (id of r) & "
        f'"{FIELD_SEP}" & dwhen & "{FIELD_SEP}" & (name of r) & "{FIELD_SEP}" & linefeed\n'
        "    set k to k + 1\n"
        f"    if k ≥ {int(limit)} then exit repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )


@tool(destructive=True)
async def complete_reminder(
    title: str, index: int | None = None, confirmed: bool = False
) -> ToolResult:
    """Marca como completado UN recordatorio pendiente `title`. Pide confirmación.

    Si hay varios con el mismo nombre, los enumera (con fecha) y pide cuál (por
    número) — nunca completa todos a ciegas (Bug 19.2-B2)."""
    t = macos.esc_applescript(title)
    try:
        raw = await macos.osascript(
            _enumerate_reminders_script(t, limit=25), timeout_s=_REM_TIMEOUT_S
        )
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer los recordatorios: {exc}", False)
    matches = parse_matches(raw)

    chosen, response = disambiguate(matches, index, noun="recordatorio", title=title)
    if response is not None:
        return response
    assert chosen is not None

    if not confirmed:
        when = f" (vence {chosen.when})" if chosen.when else ""
        return ToolResult(
            True,
            {"title": chosen.title, "id": chosen.id},
            f"¿Marco completado '{chosen.title}'{when}?",
            True,
        )

    rid = macos.esc_applescript(chosen.id)
    script = (
        f'tell application "Reminders"\n  set completed of (reminder id "{rid}") to true\nend tell'
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_REM_TIMEOUT_S, on_error="No pude completar el recordatorio"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    # 28.1-B1: completing is a flag flip — the inverse is the flip back (no snapshot).
    reverse = episodic.blueprint_inverse("uncomplete_reminder", {"title": chosen.title})
    return ToolResult(
        True,
        {"completed": 1, "title": chosen.title, "_reverse_blueprint": reverse},
        f"Hecho: {chosen.title}.",
        False,
    )


@tool(destructive=True)
async def uncomplete_reminder(
    title: str, index: int | None = None, confirmed: bool = False
) -> ToolResult:
    """Reabre (marca como NO completado) un recordatorio que estaba completado.

    Para deshacer un 'completar' o si lo marcaste por error. Pide confirmación;
    si hay varios con el mismo nombre, enumera y pide cuál."""
    t = macos.esc_applescript(title)
    try:
        raw = await macos.osascript(
            _enumerate_reminders_script(t, limit=25, completed=True), timeout_s=_REM_TIMEOUT_S
        )
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer los recordatorios: {exc}", False)
    chosen, response = disambiguate(parse_matches(raw), index, noun="recordatorio", title=title)
    if response is not None:
        return response
    assert chosen is not None
    if not confirmed:
        return ToolResult(True, {"title": chosen.title, "id": chosen.id}, f"¿Reabro '{chosen.title}'?", True)
    rid = macos.esc_applescript(chosen.id)
    script = f'tell application "Reminders"\n  set completed of (reminder id "{rid}") to false\nend tell'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_REM_TIMEOUT_S, on_error="No pude reabrir el recordatorio"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    reverse = episodic.blueprint_inverse("complete_reminder", {"title": chosen.title})
    return ToolResult(
        True, {"reopened": 1, "title": chosen.title, "_reverse_blueprint": reverse},
        f"Reabrí: {chosen.title}.", False,
    )


@tool(destructive=True)
async def delete_reminder(
    title: str, index: int | None = None, confirmed: bool = False
) -> ToolResult:
    """Borra UN recordatorio pendiente `title`. Pide confirmación.

    Si hay varios con el mismo nombre, enumera y pide cuál — nunca borra todos
    (Bug 19.2-B2)."""
    t = macos.esc_applescript(title)
    try:
        raw = await macos.osascript(
            _enumerate_reminders_script(t, limit=25), timeout_s=_REM_TIMEOUT_S
        )
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer los recordatorios: {exc}", False)
    chosen, response = disambiguate(parse_matches(raw), index, noun="recordatorio", title=title)
    if response is not None:
        return response
    assert chosen is not None
    if not confirmed:
        when = f" (vence {chosen.when})" if chosen.when else ""
        return ToolResult(
            True, {"title": chosen.title, "id": chosen.id},
            f"¿Borro el recordatorio '{chosen.title}'{when}?", True,
        )
    # 28.1-B3: snapshot title + due BEFORE deleting so undo recreates it.
    due_iso = chosen.when or ""
    rid = macos.esc_applescript(chosen.id)
    script = f'tell application "Reminders" to delete (reminder id "{rid}")'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_REM_TIMEOUT_S, on_error="No pude borrar el recordatorio"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    reverse = episodic.blueprint_inverse("add_reminder", {"title": chosen.title, "due_iso": due_iso})
    return ToolResult(
        True, {"deleted": 1, "title": chosen.title, "_reverse_blueprint": reverse},
        f"Listo, borré el recordatorio '{chosen.title}'.", False,
    )
