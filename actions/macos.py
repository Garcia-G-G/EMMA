"""macOS primitives for tools. One subprocess shell per call.

We use `osascript` for AppleScript/JXA and the `brightness` Homebrew CLI
for display brightness. The brightness binary is documented in the
project README as a prerequisite - it avoids pulling pyobjc/IOKit just
for one knob. If the binary is missing, the tool surfaces a clean error.
"""
from __future__ import annotations

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
