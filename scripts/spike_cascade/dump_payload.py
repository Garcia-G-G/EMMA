"""Dump the exact per-turn payload Emma sends: instructions + Realtime tool specs.

Run with the PROJECT venv (it imports Emma). Tokenize with measure_payload.py
(spike venv, has tiktoken). Output is Personal tier (memory priming) → data/,
gitignored.

  .venv/bin/python scripts/spike_cascade/dump_payload.py scripts/spike_cascade/data/payload_live.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main(out: str) -> None:
    from core import conversation
    from memory.long_term import priming_block
    from tools import registry

    instructions = await conversation._build_instructions()
    try:
        memory = await priming_block(context=None)
    except Exception:
        memory = ""
    # What a session actually opens with (core + loader + sticky under
    # TOOL_RETRIEVAL; the capped full set otherwise). "--all" = every available tool.
    specs = (
        registry.available_specs()
        if "--all" in sys.argv
        else conversation._session_tool_specs()
    )
    rt = conversation._adapt_tool_specs_for_realtime(specs)
    mod = {e.name: registry._module_of(e) for e in registry.available_tools()}
    Path(out).write_text(
        json.dumps(
            {
                "instructions": instructions,
                "memory_block": memory,
                "realtime_tools": rt,
                "tool_module": {t["name"]: mod.get(t["name"], "?") for t in rt},
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    print(len(instructions), "chars instructions;", len(rt), "tools")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
