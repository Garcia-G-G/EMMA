"""Post-turn reflection: pull durable facts out of the conversation.

After every turn the orchestrator schedules :func:`reflect_async` as a
fire-and-forget task. It feeds gpt-4o-mini a short window of the
session (the just-completed user/assistant pair plus a few prior
turns) and asks for a small list of new long-term facts about Garcia.
Each fact is then upserted into the long-term store via
:func:`memory.long_term.remember`.

The reflection runs in the background so it never adds to per-turn
latency. If the LLM call fails or returns garbage, we drop the result
silently - the turn still completed for the user.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from memory import long_term, short_term

log = structlog.get_logger("emma.memory.reflection")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


_SYSTEM_PROMPT = """You read short windows of a conversation between Garcia and his \
voice assistant Emma. Your job is to extract NEW long-term facts about Garcia \
that Emma should remember across sessions.

Output strict JSON with this shape:
{"facts": [{"content": "...", "kind": "preference|name|habit|fact|language|tool_use|general", "confidence": 0.0-1.0}]}

Rules:
- Only include facts that are stable about Garcia himself - preferences, names, \
allergies, habitual choices, language preferences, how he likes Emma to behave.
- DO NOT include ephemeral / one-shot info ("he asked about the weather today", \
"he played Bad Bunny just now"). Skip those.
- DO NOT include facts about external entities (creators, brands, places) unless \
they describe Garcia's preference for them.
- If nothing durable was learned in this window, return {"facts": []}.
- Keep each fact to one short sentence. No translation - keep the language Garcia \
used or natural English, your choice.
- Confidence: 0.9 if Garcia stated it directly; 0.7 if you inferred it from \
context; below 0.6 you should usually skip the fact entirely.
- Max 5 facts per call.
"""


def _format_window(window: list[short_term.Turn]) -> str:
    lines: list[str] = []
    for t in window:
        if t.user_text:
            lines.append(f"USER: {t.user_text}")
        if t.assistant_text:
            lines.append(f"EMMA: {t.assistant_text}")
    return "\n".join(lines)


async def reflect_once(window: list[short_term.Turn]) -> list[dict[str, Any]]:
    """Run one reflection pass over `window`. Returns the list of fact dicts.

    Returns an empty list on any failure. Never raises.
    """
    if not window:
        return []
    body = _format_window(window)
    if not body.strip():
        return []
    try:
        completion = await asyncio.wait_for(
            _get_client().chat.completions.create(
                model=settings.MEMORY_REFLECTION_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": body},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            ),
            timeout=settings.API_TIMEOUT_S,
        )
    except Exception as exc:
        log.warning("reflection_llm_failed", error=str(exc))
        return []

    text = (completion.choices[0].message.content or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("reflection_parse_failed", error=str(exc), text=text[:200])
        return []

    facts = payload.get("facts")
    if not isinstance(facts, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for f in facts[: settings.MEMORY_REFLECTION_MAX_FACTS_PER_TURN]:
        if not isinstance(f, dict):
            continue
        content = str(f.get("content", "")).strip()
        if not content:
            continue
        kind = str(f.get("kind", "general")).strip() or "general"
        try:
            confidence = float(f.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))
        cleaned.append({"content": content, "kind": kind, "confidence": confidence})
    return cleaned


async def reflect_async(window: list[short_term.Turn]) -> None:
    """Fire-and-forget entrypoint: extract facts and write them to long-term."""
    facts = await reflect_once(window)
    if not facts:
        return
    written = 0
    for f in facts:
        try:
            await long_term.remember(
                f["content"],
                kind=f["kind"],
                confidence=f["confidence"],
                source="reflection",
            )
            written += 1
        except Exception as exc:
            log.warning("reflection_write_failed", error=str(exc), content=f.get("content"))
    if written:
        log.info("reflection_committed", count=written)


def schedule_reflection(window: list[short_term.Turn]) -> asyncio.Task[None] | None:
    """Start reflection in the background. Returns the task (or None on no-op)."""
    if not window:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(reflect_async(list(window)))
