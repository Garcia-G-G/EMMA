"""macOS Keychain wrapper — the Secret trust tier.

Secret-tier values (passwords, API keys, account numbers, government IDs)
live ONLY in the login Keychain under the ``com.garcia.emma`` service.
They never touch ``memory.db``, logs, or the system prompt. ``memory.db``
may carry a ``vault_ref`` label, never the value.

We shell out to the macOS ``security`` CLI (no new Python deps). The
service name is read from the ``EMMA_KEYCHAIN_SERVICE`` env var (the same
var pydantic ``settings`` exposes) rather than importing ``settings`` —
``settings`` imports :func:`retrieve_sync` from here, so importing it back
would be circular.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date
from pathlib import Path

import structlog

log = structlog.get_logger("emma.secrets")

SERVICE = os.environ.get("EMMA_KEYCHAIN_SERVICE") or "com.garcia.emma"
_TIMEOUT_S = 5.0

# Credential env-var heuristics for .env migration. ``_WEBHOOK`` covers Discord
# channel webhook URLs (Prompt 26) — the URL itself grants posting rights, so it
# is secret-tier and must land in Keychain, never .env or memory.db. (X_BEARER_
# TOKEN / LINKEDIN_ACCESS_TOKEN are already caught by ``_TOKEN``.)
_CRED_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_DSN", "_WEBHOOK")


async def _run(args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/security",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise RuntimeError("security CLI timed out") from None
    assert proc.returncode is not None  # set after communicate() returns
    return (
        proc.returncode,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def store(label: str, value: str, kind: str = "secret") -> None:
    """Store (or update, via -U) a secret under `label`. Never logs `value`.

    Raises on any non-zero exit from the `security` CLI. The success log is
    emitted only AFTER the rc check, so a failed store never logs success.
    The exception text never includes `value` (it is secret).
    """
    rc, _out, err = await _run(
        ["add-generic-password", "-s", SERVICE, "-a", label, "-D", kind, "-w", value, "-U"]
    )
    if rc != 0:
        raise RuntimeError(f"keychain store failed for {label} (rc={rc}): {err.strip()}")
    log.info("secret_stored", label=label, kind=kind, value="<hidden>")


async def retrieve(label: str) -> str | None:
    """Return the stored value for `label`, or None if absent. Never logs the value."""
    rc, out, _err = await _run(["find-generic-password", "-s", SERVICE, "-a", label, "-w"])
    log.info("secret_read", label_read=label)
    if rc != 0:
        return None
    return out.rstrip("\n")


async def delete(label: str) -> bool:
    """Delete the secret under `label`. Returns True if something was deleted."""
    rc, _out, _err = await _run(["delete-generic-password", "-s", SERVICE, "-a", label])
    log.info("secret_deleted", label=label, ok=rc == 0)
    return rc == 0


async def has(label: str) -> bool:
    return (await retrieve(label)) is not None


async def list_labels() -> list[str]:
    """List account labels stored under our service.

    Parses ``security dump-keychain`` attribute metadata (NOT ``-d``, which
    would dump password *data* and prompt per item). Returns [] on failure.
    """
    rc, out, _err = await _run(["dump-keychain"])
    if rc != 0:
        return []
    labels: list[str] = []
    # Items are delimited by "keychain: ..." headers; within a block we want
    # the acct when svce == our SERVICE.
    for block in re.split(r"^keychain: ", out, flags=re.MULTILINE):
        if f'"svce"<blob>="{SERVICE}"' not in block:
            continue
        m = re.search(r'"acct"<blob>="([^"]*)"', block)
        if m:
            labels.append(m.group(1))
    return sorted(set(labels))


def retrieve_sync(label: str) -> str | None:
    """Synchronous retrieve for pre-event-loop callers (settings init).

    Returns None if an event loop is already running (cannot block) or on error.
    """
    try:
        asyncio.get_running_loop()
        return None  # inside a running loop; caller must use async retrieve
    except RuntimeError:
        pass
    try:
        return asyncio.run(retrieve(label))
    except Exception as exc:
        log.warning("retrieve_sync_failed", label=label, error=str(exc))
        return None


def _is_credential(key: str, value: str) -> bool:
    if key.endswith(_CRED_SUFFIXES):
        return True
    # high-entropy / API-key-shaped value heuristic (lazy import to avoid cycle)
    from core.redaction import looks_like_api_key

    return looks_like_api_key(value)


async def bootstrap_from_env(env_path: Path) -> dict[str, list[str]]:
    """Move credentials from `.env` to Keychain (one-way). Returns {moved, skipped}.

    A line is a credential if its key matches *_KEY/_TOKEN/_SECRET/_PASSWORD/_DSN
    or its value looks like an API key. Migrated lines are kept in the file but
    blanked, with a ``# moved to Keychain on <date>`` comment, so the settings
    loader still parses.

    Data-safety contract (Prompt 15.9 Bug 1): a credential's ``.env`` line is
    blanked ONLY after the stored value is read back from Keychain and matches.
    If any store or readback fails, the original ``.env`` is restored verbatim
    and a ``RuntimeError`` is raised — never a half-migrated split where the
    value is gone from both ``.env`` and Keychain. The final write happens once,
    after every credential has been verified, so the file is all-or-nothing.
    """
    original_text = env_path.read_text()
    moved: list[str] = []
    skipped: list[str] = []
    out_lines: list[str] = []
    stamp = date.today().isoformat()

    for raw in original_text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and _is_credential(key, value):
            try:
                await store(key, value, kind="env_credential")
                readback = await retrieve(key)
            except Exception:
                env_path.write_text(original_text)  # restore (it was untouched)
                log.error("bootstrap_store_failed", field=key)
                raise
            if readback != value:
                env_path.write_text(original_text)  # leave .env intact
                log.error("bootstrap_readback_failed", field=key)
                raise RuntimeError(
                    f"Keychain readback mismatch for {key}; .env left intact (no value blanked)."
                )
            moved.append(key)
            out_lines.append(f"{key}=  # moved to Keychain on {stamp}")
        else:
            skipped.append(key)
            out_lines.append(line)

    env_path.write_text("\n".join(out_lines) + "\n")
    log.info("env_bootstrap", moved=moved, skipped=skipped)
    return {"moved": moved, "skipped": skipped}
