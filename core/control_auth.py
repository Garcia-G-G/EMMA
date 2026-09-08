"""Shared secret for Emma's loopback control sockets (LAUNCH-11 Part 2).

``dashboard/server.py`` guarded ``/control`` with an Origin allowlist that
**explicitly accepts any client sending no Origin header**, because the native
UI is such a client. That stops a foreign *web page* from hijacking the socket
(CSWSH), which is real and worth keeping — but it was never authentication.
Every non-browser process on the machine passed it, and through it could read
200 rows of personal memory, unmute a deliberately muted microphone, unpair the
Mac, and shut Emma down (audit P1-13).

The token closes that. The daemon mints one, hands it to the UI process it spawns
through the environment, and the served page receives it in the URL it is opened
with — never in the page body, because any local process can fetch
``http://127.0.0.1:3200/`` and read whatever is in there.

It is per-INSTALLATION, not per-boot, and that is deliberate. Against the actual
threat — a process that can read a 0600 file in this user's home — rotation buys
nothing, while a token that changed under a running page or a still-live UI would
strand it with no way to reconnect. Stability is worth more than the theater.

**The honest limit.** The fallback file is mode 0600, so this raises the bar from
"any local process" to "any process running as this user that reads a file in
their home directory" — the same bar that already protects ``~/.emma/memory.db``.
macOS gives no per-process isolation between programs run by one user, so that
is the ceiling available here, and claiming more would be dishonest. What it does
buy: a sandboxed or differently-owned process, and every piece of software that
merely opens a loopback socket without going looking for credentials, is now shut
out of a channel that was wide open to all of them.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import structlog

log = structlog.get_logger("emma.control_auth")

_TOKEN: str | None = None


def _token_path() -> Path:
    home = Path(os.environ.get("EMMA_HOME") or (Path.home() / ".emma")).expanduser()
    return home / "control_token"


def token() -> str:
    """This installation's control-channel token. Memoized per process.

    Resolution order:

    1. ``EMMA_CONTROL_TOKEN`` — how the daemon hands it to the UI it spawns. A
       child must use the parent's token, never mint a second one, or the two
       would disagree and every control command would fail closed.
    2. The 0600 file — lets a UI whose environment lost the variable recover, and
       lets a standalone ``python dashboard/server.py`` be reachable in dev.
    3. Mint a fresh one and write it.
    """
    global _TOKEN
    if _TOKEN:
        return _TOKEN

    injected = os.environ.get("EMMA_CONTROL_TOKEN", "").strip()
    if injected:
        _TOKEN = injected
        return _TOKEN

    path = _token_path()
    try:
        if path.is_file():
            existing = path.read_text().strip()
            if existing:
                _TOKEN = existing
                return _TOKEN
    except Exception as exc:
        log.warning("control_token_unreadable", error=str(exc))

    minted = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the start. Writing then chmod-ing leaves a window
        # where the token is world-readable, which is the whole thing we are
        # trying to prevent.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, minted.encode())
        finally:
            os.close(fd)
        os.chmod(path, 0o600)  # in case the file pre-existed with looser modes
    except Exception as exc:
        # An unwritable home must not stop Emma from booting. The token still
        # works for this process and the child it spawns via the environment;
        # only the recover-from-file path is lost.
        log.warning("control_token_unwritable", error=str(exc))

    _TOKEN = minted
    return _TOKEN


def matches(candidate: str | None) -> bool:
    """Constant-time comparison against this process's token."""
    if not candidate:
        return False
    return secrets.compare_digest(candidate, token())


def reset_for_tests() -> None:
    """Forget the memoized token. Tests only."""
    global _TOKEN
    _TOKEN = None
