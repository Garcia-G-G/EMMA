"""Multi-step workflow tool (Prompt 32).

When Garcia asks for several things in one breath ("haz X y Y y agrega Z"), Emma
builds a list of steps and calls ``run_workflow``. The first call returns the plan
as a bullet list for a single confirmation; on "sí" the orchestrator replays with
``confirmed=True`` and the steps execute in dependency order.

The whole plan is confirmed ONCE up front — destructive steps are auto-confirmed
inside the run (their ``confirmed=True`` is injected here), so Emma never asks
per-step.
"""

from __future__ import annotations

from typing import Any

import structlog

from core import workflow
from tools.base import ToolResult, tool
from tools.registry import dispatch, get_tool

log = structlog.get_logger("emma.tools.workflow")


def _parse_steps(raw: list[dict[str, Any]]) -> list[workflow.Step]:
    steps: list[workflow.Step] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not s.get("tool"):
            raise ValueError(f"paso {i} sin 'tool'")
        on_fail = s.get("on_failure", "abort")
        if on_fail not in ("abort", "continue"):
            on_fail = "abort"
        steps.append(workflow.Step(
            tool=str(s["tool"]),
            args=dict(s.get("args") or {}),
            depends_on=[int(d) for d in (s.get("depends_on") or [])],
            on_failure=on_fail,
            desc=str(s.get("desc") or ""),
        ))
    return steps


@tool(destructive=True)
async def run_workflow(steps: list[dict[str, Any]], confirmed: bool = False) -> ToolResult:
    """Encadena varios pasos en un solo flujo ("haz X, luego Y, y agrega Z").

    `steps` es una lista; cada paso es {tool, args, depends_on?(índices), on_failure?
    ("abort"|"continue"), desc?(frase en español)}. Muestra el plan y pide UNA
    confirmación; al confirmar, corre los pasos en orden de dependencia.
    """
    if not steps:
        return ToolResult(False, None, "No me diste pasos para el flujo.", False)
    try:
        parsed = _parse_steps(steps)
        workflow.topo_order(parsed)  # validate DAG early (cycle / bad index)
    except (ValueError, workflow.WorkflowError) as exc:
        return ToolResult(False, None, f"Ese flujo no es válido: {exc}", False)

    unknown = [s.tool for s in parsed if get_tool(s.tool) is None]
    if unknown:
        return ToolResult(False, None, f"No conozco estas acciones: {', '.join(unknown)}.", False)

    if not confirmed:
        plan = workflow.plan_bullets(parsed)
        return ToolResult(
            True, {"steps": len(parsed)},
            f"Voy a hacer esto, en orden:\n{plan}\n¿Lo confirmo?",
            requires_confirmation=True,
        )

    # Confirmed: auto-confirm destructive steps so we never re-ask per step.
    for s in parsed:
        entry = get_tool(s.tool)
        if entry is not None and entry.destructive:
            s.args = {**s.args, "confirmed": True}

    run = await workflow.run(parsed, dispatch)
    done = sum(1 for r in run.results if r.success)
    failed = [r for r in run.results if not r.success and not r.skipped]
    skipped = [r for r in run.results if r.skipped]
    parts = [f"Listo: {done} de {len(parsed)} pasos."]
    if failed:
        parts.append("Falló: " + "; ".join(f"{r.tool} ({r.message})" for r in failed) + ".")
    if skipped:
        parts.append(f"Omití {len(skipped)} por la falla.")
    return ToolResult(run.ok, {"results": [r.__dict__ for r in run.results]}, " ".join(parts), False)
