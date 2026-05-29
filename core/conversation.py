"""Pipecat-based conversation core for Emma.

Replaces the hand-rolled ``core/realtime.py`` WebSocket client with
Pipecat's :class:`OpenAIRealtimeLLMService` running inside a streaming
pipeline:

    LocalAudioTransport.input → OpenAIRealtimeLLMService → LocalAudioTransport.output

Function calls dispatch to the existing :mod:`tools.registry`. Wake
word remains local (openWakeWord) — the orchestrator opens a
conversation session only after wake fires.

Pipecat 1.2.1 API surface, verified by inspection of the installed
package source:

- ``OpenAIRealtimeLLMService`` lives at
  ``pipecat.services.openai.realtime.llm`` (the ``__init__.py`` of the
  ``realtime`` package is empty; the new-Prompt-13 sketch imported it
  one level higher which fails).
- Session config is a typed ``SessionProperties`` pydantic model from
  ``pipecat.services.openai.realtime.events``, with nested
  ``AudioConfiguration{input, output}``, ``PCMAudioFormat``,
  ``InputAudioTranscription``, ``TurnDetection`` objects.
- Function-call handlers receive a single
  :class:`pipecat.services.llm_service.FunctionCallParams` arg and
  report their result via ``await params.result_callback(payload)``.
"""

from __future__ import annotations

from typing import Any

import structlog
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    LLMContextFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    PCMAudioFormat,
    SessionProperties,
    TurnDetection,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from config.settings import settings
from core.echo_gate import EchoGateFilter
from memory.long_term import priming_block
from memory.reflection import schedule_reflection
from memory.short_term import append_turn, last_turns
from tools.registry import dispatch, openai_tool_specs

log = structlog.get_logger("emma.conversation")

# Native sample rate of the Realtime model.
SAMPLE_RATE_HZ = 24000

# Substrings in a Realtime error that mean "reconnecting will never help" —
# the key/model/permission is wrong, so terminate instead of looping. The
# first four are OpenAI session-level error codes (surfaced via an `error`
# server event). The HTTP markers cover the WebSocket-handshake rejection a
# bad key produces at connect time ("Error connecting: ... HTTP 401"), which
# never carries the `invalid_api_key` code. Transient failures (timeouts,
# DNS, 5xx) match none of these, so the existing reconnect still applies.
_TERMINAL_AUTH_MARKERS = (
    "invalid_api_key",
    "permission_denied",
    "model_not_found",
    "organization_not_authorized",
    "HTTP 401",
    "HTTP 403",
    "Unauthorized",
    "Forbidden",
)


def _looks_like_openai_key(s: str) -> bool:
    """Cheap shape check for an OpenAI key: ``sk-`` prefix, ≥40 chars, no spaces."""
    return bool(s) and s.startswith("sk-") and len(s) >= 40 and " " not in s and "\t" not in s


def _is_terminal_auth_error(message: str) -> bool:
    """True if `message` names an auth/config error that reconnecting can't fix."""
    return any(marker in message for marker in _TERMINAL_AUTH_MARKERS)


class EchoGateProcessor(FrameProcessor):
    """Watches BotStarted/StoppedSpeakingFrames and toggles the echo gate."""

    def __init__(self, gate: EchoGateFilter):
        super().__init__()
        self._gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._gate.set_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._gate.set_bot_speaking(False)
        await self.push_frame(frame, direction)


class TranscriptCollector(FrameProcessor):
    """Captures user + assistant transcripts for memory reflection.

    Collects TranscriptionFrames (user speech, pushed upstream by the
    Realtime LLM) and LLMTextFrames (assistant text, pushed downstream).
    When the bot stops speaking, the collected turn is appended to
    short-term memory and reflection is scheduled in the background.
    """

    def __init__(self):
        super().__init__()
        self._user_text = ""
        self._assistant_text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            self._user_text += frame.text + " "
        elif isinstance(frame, LLMTextFrame) and frame.text:
            self._assistant_text += frame.text
        elif isinstance(frame, BotStoppedSpeakingFrame):
            user = self._user_text.strip()
            assistant = self._assistant_text.strip()
            if user or assistant:
                append_turn(user, assistant)
                schedule_reflection(last_turns(4))
                log.debug("transcript_captured", user=user[:80], assistant=assistant[:80])
            self._user_text = ""
            self._assistant_text = ""
        await self.push_frame(frame, direction)


