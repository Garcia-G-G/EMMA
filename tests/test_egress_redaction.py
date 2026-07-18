"""Egress guard: secrets/PII must be redacted BEFORE anything is sent to OpenAI
(screen text, OCR, transcripts, web) — redaction guards egress, not just logs."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import core.conversation as conv
import tools.screen_vision_tool as svt
from tools.base import ToolResult

_CARD = "4111 1111 1111 1111"  # Luhn-valid test card


def test_tool_result_data_and_message_redacted_at_seam() -> None:
    """CRITICAL audit finding 1: a tool's data/user_message reach the Realtime
    model verbatim — they MUST be redacted at the result_callback seam, so a
    secret OCR'd/AX-read from the screen never egresses even if the tool forgot."""
    captured = {}

    class _P:
        function_name = "look_at_screen"

        def __init__(self):
            self.arguments: dict = {}

        async def result_callback(self, payload):
            captured.update(payload)

    async def fake_dispatch(name, args):
        # a tool that surfaces a visible secret in BOTH channels
        return ToolResult(
            True,
            {"text": f"la tarjeta en pantalla es {_CARD}", "scope": "window"},
            f"Veo la tarjeta {_CARD}.",
            False,
        )

    handler = conv._make_function_handler(conv.SessionControl())
    with patch("core.conversation.dispatch", new=fake_dispatch):
        asyncio.run(handler(_P()))

    blob = str(captured)
    assert _CARD not in blob  # neither data nor user_message leaked the card
    assert "REDACTED" in captured["user_message"]
    # 24.6-B3: look_at_screen is untrusted → redaction runs at the seam, THEN the
    # (already-redacted) content is folded into the <untrusted_content> fence, so
    # data is nulled and everything — redacted secret + non-secret scope — lives
    # inside the fence. Redaction still guards egress; fencing marks it as data.
    assert '<untrusted_content source="look_at_screen">' in captured["user_message"]
    assert captured["data"] is None  # nothing untrusted left outside the fence
    assert "REDACTED" in captured["user_message"]  # secret stripped inside the fence
    assert "window" in captured["user_message"]  # non-secret data preserved (fenced)


class _FakeCompletion:
    def __init__(self, text: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


@pytest.mark.asyncio
async def test_summarize_redacts_screen_text_before_send(monkeypatch) -> None:
    sent = {}

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": self})()

        async def create(self, *, model, messages, **kw):
            sent["messages"] = messages
            return _FakeCompletion("ok")

    monkeypatch.setattr(svt, "AsyncOpenAI", _FakeClient)
    # screen content carrying a secret card number
    await svt._summarize(f"App: Terminal\nText: la tarjeta {_CARD}", "¿qué ves?")
    blob = str(sent["messages"])
    assert _CARD not in blob              # the secret never crossed the wire
    assert "REDACTED" in blob             # it was stripped, not just dropped
