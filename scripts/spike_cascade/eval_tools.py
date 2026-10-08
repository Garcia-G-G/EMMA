"""Measurement 1: which tool does a text model pick for each labelled utterance?

Spike code: disposable. One JSONL row per (item, condition), resumable.

  python eval_tools.py --backend ollama --model qwen3:8b --cond full
  python eval_tools.py --backend openai_compat --base-url https://api.groq.com/openai/v1 \
      --key-env GROQ_API_KEY --model llama-3.3-70b-versatile --cond full

Conditions: full (all 184 registered), static40 (first 40 in registry trim order),
static128 (first 128), retr40 (per-utterance BM25 top-40 over name + description),
and core10 (LAUNCH-12 production selection: core + BM25 top-10, via
core.tool_selection itself — the shipped code, not a copy). core10 implies the
post-LAUNCH-12 system prompt (--system).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
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
    model: str,
    system: str,
    text: str,
    tools: list[dict],
    num_ctx: int,
    history: list[dict] | None = None,
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
                *(history or []),
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
        "need": _need_arg(msg.get("tool_calls") or []),
        "_raw_calls": msg.get("tool_calls") or [],
        "content": (msg.get("content") or "")[:300],
        "wall_s": wall,
        "prompt_tokens": r.get("prompt_eval_count"),
        "out_tokens": r.get("eval_count"),
        "prefill_s": (r.get("prompt_eval_duration") or 0) / 1e9,
        "gen_s": (r.get("eval_duration") or 0) / 1e9,
        "load_s": (r.get("load_duration") or 0) / 1e9,
    }


LOADER_NAME = "find_tools"


def _need_arg(tool_calls: list[dict]) -> str:
    """The `need` the model passed to find_tools (its own phrasing of the request)."""
    for c in tool_calls:
        fn = c.get("function") or {}
        if fn.get("name") != LOADER_NAME:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        return str((args or {}).get("need") or "")
    return ""


def _loader_history(r: dict, loaded: list[str], backend: str) -> list[dict]:
    """Assistant find_tools call + its result, in the backend's wire format."""
    raw = r.get("_raw_calls") or []
    payload = json.dumps(
        {
            "success": bool(loaded),
            "user_message": (
                "Tools now available: "
                + ", ".join(loaded)
                + ". Call the one that fits the user's request now; don't mention this step."
            )
            if loaded
            else (
                "No encontré una herramienta para eso. Dile al usuario brevemente que "
                "no puedes hacerlo, o pídele que lo diga de otra forma."
            ),
            "data": {"loaded": loaded},
            "requires_confirmation": False,
        },
        ensure_ascii=False,
    )
    assistant = {"role": "assistant", "content": "", "tool_calls": raw}
    if backend == "ollama":
        return [assistant, {"role": "tool", "tool_name": LOADER_NAME, "content": payload}]
    tc_id = (raw[0] if raw else {}).get("id", "call_0")
    return [assistant, {"role": "tool", "tool_call_id": tc_id, "content": payload}]


def call_openai_compat(
    base: str,
    key: str,
    model: str,
    system: str,
    text: str,
    tools: list[dict],
    tool_choice: str = "auto",
    history: list[dict] | None = None,
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
                *(history or []),
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
        "need": _need_arg(msg.get("tool_calls") or []),
        "_raw_calls": msg.get("tool_calls") or [],
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
        "--cond",
        required=True,
        choices=["full", "static40", "static128", "retr40", "core10", "loader"],
    )
    ap.add_argument("--base-url")
    ap.add_argument("--key-env")
    ap.add_argument("--num-ctx", type=int, default=40960)
    ap.add_argument(
        "--rpm", type=float, default=0, help="client-side rate limit (free tiers)"
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tool-choice", default="auto", choices=["auto", "required"])
    ap.add_argument(
        "--system", default="system_prompt.txt", help="prompt file under data/"
    )
    ap.add_argument(
        "--specs", default="specs_all.json", help="tool specs file under data/"
    )
    ap.add_argument("--tag", default="", help="suffix for the results filename")
    a = ap.parse_args()

    specs = json.load(open(DATA / a.specs))
    system = (DATA / a.system).read_text()
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
    selector = None
    if a.cond in ("core10", "loader"):
        sys.path.insert(0, str(HERE.parents[1]))
        from core.tool_selection import ToolSelector  # the SHIPPED selector, not a copy

        selector = ToolSelector(specs)

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
            if a.cond == "loader":
                # Core only (find_tools included) — the shipped Realtime shape.
                tools = [selector.by_name[n] for n in selector.core]
            elif selector is not None:
                tools = selector.select_for_text(it["text"])
            else:
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
            # The loader round-trip, shaped like production: the model's own
            # find_tools call and the tool's result (tools/tool_loader_tool.py,
            # as the function handler serializes it) go back into the
            # conversation, the retrieved tools are added, and the second choice
            # is the one scored. Retrieval runs on the model's phrasing of `need`.
            loader_used = False
            hist: list[dict] = []
            if (
                a.cond == "loader"
                and err is None
                and (r.get("calls") or [None])[0] == LOADER_NAME
            ):
                loader_used = True
                loaded = selector.retrieve(r.get("need") or it["text"])
                tools2 = tools + [
                    selector.by_name[n] for n in loaded if n not in selector.core
                ]
                offered = [t["function"]["name"] for t in tools2]
                hist = _loader_history(r, loaded, a.backend)
                r["loader_need"] = r.get("need")
                r["loader_hop1_calls"] = r.get("calls")
                if gap:
                    time.sleep(max(0.0, last + gap - time.time()))
                    last = time.time()
                try:
                    if a.backend == "ollama":
                        r2 = call_ollama(
                            a.model, system, it["text"], tools2, a.num_ctx, hist
                        )
                    else:
                        r2 = call_openai_compat(
                            a.base_url,
                            key,
                            a.model,
                            system,
                            it["text"],
                            tools2,
                            a.tool_choice,
                            hist,
                        )
                    r2["loader_need"] = r["loader_need"]
                    r2["loader_hop1_calls"] = r["loader_hop1_calls"]
                    r2["wall_s"] = r.get("wall_s", 0) + r2.get("wall_s", 0)
                    r2["prompt_tokens"] = (r.get("prompt_tokens") or 0) + (
                        r2.get("prompt_tokens") or 0
                    )
                    r, err = r2, None
                except Exception as e:
                    body = (
                        e.read().decode(errors="replace")[:400]
                        if hasattr(e, "read")
                        else ""
                    )
                    err = f"loader_hop2 {type(e).__name__}: {str(e)[:200]} {body}"
            r.pop("_raw_calls", None)
            row = {
                "id": it["id"],
                "loader_used": loader_used,
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
