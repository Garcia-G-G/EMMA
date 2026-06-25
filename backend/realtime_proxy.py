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

from backend import db
from backend.config import settings
from backend.session import decode_token

router = APIRouter()


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Estimate session cost from Realtime token usage."""
    return round(
        tokens_in / 1_000_000 * settings.COST_PER_M_INPUT
        + tokens_out / 1_000_000 * settings.COST_PER_M_OUTPUT,
        4,
    )


@router.websocket("/realtime")
async def realtime(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    try:
        claims = decode_token(token)
    except Exception:
        await ws.close(code=4401)  # invalid/expired session token
        return

    await ws.accept()
    sid = str(claims["sid"])
    max_seconds = int(claims.get("max_seconds", settings.DEMO_SESSION_SECONDS))
    started = time.time()
    usage = {"in": 0, "out": 0}

    oai_url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    # GA Realtime API as of 2026-06: NO OpenAI-Beta header. Beta shape was deprecated.
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

    try:
        async with websockets.connect(oai_url, additional_headers=headers, max_size=None) as oai:
            async def client_to_openai() -> None:
                while True:
                    await oai.send(await ws.receive_text())

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
                await asyncio.sleep(max_seconds)
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
