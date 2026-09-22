"""Offline retriever comparison: recall@k of an acceptable tool over the frozen set.

No LLM involved — this only asks "is a correct tool among the k offered?".
Items labelled __none__ are skipped (nothing to retrieve).

  .venv/bin/python scripts/spike_cascade/retrieval_recall.py [specs.json] [core.json]

specs.json defaults to data/specs_all.json (the frozen spike registry). With
core.json (a list of tool names), recall counts a hit if the tool is in core OR
in the top-k retrieved from the non-core remainder — the production shape.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from eval_tools import BM25, _toks  # noqa: E402


def _embed(model: str, texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(
        "http://localhost:11434/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["embeddings"]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    specs_path = Path(args[0]) if args else HERE / "data" / "specs_all.json"
    core = set(json.load(open(args[1]))) if len(args) > 1 else set()
    mode = flags.get("mode", "bm25")  # bm25 | embed | hybrid
    emodel = flags.get("embed", "nomic-embed-text")
    specs = json.load(open(specs_path))
    items = json.load(open(HERE / "data" / "labelled.json"))
    pool = [s for s in specs if s["function"]["name"] not in core]
    names = [s["function"]["name"] for s in pool]
    docs = [s["function"]["name"] + " " + s["function"].get("description", "") for s in pool]
    bm = BM25([_toks(d) for d in docs])
    tvec = _embed(emodel, docs) if mode != "bm25" else []
    ks = [5, 10, 15, 20, 30, 40]
    hits = dict.fromkeys(ks, 0)
    core_hits = 0
    n = 0
    for it in items:
        acc = [a for a in it["accept"] if a != "__none__"]
        if not acc:
            continue
        n += 1
        if core & set(acc):
            core_hits += 1
            for k in ks:
                hits[k] += 1
            continue
        bs = bm.scores(_toks(it["text"]))
        rank_b = sorted(range(len(names)), key=lambda i: -bs[i])
        if mode != "bm25":
            q = _embed(emodel, [it["text"]])[0]
            es = [_cos(q, v) for v in tvec]
            rank_e = sorted(range(len(names)), key=lambda i: -es[i])
        if mode == "bm25":
            idx = rank_b
        elif mode == "embed":
            idx = rank_e
        else:  # reciprocal-rank fusion
            rrf = [0.0] * len(names)
            for r in (rank_b, rank_e):
                for pos, i in enumerate(r):
                    rrf[i] += 1 / (60 + pos)
            idx = sorted(range(len(names)), key=lambda i: -rrf[i])
        order = [names[i] for i in idx]
        for k in ks:
            if set(order[:k]) & set(acc):
                hits[k] += 1
    print(f"{specs_path.name} [{mode}{'/' + emodel if mode != 'bm25' else ''}]: {len(specs)} tools, core={len(core)}, items with a tool={n}")
    if core:
        print(f"  covered by core alone: {core_hits}/{n} = {core_hits / n:.1%}")
    for k in ks:
        print(f"  recall@{k:<2} {hits[k]:3d}/{n} = {hits[k] / n:.1%}")


if __name__ == "__main__":
    main()
