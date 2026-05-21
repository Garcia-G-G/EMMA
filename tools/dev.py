"""Developer-mode voice tools.

These are the deliberate escape hatches Garcia uses to take Emma offline
for live edits, and to ask Emma about her own state. Resuming from dev
mode is intentionally manual (the terminal Emma opens shows the exact
command) - there is no resume-by-voice tool.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

from actions import macos
from core import dev_state
from tools.base import ToolResult, tool
from tools.diagnostics import health_check

log = structlog.get_logger("emma.tools.dev")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _editor_command() -> str | None:
    for cmd in ("cursor", "code", "subl"):
        if shutil.which(cmd):
            return cmd
    return None


def _git(args: list[str]) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), *args],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return "<unknown>"


def _build_banner(branch: str, last_commit: str, restart_cmd: str) -> str:
    border = "+" + "-" * 58 + "+"
    return (
        f"\n{border}\n"
        f"|  EMMA DEV MODE\n"
        f"|\n"
        f"|  Repo:   {REPO_ROOT}\n"
        f"|  Branch: {branch}\n"
        f"|  Last:   {last_commit}\n"
        f"|\n"
        f"|  Resume Emma when you're done editing:\n"
        f"|    {restart_cmd}\n"
        f"{border}\n"
    )


@tool()
async def open_workspace_for_debugging() -> ToolResult:
    """Open Emma's source for live editing, then stop the running service.

    Use this when Garcia says any of these (and similar phrasings):

    - "Emma, te voy a debuggear"
    - "voy a hacerte reparaciones"
    - "ábreme tu código"
    - "abre tu workspace"
    - "dev mode"
    - "open your codebase"

    Opens a Terminal at the repo root with a banner showing the repo
    path, current branch, last commit, and the exact command to resume.
    Also opens the user's preferred editor (cursor > code > subl >
    Finder). Stops the launchd service so Emma is fully out of the way
    until manually resumed.
    """
    branch = _git(["branch", "--show-current"]) or "<detached>"
    last_commit = _git(["log", "-1", "--oneline"])
    uid = os.getuid()
    restart_cmd = (
        f"launchctl enable gui/{uid}/com.garcia.emma && "
        f"launchctl kickstart -k gui/{uid}/com.garcia.emma"
    )
    banner = _build_banner(branch, last_commit, restart_cmd)

    banner_path = Path(tempfile.mkstemp(prefix="emma_devmode_", suffix=".txt")[1])
    banner_path.write_text(banner)

    cd_cmd = f"cd {shlex.quote(str(REPO_ROOT))} && clear && cat {shlex.quote(str(banner_path))}"
    try:
        macos.run_applescript(
            f'tell application "Terminal" to activate\n'
            f'tell application "Terminal" to do script "{cd_cmd}"'
        )
    except macos.AppleScriptError as exc:
        log.error("dev_terminal_open_failed", error=str(exc))

    editor = _editor_command()
    if editor:
        try:
            subprocess.Popen(
                [editor, str(REPO_ROOT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.error("dev_editor_open_failed", editor=editor, error=str(exc))
    else:
        try:
            subprocess.run(["open", "-R", str(REPO_ROOT)], check=False, timeout=3)
        except Exception:
            pass

    # Disable the service so launchd respects our clean exit.
    try:
        subprocess.run(
            ["launchctl", "disable", f"gui/{uid}/com.garcia.emma"],
            check=False,
            timeout=5,
        )
    except Exception:
        pass

    log.info("dev_mode_requested", repo=str(REPO_ROOT), branch=branch, editor=editor)
    dev_state.shutdown_requested.set()

    return ToolResult(
        success=True,
        data={
            "repo": str(REPO_ROOT),
            "branch": branch,
            "restart_cmd": restart_cmd,
            "editor": editor,
        },
        user_message="Abriendo mi código. Me detengo hasta que reinicies el servicio.",
        requires_confirmation=False,
    )


@tool()
async def describe_my_health() -> ToolResult:
    """Run the health check and speak the result.

    Use when Garcia says:

    - "Emma, ¿cómo te sientes?"
    - "¿estás bien?"
    - "diagnóstico"
    - "health check"
    """
    return await health_check()
