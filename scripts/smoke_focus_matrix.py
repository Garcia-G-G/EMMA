#!/usr/bin/env python3
"""
Smoke helper for Prompt 27.1 — pane awareness across apps.

Garcia: focus any app + pane on your screen, then run

    .venv/bin/python scripts/smoke_focus_matrix.py

You get a one-shot snapshot of what Emma "sees" right now:
- which app is forward
- which window
- the focused pane (PaneInfo)
- the window's layout map

Repeat across (app x focus target) combinations to fill the
27.1 acceptance matrix. The script is read-only — it never
clicks, types, or modifies anything.

Continuous mode (re-snapshot every 2s) for moving around between
panes without re-launching:

    .venv/bin/python scripts/smoke_focus_matrix.py --watch

Aim: ≥ 80% ✓ across at least 8 cells (app x focus target),
picking apps NOT mentioned in core/screen_vision.py or
tools/screen_vision_tool.py — that's the generalization test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

# Make the project importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from core.screen_vision import focused_pane, frontmost_window, window_panes
except Exception as e:
    print(f"[fatal] could not import core.screen_vision: {e}", file=sys.stderr)
    print("  Run from the repo root with `.venv/bin/python`.", file=sys.stderr)
    sys.exit(2)


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def _snapshot() -> dict[str, Any]:
    win = None
    pane = None
    layout: Any = []
    errs: list[str] = []

    t0 = time.perf_counter()
    try:
        win = frontmost_window()
    except Exception as e:
        errs.append(f"frontmost_window: {type(e).__name__}: {e}")
    t_win = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        pane = focused_pane()
    except Exception as e:
        errs.append(f"focused_pane: {type(e).__name__}: {e}")
    t_pane = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        layout = window_panes()
    except Exception as e:
        errs.append(f"window_panes: {type(e).__name__}: {e}")
    t_layout = (time.perf_counter() - t0) * 1000

    return {
        "timing_ms": {
            "frontmost_window": round(t_win, 1),
            "focused_pane":     round(t_pane, 1),
            "window_panes":     round(t_layout, 1),
        },
        "raw_types": {
            "window": type(win).__name__,
            "focused_pane": type(pane).__name__,
            "window_layout": type(layout).__name__,
        },
        "window": _to_dict(win),
        "focused_pane": _to_dict(pane),
        "window_layout": layout,
        "errors": errs,
    }


def _print_human(snap: dict[str, Any]) -> None:
    win = snap.get("window") or {}
    pane = snap.get("focused_pane") or {}
    layout = snap.get("window_layout") or []
    t = snap["timing_ms"]
    raw = snap.get("raw_types", {})
    errs = snap.get("errors", [])

    print("─" * 64)
    if errs:
        print("ERRORS:")
        for e in errs:
            print(f"  {e}")
    print(f"raw types: window={raw.get('window')} pane={raw.get('focused_pane')} layout={raw.get('window_layout')}")
    # WindowSnapshot fields: app, title, role, bounds, focused_role, focused_title
    print(f"App      : {win.get('app', '?')}")
    print(f"Window   : {win.get('title', '?')}")
    fr = win.get("focused_role")
    ft = win.get("focused_title")
    if fr or ft:
        print(f"Focused  : {fr or '?'}  /  {ft or ''}")
    if pane:
        print()
        print("Focused pane")
        for k in ("role", "role_description", "identifier", "title",
                  "focused_role", "focused_role_description"):
            v = pane.get(k)
            if v:
                print(f"  {k:<24} {v}")
        snip = pane.get("snippet", "")
        if snip:
            print(f"  snippet                  {snip[:160]!r}")
    else:
        print("Focused pane: (none — AX gave nothing useful for this app)")

    if layout:
        print()
        print(f"Window layout ({len(layout)} panes):")
        for i, p in enumerate(layout, 1):
            label = (
                p.get("role_description")
                or p.get("identifier")
                or p.get("title")
                or p.get("role")
                or "?"
            )
            print(f"  {i}. {label}")

    print()
    print(f"Latency  : window={t['frontmost_window']}ms  "
          f"pane={t['focused_pane']}ms  layout={t['window_panes']}ms")
    print("─" * 64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable form")
    ap.add_argument("--watch", action="store_true",
                    help="re-snapshot with a 5s countdown so you can switch focus")
    ap.add_argument("--interval", type=int, default=5,
                    help="seconds between snapshots in --watch mode (default 5)")
    args = ap.parse_args()

    def once() -> None:
        snap = _snapshot()
        if args.json:
            print(json.dumps(snap, indent=2, default=str))
        else:
            _print_human(snap)

    if args.watch:
        print(
            f"Watch mode. Every {args.interval}s I'll snapshot the FRONTMOST app.\n"
            "Switch focus (Cmd+Tab) to the app/pane you want to test, then wait\n"
            "for the countdown to hit 0. The script will print what Emma sees there.\n"
            "Ctrl-C to exit.\n"
        )
        try:
            while True:
                for i in range(args.interval, 0, -1):
                    sys.stdout.write(f"\r  snapshot in {i}s … switch focus now")
                    sys.stdout.flush()
                    time.sleep(1.0)
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()
                once()
        except KeyboardInterrupt:
            print("\nbye")
    else:
        once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
