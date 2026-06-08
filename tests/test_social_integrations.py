"""Prompt 26: voice social posting (X / LinkedIn / Discord / WhatsApp).

Network (httpx), Keychain (secrets), the macOS `open`, and pbcopy are all
stubbed — these assert URL construction, the confirmation gate, contact
resolution, and the API/webhook-vs-composer branching.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.dictionary import Contact
from tools import social_tool as s


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Never actually open URLs or touch the clipboard/Keychain in tests."""
    monkeypatch.setattr(s, "_open", AsyncMock())
    monkeypatch.setattr(s, "_copy_clipboard", AsyncMock(return_value=True))
    monkeypatch.setattr(s.secrets, "retrieve", AsyncMock(return_value=None))


class _FakeResp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _fake_httpx(status_code: int, capture: dict | None = None, body: dict | None = None):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if capture is not None:
                capture["url"] = url
                capture.update(kw)
            return _FakeResp(status_code, body)

    return lambda *a, **k: _Client()


# ---- URL builders ---------------------------------------------------------


class TestUrlBuilders:
    def test_x_intent_encodes_text_and_emoji(self):
        url = s._composer_url("x", "post", text="hola & adiós 🚀")
        assert url.startswith("https://twitter.com/intent/tweet?text=")
        assert "%F0%9F%9A%80" in url and "%26" in url and "%20" in url  # emoji, &, space

    def test_whatsapp_walink(self):
        url = s._composer_url("whatsapp", "message", phone="5218112345678", text="hola")
        assert url == "https://wa.me/5218112345678?text=hola"

    def test_discord_channel_deeplink(self):
        url = s._composer_url("discord", "channel", server_id="111", channel_id="222")
        assert url == "discord://-/channels/111/222"

    def test_webhook_label_slugified(self):
        assert s._webhook_label("General Chat") == "discord_webhook_general_chat"


# ---- X --------------------------------------------------------------------


class TestPostToX:
    @pytest.mark.asyncio
    async def test_confirmation_gate(self):
        r = await s.post_to_x("hola")
        assert r.requires_confirmation and not r.data["truncated"]

    @pytest.mark.asyncio
    async def test_truncates_over_280(self):
        r = await s.post_to_x("a" * 400)
        assert r.data["truncated"] and len(r.data["text"]) == 280

    @pytest.mark.asyncio
    async def test_no_token_prompts_setup(self):
        # After 26.1 the composer fallback is OFF by default → prompt x_setup.
        r = await s.post_to_x("hola", confirmed=True)
        assert not r.success and r.data["needs_setup"]
        assert "emma.x_setup" in r.user_message
        s._open.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_composer_fallback_when_enabled(self, monkeypatch):
        monkeypatch.setattr(s.settings, "X_USE_COMPOSER_FALLBACK", True)
        r = await s.post_to_x("hola", confirmed=True)
        assert r.success and r.data["via"] == "composer"
        s._open.assert_awaited()

    @pytest.mark.asyncio
    async def test_api_path_on_token(self, monkeypatch):
        monkeypatch.setattr(s.secrets, "retrieve", AsyncMock(return_value="usr-token"))
        monkeypatch.setattr(s.httpx, "AsyncClient", _fake_httpx(201, body={"data": {"id": "42"}}))
        r = await s.post_to_x("hola", confirmed=True)
        assert r.success and r.data["via"] == "api" and r.data["tweet_id"] == "42"

    @pytest.mark.asyncio
    async def test_api_rate_limited(self, monkeypatch):
        monkeypatch.setattr(s.secrets, "retrieve", AsyncMock(return_value="usr-token"))
        monkeypatch.setattr(s.httpx, "AsyncClient", _fake_httpx(429))
        r = await s.post_to_x("hola", confirmed=True)
        assert not r.success and "ritmo" in r.user_message


# ---- LinkedIn -------------------------------------------------------------


class TestPostToLinkedIn:
    @pytest.mark.asyncio
    async def test_gate_then_composer_plus_clipboard(self):
        assert (await s.post_to_linkedin("shipping")).requires_confirmation
        r = await s.post_to_linkedin("shipping a feature", confirmed=True)
        assert r.success and r.data["copied"] is True
        s._open.assert_awaited()
        s._copy_clipboard.assert_awaited()


# ---- Discord --------------------------------------------------------------


class TestSendToDiscord:
    @pytest.mark.asyncio
    async def test_no_webhook_explains_setup(self):
        r = await s.send_to_discord("general", "hola", confirmed=True)
        assert not r.success and r.data["needs_webhook"]
        assert "Webhooks" in r.user_message

    @pytest.mark.asyncio
    async def test_webhook_posts_content(self, monkeypatch):
        monkeypatch.setattr(
            s.secrets, "retrieve", AsyncMock(return_value="https://discord.com/api/webhooks/1/x")
        )
        captured: dict = {}
        monkeypatch.setattr(s.httpx, "AsyncClient", _fake_httpx(204, captured))
        r = await s.send_to_discord("general", "hola equipo", confirmed=True)
        assert r.success and r.data["via"] == "webhook"
        assert captured["json"] == {"content": "hola equipo"}


# ---- WhatsApp -------------------------------------------------------------


class TestSendWhatsApp:
    @pytest.mark.asyncio
    async def test_resolves_contact_to_phone(self, monkeypatch):
        juan = Contact("juan", "Juan", "", "amigo", ["juancho"], phone="+52 81 1234 5678")
        monkeypatch.setattr(s.dictionary, "find_contact", lambda q: juan)
        r = await s.send_whatsapp("Juan", "nos vemos", confirmed=False)
        assert r.requires_confirmation and r.data["phone"] == "528112345678"
        assert r.data["to"] == "Juan"

    @pytest.mark.asyncio
    async def test_literal_number(self, monkeypatch):
        monkeypatch.setattr(s.dictionary, "find_contact", lambda q: None)
        r = await s.send_whatsapp("+1 (555) 123-4567", "hi", confirmed=True)
        assert r.success
        s._open.assert_awaited()

    @pytest.mark.asyncio
    async def test_unknown_contact_friendly_error(self, monkeypatch):
        monkeypatch.setattr(s.dictionary, "find_contact", lambda q: None)
        r = await s.send_whatsapp("Nadie", "hola")
        assert not r.success and "No encontré" in r.user_message
