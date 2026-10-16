"""Structured memory: (subject, attribute, value) facts, no model (Step 42 Part 3).

ADR-42: where the meaning is closed, a contradiction is "same subject + attribute,
different value" — decided by structure, never by similarity (measured: no
embedder tells a restatement from a changed value). Statements and questions are
read by one small grammar, Spanish and English, neutral (no regional forms):

* ``parse_statement("mi perro se llama Toby")`` -> Slot("dog", "name", "Toby")
* ``parse_question("what's my dog's name?")``   -> ("dog", "name")

A slot question is answered from the table or with "I don't have that" — never
with the nearest free-text note. Anything the grammar doesn't read is a note
(``memory.long_term``). Writes never overwrite silently: :func:`propose` says
whether a statement is new, a restatement, a change (old value named) or a
retraction ("ya no soy vegetariano"); the caller confirms by voice before
:func:`commit` replaces anything. History is kept (``valid_to``, ``supersedes``).
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Literal

# Relations and pets, ES/EN -> one key. Closed and neutral.
_SUBJECTS: dict[str, str] = {
    "hermana": "sister", "sister": "sister", "hermano": "brother", "brother": "brother",
    "mama": "mother", "madre": "mother", "mom": "mother", "mother": "mother",
    "papa": "father", "padre": "father", "dad": "father", "father": "father",
    "pareja": "partner", "partner": "partner", "esposa": "spouse", "esposo": "spouse",
    "wife": "spouse", "husband": "spouse", "hija": "daughter", "daughter": "daughter",
    "hijo": "son", "son": "son", "jefa": "boss", "jefe": "boss", "boss": "boss",
    "mejor amigo": "best_friend", "mejor amiga": "best_friend", "best friend": "best_friend",
    "perro": "dog", "perra": "dog", "dog": "dog", "gato": "cat", "gata": "cat", "cat": "cat",
}
# Things a "favorite" can be about. Closed.
_THINGS: dict[str, str] = {
    "color": "color", "colour": "color", "pelicula": "movie", "movie": "movie", "film": "movie",
    "libro": "book", "book": "book", "banda": "band", "band": "band", "grupo": "band",
    "comida": "food", "food": "food", "estacion": "season", "season": "season",
    "cancion": "song", "song": "song", "serie": "show", "show": "show", "deporte": "sport",
    "sport": "sport", "numero de la suerte": "lucky_number", "lucky number": "lucky_number",
}
_SIZES: dict[str, str] = {
    "zapato": "shoe", "zapatos": "shoe", "shoe": "shoe", "camisa": "shirt", "shirt": "shirt",
    "pantalon": "pants", "pants": "pants",
}
_DIETS: dict[str, str] = {
    "vegetariano": "vegetarian", "vegetariana": "vegetarian", "vegetarian": "vegetarian",
    "vegano": "vegan", "vegana": "vegan", "vegan": "vegan",
}
_MONTHS: dict[str, int] = {m: i for i, ms in enumerate((
    ("enero", "january"), ("febrero", "february"), ("marzo", "march"), ("abril", "april"),
    ("mayo", "may"), ("junio", "june"), ("julio", "july"), ("agosto", "august"),
    ("septiembre", "september"), ("octubre", "october"), ("noviembre", "november"),
    ("diciembre", "december")), start=1) for m in ms}
# Attributes that hold several values at once (a second allergy is not a change).
MULTI = frozenset({"allergy"})


def fold(s: str) -> str:
    d = unicodedata.normalize("NFKD", s.lower().replace("\u2019", "'"))
    return "".join(c for c in d if not unicodedata.combining(c))


@dataclass(frozen=True)
class Slot:
    subject: str  # "user" or a _SUBJECTS key
    attribute: str  # name | birthday | favorite:<thing> | preferred_app:<purpose> | allergy | diet | size:<thing>
    value: str  # display form ("" for a retraction)
    retract: bool = False  # "ya no soy vegetariano": close the slot


_TRIGGER = re.compile(
    r"^(?:(?:hey|oye|ey)\s+)?(?:emma[\s,]+)?(?:(?:recuerdame|recuerdeme|recuerda|acuerdate de|acuerdate|"
    r"no olvides|guarda|apunta|anota|ten en cuenta)\s+que\s+|(?:remember|don't forget|dont forget|"
    r"keep in mind|note)\s+that\s+)?")
_SUBJ = "|".join(sorted((re.escape(k) for k in _SUBJECTS), key=len, reverse=True))
_THING = "|".join(sorted((re.escape(k) for k in _THINGS), key=len, reverse=True))
_SIZE = "|".join(sorted((re.escape(k) for k in _SIZES), key=len, reverse=True))
_DIET = "|".join(sorted((re.escape(k) for k in _DIETS), key=len, reverse=True))
_NOW = r"(?:ahora\s+|now\s+|nuevo\s+|nueva\s+|new\s+)?"

_STATEMENTS: list[tuple[re.Pattern[str], str]] = [(re.compile(p), a) for p, a in [
    # names
    (r"^(?:me llamo|mi nombre es|my name is|call me)\s+(?P<v>.+)$", "name:user"),
    (rf"^(?:mi|my)\s+{_NOW}(?P<s>{_SUBJ})\s+(?:se llama|is called|is named)\s+(?P<v>.+)$", "name"),
    (rf"^(?:my)\s+{_NOW}(?P<s>{_SUBJ})'s name is\s+(?P<v>.+)$", "name"),
    (rf"^el nombre de mi\s+(?P<s>{_SUBJ})\s+es\s+(?P<v>.+)$", "name"),
    (rf"^(?P<v>[\w .'-]+?)\s+es mi\s+(?P<s>{_SUBJ})$", "name"),
    # birthdays
    (rf"^el cumpleanos de mi\s+(?P<s>{_SUBJ})\s+es\s+(?:el\s+)?(?P<v>.+)$", "birthday"),
    (rf"^my\s+(?P<s>{_SUBJ})'s birthday is\s+(?:on\s+)?(?P<v>.+)$", "birthday"),
    (r"^(?:mi cumpleanos es|my birthday is)\s+(?:el\s+|on\s+)?(?P<v>.+)$", "birthday:user"),
    # favorites
    (rf"^{_NOW}mi\s+(?P<t>{_THING})\s+favorit[oa]\s+(?:ahora\s+)?es\s+(?:el\s+|la\s+|ahora\s+)?(?P<v>.+)$", "favorite"),
    (rf"^my\s+favou?rite\s+(?P<t>{_THING})\s+is\s+(?:now\s+)?(?P<v>.+?)(?:\s+now)?$", "favorite"),
    (rf"^(?:el\s+|la\s+)?(?P<v>[\w .'-]+?)\s+es mi\s+(?P<t>{_THING})\s+favorit[oa]$", "favorite"),
    (rf"^(?P<v>[\w .'-]+?)\s+is my\s+favou?rite\s+(?P<t>{_THING})$", "favorite"),
    (r"^mi numero de la suerte\s+(?:ahora\s+)?es\s+(?:el\s+)?(?P<v>\w+)$", "favorite:lucky_number"),
    (r"^my lucky number is\s+(?:now\s+)?(?P<v>\w+)$", "favorite:lucky_number"),
    # preferred app per purpose
    (r"^(?:ahora\s+)?(?:prefiero|uso|i prefer|i use|now i use)\s+(?P<v>[\w .+-]+?)\s+(?:para|for)\s+"
     r"(?:la\s+|el\s+|las\s+|los\s+|mis\s+|my\s+)?(?P<p>[\w -]+?)(?:\s+now)?$", "preferred_app"),
    (r"^para\s+(?P<p>[\w ]+?)\s+uso\s+(?P<v>[\w .+-]+)$", "preferred_app"),
    # allergies (several at once)
    (r"^(?:soy alergic[oa] a|tengo alergia a|i'm allergic to|i am allergic to)\s+(?:la\s+|el\s+|los\s+|las\s+)?(?P<v>.+)$",
     "allergy:user"),
    # diet; "ya no" / "not anymore" closes it
    (rf"^(?:ya no soy|i'm not|i am not|i'm no longer|i am no longer)\s+(?P<v>{_DIET})(?:\s+anymore)?$", "diet:retract"),
    (rf"^(?:soy|i'm|i am)\s+(?P<v>{_DIET})$", "diet:user"),
    # sizes
    (rf"^mi talla de\s+(?P<t>{_SIZE})\s+es\s+(?:el\s+|la\s+)?(?P<v>.+)$", "size"),
    (rf"^my\s+(?P<t>{_SIZE})\s+size is\s+(?:now\s+)?(?P<v>.+)$", "size"),
]]


def _clean(text: str) -> str:
    t = fold(text).strip()
    t = re.sub(r"[¿?¡!.,;:]+$", "", t).strip()
    t = re.sub(r"^[¿¡]", "", t)
    return _TRIGGER.sub("", t, count=1).strip()


def _display(original: str, folded_value: str) -> str:
    """The value as the person said it (accents/case kept) when it can be found."""
    m = re.search(re.escape(folded_value), fold(original))
    if m and len(fold(original)) == len(original):
        return original[m.start():m.end()].strip(" .,")
    return folded_value


def _date(v: str) -> str | None:
    """'3 de mayo' / 'june 2' / 'october 12' -> 'MM-DD'; None if not a day of a month."""
    m = re.match(r"^(\d{1,2})\s+de\s+(\w+)$", v) or re.match(r"^(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?$", v)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    day, month = (a, b) if a.isdigit() else (b, a)
    if month not in _MONTHS or not 1 <= int(day) <= 31:
        return None
    return f"{_MONTHS[month]:02d}-{int(day):02d}"


def parse_statement(text: str) -> Slot | None:
    """The slot a statement sets (or closes), or None: then it's a free-text note."""
    t = _clean(text)
    for pat, attr in _STATEMENTS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groupdict()
        v = (g.get("v") or "").strip()
        if not v:
            continue
        kind, _, fixed = attr.partition(":")
        subject = "user" if "s" not in g else _SUBJECTS[g["s"]]
        if kind == "name":
            return Slot(subject, "name", _display(text, v).strip().title() if v.islower() else v)
        if kind == "birthday":
            d = _date(v)
            return Slot(subject, "birthday", d) if d else None
        if kind == "favorite":
            thing = fixed or _THINGS[g["t"]]
            return Slot("user", f"favorite:{thing}", _display(text, v))
        if kind == "preferred_app":
            purpose = g["p"].strip()
            return Slot("user", f"preferred_app:{purpose}", _display(text, v))
        if kind == "allergy":
            return Slot("user", "allergy", v)
        if kind == "diet":
            return Slot("user", "diet", _DIETS[v], retract=fixed == "retract")
        if kind == "size":
            return Slot("user", f"size:{_SIZES[g['t']]}", v)
    return None


