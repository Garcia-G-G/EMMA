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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core import orchestrator, permissions
from core.crash_handler import handle_crash
from core.redaction import redaction_processor

LOG_DIR = Path.home() / "Library/Logs/Emma"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Background tasks (e.g. the opt-in dashboard) kept alive for the process lifetime.
_bg_tasks: list[asyncio.Task[Any]] = []


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
    p.add_argument(
        "--test",
        action="store_true",
        help="voice-acceptance harness mode (19.7): forces EMMA_TEST_MODE on, "
        "activating test-only hooks (input-device override, arg/transcript "
        "logging). Equivalent to env EMMA_TEST_MODE=true. Never set by launchd.",
    )
    p.add_argument(
        "--first-run",
        action="store_true",
        help="open the first-run setup wizard (Prompt 29) instead of the daemon. "
        "Emma.app passes this on the post-install launch.",
    )
    p.add_argument(
        "--pair",
        action="store_true",
        help="with --first-run: run the device-pairing flow (RFC 8628) in the "
        "foreground instead of the HTML wizard. The install.sh calls "
        "`emma --first-run --pair` before registering the LaunchAgent.",
    )
    return p.parse_args()


def _run_pairing(log: structlog.BoundLogger) -> int:
    """Foreground device pairing for install.sh step 7 (CLIENT-INSTALL-PHASE-3).

    Runs with stdin/stdout attached, BEFORE the LaunchAgent is bootstrapped, so a
    failed pairing surfaces visibly in the installer terminal rather than in
    ~/Library/Logs. Emma isn't running yet (no Realtime session), so the pair code
    is spoken via macOS `say`. The device token is Secret-tier — core.pairing
    persists it to the Keychain, never to disk or logs.
    """
    from core import pairing

    async def do_pair() -> int:
        if await pairing.is_paired():
            print("  Ya vinculada. Nada que hacer.")
            return 0
        info = await pairing.start_pairing()
        code = info["user_code"]
        uri = info.get("verification_uri") or "https://theemmafamily.com/pair"
        print(f"\n  Código de vinculación: {code}")
        print(f"  Abre:                    {uri}\n")
        spoken = " ".join(code.replace("-", " guion "))
        subprocess.run(
            ["say", "-v", "Paulina",
             f"Tu código de vinculación es: {spoken}. Ábrelo en tu navegador"],
            check=False,
        )
        subprocess.run(["open", uri], check=False)
        result = await pairing.poll_until_authorized(
            info["device_code"], int(info.get("interval", 5)), int(info.get("expires_in", 900))
        )
        if result:
            subprocess.run(
                ["say", "-v", "Paulina", "Emma vinculada. Ya puedes usarme"],
                check=False,
            )
            print("\n  ✓ Vinculada exitosamente.\n")
            return 0
        print("\n  ✗ Vinculación no completada (código expiró o fue rechazado).\n")
        return 2

    log.info("first_run_pairing")
    return asyncio.run(do_pair())


def _credential_preflight(log: structlog.BoundLogger) -> int | None:
    """Fast startup credential check. Returns exit code 2 on a bad key, else None.

    A missing or malformed OpenAI key can never produce a working session, so
    we fail fast — before probing permissions, opening the mic, or waiting for
    a wake word — instead of looping on reconnect. Exit code 2 is distinct from
    0 (success) and 1 (generic), so launchd's SuccessfulExit=false policy treats
    it as a real failure rather than a retry case.

    Managed/client mode (EMMA_REQUIRE_PAIRING) is exempt: there is no local sk- key
    to validate — the credential is the paired device bearer, resolved from Keychain
    AFTER the app pairs this Mac (which happens post-boot, see orchestrator._ensure_
    paired). Failing here would stop the daemon from ever booting to show onboarding.
    """
    if os.environ.get("EMMA_REQUIRE_PAIRING", "").lower() in ("1", "true", "yes"):
        return None
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


