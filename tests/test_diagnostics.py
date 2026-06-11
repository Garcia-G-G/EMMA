"""Prompt 37 A/B/C — diagnostics, live reload, telemetry."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import tools.diagnostics_tool as dt
from core import diagnostics as diag
from core.diagnostics import HealthReport

# ---- A: diagnose_self -------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnose_self_speaks_real_metrics(monkeypatch) -> None:
    hr = HealthReport(uptime_s=3 * 3600 + 600, disk_free_gb=120.5, disk_total_gb=500.0,
                      battery_pct=88, charging=False, thermal="normal", facts_count=42,
                      last_reflection_ago_s=300, last_error=None, mic_rms=0.12)
    monkeypatch.setattr(dt.diag, "gather_health_sync", lambda: hr)
    monkeypatch.setattr(dt.diag, "openai_rtt_ms", AsyncMock(return_value=240))
    res = await dt.diagnose_self()
    assert res.success
    msg = res.user_message
    assert "Estoy bien" in msg
    assert "3h 10m" in msg and "240 ms" in msg and "mic en 0.12" in msg and "42 hechos" in msg
    assert "Sin errores recientes" in msg


@pytest.mark.asyncio
async def test_diagnose_self_surfaces_an_error_honestly(monkeypatch) -> None:
    hr = HealthReport(uptime_s=60, disk_free_gb=None, disk_total_gb=None, battery_pct=None,
                      charging=None, thermal=None, facts_count=None, last_reflection_ago_s=None,
                      last_error="OSError: device busy", mic_rms=None)
    monkeypatch.setattr(dt.diag, "gather_health_sync", lambda: hr)
    monkeypatch.setattr(dt.diag, "openai_rtt_ms", AsyncMock(return_value=None))
    res = await dt.diagnose_self()
    assert "error reciente" in res.user_message and "device busy" in res.user_message


# ---- B: reload_tools --------------------------------------------------------


def test_reload_all_tools_returns_shape_and_survives() -> None:
    r = diag.reload_all_tools()
    assert isinstance(r["reloaded"], list) and isinstance(r["errors"], list)
    assert r["tool_count"] > 0
    assert "diagnostics_tool" in r["reloaded"]  # our own module reloaded


def test_reload_reports_failed_module_without_crashing(monkeypatch) -> None:
    orig = importlib.reload

    def fake_reload(module):
        if getattr(module, "__name__", "") == "tools.shell":
            raise RuntimeError("boom in shell")
        return orig(module)

    monkeypatch.setattr(importlib, "reload", fake_reload)
    r = diag.reload_all_tools()  # must NOT raise
    failed = {e["module"] for e in r["errors"]}
    assert "shell" in failed
    assert "diagnostics_tool" in r["reloaded"]  # the rest still reloaded


# ---- C: telemetry_summary ---------------------------------------------------


def _gap(tool, cat, ms, ts):
    return {"ts": ts.isoformat(), "tool": tool, "category": cat, "elapsed_ms": ms, "success": False}


def test_telemetry_aggregates_within_period(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC)
    ledger = tmp_path / "capability_gaps.jsonl"
    rows = [
        _gap("macos", "timeout", 1200, now),
        _gap("macos", "timeout", 1800, now - timedelta(hours=2)),
        _gap("notes", "error", 400, now - timedelta(days=2)),
        _gap("old", "stale", 99, now - timedelta(days=40)),  # outside the week window
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows))

    class _S:
        EMMA_HOME = tmp_path
        MEMORY_DB_PATH = tmp_path / "mem.db"

    monkeypatch.setattr(diag, "settings", _S)
    from memory import episodic

    monkeypatch.setattr(episodic, "settings", _S)  # empty actions DB

    t = diag.telemetry_rollup("week")
    assert t["failures"] == 3  # the 40-day-old row excluded
    assert t["top_categories"][0][0] == "timeout"
    assert t["top_tools"][0] == ("macos", 2)
    assert t["lat_p50_ms"] is not None and t["lat_p95_ms"] is not None
    assert t["actions_recorded"] == 0


@pytest.mark.asyncio
async def test_telemetry_summary_tool_speaks(monkeypatch) -> None:
    monkeypatch.setattr(
        dt.diag, "telemetry_rollup",
        lambda period: {"period": "week", "failures": 14, "top_categories": [("timeout", 9)],
                        "top_tools": [("macos", 9)], "lat_p50_ms": 300, "lat_p95_ms": 1400,
                        "actions_recorded": 57},
    )
    res = await dt.telemetry_summary("week")
    assert res.success
    assert "14 fallo" in res.user_message and "timeout" in res.user_message and "1400 ms" in res.user_message
