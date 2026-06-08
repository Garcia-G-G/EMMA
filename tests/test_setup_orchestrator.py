"""Prompt 26.2-A: the unified setup orchestrator (registry, skip, idempotency)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from emma import setup


def _args(**kw):
    base = {"only": set(), "skip": set(), "non_interactive": False, "skip_tcc": True}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "_STATE_PATH", tmp_path / "setup_state.json")


X = setup.SERVICES[0]  # the X service entry


def _resolver(token_status="missing", setup_ok=True, on_call=None):
    """A fake _resolve: returns a callable matching the requested capability."""

    def resolve(path):
        if on_call:
            on_call(path)
        if "token_status" in path:
            return AsyncMock(return_value=token_status)
        return AsyncMock(return_value=setup_ok)

    return resolve


class TestSelection:
    def test_only_filters(self):
        assert [s["name"] for s in setup._selected(_args(only={"x"}))] == ["x"]

    def test_skip_filters(self):
        names = [s["name"] for s in setup._selected(_args(skip={"spotify"}))]
        assert "spotify" not in names and "x" in names


class TestServiceFlow:
    @pytest.mark.asyncio
    async def test_valid_token_is_configured(self, monkeypatch):
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="valid"))
        assert await setup._setup_service(X, _args(), {}) == "configured"

    @pytest.mark.asyncio
    async def test_has_client_id_runs_setup(self, monkeypatch):
        monkeypatch.setattr(setup.settings, "X_CLIENT_ID", "CID")
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="missing", setup_ok=True))
        assert await setup._setup_service(X, _args(), {}) == "configured"

    @pytest.mark.asyncio
    async def test_missing_client_id_declined_is_skipped(self, monkeypatch):
        monkeypatch.setattr(setup.settings, "X_CLIENT_ID", "")
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="missing"))
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        assert await setup._setup_service(X, _args(), {}) == "skipped"

    @pytest.mark.asyncio
    async def test_missing_client_id_non_interactive_is_pending(self, monkeypatch):
        monkeypatch.setattr(setup.settings, "X_CLIENT_ID", "")
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="missing"))
        assert await setup._setup_service(X, _args(non_interactive=True), {}) == "pending"

    @pytest.mark.asyncio
    async def test_prior_skip_not_reprompted(self, monkeypatch):
        # state says skipped; without --only we must NOT even probe the token.
        called: list[str] = []
        monkeypatch.setattr(setup, "_resolve", _resolver(on_call=called.append))
        st = await setup._setup_service(X, _args(), {"x": {"status": "skipped"}})
        assert st == "skipped" and called == []

    @pytest.mark.asyncio
    async def test_only_overrides_prior_skip(self, monkeypatch):
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="valid"))
        st = await setup._setup_service(X, _args(only={"x"}), {"x": {"status": "skipped"}})
        assert st == "configured"


class TestRunAndState:
    @pytest.mark.asyncio
    async def test_run_persists_state_and_returns_pending_code(self, monkeypatch):
        monkeypatch.setattr(setup.settings, "X_CLIENT_ID", "")
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="missing"))
        rc = await setup._run(_args(only={"x"}, non_interactive=True))
        assert rc == 1  # pending → non-zero (installer treats as a warning)
        state = json.loads(setup._STATE_PATH.read_text())
        assert state["x"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_all_configured_returns_zero(self, monkeypatch):
        monkeypatch.setattr(setup, "_resolve", _resolver(token_status="valid"))
        rc = await setup._run(_args())
        assert rc == 0


class TestArgParsing:
    def test_main_parses_and_runs(self, monkeypatch):
        captured = {}

        async def _fake_run(args):
            captured["only"] = args.only
            captured["skip"] = args.skip
            captured["skip_tcc"] = args.skip_tcc
            return 0

        monkeypatch.setattr(setup, "_run", _fake_run)
        rc = setup.main(["--only", "x", "--skip", "spotify", "--skip-tcc"])
        assert rc == 0
        assert captured["only"] == {"x"} and captured["skip"] == {"spotify"}
        assert captured["skip_tcc"] is True
