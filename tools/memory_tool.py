"""Voice-callable memory tools.

These expose the long-term store directly to the LLM so Garcia can
say things like "Emma, recuérdame que soy alérgico a los mariscos"
and have the fact persisted explicitly with high confidence, or ask
"¿qué sabes de mí?" to hear Emma recite what she has on file.

Implicit memory (the reflection step) writes to the same store
behind the scenes; explicit writes via these tools simply bump
confidence to 1.0 and tag source="explicit".
"""

from __future__ import annotations

import structlog

from memory import long_term
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.memory")


@tool()
async def remember_fact(content: str, kind: str = "general") -> ToolResult:
    """Save a durable fact about Garcia to long-term memory.

    Use when Garcia says any of:

    - "Emma, recuérdame que ..."
    - "Emma, acuérdate de que ..."
    - "remember that I ..."
    - "save this: I ..."

    ``kind`` is a soft taxonomy hint: ``name``, ``preference``,
    ``habit``, ``fact``, ``language``, ``tool_use``, or ``general``.
    The fact is stored with confidence 1.0 (explicit user statement).
    """
    text = (content or "").strip()
    if not text:
        return ToolResult(False, None, "No me llegó contenido para recordar.", False)
    try:
        fact_id = await long_term.remember(
            text, kind=kind or "general", confidence=1.0, source="explicit"
        )
    except Exception as exc:
        log.error("remember_failed", error=str(exc), content=text[:80])
        return ToolResult(False, None, f"No pude guardar eso: {exc}", False)
    return ToolResult(
        True,
        {"id": fact_id, "content": text, "kind": kind},
        f"Anotado: {text}.",
        False,
    )


@tool()
async def recall_facts(query: str = "") -> ToolResult:
    """Look up what Emma already knows about Garcia.

    Use when Garcia says any of:

    - "¿qué sabes de mí?"
    - "what do you know about me?"
    - "¿recuerdas algo sobre X?"  (X goes in `query`)

    Pass ``query`` to filter on a substring; leave empty for the
    highest-confidence facts overall. Returns up to 15 facts.
    """
    q = (query or "").strip() or None
    try:
        facts = await long_term.recall(query=q, limit=15)
    except Exception as exc:
        log.error("recall_failed", error=str(exc))
        return ToolResult(False, None, f"No pude consultar la memoria: {exc}", False)
    if not facts:
        if q:
            msg = f"No tengo nada anotado sobre '{q}'."
        else:
            msg = "Todavía no tengo nada anotado sobre ti."
        return ToolResult(True, {"facts": []}, msg, False)
    lines = "; ".join(f.content for f in facts[:10])
    summary_n = min(len(facts), 10)
    msg = f"Esto es lo que tengo anotado ({summary_n}): {lines}."
    return ToolResult(
        True,
        {
            "facts": [
                {"content": f.content, "kind": f.kind, "confidence": f.confidence} for f in facts
            ]
        },
        msg,
        False,
    )


@tool()
async def forget_fact(content: str) -> ToolResult:
    """Delete a fact (or facts) matching the given content from long-term memory.

    Use when Garcia says any of:

    - "Emma, olvida que ..."
    - "Emma, borra lo de ..."
    - "forget that I ..."
    """
    text = (content or "").strip()
    if not text:
        return ToolResult(False, None, "Dime qué quieres que olvide.", False)
    try:
        removed = await long_term.forget(text)
    except Exception as exc:
        log.error("forget_failed", error=str(exc), content=text[:80])
        return ToolResult(False, None, f"No pude borrar eso: {exc}", False)
    if removed == 0:
        return ToolResult(True, {"removed": 0}, f"No tenía nada anotado sobre '{text}'.", False)
    return ToolResult(
        True,
        {"removed": removed},
        f"Listo, olvidé {removed} entrada{'s' if removed != 1 else ''}.",
        False,
    )


# "lo que acabo de decir" refers to the PREVIOUS utterance, not this command, so a
# fixed recent window is the right anchor (facts from that turn were written
# seconds ago by reflection). Capped so it never nukes older memory.
_LAST_TURN_WINDOW_S = 120.0
_REFLECTION_BLACKOUT_S = 45.0


@tool()
async def forget_last_turn() -> ToolResult:
    """Borra lo que Emma acaba de aprender de ti en este ratito.

    Para "borra lo que acabo de decir", "olvida lo que te acabo de decir",
    "borra lo último que aprendiste de mí", "eso no lo guardes". Quita los datos
    recién anotados de la conversación reciente (no toca lo de días anteriores).
    """
    from memory import reflection

    # Blackout auto-reflection FIRST, so the in-flight extraction from the turn
    # being purged can't rewrite the facts right after we delete them.
    reflection.suppress(_REFLECTION_BLACKOUT_S)
    try:
        removed = await long_term.forget_recent(_LAST_TURN_WINDOW_S)
    except Exception as exc:
        log.error("forget_last_turn_failed", error=str(exc))
        return ToolResult(False, None, f"No pude borrar eso: {exc}", False)
    if removed == 0:
        return ToolResult(
            True, {"removed": 0}, "No había apuntado nada nuevo de este ratito; nada que borrar.", False
        )
    return ToolResult(
        True,
        {"removed": removed},
        f"Listo, borré lo que acababa de aprender de ti ({removed} "
        f"cosa{'s' if removed != 1 else ''}).",
        False,
    )
