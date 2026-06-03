"""Let Garcia add knowledge-dictionary entries by voice (public info only)."""

from __future__ import annotations

import subprocess

from core import dictionary, vocabulary
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


_USER_FIELDS_ES = {
    "display_name": "nombre",
    "full_name": "nombre completo",
    "github_username": "usuario de GitHub",
    "linkedin": "LinkedIn",
    "website": "sitio web",
    "preferred_lang": "idioma",
}


@tool()
async def remember_user_profile(field: str, value: str) -> ToolResult:
    """Guarda un dato de identidad de Garcia (quién es 'yo/mi/mis').

    Úsalo cuando diga "mi usuario de GitHub es X", "me llamo Y",
    "mi LinkedIn es Z". `field` ∈ display_name, full_name, github_username,
    linkedin, website, preferred_lang.
    """
    f = field.strip().lower()
    if f not in _USER_FIELDS_ES:
        opciones = ", ".join(_USER_FIELDS_ES)
        return ToolResult(
            False, None, f"No conozco el campo '{field}'. Opciones: {opciones}.", False
        )
    v = value.strip()
    if not v:
        return ToolResult(False, None, "Dime el valor a guardar.", False)
    if not dictionary.set_user_field(f, v):
        return ToolResult(False, None, f"No pude guardar tu {_USER_FIELDS_ES[f]}.", False)
    return ToolResult(
        True, {"field": f, "value": v}, f"Anoté que tu {_USER_FIELDS_ES[f]} es {v}.", False
    )


@tool()
async def remember_stt_correction(wrong: str, right: str, section: str = "auto") -> ToolResult:
    """Aprende de una corrección de pronunciación/transcripción.

    Úsalo cuando Garcia te corrige ('no, es X, no Y'). `wrong` es lo que
    entendiste, `right` lo correcto. Lo guardo para no volver a equivocarme.

    Las correcciones viven en la librería de vocabulario (el almacén de
    correcciones de STT); `section` se acepta pero es informativo — el
    vocabulario ya alimenta tanto el sesgo de transcripción como la corrección.
    """
    w = (wrong or "").strip()
    rt = (right or "").strip()
    if not w or not rt:
        return ToolResult(False, None, "Necesito qué entendí mal y cómo se dice bien.", False)
    if w.lower() == rt.lower():
        return ToolResult(False, None, "Eso ya es igual, no hay nada que corregir.", False)
    existing = vocabulary.find_canonical(rt)
    if existing:
        vocabulary.add_alias(existing, w)
    else:
        vocabulary.append_entry(canonical=rt, aliases=[w])
    return ToolResult(
        True,
        {"wrong": w, "right": rt},
        f"Anoté: '{w}' es '{rt}'. La próxima vez te entiendo.",
        False,
    )


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
