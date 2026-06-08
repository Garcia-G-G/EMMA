"""Prompt 26.2-B2: eager Spotify setup callable + token-status (no spotipy needed)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from tools import music


@pytest.fixture
def emma_home(tmp_path, monkeypatch):
    monkeypatch.setattr(music.settings, "EMMA_HOME", tmp_path)
    return tmp_path


class TestTokenStatus:
    def test_missing(self, emma_home):
        assert music.spotify_token_status() == "missing"

    def test_valid(self, emma_home):
        (emma_home / "spotify_token.json").write_text(
            json.dumps({"expires_at": time.time() + 3600})
        )
        assert music.spotify_token_status() == "valid"

    def test_expired(self, emma_home):
        (emma_home / "spotify_token.json").write_text(json.dumps({"expires_at": time.time() - 10}))
        assert music.spotify_token_status() == "expired"

    def test_corrupt_file_is_missing(self, emma_home):
        (emma_home / "spotify_token.json").write_text("not json")
        assert music.spotify_token_status() == "missing"


class TestRunSpotifySetup:
    def test_no_creds_returns_false(self, monkeypatch, emma_home):
        monkeypatch.setattr(music, "_have_spotify_creds", lambda: False)
        assert asyncio.run(music.run_spotify_setup()) is False

    def test_non_interactive_returns_false(self, monkeypatch, emma_home):
        monkeypatch.setattr(music, "_have_spotify_creds", lambda: True)
        assert asyncio.run(music.run_spotify_setup(non_interactive=True)) is False

    def test_authorizes_eagerly(self, monkeypatch, emma_home):
        monkeypatch.setattr(music, "_have_spotify_creds", lambda: True)

        class _Auth:
            def get_access_token(self, as_dict=False, check_cache=True):
                return "TOKEN"

        monkeypatch.setattr(music, "_spotify_auth", lambda: _Auth())
        assert asyncio.run(music.run_spotify_setup()) is True

    def test_auth_failure_returns_false(self, monkeypatch, emma_home):
        monkeypatch.setattr(music, "_have_spotify_creds", lambda: True)

        def _boom():
            raise RuntimeError("spotipy missing")

        monkeypatch.setattr(music, "_spotify_auth", _boom)
        assert asyncio.run(music.run_spotify_setup()) is False
