"""Generic notification stub (Prompt 30) — quiet no-op on unknown platforms."""

from __future__ import annotations

import sys


class StubNotify:
    def send(self, title: str, body: str) -> None:
        print(f"[Emma] {title}: {body}", file=sys.stderr)
