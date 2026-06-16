"""Notion append (Prompt 34, Part D) — real Notion API.

Searches for a page by title (disambiguates if several), then appends a paragraph
block to its body. APPEND only — page creation is out of scope. Uses an integration
token (``NOTION_API_KEY``, Keychain-backed). Registered with 26.2.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.notion")

_BASE = "https://api.notion.com/v1"
_VERSION = "2022-06-28"


async def _notion(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Notion API call. Raises on HTTP error. Mockable seam for tests."""
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        r = await client.request(
            method, f"{_BASE}{path}", json=body,
            headers={
                "Authorization": f"Bearer {settings.NOTION_API_KEY}",
                "Notion-Version": _VERSION,
                "Content-Type": "application/json",
            },
        )
        r.raise_for_status()
        return dict(r.json())


def _page_title(page: dict[str, Any]) -> str:
    """Pull the plain-text title out of a Notion page object (best-effort)."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return str(page.get("id", ""))


async def _find_page(title: str) -> tuple[str | None, str, list[str]]:
    """(page_id, resolved_title, options). id None + options → ambiguous; None + [] → none."""
    data = await _notion("POST", "/search",
                         {"query": title, "filter": {"value": "page", "property": "object"}})
    results = [p for p in data.get("results", []) if p.get("object") == "page"]
    if not results:
        return None, "", []
    exact = [p for p in results if _page_title(p).lower() == title.lower()]
    if len(exact) == 1:
        return exact[0]["id"], _page_title(exact[0]), []
    if len(results) == 1:
        return results[0]["id"], _page_title(results[0]), []
    return None, "", [_page_title(p) for p in results[:5]]


@tool(destructive=True)
async def notion_append(page_title: str, text: str, confirmed: bool = False) -> ToolResult:
    """Agrega texto al final de una página de Notion ("agrega a mi página de ideas: 'X'").

    Busca la página por título; si hay varias, te pregunta cuál. Confirma antes de escribir.
    """
    if not settings.NOTION_API_KEY:
        return ToolResult(
            False, None,
            "Necesito tu token de Notion. Configúralo con «python -m emma.setup --only notion».",
            False,
        )
    if not (page_title.strip() and text.strip()):
        return ToolResult(False, None, "Necesito la página y el texto a agregar.", False)

    try:
        page_id, resolved_title, options = await _find_page(page_title)
    except httpx.HTTPError as exc:
        log.error("notion_search_failed", error=str(exc))
        return ToolResult(False, None, "No pude buscar en Notion.", False)
    if page_id is None:
        if options:
            return ToolResult(True, {"pages": options},
                              f"Encontré varias: {', '.join(options)}. ¿Cuál?", False)
        return ToolResult(False, None, f"No encontré la página «{page_title}».", False)

    if not confirmed:
        # Confirm the page Notion actually resolved (its real title), not the raw
        # query — a fuzzy search for "ideas" can land on "Bad ideas archive".
        return ToolResult(True, {"page_title": resolved_title},
                          f"Voy a agregar a «{resolved_title}»: «{text}». ¿Lo hago?",
                          requires_confirmation=True)

    block = {
        "children": [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }]
    }
    try:
        await _notion("PATCH", f"/blocks/{page_id}/children", block)
    except httpx.HTTPError as exc:
        log.error("notion_append_failed", error=str(exc))
        return ToolResult(False, None, "No pude agregar el texto en Notion.", False)
    return ToolResult(True, {"page_id": page_id}, f"Listo, lo agregué a «{resolved_title}».", False)


# ---- 26.2 setup orchestrator hooks ------------------------------------------


def notion_token_status() -> str:
    return "valid" if settings.NOTION_API_KEY else "missing"


async def run_notion_setup(non_interactive: bool = False) -> bool:
    """Validate the token with a cheap `users/me` call. Never prompts/raises."""
    if not settings.NOTION_API_KEY:
        return False
    try:
        await _notion("GET", "/users/me", None)
        return True
    except Exception as exc:
        log.warning("notion_setup_validate_failed", error=str(exc))
        return False
