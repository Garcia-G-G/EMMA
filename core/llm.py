"""Compat shim post Realtime-API migration (Prompt 13).

The pre-migration ``core/llm.py`` ran the OpenAI Chat Completions tool
loop directly. Phase 13 collapsed STT + LLM + TTS into a single
``core.realtime`` WebSocket session, so the streaming loop and the
elaborate language-policy system prompt are gone.

What remains: a small set of typed records that other code (the
acceptance runner, the orchestrator's bookkeeping) imports. The
function-call streaming used to live here; it now lives in
:mod:`core.realtime`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["user", "assistant", "system", "tool"]
SpokenLang = Literal["es", "en"]


@dataclass
class Message:
    """Single turn in the conversation history."""

    role: Role
    content: str


@dataclass
class PendingConfirmation:
    """Legacy two-phase-confirmation record.

    Held over only because the acceptance runner and any out-of-tree
    callers reference the type. With Realtime, destructive-action
    confirmation is conversational (the model asks via voice and waits
    for the user's natural yes/no reply on the open WebSocket); the
    orchestrator no longer constructs these.
    """

    tool_name: str
    args: dict[str, Any]
    question: str
