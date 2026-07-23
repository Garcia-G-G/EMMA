"""B49.1 — granular unit tests for memory.reflection.

The voice harness exercises reflection end-to-end and test_operational_hardening
covers the *wiring* (_BotTextTap → schedule_reflection). These tests target the
pure functions instead, so a regression surfaces as a precise failure rather
than a flaky voice scenario. The OpenAI client is mocked; nothing hits the network
or a real DB.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory import reflection


@pytest.fixture(autouse=True)
def _restore_current_event_loop():
    """`_run` uses asyncio.run(), which closes its loop and clears the current
    one. Some legacy suites (e.g. test_registry) still call the deprecated
    asyncio.get_event_loop(); leave a live loop so test ORDER can't break them.
    The real fix belongs in those legacy suites — flagged in the 24.1 report."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _run(coro):
    return asyncio.run(coro)


def _turn(user: str | None = None, assistant: str | None = None) -> SimpleNamespace:
    # _format_window only reads .user_text / .assistant_text — duck-type the Turn.
    return SimpleNamespace(user_text=user, assistant_text=assistant)


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _mock_client(monkeypatch, *, content: str | None = None, raises: Exception | None = None):
    client = MagicMock()
    if raises is not None:
        client.chat.completions.create = AsyncMock(side_effect=raises)
    else:
        client.chat.completions.create = AsyncMock(return_value=_completion(content or ""))
    monkeypatch.setattr(reflection, "_get_client", lambda: client)
    return client


# ---- reflect_once: parsing + cleaning ---------------------------------------


def test_empty_window_returns_no_facts() -> None:
    """No turns to reflect on → no LLM call, empty result."""
    assert _run(reflection.reflect_once([])) == []


def test_window_with_no_text_returns_no_facts() -> None:
    """Turns that carry no text produce an empty body → short-circuit to []."""
    assert _run(reflection.reflect_once([_turn(), _turn()])) == []


def test_malformed_json_is_dropped(monkeypatch) -> None:
    """Garbage from gpt-4o-mini must degrade to [] — never raise."""
    _mock_client(monkeypatch, content="this is not json {")
    assert _run(reflection.reflect_once([_turn(user="hola")])) == []


def test_facts_not_a_list_returns_empty(monkeypatch) -> None:
    """A well-formed JSON object with a non-list `facts` is ignored."""
    _mock_client(monkeypatch, content='{"facts": "oops"}')
    assert _run(reflection.reflect_once([_turn(user="hola")])) == []


def test_valid_fact_is_passed_through(monkeypatch) -> None:
    _mock_client(
        monkeypatch,
        content='{"facts": [{"content": "toma café negro", "kind": "preference", "confidence": 0.9}]}',
    )
    out = _run(reflection.reflect_once([_turn(user="me gusta el café negro")]))
    assert out == [{"content": "toma café negro", "kind": "preference", "confidence": 0.9}]


def test_missing_kind_and_confidence_get_defaults(monkeypatch) -> None:
    _mock_client(monkeypatch, content='{"facts": [{"content": "vive en San José"}]}')
    out = _run(reflection.reflect_once([_turn(user="vivo en San José")]))
    assert out == [{"content": "vive en San José", "kind": "general", "confidence": 0.7}]


def test_confidence_is_clamped_and_coerced(monkeypatch) -> None:
    """Out-of-range floats clamp to [0,1]; non-numeric falls back to 0.7."""
    _mock_client(
        monkeypatch,
        content=(
            '{"facts": ['
            '{"content": "a", "confidence": 5},'
            '{"content": "b", "confidence": "high"}'
            "]}"
        ),
    )
    out = _run(reflection.reflect_once([_turn(user="x")]))
    assert [f["confidence"] for f in out] == [1.0, 0.7]


def test_empty_content_facts_are_skipped(monkeypatch) -> None:
    _mock_client(
        monkeypatch,
        content='{"facts": [{"content": "  "}, {"content": "real fact"}]}',
    )
    out = _run(reflection.reflect_once([_turn(user="x")]))
    assert [f["content"] for f in out] == ["real fact"]


# ---- _classify_sensitivity --------------------------------------------------


def test_redaction_hit_classifies_secret_without_llm(monkeypatch) -> None:
    """A deterministic redaction match short-circuits to 'secret' — no LLM call."""
    monkeypatch.setattr(reflection.redaction, "redact", lambda s: "[REDACTED]")
    client = _mock_client(monkeypatch, content="personal")  # would say personal if asked
    assert _run(reflection._classify_sensitivity("mi tarjeta es 4111 1111 1111 1111")) == "secret"
    client.chat.completions.create.assert_not_awaited()


def test_llm_classifies_secret(monkeypatch) -> None:
    monkeypatch.setattr(reflection.redaction, "redact", lambda s: s)  # no pattern hit
    _mock_client(monkeypatch, content="secret")
    assert _run(reflection._classify_sensitivity("mi contraseña es hunter2")) == "secret"


def test_llm_failure_defaults_to_personal(monkeypatch) -> None:
    """On LLM error we default to personal — the redaction net already caught
    pattern-based secrets, and personal is the safe-to-store tier."""
    monkeypatch.setattr(reflection.redaction, "redact", lambda s: s)
    _mock_client(monkeypatch, raises=RuntimeError("api down"))
    assert _run(reflection._classify_sensitivity("le gusta el jazz")) == "personal"


# ---- reflect_async: no partial / spurious writes (B49.3 regression) ----------


def test_reflect_async_empty_window_writes_nothing(monkeypatch) -> None:
    remember = AsyncMock()
    monkeypatch.setattr(reflection.long_term, "remember", remember)
    _run(reflection.reflect_async([]))
    remember.assert_not_called()


def test_reflect_async_malformed_json_writes_nothing(monkeypatch) -> None:
    """REGRESSION: garbage from gpt-4o-mini must never land a partial fact in
    memory.db. A parse failure yields zero facts → zero writes."""
    _mock_client(monkeypatch, content="not json at all")
    remember = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(reflection.long_term, "remember", remember)
    monkeypatch.setattr(reflection.secrets, "store", store)
    _run(reflection.reflect_async([_turn(user="hola Emma")]))
    remember.assert_not_called()
    store.assert_not_called()
