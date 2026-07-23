"""Let the user teach Emma new words by voice."""

from __future__ import annotations

from core import vocabulary
from tools.base import ToolResult, tool


@tool()
async def add_vocabulary_word(
    canonical: str,
    say_es: str = "",
    say_en: str = "",
    aliases: list[str] | None = None,
    description: str = "",
) -> ToolResult:
    """Teach Emma how to pronounce or transcribe a new technical name.

    Use when the user says any of:
    - "Emma, agrega 'X' al vocabulario, se pronuncia 'Y' en español."
    - "Emma, cuando diga 'X' entiende que es 'Y'."
    - "Emma, recuerda que 'X' se dice 'Y'."
    """
    canonical = (canonical or "").strip()
    if not canonical:
        return ToolResult(False, None, "¿Cuál palabra agrego?", False)
    vocabulary.append_entry(
        canonical=canonical,
        say_es=say_es.strip(),
        say_en=say_en.strip(),
        aliases=[a.strip() for a in (aliases or []) if a.strip()],
        description=description.strip(),
    )
    return ToolResult(
        True, {"canonical": canonical}, f"Listo, agregué '{canonical}' al vocabulario.", False
    )
