"""Apple Notes via AppleScript: list, search, create, append."""

from __future__ import annotations

from typing import Any

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.notes")

_NOTES_TIMEOUT_S = 15.0


def _parse_notes(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        title, _, excerpt = line.partition("|")
        out.append({"title": title.strip(), "excerpt": excerpt.strip()})
    return out


async def _list(name_filter: str, limit: int) -> list[dict[str, Any]]:
    if name_filter:
        f = macos.esc_applescript(name_filter)
        selector = f'(notes whose name contains "{f}")'
    else:
        selector = "notes"
    script = (
        'tell application "Notes"\n'
        'set out to ""\n'
        "set n to 0\n"
        f"repeat with nt in {selector}\n"
        '  set ex to ""\n'
        "  try\n"
        "    set ex to paragraph 2 of (plaintext of nt)\n"
        "  end try\n"
        '  set out to out & (name of nt) & "|" & ex & linefeed\n'
        "  set n to n + 1\n"
        f"  if n ≥ {int(limit)} then exit repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    raw = await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
    return _parse_notes(raw)


@tool()
async def list_notes(query: str = "", limit: int = 10) -> ToolResult:
    """Lista tus notas de Apple Notes (título y un fragmento). `query` filtra por título."""
    try:
        notes = await _list(query, limit)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
    if not notes:
        return ToolResult(True, {"notes": []}, "No encontré notas.", False)
    spoken = "; ".join(n["title"] for n in notes)
    return ToolResult(True, {"notes": notes}, f"Tus notas: {spoken}.", False)


@tool()
async def search_notes(query: str, limit: int = 10) -> ToolResult:
    """Busca notas cuyo título contiene `query`."""
    try:
        notes = await _list(query, limit)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude buscar notas: {exc}", False)
    spoken = "; ".join(n["title"] for n in notes) or "nada"
    return ToolResult(True, {"notes": notes}, f"Encontré: {spoken}.", False)


@tool()
async def create_note(title: str, body: str, folder: str = "") -> ToolResult:
    """Crea una nota nueva en Apple Notes con `title` y `body`.

    `folder` es opcional; si se omite, usa la carpeta por defecto.
    """
    t = macos.esc_applescript(title)
    b = macos.esc_applescript(body)
    note_props = f'{{body:"{t}" & linefeed & "{b}"}}'
    if folder:
        f = macos.esc_applescript(folder)
        make = f'tell folder "{f}" to make new note with properties {note_props}'
    else:
        make = f"make new note with properties {note_props}"
    script = f'tell application "Notes"\n{make}\nend tell'
    try:
        await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear la nota: {exc}", False)
    return ToolResult(True, {"title": title}, f"Listo, creé la nota '{title}'.", False)


@tool()
async def append_to_note(title: str, text: str) -> ToolResult:
    """Agrega `text` al final de la nota cuyo título es `title`."""
    t = macos.esc_applescript(title)
    x = macos.esc_applescript(text)
    script = (
        'tell application "Notes"\n'
        f'  set matches to (notes whose name is "{t}")\n'
        '  if (count of matches) is 0 then error "no encontré esa nota"\n'
        "  set theNote to item 1 of matches\n"
        f'  set body of theNote to (body of theNote) & "<div>{x}</div>"\n'
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude agregar a la nota: {exc}", False)
    return ToolResult(True, {"title": title}, f"Agregado a '{title}'.", False)


@tool(destructive=True)
async def delete_note(title: str, confirmed: bool = False) -> ToolResult:
    """Borra la nota de Apple Notes cuyo título es `title`. Pide confirmación."""
    if not confirmed:
        return ToolResult(True, {"title": title}, f"¿Borro la nota '{title}'?", True)
    t = macos.esc_applescript(title)
    script = (
        'tell application "Notes"\n'
        f'  set matches to (notes whose name is "{t}")\n'
        "  set deletedCount to 0\n"
        "  repeat with nt in matches\n"
        "    delete nt\n"
        "    set deletedCount to deletedCount + 1\n"
        "  end repeat\n"
        "  return deletedCount\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_NOTES_TIMEOUT_S, on_error="No pude borrar la nota"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    try:
        n = int((out or "0").strip())
    except ValueError:
        n = 0
    if n == 0:
        return ToolResult(True, {"deleted": 0}, f"No encontré la nota '{title}'.", False)
    return ToolResult(True, {"deleted": n}, f"Listo, borré la nota '{title}'.", False)
