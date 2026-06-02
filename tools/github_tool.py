"""GitHub repo search for Emma's voice flow.

Two tools:

- ``search_github(query, limit=5)`` returns up to 5 matching public
  repositories. Each match has ``name``, ``full_name``, ``url``,
  ``clone_url``, ``description``, ``stars``, ``language``.

- ``get_repo_url(query)`` is a convenience wrapper returning the single top
  match's ``clone_url`` (or the failure result). Used when Emma chains
  search → clone in one voice command.

A ``GITHUB_TOKEN`` env var is honored if present (raises the rate limit from
60/hr to 5000/hr). It is treated as a credential by the 15.6 Keychain migration.
"""

from __future__ import annotations

import httpx
import structlog

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.github")

_API = "https://api.github.com/search/repositories"
_TIMEOUT = 8.0


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "emma/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = settings.GITHUB_TOKEN
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@tool()
async def search_github(query: str, limit: int = 5) -> ToolResult:
    """Search GitHub public repositories by name or keyword.

    Use when Garcia says any of:
    - "Emma, busca el repo X en GitHub"
    - "Emma, búscame un repo de Y"
    - "Emma, ¿hay un proyecto open source de Z?"
    """
    q = (query or "").strip()
    if not q:
        return ToolResult(False, None, "Dime qué buscar.", False)
    params: dict[str, str | int] = {"q": q, "per_page": max(1, min(limit, 10)), "sort": "stars"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
            r = await cli.get(_API, headers=_headers(), params=params)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            return ToolResult(
                False,
                None,
                "GitHub me limitó el ritmo. Espera unos minutos o agrega un GITHUB_TOKEN al .env.",
                False,
            )
        r.raise_for_status()
    except Exception as exc:
        log.error("github_search_failed", error=str(exc))
        return ToolResult(False, None, f"No pude buscar en GitHub: {exc}", False)

    items = r.json().get("items", [])
    if not items:
        return ToolResult(True, {"matches": []}, f"No encontré repos para '{q}'.", False)

    matches = [
        {
            "name": it["name"],
            "full_name": it["full_name"],
            "url": it["html_url"],
            "clone_url": it["clone_url"],
            "description": (it.get("description") or "")[:160],
            "stars": it.get("stargazers_count", 0),
            "language": it.get("language") or "",
        }
        for it in items[:limit]
    ]
    summary = "\n".join(
        f"{i + 1}. {m['full_name']} ({m['stars']}★) — {m['description'] or 'sin descripción'}"
        for i, m in enumerate(matches)
    )
    return ToolResult(
        True,
        {"matches": matches, "top": matches[0]},
        f"Encontré {len(matches)}:\n{summary}",
        False,
    )


@tool()
async def get_repo_url(query: str) -> ToolResult:
    """Resolve a repo query to its top clone URL. Used when Garcia chains
    'busca X y clónalo' in one breath."""
    res = await search_github(query, limit=1)
    if not res.success or not (res.data and res.data.get("matches")):
        return res
    top = res.data["matches"][0]
    return ToolResult(
        True,
        {"clone_url": top["clone_url"], "full_name": top["full_name"]},
        f"{top['full_name']}",
        False,
    )
