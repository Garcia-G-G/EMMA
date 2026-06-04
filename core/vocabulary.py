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


# The Realtime API caps InputAudioTranscription.prompt at ~500 chars. We bound
# the joined bias string a touch under that so the caller's [:500] trim never
# slices a word in half.
_BIAS_CHAR_BUDGET = 480


def _vocab_bias_words() -> list[str]:
    """Vocabulary canonical names + their STT aliases."""
    out: list[str] = []
    for entry in _entries.values():
        out.append(entry["canonical"])
        out.extend(entry.get("stt_aliases", []))
    return out


def bias_words() -> list[str]:
    """Proper nouns to seed the Whisper decoder (hot-word biasing).

    Unions every proper noun Emma already knows — identity, contacts, glossary,
    pages, apps, and the technical vocabulary — so STT stops mangling names it
    has been taught (Bug 19.4-B12). Deduped case-insensitively (first spelling
    wins), and length-bounded: when the union overflows the prompt budget, the
    lowest-priority groups (pages → apps → vocabulary) truncate first so identity
    and contacts always survive.

    ``core.dictionary`` is imported lazily to avoid a circular import (both load
    near startup); if it isn't ready, this degrades to the vocabulary-only list.
    """
    # Priority order: identity → contacts → glossary → pages → apps → vocabulary.
    groups: list[list[str]] = []
    try:
        from core import dictionary

        prof = dictionary.user_profile()
        groups.append([prof.get(f, "") for f in ("display_name", "full_name", "github_username")])

        contact_words: list[str] = []
        for c in dictionary.contacts().values():
            contact_words.append(c.name)
            contact_words.extend(c.aliases)
        groups.append(contact_words)

        groups.append(list(dictionary.terms().keys()))  # glossary acronyms
        groups.append([p.title for p in dictionary.pages().values()])
        groups.append(list(dictionary.apps_preferences().values()))
    except Exception:
        # Dictionary not loaded (e.g. an isolated unit test) — vocab-only.
        groups = []

    groups.append(_vocab_bias_words())  # lowest priority: truncates first

    seen: set[str] = set()
    result: list[str] = []
    length = 0
    for group in groups:
        for raw in group:
            word = (raw or "").strip()
            if not word:
                continue
            key = word.lower()
            if key in seen:
                continue
            extra = len(word) + (1 if result else 0)  # +1 for the joining space
            if length + extra > _BIAS_CHAR_BUDGET:
                continue  # over budget — skip, but keep scanning higher-priority leftovers
            seen.add(key)
            result.append(word)
            length += extra
    return result


def bias_render(mode: str, budget_chars: int = 500) -> str:
    """Render the bias words for the transcription model's expected format (19.5-A2).

    - ``mode == "prompt"`` (whisper-1): space-joined free text, hard-capped at
      ``budget_chars`` — byte-for-byte today's behavior.
    - ``mode == "keywords"`` (gpt-realtime-whisper): a ``"Keywords: a, b, c"``
      list, accumulated whole-word so it never exceeds ``budget_chars``. This is
      the format the Realtime transcription docs recommend for the streaming
      model, which ignores the free-text ``prompt``.

    Priority order (identity → contacts → glossary → pages → apps → vocab) and
    the dedup come from :func:`bias_words`; this only reformats + bounds.
    """
    words = bias_words()
    if mode == "keywords":
        prefix = "Keywords: "
        picked: list[str] = []
        length = len(prefix)
        for w in words:
            extra = len(w) + (2 if picked else 0)  # ", " separator
            if length + extra > budget_chars:
                break
            picked.append(w)
            length += extra
        return prefix + ", ".join(picked)
    # "prompt" (default): preserve today's exact rendering.
    return " ".join(words)[:budget_chars]


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) value.

    Vocabulary values are always single-line (names, aliases, pronunciations),
    so control characters — newlines, tabs, CR — are stripped outright. This is
    the security boundary for :func:`append_entry`: without it, a crafted value
    containing ``\\n[BadSection]`` would break the single-line string and inject
    rogue TOML structure (or corrupt the whole file). After stripping, the
    backslash and double-quote are escaped so the value stays one basic string.
    """
    value = "".join(ch for ch in value if ord(ch) >= 0x20)
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


def find_canonical(text: str) -> str | None:
    """Return the canonical name matching ``text`` (case-insensitive), or None."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for entry in _entries.values():
        if entry["canonical"].strip().lower() == t:
            return str(entry["canonical"])
    return None


def add_alias(canonical: str, alias: str) -> bool:
    """Append ``alias`` to an existing entry's ``stt_aliases`` (Bug 19.4-B15).

    In-place edit (TOML forbids redeclaring the section), preserving the rest of
    the file byte-for-byte. Returns False if no entry has that canonical. A
    duplicate alias is a no-op success.
    """
    alias = (alias or "").strip()
    if not alias:
        return False
    target_slug: str | None = None
    existing: list[str] = []
    for slug, body in _entries.items():
        if body["canonical"].strip().lower() == canonical.strip().lower():
            target_slug = slug
            existing = list(body.get("stt_aliases", []))
            break
    if target_slug is None:
        return False
    if alias.lower() in [a.lower() for a in existing]:
        return True  # already known

    text = _VOCAB_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    in_target = False
    edited = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_target = s == f"[{target_slug}]"
        if in_target and not edited and s.startswith("stt_aliases"):
            rendered = ", ".join(f'"{_toml_escape(a)}"' for a in [*existing, alias])
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}stt_aliases = [{rendered}]")
            edited = True
            continue
        out.append(line)
    if not edited:
        # Entry lacked an stt_aliases line — insert one right after its header.
        rebuilt: list[str] = []
        for line in out:
            rebuilt.append(line)
            if line.strip() == f"[{target_slug}]":
                rebuilt.append(f'stt_aliases = ["{_toml_escape(alias)}"]')
        out = rebuilt
    _VOCAB_PATH.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    reload()
    return True


# Load once on import.
reload()
