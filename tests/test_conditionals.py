"""Prompt 32 — conditional triggers: DSL parse, matching, fire-once, expiry."""

from __future__ import annotations

import datetime as dt

import pytest

from config.settings import settings
from core import conditionals as cond
from tools.base import ToolResult


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EMMA_HOME", str(tmp_path))
    cond._reset_for_test()
    yield


# ---- DSL parsing ------------------------------------------------------------


def test_parse_email_from_with_contains() -> None:
    t = cond.parse_trigger('email_from("ana@x.com", contains="confirmo")')
    assert t.kind == "email_from"
    assert t.params["addr"] == "ana@x.com" and t.params["contains"] == "confirmo"


def test_parse_calendar_event_created() -> None:
    t = cond.parse_trigger('calendar_event("Café con Ana") created')
    assert t.kind == "calendar_event" and t.params["name"] == "Café con Ana"


def test_parse_time_at_iso() -> None:
    t = cond.parse_trigger('time_at("2026-06-17T09:00:00")')
    assert t.kind == "time_at" and t.params["when"].hour == 9


def test_parse_unknown_raises() -> None:
    with pytest.raises(ValueError):
        cond.parse_trigger('rm_rf("/")')


# ---- matching ---------------------------------------------------------------


async def _disp_factory(blob=""):
    async def dispatch(tool, args):
        return ToolResult(True, {"hits": blob}, blob, False)

    return dispatch


@pytest.mark.asyncio
async def test_time_at_matches_when_past() -> None:
    t = cond.parse_trigger('time_at("2026-06-17T09:00:00")')
    disp = await _disp_factory()
    assert await cond.trigger_matches(t, disp, dt.datetime(2026, 6, 17, 9, 1)) is True
    assert await cond.trigger_matches(t, disp, dt.datetime(2026, 6, 17, 8, 59)) is False


@pytest.mark.asyncio
async def test_email_from_matches_on_sender_and_text() -> None:
    t = cond.parse_trigger('email_from("ana@x.com", contains="confirmo")')
    hit = await _disp_factory("De: ana@x.com — Asunto: sí, confirmo la reunión")
    miss = await _disp_factory("De: otro@x.com — hola")
    now = dt.datetime(2026, 6, 16, 12, 0)
    assert await cond.trigger_matches(t, hit, now) is True
    assert await cond.trigger_matches(t, miss, now) is False


@pytest.mark.asyncio
async def test_calendar_event_matches_on_title() -> None:
    t = cond.parse_trigger('calendar_event("Café con Ana") created')
    hit = await _disp_factory("[{'title': 'Café con Ana', 'start': '...'}]")
    now = dt.datetime(2026, 6, 16, 12, 0)
    assert await cond.trigger_matches(t, hit, now) is True


# ---- store + watcher tick ---------------------------------------------------


def test_add_and_active() -> None:
    cid = cond.add('time_at("2026-06-17T09:00:00")', "create_note", {"title": "x"}, None)
    rows = cond.active()
    assert len(rows) == 1 and rows[0]["id"] == cid
    cond.mark_fired(cid)
    assert cond.active() == []


@pytest.mark.asyncio
async def test_check_once_fires_exactly_once() -> None:
    cond.add('time_at("2026-06-17T09:00:00")', "create_note", {"title": "x"}, None)
    calls = []

    async def dispatch(tool, args):
        calls.append((tool, args))
        return ToolResult(True, None, "ok", False)

    now = dt.datetime(2026, 6, 17, 9, 5)
    fired1 = await cond.check_once(dispatch, now)
    fired2 = await cond.check_once(dispatch, now)
    assert len(fired1) == 1 and fired2 == []   # no double-fire
    assert calls == [("create_note", {"title": "x"})]
    assert cond.active() == []


@pytest.mark.asyncio
async def test_expired_conditional_does_not_fire() -> None:
    cond.add('time_at("2026-06-17T09:00:00")', "create_note", {"title": "x"},
             "2026-06-16T00:00:00")  # already expired
    calls = []

    async def dispatch(tool, args):
        calls.append(tool)
        return ToolResult(True, None, "ok", False)

    fired = await cond.check_once(dispatch, dt.datetime(2026, 6, 17, 9, 5))
    assert fired == [] and calls == []
    assert cond.active() == []  # moved to expired, not active
