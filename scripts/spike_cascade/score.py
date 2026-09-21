"""Score Measurement 1 result files against the frozen labelled set.

python score.py results/m1_*.jsonl
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"
ITEMS = {i["id"]: i for i in json.load(open(DATA / "labelled.json"))}


def correct(accept: list[str], calls: list[str] | None) -> bool:
    if calls is None:
        return False
    if not calls:
        return "__none__" in accept
    return calls[0] in accept


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}% ({n}/{d})" if d else "   n/a"


def q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def score_rows(rows: dict[str, dict], label: str) -> None:
    real = [i for i in rows if ITEMS[i]["src"] != "synthetic"]
    syn = [i for i in rows if ITEMS[i]["src"] == "synthetic"]
    ok = {i: correct(ITEMS[i]["accept"], rows[i].get("calls")) for i in rows}
    errs = [i for i in rows if rows[i].get("error")]
    print(f"\n=== {label}  (n={len(rows)}, errors={len(errs)})")
    print(f"  REAL (scenario+log): {pct(sum(ok[i] for i in real), len(real))}")
    print(
        f"     scenario only   : {pct(sum(ok[i] for i in real if ITEMS[i]['src'] == 'scenario'), sum(ITEMS[i]['src'] == 'scenario' for i in real))}"
    )
    print(
        f"     log only        : {pct(sum(ok[i] for i in real if ITEMS[i]['src'] == 'log'), sum(ITEMS[i]['src'] == 'log' for i in real))}"
    )
    print(f"  synthetic (not in verdict): {pct(sum(ok[i] for i in syn), len(syn))}")
    inoff = [i for i in real if rows[i].get("accept_in_offer")]
    if len(inoff) != len(real):
        print(
            f"  REAL, correct tool was offered: {pct(sum(ok[i] for i in inoff), len(inoff))}"
            f"   | offer recall {pct(len(inoff), len(real))}"
        )
    bycat = defaultdict(list)
    for i in rows:
        bycat[ITEMS[i]["cat"]].append(ok[i])
    print(
        "  by category:",
        ", ".join(f"{c} {sum(v)}/{len(v)}" for c, v in sorted(bycat.items())),
    )
    conf = Counter()
    for i in rows:
        if not ok[i] and not rows[i].get("error"):
            exp = "/".join(a for a in ITEMS[i]["accept"])[:45]
            got = (rows[i].get("calls") or ["<none>"])[0]
            conf[(exp, got)] += 1
    print("  misses (expected -> got):")
    for (e, g), n in conf.most_common():
        print(f"     {n}x  {e:45s} -> {g}")
    lat = [
        rows[i]["wall_s"]
        for i in rows
        if rows[i].get("wall_s") and rows[i]["wall_s"] < 60
    ]
    cold = [
        rows[i]["wall_s"]
        for i in rows
        if rows[i].get("wall_s") and rows[i]["wall_s"] >= 60
    ]
    if lat:
        print(
            f"  latency warm p50 {q(lat, 0.5):.2f}s p95 {q(lat, 0.95):.2f}s (n={len(lat)}); cold/recompute calls >=60s: {len(cold)}"
            + (f" p50 {statistics.median(cold):.0f}s" if cold else "")
        )
    pt = [rows[i]["prompt_tokens"] for i in rows if rows[i].get("prompt_tokens")]
    ot = [rows[i]["out_tokens"] for i in rows if rows[i].get("out_tokens")]
    if pt:
        print(
            f"  tokens/call: prompt median {statistics.median(pt):.0f}, output median {statistics.median(ot) if ot else 0:.0f}"
        )


def realtime_hist() -> None:
    """Partial historical baseline: what Realtime fired after the real log utterances."""
    rows = {
        i: {"calls": it["rt_hist"]} for i, it in ITEMS.items() if it["src"] == "log"
    }
    ok = {i: correct(ITEMS[i]["accept"], rows[i]["calls"]) for i in rows}
    print(
        f"\n=== Realtime, historical (log items only, first tool fired within 25 s): {pct(sum(ok.values()), len(ok))}"
    )
    for i in rows:
        if not ok[i]:
            print(
                f"     {ITEMS[i]['text'][:60]!r:64s} expected {ITEMS[i]['accept']} got {rows[i]['calls'] or '<none>'}"
            )


if __name__ == "__main__":
    for path in sys.argv[1:]:
        rows: dict[str, dict] = {}
        for line in open(path):
            r = json.loads(line)
            rows[r["id"]] = r  # last row per id wins (resume/retry)
        score_rows(rows, Path(path).stem)
    realtime_hist()
