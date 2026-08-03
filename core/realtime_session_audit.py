"""Make the Realtime `session.update` handshake observable — and send it once.

Two failures this module exists to prevent, both found by the 2026-08-03
pre-launch audit:

**1. Nobody checked what the server actually accepted.** ``core/conversation.py``
logged ``len(openai_tool_specs())`` — the *client's* count. If OpenAI had
silently truncated the tool list (the audit's leading hypothesis; disproven in
``_planning/notes/LAUNCH-1-VERIFY.md``, the server echoes all 183), or rejected
the whole update, nothing in Emma would have said so. pipecat surfaces a server
``error`` as a generic ``ErrorFrame``, and Emma's two watchers filter on
substrings — ``AuthErrorWatcher`` on ``_TERMINAL_AUTH_MARKERS``,
``DeadSessionWatcher`` on ``_ZOMBIE_MARKERS`` — so a rejected ``session.update``
matched neither and vanished. Now: every session logs what the server echoed,
a mismatch is an ERROR naming the missing tools, and every raw server error is
logged with its code/param before pipecat generalizes it.

**2. The session properties were sent twice.** Both sends come from pipecat,
not Emma: ``llm.py:799-802`` sends on ``session.created``, then ``llm.py:1104``
sends again inside ``_create_response``'s ``_llm_needs_conversation_setup``
block — which fires when the ``LLMContextFrame`` Emma queues at
``core/conversation.py:1657`` reaches ``_handle_context``. That frame is
load-bearing (it initializes the function-call pipeline, per CLAUDE.md), and
both sends live in vendored code, so the fix is here: skip a ``session.update``
whose payload is byte-identical to the one already sent. Provably
state-neutral, and it saves ~104 KB / ~18k tokens per session.

Coupling note: same contract as ``core/realtime_playback.py`` — this overrides
pipecat 1.2.1 private members (``_send_session_update`` is left alone;
``send_client_event``, ``_handle_evt_session_updated``, ``_handle_evt_error``
are wrapped). Every override delegates to ``super()``, so a pipecat upgrade
degrades to stock behavior rather than crashing.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pipecat.services.openai.realtime import events

log = structlog.get_logger("emma.realtime_session")


def _tool_names(tools: Any) -> list[str]:
    """Names out of either tool shape (Realtime flat, or Chat Completions nested)."""
    out: list[str] = []
    for t in tools or []:
        if isinstance(t, dict):
            name = t.get("name") or (t.get("function") or {}).get("name")
        else:
            name = getattr(t, "name", None)
        if name:
            out.append(str(name))
    return out


class SessionUpdateAuditMixin:
    """Audits the ``session.update`` handshake and de-duplicates the send.

    Mix in *before* ``OpenAIRealtimeLLMService`` so these overrides win.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Canonical JSON of the last session.update actually put on the wire.
        self._last_session_update: str | None = None
        self._sent_tool_names: list[str] = []

    # Read through helpers rather than attributes: tests build the service with
    # ``__new__`` to skip its network-y __init__, so these must not assume the
    # instance attributes exist.
    def _last_update(self) -> str | None:
        return getattr(self, "_last_session_update", None)

    def _sent_names(self) -> list[str]:
        return getattr(self, "_sent_tool_names", [])

    async def send_client_event(self, event: Any) -> None:
        if isinstance(event, events.SessionUpdateEvent):
            try:
                dumped = event.model_dump(exclude_none=True)
                # `event_id` is a fresh uuid4 per event (events.py:353), so it
                # differs on every send while the session properties are
                # identical. Compare everything else.
                dumped.pop("event_id", None)
                payload = json.dumps(dumped, sort_keys=True)
            except Exception:  # unserializable → don't block the send
                payload = None
            if payload is not None:
                if payload == self._last_update():
                    log.info("session_update_duplicate_suppressed", bytes=len(payload))
                    return
                self._last_session_update = payload
                self._sent_tool_names = _tool_names(getattr(event.session, "tools", None))
                log.info(
                    "session_update_sent",
                    tools=len(self._sent_tool_names),
                    bytes=len(payload),
                    instructions_chars=len(getattr(event.session, "instructions", "") or ""),
                )
        await super().send_client_event(event)  # type: ignore[misc]

    async def _handle_evt_session_updated(self, evt: Any) -> None:
        """Compare the SERVER's echo against what we sent. A mismatch is an error."""
        echoed = _tool_names(getattr(getattr(evt, "session", None), "tools", None))
        sent = self._sent_names()
        if sent and len(echoed) != len(sent):
            missing = [n for n in sent if n not in set(echoed)]
            log.error(
                "session_tools_mismatch",
                sent=len(sent),
                echoed=len(echoed),
                missing=missing[:40],
                hint="the server did not accept every tool we registered",
            )
        else:
            log.info("session_update_acked", tools=len(echoed))
        await super()._handle_evt_session_updated(evt)  # type: ignore[misc]

    async def _handle_evt_error(self, evt: Any) -> None:
        """Log the raw server error before pipecat flattens it into an ErrorFrame.

        pipecat's ``push_error`` stringifies the whole event into one message;
        neither of Emma's watchers matches a ``session.update`` rejection, so
        without this the session would just die quietly.
        """
        err = getattr(evt, "error", None)
        param = getattr(err, "param", None) or ""
        code = getattr(err, "code", None) or ""
        message = getattr(err, "message", "") or ""
        is_session = "session" in param.lower() or "session" in code.lower()
        log.error(
            "realtime_server_error",
            code=code,
            type=getattr(err, "type", ""),
            param=param,
            message=message[:500],
            session_update_rejected=is_session,
            tools_sent=len(self._sent_names()),
        )
        await super()._handle_evt_error(evt)  # type: ignore[misc]
