"""Safari via AppleScript: current tab URL/text, open URL, bookmark."""

from __future__ import annotations

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.safari")

_SAFARI_TIMEOUT_S = 12.0


@tool()
async def current_url() -> ToolResult:
    """Devuelve la URL de la pestaña activa de Safari."""
    script = (
        'tell application "Safari"\n'
        '  if (count of windows) is 0 then return ""\n'
        "  return URL of current tab of front window\n"
        "end tell"
    )
    try:
        url = await macos.osascript(script, timeout_s=_SAFARI_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer Safari: {exc}", False)
    if not url:
        return ToolResult(True, {"url": ""}, "No hay ninguna pestaña abierta.", False)
    return ToolResult(True, {"url": url}, f"La pestaña actual es {url}.", False)


@tool()
async def current_page_text() -> ToolResult:
    """Devuelve el texto legible de la pestaña activa de Safari.

    Requiere activar "Permitir JavaScript desde eventos de Apple" en el
    menú Desarrollo de Safari.
    """
    script = (
        'tell application "Safari"\n'
        '  if (count of windows) is 0 then return ""\n'
        '  return (do JavaScript "document.body.innerText" in current tab of front window)\n'
        "end tell"
    )
    try:
        text = await macos.osascript(script, timeout_s=_SAFARI_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer la página: {exc}", False)
    excerpt = text[:2000]
    return ToolResult(True, {"text": text}, excerpt or "La página no tiene texto.", False)


@tool()
async def open_url(url: str) -> ToolResult:
    """Abre `url` en una pestaña nueva de Safari."""
    u = macos.esc_applescript(url)
    script = (
        'tell application "Safari"\n'
        "  activate\n"
        "  if (count of windows) is 0 then\n"
        f'    make new document with properties {{URL:"{u}"}}\n'
        "  else\n"
        f'    tell front window to set current tab to (make new tab with properties {{URL:"{u}"}})\n'
        "  end if\n"
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_SAFARI_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude abrir el enlace: {exc}", False)
    return ToolResult(True, {"url": url}, f"Abriendo {url}.", False)


@tool()
async def bookmark_current(folder: str = "") -> ToolResult:
    """Guarda la pestaña activa de Safari en marcadores (vía Cmd+D).

    Safari no expone la creación de marcadores en su diccionario
    AppleScript, así que esto usa scripting de interfaz (System Events) y
    requiere permiso de Accesibilidad. `folder` no se puede elegir aquí.
    """
    script = (
        'tell application "Safari" to activate\n'
        'tell application "System Events"\n'
        '  keystroke "d" using {command down}\n'
        "  delay 0.4\n"
        "  key code 36\n"  # Return to confirm the add-bookmark sheet
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_SAFARI_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude guardar el marcador: {exc}", False)
    return ToolResult(True, None, "Guardé la página en marcadores.", False)
