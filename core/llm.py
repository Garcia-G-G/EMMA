"""GPT-4o conversational client with streaming + function calling.

``converse()`` is the high-level entry. It streams text for TTS and
internally runs the OpenAI tool loop, dispatching to the tool registry.
A destructive tool that needs confirmation appends a
:class:`PendingConfirmation` to the caller-supplied list and stops; the
orchestrator captures the user's yes/no and re-dispatches.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core.stt import Transcript
from tools.registry import dispatch, openai_tool_specs, result_to_tool_message

log = structlog.get_logger("emma.llm")

Role = Literal["user", "assistant", "system", "tool"]
SpokenLang = Literal["es", "en"]

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class PendingConfirmation:
    tool_name: str
    args: dict[str, Any]
    question: str


@dataclass
class _ToolCallAcc:
    id: str = ""
    name: str = ""
    args: str = ""


def _now() -> dt.datetime:
    if settings.TIMEZONE:
        return dt.datetime.now(zoneinfo.ZoneInfo(settings.TIMEZONE))
    return dt.datetime.now().astimezone()


def _language_block(transcript: Transcript, lang_name: str) -> str:
    if not settings.STRICT_LANGUAGE_POLICY:
        return (
            f"Detected language for this turn: {transcript.language}. "
            f"Respond in {lang_name}."
        )
    if transcript.language == "other":
        return (
            "LANGUAGE GUIDANCE:\n"
            "- The user's utterance had mixed or unclear language. Choose the\n"
            "  response language based on what the user most likely wants given\n"
            "  the content. Default to the user's prior language if ambiguous.\n"
            "- If the user explicitly requests a specific language for the\n"
            "  response, use that language.\n"
            "- Avoid mixing languages within one response unless the user asked\n"
            "  for translation."
        )
    return (
        "LANGUAGE POLICY (mandatory):\n"
        f"- The user's current utterance was detected as: {transcript.language}.\n"
        f"- Respond entirely in {lang_name}. Do not insert words, phrases, or sentences in any\n"
        "  other language inside your response.\n"
        "- This rule has exactly four exceptions; outside of them, code-switching is forbidden:\n"
        "  1. The user explicitly asked you to translate something\n"
        "     (e.g., \"traduce esto al inglés\", \"how do you say X in Spanish\").\n"
        "  2. The user asked for a vocabulary, definition, or comparison across languages\n"
        "     (e.g., \"qué significa 'deadline' en español\", \"what's the English for 'sobremesa'\").\n"
        "  3. You are repeating back a proper noun (person name, brand, place, song title)\n"
        "     that was originally in the other language. Quote it verbatim; do not translate it.\n"
        "  4. The user explicitly asked you to switch the response language\n"
        "     (e.g., \"háblame en español\", \"respond in English\", \"switch to French\").\n"
        "     Use the requested language for this turn and subsequent turns until they switch back.\n"
        "- When in doubt, do not switch languages."
    )


def build_system_prompt(
    transcript: Transcript,
    spoken_language: SpokenLang,
    memory_priming: str = "",
) -> str:
    lang_name = "Spanish" if spoken_language == "es" else "English"
    now = _now()
    return (
        "You are Emma, a warm and concise bilingual voice assistant for Garcia.\n"
        f"Current local time: {now.strftime('%A %Y-%m-%d %H:%M %Z')}.\n"
        f"{_language_block(transcript, lang_name)}\n"
        "Keep replies short and conversational - this will be spoken aloud, not read. "
        "Prefer tools over guessing when a tool can do it. After a tool runs, briefly "
        "confirm what happened.\n"
        f"{memory_priming}"
    ).strip()


def fallback_text(spoken_language: SpokenLang) -> str:
    if spoken_language == "es":
        return "Tuve un problema conectándome, intenta de nuevo."
    return "I hit a connection issue, try again."


def _build_messages(
    transcript: Transcript,
    history: list[Message],
    spoken_lang: SpokenLang,
    memory_priming: str = "",
) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_system_prompt(transcript, spoken_lang, memory_priming),
        }
    ]
    for m in history[-20:]:
        msgs.append({"role": m.role, "content": m.content})
    msgs.append({"role": "user", "content": transcript.text})
    return msgs


async def _stream_completion(
    messages: list[dict[str, Any]],
    tools_spec: list[dict[str, Any]] | None,
) -> AsyncIterator[tuple[str, Any]]:
    """Stream one chat completion. Yields ("text", str) and ("tc_delta", obj)."""
    try:
        stream = await asyncio.wait_for(
            _get_client().chat.completions.create(
                model="gpt-4o",
                messages=messages,  # type: ignore[arg-type]
                stream=True,
                temperature=0.7,
                tools=tools_spec or None,
                tool_choice="auto" if tools_spec else None,
            ),
            timeout=settings.API_TIMEOUT_S,
        )
    except Exception as exc:
        log.error("llm_open_failed", error=str(exc))
        yield ("error", exc)
        return
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield ("text", delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    yield ("tc_delta", tc)
    except Exception as exc:
        log.error("llm_stream_failed", error=str(exc))
        yield ("error", exc)


def _accumulate_tc(accs: dict[int, _ToolCallAcc], tc_delta: Any) -> None:
    idx = tc_delta.index
    acc = accs.setdefault(idx, _ToolCallAcc())
    if getattr(tc_delta, "id", None):
        acc.id = tc_delta.id
    fn = getattr(tc_delta, "function", None)
    if fn is not None:
        if getattr(fn, "name", None):
            acc.name = fn.name
        if getattr(fn, "arguments", None):
            acc.args += fn.arguments


def _build_assistant_msg(text: str, accs: dict[int, _ToolCallAcc]) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if accs:
        msg["tool_calls"] = [
            {
                "id": accs[i].id,
                "type": "function",
                "function": {"name": accs[i].name, "arguments": accs[i].args or "{}"},
            }
            for i in sorted(accs)
        ]
    return msg


async def converse(
    transcript: Transcript,
    history: list[Message],
    spoken_lang: SpokenLang,
    pending: list[PendingConfirmation] | None = None,
    memory_priming: str = "",
) -> AsyncIterator[str]:
    """Drive the LLM tool loop. Yields text for TTS.

    ``pending`` is a mutable list the caller checks after iteration: if
    non-empty, a destructive tool needs a yes/no follow-up before it
    actually fires. ``memory_priming`` is a pre-formatted block of
    known facts about the user that gets folded into the system prompt
    (built by the orchestrator from :mod:`memory.long_term`).
    """
    if pending is None:
        pending = []
    messages = _build_messages(transcript, history, spoken_lang, memory_priming)
    tools_spec = openai_tool_specs()

    for step in range(settings.MAX_TOOL_STEPS + 1):
        text_pieces: list[str] = []
        accs: dict[int, _ToolCallAcc] = {}
        errored = False
        async for kind, payload in _stream_completion(messages, tools_spec):
            if kind == "text":
                text_pieces.append(payload)
                yield payload
            elif kind == "tc_delta":
                _accumulate_tc(accs, payload)
            elif kind == "error":
                errored = True
                break

        if errored:
            if not text_pieces:
                yield fallback_text(spoken_lang)
            return

        if not accs:
            return  # plain text reply, no tools - we're done

        if step == settings.MAX_TOOL_STEPS:
            yield (
                " " + (
                    "Llegué al límite de pasos para esta tarea."
                    if spoken_lang == "es"
                    else "I hit the step limit for this task."
                )
            )
            return

        messages.append(_build_assistant_msg("".join(text_pieces), accs))

        # Execute each tool call in order. Stop the turn if any needs confirmation.
        for idx in sorted(accs):
            acc = accs[idx]
            try:
                args: dict[str, Any] = json.loads(acc.args) if acc.args else {}
            except json.JSONDecodeError:
                args = {}

            result = await dispatch(acc.name, args)

            if result.requires_confirmation:
                pending.append(
                    PendingConfirmation(tool_name=acc.name, args=args, question=result.user_message)
                )
                # Tell the LLM we're waiting on the user. Also speak the question.
                yield result.user_message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": acc.id,
                        "content": json.dumps(
                            {"status": "awaiting_user_confirmation", "question": result.user_message},
                            ensure_ascii=False,
                        ),
                    }
                )
                return

            messages.append(result_to_tool_message(acc.id, result))


# Backwards-compatible thin wrapper for any phase-1 code paths still calling it.
async def respond(
    transcript: Transcript,
    history: list[Message],
    spoken_lang: SpokenLang,
) -> AsyncIterator[str]:
    pending: list[PendingConfirmation] = []
    async for piece in converse(transcript, history, spoken_lang, pending):
        yield piece
