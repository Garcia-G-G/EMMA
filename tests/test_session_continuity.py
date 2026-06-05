"""22-B31: the Pipecat micro-session is invisible — warm opens + continuation."""

from __future__ import annotations

import pytest

from core import session_memory


@pytest.fixture(autouse=True)
def _fresh():
    session_memory.clear()
    yield
    session_memory.clear()


class TestRecentMessagesForLlm:
    def test_chat_shape_and_cap(self):
        for i in range(5):
            session_memory.push_event("user", "speech", f"pregunta {i}")
            session_memory.push_event("assistant", "speech", f"respuesta {i}")
        msgs = session_memory.recent_messages_for_llm(3)
        assert len(msgs) == 3
        assert msgs[-1] == {"role": "assistant", "content": "respuesta 4"}
        assert all(set(m) == {"role", "content"} for m in msgs)

    def test_internal_events_excluded(self):
        session_memory.push_event("user", "speech_started", "")
        session_memory.push_event("user", "low_confidence", "blah")
        session_memory.push_event("tool", "completed", "{}")
        session_memory.push_event("user", "speech", "hola")
        msgs = session_memory.recent_messages_for_llm()
        assert msgs == [{"role": "user", "content": "hola"}]


class TestContinuationSeed:
    def _seed(self, monkeypatch, *, last_spoke_ago: float | None):
        import core.conversation as conv

        if last_spoke_ago is None:
            monkeypatch.setattr(conv.session_memory, "last_assistant_speech_ts", lambda: None)
        else:
            import time

            now = time.monotonic()
            monkeypatch.setattr(
                conv.session_memory, "last_assistant_speech_ts", lambda: now - last_spoke_ago
            )
        return conv._session_seed_messages()

    def test_recent_speech_adds_continuation_directive(self, monkeypatch):
        session_memory.push_event("user", "speech", "pon música")
        session_memory.push_event("assistant", "speech", "Listo, sonando.")
        seed = self._seed(monkeypatch, last_spoke_ago=30.0)
        contents = [m["content"] for m in seed if m["role"] == "system"]
        assert any("continuation of an in-progress conversation" in c for c in contents)
        # the warm history rides along, after the system messages
        assert {"role": "assistant", "content": "Listo, sonando."} in seed

    def test_old_speech_means_fresh_greeting(self, monkeypatch):
        seed = self._seed(monkeypatch, last_spoke_ago=600.0)
        contents = [m["content"] for m in seed if m["role"] == "system"]
        assert not any("continuation" in c for c in contents)

    def test_first_session_ever_has_no_continuation(self, monkeypatch):
        seed = self._seed(monkeypatch, last_spoke_ago=None)
        contents = [m["content"] for m in seed if m["role"] == "system"]
        assert not any("continuation" in c for c in contents)

    @pytest.mark.asyncio
    async def test_prompt_carries_continuity_rules(self, monkeypatch):
        from unittest.mock import AsyncMock

        import core.conversation as conv

        monkeypatch.setattr(conv, "priming_block", AsyncMock(return_value=""))
        text = await conv._build_instructions()
        assert "# Session continuity" in text
        assert "DO NOT greet" in text
        assert "# App routing" in text
        assert "# Tool failure recovery" in text
        assert "failure_reason" in text


class TestSessionMaxBump:
    def test_idle_timeout_is_15_minutes(self):
        from config.settings import settings

        assert settings.SESSION_MAX_S == 900
