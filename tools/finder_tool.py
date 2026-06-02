"""Finder / filesystem via AppleScript do-shell-script: list, Spotlight search, move, open."""

from __future__ import annotations

import os

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.finder")

_FINDER_TIMEOUT_S = 15.0


def _expand(path: str) -> str:
    return os.path.expanduser(path)


@tool()
async def list_folder(path: str) -> ToolResult:
    """Lista el contenido de una carpeta (nombre y fecha de modificación)."""
    p = macos.esc_applescript(_expand(path))
    # cd then stat each entry (basename); [ -e ] guards the no-match glob.
    script = (
        f'set p to "{p}"\n'
        'do shell script "cd " & quoted form of p & " && for f in *; do '
        '[ -e \\"$f\\" ] && stat -f \'%Sm|%N\' -t \'%Y-%m-%d %H:%M\' \\"$f\\"; done | head -200"'
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_FINDER_TIMEOUT_S, on_error="No pude leer la carpeta"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    entries: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        modified, _, name = line.partition("|")
        entries.append({"name": name.strip(), "modified": modified.strip()})
    if not entries:
        return ToolResult(True, {"entries": []}, "La carpeta está vacía.", False)
    return ToolResult(True, {"entries": entries}, f"{len(entries)} elementos en {path}.", False)


@tool()
async def find_recent(query: str = "", days: int = 7, limit: int = 10) -> ToolResult:
    """Busca archivos modificados en los últimos `days` días, opcionalmente con `query` en el nombre."""
    q = query.replace('"', "").strip()
    expr = f"kMDItemContentModificationDate >= $time.today(-{int(days)})"
    if q:
        expr += f' && kMDItemDisplayName == "*{q}*"cd'
    expr_esc = macos.esc_applescript(expr)
    script = (
        f'set expr to "{expr_esc}"\n'
        f'do shell script "mdfind " & quoted form of expr & " | head -{int(limit)}"'
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_FINDER_TIMEOUT_S, on_error="No pude buscar archivos"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    files = [
        {"name": os.path.basename(p.strip()), "path": p.strip()}
        for p in out.splitlines()
        if p.strip()
    ]
    if not files:
        return ToolResult(True, {"files": []}, "No encontré archivos recientes con eso.", False)
    spoken = "; ".join(f["name"] for f in files)
    return ToolResult(True, {"files": files}, f"Encontré: {spoken}.", False)


@tool()
async def open_item(path: str) -> ToolResult:
    """Abre un archivo o carpeta con su aplicación predeterminada."""
    p = macos.esc_applescript(_expand(path))
    script = f'set p to "{p}"\ndo shell script "open " & quoted form of p'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_FINDER_TIMEOUT_S, on_error="No pude abrir eso"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"path": path}, f"Abriendo {path}.", False)


@tool(destructive=True)
async def move_item(src: str, dst: str, confirmed: bool = False) -> ToolResult:
    """Mueve un archivo o carpeta de `src` a `dst`. Pide confirmación primero."""
    if not confirmed:
        return ToolResult(True, {"src": src, "dst": dst}, f"¿Muevo '{src}' a '{dst}'?", True)
    s = macos.esc_applescript(_expand(src))
    d = macos.esc_applescript(_expand(dst))
    script = (
        f'set s to "{s}"\nset d to "{d}"\n'
        'do shell script "mv -n " & quoted form of s & " " & quoted form of d'
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_FINDER_TIMEOUT_S, on_error="No pude mover el elemento"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"src": src, "dst": dst}, f"Movido a {dst}.", False)
