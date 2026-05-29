"""Emma's vocabulary library: STT correction + pronunciation hints.

The library is a small TOML file (``config/vocabulary.toml``) of technical
names Emma routinely mishears or mispronounces. Each entry carries:

- ``canonical`` — the official spelling Emma reads and writes.
- ``stt_aliases`` — common mistranscriptions folded back to canonical.
- ``say_es`` / ``say_en`` — optional phonetic spelling for spoken output.
- ``description`` — one-liner for the LLM's situational awareness.

The file is parsed **once on import** into a module-level cache. Call
:func:`reload` after editing the file on disk (e.g. via
:func:`append_entry`) to rebuild the cache and the precompiled alias regex.

Public surface:
    reload() -> int
    corrections(text) -> str
    pronunciation_block(lang="es") -> str
    bias_words() -> list[str]
    append_entry(canonical, say_es, say_en, aliases, description) -> None
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("emma.vocabulary")

_VOCAB_PATH = Path(__file__).resolve().parent.parent / "config" / "vocabulary.toml"

# Module-level cache, populated by reload().
_entries: dict[str, dict[str, Any]] = {}
_alias_to_canonical: dict[str, str] = {}
_alias_pattern: re.Pattern[str] | None = None


def _build_alias_index(
    entries: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], re.Pattern[str] | None]:
    """Map lowercased aliases → canonical and compile a whole-word regex.

    Aliases are sorted longest-first so multi-word phrases (``"cloud code"``)
    win over the shorter aliases they contain (``"cloud"``) — Python's ``re``
    alternation is ordered, not longest-match.
    """
    alias_to_canonical: dict[str, str] = {}
    for entry in entries.values():
        canonical = entry["canonical"]
        for alias in entry.get("stt_aliases", []):
            key = alias.lower().strip()
            if key:
                alias_to_canonical[key] = canonical
    if not alias_to_canonical:
        return {}, None
    ordered = sorted(alias_to_canonical, key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in ordered) + r")\b",
        re.IGNORECASE,
    )
    return alias_to_canonical, pattern


def reload() -> int:
    """Re-parse the TOML file, rebuild the alias index, return entry count."""
    global _entries, _alias_to_canonical, _alias_pattern
    try:
        with open(_VOCAB_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        log.warning("vocabulary_missing", path=str(_VOCAB_PATH))
        data = {}
    except tomllib.TOMLDecodeError as exc:
        log.error("vocabulary_parse_failed", path=str(_VOCAB_PATH), error=str(exc))
        data = {}
    # Keep only well-formed entries (must have a canonical name).
    entries: dict[str, dict[str, Any]] = {
        slug: body
        for slug, body in data.items()
        if isinstance(body, dict) and body.get("canonical")
    }
    _entries = entries
    _alias_to_canonical, _alias_pattern = _build_alias_index(entries)
    return len(_entries)


def corrections(text: str) -> str:
    """Fold known STT mistranscriptions back to their canonical spelling.

    Case-insensitive, whole-word only (``cloud`` inside ``cloudy`` is left
    alone). The replacement always uses the canonical capitalization.
    """
    if not text or _alias_pattern is None:
        return text

    def _repl(match: re.Match[str]) -> str:
        return _alias_to_canonical.get(match.group(0).lower(), match.group(0))

    return _alias_pattern.sub(_repl, text)


def pronunciation_block(lang: str = "es") -> str:
    """Build the system-prompt pronunciation guide for ``lang``.

    Returns an empty string when no entry has a phonetic hint for that
    language (so callers can skip appending it).
    """
    key = "say_es" if lang == "es" else "say_en"
    lines: list[str] = []
    for entry in _entries.values():
        hint = (entry.get(key) or "").strip()
        if hint:
            lines.append(f'- {entry["canonical"]} → say "{hint}"')
    if not lines:
        return ""
    header = (
        "# Pronunciation guide (mandatory)\n"
        "When you say any of these names out loud, pronounce them as written "
        "(phonetic, in the spoken language):"
    )
    return header + "\n" + "\n".join(lines)


def bias_words() -> list[str]:
    """Flat list of canonical names (for transcription hot-word bias)."""
    return [entry["canonical"] for entry in _entries.values()]


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _slug_for(canonical: str) -> str:
    """ASCII section slug derived from the canonical name."""
    slug = re.sub(r"[^A-Za-z0-9]", "", canonical)
    return slug or "Entry"


def append_entry(
    canonical: str,
    say_es: str = "",
    say_en: str = "",
    aliases: list[str] | None = None,
    description: str = "",
) -> None:
    """Append a new vocabulary block to the TOML file and reload the cache."""
    canonical = (canonical or "").strip()
    if not canonical:
        return
    aliases = [a.strip() for a in (aliases or []) if a.strip()]

    block = [f"\n[{_slug_for(canonical)}]", f'canonical = "{_toml_escape(canonical)}"']
    if description.strip():
        block.append(f'description = "{_toml_escape(description.strip())}"')
    alias_list = ", ".join(f'"{_toml_escape(a)}"' for a in aliases)
    block.append(f"stt_aliases = [{alias_list}]")
    if say_es.strip():
        block.append(f'say_es = "{_toml_escape(say_es.strip())}"')
    if say_en.strip():
        block.append(f'say_en = "{_toml_escape(say_en.strip())}"')

    with open(_VOCAB_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    reload()


# Load once on import.
reload()
