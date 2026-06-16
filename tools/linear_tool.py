"""Linear issue creation (Prompt 34, Part C) — real GraphQL API.

Uses a Linear personal API key (``LINEAR_API_KEY``, Keychain-backed). Resolves the
team name to an id, confirms the issue once, then creates it. Registered with the
26.2 setup orchestrator via ``linear_token_status`` / ``run_linear_setup``.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.linear")

_API = "https://api.linear.app/graphql"


def _missing() -> ToolResult:
    return ToolResult(
        False, None,
        "Necesito tu API key de Linear. Configúrala con «python -m emma.setup --only linear».",
        False,
    )


async def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL query. Raises on transport/GraphQL errors."""
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        r = await client.post(
            _API,
            json={"query": query, "variables": variables},
            headers={"Authorization": settings.LINEAR_API_KEY, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"])[:200])
    return payload.get("data") or {}


async def _resolve_team(team: str) -> tuple[str | None, list[str]]:
    """(team_id, options). team_id None + options → ambiguous; None + [] → none found."""
    data = await _graphql("query{ teams{ nodes{ id name key } } }", {})
    teams = data.get("teams", {}).get("nodes", [])
    if not teams:
        return None, []
    if not team.strip():
        return (teams[0]["id"], []) if len(teams) == 1 else (None, [t["name"] for t in teams])
    q = team.strip().lower()
    for t in teams:
        if q in (t["name"].lower(), t["key"].lower()):
            return t["id"], []
    return None, [t["name"] for t in teams]


@tool(destructive=True)
async def create_linear_issue(
    title: str, description: str = "", team: str = "", confirmed: bool = False
) -> ToolResult:
    """Crea un issue en Linear ("crea issue en Linear: 'X'").

    Resuelve el `team` por nombre; si hay varios y no especificas, te pregunta cuál.
    Confirma antes de crear.
    """
    if not settings.LINEAR_API_KEY:
        return _missing()
    if not title.strip():
        return ToolResult(False, None, "¿Qué título le pongo al issue?", False)

    try:
        team_id, options = await _resolve_team(team)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("linear_team_failed", error=str(exc))
        return ToolResult(False, None, "No pude leer tus equipos de Linear.", False)
    if team_id is None:
        if options:
            return ToolResult(True, {"teams": options},
                              f"¿En qué equipo lo creo? Tengo: {', '.join(options)}.", False)
        return ToolResult(False, None, "No encontré equipos en tu Linear.", False)

    if not confirmed:
        where = f" en {team}" if team.strip() else ""
        return ToolResult(True, {"title": title}, f"Voy a crear el issue «{title}»{where}. ¿Lo creo?",
                          requires_confirmation=True)

    mutation = (
        "mutation($i:IssueCreateInput!){ issueCreate(input:$i){ success "
        "issue{ identifier url } } }"
    )
    try:
        data = await _graphql(mutation, {"i": {"title": title, "description": description, "teamId": team_id}})
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("linear_create_failed", error=str(exc))
        return ToolResult(False, None, "No pude crear el issue en Linear.", False)
    issue = (data.get("issueCreate") or {}).get("issue") or {}
    ident, url = issue.get("identifier", "?"), issue.get("url", "")
    return ToolResult(True, {"identifier": ident, "url": url},
                      f"Listo, creé {ident}: {url}", False)


# ---- 26.2 setup orchestrator hooks ------------------------------------------


def linear_token_status() -> str:
    """Cheap Keychain-backed presence check (no network), per the 26.2 convention."""
    return "valid" if settings.LINEAR_API_KEY else "missing"


async def run_linear_setup(non_interactive: bool = False) -> bool:
    """Validate the key with a cheap `viewer` query. Never prompts/raises."""
    if not settings.LINEAR_API_KEY:
        return False
    try:
        await _graphql("query{ viewer{ id } }", {})
        return True
    except Exception as exc:
        log.warning("linear_setup_validate_failed", error=str(exc))
        return False
