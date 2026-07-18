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
import time
import uuid
from typing import Any, Literal

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core import redaction, secrets
from memory import long_term, short_term

log = structlog.get_logger("emma.memory.reflection")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key(), base_url=settings.openai_base_url())
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
    body = redaction.redact(body)  # egress guard: strip secrets/PII before the transcript leaves
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


_SENSITIVITY_PROMPT = """Classify the following fact as either "personal" or "secret".
A fact is "secret" if it contains a password, an account number, a government
ID, a credit card, a private cryptographic key, or any other value whose
disclosure would harm the person.
A fact is "personal" if it describes preferences, habits, names, locations,
non-sensitive relationships.
Output a single word: personal or secret."""


async def _classify_sensitivity(fact: str) -> Literal["personal", "secret"]:
    """Route a candidate fact to the Personal or Secret tier.

    Deterministic first: if a redaction pattern fires (card/CURP/RFC/SSN/
    IBAN/phone/API-key), it's secret. Otherwise ask gpt-4o-mini for the
    unstructured cases ("my password is ..."). Defaults to personal on LLM
    failure — the redaction net already catches pattern-based secrets.
    """
    if redaction.redact(fact) != fact:
        return "secret"
    try:
        completion = await asyncio.wait_for(
            _get_client().chat.completions.create(
                model=settings.MEMORY_REFLECTION_MODEL,
                messages=[
                    {"role": "system", "content": _SENSITIVITY_PROMPT},
                    {"role": "user", "content": fact},
                ],
                temperature=0,
            ),
            timeout=settings.API_TIMEOUT_S,
        )
        answer = (completion.choices[0].message.content or "").strip().lower()
        return "secret" if "secret" in answer else "personal"
    except Exception as exc:
        log.warning("sensitivity_classify_failed", error=str(exc))
        return "personal"


_suppress_until: float = 0.0


def suppress(seconds: float) -> None:
    """Blackout auto-reflection for ``seconds`` (wall clock).

    Used by "borra lo que acabo de decir": the reflection extracted from the
    just-purged turn is fire-and-forget and may commit AFTER the delete, which
    would rewrite the facts we just removed. A short forward blackout swallows it.
    """
    global _suppress_until
    _suppress_until = max(_suppress_until, time.time() + max(0.0, float(seconds)))


def _is_suppressed() -> bool:
    return time.time() < _suppress_until


async def reflect_async(window: list[short_term.Turn]) -> None:
    """Fire-and-forget entrypoint: extract facts, classify, route, persist.

    Personal facts go to long-term memory; secret-classified facts have their
    value stored in Keychain and only a placeholder + vault_ref written to the
    DB. Fact content is never logged (it may be a secret).
    """
    if _is_suppressed():
        log.info("reflection_suppressed")
        return
    facts = await reflect_once(window)
    if not facts:
        return
    # Re-check after the LLM round-trip: "borra lo que acabo de decir" may have
    # fired the blackout while reflect_once was in flight.
    if _is_suppressed():
        log.info("reflection_suppressed_post_extract")
        return
    personal = 0
    secret = 0
    for f in facts:
        value = f["content"]
        try:
            tier = await _classify_sensitivity(value)
            if tier == "secret":
                label = f"fact_{uuid.uuid4().hex[:8]}"
                await secrets.store(label, value, kind="secret_fact")
                await long_term.remember(
                    "secret",
                    kind=f["kind"],
                    confidence=f["confidence"],
                    source="reflection",
                    vault_ref=label,
                )
                secret += 1
            else:
                await long_term.remember(
                    value, kind=f["kind"], confidence=f["confidence"], source="reflection"
                )
                personal += 1
        except Exception as exc:
            log.warning("reflection_write_failed", error=str(exc), content="<hidden>")
    if personal or secret:
        log.info("reflection_committed", personal=personal, secret=secret)


# Strong refs to in-flight reflection tasks. asyncio only holds a WEAK ref to a
# bare create_task() result, so without this the GC can collect the task mid-write
# and silently drop the fact extraction (the "facts captured automatically" promise).
_INFLIGHT: set[asyncio.Task[None]] = set()


def schedule_reflection(window: list[short_term.Turn]) -> asyncio.Task[None] | None:
    """Start reflection in the background. Returns the task (or None on no-op)."""
    if not window:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(reflect_async(list(window)))
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task