_QUESTIONS: list[tuple[re.Pattern[str], str]] = [(re.compile(p), a) for p, a in [
    (r"^(?:como me llamo|cual es mi nombre|what's my name|what is my name)$", "name:user"),
    (rf"^(?:como se llama|quien es) (?:mi|la|el)\s+(?P<s>{_SUBJ})(?: de mi \w+)?$", "name"),
    (rf"^(?:what's|what is) my\s+(?P<s>{_SUBJ})'s name$", "name"),
    (rf"^(?:who is|who's) my\s+(?P<s>{_SUBJ})$", "name"),
    (rf"^(?:remind me )?what my\s+(?P<s>{_SUBJ}) is called$", "name"),
    (rf"^(?:cuando es|dime) el cumpleanos de mi\s+(?P<s>{_SUBJ})$", "birthday"),
    (rf"^(?:when is|what's) my\s+(?P<s>{_SUBJ})'s birthday$", "birthday"),
    (r"^(?:cuando es mi cumpleanos|when is my birthday)$", "birthday:user"),
    (rf"^cual es mi\s+(?P<t>{_THING})\s+favorit[oa]$", "favorite"),
    (r"^(?:cual es mi numero de la suerte|what's my lucky number|what is my lucky number)$",
     "favorite:lucky_number"),
    (rf"^(?:what's|what is) my\s+favou?rite\s+(?P<t>{_THING})$", "favorite"),
    (r"^(?:que|con que)\s+(?:\w+\s+)?(?:uso|prefiero)\s+para\s+(?:la\s+|el\s+|las\s+|los\s+|mis\s+)?(?P<p>[\w ]+)$",
     "preferred_app"),
    (r"^(?:which|what)\s+(?:\w+\s+)?(?:app|editor|tool)?\s*do i (?:use|prefer) for\s+(?:my\s+)?(?P<p>[\w -]+)$",
     "preferred_app"),
    (r"^(?:a que soy alergic[oa]|what am i allergic to)$", "allergy:user"),
    (r"^(?:soy vegetarian[oa]|soy vegan[oa]|como carne|am i vegetarian|am i vegan|do i eat meat)$", "diet:user"),
    (rf"^que talla de\s+(?P<t>{_SIZE})\s+uso$", "size"),
    (rf"^(?:what's|what is) my\s+(?P<t>{_SIZE})\s+size$", "size"),
]]


