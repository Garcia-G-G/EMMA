"""Manual pane-awareness smoke (27.1) — run interactively, NOT in CI.

The app-by-focus generalization matrix can't be produced headlessly (you can't drive
GUI focus from a detached process). Run this yourself: focus a sub-region of any
app (a sidebar, an editor, a terminal, an address bar, a chat box…), then run

    .venv/bin/python tests/smoke_screen_vision.py

It prints exactly what Emma's where_am_i / window_layout / read_pane_text would
say about whatever currently holds focus — across ANY app, with no app-specific
code involved. Try the same command focused on two different panes of the same
app and confirm the answers differ and each names the right region.
"""

from __future__ import annotations

import asyncio
import time

import tools.screen_vision_tool as svt
from core import screen_vision as sv


def main() -> None:
    t0 = time.perf_counter()
    pane = sv.focused_pane()
    dt = (time.perf_counter() - t0) * 1000
    print(f"focused_pane resolved in {dt:.0f} ms\n")
    if pane is not None:
        print(f"  app            : {pane.app}")
        print(f"  label          : {pane.label!r}")
        print(f"  role           : {pane.role}  ({pane.role_description})")
        print(f"  identifier     : {pane.identifier!r}")
        print(f"  position        : {pane.position!r}")
        print(f"  focused element: {pane.focused_role} ({pane.focused_role_description})")
        print(f"  ancestors      : {pane.ancestors}")
        print(f"  snippet        : {pane.snippet[:160]!r}\n")
    print("  where_am_i      ->", asyncio.run(svt.where_am_i()).user_message)
    print("  window_layout   ->", asyncio.run(svt.window_layout()).user_message)
    print("  read_pane_text  ->", asyncio.run(svt.read_pane_text()).user_message[:160])


if __name__ == "__main__":
    main()
