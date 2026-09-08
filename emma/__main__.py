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
import time
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core import boot_guard, control_auth, orchestrator, permissions
from core.crash_handler import handle_crash
from core.redaction import redaction_processor

LOG_DIR = Path.home() / "Library/Logs/Emma"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Background tasks (e.g. the opt-in dashboard) kept alive for the process lifetime.
_bg_tasks: list[asyncio.Task[Any]] = []


def _report_bg_failure(task: asyncio.Task[Any]) -> None:
    """Surface a background task's exception instead of letting it vanish."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        structlog.get_logger("emma").error(
            "background_task_failed", task=task.get_name(), error=f"{type(exc).__name__}: {exc}"
        )


def _spawn_bg(coro: Any, *, name: str) -> asyncio.Task[Any]:
    """Create a tracked sibling task whose exception can never vanish silently.

    Every long-lived background task (dashboard, UI supervisor, proactive engine,
    conditionals watcher) must carry `_report_bg_failure`, or a raise inside it dies
    unseen — the exact invisibility the callback exists to prevent."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_report_bg_failure)
    _bg_tasks.append(task)
    return task


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

    # stderr ALWAYS, not just in debug. launchd already captures it to
    # StandardErrorPath, so this costs nothing and buys the thing whose absence
    # cost an entire investigation: with only the file handler attached, every
    # structlog line after configure() went to ~/Library/Logs/Emma/emma.log and
    # NOWHERE else. stdout.log stopped mid-boot — not where the process died,
    # but where logging changed destination — and the operator had no way to
    # know the real log existed. A daemon's logs belong where whoever is
    # debugging it is already looking.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(console)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    # Printed, not logged: this must be readable even if structlog itself is
    # misconfigured, and it is the pointer that was missing from stdout/stderr.
    print(f"[emma] logging to {LOG_DIR / 'emma.log'}", file=sys.stderr, flush=True)

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


