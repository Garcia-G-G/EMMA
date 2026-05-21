"""macOS privacy permission probes.

macOS does not expose a reliable read-only check for TCC permissions, so
we probe by attempting the underlying operation and catching the
specific failure. On denial we ``say`` a short instruction and open the
relevant System Settings pane.
"""
from __future__ import annotations

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
    try:
        subprocess.run(["open", url], check=False, timeout=3)
    except Exception:
        pass


def check_microphone() -> bool:
    """Probe the default input device. Returns True if accessible."""
    try:
        import sounddevice as sd

        # Construct without start to avoid pulling in samples; opening alone is
        # enough to trigger or surface the TCC prompt.
        stream = sd.RawInputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=512
        )
        stream.start()
        stream.stop()
        stream.close()
        return True
    except Exception as exc:
        log.warning("mic_probe_failed", error=str(exc))
        return False


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
