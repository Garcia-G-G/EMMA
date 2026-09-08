"""What code is this daemon actually running?

This module exists because of a two-investigation bug. A clean install exited 2
on every boot; the audit proved the managed-mode exemption was present by
running ``git merge-base --is-ancestor 06b6f8d public/main`` — which verified
the **repo** and said nothing about the **installation**. The installed
``~/.emma/src`` was a months-old release tarball that simply did not contain the
exemption. Old code doing what old code did.

The distinction between "the code I test" and "the code that is running" was
invisible, so nobody thought to check it. That is what this fixes: the daemon
states, at boot and on demand, exactly which source it is running.

Three signals, cheapest first, none of which need the network:

- ``stamp`` — what the installer recorded (``~/.emma/install.json``: the ref it
  fetched, the commit, when). Authoritative when present.
- ``git`` — ``git describe`` when the source is a checkout (dev machines).
- ``fingerprint`` — a content hash of the shipped Python. Always available, even
  for a tarball with no stamp and no ``.git``, and it is the signal that would
  have caught the original bug: two machines running the same code share a
  fingerprint, and a stale one differs from the repo's.

``upstream_head()`` adds a network comparison for ``emma.permissions check``.
It is never called at boot — a daemon must not need GitHub to start.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("emma.version")

# Files whose content defines "which Emma is this". Deliberately a small, stable
# set: the boot path, the managed-mode gate, and the orchestrator — the three
# places the stale-tarball bug actually lived. Hashing the whole tree would make
# the fingerprint churn on every doc edit and stop meaning anything.
_FINGERPRINT_FILES = (
    "emma/__main__.py",
    "core/orchestrator.py",
    "config/settings.py",
    "core/pairing.py",
)

_REPO = "theemmafamily/emma"
_BRANCH = "main"


def source_dir() -> Path:
    """The directory the running code was imported from."""
    return Path(__file__).resolve().parent.parent


def _stamp() -> dict[str, Any] | None:
    """What the installer recorded, if it recorded anything."""
    home = Path(os.environ.get("EMMA_HOME") or (Path.home() / ".emma"))
    path = home / "install.json"
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.debug("install_stamp_unreadable", error=str(exc))
        return None


def _git_describe() -> str | None:
    """`git describe` when the source is a checkout. None for a tarball."""
    root = source_dir()
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/git", "-C", str(root), "describe", "--always", "--dirty", "--tags"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def fingerprint() -> str:
    """Short content hash of the shipped Python. Always available.

    The one signal a tarball install cannot lose. Missing files hash as empty
    rather than raising, so a partial install still produces a comparable value
    instead of no value.
    """
    h = hashlib.sha256()
    root = source_dir()
    for rel in _FINGERPRINT_FILES:
        h.update(rel.encode())
        try:
            h.update((root / rel).read_bytes())
        except Exception:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def report() -> dict[str, Any]:
    """Everything known locally about which code is running. Never raises."""
    stamp = _stamp() or {}
    return {
        "fingerprint": fingerprint(),
        "commit": stamp.get("commit") or _git_describe(),
        "ref": stamp.get("ref"),
        "installed_at": stamp.get("installed_at"),
        "channel": stamp.get("channel"),
        "source": str(source_dir()),
        "is_checkout": (source_dir() / ".git").exists(),
    }


def boot_line() -> dict[str, Any]:
    """The subset worth logging on every boot — small, and no network.

    Carries the TIER as well as the code (LAUNCH-11). With two supported tiers
    the support question "it doesn't work" has two completely different answers,
    and the first thing anyone reading a log needs to know is which daemon this
    is: a BYOK one that never talks to the backend, or a managed one that does
    nothing else.
    """
    r = report()
    out = {k: r[k] for k in ("fingerprint", "commit", "installed_at", "source") if r[k]}
    try:
        from config.settings import settings

        out["mode"] = settings.mode()
    except Exception as exc:  # a boot line must never be the thing that fails a boot
        log.debug("boot_line_mode_unavailable", error=str(exc))
    return out


async def upstream_head(timeout_s: float = 6.0) -> str | None:
    """The commit the release channel is currently serving, or None.

    Best-effort and explicitly opt-in: called by ``emma.permissions check`` and
    the app's Cuenta panel, NEVER at boot. A daemon that needs GitHub reachable
    in order to start is a worse daemon.
    """
    try:
        import httpx

        url = f"https://api.github.com/repos/{_REPO}/commits/{_BRANCH}"
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.get(url, headers={"Accept": "application/vnd.github.sha"})
            if r.status_code != 200:
                return None
            sha = r.text.strip()
            return sha[:12] if sha else None
    except Exception as exc:
        log.debug("upstream_head_failed", error=str(exc))
        return None


async def staleness(timeout_s: float = 6.0) -> dict[str, Any]:
    """Local version + the upstream head + a verdict.

    ``stale`` is True / False / None, and None genuinely means "couldn't tell"
    (offline, or a tarball with no recorded commit) rather than "fine". Saying
    "up to date" when we don't know is exactly the failure this module exists
    to prevent.
    """
    local = report()
    head = await upstream_head(timeout_s)
    installed = (local.get("commit") or "").lstrip("g")
    if head is None or not installed:
        verdict: bool | None = None
    else:
        verdict = not (installed.startswith(head[:7]) or head.startswith(installed[:7]))
    return {**local, "upstream": head, "stale": verdict}
