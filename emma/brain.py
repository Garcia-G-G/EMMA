"""``python -m emma.brain`` — the brain's shadow-mode report and tools (BRAIN-1 Part 7).

  python -m emma.brain report [--log PATH]   what a week of shadow mode says
  python -m emma.brain decide "abre spotify" one decision, with every feature
  python -m emma.brain fetch-model           the local embedder (pinned, SHA-checked)

The report answers BRAIN-2's question — is the gate trustworthy enough to
execute? — from ``~/.emma/brain_decisions.jsonl``: how often the brain would
have served the turn, and when it would have, whether it agreed with what the
live path actually did.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from core.brain import log as brain_log


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}% ({n}/{d})" if d else "   n/a"


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))] if xs else 0.0


def report(rows: list[dict[str, Any]]) -> str:
    turns = [r for r in rows if "brain_verdict" in r]
    out: list[str] = []
    n = len(turns)
    out.append(f"turns logged: {n}")
    if not n:
        out.append("(no shadow turns yet — the log fills while Emma is used normally)")
        return "\n".join(out)

    verdicts = Counter(str(r["brain_verdict"]) for r in turns)
    served = n - verdicts.get("defer", 0)
    out.append(f"served without an LLM: {_pct(served, n)}   "
               + "  ".join(f"{k} {v}" for k, v in sorted(verdicts.items())))

    local = [r for r in turns if r["brain_verdict"] == "local"]
    compared = [r for r in local if r.get("agree") is not None]
    agreed = [r for r in compared if r["agree"]]
    out.append(f"local verdicts: {_pct(len(local), n)}")
    out.append(f"agreement with the live path on local verdicts: {_pct(len(agreed), len(compared))}")

    templ = [r for r in turns if r["brain_verdict"] in ("none", "clarify", "refuse")]
    templ_ok = [r for r in templ if r.get("agree")]
    out.append(f"no-tool verdicts where the live path also used no tool: {_pct(len(templ_ok), len(templ))}")

    bad = [r for r in turns if r.get("agree") is False]
    out.append("")
    out.append(f"disagreements ({len(bad)}):")
    for r in bad:
        out.append(f"  [{r.get('session', '')}#{r.get('turn', '')}] {str(r.get('utterance', ''))[:70]!r}")
        out.append(f"      brain: {r['brain_verdict']} {r.get('brain_tool') or ''}  "
                   f"slots={ {k: v.get('value') for k, v in (r.get('slots') or {}).items()} }")
        out.append(f"      live:  {r.get('realtime_tool') or 'no tool'}  args={r.get('realtime_args') or {}}")

    reasons = Counter(str(r.get("reason")) for r in turns if r["brain_verdict"] == "defer")
    out.append("")
    out.append("defer reasons:")
    for reason, c in reasons.most_common():
        out.append(f"  {reason:28s} {c:5d}  {100 * c / n:5.1f}%")

    ms = [float(r["timings"]["total_ms"]) for r in turns
          if isinstance(r.get("timings"), dict) and "total_ms" in r["timings"]]
    out.append("")
    out.append(f"decision latency: p50 {_q(ms, 0.5):.1f} ms   p95 {_q(ms, 0.95):.1f} ms   (n={len(ms)})")
    return "\n".join(out)


def _decide(text: str) -> dict[str, Any]:
    from core.brain.brain import Brain
    from core.brain.embed import Embedder
    from core.brain.index import DEFAULT_PATH
    from core.brain_host import build_index, thresholds_from_settings, tool_docs, vocab
    from core.brain_shadow import installed_apps

    docs = tool_docs(available_only=True)
    emb = Embedder()
    index = build_index(emb, DEFAULT_PATH) if emb.available() else None
    brain = Brain(docs, vocab(apps=installed_apps()), thresholds_from_settings(), index, emb)
    return brain.decide(text).to_record()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emma.brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="summarize the shadow-mode log")
    rp.add_argument("--log", type=Path, default=brain_log.DEFAULT_PATH)
    dp = sub.add_parser("decide", help="run one utterance through the brain")
    dp.add_argument("text")
    sub.add_parser("fetch-model", help="download the pinned local embedder")
    a = ap.parse_args(argv)

    if a.cmd == "report":
        print(report(brain_log.read(a.log)))
        return 0
    if a.cmd == "decide":
        print(json.dumps(_decide(a.text), ensure_ascii=False, indent=1, default=str))
        return 0
    from core.brain.embed import fetch

    for f, h in fetch().items():
        print(f"{f}  sha256 {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
