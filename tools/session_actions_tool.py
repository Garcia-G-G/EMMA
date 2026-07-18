"""Anaphora support: what did Emma just do? (21-B29)

"Otra vez", "como antes", "lo de hace rato" have no referent unless Emma can
look at her own recent actions. ``core/session_memory`` records every
successful tool call (sanitized, 30-min TTL, never persisted); this tool
surfaces the tail so the LLM can confirm the referent and re-call the
original tool with the same args.
"""

from __future__ import annotations

from core import session_memory
from tools.base import ToolResult, tool


def _describe(action: dict[str, object]) -> str:
    name = str(action.get("name", "algo"))
    args = action.get("args") or {}
    if isinstance(args, dict) and args:
        hint = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3] if k != "confirmed")
        return f"{name} ({hint})"
    return name


@tool()
async def recall_last_action() -> ToolResult:
    """Devuelve la última acción que Emma completó en esta sesión.

    Úsalo PRIMERO cuando Garcia diga 'otra vez', 'como antes', 'lo de hace
    rato', 'eso', 'lo mismo', 'deshazlo' — confirma con él el referente y
    luego re-llama la herramienta original con los mismos argumentos.
    """
    actions = session_memory.recent_completed_actions(within_s=session_memory.ACTION_TTL_S)
    if not actions:
        return ToolResult(
            True,
            {"last_action": None, "recent": []},
            "No he hecho nada en los últimos minutos; ¿a qué te refieres?",
            False,
        )
    last = actions[-1]
    recent = actions[-5:]
    return ToolResult(
        True,
        {"last_action": last, "recent": recent},
        f"Lo último que hice fue {_describe(last)}.",
        False,
    )


@tool()
async def repeat_last() -> ToolResult:
    """Repite, TAL CUAL, lo último que Emma dijo (sin volver a generarlo).

    Para "repite eso", "¿qué dijiste?", "otra vez pero repítelo", "no te
    escuché, repite", "say that again". Habla de nuevo la MISMA respuesta,
    palabra por palabra — no la reformules ni la regeneres.
    """
    text = session_memory.last_assistant_speech_text()
    if not text:
        return ToolResult(
            True, {"repeated": None}, "Todavía no he dicho nada que pueda repetir.", False
        )
    # Return the exact text as the spoken message so Emma says it verbatim.
    return ToolResult(True, {"repeated": text}, text, False)
