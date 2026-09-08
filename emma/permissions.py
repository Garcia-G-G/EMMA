"""CLI for Emma's macOS permissions.

    python -m emma.permissions bootstrap   # interactive install-time walkthrough (shows dialogs)
    python -m emma.permissions retry       # re-run the walkthrough standalone (no reinstall)
    python -m emma.permissions check       # probe current state, no UI changes

`bootstrap` is what installer/install_macos.sh runs at install time so every
TCC permission is requested upfront instead of surfacing mid-conversation.
`retry` runs the same walkthrough on demand so a missed dialog can be
re-triggered without reinstalling.
"""

from __future__ import annotations

import argparse
import asyncio

from core import permissions

_MODE_HELP = {
    "byok": "your own OpenAI key, direct — the backend is never contacted",
    "managed": "paired device token via the Emma proxy",
    "unconfigured": "no key and no pairing yet — open the app to choose a tier",
}


def _check() -> int:
    """Probe each permission and print its state. Always exits 0."""
    probes = {
        # Accessibility reads real AX trust for THIS process. It used to call a
        # probe that asked osascript about System Events — an Automation grant —
        # and printed "granted" while screen vision could not read a thing.
        "Accessibility": permissions.check_accessibility_ax,
        "ScreenRecording": permissions.check_screen_recording,
        "Microphone": permissions.check_microphone,
        "Automation": permissions.check_automation,
        "SystemEventsAutomation": permissions.check_system_events_automation,
        "Calendars": permissions.check_calendar,
    }
    for name, fn in probes.items():
        try:
            status = "granted" if fn() else "denied_or_inconclusive"
        except Exception as exc:  # never fail the check command
            status = f"error: {exc}"
        print(f"{name}: {status}")

    # A functional check on top of the TCC read: trusted-but-dead is possible
    # (a grant left behind against a binary that has since been replaced), and
    # it looks identical to healthy from the probe alone.
    ok, reason = permissions.ax_smoke()
    print(f"AccessibilitySmoke: {'ok' if ok else 'FAILED'} ({reason})")

    # Which code is answering these questions? A months-old installed tarball
    # will happily report healthy permissions for features it does not contain.
    from config.settings import settings as live_settings
    from core import version

    # Which TIER is this daemon? With BYO-key and managed both supported, "it
    # doesn't work" has two different answers and this is the one command that
    # tells them apart (LAUNCH-11 Part 1).
    mode = live_settings.mode()
    print(f"\nMode: {mode} ({_MODE_HELP.get(mode, 'unknown')})")

    v = asyncio.run(version.staleness())
    print(f"\nSource: {v['source']}")
    print(f"  fingerprint: {v['fingerprint']}")
    print(f"  commit:      {v['commit'] or 'unknown (tarball, no install stamp)'}")
    if v.get("installed_at"):
        print(f"  installed:   {v['installed_at']}")
    if v["stale"] is True and v["is_checkout"]:
        # A dev checkout is usually AHEAD of the channel, not behind it. Telling
        # someone to re-run the installer over their own working tree would be
        # actively wrong.
        print(f"  differs from the release channel ({v['upstream']}) — dev checkout")
    elif v["stale"] is True:
        print(f"  UP TO DATE:  NO — the channel is at {v['upstream']}. Re-run install.sh.")
    elif v["stale"] is False:
        print("  up to date:  yes")
    else:
        print("  up to date:  unknown (offline, or no recorded commit to compare)")
    return 0


def _retry() -> int:
    """Re-run the full install-time permission walkthrough standalone.

    Same code path as the installer's step 7.5, so the user can re-trigger any
    dialog that got missed without reinstalling. Exits 0 when the walkthrough
    completes, 1 if it raises (e.g. the `say` binary is unavailable).
    """
    try:
        asyncio.run(permissions.bootstrap())
    except Exception as exc:
        print(f"retry failed: {exc}")
        return 1
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
        "retry",
        help="Re-run the permission walkthrough on demand (no reinstall needed).",
    )
    sub.add_parser(
        "check",
        help="Probe current permission state without changing anything.",
    )
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        asyncio.run(permissions.bootstrap())
        return 0
    if args.command == "retry":
        return _retry()
    if args.command == "check":
        return _check()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
