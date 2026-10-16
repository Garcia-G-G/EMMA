"""Time, deterministically (Step 43 Part 3, "L3"). No model, no clock of its own.

Two readings over folded tokens, both returning the token spans they explain:

* ``moment(...)`` -> a ``datetime``: a day ("hoy", "mañana", "tomorrow",
  "el viernes", "on friday") and/or a time ("a las nueve", "at 9", "9:30",
  "nueve y media", "half past nine", "at noon"), or a relative one ("en veinte
  minutos", "in 2 hours"). Day and time may sit apart and in two languages:
  "mañana at nine", "tomorrow a las nueve".
* ``duration(...)`` -> minutes, for timers: "veinte minutos", "an hour and a
  half", "media hora".

Neutral by rule (AGENTS.md rule 2): the default grammar holds no regional
expression; ``REGIONAL_DENYLIST`` is checked by a test. A person's own phrases
come from their profile (``[user.time]`` in the dictionary, phrase = minutes
from now) and are absent by default.

An hour from 1 to 12 with no "de la tarde" / "pm" is ambiguous: today it is the
next of the two after ``now``; on another day, the morning one. The caller says
the resolved time back, in the confirmation, so a wrong pick is heard and fixed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from core.brain.slots import _TENS, _UNITS

Span = tuple[int, int]

# Expressions the DEFAULT grammar must never contain (regional; a profile may add them).
REGIONAL_DENYLIST = frozenset({
    "ahorita", "orita", "horita", "al rato", "en un rato", "ratito", "luego luego", "ya mismo",
    "en la manana", "en la tarde", "en la noche", "cuarto para", "half nine", "arvo",
})

_DAY_WORDS: dict[tuple[str, ...], int] = {
    ("pasado", "manana"): 2, ("the", "day", "after", "tomorrow"): 2, ("manana",): 1,
    ("tomorrow",): 1, ("hoy",): 0, ("today",): 0,
}
_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_LEAD = (("el", "proximo"), ("este",), ("el",), ("proximo",), ("next",), ("this",), ("on",))
# (tokens) -> 12-hour half: "am" | "pm"
_MARKERS: dict[tuple[str, ...], str] = {
    ("de", "la", "manana"): "am", ("por", "la", "manana"): "am", ("de", "la", "madrugada"): "am",
    ("in", "the", "morning"): "am", ("am",): "am", ("a", "m"): "am",
    ("de", "la", "tarde"): "pm", ("por", "la", "tarde"): "pm", ("de", "la", "noche"): "pm",
    ("por", "la", "noche"): "pm", ("in", "the", "afternoon"): "pm", ("in", "the", "evening"): "pm",
    ("at", "night"): "pm", ("pm",): "pm", ("p", "m"): "pm",
}
_TONIGHT = (("esta", "noche"), ("tonight",))
_NAMED: dict[tuple[str, ...], tuple[int, int]] = {
    ("al", "mediodia"): (12, 0), ("a", "mediodia"): (12, 0), ("mediodia",): (12, 0),
    ("at", "noon"): (12, 0), ("noon",): (12, 0), ("a", "medianoche"): (0, 0),
    ("medianoche",): (0, 0), ("at", "midnight"): (0, 0), ("midnight",): (0, 0),
}
_HOUR_LEAD = (("a", "las"), ("a", "la"), ("at",))
_MINUTE_UNITS = {"minuto": 1, "minutos": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
                 "hora": 60, "horas": 60, "hour": 60, "hours": 60}
_RELATIVE_LEAD = (("dentro", "de"), ("en",), ("in",))
_OCLOCK = (("en", "punto"), ("o'clock",), ("oclock",))


def _at(toks: Sequence[str], used: Sequence[bool], i: int, words: tuple[str, ...]) -> bool:
    j = i + len(words)
    return j <= len(toks) and tuple(toks[i:j]) == words and not any(used[i:j])


def _num(toks: Sequence[str], used: Sequence[bool], i: int) -> tuple[int, int] | None:
    """(value, end) for a number at ``i``: digits, ES/EN words, "treinta y cinco"."""
    if i >= len(toks) or used[i]:
        return None
    t = toks[i]
    if t.isdigit():
        return int(t), i + 1
    if t in _TENS and _TENS[t] < 100:
        val, end = _TENS[t], i + 1
        if end + 1 < len(toks) and toks[end] == "y" and toks[end + 1] in _UNITS:
            return val + _UNITS[toks[end + 1]], end + 2
        if end < len(toks) and _UNITS.get(toks[end], 99) < 10:
            return val + _UNITS[toks[end]], end + 1
        return val, end
    if t in _UNITS:
        return _UNITS[t], i + 1
    return None


def _first(
    toks: Sequence[str], used: Sequence[bool], table: Mapping[tuple[str, ...], object]
) -> tuple[tuple[str, ...], int] | None:
    for i in range(len(toks)):
        for words in sorted(table, key=len, reverse=True):
            if _at(toks, used, i, words):
                return words, i
    return None


def _day(toks: Sequence[str], used: Sequence[bool], now: datetime) -> tuple[datetime, Span] | None:
    for i in range(len(toks)):
        for words, plus in sorted(_DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
            if not _at(toks, used, i, words):
                continue
            # "de la mañana" / "por la mañana" is the morning, not tomorrow.
            if words == ("manana",) and i >= 1 and toks[i - 1] == "la":
                continue
            return (now + timedelta(days=plus)).replace(hour=0, minute=0, second=0,
                                                        microsecond=0), (i, i + len(words))
        if toks[i] in _WEEKDAYS and not used[i]:
            lead = next((w for w in _WEEKDAY_LEAD if i >= len(w)
                         and tuple(toks[i - len(w):i]) == w and not any(used[i - len(w):i])), ())
            ahead = (_WEEKDAYS[toks[i]] - now.weekday()) % 7 or 7  # "el viernes" on a Friday: next one
            day = (now + timedelta(days=ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
            return day, (i - len(lead), i + 1)
    return None


def _clock(
    toks: Sequence[str], used: Sequence[bool], folded: str
) -> tuple[int, int, str | None, list[Span]] | None:
    """(hour, minute, "am"|"pm"|None, spans) for a time of day, or None."""
    named = _first(toks, used, _NAMED)
    if named:
        words, i = named
        h, m = _NAMED[words]
        return h, m, None, [(i, i + len(words))]
    for i in range(len(toks)):
        # "9:30" (STT writes the colon; the tokens are "9", "30")
        if toks[i].isdigit() and i + 1 < len(toks) and toks[i + 1].isdigit() and not any(used[i:i + 2]) \
                and re.search(rf"(?<!\d){toks[i]}:{toks[i + 1]}(?!\d)", folded):
            before = next((w for w in _HOUR_LEAD if i >= len(w) and _at(toks, used, i - len(w), w)), ())
            return int(toks[i]), int(toks[i + 1]), None, [(i - len(before), i + 2)]
        lead = next((w for w in _HOUR_LEAD if _at(toks, used, i, w)), None)
        # "quarter past nine", "half past nine", "quarter to nine"
        for words, delta in ((("quarter", "past"), 15), (("half", "past"), 30), (("quarter", "to"), -15)):
            at_i = i + (len(lead) if lead else 0)
            if _at(toks, used, at_i, words) and (n := _num(toks, used, at_i + 2)) and 1 <= n[0] <= 12:
                h, m = (n[0], delta) if delta > 0 else ((n[0] - 1) or 12, 60 + delta)
                return h, m, None, [(i, n[1])]
        if lead is None:
            continue
        n = _num(toks, used, i + len(lead))
        if not n or not 0 <= n[0] <= 24:
            continue
        h, end, m = n[0], n[1], 0
        if _at(toks, used, end, ("y", "media")):
            m, end = 30, end + 2
        elif _at(toks, used, end, ("y", "cuarto")):
            m, end = 15, end + 2
        elif _at(toks, used, end, ("menos", "cuarto")):
            h, m, end = (h - 1) % 24 or 12, 45, end + 2
        elif ((_at(toks, used, end, ("y",)) and (mm := _num(toks, used, end + 1)) and mm[0] < 60)
              or ((mm := _num(toks, used, end)) and 10 <= mm[0] < 60)):  # "y diez", "nine thirty"
            m, end = mm[0], mm[1]
        for words in _OCLOCK:
            if _at(toks, used, end, words):
                end += len(words)
        return h, m, None, [(i, end)]
    return None


def moment(
    toks: Sequence[str],
    used: Sequence[bool],
    folded: str,
    now: datetime,
    overlay: Mapping[str, int] | None = None,
) -> tuple[datetime, list[Span]] | None:
    """The moment the sentence names, and every span that named it; None if none."""
    for phrase, minutes in (overlay or {}).items():  # the person's own words first
        words = tuple(phrase.split())
        for i in range(len(toks)):
            if _at(toks, used, i, words):
                return _minute(now + timedelta(minutes=minutes)), [(i, i + len(words))]
    rel = _relative(toks, used, now)
    if rel:
        return rel
    mask = list(used)
    spans: list[Span] = []

    def mark(s: Span) -> None:
        spans.append(s)
        for k in range(*s):
            mask[k] = True

    tonight = _first(toks, mask, {w: 1 for w in _TONIGHT})
    if tonight:
        mark((tonight[1], tonight[1] + len(tonight[0])))
    day = _day(toks, mask, now)
    if day:
        mark(day[1])
    clock = _clock(toks, mask, folded)
    half = "pm" if tonight else None
    if clock:
        for s in clock[3]:
            mark(s)
    marker = _first(toks, mask, _MARKERS)
    if marker and (clock or day):
        half = _MARKERS[marker[0]]
        mark((marker[1], marker[1] + len(marker[0])))
    if not (clock or day or tonight):
        return None
    base = day[0] if day else now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not clock:
        if tonight:
            return None  # "esta noche" alone names no time: ask, don't pick one
        return base.replace(hour=9), spans  # a day alone: 9:00, said back in the confirmation
    h, m = clock[0], clock[1]
    if not 0 <= h <= 24 or not 0 <= m < 60:
        return None
    if h > 12 or h == 0 or half is not None:
        if half == "pm" and h < 12:
            h += 12
        elif half == "am" and h == 12:
            h = 0
        when = base.replace(hour=h % 24, minute=m)
        return (when + timedelta(days=1) if not day and when <= now else when), spans
    # 1-12 with no half named: the next one after now today; the morning one on another day.
    options = [base.replace(hour=h % 12, minute=m), base.replace(hour=h % 12 + 12, minute=m)]
    if day and day[0].date() != now.date():
        return options[0], spans
    for o in options:
        if o > now:
            return o, spans
    return options[0] + timedelta(days=1), spans


def _relative(toks: Sequence[str], used: Sequence[bool], now: datetime) -> tuple[datetime, list[Span]] | None:
    for i in range(len(toks)):
        lead = next((w for w in _RELATIVE_LEAD if _at(toks, used, i, w)), None)
        if lead is None:
            continue
        d = _duration_at(toks, used, i + len(lead))
        if d:
            return _minute(now + timedelta(minutes=d[0])), [(i, d[1])]
    return None


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _duration_at(toks: Sequence[str], used: Sequence[bool], i: int) -> tuple[int, int] | None:
    """(minutes, end) for a duration starting at ``i``."""
    for words, minutes in ((("media", "hora"), 30), (("half", "an", "hour"), 30),
                           (("una", "hora", "y", "media"), 90), (("hora", "y", "media"), 90),
                           (("an", "hour", "and", "a", "half"), 90), (("an", "hour"), 60),
                           (("una", "hora"), 60), (("un", "minuto"), 1), (("a", "minute"), 1)):
        if _at(toks, used, i, words):
            return minutes, i + len(words)
    n = _num(toks, used, i)
    if not n or n[1] >= len(toks) or toks[n[1]] not in _MINUTE_UNITS or used[n[1]]:
        return None
    total, end = n[0] * _MINUTE_UNITS[toks[n[1]]], n[1] + 1
    if _MINUTE_UNITS[toks[n[1]]] == 60:  # "dos horas y diez minutos", "2 hours 10 minutes"
        k = end + 1 if end < len(toks) and toks[end] in ("y", "and") else end
        if (mm := _num(toks, used, k)) and mm[1] < len(toks) and _MINUTE_UNITS.get(toks[mm[1]]) == 1:
            total, end = total + mm[0], mm[1] + 1
    return total, end


def duration(toks: Sequence[str], used: Sequence[bool]) -> tuple[int, Span] | None:
    """(minutes, span) for the first duration in the sentence ("de veinte minutos")."""
    for i in range(len(toks)):
        d = _duration_at(toks, used, i)
        if d and d[0] > 0:
            start = i - 1 if i >= 1 and toks[i - 1] in ("de", "for") and not used[i - 1] else i
            return d[0], (start, d[1])
    return None


def spoken(iso: str, lang: str, now: datetime | None = None) -> str:
    """'mañana a las 9:00' / 'tomorrow at 9:00' for an ISO moment; the ISO itself if unparsable."""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = now or datetime.now()
    days = (t.date() - now.date()).days
    if lang == "en":
        day = {0: "today", 1: "tomorrow"}.get(days) or (
            t.strftime("%A").lower() if 0 < days < 7 else t.strftime("%Y-%m-%d"))
        return f"{day} at {t.hour}:{t.minute:02d}"
    es_days = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    day = {0: "hoy", 1: "mañana", 2: "pasado mañana"}.get(days) or (
        f"el {es_days[t.weekday()]}" if 0 < days < 7 else t.strftime("%Y-%m-%d"))
    return f"{day} a las {t.hour}:{t.minute:02d}"
