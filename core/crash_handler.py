"""Top-level crash handler with Terminal handoff.

On an unhandled exception we:

1. Write ``~/Library/Logs/Emma/crashes/crash_YYYYMMDD_HHMMSS.md``.
2. If fewer than 3 crashes in the last 60 s, open a Terminal at the repo
   root and ``cat`` the report so the user sees it instantly.
3. Speak the failure via the system ``say`` command (not ElevenLabs -
   TTS may be the thing that broke).

Always exits 1 so launchd's ``KeepAlive.Crashed = true`` restarts us.
"""
from __future__ import annotations

import json
import platform
import shlex
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / "Library/Logs/Emma"
CRASH_DIR = LOG_DIR / "crashes"
RECENT_FILE = CRASH_DIR / "_recent.json"

RATE_LIMIT_COUNT = 3
RATE_LIMIT_WINDOW_S = 60
LOG_TAIL_LINES = 50


def _recent_crashes() -> list[float]:
    if not RECENT_FILE.exists():
        return []
    try:
        data = json.loads(RECENT_FILE.read_text())
        return [float(t) for t in data]
    except Exception:
        return []


def _record_crash(ts: float) -> None:
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = ts - RATE_LIMIT_WINDOW_S
    recent = [t for t in _recent_crashes() if t >= cutoff]
    recent.append(ts)
    RECENT_FILE.write_text(json.dumps(recent[-10:]))


def _should_open_terminal(ts: float) -> bool:
    cutoff = ts - RATE_LIMIT_WINDOW_S
    # _recent_crashes() reflects state BEFORE this crash; if 3+ are already
    # within the window, suppress the auto-open for this one too.
    return len([t for t in _recent_crashes() if t >= cutoff]) < RATE_LIMIT_COUNT


def _tail_log(n: int = LOG_TAIL_LINES) -> str:
    log = LOG_DIR / "emma.log"
    if not log.exists():
        return "<no emma.log yet>"
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-n:])
    except Exception as exc:
        return f"<failed to read log: {exc}>"


def _format_report(exc: BaseException, context: dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc).astimezone()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"""# Emma crash report

- **When:** {ts.isoformat()}
- **Emma:** 0.1.0 (phase 04)
- **Python:** {sys.version.split()[0]}
- **macOS:** {platform.mac_ver()[0]} ({platform.machine()})

## Exception

```
{type(exc).__name__}: {exc}
```

## Traceback

```
{tb}```

## In-flight context

- **turn_id:** {context.get("turn_id") or "<none>"}
- **last user transcript:** {context.get("last_transcript") or "<none>"}
- **in-progress response:** {context.get("response_text") or "<none>"}
- **tool calls in flight:** {context.get("tool_calls") or []}

## Last {LOG_TAIL_LINES} log lines

```
{_tail_log()}
```
"""


def _write_report(exc: BaseException, context: dict[str, Any]) -> Path:
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = CRASH_DIR / f"crash_{stamp}.md"
    path.write_text(_format_report(exc, context))
    return path


def _open_terminal(report: Path, repo_root: Path) -> None:
    cmd = f"cd {shlex.quote(str(repo_root))} && clear && cat {shlex.quote(str(report))}"
    script = (
        f'tell application "Terminal" to activate\n'
        f'tell application "Terminal" to do script "{cmd}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass


def _say_failure() -> None:
    try:
        subprocess.Popen(
            ["say", "-v", "Mónica", "Tuve un error, dejé los detalles en una terminal."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def handle_crash(
    exc: BaseException,
    context: dict[str, Any],
    repo_root: Path,
) -> int:
    """Write report, optionally open Terminal + say, return exit code."""
    now = time.time()
    open_term = _should_open_terminal(now)
    report = _write_report(exc, context)
    _record_crash(now)
    if open_term:
        _open_terminal(report, repo_root)
        _say_failure()
    return 1
