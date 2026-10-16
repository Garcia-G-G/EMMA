"""Voice-callable memory tools, all on the Mac (Step 42 Part 4).

"recuerda que mi perro se llama Toby" → a structured slot (memory.structured);
anything the grammar doesn't read → a free-text note (memory.long_term, local
vectors). Nothing is written without a spoken yes: the unconfirmed call only asks
— naming the old value when it would change one — and the call with
``confirmed=True`` (the guard checks the yes) writes. Secret-shaped content is
refused here (secrets go to the Keychain, never memory.db). "¿cómo se llama mi
perro?" is answered from the slot or with "no lo tengo", never with a neighbour.
"""

from __future__ import annotations

import asyncio
import re

import structlog

from core import runtime
from memory import long_term, structured
from memory.reflection import _classify_sensitivity
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.memory")
_SHORT = 3  # things said before "¿quieres que te diga más?"
# "¿qué sabes de mí?" / "what do you know about me": the summary, not one answer.
_SUMMARY = re.compile(
    r"^(?:(?:que|what)\b.*\b(?:sabes|sabe|recuerdas|tienes guardado|tienes anotado|datos tienes|"
    r"has aprendido|know|remember|have you saved|have you learned|facts do you have)\b.*"
    r"\b(?:de mi|sobre mi|about me|on me)|que te he contado|what have i told you)$")


def _en() -> bool:
    return runtime.get_spoken_lang() == "en"


def _t(es: str, en: str) -> str:
    return en if _en() else es


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _say(subject: str, attribute: str, value: str) -> str:
    return structured.say(subject, attribute, value, "en" if _en() else "es")


def _join(items: list[str]) -> str:
    last = " and " if _en() else " y "
    return items[0] if len(items) == 1 else "; ".join(items[:-1]) + last + items[-1]


def _propose(slot: structured.Slot) -> structured.Proposal:
    with long_term._connect() as conn:
        return structured.propose(conn, slot)


def _commit(p: structured.Proposal) -> int:
    with long_term._connect() as conn:
        return structured.commit(conn, p)


def _lookup(subject: str, attribute: str) -> list[str]:
    with long_term._connect() as conn:
        return structured.lookup(conn, subject, attribute)


def _slots() -> list[tuple[int, str, str, str]]:
    with long_term._connect() as conn:
        return structured.all_active(conn)


def _active(subject: str, attribute: str) -> list[tuple[int, str]]:
    with long_term._connect() as conn:
        return structured.active(conn, subject, attribute)


def _forget_slot(row_id: int) -> bool:
    with long_term._connect() as conn:
        return structured.forget(conn, row_id)


