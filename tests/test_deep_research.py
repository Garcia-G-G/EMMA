"""Prompt 33 — deep_research pipeline (search/fetch/LLM stubbed) + cost guard."""

from __future__ import annotations

import json

import pytest

import tools.deep_research as dr
from config.settings import settings
from core import research_budget


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EMMA_HOME", tmp_path)
    yield


_CANDS = [
    {"title": "Aggregator", "url": "https://google.com/search?q=x", "snippet": "g"},
    {"title": "OpenAI Blog", "url": "https://openai.com/blog/post", "snippet": "o"},
    {"title": "The Verge", "url": "https://www.theverge.com/x", "snippet": "v"},
    {"title": "Bing", "url": "https://bing.com/n", "snippet": "b"},
]


# ---- ranking / trust filter -------------------------------------------------


def test_rank_prefers_original_sources() -> None:
    ranked = dr._rank(_CANDS, 2)
    assert [c["title"] for c in ranked] == ["OpenAI Blog", "The Verge"]  # aggregators skipped


def test_rank_backfills_with_aggregators_if_needed() -> None:
    only_aggs = [_CANDS[0], _CANDS[3]]
    assert len(dr._rank(only_aggs, 2)) == 2  # nothing else to use → backfill


# ---- synthesis pipeline -----------------------------------------------------


@pytest.mark.asyncio
async def test_synthesis_format(monkeypatch) -> None:
    async def fake_search(q, c):
        return _CANDS

    async def fake_fetch(url):
        return f"contenido real de {url}"

    captured = {}

    async def fake_synth(q, triplets):
        captured["triplets"] = triplets
        return "OpenAI presentó algo nuevo [1]. The Verge lo cubrió [2]."

    monkeypatch.setattr(dr, "search_results", fake_search)
    monkeypatch.setattr(dr, "_fetch_text", fake_fetch)
    monkeypatch.setattr(dr, "_synthesize", fake_synth)

    res = await dr.deep_research("¿qué pasó con OpenAI?", depth=2)
    assert res.success
    assert "[1]" in res.data["answer"] and "[2]" in res.data["answer"]
    assert res.user_message == res.data["answer"]  # spoken == answer
    srcs = res.data["sources"]
    assert len(srcs) == 2 and srcs[0]["n"] == 1 and srcs[0]["title"] == "OpenAI Blog"
    assert captured["triplets"][0][0] == 1  # triplets numbered from 1
    assert research_budget.usage_today() == 1  # one call charged


@pytest.mark.asyncio
async def test_fetch_timeout_skips_gracefully(monkeypatch) -> None:
    async def fake_search(q, c):
        return _CANDS

    async def fake_fetch(url):
        return "" if "theverge" in url else "texto bueno"  # one fetch "times out"

    async def fake_synth(q, triplets):
        return "respuesta [1]"

    monkeypatch.setattr(dr, "search_results", fake_search)
    monkeypatch.setattr(dr, "_fetch_text", fake_fetch)
    monkeypatch.setattr(dr, "_synthesize", fake_synth)

    res = await dr.deep_research("openai", depth=2)
    assert res.success
    assert len(res.data["sources"]) == 1  # the unreadable source dropped, call still works


@pytest.mark.asyncio
async def test_no_readable_content_fails_cleanly(monkeypatch) -> None:
    async def fake_search(q, c):
        return _CANDS

    async def fake_fetch(url):
        return ""  # nothing readable anywhere

    monkeypatch.setattr(dr, "search_results", fake_search)
    monkeypatch.setattr(dr, "_fetch_text", fake_fetch)

    res = await dr.deep_research("openai", depth=2)
    assert not res.success and "contenido" in res.user_message.lower()


# ---- cost guard -------------------------------------------------------------


def test_budget_records_and_resets_daily(tmp_path) -> None:
    assert research_budget.usage_today() == 0
    research_budget.record()
    assert research_budget.usage_today() == 1
    # simulate a stale (previous-day) file → resets to 0
    research_budget._path().write_text(json.dumps({"date": "2020-01-01", "count": 40, "cost_usd": 0.4}))
    assert research_budget.usage_today() == 0
    assert research_budget.can_run() is True


@pytest.mark.asyncio
async def test_daily_cap_blocks_before_searching(monkeypatch) -> None:
    for _ in range(research_budget.cap()):
        research_budget.record()
    assert not research_budget.can_run()

    searched = {"hit": False}

    async def fake_search(q, c):
        searched["hit"] = True
        return _CANDS

    monkeypatch.setattr(dr, "search_results", fake_search)
    res = await dr.deep_research("openai")
    assert not res.success and "límite" in res.user_message.lower()
    assert searched["hit"] is False  # short-circuited before any spend
