"""Run Emma: ``python -m emma [--debug] [--simulate-crash]``.

Sets up rotated JSON logging, runs permission and wake-word preflights,
wraps the orchestrator in a top-level crash handler, and exits cleanly
when the dev tool requests it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from config.settings import settings
from core import orchestrator, permissions
from core.crash_handler import handle_crash

LOG_DIR = Path.home() / "Library/Logs/Emma"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup_logging(debug: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else getattr(
        logging, settings.LOG_LEVEL.upper(), logging.INFO
    )

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


def main() -> int:
    args = _parse_args()
    _setup_logging(args.debug)
    log = structlog.get_logger("emma")
    log.info("starting", debug=args.debug, simulate_crash=args.simulate_crash)

    if args.simulate_crash:
        orchestrator.enable_simulate_crash()

    if not permissions.preflight():
        log.error("permissions_preflight_failed")
        # Don't exit hard - the user is being asked to grant. Let launchd retry
        # on the next start once permissions are granted.
        return 1

    orchestrator.preflight()

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    except SystemExit:
        raise
    except BaseException as exc:
        log.error("unhandled_exception", error=str(exc))
        ctx = orchestrator.last_context()
        return handle_crash(exc, ctx, REPO_ROOT)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
