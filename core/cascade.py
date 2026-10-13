"""The LLM-free voice path: Scribe STT -> core/brain -> ElevenLabs TTS (VOICE-1).

::

    LocalAudioTransport.input (16 kHz, echo-gated)
      -> VADProcessor (Silero; its onset/stop drive Scribe's manual commit)
      -> ElevenLabsRealtimeSTTService (scribe_v2_realtime, language unset)
      -> _UserTurnTap (session memory, injection guard, barge-in)
      -> BrainProcessor (decide -> template / execute / thinker)
      -> ElevenLabsHttpTTSService (eleven_v4_turbo, 24 kHz, word timestamps)
      -> _SpokenTap (what was actually played, cut at the barge-in word)
      -> EchoGateProcessor -> _AudioLevelTap -> LocalAudioTransport.output (24 kHz)

Pipecat 1.2.1, verified against the installed package and the live API:

- ``ElevenLabsRealtimeSTTService`` (``pipecat.services.elevenlabs.stt``) maps a
  16 kHz pipeline to ``audio_format=pcm_16000`` and, in its default MANUAL commit
  strategy, commits on ``VADUserStoppedSpeakingFrame``. 1.2.1's transport ignores
  ``vad_analyzer`` (``LocalAudioTransportParams`` has no such field), so a
  ``VADProcessor`` is in the pipeline explicitly. Silero only takes 8/16 kHz,
  which is why the mic runs at 16 kHz.
- ``ElevenLabsTTSService`` (WebSocket, ``multi-stream-input``) rejects
  ``eleven_v4_turbo`` with ``unsupported_model``. ``ElevenLabsHttpTTSService``
  streams it from ``/stream/with-timestamps`` (word timestamps intact); the
  first audio arrived in ~300 ms median in a live probe. Emma speaks whole
  template sentences, so the WebSocket's token streaming buys nothing here.
- Output runs at 24 kHz (``pcm_24000``). The echo gate's reference is
  resampled to the mic rate at its boundary — the brain never sees audio.

The Realtime path (``core.conversation``) is untouched and selected by
``VOICE_BACKEND=realtime``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import aiohttp
import structlog
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndTaskFrame,
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from config.settings import settings
from core import (
    audio_devices,
    brain_shadow,
    events_bus,
    injection,
    session_memory,
    speaker,
    vocabulary,
)
from core.brain import log as brain_log
from core.brain.decision import Decision
from core.brain.processor import BrainProcessor, Outcome
from core.confidence import is_low_confidence
from core.echo_gate import EchoGateFilter, SpeechPhase
from core.redaction import redact

log = structlog.get_logger("emma.cascade")

MIC_RATE_HZ = 16000  # Silero VAD and Scribe's pcm_16000, both native
SPEAKER_RATE_HZ = 24000  # v4 Turbo pcm_24000


class ScribeSTT(ElevenLabsRealtimeSTTService):
    """``ElevenLabsRealtimeSTTService`` without its double transcript.

    Pipecat 1.2.1 skips ``committed_transcript`` when ``include_timestamps`` is
    on, but pushes ``committed_transcript_with_timestamps`` unconditionally —
    and the server sends that message even with timestamps off (seen live:
    both arrive for every utterance). Unpatched, every utterance became two
    TranscriptionFrames, i.e. two brain turns. Timestamps are off here, so the
    plain ``committed_transcript`` is the one transcript.
    """

    async def _on_committed_transcript_with_timestamps(self, data: dict[str, Any]) -> None:
        if self._include_timestamps:
            await super()._on_committed_transcript_with_timestamps(data)


class _UserTurnTap(FrameProcessor):
    """After STT: the user's turn reaches session memory, the injection guard, the UI.

    VAD onset is the confirmation invariant's turn marker (``core.injection``):
    only a transcript that follows a NEW onset after Emma's question can answer
    it. An onset while Emma is speaking is a barge-in — the echo gate already
    let it through only above the barge-in energy — so it interrupts her.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bot_speaking = False
        self.last_user_stop = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            session_memory.push_event("user", "speech_started", "")
            events_bus.publish("state", state="thinking")
            if self.bot_speaking:
                log.info("barge_in")
                await self.push_frame(frame, direction)
                await self.broadcast_interruption()  # type: ignore[no-untyped-call]
                return
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.last_user_stop = time.monotonic()
        elif isinstance(frame, TranscriptionFrame) and frame.text:
            cleaned = vocabulary.corrections(frame.text)
            if cleaned != frame.text:
                log.debug("transcript_corrected", before=frame.text, after=cleaned)
                frame.text = cleaned
            session_memory.push_event("user", "speech", cleaned)
            guard = injection.current()
            if guard is not None:
                guard.note_user_transcript(cleaned)
            if is_low_confidence(cleaned, _last_assistant_speech()):
                session_memory.push_event("user", "low_confidence", cleaned)
            log.info("user_transcript", text=cleaned[:120],
                     lang=getattr(frame, "language", None),
                     stop_to_text_ms=_ms_since(self.last_user_stop))
            if settings.EMMA_TEST_MODE:
                log.info("stt_user_test", text=cleaned)
        await self.push_frame(frame, direction)


