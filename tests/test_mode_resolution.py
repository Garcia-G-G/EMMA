"""LAUNCH-11 Part 1 — the daemon's tier is a three-valued thing, resolved once.

Before this, "managed" was inferred from the absence of a key. With two
supported tiers that inference is not enough: a user with no key AND no device
token is in a third state — fresh install, nothing chosen — and the app has to
be able to ask which tier they want.

    byok          a key in Keychain          -> straight to api.openai.com
    managed       no key, a device token     -> the proxy, unchanged
    unconfigured  neither                    -> park, publish needs_onboarding

``_is_managed()`` is kept for its 18 call sites but is now DERIVED: it means
"not BYOK", which is what every one of those call sites actually wanted. An
unconfigured daemon must answer True there, or ``_credential_preflight`` would
exit on a key it is not supposed to have yet and ``_ensure_paired`` would fall
through to a wake loop with no credential at all.

The load-bearing test in this file is the last one: **a BYOK daemon must not
touch api.theemmafamily.com.** That is the product claim of the tier.
"""

from __future__ import annotations

import asyncio

import pytest

from config import settings as settings_mod
from config.settings import Settings
from core import orchestrator

_KEY = "sk-" + "k" * 45
_TOKEN = "device-" + "t" * 40
_BACKEND_HOST = "theemmafamily.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    settings_mod.invalidate_mode_cache()
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False
    yield
    settings_mod.invalidate_mode_cache()
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False


def _settings(monkeypatch, *, key: str = "", token: str | None = None) -> Settings:
    """A Settings resolved the way a real daemon resolves one: nothing in .env,
    everything (or nothing) in Keychain."""
    keychain = {"OPENAI_API_KEY": key} if key else {}
    monkeypatch.setattr(
        "core.secrets.retrieve_sync", lambda label: keychain.get(label), raising=True
    )
    monkeypatch.setattr("core.pairing.cached_token", lambda: token, raising=True)
    monkeypatch.setattr("core.pairing._token_cache", token, raising=False)
    blank = dict.fromkeys(settings_mod._CREDENTIAL_FIELDS, "")
    settings_mod.invalidate_mode_cache()
    return Settings(_env_file=None, **blank)  # type: ignore[arg-type]


# ---- the three states ------------------------------------------------------


def test_key_in_keychain_resolves_byok(monkeypatch):
    s = _settings(monkeypatch, key=_KEY)
    assert s.mode() == "byok"
    assert s._is_managed() is False


def test_token_but_no_key_resolves_managed(monkeypatch):
    s = _settings(monkeypatch, token=_TOKEN)
    assert s.mode() == "managed"
    assert s._is_managed() is True


def test_neither_resolves_unconfigured(monkeypatch):
    s = _settings(monkeypatch)
    assert s.mode() == "unconfigured"
    # Still "not BYOK" for the legacy call sites: preflight must exempt it and
    # _ensure_paired must park it, exactly as before.
    assert s._is_managed() is True


def test_require_pairing_env_forces_non_byok_even_with_a_key(monkeypatch):
    """The explicit managed signal still wins. A managed install that somehow
    also has a key on disk is managed — that is what the operator declared."""
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    s = _settings(monkeypatch, key=_KEY, token=_TOKEN)
    assert s.mode() == "managed"
    assert s._is_managed() is True


def test_require_pairing_without_a_token_is_still_managed(monkeypatch):
    """Managed-but-not-yet-paired is 'managed awaiting pairing', not
    'unconfigured' — the tier was chosen, the pairing just has not happened."""
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    s = _settings(monkeypatch)
    assert s.mode() == "managed"


# ---- the mode is answerable in one command ---------------------------------


def test_boot_line_states_the_mode(monkeypatch):
    from core import version

    monkeypatch.setattr(settings_mod.settings, "OPENAI_API_KEY", _KEY)
    settings_mod.invalidate_mode_cache()
    assert version.boot_line().get("mode") == "byok"


