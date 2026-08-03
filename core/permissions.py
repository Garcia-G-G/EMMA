"""macOS privacy permission probes.

macOS does not expose a reliable read-only check for TCC permissions, so
we probe by attempting the underlying operation and catching the
specific failure. On denial we ``say`` a short instruction and open the
relevant System Settings pane.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import structlog

log = structlog.get_logger("emma.permissions")


def harden_local_files() -> dict[str, int]:
    """Lock down ``~/.emma`` perms at startup (24.6-E5). Idempotent, best-effort.

    Personal + secret-adjacent data lives here; tighten the dir to 0700 and every
    file under it to 0600 so nothing is group/other-readable even if FileVault is
    off or the file was created with a looser umask. Returns {dir_fixed, files_fixed}.
    """
    home = Path.home() / ".emma"
    fixed = {"dirs": 0, "files": 0}
    # Never operate on a symlinked ~/.emma — a planted symlink must not turn this
    # into a chmod primitive against an arbitrary target (24.6 audit, HIGH).
    if not home.exists() or home.is_symlink():
        return fixed
    with contextlib.suppress(OSError):
        if (home.stat().st_mode & 0o777) != 0o700:
            os.chmod(home, 0o700)
            fixed["dirs"] += 1
    for p in home.rglob("*"):
        with contextlib.suppress(OSError):
            # chmod follows symlinks; skip them so a symlink inside ~/.emma can't
            # redirect the chmod onto ~/.ssh/id_ed25519 etc.
            if p.is_symlink():
                continue
            mode = p.stat().st_mode & 0o777
            if p.is_dir() and mode != 0o700:
                os.chmod(p, 0o700)
                fixed["dirs"] += 1
            elif p.is_file() and mode != 0o600:
                os.chmod(p, 0o600)
                fixed["files"] += 1
    if fixed["dirs"] or fixed["files"]:
        log.info("local_files_hardened", **fixed)
    return fixed

Pane = Literal[
    "Microphone", "Accessibility", "Automation", "AllFiles", "Calendars", "ScreenCapture"
]


def _say(text: str, *, voice: str = "Mónica") -> None:
    """Speak `text` via macOS `say`. BLOCKING — returns when speech ends.

    Bootstrap relies on this: each prompt should finish before the next so
    the Spanish phrases don't overlap.
    """
    try:
        subprocess.run(
            ["say", "-v", voice, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
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


def check_screen_recording() -> bool:
    """True if Screen Recording (needed for visual screen OCR) is granted.

    Uses CGPreflightScreenCaptureAccess — a non-prompting check. Degrades to
    False if Quartz is unavailable; visual screen reading simply won't work then.
    """
    try:
        import Quartz

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception as exc:
        log.warning("screen_recording_probe_failed", error=str(exc))
        return False


def check_accessibility_ax() -> bool:
    """True if THIS process is Accessibility-trusted (AXIsProcessTrusted).

    The only probe that answers the question screen vision actually asks. It
    reads the TCC decision for the running executable — no prompt, no
    subprocess, no proxy.

    Historical note, because it cost the project a whole feature: the probe
    that used to live under the name ``check_accessibility`` shelled out to
    ``osascript`` and asked System Events for a process list. That measures an
    *Automation* grant against System Events, which is a different permission
    entirely, and it returns True while AX is fully denied. Prompt 27 read
    "granted" off it, concluded no permission work was needed
    (``_planning/prompts/27-screen-vision-accessibility.md:11-13``), and shipped
    a screen-vision feature that could never work. The old probe still exists,
    correctly named, as ``check_system_events_automation``.
    """
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception as exc:
        log.warning("ax_trust_probe_failed", error=str(exc))
        return False


def request_accessibility_trust() -> bool:
    """Ask macOS for Accessibility trust, showing the system alert.

    THE call that makes Emma exist in System Settings → Privacy & Security →
    Accessibility. A process gets a row there only after calling
    ``AXIsProcessTrustedWithOptions`` with the prompt option — opening the pane
    without this shows the user a list Emma is simply not in, which is what
    Emma did for its whole history.

    Returns current trust (False on the first run: the alert is asynchronous
    and the user has not toggled anything yet).
    """
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception as exc:
        log.warning("ax_trust_request_failed", error=str(exc))
        return False


def request_screen_recording() -> bool:
    """Ask macOS for Screen Recording, showing the system alert.

    Same story as Accessibility: ``CGPreflightScreenCaptureAccess`` (what
    ``check_screen_recording`` reads) never creates the row —
    ``CGRequestScreenCaptureAccess`` does. Without it ``screencapture`` returns
    a desktop-only image with rc==0, so ``core/visual_screen.py`` cannot even
    tell that it failed.
    """
    try:
        import Quartz

        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception as exc:
        log.warning("screen_recording_request_failed", error=str(exc))
        return False


def check_system_events_automation() -> bool:
    """True if osascript may drive System Events (an *Automation* grant).

    Used by UI-scripting paths (``tools/safari_tool.bookmark_current`` and the
    app-control keystroke tools). Renamed from ``check_accessibility``, which
    is what it was never measuring — see ``check_accessibility_ax``.
    """
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
        log.warning("system_events_automation_probe_failed", error=str(exc))
        return False


def check_calendar() -> bool:
    """Probe EventKit calendar READ authorization (Prompt 24).

    EventKit (``tools/calendar_tool.py`` reads + the proactive engine) needs the
    Calendars TCC grant, which is a separate permission from Automation→Calendar
    (that one covers the AppleScript create/delete writes). True on
    authorized / fullAccess.
    """
    try:
        from actions import calendar_store

        return calendar_store.is_authorized()
    except Exception as exc:
        log.warning("calendar_probe_failed", error=str(exc))
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


def ax_smoke() -> tuple[bool, str]:
    """Does the Accessibility API actually return anything? (ok, reason)

    Measures the thing rather than a proxy. ``AXUIElementCreateApplication``
    succeeds without any permission — it only mints a handle — so the first
    call that can distinguish granted from denied is a real attribute read.

    The tell: ``NSWorkspace`` reports a frontmost app (that needs no
    permission) while ``frontmost_window()`` returns None (that needs AX).
    A Mac with a focused app always has a focused window; the asymmetry is the
    signature of a TCC denial, not of an empty desktop.

    Reasons: ``ok`` | ``ax_denied`` | ``no_frontmost_app`` (screen locked, or
    login window — inconclusive, not a denial) | ``probe_error:<x>``.
    """
    try:
        from AppKit import NSWorkspace

        from core import screen_vision

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return (True, "no_frontmost_app")
        if screen_vision.frontmost_window() is not None:
            return (True, "ok")
        return (False, "ax_denied")
    except Exception as exc:
        # An import/runtime failure is not evidence of a denial.
        return (True, f"probe_error:{exc}")


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

    # Accessibility: the real AX-trust probe, plus a functional smoke test.
    # Both, because they can disagree: AXIsProcessTrusted reads the TCC row,
    # the smoke test proves the API actually answers. A stale grant against a
    # replaced binary shows up as trusted-but-dead, and that is precisely the
    # failure that went unnoticed for the life of this feature.
    ax_trusted = check_accessibility_ax()
    smoke_ok, smoke_reason = ax_smoke()
    if not ax_trusted or not smoke_ok:
        log.error(
            "ax_denied",
            trusted=ax_trusted,
            smoke=smoke_reason,
            hint="screen vision returns 'no veo una ventana' until this is granted",
        )
        _say(
            "No tengo permiso de accesibilidad, así que no puedo leer la pantalla. "
            "Abre Configuración del Sistema, Privacidad y Seguridad, Accesibilidad, "
            "y activa Emma."
        )
        _open_settings("Accessibility")
        # Not fatal — everything that isn't screen vision still works.
    else:
        log.info("ax_ok", smoke=smoke_reason)

    if not check_screen_recording():
        # The look_at_screen fallback silently returns a blank desktop capture
        # without this, so it has to be visible.
        log.error("screen_recording_denied")
        _open_settings("ScreenCapture")

    if not check_system_events_automation():
        log.warning("system_events_automation_pending")

    if not check_automation():
        log.warning("automation_pending")
        # First-run prompt; not fatal.

    if not check_calendar():
        log.warning("calendar_unauthorized_or_pending")
        _open_settings("Calendars")
        # Not fatal — calendar reads degrade to a friendly "grant access" message.

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

# Data-model queries that exist on each app without needing a UI window open.
# These are the calls that cross the TCC Automation boundary and surface the
# permission dialog the first time. Every _AUTOMATION_APPS entry has one so the
# query is a real data-model probe rather than the UI-only "count windows"
# fallback (which does not reliably fire TCC for freshly-launched apps).
_AUTOMATION_QUERIES = {
    "Calendar": "count calendars",
    "Mail": "count accounts",
    "Messages": "count services",
    "Notes": "count folders",
    "Reminders": "count lists",
    "Safari": "count tabs of windows",  # works even with no open window: returns 0
    "Finder": "count items of (path to home folder)",
    "Music": "count playlists",
    "Terminal": "count windows",
}

# After _say returns (speech finished), give the user this long to actually
# click Allow on the dialog before triggering the next app's ping.
_DWELL_AFTER_DIALOG_S = 4.0

# Panes the user toggles by hand. Each entry is (pane, spoken instruction,
# requester) where `requester` is the API that makes macOS CREATE the row —
# without it the pane opens on a list Emma is not in, and there is nothing to
# toggle. That was the bug: Accessibility and Screen Recording were "requested"
# by opening a Settings pane and hoping. Only Full Disk Access and Calendars
# genuinely have no request API (the first is toggle-only; the second is
# requested by EventKit on first real use via actions/calendar_store).
_MANUAL_PANES: tuple[tuple[Pane, str, Callable[[], bool] | None], ...] = (
    (
        "Accessibility",
        "Necesito permiso de accesibilidad para leer la pantalla cuando me lo pidas. "
        "Acepta la alerta y activa Emma en la lista.",
        request_accessibility_trust,
    ),
    (
        "AllFiles",
        "Necesito acceso a tu disco para leer mensajes y correos cuando me lo pidas.",
        None,
    ),
    (
        "ScreenCapture",
        "Necesito permiso de Grabación de pantalla para leer la pantalla con visión "
        "(captura + OCR local) cuando me lo pidas.",
        request_screen_recording,
    ),
    (
        "Calendars",
        "Necesito acceso a Calendarios para leer tu agenda. Actívalo para Emma en "
        "Privacidad y Seguridad, Calendarios.",
        None,
    ),
)


async def _ping_automation(app: str) -> tuple[str, str]:
    """Trigger the Automation permission dialog for `app`.

    Strategy:
    1. `launch application "X"` to make sure the app is running (this alone
       does not trigger TCC, but ensures the subsequent target exists).
    2. `tell application "X" to count <data_model_thing>` — this is what
       actually crosses the TCC boundary and surfaces the dialog the first
       time. Subsequent runs return immediately.

    Returns (app, status) where status is one of:
      'dialog_shown'    — script executed cleanly (user saw a dialog or
                          permission was already granted)
      'not_running'     — could not launch the app (-600 persists)
      'denied'          — explicit denial (-1743)
      'error:<code>'    — anything else
      'timeout'         — script didn't return in time
    """
    # Stage 1: launch (no-op if already running). Does not need permission.
    launch_script = f'launch application "{app}"'
    launch_proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript",
        "-e",
        launch_script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(launch_proc.communicate(), timeout=5.0)
    except TimeoutError:
        launch_proc.kill()
        return (app, "timeout")

    # Stage 2: data-model query (per-app, because each app exposes different things).
    query = _AUTOMATION_QUERIES.get(app, "count windows")
    script = f'tell application "{app}" to {query}'
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError:
        proc.kill()
        return (app, "timeout")

    if proc.returncode == 0:
        return (app, "dialog_shown")
    err = stderr.decode("utf-8", errors="replace")
    if "-1743" in err:
        return (app, "denied")
    if "-600" in err:
        return (app, "not_running")
    return (app, f"error:{proc.returncode}")


async def _ping_microphone() -> bool:
    """Trigger the Microphone permission dialog with a short capture probe."""
    return check_microphone()  # reuse the existing probe


async def bootstrap() -> dict[str, Any]:
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
        _, status = await _ping_automation(app)
        results[f"Automation:{app}"] = status
        # _say already blocked until the phrase ended; now give the user time
        # to actually click Allow before the next app's ping fires.
        await asyncio.sleep(_DWELL_AFTER_DIALOG_S)

    # 3. Manual panes (Accessibility, Screen Recording, Full Disk Access, Calendars)
    for pane, instruction, requester in _MANUAL_PANES:
        print(f"→ Manual: {pane}")
        _say(instruction)
        if requester is not None:
            # Fire the API that CREATES the row before opening the pane. macOS
            # shows its own "wants to control this computer" alert here; the
            # return is the state *before* the user answers, so False on a
            # first run is expected, not a failure.
            granted = requester()
            results[pane] = "granted" if granted else "requested"
            print(f"   {'ya estaba concedido' if granted else 'alerta mostrada'}")
        else:
            results[pane] = "settings_opened"
        _open_settings(pane)
        await asyncio.sleep(6)  # manual panes need a longer dwell to interact

    # Re-probe the two we can actually read back, so the recap reports the
    # user's answer rather than what we asked for.
    results["Accessibility"] = "granted" if check_accessibility_ax() else results["Accessibility"]
    results["ScreenCapture"] = (
        "granted" if check_screen_recording() else results["ScreenCapture"]
    )

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
