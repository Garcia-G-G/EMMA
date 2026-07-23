"""Personality axes → the system prompt's # Personality section (EMMA-APP Part 4).

Five axes, each 1-5, default 3. At ALL defaults the section reproduces today's
exact three bullets byte-for-byte — a user who never opens the panel gets zero
behavior change (a test asserts this). Off-default axes only APPEND a clarifying
line; they never rewrite the base bullets.

Hard rule: personality is TASTE, not correctness. These lines never touch the
Language, Confirmation flow, or Tool Results sections. Verbosidad NUDGES within
the existing # Response Length limits (shortest vs fullest end) — it does NOT
rewrite the sentence caps. The UI says "se aplica en tu próxima conversación"
because the system prompt is built once per session (no live mid-session change).
"""

from __future__ import annotations

# axis -> {level: spanish label}. Level 3 is the default (bold in the UI).
AXES: dict[str, dict[str, object]] = {
    "calidez": {
        "titulo": "Calidez",
        "labels": {1: "distante", 2: "sobria", 3: "cálida", 4: "muy cálida", 5: "efusiva"},
    },
    "formalidad": {
        "titulo": "Formalidad",
        "labels": {1: "coloquial", 2: "relajada", 3: "colega de confianza", 4: "profesional", 5: "formal"},
    },
    "humor": {
        "titulo": "Humor",
        "labels": {1: "seca", 2: "sobria", 3: "ligeramente ingeniosa", 4: "bromista", 5: "juguetona"},
    },
    "verbosidad": {
        "titulo": "Verbosidad",
        "labels": {1: "telegráfica", 2: "breve", 3: "concisa", 4: "explicativa", 5: "detallada"},
    },
    "proactividad": {
        "titulo": "Proactividad",
        "labels": {1: "solo responde", 2: "reservada", 3: "sugiere a veces", 4: "propositiva", 5: "se adelanta"},
    },
}

DEFAULTS: dict[str, int] = dict.fromkeys(AXES, 3)

# Today's exact # Personality bullets. DEFAULT output — do not edit without a
# matching change to tests/test_personality.py (the byte-for-byte guarantee).
BASE_LINES: tuple[str, ...] = (
    "- Confident, calm, slightly witty. Never flustered.",
    "- Talk to the user like a trusted colleague, not a customer.",
    "- Be direct. No filler, no hedging, no apologies.",
)

# Off-default guidance. Each maps a direction (below/above 3) to one appended line.
# None means "no line for this direction". Verbosidad stays WITHIN the response
# caps; proactividad speaks to volunteering, not to any confirmation flow.
_MODIFIERS: dict[str, dict[str, str | None]] = {
    "calidez": {
        "low": "- Lean cooler and more matter-of-fact; go easy on the warmth.",
        "high": "- Lean warmer and more personable; a little extra warmth is welcome.",
    },
    "formalidad": {
        "low": "- Speak casually, like a close friend.",
        "high": "- Keep a more formal, professional register.",
    },
    "humor": {
        "low": "- Play it straight; keep humor to a minimum.",
        "high": "- Be more playful; a light joke here and there is welcome.",
    },
    "verbosidad": {
        "low": "- Prefer the shortest end of the response-length range (still within the caps).",
        "high": "- Prefer the fuller end of the response-length range (never past the caps).",
    },
    "proactividad": {
        "low": "- Do not volunteer suggestions; answer only what was asked.",
        "high": "- Proactively suggest an obvious next step when it clearly helps.",
    },
}


def normalize(profile: dict[str, int] | None) -> dict[str, int]:
    """Clamp to valid axes/levels, filling missing axes with the default (3)."""
    out = dict(DEFAULTS)
    for axis, val in (profile or {}).items():
        if axis in AXES:
            try:
                out[axis] = max(1, min(5, int(val)))
            except (TypeError, ValueError):
                out[axis] = DEFAULTS[axis]
    return out


def personality_lines(profile: dict[str, int] | None = None) -> list[str]:
    """The # Personality bullets for the given axis profile.

    At all defaults this is exactly BASE_LINES (byte-for-byte). Each off-default
    axis appends one clarifying line, in axis order.
    """
    p = normalize(profile)
    lines = list(BASE_LINES)
    for axis in AXES:
        val = p[axis]
        if val == 3:
            continue
        line = _MODIFIERS[axis]["low" if val < 3 else "high"]
        if line:
            lines.append(line)
    return lines
