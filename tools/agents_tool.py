"""Delegate a task to Claude Code (one-shot, headless) and capture its work."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from core.background import MAX_PARALLEL_TASKS, registry
from tools.base import ToolResult, tool


def _claude_available() -> bool:
    return shutil.which("claude") is not None


@tool(destructive=True)
async def delegate_to_claude_code(
    task: str,
    cwd: str = "~/Documents/EMMA",
    branch: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Hand a coding task off to Claude Code (the CLI) and let it work.

    Use when Garcia says any of:
    - "Emma, reprograma X"
    - "Emma, arregla el bug de Y"
    - "Emma, refactoriza Z"
    - "Emma, agrega tests para W"

    The CLI runs as a background subprocess. Captures stdout/stderr.
    If `branch` is set, Emma creates a fresh git worktree on that branch
    (so the main checkout stays clean and Garcia can review the diff later).

    On completion Emma fires a macOS notification and updates the visualizer.
    """
    if not _claude_available():
        return ToolResult(False, None, "No tengo el CLI de Claude Code instalado.", False)

    repo = Path(os.path.expanduser(cwd)).resolve()
    if not (repo / ".git").exists():
        return ToolResult(
            False, None, f"{repo} no es un repo git, no puedo crear un worktree seguro.", False
        )

    if not confirmed:
        target = f"worktree en '{branch}'" if branch else "el repo activo"
        return ToolResult(
            True,
            {"task": task, "cwd": str(repo), "branch": branch},
            f"¿Le paso a Claude Code la tarea «{task[:90]}» sobre {target}?",
            requires_confirmation=True,
        )

    reg = registry()
    if reg.at_capacity():
        return ToolResult(
            False,
            None,
            f"Tengo {MAX_PARALLEL_TASKS} tareas corriendo ya; espera a que termine alguna.",
            False,
        )

    work_dir = repo
    if branch:
        wt_root = repo.parent / f"emma-wt-{branch.replace('/', '-')}"

        async def _setup() -> None:
            for cmd in (
                ["/usr/bin/git", "fetch", "--quiet"],
                ["/usr/bin/git", "worktree", "add", "-B", branch, str(wt_root), "HEAD"],
            ):
                p = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo))
                await p.wait()

        await _setup()
        work_dir = wt_root

    async def runner(ctrl: Any) -> int:
        argv = ["claude", "-p", task, "--cwd", str(work_dir)]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert proc.stdout is not None  # PIPE above guarantees it
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            ctrl.append_output(line.decode("utf-8", errors="replace"))
        return await proc.wait()

    rec = await reg.start(
        name=f"claude:{task[:24]}",
        kind="claude_code",
        coro_factory=runner,
        meta={"task": task, "cwd": str(work_dir), "branch": branch},
    )
    msg = "Listo, Claude Code está trabajando. Te aviso cuando termine."
    return ToolResult(True, {"id": rec.id}, msg, False)
