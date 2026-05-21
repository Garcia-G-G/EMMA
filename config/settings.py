"""Application settings loaded from environment variables.

Validates on import - importing this module without a complete `.env`
will raise a pydantic ValidationError.
"""
from __future__ import annotations

from pathlib import Path

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
    PICOVOICE_ACCESS_KEY: str
    YOUTUBE_API_KEY: str | None = None
    SPOTIFY_CLIENT_ID: str | None = None
    SPOTIFY_CLIENT_SECRET: str | None = None
    POSTGRES_DSN: str | None = None
    ELEVENLABS_VOICE_ID_ES: str
    ELEVENLABS_VOICE_ID_EN: str
    WAKE_WORD_PATH: str
    LOG_LEVEL: str = "INFO"
    EMMA_HOME: Path = Path.home() / ".emma"

    # IANA tz name (e.g. "America/Mexico_City"). None -> system local.
    TIMEZONE: str | None = None

    # End-of-utterance VAD: required trailing silence and RMS threshold over
    # int16 frames. Defaults tuned for a quiet room and a USB mic; raise
    # VAD_ENERGY_THRESHOLD in noisy environments.
    VAD_SILENCE_MS: int = 700
    VAD_ENERGY_THRESHOLD: float = 500.0
    VAD_MAX_UTTERANCE_S: float = 12.0

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


settings = Settings()