class AuthErrorWatcher(FrameProcessor):
    """Terminates the session on a known-terminal auth/config error.

    Pipecat's Realtime service reports failures by pushing an ``ErrorFrame``
    downstream (``push_error``). On a transient error it would otherwise
    reconnect forever; on a terminal auth error (bad key, missing model,
    permission denied) reconnecting is pointless. When we see one we log a
    single ``credentials_invalid`` line and cancel the pipeline task, so
    ``run_session`` returns and the orchestrator can exit non-zero.
    """

    def __init__(self) -> None:
        super().__init__()
        self._task: PipelineTask | None = None
        self.terminal_error: str | None = None

    def set_task(self, task: PipelineTask) -> None:
        self._task = task

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame):
            message = str(getattr(frame, "error", "") or "")
            if self.terminal_error is None and _is_terminal_auth_error(message):
                self.terminal_error = message
                log.error("credentials_invalid", reason=message[:200])
                if self._task is not None:
                    await self._task.cancel()
        await self.push_frame(frame, direction)


def _adapt_tool_specs_for_realtime(chat_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions tool specs are nested; Realtime expects them flat.

    ``tools.registry.openai_tool_specs()`` returns the Chat Completions
    shape; ``SessionProperties.tools`` accepts ``list[dict]`` in the GA
    Realtime shape (flat ``type``/``name``/``description``/``parameters``).
    """
    out: list[dict[str, Any]] = []
    for spec in chat_specs:
        if spec.get("type") == "function" and "function" in spec:
            fn = spec["function"]
            out.append(
                {
                    "type": "function",
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        else:
            out.append(spec)
    return out


async def _build_instructions() -> str:
    """System prompt for the Realtime session, with memory priming."""
    base = (
        "# Role\n"
        "You are Emma, Garcia's personal AI assistant on his Mac. "
        "You are like Jarvis — sharp, warm, capable. You control his "
        "apps, music, browser, files, and system through tools.\n\n"
        "# Personality\n"
        "- Confident, calm, slightly witty. Never flustered.\n"
        "- Talk to Garcia like a trusted colleague, not a customer.\n"
        "- Be direct. No filler, no hedging, no apologies.\n\n"
        "# Language\n"
        "- Garcia speaks Mexican Spanish (Monterrey) and English.\n"
        "- ALWAYS reply in the SAME language Garcia just spoke.\n"
        "- NEVER switch mid-response. NEVER use any other language.\n"
        "- Spanish: use 'tú', Mexican colloquialisms are fine.\n"
        "- If unsure, default to Spanish.\n\n"
        "# Response Length\n"
        "- 1 sentence for confirmations: 'Listo.', 'Done.'\n"
        "- 1-2 sentences for answers.\n"
        "- 3 sentences MAX for explanations. Never monologue.\n\n"
        "# Variety\n"
        "- NEVER start two consecutive responses the same way.\n"
        "- VARY confirmations: 'Listo', 'Ya', 'Hecho', 'Done', "
        "'On it', 'Got it'. Rotate.\n\n"
        "# Preambles (before tool calls)\n"
        "- For FAST tools (time, volume, open app): say NOTHING "
        "before calling. Just call the tool silently, then speak "
        "the result.\n"
        "- For SLOW tools (web search, browser): ONE short preamble "
        "only. 'Déjame ver.' or 'Checking.' — then stay silent "
        "until the result arrives.\n"
        "- NEVER say 'un momento', 'estoy verificando', 'let me "
        "check that for you', or any filler while waiting.\n"
        "- NEVER narrate what you're about to do. Just do it.\n\n"
        "# Tool Results\n"
        "- Speak the result in ONE sentence after the tool returns.\n"
        "- If a tool fails: say what went wrong briefly, suggest "
        "an alternative.\n"
        "- run_command: use ONE simple command. Never chain with "
        "&& or write inline scripts. Call multiple times if needed.\n\n"
        "# Forbidden\n"
        "- No filler: 'Great question!', 'Absolutely!', 'Of course!'\n"
        "- No closers: '¿Algo más?', 'Anything else?'\n"
        "- No self-description unless asked.\n"
        "- No languages other than Spanish and English.\n"
        "- No lists unless requested.\n"
        "- No repeating the same information.\n"
    )
    try:
        memory = await priming_block()
    except Exception as exc:
        log.warning("memory_priming_failed", error=str(exc))
        memory = ""
    if memory:
        base += f"\n# Memory\n{memory}\n"
    return base


async def _on_function_call(params: FunctionCallParams) -> None:
    """Pipecat function-call handler. Dispatches to the existing tools registry.

    Pipecat's ``register_function`` contract: handler receives a
    :class:`FunctionCallParams` and reports its result by awaiting
    ``params.result_callback(payload)``. The payload is serialized to
    JSON and sent back to the Realtime model as a ``function_call_output``.
    """
    name = params.function_name
    args = dict(params.arguments or {})
    log.info("fn_call", name=name, args=args)
    result = await dispatch(name, args)
    payload: dict[str, Any] = {
        "success": result.success,
        "user_message": result.user_message,
        "data": result.data,
        "requires_confirmation": result.requires_confirmation,
    }
    await params.result_callback(payload)


async def _build_session_properties() -> SessionProperties:
    instructions = await _build_instructions()
    return SessionProperties(
        type="realtime",
        model=settings.REALTIME_MODEL,
        output_modalities=["audio"],
        instructions=instructions,
        audio=AudioConfiguration(
            input=AudioInput(
                format=PCMAudioFormat(type="audio/pcm", rate=SAMPLE_RATE_HZ),
                transcription=InputAudioTranscription(model="whisper-1"),
                noise_reduction=InputAudioNoiseReduction(type="far_field"),
                turn_detection=TurnDetection(
                    type="server_vad",
                    threshold=0.75,
                    prefix_padding_ms=300,
                    silence_duration_ms=800,
                ),
            ),
            output=AudioOutput(
                format=PCMAudioFormat(type="audio/pcm", rate=SAMPLE_RATE_HZ),
                voice=settings.REALTIME_VOICE,
                speed=1.0,
            ),
        ),
        tools=_adapt_tool_specs_for_realtime(openai_tool_specs()),
        tool_choice="auto",
    )


async def build_pipeline() -> tuple[
    Pipeline, PipelineTask, LocalAudioTransport, LLMContext, AuthErrorWatcher
]:
    """Wire transport + Realtime LLM + tool handlers into a Pipecat pipeline.

    Returns (pipeline, task, transport, context, auth_watcher) so the
    orchestrator can manage their lifecycle. The transport opens the
    mic + speaker streams when the pipeline starts and closes them on
    cancel / idle-timeout. The auth_watcher lets run_session detect a
    terminal auth error and exit non-zero instead of reconnecting.
    """
    echo_gate = EchoGateFilter(tail_ms=600, barge_in_rms=4000.0)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE_HZ,
            audio_out_sample_rate=SAMPLE_RATE_HZ,
            audio_in_filter=echo_gate,
            vad_analyzer=SileroVADAnalyzer(),
        )
    )

    gate_proc = EchoGateProcessor(echo_gate)

    session_props = await _build_session_properties()
    llm = OpenAIRealtimeLLMService(
        api_key=settings.OPENAI_API_KEY,
        settings=OpenAIRealtimeLLMService.Settings(
            model=settings.REALTIME_MODEL,
            session_properties=session_props,
        ),
    )

    for spec in openai_tool_specs():
        fn = spec.get("function", spec)
        name = fn.get("name")
        if name:
            llm.register_function(name, _on_function_call)

    context = LLMContext(messages=[])
    assistant_aggregator = LLMAssistantAggregator(context)
    transcript_collector = TranscriptCollector()
    auth_watcher = AuthErrorWatcher()

    pipeline = Pipeline([
        transport.input(),
        llm,
        auth_watcher,  # right after the LLM so it sees its ErrorFrames first
        assistant_aggregator,
        transcript_collector,
        gate_proc,
        transport.output(),
    ])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE_HZ,
            audio_out_sample_rate=SAMPLE_RATE_HZ,
        ),
        idle_timeout_secs=float(settings.SESSION_MAX_S),
        cancel_on_idle_timeout=True,
    )
    auth_watcher.set_task(task)
    return pipeline, task, transport, context, auth_watcher


async def run_session() -> None:
    """Run one Pipecat session until idle, cancellation, or error.

    The orchestrator calls this once per wake-word detection. The
    pipeline starts the mic + speaker streams; OpenAI Realtime serves
    audio in/out + function calls; the runner blocks here until idle.

    Two credential guards: a pre-flight key-shape check before any session
    opens (raises SystemExit(2) on a missing/malformed key), and an
    auth-error watcher inside the pipeline that terminates with SystemExit(2)
    on a terminal auth error instead of reconnecting forever.
    """
    if not _looks_like_openai_key(settings.OPENAI_API_KEY):
        log.error(
            "credentials_invalid",
            field="OPENAI_API_KEY",
            present=bool(settings.OPENAI_API_KEY),
            length=len(settings.OPENAI_API_KEY or ""),
        )
        raise SystemExit(2)  # non-zero exit; launchd KeepAlive treats as real failure

    _pipeline, task, _transport, context, auth_watcher = await build_pipeline()
    runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
    log.info("conversation_start", voice=settings.REALTIME_VOICE, tools=len(openai_tool_specs()))
    try:
        await task.queue_frame(LLMContextFrame(context=context))
        await runner.run(task)
    finally:
        log.info("conversation_end")

    if auth_watcher.terminal_error is not None:
        # A terminal auth/config error surfaced; do not let the orchestrator
        # loop reopen the session. Bubble out as a non-zero exit.
        raise SystemExit(2)
