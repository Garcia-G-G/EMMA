"""The brain's own retrieval text: ``phrasebook.toml`` (BRAIN-1.5 Part 1).

Both retrievers used to index text written for an LLM to read (docstrings and
routing guidance, 14 of them English-only), so they couldn't hear how people
talk. The phrasebook is how people talk to an assistant, per tool, in neutral
Spanish and English, plus speech-recognition manglings. No LLM ever reads it.

* ``bm25_text(tool)`` — every phrasing with placeholders stripped, plus the
  tool name split on underscores: the document the brain's BM25 indexes.
* ``expansions(tool)`` — each phrasing with its ``{placeholders}`` filled with
  2 representative values, for the kNN index. Fill values are generic and
  deliberately NOT taken from the labelled set (the 140 are the exam).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).with_name("phrasebook.toml")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
FILLS_PER_PHRASE = 2

# Fill values by parameter name; ``_TOOL_FILLS`` overrides where one name means
# different things ("name" is an app, a page, a playlist, a button, a person…).
_FILLS: dict[str, tuple[str, ...]] = {
    "name": ("Safari", "Finder"), "app": ("Safari", "Notion"), "ide": ("Zed", "Visual Studio Code"),
    "percent": ("30", "75"), "query": ("noticias de hoy", "machine learning"),
    "url": ("wikipedia.org", "duckduckgo.com"), "title": ("Ideas", "Viaje"),
    "text": ("revisar el correo", "comprar huevos"), "body": ("ideas para el viaje", "lista de pendientes"),
    "command": ("git status", "ls -la"), "path": ("notas.txt", "presupuesto.xlsx"),
    "content": ("hola mundo", "versión final"), "task": ("arreglar el login", "agregar pruebas"),
    "intent": ("comprar boletos", "llenar el formulario"), "keys": ("Cmd+T", "Cmd+Shift+P"),
    "target": ("prod-db", "soporte"), "minutes": ("10", "30"), "duration_min": ("5", "25"),
    "label": ("té", "pasta"), "timezone": ("GMT", "UTC+2"), "when": ("el lunes", "la semana pasada"),
    "value": ("sam-dev", "Sam Rivera"), "relation": ("hermana", "jefa"), "email": ("sam@example.org",
    "rivera@example.net"), "url_scheme": ("notion://", "slack://"), "key": ("SLA", "KPI"),
    "expansion": ("acuerdo de nivel de servicio", "indicador clave"), "wrong": ("clod", "nil"),
    "right": ("Claude", "Nill"), "canonical": ("Kubernetes", "Postgres"), "date": ("3 de mayo", "12 de octubre"),
    "source_title": ("Borrador", "Notas sueltas"), "target_title": ("Proyecto", "Archivo"),
    "app_name": ("Brave", "Zed"), "category": ("programar", "navegar"), "creator_name": ("Veritasium", "Kurzgesagt"),
}
_TOOL_FILLS: dict[tuple[str, str], tuple[str, ...]] = {
    ("open_my_page", "name"): ("linkedin", "portafolio"),
    ("play_playlist", "name"): ("Focus", "Chill"),
    ("play_track", "query"): ("Shakira", "jazz"),
    ("search_github", "query"): ("fastapi", "react"),
    ("find_file", "query"): ("contrato", "factura"),
    ("find_button", "name"): ("Guardar", "Siguiente"),
    ("click_button", "name"): ("Guardar", "Siguiente"),
    ("remember_connection", "name"): ("prod-db", "staging"),
    ("remember_page", "name"): ("blog", "portafolio"),
    ("remember_app", "name"): ("Notion", "Raycast"),
    ("remember_contact", "name"): ("Sam", "Rivera"),
    ("birthday_remember", "name"): ("Sam", "Rivera"),
    ("add_reminder", "title"): ("pagar la luz", "llamar al banco"),
    ("remember_fact", "content"): ("me gusta el café", "trabajo los sábados"),
    ("recall_facts", "query"): ("mi trabajo", "mis gustos"),
    ("remember_secret", "label"): ("banco", "correo"),
    ("recall_secret", "label"): ("banco", "correo"),
    ("start_timer", "label"): ("té", "pasta"),
}


@dataclass(frozen=True)
class Phrasing:
    tool: str
    lang: str  # "es" | "en" | "stt"
    template: str


@lru_cache(maxsize=1)
def load(path: Path = PATH) -> dict[str, tuple[Phrasing, ...]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[Phrasing, ...]] = {}
    for tool, langs in data.items():
        rows = [Phrasing(tool, lang, t) for lang in ("es", "en", "stt") for t in langs.get(lang, [])]
        out[tool] = tuple(rows)
    return out


def placeholders(template: str) -> list[str]:
    return _PLACEHOLDER.findall(template)


def strip(template: str) -> str:
    return re.sub(r"\s+", " ", _PLACEHOLDER.sub(" ", template)).strip()


def bm25_text(tool: str, book: dict[str, tuple[Phrasing, ...]] | None = None) -> str:
    rows = (book or load()).get(tool, ())
    return " ".join([tool.replace("_", " "), *(strip(p.template) for p in rows)])


def fill_values(tool: str, param: str) -> tuple[str, ...]:
    return _TOOL_FILLS.get((tool, param)) or _FILLS.get(param) or ("algo", "eso otro")


def expand(p: Phrasing) -> list[str]:
    """The phrasing with its placeholders filled, FILLS_PER_PHRASE ways (zipped, not crossed)."""
    names = placeholders(p.template)
    if not names:
        return [p.template]
    out = []
    for i in range(FILLS_PER_PHRASE):
        text = p.template
        for n in names:
            vals = fill_values(p.tool, n)
            text = text.replace("{" + n + "}", vals[i % len(vals)], 1)
        out.append(text)
    return out


def expansions(tool: str, book: dict[str, tuple[Phrasing, ...]] | None = None) -> list[tuple[str, str]]:
    """(lang, filled text) for every phrasing of ``tool``."""
    return [(p.lang, t) for p in (book or load()).get(tool, ()) for t in expand(p)]
