"""Voice-controlled environment preferences.

The shortlists in :mod:`actions.environment` are the contract: any app
that is not in the shortlist for a category is refused with a list of
the supported options.
"""

from __future__ import annotations

import asyncio

import structlog

from actions.environment import (
    BROWSER_SHORTLIST,
    SHORTLISTS,
    Category,
    default_browser_bundle,
    detect_preferred,
    install_cask,
    set_preference,
    trigger_default_browser_change,
)
from core.runtime import get_spoken_lang
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.preferences")


def _category_options(category: Category) -> list[str]:
    return [e["key"] for e in SHORTLISTS[category]]


def _normalize_app(category: Category, raw: str) -> str | None:
    """Loose match: 'VS Code' -> 'code', 'iTerm2' -> 'iterm', etc."""
    norm = raw.strip().lower().replace(" ", "").replace("-", "")
    aliases = {
        "ide": {
            "cursor": "cursor",
            "vscode": "code",
            "visualstudiocode": "code",
            "code": "code",
            "zed": "zed",
            "sublime": "subl",
            "sublimetext": "subl",
            "subl": "subl",
        },
        "terminal": {
            "iterm": "iterm",
            "iterm2": "iterm",
            "warp": "warp",
            "ghostty": "ghostty",
            "terminal": "terminal",
            "terminalapp": "terminal",
        },
        "music": {
            "spotify": "spotify",
            "music": "music",
            "applemusic": "music",
        },
        "browser": {
            "brave": "brave",
            "bravebrowser": "brave",
            "chrome": "chrome",
            "googlechrome": "chrome",
            "firefox": "firefox",
            "arc": "arc",
            "safari": "safari",
        },
    }
    return aliases.get(category, {}).get(norm)


@tool()
def set_preferred_app(category: str, app_name: str) -> ToolResult:
    """Save the user's preferred app for a category.

    Use when the user says things like:

    - "Emma, prefiero Zed para código"
    - "Emma, usa Warp en lugar de Ghostty"
    - "Emma, cámbiame el editor a Cursor"
    - "Emma, prefer Brave for testing"

    Categories: ``ide``, ``terminal``, ``music``, ``browser``. The app
    must be in the project's supported shortlist - unknown apps are
    refused with the list of options.
    """
    cat = category.lower().strip()
    if cat not in SHORTLISTS:
        return ToolResult(
            False,
            None,
            f"No reconozco la categoría '{category}'. Las categorías son: ide, terminal, music, browser.",
            False,
        )
    key = _normalize_app(cat, app_name)
    if key is None:
        options = ", ".join(_category_options(cat))
        return ToolResult(
            False,
            None,
            f"No soporto {app_name} todavía. Las opciones para {cat} son: {options}.",
            False,
        )
    # Ground the preference in reality: only honor an app the user actually has.
    detected = detect_preferred(cat)
    installed = detected.available_alternatives
    set_preference(cat, key)
    if key in installed:
        msg = f"Listo. Voy a usar {app_name} para {cat} de ahora en adelante."
    else:
        have = ", ".join(installed) if installed else "ninguna"
        msg = (
            f"Anoté {app_name} como tu preferencia de {cat}, pero no la tienes instalada. "
            f"Instaladas: {have}. Dime si quieres que la instale o uso una de esas."
        )
    return ToolResult(
        True,
        {"category": cat, "app": key, "installed": key in installed, "available": installed},
        msg,
        False,
    )


@tool()
def get_preferred_app(category: str) -> ToolResult:
    """Tell the user which app Emma is currently using for a category.

    Use when the user says things like:

    - "Emma, ¿qué editor uso?"
    - "Emma, ¿qué terminal prefieres?"
    """
    cat = category.lower().strip()
    if cat not in SHORTLISTS:
        return ToolResult(
            False,
            None,
            f"No reconozco la categoría '{category}'.",
            False,
        )
    result = detect_preferred(cat)
    if result.app_name is None:
        return ToolResult(
            True,
            {"category": cat, "app": None},
            f"No tengo ninguna app de {cat} configurada.",
            False,
        )
    suffix = " (lo elegiste tú)" if result.is_user_override else ""
    return ToolResult(
        True,
        {
            "category": cat,
            "app": result.app_name,
            "is_user_override": result.is_user_override,
            "available": result.available_alternatives,
        },
        f"Uso {result.app_name} para {cat}{suffix}.",
        False,
    )


