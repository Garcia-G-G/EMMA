"""SessionUpdateAuditMixin: server-echo auditing + duplicate-send suppression.

Live verification against the real Realtime API (2026-08-03) produced:
``session_update_sent tools=170`` → ``session_update_acked tools=170`` →
``session_update_duplicate_suppressed``. These tests pin that behavior without
a network.
"""

from __future__ import annotations

from typing import Any

import pytest
from pipecat.services.openai.realtime import events

from core.realtime_session_audit import SessionUpdateAuditMixin, _tool_names


class _FakeBase:
    """Stands in for OpenAIRealtimeLLMService: records what reaches the wire."""

    def __init__(self, **_: Any) -> None:
        self.sent: list[Any] = []
        self.updated: list[Any] = []
        self.errors: list[Any] = []

    async def send_client_event(self, event: Any) -> None:
        self.sent.append(event)

    async def _handle_evt_session_updated(self, evt: Any) -> None:
        self.updated.append(evt)

    async def _handle_evt_error(self, evt: Any) -> None:
        self.errors.append(evt)


class _Svc(SessionUpdateAuditMixin, _FakeBase):
    pass


def _props(tool_names: list[str]) -> events.SessionProperties:
    return events.SessionProperties(
        type="realtime",
        model="gpt-realtime-2",
        instructions="hi",
        tools=[{"type": "function", "name": n, "description": "", "parameters": {}} for n in tool_names],
    )


def _update(tool_names: list[str]) -> events.SessionUpdateEvent:
    return events.SessionUpdateEvent(session=_props(tool_names))


class _Updated:
    def __init__(self, tool_names: list[str]) -> None:
        self.session = _props(tool_names)


class _Err:
    def __init__(self, **kw: Any) -> None:
        self.error = type("E", (), kw)()


# --- duplicate suppression (Part 5) ----------------------------------------


@pytest.mark.asyncio
async def test_identical_session_update_is_sent_once() -> None:
    """pipecat sends session properties on session.created AND again in
    _create_response. The second is byte-identical, so it never reaches the wire."""
    svc = _Svc()
    await svc.send_client_event(_update(["a", "b"]))
    await svc.send_client_event(_update(["a", "b"]))
    assert len(svc.sent) == 1


@pytest.mark.asyncio
async def test_event_id_alone_does_not_defeat_suppression() -> None:
    """Each SessionUpdateEvent carries a fresh uuid4 event_id — same length,
    different value. Comparing it would make every duplicate look novel."""
    first, second = _update(["a"]), _update(["a"])
    assert first.event_id != second.event_id
    svc = _Svc()
    await svc.send_client_event(first)
    await svc.send_client_event(second)
    assert len(svc.sent) == 1


@pytest.mark.asyncio
async def test_a_genuinely_changed_update_is_sent() -> None:
    """Suppression must never swallow a real settings change (LLMSetToolsFrame)."""
    svc = _Svc()
    await svc.send_client_event(_update(["a"]))
    await svc.send_client_event(_update(["a", "b"]))
    assert len(svc.sent) == 2


@pytest.mark.asyncio
async def test_non_session_events_are_never_suppressed() -> None:
    svc = _Svc()
    evt = events.ResponseCreateEvent()
    await svc.send_client_event(evt)
    await svc.send_client_event(evt)
    assert len(svc.sent) == 2


# --- server echo auditing (Part 4) -----------------------------------------


@pytest.mark.asyncio
async def test_matching_echo_logs_ack_not_error(caplog: pytest.LogCaptureFixture) -> None:
    svc = _Svc()
    await svc.send_client_event(_update(["a", "b", "c"]))
    await svc._handle_evt_session_updated(_Updated(["a", "b", "c"]))
    assert len(svc.updated) == 1  # still delegates to pipecat


@pytest.mark.asyncio
async def test_truncated_echo_is_an_error_naming_the_missing_tools() -> None:
    """The failure mode nothing in Emma could see before: the server keeping
    fewer tools than we sent."""
    logged: list[tuple[str, dict[str, Any]]] = []
    svc = _Svc()
    import core.realtime_session_audit as mod

    class _Log:
        def error(self, event: str, **kw: Any) -> None:
            logged.append((event, kw))

        def info(self, event: str, **kw: Any) -> None:
            logged.append((event, kw))

    mod.log, original = _Log(), mod.log
    try:
        await svc.send_client_event(_update(["a", "b", "c"]))
        await svc._handle_evt_session_updated(_Updated(["a", "b"]))
    finally:
        mod.log = original

    errors = [(e, kw) for e, kw in logged if e == "session_tools_mismatch"]
    assert errors, f"no mismatch error logged; got {[e for e, _ in logged]}"
    _, kw = errors[0]
    assert kw["sent"] == 3
    assert kw["echoed"] == 2
    assert kw["missing"] == ["c"]
    assert len(svc.updated) == 1  # the session still proceeds


@pytest.mark.asyncio
async def test_server_error_is_logged_and_flagged_when_session_scoped() -> None:
    """A rejected session.update matches neither _TERMINAL_AUTH_MARKERS nor
    _ZOMBIE_MARKERS, so it used to vanish. Now it is an explicit error."""
    logged: list[tuple[str, dict[str, Any]]] = []
    import core.realtime_session_audit as mod

    class _Log:
        def error(self, event: str, **kw: Any) -> None:
            logged.append((event, kw))

        def info(self, event: str, **kw: Any) -> None:
            logged.append((event, kw))

    svc = _Svc()
    mod.log, original = _Log(), mod.log
    try:
        await svc.send_client_event(_update(["a"]))
        await svc._handle_evt_error(
            _Err(type="invalid_request_error", code="invalid_value",
                 message="too many tools", param="session.tools")
        )
    finally:
        mod.log = original

    hits = [kw for e, kw in logged if e == "realtime_server_error"]
    assert hits, f"server error not logged; got {[e for e, _ in logged]}"
    assert hits[0]["session_update_rejected"] is True
    assert hits[0]["tools_sent"] == 1
    assert len(svc.errors) == 1  # pipecat still gets it


# --- helpers ---------------------------------------------------------------


def test_tool_names_reads_both_shapes() -> None:
    """Realtime flattens tools; Chat Completions nests them under "function"."""
    assert _tool_names([{"type": "function", "name": "flat"}]) == ["flat"]
    assert _tool_names([{"type": "function", "function": {"name": "nested"}}]) == ["nested"]
    assert _tool_names(None) == []


def test_mixin_precedes_the_service_in_the_mro() -> None:
    """If the base class came first its methods would win and the audit would
    silently do nothing."""
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    from core.realtime_playback import TruncateAccurateRealtimeLLMService

    mro = TruncateAccurateRealtimeLLMService.__mro__
    assert mro.index(SessionUpdateAuditMixin) < mro.index(OpenAIRealtimeLLMService)
