"""Health checks for Emma's external dependencies.

Speakable via the ``describe_my_health`` tool in ``tools/dev.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import structlog
from openai import AsyncOpenAI

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.diagnostics")


async def _ping_openai() -> dict[str, Any]:
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        await asyncio.wait_for(client.models.retrieve("gpt-4o"), timeout=settings.API_TIMEOUT_S)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


async def _ping_elevenlabs() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as c:
            r = await c.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
            )
            r.raise_for_status()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


async def _ping_postgres() -> dict[str, Any]:
    if not settings.POSTGRES_DSN:
        return {"ok": True, "note": "not configured (SQLite fallback)"}
    try:
        import psycopg

        async with (
            await asyncio.wait_for(
                psycopg.AsyncConnection.connect(settings.POSTGRES_DSN),
                timeout=5.0,
            ) as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("SELECT 1")
            await cur.fetchone()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _check_playwright() -> dict[str, Any]:
    """Cheap check: does the Playwright browser cache exist?"""
    cache = Path.home() / "Library/Caches/ms-playwright"
    if cache.exists() and any(cache.glob("chromium-*")):
        return {"ok": True}
    return {
        "ok": False,
        "error": "chromium not installed (run: uv run playwright install chromium)",
    }


def _spoken_summary(data: dict[str, dict[str, Any]]) -> str:
    failing = [name for name, s in data.items() if not s.get("ok")]
    if not failing:
        return "Todo bien. OpenAI, ElevenLabs, Postgres y Playwright responden."
    return "Hay problemas con: " + ", ".join(failing) + "."


@tool()
async def health_check() -> ToolResult:
    """Check Emma's external dependencies (OpenAI, ElevenLabs, Postgres, Playwright)
    and report status per service.
    """
    openai_status, eleven_status, pg_status, pw_status = await asyncio.gather(
        _ping_openai(),
        _ping_elevenlabs(),
        _ping_postgres(),
        asyncio.to_thread(_check_playwright),
    )
    data: dict[str, dict[str, Any]] = {
        "OpenAI": openai_status,
        "ElevenLabs": eleven_status,
        "Postgres": pg_status,
        "Playwright": pw_status,
    }
    success = all(s.get("ok") for s in data.values())
    return ToolResult(
        success=success,
        data=data,
        user_message=_spoken_summary(data),
        requires_confirmation=False,
    )
