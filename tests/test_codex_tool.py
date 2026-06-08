"""Prompt 23 Part B: the delegate_to_codex voice tool."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.background import TaskRecord
from tools import codex_tool


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(codex_tool.settings, "OPENAI_API_KEY", "sk-" + "a" * 45)


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """A workdir that lives under a (mocked) $HOME so the guard accepts it."""
    home = tmp_path
    repo = home / "repo"
    repo.mkdir()
    monkeypatch.setattr(codex_tool.Path, "home", staticmethod(lambda: home))
    return repo


def _reg(at_capacity=False, start=None, tasks=None):
    """A registry double with sync methods sync (AsyncMock would return coros)."""
    reg = MagicMock()
    reg.at_capacity.return_value = at_capacity
    reg.list.return_value = tasks if tasks is not None else []
    reg.get.return_value = None

    async def default_start(name, kind, coro_factory, meta=None):
        return TaskRecord(id="tid", name=name, kind=kind, started_at=0.0)

    reg.start = start or default_start
    return reg


class TestPreflight:
    @pytest.mark.asyncio
    async def test_cwd_outside_home_refused(self):
        res = await codex_tool.delegate_to_codex("x", cwd="/etc", confirmed=True)
        assert res.success is False
        assert "carpeta de usuario" in res.user_message

    @pytest.mark.asyncio
    async def test_missing_dir(self, workdir):
        res = await codex_tool.delegate_to_codex("x", cwd=str(workdir / "nope"), confirmed=True)
        assert res.success is False
        assert "No encontré" in res.user_message

    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch, workdir):
        monkeypatch.setattr(codex_tool.settings, "OPENAI_API_KEY", "")
        res = await codex_tool.delegate_to_codex("x", cwd=str(workdir), confirmed=True)
        assert res.success is False
        assert "API key" in res.user_message

    @pytest.mark.asyncio
    async def test_branch_requires_git(self, workdir):
        res = await codex_tool.delegate_to_codex(
            "x", cwd=str(workdir), branch="feat", confirmed=True
        )
        assert res.success is False
        assert "repo git" in res.user_message


class TestConfirmationGate:
    @pytest.mark.asyncio
    async def test_unconfirmed_asks(self, workdir):
        res = await codex_tool.delegate_to_codex("refactor utils", cwd=str(workdir))
        assert res.requires_confirmation is True
        assert "¿Le encargo" in res.user_message

    @pytest.mark.asyncio
    async def test_big_task_shows_estimate(self, workdir, monkeypatch):
        monkeypatch.setattr(codex_tool.settings, "CODING_AGENT_MAX_COST_USD", 0.001)
        res = await codex_tool.delegate_to_codex("reescribe todo en rust", cwd=str(workdir))
        assert res.requires_confirmation is True
        assert "Estimo ~$" in res.user_message


class TestDispatch:
    @pytest.mark.asyncio
    async def test_confirmed_hands_off_to_registry(self, workdir, monkeypatch):
        captured = {}

        async def fake_start(name, kind, coro_factory, meta=None):
            captured.update(name=name, kind=kind, meta=meta)
            return TaskRecord(id="tid123", name=name, kind=kind, started_at=0.0)

        monkeypatch.setattr(codex_tool, "registry", lambda: _reg(start=fake_start))
        res = await codex_tool.delegate_to_codex("add tests", cwd=str(workdir), confirmed=True)
        assert res.success is True
        assert res.data["id"] == "tid123"
        assert captured["kind"] == "coding_agent"
        assert captured["meta"]["task"] == "add tests"
        assert captured["meta"]["model"] == "gpt-5.3-codex"

    @pytest.mark.asyncio
    async def test_unknown_model_falls_back_to_default(self, workdir, monkeypatch):
        captured = {}

        async def fake_start(name, kind, coro_factory, meta=None):
            captured["meta"] = meta
            return TaskRecord(id="t", name=name, kind=kind, started_at=0.0)

        monkeypatch.setattr(codex_tool, "registry", lambda: _reg(start=fake_start))
        await codex_tool.delegate_to_codex(
            "x", cwd=str(workdir), model="evil-model", confirmed=True
        )
        assert captured["meta"]["model"] == "gpt-5.3-codex"

    @pytest.mark.asyncio
    async def test_at_capacity(self, workdir, monkeypatch):
        monkeypatch.setattr(codex_tool, "registry", lambda: _reg(at_capacity=True))
        res = await codex_tool.delegate_to_codex("x", cwd=str(workdir), confirmed=True)
        assert res.success is False
        assert "corriendo ya" in res.user_message


class TestStatus:
    @pytest.mark.asyncio
    async def test_no_tasks(self, monkeypatch):
        monkeypatch.setattr(codex_tool, "registry", lambda: _reg(tasks=[]))
        res = await codex_tool.codex_status()
        assert "todavía" in res.user_message

    @pytest.mark.asyncio
    async def test_reports_running_task(self, monkeypatch):
        rec = TaskRecord(
            id="t1",
            name="codex:x",
            kind="coding_agent",
            started_at=0.0,
            status="running",
            meta={"task": "refactor utils"},
        )
        monkeypatch.setattr(codex_tool, "registry", lambda: _reg(tasks=[rec]))
        res = await codex_tool.codex_status()
        assert "Sigue trabajando" in res.user_message
