"""Run an arbitrary shell command as a background task."""

from __future__ import annotations

import asyncio
import contextlib
import os

from core.background import MAX_PARALLEL_TASKS, registry
from tools.base import ToolResult, tool


@tool(destructive=True)
async def run_shell_task(
    command: str,
    name: str,
    timeout_s: int = 600,
    confirmed: bool = False,
) -> ToolResult:
    """Start an arbitrary shell command as a background task.

    Use when Garcia wants something to keep running while he keeps doing other
    things: "Emma, corre estos tests y avísame", "Emma, descarga este repo".

    The command runs via `/bin/zsh -lc <command>` so PATH and shell aliases are
    available. Output is captured (last 8KB visible via task_status).
    Cancellation kills the process group.
    """
    if not confirmed:
        return ToolResult(
            True,
            {"command": command, "name": name},
            f"¿Corro '{command}' como tarea '{name}' en segundo plano?",
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

    async def runner(ctrl):
        proc = await asyncio.create_subprocess_exec(
            "/bin/zsh",
            "-lc",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_s)
                if not line:
                    break
                ctrl.append_output(line.decode("utf-8", errors="replace"))
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 15)
            raise
        finally:
            rc = await proc.wait()
        return rc

    rec = await reg.start(name=name, kind="shell", coro_factory=runner, meta={"command": command})
    return ToolResult(
        True,
        {"id": rec.id, "name": name},
        f"Listo, '{name}' corriendo en segundo plano. Te aviso cuando termine.",
        False,
    )
