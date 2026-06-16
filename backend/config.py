"""Backend configuration (Prompt 31). All from env; dev-safe defaults.

This is a SEPARATE project from the Emma daemon — its own deps (backend/
requirements.txt), never added to the daemon's pyproject. The OpenAI master key
lives here server-side and is NEVER sent to the browser (the WS proxy is the seam).
"""

from __future__ import annotations

import os

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

    # ---- misc ----
    DATABASE_URL = os.environ.get("DATABASE_URL", "backend_emma.db")
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
    DOWNLOAD_PKG_URL = os.environ.get("DOWNLOAD_PKG_URL", "https://github.com/garcia/emma/releases/latest")
    DOWNLOAD_MSI_URL = os.environ.get("DOWNLOAD_MSI_URL", "https://github.com/garcia/emma/releases/latest")

    @property
    def captcha_enabled(self) -> bool:
        return bool(self.CLOUDFLARE_TURNSTILE_SECRET)


settings = Settings()
