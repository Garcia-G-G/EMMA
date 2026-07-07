"""CLIENT-INSTALL Phase 2B — daemon credential resolvers (managed vs BYOK).

Gated on EMMA_REQUIRE_PAIRING so a dev/BYOK daemon is unaffected: managed mode
uses the paired device token + the backend proxy; dev mode uses the local sk- key
against api.openai.com directly.
"""
from __future__ import annotations

from config.settings import settings
from core import pairing


def test_dev_uses_local_key_and_openai_direct(monkeypatch):
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-" + "x" * 45)
    assert settings._is_managed() is False
    assert settings.openai_api_key() == "sk-" + "x" * 45
    assert settings.openai_base_url() is None                     # SDK default → OpenAI
    assert settings.realtime_base_url() == "wss://api.openai.com/v1/realtime"


def test_managed_uses_device_token_and_proxy(monkeypatch):
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr(pairing, "_token_cache", "device-" + "y" * 40)
    assert settings._is_managed() is True
    assert settings.openai_api_key() == "device-" + "y" * 40
    assert settings.openai_base_url() == settings.OPENAI_BASE_URL
    assert settings.realtime_base_url() == "wss://api.theemmafamily.com/realtime"


def test_managed_unpaired_falls_back_to_env_key(monkeypatch):
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr(pairing, "_token_cache", None)
    monkeypatch.setattr(pairing.kc, "retrieve_sync", lambda label: None)  # cold Keychain
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-" + "z" * 45)
    assert settings.openai_api_key() == "sk-" + "z" * 45


def test_a_migrated_site_passes_proxy_base_url(monkeypatch):
    """A real AsyncOpenAI call site (embeddings) must hand the SDK the proxy base_url
    + device token in managed mode."""
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr(pairing, "_token_cache", "device-" + "y" * 40)
    import memory.embeddings as emb
    emb._client = None  # reset singleton
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(emb, "AsyncOpenAI", _FakeClient)
    emb._client_singleton()
    assert captured["base_url"] == settings.OPENAI_BASE_URL
    assert captured["api_key"] == "device-" + "y" * 40
