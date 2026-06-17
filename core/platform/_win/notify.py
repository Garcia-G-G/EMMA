"""Windows notification (Prompt 30). Best-effort no-op for now; native toast → 30.x."""

from __future__ import annotations

import sys


class WinNotify:
    def send(self, title: str, body: str) -> None:
        # Notifications are fire-and-forget — degrade quietly rather than raise.
        # A native Action Center toast (winrt / PowerShell) lands in a 30.x follow-up.
        print(f"[Emma] {title}: {body}", file=sys.stderr)
