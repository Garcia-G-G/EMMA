"""Egress guard: secrets/PII must be redacted BEFORE anything is sent to OpenAI
(screen text, OCR, transcripts, web) — redaction guards egress, not just logs."""

from __future__ import annotations

import pytest

import tools.screen_vision_tool as svt

_CARD = "4111 1111 1111 1111"  # Luhn-valid test card


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
