"""B19 (19.6): list tabs, close duplicates (protecting google.com), close by
pattern — all via one AppleScript round trip per browser."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.user_browser as ub

# window‖index‖title‖url — what the enumeration script returns.
RAW = (
    "1‖1‖Alpha‖https://a.com/x\n"
    "1‖2‖Alpha dup‖https://a.com/x?utm_source=tw\n"
    "1‖3‖Calendario‖https://google.com/cal\n"
    "2‖1‖Calendario‖https://google.com/cal\n"
    "2‖2‖watch on youtube.com‖https://youtu.be/x\n"
)


@pytest.fixture()
def _wired(monkeypatch):
    monkeypatch.setattr(ub.macos, "osascript", AsyncMock(return_value=RAW))
    closer = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(ub.macos, "osascript_or_friendly", closer)
    monkeypatch.setattr(ub.app_router, "preferred", lambda _cat: "Safari")
    return closer


class TestListTabs:
    @pytest.mark.asyncio
    async def test_lists_all_tabs_with_window_index(self, _wired):
        res = await ub.list_browser_tabs()
        assert res.success
        tabs = res.data["tabs"]
        assert len(tabs) == 5
        assert tabs[0] == {"window": 1, "index": 1, "title": "Alpha", "url": "https://a.com/x"}
        assert "5 pestañas" in res.user_message
        assert "2 ventanas" in res.user_message


class TestCanonicalUrl:
    def test_strips_tracking_fragment_and_slash(self):
        assert ub._canon_url("https://A.com/x/?utm_source=tw&b=1#frag") == ub._canon_url(
            "https://a.com/x?b=1"
        )

    def test_different_paths_stay_different(self):
        assert ub._canon_url("https://a.com/x") != ub._canon_url("https://a.com/y")


class TestCloseDuplicates:
    @pytest.mark.asyncio
    async def test_finds_one_candidate_and_protects_google(self, _wired):
        res = await ub.close_duplicate_tabs()
        assert res.requires_confirmation is True
        candidates = res.data["close"]
        assert len(candidates) == 1  # a.com dup; google.com dup is protected
        assert candidates[0]["url"].startswith("https://a.com/x?utm")
        assert "google" in res.user_message.lower()

    @pytest.mark.asyncio
    async def test_confirmed_closes_in_reverse_order(self, _wired):
        closer = _wired
        res = await ub.close_duplicate_tabs(protect_domains=[], confirmed=True)
        assert res.success
        script = closer.await_args.args[0]
        # both dups close: (2,1) must come BEFORE (1,2) so indices stay valid
        assert script.index("close tab 1 of window 2") < script.index("close tab 2 of window 1")

    @pytest.mark.asyncio
    async def test_explicit_empty_protect_list_overrides_default(self, _wired):
        res = await ub.close_duplicate_tabs(protect_domains=[])
        assert len(res.data["close"]) == 2  # google dup now closable


class TestCloseMatching:
    @pytest.mark.asyncio
    async def test_matches_title_and_url(self, _wired):
        res = await ub.close_tabs_matching("youtube.com")
        assert res.requires_confirmation is True
        assert len(res.data["close"]) == 1  # matched by TITLE (url is youtu.be)

        res = await ub.close_tabs_matching("a.com")
        assert len(res.data["close"]) == 2  # matched by URL

    @pytest.mark.asyncio
    async def test_protects_google_by_default(self, _wired):
        res = await ub.close_tabs_matching("calendario")
        assert res.data["close"] == []
