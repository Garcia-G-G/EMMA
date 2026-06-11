"""Part D — the Spanish day parser + what_did_you_do summary."""

from __future__ import annotations

import time
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

import tools.history_tool as ht
from memory.episodic import ActionRecord


def test_parse_today_variants() -> None:
    assert ht._parse_when("") == date.today()
    assert ht._parse_when("hoy") == date.today()
    assert ht._parse_when("esta mañana") == date.today()


def test_parse_ayer_anteayer() -> None:
    assert ht._parse_when("ayer") == date.today() - timedelta(days=1)
    assert ht._parse_when("anteayer") == date.today() - timedelta(days=2)


def test_parse_weekday_is_most_recent_past() -> None:
    d = ht._parse_when("el martes")
    assert d is not None and d.weekday() == 1  # martes
    assert d < date.today() and (date.today() - d).days <= 7


def test_parse_iso_date() -> None:
    assert ht._parse_when("el 2026-06-08") == date(2026, 6, 8)


def test_parse_de_mes() -> None:
    d = ht._parse_when("el 8 de junio")
    assert d is not None and d.month == 6 and d.day == 8


def test_parse_garbage_returns_none() -> None:
    assert ht._parse_when("ksjdfh lalala") is None


def _rec(**kw) -> ActionRecord:
    base = dict(id=1, ts=time.time(), tool_name="create_note", args={"title": "X"}, result=None,
                user_speech="crea X", reverse_kind="inverse_call", reverse=None, reversed_at=None)
    base.update(kw)
    return ActionRecord(**base)


@pytest.mark.asyncio
async def test_what_did_you_do_summarizes(monkeypatch) -> None:
    recs = [_rec(tool_name="create_note"), _rec(id=2, tool_name="delete_note", reverse_kind="manual")]
    monkeypatch.setattr(ht.episodic, "query_by_date", AsyncMock(return_value=recs))
    res = await ht.what_did_you_do("hoy")
    assert res.success
    assert "create_note" in res.user_message and "delete_note" in res.user_message
    assert res.data["actions"][0]["reversible"] is True  # inverse_call, not reversed
    assert res.data["actions"][1]["reversible"] is False  # manual


@pytest.mark.asyncio
async def test_what_did_you_do_empty_day(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "query_by_date", AsyncMock(return_value=[]))
    res = await ht.what_did_you_do("ayer")
    assert res.success and res.data["actions"] == []


@pytest.mark.asyncio
async def test_what_did_you_do_unparseable(monkeypatch) -> None:
    res = await ht.what_did_you_do("ksjdfh")
    assert not res.success
