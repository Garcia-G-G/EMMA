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
    WAKE_WORD_THRESHOLD: float = 0.5
    LOG_LEVEL: str = "INFO"
    EMMA_HOME: Path = Path.home() / ".emma"

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

    # Web search: pick one. Brave is the default; Tavily works as a drop-in if set.
    BRAVE_API_KEY: str | None = None
    TAVILY_API_KEY: str | None = None

    # Spotify OAuth redirect (Spotify uses Auth Code + PKCE; the prompt's
    # "device flow" wording is loose - this is the actual flow spotipy drives).
    SPOTIFY_REDIRECT_URI: str = "http://127.0.0.1:8888/callback"

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
    # Idle close: shut the Realtime WebSocket when no user/assistant
    # activity has been observed for this many seconds. Wake word
    # re-arms after each idle close.
    IDLE_TIMEOUT_S: float = 30.0

    # Pipecat-pipeline session ceiling (Prompt 13 / Path A). Used as
    # PipelineTask.idle_timeout_secs. Pipecat will cancel the pipeline
    # if no BotSpeakingFrame / UserSpeakingFrame has fired for this
    # long; the orchestrator then loops back to wake-word listening.
    # 300s (5 min) keeps Emma in the conversation through natural pauses
    # instead of dropping out after 2 minutes mid-task.
    SESSION_MAX_S: int = 300


settings = Settings()
