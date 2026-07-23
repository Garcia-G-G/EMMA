"""IDE control actions.

Layered fallback per call (first that works wins):
  1. CLI binary in PATH — open at a line natively:
       VS Code / Cursor:  ``<cli> -g <file>:<line>``  (--goto)
       Zed:               ``<cli> <file>:<line>``      (no -g flag)
  2. ``open -a "<App>" <path>`` (no line jump).
  3. (search only) AppleScript via System Events.

the user's preferred IDE comes from :func:`core.apps.resolve("editor")`.

Sources: VS Code CLI `--goto` (code.visualstudio.com/docs/configure/command-line),
Zed CLI `file:line` (zed.dev/docs/reference/cli).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from actions import macos
from core.apps import resolve
from tools.app_control import app_keystroke
from tools.base import ToolResult, tool

# Per-IDE "toggle integrated terminal" shortcut (Bug 19.2-B6).
# VS Code / Cursor: Ctrl+` (code.visualstudio.com/docs/terminal/basics).
# Zed: Cmd+J (zed.dev/docs).
_TERMINAL_SHORTCUT = {
    "VS Code": "Ctrl+`",
    "Visual Studio Code": "Ctrl+`",
    "Cursor": "Ctrl+`",
    "Zed": "Cmd+J",
}

# Display name → CLI binary. Includes both VS Code display variants.
_CLI_FOR_APP = {
    "VS Code": "code",
    "Visual Studio Code": "code",
    "Cursor": "cursor",
    "Zed": "zed",
}


def _cli_for(app_name: str) -> str | None:
    binary = _CLI_FOR_APP.get(app_name)
    return shutil.which(binary) if binary else None


def _open_args(cli: str | None, app: str, path: str, line: int) -> list[str]:
    """Build the argv for opening ``path`` (optionally at ``line``)."""
    if cli and line > 0:
        # VS Code/Cursor use -g file:line; Zed takes file:line directly.
        if cli.endswith("zed"):
            return [cli, f"{path}:{line}"]
        return [cli, "-g", f"{path}:{line}"]
    if cli:
        return [cli, path]
    return ["open", "-a", app, path]


@tool()
async def open_in_ide(
    path: str, line: int = 0, ide: str = "", project_mode: bool = False
) -> ToolResult:
    """Abre un archivo o carpeta en el IDE preferido de the user.

    Úsalo cuando diga:
    - "Emma, abre <path> en mi IDE"
    - "Emma, abre <path> en la línea N"
    - "Emma, ábreme esto en Cursor"

    `project_mode=True` abre `path` como CARPETA de proyecto (el árbol en la
    barra lateral) en vez de como archivo — Cursor/VS Code/Zed aceptan un
    directorio en su CLI (23.1-B43).
    """
    app = ide or resolve("editor")
    if not app:
        return ToolResult(False, None, "No tengo un IDE configurado.", False)
    p = Path(path).expanduser()
    if not p.exists():
        return ToolResult(False, None, f"No encontré {p}.", False)

    cli = _cli_for(app)
    # A project (directory) open never carries a line jump.
    args = _open_args(cli, app, str(p), 0 if project_mode else line)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=8.0)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir en {app}: {exc}", False)

    if project_mode:
        spoken = f"Listo, abriendo el proyecto en {app}."
    else:
        at_line = f" en la línea {line}" if line > 0 else ""
        spoken = f"Listo, abriendo en {app}{at_line}."
    return ToolResult(
        True,
        {
            "app": app,
            "path": str(p),
            "line": 0 if project_mode else line,
            "project_mode": project_mode,
            "via": "cli" if cli else "open",
        },
        spoken,
        False,
    )


@tool()
async def new_file_in_ide(path: str, content: str = "", ide: str = "") -> ToolResult:
    """Crea un archivo nuevo (con contenido opcional) y lo abre en el IDE."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return await open_in_ide(str(p), ide=ide)


