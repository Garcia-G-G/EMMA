"""Measurement 1: which tool does a text model pick for each labelled utterance?

Spike code: disposable. One JSONL row per (item, condition), resumable.

  python eval_tools.py --backend ollama --model qwen3:8b --cond full
  python eval_tools.py --backend openai_compat --base-url https://api.groq.com/openai/v1 \
      --key-env GROQ_API_KEY --model llama-3.3-70b-versatile --cond full

Conditions: full (all 184 registered), static40 (first 40 in registry trim order),
retr40 (per-utterance BM25 top-40 over name + description).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
RESULTS = HERE / "results"


def load_specs() -> list[dict]:
    return json.load(open(DATA / "specs_all.json"))


def static40(specs: list[dict], n: int = 40) -> list[dict]:
    order = json.load(open(DATA / "trim_order.json"))  # names, registry rank order
    by = {s["function"]["name"]: s for s in specs}
    return [by[n] for n in order[:n]]


# --- BM25, tiny and deterministic -------------------------------------------------
_TOK = re.compile(r"[a-záéíóúñü0-9]+")


def _toks(s: str) -> list[str]:
    s = s.lower().replace("_", " ")
    return [t for t in _TOK.findall(s) if len(t) > 2]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.avg = sum(map(len, docs)) / len(docs)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += (
                        self.idf[t]
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
                    )
            out.append(s)
        return out


def retr40(specs: list[dict], text: str, bm: BM25) -> list[dict]:
    sc = bm.scores(_toks(text))
    idx = sorted(range(len(specs)), key=lambda i: (-sc[i], i))[:40]
    return [specs[i] for i in sorted(idx)]  # keep registry order inside the 40


# --- backends ----------------------------------------------------------------------
def _post(url: str, body: dict, headers: dict, timeout: float = 3600) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "emma-spike/0.1",
            **headers,
        },
    )  # CF 1010 blocks urllib UA
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call_ollama(
    model: str, system: str, text: str, tools: list[dict], num_ctx: int
) -> dict:
    t0 = time.perf_counter()
    r = _post(
        "http://localhost:11434/api/chat",
        {
            "model": model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "tools": tools,
            "options": {"temperature": 0, "num_ctx": num_ctx},
            "keep_alive": "30m",
        },
        {},
    )
    wall = time.perf_counter() - t0
    msg = r.get("message", {})
    calls = [c["function"]["name"] for c in msg.get("tool_calls") or []]
    return {
        "calls": calls,
        "content": (msg.get("content") or "")[:300],
        "wall_s": wall,
        "prompt_tokens": r.get("prompt_eval_count"),
        "out_tokens": r.get("eval_count"),
        "prefill_s": (r.get("prompt_eval_duration") or 0) / 1e9,
        "gen_s": (r.get("eval_duration") or 0) / 1e9,
        "load_s": (r.get("load_duration") or 0) / 1e9,
    }


def call_openai_compat(
    base: str,
    key: str,
    model: str,
    system: str,
    text: str,
    tools: list[dict],
    tool_choice: str = "auto",
) -> dict:
    t0 = time.perf_counter()
    r = _post(
        base.rstrip("/") + "/chat/completions",
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "tools": tools,
            "tool_choice": tool_choice,
        },
        {"Authorization": f"Bearer {key}"},
        timeout=120,
    )
    wall = time.perf_counter() - t0
    msg = r["choices"][0]["message"]
    calls = [c["function"]["name"] for c in msg.get("tool_calls") or []]
    u = r.get("usage") or {}
    return {
        "calls": calls,
        "content": (msg.get("content") or "")[:300],
        "wall_s": wall,
        "prompt_tokens": u.get("prompt_tokens"),
        "out_tokens": u.get("completion_tokens"),
        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "usage": u,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["ollama", "openai_compat"])
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--cond", required=True, choices=["full", "static40", "static128", "retr40"]
    )
    ap.add_argument("--base-url")
    ap.add_argument("--key-env")
    ap.add_argument("--num-ctx", type=int, default=40960)
    ap.add_argument(
        "--rpm", type=float, default=0, help="client-side rate limit (free tiers)"
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tool-choice", default="auto", choices=["auto", "required"])
    a = ap.parse_args()

    specs = load_specs()
    system = (DATA / "system_prompt.txt").read_text()
    items = json.load(open(DATA / "labelled.json"))
    if a.limit:
        items = items[: a.limit]
    bm = BM25(
        [
            _toks(s["function"]["name"] + " " + s["function"].get("description", ""))
            for s in specs
        ]
    )
    fixed = (
        specs
        if a.cond == "full"
        else static40(specs)
        if a.cond == "static40"
        else static40(specs, 128)
        if a.cond == "static128"
        else None
    )  # Groq caps tools at 128

    RESULTS.mkdir(exist_ok=True)
    tc = "" if a.tool_choice == "auto" else f"_tc-{a.tool_choice}"
    out = (
        RESULTS
        / f"m1_{a.backend}_{a.model.replace('/', '_').replace(':', '_')}_{a.cond}{tc}.jsonl"
    )
    done = (
        {r["id"] for r in map(json.loads, open(out)) if not r.get("error")}
        if out.exists()
        else set()
    )
    key = os.environ.get(a.key_env, "") if a.key_env else ""
    if a.backend == "openai_compat" and not key:
        raise SystemExit(f"{a.key_env} is not set in this process's environment")
    gap = 60.0 / a.rpm if a.rpm else 0
    last = 0.0
    with open(out, "a") as f:
        for it in items:
            if it["id"] in done:
                continue
            tools = fixed if fixed is not None else retr40(specs, it["text"], bm)
            offered = [t["function"]["name"] for t in tools]
            if gap:
                time.sleep(max(0.0, last + gap - time.time()))
                last = time.time()
            try:
                if a.backend == "ollama":
                    r = call_ollama(a.model, system, it["text"], tools, a.num_ctx)
                else:
                    r = call_openai_compat(
                        a.base_url,
                        key,
                        a.model,
                        system,
                        it["text"],
                        tools,
                        a.tool_choice,
                    )
                err = None
            except Exception as e:  # recorded, not swallowed. The body is the
                # provider's error JSON (rate-limit detail); the key is never in it.
                body = (
                    e.read().decode(errors="replace")[:400]
                    if hasattr(e, "read")
                    else ""
                )
                r, err = {"calls": None}, f"{type(e).__name__}: {str(e)[:200]} {body}"
            row = {
                "id": it["id"],
                "cond": a.cond,
                "model": a.model,
                "n_tools": len(tools),
                "accept_in_offer": any(
                    x in offered for x in it["accept"] if x != "__none__"
                )
                or it["accept"] == ["__none__"],
                "error": err,
                **r,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(
                it["id"],
                r.get("calls"),
                f"{r.get('wall_s', 0):.1f}s",
                r.get("prompt_tokens"),
                err or "",
            )


if __name__ == "__main__":
    main()
