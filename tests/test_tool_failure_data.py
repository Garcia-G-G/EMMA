"""22-B33: OS-state failures carry structured data the LLM can act on."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core import app_router
from tools import music, user_browser


@pytest.fixture(autouse=True)
def _no_router_cache(monkeypatch):
    monkeypatch.setattr(app_router, "_FRONTMOST_TTL_S", -1.0)
    monkeypatch.setattr(app_router, "_RUNNING_TTL_S", -1.0)


def _mock_router(monkeypatch, *, front=None, running=(), pref=None):
    monkeypatch.setattr(app_router, "_frontmost_pair", lambda: front)
    monkeypatch.setattr(app_router, "_running_pairs", lambda: list(running))
    monkeypatch.setattr(app_router, "_dictionary_preference", lambda c, cat: pref)


SPOTIFY = ("Spotify", "com.spotify.client")
MUSIC = ("Music", "com.apple.Music")


class TestMusicAsksWhenNothingOpen:
    @pytest.mark.asyncio
    async def test_play_track_asks_instead_of_launching(self, monkeypatch):
        """V64 contract: nothing music-ish running → structured ask, no launch."""
        _mock_router(monkeypatch, front=None, running=[], pref="Spotify")
        launcher = AsyncMock()
        monkeypatch.setattr(music, "_ensure_running", launcher)

        res = await music.play_track("Bad Bunny")

        assert res.success is False
        assert res.data["failure_reason"] == "app_not_running"
        assert res.data["wanted"] == "Spotify"
        assert "Music" in res.data["alternatives"]
        assert "¿Abro" in res.user_message
        launcher.assert_not_awaited()  # never silently launched

    @pytest.mark.asyncio
    async def test_explicit_app_proceeds_and_launches(self, monkeypatch):
        """the user picked → app= bypasses the ask and launching is allowed."""
        _mock_router(monkeypatch, front=None, running=[], pref="Spotify")
        launcher = AsyncMock()
        monkeypatch.setattr(music, "_ensure_running", launcher)
        monkeypatch.setattr(music, "_spotify_search_uri", lambda q: None)
        monkeypatch.setattr(music.macos, "run_applescript", lambda s: "")

        res = await music.play_track("Bad Bunny", app="Music")

        assert res.success is True
        launcher.assert_awaited()

    @pytest.mark.asyncio
    async def test_running_app_plays_without_asking(self, monkeypatch):
        _mock_router(monkeypatch, front=None, running=[MUSIC], pref=None)
        monkeypatch.setattr(music, "_ensure_running", AsyncMock())
        monkeypatch.setattr(music.macos, "run_applescript", lambda s: "")
        res = await music.play_track("Bad Bunny")
        assert res.success is True
        assert "Music" in res.user_message

    @pytest.mark.asyncio
    async def test_pause_with_nothing_running_says_so(self, monkeypatch):
        _mock_router(monkeypatch, front=None, running=[], pref="Spotify")
        res = await music.pause()
        assert res.success is False
        assert res.data["failure_reason"] == "app_not_running"


class TestAppleScriptStateFailures:
    def test_apple_music_dash600_carries_data(self, monkeypatch):
        _mock_router(monkeypatch, front=None, running=[MUSIC], pref=None)

        def boom(script):
            raise music.macos.AppleScriptError("execution error: ... (-600)")

        monkeypatch.setattr(music.macos, "run_applescript", boom)
        res = music._apple_music("tell ...", "ok", app="Spotify")
        assert res.success is False
        assert res.data["failure_reason"] == "app_not_running"
        assert res.data["wanted"] == "Spotify"

    def test_browser_state_failure_helper(self, monkeypatch):
        _mock_router(monkeypatch, front=None, running=[], pref="Brave Browser")
        data = user_browser._state_failure("Google Chrome", "Application isn't running. (-600)")
        assert data is not None
        assert data["failure_reason"] == "app_not_running"
        assert data["wanted"] == "Google Chrome"

    def test_non_state_errors_stay_unstructured(self):
        assert user_browser._state_failure("Safari", "syntax error: blah") is None


class TestRouteDecisionOnSuccess:
    @pytest.mark.asyncio
    async def test_close_tab_success_carries_route(self, monkeypatch):
        _mock_router(monkeypatch, front=("Google Chrome", "com.google.Chrome"), running=[])
        monkeypatch.setattr(
            user_browser.macos, "osascript_or_friendly", AsyncMock(return_value=(True, ""))
        )
        res = await user_browser.close_current_tab(browser="Google Chrome")
        assert res.success
        assert res.data["route_decision"]["picked"] == "Google Chrome"
        assert res.data["route_decision"]["source"] == "frontmost"
