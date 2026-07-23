"""CLIENT-INSTALL Phase 2A — OpenAI-compatible HTTP proxy (managed key).

Daemon HTTP OpenAI calls (chat/completions, embeddings, images, responses) hit
``api.theemmafamily.com/v1/*`` with ``Authorization: Bearer <device token>``. We
swap in Emma's upstream key, stream the response through untouched, and meter token
usage into ``usage_events``. Adapted to raw sqlite (the prompt's SQLAlchemy
``get_db``/``Session``/``device.user`` don't exist here).
"""
from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from backend import metering
from backend.config import settings
from backend.device_pairing import resolve_token

router = APIRouter(prefix="/v1", tags=["openai-proxy"])
_UPSTREAM = "https://api.openai.com/v1"
_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
# Hop-by-hop / rewritten headers we never forward in either direction.
_STRIP = {"authorization", "host", "content-length", "content-encoding", "transfer-encoding",
          "accept-encoding"}


async def close_client() -> None:
    """Release the shared upstream connection pool during app shutdown."""
    await _CLIENT.aclose()


def _authorize(req: Request) -> dict[str, Any]:
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer")
    device = resolve_token(auth.split(" ", 1)[1].strip())
    if not device:
        raise HTTPException(401, "invalid token")
    return device


def _upstream_headers(req: Request) -> dict[str, str]:
    """Client headers minus Authorization/Host, plus Emma's real key."""
    h = {k: v for k, v in req.headers.items() if k.lower() not in _STRIP}
    h["Authorization"] = f"Bearer {settings.OPENAI_API_KEY}"
    h["Accept-Encoding"] = "identity"  # forward plain bytes (no br/gzip to decode)
    return h


def _meter(device: dict[str, Any], usage: dict[str, Any], model: str, kind: str) -> None:
    if not usage:
        return
    itd = usage.get("prompt_tokens_details") or {}
    with contextlib.suppress(Exception):
        metering.record_usage_tokens(
            device["user_id"], device["id"], kind=kind, model=model,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cached_tokens=int(itd.get("cached_tokens", 0)),
            audio_tokens=int(itd.get("audio_tokens", 0)))


@router.api_route("/{path:path}", methods=["POST", "GET", "DELETE"])
async def proxy(path: str, req: Request) -> Response:
    device = _authorize(req)
    body = await req.body()
    url = f"{_UPSTREAM}/{path}"
    headers = _upstream_headers(req)
    params = dict(req.query_params)

    is_stream = False
    if body:
        with contextlib.suppress(Exception):
            is_stream = bool(json.loads(body).get("stream"))

    if not is_stream:
        r = await _CLIENT.request(req.method, url, content=body, headers=headers, params=params)
        with contextlib.suppress(Exception):
            data = r.json()
            _meter(device, data.get("usage") or {}, data.get("model") or "", "http")
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in _STRIP}
        return Response(content=r.content, status_code=r.status_code,
                        headers=resp_headers, media_type=r.headers.get("content-type"))

    async def gen():
        last = b""
        async with _CLIENT.stream(req.method, url, content=body, headers=headers, params=params) as r:
            if r.status_code != 200:
                yield await r.aread()
                return
            buf = b""
            async for chunk in r.aiter_raw():
                yield chunk
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line.startswith(b"data: ") and line != b"data: [DONE]":
                        last = line[6:]
        with contextlib.suppress(Exception):
            frame = json.loads(last)
            _meter(device, frame.get("usage") or {}, frame.get("model") or "", "http-stream")

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
