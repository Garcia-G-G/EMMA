"""LAUNCH-12: per-tool routing guidance moved out of the always-on prompt.

The rules now travel in each tool's description (tools/guidance.py). These tests
prove they still reach the model, and that they left the prompt (otherwise every
turn would pay for them twice).
"""

from __future__ import annotations

import pytest

from core import conversation
from tools import registry
from tools.guidance import GUIDANCE
from tools.registry import _spec

# Every quoted trigger phrase that was in the removed prompt sections (extracted
# from the pre-LAUNCH-12 prompt). The model matches on these strings, so they
# must survive the move verbatim.
MOVED_PHRASES = [
    "20°C en F",
    "5 km en millas",
    "Abre el canal Y en Slack",
    "Agrega X a <título>",
    "Agrega a mi página de ideas: «X»",
    "Apágate",
    "Cierra ese diálogo",
    "Cierra las de YouTube",
    "Cierra las duplicadas",
    "Corre `python -m emma.x_setup` una vez en tu Terminal, autoriza a Emma, y listo.",
    "Corre el collection de health en Postman",
    "Crea issue en Jira en el proyecto ENG: «X»",
    "Crea issue en Linear: «X»",
    "Cuánto es 100 USD en MXN",
    "Deshaz la de las 3",
    "Deshaz lo último",
    "Duérmete",
    "Ejecuta select … en mi base X",
    "El repo de <alguien-más>",
    "Emma, abre la conexión X",
    "Emma, agrega Y al inicio de X",
    "Emma, en la terminal de Cursor corre X",
    "Emma, en mi archivo X agrega Y al final",
    "Emma, esta es mi voz",
    "Emma, reemplaza A por B en X",
    "Emma, sobrescribe X con esto: …",
    "Escribe mi password en el campo",
    "Esta es mi voz",
    "Guarda que el cumpleaños de X es Y",
    "Hey Emma",
    "La última nota",
    "Libera espacio",
    "Léeme este panel",
    "Mi <thing>",
    "Mis repos",
    "No me interrumpas N min",
    "No tengo permiso para publicar en X",
    "Recarga las herramientas",
    "Reiníciate",
    "Renombra los X a Y",
    "Resumen de la semana / del día / del mes",
    "Según OpenAI Blog y The Verge, …",
    "Si X pasa, haz Y",
    "Timer de N minutos",
    "Tira una moneda",
    "abre <app>",
    "abre google y busca",
    "aprende mi voz",
    "apunta esto en la nota de antes",
    "busca X y clónalo",
    "busca el PDF de Y",
    "buscar un repo",
    "channel",
    "clónalo en mi IDE",
    "connection",
    "crea una nueva <título>",
    "dame una contraseña",
    "deja de escuchar del todo",
    "descansa N minutos",
    "describe la pantalla",
    "describe lo que ves",
    "deshazlo",
    "diagnóstico",
    "echa para atrás",
    "editor",
    "el botón X de este panel",
    "el repo que hice de X",
    "el árbol de accesibilidad no me dijo mucho, déjame mirar la pantalla.",
    "elige por mí",
    "encuentra el botón Cancelar",
    "enrolla mi voz",
    "esa nota que acabo de crear",
    "escribe X en la terminal",
    "estoy enojado",
    "haz X y Y y agrega Z",
    "haz click en Aceptar",
    "hazme default Chrome",
    "incluyendo Google",
    "investígame Y",
    "le pedí al agente, te voy abriendo cosas.",
    "listo, clonando X",
    "listo, lo cambié y lo abrí en Cursor",
    "los repos que tengo",
    "léeme ese PDF",
    "léeme la pantalla",
    "léeme la terminal",
    "manda en Discord al canal X: <texto>",
    "mi <name>",
    "mi github",
    "mi última nota",
    "mira la pantalla",
    "mis repos",
    "miércoles",
    "mándale a Juan en WhatsApp: <texto>",
    "no encontré… ¿la creo nueva?",
    "no escuches un rato",
    "olvida la voz de X",
    "ponme 10 minutos",
    "prefiero Zed",
    "publica en LinkedIn: <texto>",
    "publica en X: <texto>",
    "python -m emma.setup --only <servicio>",
    "recuerda que X usa el esquema Y",
    "recuerda que...",
    "recárgate",
    "restart",
    "resume lo último de Z",
    "resúmeme el panel",
    "saca un dado",
    "shut down",
    "silencio",
    "slack",
    "sí, hazlo",
    "the last note",
    "todas las ocurrencias",
    "toma una captura",
    "tuitea: <texto>",
    "usa VS Code",
    "voy a agregar 3 líneas al final de utils.py — ¿confirmas?",
    "vuelve a arrancar",
    "youtube",
    "¿Cursor, VS Code o Zed?",
    "¿Cuántas pestañas tengo?",
    "¿Cómo estás?",
    "¿De qué va esta URL?",
    "¿Dónde estoy?",
    "¿Dónde está X?",
    "¿Hay un botón X?",
    "¿Qué está ocupando lugar?",
    "¿Qué hiciste ayer?",
    "¿Qué hora es?",
    "¿Qué pasó con X?",
    "¿Qué tengo en esta ventana?",
    "¿Qué tienes pendiente?",
    "¿Qué veo?",
    "¿clono el de X?",
    "¿cuál es tu usuario de GitHub?",
    "¿cómo va?",
    "¿de qué trata el artículo?",
    "¿dónde guardé Z?",
    "¿en qué panel estoy?",
    "¿en ~/repos/myapp, y en una rama nueva?",
    "¿funcionas bien?",
    "¿la de \\",
    "¿para cuándo?",
    "¿por qué está lleno el disco?",
    "¿quién cumple hoy / esta semana?",
    "¿quién está hablando?",
    "¿qué dice esa imagen?",
    "¿qué dice esa ventana?",
    "¿qué dice este panel?",
    "¿qué día es?",
    "¿qué hace este código?",
    "¿qué hiciste el martes?",
    "¿qué hiciste hoy?",
    "¿qué paneles hay?",
    "¿qué quedó condicional?",
    "¿qué timers tengo?",
    "¿soy yo?",
    "Últimas de <feed>",
    "última",
]

