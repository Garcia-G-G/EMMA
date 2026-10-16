"""What Emma says when the brain answers without an LLM (BRAIN-1 Part 6).

Text only — ``core/brain/processor.py`` hands it to TTS (VOICE-1). Three jobs, per LLM-DIET C1/C2/C3:

* **Templates with variation.** 4-6 phrasings per language for every verdict and
  every L1/L2 tool, chosen by weighted random with no immediate repeat, so the
  local path doesn't sound like a recording. Language follows the utterance.
* **Confirmation turn.** After a ``needs_confirmation`` decision, the next
  transcript is matched against the closed yes/no/cancel lists. This is the ONLY
  producer of ``confirmed=True`` on the brain's path — and the dispatch guard
  (``core.injection``) still requires the spoken yes on its own.
* **Clarification turn.** After a ``clarify`` with numbered options, the answer
  is matched by ordinal ("el primero", "2", "the last one") or by fuzzy label.

Neutral Spanish and English; no regionalisms (AGENTS.md language convention).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from rapidfuzz import fuzz

from core.brain import when
from core.brain.decision import Decision, SlotScalar
from core.brain.normalize import fold, lang_hint, normalize
from core.brain.phrases import is_no, is_yes
from core.brain.slots import FILLER, STOP
from core.runtime import SpokenLang

Phrasings = Mapping[SpokenLang, tuple[str, ...]]

# ---- tool acknowledgements ({slot} = the resolved value) ------------------------------

_TOOL: dict[str, Phrasings] = {
    "set_volume": {
        "es": ("Volumen al {percent}.", "Listo, {percent} por ciento.", "Ya está en {percent}.",
               "Puse el volumen en {percent}.", "Volumen en {percent}, listo."),
        "en": ("Volume at {percent}.", "Done, {percent} percent.", "Set to {percent}.",
               "Volume's at {percent} now.", "There, {percent} percent."),
    },
    "set_brightness": {
        "es": ("Brillo al {percent}.", "Listo, brillo en {percent}.", "Puse el brillo en {percent}.",
               "Ya está el brillo en {percent}."),
        "en": ("Brightness at {percent}.", "Done, brightness {percent}.",
               "Set brightness to {percent}.", "Brightness is at {percent} now."),
    },
    "mute": {
        "es": ("Silenciado.", "Listo, sin sonido.", "Ya, en silencio.", "Audio silenciado."),
        "en": ("Muted.", "Done, sound's off.", "Muted it.", "Audio's off."),
    },
    "open_application": {
        "es": ("Listo, abrí {name}.", "{name}, ya.", "Ahí está {name}.", "Abriendo {name}.",
               "{name} abierto."),
        "en": ("Opened {name}.", "{name}, done.", "There's {name}.", "Opening {name}.",
               "{name} is open."),
    },
    "open_url": {
        "es": ("Abriendo {url}.", "Listo, {url}.", "Ahí está {url}.", "Abrí {url}."),
        "en": ("Opening {url}.", "Done, {url}.", "There's {url}.", "Opened {url}."),
    },
    "open_my_page": {
        "es": ("Abriendo tu {name}.", "Listo, tu {name}.", "Ahí está tu {name}.", "Abrí tu {name}."),
        "en": ("Opening your {name}.", "Your {name}, done.", "There's your {name}.",
               "Opened your {name}."),
    },
    "open_in_app": {
        "es": ("Abriendo {target}.", "Listo, {target}.", "Ahí está {target}.", "Abrí {target}."),
        "en": ("Opening {target}.", "Done, {target}.", "There's {target}.", "Opened {target}."),
    },
    "play_track": {
        "es": ("Pongo {query}.", "Va {query}.", "Sonando {query}.", "Ahí va {query}."),
        "en": ("Playing {query}.", "Here's {query}.", "{query}, coming up.", "Putting on {query}."),
    },
    "play_playlist": {
        "es": ("Pongo {name}.", "Va la playlist {name}.", "Sonando {name}.", "Ahí va {name}."),
        "en": ("Playing {name}.", "Here's {name}.", "{name}, coming up.", "Putting on {name}."),
    },
    "resume": {
        "es": ("Sigo con la música.", "Ahí va.", "Reanudado.", "Continúo."),
        "en": ("Resuming.", "Back on.", "Here it goes.", "Picking it back up."),
    },
    "latest_video_from_creator": {
        "es": ("Abro lo último de {creator_name}.", "Va el video más reciente de {creator_name}.",
               "Ahí está lo nuevo de {creator_name}.", "Busco lo último de {creator_name}."),
        "en": ("Opening {creator_name}'s latest.", "Here's the newest from {creator_name}.",
               "Pulling up {creator_name}'s latest video.", "{creator_name}'s newest, coming up."),
    },
    "search_github": {
        "es": ("Busco {query} en GitHub.", "Buscando repos de {query}.", "Voy con {query} en GitHub.",
               "Reviso GitHub por {query}."),
        "en": ("Searching GitHub for {query}.", "Looking up {query} repos.",
               "Checking GitHub for {query}.", "On it: {query} on GitHub."),
    },
    "my_repos": {
        "es": ("Reviso tus repos.", "Van tus repos.", "Aquí están tus repos.", "Busco tus repos."),
        "en": ("Checking your repos.", "Here are your repos.", "Pulling up your repos.",
               "Looking at your repos."),
    },
    "append_to_note": {
        "es": ("Agregué {text} a {title}.", "Listo, va en {title}.", "Anotado en {title}.",
               "{title} actualizada."),
        "en": ("Added {text} to {title}.", "Done, it's in {title}.", "Noted in {title}.",
               "{title} updated."),
    },
    "read_note": {
        "es": ("Te leo {title}.", "Va {title}.", "Esto dice {title}.", "Aquí está {title}."),
        "en": ("Reading {title}.", "Here's {title}.", "This is {title}.", "{title} says this."),
    },
    "delete_note": {
        "es": ("Borré {title}.", "Listo, {title} eliminada.", "{title} ya no está.",
               "Eliminé la nota {title}."),
        "en": ("Deleted {title}.", "Done, {title}'s gone.", "{title} removed.",
               "Removed the note {title}."),
    },
    "list_notes": {
        "es": ("Estas son tus notas.", "Van tus notas.", "Reviso tus notas.", "Aquí están tus notas."),
        "en": ("Here are your notes.", "Your notes.", "Checking your notes.", "These are your notes."),
    },
    "ide_terminal_send": {
        "es": ("Escribí {text} en la terminal.", "Listo, {text}.", "Mandé {text}.", "{text}, enviado."),
        "en": ("Typed {text} in the terminal.", "Done, {text}.", "Sent {text}.", "{text}, sent."),
    },
    "run_command": {
        "es": ("Corrí {command}.", "Listo, {command}.", "Ejecuté {command}.", "{command}, hecho."),
        "en": ("Ran {command}.", "Done, {command}.", "Executed {command}.", "{command}, done."),
    },
    "remember_fact": {
        "es": ("Lo recuerdo.", "Anotado.", "Guardado.", "No se me olvida.", "Listo, lo guardé."),
        "en": ("I'll remember that.", "Noted.", "Saved.", "Got it.", "Done, saved."),
    },
    "remember_connection": {
        "es": ("Guardé {name} en {app}.", "Listo, recuerdo {name}.", "Anotada {name}.",
               "{name} guardada."),
        "en": ("Saved {name} in {app}.", "Done, I'll remember {name}.", "Noted {name}.",
               "{name} saved."),
    },
    "remember_page": {
        "es": ("Guardé tu {name}.", "Listo, recuerdo tu {name}.", "Anotada tu {name}.",
               "Tu {name}, guardada."),
        "en": ("Saved your {name}.", "Done, I'll remember your {name}.", "Noted your {name}.",
               "Your {name}, saved."),
    },
    "remember_user_profile": {
        "es": ("Anotado.", "Listo, lo guardé.", "Guardado.", "Lo recuerdo."),
        "en": ("Noted.", "Done, saved.", "Saved.", "Got it."),
    },
    "set_preferred_app": {
        "es": ("Usaré {app_name}.", "Listo, {app_name} por defecto.", "Anotado: {app_name}.",
               "De ahora en adelante, {app_name}."),
        "en": ("I'll use {app_name}.", "Done, {app_name} by default.", "Noted: {app_name}.",
               "From now on, {app_name}."),
    },
    "remember_app": {
        "es": ("Aprendí {name}.", "Listo, ya sé usar {name}.", "Anotado {name}.", "{name}, guardada."),
        "en": ("Learned {name}.", "Done, I know {name} now.", "Noted {name}.", "{name}, saved."),
    },
    "find_button": {
        "es": ("Busco {name}.", "Reviso si hay {name}.", "A ver si está {name}.", "Busco el botón {name}."),
        "en": ("Looking for {name}.", "Checking for {name}.", "Let me find {name}.",
               "Looking for the {name} button."),
    },
    "click_button": {
        "es": ("Hice click en {name}.", "Listo, {name}.", "Presioné {name}.", "{name}, hecho."),
        "en": ("Clicked {name}.", "Done, {name}.", "Pressed {name}.", "{name}, done."),
    },
    "current_time": {
        "es": ("Te digo la hora.", "Va la hora.", "Reviso la hora.", "Ahí va la hora."),
        "en": ("Here's the time.", "Checking the time.", "The time.", "Let me tell you the time."),
    },
    "current_datetime_speak": {
        "es": ("Te digo la hora.", "Va la hora.", "Reviso la hora.", "Ahí va la hora."),
        "en": ("Here's the time.", "Checking the time.", "The time.", "Let me tell you the time."),
    },
    "recall_facts": {
        "es": ("Esto sé de ti.", "Va lo que recuerdo.", "Reviso lo que sé.", "Esto tengo de ti."),
        "en": ("Here's what I know.", "This is what I remember.", "Checking what I know.",
               "Here's what I have on you."),
    },
    "today_events": {
        "es": ("Reviso tu día.", "Esto tienes hoy.", "Va tu agenda de hoy.", "Aquí está tu día."),
        "en": ("Checking your day.", "Here's today.", "Your agenda for today.", "This is your day."),
    },
    "next_event": {
        "es": ("Tu siguiente evento.", "Reviso lo que sigue.", "Esto viene.", "Lo que sigue en tu agenda."),
        "en": ("Your next event.", "Checking what's next.", "Here's what's next.",
               "Next on your calendar."),
    },
    "list_today": {
        "es": ("Tus pendientes de hoy.", "Reviso tus recordatorios.", "Esto tienes hoy.",
               "Van tus recordatorios."),
        "en": ("Today's reminders.", "Checking your reminders.", "Here's what's due today.",
               "Your reminders."),
    },
    "list_browser_tabs": {
        "es": ("Reviso tus pestañas.", "Van tus pestañas.", "Cuento tus pestañas.",
               "Estas son tus pestañas."),
        "en": ("Checking your tabs.", "Here are your tabs.", "Counting your tabs.",
               "These are your tabs."),
    },
    "close_current_tab": {
        "es": ("Cerrada.", "Listo, la cerré.", "Pestaña cerrada.", "Ya está."),
        "en": ("Closed.", "Done, closed it.", "Tab closed.", "There."),
    },
    "close_duplicate_tabs": {
        "es": ("Cerré las duplicadas.", "Listo, sin duplicadas.", "Pestañas repetidas cerradas.",
               "Ya no hay duplicadas."),
        "en": ("Closed the duplicates.", "Done, no duplicates.", "Duplicate tabs closed.",
               "No more duplicates."),
    },
    "recall_last_action": {
        "es": ("Va de nuevo.", "Lo repito.", "Otra vez.", "De nuevo."),
        "en": ("Again.", "Doing it again.", "Once more.", "Repeating that."),
    },
    "repeat_last": {
        "es": ("Te lo repito.", "Va otra vez.", "Lo repito.", "De nuevo."),
        "en": ("Here it is again.", "Again.", "Repeating.", "Once more."),
    },
    "shutdown_emma": {
        "es": ("Me apago. Hasta luego.", "Listo, me apago.", "Hasta pronto.", "Nos vemos."),
        "en": ("Shutting down. Bye.", "Done, shutting down.", "See you soon.", "Bye for now."),
    },
    "start_timer": {
        "es": ("Timer de {duration_min} minutos.", "Listo, {duration_min} minutos.",
               "Cuento {duration_min} minutos.", "Va, {duration_min} minutos."),
        "en": ("Timer set for {duration_min} minutes.", "Done, {duration_min} minutes.",
               "Counting {duration_min} minutes.", "Okay, {duration_min} minutes."),
    },
    "add_reminder": {
        "es": ("Listo, te lo recuerdo.", "Anotado en recordatorios.", "Hecho, te aviso."),
        "en": ("Done, I'll remind you.", "Added to your reminders.", "Okay, I'll remind you."),
    },
    "snooze_listening": {
        "es": ("Dejo de escuchar un rato.", "Listo, en pausa.", "Me duermo un rato.", "En pausa."),
        "en": ("Pausing for a bit.", "Done, paused.", "Taking a break.", "Paused."),
    },
}

# What a confirmation question names: the concrete action, with its target —
# recipient, host, path — so the "sí" agrees to something specific (CORE-LOCAL §4.3).
_ACTION: dict[str, Mapping[SpokenLang, str]] = {
    "delete_note": {"es": "borrar la nota {title}", "en": "delete the note {title}"},
    "click_button": {"es": "hacer click en {name}", "en": "click {name}"},
    "close_duplicate_tabs": {"es": "cerrar las pestañas duplicadas", "en": "close the duplicate tabs"},
    "run_command": {"es": "correr «{command}»", "en": "run «{command}»"},
    "ide_terminal_send": {"es": "escribir «{text}» en la terminal", "en": "type «{text}» in the terminal"},
    "open_url": {"es": "abrir {url}", "en": "open {url}"},
    "open_in_app": {"es": "abrir {target} en {app}", "en": "open {target} in {app}"},
    "search_github": {"es": "buscar {query} en GitHub", "en": "search GitHub for {query}"},
    "latest_video_from_creator": {"es": "abrir lo último de {creator_name}",
                                  "en": "open {creator_name}'s latest video"},
    "remember_fact": {"es": "recordar que {content}", "en": "remember that {content}"},
    "remember_connection": {"es": "guardar la conexión {name} de {app}",
                            "en": "save the {app} connection {name}"},
    "remember_page": {"es": "guardar {url} como tu {name}", "en": "save {url} as your {name}"},
    "remember_user_profile": {"es": "guardar {value} como tu {field}", "en": "save {value} as your {field}"},
    "remember_app": {"es": "aprender {name} con {url_scheme}", "en": "learn {name} via {url_scheme}"},
    "set_preferred_app": {"es": "usar {app_name} para {category}", "en": "use {app_name} for {category}"},
    "append_to_note": {"es": "agregar «{text}» a {title}", "en": "add «{text}» to {title}"},
    "add_reminder": {"es": "recordarte «{title}» {due_iso}", "en": "remind you to «{title}» {due_iso}"},
    "add_reminder:no_time": {"es": "recordarte «{title}»", "en": "remind you to «{title}»"},
    "shutdown_emma": {"es": "apagarme", "en": "shut down"},
    "snooze_listening": {"es": "dejar de escuchar un rato", "en": "stop listening for a while"},
}

# ---- verdicts that aren't a tool ------------------------------------------------------------

_CONFIRM: Phrasings = {
    "es": ("¿{action}? Dime sí o no.", "Antes de hacerlo: {action}. ¿Lo hago?",
           "¿Confirmas? {action}.", "{action}. ¿Sí o no?"),
    "en": ("{action}? Yes or no.", "Before I do it: {action}. Go ahead?",
           "Confirm? {action}.", "{action}. Yes or no?"),
}
_CANCELLED: Phrasings = {
    "es": ("Va, no lo hago.", "Cancelado.", "Listo, lo dejo.", "Entendido, nada."),
    "en": ("Okay, I won't.", "Cancelled.", "Done, leaving it.", "Got it, nothing."),
}
_UNCLEAR_ANSWER: Phrasings = {
    "es": ("No te entendí. ¿Sí o no?", "Perdón, ¿lo hago o no?", "¿Sí o no?", "Dime sí o no, por favor."),
    "en": ("Sorry, yes or no?", "I didn't catch that. Go ahead or not?", "Yes or no?",
           "Just yes or no, please."),
}
_CLARIFY: dict[str, Phrasings] = {
    "search": {"es": ("¿Qué busco?", "¿Qué quieres que busque?", "Dime qué buscar.", "¿Buscar qué?"),
               "en": ("What should I search for?", "Search for what?", "What am I looking for?",
                      "Tell me what to search.")},
    "send": {"es": ("¿A quién y qué le digo?", "¿Para quién es el mensaje?", "¿Qué mensaje y a quién?",
                    "Dime a quién y qué."),
             "en": ("Who to, and what should it say?", "Who's the message for?",
                    "What message, and to whom?", "Tell me who and what.")},
    "close": {"es": ("¿Qué cierro?", "¿Cerrar qué exactamente?", "Dime qué cierro.", "¿Qué quieres cerrar?"),
              "en": ("Close what?", "What should I close?", "Tell me what to close.",
                     "What exactly should I close?")},
    "generic": {"es": ("¿Qué exactamente?", "Dime un poco más.", "¿De qué se trata?", "¿Qué necesitas?"),
                "en": ("What exactly?", "Tell me a bit more.", "What do you mean?", "What do you need?")},
    "referent": {"es": ("¿A qué te refieres?", "¿Qué cosa?", "¿Cuál, exactamente?"),
                 "en": ("What do you mean?", "Which one?", "Which one, exactly?")},
    "options": {"es": ("¿Cuál? {options}", "Tengo varias: {options}. ¿Cuál?", "¿Cuál de estas? {options}"),
                "en": ("Which one? {options}", "I have a few: {options}. Which?",
                       "Which of these? {options}")},
}
_REFUSE: dict[str, Phrasings] = {
    "safety": {"es": ("Eso no lo voy a hacer.", "Eso no lo hago.", "Prefiero no hacer eso.",
                      "Esa no la hago."),
               "en": ("I won't do that.", "That one I won't do.", "I'd rather not do that.",
                      "Not doing that one.")},
    "phone_call": {"es": ("No puedo hacer llamadas.", "Llamar no lo puedo hacer.",
                          "Todavía no hago llamadas.", "Las llamadas no las puedo hacer."),
                   "en": ("I can't make calls.", "Calling isn't something I can do.",
                          "I don't make calls yet.", "Phone calls I can't do.")},
}
# VOICE-1: tools whose result IS the answer ("Son las 16:47", "Hoy tienes: ...").
# Their _TOOL lines are preambles written for the Realtime model, which then read
# the result; on the cascade nothing reads it, so the result is what's spoken.
SPEAKS_RESULT: frozenset[str] = frozenset({
    "current_time", "current_datetime_speak", "describe_capabilities", "find_file",
    "list_browser_tabs", "list_notes", "list_timers", "list_today", "my_repos", "next_event",
    "now_playing", "read_note", "recall_facts", "recall_last_action", "repeat_last",
    "today_events", "what_did_you_do",
})

_DONE: Phrasings = {
    "es": ("Listo.", "Hecho.", "Ya está.", "Va."),
    "en": ("Done.", "There.", "All set.", "Okay."),
}

# Right after Emma acted, the user said it was wrong (VOICE-1 Part 3).
_CORRECTED: Phrasings = {
    "es": ("Perdón, lo anoto.", "Entendido, lo anoté.", "Perdón. Ya quedó anotado.",
           "Va, lo apunto para hacerlo mejor."),
    "en": ("Sorry, noted.", "Got it, I noted that.", "Sorry. Noted.",
           "Okay, I'll do better with that one."),
}

# A deferred turn with no thinker yet (VOICE-1). Not an error and not "I didn't
# understand": she heard it, can't do it alone yet, and kept it to learn from.
_NOT_YET: Phrasings = {
    "es": ("Eso todavía no lo hago sola; lo apunté.", "Esa aún no la hago por mi cuenta. Ya quedó anotada.",
           "Todavía no puedo con esa sola; la guardé para aprenderla.",
           "Esa me falta aprenderla. La dejé apuntada.",
           "Por ahora esa no la hago sola, pero ya la tengo anotada."),
    "en": ("I can't do that one on my own yet; I noted it.", "That one I don't do alone yet. Noted.",
           "I don't handle that by myself yet, but I saved it.",
           "Still learning that one. I wrote it down.",
           "Not on my own yet. I've noted it for later."),
}
# A memory request while memory can't store anything (Step 42 Part 0): the truth,
# never "lo apunté" — nothing would be kept.
_NO_MEMORY: Phrasings = {
    "es": ("Todavía no puedo guardar recuerdos.", "Aún no sé guardar recuerdos; eso no quedó guardado.",
           "Por ahora no guardo recuerdos, así que eso no lo tengo."),
    "en": ("I can't save memories yet.", "I don't keep memories yet, so that wasn't saved.",
           "I can't remember things yet; that wasn't stored."),
}
_SMALLTALK: Phrasings = {
    "es": ("Bien, ¿en qué te ayudo?", "Aquí ando, ¿qué necesitas?", "Todo bien. ¿Qué hacemos?",
           "Con gusto.", "Aquí estoy."),
    "en": ("Good. What can I do?", "Here. What do you need?", "All good. What's next?",
           "Happy to help.", "I'm here."),
}

Situation = Literal["local", "confirm", "cancelled", "unclear", "clarify", "refuse", "smalltalk", "noise"]


@dataclass
class Responder:
    """Picks phrasings. ``rng`` is injectable so tests are deterministic."""

    rng: random.Random = field(default_factory=random.Random)
    _last: dict[str, str] = field(default_factory=dict)

    def pick(self, key: str, options: Sequence[str]) -> str:
        """Weighted random, never the same phrasing twice in a row for one key."""
        pool = [o for o in options if o != self._last.get(key)] or list(options)
        weights = [1.0 / (1 + i * 0.15) for i in range(len(pool))]  # earlier = a bit likelier
        choice = self.rng.choices(pool, weights=weights, k=1)[0]
        self._last[key] = choice
        return choice

    def say(self, d: Decision, lang: SpokenLang | None = None) -> str:
        """The sentence for a decision. "" for noise (say nothing), never for a defer."""
        lang = lang or language(d.utterance)
        if d.verdict == "defer":
            raise ValueError("a deferred decision is the LLM's to answer")
        if d.verdict == "none":
            if d.reason.startswith("noise") or d.reason == "empty":
                return ""
            return self.pick(f"smalltalk:{lang}", _SMALLTALK[lang])
        if d.verdict == "refuse":
            kind = "phone_call" if d.reason == "unsupported:phone_call" else "safety"
            return self.pick(f"refuse:{kind}:{lang}", _REFUSE[kind][lang])
        if d.verdict == "clarify":
            return self.pick(f"clarify:{lang}", _CLARIFY[_clarify_kind(d)][lang])
        assert d.chosen is not None
        slots = {k: v.value for k, v in d.slots.items()}
        if d.needs_confirmation:
            action = self._action(d.chosen, slots, lang)
            return self.pick(f"confirm:{lang}", _CONFIRM[lang]).format(action=action)
        return self.ack(d.chosen, slots, lang)

    def ack(self, tool: str, slots: Mapping[str, SlotScalar], lang: SpokenLang) -> str:
        phr = _TOOL.get(tool)
        if phr is None:
            return self.pick(f"done:{lang}", _DONE[lang])
        usable = [p for p in phr[lang] if _fills(p, slots)]
        return self.pick(f"{tool}:{lang}", usable or phr[lang]).format_map(_Default(slots))

    def _action(self, tool: str, slots: Mapping[str, SlotScalar], lang: SpokenLang) -> str:
        return action_phrase(tool, slots, lang)

    def cancelled(self, lang: SpokenLang) -> str:
        return self.pick(f"cancelled:{lang}", _CANCELLED[lang])

    def corrected(self, lang: SpokenLang) -> str:
        return self.pick(f"corrected:{lang}", _CORRECTED[lang])

    def not_yet(self, lang: SpokenLang) -> str:
        return self.pick(f"not_yet:{lang}", _NOT_YET[lang])

    def no_memory(self, lang: SpokenLang) -> str:
        return self.pick(f"no_memory:{lang}", _NO_MEMORY[lang])

    def unclear(self, lang: SpokenLang) -> str:
        return self.pick(f"unclear:{lang}", _UNCLEAR_ANSWER[lang])

    def options(self, labels: Sequence[str], lang: SpokenLang) -> str:
        listed = ", ".join(f"{i}. {lab}" for i, lab in enumerate(labels, start=1))
        return self.pick(f"options:{lang}", _CLARIFY["options"][lang]).format(options=listed)


def action_phrase(
    tool: str, slots: Mapping[str, SlotScalar], lang: SpokenLang, now: datetime | None = None
) -> str:
    """The concrete action and its target — what a "sí" agrees to. Capitalized."""
    if any(k.endswith("_iso") for k in slots):  # a moment is said, never read as ISO
        slots = {k: when.spoken(str(v), lang, now) if k.endswith("_iso") else v
                 for k, v in slots.items()}
    phrase = _ACTION.get(tool, {}).get(lang)
    if phrase and not _fills(phrase, slots):
        phrase = _ACTION.get(f"{tool}:no_time", {}).get(lang)
    if phrase and _fills(phrase, slots):
        text = phrase.format_map(_Default(slots))
    else:  # never the tool's code name alone: say its target too
        detail = ", ".join(str(v) for v in slots.values() if str(v).strip())
        text = f"{tool.replace('_', ' ')}: {detail}" if detail else tool.replace("_", " ")
    return text[:1].upper() + text[1:]


class _Default(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return ""


def _fills(template: str, slots: Mapping[str, SlotScalar]) -> bool:
    import string

    names = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    return all(n in slots and str(slots[n]).strip() for n in names)


def _clarify_kind(d: Decision) -> str:
    if d.reason == "anaphora:no_referent":
        return "referent"
    first = normalize(d.utterance).tokens[:1]
    verb = first[0] if first else ""
    if verb.startswith(("busca", "search")):
        return "search"
    if verb.startswith(("manda", "envia", "send")):
        return "send"
    if verb.startswith(("cierra", "close")):
        return "close"
    return "generic"


def language(utterance: str) -> SpokenLang:
    return "en" if lang_hint(normalize(utterance).tokens) == "en" else "es"


# ---- follow-up turns ---------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationOutcome:
    kind: Literal["execute", "cancel", "unclear"]
    tool: str
    slots: Mapping[str, SlotScalar]
    confirmed: bool  # True only for "execute" — the brain's ONLY path to confirmed=True


def answer_confirmation(
    tool: str, slots: Mapping[str, SlotScalar], transcript: str
) -> ConfirmationOutcome:
    """Resolve the turn after Emma asked to confirm ``tool``. Closed lists only."""
    if is_yes(transcript):
        return ConfirmationOutcome("execute", tool, slots, confirmed=True)
    if is_no(transcript):
        return ConfirmationOutcome("cancel", tool, slots, confirmed=False)
    return ConfirmationOutcome("unclear", tool, slots, confirmed=False)


_ORDINALS = {
    "primero": 1, "primera": 1, "uno": 1, "1": 1, "first": 1, "one": 1,
    "segundo": 2, "segunda": 2, "dos": 2, "2": 2, "second": 2, "two": 2,
    "tercero": 3, "tercera": 3, "tres": 3, "3": 3, "third": 3, "three": 3,
    "cuarto": 4, "cuarta": 4, "cuatro": 4, "4": 4, "fourth": 4, "four": 4,
    "quinto": 5, "quinta": 5, "cinco": 5, "5": 5, "fifth": 5, "five": 5,
}
_LAST = frozenset({"ultimo", "ultima", "last"})
CLARIFY_FLOOR = 0.8


def answer_clarification(options: Sequence[str], transcript: str) -> int | None:
    """Index of the chosen option, or None when the answer doesn't pick one.

    By ordinal ("el primero", "la dos", "the last one") or by label
    ("Juan Pérez", "el de la oficina" against "Juan (oficina)") above the floor.
    Ambiguous (two labels equally close) is None — ask again, don't guess.
    """
    toks = normalize(transcript).tokens
    for t in toks:
        if t in _ORDINALS and _ORDINALS[t] <= len(options):
            return _ORDINALS[t] - 1
        if t in _LAST and options:
            return len(options) - 1
    # Match on content words: "el de la oficina" is about "oficina".
    said = " ".join(t for t in toks if t not in STOP and t not in FILLER)
    if not said:
        return None
    scores = [fuzz.partial_ratio(said, fold(o)) / 100.0 for o in options]
    if not scores:
        return None
    best = max(scores)
    if best < CLARIFY_FLOOR or scores.count(best) > 1:
        return None
    return scores.index(best)


def fixed_phrasings() -> list[tuple[str, SpokenLang, str]]:
    """Every phrasing with no slot to fill, as (key, lang, text), most frequent first.

    What can be rendered ahead of time and cached (VOICE-1 Part 4). Order is the
    verdict's share on the 140 (BRAIN-1.5: defer 63, local 51, then no-tool
    templates), until the live log can rank them; Spanish before English.
    """
    ordered: list[tuple[str, Phrasings]] = [
        ("not_yet", _NOT_YET), ("no_memory", _NO_MEMORY), ("done", _DONE),
        *((f"tool:{name}", phr) for name, phr in _TOOL.items()),
        ("smalltalk", _SMALLTALK), ("refuse:safety", _REFUSE["safety"]),
        *((f"clarify:{k}", v) for k, v in _CLARIFY.items() if k != "options"),
        ("cancelled", _CANCELLED), ("unclear", _UNCLEAR_ANSWER), ("corrected", _CORRECTED),
        ("refuse:phone_call", _REFUSE["phone_call"]),
    ]
    out: list[tuple[str, SpokenLang, str]] = []
    for lang in ("es", "en"):
        for key, phr in ordered:
            out.extend((key, lang, t) for t in phr[lang] if "{" not in t)
    order = {k: i for i, (k, _) in enumerate(ordered)}
    out.sort(key=lambda r: (order[r[0]], r[1] != "es"))
    return out
