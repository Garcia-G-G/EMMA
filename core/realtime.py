"""OpenAI Realtime API client — Emma's audio-to-audio conversational core.

Replaces the old ``stt → llm → tts`` pipeline with a single persistent
WebSocket session to ``gpt-realtime``. The model hears the user's audio
directly, dispatches function calls to :mod:`tools.registry`, and emits
PCM audio back as a stream of ``response.audio.delta`` events.

Session lifecycle is owned by :mod:`core.orchestrator`:

1. Wake word fires → orchestrator calls :func:`connect`.
2. Orchestrator pumps mic audio into the session via :func:`send_audio`
   and runs :func:`run_event_loop` until idle.
3. Orchestrator closes the session and re-arms wake-word listening.

Why a separate 24 kHz audio path: the Realtime model is native
24 kHz mono int16. openWakeWord requires 16 kHz. They coexist by
running on independent ``sounddevice`` streams that we open and close
explicitly between phases.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sounddevice as sd
import structlog
import websockets

from config.settings import settings
from tools.registry import dispatch, openai_tool_specs

log = structlog.get_logger("emma.realtime")

REALTIME_URL_TEMPLATE = "wss://api.openai.com/v1/realtime?model={model}"

# Native Realtime audio format. Do not change without also changing the
# mic + output stream rates below.
SAMPLE_RATE_HZ = 24000
MIC_BLOCK_MS = 100
MIC_BLOCK_SAMPLES = SAMPLE_RATE_HZ * MIC_BLOCK_MS // 1000  # 2400

UserTranscriptHandler = Callable[[str], Awaitable[None]]
AssistantTranscriptHandler = Callable[[str], Awaitable[None]]


@dataclass
class RealtimeSession:
    ws: Any
    audio_out_queue: asyncio.Queue[bytes]
    last_activity: float = field(default_factory=time.monotonic)
    closed: bool = False

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def send(self, payload: dict[str, Any]) -> None:
        if self.closed:
            return
        await self.ws.send(json.dumps(payload))

    async def send_audio(self, pcm_bytes: bytes) -> None:
        if self.closed or not pcm_bytes:
            return
        await self.ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm_bytes).decode("ascii"),
                }
            )
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close()
        except Exception:
            pass


# ---------- session configuration ----------------------------------------

def _adapt_tool_specs(chat_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions tools are nested; Realtime expects them flat."""
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


def _build_instructions(memory_priming: str = "") -> str:
    """System prompt for the Realtime session.

    Much simpler than the old pipeline's system prompt: the model hears
    the user's voice directly, so the elaborate `_language_block` with
    its four exceptions is no longer needed. Memory priming (Phase 03)
    still folds in as a prefix when present.
    """
    base = (
        "You are Emma, a warm and concise bilingual voice assistant for Garcia. "
        "You hear his voice directly - match the language he speaks naturally. "
        "Keep replies short and conversational; this is a spoken dialogue, not a "
        "written one. Prefer tools when a tool fits. After a tool runs, briefly "
        "confirm what happened."
    )
    if memory_priming.strip():
        return f"{memory_priming.strip()}\n\n{base}"
    return base


def _build_session_config(memory_priming: str = "") -> dict[str, Any]:
    """Build the ``session.update`` payload for the GA Realtime API.

    GA-shape differences vs the pre-GA spec in Prompt 13:
    - ``session.type`` ("realtime") is now mandatory.
    - ``modalities`` -> ``output_modalities`` (audio-only by default).
    - ``voice`` / ``input_audio_format`` / ``output_audio_format`` /
      ``input_audio_transcription`` / ``turn_detection`` collapsed into
      a nested ``audio.{input, output}`` block.
    """
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.REALTIME_MODEL,
            "output_modalities": ["audio"],
            "instructions": _build_instructions(memory_priming),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE_HZ},
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": settings.VAD_SILENCE_MS,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE_HZ},
                    "voice": settings.REALTIME_VOICE,
                },
            },
            "tools": _adapt_tool_specs(openai_tool_specs()),
            "tool_choice": "auto",
        },
    }


