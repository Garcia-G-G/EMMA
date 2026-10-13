"""VOICE-1 Part 6 — how the LLM-free voice did: latency, spend, and the brain's day.

    python -m emma.voice_report                 # everything since the first cascade turn
    python -m emma.voice_report --since 2026-10-14T09:00
    python -m emma.voice_report --usd-per-1k-credits 0.22

Three sources, one window:

* ``~/.emma/voice_latency.jsonl`` (core.cascade.Latency): end of speech -> first
  audio per turn, wake -> first audio per session, and session lengths, i.e.
  the minutes of real use.
* ElevenLabs' usage API (``/v1/usage/character-stats``): STT and TTS credits
  over the window, read with the user's own key directly from ElevenLabs.
* ``~/.emma/brain_decisions.jsonl``: the cascade section of ``emma.brain report``
  (local rate, defer reasons, corrections).

USD is credits x a rate you pass. Emma doesn't know your plan's price, and the
default (Creator list price, $22 per 100k credits) is labelled as an assumption.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_USD_PER_1K = 0.22  # Creator plan list price ($22 / 100k credits): an assumption
LATENCY_LOG = Path.home() / ".emma" / "voice_latency.jsonl"


def _q(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def read_jsonl(path: Path, since: float) -> list[dict[str, Any]]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if float(r.get("ts", 0)) >= since:
                out.append(r)
    except OSError:
        pass
    return out


def latency_section(rows: list[dict[str, Any]]) -> tuple[list[str], float]:
    turns = [r for r in rows if r.get("kind") == "turn"]
    stop = [float(r["stop_to_audio_ms"]) for r in turns if "stop_to_audio_ms" in r]
    wake = [float(r["wake_to_audio_ms"]) for r in turns if "wake_to_audio_ms" in r]
    minutes = sum(float(r.get("seconds", 0)) for r in rows if r.get("kind") == "session") / 60
    lines = [
        f"end of speech -> first audio: p50 {_q(stop, 0.5):.0f} ms   p95 {_q(stop, 0.95):.0f} ms"
        f"   (n={len(stop)})",
        f"wake -> first audio:          p50 {_q(wake, 0.5):.0f} ms   p95 {_q(wake, 0.95):.0f} ms"
        f"   (n={len(wake)} sessions)",
        f"minutes of use (sessions open): {minutes:.1f}",
    ]
    return lines, minutes


def usage_credits(key: str, since: float, until: float) -> dict[str, float]:
    """{"STT": credits, "TTS": credits} from ElevenLabs, directly. {} on failure."""
    import urllib.request

    url = ("https://api.elevenlabs.io/v1/usage/character-stats"
           f"?start_unix={int(since * 1000)}&end_unix={int(until * 1000)}"
           "&breakdown_type=product_type&aggregation_interval=day")
    req = urllib.request.Request(url, headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return {}
    return {k: float(sum(v)) for k, v in (data.get("usage") or {}).items()}


def spend_section(credits: dict[str, float], minutes: float, usd_per_1k: float) -> list[str]:
    if not credits:
        return ["spend: ElevenLabs usage API unavailable (no key, or offline)"]
    total = sum(credits.values())
    lines = ["ElevenLabs credits: " + "  ".join(f"{k} {v:.0f}" for k, v in sorted(credits.items()))
             + f"  total {total:.0f}"]
    usd = total / 1000 * usd_per_1k
    if minutes > 0:
        lines.append(f"spend: ${usd:.3f} over {minutes:.1f} min = ${usd / minutes:.4f}/min "
                     f"(at ${usd_per_1k}/1k credits; estimate was $0.02-0.03/min)")
    else:
        lines.append(f"spend: ${usd:.3f} (no session minutes logged in the window)")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="emma.voice_report")
    ap.add_argument("--since", help="ISO time (local); default: the first logged cascade turn")
    ap.add_argument("--usd-per-1k-credits", type=float, default=DEFAULT_USD_PER_1K)
    a = ap.parse_args(argv)
    since = datetime.fromisoformat(a.since).timestamp() if a.since else 0.0
    now = time.time()
    rows = read_jsonl(LATENCY_LOG, since)
    if not since and rows:
        since = min(float(r["ts"]) for r in rows)

    if not rows and not since:
        print("no cascade turns logged yet (~/.emma/voice_latency.jsonl fills as Emma is used)")
        return 0
    lat, minutes = latency_section(rows)
    print(f"window: {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} -> "
          f"{datetime.fromtimestamp(now):%Y-%m-%d %H:%M}\n")
    print("\n".join(lat))
    from config.settings import settings

    credits = usage_credits(settings.ELEVENLABS_API_KEY, since, now) \
        if settings.ELEVENLABS_API_KEY and since else {}
    print("\n".join(spend_section(credits, minutes, a.usd_per_1k_credits)))

    from core.brain import log as brain_log
    from emma.brain import report

    decisions: list[dict[str, Any]] = [
        dict(r) for r in brain_log.read(brain_log.DEFAULT_PATH)
        if isinstance(r.get("ts"), int | float) and float(r["ts"]) >= since  # type: ignore[arg-type]
    ]
    print()
    print(report(decisions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
