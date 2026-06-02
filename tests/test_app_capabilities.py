"""Phase 19.1: app-capabilities registry + open_in_app deep-linking."""

from __future__ import annotations

import tomllib

import pytest

from core import app_capabilities as ac
from tools import app_url_tool

_REAL_CAPS = ac._CAPS_PATH


@pytest.fixture(autouse=True)
def _restore_real_caps():
    """After each test, force the registry cache back to the shipped file."""
    yield
    ac._CAPS_PATH = _REAL_CAPS
    ac.reload()


class TestRegistry:
    def test_parses_at_least_15(self):
        assert ac.reload() >= 15
        # The shipped TOML is valid.
        tomllib.loads(ac._CAPS_PATH.read_text())

    def test_caps_for_case_insensitive(self):
        assert ac.caps_for("Slack").url_scheme == "slack"
        assert ac.caps_for("slack").category == "chat"
        assert ac.caps_for("GitHub Desktop").url_scheme == "x-github-client"
        assert ac.caps_for("nonexistent") is None

    def test_apps_with_url_scheme(self):
        s = set(ac.apps_with("url_scheme"))
        assert {"slack", "figma", "linear", "notion"} <= s
        assert "apple_music" not in s  # no scheme

    def test_apps_in_category_chat(self):
        chat = set(ac.apps_in_category("chat"))
        assert {"slack", "discord", "whatsapp"} <= chat


class TestRememberApp:
    @pytest.mark.asyncio
    async def test_append_roundtrip(self, tmp_path, monkeypatch):
        caps = tmp_path / "app_capabilities.toml"
        caps.write_text('[slack]\nurl_scheme = "slack"\ncategory = "chat"\n', encoding="utf-8")
        monkeypatch.setattr(ac, "_CAPS_PATH", caps)
        ac.reload()

        r = await app_url_tool.remember_app(
            "Raycast", url_scheme="raycast", category="productivity"
        )
        assert r.success is True
        assert ac.caps_for("Raycast").url_scheme == "raycast"
        # File still valid TOML.
        tomllib.loads(caps.read_text())
        ac.reload()  # back to the (still temp) file; real file untouched


class TestOpenInApp:
    @pytest.mark.asyncio
    async def test_scheme_url_opens_directly(self, monkeypatch):
        captured = []

        async def _fake_open(*args):
            captured.append(args)

        monkeypatch.setattr(app_url_tool, "_open", _fake_open)
        r = await app_url_tool.open_in_app("slack://channel?team=T1&id=general")
        assert r.success is True
        assert captured == [("slack://channel?team=T1&id=general",)]

    @pytest.mark.asyncio
    async def test_name_plus_app_builds_slack_deeplink(self, monkeypatch):
        captured = []

        async def _fake_open(*args):
            captured.append(args)

        monkeypatch.setattr(app_url_tool, "_open", _fake_open)
        monkeypatch.setattr(app_url_tool.dictionary, "user_app", lambda n: {"workspace": "T9"})
        r = await app_url_tool.open_in_app("general", app="Slack")
        assert r.success is True
        assert captured == [("slack://channel?team=T9&id=general",)]

    @pytest.mark.asyncio
    async def test_things_add_urlencodes(self, monkeypatch):
        captured = []

        async def _fake_open(*args):
            captured.append(args)

        monkeypatch.setattr(app_url_tool, "_open", _fake_open)
        r = await app_url_tool.open_in_app("comprar leche", app="Things")
        assert r.success is True
        assert captured == [("things:///add?title=comprar+leche",)]

    @pytest.mark.asyncio
    async def test_no_scheme_app_launches(self, monkeypatch):
        captured = []

        async def _fake_open(*args):
            captured.append(args)

        monkeypatch.setattr(app_url_tool, "_open", _fake_open)
        r = await app_url_tool.open_in_app("anything", app="Figma")
        assert r.success is True
        assert captured == [("-a", "Figma")]

    @pytest.mark.asyncio
    async def test_plain_target_no_app_asks(self):
        r = await app_url_tool.open_in_app("general")
        assert r.success is False
