"""22-B30: dynamic app routing — frontmost → running → preferred → fallback."""

from __future__ import annotations

import pytest

from core import app_router

CHROME = ("Google Chrome", "com.google.Chrome")
BRAVE = ("Brave Browser", "com.brave.Browser")
SLACK = ("Slack", "com.tinyspeck.slackmacgap")
SPOTIFY = ("Spotify", "com.spotify.client")
MUSIC = ("Music", "com.apple.Music")


@pytest.fixture(autouse=True)
def _no_caches(monkeypatch):
    """Disable the spam-guard caches so each test sees its own mocks."""
    monkeypatch.setattr(app_router, "_frontmost_cache", (0.0, None))
    monkeypatch.setattr(app_router, "_running_cache", (0.0, []))
    monkeypatch.setattr(app_router, "_FRONTMOST_TTL_S", -1.0)
    monkeypatch.setattr(app_router, "_RUNNING_TTL_S", -1.0)


def _mock_system(monkeypatch, *, front=None, running=(), pref=None):
    monkeypatch.setattr(app_router, "_frontmost_pair", lambda: front)
    monkeypatch.setattr(app_router, "_running_pairs", lambda: list(running))
    monkeypatch.setattr(app_router, "_dictionary_preference", lambda c, cat: pref)


class TestRoutingOrder:
    def test_frontmost_wins_over_preference(self, monkeypatch):
        """the user is LOOKING at Chrome → Chrome is the browser. Period."""
        _mock_system(monkeypatch, front=CHROME, running=[CHROME, BRAVE], pref="Brave Browser")
        d = app_router.inspect("browser")
        assert d.picked == "Google Chrome"
        assert d.source == "frontmost"

    def test_non_category_frontmost_falls_to_running_with_pref_tiebreak(self, monkeypatch):
        _mock_system(monkeypatch, front=SLACK, running=[CHROME, BRAVE], pref="Brave Browser")
        d = app_router.inspect("browser")
        assert d.picked == "Brave Browser"
        assert d.source == "running"

    def test_running_without_pref_uses_shortlist_order(self, monkeypatch):
        # Shortlist order is brave → chrome, so Brave wins when no preference.
        _mock_system(monkeypatch, front=SLACK, running=[CHROME, BRAVE], pref=None)
        d = app_router.inspect("browser")
        assert d.picked == "Brave Browser"  # shortlist order, not mock order
        assert d.source == "running"

    def test_nothing_running_uses_preference(self, monkeypatch):
        _mock_system(monkeypatch, front=SLACK, running=[], pref="Arc")
        d = app_router.inspect("browser")
        assert d.picked == "Arc"
        assert d.source == "preferred"

    def test_all_empty_uses_shortlist_fallback(self, monkeypatch):
        _mock_system(monkeypatch, front=None, running=[], pref=None)
        assert app_router.inspect("browser").picked == "Safari"
        assert app_router.inspect("music").picked == "Music"

    def test_music_frontmost_spotify(self, monkeypatch):
        _mock_system(monkeypatch, front=SPOTIFY, running=[SPOTIFY, MUSIC], pref="Music")
        d = app_router.inspect("music")
        assert d.picked == "Spotify"
        assert d.source == "frontmost"


class TestBundleNameMapping:
    def test_vscode_localized_name_maps_to_canonical(self, monkeypatch):
        """NSWorkspace says 'Code'; AppleScript needs 'Visual Studio Code'."""
        vscode = ("Code", "com.microsoft.VSCode")
        _mock_system(monkeypatch, front=vscode, running=[vscode], pref=None)
        d = app_router.inspect("editor")
        assert d.picked == "Visual Studio Code"


class TestResilience:
    def test_probe_failure_degrades_to_preference(self, monkeypatch):
        monkeypatch.setattr(app_router, "_frontmost_pair", lambda: None)
        monkeypatch.setattr(app_router, "_running_pairs", lambda: [])
        monkeypatch.setattr(app_router, "_dictionary_preference", lambda c, cat: "Brave Browser")
        assert app_router.preferred("browser") == "Brave Browser"

    def test_unknown_category(self):
        d = app_router.inspect("toaster")
        assert d.picked == "" and d.source == "fallback"


class TestFailureData:
    def test_shape_for_b33(self, monkeypatch):
        _mock_system(monkeypatch, front=None, running=[MUSIC], pref="Spotify")
        data = app_router.failure_data("music", "Spotify", "app_not_running")
        assert data["failure_reason"] == "app_not_running"
        assert data["wanted"] == "Spotify"
        assert "Music" in data["alternatives"]
        assert data["route_decision"]["source"] in ("running", "preferred")
