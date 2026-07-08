"""Backend configuration (Prompt 31). All from env; dev-safe defaults.

This is a SEPARATE project from the Emma daemon — its own deps (backend/
requirements.txt), never added to the daemon's pyproject. The OpenAI master key
lives here server-side and is NEVER sent to the browser (the WS proxy is the seam).
"""

from __future__ import annotations

import os
from pathlib import Path

# Per-plan caps (LANDING-27). One source of truth the demo bridge reads to size a
# session. ``session_seconds`` = per-session length; ``daily_seconds`` = user/day
# ceiling (0 = none beyond monthly); ``monthly_seconds`` = user/month; ``cost_cap_cents``
# = hard $/session ceiling. Free is the anonymous 60s/IP discovery path.
#
# CLIENT-INSTALL-PIPELINE Phase 1 added two managed-daemon keys (the web demo ignores
# them): ``daemon_session_max_seconds`` = per managed voice session ceiling (much
# longer than the demo's ``session_seconds``); ``overage_per_min_usd`` = Stripe overage
# rate (Phase 5). The managed MONTHLY allowance reuses ``monthly_seconds``.
# ⚠️ PRICING: free.monthly_seconds is 0 → free gets NO managed daemon minutes (demo
# only). Bump it if you want free trial minutes on the daemon. pro=3600s (60min) /
# power=12000s (200min) match /api/plans; the CLIENT-INSTALL prompt's 200/1000 figures
# are a separate pricing decision — change here + /api/plans together if adopting them.
# ABUSE-PROTECTION-2 added `concurrent_sessions` (Capa 2) and `reconnect_min_seconds`
# (Capa 3, sliding-window floor between reconnects). free's 30s is deliberately
# aggressive — free gets no managed minutes anyway (monthly_seconds=0).
PLAN_CAPS: dict[str, dict[str, object]] = {
    "free":  {"session_seconds": 60,   "daily_seconds": 60,    "monthly_seconds": 0,
              "cost_cap_cents": 40,   "daemon_session_max_seconds": 900,  "overage_per_min_usd": 0.30,
              "concurrent_sessions": 1, "reconnect_min_seconds": 30},
    "pro":   {"session_seconds": 300,  "daily_seconds": 600,   "monthly_seconds": 3600,
              "cost_cap_cents": 200,  "daemon_session_max_seconds": 1800, "overage_per_min_usd": 0.20,
              "concurrent_sessions": 1, "reconnect_min_seconds": 5},
    "power": {"session_seconds": 900,  "daily_seconds": 1800,  "monthly_seconds": 12000,
              "cost_cap_cents": 1000, "daemon_session_max_seconds": 3600, "overage_per_min_usd": 0.15,
              "concurrent_sessions": 2, "reconnect_min_seconds": 2},
}
# Back-compat: the old "team" tier maps to "power".
PLAN_CAPS["team"] = PLAN_CAPS["power"]


def plan_caps(plan: str | None) -> dict[str, object]:
    return PLAN_CAPS.get(plan or "free", PLAN_CAPS["free"])


class Settings:
    # ---- OpenAI (server-side only) ----
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    # 25.0.3: GA model name (beta "gpt-realtime-2" is gone). session.created echoes
    # "gpt-realtime". Override via env if OpenAI ships a dated GA snapshot.
    OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
    OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
    # Dump every OpenAI Realtime event type to the log — on while iterating on GA shape.
    DEMO_DEBUG_REALTIME = os.environ.get("DEMO_DEBUG_REALTIME", "").lower() in ("1", "true", "yes")

    # ---- session tokens / cookies ----
    JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
    SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
    ADMIN_EMAILS = os.environ.get("ADMIN_EMAILS", "")  # comma-separated operator emails
    SESSION_TOKEN_TTL_S = 300  # signed session_token validity (5 min) — A3
    DEMO_SESSION_SECONDS = int(os.environ.get("DEMO_SESSION_SECONDS", "120"))

    # ---- live talk-to-Emma demo (LANDING-25.0) ----
    DEMO_REALTIME_VOICE = os.environ.get("DEMO_REALTIME_VOICE", "coral")  # match the daemon
    DEMO_TALK_SECONDS = int(os.environ.get("DEMO_TALK_SECONDS", "60"))    # hard session length
    DEMO_WARNING_SECONDS = int(os.environ.get("DEMO_WARNING_SECONDS", "45"))  # nudge-to-wrap mark
    DEMO_COST_CAP_CENTS = int(os.environ.get("DEMO_COST_CAP_CENTS", "40"))  # hard $/session ceiling
    DEMO_MAX_FRAME_BYTES = int(os.environ.get("DEMO_MAX_FRAME_BYTES", "256000"))  # anti memory-bomb (256KB)
    DEMO_MAX_SESSION_BYTES = int(os.environ.get("DEMO_MAX_SESSION_BYTES", "52428800"))  # 50MB anti-drain
    # 24.7-B2: daily WALLET ceiling across ALL demo sessions (brake vs VPN-rotation
    # abuse that sidesteps the per-IP limit). Demo opens 503 past this until midnight.
    DEMO_DAILY_USD_CEILING = float(os.environ.get("DEMO_DAILY_USD_CEILING", "50"))
    OPS_ALERT_WEBHOOK = os.environ.get("OPS_ALERT_WEBHOOK", "")  # optional Slack/Discord; no-op if unset
    # Hashed-IP salt + Garcia's test bypass token. BOTH are secrets — set via env on
    # the Fly host (migrated to the host's secret store), NEVER committed.
    DEMO_IP_SALT = os.environ.get("DEMO_IP_SALT", "")
    DEMO_BYPASS_TOKEN = os.environ.get("DEMO_BYPASS_TOKEN", "")
    # Public Turnstile site key the landing embeds (NOT a secret; the SECRET is
    # CLOUDFLARE_TURNSTILE_SECRET, server-side only).
    TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
    # Optional web-search backend for the demo's one read-only tool.
    BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

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
    STRIPE_PRICE_POWER = os.environ.get("STRIPE_PRICE_POWER", "")
    STRIPE_PRICE_TEAM = os.environ.get("STRIPE_PRICE_TEAM", "")  # legacy alias for power

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
    # Empty until a real signed installer is hosted; the download UI shows
    # "not available" rather than a dead link. Set these in /root/emma.env.
    DOWNLOAD_PKG_URL = os.environ.get("DOWNLOAD_PKG_URL", "")
    DOWNLOAD_MSI_URL = os.environ.get("DOWNLOAD_MSI_URL", "")

    @property
    def captcha_enabled(self) -> bool:
        return bool(self.CLOUDFLARE_TURNSTILE_SECRET)


settings = Settings()

_INSECURE_DEFAULT = "dev-insecure-change-me"


def assert_secure_secrets() -> None:
    """Refuse to boot in prod with the committed dev signing keys.

    The default JWT/SESSION secrets are public (in this repo). If a prod host (HTTPS
    PUBLIC_URL) is ever deployed without overriding them, anyone could forge session
    cookies and demo JWTs. Fail loud at startup instead of silently trusting them.
    """
    if not settings.PUBLIC_URL.lower().startswith("https"):
        return  # local/dev over http — defaults are fine
    weak = [n for n in ("JWT_SECRET", "SESSION_SECRET")
            if getattr(settings, n) == _INSECURE_DEFAULT]
    if weak:
        raise RuntimeError(
            f"Refusing to start: {', '.join(weak)} still set to the insecure dev "
            "default on an HTTPS deploy. Set them via the host's secret store.")
