"""macOS privacy permission probes.

macOS does not expose a reliable read-only check for TCC permissions, so
we probe by attempting the underlying operation and catching the
specific failure. On denial we ``say`` a short instruction and open the
relevant System Settings pane.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from typing import Literal

import structlog

log = structlog.get_logger("emma.permissions")

Pane = Literal["Microphone", "Accessibility", "Automation", "AllFiles"]


def _say(text: str) -> None:
    try:
        subprocess.Popen(
            ["say", "-v", "Mónica", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error("say_failed", error=str(exc))


def _open_settings(pane: Pane) -> None:
    url = f"x-apple.systempreferences:com.apple.preference.security?Privacy_{pane}"
    with contextlib.suppress(Exception):
        subprocess.run(["open", url], check=False, timeout=3)


def check_microphone() -> bool:
    """Probe the default input device. Returns True if accessible.

    Runs the open/start/stop/close cycle in a worker thread with a hard
    timeout - on macOS, after a hard kill of the previous Emma process,
    CoreAudio's HAL can hold an internal mutex that makes ``stream.stop``
    or ``stream.close`` block indefinitely. We treat that as a transient
    "probe inconclusive, continue boot" rather than a denial; Realtime
    will retry the mic when the session actually opens.
    """
    import threading

    result: dict[str, object] = {"ok": False, "error": None}

    def _probe() -> None:
        try:
            import sounddevice as sd

            stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=512)
            stream.start()
            stream.stop()
            stream.close()
            result["ok"] = True
        except Exception as exc:
            result["error"] = str(exc)

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout=3.0)
    if t.is_alive():
        log.warning(
            "mic_probe_timeout",
            hint="coreaudio mutex; previous SIGKILL may have left HAL state stale",
        )
        # Optimistic: assume permission is granted (we can't tell while
        # the HAL is locked) and let the Realtime mic stream surface a
        # real error if there is one.
        return True
    if not result["ok"]:
        log.warning("mic_probe_failed", error=str(result.get("error")))
        return False
    return True


def check_accessibility() -> bool:
    """Probe an AppleScript that needs Accessibility access."""
    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception as exc:
        log.warning("accessibility_probe_failed", error=str(exc))
        return False


def check_automation() -> bool:
    """Probe a no-op AppleScript on Finder. First call surfaces the prompt."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get name'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception as exc:
        log.warning("automation_probe_failed", error=str(exc))
        return False


def preflight() -> bool:
    """Run all permission checks at startup. Returns True if Emma can proceed."""
    proceed = True

    if not check_microphone():
        proceed = False
        log.error("microphone_denied")
        _say(
            "No tengo permiso para usar el micrófono. Abre Configuración del Sistema, "
            "Privacidad y Seguridad, Micrófono, y activa Emma."
        )
        _open_settings("Microphone")

    if not check_accessibility():
        log.warning("accessibility_denied_or_pending")
        _open_settings("Accessibility")
        # Not fatal - many tools still work.

    if not check_automation():
        log.warning("automation_pending")
        # First-run prompt; not fatal.

    return proceed


# === Bootstrap (install-time) =================================================

# Apps the AppleScript tools control. Must stay in sync with tools/*.
# The first 7 come from tools/{calendar,mail,messages,notes,reminders,safari,
# finder}_tool.py; "Music" is controlled by tools/music.py and "Terminal" by
# tools/dev.py (dev-mode resume window) — both surfaced in the pre-flight audit.
_AUTOMATION_APPS = (
    "Calendar",
    "Mail",
    "Messages",
    "Notes",
    "Reminders",
    "Safari",
    "Finder",
    "Music",
    "Terminal",
)

# Permissions Apple does not let us trigger programmatically.
# We open the Settings pane and speak instructions.
_MANUAL_PANES: tuple[tuple[Pane, str], ...] = (
    ("Accessibility", "Necesito permiso de accesibilidad para leer la pantalla cuando me lo pidas."),
    ("AllFiles", "Necesito acceso a tu disco para leer mensajes y correos cuando me lo pidas."),
)


async def _ping_automation(app: str) -> tuple[str, bool]:
    """Trigger the Automation permission dialog for `app` via a minimal AppleScript.

    macOS does not expose the user's choice; we treat a clean execution as
    'dialog shown, user responded somehow.' Returns (app, executed_without_error).
    """
    script = f'tell application "{app}" to count windows'
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError:
        proc.kill()
        return (app, False)
    return (app, proc.returncode == 0)


async def _ping_microphone() -> bool:
    """Trigger the Microphone permission dialog with a short capture probe."""
    return check_microphone()  # reuse the existing probe


async def bootstrap() -> dict:
    """Interactive install-time permission walkthrough.

    Prints headers, speaks one-line context in Spanish, triggers each system
    dialog (or opens Settings for manual panes). Returns a dict of
    {permission: status} for the final report.
    """
    results: dict[str, str] = {}

    print("\n=== Permisos de macOS ===")
    print("Voy a abrir cada diálogo de permisos. Dale Allow a cada uno.\n")

    # 1. Microphone
    print("→ Micrófono")
    _say("Primero, micrófono. Dale Allow.")
    mic_ok = await _ping_microphone()
    results["Microphone"] = "granted" if mic_ok else "denied_or_pending"
    await asyncio.sleep(2)

    # 2. Automation (one prompt per app)
    for app in _AUTOMATION_APPS:
        print(f"→ Automation: {app}")
        _say(f"Permiso para controlar {app}. Dale Allow.")
        await _ping_automation(app)
        results[f"Automation:{app}"] = "dialog_shown"
        await asyncio.sleep(2)

    # 3. Manual panes (Accessibility, Full Disk Access)
    for pane, instruction in _MANUAL_PANES:
        print(f"→ Manual: {pane}")
        _say(instruction)
        _open_settings(pane)
        results[pane] = "settings_opened"
        await asyncio.sleep(4)  # give the user time to interact

    # Recap
    print("\n=== Resumen ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    print(
        "\nSi te perdiste algún diálogo, abre Configuración del Sistema → "
        "Privacidad y Seguridad y autoriza manualmente.\n"
    )
    _say("Listo, permisos pedidos.")
    return results
