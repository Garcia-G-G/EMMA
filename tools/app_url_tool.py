"""Universal app control via URL schemes + a capabilities registry.

One tool drives many apps: it builds the app's deep-link URL from
``core.app_capabilities`` (public capability) + ``core.dictionary.user_app``
(per-user IDs) and hands it to macOS ``open``. Apps without a scheme just launch.

Scaling happens in the TOML registry, not here — ``remember_app`` lets Garcia
add an app by voice.

Sources (verified): Slack slack://channel?team&id (api.slack.com/reference/
deep-linking), Things things:///add?title (culturedcode.com), Obsidian
obsidian://open?vault&file (obsidian.md/help/uri).
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

from core import app_capabilities, dictionary
from tools.base import ToolResult, tool


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _qp(s: str) -> str:
    return urllib.parse.quote_plus(s)


def _build_deeplink(app_slug: str, target: str, ucfg: dict[str, Any]) -> str | None:
    """Build a deep-link URL for a plain `target` + app. None → just launch."""
    if app_slug == "slack":
        return f"slack://channel?team={_q(ucfg.get('workspace', ''))}&id={_q(target)}"
    if app_slug == "things":
        return f"things:///add?title={_qp(target)}"
    if app_slug == "obsidian":
        return f"obsidian://open?vault={_q(ucfg.get('default_vault', ''))}&file={_q(target)}"
    if app_slug == "todoist":
        return f"todoist://addtask?content={_qp(target)}"
    if app_slug == "whatsapp":
        return f"whatsapp://send?phone={_q(target)}"
    # Has a scheme but no parameter builder (figma/linear/notion need IDs we
    # can't synthesize from a free-text target) → caller launches the app.
    return None


async def _open(*args: str) -> None:
    # exec (no shell) keeps the loop unblocked and avoids injection.
    proc = await asyncio.create_subprocess_exec(
        "open", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await asyncio.wait_for(proc.wait(), timeout=5.0)


@tool()
async def open_in_app(target: str, app: str = "") -> ToolResult:
    """Abre algo en una app: una URL/esquema directo, o un destino + app.

    Úsalo cuando Garcia diga:
    - "Emma, abre el canal general en Slack"
    - "Emma, crea una tarea en Things: comprar leche"
    - "Emma, abre <url>"
    """
    target = (target or "").strip()
    if not target and not app:
        return ToolResult(False, None, "¿Qué abro y en qué app?", False)

    # 1. Already a URL or app scheme → open it directly.
    if "://" in target:
        try:
            await _open(target)
        except Exception as exc:
            return ToolResult(False, None, f"No pude abrir eso: {exc}", False)
        return ToolResult(True, {"opened": target}, "Abriendo.", False)

    # 2. Plain target needs an app.
    if not app:
        return ToolResult(False, None, "¿En qué app lo abro?", False)
    caps = app_capabilities.caps_for(app)
    try:
        if caps and caps.url_scheme:
            url = _build_deeplink(caps.app, target, dictionary.user_app(app))
            if url:
                await _open(url)
                return ToolResult(True, {"url": url, "app": app}, f"Abriendo en {app}.", False)
        # No scheme / no builder → just launch the app.
        await _open("-a", app)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir {app}: {exc}", False)
    return ToolResult(True, {"app": app, "launched": True}, f"Abriendo {app}.", False)


@tool()
async def remember_app(
    name: str,
    url_scheme: str = "",
    category: str = "",
    applescript_dict: str = "",
    cli: str = "",
    notes: str = "",
) -> ToolResult:
    """Enseña a Emma a controlar una app nueva (info pública, no destructiva).

    Úsalo cuando Garcia diga "Emma, recuerda que <app> usa el esquema <scheme>".
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿Cuál app agrego?", False)
    slug = app_capabilities.append_app(
        name,
        url_scheme=url_scheme.strip(),
        category=category.strip(),
        applescript_dict=applescript_dict.strip(),
        cli=cli.strip(),
        notes=notes.strip(),
    )
    return ToolResult(
        True, {"slug": slug}, f"Listo, agregué {name} a las apps que controlo.", False
    )
