"""PII redaction for logs and any text that might carry a secret.

`redact(text)` replaces known sensitive patterns with ``[REDACTED:<type>]``.
A structlog processor (:func:`redaction_processor`) applies it to every
string field of every log event before emission, so a credential that
slips into a log message never lands on disk verbatim.

Patterns are applied most-specific-first; each match is replaced before the
next pattern runs, so placeholders are never re-matched. Guards (Luhn for
cards, digit-count for phones, length for IBAN) keep false positives — like
ISO dates — from being redacted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, MutableMapping
from typing import Any

_API_KEY_RE = re.compile(r"[A-Za-z0-9+/_\-]{32,}")


def looks_like_api_key(value: str) -> bool:
    """True if `value` contains a 32+ char high-entropy (key-shaped) run."""
    return bool(_API_KEY_RE.search(value or ""))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_sub(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED:CREDIT_CARD]"
    return m.group(0)


def _iban_sub(m: re.Match[str]) -> str:
    return "[REDACTED:IBAN]" if len(m.group(0)) >= 15 else m.group(0)


def _phone_sub(m: re.Match[str]) -> str:
    raw = m.group(0)
    # Require a phone-shaped match: a leading "+" or an internal separator
    # (space/dash/paren). A bare digit run is almost always an ID, not a
    # phone — Emma logs epoch timestamps (X token expiry) and 19-digit tweet
    # IDs constantly, and mangling those to [REDACTED:PHONE_INTL] destroys
    # debuggability without preventing any real leak.
    has_shape = raw.startswith("+") or bool(re.search(r"[ \-()]", raw))
    if has_shape and len(re.sub(r"\D", "", raw)) >= 10:
        return "[REDACTED:PHONE_INTL]"
    return raw


# (type, compiled pattern, replacement) — order matters.
_RULES: list[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    ("CREDIT_CARD", re.compile(r"\d(?:[ -]?\d){12,18}"), _card_sub),
    ("CURP", re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z\d]\d\b"), "[REDACTED:CURP]"),
    ("RFC", re.compile(r"\b[A-Z&Ñ]{3,4}\d{6}[A-Z\d]{3}\b"), "[REDACTED:RFC]"),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:US_SSN]"),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), _iban_sub),
    ("API_KEY_LIKE", _API_KEY_RE, "[REDACTED:API_KEY_LIKE]"),
    ("PHONE_INTL", re.compile(r"\+?\d[\d\s\-()]{7,}\d"), _phone_sub),
]


def redact(text: str) -> str:
    """Replace each sensitive match with ``[REDACTED:<type>]``."""
    if not text:
        return text
    out = text
    for _kind, pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out


def contains_secret(text: str) -> bool:
    """True if `text` carries a HIGH-CONFIDENCE secret.

    Stricter than ``redact() != text``: built for callers that must REFUSE on a
    secret rather than over-redact harmless text (e.g. the X-post guard). The
    difference vs ``redact``:
      * a phone number is shareable PII, NOT a secret → ignored here;
      * an API-key-shaped run must mix letters AND digits to count — a long plain
        word or hashtag (``#SuperLargoHashtag…``) is not a secret;
      * cards still require a valid Luhn; IBAN still requires the length guard.
    """
    if not text:
        return False
    for kind, pattern, _repl in _RULES:
        if kind == "PHONE_INTL":
            continue
        for m in pattern.finditer(text):
            frag = m.group(0)
            if kind == "API_KEY_LIKE":
                if any(c.isalpha() for c in frag) and any(c.isdigit() for c in frag):
                    return True
                continue
            if kind == "CREDIT_CARD":
                digits = re.sub(r"\D", "", frag)
                if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                    return True
                continue
            if kind == "IBAN" and len(frag) < 15:
                continue
            return True  # CURP / RFC / US_SSN / valid IBAN — structural, high-confidence
    return False


def _redact_value(v: Any) -> Any:
    """Recursively redact strings inside nested dict/list/tuple structures."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _redact_value(sub) for k, sub in v.items()}
    if isinstance(v, (list, tuple)):
        return type(v)(_redact_value(sub) for sub in v)
    return v


def redaction_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact every string value in the event dict.

    Recurses into nested dict/list values so a credential passed as e.g.
    ``log.error("x", args={"password": "..."})`` is redacted too — not just
    top-level string fields.
    """
    for k, v in list(event_dict.items()):
        event_dict[k] = _redact_value(v)
    return event_dict
