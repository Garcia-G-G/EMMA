"""GA Realtime bridge shape tests (LANDING-25.0.3).

The live OpenAI handshake can't run here (no network); these assert the GA SHAPE
of what the bridge SENDS — the half that broke when the beta API was retired.
"""

from __future__ import annotations

import asyncio

import pytest

from backend import demo_session

# ---- B: session.update is GA shape ------------------------------------------


def test_session_update_is_ga_shape():
    cfg = demo_session.session_config("es")
    assert cfg["type"] == "session.update"
    s = cfg["session"]
    assert s["type"] == "realtime"                       # GA: required marker
    assert s["output_modalities"] == ["audio"]           # GA: not "modalities"
    assert "modalities" not in s and "voice" not in s    # beta fields gone from root


def test_voice_and_audio_format_are_ga_nested():
    s = demo_session.session_config("es")["session"]
    assert s["audio"]["output"]["voice"] == "coral"      # voice moved under audio.output
    assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["input"]["turn_detection"] == {"type": "server_vad"}


def test_tools_unchanged_exactly_four():
    s = demo_session.session_config("es")["session"]
    assert {t["name"] for t in s["tools"]} == set(demo_session.DEMO_TOOL_NAMES)
    assert len(s["tools"]) == 4 and s["tool_choice"] == "auto"
    assert all(t["type"] == "function" for t in s["tools"])  # GA flat function tool


def test_persona_and_lang_preserved():
    assert "vive en tu Mac" in demo_session.session_config("es")["session"]["instructions"]
    assert "lives on your Mac" in demo_session.session_config("en")["session"]["instructions"]


# ---- the session.created gate -----------------------------------------------


class _FakeOAI:
    """Minimal async-iterable + send() stand-in for the OpenAI WS."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[str] = []

    def __aiter__(self):
        async def gen():
            for f in self._frames:
                yield f
        return gen()

    async def send(self, raw):
        self.sent.append(raw)


def test_await_session_created_returns_event():
    oai = _FakeOAI(['{"type":"session.created","session":{"type":"realtime"}}'])
    ev = asyncio.run(demo_session._await_session_created(oai))
    assert ev["type"] == "session.created"


def test_await_session_created_raises_on_error_frame():
    oai = _FakeOAI(['{"type":"error","error":{"message":"bad","code":"x"}}'])
    with pytest.raises(RuntimeError, match="openai_realtime_error"):
        asyncio.run(demo_session._await_session_created(oai))


def test_await_session_created_times_out_without_event():
    oai = _FakeOAI([])  # OpenAI never sends session.created
    with pytest.raises(TimeoutError):
        asyncio.run(demo_session._await_session_created(oai, timeout=0.05))


# ---- no beta header / beta model --------------------------------------------


def test_no_beta_header_or_model_anywhere():
    import inspect
    src = inspect.getsource(demo_session)
    assert '"OpenAI-Beta":' not in src        # the header DICT KEY (comment mention is fine)
    from backend.config import settings
    assert "gpt-realtime-2" not in settings.OPENAI_REALTIME_MODEL  # GA model name