class _SpokenTap(FrameProcessor):
    """After TTS: the words Emma actually got out, cut where a barge-in stopped her.

    ``ElevenLabsHttpTTSService`` turns its word timestamps into ``TTSTextFrame``s
    released in step with playback; on an interruption the unplayed ones never
    come. So what this collects is what the user heard, not what was planned.
    """

    def __init__(self, host: _Host | None = None) -> None:
        super().__init__()
        self._words: list[str] = []
        self._host = host

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and frame.text:
            self._words.append(frame.text)
        elif isinstance(frame, InterruptionFrame):
            self._flush(interrupted=True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._flush(interrupted=False)
            if self._host is not None and self._host.end_after_speech:
                # A tool asked to end the session (e.g. "apágate"): after its line is out.
                self._host.end_after_speech = False
                await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)

    def _flush(self, interrupted: bool) -> None:
        text = " ".join(self._words).strip()
        self._words.clear()
        if not text:
            return
        session_memory.push_event("assistant", "speech", text)
        if interrupted:
            log.info("speech_interrupted", heard=text[:120])
        if settings.EMMA_TEST_MODE:
            log.info("bot_text_test", text=text)


def _ms_since(t: float) -> float | None:
    return round((time.monotonic() - t) * 1000, 1) if t else None


def _last_assistant_speech() -> str:
    for ev in reversed(session_memory.recent(20)):
        if ev.role == "assistant" and ev.kind == "speech":
            return ev.content
    return ""


class _Turns:
    """Writes one ``brain_decisions.jsonl`` line per turn: decision + what happened."""

    def __init__(self) -> None:
        self.session = uuid.uuid4().hex[:12]
        self.n = 0

    def record(self, d: Decision, said: str, extra: dict[str, object]) -> None:
        self.n += 1
        brain_log.write(d, extra={"session": self.session, "turn": self.n,
                                  "backend": "cascade", **extra})


class _Host:
    """The brain's hands, host-side (VOICE-1 Part 2): ``core/brain`` never imports ``tools``.

    ``execute`` is ``tools.registry.dispatch`` — so the injection guard runs on
    every local execution: taint, sink provenance, and mic-only confirmation
    (a ``confirmed=True`` from the brain still needs the spoken yes the guard
    can see in session memory). Bookkeeping mirrors the Realtime handler: the
    question opens a pending confirmation, a success joins the anaphora tail.
    """

    TIMEOUT_S = 20.0

    def __init__(self) -> None:
        self._facts: list[str] = []
        self.end_after_speech = False

    async def load_facts(self) -> None:
        """Non-secret facts, confidence-ordered, read once per session (no embedding call)."""
        from memory.long_term import _recall_sync

        try:
            facts = await asyncio.to_thread(_recall_sync, None, 200)
            self._facts = [f.content for f in facts]
        except Exception as exc:
            log.warning("facts_load_failed", error=str(exc))

    def facts(self) -> list[str]:
        return self._facts

    def asked(self, tool: str) -> None:
        session_memory.push_event("tool", f"requires_confirmation:{tool}")

    async def execute(self, tool: str, args: dict[str, Any]) -> Outcome:
        from tools.registry import dispatch

        events_bus.publish("tool_started", name=tool)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(dispatch(tool, dict(args)), timeout=self.TIMEOUT_S)
        except TimeoutError:
            log.error("tool_timed_out", name=tool)
            return Outcome(False, "Esa acción se quedó pegada. Intenta de nuevo.")
        except Exception as exc:
            log.error("tool_failed", name=tool, error=str(exc))
            return Outcome(False, "Algo falló con esa acción.")
        elapsed = int((time.monotonic() - t0) * 1000)
        log.info("tool_completed", name=tool, elapsed_ms=elapsed, success=result.success,
                 by="brain")
        events_bus.publish("tool_completed", name=tool, elapsed_ms=elapsed,
                           success=result.success)
        if result.requires_confirmation:
            self.asked(tool)
        elif result.success:
            session_memory.record_completed_action(
                tool, dict(args), user_text=session_memory.last_user_speech_text()
            )
        if getattr(result, "ends_session", False):
            self.end_after_speech = True
        return Outcome(result.success, redact(result.user_message or ""),
                       asks=bool(result.requires_confirmation))