async def _supervise_ui(log: structlog.BoundLogger) -> None:
    """Spawn the menubar UI (`python -m emma.ui`) and respawn it if it dies.

    Killing the UI never kills the daemon — it's a child we own and simply relaunch
    (DoD item 8). Killing the daemon cancels this task, which terminates the child.
    Gated on EMMA_DASHBOARD because the UI needs the in-daemon control channel.

    Escalating backoff on RAPID exits so a UI that can't start (no GUI/WindowServer
    session over SSH, a deterministic crash) doesn't respawn ~30x/min forever; a
    healthy run resets it to the fast cadence.
    """
    min_backoff, max_backoff, healthy_s = 2.0, 60.0, 30.0
    backoff = min_backoff
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "emma.ui",
                env={**os.environ, "EMMA_DASHBOARD_PORT": str(settings.DASHBOARD_PORT)},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:  # e.g. no GUI session — back off, don't spin
            log.warning("emma_ui_spawn_failed", error=str(exc), respawn_in_s=round(backoff, 1))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue
        log.info("emma_ui_spawned", pid=proc.pid)
        started = time.monotonic()
        try:
            await proc.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                proc.terminate()
            raise
        # A UI that ran a healthy while → reset to fast respawn; a rapid exit
        # (crash loop) → escalate the wait up to the cap.
        backoff = min_backoff if (time.monotonic() - started) >= healthy_s else min(backoff * 2, max_backoff)
        log.info("emma_ui_exited", code=proc.returncode, respawn_in_s=round(backoff, 1))
        await asyncio.sleep(backoff)


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
        _bg_tasks.append(asyncio.create_task(dashboard.start()))  # type: ignore[no-untyped-call]
        log.info("dashboard_started", port=settings.DASHBOARD_PORT)

        # Spawn + supervise the menubar UI (EMMA-APP). It needs the control channel
        # the dashboard just opened, so it rides the same opt-in flag.
        _bg_tasks.append(asyncio.create_task(_supervise_ui(log), name="emma-ui-supervisor"))
        log.info("emma_ui_supervisor_spawned")

    # Proactive engine (Prompt 17): scheduled briefings + event triggers. Runs
    # as a sibling task in this process so it shares the events_bus + memory.
    if settings.PROACTIVE_ENABLED:
        from core.proactive import engine as proactive_engine

        _bg_tasks.append(asyncio.create_task(proactive_engine.run(), name="emma-proactive"))
        log.info("proactive_engine_spawned")

    # Conditional-trigger watcher (Prompt 32): polls mail / calendar / clock and
    # fires "si X pasa, haz Y" actions once. Independent of the proactive engine.
    from core import conditionals

    _bg_tasks.append(asyncio.create_task(conditionals.watch(), name="emma-conditionals"))
    log.info("conditionals_watcher_spawned")

    def _shutdown(sig: int) -> None:
        log.info("signal_received", sig=signal.Signals(sig).name)
        # Set the flag first so the loop exits even if Pipecat swallows the
        # cancel during an active session; cancel makes idle wake-listening
        # unwind immediately.
        orchestrator.request_shutdown()
        orchestrator_task.cancel()
        for t in _bg_tasks:
            t.cancel()

    def _reload_tools_sighup() -> None:
        # 37-B: `kill -HUP <pid>` live-reloads tools without restarting the session.
        from core import diagnostics

        result = diagnostics.reload_all_tools()
        log.info("tools_reloaded_via_sighup", reloaded=len(result["reloaded"]), errors=len(result["errors"]))

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(s, _shutdown, s)
    with contextlib.suppress(NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGHUP, _reload_tools_sighup)

    try:
        await orchestrator_task
    except asyncio.CancelledError:
        log.info("orchestrator_cancelled")
    return 0


def main() -> int:
    args = _parse_args()
    _setup_logging(args.debug)
    log = structlog.get_logger("emma")

    # First-run setup wizard (Prompt 29): the installer launches Emma.app with
    # --first-run; serve the guided wizard instead of starting the daemon.
    if args.first_run and args.pair:
        return _run_pairing(log)
    if args.first_run:
        from installer.firstrun import wizard

        log.info("first_run_wizard")
        wizard.run()
        return 0
    if args.test:
        settings.EMMA_TEST_MODE = True  # same switch the harness env sets
    log.info(
        "starting",
        debug=args.debug,
        simulate_crash=args.simulate_crash,
        test_mode=settings.EMMA_TEST_MODE,
    )

    # Credential pre-flight FIRST: fail fast on a bad OpenAI key (exit 2) before
    # any permission probe, mic open, or wake-word wait.
    cred_rc = _credential_preflight(log)
    if cred_rc is not None:
        return cred_rc

    if args.simulate_crash:
        orchestrator.enable_simulate_crash()

    # 24.6-E5: tighten ~/.emma file perms (0700 dir / 0600 files) before anything
    # personal is read or written. Best-effort, never blocks startup.
    with contextlib.suppress(Exception):
        permissions.harden_local_files()

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
