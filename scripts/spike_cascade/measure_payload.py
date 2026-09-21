"""Token breakdown of a dumped payload (o200k_base — the GPT-4o / Realtime family).

Run with the SPIKE venv (tiktoken):
  scripts/spike_cascade/.venv/bin/python scripts/spike_cascade/measure_payload.py data/payload_live.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter

import tiktoken

ENC = tiktoken.get_encoding("o200k_base")


def tok(s: str) -> int:
    return len(ENC.encode(s))


def sections(text: str) -> list[tuple[str, str]]:
    """Split on markdown H1 headers ('# Foo' at line start)."""
    parts = re.split(r"(?m)^(?=# )", text)
    out = []
    for p in parts:
        if not p.strip():
            continue
        head = p.splitlines()[0] if p.startswith("# ") else "(preamble)"
        out.append((head, p))
    return out


def main(path: str) -> None:
    d = json.load(open(path))
    ins, mem, tools = d["instructions"], d["memory_block"], d["realtime_tools"]
    ins_t = tok(ins)
    mem_t = tok(mem) if mem and mem in ins else 0
    tools_t = tok(json.dumps(tools, ensure_ascii=False))
    print(
        f"instructions: {len(ins):,} chars, {ins_t:,} tokens (memory block inside: {mem_t:,})"
    )
    print(f"tools: {len(tools)} specs, {tools_t:,} tokens (JSON as sent)")
    print(f"TOTAL static payload: {ins_t + tools_t:,} tokens\n")
    print("## instructions by section")
    for head, body in sections(ins):
        print(f"{tok(body):6,}  {head}")
    print("\n## tools by module")
    by: Counter[str] = Counter()
    n: Counter[str] = Counter()
    for t in tools:
        m = d["tool_module"].get(t["name"], "?")
        by[m] += tok(json.dumps(t, ensure_ascii=False))
        n[m] += 1
    for m, v in by.most_common():
        print(f"{v:6,}  {n[m]:3d}  {m}")
    per = sorted(
        ((tok(json.dumps(t, ensure_ascii=False)), t["name"]) for t in tools),
        reverse=True,
    )
    print("\n## largest single tools")
    for v, name in per[:15]:
        print(f"{v:6,}  {name}")


if __name__ == "__main__":
    main(sys.argv[1])