def parse_question(text: str) -> tuple[str, str] | None:
    """(subject, attribute) a slot question asks for, or None (not a slot question)."""
    t = _clean(text)
    for pat, attr in _QUESTIONS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groupdict()
        kind, _, fixed = attr.partition(":")
        subject = "user" if fixed == "user" or "s" not in g else _SUBJECTS[g["s"]]
        if kind == "favorite":
            return "user", f"favorite:{fixed or _THINGS[g['t']]}"
        if kind == "size":
            return "user", f"size:{_SIZES[g['t']]}"
        if kind == "preferred_app":
            return "user", f"preferred_app:{g['p'].strip()}"
        return subject, kind
    return None


# ---- storage --------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    attribute  TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'explicit',
    created_at REAL NOT NULL,
    valid_to   REAL,
    supersedes INTEGER,
    times_observed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_user_facts_slot ON user_facts(subject, attribute, valid_to);
"""

Change = Literal["new", "same", "change", "retract", "nothing_to_retract"]


@dataclass(frozen=True)
class Proposal:
    slot: Slot
    change: Change
    old: tuple[int, str] | None = None  # (row id, value) the change replaces or closes


def _purpose_key(attribute: str) -> str:
    """'preferred_app:programar' and 'preferred_app:code' compare by folded purpose words."""
    return attribute if not attribute.startswith("preferred_app:") else \
        "preferred_app:" + " ".join(fold(attribute.split(":", 1)[1]).split())


def active(conn: sqlite3.Connection, subject: str, attribute: str) -> list[tuple[int, str]]:
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT id, value, attribute FROM user_facts WHERE subject = ? AND valid_to IS NULL",
        (subject,)).fetchall()
    want = _purpose_key(attribute)
    return [(int(r[0]), str(r[1])) for r in rows if _purpose_key(str(r[2])) == want]


def propose(conn: sqlite3.Connection, slot: Slot) -> Proposal:
    """What writing ``slot`` would do. Writes nothing."""
    current = active(conn, slot.subject, slot.attribute)
    same = [c for c in current if fold(c[1]) == fold(slot.value)]
    if slot.retract:
        return Proposal(slot, "retract", same[0]) if same else Proposal(slot, "nothing_to_retract")
    if same:
        return Proposal(slot, "same", same[0])
    if current and slot.attribute not in MULTI:
        return Proposal(slot, "change", current[0])
    return Proposal(slot, "new")


def commit(conn: sqlite3.Connection, p: Proposal, source: str = "explicit") -> int:
    """Apply a proposal (after the spoken yes for a change/retract). Returns the row id.

    Never deletes: a change closes the old row (``valid_to``) and links the new
    one (``supersedes``); a retraction only closes."""
    now = time.time()
    conn.executescript(SCHEMA)
    if p.change == "same" and p.old is not None:
        conn.execute("UPDATE user_facts SET times_observed = times_observed + 1 WHERE id = ?",
                     (p.old[0],))
        return p.old[0]
    if p.change in ("retract", "change") and p.old is not None:
        conn.execute("UPDATE user_facts SET valid_to = ? WHERE id = ?", (now, p.old[0]))
        if p.change == "retract":
            return p.old[0]
    if p.change == "nothing_to_retract":
        return -1
    cur = conn.execute(
        "INSERT INTO user_facts (subject, attribute, value, source, created_at, supersedes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (p.slot.subject, p.slot.attribute, p.slot.value, source, now,
         p.old[0] if p.change == "change" and p.old else None))
    return int(cur.lastrowid or 0)


def lookup(conn: sqlite3.Connection, subject: str, attribute: str) -> list[str]:
    """The current values of a slot ([] = not known)."""
    return [v for _id, v in active(conn, subject, attribute)]


def all_active(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT id, subject, attribute, value FROM user_facts "
                        "WHERE valid_to IS NULL ORDER BY id").fetchall()
    return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


def forget(conn: sqlite3.Connection, row_id: int) -> bool:
    """Close one slot value (the user asked to forget it). History stays."""
    cur = conn.execute("UPDATE user_facts SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                       (time.time(), row_id))
    return cur.rowcount > 0


# ---- saying a slot back (ES/EN, neutral, no gendered adjectives) ------------------

_SUBJECT_ES = {"sister": "tu hermana", "brother": "tu hermano", "mother": "tu mamá",
               "father": "tu papá", "partner": "tu pareja", "spouse": "tu pareja",
               "daughter": "tu hija", "son": "tu hijo", "boss": "tu jefe o jefa",
               "best_friend": "tu mejor amistad", "dog": "tu perro", "cat": "tu gato"}
_SUBJECT_EN = {"sister": "your sister", "brother": "your brother", "mother": "your mom",
               "father": "your dad", "partner": "your partner", "spouse": "your partner",
               "daughter": "your daughter", "son": "your son", "boss": "your boss",
               "best_friend": "your best friend", "dog": "your dog", "cat": "your cat"}
_THING_ES = {"color": "color", "movie": "película", "book": "libro", "band": "banda",
             "food": "comida", "season": "estación", "song": "canción", "show": "serie",
             "sport": "deporte", "lucky_number": "número de la suerte"}
_FEMININE = frozenset({"movie", "band", "food", "season", "song", "show"})
_SIZE_ES = {"shoe": "zapato", "shirt": "camisa", "pants": "pantalón"}
_MONTH_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre")
_MONTH_EN = ("January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December")


def _day(mmdd: str, lang: str) -> str:
    m, d = (int(x) for x in mmdd.split("-"))
    return f"{_MONTH_EN[m - 1]} {d}" if lang == "en" else f"{d} de {_MONTH_ES[m - 1]}"


def say(subject: str, attribute: str, value: str, lang: str) -> str:
    """'tu perro se llama Toby' / 'your dog is called Toby' — what Emma says back."""
    en = lang == "en"
    kind, _, detail = attribute.partition(":")
    who = (_SUBJECT_EN if en else _SUBJECT_ES).get(subject, "")
    if kind == "name":
        if subject == "user":
            return f"your name is {value}" if en else f"te llamas {value}"
        return f"{who} is called {value}" if en else f"{who} se llama {value}"
    if kind == "birthday":
        day = _day(value, lang)
        if subject == "user":
            return f"your birthday is {day}" if en else f"tu cumpleaños es el {day}"
        return f"{who}'s birthday is {day}" if en else f"el cumpleaños de {who} es el {day}"
    if kind == "favorite":
        thing = detail.replace("_", " ") if en else _THING_ES.get(detail, detail)
        fav = "favorita" if detail in _FEMININE else "favorito"
        return f"your favorite {thing} is {value}" if en else f"tu {thing} {fav} es {value}"
    if kind == "preferred_app":
        return f"you use {value} for {detail}" if en else f"usas {value} para {detail}"
    if kind == "allergy":
        return f"you're allergic to {value}" if en else f"tienes alergia a {value}"
    if kind == "diet":
        return f"your diet is {value}" if en else f"tu dieta es {'vegetariana' if value == 'vegetarian' else 'vegana'}"
    if kind == "size":
        return f"your {detail} size is {value}" if en else f"tu talla de {_SIZE_ES.get(detail, detail)} es {value}"
    return value


# ---- routing for the brain (injected by core.brain_host; core/brain stays generic) ----

# Attributes only memory keeps. Birthdays (birthday_remember) and app preferences
# (set_preferred_app) have tools of their own; those stay with retrieval.
_ROUTED = frozenset({"name", "favorite", "allergy", "diet", "size"})


def strip_trigger(text: str) -> str:
    """The sentence without its wake word and "recuérdame que" / "remember that" opener,
    accents and case kept where the folded form lines up with the original."""
    t = text.strip()
    folded = fold(t)
    m = _TRIGGER.match(folded)
    cut = m.end() if m else 0
    out = t[cut:] if len(folded) == len(t) else folded[cut:]
    return out.strip(" ,.;:¿?¡!")


_FORGET = re.compile(r"^(?:(?:hey|oye|ey)\s+)?(?:emma[\s,]+)?(?:olvida que|olvidate de que|borra que|"
                     r"deja de recordar que|forget that|stop remembering that)\s+")


def route(text: str) -> tuple[str, str] | None:
    """(tool, argument) for a statement memory owns ('remember_fact', the content),
    a request to forget one ('forget_fact', the content) or a question about one
    ('recall_facts', the question); else None. Stores and forgets still ask first
    (the tool's own question)."""
    folded = fold(text).strip()
    m = _FORGET.match(folded)
    if m:
        rest = text.strip()[m.end():] if len(folded) == len(text.strip()) else folded[m.end():]
        f = parse_statement(rest)
        if f is not None and f.attribute.split(":")[0] in _ROUTED:
            return "forget_fact", rest.strip(" ,.;:¿?¡!")
        return None
    s = parse_statement(text)
    if s is not None and s.attribute.split(":")[0] in _ROUTED:
        return "remember_fact", strip_trigger(text)
    q = parse_question(text)
    if q is not None and q[1].split(":")[0] in _ROUTED:
        return "recall_facts", strip_trigger(text)
    return None
