"""System control: apps, volume, brightness, sleep, screen lock, clock."""
from __future__ import annotations

import datetime as dt
import zoneinfo

import structlog

from actions import macos
from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.system")


@tool()
def open_application(name: str) -> ToolResult:
    """Open a macOS application by display name (e.g. "Spotify", "Safari", "Notes")."""
    try:
        macos.open_app(name)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude abrir {name}: {exc}", False)
    return ToolResult(True, {"name": name}, f"Abriendo {name}.", False)


@tool()
def set_volume(percent: int) -> ToolResult:
    """Set the system output volume. `percent` is 0-100."""
    try:
        macos.set_volume(percent)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude cambiar el volumen: {exc}", False)
    p = max(0, min(100, int(percent)))
    return ToolResult(True, {"volume": p}, f"Volumen al {p} por ciento.", False)


@tool()
def mute() -> ToolResult:
    """Mute the system output."""
    try:
        macos.set_mute(True)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, str(exc), False)
    return ToolResult(True, None, "Silenciado.", False)


@tool()
def unmute() -> ToolResult:
    """Unmute the system output."""
    try:
        macos.set_mute(False)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, str(exc), False)
    return ToolResult(True, None, "Sonido restaurado.", False)


@tool()
def set_brightness(percent: int) -> ToolResult:
    """Set the display brightness. `percent` is 0-100.

    Requires the Homebrew `brightness` CLI (`brew install brightness`).
    """
    try:
        macos.set_brightness(percent)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, str(exc), False)
    p = max(0, min(100, int(percent)))
    return ToolResult(True, {"brightness": p}, f"Brillo al {p} por ciento.", False)


@tool()
def sleep_display() -> ToolResult:
    """Put the display to sleep immediately."""
    try:
        macos.sleep_display()
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, str(exc), False)
    return ToolResult(True, None, "Apagando la pantalla.", False)


@tool()
def lock_screen() -> ToolResult:
    """Lock the screen (control+command+Q)."""
    try:
        macos.lock_screen()
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, str(exc), False)
    return ToolResult(True, None, "Bloqueando la pantalla.", False)


@tool()
def current_time(timezone: str | None = None) -> ToolResult:
    """Return the current time. Pass `timezone` as an IANA name
    ("Asia/Tokyo", "America/Mexico_City") to ask about a specific city.

    Omit `timezone` for the user's local time.
    """
    tz_name = timezone or settings.TIMEZONE
    try:
        tz = zoneinfo.ZoneInfo(tz_name) if tz_name else None
    except zoneinfo.ZoneInfoNotFoundError:
        return ToolResult(False, None, f"No conozco la zona horaria '{timezone}'.", False)
    now = dt.datetime.now(tz) if tz else dt.datetime.now().astimezone()
    pretty = now.strftime("%H:%M")
    label = tz_name or now.tzname() or "local"
    return ToolResult(
        True,
        {"iso": now.isoformat(), "timezone": label},
        f"Son las {pretty} en {label}.",
        False,
    )
