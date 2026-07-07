"""Tests for the Prompt 15.9 fixes.

Bug 1 — credential migration is verify-then-blank with rollback:
  * a failing ``store`` leaves ``.env`` untouched and raises;
  * a readback mismatch leaves ``.env`` untouched and raises;
  * a credential line is blanked ONLY after the value reads back from Keychain.

Bug 3 — auth-error handling:
  * ``_looks_like_openai_key`` shape check (positive + negative);
  * ``_is_terminal_auth_error`` classification (terminal vs transient);
  * ``run_session`` raises ``SystemExit(2)`` on a terminal auth error and
    returns normally otherwise (the reconnect case);
  * ``AuthErrorWatcher`` cancels the task + records the error on a terminal
    ``ErrorFrame`` and ignores a transient one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import secrets


@pytest.fixture(autouse=True)
def _restore_loop():
    # asyncio.run() leaves no current loop (py3.12); restore one so sibling
    # tests using get_event_loop() are unaffected.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# --- Bug 1: bootstrap_from_env verify-then-blank + rollback ----------------

_ENV_BODY = "OPENAI_API_KEY=sk-realvalue-aaaaaaaaaaaaaaaaaaaaaaaa\nFOO=bar\n"


def test_bootstrap_rollback_when_store_raises(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_ENV_BODY)

    async def boom(*_a, **_k):
        raise RuntimeError("keychain store failed (rc=1)")

    with patch("core.secrets.store", new=boom):  # noqa: SIM117
        with pytest.raises(RuntimeError):
            asyncio.run(secrets.bootstrap_from_env(env))

    assert env.read_text() == _ENV_BODY  # .env left intact


def test_bootstrap_rollback_when_readback_fails(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_ENV_BODY)

    async def ok_store(*_a, **_k):
        return None

    async def none_retrieve(_label):
        return None  # value did not persist

    with (
        patch("core.secrets.store", new=ok_store),
        patch("core.secrets.retrieve", new=none_retrieve),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(secrets.bootstrap_from_env(env))

    assert env.read_text() == _ENV_BODY  # .env left intact (no value blanked)


def test_bootstrap_blanks_only_after_readback(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_ENV_BODY)
    stored: dict[str, str] = {}

    async def ok_store(label, value, kind="secret"):
        stored[label] = value

    async def good_retrieve(label):
        return stored.get(label)

    with (
        patch("core.secrets.store", new=ok_store),
        patch("core.secrets.retrieve", new=good_retrieve),
    ):
        result = asyncio.run(secrets.bootstrap_from_env(env))

    assert "OPENAI_API_KEY" in result["moved"]
    text = env.read_text()
    assert "sk-realvalue" not in text  # value blanked
    assert "moved to Keychain" in text
    assert "FOO=bar" in text  # non-credential untouched


def test_store_raises_on_nonzero_and_omits_value():
    """store must raise on non-zero rc and never include the secret value."""

    async def fake_run(_args):
        return (1, "", "the-keychain-error")

    secret = "sk-supersecret-value-do-not-leak"
    with patch("core.secrets._run", new=fake_run), pytest.raises(RuntimeError) as ei:
        asyncio.run(secrets.store("OPENAI_API_KEY", secret))
    assert secret not in str(ei.value)
    assert "OPENAI_API_KEY" in str(ei.value)


# --- Bug 3: key shape + terminal-error classification ----------------------


def test_looks_like_openai_key():
    from core.conversation import _looks_like_openai_key as f

    assert f("sk-" + "a" * 45) is True
    assert f("x" * 50) is True  # Phase 2B: Emma device bearer (urlsafe ≥40, no sk-)
    assert f("") is False
    assert f("sk-") is False
    assert f("sk-12345") is False  # too short (and not a ≥40 bearer)
    assert f("x" * 30) is False  # too short for a device bearer
    assert f("sk-" + "a" * 40 + " " + "b" * 5) is False  # contains a space
    assert f("sk-" + "a" * 40 + "\t" + "b" * 5) is False  # contains a tab


def test_is_terminal_auth_error():
    from core.conversation import _is_terminal_auth_error as t

    assert t("Error: invalid_api_key: Incorrect API key") is True
    assert t("permission_denied for the requested model") is True
    assert t("model_not_found: gpt-x") is True
    assert t("organization_not_authorized") is True
    assert t("Error connecting: server rejected WebSocket connection: HTTP 401") is True
    # transient — reconnect should still apply
    assert t("Error connecting: [Errno 8] nodename nor servname provided") is False
    assert t("503 Service Unavailable") is False
    assert t("read timeout") is False


# --- Bug 3: run_session termination decision -------------------------------


def _run_session_with(terminal_error):
    """Drive run_session with a stubbed pipeline whose watcher saw `terminal_error`."""
    from core import conversation as conv

    class FakeTask:
        async def queue_frame(self, _f):
            return None

    class FakeRunner:
        def __init__(self, **_k):
            pass

        async def run(self, _task):
            return None  # session ended (any error already handled in-pipeline)

    fake_watcher = SimpleNamespace(terminal_error=terminal_error)
    # run_session now also receives the llm so it can close the WebSocket on exit (B1).
    fake_llm = MagicMock()
    fake_llm._disconnect = AsyncMock()

    async def fake_build(immediate_command=False):
        return (None, FakeTask(), None, object(), fake_watcher, fake_llm)

    with (
        patch.object(conv, "_looks_like_openai_key", return_value=True),
        patch.object(conv, "build_pipeline", new=fake_build),
        patch.object(conv, "PipelineRunner", new=FakeRunner),
    ):
        return asyncio.run(conv.run_session())


def test_run_session_exits_2_on_terminal_auth_error():
    with pytest.raises(SystemExit) as ei:
        _run_session_with("invalid_api_key: bad key")
    assert ei.value.code == 2


def test_run_session_returns_on_transient_error():
    # No terminal error -> run_session returns normally; orchestrator reconnects.
    assert _run_session_with(None) is None


def test_run_session_preflight_exits_2_on_bad_key():
    from core import conversation as conv

    with (
        patch.object(conv, "_looks_like_openai_key", return_value=False),
        pytest.raises(SystemExit) as ei,
    ):
        asyncio.run(conv.run_session())
    assert ei.value.code == 2


# --- Bug 3: AuthErrorWatcher behavior --------------------------------------


def _drive_watcher(error_message):
    from pipecat.frames.frames import ErrorFrame
    from pipecat.processors.frame_processor import FrameDirection

    from core import conversation as conv

    async def t():
        w = conv.AuthErrorWatcher()
        task = MagicMock()
        task.cancel = AsyncMock()
        w.set_task(task)
        w.push_frame = AsyncMock()
        with patch.object(conv.FrameProcessor, "process_frame", new=AsyncMock()):
            await w.process_frame(ErrorFrame(error=error_message), FrameDirection.DOWNSTREAM)
        return w.terminal_error, task.cancel.await_count

    return asyncio.run(t())


def test_auth_watcher_cancels_on_terminal_error():
    terminal_error, cancels = _drive_watcher("boom invalid_api_key boom")
    assert terminal_error is not None
    assert cancels == 1


def test_auth_watcher_ignores_transient_error():
    terminal_error, cancels = _drive_watcher("transient 503 unavailable")
    assert terminal_error is None
    assert cancels == 0
