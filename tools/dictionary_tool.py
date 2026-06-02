"""Let Garcia add knowledge-dictionary entries by voice (public info only)."""

from __future__ import annotations

import subprocess

from core import dictionary
from tools.base import ToolResult, tool


@tool()
async def remember_page(name: str, url: str, title: str = "") -> ToolResult:
    """Recuerda una página que Garcia abre seguido.

    Úsalo cuando diga:
    - "Emma, recuerda que mi <name> es <url>"
    - "Emma, mi <name> está en <url>"
    """
    slug = name.lower().replace(" ", "_")
    dictionary.append_page(slug, url, title=title or name)
    return ToolResult(True, {"slug": slug}, f"Listo, recordaré tu {name}.", False)


@tool()
async def remember_contact(
    name: str,
    email: str = "",
    relation: str = "",
    aliases: list[str] | None = None,
) -> ToolResult:
    """Recuerda un contacto (solo info pública — sin números de teléfono).

    Úsalo cuando Garcia diga "Emma, recuerda que mi mamá es Ana, su correo es ...".
    """
    slug = (relation or name).lower().replace(" ", "_")
    dictionary.append_contact(
        slug, name=name, email=email, relation=relation, aliases=aliases or []
    )
    return ToolResult(True, {"slug": slug}, f"Listo, agregué a {name}.", False)


@tool()
async def remember_term(key: str, expansion: str, context: str = "") -> ToolResult:
    """Recuerda un término del glosario (sigla, codename, jerga)."""
    dictionary.append_term(key.upper(), expansion, context=context)
    return ToolResult(True, {"key": key}, f"Listo, {key} → {expansion}.", False)


@tool()
async def open_my_page(name: str) -> ToolResult:
    """Abre una de las páginas guardadas de Garcia por nombre.

    Úsalo cuando diga "Emma, abre mi <name>" (GitHub, calendar, portfolio, ...).
    """
    p = dictionary.find_page(name)
    if not p:
        return ToolResult(False, None, f"No conozco una página llamada '{name}'.", False)
    subprocess.Popen(["open", p.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ToolResult(True, {"url": p.url}, f"Abriendo {p.title}.", False)
