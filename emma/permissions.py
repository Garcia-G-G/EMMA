"""CLI for Emma's macOS permissions.

    python -m emma.permissions bootstrap   # interactive install-time walkthrough (shows dialogs)
    python -m emma.permissions check       # probe current state, no UI changes

`bootstrap` is what installer/install_macos.sh runs at install time so every
TCC permission is requested upfront instead of surfacing mid-conversation.
"""

from __future__ import annotations

import argparse
import asyncio

from core import permissions


def _check() -> int:
    """Probe each permission and print its state. Always exits 0."""
    probes = {
        "Microphone": permissions.check_microphone,
        "Accessibility": permissions.check_accessibility,
        "Automation": permissions.check_automation,
    }
    for name, fn in probes.items():
        try:
            status = "granted" if fn() else "denied_or_inconclusive"
        except Exception as exc:  # never fail the check command
            status = f"error: {exc}"
        print(f"{name}: {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emma.permissions",
        description="Request or check Emma's macOS permissions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "bootstrap",
        help="Interactive install-time permission walkthrough (speaks + shows dialogs).",
    )
    sub.add_parser(
        "check",
        help="Probe current permission state without changing anything.",
    )
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        asyncio.run(permissions.bootstrap())
        return 0
    if args.command == "check":
        return _check()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
