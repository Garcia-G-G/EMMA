"""Prompt 21: unit shapes for B24-B29 (session memory, self-talk invariant,
fuzzy suggestions, language mirror, STT-correction directive, confidence
guard, anaphora)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import session_memory
from core.confidence import is_low_confidence
from tools.disambiguation import suggest_similar, suggestion_question


@pytest.fixture(autouse=True)
def _fresh_session():
    session_memory.clear()
    yield
    session_memory.clear()


# ---- B24: session memory + ordering invariant -------------------------------


class TestSessionMemory:
    def test_push_and_recent_order(self):
        session_memory.push_event("user", "speech", "hola")
        session_memory.push_event("assistant", "speech", "qué onda")
        evs = session_memory.recent()
        assert [e.role for e in evs] == ["user", "assistant"]
        assert evs[-1].content == "qué onda"

    def test_last_user_turn_ts(self):
        assert session_memory.last_user_turn_ts() is None
        session_memory.push_event("user", "speech", "hola")
        assert session_memory.last_user_turn_ts() is not None

    def test_confirmation_request_and_consumption(self):
        session_memory.push_event("tool", "requires_confirmation:delete_note")
        assert session_memory.last_tool_request_confirmation_ts("delete_note") is not None
        session_memory.consume_confirmation_request("delete_note")
        assert session_memory.last_tool_request_confirmation_ts("delete_note") is None

    def test_secretish_args_never_logged(self):
        session_memory.record_completed_action(
            "remember_secret", {"name": "wifi", "password": "hunter2"}, "guarda esto"
        )
        action = session_memory.recent_completed_actions()[-1]
        assert "password" not in action["args"]
        assert action["args"]["name"] == "wifi"


class _FakeParams(SimpleNamespace):
    """Stands in for pipecat FunctionCallParams."""


def _handler():
    from core.conversation import SessionControl, _make_function_handler

    return _make_function_handler(SessionControl())


def _call(handler, name, args):
    payloads: list[dict] = []

    async def cb(payload):
        payloads.append(payload)

    params = _FakeParams(function_name=name, arguments=args, result_callback=cb)
    asyncio.run(handler(params))
    return payloads[-1]


class TestConfirmationInvariant:
    def test_self_talk_confirm_refused(self, monkeypatch):
        """Question asked, NO user speech after it → confirmed call refused."""
        handler = _handler()
        session_memory.push_event("user", "speech", "borra mi nota compras see")
        session_memory.push_event("tool", "requires_confirmation:delete_note")
        # No new user event — the LLM is answering itself.
        payload = _call(handler, "delete_note", {"title": "Compras", "confirmed": True})
        assert payload["success"] is False
        assert "con tu voz" in payload["user_message"]

    def test_confirm_after_real_user_turn_proceeds(self, monkeypatch):

        async def fake_dispatch(name, args):
            from tools.base import ToolResult

            return ToolResult(True, {"deleted": 1}, "Listo, borré la nota.", False)

        import core.conversation as conv

        monkeypatch.setattr(conv, "dispatch", fake_dispatch)
        handler = _handler()
        session_memory.push_event("tool", "requires_confirmation:delete_note")
        session_memory.push_event("user", "speech", "sí, bórrala")  # AFTER the question
        payload = _call(handler, "delete_note", {"title": "Compras", "confirmed": True})
        assert payload["success"] is True

    def test_preemptive_confirm_without_question_allowed(self, monkeypatch):
        """'borra X, sí, seguro' in one breath — no prior question to spoof."""
        import core.conversation as conv
        from tools.base import ToolResult

        monkeypatch.setattr(
            conv, "dispatch", AsyncMock(return_value=ToolResult(True, None, "Listo.", False))
        )
        handler = _handler()
        session_memory.push_event("user", "speech", "borra la nota compras, sí, seguro")
        payload = _call(handler, "delete_note", {"title": "Compras", "confirmed": True})
        assert payload["success"] is True

    def test_consumed_question_cannot_be_replayed(self, monkeypatch):
        """After a granted confirm, a second confirmed call needs a NEW question."""
        import core.conversation as conv
        from tools.base import ToolResult

        monkeypatch.setattr(
            conv, "dispatch", AsyncMock(return_value=ToolResult(True, None, "Listo.", False))
        )
        handler = _handler()
        session_memory.push_event("tool", "requires_confirmation:delete_note")
        session_memory.push_event("user", "speech", "sí")
        assert _call(handler, "delete_note", {"confirmed": True})["success"] is True
        # Replay without a fresh question: pending request was consumed → treated
        # as preemptive consent (allowed) — but a fresh QUESTION with no speech
        # after it must refuse again:
        session_memory.push_event("tool", "requires_confirmation:delete_note")
        assert _call(handler, "delete_note", {"confirmed": True})["success"] is False


# ---- B25: fuzzy suggestions --------------------------------------------------


class TestSuggestSimilar:
    def test_catches_voice_mistranscription(self):
        hits = suggest_similar(
            "learning-rots-local",
            ["learning-roots-local", "learning-rooks-local", "prod-cluster"],
        )
        assert len(hits) == 2
        assert all(r >= 0.55 for _, r in hits)
        assert hits[0][1] >= hits[1][1]  # sorted desc

    def test_nonsense_stays_out(self):
        assert suggest_similar("zzz-nothing", ["learning-roots-local"]) == []

    def test_question_phrasing(self):
        q = suggestion_question("x", [("a", 0.9), ("b", 0.8)], noun="una conexión")
        assert "¿Quisiste decir 'a' o 'b'?" in q

    @pytest.mark.asyncio
    async def test_contact_near_miss_suggests(self, monkeypatch):
        from core import dictionary
        from tools.contact_resolve import resolve_recipient

        monkeypatch.setattr(
            dictionary,
            "contacts",
            lambda: {
                "mom": SimpleNamespace(
                    name="Ana García", relation="madre", aliases=["mami", "mamá"], email="a@x.com"
                )
            },
        )
        monkeypatch.setattr(dictionary, "find_contact", lambda q: None)
        resolved, miss = resolve_recipient("Anna Garcia")
        assert resolved is None
        assert miss is not None and miss.requires_confirmation
        assert "Ana García" in miss.user_message

    @pytest.mark.asyncio
    async def test_address_passes_through(self):
        from tools.contact_resolve import resolve_recipient

        resolved, miss = resolve_recipient("ana@example.com")
        assert resolved == "ana@example.com" and miss is None


# ---- B26/B27: system prompt content ------------------------------------------


class TestSystemPromptDirectives:
    @pytest.mark.asyncio
    async def test_prompt_carries_all_new_sections(self, monkeypatch):
        import core.conversation as conv

        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        text = await conv._build_instructions()
        assert "# Language mirror (strict)" in text
        assert "tool results does NOT govern" in text
        assert "# Learn from corrections (mandatory, not optional)" in text
        assert "remember_stt_correction('Nill Ojeda', 'Neil Ojeda')" in text
        assert "remember_stt_correction('gilbergaciata', 'gilbergarciata')" in text
        assert "# Anaphora resolution" in text
        assert "recall_last_action" in text
        assert "Do NOT in the same turn generate the user's consent" in text
        assert "picked=<his answer>" in text


# ---- B28: confidence ----------------------------------------------------------


class TestLowConfidence:
    def test_echo_of_bot_speech_flags(self):
        bot = "Listo, son las siete de la mañana en Tokio."
        assert is_low_confidence("son las siete de la mañana", bot) is True

    def test_normal_command_passes(self):
        assert is_low_confidence("borra mi nota compras por favor") is False

    def test_confirmation_words_never_flag(self):
        assert is_low_confidence("sí") is False
        assert is_low_confidence("no") is False
        assert is_low_confidence("dale") is False

    def test_suspicious_fragment_flags(self):
        assert is_low_confidence("compras see") is True
        assert is_low_confidence("") is True

    def test_destructive_dispatch_hedges_on_flagged_turn(self, monkeypatch):
        handler = _handler()
        session_memory.push_event("user", "speech", "compras see")
        session_memory.push_event("user", "low_confidence", "compras see")
        payload = _call(handler, "delete_note", {"title": "Compras"})
        assert payload["requires_confirmation"] is True
        assert "no estoy segura" in payload["user_message"]


# ---- B29: anaphora -------------------------------------------------------------


class TestAnaphora:
    def test_ttl_filters_old_actions(self, monkeypatch):
        session_memory.record_completed_action("play_track", {"query": "Bad Bunny"}, "pon música")
        assert len(session_memory.recent_completed_actions(within_s=600)) == 1
        assert session_memory.recent_completed_actions(within_s=0.0) == []

    @pytest.mark.asyncio
    async def test_recall_last_action_describes_last(self):
        from tools.session_actions_tool import recall_last_action

        session_memory.record_completed_action("play_track", {"query": "Bad Bunny"}, "pon música")
        res = await recall_last_action()
        assert res.success
        assert res.data["last_action"]["name"] == "play_track"
        assert "play_track" in res.user_message

    @pytest.mark.asyncio
    async def test_recall_with_empty_session(self):
        from tools.session_actions_tool import recall_last_action

        res = await recall_last_action()
        assert res.data["last_action"] is None
        assert "a qué te refieres" in res.user_message
