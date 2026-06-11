"""Voice-facing screen-vision tools (Prompt 27).

Read-only by default (describe / read / find / summarize); click + type are
``destructive=True`` and go through the two-phase confirmation gate. All AX work
lives in :mod:`core.screen_vision`; these tools shape it into Spanish results and
never read or speak secret-field values.
"""

from __future__ import annotations

import asyncio
import re

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core import screen_vision as sv
from tools.base import ToolResult, tool
from tools.disambiguation import suggest_similar

log = structlog.get_logger("emma.tools.screen_vision")

_NO_WINDOW = "No veo una ventana enfocada ahora mismo."


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
    return ToolResult(True, {"screen": r.structured}, _short_summary(r), False)


@tool()
async def read_window_text() -> ToolResult:
    """Lee el texto visible de la ventana enfocada ("léeme lo que dice esa ventana")."""
    r = await sv.current_screen()
    if r is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    text = " ".join(r.texts).strip() or "(no hay texto visible para leer)"
    return ToolResult(True, {"screen": r.structured}, text[:600], False)


@tool()
async def find_button(name: str) -> ToolResult:
    """Dice si existe un botón con ese nombre en la ventana de adelante.

    Úsalo cuando Garcia pregunte "¿hay un botón de X?" / "encuentra el botón Y".
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿Qué botón busco?", False)
    fw = await asyncio.to_thread(sv._frontmost_window_element)
    if fw is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    _app, win = fw
    labels = await asyncio.to_thread(sv.button_labels, win)
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
    answer = await _summarize(r.structured, (question or "").strip())
    return ToolResult(True, {"screen": r.structured}, answer, False)


async def _summarize(structured: str, question: str) -> str:
    q = question or "Resume lo que hay en la pantalla."
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        completion = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.MEMORY_REFLECTION_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Eres Emma. En español y en 1-2 frases, resume lo que hay en la "
                            "pantalla de Garcia, enfocándote en lo que responde su pregunta. "
                            "Usa SOLO lo que aparece; no inventes. Nunca repitas contraseñas."
                        ),
                    },
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
async def click_button(name: str, confirmed: bool = False) -> ToolResult:
    """Hace click en un botón de la ventana de adelante. SIEMPRE confirma antes.

    Úsalo para "cierra ese diálogo", "haz click en Aceptar / Cancelar". Un click
    de UI puede autorizar pagos — por eso la confirmación es obligatoria.
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿En qué botón hago click?", False)
    fw = await asyncio.to_thread(sv._frontmost_window_element)
    if fw is None:
        return ToolResult(False, None, _NO_WINDOW, False)
    app, win = fw
    labels = await asyncio.to_thread(sv.button_labels, win)
    match = _best_button(name, labels)
    if not match:
        return ToolResult(False, {"buttons": labels}, f"No encontré un botón «{name}» para hacer click.", False)
    if not confirmed:
        return ToolResult(
            True, {"button": match, "app": app},
            f"Voy a hacer click en «{match}» de {app}. ¿Lo confirmo?",
            requires_confirmation=True,
        )
    ref = await asyncio.to_thread(sv.find_element, win, sv.ROLE_BUTTON, re.escape(match))
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
