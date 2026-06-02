"""Apple Messages via AppleScript: list recent threads, send iMessage (confirmed).

Reading message *content* requires Full Disk Access to chat.db and is
intentionally out of scope here (a later prompt). This module only lists
conversation metadata and sends new iMessages.
"""

from __future__ import annotations

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.messages")

_MSG_TIMEOUT_S = 12.0


@tool()
async def recent_threads(limit: int = 5) -> ToolResult:
    """Lista los identificadores de las conversaciones recientes de Mensajes."""
    script = (
        'tell application "Messages"\n'
        'set out to ""\n'
        "set n to 0\n"
        "repeat with c in chats\n"
        "  set out to out & (id of c) & linefeed\n"
        "  set n to n + 1\n"
        f"  if n ≥ {int(limit)} then exit repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MSG_TIMEOUT_S, on_error="No pude leer las conversaciones"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    handles = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not handles:
        return ToolResult(True, {"threads": []}, "No encontré conversaciones recientes.", False)
    return ToolResult(
        True, {"threads": handles}, f"Tienes {len(handles)} conversaciones recientes.", False
    )


@tool(destructive=True)
async def send_imessage(recipient: str, body: str, confirmed: bool = False) -> ToolResult:
    """Envía un iMessage a `recipient`. Pide confirmación antes de enviar.

    `recipient` es un número de teléfono o correo asociado a iMessage.
    """
    if not confirmed:
        return ToolResult(
            True,
            {"recipient": recipient},
            f"¿Le mando el mensaje a {recipient}: '{body}'?",
            True,
        )
    r = macos.esc_applescript(recipient)
    b = macos.esc_applescript(body)
    script = (
        'tell application "Messages"\n'
        "  set targetService to first service whose service type = iMessage\n"
        f'  set targetBuddy to buddy "{r}" of targetService\n'
        f'  send "{b}" to targetBuddy\n'
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MSG_TIMEOUT_S, on_error="No pude enviar el mensaje"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"recipient": recipient}, f"Mensaje enviado a {recipient}.", False)
