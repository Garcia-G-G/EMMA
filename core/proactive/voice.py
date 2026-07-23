"""Open a synthetic Pipecat session so Emma can speak unprompted in her real
voice (not the macOS ``say`` fallback).

The session reuses ``core.conversation.build_pipeline`` and seeds the LLM context
with a single user turn wrapped in ``<UNPROMPTED_SPEECH>...</UNPROMPTED_SPEECH>``.
The system prompt (see ``_build_instructions``) tells Emma to voice that line
verbatim and stop. The session is capped at ``PROACTIVE_VOICE_TIMEOUT_S`` and
then torn down.

KNOWN LIMITATION (needs live verification): this opens its own
``LocalAudioTransport`` (mic + speaker). If the orchestrator's wake-word loop is
holding the mic, the two can contend. Delivery always sends the NOTIFY-level
notification *before* calling this, so if the voice session fails to open, the user
still got the alert — the spoken layer degrades gracefully to silent-but-notified.
A future change should coordinate mic ownership with the orchestrator.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import structlog

from config.settings import settings

log = structlog.get_logger("emma.proactive.voice")


async def speak_unprompted(text: str) -> None:
    """Emma speaks ``text`` aloud via a one-shot Realtime session.

    Never raises — any failure is logged and swallowed (the caller already
    delivered a notification). The session is hard-capped by
    ``PROACTIVE_VOICE_TIMEOUT_S`` so a hung session can't wedge the engine.
    """
    text = (text or "").strip()
    if not text:
        return
    if not settings.openai_api_key():
        log.warning("proactive_voice_skipped", reason="no_openai_key")
        return

    # Lazy import: the proactive subsystem must not fail-load just because the
    # Pipecat/Realtime stack isn't importable at module-load time.
    from core import conversation

    task = None
    transport = None
    runner = None
    llm = None
    try:
        from pipecat.frames.frames import LLMContextFrame
        from pipecat.pipeline.runner import PipelineRunner

        _pipeline, task, transport, context, _auth, llm = await conversation.build_pipeline()
        context.messages.append(
            {"role": "user", "content": f"<UNPROMPTED_SPEECH>{text}</UNPROMPTED_SPEECH>"}
        )
        runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
        log.info("proactive_voice_start", chars=len(text))
        await task.queue_frame(LLMContextFrame(context=context))
        await asyncio.wait_for(runner.run(task), timeout=float(settings.PROACTIVE_VOICE_TIMEOUT_S))
    except TimeoutError:
        log.info("proactive_voice_timeout_closed", chars=len(text))
    except Exception as exc:
        log.error("proactive_voice_failed", error=str(exc))
    finally:
        if task is not None:
            with suppress(Exception):
                await task.cancel()
        if transport is not None:
            with suppress(Exception):
                await transport.cleanup()  # type: ignore[no-untyped-call]
        if llm is not None:
            with suppress(Exception):
                await llm._disconnect()  # type: ignore[no-untyped-call]  # close WebSocket (B1)
