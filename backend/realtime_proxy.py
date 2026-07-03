"""WebSocket proxy to the OpenAI Realtime API (Prompt 31, A2).

The browser connects to ``/realtime?token=<jwt>``; the backend validates the JWT,
opens its OWN socket to OpenAI with the master key, and relays messages both ways.
The OpenAI key NEVER reaches the client. The 2-min cap is enforced here, server-side
(a timer task), so a tampered client clock can't extend it. Usage is tallied from
OpenAI ``response.done`` events and written to the session row on close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import websockets
from fastapi import APIRouter, WebSocket

from backend import db, metering
from backend.config import PLAN_CAPS, settings
from backend.device_pairing import resolve_token
from backend.session import decode_token

router = APIRouter()


def _is_client_session_frame(raw: str) -> bool:
    """True if a browser frame tries to reconfigure the session (instructions, tools,
    voice, model). Those must be dropped — only our server-side session.update is trusted."""
    try:
        return str(json.loads(raw).get("type", "")).startswith("session.")
    except Exception:
        return False


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Estimate session cost from Realtime token usage."""
    return round(
        tokens_in / 1_000_000 * settings.COST_PER_M_INPUT
        + tokens_out / 1_000_000 * settings.COST_PER_M_OUTPUT,
        4,
    )


@router.websocket("/realtime")
async def realtime(ws: WebSocket) -> None:
    """Two clients share this endpoint (two clear paths, no branching merge):

    - **Daemon** (managed voice): presents ``Authorization: Bearer <device token>``;
      routed to :func:`_device_realtime` (resolves the device, enforces the plan
      cap, meters seconds into ``usage_events``).
    - **Web demo**: presents ``?token=<JWT>``; routed to :func:`_demo_realtime`
      (unchanged 60-second flow).
    """
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        await _device_realtime(ws, auth.split(" ", 1)[1].strip())
    else:
        await _demo_realtime(ws)


async def _device_realtime(ws: WebSocket, token: str) -> None:
    """Managed voice: daemon → backend → OpenAI. Backend holds OUR key and meters.

    The daemon is a trusted first-party client (unlike the browser demo), so its
    ``session.*`` frames ARE forwarded — that's how Pipecat configures voice/tools.
    """
    device = resolve_token(token)
    if not device:
        await ws.close(code=4401, reason="invalid token")
        return
    user = db.get_user(device["user_id"])
    plan = (user or {}).get("plan", "free")
    cap = PLAN_CAPS.get(plan, PLAN_CAPS["free"])
    # Managed monthly allowance reuses monthly_seconds (free=0 → no managed minutes).
    monthly_cap_s = int(cap.get("monthly_seconds", 0) or 0)
    if metering.seconds_used_this_month(device["user_id"]) >= monthly_cap_s:
        await ws.close(code=4402, reason="monthly cap exceeded")
        return
    session_max = int(cap.get("daemon_session_max_seconds", 1800))

    await ws.accept()
    started = time.monotonic()
    oai_url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
    try:
        async with websockets.connect(oai_url, additional_headers=headers, max_size=None) as oai:
            async def client_to_openai() -> None:
                while True:
                    await oai.send(await ws.receive_text())

            async def openai_to_client() -> None:
                async for msg in oai:
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            async def hard_timeout() -> None:
                await asyncio.sleep(session_max)  # server-side session ceiling
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "emma.session_expired"}))

            tasks = {asyncio.create_task(t())
                     for t in (client_to_openai, openai_to_client, hard_timeout)}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        seconds = int(min(time.monotonic() - started, float(session_max)))
        with contextlib.suppress(Exception):
            metering.record_usage(device["user_id"], device["id"], seconds)
        with contextlib.suppress(Exception):
            await ws.close()


async def _demo_realtime(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    try:
        claims = decode_token(token)
    except Exception:
        await ws.close(code=4401)  # invalid/expired session token
        return

    if claims.get("kind") not in ("demo", "user"):
        await ws.close(code=4401)
        return

    await ws.accept()
    sid = str(claims["sid"])
    max_seconds = int(claims.get("max_seconds", settings.DEMO_SESSION_SECONDS))
    # Hard $/session ceiling — signed into the token so the client can't widen it.
    cost_cap = int(claims.get("cost_cap_cents") or settings.DEMO_COST_CAP_CENTS) / 100.0
    started = time.time()
    usage = {"in": 0, "out": 0}

    oai_url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    # GA Realtime API as of 2026-06: NO OpenAI-Beta header. Beta shape was deprecated.
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

    try:
        async with websockets.connect(oai_url, additional_headers=headers, max_size=None) as oai:
            async def client_to_openai() -> None:
                while True:
                    raw = await ws.receive_text()
                    # NEVER let the browser reconfigure the session (instructions, tools,
                    # voice, model). Only OUR session.update — sent server-side — is trusted.
                    if _is_client_session_frame(raw):
                        continue
                    await oai.send(raw)

            async def openai_to_client() -> None:
                async for msg in oai:
                    with contextlib.suppress(Exception):
                        ev = json.loads(msg)
                        if ev.get("type") == "response.done":
                            u = ev.get("response", {}).get("usage", {})
                            usage["in"] += int(u.get("input_tokens", 0))
                            usage["out"] += int(u.get("output_tokens", 0))
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            async def hard_timeout() -> None:
                # Wake periodically so the cost cap can also end the session early.
                while time.time() - started < max_seconds:
                    if cost_usd(usage["in"], usage["out"]) >= cost_cap:
                        break
                    await asyncio.sleep(1.0)
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "emma.session_expired"}))

            tasks = {asyncio.create_task(t()) for t in (client_to_openai, openai_to_client, hard_timeout)}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        seconds = min(time.time() - started, float(max_seconds))
        db.end_session(sid, seconds, usage["in"], usage["out"], cost_usd(usage["in"], usage["out"]))
        with contextlib.suppress(Exception):
            await ws.close()
