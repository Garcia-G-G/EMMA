"""run_workflow tool — the confirmation gate + destructive auto-confirm injection.

The engine (core/workflow.py) is covered by test_workflow.py; this covers the TOOL
wrapper, whose `confirmed=True` injection into every destructive step is the single
most dangerous line in the tool layer and was previously untested.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

import tools.workflow_tool as wt
from tools.base import ToolResult


@dataclass
class _FakeTool:
    destructive: bool


@pytest.fixture
def registry(monkeypatch):
    """A fake tool registry: 'send' is destructive, 'read' is not."""
    tools = {"send": _FakeTool(True), "read": _FakeTool(False)}
    monkeypatch.setattr(wt, "get_tool", lambda name: tools.get(name))
    calls: list[tuple[str, dict]] = []

    async def _dispatch(name, args):
        calls.append((name, dict(args)))
        return ToolResult(True, None, "ok", False)

    monkeypatch.setattr(wt, "dispatch", AsyncMock(side_effect=_dispatch))
    return calls


@pytest.mark.asyncio
async def test_empty_steps_is_friendly(registry):
    res = await wt.run_workflow([])
    assert not res.success and "pasos" in res.user_message
    assert registry == []  # nothing dispatched


@pytest.mark.asyncio
async def test_unconfirmed_returns_plan_and_runs_nothing(registry):
    res = await wt.run_workflow([{"tool": "read", "desc": "leer"}], confirmed=False)
    assert res.requires_confirmation is True
    assert "¿Lo confirmo?" in res.user_message
    assert registry == []  # MUST NOT execute before confirmation


@pytest.mark.asyncio
async def test_unknown_tool_rejected_before_execution(registry):
    res = await wt.run_workflow(
        [{"tool": "read"}, {"tool": "nope"}], confirmed=True)
    assert not res.success and "nope" in res.user_message
    assert registry == []  # unknown step aborts the whole flow, nothing runs


@pytest.mark.asyncio
async def test_confirm_injects_only_into_destructive_steps(registry):
    res = await wt.run_workflow(
        [{"tool": "read", "args": {"q": "x"}},
         {"tool": "send", "args": {"to": "a"}}],
        confirmed=True,
    )
    assert res.success
    by_tool = {name: args for name, args in registry}
    # destructive step got confirmed=True injected...
    assert by_tool["send"]["confirmed"] is True
    # ...the non-destructive one was left untouched
    assert "confirmed" not in by_tool["read"]


@pytest.mark.asyncio
async def test_invalid_dag_rejected(registry):
    # depends_on points at a non-existent step → invalid, nothing runs
    res = await wt.run_workflow(
        [{"tool": "read", "depends_on": [5]}], confirmed=True)
    assert not res.success and "no es válido" in res.user_message
    assert registry == []
