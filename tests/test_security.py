"""Tests for the local-first security layer (redaction, Keychain, routing).

Redaction + log tests are pure. Keychain tests need the macOS `security` CLI
(skipped elsewhere). Memory/reflection tests mock the embedding + LLM calls so
they never hit the network.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from unittest.mock import AsyncMock, patch

import pytest

from core.redaction import redact, redaction_processor

_HAS_SECURITY = sys.platform == "darwin" and shutil.which("security") is not None
_FAKE_VEC = [0.05] * 1536


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# ---------- redaction ----------


def test_redaction_positive_cases() -> None:
    assert "[REDACTED:CREDIT_CARD]" in redact("card 4111 1111 1111 1111")
    assert "[REDACTED:CURP]" in redact("CURP HEGG560427MVZRRL04")
    assert "[REDACTED:US_SSN]" in redact("ssn 123-45-6789")
    assert "[REDACTED:API_KEY_LIKE]" in redact("key sk-proj-" + "a" * 40)
    assert "[REDACTED:PHONE_INTL]" in redact("tel +52 81 1234 5678")


def test_redaction_negative_cases() -> None:
    # ISO date and short numbers must survive untouched.
    assert redact("Date 2024-01-15 should not change") == "Date 2024-01-15 should not change"
    assert redact("meet at 5pm in room 12") == "meet at 5pm in room 12"


def test_luhn_rejects_invalid_card() -> None:
    # 4111...1112 fails Luhn -> not a CREDIT_CARD (may still match PHONE; that's fine).
    assert "[REDACTED:CREDIT_CARD]" not in redact("4111 1111 1111 1112")


def test_redaction_bare_digit_ids_not_treated_as_phone() -> None:
    # Bare digit runs (e.g. a 10-digit epoch token-expiry timestamp) are IDs,
    # not phones — they must stay readable in logs. A real phone has a "+" or
    # internal separators. (Card-length bare runs that coincidentally pass Luhn
    # may still redact as CREDIT_CARD; that's the safe direction, so we don't
    # fight it here.)
    assert redact("X token expires 1750000000") == "X token expires 1750000000"
    assert "[REDACTED:PHONE_INTL]" not in redact("tweet 1760123456789012345 posted")
    # A genuinely phone-shaped number is still redacted.
    assert "[REDACTED:PHONE_INTL]" in redact("call +52 81 1234 5678")
    assert "[REDACTED:PHONE_INTL]" in redact("call 81-1234-5678")


def test_redaction_processor_redacts_strings_only() -> None:
    ev = redaction_processor(None, "info", {"event": "paid 4111 1111 1111 1111", "count": 7})
    assert "[REDACTED:CREDIT_CARD]" in ev["event"]
    assert ev["count"] == 7  # non-string values untouched


def test_redaction_processor_recurses_into_nested_args() -> None:
    # A secret passed inside a dict/list value (e.g. log.error(..., args={...}))
    # must still be redacted, not just top-level string fields.
    ev = redaction_processor(
        None,
        "error",
        {"event": "bad", "args": {"key": "sk-proj-" + "a" * 40}, "items": ["123-45-6789"]},
    )
    assert "[REDACTED:API_KEY_LIKE]" in ev["args"]["key"]
    assert "[REDACTED:US_SSN]" in ev["items"][0]


def test_log_redaction_end_to_end(capsys) -> None:
    import structlog

    structlog.configure(
        processors=[redaction_processor, structlog.processors.KeyValueRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        structlog.get_logger("t").info("payment", card="4111 1111 1111 1111")
        out = capsys.readouterr().out
        assert "4111" not in out
        assert "[REDACTED:CREDIT_CARD]" in out
    finally:
        structlog.reset_defaults()


# ---------- Keychain ----------


@pytest.mark.skipif(not _HAS_SECURITY, reason="needs macOS security CLI")
def test_secrets_roundtrip() -> None:
    from core.secrets import delete, retrieve, store

    async def t() -> None:
        await store("sec_test_roundtrip", "value-xyz-123")
        assert await retrieve("sec_test_roundtrip") == "value-xyz-123"
        assert await delete("sec_test_roundtrip") is True
        assert await retrieve("sec_test_roundtrip") is None

    asyncio.run(t())


@pytest.mark.skipif(not _HAS_SECURITY, reason="needs macOS security CLI")
def test_bootstrap_from_env_moves_credentials(tmp_path) -> None:
    from core.secrets import bootstrap_from_env, delete, retrieve

    env = tmp_path / "x.env"
    env.write_text("OPENAI_API_KEY=sk-" + "a" * 40 + "\nWHISPER_MODEL=whisper-1\n")

    async def t() -> None:
        result = await bootstrap_from_env(env)
        assert "OPENAI_API_KEY" in result["moved"]
        assert "WHISPER_MODEL" in result["skipped"]
        text = env.read_text()
        assert "moved to Keychain" in text
        assert "WHISPER_MODEL=whisper-1" in text  # non-credential preserved
        assert await retrieve("OPENAI_API_KEY")
        await delete("OPENAI_API_KEY")

    asyncio.run(t())


# ---------- memory routing (mocked APIs) ----------


def test_priming_block_excludes_vault_ref(tmp_path) -> None:
    from memory import long_term

    with (
        patch("memory.long_term.settings") as ms,
        patch("memory.long_term.embeddings.embed", new=AsyncMock(return_value=_FAKE_VEC)),
    ):
        ms.MEMORY_DB_PATH = tmp_path / "p.db"
        ms.MEMORY_PRIMING_TOP_N = 10

        async def t() -> None:
            await long_term.remember("the user likes coffee", kind="pref")
            await long_term.remember("ignored", kind="fact", vault_ref="fact_zzz")
            pb = await long_term.priming_block()
            assert "coffee" in pb
            assert "fact_zzz" not in pb

        asyncio.run(t())


def test_reflection_routes_secret_to_keychain_not_db(tmp_path) -> None:
    from memory import long_term, reflection
    from memory.short_term import Turn

    fact = {"content": "mi contraseña del banco es hunter2", "kind": "fact", "confidence": 0.9}
    with (
        patch("memory.long_term.settings") as ms,
        patch("memory.long_term.embeddings.embed", new=AsyncMock(return_value=_FAKE_VEC)),
        patch("memory.reflection.reflect_once", new=AsyncMock(return_value=[fact])),
        patch("memory.reflection._classify_sensitivity", new=AsyncMock(return_value="secret")),
        patch("memory.reflection.secrets.store", new=AsyncMock()) as store_mock,
    ):
        ms.MEMORY_DB_PATH = tmp_path / "m.db"
        ms.MEMORY_PRIMING_TOP_N = 10

        asyncio.run(reflection.reflect_async([Turn("u", "a", 0.0)]))

        # secret value went to Keychain (mocked), with the real value
        assert store_mock.await_count == 1
        assert "hunter2" in store_mock.await_args.args[1]

        # memory.db row carries a placeholder + vault_ref, never the value
        with long_term._connect() as conn:
            rows = conn.execute("SELECT content, vault_ref FROM facts").fetchall()
        assert len(rows) == 1
        assert "hunter2" not in rows[0]["content"]
        assert rows[0]["vault_ref"] is not None


def test_redaction_spares_hashes_uuids_and_ids() -> None:
    # audit fix: the tool-egress seam must not corrupt legit output the LLM needs.
    from core.redaction import redact
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"   # 40-char git SHA
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    tweet = "1718900000123456789"                       # 19-digit ID
    for s in (sha, uuid, tweet):
        assert redact(f"value {s} here") == f"value {s} here", s
    # but real secrets / cards still go:
    assert "REDACTED" in redact("sk-proj-" + "a" * 40)
    assert "REDACTED:CREDIT_CARD" in redact("4111 1111 1111 1111")
