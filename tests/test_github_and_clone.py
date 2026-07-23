"""Phase 18: GitHub search + clone-and-open flow.

httpx and the background registry are mocked so nothing hits the network or
spawns a real `git clone` / IDE.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools import git_tool, github_tool

_FAKE_ITEMS = {
    "items": [
        {
            "name": "pipecat",
            "full_name": "pipecat-ai/pipecat",
            "html_url": "https://github.com/pipecat-ai/pipecat",
            "clone_url": "https://github.com/pipecat-ai/pipecat.git",
            "description": "Open source voice AI framework",
            "stargazers_count": 12609,
            "language": "Python",
        },
        {
            "name": "other",
            "full_name": "someone/other",
            "html_url": "https://github.com/someone/other",
            "clone_url": "https://github.com/someone/other.git",
            "description": None,
            "stargazers_count": 5,
            "language": None,
        },
    ]
}


class _FakeResp:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return _FAKE_ITEMS


class _FakeClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        return _FakeResp()


@pytest.fixture
def mock_github(monkeypatch):
    monkeypatch.setattr(github_tool.httpx, "AsyncClient", _FakeClient)


# --- 422 strip-and-retry harness ----------------------------------------

_VALIDATION_422 = {
    "message": "Validation Failed",
    "errors": [
        {
            "message": (
                "The listed users and repositories cannot be searched either "
                "because the resources do not exist or you do not have "
                "permission to view them."
            ),
            "resource": "Search",
            "field": "q",
            "code": "invalid",
        }
    ],
}


class _ProgrammedResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _client_from_handler(handler, calls):
    """Build a fake AsyncClient whose .get() delegates to `handler(q)` and
    records each queried `q` into `calls`."""

    class _C:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            q = params["q"]
            calls.append(q)
            return handler(q)

    return _C


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_parses_matches(self, mock_github):
        r = await github_tool.search_github("voice ai", limit=5)
        assert r.success is True
        ms = r.data["matches"]
        assert ms[0]["full_name"] == "pipecat-ai/pipecat"
        assert ms[0]["clone_url"] == "https://github.com/pipecat-ai/pipecat.git"
        assert ms[0]["stars"] == 12609
        assert ms[1]["description"] == ""  # None coerced to ""

    @pytest.mark.asyncio
    async def test_empty_query_rejected(self):
        r = await github_tool.search_github("   ")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_get_repo_url_returns_top_clone_url(self, mock_github):
        r = await github_tool.get_repo_url("pipecat")
        assert r.success is True
        assert r.data["clone_url"] == "https://github.com/pipecat-ai/pipecat.git"


class TestQualifier422:
    """A bad user:/org:/repo: qualifier makes GitHub 422 the whole query.
    Emma strips scoping qualifiers and retries as free text rather than
    failing an otherwise-answerable search."""

    def test_strip_scope_qualifiers(self):
        f = github_tool._strip_scope_qualifiers
        assert f("user:example-user Reachi") == "Reachi"
        assert f("org:foo repo:bar/baz voice ai") == "voice ai"
        assert f("REPOSITORY:x/y kanban") == "kanban"
        assert f("user:only") == ""  # nothing left to search
        assert f("plain query") == "plain query"  # untouched

    @pytest.mark.asyncio
    async def test_422_bad_qualifier_retries_stripped(self, monkeypatch):
        calls: list[str] = []

        def handler(q: str):
            if "user:" in q:
                return _ProgrammedResp(422, _VALIDATION_422)
            return _ProgrammedResp(200, _FAKE_ITEMS)

        monkeypatch.setattr(github_tool.httpx, "AsyncClient", _client_from_handler(handler, calls))
        r = await github_tool.search_github("user:example-user Reachi")
        assert r.success is True
        assert r.data["matches"][0]["full_name"] == "pipecat-ai/pipecat"
        # First the original query 422'd, then the stripped retry succeeded.
        assert calls == ["user:example-user Reachi", "Reachi"]

    @pytest.mark.asyncio
    async def test_422_persists_surfaces_github_message(self, monkeypatch):
        calls: list[str] = []

        def handler(q: str):
            return _ProgrammedResp(422, _VALIDATION_422)

        monkeypatch.setattr(github_tool.httpx, "AsyncClient", _client_from_handler(handler, calls))
        r = await github_tool.search_github("user:the user-G-H Reachi")
        assert r.success is False
        # GitHub's real validation message is surfaced, not an opaque httpx error.
        assert "cannot be searched" in r.user_message

    @pytest.mark.asyncio
    async def test_422_with_no_free_text_does_not_retry(self, monkeypatch):
        calls: list[str] = []

        def handler(q: str):
            return _ProgrammedResp(422, _VALIDATION_422)

        monkeypatch.setattr(github_tool.httpx, "AsyncClient", _client_from_handler(handler, calls))
        r = await github_tool.search_github("user:nonexistent")
        assert r.success is False
        # Stripping leaves nothing searchable -> no pointless second request.
        assert calls == ["user:nonexistent"]


class TestCloneAndOpen:
    def test_build_clone_cmd_exact(self):
        cmd = git_tool._build_clone_cmd(
            "https://github.com/octocat/Hello-World.git",
            "/Users/go/Documents/repos/Hello-World",
            "Cursor",
        )
        assert cmd == (
            'rm -rf "/Users/go/Documents/repos/Hello-World" && '
            'git clone --depth 1 "https://github.com/octocat/Hello-World.git" '
            '"/Users/go/Documents/repos/Hello-World" && '
            'open -a "Cursor" "/Users/go/Documents/repos/Hello-World"'
        )

    @pytest.mark.asyncio
    async def test_first_call_requires_confirmation(self, monkeypatch):
        monkeypatch.setattr(git_tool, "_resolve_ide", lambda ide="": "Cursor")
        r = await git_tool.clone_and_open("octocat/Hello-World")
        assert r.requires_confirmation is True
        assert "Hello-World" in r.user_message
        assert "Cursor" in r.user_message

    @pytest.mark.asyncio
    async def test_no_ide_configured_errors(self, monkeypatch):
        monkeypatch.setattr(git_tool, "_resolve_ide", lambda ide="": None)
        r = await git_tool.clone_and_open("octocat/Hello-World")
        assert r.success is False
        assert "IDE" in r.user_message

    @pytest.mark.asyncio
    async def test_confirmed_schedules_registry_task(self, monkeypatch):
        monkeypatch.setattr(git_tool, "_resolve_ide", lambda ide="": "Cursor")
        fake_rec = SimpleNamespace(id="task-42")
        fake_reg = MagicMock()
        fake_reg.start = AsyncMock(return_value=fake_rec)
        monkeypatch.setattr(git_tool, "registry", lambda: fake_reg)

        r = await git_tool.clone_and_open("octocat/Hello-World", confirmed=True)
        assert r.success is True
        assert r.requires_confirmation is False
        assert "clonando" in r.user_message.lower()
        fake_reg.start.assert_awaited_once()
        kwargs = fake_reg.start.await_args.kwargs
        assert kwargs["name"] == "clone:Hello-World"
        assert kwargs["kind"] == "shell"
        # The assembled command rides in meta and matches _build_clone_cmd.
        assert kwargs["meta"]["cmd"] == r.data["cmd"]
        assert kwargs["meta"]["cmd"].startswith('rm -rf "')
        assert 'git clone --depth 1 "https://github.com/octocat/Hello-World.git"' in r.data["cmd"]

    @pytest.mark.asyncio
    async def test_injection_guard_rejects_bad_subdir(self, monkeypatch):
        monkeypatch.setattr(git_tool, "_resolve_ide", lambda ide="": "Cursor")
        r = await git_tool.clone_and_open(
            "octocat/Hello-World", dest_subdir='evil" ; rm -rf ~', confirmed=True
        )
        assert r.success is False
        assert "seguridad" in r.user_message
