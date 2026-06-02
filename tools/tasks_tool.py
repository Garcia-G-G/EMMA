"""Voice-callable inspection and control of Emma's background tasks."""

from __future__ import annotations

from core.background import TaskRecord, registry
from tools.base import ToolResult, tool


def _format(rec: TaskRecord) -> str:
    elapsed = int((rec.ended_at or rec.started_at) - rec.started_at)
    return f"{rec.name} · {rec.kind} · {rec.status} · {elapsed}s"


@tool()
async def list_my_tasks(status: str = "") -> ToolResult:
    """List Emma's background tasks. Filter by status (running/completed/failed/cancelled/aborted)."""
    items = registry().list(status=status or None, limit=15)  # type: ignore[arg-type]
    if not items:
        return ToolResult(True, {"tasks": []}, "No tengo tareas anotadas.", False)
    lines = [_format(r) for r in items]
    return ToolResult(True, {"tasks": [r.__dict__ for r in items]}, "\n".join(lines), False)


@tool()
async def task_status(name_or_id: str) -> ToolResult:
    """Report status of a specific background task by name or id."""
    rec = registry().get(name_or_id)
    if rec is None:
        return ToolResult(False, None, f"No conozco la tarea '{name_or_id}'.", False)
    return ToolResult(True, rec.__dict__, _format(rec), False)


@tool(destructive=True)
async def cancel_my_task(name_or_id: str, confirmed: bool = False) -> ToolResult:
    """Cancel a running background task."""
    rec = registry().get(name_or_id)
    if rec is None:
        return ToolResult(False, None, f"No conozco la tarea '{name_or_id}'.", False)
    if rec.status != "running":
        return ToolResult(False, None, f"'{rec.name}' ya está en estado {rec.status}.", False)
    if not confirmed:
        return ToolResult(
            True, {"name": rec.name}, f"¿Cancelo '{rec.name}'?", requires_confirmation=True
        )
    ok = await registry().cancel(rec.id)
    return ToolResult(ok, {"name": rec.name}, "Cancelada." if ok else "No pude cancelarla.", False)


@tool()
async def wait_for_my_task(name_or_id: str, timeout_s: int = 30) -> ToolResult:
    """Block until a background task finishes (or timeout). Useful inside a tool chain."""
    rec = await registry().wait(name_or_id, timeout_s=timeout_s)
    if rec is None:
        return ToolResult(False, None, f"No conozco la tarea '{name_or_id}'.", False)
    return ToolResult(True, rec.__dict__, _format(rec), False)
