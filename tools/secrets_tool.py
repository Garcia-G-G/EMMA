"""Voice-callable secret management (the Secret trust tier).

Values live only in macOS Keychain via :mod:`core.secrets`. They are never
logged (only labels), never written to ``memory.db``, and never placed in
``data`` payloads. ``recall_secret`` is the one path that speaks a value
aloud — by necessity it reaches the Realtime model, so it is gated to
"use only when alone."
"""

from __future__ import annotations

import structlog

from core import secrets
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.secrets")


@tool(destructive=True)
async def remember_secret(label: str, value: str, confirmed: bool = False) -> ToolResult:
    """Guarda un secreto (contraseña, número de cuenta, ID) bajo `label`.

    El valor se guarda cifrado en el Llavero de macOS, nunca en texto plano.
    Pide confirmación antes de guardar.
    """
    if not confirmed:
        return ToolResult(True, {"label": label}, f"¿Guardo el secreto bajo '{label}'?", True)
    try:
        await secrets.store(label, value, kind="user_secret")
    except Exception as exc:
        return ToolResult(False, None, f"No pude guardar el secreto: {exc}", False)
    log.info("secret_tool_stored", label=label)  # value never logged
    return ToolResult(True, {"label": label}, f"Guardado de forma segura bajo '{label}'.", False)


@tool()
async def recall_secret(label: str) -> ToolResult:
    """Lee un secreto guardado y lo dice en voz alta. Úsalo solo cuando estés a solas.

    El valor se devuelve solo en el mensaje hablado, nunca en datos ni en logs.
    """
    value = await secrets.retrieve(label)
    log.info("secret_tool_recall", label_read=label)  # never log the value
    if value is None:
        return ToolResult(True, {"label": label, "found": False}, f"No tengo nada bajo '{label}'.", False)
    # value goes only in user_message (to be spoken); never in `data`.
    return ToolResult(True, {"label": label, "found": True}, value, False)


@tool(destructive=True)
async def forget_secret(label: str, confirmed: bool = False) -> ToolResult:
    """Borra permanentemente un secreto guardado. Pide confirmación."""
    if not confirmed:
        return ToolResult(True, {"label": label}, f"¿Borro permanentemente el secreto '{label}'?", True)
    ok = await secrets.delete(label)
    return ToolResult(ok, {"label": label}, "Borrado." if ok else f"No encontré '{label}'.", False)


@tool()
async def list_secrets() -> ToolResult:
    """Lista las etiquetas de los secretos guardados. Nunca devuelve los valores."""
    labels = await secrets.list_labels()
    if not labels:
        return ToolResult(True, {"labels": []}, "No tienes secretos guardados.", False)
    return ToolResult(
        True, {"labels": labels}, f"Tienes {len(labels)} secretos: {', '.join(labels)}.", False
    )
