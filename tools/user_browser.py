"""User-browser actions — Garcia's everyday browser (Arc/Chrome/Safari/Brave).

Distinct from ``tools/browser.py``, which drives a headless Playwright Chromium
for automation. This drives the real default browser via ``open -a`` + a couple
of AppleScript reads.

Sources: Chrome `URL of active tab of front window`, Safari `URL of current tab
of front window` (Apple/AppleScript browser-scripting references).
"""

from __future__ import annotations

import asyncio
import urllib.parse
import webbrowser

from actions import macos
from core.apps import resolve
from tools.base import ToolResult, tool


@tool()
async def open_url(url: str, new_window: bool = False) -> ToolResult:
    """Abre una URL en el navegador preferido de Garcia.

    Úsalo cuando diga:
    - "Emma, abre <url>"
    - "Emma, ábreme la página de <thing>"
    """
    if "://" not in url:
        url = "https://" + url
    app = resolve("browser") or ""
    try:
        if app:
            args = ["open", "-a", app, url]
            if new_window:
                args.insert(1, "-n")
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        else:
            webbrowser.open(url, new=1 if new_window else 0)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir la página: {exc}", False)
    return ToolResult(
        True, {"url": url, "app": app}, f"Abriendo {url} en {app or 'el navegador'}.", False
    )


@tool()
async def web_search_in_browser(query: str) -> ToolResult:
    """Abre una búsqueda de Google para `query` en el navegador preferido.

    Úsalo cuando Garcia diga "Emma, busca <query> en Google"."""
    q = urllib.parse.quote_plus(query)
    return await open_url(f"https://www.google.com/search?q={q}")


# Chromium-family browsers share Chrome's AppleScript dictionary
# ("close active tab of front window"). Verified for Brave in Garcia's own use.
_CHROME_SYNTAX = ("Google Chrome", "Chrome", "Brave Browser", "Microsoft Edge")


@tool()
async def close_current_tab(browser: str = "") -> ToolResult:
    """Cierra la pestaña activa del navegador. Directo y rápido (Bug 19.2-B5).

    Úsalo cuando Garcia diga 'cierra esta pestaña' / 'cierra la pestaña'."""
    app = browser or resolve("browser") or "Safari"
    if app == "Safari":
        script = 'tell application "Safari" to close current tab of front window'
    elif app in _CHROME_SYNTAX:
        script = (
            f'tell application "{macos.esc_applescript(app)}" to close active tab of front window'
        )
    else:
        # No direct dictionary verb — fall back to ⌘W via System Events, and say so.
        a = macos.esc_applescript(app)
        script = (
            f'tell application "{a}" to activate\n'
            "delay 0.15\n"
            'tell application "System Events" to keystroke "w" using {command down}'
        )
        ok, _ = await macos.osascript_or_friendly(
            script, timeout_s=4.0, on_error="No pude cerrar la pestaña"
        )
        msg = (
            f"Cerré la pestaña en {app} con un atajo (no tiene control directo, puede ser menos preciso)."
            if ok
            else f"No pude cerrar la pestaña en {app}."
        )
        return ToolResult(ok, {"browser": app, "via": "keystroke"}, msg, False)

    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=4.0, on_error="No pude cerrar la pestaña"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True, {"browser": app, "via": "applescript"}, "Listo, cerré la pestaña.", False
    )


@tool()
async def current_tab_url(browser: str = "") -> ToolResult:
    """Devuelve la URL de la pestaña activa del navegador preferido.

    Funciona con Safari y Chrome vía AppleScript; otros devuelven 'no soportado'.
    """
    app = browser or resolve("browser") or ""
    if app not in ("Safari", "Google Chrome", "Chrome"):
        return ToolResult(
            False,
            None,
            f"Saber la URL activa solo funciona con Safari o Chrome (tienes {app}).",
            False,
        )
    if app == "Safari":
        script = 'tell application "Safari" to get URL of current tab of front window'
    else:
        script = 'tell application "Google Chrome" to get URL of active tab of front window'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=4.0, on_error="No pude leer la URL"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"url": out.strip()}, out.strip(), False)
