"""Prompt 34 A+B — tableplus_query + postman_run (subprocess/CLI mocked)."""

from __future__ import annotations

import pytest

import tools.postman_tool as pt
import tools.tableplus_tool as tt
from tools.base import ToolResult

# ---- TablePlus --------------------------------------------------------------


def test_is_write_detects_writes_and_reads() -> None:
    assert tt._is_write("select * from users") is False
    assert tt._is_write("  SELECT 1") is False
    for w in ("insert into t values(1)", "UPDATE t set x=1", "delete from t",
              "drop table t", "ALTER TABLE t add c int", "truncate t"):
        assert tt._is_write(w) is True


def test_parse_rows_tsv_limit() -> None:
    raw = "id\tname\n1\tana\n2\tbeto\n"
    rows = tt._parse_rows(raw)
    assert rows == [{"id": "1", "name": "ana"}, {"id": "2", "name": "beto"}]


@pytest.mark.asyncio
async def test_write_requires_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(tt.dictionary, "find_connection", lambda q: {"name": "learning-rots"})
    res = await tt.tableplus_query("learning-rots", "DELETE FROM users")
    assert res.requires_confirmation and "modifica datos" in res.user_message


@pytest.mark.asyncio
async def test_select_runs_via_cli(monkeypatch) -> None:
    monkeypatch.setattr(tt.dictionary, "find_connection", lambda q: {"name": "learning-rots"})
    monkeypatch.setattr(tt, "_cli_path", lambda: "/usr/local/bin/tableplus-cli")

    async def fake_cli(cli, conn, sql, timeout=30.0):
        assert conn == "learning-rots"
        return (0, "count\n42\n")

    monkeypatch.setattr(tt, "_run_cli", fake_cli)
    res = await tt.tableplus_query("learning-rots", "select count(*) from users")
    assert res.success and res.data["rows"] == [{"count": "42"}] and res.data["via"] == "cli"


@pytest.mark.asyncio
async def test_falls_back_to_ax_when_no_cli(monkeypatch) -> None:
    monkeypatch.setattr(tt.dictionary, "find_connection", lambda q: None)
    monkeypatch.setattr(tt, "_cli_path", lambda: None)
    called = {}

    async def fake_ax(sql):
        called["sql"] = sql
        return ToolResult(True, {"via": "ax"}, "Ejecuté la consulta en TablePlus.", False)

    monkeypatch.setattr(tt, "_run_ax", fake_ax)
    res = await tt.tableplus_query("rots", "select 1")
    assert res.success and res.data["via"] == "ax" and called["sql"] == "select 1"


# ---- Postman ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_postman_needs_newman(monkeypatch) -> None:
    monkeypatch.setattr(pt, "_newman_path", lambda: None)
    res = await pt.postman_run("health")
    assert not res.success and "newman" in res.user_message.lower()


@pytest.mark.asyncio
async def test_postman_collection_not_found(monkeypatch) -> None:
    monkeypatch.setattr(pt, "_newman_path", lambda: "/usr/local/bin/newman")
    monkeypatch.setattr(pt, "_resolve_collection", lambda n: (None, []))
    res = await pt.postman_run("nope")
    assert not res.success and "no encontré" in res.user_message.lower()


@pytest.mark.asyncio
async def test_postman_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(pt, "_newman_path", lambda: "/usr/local/bin/newman")
    monkeypatch.setattr(pt, "_resolve_collection", lambda n: (None, ["health-dev", "health-prod"]))
    res = await pt.postman_run("health")
    assert res.success and "health-dev" in res.user_message


def test_summarize_newman_report() -> None:
    run = {"stats": {"assertions": {"total": 10, "failed": 2}},
           "failures": [{"error": {"name": "AssertionError", "message": "status 500"}}]}
    s = pt._summarize(run)
    assert s == {"passed": 8, "failed": 2, "total": 10,
                 "failures": ["AssertionError: status 500"]}


@pytest.mark.asyncio
async def test_postman_run_reports_summary(monkeypatch) -> None:
    monkeypatch.setattr(pt, "_newman_path", lambda: "/usr/local/bin/newman")
    monkeypatch.setattr(pt, "_resolve_collection", lambda n: ("/x/health.json", []))
    monkeypatch.setattr(pt, "_resolve_environment", lambda n: None)

    async def fake_run(coll, env):
        return {"stats": {"assertions": {"total": 5, "failed": 0}}, "failures": []}

    monkeypatch.setattr(pt, "_run_newman", fake_run)
    res = await pt.postman_run("health")
    assert res.success and res.data["passed"] == 5 and "5 de 5" in res.user_message
