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
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
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
from tools.registry import dispatch, openai_tool_specs

log = structlog.get_logger("emma.conversation")

# Native sample rate of the Realtime model.
SAMPLE_RATE_HZ = 24000


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


def _build_instructions() -> str:
    """System prompt for the Realtime session.

    Much shorter than the pre-Pipecat pipeline's instructions: the model
    hears Garcia's voice directly, so the multi-paragraph LANGUAGE
    POLICY isn't needed - the model holds the conversation in whatever
    language he speaks.
    """
    return (
        "You are Emma, a warm bilingual voice assistant for Garcia. "
        "You hear his voice directly - match the language he speaks naturally "
        "and stay in that language for the whole turn. Be concise; this is a "
        "spoken conversation, not a written one. Prefer tools when one fits. "
        "After a tool runs, briefly confirm what happened."
    )


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


def _build_session_properties() -> SessionProperties:
    return SessionProperties(
        type="realtime",
        model=settings.REALTIME_MODEL,
        output_modalities=["audio"],
        instructions=_build_instructions(),
        audio=AudioConfiguration(
            input=AudioInput(
                format=PCMAudioFormat(type="audio/pcm", rate=SAMPLE_RATE_HZ),
                transcription=InputAudioTranscription(model="whisper-1"),
                turn_detection=TurnDetection(
                    type="server_vad",
                    threshold=0.5,
                    prefix_padding_ms=300,
                    silence_duration_ms=settings.VAD_SILENCE_MS,
                ),
            ),
            output=AudioOutput(
                format=PCMAudioFormat(type="audio/pcm", rate=SAMPLE_RATE_HZ),
                voice=settings.REALTIME_VOICE,
            ),
        ),
        tools=_adapt_tool_specs_for_realtime(openai_tool_specs()),
        tool_choice="auto",
    )


def build_pipeline() -> tuple[Pipeline, PipelineTask, LocalAudioTransport]:
    """Wire transport + Realtime LLM + tool handlers into a Pipecat pipeline.

    Returns the constructed (pipeline, task, transport) triple so the
    orchestrator can manage their lifecycle. The transport opens the
    mic + speaker streams when the pipeline starts and closes them on
    cancel / idle-timeout.
    """
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE_HZ,
            audio_out_sample_rate=SAMPLE_RATE_HZ,
            vad_analyzer=SileroVADAnalyzer(),
        )
    )

    llm = OpenAIRealtimeLLMService(
        api_key=settings.OPENAI_API_KEY,
        model=settings.REALTIME_MODEL,
        session_properties=_build_session_properties(),
    )

    # Register the same async handler for every registered tool. Pipecat
    # 1.2.1 supports a None-name catch-all but iterating gives clearer
    # log lines per tool.
    for spec in openai_tool_specs():
        fn = spec.get("function", spec)
        name = fn.get("name")
        if name:
            llm.register_function(name, _on_function_call)

    pipeline = Pipeline([transport.input(), llm, transport.output()])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE_HZ,
            audio_out_sample_rate=SAMPLE_RATE_HZ,
        ),
        idle_timeout_secs=float(settings.SESSION_MAX_S),
        cancel_on_idle_timeout=True,
    )
    return pipeline, task, transport


async def run_session() -> None:
    """Run one Pipecat session until idle, cancellation, or error.

    The orchestrator calls this once per wake-word detection. The
    pipeline starts the mic + speaker streams; OpenAI Realtime serves
    audio in/out + function calls; the runner blocks here until idle.
    """
    pipeline, task, _transport = build_pipeline()
    runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
    log.info("conversation_start", voice=settings.REALTIME_VOICE, tools=len(openai_tool_specs()))
    try:
        await runner.run(task)
    finally:
        log.info("conversation_end")
