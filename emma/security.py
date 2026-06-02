"""CLI for Emma's security layer: Keychain provisioning + audit.

    python -m emma.security bootstrap   # store sentinel + migrate .env credentials
    python -m emma.security verify      # readback every credential field from Keychain
    python -m emma.security audit       # list Keychain labels + recent vault_ref rows
    python -m emma.security wipe-all     # delete every Emma Keychain item (destructive)

`bootstrap` is run by installer step 7.6 so credentials move out of plaintext
`.env` and into Keychain at install time.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from core import secrets

_REPO_ROOT = Path(__file__).resolve().parent.parent


async def _bootstrap() -> None:
    # Sentinel lets us later detect a wiped Keychain (master_present missing).
    await secrets.store("emma.master_present", "1", kind="sentinel")
    env = _REPO_ROOT / ".env"
    if env.exists():
        result = await secrets.bootstrap_from_env(env)
        print(f"migrated to Keychain: {result['moved']}")
        print(f"left in .env (non-credential): {result['skipped']}")
    else:
        print("no .env found; stored sentinel only")


async def _audit() -> None:
    labels = await secrets.list_labels()
    print(f"Keychain labels under {secrets.SERVICE}:")
    for label in labels:
        print(f"  {label}")
    try:
        from memory.long_term import _connect

        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, vault_ref FROM facts WHERE vault_ref IS NOT NULL "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
        print("recent vault_ref rows in memory.db:")
        for r in rows:
            print(f"  id={r['id']} vault_ref={r['vault_ref']}")
        if not rows:
            print("  (none)")
    except Exception as exc:
        print(f"  (memory.db unavailable: {exc})")
    print("log redaction: wired into structlog (emma.__main__).")


async def _verify() -> int:
    """Read back every credential field from Keychain. Exit 1 if any missing.

    Prints a field/status/length table. Only the length of each value is shown,
    never the value itself. This is the post-migration health check for Bug 1:
    a credential blanked in .env but absent here means a lost secret.
    """
    from config.settings import _CREDENTIAL_FIELDS

    print(f"{'field':<24} {'status':<10} length")
    all_present = True
    for field in _CREDENTIAL_FIELDS:
        value = await secrets.retrieve(field)
        if value:
            print(f"{field:<24} {'present':<10} {len(value)}")
        else:
            all_present = False
            print(f"{field:<24} {'MISSING':<10} 0")
    return 0 if all_present else 1


async def _wipe_all() -> None:
    labels = await secrets.list_labels()
    deleted = 0
    for label in labels:
        if await secrets.delete(label):
            deleted += 1
    print(f"deleted {deleted} Keychain item(s) under {secrets.SERVICE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emma.security", description="Emma Keychain provisioning + audit."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Store sentinel + migrate .env credentials to Keychain.")
    sub.add_parser(
        "verify", help="Read back every credential field from Keychain (exit 1 if any missing)."
    )
    sub.add_parser("audit", help="List Keychain labels + recent vault_ref rows.")
    sub.add_parser("wipe-all", help="Delete every Emma Keychain item (DESTRUCTIVE).")
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        asyncio.run(_bootstrap())
    elif args.command == "verify":
        return asyncio.run(_verify())
    elif args.command == "audit":
        asyncio.run(_audit())
    elif args.command == "wipe-all":
        asyncio.run(_wipe_all())
    else:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
