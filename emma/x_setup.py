"""Backward-compat alias (26.2): `python -m emma.x_setup` == `emma.setup --only x`.

The X PKCE flow moved into the unified installer (emma.setup / core.x_oauth).
This thin wrapper keeps 26.1's documented command working.
"""

from __future__ import annotations

from emma.setup import main as _setup_main


def main() -> int:
    return _setup_main(["--only", "x"])


if __name__ == "__main__":
    raise SystemExit(main())
