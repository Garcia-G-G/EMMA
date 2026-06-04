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

import asyncio
import contextlib
import json
import math
import time
from collections.abc import Awaitable, Callable
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
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
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
from core import audio_devices, capability_gaps, dictionary, events_bus, vocabulary
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

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._gate.set_bot_speaking(True)
            events_bus.publish("state", state="speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._gate.set_bot_speaking(False)
            events_bus.publish("state", state="listening")
        await self.push_frame(frame, direction)


class TranscriptCollector(FrameProcessor):
    """Captures user + assistant transcripts for memory reflection.

    Collects TranscriptionFrames (user speech, pushed upstream by the
    Realtime LLM) and LLMTextFrames (assistant text, pushed downstream).
    When the bot stops speaking, the collected turn is appended to
    short-term memory and reflection is scheduled in the background.
    """

    def __init__(self) -> None:
        super().__init__()
        self._user_text = ""
        self._assistant_text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            events_bus.publish("state", state="thinking")
        if isinstance(frame, TranscriptionFrame) and frame.text:
            cleaned = vocabulary.corrections(frame.text)
            if cleaned != frame.text:
                log.debug("transcript_corrected", before=frame.text, after=cleaned)
            self._user_text += cleaned + " "
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


class _TestTranscriptTap(FrameProcessor):
    """EMMA_TEST_MODE only (19.7-VAH3): surface STT + bot text as JSON logs.

    The production ``TranscriptCollector`` never actually receives these
    frames: user ``TranscriptionFrame``s are pushed UPSTREAM of the LLM
    (toward transport.input), and assistant ``LLMTextFrame``s are absorbed
    by ``LLMAssistantAggregator`` before the collector — the long-standing
    reflection gap (CLAUDE.md "not yet implemented"; root-caused during
    19.7, fix is its own prompt). The harness taps the stream at the two
    spots where the frames DO exist. Never constructed in production.

    role="user": sits between transport.input and the LLM; sees the
    upstream TranscriptionFrames → ``stt_user_test`` per utterance.
    role="bot": sits right after the LLM; accumulates downstream
    LLMTextFrames and flushes ``bot_text_test`` on BotStoppedSpeaking.
    """

    def __init__(self, role: str) -> None:
        super().__init__()
        self._role = role
        self._bot_text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._role == "user":
            if isinstance(frame, TranscriptionFrame) and frame.text:
                log.info("stt_user_test", text=frame.text)
        elif isinstance(frame, LLMTextFrame) and frame.text:
            self._bot_text += frame.text
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._bot_text.strip():
                log.info("bot_text_test", text=self._bot_text.strip())
            self._bot_text = ""
        await self.push_frame(frame, direction)


class _AudioLevelTap(FrameProcessor):
    """Publishes the RMS of outbound audio so the visualizer core pulses live.

    Computes a normalized 0..1 level from each ``OutputAudioRawFrame`` (the
    bot's voice) and publishes ``output_level`` at ~10 Hz (throttled). On the
    first audio frame of the session it also publishes a ``latency`` event
    (tap-construction → first audio ≈ model response time). Cheap: int16 RMS,
    no resampling. Placed just before ``transport.output()``.
    """

    _FULL_SCALE = 9000.0  # int16 RMS that maps to level 1.0 (speech is well below 32767)

    def __init__(self) -> None:
        super().__init__()
        self._t0 = time.monotonic()
        self._last_pub = 0.0
        self._first_audio_done = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame) and frame.audio:
            now = time.monotonic()
            if not self._first_audio_done:
                self._first_audio_done = True
                events_bus.publish("latency", wake_to_first_audio_ms=int((now - self._t0) * 1000))
            if now - self._last_pub >= 0.1:  # throttle to ~10 Hz
                self._last_pub = now
                level = self._rms(frame.audio) / self._FULL_SCALE
                events_bus.publish("output_level", level=max(0.0, min(1.0, level)))
        await self.push_frame(frame, direction)

    @staticmethod
    def _rms(audio: bytes) -> float:
        n = len(audio) // 2
        if n == 0:
            return 0.0
        total = 0
        # int16 little-endian; sample a stride for speed on big frames
        stride = max(1, n // 1024)
        count = 0
        for i in range(0, n * 2, 2 * stride):
            s = int.from_bytes(audio[i : i + 2], "little", signed=True)
            total += s * s
            count += 1
        return math.sqrt(total / count) if count else 0.0


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
    """System prompt for the Realtime session, with memory priming.

    The session-language pin (19.6-B20) is read FIRST from
    ``user_profile.preferred_lang`` (source of truth — never hard-code "es")
    and injected at the very top: the model must NOT pick the greeting
    language itself; mirroring starts on turn 2.
    """
    preferred_lang = dictionary.user_profile().get("preferred_lang", "es") or "es"
    lang_name = "English" if preferred_lang == "en" else "Spanish"
    base = (
        "# Session language\n"
        f"Your first sentence MUST be in {lang_name}. Do NOT decide the "
        "language from any future user turn until you have spoken the "
        "greeting. After the greeting, mirror Garcia's language per turn.\n\n"
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
        "# Confirmation flow (mandatory)\n"
        "- When a tool returns `requires_confirmation: true`, treat its `user_message` "
        "as a yes/no question. SPEAK the user_message verbatim, then STOP and WAIT for "
        "the user's reply.\n"
        "- If the user says yes (sí, claro, dale, ok, hazlo, confirm, do it, yes), "
        "re-call the SAME tool with the SAME arguments PLUS `confirmed: true`. "
        "Do NOT call a different tool first.\n"
        "- If the user says no (no, cancela, mejor no, déjalo, cancel), abandon the "
        "operation and say 'Listo, lo dejo' / 'Got it, leaving it' (rotate).\n"
        "- If the user says something else, treat it as 'no' for safety — say "
        "'No te entendí, cancelo por si acaso' / 'Didn't catch that, cancelling to "
        "be safe'.\n"
        "- EXCEPTION — pick by number: if the confirmation question enumerated "
        "several matches ('encontré 2: …, ¿a cuál te refieres? Dime el número') "
        "and the user answers with a number ('el 2', 'la segunda'), re-call the "
        "SAME tool with that 1-based `index` PLUS `confirmed: true`. This is a "
        "selection, not a yes/no — do not treat it as cancel.\n\n"
        "# Defaults & apps\n"
        "- You CAN set and change Garcia's preferred app per category "
        "(editor/ide, terminal, music, browser) and read it back. When he asks "
        "to set or change a default ('usa VS Code', 'hazme default Chrome', "
        "'prefiero Zed'), just do it with your tools — never refuse.\n"
        "- Only ever pick or open an app Garcia actually has installed. If you "
        "are not sure what he has, check with your tools FIRST. Never assume an "
        "app is present — e.g. don't open Firefox if he only has Chrome.\n"
        "- If he wants an app he doesn't have, say so and offer to install it; "
        "don't silently fail.\n\n"
        "# Forbidden\n"
        "- No filler: 'Great question!', 'Absolutely!', 'Of course!'\n"
        "- No closers: '¿Algo más?', 'Anything else?'\n"
        "- No self-description unless asked.\n"
        "- No languages other than Spanish and English.\n"
        "- No lists unless requested.\n"
        "- No repeating the same information.\n"
    )
    base = (
        f"{base}\n\n# Unprompted speech (mandatory)\n"
        "- If a turn's first message is wrapped in "
        "<UNPROMPTED_SPEECH>...</UNPROMPTED_SPEECH>, Garcia did NOT ask for it: "
        "it's a proactive line (a briefing, reminder, or alert). Speak the "
        "content in his preferred language (default Spanish), naturally, then "
        "STOP. Do not invite a follow-up or append questions unless the "
        "directive itself contains one.\n"
        "\n# Vague search guard (mandatory)\n"
        "- Before calling search_github or search_web with a user-supplied "
        "query: if the query has fewer than 2 distinct content words, no proper "
        "noun, or no clear intent, DO NOT search. Ask Garcia to specify in "
        "Spanish first.\n"
        "- Examples to ask: '¿de qué quieres el repo?', '¿de quién?', '¿qué "
        "lenguaje?'.\n"
        "- Once Garcia clarifies, proceed with the search.\n"
        "\n# Repo cloning flow (mandatory)\n"
        "- When Garcia asks to 'buscar un repo', call search_github. Read the "
        "top 1-3 matches by name + star count. If he names one (a number or "
        "owner), pick it; otherwise present the top match and ask '¿clono el "
        "de X?'.\n"
        "- When he says 'clónalo en mi IDE' or chains 'busca X y clónalo', call "
        "clone_and_open with the resolved repo (use get_repo_url first if you "
        "only have a name). It returns requires_confirmation the first time — "
        "speak the question, wait for sí/no, then re-call with confirmed: true.\n"
        "- Once the clone is spawned, do NOT narrate. Say one short line "
        "('listo, clonando X') and stop. The macOS notification + the IDE "
        "opening on completion are enough signal.\n"
        "- 'Mis repos' / 'mi github' / 'los repos que tengo' / 'el repo que hice "
        "de X' → call my_repos. NUNCA pongas el nombre de Garcia como query de "
        "búsqueda. Si menciona un tema, filtra los resultados tú.\n"
        "- 'El repo de <alguien-más>' que suena a handle (una palabra, sin "
        "espacios) → search_github con user:<handle>, no texto libre.\n"
        "- Si search_github no encuentra nada Y la query parece un handle, NO "
        "aceptes el resultado vacío: ofrece my_repos (si se refería a él mismo) o "
        "pídele confirmar el usuario. El usuario pudo haberse transcrito mal.\n"
        "\n# Knowledge dictionary (mandatory)\n"
        "- Before calling search_web or search_github, check if Garcia means "
        "one of his saved pages (open_my_page) or a glossary term he already "
        "taught you. If so, use the dictionary path — it's instant and grounded.\n"
        "- 'Mi <thing>' or 'mi <name>' usually means a dictionary page "
        "(open_my_page).\n"
        "- Short acronyms (MCP, OWASP, MVP) usually have a dictionary expansion. "
        "If found, use it in your reply without explaining unless Garcia asks.\n"
        "- If Garcia teaches you something ('recuerda que...'), use remember_page "
        "/ remember_contact / remember_term as appropriate.\n"
        "- Identidad (yo/mi/mío/mis): cuando Garcia se refiera a sí mismo, resuelve "
        "con su perfil de usuario ANTES de cualquier búsqueda externa. Si pide "
        "'mis repos' y aún no sabes su usuario de GitHub, pregúntale UNA vez "
        "('¿cuál es tu usuario de GitHub?') y llama remember_user_profile. No "
        "adivines su usuario a partir de su nombre.\n"
        "- Si transcribes un nombre y Garcia te corrige de inmediato ('no, es X'), "
        "llama remember_stt_correction(wrong=lo_que_oíste, right=lo_que_dijo). No "
        "pidas permiso — ya te lo dio al corregirte.\n"
        "\n# Smart note append (mandatory)\n"
        "- 'Agrega X a <título>' → llama append_to_note(title=<título>, text=X). "
        "NO desambigües tú antes; la herramienta devuelve requires_confirmation "
        "cuando necesita tu ayuda.\n"
        "- Si responde con '¿para cuándo?' (o te pide elegir un sufijo), repite la "
        "pregunta a Garcia tal cual. Cuando conteste ('miércoles'), re-llama con "
        "suffix=<respuesta> y confirmed=true.\n"
        "- Si responde 'no encontré… ¿la creo nueva?', transmítelo; con el sí de "
        "Garcia re-llama con create_if_missing=true y confirmed=true.\n"
        "- Si Garcia pide explícitamente 'crea una nueva <título>', llama "
        "append_to_note con el título completo, create_if_missing=true y "
        "confirmed=true desde la primera llamada (sin confirmación intermedia).\n"
        "- 'La última nota' / 'mi última nota' / 'the last note' / 'esa nota "
        "que acabo de crear' → pon recent=true en la siguiente herramienta de "
        "notas. NUNCA busques 'última' como título literal.\n"
        "- Si no estás seguro ('apunta esto en la nota de antes'), llama "
        "resolve_recent_note PRIMERO y confirma con Garcia ('¿la de "
        "\\'Pendientes para mañana\\'?') antes de modificar nada.\n"
        "\n# App control layering (mandatory)\n"
        "- For IDE actions prefer the specialized tools: open_in_ide, "
        "new_file_in_ide, search_in_ide. Don't hand-roll AppleScript when these "
        "exist.\n"
        "- To open URLs use open_url (Garcia's normal browser). Do NOT use "
        "browser_navigate — that's headless Playwright, a different flow.\n"
        "- For shell commands Garcia wants to watch, use run_in_terminal; for "
        "background work he won't watch, use run_shell_task.\n"
        "- For music use play_track / play_playlist / pause / resume — don't send "
        "keystrokes for play/pause.\n"
        "- Only reach for app_keystroke / app_menu_click / app_focus when no "
        "specialized action exists for what Garcia asked.\n"
        "\n# App URL schemes (mandatory)\n"
        "- When Garcia names an app + an action (Slack, Figma, Linear, Notion, "
        "Things, Obsidian, Discord, WhatsApp...), use open_in_app — it builds the "
        "app's deep-link URL from the capabilities registry. Don't fall back to "
        "app_keystroke unless the app has no URL scheme.\n"
        "- For chat apps (Slack/Discord/WhatsApp) prefer channel deep-linking via "
        "open_in_app over app_focus.\n"
        "- For plain 'abre <app>' with no further intent, just use "
        "open_application — don't reinvent it.\n"
        "- If Garcia teaches you a new app ('recuerda que X usa el esquema Y'), "
        "use remember_app.\n"
        "\n# In-app resources (mandatory)\n"
        "- 'Emma, abre la conexión X' (TablePlus, bases de datos) → "
        "open_in_app(target=X, kind='connection'). The dictionary resolves "
        "saved names to the right app + deep link.\n"
        "- 'Abre el canal Y en Slack' → open_in_app(target=Y, app='slack', "
        "kind='channel').\n"
        "- If the tool answers that it doesn't know the resource, offer to "
        "save it: ask for the exact name and call remember_connection(name, "
        "app, kind).\n"
        "\n# Browser tabs\n"
        "- '¿Cuántas pestañas tengo?' → list_browser_tabs.\n"
        "- 'Cierra las duplicadas' → close_duplicate_tabs (asks first; "
        "google.com is protected by default).\n"
        "- 'Cierra las de YouTube' → close_tabs_matching('youtube').\n"
        "- Only if Garcia EXPLICITLY says to include Google ('incluyendo "
        "Google'), pass protect_domains=[] on that single call — never make "
        "it the default.\n"
        "\n# Terminal in IDE\n"
        "- 'Emma, en la terminal de Cursor corre X' / 'escribe X en la "
        "terminal' → ide_terminal_send(text=X). It opens the terminal if "
        "needed, pastes, and presses Enter.\n"
        "- For interactive TUI prompts (Claude Code etc.) Enter-by-script "
        "does NOT submit; call with enter=false and tell Garcia to press "
        "Enter himself.\n"
        "\n# Editing files (mandatory)\n"
        "- 'Emma, en mi archivo X agrega Y al final' → edit_file_append.\n"
        "- 'Emma, agrega Y al inicio de X' → edit_file_prepend.\n"
        "- 'Emma, reemplaza A por B en X' → edit_file_search_replace (literal; "
        "'todas las ocurrencias' → count=-1).\n"
        "- 'Emma, sobrescribe X con esto: …' → edit_file_replace.\n"
        "- All four ask for confirmation: SPEAK the diff summary the tool returns "
        "('voy a agregar 3 líneas al final de utils.py — ¿confirmas?'), wait for "
        "sí, re-call with confirmed=true.\n"
        "- After editing, confirm briefly ('listo, lo cambié y lo abrí en "
        "Cursor'). Do NOT read the file contents back — the IDE already shows "
        "the change.\n"
    )
    pron = vocabulary.pronunciation_block("es")
    if pron:
        base = f"{base}\n\n{pron}"
    try:
        memory = await priming_block()
    except Exception as exc:
        log.warning("memory_priming_failed", error=str(exc))
        memory = ""
    if memory:
        base += f"\n# Memory\n{memory}\n"
    return base


def _session_seed_messages() -> list[Any]:
    """Synthetic context seed pinning the language bias (19.6-B20).

    For a Spanish profile, one developer message rides in the LLMContext so
    context replays (after history grows or a reconnect) keep the bias even
    when the greeting is long gone. English profiles need no seed — English
    is the model's natural default.
    """
    lang = dictionary.user_profile().get("preferred_lang", "es") or "es"
    if lang == "es":
        return [
            {
                "role": "system",
                "content": "Speak Spanish by default — see the Session language directive.",
            }
        ]
    return []


class SessionControl:
    """Lets a tool request the session close after Emma finishes speaking.

    Playback tools set ``ToolResult.ends_session=True``; the function-call
    handler flags it here, and :class:`EndSessionWatcher` cancels the pipeline
    task on the next ``BotStoppedSpeakingFrame`` (after the spoken confirmation),
    so the open mic stops fighting the music it just started. A fallback timer
    ends the session even if the model never speaks.
    """

    def __init__(self) -> None:
        self.task: PipelineTask | None = None
        self._end_requested = False
        self._fallback: asyncio.Task[None] | None = None

    def set_task(self, task: PipelineTask) -> None:
        self.task = task

    @property
    def end_requested(self) -> bool:
        return self._end_requested

    def request_end(self) -> None:
        if self._end_requested:
            return
        self._end_requested = True
        self._fallback = asyncio.create_task(self._fallback_end())

    async def _fallback_end(self) -> None:
        await asyncio.sleep(10.0)
        await self.end_now("fallback_timeout")

    async def end_now(self, reason: str) -> None:
        if not self._end_requested:
            return
        self._end_requested = False
        if self.task is not None:
            log.info("session_end_after_tool", reason=reason)
            await self.task.cancel()


class EndSessionWatcher(FrameProcessor):
    """Ends the session after the bot stops speaking, when a tool requested it."""

    def __init__(self, control: SessionControl) -> None:
        super().__init__()
        self._control = control

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if self._control.end_requested and isinstance(frame, BotStoppedSpeakingFrame):
            await self._control.end_now("after_speech")


def _make_function_handler(
    control: SessionControl,
) -> Callable[[FunctionCallParams], Awaitable[None]]:
    """Build the Pipecat function-call handler bound to a session's control.

    Pipecat's ``register_function`` contract: the handler receives a
    :class:`FunctionCallParams` and reports its result by awaiting
    ``params.result_callback(payload)``. The payload is serialized to JSON and
    sent back to the Realtime model as a ``function_call_output``. If the tool
    set ``ends_session``, we ask the session to close after Emma speaks.
    """

    async def _handler(params: FunctionCallParams) -> None:
        name = params.function_name
        args = dict(params.arguments or {})
        # Log only the argument NAMES, never values — secret args (passwords,
        # tokens) must not reach the logs even before the redaction processor.
        log.info("tool_started", name=name, args_keys=list(args.keys()))
        events_bus.publish("tool_started", name=name)
        if settings.EMMA_TEST_MODE:
            # Voice-harness hook (19.7-VAH3): the runner asserts on argument
            # VALUES, which production logs deliberately omit. Test-mode only;
            # the redaction processor still scrubs PII patterns from this line.
            with contextlib.suppress(Exception):  # never let the hook break a call
                log.info("tool_args_test", name=name, args=json.dumps(args, default=str)[:500])
        t_start = time.monotonic()
        try:
            result = await asyncio.wait_for(dispatch(name, args), timeout=20.0)
        except TimeoutError:
            elapsed = int((time.monotonic() - t_start) * 1000)
            log.error("tool_timed_out", name=name, elapsed_ms=elapsed)
            events_bus.publish("tool_timed_out", name=name, elapsed_ms=elapsed)
            capability_gaps.record(
                name=name,
                args_keys=list(args.keys()),
                success=False,
                user_message="Esa acción se quedó pegada (timeout 20s).",
                elapsed_ms=elapsed,
                timed_out=True,
            )
            await params.result_callback(
                {
                    "success": False,
                    "user_message": "Esa acción se quedó pegada. Intenta de nuevo.",
                    "data": None,
                    "requires_confirmation": False,
                }
            )
            return
        except Exception as exc:
            elapsed = int((time.monotonic() - t_start) * 1000)
            log.error("tool_failed", name=name, elapsed_ms=elapsed, error=str(exc))
            events_bus.publish("tool_failed", name=name, elapsed_ms=elapsed)
            capability_gaps.record(
                name=name,
                args_keys=list(args.keys()),
                success=False,
                user_message=str(exc),
                elapsed_ms=elapsed,
                errored=True,
            )
            await params.result_callback(
                {
                    "success": False,
                    "user_message": "Algo falló con esa acción.",
                    "data": None,
                    "requires_confirmation": False,
                }
            )
            return
        elapsed = int((time.monotonic() - t_start) * 1000)
        log.info("tool_completed", name=name, elapsed_ms=elapsed, success=result.success)
        events_bus.publish("tool_completed", name=name, elapsed_ms=elapsed, success=result.success)
        capability_gaps.record(
            name=name,
            args_keys=list(args.keys()),
            success=result.success,
            user_message=result.user_message,
            data=result.data,
            elapsed_ms=elapsed,
        )
        payload: dict[str, Any] = {
            "success": result.success,
            "user_message": result.user_message,
            "data": result.data,
            "requires_confirmation": result.requires_confirmation,
        }
        await params.result_callback(payload)
        if getattr(result, "ends_session", False):
            control.request_end()

    return _handler


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
                transcription=InputAudioTranscription(
                    model=settings.REALTIME_TRANSCRIPTION_MODEL,
                    # Hot-word bias toward every proper noun Emma knows (identity,
                    # contacts, glossary, apps, vocab — see vocabulary.bias_render),
                    # rendered for the active model's format and ≤500 chars.
                    prompt=vocabulary.bias_render(settings.REALTIME_BIAS_MODE),
                ),
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
    Pipeline,
    PipelineTask,
    LocalAudioTransport,
    LLMContext,
    AuthErrorWatcher,
    OpenAIRealtimeLLMService,
]:
    """Wire transport + Realtime LLM + tool handlers into a Pipecat pipeline.

    Returns (pipeline, task, transport, context, auth_watcher, llm) so the
    orchestrator can manage their lifecycle. The transport opens the
    mic + speaker streams when the pipeline starts and closes them on
    cancel / idle-timeout. The auth_watcher lets run_session detect a
    terminal auth error and exit non-zero instead of reconnecting. The llm is
    returned so run_session can explicitly close its WebSocket on exit (B1).
    """
    # barge_in_rms must sit ABOVE Emma's own speaker echo or she self-interrupts
    # after ~1 word. Measured echo on this MacBook ranged 4000-12600 RMS
    # (median ~5400); anything at/below that let the echo through to the Realtime
    # VAD. 18000 suppresses the echo while still letting a deliberate, close,
    # louder human voice barge in.
    echo_gate = EchoGateFilter(tail_ms=600, barge_in_rms=18000.0)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE_HZ,
            audio_out_sample_rate=SAMPLE_RATE_HZ,
            audio_in_filter=echo_gate,
            vad_analyzer=SileroVADAnalyzer(),
            # None in production (system default). The voice acceptance
            # harness points this at a virtual cable (19.7-VAH2).
            input_device_index=audio_devices.test_input_device_index(),
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

    session_control = SessionControl()
    function_handler = _make_function_handler(session_control)
    for spec in openai_tool_specs():
        fn = spec.get("function", spec)
        name = fn.get("name")
        if name:
            llm.register_function(name, function_handler)

    context = LLMContext(messages=_session_seed_messages())
    assistant_aggregator = LLMAssistantAggregator(context)
    transcript_collector = TranscriptCollector()
    auth_watcher = AuthErrorWatcher()
    end_watcher = EndSessionWatcher(session_control)
    audio_tap = _AudioLevelTap()  # publishes output RMS for the visualizer core pulse

    stages: list[FrameProcessor] = [transport.input()]
    if settings.EMMA_TEST_MODE:
        stages.append(_TestTranscriptTap("user"))  # upstream STT frames (19.7)
    stages.append(llm)
    if settings.EMMA_TEST_MODE:
        stages.append(_TestTranscriptTap("bot"))  # bot text before the aggregator eats it
    stages += [
        auth_watcher,  # right after the LLM so it sees its ErrorFrames first
        end_watcher,  # closes the session after playback tools finish speaking
        assistant_aggregator,
        transcript_collector,
        gate_proc,
        audio_tap,  # just before output: taps the bot's outgoing audio RMS
        transport.output(),
    ]
    pipeline = Pipeline(stages)
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
    session_control.set_task(task)
    return pipeline, task, transport, context, auth_watcher, llm


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

    _pipeline, task, _transport, context, auth_watcher, llm = await build_pipeline()
    runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
    log.info("conversation_start", voice=settings.REALTIME_VOICE, tools=len(openai_tool_specs()))
    try:
        await task.queue_frame(LLMContextFrame(context=context))
        await runner.run(task)
    finally:
        # Explicitly close the Realtime WebSocket. Pipecat's idle-cancel usually
        # closes it, but on the orchestrator's outer-timeout path the task is
        # abandoned mid-flight and the socket can linger half-open, blocking the
        # next session's mic. _disconnect() is idempotent (no-ops if already
        # closed). Best-effort: never let cleanup mask the real exit (B1).
        with contextlib.suppress(Exception):
            # _disconnect is Pipecat-internal (untyped) but the public surface
            # (reset_conversation) reconnects, which we don't want on teardown.
            await asyncio.wait_for(llm._disconnect(), timeout=3.0)  # type: ignore[no-untyped-call]
        log.info("conversation_end")

    if auth_watcher.terminal_error is not None:
        # A terminal auth/config error surfaced; do not let the orchestrator
        # loop reopen the session. Bubble out as a non-zero exit.
        raise SystemExit(2)
