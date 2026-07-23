"""Drive the user's preferred terminal app to run a shell command in a real,
watchable window/tab.

Difference from ``tools/shell_tool.run_shell_task``:
  - ``run_shell_task`` runs headless in the background (the user walks away).
  - This opens a real terminal window he can watch.

iTerm + Terminal have native AppleScript; others fall back to Terminal.
Source: iTerm2 scripting docs (create window with default profile + write text);
Terminal `do script`.
"""

from __future__ import annotations

from actions import macos
from core.apps import resolve
from tools.base import ToolResult, tool


def _script_iterm(command: str, cwd: str | None) -> str:
    cd = f"cd {macos.esc_applescript(cwd)} && " if cwd else ""
    cmd = macos.esc_applescript(cd + command)
    return (
        'tell application "iTerm"\n'
        "    set w to (create window with default profile)\n"
        "    tell current session of w\n"
        f'        write text "{cmd}"\n'
        "    end tell\n"
        "end tell"
    )


def _script_terminal(command: str, cwd: str | None) -> str:
    cd = f"cd {macos.esc_applescript(cwd)} && " if cwd else ""
    cmd = macos.esc_applescript(cd + command)
    return f'tell application "Terminal" to do script "{cmd}"'


def _build_script(app: str, command: str, cwd: str | None) -> str:
    return _script_iterm(command, cwd) if app == "iTerm" else _script_terminal(command, cwd)


@tool(destructive=True)
async def run_in_terminal(command: str, cwd: str = "", confirmed: bool = False) -> ToolResult:
    """Abre una terminal real y corre `command` (the user lo ve ejecutarse).

    Úsalo cuando diga:
    - "Emma, corre <comando> en mi terminal"
    - "Emma, abre una terminal en <path> y corre <comando>"
    """
    app = resolve("terminal") or "Terminal"
    if not confirmed:
        where = f" en {cwd}" if cwd else ""
        return ToolResult(
            True,
            {"app": app, "command": command, "cwd": cwd},
            f"¿Corro `{command[:80]}`{where} en {app}?",
            requires_confirmation=True,
        )
    script = _build_script(app, command, cwd or None)
    ok, _ = await macos.osascript_or_friendly(
        script, timeout_s=8.0, on_error="No pude lanzar el comando"
    )
    if not ok:
        return ToolResult(False, None, "No pude lanzar el comando.", False)
    return ToolResult(True, {"app": app, "command": command}, f"Corriendo en {app}.", False)
