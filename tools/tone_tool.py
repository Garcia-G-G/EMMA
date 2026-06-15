"""Voice-driven tone control (Prompt 35).

Lets Garcia steer Emma's spoken style directly ("háblame más serio", "relájate",
"tono normal"). Writes a directive into the runtime style state that is appended
to the system prompt on the next session build. Complements the automatic
affect detection in ``core/affect.py`` — this is the explicit override.
"""

from __future__ import annotations

from core import runtime
from tools.base import ToolResult, tool

# Substring -> directive. Matched against the lowercased style argument so
# "más serio", "ponte serio", "serio por favor" all land on the same preset.
_PRESETS: list[tuple[tuple[str, ...], str]] = [
    (("neutral", "normal", "como siempre", "quita", "default"), ""),  # clears the override
    (("serio", "formal", "profesional"),
     "Mantén un tono serio y profesional: frases completas, sin bromas ni diminutivos."),
    (("relaj", "casual", "tranqui", "suelt", "amig"),
     "Habla relajada y casual, como con un amigo de confianza."),
    (("anima", "alegr", "entusias", "energía", "energia"),
     "Habla con energía y entusiasmo, tono alegre y cercano."),
    (("breve", "cort", "concis", "al grano", "rápid", "rapid"),
     "Sé muy concisa: responde en una sola frase, sin preámbulo."),
    (("cariñ", "carin", "dulce", "suave", "tierna"),
     "Usa un tono cálido y cariñoso, cercano y amable."),
]


@tool()
async def set_conversation_tone(style: str) -> ToolResult:
    """Ajusta el tono con el que Emma te habla.

    Ejemplos: "háblame más serio", "relájate", "ponte animada", "sé breve",
    "tono normal" (quita el ajuste). El cambio aplica a partir de la siguiente
    respuesta.
    """
    s = (style or "").strip().lower()
    if not s:
        return ToolResult(False, None, "¿Qué tono quieres? Serio, relajado, animado o normal.", False)

    for keys, hint in _PRESETS:
        if any(k in s for k in keys):
            runtime.set_style_hint(hint)
            if not hint:
                return ToolResult(True, {"style": "neutral"}, "Listo, vuelvo a mi tono de siempre.", False)
            return ToolResult(True, {"style": s}, "Hecho, ajusto el tono.", False)

    # Unknown phrasing: take it literally as the directive rather than failing.
    runtime.set_style_hint(f"Adapta tu tono a esto: {style.strip()}.")
    return ToolResult(True, {"style": s}, "Va, ajusto el tono.", False)
