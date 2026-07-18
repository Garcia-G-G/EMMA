"""Visual screen reading via on-device OCR (Apple Vision).

The visual complement to the AX-based screen tools: when the answer is in an
image, a canvas, a PDF, or an app with no accessibility tree, this captures a
screenshot and OCRs it locally. Nothing leaves the Mac — the screenshot is OCR'd
on-device and deleted; only the recognized text (optionally summarized, exactly
like describe_screen) is used.
"""

from __future__ import annotations

import structlog

from core import visual_screen as vis
from tools.base import ToolResult, tool
from tools.screen_vision_tool import _summarize

log = structlog.get_logger("emma.tools.visual_screen")

_FAILED = ("No pude leer la pantalla con visión. Revisa el permiso de Grabación de "
           "pantalla en Ajustes → Privacidad y seguridad.")


@tool(returns_untrusted_content=True)
async def look_at_screen(question: str = "") -> ToolResult:
    """Lee la pantalla con visión (captura + OCR local) — texto en imágenes, PDFs,
    apps sin árbol de accesibilidad, etc.

    Úsalo cuando describe_screen no alcance ("no veo el contenido"), o cuando
    Garcia diga "mira la pantalla", "toma una captura y léela", "¿qué dice esa
    imagen?". Todo es local: la captura se procesa en el Mac y se borra; nunca
    se sube a la nube. Si pasas `question`, responde a esa pregunta sobre lo leído.
    """
    r = await vis.read_screen()
    if r is None:
        return ToolResult(False, None, _FAILED, False)
    if not r.text:
        return ToolResult(
            True, {"app": r.app, "line_count": 0, "scope": r.scope},
            f"Capturé la {('ventana' if r.scope == 'window' else 'pantalla')} de {r.app} "
            "pero no encontré texto legible.", False,
        )
    data = {"app": r.app, "text": r.text, "line_count": r.line_count, "scope": r.scope}
    q = (question or "").strip()
    if q:
        answer = await _summarize(r.text, q, web_content=True)
        return ToolResult(True, data, answer, False)
    return ToolResult(True, data, r.text[:600], False)