@tool()
async def toggle_ide_terminal(ide: str = "") -> ToolResult:
    """Abre/cierra la terminal integrada del IDE (Bug 19.2-B6).

    Úsalo cuando the user diga 'abre la terminal', 'muéstrame la terminal',
    'abre una terminal en Cursor'."""
    app = ide or resolve("editor")
    if not app:
        return ToolResult(False, None, "No tengo un IDE configurado.", False)
    keys = _TERMINAL_SHORTCUT.get(app)
    if not keys:
        return ToolResult(False, None, f"No sé cómo abrir la terminal en {app}.", False)
    # UI toggle, not a data write → auto-confirm the (destructive-flagged) keystroke.
    res = await app_keystroke(app, keys, confirmed=True)
    if not res.success:
        return ToolResult(False, None, f"No pude abrir la terminal en {app}.", False)
    return ToolResult(True, {"app": app, "keys": keys}, f"Listo, terminal de {app}.", False)


def _terminal_paste_script(app: str, text: str, enter: bool) -> str:
    """Activate the IDE, clipboard-paste ``text`` into its terminal, optional Return."""
    q = macos.esc_applescript(text)
    a = macos.esc_applescript(app)
    lines = [
        f'tell application "{a}" to activate',
        "delay 0.2",
        f'set the clipboard to "{q}"',
        'tell application "System Events"',
        '    keystroke "v" using {command down}',
    ]
    if enter:
        lines.append("    keystroke return")
    lines.append("end tell")
    return "\n".join(lines)


@tool()
async def ide_terminal_send(text: str, enter: bool = True, ide: str = "") -> ToolResult:
    """Escribe `text` en la terminal integrada del IDE y opcionalmente da Enter.

    Úsalo cuando the user diga "Emma, en la terminal de Cursor corre 'npm test'"
    o "escribe X en la terminal". Abre/enfoca la terminal primero (Bug 19.6-B18).

    Limitación conocida: TUIs interactivas estilo Ink (p. ej. los prompts de
    Claude Code) tratan el salto de línea programático como newline, NO como
    submit — solo la tecla Return física dispara el envío en esos prompts
    (github.com/anthropics/claude-code/issues/15553, Approach 7). Si el
    destino es una de esas, usa enter=false y que the user presione Enter.
    Nota: si la terminal ya estaba abierta Y enfocada, el toggle puede
    cerrarla (sin señal fiable de visibilidad entre IDEs); re-pedirlo la
    reabre.
    """
    app = ide or resolve("editor")
    if not app:
        return ToolResult(False, None, "No tengo un IDE configurado.", False)
    if not (text or "").strip():
        return ToolResult(False, None, "¿Qué escribo en la terminal?", False)

    opened = await toggle_ide_terminal(ide=app)
    if not opened.success:
        return opened
    await asyncio.sleep(0.3)  # let the panel grab focus before pasting

    ok, _ = await macos.osascript_or_friendly(
        _terminal_paste_script(app, text, enter),
        timeout_s=5.0,
        on_error="No pude escribir en la terminal",
    )
    if not ok:
        return ToolResult(False, None, f"No pude escribir en la terminal de {app}.", False)
    action = "y lo ejecuté" if enter else "(sin Enter, tú lo lanzas)"
    return ToolResult(
        True,
        {"app": app, "text": text, "enter": enter},
        f"Listo, escribí el comando en la terminal de {app} {action}.",
        False,
    )


@tool()
async def search_in_ide(query: str, ide: str = "") -> ToolResult:
    """Lanza 'Buscar en archivos' (Cmd+Shift+F) en el IDE con `query`.

    Pre-rellena la búsqueda vía portapapeles + pegar."""
    app = ide or resolve("editor")
    if not app:
        return ToolResult(False, None, "No tengo un IDE configurado.", False)
    q = macos.esc_applescript(query)
    script = (
        f'tell application "{app}" to activate\n'
        "delay 0.3\n"
        f'set the clipboard to "{q}"\n'
        'tell application "System Events"\n'
        '    keystroke "f" using {command down, shift down}\n'
        "    delay 0.2\n"
        '    keystroke "v" using {command down}\n'
        "    keystroke return\n"
        "end tell"
    )
    ok, _ = await macos.osascript_or_friendly(
        script, timeout_s=5.0, on_error="No pude lanzar la búsqueda"
    )
    if not ok:
        return ToolResult(False, None, "No pude lanzar la búsqueda.", False)
    return ToolResult(True, {"app": app, "query": query}, f"Buscando '{query}' en {app}.", False)
