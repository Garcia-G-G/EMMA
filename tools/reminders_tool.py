"""Apple Reminders via AppleScript: today's list, add, complete."""

from __future__ import annotations

import datetime as dt

import structlog

from actions import macos
from tools.base import ToolResult, tool

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
async def add_reminder(title: str, due_iso: str = "", list_name: str = "Reminders") -> ToolResult:
    """Agrega un recordatorio. `due_iso` (ISO) es opcional; `list_name` es la lista destino."""
    t = macos.esc_applescript(title)
    ln = macos.esc_applescript(list_name)
    if due_iso:
        try:
            due = dt.datetime.fromisoformat(due_iso)
        except ValueError:
            return ToolResult(False, None, "La fecha debe estar en formato ISO.", False)
        props = f'{{name:"{t}", due date:dueD}}'
        make = _date_setter("dueD", due) + f'make new reminder with properties {props}'
    else:
        make = f'make new reminder with properties {{name:"{t}"}}'
    script = (
        'tell application "Reminders"\n'
        f'tell list "{ln}"\n'
        f"{make}\n"
        "end tell\n"
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_REM_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear el recordatorio: {exc}", False)
    return ToolResult(True, {"title": title}, f"Listo, te recordaré: {title}.", False)


@tool(destructive=True)
async def complete_reminder(title: str, confirmed: bool = False) -> ToolResult:
    """Marca como completado el recordatorio pendiente `title`. Pide confirmación."""
    if not confirmed:
        return ToolResult(True, {"title": title}, f"¿Marco completado '{title}'?", True)
    t = macos.esc_applescript(title)
    script = (
        'tell application "Reminders"\n'
        f'  set matches to (reminders whose name is "{t}" and completed is false)\n'
        '  if (count of matches) is 0 then error "no encontré ese recordatorio"\n'
        "  set completed of (item 1 of matches) to true\n"
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_REM_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        msg = str(exc)
        if "app_dialog_blocked" in msg:
            return ToolResult(
                False, None, "macOS me pidió confirmar en pantalla. Autorízalo y dime otra vez.", False
            )
        return ToolResult(False, None, f"No pude completar el recordatorio: {msg}", False)
    return ToolResult(True, {"title": title}, f"Hecho: {title}.", False)
