"""Prompt 32 — workflow DAG resolution + failure cascade (dispatcher mocked)."""

from __future__ import annotations

import pytest

from core import workflow as wf
from tools.base import ToolResult


def _step(tool, deps=None, on_failure="abort", desc=""):
    return wf.Step(tool=tool, args={"x": tool}, depends_on=deps or [], on_failure=on_failure, desc=desc)


# ---- topological order ------------------------------------------------------


def test_topo_linear() -> None:
    steps = [_step("a"), _step("b", [0]), _step("c", [1])]
    assert wf.topo_order(steps) == [0, 1, 2]


def test_topo_diamond_respects_deps() -> None:
    # a → b,c → d
    steps = [_step("a"), _step("b", [0]), _step("c", [0]), _step("d", [1, 2])]
    order = wf.topo_order(steps)
    assert order[0] == 0 and order[-1] == 3
    assert order.index(1) < order.index(3) and order.index(2) < order.index(3)


def test_topo_cycle_raises() -> None:
    steps = [_step("a", [1]), _step("b", [0])]
    with pytest.raises(wf.WorkflowError):
        wf.topo_order(steps)


def test_topo_bad_dependency_index_raises() -> None:
    with pytest.raises(wf.WorkflowError):
        wf.topo_order([_step("a", [5])])


# ---- run --------------------------------------------------------------------


def _dispatcher(fail_tools=()):
    async def dispatch(tool, args):
        if tool in fail_tools:
            return ToolResult(False, None, f"{tool} falló", False)
        return ToolResult(True, {"tool": tool}, f"{tool} ok", False)

    return dispatch


@pytest.mark.asyncio
async def test_run_all_succeed_in_order() -> None:
    steps = [_step("a"), _step("b", [0]), _step("c", [1])]
    seen = []

    async def dispatch(tool, args):
        seen.append(tool)
        return ToolResult(True, None, f"{tool} ok", False)

    run = await wf.run(steps, dispatch)
    assert run.ok and seen == ["a", "b", "c"]
    assert all(r.success for r in run.results)


@pytest.mark.asyncio
async def test_failure_aborts_downstream_by_default() -> None:
    # b fails (default on_failure=abort) → c is skipped, whole run not ok
    steps = [_step("a"), _step("b", [0]), _step("c", [1])]
    run = await wf.run(steps, _dispatcher(fail_tools=("b",)))
    assert not run.ok
    assert run.results[0].success
    assert not run.results[1].success and not run.results[1].skipped
    assert run.results[2].skipped  # downstream cancelled


@pytest.mark.asyncio
async def test_on_failure_continue_runs_independent_steps() -> None:
    # b fails with continue → its dependent d is skipped, independent c still runs
    steps = [_step("a"), _step("b", [0], on_failure="continue"), _step("c", [0]), _step("d", [1])]
    run = await wf.run(steps, _dispatcher(fail_tools=("b",)))
    assert run.results[2].success      # independent branch ran
    assert run.results[3].skipped      # dependent of failed b skipped
    assert not run.ok


@pytest.mark.asyncio
async def test_plan_bullets_uses_desc() -> None:
    steps = [_step("create_note", desc="Crear nota «pruebas»"), _step("add_reminder", [0])]
    bullets = wf.plan_bullets(steps)
    assert "Crear nota «pruebas»" in bullets
    assert bullets.count("\n") >= 1  # one line per step
