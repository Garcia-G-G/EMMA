"""macOS primitives for tools. One subprocess shell per call.

We use `osascript` for AppleScript/JXA and the `brightness` Homebrew CLI
for display brightness. The brightness binary is documented in the
project README as a prerequisite - it avoids pulling pyobjc/IOKit just
for one knob. If the binary is missing, the tool surfaces a clean error.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess

import structlog

log = structlog.get_logger("emma.actions.macos")


class AppleScriptError(RuntimeError):
    pass


def _run(cmd: list[str], *, input_text: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        log.error("subprocess_failed", cmd=cmd, stderr=proc.stderr.strip())
        raise AppleScriptError(f"{shlex.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_applescript(script: str) -> str:
    return _run(["osascript", "-e", script])


def osascript_jxa(script: str) -> str:
    return _run(["osascript", "-l", "JavaScript", "-e", script])


async def osascript(script: str, timeout_s: float = 15.0) -> str:
    """Run an AppleScript via ``osascript -e``, returning stdout.

    Async (non-blocking) counterpart to :func:`run_applescript`; the
    AppleScript-driven tools (Calendar/Mail/Notes/...) use this so they
    never block the Pipecat event loop. Raises :class:`AppleScriptError`
    on a non-zero exit or timeout.

    On timeout the error message carries the ``app_dialog_blocked`` marker:
    the usual cause is a destructive op (delete an event/note) triggering a
    macOS confirmation dialog that blocks osascript. Callers check for that
    marker and tell the user to authorize on screen instead of showing a
    generic failure. The app name isn't knowable at this layer.
    """
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        raise AppleScriptError(
            f"app_dialog_blocked: osascript timed out after {timeout_s}s "
            "(an app likely opened a confirmation dialog)"
        ) from None
    if proc.returncode != 0:
        raise AppleScriptError(stderr.decode("utf-8", errors="replace").strip())
    return stdout.decode("utf-8", errors="replace").strip()


async def osascript_or_friendly(
    script: str, timeout_s: float = 15.0, on_error: str = "No pude hacerlo"
) -> tuple[bool, str]:
    """Run :func:`osascript`; never raise. Returns ``(ok, output_or_message)``.

    On success: ``(True, stdout)``. On the ``app_dialog_blocked`` timeout
    marker: ``(False, <dialog guidance>)``. On any other AppleScript failure:
    ``(False, f"{on_error}: {msg}")`` — callers pass their own ``on_error``
    prefix (e.g. "No pude borrar la nota") so each tool keeps its specific
    message while the duplicated try/except + dialog handling lives here once.
    """
    try:
        out = await osascript(script, timeout_s=timeout_s)
        return True, out
    except AppleScriptError as exc:
        msg = str(exc)
        if "app_dialog_blocked" in msg:
            return False, "macOS me pidió confirmar en pantalla. Autorízalo y pídemelo otra vez."
        return False, f"{on_error}: {msg}"


def esc_applescript(s: str) -> str:
    """Escape a Python string for safe embedding in an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _process_name(app: str) -> str:
    """Map a display app name to its running-process name for ``pgrep -x``.

    Apple Music's app is "Music"/"Apple Music" but its process is "Music".
    """
    if app in ("Apple Music", "Music"):
        return "Music"
    return app


async def app_is_running(app: str) -> bool:
    """True if ``app`` is currently running (``pgrep -x`` on the process name).

    No new dependency — shells out to the system ``pgrep`` (Bug 19.2-B3).
    """
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pgrep",
        "-x",
        _process_name(app),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def launch_app(app: str, warmup_s: float = 1.5) -> None:
    """Launch ``app`` via ``open -a`` and wait ``warmup_s`` for it to register
    (so a follow-up AppleScript transport command lands on a live app). B3."""
    proc = await asyncio.create_subprocess_exec(
        "open",
        "-a",
        app,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(warmup_s)


def open_url(url: str) -> None:
    _run(["open", url])


def open_app(bundle_id_or_name: str) -> None:
    # `open -a` accepts either an app name or bundle id (with -b).
    if bundle_id_or_name.count(".") >= 2 and " " not in bundle_id_or_name:
        _run(["open", "-b", bundle_id_or_name])
    else:
        _run(["open", "-a", bundle_id_or_name])


def set_volume(percent: int) -> None:
    p = max(0, min(100, int(percent)))
    run_applescript(f"set volume output volume {p}")


def get_volume() -> int:
    out = run_applescript("output volume of (get volume settings)")
    try:
        return int(out)
    except ValueError:
        return 0


def set_mute(muted: bool) -> None:
    run_applescript(f"set volume output muted {'true' if muted else 'false'}")


def set_brightness(percent: int) -> None:
    if shutil.which("brightness") is None:
        raise AppleScriptError(
            "`brightness` CLI not found. Install with `brew install brightness`."
        )
    val = max(0.0, min(1.0, int(percent) / 100.0))
    _run(["brightness", f"{val:.3f}"])


def notify(title: str, body: str) -> None:
    # AppleScript needs literal quotes escaped; we use JSON-style escapes.
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    run_applescript(f'display notification "{_esc(body)}" with title "{_esc(title)}"')


def sleep_display() -> None:
    _run(["pmset", "displaysleepnow"])


def lock_screen() -> None:
    # Equivalent to ⌃⌘Q on modern macOS.
    run_applescript(
        'tell application "System Events" to keystroke "q" using {control down, command down}'
    )
