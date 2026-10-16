"""B49.1 — granular unit tests for memory.reflection.

The voice harness exercises reflection end-to-end and test_operational_hardening
covers the *wiring* (_BotTextTap → schedule_reflection). These tests target the
pure functions instead, so a regression surfaces as a precise failure rather
than a flaky voice scenario. Step 42: the model is injected (``configure``) and
reflection is off unless the owner opts in; here a fake model stands in. Nothing
hits the network or a real DB.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.fixture(autouse=True)
def _unwired():
    yield
    reflection.configure(None)


def _mock_client(monkeypatch, *, content: str | None = None, raises: Exception | None = None):
    """Opt in and wire a fake model; returns the AsyncMock that stands for it."""
    monkeypatch.setattr(reflection.settings, "MEMORY_CONSOLIDATION_OPT_IN", True)
    fake = AsyncMock(side_effect=raises) if raises is not None else AsyncMock(return_value=content or "")
    reflection.configure(fake)
    return fake


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
    _mock_client(monkeypatch, content='{"facts": [{"content": "vive cerca del mar"}]}')
    out = _run(reflection.reflect_once([_turn(user="vivo cerca del mar")]))
    assert out == [{"content": "vive cerca del mar", "kind": "general", "confidence": 0.7}]


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


# ---- _classify_sensitivity (rules only since Step 42) ---------------------------


def test_redaction_hit_classifies_secret(monkeypatch) -> None:
    monkeypatch.setattr(reflection.redaction, "redact", lambda s: "[REDACTED]")
    assert reflection._classify_sensitivity("mi tarjeta es 4111 1111 1111 1111") == "secret"


def test_credential_words_classify_secret_without_a_model() -> None:
    assert reflection._classify_sensitivity("mi contraseña es hunter2") == "secret"
    assert reflection._classify_sensitivity("the wifi password is in the drawer") == "secret"


def test_everything_else_is_personal() -> None:
    assert reflection._classify_sensitivity("le gusta el jazz") == "personal"


# ---- Step 42: off by default, nothing secret sent, what was sent is visible -------


def test_off_by_default_even_with_a_model_wired(monkeypatch) -> None:
    fake = AsyncMock(return_value='{"facts": []}')
    reflection.configure(fake)
    assert reflection.settings.MEMORY_CONSOLIDATION_OPT_IN is False
    assert not reflection.enabled()
    assert _run(reflection.reflect_once([_turn(user="me gusta el jazz")])) == []
    assert reflection.schedule_reflection([_turn(user="x")]) is None
    fake.assert_not_awaited()


def test_opted_in_but_no_model_wired_does_nothing(monkeypatch) -> None:
    monkeypatch.setattr(reflection.settings, "MEMORY_CONSOLIDATION_OPT_IN", True)
    assert not reflection.enabled()


def test_secret_lines_are_never_sent_and_the_payload_is_recorded(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reflection.settings, "EMMA_HOME", tmp_path)
    fake = _mock_client(monkeypatch, content='{"facts": []}')
    window = [_turn(user="mi contraseña del banco es hunter2"),
              _turn(user="me gusta el jazz", assistant="Anotado.")]
    _run(reflection.reflect_once(window))
    sent = fake.await_args.args[1]
    assert "hunter2" not in sent and "contraseña" not in sent and "jazz" in sent
    log = (tmp_path / "memory_consolidation.jsonl").read_text(encoding="utf-8")
    assert "jazz" in log and "hunter2" not in log
    assert oct((tmp_path / "memory_consolidation.jsonl").stat().st_mode & 0o777) == "0o600"


def test_a_window_with_only_secrets_sends_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reflection.settings, "EMMA_HOME", tmp_path)
    fake = _mock_client(monkeypatch, content='{"facts": []}')
    assert _run(reflection.reflect_once([_turn(user="my api key is in the notes")])) == []
    fake.assert_not_awaited()
    assert not (tmp_path / "memory_consolidation.jsonl").exists()


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