# ---------- connection ---------------------------------------------------

async def connect(memory_priming: str = "") -> RealtimeSession:
    """Open a Realtime WebSocket session. Caller owns the lifecycle.

    Note on headers: the ``OpenAI-Beta: realtime=v1`` header was needed
    during the 2024 beta. ``gpt-realtime`` went GA in 2025 and the
    server now rejects that header with ``beta_api_shape_disabled``,
    so we send only ``Authorization``.
    """
    url = REALTIME_URL_TEMPLATE.format(model=settings.REALTIME_MODEL)
    headers = [("Authorization", f"Bearer {settings.OPENAI_API_KEY}")]
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers, max_size=2**24),
            timeout=settings.API_TIMEOUT_S,
        )
    except Exception as exc:
        log.error("realtime_connect_failed", error=str(exc))
        raise

    session = RealtimeSession(ws=ws, audio_out_queue=asyncio.Queue())
    await session.send(_build_session_config(memory_priming))
    log.info(
        "realtime_session_open",
        model=settings.REALTIME_MODEL,
        voice=settings.REALTIME_VOICE,
        tools=len(openai_tool_specs()),
    )
    return session


# ---------- event loop ---------------------------------------------------

async def _dispatch_function_call(
    session: RealtimeSession, call_id: str, name: str, args_json: str
) -> None:
    """Run the tool and send the result back to the Realtime session."""
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        args = {}
    log.info("fn_call", name=name, args=args)
    result = await dispatch(name, args)
    payload: dict[str, Any] = {
        "success": result.success,
        "user_message": result.user_message,
        "data": result.data if _json_safe(result.data) else str(result.data),
        "requires_confirmation": result.requires_confirmation,
    }
    await session.send(
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(payload, ensure_ascii=False, default=str),
            },
        }
    )
    # Tell the model it can continue speaking now that the tool returned.
    await session.send({"type": "response.create"})


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


