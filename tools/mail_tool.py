"""Apple Mail via AppleScript: read unread, search, draft, send (with confirmation)."""

from __future__ import annotations

from typing import Any

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.mail")

_MAIL_TIMEOUT_S = 15.0


def _parse_pairs(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        left, _, right = line.partition("|")
        out.append({"from": left.strip(), "subject": right.strip()})
    return out


@tool(returns_untrusted_content=True)
async def list_unread(limit: int = 10) -> ToolResult:
    """Lista los correos sin leer más recientes (remitente y asunto)."""
    script = (
        'tell application "Mail"\n'
        'set out to ""\n'
        "set n to 0\n"
        "repeat with m in (messages of inbox whose read status is false)\n"
        '  set out to out & (sender of m) & "|" & (subject of m) & linefeed\n'
        "  set n to n + 1\n"
        f"  if n ≥ {int(limit)} then exit repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MAIL_TIMEOUT_S, on_error="No pude leer el correo"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    msgs = _parse_pairs(out)
    if not msgs:
        return ToolResult(True, {"messages": []}, "No tienes correos sin leer.", False)
    spoken = "; ".join(f"{m['from']}: {m['subject']}" for m in msgs)
    return ToolResult(True, {"messages": msgs}, f"Sin leer: {spoken}.", False)


@tool(returns_untrusted_content=True)
async def search_mail(query: str, limit: int = 10) -> ToolResult:
    """Busca correos en la bandeja de entrada cuyo asunto contiene `query`."""
    q = macos.esc_applescript(query)
    script = (
        'tell application "Mail"\n'
        'set out to ""\n'
        "set n to 0\n"
        f'repeat with m in (messages of inbox whose subject contains "{q}")\n'
        '  set out to out & (sender of m) & "|" & (subject of m) & linefeed\n'
        "  set n to n + 1\n"
        f"  if n ≥ {int(limit)} then exit repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MAIL_TIMEOUT_S, on_error="No pude buscar en el correo"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    msgs = _parse_pairs(out)
    spoken = "; ".join(f"{m['from']}: {m['subject']}" for m in msgs) or "nada"
    return ToolResult(True, {"messages": msgs}, f"Encontré: {spoken}.", False)


@tool(returns_untrusted_content=True)
async def recent_from(sender: str, limit: int = 10) -> ToolResult:
    """Busca correos recientes de un remitente (incluye un fragmento del cuerpo).

    Filtra por remitente, no por asunto — sirve para «¿me escribió X?» y para los
    triggers condicionales email_from (que necesitan ver el cuerpo, no solo el asunto).
    """
    q = macos.esc_applescript(sender)
    script = (
        'tell application "Mail"\n'
        'set out to ""\n'
        "set n to 0\n"
        f'repeat with m in (messages of inbox whose sender contains "{q}")\n'
        '  set c to ""\n'
        "  try\n"
        "    set c to content of m\n"
        "    if (count of c) > 280 then set c to (text 1 thru 280 of c)\n"
        "  end try\n"
        '  set out to out & (sender of m) & " || " & (subject of m) & " || " & c & "<<<EOM>>>"\n'
        "  set n to n + 1\n"
        f"  if n ≥ {int(limit)} then exit repeat\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MAIL_TIMEOUT_S, on_error="No pude buscar en el correo"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    msgs: list[dict[str, Any]] = []
    for chunk in out.split("<<<EOM>>>"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(" || ", 2)
        msgs.append({"from": parts[0].strip(),
                     "subject": parts[1].strip() if len(parts) > 1 else "",
                     "preview": parts[2].strip() if len(parts) > 2 else ""})
    spoken = "; ".join(f"{m['from']}: {m['subject']}" for m in msgs) or "nada"
    # `raw` carries sender+subject+body so substring triggers can match the body.
    return ToolResult(True, {"messages": msgs, "raw": out}, f"De ese remitente: {spoken}.", False)


@tool()
async def draft_to(recipient: str, subject: str, body: str) -> ToolResult:
    """Abre un borrador de correo (no lo envía) para `recipient` con asunto y cuerpo."""
    r = macos.esc_applescript(recipient)
    s = macos.esc_applescript(subject)
    b = macos.esc_applescript(body)
    script = (
        'tell application "Mail"\n'
        f'  set newMsg to make new outgoing message with properties {{subject:"{s}", content:"{b}", visible:true}}\n'
        "  tell newMsg\n"
        f'    make new to recipient at end of to recipients with properties {{address:"{r}"}}\n'
        "  end tell\n"
        "  activate\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MAIL_TIMEOUT_S, on_error="No pude abrir el borrador"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"recipient": recipient}, f"Abrí un borrador para {recipient}.", False)


@tool(destructive=True)
async def send_to(
    recipient: str, subject: str, body: str, picked: str = "", confirmed: bool = False
) -> ToolResult:
    """Envía un correo a `recipient`. Pide confirmación antes de enviarlo.

    `recipient` puede ser un correo O un contacto guardado ('mi mamá'). Si
    sugerí contactos parecidos y Garcia eligió, re-llámame con
    `picked=<su elección>` (21-B25)."""
    from tools.contact_resolve import resolve_recipient

    resolved, miss = resolve_recipient(picked or recipient)
    if miss is not None:
        return miss
    assert resolved is not None
    recipient = resolved
    if not confirmed:
        return ToolResult(
            True,
            {"recipient": recipient, "subject": subject},
            f"¿Envío el correo a {recipient} con asunto '{subject}'?",
            True,
        )
    r = macos.esc_applescript(recipient)
    s = macos.esc_applescript(subject)
    b = macos.esc_applescript(body)
    script = (
        'tell application "Mail"\n'
        f'  set newMsg to make new outgoing message with properties {{subject:"{s}", content:"{b}", visible:false}}\n'
        "  tell newMsg\n"
        f'    make new to recipient at end of to recipients with properties {{address:"{r}"}}\n'
        "  end tell\n"
        "  send newMsg\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_MAIL_TIMEOUT_S, on_error="No pude enviar el correo"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"recipient": recipient}, f"Correo enviado a {recipient}.", False)
