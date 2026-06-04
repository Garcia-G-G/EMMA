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

import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from tools.base import ToolResult

# 19.6-B23: title normalization. Separators become spaces (so "a:b" ≠ "ab");
# quotes/apostrophes vanish. Built ONCE at module load (anti-pattern: no
# per-call table building).
_PUNCT_TO_SPACE = str.maketrans(dict.fromkeys(":;,.!?()[]{}", " "))
_PUNCT_DROP = str.maketrans("", "", "\"'")
# ñ is a DISTINCT letter in Spanish, not "n with decoration" — protect it
# through the NFD pass with a sentinel that survives casefolding.
_ENYE_SENTINEL = "\x00"


def normalize_title(s: str) -> str:
    """Punctuation/diacritics/whitespace-tolerant form for title comparison.

    'Limitación: terminal Cursor' ≡ 'limitación terminal Cursor'. Accents are
    stripped (NFD, drop combining marks) but ñ is preserved. Pure stdlib."""
    s = s.lower().replace("ñ", _ENYE_SENTINEL)
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace(_ENYE_SENTINEL, "ñ")
    s = s.translate(_PUNCT_TO_SPACE).translate(_PUNCT_DROP)
    return " ".join(s.split())


# Day/time words that make a "¿para cuándo?" question feel natural when notes are
# distinguished by date suffixes ("Pendientes para hoy" / "… el miércoles").
_TEMPORAL_WORDS = frozenset(
    {
        "hoy",
        "mañana",
        "manana",
        "ayer",
        "lunes",
        "martes",
        "miércoles",
        "miercoles",
        "jueves",
        "viernes",
        "sábado",
        "sabado",
        "domingo",
        "semana",
        "mes",
        "día",
        "dia",
        "today",
        "tomorrow",
        "yesterday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "week",
        "month",
        "day",
    }
)

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


async def find_by_title(
    enumerate_fn: Callable[[str, int], Awaitable[list[Match]]],
    title_esc: str,
    *,
    limit: int = 25,
) -> tuple[list[Match], str]:
    """Find notes by title with a tiered strategy, short-circuiting on the first
    tier that yields anything (Bug 19.5-B1).

    Tiers: exact ``name is`` → ``name starts with`` → ``name contains``.
    ``enumerate_fn`` runs an AppleScript ``name``-filter clause and returns
    Matches (e.g. ``notes_tool._enumerate``); ``title_esc`` is already escaped
    via ``macos.esc_applescript``. Returns ``(matches, strategy)`` so the caller
    can phrase its reply per strategy ("exact" can skip the "did you mean" tone).
    """
    for strategy, op in (("exact", "is"), ("starts_with", "starts with"), ("contains", "contains")):
        matches = await enumerate_fn(f' whose name {op} "{title_esc}"', limit)
        if matches:
            return matches, strategy
    # Last tier (19.6-B23): punctuation/diacritics-tolerant equality, so
    # "Limitación: terminal Cursor" finds "Limitación terminal Cursor".
    want = normalize_title(title_esc)
    if want:
        candidates = await enumerate_fn("", limit)
        normalized = [m for m in candidates if normalize_title(m.title) == want]
        if normalized:
            return normalized, "normalized"
    return [], "none"


def word_common_prefix(titles: list[str]) -> str:
    """Longest prefix shared by all titles, trimmed to whole words (never mid-word).

    'Pendientes para hoy' + 'Pendientes para el miércoles' → 'Pendientes para'.
    """
    if not titles:
        return ""
    word_lists = [t.split() for t in titles]
    common: list[str] = []
    for group in zip(*word_lists, strict=False):
        if all(w == group[0] for w in group):
            common.append(group[0])
        else:
            break
    return " ".join(common)


def _suffix_after(title: str, prefix: str) -> str:
    if prefix and title.lower().startswith(prefix.lower()):
        return title[len(prefix) :].strip()
    return title.strip()


def _looks_temporal(suffixes: list[str]) -> bool:
    for s in suffixes:
        for w in s.lower().replace("'", " ").split():
            if w in _TEMPORAL_WORDS:
                return True
    return False


def suffix_prompt(matches: list[Match], common_prefix: str, *, lang: str = "es") -> str:
    """Ask which match by their distinguishing suffix, not by number (Bug 19.5-B2).

    "Pendientes, ¿para cuándo? Tengo 'hoy' y 'el miércoles'." Falls back to a
    numeric prompt when the shared prefix is trivial, a suffix is empty, or there
    are more than 4 options (too many to read out conversationally).
    """
    prefix = common_prefix.strip()
    suffixes = [_suffix_after(m.title, prefix) for m in matches]
    if not prefix or len(matches) > 4 or any(not s for s in suffixes):
        listing = _enumerate_es(matches)
        if lang == "en":
            return f"I found {len(matches)}: {listing}. Which number?"
        return f"Encontré {len(matches)}: {listing}. ¿Cuál número?"
    temporal = _looks_temporal(suffixes)
    if lang == "en":
        q = "when for?" if temporal else "which one?"
        quoted = " and ".join(f"'{s}'" for s in suffixes)
        return f"{prefix} — {q} I have {quoted}."
    q = "¿para cuándo?" if temporal else "¿cuál?"
    quoted = " y ".join(f"'{s}'" for s in suffixes)
    return f"{prefix}, {q} Tengo {quoted}."


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
