"""VOICE-1 Part 1 — the cascade pipeline: Scribe -> BrainProcessor -> ElevenLabs, no LLM."""

from __future__ import annotations

from typing import Any

import aiohttp
import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.tests.utils import run_test

from config.settings import settings
from core import cascade, orchestrator, session_memory
from core.brain.decision import Decision
from core.brain.processor import BrainProcessor
from core.conversation import EchoGateProcessor
from core.echo_gate import EchoGateFilter


@pytest.fixture(autouse=True)
def _cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "sk_test")
    monkeypatch.setattr(settings, "VOICE_ID", "voice123")


def _flat(p: Any) -> list[Any]:
    """Every processor, depth-first (the task wraps our Pipeline in its own)."""
    if isinstance(p, Pipeline):
        return [x for k in p.processors for x in _flat(k)]
    return [p]


@pytest.mark.asyncio
async def test_pipeline_is_vad_scribe_brain_tts_with_formats_at_the_boundary() -> None:
    async with aiohttp.ClientSession() as http:
        task, brain_proc, stt = await cascade.build_pipeline(http)
    procs = _flat(task.pipeline)
    names = [type(p).__name__ for p in procs]
    order = ["VADProcessor", "ScribeSTT", "_UserTurnTap", "BrainProcessor",
             "CachedTTS", "_SpokenTap", "EchoGateProcessor"]
    assert [n for n in names if n in order] == order
    assert "OpenAIRealtimeLLMService" not in " ".join(names)
    assert "TruncateAccurateRealtimeLLMService" not in names
    # Scribe: realtime model, language UNSET (auto-detect = code-switching), 16 kHz in.
    assert stt._settings.model == "scribe_v2_realtime"
    assert stt._settings.language is None
    assert stt._init_sample_rate == cascade.MIC_RATE_HZ
    tts = next(p for p in procs if isinstance(p, ElevenLabsHttpTTSService))
    assert tts._settings.model == "eleven_v4_turbo"
    assert tts._settings.voice == "voice123"
    assert tts._init_sample_rate == cascade.SPEAKER_RATE_HZ
    vad = next(p for p in procs if isinstance(p, VADProcessor))
    assert vad._vad_controller._vad_analyzer.params.stop_secs == settings.CASCADE_VAD_STOP_S
    assert isinstance(brain_proc, BrainProcessor)


@pytest.mark.asyncio
async def test_scribe_emits_one_transcript_per_utterance() -> None:
    """The server sends committed_transcript AND ..._with_timestamps; pipecat pushed both."""
    stt = cascade.ScribeSTT(api_key="sk_test", sample_rate=16000)
    pushed: list[Any] = []

    async def capture(frame: Any, direction: Any = None) -> None:
        pushed.append(frame)

    stt.push_frame = capture  # type: ignore[method-assign]

    async def noop(*a: Any, **k: Any) -> None:
        return None

    stt.stop_processing_metrics = noop  # type: ignore[method-assign]
    msg = {"text": "Emma, abre Safari.", "language_code": "es"}
    await stt._process_response({"message_type": "committed_transcript", **msg})
    await stt._process_response({"message_type": "committed_transcript_with_timestamps", **msg,
                                 "words": None})
    transcripts = [f for f in pushed if isinstance(f, TranscriptionFrame)]
    assert [t.text for t in transcripts] == ["Emma, abre Safari."]


