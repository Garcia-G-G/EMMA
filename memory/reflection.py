"""Post-turn reflection: a language model proposes durable facts from the talk.

**Off by default** (Step 42, owner decision): it sends conversation text off the
Mac, so it runs only when the owner sets ``MEMORY_CONSOLIDATION_OPT_IN`` AND a
caller outside ``memory/`` has wired a model with :func:`configure` (today only
the Realtime path does). ``memory/`` imports no model client.

Before anything is sent, secret-shaped lines are dropped whole (redaction
patterns and a closed list of credential words), and the exact payload is
appended to ``~/.emma/memory_consolidation.jsonl`` so what left is visible. A
proposed fact that still looks secret goes to the Keychain, never to memory.db;
that check is rules only (no second model call).

Runs in the background, never adds to turn latency; any failure drops silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import structlog

from config.settings import settings
from core import redaction, secrets
from memory import long_term, short_term

log = structlog.get_logger("emma.memory.reflection")

# (system prompt, user text) -> the model's reply (JSON text). Wired from outside memory/.
Complete = Callable[[str, str], Awaitable[str]]
_complete: Complete | None = None

# Lines carrying any of these are never sent, even after redaction (rules, no model).
_SECRET_WORDS = re.compile(
    r"contrase[nñ]a|password|passcode|\bpin\b|\bclave\b|token|api ?key|cvv|tarjeta|"
    r"credit card|card number|iban|cuenta bancaria|bank account|n[uú]mero de cuenta|"
    r"account number|seed phrase|private key|clave privada",
    re.IGNORECASE,
)


def configure(complete: Complete | None) -> None:
    """Wire (or unwire) the model reflection may call. Only callers outside memory/."""
    global _complete
    _complete = complete


def enabled() -> bool:
    return bool(settings.MEMORY_CONSOLIDATION_OPT_IN) and _complete is not None


def _sendable(text: str) -> str:
    """Drop every line that looks secret; redact what's left."""
    keep = [ln for ln in text.splitlines()
            if not _SECRET_WORDS.search(ln) and redaction.redact(ln) == ln]
    return "\n".join(keep)


def _record_sent(payload: str) -> None:
    """Append exactly what is about to leave the Mac (owner-visible, 0600)."""
    path = settings.EMMA_HOME / "memory_consolidation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "sent": payload}, ensure_ascii=False) + "\n")


_SYSTEM_PROMPT = """You read short windows of a conversation between the user and his \
voice assistant Emma. Your job is to extract NEW long-term facts about the user \
that Emma should remember across sessions.

Output strict JSON with this shape:
{"facts": [{"content": "...", "kind": "preference|name|habit|fact|language|tool_use|general", "confidence": 0.0-1.0}]}

Rules:
- Only include facts that are stable about the user himself - preferences, names, \
allergies, habitual choices, language preferences, how he likes Emma to behave.
- DO NOT include ephemeral / one-shot info ("he asked about the weather today", \
"he played Bad Bunny just now"). Skip those.
- DO NOT include facts about external entities (creators, brands, places) unless \
they describe the user's preference for them.
- If nothing durable was learned in this window, return {"facts": []}.
- Keep each fact to one short sentence. No translation - keep the language the user \
used or natural English, your choice.
- Confidence: 0.9 if the user stated it directly; 0.7 if you inferred it from \
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
    if not window or not enabled() or _complete is None:
        return []
    body = _sendable(_format_window(window))
    if not body.strip():
        return []
    try:
        _record_sent(body)
        text = (await asyncio.wait_for(_complete(_SYSTEM_PROMPT, body),
                                       timeout=settings.API_TIMEOUT_S)).strip()
    except Exception as exc:
        log.warning("reflection_llm_failed", error=type(exc).__name__)
        return []

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


def _classify_sensitivity(fact: str) -> Literal["personal", "secret"]:
    """Secret if a redaction pattern fires or a credential word appears. Rules only."""
    if redaction.redact(fact) != fact or _SECRET_WORDS.search(fact):
        return "secret"
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
            tier = _classify_sensitivity(value)
            # Re-check AFTER the long classify await, right before any write:
            # `forget_last_turn` may have fired the blackout while we were awaiting.
            # Without this, a fact from the just-purged turn is written back AFTER
            # the delete (TOCTOU) and the purge silently leaks.
            if _is_suppressed():
                log.info("reflection_suppressed_mid_loop")
                return
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
    if not window or not enabled():
        return None  # off unless the owner opted in and a model is wired
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(reflect_async(list(window)))
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task
