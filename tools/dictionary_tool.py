"""Let the user add knowledge-dictionary entries by voice (public info only)."""

from __future__ import annotations

import subprocess

from core import dictionary, vocabulary
from tools.base import ToolResult, tool
from tools.disambiguation import suggest_similar, suggestion_question


@tool()
async def remember_page(name: str, url: str, title: str = "") -> ToolResult:
    """Recuerda una página que the user abre seguido.

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

    Úsalo cuando the user diga "Emma, recuerda que mi mamá es Ana, su correo es ...".
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
    """Guarda un dato de identidad de the user (quién es 'yo/mi/mis').

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


# Voice/category aliases → the dictionary-native [apps.<key>] block.
_APP_CATEGORY_ALIASES = {
    "editor": "editor",
    "ide": "editor",
    "code": "editor",
    "browser": "browser",
    "terminal": "terminal",
    "shell": "terminal",
    "music": "music",
}


def _canonical_app_display(category: str, app: str) -> str | None:
    """Loose app name → the shortlist's canonical display name ('vscode' /
    'VS Code' → 'Visual Studio Code'), or None if unsupported. Reuses the
    preference normalizer's alias table so both paths agree."""
    from actions.environment import SHORTLISTS
    from tools.preferences import _normalize_app

    env_cat = "ide" if category == "editor" else category
    key = _normalize_app(env_cat, app)  # type: ignore[arg-type]
    if key is None:
        return None
    for entry in SHORTLISTS.get(env_cat, []):  # type: ignore[call-overload]
        if entry.get("key") == key:
            apps = entry.get("apps") or []
            return apps[0] if apps else None
    return None


@tool()
async def remember_app_preference(category: str, app: str) -> ToolResult:
    """Guarda la app preferida de the user para una categoría y la usa de ahí en adelante.

    Úsalo (1) la PRIMERA vez que necesites abrir algo y un tool te diga que no
    hay editor configurado (`data.editor_unset`), después de que the user elija, o
    (2) cuando diga "cambia mi editor a X" / "usa Y para código".

    `category` ∈ editor, browser, terminal, music. Escribe en el diccionario —
    la fuente que el router lee primero — y recarga en caliente, así que la
    próxima edición ya abre en la app elegida.
    """
    cat = _APP_CATEGORY_ALIASES.get(category.strip().lower())
    if not cat:
        return ToolResult(
            False,
            None,
            f"No reconozco la categoría '{category}'. Usa: editor, browser, terminal, music.",
            False,
        )
    display = _canonical_app_display(cat, app)
    if display is None:
        return ToolResult(False, None, f"No soporto {app} para {cat} todavía.", False)
    if not dictionary.set_app_preference(cat, display):
        return ToolResult(False, None, f"No pude guardar tu preferencia de {cat}.", False)
    return ToolResult(
        True,
        {"category": cat, "app": display},
        f"Listo, usaré {display} para {cat} de ahora en adelante.",
        False,
    )


@tool()
async def remember_connection(name: str, app: str, kind: str = "connection") -> ToolResult:
    """Recuerda un recurso DENTRO de una app: conexión de TablePlus, canal, etc.

    Úsalo cuando the user diga "Emma, recuerda la conexión <name> de TablePlus"
    o cuando intente abrir una conexión que aún no conozco y me dicte el
    nombre exacto. `kind` ∈ connection, channel, dm, note (19.6-B17).
    """
    n = (name or "").strip()
    a = (app or "").strip().lower()
    if not n or not a:
        return ToolResult(False, None, "Necesito el nombre del recurso y la app.", False)
    slug = dictionary.append_connection(n, app=a, kind=kind.strip() or "connection")
    return ToolResult(
        True, {"slug": slug, "app": a}, f"Listo, anoté la conexión '{n}' de {app}.", False
    )


@tool()
async def remember_stt_correction(wrong: str, right: str, section: str = "auto") -> ToolResult:
    """Aprende de una corrección de pronunciación/transcripción.

    Úsalo cuando the user te corrige ('no, es X, no Y'). `wrong` es lo que
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
async def open_my_page(name: str, picked: str = "", confirmed: bool = False) -> ToolResult:
    """Abre una de las páginas guardadas de the user por nombre.

    Úsalo cuando diga "Emma, abre mi <name>" (GitHub, calendar, portfolio, ...).
    Si sugerí opciones y the user eligió, re-llámame con `picked=<su elección>`
    y confirmed=true (21-B25).
    """
    query = (picked or name or "").strip()
    p = dictionary.find_page(query)
    if not p:
        pages = dictionary.pages()
        candidates = sorted({k for k in pages} | {pg.title for pg in pages.values() if pg.title})
        suggestions = suggest_similar(query, candidates)
        if suggestions:
            return ToolResult(
                True,
                {"query": query, "suggestions": [s for s, _ in suggestions]},
                suggestion_question(query, suggestions, noun="una página llamada"),
                requires_confirmation=True,
            )
        return ToolResult(False, None, f"No conozco una página llamada '{query}'.", False)
    subprocess.Popen(["open", p.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ToolResult(True, {"url": p.url}, f"Abriendo {p.title}.", False)
