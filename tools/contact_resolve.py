"""Shared recipient resolution for mail/messages (21-B25 sites 4+5).

"Manda mensaje a mi mamá" → the LLM passes a NAME, not an address. Exact
dictionary hit resolves silently; a near-miss (mistranscribed name) answers
with the transversal "¿quisiste decir…?" suggestion shape instead of a
dead "no encontré".
"""

from __future__ import annotations

from core import dictionary
from tools.base import ToolResult
from tools.disambiguation import suggest_similar, suggestion_question


def _looks_like_address(recipient: str) -> bool:
    r = recipient.strip()
    return "@" in r or any(ch.isdigit() for ch in r)


def resolve_recipient(recipient: str) -> tuple[str | None, ToolResult | None]:
    """(address, None) on success, (None, suggestion/None-result) on miss.

    Addresses (emails/phones) pass through verbatim. Names resolve via the
    contacts dictionary; near-misses return the B25 suggestion ToolResult
    for the caller to surface (requires_confirmation + picked re-call).
    """
    r = (recipient or "").strip()
    if not r:
        return None, ToolResult(False, None, "¿A quién se lo mando?", False)
    if _looks_like_address(r):
        return r, None

    contact = dictionary.find_contact(r)
    if contact is not None and contact.email:
        return contact.email, None

    candidates: set[str] = set()
    for c in dictionary.contacts().values():
        candidates.add(c.name)
        candidates.add(c.relation)
        candidates.update(c.aliases)
    suggestions = suggest_similar(r, sorted(c for c in candidates if c))
    if suggestions:
        return None, ToolResult(
            True,
            {"query": r, "suggestions": [s for s, _ in suggestions]},
            suggestion_question(r, suggestions, noun="un contacto llamado"),
            requires_confirmation=True,
        )
    return None, ToolResult(
        False,
        None,
        f"No tengo un contacto llamado '{r}'. Dime su correo o enséñamelo con remember_contact.",
        False,
    )