# Sections that left the prompt. Rules true of every turn stayed.
MOVED_SECTIONS = [
    "# Defaults & apps",
    "# Screen vision",
    "# Action history + undo",
    "# Self-diagnostics",
    "# Life utilities",
    "# File operations",
    "# Workflows + conditionals",
    "# Investigación",
    "# Integraciones",
    "# Speaker ID",
    "# Repo cloning flow",
    "# Knowledge dictionary",
    "# Smart note append",
    "# App control layering",
    "# App URL schemes",
    "# In-app resources",
    "# Browser tabs",
    "# Terminal in IDE",
    "# Editing files",
    "# Coding agent delegation",
    "# Social platforms",
]


def _all_descriptions() -> dict[str, str]:
    registry.available_tools()  # triggers discovery
    return {e.name: _spec(e)["function"]["description"] for e in registry.get_registry().values()}


def test_every_guidance_key_is_a_registered_tool() -> None:
    names = set(_all_descriptions())
    orphans = sorted(set(GUIDANCE) - names)
    assert not orphans, f"guidance for tools that don't exist (renamed?): {orphans}"


def test_guidance_reaches_the_spec_untruncated() -> None:
    descs = _all_descriptions()
    for name, guide in GUIDANCE.items():
        assert descs[name].endswith(guide), name


def test_guidance_reaches_the_realtime_payload() -> None:
    rt = conversation._adapt_tool_specs_for_realtime(registry.openai_tool_specs())
    by = {t["name"]: t["description"] for t in rt}
    for name, guide in GUIDANCE.items():
        if name in by:
            assert guide in by[name], name


def test_every_moved_trigger_phrase_reaches_a_tool_description() -> None:
    blob = "\n".join(_all_descriptions().values())
    missing = [p for p in MOVED_PHRASES if p not in blob]
    assert not missing, missing


@pytest.mark.asyncio
async def test_moved_sections_left_the_prompt() -> None:
    prompt = await conversation._build_instructions()
    still = [s for s in MOVED_SECTIONS if s in prompt]
    assert not still, f"moved to tools/guidance.py but still billed every turn: {still}"


@pytest.mark.asyncio
async def test_always_on_prompt_stays_small() -> None:
    # 2,662 o200k tokens measured at LAUNCH-12 (11,105 chars ≈ 4.17 chars/token).
    # 12,500 chars ≈ 3k tokens: the spec's ceiling. A tokenizer is not a project
    # dependency, so this guards on characters; LAUNCH-12-VERIFY.md has the
    # tokenized numbers.
    prompt = await conversation._build_instructions()
    assert len(prompt) <= 12_500, len(prompt)
