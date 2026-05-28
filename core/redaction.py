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


def _card_sub(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED:CREDIT_CARD]"
    return m.group(0)


def _iban_sub(m: re.Match) -> str:
    return "[REDACTED:IBAN]" if len(m.group(0)) >= 15 else m.group(0)


def _phone_sub(m: re.Match) -> str:
    if len(re.sub(r"\D", "", m.group(0))) >= 10:
        return "[REDACTED:PHONE_INTL]"
    return m.group(0)


# (type, compiled pattern, replacement) — order matters.
_RULES: list[tuple[str, re.Pattern, object]] = [
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


def redaction_processor(logger, method_name, event_dict):
    """structlog processor: redact every string value in the event dict."""
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = redact(v)
    return event_dict
