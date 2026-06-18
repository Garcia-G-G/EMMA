"""Backend configuration (Prompt 31). All from env; dev-safe defaults.

This is a SEPARATE project from the Emma daemon — its own deps (backend/
requirements.txt), never added to the daemon's pyproject. The OpenAI master key
lives here server-side and is NEVER sent to the browser (the WS proxy is the seam).
"""

from __future__ import annotations

import os
from pathlib import Path

# Per-plan session caps. Free is the default; paid tiers lift the limits.
PLAN_CAPS: dict[str, dict[str, object]] = {
    "free": {"max_session_seconds": 120, "daily_sessions": 2, "unlimited": False},
    "pro": {"max_session_seconds": 600, "daily_sessions": 0, "unlimited": True},
    "team": {"max_session_seconds": 1800, "daily_sessions": 0, "unlimited": True},
}


class Settings:
    # ---- OpenAI (server-side only) ----
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

    # ---- session tokens / cookies ----
    JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
    SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
    SESSION_TOKEN_TTL_S = 300  # signed session_token validity (5 min) — A3
    DEMO_SESSION_SECONDS = int(os.environ.get("DEMO_SESSION_SECONDS", "120"))

    # ---- cost guard (A4) ----
    MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "50"))
    # gpt-realtime audio pricing (approx, USD per 1M tokens) for cost accounting.
    COST_PER_M_INPUT = float(os.environ.get("COST_PER_M_INPUT", "40"))
    COST_PER_M_OUTPUT = float(os.environ.get("COST_PER_M_OUTPUT", "80"))

    # ---- captcha ----
    CLOUDFLARE_TURNSTILE_SECRET = os.environ.get("CLOUDFLARE_TURNSTILE_SECRET", "")
    TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    # ---- OAuth ----
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    GITHUB_OAUTH_CLIENT_ID = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
    GITHUB_OAUTH_CLIENT_SECRET = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")

    # ---- Stripe ----
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")
    STRIPE_PRICE_TEAM = os.environ.get("STRIPE_PRICE_TEAM", "")

    # ---- Wake Word Studio (Prompt 16.3) ----
    # Auth-gate the Studio by default; flip to false only for local dev.
    WAKE_STUDIO_REQUIRE_AUTH = os.environ.get("WAKE_STUDIO_REQUIRE_AUTH", "true").lower() != "false"
    # ElevenLabs character price. Default $0.30 / 1000 chars (Starter plan ballpark);
    # the runner multiplies live spend by this to surface cost_so_far_usd.
    WAKE_COST_PER_1K_CHARS = float(os.environ.get("WAKE_COST_PER_1K_CHARS", "0.30"))
    # Hard sanity cap on a single job's estimated spend (raise via env for power users).
    WAKE_MAX_COST_USD = float(os.environ.get("WAKE_MAX_COST_USD", "20"))
    # Comma-separated ElevenLabs voice-ID pools the Studio draws from for diversity.
    # Default: the daemon's two configured voices (ES + EN) so this works out of the
    # box; add more library voice IDs here to widen the acoustic spread.
    WAKE_VOICE_POOL_ES = os.environ.get("WAKE_VOICE_POOL_ES", "")
    WAKE_VOICE_POOL_EN = os.environ.get("WAKE_VOICE_POOL_EN", "")
    # Where trained models + scratch data live (under the daemon's ~/.emma by default).
    WAKE_MODELS_DIR = os.environ.get("WAKE_MODELS_DIR", str(Path.home() / ".emma" / "wake_models"))
    WAKE_DATA_DIR = os.environ.get("WAKE_DATA_DIR", str(Path.home() / ".emma" / "wake_jobs"))
    # The daemon's .env that "install" rewrites (WAKE_WORD_* keys only). Overridable
    # so tests never touch the real one.
    WAKE_DAEMON_ENV_FILE = os.environ.get(
        "WAKE_DAEMON_ENV_FILE", str(Path(__file__).resolve().parent.parent / ".env"))

    # ---- misc ----
    DATABASE_URL = os.environ.get("DATABASE_URL", "backend_emma.db")
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
    DOWNLOAD_PKG_URL = os.environ.get("DOWNLOAD_PKG_URL", "https://github.com/garcia/emma/releases/latest")
    DOWNLOAD_MSI_URL = os.environ.get("DOWNLOAD_MSI_URL", "https://github.com/garcia/emma/releases/latest")

    @property
    def captcha_enabled(self) -> bool:
        return bool(self.CLOUDFLARE_TURNSTILE_SECRET)


settings = Settings()
