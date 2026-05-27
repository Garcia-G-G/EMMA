"""Emma real-time dashboard server.

Run: .venv/bin/python dashboard/server.py
Open: http://localhost:3200
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import websockets
from websockets.asyncio.server import serve

EMMA_LOG = Path("/tmp/emma_session.log")
EMMA_HOME = Path.home() / ".emma"
MEMORY_DB = EMMA_HOME / "memory.db"
CRASH_DIR = Path.home() / "Library/Logs/Emma/crashes"
REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
PORT = 3200

# OpenAI Realtime pricing (per million tokens, May 2026)
PRICE_AUDIO_IN = 40.00  # cached input
PRICE_AUDIO_OUT = 80.00
PRICE_TEXT_IN = 2.50
PRICE_TEXT_OUT = 10.00
# Rough estimate: ~50 tokens/second of audio
TOKENS_PER_SEC_AUDIO = 50


def _emma_running() -> dict:
    try:
        out = subprocess.check_output(["pgrep", "-f", "python -m emma"], text=True, timeout=3)
        pids = [int(p) for p in out.strip().split("\n") if p.strip()]
        return {"running": bool(pids), "pid": pids[0] if pids else None}
    except Exception:
        return {"running": False, "pid": None}


def _parse_log_events() -> dict:
    stats = {
        "wake_count": 0,
        "speech_started": 0,
        "speech_stopped": 0,
        "responses": 0,
        "tool_calls": [],
        "errors": [],
        "sessions": 0,
        "audio_out_events": 0,
        "first_event_ts": None,
        "last_event_ts": None,
        "session_durations": [],
    }
    if not EMMA_LOG.exists():
        return stats

    text = EMMA_LOG.read_text(errors="replace")
    for line in text.splitlines():
        if "wake_detected" in line:
            stats["wake_count"] += 1
        if "conversation_start" in line:
            stats["sessions"] += 1
        if "speech_started" in line and "input_audio_buffer" in line:
            stats["speech_started"] += 1
        if "speech_stopped" in line and "input_audio_buffer" in line:
            stats["speech_stopped"] += 1
        if "response.done" in line:
            stats["responses"] += 1
        if "output_audio" in line or "audio.delta" in line:
            stats["audio_out_events"] += 1
        if '"event": "fn_call"' in line:
            m = re.search(r'"name":\s*"([^"]+)"', line)
            if m:
                stats["tool_calls"].append(m.group(1))
        if '"level": "error"' in line or "ERROR" in line:
            stats["errors"].append(line[:200])

        ts_m = re.search(r'"timestamp":\s*"([^"]+)"', line)
        if ts_m:
            ts = ts_m.group(1)
            if stats["first_event_ts"] is None:
                stats["first_event_ts"] = ts
            stats["last_event_ts"] = ts

    return stats


def _estimate_cost(stats: dict) -> dict:
    total_audio_secs = stats["speech_started"] * 5 + stats["responses"] * 3
    tokens_in = stats["speech_started"] * 5 * TOKENS_PER_SEC_AUDIO
    tokens_out = stats["responses"] * 3 * TOKENS_PER_SEC_AUDIO
    cost_in = (tokens_in / 1_000_000) * PRICE_AUDIO_IN
    cost_out = (tokens_out / 1_000_000) * PRICE_AUDIO_OUT
    return {
        "estimated_audio_secs": total_audio_secs,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_in_usd": round(cost_in, 4),
        "cost_out_usd": round(cost_out, 4),
        "total_usd": round(cost_in + cost_out, 4),
    }


def _memory_facts() -> list[dict]:
    if not MEMORY_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(MEMORY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT content, kind, confidence, source FROM facts ORDER BY confidence DESC, last_seen_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _known_issues() -> list[dict]:
    return [
        {
            "id": "AUDIO-01",
            "severity": "critical",
            "status": "fixed",
            "title": "No audio output (coral or ash)",
            "detail": "Root cause: LLMContext frame never pushed + LLMAssistantAggregator missing. Fixed both. Audio works on coral and ash.",
        },
        {
            "id": "ECHO-01",
            "severity": "high",
            "status": "fixed",
            "title": "Echo self-interruption on MacBook speakers",
            "detail": "Echo gate tuned: tail=600ms, barge_in_rms=3000 (echo peaks ~1900). VAD threshold 0.7.",
        },
        {
            "id": "WAKE-01",
            "severity": "low",
            "status": "mitigated",
            "title": "Wake chime leaks into session",
            "detail": "Chime now blocking + 0.8s delay. First speech_started is benign (interrupts nothing). Model recovers.",
        },
        {
            "id": "MEM-01",
            "severity": "medium",
            "status": "partial",
            "title": "Reflection not wired to Pipecat",
            "detail": "Explicit remember_fact works. Priming wired into system prompt. Auto-learning needs transcript event hooks.",
        },
        {
            "id": "YT-01",
            "severity": "low",
            "status": "open",
            "title": "YouTube disambiguation loop",
            "detail": "Exact creator name match added but needs live validation.",
        },
        {
            "id": "WAKE-02",
            "severity": "low",
            "status": "open",
            "title": "Custom 'Hey Emma' wake word not trained",
            "detail": "Using built-in hey_jarvis. Colab training not done yet.",
        },
    ]


def _recent_crashes() -> list[dict]:
    if not CRASH_DIR.exists():
        return []
    crashes = sorted(CRASH_DIR.glob("crash_*.md"), reverse=True)[:5]
    out = []
    for p in crashes:
        out.append({"file": p.name, "time": p.stat().st_mtime, "size": p.stat().st_size})
    return out


def _git_info() -> dict:
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "branch", "--show-current"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
        commit = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--oneline"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
        return {"branch": branch, "commit": commit}
    except Exception:
        return {"branch": "unknown", "commit": "unknown"}


def build_state() -> dict:
    emma = _emma_running()
    stats = _parse_log_events()
    cost = _estimate_cost(stats)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "emma": emma,
        "stats": stats,
        "cost": cost,
        "memory": _memory_facts(),
        "issues": _known_issues(),
        "crashes": _recent_crashes(),
        "git": _git_info(),
    }


async def tail_log(ws):
    """Stream new log lines via WebSocket."""
    if not EMMA_LOG.exists():
        EMMA_LOG.touch()
    proc = await asyncio.create_subprocess_exec(
        "tail",
        "-f",
        str(EMMA_LOG),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").strip()
            if not decoded:
                continue
            # Filter to important events only
            if any(
                k in decoded
                for k in (
                    "wake_detected",
                    "conversation_start",
                    "conversation_end",
                    "speech_started",
                    "speech_stopped",
                    "response.done",
                    "response.created",
                    "output_audio",
                    "fn_call",
                    "echo_gate",
                    "error",
                    "ERROR",
                    "warning",
                    "WARNING",
                    "session_timeout",
                    "session_close",
                    "output_item",
                    "broadcasting",
                    "committed",
                )
            ):
                await ws.send(json.dumps({"type": "log", "line": decoded}))
    except websockets.ConnectionClosed:
        pass
    finally:
        proc.kill()


async def handler(ws):
    # Send initial state
    state = build_state()
    await ws.send(json.dumps({"type": "state", "data": state}))

    # Start log tail in background
    tail_task = asyncio.create_task(tail_log(ws))

    # Periodic state refresh
    try:
        while True:
            await asyncio.sleep(3)
            state = build_state()
            await ws.send(json.dumps({"type": "state", "data": state}))
    except websockets.ConnectionClosed:
        pass
    finally:
        tail_task.cancel()


async def main():
    # Serve static files via a simple HTTP server in a thread
    import http.server
    import threading

    class DashHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

        def log_message(self, format, *args):
            pass  # silence

    httpd = http.server.HTTPServer(("0.0.0.0", PORT), DashHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # WebSocket on PORT+1
    async with serve(handler, "0.0.0.0", PORT + 1):
        print(f"  Dashboard:  http://localhost:{PORT}")
        print(f"  WebSocket:  ws://localhost:{PORT + 1}")
        print()
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    print()
    print("  EMMA Dashboard starting...")
    asyncio.run(main())
