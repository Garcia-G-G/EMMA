"""B17 (19.6): operate inside apps — resource deep-links via the capabilities
registry + per-user [connections] in the dictionary."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.app_url_tool as aut
from core import app_capabilities, dictionary

BASE_TOML = """
[user]
preferred_lang = "es"

[user_apps.slack]
workspace = "T123"

[connections.learning-rots-local]
app = "tableplus"
kind = "connection"
name = "learning-rots-local"

[connections.slack-general]
app = "slack"
kind = "channel"
channel = "general"
"""


@pytest.fixture(autouse=True)
def _dict_sandbox(monkeypatch, tmp_path):
    """Point the dictionary at a temp TOML; restore the real cache afterwards."""
    p = tmp_path / "dictionary.toml"
    p.write_text(BASE_TOML, encoding="utf-8")
    monkeypatch.setattr(dictionary, "_DICT_PATH", p)
    dictionary.reload()
    yield p
    monkeypatch.undo()
    dictionary.reload()


@pytest.fixture()
def _open_mock(monkeypatch):
    m = AsyncMock()
    monkeypatch.setattr(aut, "_open", m)
    return m


class TestDictionaryConnections:
    def test_connections_roundtrip(self):
        conns = dictionary.connections()
        assert conns["learning-rots-local"]["app"] == "tableplus"

    def test_append_connection_keeps_dashes(self):
        dictionary.append_connection("staging-db", app="tableplus")
        assert dictionary.connections()["staging-db"]["app"] == "tableplus"
        assert dictionary.find_connection("Staging-DB")["name"] == "staging-db"

    @pytest.mark.asyncio
    async def test_remember_connection_tool(self):
        from tools.dictionary_tool import remember_connection

        res = await remember_connection("prod-db", app="tableplus")
        assert res.success
        assert dictionary.find_connection("prod-db")["app"] == "tableplus"


class TestOpenInAppRouting:
    @pytest.mark.asyncio
    async def test_tableplus_connection_from_dictionary(self, _open_mock):
        res = await aut.open_in_app("learning-rots-local")
        assert res.success
        _open_mock.assert_awaited_once_with("tableplus://connect/learning-rots-local")

    @pytest.mark.asyncio
    async def test_slack_channel_with_user_workspace(self, _open_mock):
        res = await aut.open_in_app("slack-general", app="slack", kind="channel")
        assert res.success
        _open_mock.assert_awaited_once_with("slack://channel?team=T123&id=general")

    @pytest.mark.asyncio
    async def test_explicit_app_kind_without_dictionary_entry(self, _open_mock):
        """Garcia dictates an exact connection name that isn't saved yet."""
        res = await aut.open_in_app("scratch-db", app="tableplus", kind="connection")
        assert res.success
        _open_mock.assert_awaited_once_with("tableplus://connect/scratch-db")

    @pytest.mark.asyncio
    async def test_unknown_connection_offers_to_learn_it(self, _open_mock):
        res = await aut.open_in_app("learning-rots-prod", kind="connection")
        assert res.success is False
        assert "learning-rots-prod" in res.user_message
        assert "anoto" in res.user_message
        _open_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_placeholder_friendly_error(self, _open_mock, monkeypatch):
        """A template that needs a field nobody provided → Spanish miss, no open."""
        # Slack DM template needs {user}; neither dictionary nor kwargs has it.
        res = await aut.open_in_app("whoever", app="slack", kind="dm")
        assert res.success is False
        _open_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_url_passthrough_still_works(self, _open_mock):
        res = await aut.open_in_app("https://example.com")
        assert res.success
        _open_mock.assert_awaited_once_with("https://example.com")


class TestCapabilitiesResourceTemplates:
    def test_tableplus_template_loaded(self):
        caps = app_capabilities.caps_for("tableplus")
        assert caps is not None
        assert caps.resource_url["connection"] == "tableplus://connect/{name}"

    def test_slack_templates_loaded(self):
        caps = app_capabilities.caps_for("slack")
        assert "channel" in caps.resource_url
