"""Canonical multi-match disambiguation for destructive AppleScript tools.

Bug 19.2-B2 (DATA LOSS): tools like ``delete_note`` / ``delete_event`` ran an
AppleScript ``repeat with x in matches`` loop that *deleted every match* sharing
a title. Two notes both named "Pendientes" → both gone, silently. The live
``repeat ... delete`` also crashes with ``-1728`` because the collection shrinks
mid-iteration.

The fix, applied uniformly to notes / calendar / reminders:

1. AppleScript *enumerates* matches (``id`` + a human date + title), it never
   deletes in a loop.
2. Python decides via :func:`disambiguate`:
   - 0 matches  → friendly "no encontré…".
   - 1 match    → proceed (the tool's own yes/no confirmation still applies).
   - >1 matches → ``requires_confirmation=True`` enumerating the matches with
     dates; the user picks one by number.
   - an explicit 1-based ``index`` → that single match, addressed by ``id``.
3. The tool deletes/completes exactly ONE match, by its stable ``id``.

Note on the confirmation convention: ``tools/base`` reserves
``requires_confirmation`` for binary yes/no. The >1-match branch here returns it
for a "pick a number" prompt — a deliberate, documented exception for Bug B2,
because the spec's evidence asserts ``requires_confirmation=True`` for the
ambiguous case and the visible ``index`` parameter (plus a system-prompt line)
lets the model re-call with the chosen number. See PR 19.2 notes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from tools.base import ToolResult

# AppleScript field/record separators. U+2016 (‖) is vanishingly unlikely to
# appear in a note title or event summary, so it survives round-tripping.
FIELD_SEP = "‖"
RECORD_SEP = "\n"

# Shared AppleScript handler: format an AppleScript `date` as ISO 8601
# (YYYY-MM-DDThh:mm:ss). Prepend to a script and call as `my isoDate(someDate)`
# from inside a `tell application` block — but resolve the date IN the block and
# pass it in (a top-level handler runs outside the app context). Used by the
# notes/calendar/reminders enumerators so disambiguation shows real timestamps.
ISO_DATE_HANDLER = (
    "on isoDate(d)\n"
    "  set m to (month of d) as integer\n"
    '  set mz to text -2 thru -1 of ("0" & m)\n'
    '  set dz to text -2 thru -1 of ("0" & ((day of d) as integer))\n'
    '  set hz to text -2 thru -1 of ("0" & ((hours of d) as integer))\n'
    '  set nz to text -2 thru -1 of ("0" & ((minutes of d) as integer))\n'
    '  set sz to text -2 thru -1 of ("0" & ((seconds of d) as integer))\n'
    '  return ((year of d) as text) & "-" & mz & "-" & dz & "T" & hz & ":" & nz & ":" & sz\n'
    "end isoDate\n"
)


@dataclass(frozen=True)
class Match:
    """One candidate the user might have meant."""

    id: str
    title: str
    when: str = ""  # human/ISO date (modification, start, or due) — may be ""
    preview: str = ""


def parse_matches(raw: str) -> list[Match]:
    """Parse the ``id‖when‖title‖preview`` lines an enumeration script returns."""
    out: list[Match] = []
    for line in raw.split(RECORD_SEP):
        line = line.strip()
        if not line:
            continue
        parts = line.split(FIELD_SEP)
        # Tolerate missing trailing fields.
        parts += [""] * (4 - len(parts))
        mid, when, title, preview = parts[0], parts[1], parts[2], parts[3]
        if not mid:
            continue
        out.append(
            Match(id=mid.strip(), title=title.strip(), when=when.strip(), preview=preview.strip())
        )
    return out


def _enumerate_es(matches: list[Match]) -> str:
    """'1, modificada 2026-06-02T18:20; 2, modificada 2026-06-02T18:21'."""
    bits = []
    for i, m in enumerate(matches, start=1):
        when = f" ({m.when})" if m.when else ""
        bits.append(f"{i}{when}")
    return "; ".join(bits)


def disambiguate(
    matches: list[Match],
    index: int | None,
    *,
    noun: str,
    title: str,
) -> tuple[Match | None, ToolResult | None]:
    """Decide which single match to act on.

    Returns ``(chosen, response)`` where exactly one is non-None:
    - ``chosen`` set, ``response`` None → caller proceeds on that one match.
    - ``response`` set, ``chosen`` None → caller returns it verbatim (not found,
      ambiguous-pick prompt, or a bad index).

    ``noun`` is the Spanish singular ("nota", "evento", "recordatorio") used in
    the user-facing strings; ``title`` is what the user asked for.
    """
    n = len(matches)
    if n == 0:
        return None, ToolResult(
            True, {"matches": 0}, f"No encontré ningún/a {noun} con el nombre '{title}'.", False
        )

    if index is not None:
        if 1 <= index <= n:
            return matches[index - 1], None
        return None, ToolResult(
            True,
            {"matches": n},
            f"Solo hay {n}. Dime un número entre 1 y {n}.",
            False,
        )

    if n == 1:
        return matches[0], None

    # n > 1, no index → ask which one (pick by number). Neutral verb so the same
    # helper fits delete / complete / merge.
    return None, ToolResult(
        True,
        {"matches": [asdict(m) for m in matches]},
        f"Encontré {n} {noun} con el nombre '{title}': {_enumerate_es(matches)}. "
        "¿A cuál te refieres? Dime el número.",
        requires_confirmation=True,
    )
