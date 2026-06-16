"""Multi-step workflow engine (Prompt 32).

A workflow is a DAG of :class:`Step` s. Each step names a tool, its args, and the
indices of the steps it depends on. ``run`` executes them in dependency
(topological) order, threading results through a caller-supplied ``dispatcher``
(in production, ``tools.registry.dispatch``).

Failure semantics — kept deliberately small:
  - A step that fails with ``on_failure="abort"`` (the default) stops the whole
    workflow: every remaining step is marked skipped.
  - A step that fails with ``on_failure="continue"`` only cancels its *dependents*
    (transitively, via the dependency check); independent branches keep running.
  - A step whose dependency failed or was skipped is itself skipped — failure
    cascades downstream.

The engine is pure: it knows nothing about the registry, confirmation, or the
LLM. The destructive-confirmation policy (confirm the whole plan once up front)
lives in ``tools/workflow_tool.py``, which injects ``confirmed=True`` into the
args of destructive steps before handing them here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

OnFailure = Literal["abort", "continue"]


class WorkflowError(ValueError):
    """Raised for an ill-formed DAG (cycle or out-of-range dependency)."""


# The dispatcher returns anything with ``.success`` / ``.user_message`` (a
# ToolResult in production); ``run`` reads them via getattr, so we stay untyped
# here and avoid importing the tools layer into this pure engine.
Dispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)
    on_failure: OnFailure = "abort"
    desc: str = ""  # human-facing line for the plan readback (optional)


@dataclass
class StepResult:
    index: int
    tool: str
    success: bool
    message: str
    skipped: bool = False


@dataclass
class WorkflowRun:
    results: list[StepResult]
    ok: bool


def topo_order(steps: list[Step]) -> list[int]:
    """Indices of ``steps`` in dependency order (Kahn's algorithm).

    Raises :class:`WorkflowError` on a cycle or a dependency index that doesn't
    point at a real step.
    """
    n = len(steps)
    indeg = [0] * n
    children: list[list[int]] = [[] for _ in range(n)]
    for i, step in enumerate(steps):
        for d in step.depends_on:
            if d < 0 or d >= n or d == i:
                raise WorkflowError(f"paso {i} depende de un índice inválido: {d}")
            indeg[i] += 1
            children[d].append(i)
    # Stable: process ready nodes in ascending index so equal-depth steps keep
    # the order the LLM listed them (matters for readback + side-effect ordering).
    ready = sorted(i for i in range(n) if indeg[i] == 0)
    order: list[int] = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for c in children[i]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort()
    if len(order) != n:
        raise WorkflowError("el workflow tiene un ciclo de dependencias")
    return order


def plan_bullets(steps: list[Step]) -> str:
    """A one-line-per-step bullet list for the spoken/print plan readback."""
    lines = []
    for s in steps:
        label = s.desc.strip() or f"{s.tool}({', '.join(f'{k}={v}' for k, v in s.args.items())})"
        lines.append(f"- {label}")
    return "\n".join(lines)


async def run(steps: list[Step], dispatcher: Dispatcher) -> WorkflowRun:
    """Execute ``steps`` in dependency order via ``dispatcher``. Never raises for a
    step failure — failures are captured in the per-step results."""
    order = topo_order(steps)
    results: list[StepResult | None] = [None] * len(steps)
    failed: set[int] = set()
    skipped: set[int] = set()
    aborted = False

    for i in order:
        step = steps[i]
        if aborted:
            skipped.add(i)
            results[i] = StepResult(i, step.tool, False, "omitido (workflow abortado)", skipped=True)
            continue
        if any(d in failed or d in skipped for d in step.depends_on):
            skipped.add(i)
            results[i] = StepResult(i, step.tool, False, "omitido (una dependencia falló)", skipped=True)
            continue
        res = await dispatcher(step.tool, step.args)
        ok = bool(getattr(res, "success", False))
        results[i] = StepResult(i, step.tool, ok, getattr(res, "user_message", ""))
        if not ok:
            failed.add(i)
            if step.on_failure == "abort":
                aborted = True

    ordered = [r for r in results if r is not None]
    return WorkflowRun(ordered, ok=not failed and not skipped)
