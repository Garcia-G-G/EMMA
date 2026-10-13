"""Prompt-injection defense enforced in dispatch, not in the prompt (BRAIN-1 Part 2).

CORE-LOCAL-ARCHITECTURE §4: the model choice must not decide safety. So the
rules live where the tool actually runs (``tools.registry.dispatch``), and the
tools array stays byte-stable:

1. **Session taint.** The first result from a ``returns_untrusted_content`` tool
   taints the session. Cleared only by a new wake-word session or a spoken
   "olvida eso" / "forget that".
2. **Sink policy.** While tainted, every sink (``core.brain.sinks``) needs a
   spoken two-phase confirmation, and every free-text string argument must
   appear in the user's own transcripts this session or in a trusted tool
   result. Otherwise: refused, and the refusal says what was blocked.
3. **Confirmation comes from the microphone.** ``confirmed=True`` runs only if a
   closed-list "yes" was transcribed AFTER the question was asked — tainted or
   not. The model cannot confirm its own action.

One guard per live session, installed by ``core.conversation``. Code that
calls ``dispatch`` with no guard installed (background tasks, tests, the
acceptance runner) is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core import session_memory
from core.brain.normalize import fold
from core.brain.phrases import is_forget, is_yes
from core.brain.sinks import CLOSED_ARGS, is_sink
from core.redaction import contains_secret, redact

log = structlog.get_logger("emma.injection")

# A "sí" can be transcribed a beat after the model already fired the confirmed
# call (the transcript lags the audio). If the user started speaking after the
# question, wait this long for the words before deciding.
TRANSCRIPT_WAIT_S = 2.5
_POLL_S = 0.05

_SPOKEN_URL = (
    (re.compile(r"\s+punto\s+"), "."),
    (re.compile(r"\s+arroba\s+"), "@"),
    (re.compile(r"\s+dot\s+"), "."),
    (re.compile(r"\s+slash\s+"), "/"),
)
_NON_ALNUM = re.compile(r"[^0-9a-z@./:_-]+")


def _canon(text: str) -> str:
    """Comparable form: folded, spoken URL words joined, punctuation collapsed."""
    s = fold(text)
    for pat, rep in _SPOKEN_URL:
        s = pat.sub(rep, s)
    return " " + _NON_ALNUM.sub(" ", s).strip() + " "


def _strings(value: Any, key: str = "") -> list[tuple[str, str]]:
    """Every (arg name, string) inside an argument, recursively."""
    if isinstance(value, str):
        return [(key, value)]
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for k, v in value.items():
            out.extend(_strings(v, f"{key}.{k}" if key else str(k)))
        return out
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v, key)]
    return []


_URL_DECOR = re.compile(r"^(?:https?://)?(?:www\.)?|/+$")


@dataclass
class Refusal:
    message: str
    ask: bool  # True: a yes/no question (requires_confirmation); False: blocked outright
    reason: str


@dataclass
class Clearance:
    """Run it. ``by_mic`` = a spoken yes after a question approved it."""

    by_mic: bool = False


@dataclass
class SessionGuard:
    started: float = field(default_factory=time.monotonic)
    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)
    _trusted: list[str] = field(default_factory=list)  # canon text of trusted results
    _asked: dict[str, float] = field(default_factory=dict)  # sink -> when WE asked
    _used_yes: set[float] = field(default_factory=set)  # yes events already spent

    # ---- inputs --------------------------------------------------------------

    def note_user_transcript(self, text: str) -> None:
        if self.tainted and is_forget(text):
            log.info("injection_taint_cleared", reason="forget_phrase", sources=self.taint_sources)
            self.tainted = False
            self.taint_sources.clear()

    def note_result(self, name: str, untrusted: bool, user_message: str, data: Any) -> None:
        if untrusted:
            if not self.tainted:
                log.info("injection_tainted", source=name)
            self.tainted = True
            if name not in self.taint_sources:
                self.taint_sources.append(name)
            return
        try:
            blob = json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""
        except Exception:
            blob = str(data)
        self._trusted.append(_canon(f"{user_message} {blob}"))

    # ---- the check -------------------------------------------------------------

    async def check(self, name: str, args: dict[str, Any]) -> Refusal | Clearance:
        """A ``Clearance`` = run it. A ``Refusal`` = don't, and here is what to say."""
        confirmed = bool(args.get("confirmed"))
        sink = is_sink(name)

        if self.tainted and sink:
            blocked = self._unprovenanced(args)
            if blocked:
                arg, value = blocked
                log.warning("injection_blocked", tool=name, arg=arg, reason="provenance",
                            sources=self.taint_sources)
                return Refusal(
                    f"Bloqueé {name}: el valor de «{arg}» ({redact(value)[:60]}) no salió de "
                    f"ti, salió de contenido que leí ({', '.join(self.taint_sources)}). "
                    "Si es lo que quieres, dímelo tú.",
                    ask=False,
                    reason="provenance",
                )
            secret = next((v for _, v in _strings(args) if contains_secret(v)), None)
            if secret is not None:
                log.warning("injection_blocked", tool=name, reason="secret_in_sink")
                return Refusal(
                    f"Bloqueé {name}: llevaba algo que parece un secreto, y acabo de leer "
                    "contenido que no es tuyo.",
                    ask=False,
                    reason="secret",
                )

        needs_yes = confirmed or (self.tainted and sink)
        if not needs_yes:
            return Clearance()

        t_req = self._question_ts(name)
        if t_req is not None and await self._yes_after(t_req):
            self._asked.pop(name, None)
            return Clearance(by_mic=True)

        # No spoken yes after a question: ask (again). The answer must be a NEW
        # transcript, so this also defeats a model that asks and answers itself.
        self._asked[name] = time.monotonic()
        reason = "tainted_sink" if (self.tainted and sink and not confirmed) else "no_mic_yes"
        log.info("injection_ask", tool=name, reason=reason, tainted=self.tainted)
        return Refusal(self._question(name, args), ask=True, reason=reason)

    # ---- helpers ----------------------------------------------------------------

    def _unprovenanced(self, args: dict[str, Any]) -> tuple[str, str] | None:
        heard = [
            _canon(ev.content)
            for ev in session_memory.recent(200)
            if ev.role == "user" and ev.kind == "speech" and ev.ts >= self.started
        ]
        corpus = heard + self._trusted
        for arg, value in _strings(args):
            if arg.split(".")[0] in CLOSED_ARGS or arg in ("confirmed", "cancelled", "picked"):
                continue
            v = _canon(value).strip()
            if len(v) < 2:
                continue
            bare = _URL_DECOR.sub("", v)  # the model adds https:// the user never said
            if not any(v in c or (bare and bare in c) for c in corpus):
                return arg, value
        return None

    def _question_ts(self, name: str) -> float | None:
        """Newest moment Emma asked about ``name`` (the tool's own question or ours)."""
        kind = f"requires_confirmation:{name}"
        ts = [ev.ts for ev in session_memory.recent(200) if ev.role == "tool" and ev.kind == kind]
        if name in self._asked:
            ts.append(self._asked[name])
        return max(ts) if ts else None

    async def _yes_after(self, t_req: float) -> bool:
        """Is the user's VERY NEXT utterance after the question a yes?

        The answer window opens at the first VAD onset after the question. A
        transcript landing after the question but belonging to speech that began
        before it — the request itself, transcribed late (Realtime fires tools
        before Whisper finishes) — is not the answer and is skipped. Then the
        first transcript decides: one "sí" confirms, anything else closes the
        window and the next attempt asks again.
        """
        deadline = time.monotonic() + TRANSCRIPT_WAIT_S
        while True:
            onset: float | None = None
            for ev in session_memory.recent(200):
                if ev.role != "user" or ev.ts <= t_req:
                    continue
                if ev.kind == "speech_started" and onset is None:
                    onset = ev.ts
                elif ev.kind == "speech" and onset is not None and ev.ts > onset:
                    if ev.ts in self._used_yes or not is_yes(ev.content):
                        return False
                    self._used_yes.add(ev.ts)
                    return True
            # Only wait while an answer has started but its words haven't landed.
            if onset is None or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL_S)

    @staticmethod
    def _question(name: str, args: dict[str, Any]) -> str:
        """Short, and names the action: "¿Borrar la nota Compras?" — one word answers it."""
        from core.brain.respond import action_phrase

        # Bounded: 60 chars a value, 3 values — a long body (possibly attacker
        # text, when tainted) is never read aloud as "the short question".
        pairs = [(k, redact(v)) for k, v in _strings(args) if k not in ("confirmed", "cancelled")]
        clean = {k: (v if len(v) <= 60 else v[:57] + "...") for k, v in pairs[:3]}
        return f"¿{action_phrase(name, clean, 'es')}?"


# ---- the one live guard ----------------------------------------------------------

_guard: SessionGuard | None = None


def install() -> SessionGuard:
    """A fresh guard for a new wake-word session: taint starts clear."""
    global _guard
    _guard = SessionGuard()
    return _guard


def uninstall() -> None:
    global _guard
    _guard = None


def current() -> SessionGuard | None:
    return _guard
