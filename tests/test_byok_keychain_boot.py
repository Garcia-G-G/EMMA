"""LAUNCH-10 Part 2 — a BYOK daemon whose key lives only in Keychain must boot.

The security convention mandates ``core.secrets.bootstrap_from_env``, which
blanks the ``.env`` line (``core/secrets.py:184``) once the value reads back
from Keychain. From then on the ONLY copy of ``OPENAI_API_KEY`` is in the
Keychain, and ``Settings`` must read it back.

It did not. ``_fill_credentials_from_keychain`` returns early when the field is
non-empty, so by the time it reaches the skip list the field is *guaranteed*
blank — and ``_is_managed()`` is defined as ``not self.OPENAI_API_KEY``. The
managed-mode branch was therefore always taken, and the three
``_BYOK_ONLY_CREDENTIALS`` were never read back for anyone.

The whole chain then fails silently, green:

  blank key -> _is_managed() True -> _credential_preflight exempts (no exit)
  -> _ensure_paired parks forever on a device token that will never arrive
  -> realtime_base_url() points at the managed proxy with no bearer

These tests pin the ordering: the Keychain gets its say about
``OPENAI_API_KEY`` BEFORE anything asks which mode we are in.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import structlog

import emma.__main__ as emma_main
from config import settings as settings_mod
from config.settings import Settings
from core import orchestrator

_BYOK_KEY = "sk-" + "b" * 45


@pytest.fixture(autouse=True)
def _clean_park_state():
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False
    yield
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False


def _migrated_settings(monkeypatch, *, keychain: dict[str, str]) -> Settings:
    """A Settings built the way a post-migration BYOK daemon builds one:
    every credential blank in .env, the values only in Keychain."""
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setattr(
        "core.secrets.retrieve_sync", lambda label: keychain.get(label), raising=True
    )
    blank = dict.fromkeys(settings_mod._CREDENTIAL_FIELDS, "")
    return Settings(_env_file=None, **blank)  # type: ignore[arg-type]


# ---- the ordering bug itself ---------------------------------------------


def test_migrated_byok_key_is_read_back_from_keychain(monkeypatch):
    s = _migrated_settings(monkeypatch, keychain={"OPENAI_API_KEY": _BYOK_KEY})
    assert s.OPENAI_API_KEY == _BYOK_KEY
    assert s._is_managed() is False


def test_a_genuinely_managed_daemon_still_resolves_managed(monkeypatch):
    """No key in .env AND none in Keychain is what actually defines managed mode."""
    s = _migrated_settings(monkeypatch, keychain={})
    assert s.OPENAI_API_KEY == ""
    assert s._is_managed() is True


def test_the_other_byok_only_credentials_come_back_too(monkeypatch):
    """POSTGRES_DSN and PICOVOICE_ACCESS_KEY were collateral damage of the same
    dead branch — with a local key present, the mode is BYOK and they load."""
    s = _migrated_settings(
        monkeypatch,
        keychain={
            "OPENAI_API_KEY": _BYOK_KEY,
            "POSTGRES_DSN": "postgresql://localhost/emma",
            "PICOVOICE_ACCESS_KEY": "pv-" + "c" * 20,
        },
    )
    assert s.POSTGRES_DSN == "postgresql://localhost/emma"
    assert s.PICOVOICE_ACCESS_KEY == "pv-" + "c" * 20


def test_managed_daemon_still_skips_the_byok_only_keychain_reads(monkeypatch):
    """The fan-out fix (c478a63) must survive: a managed daemon asks the Keychain
    about OPENAI_API_KEY (it has to — that answer decides the mode) but not about
    POSTGRES_DSN or PICOVOICE_ACCESS_KEY."""
    asked: list[str] = []

    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)

    def _spy(label: str) -> str | None:
        asked.append(label)
        return None

    monkeypatch.setattr("core.secrets.retrieve_sync", _spy, raising=True)
    blank = dict.fromkeys(settings_mod._CREDENTIAL_FIELDS, "")
    Settings(_env_file=None, **blank)  # type: ignore[arg-type]

    assert "OPENAI_API_KEY" in asked
    assert "POSTGRES_DSN" not in asked
    assert "PICOVOICE_ACCESS_KEY" not in asked


# ---- and the boot chain it silently broke ---------------------------------


def test_migrated_byok_daemon_passes_credential_preflight(monkeypatch, tmp_path):
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))  # keep boot_guard out of ~/.emma
    s = _migrated_settings(monkeypatch, keychain={"OPENAI_API_KEY": _BYOK_KEY})
    monkeypatch.setattr(emma_main, "settings", s)
    assert emma_main._credential_preflight(structlog.get_logger("test")) is None


@pytest.mark.asyncio
async def test_migrated_byok_daemon_reaches_the_wake_loop(monkeypatch):
    """The definition of done: it does not park, and it never asks about pairing.

    Bounded by wait_for on purpose. The regression's signature is that
    ``_ensure_paired`` never returns — an unbounded await here hangs the whole
    suite instead of failing it, which is how this stayed invisible.
    """
    s = _migrated_settings(monkeypatch, keychain={"OPENAI_API_KEY": _BYOK_KEY})
    monkeypatch.setattr(orchestrator, "settings", s)
    never = AsyncMock(side_effect=AssertionError("a BYOK daemon must not probe pairing"))
    monkeypatch.setattr("core.pairing.is_paired", never)

    try:
        await asyncio.wait_for(orchestrator._ensure_paired(), timeout=2.0)
    except TimeoutError:
        pytest.fail("BYOK daemon parked waiting to be paired instead of reaching the wake loop")

    assert orchestrator.onboarding_needed() is False


@pytest.mark.asyncio
async def test_migrated_byok_daemon_talks_to_openai_not_the_proxy(monkeypatch):
    s = _migrated_settings(monkeypatch, keychain={"OPENAI_API_KEY": _BYOK_KEY})
    assert s.openai_api_key() == _BYOK_KEY
    assert s.realtime_base_url() == "wss://api.openai.com/v1/realtime"
    assert s.openai_base_url() is None
