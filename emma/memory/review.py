"""Supersession review CLI (Prompt 25-C).

    python -m emma.memory.review            # show stats + last 20 supersessions
    python -m emma.memory.review --undo 42  # revert ONE supersession (id 42 = the replacement)

Audit / safety net for Garcia's periodic review — never invoked from voice.
Superseded facts are marked, never deleted, so every change here is reversible.
"""

from __future__ import annotations

import argparse
import asyncio

from memory import long_term


async def _run(undo_id: int | None, limit: int) -> int:
    if undo_id is not None:
        ok = await long_term.undo_supersede(undo_id)
        if ok:
            print(
                f"✓ Reverted supersession: reactivated the old fact, removed replacement #{undo_id}."
            )
            return 0
        print(f"✗ #{undo_id} is not a supersession replacement (nothing to undo).")
        return 1

    active, superseded = await long_term.stats()
    print(f"Memory: {active} active fact(s), {superseded} superseded.")
    pairs = await long_term.recent_supersessions(limit)
    if not pairs:
        print("No supersessions recorded yet.")
        return 0
    print(f"\nLast {len(pairs)} supersession(s) (newest first):")
    for p in pairs:
        print(f"  #{p['new_id']} replaced #{p['old_id']}")
        print(f"      old: {p['old_content']}")
        print(f"      new: {p['new_content']}")
    print("\nUndo one with:  python -m emma.memory.review --undo <new_id>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="emma.memory.review", description=__doc__)
    ap.add_argument(
        "--undo",
        type=int,
        metavar="NEW_ID",
        help="revert the supersession whose replacement id is NEW_ID",
    )
    ap.add_argument("--limit", type=int, default=20, help="how many recent supersessions to show")
    args = ap.parse_args()
    return asyncio.run(_run(args.undo, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