async def run_event_loop(
    session: RealtimeSession,
    *,
    on_user_transcript: UserTranscriptHandler,
    on_assistant_transcript: AssistantTranscriptHandler,
) -> None:
    """Pump server events until the WebSocket closes or session.expired fires.

    Callbacks are awaited inline so the orchestrator can route transcripts
    into Phase 03 memory + short-term logs.
    """
    try:
        async for raw in session.ws:
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("realtime_invalid_json", raw=str(raw)[:120])
                continue

            et = evt.get("type", "")

            if et == "response.audio.delta":
                pcm = base64.b64decode(evt.get("delta", ""))
                if pcm:
                    await session.audio_out_queue.put(pcm)
                    session.touch()

            elif et == "response.audio.done":
                # Sentinel: lets the playback task know the utterance ended.
                await session.audio_out_queue.put(b"")

            elif et == "input_audio_buffer.speech_started":
                log.info("barge_in_detected")
                # Native barge-in: drain pending playback. The model also
                # cancels its in-flight response server-side automatically.
                while not session.audio_out_queue.empty():
                    try:
                        session.audio_out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                session.touch()

            elif et == "input_audio_buffer.speech_stopped":
                session.touch()

            elif et == "conversation.item.input_audio_transcription.completed":
                transcript = (evt.get("transcript") or "").strip()
                if transcript:
                    log.info("user_transcript", text=transcript)
                    try:
                        await on_user_transcript(transcript)
                    except Exception as exc:
                        log.warning("user_transcript_handler_failed", error=str(exc))
                session.touch()

            elif et == "response.audio_transcript.done":
                transcript = (evt.get("transcript") or "").strip()
                if transcript:
                    log.info("assistant_transcript", text=transcript)
                    try:
                        await on_assistant_transcript(transcript)
                    except Exception as exc:
                        log.warning("assistant_transcript_handler_failed", error=str(exc))
                session.touch()

            elif et == "response.function_call_arguments.done":
                call_id = evt.get("call_id", "")
                name = evt.get("name", "")
                args_json = evt.get("arguments", "{}")
                if call_id and name:
                    await _dispatch_function_call(session, call_id, name, args_json)
                session.touch()

            elif et == "error":
                err = evt.get("error", {})
                log.error("realtime_error", code=err.get("code"), message=err.get("message"))

            elif et == "session.expired":
                log.warning("realtime_session_expired")
                break

            elif et == "session.created":
                log.info("realtime_session_created", session_id=evt.get("session", {}).get("id"))

            # other events (session.updated, rate_limits.updated, etc.) are
            # intentionally ignored.
    except websockets.exceptions.ConnectionClosedOK:
        log.info("realtime_ws_closed_ok")
    except websockets.exceptions.ConnectionClosedError as exc:
        log.warning("realtime_ws_closed_error", code=exc.code, reason=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("realtime_event_loop_failed", error=str(exc))
    finally:
        await session.close()


# ---------- audio I/O (24 kHz, Realtime-native) --------------------------

async def mic_to_session(session: RealtimeSession) -> None:
    """Open a 24 kHz mono int16 RawInputStream and forward chunks to the WS.

    Runs until cancelled or the session closes.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("realtime_mic_status", status=str(status))
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))  # type: ignore[arg-type]

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE_HZ,
        channels=1,
        dtype="int16",
        blocksize=MIC_BLOCK_SAMPLES,
        callback=_cb,
    )
    stream.start()
    try:
        while not session.closed:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await session.send_audio(chunk)
    except asyncio.CancelledError:
        raise
    finally:
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


async def play_session_audio(session: RealtimeSession) -> None:
    """Drain ``audio_out_queue`` to a 24 kHz mono output stream.

    The empty-bytes sentinel marks utterance boundaries; we don't use it
    for anything beyond that today.
    """
    stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE_HZ,
        channels=1,
        dtype="int16",
        blocksize=0,
    )
    stream.start()
    try:
        while not session.closed:
            try:
                chunk = await asyncio.wait_for(session.audio_out_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                continue
            await asyncio.to_thread(stream.write, chunk)
    except asyncio.CancelledError:
        # Drop pending audio on cancel (barge-in cleanup is the caller's job).
        raise
    finally:
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


# ---------- utility: language heuristic for runtime.spoken_lang ---------

# Cheap es/en classifier - the Realtime API doesn't expose Whisper's
# `language` field, so tools that need a bilingual hint (preferences,
# dev-mode banners) read this via core.runtime.set_spoken_lang.
_ES_TOKENS = {
    "el", "la", "los", "las", "un", "una", "y", "que", "de", "qué", "cómo", "dónde",
    "cuándo", "porque", "para", "por", "es", "son", "está", "están", "soy", "estoy",
    "muy", "más", "ya", "no", "sí", "tú", "yo", "me", "te", "se", "le", "lo", "esto",
    "eso", "aquí", "allá", "ahora", "ándale", "ahorita", "órale", "qué", "hola",
    "gracias", "favor", "bueno", "amigo", "amiga", "español",
}
_EN_TOKENS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "i", "you",
    "he", "she", "it", "we", "they", "me", "my", "your", "this", "that", "what",
    "when", "where", "how", "why", "please", "hello", "thanks", "thank", "yeah",
    "yes", "no", "now", "english",
}


def detect_es_en(text: str) -> str:
    """Return 'es' or 'en' based on token overlap; defaults to 'es'."""
    if not text:
        return "es"
    tokens = [w.strip(".,?!¿¡;:").lower() for w in text.split()]
    es = sum(1 for t in tokens if t in _ES_TOKENS)
    en = sum(1 for t in tokens if t in _EN_TOKENS)
    if en > es:
        return "en"
    return "es"
