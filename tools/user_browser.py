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


# ---- Tabs management (19.6-B19) -------------------------------------------

# Tracking query params dropped when canonicalizing URLs for duplicate
# detection. utm_* is matched by prefix.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref", "igshid"})

# Garcia's standing rule: never bulk-close Google tabs unless he explicitly
# overrides with protect_domains=[] on that single call.
_DEFAULT_PROTECT = ("google.com",)

_TAB_SEP = "‖"  # same unlikely-in-titles separator as tools/disambiguation


def _canon_url(url: str) -> str:
    """Lowercased host/scheme, trailing slash + fragment dropped, utm_* stripped."""
    parts = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.urlencode(
        [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
    )
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _host_protected(url: str, protect: list[str] | tuple[str, ...]) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    for d in protect:
        d = d.strip().lower()
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def _enumerate_tabs_script(app: str) -> str:
    """One round trip: emit window‖index‖title‖url per tab, every window."""
    title_prop = "name" if app == "Safari" else "title"
    a = macos.esc_applescript(app)
    return (
        f'tell application "{a}"\n'
        '  set out to ""\n'
        "  set wi to 0\n"
        "  repeat with w in windows\n"
        "    set wi to wi + 1\n"
        "    set ti to 0\n"
        "    repeat with t in tabs of w\n"
        "      set ti to ti + 1\n"
        f'      set out to out & wi & "{_TAB_SEP}" & ti & "{_TAB_SEP}" & '
        f'({title_prop} of t) & "{_TAB_SEP}" & (URL of t) & linefeed\n'
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )


def _parse_tabs(raw: str) -> list[dict[str, object]]:
    tabs: list[dict[str, object]] = []
    for line in raw.splitlines():
        parts = line.strip().split(_TAB_SEP)
        if len(parts) < 4:
            continue
        try:
            window, index = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        tabs.append({"window": window, "index": index, "title": parts[2], "url": parts[3].strip()})
    return tabs


async def _all_tabs(app: str) -> list[dict[str, object]]:
    raw = await macos.osascript(_enumerate_tabs_script(app), timeout_s=10.0)
    return _parse_tabs(raw)


def _close_script(app: str, tabs: list[dict[str, object]]) -> str:
    """Close by (window, index) in REVERSE order so live indices stay valid."""
    a = macos.esc_applescript(app)
    ordered = sorted(tabs, key=lambda t: (t["window"], t["index"]), reverse=True)
    lines = [f'tell application "{a}"']
    lines += [f"  close tab {t['index']} of window {t['window']}" for t in ordered]
    lines.append("end tell")
    return "\n".join(lines)


@tool()
async def list_browser_tabs(browser: str = "") -> ToolResult:
    """Lista todas las pestañas abiertas del navegador (todas las ventanas).

    Úsalo cuando Garcia diga "¿cuántas pestañas tengo?" / "lista mis pestañas".
    """
    app = browser or resolve("browser") or "Safari"
    try:
        tabs = await _all_tabs(app)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las pestañas de {app}: {exc}", False)
    windows = len({t["window"] for t in tabs})
    tabs_w = "pestaña" if len(tabs) == 1 else "pestañas"
    win_w = "ventana" if windows == 1 else "ventanas"
    return ToolResult(
        True,
        {"tabs": tabs, "browser": app},
        f"Tienes {len(tabs)} {tabs_w} en {windows} {win_w} de {app}.",
        False,
    )


def _dup_candidates(
    tabs: list[dict[str, object]], protect: list[str] | tuple[str, ...]
) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for t in sorted(tabs, key=lambda t: (t["window"], t["index"])):
        groups.setdefault(_canon_url(str(t["url"])), []).append(t)
    out: list[dict[str, object]] = []
    for group in groups.values():
        for extra in group[1:]:  # keep the lowest (window, index)
            if not _host_protected(str(extra["url"]), protect):
                out.append(extra)
    return out


@tool(destructive=True)
async def close_duplicate_tabs(
    browser: str = "", protect_domains: list[str] | None = None, confirmed: bool = False
) -> ToolResult:
    """Detecta y cierra pestañas duplicadas (misma URL canónica). Pide
    confirmación y respeta dominios protegidos (google.com por defecto).

    Úsalo cuando Garcia diga "cierra las pestañas duplicadas". Si pide
    explícitamente incluir Google ("cierra todas, incluyendo Google"), pasa
    protect_domains=[] SOLO en esa llamada."""
    app = browser or resolve("browser") or "Safari"
    protect = list(_DEFAULT_PROTECT) if protect_domains is None else protect_domains
    try:
        tabs = await _all_tabs(app)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las pestañas de {app}: {exc}", False)

    candidates = _dup_candidates(tabs, protect)
    if not candidates:
        return ToolResult(True, {"close": []}, "No encontré pestañas duplicadas.", False)

    if not confirmed:
        prot = f" (respetando {', '.join(protect)})" if protect else ""
        return ToolResult(
            True,
            {"close": candidates, "browser": app},
            f"Encontré {len(candidates)} pestaña(s) duplicada(s){prot}. ¿Las cierro?",
            requires_confirmation=True,
        )

    ok, out = await macos.osascript_or_friendly(
        _close_script(app, candidates), timeout_s=10.0, on_error="No pude cerrar las pestañas"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True,
        {"closed": len(candidates), "browser": app},
        f"Listo, cerré {len(candidates)} pestaña(s) duplicada(s).",
        False,
    )


@tool(destructive=True)
async def close_tabs_matching(
    pattern: str,
    browser: str = "",
    protect_domains: list[str] | None = None,
    confirmed: bool = False,
) -> ToolResult:
    """Cierra las pestañas cuyo título O URL contienen `pattern` (literal).

    Úsalo cuando Garcia diga "cierra todas las de YouTube". Pide confirmación
    y respeta dominios protegidos (google.com por defecto)."""
    p = (pattern or "").strip().lower()
    if not p:
        return ToolResult(False, None, "¿Qué pestañas cierro? Dame un patrón.", False)
    app = browser or resolve("browser") or "Safari"
    protect = list(_DEFAULT_PROTECT) if protect_domains is None else protect_domains
    try:
        tabs = await _all_tabs(app)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las pestañas de {app}: {exc}", False)

    candidates = [
        t
        for t in tabs
        if (p in str(t["title"]).lower() or p in str(t["url"]).lower())
        and not _host_protected(str(t["url"]), protect)
    ]
    if not candidates:
        return ToolResult(
            True, {"close": []}, f"No hay pestañas que coincidan con '{pattern}'.", False
        )

    if not confirmed:
        return ToolResult(
            True,
            {"close": candidates, "browser": app},
            f"Cerraría {len(candidates)} pestaña(s) que coinciden con '{pattern}'. ¿Lo hago?",
            requires_confirmation=True,
        )

    ok, out = await macos.osascript_or_friendly(
        _close_script(app, candidates), timeout_s=10.0, on_error="No pude cerrar las pestañas"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True,
        {"closed": len(candidates)},
        f"Listo, cerré {len(candidates)} pestaña(s).",
        False,
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
