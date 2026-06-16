"""Conditional-trigger tools (Prompt 32): "si X pasa, haz Y" + the pending list.

``schedule_conditional`` confirms the trigger semantics back to Garcia before
saving (so a misheard sender/time can't silently arm). The background watcher
(``core/conditionals.watch``) fires the action once when the trigger matches.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from core import conditionals as cond
from tools.base import ToolResult, tool
from tools.registry import get_tool

log = structlog.get_logger("emma.tools.conditional")

_DEFAULT_TTL_DAYS = 14


def _resolve_expiry(raw: str) -> str:
    """ISO expiry string. Empty → default TTL; accepts ISO or a light natural form."""
    raw = (raw or "").strip()
    if not raw:
        return (dt.datetime.now() + dt.timedelta(days=_DEFAULT_TTL_DAYS)).isoformat()
    try:
        return dt.datetime.fromisoformat(raw).isoformat()
    except ValueError:
        try:
            return cond._parse_when(raw).isoformat()
        except ValueError:
            return (dt.datetime.now() + dt.timedelta(days=_DEFAULT_TTL_DAYS)).isoformat()


@tool(destructive=True)
async def schedule_conditional(
    trigger: str, action_tool: str, action_args: dict[str, Any], expires_at: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Programa una acción condicional ("si Ana confirma, agenda café").

    `trigger` usa el DSL: email_from("a@x.com", contains="ok") / calendar_event("X")
    created / time_at("2026-06-17T09:00:00"). `action_tool`+`action_args` es la
    herramienta a ejecutar al cumplirse. Confirma antes de guardar.
    """
    try:
        cond.parse_trigger(trigger)
    except ValueError as exc:
        return ToolResult(False, None, f"No entendí la condición: {exc}", False)
    if get_tool(action_tool) is None:
        return ToolResult(False, None, f"No conozco la acción «{action_tool}».", False)

    phrase = cond.describe_trigger(trigger)
    if not confirmed:
        return ToolResult(
            True, {"trigger": trigger},
            f"Entonces {phrase}, ejecuto «{action_tool}». ¿Lo guardo?",
            requires_confirmation=True,
        )

    expiry = _resolve_expiry(expires_at)
    cid = cond.add(trigger, action_tool, dict(action_args or {}), expiry)
    return ToolResult(
        True, {"id": cid},
        f"Listo, queda pendiente: {phrase} ejecuto «{action_tool}». Te aviso cuando pase.",
        False,
    )


@tool()
async def list_conditionals() -> ToolResult:
    """Dice qué acciones condicionales siguen pendientes ("¿qué tienes pendiente?")."""
    rows: list[Any] = cond.active()
    if not rows:
        return ToolResult(True, {"pending": []}, "No tienes nada condicional pendiente.", False)
    items = [f"{cond.describe_trigger(r['trigger_dsl'])} → {r['action_tool']}" for r in rows]
    spoken = "Tienes pendiente: " + "; ".join(items) + "."
    return ToolResult(True, {"pending": items, "count": len(items)}, spoken, False)
