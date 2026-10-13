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

SPECS_SHA = "b16c84964b3b3749f631b8c2ec761486156a4917cafb898a6d65085a8047d088"
CORE_SHA = "f3f81c3b86bb8bd4e71558e00ed03858823cdeb22fa36fbfa3e0cdd4c0193e8f"
TOOL_SELECTION_SRC_SHA = "3f419a25fe8bd5b316631607ec87639628ea72da5135a4548613ac55b24a4537"


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
