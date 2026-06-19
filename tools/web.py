"""Web search, page summarization, and URL open.

Search backend: Brave when ``BRAVE_API_KEY`` is set, Tavily as a fallback
if only that key is configured. Page summaries use trafilatura to pull
the main content, then GPT-4o-mini for a three-sentence rewrite.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from openai import AsyncOpenAI

from actions import macos
from config.settings import settings
from core.redaction import redact
from tools.base import ToolResult, tool


def _user_lang_name() -> str:
    """Garcia's configured language as a prompt-friendly name (21-B26)."""
    from core import dictionary

    lang = dictionary.user_profile().get("preferred_lang", "es") or "es"
    return "English" if lang == "en" else "Spanish"


log = structlog.get_logger("emma.tools.web")

_BRAVE = "https://api.search.brave.com/res/v1/web/search"
_TAVILY = "https://api.tavily.com/search"

_summary_client: AsyncOpenAI | None = None


def _get_summary_client() -> AsyncOpenAI:
    global _summary_client
    if _summary_client is None:
        _summary_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _summary_client


def _format_brave(payload: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in payload.get("web", {}).get("results", [])[:5]:
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
            }
        )
    return out


def _format_tavily(payload: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in payload.get("results", [])[:5]:
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
        )
    return out


async def search_results(query: str, count: int = 5) -> list[dict[str, str]]:
    """Raw search candidates (title/url/snippet) for `query`.

    Shared by ``search_web`` and ``deep_research`` (Prompt 33). Returns an empty
    list on any error or when no search credentials are configured — callers
    decide how to surface that.
    """
    count = max(1, min(int(count), 10))
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        if settings.BRAVE_API_KEY:
            try:
                r = await client.get(
                    _BRAVE,
                    params={"q": query, "count": str(count)},
                    headers={"X-Subscription-Token": settings.BRAVE_API_KEY, "Accept": "application/json"},
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("brave_search_failed", error=str(exc))
                return []
            return _format_brave(r.json())[:count]
        if settings.TAVILY_API_KEY:
            try:
                r = await client.post(
                    _TAVILY,
                    json={"api_key": settings.TAVILY_API_KEY, "query": query, "max_results": count},
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("tavily_search_failed", error=str(exc))
                return []
            return _format_tavily(r.json())[:count]
    return []


@tool()
async def search_web(query: str) -> ToolResult:
    """Search the web for `query` and return a short spoken synthesis plus the top results."""
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        if settings.BRAVE_API_KEY:
            try:
                r = await client.get(
                    _BRAVE,
                    params={"q": query, "count": "5"},
                    headers={
                        "X-Subscription-Token": settings.BRAVE_API_KEY,
                        "Accept": "application/json",
                    },
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                return ToolResult(False, None, f"Brave falló: {exc}", False)
            results = _format_brave(r.json())
        elif settings.TAVILY_API_KEY:
            try:
                r = await client.post(
                    _TAVILY,
                    json={
                        "api_key": settings.TAVILY_API_KEY,
                        "query": query,
                        "max_results": 5,
                        "include_answer": True,
                    },
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                return ToolResult(False, None, f"Tavily falló: {exc}", False)
            payload = r.json()
            results = _format_tavily(payload)
            if payload.get("answer"):
                return ToolResult(
                    True,
                    {"answer": payload["answer"], "results": results},
                    payload["answer"],
                    False,
                )
        else:
            return ToolResult(
                False,
                None,
                "No tengo credenciales de búsqueda web configuradas todavía.",
                False,
            )

    if not results:
        return ToolResult(False, None, f"No encontré nada para '{query}'.", False)

    snippets = redact("\n".join(f"- {r['title']}: {r['snippet']}" for r in results))  # egress guard
    try:
        completion = await _get_summary_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You synthesize web search results into one or two short "
                        "spoken sentences. Match the language of the query."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Query: {query}\n\nResults:\n{snippets}",
                },
            ],
            timeout=settings.API_TIMEOUT_S,
            temperature=0.3,
        )
        answer = (completion.choices[0].message.content or "").strip()
    except Exception as exc:
        log.error("search_synth_failed", error=str(exc))
        answer = results[0]["snippet"] or results[0]["title"]
    return ToolResult(True, {"answer": answer, "results": results}, answer, False)


@tool()
def open_url(url: str) -> ToolResult:
    """Open a URL in the user's default browser."""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        macos.open_url(url)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude abrir el navegador: {exc}", False)
    return ToolResult(True, {"url": url}, f"Abriendo {url}.", False)


@tool()
async def summarize_page(url: str) -> ToolResult:
    """Fetch a web page and return a three-sentence spoken summary."""
    try:
        import trafilatura

        from core.url_safety import safe_get_text
    except ImportError:
        return ToolResult(False, None, "Falta trafilatura.", False)
    try:
        html = await safe_get_text(
            url, timeout=settings.API_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 Emma-Assistant"},
        )
    except ValueError:
        return ToolResult(False, None, "No puedo leer esa dirección.", False)
    except httpx.HTTPError as exc:
        return ToolResult(False, None, f"No pude leer la página: {exc}", False)
    body = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    body = body.strip()
    if not body:
        return ToolResult(False, None, "La página no tenía texto que pudiera extraer.", False)
    excerpt = redact(body[:6000])  # egress guard: strip secrets/PII before the page leaves
    try:
        completion = await _get_summary_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    # 21-B26: the USER's language governs, never the article's —
                    # an English page summarized for a Spanish ask answers in Spanish.
                    "content": (
                        "Summarize the article in three short spoken sentences. "
                        f"Answer in {_user_lang_name()}, regardless of the "
                        "article's language. Keep names and URLs verbatim."
                    ),
                },
                {"role": "user", "content": excerpt},
            ],
            timeout=settings.API_TIMEOUT_S,
            temperature=0.3,
        )
        summary = (completion.choices[0].message.content or "").strip()
    except Exception as exc:
        return ToolResult(False, None, f"El resumen falló: {exc}", False)
    return ToolResult(True, {"url": url, "summary": summary}, summary, False)
