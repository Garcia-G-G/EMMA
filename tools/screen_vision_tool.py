"""Voice-facing screen-vision tools (Prompt 27).

Read-only by default (describe / read / find / summarize); click + type are
``destructive=True`` and go through the two-phase confirmation gate. All AX work
lives in :mod:`core.screen_vision`; these tools shape it into Spanish results and
never read or speak secret-field values.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core import screen_vision as sv
from core.redaction import redact
from tools.base import ToolResult, tool
from tools.disambiguation import suggest_similar

log = structlog.get_logger("emma.tools.screen_vision")

_NO_WINDOW = "No veo una ventana enfocada ahora mismo."

# Apps whose AX tree is sparse BY DESIGN — a terminal or a blank document has
# almost nothing for AX to expose, so a thin read there is expected, not a
# failure. We surface `thin_by_design=True` so the LLM does NOT chain a wasteful
# screenshot fallback for them. (27.3 — a classifier of expected-thinness, not a
# per-app routing branch.)
_THIN_BY_DESIGN_APPS = {
    "terminal", "iterm", "iterm2", "alacritty", "kitty", "wezterm", "warp",
    "hyper", "ghostty", "tmux",
}


def _ax_density(
    static_texts: list[str], buttons: list[str], app: str,
    bounds: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    """A small content-density signal the LLM reads from `data` to decide whether
    the AX read was too thin and it should fall back to `look_at_screen` (27.3).

    We never branch on this in tool code — we only *report* it. The chaining
    decision lives in the system prompt at the LLM layer.
    """
    lines = [t for t in static_texts if t and t.strip()]
    text = "\n".join(lines)
    chars = len(text)
    n_static = len(lines)
    n_buttons = len(buttons)
    big_window = bool(bounds and bounds[2] > 600 and bounds[3] > 400)
    appears_thin = (
        chars < 80
        or (len(lines) < 3 and big_window)
        or (n_static == 0 and n_buttons < 4)
    )
    return {
        "ax_lines": len(lines),
        "ax_chars": chars,
        "ax_buttons": n_buttons,
        "ax_static_text": n_static,
        "ax_appears_thin": appears_thin,
        "thin_by_design": (app or "").strip().lower() in _THIN_BY_DESIGN_APPS,
    }


def _best_button(name: str, labels: list[str]) -> str | None:
    """Exact / substring match first, then a high-confidence fuzzy match."""
    nl = name.lower()
    for lab in labels:
        if lab.lower() == nl or nl in lab.lower():
            return lab
    sims = suggest_similar(name, labels, k=1, threshold=0.7)
    return sims[0][0] if sims else None


def _short_summary(r: sv.ScreenRead) -> str:
    msg = f"Tienes {r.app} adelante"
    if r.title:
        msg += f", en «{r.title}»"
    msg += "."
    if r.buttons:
        msg += " Veo los botones " + ", ".join(r.buttons[:3]) + "."
    elif r.texts:
        msg += " " + r.texts[0][:120]
    return msg


# ---- read-only --------------------------------------------------------------


@tool()
async def describe_screen() -> ToolResult:
    """Describe lo que hay en la ventana de adelante: botones, campos y texto.

    Úsalo cuando Garcia diga "¿qué dice esa ventana?", "describe la pantalla",
    "¿qué ves?". Solo lectura — nunca lee el valor de una contraseña.
    """
    r = await sv.current_screen()
    if r is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    density = _ax_density(r.texts, r.buttons, r.app, r.bounds)
    # 27.3.post: flatten the two decision flags to top-level so the system prompt
    # reads `ax_appears_thin` / `thin_by_design` directly (no `density.` drill).
    return ToolResult(
        True, {"screen": r.structured, "web_content": r.web_content, "density": density,
               "ax_appears_thin": density["ax_appears_thin"],
               "thin_by_design": density["thin_by_design"]},
        _short_summary(r), False,
    )


@tool()
async def read_window_text() -> ToolResult:
    """Lee el texto visible de la ventana enfocada ("léeme lo que dice esa ventana")."""
    r = await sv.current_screen()
    if r is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    text = " ".join(r.texts).strip() or "(no hay texto visible para leer)"
    density = _ax_density(r.texts, r.buttons, r.app, r.bounds)
    # 27.3.post: see describe_screen above.
    return ToolResult(
        True, {"screen": r.structured, "web_content": r.web_content, "density": density,
               "ax_appears_thin": density["ax_appears_thin"],
               "thin_by_design": density["thin_by_design"]},
        text[:600], False,
    )


@tool()
async def find_button(name: str, scope: str = "window") -> ToolResult:
    """Dice si existe un botón con ese nombre en la ventana de adelante.

    Úsalo cuando Garcia pregunte "¿hay un botón de X?" / "encuentra el botón Y".
    Con scope="focus" busca SOLO en el panel donde está la atención ("el botón
    Enviar de este panel"); por defecto ("window") busca en toda la ventana.
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿Qué botón busco?", False)
    root = await _scoped_root(scope)
    if root is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    _app, search = root
    labels = await asyncio.to_thread(sv.button_labels, search)
    match = _best_button(name, labels)
    if match:
        return ToolResult(True, {"button": match, "buttons": labels}, f"Sí, encontré el botón «{match}».", False)
    sims = [s for s, _ in suggest_similar(name, labels, k=3)]
    if sims:
        return ToolResult(
            True, {"buttons": labels, "suggestions": sims},
            f"No vi «{name}», pero hay: " + ", ".join(sims) + ".", False,
        )
    return ToolResult(False, {"buttons": labels}, f"No encontré un botón «{name}» en esta ventana.", False)


@tool()
async def summarize_screen(question: str = "") -> ToolResult:
    """Resume la pantalla, enfocándose en lo que responde la pregunta de Garcia.

    Para "¿qué dice ese popup?", "resúmeme lo que veo", "¿qué significa esto?".
    Lee la pantalla y la sintetiza en una respuesta corta.
    """
    r = await sv.current_screen()
    if r is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    answer = await _summarize(r.structured, (question or "").strip(), web_content=r.web_content)
    return ToolResult(True, {"screen": r.structured, "web_content": r.web_content}, answer, False)


async def _summarize(structured: str, question: str, web_content: bool = False) -> str:
    q = question or "Resume lo que hay en la pantalla."
    # When the read crossed an embedded web area, the text IS page/article/editor
    # content — tell the model to summarize that, not the surrounding chrome.
    role = (
        "Eres Emma. El texto de abajo es el CONTENIDO de la página o el archivo que "
        "Garcia está viendo (no las pestañas ni los menús alrededor). En español y en "
        "1-3 frases, resume ese contenido enfocándote en lo que responde su pregunta. "
        "Usa SOLO lo que aparece; no inventes. Nunca repitas contraseñas."
        if web_content else
        "Eres Emma. En español y en 1-2 frases, resume lo que hay en la "
        "pantalla de Garcia, enfocándote en lo que responde su pregunta. "
        "Usa SOLO lo que aparece; no inventes. Nunca repitas contraseñas."
    )
    q = redact(q)  # egress guard: strip secrets/PII (in the screen text AND the spoken
    structured = redact(structured)  # question) before any of it reaches OpenAI
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.MEMORY_REFLECTION_MODEL,
                messages=[
                    {"role": "system", "content": role},
                    {"role": "user", "content": f"Pregunta: {q}\n\nPantalla:\n{structured}"},
                ],
                temperature=0.3,
            ),
            timeout=settings.API_TIMEOUT_S,
        )
        return (completion.choices[0].message.content or "").strip() or "No pude resumir la pantalla."
    except Exception as exc:
        log.warning("summarize_screen_failed", error=str(exc))
        return structured[:300]  # degraded: hand back the structured read