def _decision(verdict: str, reason: str = "x", text: str = "abre safari") -> Decision:
    return Decision(utterance=text, normalized=text, verdict=verdict, reason=reason)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_transcript_becomes_one_spoken_turn_and_one_record() -> None:
    records: list[tuple[Decision, str, dict[str, object]]] = []
    proc = BrainProcessor(lambda: (lambda t: _decision("refuse", "safety:x", t)),
                          record=lambda d, s, e: records.append((d, s, e)))
    down, _ = await run_test(
        proc,
        frames_to_send=[TranscriptionFrame("Emma, borra todo", "u", "t")],
        expected_down_frames=[TranscriptionFrame, TTSSpeakFrame],
    )
    said = next(f for f in down if isinstance(f, TTSSpeakFrame)).text
    assert said and len(records) == 1
    d, spoken, extra = records[0]
    assert d.verdict == "refuse" and spoken == said and extra["path"] == "template"
    assert "decide_ms" in extra["latency"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_no_brain_yet_still_answers_and_records() -> None:
    records: list[Decision] = []
    proc = BrainProcessor(lambda: None, record=lambda d, s, e: records.append(d))
    await run_test(proc, frames_to_send=[TranscriptionFrame("abre safari", "u", "t")],
                   expected_down_frames=[TranscriptionFrame, TTSSpeakFrame])
    assert records[0].reason == "brain_not_ready"


@pytest.mark.asyncio
async def test_a_failing_brain_is_a_logged_defer_not_silence() -> None:
    def boom(text: str) -> Decision:
        raise RuntimeError("x")

    records: list[Decision] = []
    proc = BrainProcessor(lambda: boom, record=lambda d, s, e: records.append(d))
    await run_test(proc, frames_to_send=[TranscriptionFrame("abre safari", "u", "t")],
                   expected_down_frames=[TranscriptionFrame, TTSSpeakFrame])
    assert records[0].reason == "brain_error:RuntimeError"


@pytest.mark.asyncio
async def test_noise_says_nothing() -> None:
    proc = BrainProcessor(lambda: (lambda t: _decision("none", "noise:stt", t)))
    await run_test(proc, frames_to_send=[TranscriptionFrame("mm", "u", "t")],
                   expected_down_frames=[TranscriptionFrame])


@pytest.mark.asyncio
async def test_onset_while_emma_speaks_interrupts_her() -> None:
    tap = cascade._UserTurnTap()
    await run_test(
        tap,
        frames_to_send=[BotStartedSpeakingFrame(), VADUserStartedSpeakingFrame()],
        expected_down_frames=[BotStartedSpeakingFrame, VADUserStartedSpeakingFrame,
                              InterruptionFrame],
        expected_up_frames=[InterruptionFrame],
    )
    assert any(e.kind == "speech_started" for e in session_memory.recent(5))


@pytest.mark.asyncio
async def test_onset_while_silent_does_not_interrupt() -> None:
    await run_test(cascade._UserTurnTap(), frames_to_send=[VADUserStartedSpeakingFrame()],
                   expected_down_frames=[VADUserStartedSpeakingFrame])


@pytest.mark.asyncio
async def test_transcripts_reach_session_memory_cleaned() -> None:
    await run_test(cascade._UserTurnTap(),
                   frames_to_send=[TranscriptionFrame("abre safari", "u", "t")],
                   expected_down_frames=[TranscriptionFrame])
    assert session_memory.last_user_speech_text() == "abre safari"


@pytest.mark.asyncio
async def test_echo_reference_is_resampled_to_the_mic_rate() -> None:
    got: list[bytes] = []

    class Gate(EchoGateFilter):
        def push_reference(self, audio: bytes) -> None:
            got.append(audio)

    proc = EchoGateProcessor(Gate(), reference_rate=16000)
    tone = (np.sin(np.arange(2400) / 10) * 8000).astype(np.int16).tobytes()  # 100 ms @ 24 kHz
    await run_test(proc, frames_to_send=[OutputAudioRawFrame(tone, 24000, 1)],
                   expected_down_frames=[OutputAudioRawFrame])
    assert abs(len(got[0]) // 2 - 1600) <= 16  # 100 ms @ 16 kHz


def test_the_wake_session_follows_voice_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(cascade, "run_session", lambda **k: called.append("cascade"))
    monkeypatch.setattr(orchestrator.conversation, "run_session",
                        lambda **k: called.append("realtime"))
    orchestrator._run_session()
    monkeypatch.setattr(settings, "VOICE_BACKEND", "realtime")
    orchestrator._run_session()
    assert called == ["cascade", "realtime"]


@pytest.mark.asyncio
async def test_no_voice_id_means_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VOICE_ID", "")

    async def must_not_build(*a: Any, **k: Any) -> Any:
        raise AssertionError("built a pipeline without a voice")

    monkeypatch.setattr(cascade, "build_pipeline", must_not_build)
    await cascade.run_session()


