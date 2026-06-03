"""Tests for Prompt 15.10: destructive actions end-to-end.

Covers: semantic forget (paraphrase deletes the right fact, conservatively),
confirmation flow on a destructive tool, AppleScript dialog-blocked handling
(no hang), and tool-call telemetry.

The memory test uses a temp DB and a mocked embedder so it neither touches
Garcia's real memory.db nor calls the embedding API.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from memory import embeddings
from memory import long_term as lt

_DIMS = embeddings.EMBED_DIMS


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# --- semantic forget -------------------------------------------------------


def _vec_a() -> list[float]:
    return [1.0] + [0.0] * (_DIMS - 1)


def _vec_b() -> list[float]:
    return [0.0, 1.0] + [0.0] * (_DIMS - 2)


async def _fake_embed(text: str) -> list[float]:
    t = text.lower()
    return _vec_a() if ("coffee" in t or "café" in t or "cafe" in t) else _vec_b()


def test_semantic_forget_deletes_paraphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(lt.settings, "MEMORY_DB_PATH", tmp_path / "mem.db")

    async def run():
        lt.initialize()
        await lt.remember("Tomo mucho café por las mañanas", kind="preference")
        await lt.remember("El clima en Monterrey es caluroso", kind="general")
        # paraphrase that maps (via mock) to the coffee vector
        removed = await lt.forget("preferencia de cafe")
        remaining = await lt.recall(limit=50)
        return removed, [f.content for f in remaining]

    with patch.object(lt.embeddings, "embed", new=_fake_embed):
        removed, remaining = asyncio.run(run())

    assert removed == 1  # exactly one fact, not a mass delete
    assert not any("café" in c for c in remaining)  # coffee fact gone
    assert any("clima" in c for c in remaining)  # unrelated fact preserved


def test_semantic_forget_no_match_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(lt.settings, "MEMORY_DB_PATH", tmp_path / "mem.db")

    async def run():
        lt.initialize()
        await lt.remember("Tomo mucho café", kind="preference")  # vec A
        # query maps to vec B (orthogonal) -> cosine 0 < floor -> nothing deleted
        return await lt.forget("algo totalmente distinto")

    with patch.object(lt.embeddings, "embed", new=_fake_embed):
        removed = asyncio.run(run())
    assert removed == 0


# --- confirmation flow -----------------------------------------------------


def test_delete_note_requires_confirmation_first():
    # 19.2-B2: enumerate matches first (read-only), then ask yes/no on the single
    # match before any delete.
    from tools import notes_tool

    one = AsyncMock(return_value="id1‖2026-06-02T10:00:00‖Cualquiera‖x")

    async def run():
        with patch.object(notes_tool.macos, "osascript", new=one):
            return await notes_tool.delete_note(title="Cualquiera")

    r = asyncio.run(run())
    assert r.success is True
    assert r.requires_confirmation is True
    assert "Borro la nota" in r.user_message


def test_complete_reminder_requires_confirmation_first():
    from tools import reminders_tool

    one = AsyncMock(return_value="r1‖‖Pagar luz‖")

    async def run():
        with patch.object(reminders_tool.macos, "osascript", new=one):
            return await reminders_tool.complete_reminder(title="Pagar luz")

    r = asyncio.run(run())
    assert r.requires_confirmation is True
    assert "completado" in r.user_message


# --- AppleScript dialog-blocked handling (no hang) -------------------------


def _osascript_then_raise(error_msg):
    """First call (enumerate) succeeds with one match; the next call (the actual
    delete) raises — exercising the dialog/error path on the destructive step."""
    from actions import macos

    seq = ["id1‖2026-06-02T10:00:00‖X‖p"]

    async def osa(script, timeout_s=15.0):
        if seq:
            return seq.pop(0)
        raise macos.AppleScriptError(error_msg)

    return osa


def test_applescript_dialog_blocked_returns_recovery_message():
    from tools import notes_tool

    osa = _osascript_then_raise("app_dialog_blocked: osascript timed out after 15.0s")

    async def run():
        with patch.object(notes_tool.macos, "osascript", new=osa):
            return await notes_tool.delete_note(title="X", confirmed=True)

    r = asyncio.run(run())
    assert r.success is False
    assert "pantalla" in r.user_message  # "autorízalo en pantalla"


def test_applescript_generic_error_distinct_from_dialog():
    from tools import notes_tool

    osa = _osascript_then_raise("some other failure")

    async def run():
        with patch.object(notes_tool.macos, "osascript", new=osa):
            return await notes_tool.delete_note(title="X", confirmed=True)

    r = asyncio.run(run())
    assert r.success is False
    assert "pantalla" not in r.user_message
    assert "No pude borrar" in r.user_message


# --- telemetry -------------------------------------------------------------


def test_handler_emits_started_and_completed():
    import core.conversation as conv
    from tools.base import ToolResult

    events: list[str] = []

    def cap(logger, method, event_dict):
        ev = event_dict.get("event")
        if ev:
            events.append(ev)
        return ev or ""  # terminal processor: return a string for the stdlib logger

    class _P:
        function_name = "now_playing"

        def __init__(self):
            self.arguments: dict = {}

        async def result_callback(self, payload):
            return None

    async def fake_dispatch(name, args):
        return ToolResult(True, None, "ok", False)

    ctl = conv.SessionControl()
    handler = conv._make_function_handler(ctl)
    try:
        with patch("core.conversation.dispatch", new=fake_dispatch):
            structlog.configure(processors=[cap], cache_logger_on_first_use=False)
            asyncio.run(handler(_P()))
    finally:
        structlog.reset_defaults()

    assert "tool_started" in events
    assert "tool_completed" in events
