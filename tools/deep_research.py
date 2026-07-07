"""Deep research (Prompt 33): search → fetch → read → synthesize with citations.

Where ``search_web`` reads titles back, ``deep_research`` fetches the top trusted
sources, extracts their main text (trafilatura), and asks gpt-4o-mini for a 2-3
sentence Spanish answer that cites sources by ``[n]``. Single-shot (no follow-ups).

Seams ``_fetch_text`` / ``_synthesize`` / ``search_results`` are module-level so
tests can stub the network + LLM and exercise the pipeline deterministically.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core import research_budget
from core.redaction import redact
from tools.base import ToolResult, tool
from tools.web import search_results

log = structlog.get_logger("emma.tools.deep_research")

_FETCH_TIMEOUT_S = 8.0
_MAX_CHARS = 2000

# Aggregators / low-signal hosts: prefer original sources over these.
_AGGREGATORS = (
    "google.", "bing.com", "duckduckgo.", "news.google.", "reddit.com",
    "quora.com", "pinterest.", "facebook.com", "msn.com", "yahoo.com",
)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key(), base_url=settings.openai_base_url())
    return _client


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _rank(candidates: list[dict[str, str]], depth: int) -> list[dict[str, str]]:
    """Top `depth` candidates, original sources first, aggregators only as backfill."""
    preferred: list[dict[str, str]] = []
    rest: list[dict[str, str]] = []
    for c in candidates:  # one pass; preserves duplicates, no O(n^2) membership test
        bucket = rest if any(a in _domain(c.get("url", "")) for a in _AGGREGATORS) else preferred
        bucket.append(c)
    return (preferred + rest)[:depth]


async def _fetch_text(url: str) -> str:
    """Fetch + extract main text, truncated to ~2K chars. Returns "" on any failure
    (SSRF-blocked, timeout, HTTP error, empty extraction) so one bad source never
    sinks the call. URLs come from a web search → the SSRF guard is load-bearing."""
    try:
        import trafilatura

        from core.url_safety import safe_get_text
    except ImportError:
        return ""
    try:
        html = await safe_get_text(
            url, timeout=_FETCH_TIMEOUT_S, headers={"User-Agent": "Mozilla/5.0 Emma-Assistant"}
        )
    except Exception as exc:
        log.info("deep_research_fetch_skipped", url=url, error=str(exc)[:120])
        return ""
    body = (trafilatura.extract(html, include_comments=False, include_tables=False) or "").strip()
    return body[:_MAX_CHARS]


async def _synthesize(query: str, triplets: list[tuple[int, dict[str, str], str]]) -> str:
    """gpt-4o-mini: a 2-3 sentence Spanish answer that cites sources by [n]."""
    blocks = redact("\n\n".join(  # egress guard: strip secrets/PII before sources leave
        f"[{n}] {c.get('title', '')} ({c.get('url', '')})\n{text}" for n, c, text in triplets
    ))
    completion = await _get_client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Sintetiza una respuesta de 2 a 3 oraciones a la consulta, "
                    "usando solo las fuentes dadas. Cita las fuentes por [n] al final "
                    "de cada afirmación. Responde en español, en tono hablado y breve. "
                    "Si las fuentes no responden, dilo con honestidad."
                ),
            },
            {"role": "user", "content": f"Consulta: {redact(query)}\n\nFuentes:\n{blocks}"},
        ],
        timeout=settings.API_TIMEOUT_S,
        temperature=0.3,
    )
    return (completion.choices[0].message.content or "").strip()


@tool()
async def deep_research(query: str, depth: int = 3) -> ToolResult:
    """Investiga a fondo: busca, lee las mejores fuentes y sintetiza con citas.

    Para "¿qué pasó con X?", "investígame Y", "resume lo último de Z". Lee el
    contenido real de las páginas (no solo títulos) y responde en 2-3 oraciones
    citando las fuentes por [n]. `depth` = cuántas fuentes leer (1-5).
    """
    depth = max(1, min(int(depth), 5))
    if not query.strip():
        return ToolResult(False, None, "¿Qué quieres que investigue?", False)

    if not research_budget.can_run():
        return ToolResult(
            False, {"used": research_budget.usage_today()},
            f"Llegué al límite de {research_budget.cap()} investigaciones por hoy. "
            "Lo retomamos mañana.", False,
        )

    candidates = await search_results(query, depth + 2)
    if not candidates:
        return ToolResult(False, None, "No encontré fuentes para investigar eso.", False)

    chosen = _rank(candidates, depth)
    texts = await asyncio.gather(*(_fetch_text(c.get("url", "")) for c in chosen))
    triplets = [(i + 1, c, t) for i, (c, t) in enumerate(zip(chosen, texts, strict=False)) if t]
    if not triplets:
        return ToolResult(False, None, "Encontré fuentes pero no pude leer su contenido.", False)

    try:
        answer = await _synthesize(query, triplets)
    except Exception as exc:
        log.error("deep_research_synth_failed", error=str(exc))
        return ToolResult(False, None, "Pude leer las fuentes pero falló la síntesis.", False)

    research_budget.record()
    sources = [{"n": n, "title": c.get("title", ""), "url": c.get("url", "")} for n, c, _ in triplets]
    return ToolResult(True, {"answer": answer, "sources": sources, "cost_usd": 0.01}, answer, False)
