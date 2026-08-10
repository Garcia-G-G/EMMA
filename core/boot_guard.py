"""Backoff + notice rate-limiting across daemon restarts.

Two problems, one mechanism, because they are the same problem: the daemon has
no memory across boots, so anything it does on a failing boot it does *forever*
at launchd's restart cadence.

1. **The respawn loop.** ``KeepAlive{SuccessfulExit=false}`` means "restart
   whenever the exit was NOT successful" — every non-zero code, not some of
   them. The comments in the codebase said the opposite. With no
   ``ThrottleInterval`` that was a 10-second loop forever on any config failure.
   The plist now throttles to 30 s; this adds the second half — after enough
   consecutive identical failures, the daemon exits **0** so launchd leaves it
   down instead of retrying something only a human can fix.

2. **The spoken notice.** LAUNCH-2's preflight speaks when Accessibility is
   denied. Combined with an un-throttled respawn, a user who declines the
   prompt hears the same sentence on every restart, indefinitely — on precisely
   the install where they already said no. ``should_speak`` makes a notice
   at-most-once per interval across boots.

State lives in ``~/.emma/boot_state.json``. Every function is best-effort: if
the file cannot be read or written, the daemon behaves exactly as it did before
this module existed rather than failing to boot over its own bookkeeping.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("emma.boot_guard")

# Consecutive identical failures before we stop retrying. Four attempts at the
# plist's 30s ThrottleInterval is ~2 minutes of trying — long enough to ride out
# a transient (a Keychain still unlocking, a slow login), short enough that a
# genuinely broken config stops burning CPU and speaking.
_MAX_CONSECUTIVE = 4
# A failure older than this is not part of the current streak: the user probably
# fixed something and rebooted hours later.
_STREAK_WINDOW_S = 3600.0
# Default gap between repeats of the same spoken notice.
_SPEAK_INTERVAL_S = 6 * 3600.0


def _path() -> Path:
    home = Path(os.environ.get("EMMA_HOME") or (Path.home() / ".emma"))
    return home / "boot_state.json"


def _read() -> dict[str, Any]:
    try:
        p = _path()
        return json.loads(p.read_text()) if p.is_file() else {}
    except Exception:
        return {}


def _write(state: dict[str, Any]) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=1))
        os.chmod(p, 0o600)  # ~/.emma is 0700/0600 by convention (harden_local_files)
    except Exception as exc:
        log.debug("boot_state_unwritable", error=str(exc))


def record_failure(kind: str) -> int:
    """Note one failed boot of ``kind``; return the consecutive count."""
    state = _read()
    entry = state.get(kind) or {}
    now = time.time()
    last = float(entry.get("at") or 0.0)
    count = int(entry.get("count") or 0)
    count = count + 1 if (now - last) < _STREAK_WINDOW_S else 1
    state[kind] = {"count": count, "at": now}
    _write(state)
    return count


def clear(kind: str | None = None) -> None:
    """A boot got past the failing stage — forget the streak.

    Called on a successful boot so a user who fixes their config is not still
    being held down by yesterday's failures.
    """
    state = _read()
    if kind is None:
        # Forget every failure streak, but KEEP the ``said:`` speak-rate markers —
        # a good boot must not make a declined-permission notice start repeating.
        # (The old filter matched ``endswith("_boot")``, which no recorded kind
        # uses — "credentials"/"permissions"/"wake_model" — so it cleared nothing.)
        state = {k: v for k, v in state.items() if k.startswith("said:")}
    else:
        state.pop(kind, None)
    _write(state)


def should_stay_down(kind: str) -> bool:
    """True when this failure has repeated enough that retrying is pointless."""
    entry = _read().get(kind) or {}
    if (time.time() - float(entry.get("at") or 0.0)) >= _STREAK_WINDOW_S:
        return False
    return int(entry.get("count") or 0) >= _MAX_CONSECUTIVE


def should_speak(kind: str, interval_s: float = _SPEAK_INTERVAL_S) -> bool:
    """True at most once per ``interval_s`` for this notice, across restarts.

    Same discipline as the crash-report path's rate-limited Terminal auto-open
    (CLAUDE.md), rather than a second invented mechanism. Records the decision,
    so callers must only ask when they actually intend to speak.
    """
    state = _read()
    key = f"said:{kind}"
    last = float((state.get(key) or {}).get("at") or 0.0)
    now = time.time()
    if (now - last) < interval_s:
        return False
    state[key] = {"at": now}
    _write(state)
    return True
