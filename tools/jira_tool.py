"""Jira issue creation (Prompt 34, Part C) — real REST v3 API.

Basic auth with ``JIRA_EMAIL`` + ``JIRA_API_TOKEN`` against ``JIRA_BASE_URL``. The
description is wrapped in Atlassian Document Format (ADF), which the v3 API requires.
Registered with 26.2 via ``jira_token_status`` / ``run_jira_setup``.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.jira")


def _configured() -> bool:
    return bool(settings.JIRA_BASE_URL and settings.JIRA_EMAIL and settings.JIRA_API_TOKEN)


def _base() -> str:
    return settings.JIRA_BASE_URL.rstrip("/")


_TYPE_SYNONYMS = {
    "task": "Task", "tarea": "Task", "todo": "Task",
    "bug": "Bug", "error": "Bug", "defecto": "Bug", "fallo": "Bug",
    "story": "Story", "historia": "Story", "us": "Story",
    "epic": "Epic", "épica": "Epic", "epica": "Epic",
}


def _norm_type(issue_type: str) -> str:
    """Map a loose ES/EN type to a canonical Jira issue-type name; default Task.

    Jira rejects an unknown issuetype name with a 400, so we normalise the common
    synonyms rather than pass arbitrary input straight through.
    """
    return _TYPE_SYNONYMS.get((issue_type or "").strip().lower(), issue_type.strip() or "Task")


def _adf(text: str) -> dict[str, Any]:
    """Minimal Atlassian Document Format paragraph for `text`."""
    content = [{"type": "text", "text": text}] if text.strip() else []
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}


async def _create(body: dict[str, Any]) -> dict[str, Any]:
    """POST a new issue. Raises on HTTP error. Mockable seam for tests."""
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        r = await client.post(
            f"{_base()}/rest/api/3/issue",
            json=body,
            auth=(settings.JIRA_EMAIL, settings.JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return dict(r.json())


@tool(destructive=True)
async def create_jira_issue(
    project: str, summary: str, description: str = "", issue_type: str = "Task",
    confirmed: bool = False,
) -> ToolResult:
    """Crea un issue en Jira. `project` es la clave del proyecto (ej. ENG); `issue_type`
    ∈ Task/Bug/Story. Confirma antes de crear.
    """
    if not _configured():
        return ToolResult(
            False, None,
            "Necesito tu Jira (URL, email y API token). Configúralo con "
            "«python -m emma.setup --only jira».", False,
        )
    if not (project.strip() and summary.strip()):
        return ToolResult(False, None, "Necesito el proyecto y el resumen del issue.", False)

    itype = _norm_type(issue_type)
    if not confirmed:
        return ToolResult(
            True, {"project": project, "summary": summary},
            f"Voy a crear «{summary}» en el proyecto {project} ({itype}). ¿Lo creo?",
            requires_confirmation=True,
        )

    body = {
        "fields": {
            "project": {"key": project.strip()},
            "summary": summary,
            "issuetype": {"name": itype},
            "description": _adf(description),
        }
    }
    try:
        data = await _create(body)
    except httpx.HTTPError as exc:
        log.error("jira_create_failed", error=str(exc))
        return ToolResult(False, None, "No pude crear el issue en Jira.", False)
    key = data.get("key", "?")
    url = f"{_base()}/browse/{key}"
    return ToolResult(True, {"key": key, "url": url}, f"Listo, creé {key}: {url}", False)


# ---- 26.2 setup orchestrator hooks ------------------------------------------


def jira_token_status() -> str:
    return "valid" if _configured() else "missing"


async def run_jira_setup(non_interactive: bool = False) -> bool:
    """Validate creds with a cheap `myself` call. Never prompts/raises."""
    if not _configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
            r = await client.get(
                f"{_base()}/rest/api/3/myself",
                auth=(settings.JIRA_EMAIL, settings.JIRA_API_TOKEN),
                headers={"Accept": "application/json"},
            )
            return r.status_code == 200
    except Exception as exc:
        log.warning("jira_setup_validate_failed", error=str(exc))
        return False
