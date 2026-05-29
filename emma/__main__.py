"""Run Emma: ``python -m emma [--debug] [--simulate-crash]``.

Sets up rotated JSON logging, runs permission and wake-word preflights,
wraps the orchestrator in a top-level crash handler, and exits cleanly
when the dev tool requests it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

import structlog

from config.settings import settings
from core import orchestrator, permissions
from core.crash_handler import handle_crash
from core.redaction import redaction_processor

LOG_DIR = Path.home() / "Library/Logs/Emma"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Background tasks (e.g. the opt-in dashboard) kept alive for the process lifetime.
_bg_tasks: list[asyncio.Task] = []


def _setup_logging(debug: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handlers: list[logging.Handler] = []
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "emma.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(file_handler)

    if debug:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(console)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redaction_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="emma")
    p.add_argument("--debug", action="store_true", help="verbose console logging")
    p.add_argument(
        "--simulate-crash",
        action="store_true",
        help="raise after the first wake word (exercises the crash handler)",
    )
    return p.parse_args()


def _credential_preflight(log: structlog.BoundLogger) -> int | None:
    """Fast startup credential check. Returns exit code 2 on a bad key, else None.

    A missing or malformed OpenAI key can never produce a working session, so
    we fail fast — before probing permissions, opening the mic, or waiting for
    a wake word — instead of looping on reconnect. Exit code 2 is distinct from
    0 (success) and 1 (generic), so launchd's SuccessfulExit=false policy treats
    it as a real failure rather than a retry case.
    """
    from core.conversation import _looks_like_openai_key

    if not _looks_like_openai_key(settings.OPENAI_API_KEY):
        log.error(
            "credentials_invalid",
            field="OPENAI_API_KEY",
            present=bool(settings.OPENAI_API_KEY),
            length=len(settings.OPENAI_API_KEY or ""),
        )
        return 2
    return None


async def _run_orchestrator(log: structlog.BoundLogger) -> int:
    """Run the orchestrator with cooperative SIGINT/SIGTERM shutdown.

    Cancelling the orchestrator task raises CancelledError into whatever it is
    awaiting (wake-word listen or the Pipecat runner), which unwinds cleanly and
    runs the orchestrator's finally-block cleanup. A SystemExit (terminal auth
    error from run_session) propagates out so the process exits non-zero.
    """
    orchestrator_task = asyncio.create_task(orchestrator.main_loop())

    # Opt-in: run the JARVIS dashboard/visualizer in THIS process so the
    # in-process events_bus is shared (publishers + WS subscribers same loop).
    if os.environ.get("EMMA_DASHBOARD", "").lower() in ("1", "true", "yes"):
        from dashboard import server as dashboard

        # Keep a reference so the task isn't garbage-collected mid-run.
        _bg_tasks.append(asyncio.create_task(dashboard.start()))
        log.info("dashboard_started", port=settings.DASHBOARD_PORT)

    def _shutdown(sig: int) -> None:
        log.info("signal_received", sig=signal.Signals(sig).name)
        # Set the flag first so the loop exits even if Pipecat swallows the
        # cancel during an active session; cancel makes idle wake-listening
        # unwind immediately.
        orchestrator.request_shutdown()
        orchestrator_task.cancel()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(s, _shutdown, s)

    try:
        await orchestrator_task
    except asyncio.CancelledError:
        log.info("orchestrator_cancelled")
    return 0


def main() -> int:
    args = _parse_args()
    _setup_logging(args.debug)
    log = structlog.get_logger("emma")
    log.info("starting", debug=args.debug, simulate_crash=args.simulate_crash)

    # Credential pre-flight FIRST: fail fast on a bad OpenAI key (exit 2) before
    # any permission probe, mic open, or wake-word wait.
    cred_rc = _credential_preflight(log)
    if cred_rc is not None:
        return cred_rc

    if args.simulate_crash:
        orchestrator.enable_simulate_crash()

    if not permissions.preflight():
        log.error("permissions_preflight_failed")
        # Don't exit hard - the user is being asked to grant. Let launchd retry
        # on the next start once permissions are granted.
        return 1

    orchestrator.preflight()

    # Warm the environment detection cache (idempotent; uses 24h TTL).
    try:
        from actions import environment

        environment.warm_cache()
    except Exception as exc:
        log.warning("env_warm_cache_failed", error=str(exc))

    # Bring up the long-term memory store (creates ~/.emma/memory.db on
    # first launch). Idempotent.
    try:
        from memory import long_term as memory_lt

        memory_lt.initialize()
    except Exception as exc:
        log.warning("memory_initialize_failed", error=str(exc))

    try:
        return asyncio.run(_run_orchestrator(log))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    except SystemExit:
        raise
    except BaseException as exc:
        log.error("unhandled_exception", error=str(exc))
        ctx = orchestrator.last_context()
        return handle_crash(exc, ctx, REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
