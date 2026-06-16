"""Prompt 34 — Linear / Jira / Notion integration tools (network mocked)."""

from __future__ import annotations

import pytest

import tools.jira_tool as jt
import tools.linear_tool as lt
import tools.notion_tool as nt
from config.settings import settings

# ---- Linear -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_missing_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LINEAR_API_KEY", "")
    res = await lt.create_linear_issue("algo")
    assert not res.success and "linear" in res.user_message.lower()


@pytest.mark.asyncio
async def test_linear_team_disambiguation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LINEAR_API_KEY", "k")

    async def fake_gql(query, variables):
        return {"teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"},
                                    {"id": "t2", "name": "Design", "key": "DSG"}]}}

    monkeypatch.setattr(lt, "_graphql", fake_gql)
    res = await lt.create_linear_issue("algo", team="")
    assert res.success and not res.requires_confirmation
    assert "Engineering" in res.user_message and "Design" in res.user_message


@pytest.mark.asyncio
async def test_linear_confirm_then_create(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LINEAR_API_KEY", "k")

    async def fake_gql(query, variables):
        if "teams" in query:
            return {"teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}
        return {"issueCreate": {"success": True,
                                "issue": {"identifier": "ENG-12", "url": "https://linear.app/i/ENG-12"}}}

    monkeypatch.setattr(lt, "_graphql", fake_gql)
    prev = await lt.create_linear_issue("Emma no entiende TablePlus")
    assert prev.requires_confirmation
    res = await lt.create_linear_issue("Emma no entiende TablePlus", confirmed=True)
    assert res.success and res.data["identifier"] == "ENG-12" and "ENG-12" in res.user_message


def test_linear_token_status(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LINEAR_API_KEY", "k")
    assert lt.linear_token_status() == "valid"
    monkeypatch.setattr(settings, "LINEAR_API_KEY", "")
    assert lt.linear_token_status() == "missing"


# ---- Jira -------------------------------------------------------------------


def _jira_creds(monkeypatch):
    monkeypatch.setattr(settings, "JIRA_BASE_URL", "https://org.atlassian.net")
    monkeypatch.setattr(settings, "JIRA_EMAIL", "a@b.com")
    monkeypatch.setattr(settings, "JIRA_API_TOKEN", "t")


def test_jira_type_synonym_normalization() -> None:
    assert jt._norm_type("tarea") == "Task"
    assert jt._norm_type("error") == "Bug"
    assert jt._norm_type("historia") == "Story"
    assert jt._norm_type("") == "Task"          # default
    assert jt._norm_type("Custom") == "Custom"  # unknown passes through


@pytest.mark.asyncio
async def test_jira_missing_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "JIRA_BASE_URL", "")
    res = await jt.create_jira_issue("ENG", "algo")
    assert not res.success and "jira" in res.user_message.lower()


@pytest.mark.asyncio
async def test_jira_confirm_then_create(monkeypatch) -> None:
    _jira_creds(monkeypatch)
    captured = {}

    async def fake_create(body):
        captured["body"] = body
        return {"key": "ENG-5"}

    monkeypatch.setattr(jt, "_create", fake_create)
    prev = await jt.create_jira_issue("ENG", "Arreglar TablePlus", issue_type="Bug")
    assert prev.requires_confirmation
    res = await jt.create_jira_issue("ENG", "Arreglar TablePlus", description="detalle", issue_type="Bug",
                                     confirmed=True)
    assert res.success and res.data["key"] == "ENG-5"
    assert "browse/ENG-5" in res.data["url"]
    f = captured["body"]["fields"]
    assert f["project"]["key"] == "ENG" and f["issuetype"]["name"] == "Bug"
    assert f["description"]["type"] == "doc"  # ADF wrapped


# ---- Notion -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_notion_missing_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTION_API_KEY", "")
    res = await nt.notion_append("Ideas", "x")
    assert not res.success and "notion" in res.user_message.lower()


def _page(title):
    return {"object": "page", "id": f"id-{title}",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}}}


@pytest.mark.asyncio
async def test_notion_not_found(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTION_API_KEY", "k")

    async def fake(method, path, body=None):
        return {"results": []}

    monkeypatch.setattr(nt, "_notion", fake)
    res = await nt.notion_append("Inexistente", "x")
    assert not res.success and "no encontré" in res.user_message.lower()


@pytest.mark.asyncio
async def test_notion_disambiguation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTION_API_KEY", "k")

    async def fake(method, path, body=None):
        return {"results": [_page("Ideas A"), _page("Ideas B")]}

    monkeypatch.setattr(nt, "_notion", fake)
    res = await nt.notion_append("Ideas", "x")
    assert res.success and not res.requires_confirmation
    assert "Ideas A" in res.user_message and "Ideas B" in res.user_message


@pytest.mark.asyncio
async def test_notion_confirms_resolved_title_not_query(monkeypatch) -> None:
    # fuzzy single match: confirm the page's REAL title, not the raw query (audit fix)
    monkeypatch.setattr(settings, "NOTION_API_KEY", "k")

    async def fake(method, path, body=None):
        return {"results": [_page("Bad ideas archive")]}

    monkeypatch.setattr(nt, "_notion", fake)
    res = await nt.notion_append("ideas", "x")
    assert res.requires_confirmation and "Bad ideas archive" in res.user_message


@pytest.mark.asyncio
async def test_notion_confirm_then_append(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTION_API_KEY", "k")
    calls = []

    async def fake(method, path, body=None):
        calls.append((method, path, body))
        if path == "/search":
            return {"results": [_page("Ideas")]}
        return {}

    monkeypatch.setattr(nt, "_notion", fake)
    prev = await nt.notion_append("Ideas", "comprar café")
    assert prev.requires_confirmation
    res = await nt.notion_append("Ideas", "comprar café", confirmed=True)
    assert res.success
    patch = next(c for c in calls if c[0] == "PATCH")
    assert patch[1].startswith("/blocks/id-Ideas/children")
    assert patch[2]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "comprar café"
