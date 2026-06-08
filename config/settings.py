"""Application settings loaded from environment variables.

Validates on import - importing this module without a complete `.env`
will raise a pydantic ValidationError.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Credentials that bootstrap_from_env migrates to Keychain. After migration
# the .env value is blanked, and Settings fills it back from Keychain.
_CREDENTIAL_FIELDS = (
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "YOUTUBE_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "POSTGRES_DSN",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "PICOVOICE_ACCESS_KEY",
    "GITHUB_TOKEN",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    OPENAI_API_KEY: str
    ELEVENLABS_API_KEY: str
    YOUTUBE_API_KEY: str | None = None
    SPOTIFY_CLIENT_ID: str | None = None
    SPOTIFY_CLIENT_SECRET: str | None = None
    POSTGRES_DSN: str | None = None
    # Optional. If set, GitHub search uses 5000/hr instead of 60/hr. Credential
    # (ends in _TOKEN) — migrated to Keychain by bootstrap_from_env.
    GITHUB_TOKEN: str = ""
    # Where Emma drops cloned repos by default (override in .env).
    CLONE_DIR: Path = Path.home() / "Documents" / "repos"
    ELEVENLABS_VOICE_ID_ES: str
    ELEVENLABS_VOICE_ID_EN: str
    # Path to the user-trained openWakeWord ONNX model (see README "Wake word").
    WAKE_WORD_PATH: str
    # Internal label the openWakeWord model emits in its prediction dict; must
    # match the keyword label used during training (lowercase, underscores).
    # The default `hey_emma` matches a model trained with the phrase
    # "hey emma" in the Colab notebook.
    WAKE_WORD_NAME: str = "hey_emma"
    # Detection threshold (0.0-1.0). Raise for fewer false positives (stricter),
    # lower for higher recall. 0.5 is balanced for personal use in a quiet room.
    # For pvporcupine this is passed as the per-keyword `sensitivity`.
    WAKE_WORD_THRESHOLD: float = 0.5
    # Wake-word engine: "openwakeword" (default, open-source, built-in fallback
    # models) or "pvporcupine" (Picovoice; needs a .ppn + PICOVOICE_ACCESS_KEY).
    WAKE_WORD_ENGINE: str = "openwakeword"
    # Picovoice AccessKey, only read when WAKE_WORD_ENGINE="pvporcupine". Treated
    # as a credential (ends in _KEY) — migrated to Keychain by bootstrap_from_env.
    PICOVOICE_ACCESS_KEY: str | None = None
    LOG_LEVEL: str = "INFO"
    EMMA_HOME: Path = Path.home() / ".emma"

    # ---- Barge-in tuning (22.1-B38). The single-frame SPIKE threshold sits
    # above Emma's own speaker echo (measured 4000-12600 RMS on this MacBook)
    # so a clap/shout cuts her instantly; the WINDOW pair catches Garcia's
    # NORMAL voice, which rarely spikes 18000 on one frame but sustains
    # >6000 over 250 ms. Both live during her body phase; the opener stays
    # protected regardless (22-B32).
    BARGE_IN_RMS_SPIKE: float = 18000.0
    # HOTFIX (echo-loop): 6000 sits INSIDE this Mac's measured echo band
    # (4000-12600 RMS) and triggered false barge-in during the opener — the
    # window mean of Emma's own speaker echo (~6000-7000) crossed it, so she
    # self-interrupted and re-looped ("hola soy Emma, hola soy Emma..."). 15000
    # leaves ~2400 RMS margin over the echo top. The proper fix is Layer C
    # (reference-based echo suppression); once that lands and echo no longer
    # reaches the RMS path, this can drop back toward 6000-8000.
    BARGE_IN_RMS_WINDOW: float = 15000.0
    BARGE_IN_WINDOW_MS: int = 250

    # ---- Echo-loop HOTFIX (Layer B): stop Emma's own opener from re-firing
    # the wake detector at a session boundary. wake-listen and the session are
    # sequential, so when the wake stream REOPENS after a session it can hear
    # Emma's residual "hola soy Emma" still decaying in the room. WAKE_WARMUP_MS
    # suppresses wake predictions for that long after the stream opens.
    # BOT_SPEECH_TAIL_MS keeps the (dormant, defense-in-depth) bot_speaking flag
    # set past BotStoppedSpeaking to cover speaker decay.
    WAKE_WARMUP_MS: int = 1200
    BOT_SPEECH_TAIL_MS: int = 800

    # ---- Echo-loop HOTFIX (Layer C): reference-based echo suppression. The
    # gate keeps a ring of recently-PLAYED output samples and cross-correlates
    # incoming mic audio against it while Emma speaks; high coherence => echo
    # => drop the frame before it reaches the RMS barge-in path. This is real
    # (if simple) AEC, no new dependency. ECHO_CORR_THRESHOLD is the knob:
    # raise it if Garcia's real voice ever gets eaten, lower it if echo leaks.
    # Tune from the per-decision `echo_suppressed` DEBUG logs. Once validated,
    # BARGE_IN_RMS_WINDOW (Layer A) can drop back toward 6000-8000 since echo
    # no longer reaches the RMS path.
    ECHO_CANCEL_ENABLED: bool = True
    ECHO_REF_BUFFER_MS: int = 250  # how much played audio to retain as reference
    ECHO_CORR_WINDOW_MS: int = 100  # mic window length compared each frame
    ECHO_CORR_THRESHOLD: float = 0.35  # |corr| at/above this == echo
    ECHO_CORR_MAX_LAG_MS: int = 150  # speaker->mic latency search range
    ECHO_CORR_LAG_STRIDE_MS: int = 10  # lag search resolution

    # ---- Voice acceptance harness gates (19.7-VAH2). ALL off in production —
    # the harness subprocess sets these via env. When EMMA_TEST_MODE is true
    # and EMMA_TEST_INPUT_DEVICE names a device (substring match, e.g.
    # "BlackHole"), the wake listener AND the Pipecat transport read from it
    # instead of the system default mic. EMMA_TEST_OUTPUT_DEVICE is only used
    # by the harness's own playback, never by Emma.
    EMMA_TEST_MODE: bool = False
    EMMA_TEST_INPUT_DEVICE: str = ""
    EMMA_TEST_OUTPUT_DEVICE: str = ""

    # Real-time dashboard / JARVIS visualizer HTTP port (WebSocket runs on +1).
    # The native visualizer window and the daemon's opt-in dashboard read this.
    DASHBOARD_PORT: int = 3200

    # macOS Keychain service holding Secret-tier values + migrated credentials.
    # Parameterizable for future multi-user; core/secrets.py reads the same
    # name from the EMMA_KEYCHAIN_SERVICE environment variable.
    EMMA_KEYCHAIN_SERVICE: str = "com.garcia.emma"

    # IANA tz name (e.g. "America/Mexico_City"). None -> system local.
    TIMEZONE: str | None = None

    # End-of-utterance VAD: required trailing silence and RMS threshold over
    # int16 frames. Defaults tuned for a quiet room and a USB mic; raise
    # VAD_ENERGY_THRESHOLD in noisy environments.
    VAD_SILENCE_MS: int = 500
    VAD_ENERGY_THRESHOLD: float = 500.0
    VAD_MAX_UTTERANCE_S: float = 10.0
    # If no speech is detected within VAD_SPEECH_START_S of opening the
    # mic, treat as "no utterance" and return empty. Prevents accidental
    # wake-word triggers from holding the mic open for the full 10 s.
    VAD_SPEECH_START_S: float = 3.0

    # Hard ceiling for any single external API call.
    API_TIMEOUT_S: float = 10.0

    # ---- Native coding sub-agent (Prompt 23). A Responses-API tool-calling
    # loop running INSIDE Emma — no external `codex` CLI, no Gatekeeper, no
    # npm. Reuses OPENAI_API_KEY. Model card:
    # https://developers.openai.com/api/docs/models/gpt-5.3-codex
    # Alternatives via .env: gpt-5-codex ($1.25/$10, cheaper), gpt-5.5
    # ($5/$30, more reasoning — the default once Codex variants deprecate).
    CODING_AGENT_MODEL: str = "gpt-5.3-codex"
    CODING_AGENT_MAX_ITERS: int = 30  # hard cap on tool-call loop iterations
    CODING_AGENT_MAX_COST_USD: float = 2.0  # soft cap (pre-flight confirm); hard kill at 2x
    CODING_AGENT_TIMEOUT_S: int = 1800  # 30-min wall clock
    CODING_AGENT_REASONING: str = "medium"  # low | medium | high | xhigh
    # Narrate sub-agent progress aloud every few tool calls (23.1-B43.4). Off by
    # default — a running commentary on a 30-step task is exhausting.
    CODING_AGENT_SPEAK_PROGRESS: bool = False

    # Web search: pick one. Brave is the default; Tavily works as a drop-in if set.
    BRAVE_API_KEY: str | None = None
    TAVILY_API_KEY: str | None = None

    # Spotify OAuth redirect (Spotify uses Auth Code + PKCE; the prompt's
    # "device flow" wording is loose - this is the actual flow spotipy drives).
    SPOTIFY_REDIRECT_URI: str = "http://127.0.0.1:8888/callback"

    # X / Twitter OAuth 2.0 PKCE (Prompt 26.1). CLIENT_ID is public per the PKCE
    # spec (public client) → plain .env, not Keychain. The access/refresh tokens
    # ARE secret → Keychain via core/secrets.py (X_ACCESS_TOKEN / X_REFRESH_TOKEN
    # / X_TOKEN_EXPIRES_AT). Run `python -m emma.x_setup` once to mint them.
    X_CLIENT_ID: str = ""
    X_REDIRECT_URI: str = "http://localhost:8723/callback"
    X_SCOPES: str = "tweet.read tweet.write users.read offline.access"
    # After 26.1 the API is the supported path; the unauthenticated web-intent
    # composer is OFF by default (only opened if Garcia explicitly re-enables it).
    X_USE_COMPOSER_FALLBACK: bool = False

    # Emma's browser is visible by default so the user can watch it work.
    BROWSER_HEADLESS: bool = False

    # Cap on sequential tool calls per turn to prevent runaway loops.
    MAX_TOOL_STEPS: int = 5

    # DEPRECATED — kept for env-file compat, no longer read by any
    # live module after the Prompt-13 Pipecat migration. The Realtime
    # API selects voice via `REALTIME_VOICE`; sentence-by-sentence
    # ElevenLabs TTS is gone.
    ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"
    ELEVENLABS_LATENCY_MODE: int = 1
    ELEVENLABS_OUTPUT_FORMAT: str = "pcm_16000"
    TTS_FIRST_CHUNK_MIN_CHARS: int = 120

    # DEPRECATED — kept for env-file compat, no longer read. Barge-in
    # is now native to OpenAI Realtime (server-side VAD +
    # `input_audio_buffer.speech_started` events). The custom
    # heuristic-based listener was deleted in the migration.
    BARGE_IN_ENABLED: bool = True
    BARGE_IN_BLANKING_MS: int = 350
    BARGE_IN_RMS: float = 1200.0
    BARGE_IN_FRAMES: int = 6

    # DEPRECATED — kept for env-file compat, no longer read. The
    # Realtime model hears audio directly so we no longer need a
    # separate Whisper bias prompt nor an "always stay in language X"
    # policy.
    WHISPER_PROMPT: str = ""
    STRICT_LANGUAGE_POLICY: bool = True

    @field_validator("WHISPER_PROMPT")
    @classmethod
    def _truncate_whisper_prompt(cls, v: str) -> str:
        if not v:
            return ""
        return v[:800]

    @model_validator(mode="after")
    def _fill_credentials_from_keychain(self) -> Settings:
        """Fill blank credentials from Keychain (post .env migration).

        Dormant by default: if OPENAI_API_KEY is present (the normal,
        un-migrated state) this returns immediately and never shells out to
        the `security` CLI. Only once .env has been migrated (canonical
        credential blank) do we read the Secret tier from Keychain.
        """
        if self.OPENAI_API_KEY:
            return self
        from core.secrets import retrieve_sync

        for name in _CREDENTIAL_FIELDS:
            if not getattr(self, name, None):
                val = retrieve_sync(name)
                if val:
                    object.__setattr__(self, name, val)
        return self

    # Long-term memory store. SQLite by default; the EMMA_HOME dir is
    # created on first write. POSTGRES_DSN above remains the optional
    # network backend; if it's set the long-term store will use it
    # instead of SQLite (handled in memory/long_term.py).
    MEMORY_DB_PATH: Path = Path.home() / ".emma" / "memory.db"

    # Reflection: how many facts to extract per turn (cap, model may
    # return fewer). The reflection step runs gpt-4o-mini on the last
    # user+assistant pair plus a small window of history.
    MEMORY_REFLECTION_MODEL: str = "gpt-4o-mini"
    MEMORY_REFLECTION_MAX_FACTS_PER_TURN: int = 5

    # Memory priming: how many of the highest-confidence / most-recent
    # facts to inject into the system prompt each turn. Keep small so
    # the prompt stays focused.
    MEMORY_PRIMING_TOP_N: int = 15

    # DEPRECATED — kept for env-file compat, no longer read. The
    # Realtime API transcribes server-side; out-of-band STT is gone.
    STT_MODEL: str = "gpt-4o-mini-transcribe"

    # ---- Realtime API (Prompt 13) -----------------------------------
    # Audio-to-audio session model and voice. `coral` is a warm female
    # voice with natural prosody in both Spanish and English.
    # Alternatives: `shimmer`, `sage`, `alloy` (female), `ash`, `cedar` (male).
    REALTIME_MODEL: str = "gpt-realtime-2"
    REALTIME_VOICE: str = "coral"
    # Server-side transcription model for the input audio + how we render the
    # hot-word bias (core.vocabulary.bias_render). `whisper-1` retires ~June 2026;
    # its streaming successor `gpt-realtime-whisper` ignores the free-text
    # `prompt` and wants a short "Keywords: a, b, c" list instead. Both are
    # config so the migration is a one-line flip once Garcia A/B-tests his accent.
    # Docs: https://developers.openai.com/api/docs/guides/realtime-transcription
    REALTIME_TRANSCRIPTION_MODEL: str = "whisper-1"
    REALTIME_BIAS_MODE: str = "prompt"  # "prompt" (whisper-1) | "keywords" (gpt-realtime-whisper)
    # Idle close: shut the Realtime WebSocket when no user/assistant
    # activity has been observed for this many seconds. Wake word
    # re-arms after each idle close.
    IDLE_TIMEOUT_S: float = 30.0

    # Pipecat-pipeline session ceiling (Prompt 13 / Path A). Used as
    # PipelineTask.idle_timeout_secs. Pipecat will cancel the pipeline
    # if no BotSpeakingFrame / UserSpeakingFrame has fired for this
    # long; the orchestrator then loops back to wake-word listening.
    # 900s (15 min, 22-B31): Realtime bills active audio minutes, not idle
    # WebSocket time — idle is cheap, and dying mid-thought isn't. The
    # 19.2-B1 loopback watchdog is unaffected (it measures the gap AFTER a
    # session ends, not session length). Explicit closes still work via the
    # playback tools' ends_session flag / "ya, déjalo".
    SESSION_MAX_S: int = 900

    # ---- Proactive engine (Prompt 17) -------------------------------
    # Master switch + global behavior. Conservative defaults: only the
    # smallest set of proactivities is ON; everything else is opt-in.
    PROACTIVE_ENABLED: bool = True
    PROACTIVE_QUIET_HOURS: str = "22:30-07:30"  # comma-separated H:M-H:M windows
    PROACTIVE_RESPECT_MEETINGS: bool = True  # demote during calendar events
    PROACTIVE_VIP_SENDERS: str = ""  # comma-separated VIP email addresses
    PROACTIVE_VOICE_TIMEOUT_S: float = 30.0  # hard cap on an unprompted-speech session

    # Per-proactivity flags (True = active). Default ON: morning_briefing,
    # meeting_prep, calendar_conflict, memory_followup, overdue_reminders,
    # background_task_done. The rest are opt-in.
    PROACTIVE_MORNING_BRIEFING: bool = True
    PROACTIVE_MEETING_PREP: bool = True
    PROACTIVE_EOD_REFLECTION: bool = False
    PROACTIVE_CALENDAR_CONFLICT: bool = True
    PROACTIVE_URGENT_EMAIL: bool = False
    PROACTIVE_FRIDAY_RECAP: bool = False
    PROACTIVE_HABIT_TRACKER: bool = False
    PROACTIVE_MEMORY_FOLLOWUP: bool = True
    PROACTIVE_INTENTION_SETTING: bool = False
    PROACTIVE_BIRTHDAY_ALERTS: bool = False
    PROACTIVE_OVERDUE_REMINDERS: bool = True
    PROACTIVE_FOCUS_NUDGE: bool = False
    PROACTIVE_BACKGROUND_TASK_DONE: bool = True  # 15.12 integration

    # Timing knobs (cron). croniter 5-field "m h dom mon dow".
    PROACTIVE_MORNING_BRIEFING_CRON: str = "0 8 * * 1-5"  # 8am Mon-Fri
    PROACTIVE_EOD_REFLECTION_CRON: str = "0 22 * * *"  # 10pm daily
    PROACTIVE_FRIDAY_RECAP_CRON: str = "0 17 * * 5"  # 5pm Fri
    PROACTIVE_HABIT_TRACKER_CRON: str = "0 11,15 * * *"  # 11am & 3pm


settings = Settings()
