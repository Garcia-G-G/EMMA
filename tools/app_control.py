"""Generic Accessibility / System Events automation — the last-resort layer.

Used when an app has no specialized action module. Coarse, UI-level operations
via System Events. Prefer a specialized tool (open_in_ide, play_track, ...)
when one exists.

Requires the Accessibility permission (the 15.5 bootstrap requests it).

Sources: AppleScript key codes (escape 53, delete 51, arrows 123-126;
dougscripts/eastmanreference); System Events menu hierarchy `menu item of menu
of menu bar item of menu bar 1` (Apple Mac Automation Scripting Guide).
"""

from __future__ import annotations

from actions import macos
from tools.base import ToolResult, tool

# Modifier word → AppleScript token.
_MODS = {
    "cmd": "command down",
    "command": "command down",
    "shift": "shift down",
    "alt": "option down",
    "option": "option down",
    "opt": "option down",
    "ctrl": "control down",
    "control": "control down",
}
# Keys that `keystroke <word>` accepts directly (no quotes, no key code).
_KEYWORD_KEYS = {"return": "return", "enter": "return", "tab": "tab", "space": "space"}
# Keys that need `key code N`.
_KEYCODE_KEYS = {
    "escape": 53,
    "esc": 53,
    "delete": 51,
    "backspace": 51,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}


def _keystroke_action(keys: str) -> str | None:
    """Compose the System Events action for 'Cmd+Shift+P' etc. None if unknown."""
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    if not parts:
        return None
    mods = [_MODS[p.lower()] for p in parts[:-1] if p.lower() in _MODS]
    key = parts[-1].lower()
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    if key in _KEYWORD_KEYS:
        return f"keystroke {_KEYWORD_KEYS[key]}{using}"
    if key in _KEYCODE_KEYS:
        return f"key code {_KEYCODE_KEYS[key]}{using}"
    if len(key) == 1:
        return f'keystroke "{key}"{using}'
    return None


def _menu_ref(parts: list[str]) -> str:
    """Build the System Events reference for a menu path like File > New > Item.

    parts[0] is the top-level menu; parts[-1] is the item to click.
    """
    esc = macos.esc_applescript
    ref = f'menu item "{esc(parts[-1])}"'
    for p in parts[-2:0:-1]:  # intermediate submenus, walking outward
        ref += f' of menu "{esc(p)}" of menu item "{esc(p)}"'
    ref += f' of menu "{esc(parts[0])}" of menu bar item "{esc(parts[0])}" of menu bar 1'
    return ref


@tool()
async def app_focus(app: str) -> ToolResult:
    """Trae `app` al frente. Úsalo cuando Garcia diga 'Emma, enfoca Cursor'."""
    ok, _ = await macos.osascript_or_friendly(
        f'tell application "{macos.esc_applescript(app)}" to activate',
        timeout_s=4.0,
        on_error=f"No pude enfocar {app}",
    )
    return ToolResult(
        ok, {"app": app}, f"Enfoqué {app}." if ok else f"No pude enfocar {app}.", False
    )


@tool(destructive=True)
async def app_keystroke(app: str, keys: str, confirmed: bool = False) -> ToolResult:
    """Manda un atajo de teclado a `app`. Formato: 'Cmd+T', 'Cmd+Shift+P', 'Escape'.

    Úsalo cuando Garcia diga 'Emma, presiona Cmd+T en Cursor'.
    """
    if not confirmed:
        return ToolResult(
            True,
            {"app": app, "keys": keys},
            f"¿Presiono {keys} en {app}?",
            requires_confirmation=True,
        )
    action = _keystroke_action(keys)
    if action is None:
        return ToolResult(False, None, f"No reconozco la combinación '{keys}'.", False)
    script = (
        f'tell application "{macos.esc_applescript(app)}" to activate\n'
        "delay 0.2\n"
        f'tell application "System Events" to {action}'
    )
    ok, _ = await macos.osascript_or_friendly(
        script, timeout_s=4.0, on_error="No pude mandar la tecla"
    )
    return ToolResult(
        ok, {"app": app, "keys": keys}, f"Mandé {keys} a {app}." if ok else "No pude.", False
    )


@tool(destructive=True)
async def app_menu_click(app: str, menu_path: str, confirmed: bool = False) -> ToolResult:
    """Hace click en un ítem de menú por ruta. Ejemplo: 'File > New Window'."""
    if not confirmed:
        return ToolResult(
            True,
            {"app": app, "menu": menu_path},
            f"¿Clico {menu_path} en {app}?",
            requires_confirmation=True,
        )
    parts = [p.strip() for p in menu_path.split(">") if p.strip()]
    if len(parts) < 2:
        return ToolResult(False, None, "Formato: 'Menú > Submenú > Ítem'.", False)
    a = macos.esc_applescript(app)
    script = (
        f'tell application "{a}" to activate\n'
        "delay 0.2\n"
        f'tell application "System Events" to tell process "{a}"\n'
        f"    click {_menu_ref(parts)}\n"
        "end tell"
    )
    ok, _ = await macos.osascript_or_friendly(script, timeout_s=4.0, on_error="No pude hacer click")
    return ToolResult(
        ok, {"app": app, "menu": menu_path}, f"Clicado {parts[-1]}." if ok else "No pude.", False
    )
