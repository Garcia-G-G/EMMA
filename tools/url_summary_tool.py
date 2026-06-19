"""Summarize a web page in two Spanish sentences (Prompt 38-F)."""

from __future__ import annotations

import asyncio

import httpx
import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core.redaction import redact
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.url_summary")


async def _llm_summary(text: str) -> str:
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    completion = await asyncio.wait_for(
        client.chat.completions.create(
            model=settings.MEMORY_REFLECTION_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Resume en español, en máximo 2 frases, de qué trata esta página. "
                    "Sé concreto; usa solo el contenido dado."
                )},
                {"role": "user", "content": redact(text[:8000])},  # egress guard
            ],
            temperature=0.3,
        ),
        timeout=settings.API_TIMEOUT_S,
    )
    return (completion.choices[0].message.content or "").strip()


@tool()
async def summarize_url(url: str) -> ToolResult:
    """Resume de qué trata una página web ("¿de qué va esta URL?", "resúmeme este link").

    Descarga la página, extrae el texto principal y lo sintetiza en 1-2 frases.
    """
    url = (url or "").strip()
    if not url.lower().startswith("http"):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Emma)"})
        html = resp.text
    except Exception as exc:
        log.warning("url_fetch_failed", url=url, error=str(exc))
        return ToolResult(False, None, "No pude abrir esa página.", False)

    import trafilatura

    text = trafilatura.extract(html) or ""
    if len(text.strip()) < 80:
        return ToolResult(False, {"url": url}, "No pude extraer texto legible de esa página.", False)
    try:
        summary = await _llm_summary(text)
    except Exception as exc:
        log.warning("url_summary_llm_failed", error=str(exc))
        return ToolResult(False, None, "Leí la página pero no pude resumirla ahora.", False)
    return ToolResult(True, {"url": url, "chars": len(text)}, summary or "No pude resumirla.", False)