async def _spawn(*args: str) -> int:
    """Run a short command without blocking the event loop. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        return await asyncio.wait_for(proc.wait(), timeout=30.0)
    except Exception as exc:
        structlog.get_logger("emma").warning("spawn_failed", cmd=args[0], error=str(exc))
        return -1


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
        # await, don't block: these run on the event loop (4i). An audio
        # pipeline that stalls its loop is audible, and `say` here is seconds long.
        await _spawn(
            "say", "-v", "Paulina",
            f"Tu código de vinculación es: {spoken}. Ábrelo en tu navegador",
        )
        await _spawn("open", uri)
        result = await pairing.poll_until_authorized(
            info["device_code"], int(info.get("interval", 5)), int(info.get("expires_in", 900))
        )
        if result:
            await _spawn("say", "-v", "Paulina", "Emma vinculada. Ya puedes usarme")
            print("\n  ✓ Vinculada exitosamente.\n")
            return 0
        print("\n  ✗ Vinculación no completada (código expiró o fue rechazado).\n")
        return 2

    log.info("first_run_pairing")
    return asyncio.run(do_pair())


def _looks_managed() -> bool:
    """Managed mode, resolved the resilient way (see settings._is_managed)."""
    return settings._is_managed()


def _terminal_exit(log: structlog.BoundLogger, kind: str, hint: str) -> int:
    """Exit code for a boot that failed for a reason a restart won't fix.

    Exit-code semantics, corrected. `KeepAlive{SuccessfulExit=false}` means
    "restart whenever the exit was NOT successful" — i.e. EVERY non-zero code.
    The old comments here claimed exit 2 was treated as a real failure and not
    retried; that is backwards, and with no ThrottleInterval it produced a
    10-second respawn loop forever.

    So: non-zero means "retry me" (now throttled to >=30s by the plist), and
    0 means "stay down". We return non-zero for the first few attempts, because
    plenty of these are transient — a Keychain still unlocking at login looks
    identical to a broken one — and then 0 once the streak proves otherwise,
    so a genuinely broken config stops burning CPU and stops talking.
    """
    n = boot_guard.record_failure(kind)
    if boot_guard.should_stay_down(kind):
        log.error(
            "boot_failed_terminal", kind=kind, consecutive=n, hint=hint,
            action="exiting 0 so launchd stops retrying; fix the above and run "
                   "`launchctl kickstart -k gui/$(id -u)/com.emma.daemon`",
        )
        return 0
    log.error("boot_failed_retryable", kind=kind, consecutive=n, hint=hint)
    return 1


def _credential_preflight(log: structlog.BoundLogger) -> int | None:
    """Fast startup credential check. Returns an exit code on a bad key, else None.

    A missing or malformed OpenAI key can never produce a working session, so we
    fail fast — before probing permissions, opening the mic, or waiting for a
    wake word — instead of looping on reconnect. The exit code comes from
    ``_terminal_exit``: retryable at first, then 0 to stay down.

    Managed/client mode is exempt: there is no local sk- key to validate — the
    credential is the paired device bearer, resolved from Keychain AFTER the app
    pairs this Mac (post-boot, see orchestrator._ensure_paired). Failing here
    would stop the daemon from ever booting to show onboarding.

    The exemption is deliberately NOT gated on the env var alone. A managed
    daemon whose EMMA_REQUIRE_PAIRING went missing would otherwise die on a
    credential it is never supposed to have — one absent environment variable
    should not be able to kill the daemon (LAUNCH-4 Part 1).
    """
    if _looks_managed():
        return None
    from core.conversation import _looks_like_openai_key

    if not _looks_like_openai_key(settings.OPENAI_API_KEY):
        log.error(
            "credentials_invalid",
            field="OPENAI_API_KEY",
            present=bool(settings.OPENAI_API_KEY),
            length=len(settings.OPENAI_API_KEY or ""),
        )
        return _terminal_exit(
            log, "credentials",
            "set OPENAI_API_KEY in .env (BYOK), or pair this Mac for managed mode",
        )
    return None


def _wake_preflight(log: structlog.BoundLogger) -> int | None:
    """Fail fast (with backoff) if the shipped wake engine's model is missing.

    Otherwise ``core/wake_sherpa.py`` raises ``SystemExit`` lazily on the FIRST
    session — and ``SystemExit`` is a ``BaseException``, so it slips past
    ``main_loop``'s ``except Exception`` and becomes a permanent 30s launchd loop
    (the model won't reappear on its own). Route it through the same
    ``_terminal_exit`` backoff as credentials/permissions instead. Only sherpa (the
    default) is checked here; the other engines keep their own lazy guards.
    """
    engine = (settings.WAKE_WORD_ENGINE or "sherpa").strip().lower()
    if engine != "sherpa":
        return None
    model = Path(settings.SHERPA_KWS_MODEL_PATH).expanduser()
    if (model / "tokens.txt").exists():
        return None
    log.error("wake_model_missing", engine=engine, path=str(model))
    return _terminal_exit(
        log, "wake_model",
        f"sherpa KWS model missing at {model} — re-run the installer (step 5)",
    )


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
                env={
                    **os.environ,
                    "EMMA_DASHBOARD_PORT": str(settings.DASHBOARD_PORT),
                    # The UI is the ONLY process allowed on the control channel.
                    # Hand it this boot's token rather than letting it mint one:
                    # two tokens would disagree and every command would fail
                    # closed (LAUNCH-11 Part 2).
                    "EMMA_CONTROL_TOKEN": control_auth.token(),
                },
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
        _spawn_bg(dashboard.start(), name="emma-dashboard")  # type: ignore[no-untyped-call]
        log.info("dashboard_started", port=settings.DASHBOARD_PORT)

        # Spawn + supervise the menubar UI (EMMA-APP). It needs the control channel
        # the dashboard just opened, so it rides the same opt-in flag.
        _spawn_bg(_supervise_ui(log), name="emma-ui-supervisor")
        log.info("emma_ui_supervisor_spawned")

    # Proactive engine (Prompt 17): scheduled briefings + event triggers. Runs
    # as a sibling task in this process so it shares the events_bus + memory.
    if settings.PROACTIVE_ENABLED:
        from core.proactive import engine as proactive_engine

        _spawn_bg(proactive_engine.run(), name="emma-proactive")
        log.info("proactive_engine_spawned")

    # Conditional-trigger watcher (Prompt 32): polls mail / calendar / clock and
    # fires "si X pasa, haz Y" actions once. Independent of the proactive engine.
    from core import conditionals

    _spawn_bg(conditionals.watch(), name="emma-conditionals")
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
    # WHICH code is this? A stale ~/.emma/src tarball — months old, missing the
    # managed-mode exemption — burned two investigations, because every check
    # made was against the repo rather than the installation. Log it at boot so
    # the first line of any future triage answers that question. Local only; a
    # daemon must not need GitHub reachable to start.
    from core import version as _version

    log.info("version", **_version.boot_line())

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
        # The user is being asked to grant something. Retry a few times (the
        # plist throttles to >=30s), then stay down rather than restarting
        # forever — and note that preflight SPEAKS on a denial, so an
        # unthrottled loop would repeat that sentence at the user indefinitely.
        return _terminal_exit(
            log, "permissions",
            "grant the permission in System Settings → Privacy & Security",
        )

    wake_rc = _wake_preflight(log)
    if wake_rc is not None:
        return wake_rc

    orchestrator.preflight()

    # Booted past every stage that can hold the daemon down — forget prior failure
    # streaks so a user who just fixed their config isn't tripped by yesterday's
    # (boot_guard.should_stay_down only fires on a fresh streak otherwise).
    boot_guard.clear()

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


def _flush_and_hard_exit(rc: int) -> None:
    """Terminate NOW, skipping Python's interpreter finalization.

    ``Py_FinalizeEx`` dlcloses native extension modules at exit, and several of ours
    (portaudio/CoreAudio HAL, sherpa_onnx/onnxruntime, sqlite3) intermittently
    DEADLOCK in ``dlclose``/``__cxa_finalize`` — so the daemon shuts down cleanly but
    the PROCESS never dies: voice "apágate" or a SIGTERM then hangs forever (confirmed
    by a `sample`: the main thread wedged in ``Py_FinalizeEx → finalize_modules →
    dlclose``). By the time ``main`` returns, ``asyncio.run`` has closed the loop and
    the orchestrator's cleanup has run, so nothing important is left — flush the logs
    (``os._exit`` skips atexit + stdio buffers) and hard-exit past the hazardous
    finalization. Same philosophy as ``core/wake_word._close_stream_background``: let
    wedged native teardown die with the process instead of blocking on it."""
    with contextlib.suppress(Exception):
        logging.shutdown()
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    try:
        _rc = main()
    except SystemExit as _e:  # terminal-auth exit etc. — still hard-exit, same hazard
        _rc = _e.code if isinstance(_e.code, int) else (1 if _e.code else 0)
    _flush_and_hard_exit(_rc)