@tool()
def list_apps(category: str = "") -> ToolResult:
    """List which apps are installed for a category and which one Emma uses by default.

    Use this BEFORE choosing or opening an app, and when the user asks things like:

    - "Emma, ¿qué editores tengo?"
    - "Emma, what browsers do I have?"
    - "Emma, ¿qué apps tengo instaladas?"

    Categories: ``ide``, ``terminal``, ``music``, ``browser``. With no category,
    reports all four. Never pick or open an app that isn't in the installed list.
    """
    requested = category.lower().strip()
    if requested and requested not in SHORTLISTS:
        return ToolResult(
            False,
            None,
            f"No reconozco la categoría '{category}'. Las categorías son: ide, terminal, music, browser.",
            False,
        )
    cats: list[Category] = [requested] if requested else list(SHORTLISTS.keys())
    data: dict[str, dict[str, object]] = {}
    parts: list[str] = []
    for cat in cats:
        result = detect_preferred(cat)
        installed = result.available_alternatives
        data[cat] = {
            "installed": installed,
            "default": result.app_name,
            "is_user_override": result.is_user_override,
        }
        have = ", ".join(installed) if installed else "ninguna"
        parts.append(f"{cat}: tienes {have}; uso {result.app_name or 'ninguna'}")
    return ToolResult(True, data, ". ".join(parts) + ".", False)


@tool(destructive=True)
async def set_default_browser(name: str, confirmed: bool = False) -> ToolResult:
    """Set the system default browser (URL handler for http/https).

    Use when the user says things like:

    - "Emma, hazme default Brave"
    - "Emma, make Chrome my default browser"

    macOS forces a confirmation dialog the user must click - this is an
    accepted UX cost. Emma installs the browser if missing, then opens
    it so its own "Set as default" prompt fires, then reads back the
    LaunchServices handler after 5 seconds to confirm.
    """
    cat: Category = "browser"
    key = _normalize_app(cat, name)
    if key is None:
        options = ", ".join(_category_options(cat))
        return ToolResult(
            False,
            None,
            f"No soporto {name} como navegador. Las opciones son: {options}.",
            False,
        )

    entry = next((e for e in BROWSER_SHORTLIST if e["key"] == key), None)
    if entry is None:
        options = ", ".join(_category_options(cat))
        return ToolResult(
            False,
            None,
            f"No soporto {name} como navegador. Las opciones son: {options}.",
            False,
        )
    bundle = entry["bundle"]

    # Install if missing (must explicitly confirm: this is destructive).
    detect = detect_preferred(cat)
    available = detect.available_alternatives
    needs_install = key not in available
    cask = entry.get("cask")

    if needs_install and not confirmed:
        if not cask:
            return ToolResult(
                False,
                None,
                f"{name} no está instalado y no tengo un cask para instalarlo.",
                False,
            )
        return ToolResult(
            True,
            {"pending": "install_then_set_default", "browser": key},
            f"{name} no está instalado. ¿Lo instalo y lo dejo como default?",
            requires_confirmation=True,
        )

    if needs_install and confirmed:
        lang = get_spoken_lang()
        ok, _ = await install_cask(cask, spoken_lang=lang)  # type: ignore[arg-type]
        if not ok:
            return ToolResult(False, None, f"No pude instalar {name}.", False)

    await trigger_default_browser_change(bundle)

    # macOS shows a confirmation dialog. Tell the user, wait, verify.
    await asyncio.sleep(5)
    current = default_browser_bundle()
    if current == bundle:
        return ToolResult(
            True,
            {"bundle": bundle},
            f"Listo, {name} ya es tu navegador por defecto.",
            False,
        )
    return ToolResult(
        False,
        {"current": current, "expected": bundle},
        (
            f"macOS te va a preguntar si confirmas el cambio. "
            f"Dale click a 'Use {name}'. Si no aparece, abre {name} y mira el banner superior."
        ),
        False,
    )