def test_only_the_pairing_probe_is_memoized(monkeypatch):
    """The expensive half is cached; the cheap half must stay live.

    Caching the whole verdict was tried and is wrong: the key check is a field
    read, so a cached verdict lets one Settings' tier leak into the next one's —
    which broke four existing tests before it broke anything in production.
    """
    probes: list[int] = []

    def _count():
        probes.append(1)
        return None  # unpaired: the case core.pairing does NOT memoize itself

    monkeypatch.setattr("core.pairing.cached_token", _count, raising=True)
    monkeypatch.setattr(
        "core.secrets.retrieve_sync", lambda label: None, raising=True
    )
    settings_mod.invalidate_mode_cache()
    blank = dict.fromkeys(settings_mod._CREDENTIAL_FIELDS, "")
    s = Settings(_env_file=None, **blank)  # type: ignore[arg-type]

    assert s.mode() == "unconfigured"
    for _ in range(5):
        s.mode()
    assert len(probes) == 1, "the Keychain probe ran more than once"

    # The key check is NOT cached — it is free and it is instance state.
    monkeypatch.setattr(s, "OPENAI_API_KEY", _KEY)
    assert s.mode() == "byok"


def test_invalidation_lets_a_fresh_pairing_take_effect(monkeypatch):
    token: list[str | None] = [None]
    monkeypatch.setattr("core.pairing.cached_token", lambda: token[0], raising=True)
    monkeypatch.setattr("core.secrets.retrieve_sync", lambda label: None, raising=True)
    settings_mod.invalidate_mode_cache()
    blank = dict.fromkeys(settings_mod._CREDENTIAL_FIELDS, "")
    s = Settings(_env_file=None, **blank)  # type: ignore[arg-type]

    assert s.mode() == "unconfigured"
    token[0] = _TOKEN  # the app just paired this Mac
    assert s.mode() == "unconfigured"  # probe still memoized
    settings_mod.invalidate_mode_cache()  # which is why core.pairing calls this
    assert s.mode() == "managed"


# ---- routing ---------------------------------------------------------------


def test_byok_routes_direct_to_openai(monkeypatch):
    s = _settings(monkeypatch, key=_KEY)
    assert s.realtime_base_url() == "wss://api.openai.com/v1/realtime"
    assert s.openai_base_url() is None
    assert s.openai_api_key() == _KEY


def test_managed_still_routes_through_the_proxy(monkeypatch):
    s = _settings(monkeypatch, token=_TOKEN)
    assert s.realtime_base_url() == "wss://api.theemmafamily.com/realtime"
    assert s.openai_base_url() == s.OPENAI_BASE_URL
    assert s.openai_api_key() == _TOKEN


# ---- the product claim -----------------------------------------------------


def test_byok_daemon_never_contacts_the_managed_backend(monkeypatch):
    """The tier's whole pitch: Emma stops intermediating anyone's API access.

    Arms every outbound client the daemon owns to fail loudly on the backend
    host, then drives the boot path a BYOK daemon actually runs.
    """
    import httpx

    attempts: list[str] = []

    class _Guard(httpx.AsyncClient):
        async def request(self, method, url, *a, **kw):  # type: ignore[override]
            if _BACKEND_HOST in str(url):
                attempts.append(f"{method} {url}")
                raise AssertionError(f"BYOK daemon contacted the backend: {method} {url}")
            return await super().request(method, url, *a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Guard)

    s = _settings(monkeypatch, key=_KEY)
    monkeypatch.setattr(orchestrator, "settings", s)

    async def _boom(*_a, **_k):
        raise AssertionError("BYOK daemon probed pairing")

    monkeypatch.setattr("core.pairing.is_paired", _boom)
    monkeypatch.setattr("core.pairing.start_pairing", _boom)

    async def _drive() -> None:
        await asyncio.wait_for(orchestrator._ensure_paired(), timeout=2.0)

    asyncio.run(_drive())

    assert attempts == []
    assert orchestrator.onboarding_needed() is False
    # And nothing it would dial resolves to the backend.
    assert _BACKEND_HOST not in s.realtime_base_url()
    assert _BACKEND_HOST not in (s.openai_base_url() or "")