# ---- destructive (click / type) — confirmation gate non-negotiable ----------


@tool(destructive=True)
async def click_button(name: str, confirmed: bool = False, scope: str = "window") -> ToolResult:
    """Hace click en un botón de la ventana de adelante. SIEMPRE confirma antes.

    Úsalo para "cierra ese diálogo", "haz click en Aceptar / Cancelar". Un click
    de UI puede autorizar pagos — por eso la confirmación es obligatoria. Con
    scope="focus" se limita al panel enfocado; por defecto ("window") toda la ventana.
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿En qué botón hago click?", False)
    root = await _scoped_root(scope)
    if root is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    app, search = root
    labels = await asyncio.to_thread(sv.button_labels, search)
    match = _best_button(name, labels)
    if not match:
        return ToolResult(False, {"buttons": labels}, f"No encontré un botón «{name}» para hacer click.", False)
    if not confirmed:
        return ToolResult(
            True, {"button": match, "app": app},
            f"Voy a hacer click en «{match}» de {app}. ¿Lo confirmo?",
            requires_confirmation=True,
        )
    ref = await asyncio.to_thread(sv.find_element, search, sv.ROLE_BUTTON, re.escape(match))
    ok = await asyncio.to_thread(sv.press_element, ref) if ref else False
    if ok:
        return ToolResult(True, {"clicked": match}, f"Listo, hice click en «{match}».", False)
    return ToolResult(False, {"button": match}, f"No pude hacer click en «{match}».", False)


@tool(destructive=True)
async def type_in_field(field_name: str, text: str, confirmed: bool = False) -> ToolResult:
    """Escribe texto en un campo de la ventana de adelante. SIEMPRE confirma antes.

    Para datos secretos, el valor lo trae Emma del Keychain; el texto NUNCA se
    dice en voz alta ni se muestra en la confirmación.
    """
    field_name = (field_name or "").strip()
    if not field_name or not text:
        return ToolResult(False, None, "Necesito el campo y el texto.", False)
    fw = await asyncio.to_thread(sv._frontmost_window_element)
    if fw is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    app, win = fw
    if not confirmed:
        # Never echo `text` — it could be a secret fetched from Keychain.
        return ToolResult(
            True, {"field": field_name, "app": app},
            f"Voy a escribir en el campo «{field_name}» de {app}. ¿Lo confirmo?",
            requires_confirmation=True,
        )
    ref = await asyncio.to_thread(sv.find_element, win, None, re.escape(field_name))
    if ref is None:
        return ToolResult(False, {"field": field_name}, f"No encontré el campo «{field_name}».", False)
    ok = await asyncio.to_thread(sv.set_element_value, ref, text)
    if ok:
        return ToolResult(True, {"field": field_name}, f"Listo, escribí en «{field_name}».", False)
    return ToolResult(False, {"field": field_name}, f"No pude escribir en «{field_name}».", False)


# ---- pane / focus introspection (27.1) --------------------------------------


async def _scoped_root(scope: str) -> tuple[str, object] | None:
    """(app_name, search_root). scope="focus" narrows to the focused pane; any
    other value keeps 27's whole-window behavior. Falls back to the window when
    the app exposes no focused pane (sparse AX tree)."""
    fw = await asyncio.to_thread(sv._frontmost_window_element)
    if fw is None:
        return None
    app, win = fw
    if scope == "focus":
        pane = await asyncio.to_thread(sv.focused_pane_element)
        if pane is not None:
            return app, pane
    return app, win


def _pane_data(p: sv.PaneInfo) -> dict[str, Any]:
    return {
        "app": p.app, "label": p.label, "role": p.role,
        "role_description": p.role_description, "identifier": p.identifier,
        "title": p.title, "position": p.position, "focused_role": p.focused_role,
        "focused_role_description": p.focused_role_description,
        "ancestors": p.ancestors, "snippet": p.snippet[:600],
        "web_content": p.web_content,
    }


def _pane_block(p: sv.PaneInfo) -> str:
    """A compact text block of JUST the pane — never the whole window."""
    lines = [f"App: {p.app}"]
    if p.label:
        lines.append(f"Panel: {p.label}")
    if p.position:
        lines.append(f"Posición: {p.position}")
    if p.role_description:
        lines.append(f"Tipo (AX): {p.role_description}")
    if p.snippet:
        lines.append(f"Contenido:\n{p.snippet}")
    return "\n".join(lines)


def _loc_phrase(position: str) -> str:
    if not position or position == "centro":
        return ""
    if position in ("izquierda", "derecha"):
        return f"a la {position}"
    return position  # "arriba", "abajo", "abajo a la izquierda", …


def _pane_phrase(p: sv.PaneInfo) -> str:
    loc = _loc_phrase(p.position)
    if p.label:
        return f"Estás en «{p.label}»{f' ({loc})' if loc else ''}."
    if loc:
        return f"Estás en un panel {loc} de la ventana de {p.app}."
    if p.focused_role_description:
        return f"Estás en {p.focused_role_description} de {p.app}."
    return f"No logro distinguir el panel exacto en {p.app}; te puedo leer la ventana completa."


@tool()
async def where_am_i() -> ToolResult:
    """Dice en qué panel o región de la ventana está puesta la atención de Garcia.

    Para "¿dónde estoy?", "¿en qué panel estoy?", "¿qué región es esta?".
    """
    pane = await asyncio.to_thread(sv.focused_pane)
    if pane is None:
        r = await sv.current_screen()  # app exposes no focus → degrade to the window
        if r is None:
            return ToolResult(False, None, _NO_WINDOW, False)
        return ToolResult(
            True, {"screen": r.structured, "pane": None},
            f"No logro distinguir el panel exacto en {r.app}, pero te leo la ventana «{r.title}».", False,
        )
    return ToolResult(True, {"pane": _pane_data(pane)}, _pane_phrase(pane), False)


@tool()
async def window_layout() -> ToolResult:
    """Lista los paneles/regiones de la ventana de adelante y dónde están.

    Para "¿qué tengo en esta ventana?", "¿qué paneles hay?".
    """
    panes = await asyncio.to_thread(sv.window_panes)
    if not panes:
        return ToolResult(False, None, "No pude leer la distribución de esta ventana.", False)
    names = [p["label"] for p in panes if p.get("label")][:8]
    msg = ("En esta ventana veo: " + ", ".join(names) + ".") if names else "Veo regiones, pero sin nombres claros."
    return ToolResult(True, {"panes": panes}, msg, False)


@tool()
async def read_pane_text() -> ToolResult:
    """Lee solo el texto del panel donde está la atención (no toda la ventana).

    Para "léeme este panel", "¿qué dice este panel?", "léeme la terminal".
    """
    pane = await asyncio.to_thread(sv.focused_pane)
    if pane is None or not pane.snippet:
        return ToolResult(False, None, "No logro identificar el panel para leerlo.", False)
    snippet_lines = pane.snippet.splitlines()
    density = _ax_density(snippet_lines, [], pane.app, pane.bounds)
    # 27.3.post: see describe_screen — flatten thin flags to top level.
    return ToolResult(
        True, {"pane": _pane_data(pane), "density": density,
               "ax_appears_thin": density["ax_appears_thin"],
               "thin_by_design": density["thin_by_design"]},
        pane.snippet[:600], False,
    )


@tool()
async def summarize_pane(question: str = "") -> ToolResult:
    """Resume SOLO el panel enfocado (no toda la ventana). Respuesta más corta y precisa.

    Para "resúmeme la terminal", "¿qué dice este panel?", "¿qué pasó en este chat?".
    """
    pane = await asyncio.to_thread(sv.focused_pane)
    if pane is None or not pane.snippet:
        return ToolResult(False, None, "No logro identificar el panel para resumirlo.", False)
    answer = await _summarize(_pane_block(pane), (question or "").strip(), web_content=pane.web_content)
    return ToolResult(True, {"pane": _pane_data(pane)}, answer, False)
