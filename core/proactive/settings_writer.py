"""Persist a single setting to ``.env`` and apply it to the live ``settings``.

Used by the proactive voice tools so a "disable morning briefing" survives a
daemon restart (``.env`` is the source of truth) AND takes effect immediately
(the in-memory ``settings`` attribute is updated). Only writes non-secret
``PROACTIVE_*`` keys — never a credential.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from config.settings import settings

log = structlog.get_logger("emma.proactive.settings_writer")

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def _coerce(field: str, raw: str) -> object:
    """Coerce a string to the type of the existing settings field."""
    current = getattr(settings, field, None)
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def persist(field: str, value: str) -> None:
    """Write ``field=value`` to ``.env`` (replacing any prior line) and apply it
    to ``settings`` immediately. Refuses anything not prefixed ``PROACTIVE_``."""
    if not field.startswith("PROACTIVE_"):
        raise ValueError(f"refusing to persist non-proactive key: {field}")
    # Live override first (validate/coerce against the existing field type).
    setattr(settings, field, _coerce(field, value))

    line = f"{field}={value}"
    text = _ENV_PATH.read_text() if _ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(field)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        text = text.rstrip("\n") + f"\n{line}\n" if text else f"{line}\n"
    _ENV_PATH.write_text(text)
    log.info("proactive_setting_persisted", field=field, value=value)
