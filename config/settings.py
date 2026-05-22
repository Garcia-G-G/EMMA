"""Application settings loaded from environment variables.

Validates on import - importing this module without a complete `.env`
will raise a pydantic ValidationError.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ElevenLabs voice tuning. The defaults pick the modern flash model and
    # a moderate latency mode, which produces noticeably more natural
    # prosody than eleven_multilingual_v2 + LATENCY_MODE=3 at roughly the
    # same time-to-first-byte.
    #
    # ELEVENLABS_MODEL_ID: prefer flash for snappy assistant feel; switch
    # to "eleven_turbo_v2_5" for richer expression at a small latency cost.
    ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"
    # ELEVENLABS_LATENCY_MODE: 0..4. 0 = best prosody, 4 = lowest latency.
    # 1 trades ~80 ms TTFB for a clearly more natural cadence.
    ELEVENLABS_LATENCY_MODE: int = 1
    # ELEVENLABS_OUTPUT_FORMAT: must match core.audio.SAMPLE_RATE. Changing
    # this without also changing audio.py plays back at the wrong speed.
    ELEVENLABS_OUTPUT_FORMAT: str = "pcm_16000"
    # TTS_FIRST_CHUNK_MIN_CHARS: how much text the synth needs before it can
    # commit to opening intonation. Higher = more natural starts, slightly
    # later first audio byte.
    TTS_FIRST_CHUNK_MIN_CHARS: int = 120

    # Barge-in: speak over Emma to interrupt her. Off => strict half-duplex
    # (no mic during playback, no interrupts possible).
    BARGE_IN_ENABLED: bool = True
    # Ignore mic for the first N ms of TTS playback. Echo-suppression
    # heuristic: Emma's first words are loudest leaking into the mic.
    BARGE_IN_BLANKING_MS: int = 350
    # RMS energy a 32 ms int16 frame must exceed to count as "speech"
    # during playback. Higher than idle VAD because the room is louder
    # (Emma is talking). Raise on false triggers, lower on misses.
    BARGE_IN_RMS: float = 1200.0
    # Consecutive above-threshold frames required to commit to an interrupt.
    # 6 frames ~= 192 ms of sustained speech at 32 ms/frame.
    BARGE_IN_FRAMES: int = 6

    # Whisper bias prompt: comma-separated proper nouns / brands / slang
    # Garcia actually says. Improves STT accuracy for those terms. Leave
    # empty to disable - irrelevant prompts hurt accuracy. Truncated to
    # 800 chars (≈ Whisper's 224-token hard cap).
    WHISPER_PROMPT: str = ""

    # Language policy strictness. When True, Emma never code-switches
    # inside a response unless the user explicitly asked for translation
    # or cross-lingual phrasing. False reverts to the older permissive
    # "Respond in <lang>" guidance.
    STRICT_LANGUAGE_POLICY: bool = True

    @field_validator("WHISPER_PROMPT")
    @classmethod
    def _truncate_whisper_prompt(cls, v: str) -> str:
        if not v:
            return ""
        return v[:800]


settings = Settings()
