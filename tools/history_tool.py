"""Action-history query + voice-driven undo (Prompt 28).

Reads the durable ``memory.episodic`` action log: "¿qué hiciste el martes?" and
"deshaz lo último". Undo is bounded — it reverses only actions that captured a
reverse blueprint (inverse_call / restore_text); noop and manual actions are
explained honestly in Spanish.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from memory import episodic
from tools.base import ToolResult, tool
from tools.registry import dispatch

log = structlog.get_logger("emma.tools.history")

_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}
_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_when(when: str) -> date | None:
    """Parse a Spanish day phrase to a date. None if unparseable. "" = today."""
    w = (when or "").strip().lower()
    today = date.today()
    if not w or "hoy" in w or w == "today" or w.startswith("esta "):  # hoy / esta mañana|tarde|noche
        return today
    if w in ("ayer", "yesterday"):
        return today - timedelta(days=1)
    if w in ("anteayer", "antier"):
        return today - timedelta(days=2)
    for name, idx in _WEEKDAYS.items():
        if name in w:
            delta = (today.weekday() - idx) % 7 or 7  # most recent PAST occurrence
            return today - timedelta(days=delta)
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", w)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    dm = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)", w)
    if dm and dm[2] in _MONTHS:
        try:
            cand = date(today.year, _MONTHS[dm[2]], int(dm[1]))
            return cand if cand <= today else date(today.year - 1, _MONTHS[dm[2]], int(dm[1]))
        except ValueError:
            return None
    return None


def _action_brief(r: episodic.ActionRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "tool": r.tool_name,
        "when": datetime.fromtimestamp(r.ts).strftime("%H:%M"),
        "said": r.user_speech,
        "reversible": r.reverse_kind in ("inverse_call", "restore_text") and r.reversed_at is None,
        "reverse_kind": r.reverse_kind,
    }


def _summarize_actions(actions: list[episodic.ActionRecord], when: str) -> str:
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.tool_name] = counts.get(a.tool_name, 0) + 1
    label = when.strip() or "hoy"
    parts = [f"{tool} (x{n})" if n > 1 else tool for tool, n in counts.items()]
    return f"{label.capitalize()}: hice {len(actions)} acción(es) — " + ", ".join(parts) + "."


@tool()
async def what_did_you_do(when: str = "") -> ToolResult:
    """Cuenta qué acciones hizo Emma en un día ("¿qué hiciste ayer / el martes / hoy?").

    `when` es una frase en español: "ayer", "el martes", "el 8 de junio", "hoy"
    (o vacío = hoy). Solo lectura.
    """
    d = _parse_when(when)
    if d is None:
        return ToolResult(False, None, f"No entendí qué día es «{when}». Dime 'ayer', 'el martes' o una fecha.", False)
    actions = await episodic.query_by_date(d, limit=30)
    if not actions:
        label = "hoy" if d == date.today() else "ese día"
        return ToolResult(True, {"date": d.isoformat(), "actions": []}, f"No registré acciones {label}.", False)
    return ToolResult(
        True,
        {"date": d.isoformat(), "actions": [_action_brief(a) for a in actions]},
        _summarize_actions(actions, when),
        False,
    )


def _describe(rec: episodic.ActionRecord) -> str:
    if rec.user_speech:
        return f"Lo último que hice fue cuando dijiste «{rec.user_speech}»."
    target = rec.args.get("title") or rec.args.get("path") or ""
    return f"Lo último que hice fue {rec.tool_name}" + (f" «{target}»" if target else "") + "."


async def _apply_reverse(rec: episodic.ActionRecord) -> tuple[bool, str]:
    rev = rec.reverse or {}
    kind = rev.get("kind")
    if kind == "inverse_call":
        # Garcia confirmed at the undo level → bypass the inverse tool's own gate.
        res = await dispatch(rev["tool"], {**rev.get("args", {}), "confirmed": True})
        return (res.success, "Listo, lo deshice." if res.success else f"No pude deshacerlo: {res.user_message}")
    if kind == "restore_text":
        from tools.file_edit import _atomic_write

        try:
            await asyncio.to_thread(_atomic_write, Path(rev["path"]), rev.get("before", ""))
            return (True, "Listo, restauré el archivo a como estaba antes.")
        except Exception as exc:
            return (False, f"No pude restaurar el archivo: {exc}")
    return (False, "No sé cómo deshacer ese tipo de acción.")


async def _do_undo(rec: episodic.ActionRecord, confirmed: bool) -> ToolResult:
    kind = rec.reverse_kind
    if kind == "noop":
        return ToolResult(False, {"reverse_kind": "noop"}, "No puedo deshacer ese tipo de acción (ya quedó hecha).", False)
    if kind == "manual":
        hint = (rec.reverse or {}).get("hint", "")
        return ToolResult(False, {"reverse_kind": "manual"}, f"No puedo deshacer eso automáticamente. {hint}".strip(), False)
    if not confirmed:
        return ToolResult(True, {"action_id": rec.id, "tool": rec.tool_name}, f"{_describe(rec)} ¿Lo deshago?", requires_confirmation=True)
    ok, msg = await _apply_reverse(rec)
    if ok:
        await episodic.mark_reversed(rec.id)
        return ToolResult(True, {"reversed": rec.id}, msg, False)
    return ToolResult(False, {"action_id": rec.id}, msg, False)


@tool(destructive=True)
async def undo_last_action(confirmed: bool = False) -> ToolResult:
    """Deshace la última acción reversible de Emma. SIEMPRE confirma antes.

    Para "deshaz lo último", "echa para atrás", "deshazlo". Si la acción no se
    puede deshacer (un mensaje enviado, un tweet), lo dice honestamente.
    """
    rec = await episodic.last_undoable()
    if rec is None:
        return ToolResult(False, None, "No tengo nada reciente que pueda deshacer.", False)
    return await _do_undo(rec, confirmed)


@tool(destructive=True)
async def undo_action_by_id(action_id: int, confirmed: bool = False) -> ToolResult:
    """Deshace una acción específica del historial (por su id). SIEMPRE confirma.

    Úsalo después de what_did_you_do, cuando Garcia diga "deshaz la de las 3pm".
    """
    rec = await episodic.get(action_id)
    if rec is None:
        return ToolResult(False, None, "No encontré esa acción en el historial.", False)
    if rec.reversed_at is not None:
        return ToolResult(False, None, "Esa acción ya la deshice.", False)
    return await _do_undo(rec, confirmed)