def _decide_getter() -> Any:
    brain = brain_shadow.loaded()
    return brain.decide if brain is not None else None


async def build_pipeline(
    http: aiohttp.ClientSession, host: _Host | None = None
) -> tuple[PipelineTask, BrainProcessor, ScribeSTT]:
    from core.conversation import EchoGateProcessor, _AudioLevelTap
    from emma.calibrate import load_calibration

    speech_phase = SpeechPhase()
    speaker.reset()
    cal = load_calibration()
    echo_gate = EchoGateFilter(
        tail_ms=600,
        barge_in_rms=cal.get("BARGE_IN_RMS_SPIKE", settings.BARGE_IN_RMS_SPIKE),
        phase_provider=speech_phase.current,
        barge_in_rms_window=cal.get("BARGE_IN_RMS_WINDOW", settings.BARGE_IN_RMS_WINDOW),
        window_ms=settings.BARGE_IN_WINDOW_MS,
        echo_cancel=settings.ECHO_CANCEL_ENABLED,
        echo_ref_buffer_ms=settings.ECHO_REF_BUFFER_MS,
        echo_corr_window_ms=settings.ECHO_CORR_WINDOW_MS,
        echo_corr_threshold=settings.ECHO_CORR_THRESHOLD,
        echo_corr_max_lag_ms=settings.ECHO_CORR_MAX_LAG_MS,
        echo_corr_lag_stride_ms=settings.ECHO_CORR_LAG_STRIDE_MS,
    )
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=MIC_RATE_HZ,
            audio_out_sample_rate=SPEAKER_RATE_HZ,
            audio_in_filter=echo_gate,
            input_device_index=audio_devices.test_input_device_index(),
        )
    )
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=MIC_RATE_HZ, params=VADParams(stop_secs=settings.CASCADE_VAD_STOP_S)
        )
    )
    stt = ScribeSTT(
        api_key=settings.ELEVENLABS_API_KEY,
        sample_rate=MIC_RATE_HZ,
        include_language_detection=True,
        # language deliberately unset: Scribe detects per utterance (code-switching)
        settings=ElevenLabsRealtimeSTTService.Settings(model=settings.ELEVENLABS_STT_MODEL),
        audio_passthrough=False,
    )
    tts = ElevenLabsHttpTTSService(
        api_key=settings.ELEVENLABS_API_KEY,
        aiohttp_session=http,
        sample_rate=SPEAKER_RATE_HZ,
        settings=ElevenLabsHttpTTSService.Settings(
            voice=settings.VOICE_ID, model=settings.ELEVENLABS_TTS_MODEL
        ),
    )
    turns = _Turns()
    host = host or _Host()
    brain_proc = BrainProcessor(_decide_getter, record=turns.record, host=host)
    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            _UserTurnTap(),
            brain_proc,
            tts,
            _SpokenTap(host),
            EchoGateProcessor(echo_gate, speech_phase, reference_rate=MIC_RATE_HZ),
            _AudioLevelTap(),
            transport.output(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=MIC_RATE_HZ, audio_out_sample_rate=SPEAKER_RATE_HZ
        ),
        idle_timeout_secs=float(settings.SESSION_MAX_S),
        cancel_on_idle_timeout=True,
    )
    return task, brain_proc, stt


_active_task: PipelineTask | None = None


async def stop_active_speech() -> bool:
    """Cut Emma mid-sentence (menubar "Parar"): the same InterruptionFrame as a barge-in."""
    task = _active_task
    if task is None:
        return False
    with contextlib.suppress(Exception):
        await task.queue_frame(InterruptionFrame())
        log.info("stop_active_speech")
        return True
    return False


async def run_session(immediate_command: bool = False) -> None:
    """One wake session on the cascade, until idle or cancellation."""
    del immediate_command  # no greeting to skip: the cascade speaks only when spoken to
    if not settings.VOICE_ID:
        log.error("voice_id_missing", fix="set VOICE_ID (an ElevenLabs voice id)")
        return
    brain_shadow.ensure_loading()
    injection.install()
    global _active_task
    host = _Host()
    await host.load_facts()
    async with aiohttp.ClientSession() as http:
        task, _brain_proc, stt = await build_pipeline(http, host)
        _active_task = task
        runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
        log.info("conversation_start", backend="cascade", voice=settings.VOICE_ID,
                 tts_model=settings.ELEVENLABS_TTS_MODEL, stt_model=settings.ELEVENLABS_STT_MODEL)
        try:
            await runner.run(task)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stt._disconnect(), timeout=3.0)  # type: ignore[no-untyped-call]
            _active_task = None
            injection.uninstall()
            log.info("conversation_end", backend="cascade")
