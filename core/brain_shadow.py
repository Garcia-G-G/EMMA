"""Shadow mode: the brain decides beside the live LLM path, and only logs (BRAIN-1 Part 7).

Every user turn becomes one line in ``~/.emma/brain_decisions.jsonl`` holding
the brain's full ``Decision`` AND what the live path actually did — the tool it
called (redacted args) or no tool — so each turn is a labelled comparison:
``brain_verdict``, ``brain_tool``, ``realtime_tool``, ``agree``.

**Nothing here executes.** No dispatch, no tool, no audio. And nothing here may
hurt the live session: decisions run in a worker thread, every hook swallows
its own errors, and a brain that isn't loaded yet still produces a record
(``defer`` / ``brain_not_ready``) — no record, no decision.

A turn starts at VAD onset (``speech_started``), not at the transcript: the
live model can call a tool before its transcript lands, and that call belongs
to the turn being spoken, not the previous one.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from core.brain import log as brain_log
from core.brain.brain import Brain
from core.brain.decision import Decision

log = structlog.get_logger("emma.brain_shadow")

_APP_DIRS = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")


@dataclass
class _Turn:
    n: int
    started: float
    decision: Decision | None = None
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False  # the next turn started (or the session ended)
    written: bool = False


class Shadow:
    def __init__(self, brain: Brain | None, log_path: Path | None = None) -> None:
        self.brain = brain
        self.log_path = log_path
        self.session = uuid.uuid4().hex[:12]
        self._turn: _Turn | None = None
        self._n = 0
        self._lock = threading.Lock()

    # ---- hooks (called from the live pipeline; must never raise) -------------------

    def on_speech_started(self) -> None:
        try:
            with self._lock:
                self._flush_locked()
                self._n += 1
                self._turn = _Turn(self._n, time.time())
        except Exception as exc:
            log.warning("brain_shadow_hook_failed", hook="speech_started", error=str(exc))

    def on_transcript(self, text: str) -> asyncio.Task[None] | None:
        """Schedule the decision; never awaits on the caller's frame path.

        The turn is captured NOW: a decision that lands after the user has
        started the next turn still belongs to the turn its words came from.
        """
        try:
            with self._lock:
                if self._turn is None:  # transcript with no VAD onset seen
                    self._n += 1
                    self._turn = _Turn(self._n, time.time())
                turn = self._turn
            return asyncio.get_running_loop().create_task(self._decide_into(turn, text))
        except Exception as exc:
            log.warning("brain_shadow_hook_failed", hook="transcript", error=str(exc))
            return None

    async def _decide_into(self, turn: _Turn, text: str) -> None:
        try:
            decision = await asyncio.to_thread(self._decide, text)
            with self._lock:
                if turn.decision is None:
                    turn.decision = decision
                if turn.closed:
                    self._write_locked(turn)
        except Exception as exc:
            log.warning("brain_shadow_hook_failed", hook="decide", error=str(exc))

    def on_tool(self, name: str, args: dict[str, Any]) -> None:
        try:
            with self._lock:
                if self._turn is not None:
                    clean = {k: v for k, v in args.items() if k not in ("confirmed", "cancelled")}
                    self._turn.tools.append((name, clean))
        except Exception as exc:
            log.warning("brain_shadow_hook_failed", hook="tool", error=str(exc))

    def close(self) -> None:
        try:
            with self._lock:
                self._flush_locked()
        except Exception as exc:
            log.warning("brain_shadow_hook_failed", hook="close", error=str(exc))

    # ---- internals -----------------------------------------------------------------

    def _decide(self, text: str) -> Decision:
        """Always a Decision — a missing or failing brain is a logged defer, never silence."""
        if self.brain is None:
            return Decision(utterance=text, normalized="", verdict="defer", reason="brain_not_ready")
        try:
            return self.brain.decide(text)
        except Exception as exc:
            log.warning("brain_decide_failed", error=str(exc))
            return Decision(
                utterance=text, normalized="", verdict="defer",
                reason=f"brain_error:{type(exc).__name__}",
            )

    def _flush_locked(self) -> None:
        t = self._turn
        self._turn = None
        if t is None:
            return
        t.closed = True
        self._write_locked(t)  # no-op until its decision lands

    def _write_locked(self, t: _Turn) -> None:
        if t.written or t.decision is None:
            return  # no words yet (a cough), or already logged
        t.written = True
        d = t.decision
        rt_tool, rt_args = (t.tools[0] if t.tools else (None, {}))
        brain_tool = d.chosen if d.verdict == "local" else None
        agree: bool | None
        if d.verdict == "local":
            agree = brain_tool == rt_tool
        elif d.verdict in ("none", "clarify", "refuse"):
            agree = rt_tool is None
        else:
            agree = None  # deferred: the brain made no claim to compare
        brain_log.write(
            d,
            path=self.log_path,
            extra={
                "session": self.session,
                "turn": t.n,
                "brain_verdict": d.verdict,
                "brain_tool": brain_tool,
                "realtime_tool": rt_tool,
                "realtime_args": rt_args,
                "realtime_tools_in_turn": [n for n, _ in t.tools],
                "agree": agree,
            },
        )


# ---- the one live shadow, and the brain it uses ------------------------------------

_shadow: Shadow | None = None
_brain: Brain | None = None
_brain_lock = threading.Lock()
_loading = False


def installed_apps() -> tuple[str, ...]:
    """Display names of installed .app bundles — a directory listing, no probing."""
    names: set[str] = set()
    for d in _APP_DIRS:
        try:
            names.update(p.stem for p in d.iterdir() if p.suffix == ".app")
        except OSError:
            continue
    return tuple(sorted(names))


def _load_brain() -> None:
    """Build the live brain once per daemon (background thread; ~2 s)."""
    global _brain, _loading
    try:
        from core.brain.embed import Embedder
        from core.brain.index import DEFAULT_PATH
        from core.brain_host import build_index, thresholds_from_settings, tool_docs, vocab

        docs = tool_docs(available_only=True)
        embedder = Embedder()
        index = build_index(embedder, DEFAULT_PATH, seed_docs=docs) if embedder.available() else None
        brain = Brain(docs, vocab(apps=installed_apps()), thresholds_from_settings(), index, embedder)
        with _brain_lock:
            _brain = brain
        log.info("brain_shadow_ready", tools=len(docs), knn=brain.retriever.has_knn,
                 examples=len(index) if index else 0)
    except Exception as exc:
        log.warning("brain_shadow_load_failed", error=str(exc))
    finally:
        _loading = False


def start_session() -> Shadow:
    """A fresh shadow for a wake session; starts loading the brain if needed."""
    global _shadow, _loading
    with _brain_lock:
        if _brain is None and not _loading:
            _loading = True
            threading.Thread(target=_load_brain, name="brain-shadow-load", daemon=True).start()
        _shadow = Shadow(_brain)
    return _shadow


def end_session() -> None:
    global _shadow
    if _shadow is not None:
        _shadow.close()
    _shadow = None


def current() -> Shadow | None:
    s = _shadow
    if s is not None and s.brain is None and _brain is not None:
        s.brain = _brain  # loaded mid-session: use it from the next turn on
    return s
