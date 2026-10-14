"""BRAIN-1.5 constraint: nothing the live model reads may change.

The brain got its own retrieval text (core/brain/phrasebook.toml). The LLM-facing
tool specs (docstrings + tools/guidance.py, as registry._spec renders them) and
core/tool_selection.py — the index behind Realtime's find_tools — stay
byte-identical. Hashes taken 2026-10-13 before BRAIN-1.5 touched anything.

If a change to a tool's description is intended, it is NOT a brain change: do it
in its own commit and re-pin here with the reason.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core import tool_selection
from tools import registry

# Re-pinned in Step 40: current_time's example zones became neutral (AGENTS.md
# rule 2); the only spec that changed. tool_selection.py: docstring-only note
# that the payload is the Realtime backend's (Step 40 Part 4).
SPECS_SHA = "60eb239aafd449eabba0460f2e1b425bc2b7633f706bcac040fd231d5710c576"
CORE_SHA = "f3f81c3b86bb8bd4e71558e00ed03858823cdeb22fa36fbfa3e0cdd4c0193e8f"
TOOL_SELECTION_SRC_SHA = "c3d7404fc621bfb6e25c35edddf40458fffa6a5969d8adcb6fd1e4dea4cb283b"


def _sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_every_tool_spec_the_llm_reads_is_unchanged() -> None:
    specs = [registry._spec(registry.get_tool(n)) for n in sorted(registry.list_tools())]
    assert len(specs) == 185
    assert _sha(specs) == SPECS_SHA


def test_realtime_core_and_retriever_are_unchanged() -> None:
    assert _sha(list(tool_selection.CORE_TOOLS)) == CORE_SHA
    src = Path(tool_selection.__file__).read_bytes()
    assert hashlib.sha256(src).hexdigest() == TOOL_SELECTION_SRC_SHA
