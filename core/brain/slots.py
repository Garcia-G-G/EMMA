"""Closed-vocabulary slot resolution (BRAIN-1 Part 4, "L2").

Fills a tool's arguments WITHOUT a model, from finite sets Emma already knows:

| kind          | source                                   | matcher                       |
|---------------|------------------------------------------|-------------------------------|
| app / ide     | installed apps (host adapter, plain list) | alias table, then fuzzy       |
| page          | dictionary [pages] keys + titles         | fuzzy                         |
| note/playlist/connection/contact | the tool's own listing    | fuzzy                         |
| number        | digits + ES/EN number words              | deterministic                 |
| url / scheme  | regex; "punto com" -> ".com"             | deterministic                 |
| profile_field / app_category | closed phrase tables      | deterministic                 |
| residue       | what's left after spans + the tool's frame | only for ONE free-text field |

Every ``SlotValue`` records ``source``, ``confidence`` and ``raw_span``. A fuzzy
match below the floor leaves the slot UNRESOLVED — the gate defers. Never a
guess. No time parsing here: that is L3 (BRAIN-2).

A tool's FRAME is the closed set of command words that phrase it ("borra la
nota", "pon música de"). They are consumed, not kept, so the residue is the
content and anything else is reported as ``leftover`` for the gate. The frames
were written knowing the labelled set; whether they generalize is what shadow
mode measures.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz

from core.brain.decision import SlotScalar, SlotValue
from core.brain.normalize import Normalized, fold

SlotKind = Literal[
    "app", "ide", "page", "note", "playlist", "connection", "contact", "number", "url",
    "url_scheme", "profile_field", "app_category", "connection_or_residue", "residue",
]

DEFAULT_FLOOR = 0.85
_MAX_GRAM = 4


@dataclass(frozen=True)
class Vocab:
    """The finite sets, as plain data. Built by ``core.brain_host``; empty = unknown."""

    apps: tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)  # folded alias -> canonical
    pages: Mapping[str, str] = field(default_factory=dict)  # key -> title
    notes: tuple[str, ...] = ()
    playlists: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    contacts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # name -> aliases


@dataclass(frozen=True)
class ToolSlots:
    slots: Mapping[str, SlotKind] = field(default_factory=dict)  # param -> kind
    frames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    slots: dict[str, SlotValue]
    missing: tuple[str, ...]  # required params with no value above the floor
    leftover: tuple[str, ...]  # content words nothing explained
    residue_param: str | None


# ---- closed tables ----------------------------------------------------------------

# macOS apps' Spanish display names -> the app (neutral; no regional forms).
_APP_LOCAL_NAMES = {"musica": "Music", "notas": "Notes", "calendario": "Calendar",
                    "mensajes": "Messages", "correo": "Mail", "terminal": "Terminal",
                    "recordatorios": "Reminders", "fotos": "Photos"}
_IDES = ("Cursor", "Visual Studio Code", "Code", "Zed", "Xcode", "PyCharm", "IntelliJ IDEA",
         "Windsurf", "Sublime Text")
_PROFILE_FIELDS = {
    "usuario de github": "github_username", "github": "github_username",
    "github username": "github_username", "linkedin": "linkedin",
    "nombre completo": "full_name", "full name": "full_name", "me llamo": "display_name",
    "mi nombre": "display_name", "my name": "display_name", "sitio web": "website",
    "pagina web": "website", "website": "website", "idioma": "preferred_lang",
}
_APP_CATEGORIES = {
    "programar": "ide", "programo": "ide", "codigo": "ide", "editor": "ide", "ide": "ide",
    "coding": "ide", "code": "ide", "terminal": "terminal", "consola": "terminal",
    "musica": "music", "music": "music", "navegador": "browser", "navegar": "browser",
    "browser": "browser",
}
_UNITS = {
    "cero": 0, "uno": 1, "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "zero": 0, "one": 1, "two": 2,
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_TENS = {
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
    "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_HALF = {"mitad": 50, "half": 50}
_PERCENT_TAIL = (("por", "ciento"), ("porciento",), ("percent",), ("por", "cien"))

STOP: frozenset[str] = frozenset(
    ["de", "del", "el", "la", "los", "las", "un", "una", "unos", "unas", "a", "al", "en", "que", "y", "o", "mi", "mis", "tu", "tus", "me", "te", "se", "lo", "le", "por", "para", "con", "es", "su", "sus", "esta", "este", "esa", "ese", "esto", "eso", "the", "an", "to", "of", "in", "on", "my", "for", "and", "is", "it", "this", "that", "your", "at"]
)
FILLER: frozenset[str] = frozenset(
    ["puedes", "podrias", "porfa", "favor", "ahora", "ya", "dale", "ok", "okay", "bueno", "pues", "oye", "please", "can", "you", "could", "now", "just", "tambien", "solo", "espera", "primero", "perfecto", "ademas", "poquito", "ahi", "va", "si"]
)

# Well-known sites by spoken name -> domain, for "abre yutub" (no "punto com"
# said). Closed and international; a name resolves through the STT alias table
# ("yutub" -> YouTube) or by fuzzy match above the floor. Used only when no
# literal URL was spoken.
SITES: dict[str, str] = {
    "YouTube": "youtube.com", "GitHub": "github.com", "Google": "google.com",
    "Gmail": "mail.google.com", "Wikipedia": "wikipedia.org", "LinkedIn": "linkedin.com",
    "X": "x.com", "Twitter": "x.com", "Reddit": "reddit.com", "Netflix": "netflix.com",
}
_URL = re.compile(
    r"(?<![\w@])((?:[a-z0-9-]+\s*(?:\.|\bpunto\b|\bdot\b)\s*)+"
    r"(?:com|org|net|io|mx|app|dev|ai|co|es|gov|edu|me|tv)\b(?:/[\w./-]*)?)"
)
_SCHEME = re.compile(r"\b([a-z][a-z0-9+.-]*)://")
_WORDTOK = re.compile(r"[\w'\u2019]+", re.UNICODE)  # same word rule as normalize


# ---- per-tool frames ------------------------------------------------------------------


def _t(slots: Mapping[str, SlotKind] | None = None, *frames: str) -> ToolSlots:
    return ToolSlots(dict(slots or {}), tuple(fold(f) for f in frames))


TOOLS: dict[str, ToolSlots] = {
    # -- L2: arguments from closed sets -----------------------------------------------
    "set_volume": _t({"percent": "number"}, "sube el volumen a", "baja el volumen a",
                     "pon el volumen a", "pon el volumen al", "sube el volumen",
                     "baja el volumen", "volumen al", "volumen a", "volumen",
                     "set the volume to", "volume to", "volume", "sube", "baja"),
    "set_brightness": _t({"percent": "number"}, "pon el brillo al", "pon el brillo a",
                         "sube el brillo", "baja el brillo", "brillo al", "brillo a", "brillo",
                         "set the brightness to", "brightness to", "brightness"),
    "open_application": _t({"name": "app"}, "una ventana nueva de", "ventana nueva de",
                           "nueva ventana de", "abreme", "abre", "abrir", "inicia", "open",
                           "launch", "start"),
    "open_url": _t({"url": "url"}, "abre la pagina", "abre", "ve a", "open", "go to"),
    "open_my_page": _t({"name": "page"}, "en el navegador", "abre mi", "abre", "open my",
                       "open"),
    "play_track": _t({"query": "residue", "app": "app"}, "pon una cancion de",
                     "pon musica de", "pon una cancion", "pon musica", "poner musica de",
                     "poner", "reproduce", "toca", "pon", "cancion de", "musica de",
                     "play", "song by", "music by"),
    "play_playlist": _t({"name": "playlist", "app": "app"}, "pon la playlist",
                        "pon la lista", "pon el playlist", "playlist", "pon", "play the playlist",
                        "play"),
    "latest_video_from_creator": _t({"creator_name": "residue"},
                                    "abre el video mas reciente de", "el video mas reciente de",
                                    "abre el ultimo video de", "el ultimo video de",
                                    "video mas reciente de", "ultimo video de", "abre",
                                    "open the latest video from", "latest video from"),
    "search_github": _t({"query": "residue"}, "que repositorios tiene el usuario",
                        "ensename los repos de", "busca un repo de", "busca el repo de",
                        "busca repos de", "repositorios de", "repos de", "repo de", "busca",
                        "en github", "decirme", "dime", "search github for"),
    "append_to_note": _t({"title": "note", "text": "residue"}, "a mi nota", "a la nota",
                         "en mi nota", "en la nota", "agrega", "anade", "nota",
                         "add to my note", "add"),
    "read_note": _t({"title": "note"}, "leeme la nota", "lee la nota", "leeme", "lee",
                    "la nota", "nota", "read the note", "read my note", "read"),
    "delete_note": _t({"title": "note"}, "borra mi nota", "borra la nota", "elimina la nota",
                      "borra", "elimina", "mi nota", "la nota", "nota", "delete the note",
                      "delete"),
    "ide_terminal_send": _t({"text": "residue", "ide": "ide"}, "en la terminal de",
                            "en la terminal", "terminal de", "escribe", "manda", "envia",
                            "in the terminal", "type"),
    "run_command": _t({"command": "residue"}, "corre el comando", "ejecuta el comando",
                      "el comando", "comando", "corre", "ejecuta", "run the command", "run"),
    "open_in_app": _t({"target": "connection_or_residue", "app": "app"},
                      "abre la conexion", "abre el canal", "crea una tarea en",
                      "abre la nota", "abre", "la conexion", "el canal", "open"),
    "remember_fact": _t({"content": "residue"}, "recuerdame que", "recuerda que",
                        "acuerdate que", "guarda que", "remember that", "save that"),
    "remember_connection": _t({"name": "residue", "app": "app"}, "recuerda la conexion",
                              "guarda la conexion", "la conexion", "remember the connection"),
    "remember_page": _t({"name": "residue", "url": "url"}, "recuerda que", "guarda que",
                        "remember that"),
    "remember_user_profile": _t({"field": "profile_field", "value": "residue"}),
    "set_preferred_app": _t({"category": "app_category", "app_name": "app"},
                            "recuerda que", "prefiero", "prefer", "remember that"),
    "remember_app": _t({"name": "app", "url_scheme": "url_scheme"}, "recuerda que",
                       "usa el esquema", "el esquema", "usa", "remember that", "uses"),
    "find_button": _t({"name": "residue"}, "hay un boton de", "hay un boton", "boton de",
                      "boton", "is there a button", "button"),
    "click_button": _t({"name": "residue"}, "haz click en", "haz clic en", "dale click a",
                       "click en", "clic en", "click on", "click"),
    # -- L1: nothing to fill; the frame says what counts as "explained" ------------------
    "my_repos": _t(None, "ensename mis repos de github", "mis repos de github", "mis repos",
                   "ensename", "repos", "my repos"),
    "current_time": _t(None, "que hora es", "dime la hora exacta", "dime la hora", "la hora",
                       "hora", "exacta", "what time is it", "time"),
    "current_datetime_speak": _t(None, "que hora es", "dime la hora", "la hora", "hora",
                                 "fecha", "what time is it"),
    "recall_facts": _t(None, "que sabes de mi", "what do you know about me"),
    "today_events": _t(None, "que tengo hoy en el calendario", "que tengo hoy", "calendario",
                       "hoy", "what do i have today"),
    "next_event": _t(None, "cual es mi siguiente evento", "mi siguiente evento",
                     "siguiente evento", "proximo evento", "next event"),
    "list_today": _t(None, "mis recordatorios de hoy", "recordatorios de hoy",
                     "recordatorios", "hoy", "my reminders today"),
    "mute": _t(None, "silencia el audio", "silencia", "audio", "mute"),
    "resume": _t(None, "pon musica", "reanuda", "resume", "musica"),
    "recall_last_action": _t(None, "hazlo de nuevo", "hazlo otra vez", "de nuevo",
                             "otra vez", "hazlo", "do it again"),
    "repeat_last": _t(None, "repite eso", "no te escuche", "repite", "repeat that"),
    "close_current_tab": _t(None, "cierra esta pestana", "cerrar esa pestana",
                            "cierra la pestana", "cierra", "cerrar", "pestana",
                            "close this tab", "close the tab"),
    "close_duplicate_tabs": _t(None, "cierra las pestanas duplicadas", "pestanas duplicadas",
                               "close duplicate tabs"),
    "list_browser_tabs": _t(None, "cuantas pestanas tengo abiertas", "pestanas abiertas",
                            "how many tabs"),
    "list_notes": _t(None, "que notas tengo", "decirme", "mis notas", "notas", "my notes"),
    "shutdown_emma": _t(None, "te puedes apagar", "apagate", "apagar", "para", "gracias",
                        "listo", "shut down"),
    "snooze_listening": _t(None, "duermete", "deja de escuchar", "snooze"),
}


# ---- matching ----------------------------------------------------------------------------


class _Text:
    """Folded tokens + display offsets, with a consumed mask."""

    def __init__(self, norm: Normalized) -> None:
        self.norm = norm
        self.tokens = list(norm.tokens)
        self.spans = [(m.start(), m.end()) for m in _WORDTOK.finditer(norm.text)]
        if len(self.spans) != len(self.tokens):  # defensive: fall back to word joins
            self.spans = []
        self.used = [False] * len(self.tokens)

    def display(self, idx: list[int]) -> str:
        if not idx:
            return ""
        if self.spans and idx == list(range(idx[0], idx[-1] + 1)):
            return self.norm.text[self.spans[idx[0]][0] : self.spans[idx[-1]][1]]
        return " ".join(self.norm.words[i] for i in idx)

    def grams(self) -> list[tuple[int, int, str]]:
        out = []
        n = len(self.tokens)
        for i in range(n):
            for j in range(i + 1, min(n, i + _MAX_GRAM) + 1):
                if any(self.used[i:j]):
                    break
                out.append((i, j, " ".join(self.tokens[i:j])))
        return out

    def consume_phrase(self, phrase: str) -> bool:
        pt = phrase.split()
        n = len(pt)
        for i in range(len(self.tokens) - n + 1):
            if self.tokens[i : i + n] == pt and not any(self.used[i : i + n]):
                for k in range(i, i + n):
                    self.used[k] = True
                return True
        return False


def _key(s: str) -> str:
    return " ".join(_WORDTOK.findall(fold(s).replace("-", " ").replace("_", " ")))


def _fuzzy(text: _Text, names: Mapping[str, str], floor: float) -> tuple[str, float, int, int] | None:
    """Best (canonical, ratio, i, j) over unconsumed n-grams, or None below floor.

    ``names`` maps a comparable key -> canonical value.
    """
    best: tuple[str, float, int, int] | None = None
    for i, j, gram in text.grams():
        for key, canon in names.items():
            r = fuzz.ratio(gram, key) / 100.0
            if best is None or r > best[1] or (r == best[1] and (j - i) > (best[3] - best[2])):
                best = (canon, r, i, j)
    if best is None or best[1] < floor:
        return None
    return best


def _number(text: _Text) -> tuple[int, int, int] | None:
    """(value, i, j) for the first number: digits, ES/EN words, "la mitad" / "half".

    Compounds: "setenta y cinco", "seventy five". A trailing "por ciento" /
    "percent" belongs to the span. "un"/"una"/"one" are articles, not numbers.
    """
    toks = text.tokens
    n = len(toks)
    for i, t in enumerate(toks):
        if text.used[i]:
            continue
        end = i + 1
        if t.isdigit():
            val = int(t)
        elif t in _HALF:
            val = _HALF[t]
        elif t in _TENS:
            val = _TENS[t]
            if end + 1 < n and toks[end] == "y" and toks[end + 1] in _UNITS:
                val, end = val + _UNITS[toks[end + 1]], end + 2  # "setenta y cinco"
            elif end < n and _UNITS.get(toks[end], 99) < 10:
                val, end = val + _UNITS[toks[end]], end + 1  # "seventy five"
        elif t in _UNITS and t not in ("un", "una", "uno", "one"):
            val = _UNITS[t]
        else:
            continue
        for tail in _PERCENT_TAIL:
            if tuple(toks[end : end + len(tail)]) == tail:
                end += len(tail)
                break
        return val, i, end
    return None


def _regex_span(text: _Text, pattern: re.Pattern[str]) -> tuple[str, int, int] | None:
    """Match against the folded text; return (value, token i, token j) of the span."""
    m = pattern.search(text.norm.folded)
    if not m:
        return None
    raw = m.group(1)
    want = _WORDTOK.findall(raw)
    for i in range(len(text.tokens) - len(want) + 1):
        if text.tokens[i : i + len(want)] == want and not any(text.used[i : i + len(want)]):
            return raw, i, i + len(want)
    return None


def _canon_url(raw: str) -> str:
    s = re.sub(r"\s*(?:\bpunto\b|\bdot\b)\s*", ".", raw)
    s = re.sub(r"\s*\.\s*", ".", s)
    return s.strip(" /").lower()


def _phrase_table(text: _Text, table: Mapping[str, str]) -> tuple[str, int, int] | None:
    for phrase in sorted(table, key=lambda p: -len(p.split())):
        pt = phrase.split()
        for i in range(len(text.tokens) - len(pt) + 1):
            if text.tokens[i : i + len(pt)] == pt and not any(text.used[i : i + len(pt)]):
                return table[phrase], i, i + len(pt)
    return None


def _app_names(vocab: Vocab, only: tuple[str, ...] | None = None) -> dict[str, str]:
    pool = [a for a in vocab.apps if only is None or a in only]
    if only is not None:
        pool += [a for a in only if a not in pool and a in vocab.apps]
    names = {_key(a): a for a in pool}
    for local, app in _APP_LOCAL_NAMES.items():
        if app in pool:
            names.setdefault(local, app)
    return names


def resolve(
    tool: str,
    norm: Normalized,
    vocab: Vocab,
    required: tuple[str, ...] = (),
    floor: float = DEFAULT_FLOOR,
) -> Resolution:
    spec = TOOLS.get(tool, ToolSlots())
    text = _Text(norm)
    slots: dict[str, SlotValue] = {}

    def take(param: str, value: SlotScalar, source: str, conf: float, i: int, j: int) -> None:
        for k in range(i, j):
            text.used[k] = True
        slots[param] = SlotValue(value, source, round(conf, 3), text.display(list(range(i, j))))

    # Deterministic and closed-table slots first, fuzzy entities next, residue last.
    order = sorted(spec.slots.items(), key=lambda kv: _PRIORITY[kv[1]])
    residue_param: str | None = None
    for param, kind in order:
        if kind == "residue":
            residue_param = param
            continue
        if kind == "number":
            hit = _number(text)
            if hit:
                take(param, hit[0], "regex:number", 1.0, hit[1], hit[2])
        elif kind == "url":
            m = _regex_span(text, _URL)
            if m:
                take(param, _canon_url(m[0]), "regex:url", 1.0, m[1], m[2])
            elif tool == "open_url":  # a site NAME means "open it", never "store it"
                site = _site(text, vocab, floor)
                if site:
                    take(param, SITES[site[0]], site[1], site[2], site[3], site[4])
        elif kind == "url_scheme":
            m = _regex_span(text, _SCHEME)
            if m:
                take(param, m[0] + "://", "regex:url_scheme", 1.0, m[1], m[2])
        elif kind == "profile_field":
            m = _phrase_table(text, {fold(k): v for k, v in _PROFILE_FIELDS.items()})
            if m:
                take(param, m[0], "table:profile_field", 1.0, m[1], m[2])
        elif kind == "app_category":
            m = _phrase_table(text, _APP_CATEGORIES)
            if m:
                take(param, m[0], "table:app_category", 1.0, m[1], m[2])
        elif kind in ("app", "ide"):
            app = _app(text, vocab, kind, floor)
            if app:
                take(param, app[0], app[1], app[2], app[3], app[4])
                _consume_repeats(text, {**_app_names(vocab), _key(app[0]): app[0]}, app[0], floor)
        elif kind in ("page", "note", "playlist", "connection", "contact",
                      "connection_or_residue"):
            names = _enum_names(kind, vocab)
            hit_e = _fuzzy(text, names, floor) if names else None
            if hit_e:
                take(param, hit_e[0], f"fuzzy:{kind.split('_')[0]}s", hit_e[1], hit_e[2], hit_e[3])
                _consume_repeats(text, names, hit_e[0], floor)
            elif kind == "connection_or_residue":
                residue_param = param

    for frame in sorted(spec.frames, key=lambda f: -len(f.split())):
        while text.consume_phrase(frame):
            pass

    rest = [i for i, u in enumerate(text.used) if not u]
    if residue_param is not None:
        content = [i for i in rest if text.tokens[i] not in FILLER]
        while content and text.tokens[content[0]] in STOP:
            content.pop(0)
        while content and text.tokens[content[-1]] in STOP:
            content.pop()
        if content:
            value = text.display(content).strip(" ,.;:¿?¡!")
            if value:
                slots[residue_param] = SlotValue(value, "residue", 1.0, value)
                for i in content:
                    text.used[i] = True

    leftover = tuple(
        text.tokens[i]
        for i, u in enumerate(text.used)
        if not u and text.tokens[i] not in STOP and text.tokens[i] not in FILLER
    )
    missing = tuple(p for p in required if p not in slots)
    return Resolution(slots, missing, leftover, residue_param)


_PRIORITY: dict[SlotKind, int] = {
    "number": 0, "url": 0, "url_scheme": 0, "profile_field": 1, "app_category": 1,
    "ide": 2, "app": 3, "page": 4, "note": 4, "playlist": 4, "connection": 4, "contact": 4,
    "connection_or_residue": 5, "residue": 9,
}


def _enum_names(kind: SlotKind, vocab: Vocab) -> dict[str, str]:
    if kind == "page":
        out = {_key(k): k for k in vocab.pages}
        out.update({_key(t): k for k, t in vocab.pages.items() if t})
        return out
    if kind == "note":
        return {_key(n): n for n in vocab.notes}
    if kind == "playlist":
        return {_key(n): n for n in vocab.playlists}
    if kind in ("connection", "connection_or_residue"):
        return {_key(n): n for n in vocab.connections}
    if kind == "contact":
        out = {_key(n): n for n in vocab.contacts}
        for n, al in vocab.contacts.items():
            out.update({_key(a): n for a in al})
        return out
    return {}


_REPEAT_FLOOR = 0.95  # a second mention must be near-exact: "comprar" is not "Compras"


def _consume_repeats(text: _Text, names: Mapping[str, str], value: str, floor: float) -> None:
    """A value said twice ("abre cursor, una ventana de cursor") is explained twice."""
    keys = {k: v for k, v in names.items() if v == value}
    while (again := _fuzzy(text, keys, max(floor, _REPEAT_FLOOR))) is not None:
        for k in range(again[2], again[3]):
            text.used[k] = True


def _site(
    text: _Text, vocab: Vocab, floor: float
) -> tuple[str, str, float, int, int] | None:
    """(site, source, confidence, i, j) for a well-known site said by name.

    "abre MI github" is the user's own page (open_my_page), not github.com: a
    possessive right before the name means this table doesn't apply.
    """

    def mine(i: int) -> bool:
        return i > 0 and text.tokens[i - 1] in ("mi", "mis", "my")

    for i, j, gram in text.grams():
        canon = vocab.aliases.get(gram)
        if canon in SITES:
            return None if mine(i) else (canon, "alias:sites", 1.0, i, j)
    names = {_key(n): n for n in SITES if len(n) > 1}  # "X" alone is too short to fuzz
    hit = _fuzzy(text, names, floor)
    if hit and not mine(hit[2]):
        return hit[0], "fuzzy:sites", hit[1], hit[2], hit[3]
    return None


def _app(
    text: _Text, vocab: Vocab, kind: SlotKind, floor: float
) -> tuple[str, str, float, int, int] | None:
    """(canonical, source, confidence, i, j) for an app/IDE mention, or None."""
    only = _IDES if kind == "ide" else None
    # 1. The alias table (STT forms: "espotify" -> Spotify). Exact, so confidence 1.
    for i, j, gram in text.grams():
        canon = vocab.aliases.get(gram)
        if canon and canon in vocab.apps and (only is None or canon in only):
            return canon, "alias:apps", 1.0, i, j
    # 2. Fuzzy against the installed apps (and their Spanish display names).
    names = _app_names(vocab, only)
    hit = _fuzzy(text, names, floor) if names else None
    if hit:
        return hit[0], "fuzzy:apps", hit[1], hit[2], hit[3]
    return None
