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
import re
import urllib.parse
from typing import Any

from core import app_capabilities, dictionary
from tools.base import ToolResult, tool
from tools.disambiguation import suggest_similar, suggestion_question


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# {placeholder} names inside resource_url templates (19.6-B17).
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _fill_template(template: str, data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Substitute {placeholders} from `data`, URL-escaping each value.

    Returns ``(url, None)`` or ``(None, missing_key)`` — a missing placeholder
    must NEVER substitute silently as empty (B17 anti-pattern)."""
    missing: str | None = None

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        key = m.group(1)
        val = data.get(key)
        if val in (None, ""):
            missing = missing or key
            return ""
        return _q(str(val))

    url = _PLACEHOLDER_RE.sub(sub, template)
    return (None, missing) if missing else (url, None)


def _resource_url(app_slug: str, kind: str, data: dict[str, Any]) -> tuple[str | None, str]:
    """Build a resource deep link from the capabilities template.

    Returns ``(url, "")`` on success or ``(None, spanish_error)``."""
    caps = app_capabilities.caps_for(app_slug)
    if not caps or kind not in caps.resource_url:
        return None, f"No sé abrir un recurso tipo '{kind}' en {app_slug}."
    url, missing = _fill_template(caps.resource_url[kind], data)
    if url is None:
        return (
            None,
            f"Me falta el dato '{missing}' para abrir eso en {app_slug}. Dímelo y lo anoto.",
        )
    return url, ""


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
async def open_in_app(
    target: str,
    app: str = "",
    kind: str = "",
    fields: dict[str, str] | None = None,
    picked: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Abre algo en una app: una URL directa, un recurso guardado, o destino + app.

    Úsalo cuando Garcia diga:
    - "Emma, abre la conexión learning-rots-local" → kind="connection"
    - "Emma, abre el canal general en Slack" → app="slack", kind="channel"
    - "Emma, crea una tarea en Things: comprar leche"
    - "Emma, abre <url>"
    `kind` es el tipo de recurso (connection/channel/dm/note); `fields` aporta
    datos extra. Si sugerí opciones ("¿quisiste decir…?") y Garcia eligió,
    re-llámame con `picked=<su elección>` y confirmed=true (21-B25).
    """
    target = (picked or target or "").strip()
    if not target and not app:
        return ToolResult(False, None, "¿Qué abro y en qué app?", False)

    # 1. Already a URL or app scheme → open it directly.
    if "://" in target:
        try:
            await _open(target)
        except Exception as exc:
            return ToolResult(False, None, f"No pude abrir eso: {exc}", False)
        return ToolResult(True, {"opened": target}, "Abriendo.", False)

    # 2. Saved resource? [connections] knows Garcia's in-app resource names
    #    (19.6-B17): "abre la conexión learning-rots-local" → TablePlus deep link.
    conn = dictionary.find_connection(target)
    if conn:
        conn_app = str(conn.get("app", app))
        conn_kind = str(conn.get("kind", kind or "connection"))
        data: dict[str, Any] = {
            **dictionary.user_app(conn_app),
            **conn,
            **(fields or {}),
        }
        url, err = _resource_url(conn_app, conn_kind, data)
        if url is None:
            return ToolResult(False, None, err, False)
        try:
            await _open(url)
        except Exception as exc:
            return ToolResult(False, None, f"No pude abrir eso en {conn_app}: {exc}", False)
        return ToolResult(
            True,
            {"url": url, "app": conn_app, "kind": conn_kind},
            f"Abriendo {conn.get('name', target)} en {conn_app}.",
            False,
        )

    # 3. Explicit app + kind → template with the dictated name.
    if app and kind:
        data = {
            **dictionary.user_app(app),
            **(fields or {}),
        }
        data.setdefault("name", target)
        url, err = _resource_url(app, kind, data)
        if url is None:
            return ToolResult(False, None, err, False)
        try:
            await _open(url)
        except Exception as exc:
            return ToolResult(False, None, f"No pude abrir eso en {app}: {exc}", False)
        return ToolResult(
            True, {"url": url, "app": app, "kind": kind}, f"Abriendo {target} en {app}.", False
        )

    # A resource was named (kind given) but nothing matched → fuzzy-suggest
    # from the saved connections (21-B25: the TablePlus mistranscription fix);
    # only when nothing is even close, offer to learn it.
    if kind and not app:
        conns = dictionary.connections()
        names = sorted(
            {k for k in conns} | {str(v.get("name", "")) for v in conns.values() if v.get("name")}
        )
        suggestions = suggest_similar(target, names)
        if suggestions:
            return ToolResult(
                True,
                {"query": target, "suggestions": [s for s, _ in suggestions]},
                suggestion_question(target, suggestions, noun="una conexión"),
                requires_confirmation=True,
            )
        return ToolResult(
            False,
            None,
            f"No tengo una conexión llamada '{target}'. Si me dictas el nombre "
            "exacto que usas en la app, la anoto.",
            False,
        )

    # 4. Plain target needs an app.
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