@tool()
async def remember_fact(content: str, kind: str = "general", confirmed: bool = False) -> ToolResult:
    """Save a durable fact about the user to long-term memory.

    Use when the user says any of:

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
        return ToolResult(False, None, _t("No me llegó qué recordar.", "I didn't get what to remember."), False)
    if _classify_sensitivity(text) == "secret":
        return ToolResult(False, None, _t("Eso parece un dato secreto: no lo guardo en la memoria.",
                                          "That looks secret: I don't keep it in memory."), False)
    slot = structured.parse_statement(text)
    if slot is not None:
        return await _remember_slot(slot, confirmed)
    if not confirmed:  # the question IS the first call; nothing is written yet
        return ToolResult(False, {"note": text}, _t(f"¿Guardo esto: «{text}»?", f"Save this: «{text}»?"), True)
    try:
        fact_id = await long_term.remember(text, kind=kind or "general", confidence=1.0, source="explicit")
    except Exception as exc:
        log.error("remember_failed", error=type(exc).__name__)
        return ToolResult(False, None, _t("No pude guardarlo.", "I couldn't save it."), False)
    return ToolResult(True, {"id": fact_id, "content": text, "kind": kind}, _t("Guardado.", "Saved."), False)


async def _remember_slot(slot: structured.Slot, confirmed: bool) -> ToolResult:
    p = await asyncio.to_thread(_propose, slot)
    said = _say(slot.subject, slot.attribute, slot.value)
    data = {"subject": slot.subject, "attribute": slot.attribute, "value": slot.value, "change": p.change}
    if p.change == "same":
        await asyncio.to_thread(_commit, p)
        return ToolResult(True, data, _t(f"Ya lo tenía: {said}.", f"I already had that: {said}."), False)
    if p.change == "nothing_to_retract":
        return ToolResult(True, data, _t("No tenía eso guardado.", "I didn't have that saved."), False)
    if not confirmed:
        if p.change == "change" and p.old is not None:
            before = _say(slot.subject, slot.attribute, p.old[1])
            q = _t(f"Tenía que {before}. ¿Lo cambio a {slot.value}?",
                   f"I had that {before}. Change it to {slot.value}?")
        elif p.change == "retract":
            q = _t(f"¿Borro que {said}?", f"Remove that {said}?")
        else:
            q = _t(f"¿Guardo que {said}?", f"Save that {said}?")
        return ToolResult(False, data, q, True)
    await asyncio.to_thread(_commit, p)
    if p.change == "retract":
        return ToolResult(True, data, _t("Listo, ya no lo tengo guardado.", "Done, that's no longer saved."), False)
    return ToolResult(True, data, _t(f"Listo: {said}.", f"Done: {said}."), False)


@tool(reversible=True, reversible_because="read-only: it reports what is saved, changes nothing")
async def recall_facts(query: str = "", confirmed: bool = False) -> ToolResult:
    """Look up what Emma already knows about the user.

    Use when the user says any of:

    - "¿qué sabes de mí?"
    - "what do you know about me?"
    - "¿recuerdas algo sobre X?"  (X goes in `query`)

    Pass ``query`` to filter on a substring; leave empty for the
    highest-confidence facts overall. Returns up to 15 facts.
    """
    q = (query or "").strip()
    if q and not _SUMMARY.match(structured.fold(q).strip(" ?¿!.")):
        return await _answer(q)
    slots = [_cap(_say(subj, attr, val)) for _id, subj, attr, val in await asyncio.to_thread(_slots)]
    notes = [f"«{f.content}»" for f in await long_term.recall(limit=15)]
    things = slots + notes
    if not things:
        return ToolResult(True, {"facts": []}, _t("Todavía no tengo nada guardado sobre ti.",
                                                    "I don't have anything saved about you yet."), False)
    more = not confirmed and len(things) > _SHORT
    shown = things if confirmed else things[:_SHORT]
    msg = _t("Sé esto: ", "Here's what I know: ") + _join(shown) + "."
    if more:
        msg += _t(" ¿Quieres que te diga más?", " Want to hear more?")
    # "¿Quieres que te diga más?" is a real question: a yes re-runs this with confirmed=True.
    return ToolResult(True, {"facts": things}, msg, requires_confirmation=more)


async def _answer(q: str) -> ToolResult:
    asked = structured.parse_question(q)
    if asked is not None:
        values = await asyncio.to_thread(_lookup, *asked)
        if values:
            said = _join([_say(asked[0], asked[1], v) for v in values])
            return ToolResult(True, {"values": values}, _cap(said) + ".", False)
    notes = await long_term.recall(q, limit=1)  # strict floor: a near-miss isn't an answer
    if notes:
        return ToolResult(True, {"facts": [notes[0].content]},
                          _t(f"Tengo guardado: «{notes[0].content}».", f"I have this saved: «{notes[0].content}»."),
                          False)
    return ToolResult(True, {"facts": []}, _t("No tengo eso guardado.", "I don't have that saved."), False)


@tool()
async def forget_fact(content: str, confirmed: bool = False) -> ToolResult:
    """Delete a fact (or facts) matching the given content from long-term memory.

    Use when the user says any of:

    - "Emma, olvida que ..."
    - "Emma, borra lo de ..."
    - "forget that I ..."
    """
    text = (content or "").strip()
    if not text:
        return ToolResult(False, None, _t("Dime qué quieres que olvide.", "Tell me what to forget."), False)
    slot = structured.parse_statement(text)
    if slot is not None:
        rows = await asyncio.to_thread(_active, slot.subject, slot.attribute)
        rows = [r for r in rows if structured.fold(r[1]) == structured.fold(slot.value)] or rows
        if not rows:
            return ToolResult(True, {"removed": 0}, _t("No tenía eso guardado.", "I didn't have that saved."), False)
        said = _say(slot.subject, slot.attribute, rows[0][1])
        if not confirmed:
            return ToolResult(False, {"slot": rows[0][0]}, _t(f"¿Olvido que {said}?", f"Forget that {said}?"), True)
        await asyncio.to_thread(_forget_slot, rows[0][0])
        return ToolResult(True, {"removed": 1}, _t("Listo, lo olvidé.", "Done, forgotten."), False)
    notes = await long_term.recall(text, limit=1)
    if not notes:
        return ToolResult(True, {"removed": 0}, _t("No tenía nada guardado sobre eso.",
                                                    "I had nothing saved about that."), False)
    if not confirmed:
        return ToolResult(False, {"note": notes[0].id}, _t(f"¿Olvido «{notes[0].content}»?",
                                                             f"Forget «{notes[0].content}»?"), True)
    removed = await long_term.forget(notes[0].id)
    return ToolResult(True, {"removed": removed}, _t("Listo, lo olvidé.", "Done, forgotten."), False)


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
