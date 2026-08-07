"""Emma real-time dashboard server.

Run: .venv/bin/python dashboard/server.py
Open: http://localhost:3200
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
import websockets
from websockets.asyncio.server import serve

# Ensure the repo root is importable when this is run standalone
# (`python dashboard/server.py` only puts dashboard/ on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import settings
from core import events_bus

log = structlog.get_logger("emma.dashboard")

# The log the daemon actually writes (emma/__main__.py:_setup_logging). This was
# /tmp/emma_session.log, which NOTHING in the tree ever wrote — so every session
# and cost stat on this dashboard was permanently zero on an installed machine,
# and looked like "Emma has done nothing" rather than "this file doesn't exist".
EMMA_LOG = Path.home() / "Library" / "Logs" / "Emma" / "emma.log"
EMMA_HOME = Path.home() / ".emma"
MEMORY_DB = EMMA_HOME / "memory.db"
CRASH_DIR = Path.home() / "Library/Logs/Emma/crashes"
REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
# Honor a DASHBOARD_PORT override: the daemon passes EMMA_DASHBOARD_PORT to the
# UI from this same setting, so a hardcoded 3200 would split the UI from the server.
PORT = settings.DASHBOARD_PORT

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
        # `id` matters: the Memoria panel deletes by row id, so a fact whose text
        # repeats (or contains anything sqlite would need escaped) still deletes
        # exactly the row the user pointed at.
        rows = conn.execute(
            "SELECT id, content, kind, confidence, source, last_seen_at FROM facts "
            "ORDER BY confidence DESC, last_seen_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _wake_word_card() -> dict:
    """Wake-word status card reflecting the live engine config (Prompt 16)."""
    from config.settings import settings

    engine = (settings.WAKE_WORD_ENGINE or "openwakeword").lower()
    if engine == "pvporcupine":
        ppn = Path(settings.WAKE_WORD_PATH)
        if not ppn.is_absolute():
            ppn = Path(__file__).resolve().parent.parent / ppn
        if ppn.exists():
            return {
                "id": "WAKE-02",
                "severity": "info",
                "status": "closed",
                "title": "Custom Picovoice 'Emma' wake word active",
                "detail": (
                    f"Engine pvporcupine, keyword '{settings.WAKE_WORD_NAME}', "
                    f"sensitivity {settings.WAKE_WORD_THRESHOLD}."
                ),
            }
        return {
            "id": "WAKE-02",
            "severity": "medium",
            "status": "open",
            "title": "Picovoice wake word configured but model file missing",
            "detail": (
                f"WAKE_WORD_ENGINE=pvporcupine but no .ppn at {ppn}. Train 'Emma' "
                "in the Picovoice Console and drop it at wake_words/emma.ppn."
            ),
        }
    return {
        "id": "WAKE-02",
        "severity": "low",
        "status": "open",
        "title": "Using built-in hey_jarvis (openWakeWord fallback)",
        "detail": (
            "Default engine. Switch to the custom 'Emma' wake word by training a "
            ".ppn in the Picovoice Console and setting WAKE_WORD_ENGINE=pvporcupine "
            "in .env."
        ),
    }


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
        _wake_word_card(),
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


def _permissions_card() -> dict:
    """Screen-vision permissions, the pair that silently killed the feature.

    Reported from the dashboard process, so it answers "does THIS process have
    AX" — read it as an indicator of the grant's health, not of the daemon's
    own identity when the two run as different executables.
    """
    try:
        from core import permissions

        ax = permissions.check_accessibility_ax()
        rec = permissions.check_screen_recording()
        smoke_ok, smoke_reason = permissions.ax_smoke()
    except Exception as exc:
        return {"ok": None, "error": str(exc)}
    healthy = ax and smoke_ok
    return {
        "ok": bool(healthy and rec),
        "accessibility": ax,
        "accessibility_smoke": smoke_reason,
        "screen_recording": rec,
        "detail": (
            "screen vision ready"
            if healthy and rec
            else "Accessibility missing — screen vision returns 'no veo una ventana'"
            if not healthy
            else "Screen Recording missing — look_at_screen captures a blank desktop"
        ),
    }


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
        "permissions": _permissions_card(),
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


def _tools_count() -> int:
    try:
        from tools.registry import openai_tool_specs

        return len(openai_tool_specs())
    except Exception:
        return 0


async def events_handler(ws):
    """Forward curated events_bus payloads to the JARVIS visualizer (/events).

    Sends an `init` payload immediately (static fields the page can't infer),
    then streams whatever the in-process bus publishes. Lossy + best-effort.
    """
    q = events_bus.subscribe()
    try:
        await ws.send(
            json.dumps({"type": "init", "tools_count": _tools_count(), "vad_threshold": 0.75})
        )
        while True:
            payload = await q.get()
            await ws.send(json.dumps(payload))
    except websockets.ConnectionClosed:
        pass
    except Exception:
        pass  # any send error -> end cleanly
    finally:
        events_bus.unsubscribe(q)


def _control_status() -> dict:
    """Current daemon listening state, for the menubar UI to reflect."""
    from core import orchestrator

    return {
        "muted": orchestrator.is_muted(),
        "snooze_remaining_s": int(orchestrator.snooze_remaining_s()),
    }


# Last balance the backend served, kept so Uso can degrade to a stale-but-labelled
# number instead of an error page when the network is down (LAUNCH-7 Part 3).
_last_balance: dict | None = None
_last_balance_at: float | None = None


async def _device_balance() -> dict:
    """Uso's data, via the device bearer. Never raises; degrades with a timestamp.

    Reads GET /api/device/balance — the route added for exactly this, because
    /api/balance is cookie-authed and the daemon has no cookie.
    """
    global _last_balance, _last_balance_at
    from core import pairing

    try:
        client = await pairing.authed_client()
    except Exception as exc:  # not paired yet
        return {"ok": False, "reason": "unpaired", "error": str(exc),
                "balance": _last_balance, "stale_at": _last_balance_at}
    try:
        async with client as c:
            r = await c.get("/api/device/balance")
            r.raise_for_status()
            _last_balance = r.json()
            _last_balance_at = time.time()
            return {"ok": True, "balance": _last_balance, "stale_at": None}
    except Exception as exc:
        # Offline / backend down: show what we last knew, labelled with when.
        log.warning("device_balance_failed", error=str(exc))
        return {"ok": False, "reason": "offline", "error": str(exc),
                "balance": _last_balance, "stale_at": _last_balance_at}


async def _pair_start() -> dict:
    """Onboarding step 2 — mint a device code for the browser hand-off."""
    from core import pairing

    try:
        info = await pairing.start_pairing()
    except Exception as exc:
        log.warning("pair_start_failed", error=str(exc))
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "user_code": info.get("user_code"),
        "device_code": info.get("device_code"),
        "interval": int(info.get("interval", 5)),
        "expires_in": int(info.get("expires_in", 600)),
        # The app opens this in the SYSTEM BROWSER, never in the WebView: it is
        # the full web auth stack (Google/GitHub OAuth included) and a password
        # must never be typed into a window Emma controls.
        "verify_url": f"https://theemmafamily.com/pair?code={info.get('user_code', '')}",
    }


async def _pair_poll(device_code: str, interval: int, expires_in: int) -> dict:
    """One non-blocking exchange attempt, so the UI can show progress + retry."""
    from core import pairing

    if not device_code:
        return {"ok": False, "status": "denied", "error": "device_code required"}
    status, data = await pairing.poll_once(device_code)
    out: dict = {"ok": status == "paired", "status": status,
                 "interval": interval, "expires_in": expires_in}
    if status == "paired" and data:
        out["user"] = (data.get("user") or {}).get("email")
    return out


async def dispatch_control(msg: dict) -> dict:
    """Execute one UI control command against the live daemon (EMMA-APP Part 3).

    Runs only in the in-daemon dashboard (EMMA_DASHBOARD=1), where these calls hit
    the SAME orchestrator module the wake loop reads — so a menubar "unmute" click
    is the way back that voice can't provide once the mic is off. Commands arrive
    over a loopback-only socket, so a local click is trusted-because-local (it is
    not content Emma read); destructive intent (shutdown) is confirmed UI-side.
    """
    from core import orchestrator

    cmd = str(msg.get("cmd", "")).strip()
    try:
        if cmd == "unmute":
            orchestrator.unmute_mic()
        elif cmd == "mute":
            orchestrator.mute_mic()
        elif cmd == "snooze":
            orchestrator.snooze_listening(int(msg.get("minutes", 15)))
        elif cmd == "stop":
            from core import conversation

            await conversation.stop_active_speech()
        elif cmd == "shutdown":
            from core import dev_state

            dev_state.shutdown_requested.set()
        elif cmd == "request_accessibility":
            # Recover a declined Accessibility grant FROM THE APP (LAUNCH-2.1 Item 2)
            # — not only by re-running a shell command. This runs in the daemon,
            # which (as EmmaDaemon.app) is the correct TCC subject, so the alert and
            # the resulting row belong to Emma, not to whatever launched the UI.
            from core import permissions

            granted = permissions.request_accessibility_trust()
            permissions._open_settings("Accessibility")
            return {
                "type": "control_result", "ok": True, "cmd": cmd,
                "granted": bool(granted), **_control_status(),
            }
        elif cmd == "set_personality":
            # Writes the [personality] block in config/dictionary.toml. The system
            # prompt is built ONCE per session (_build_instructions at session
            # start), so this lands on the NEXT wake, never mid-conversation. The
            # panel says so; `applies` carries it back so the UI can't drift from
            # the truth.
            from core import dictionary

            field = str(msg.get("field", ""))
            ok = dictionary.set_personality_field(field, int(msg.get("value", 3)))
            return {
                "type": "control_result", "ok": ok, "cmd": cmd, "field": field,
                "profile": dictionary.personality_profile(),
                "applies": "next_wake",
                **_control_status(),
            }
        elif cmd == "personality":
            from core import dictionary

            return {
                "type": "control_result", "ok": True, "cmd": cmd,
                "profile": dictionary.personality_profile(),
                "applies": "next_wake", **_control_status(),
            }
        elif cmd == "forget":
            # Real deletion from memory.db, not a UI filter — the Memoria panel is
            # the thing that makes Emma trustworthy, so "delete" has to mean it.
            from memory import long_term

            target: str | int = msg.get("id") if msg.get("id") is not None else str(msg.get("content", ""))
            removed = await long_term.forget(target)
            return {
                "type": "control_result", "ok": removed > 0, "cmd": cmd,
                "removed": removed, **_control_status(),
            }
        elif cmd == "facts":
            return {
                "type": "control_result", "ok": True, "cmd": cmd,
                "facts": _memory_facts(), **_control_status(),
            }
        elif cmd == "balance":
            return {"type": "control_result", "ok": True, "cmd": cmd,
                    **(await _device_balance()), **_control_status()}
        elif cmd == "pair_start":
            return {"type": "control_result", "cmd": cmd, **(await _pair_start())}
        elif cmd == "pair_poll":
            return {"type": "control_result", "cmd": cmd,
                    **(await _pair_poll(str(msg.get("device_code", "")),
                                        int(msg.get("interval", 5)),
                                        int(msg.get("expires_in", 600))))}
        elif cmd == "version":
            from core import version as _v

            return {"type": "control_result", "ok": True, "cmd": cmd,
                    **(await _v.staleness()), **_control_status()}
        elif cmd == "unpair":
            from core import pairing

            await pairing.revoke_local()
            return {"type": "control_result", "ok": True, "cmd": cmd, **_control_status()}
        elif cmd == "status":
            pass  # just report state below
        else:
            return {"type": "control_result", "ok": False, "error": f"unknown cmd: {cmd}"}
    except Exception as exc:  # never let a bad command kill the socket
        return {"type": "control_result", "ok": False, "cmd": cmd, "error": str(exc)}
    return {"type": "control_result", "ok": True, "cmd": cmd, **_control_status()}


async def control_handler(ws):
    """UI -> daemon control socket (/control). Bidirectional; loopback-only.

    The events bus is one-way (daemon -> UI); this is the missing reverse path
    that EMMA-OBVIOUS flagged. Only reachable on 127.0.0.1 (see start()).
    """
    with contextlib.suppress(Exception):
        await ws.send(json.dumps({"type": "control_hello", **_control_status()}))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            result = await dispatch_control(msg)
            await ws.send(json.dumps(result))
    except websockets.ConnectionClosed:
        pass


def _origin_ok(ws) -> bool:
    """Block cross-site WebSocket hijacking (CSWSH).

    A WebSocket handshake is NOT subject to the same-origin policy, so any web page
    the user visits could open ws://127.0.0.1:{PORT+1}/control and send
    unmute/shutdown — silently re-enabling a mic the user muted, or DoSing Emma.
    The native UI client (python-websockets) sends NO Origin header; browsers ALWAYS
    do. Accept only a missing Origin or our own loopback dashboard origin.
    """
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    origin = headers.get("Origin") if headers is not None else None
    if not origin:
        return True  # native app / non-browser client
    return origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


async def ws_router(ws):
    """Route the WebSocket by path: /events -> bus, /control -> UI commands, else legacy."""
    request = getattr(ws, "request", None)
    path = (getattr(request, "path", None) or "/").split("?")[0].rstrip("/")
    # Guard EVERY socket against foreign browser origins — not just /events and
    # /control. The legacy "/" handler streams build_state(), which includes 30
    # memory.db facts + a live log tail; a foreign page opening ws://127.0.0.1/
    # would otherwise exfiltrate personal memory. Native clients send no Origin.
    if not _origin_ok(ws):
        log.warning("ws_forbidden_origin", path=path)
        with contextlib.suppress(Exception):
            await ws.close(code=1008, reason="forbidden origin")
        return
    if path == "/events":
        await events_handler(ws)
    elif path == "/control":
        await control_handler(ws)
    else:
        await handler(ws)


async def start():
    """Run the dashboard HTTP server (thread) + WebSocket server (this loop) forever.

    Used both by ``__main__`` (standalone) and by the daemon when
    ``EMMA_DASHBOARD`` is truthy. The HTTP handler also routes ``/visualizer``
    to ``visualizer.html``.
    """
    import http.server
    import threading

    class DashHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

        def log_message(self, format, *args):
            pass  # silence

        def end_headers(self):
            # Never let the app window run a cached copy of itself. The window is
            # a WKWebView over this server, so a conditional-GET 304 after an
            # upgrade would keep serving the OLD index.html against a NEW daemon
            # — new control commands answering into a page that cannot call them.
            # Caught during LAUNCH-7 browser verification, where an edited page
            # kept behaving like the previous one.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

        def _rewrite(self):
            if self.path.split("?")[0].rstrip("/") == "/visualizer":
                self.path = "/visualizer.html"

        def do_GET(self):
            self._rewrite()
            return super().do_GET()

        def do_HEAD(self):
            self._rewrite()
            return super().do_HEAD()

    # Loopback-only (EMMA-APP Part 3): the control socket accepts trusted-because-
    # local commands (unmute, shutdown), so it must never be reachable off-box.
    # The local UI + browser reach it fine over 127.0.0.1/localhost.
    # EMMA_DASHBOARD is the control channel the app pairs this Mac through, so a
    # bind failure means onboarding is permanently impossible. It used to raise
    # OSError inside a fire-and-forget create_task, where the exception was never
    # retrieved and died silently — the most expensive way to fail.
    try:
        httpd = http.server.HTTPServer(("127.0.0.1", PORT), DashHandler)
    except OSError as exc:
        log.error(
            "dashboard_bind_failed", port=PORT, error=str(exc),
            impact="the app cannot reach the daemon; pairing and onboarding are "
                   "unavailable until this port is free",
            hint="another Emma daemon or a standalone dashboard/server.py is running",
        )
        events_bus.publish("state", state="dashboard_unavailable")
        raise
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async with serve(ws_router, "127.0.0.1", PORT + 1):
        print(f"  Dashboard:   http://localhost:{PORT}")
        print(f"  Visualizer:  http://localhost:{PORT}/visualizer")
        print(f"  WebSocket:   ws://localhost:{PORT + 1} (/events)")
        print()
        await asyncio.Future()  # run forever


async def main():
    await start()


if __name__ == "__main__":
    print()
    print("  EMMA Dashboard starting...")
    asyncio.run(main())
