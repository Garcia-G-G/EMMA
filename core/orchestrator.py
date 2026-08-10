"""Orchestrator: wake word → Pipecat session → repeat.

Pipecat owns the streaming pipeline (mic → OpenAIRealtimeLLMService →
speaker, server-side VAD, native barge-in, function-call dispatch),
so the orchestrator's job collapses to wake → run_session → loop.

Memory priming is wired into the session system prompt (reads from
long-term store on each session start). Reflection (automatic fact
extraction from transcripts) requires Pipecat transcript event
hooks — not yet implemented.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime

import structlog

from config.settings import settings
from core import background, conversation, dev_state, events_bus
from core.wake_word import listen_for_wake_word

log = structlog.get_logger("emma.orchestrator")

_shutdown = asyncio.Event()
_simulate_crash = False
_last_turn_id: str = ""
# Monotonic clock at the moment the previous session ended. The watchdog (B1)
# flags `daemon_stuck` if the loop doesn't get back to wake-word listening
# within this many seconds — surfacing a wedged mic/socket in the logs.
_last_session_end_mono: float = 0.0
_LOOPBACK_WATCHDOG_S = 30.0

# Unpaired-parking poll, ramped (LAUNCH-4 4b). Each tick spawns a `security`
# subprocess; a flat 2s was ~43k/day on a Mac left unpaired.
_PAIR_POLL_MIN_S = 2.0
_PAIR_POLL_MAX_S = 30.0

# Voice "duérmete N minutos" pauses wake listening until this monotonic deadline.
# 0 = listening normally. Set by the snooze_listening tool; honored in _one_session.
_snooze_until: float = 0.0

# Voice "mute" / "apaga el micrófono": an INDEFINITE privacy stop. Unlike snooze
# (timed, auto-resumes), mute has no deadline — while set, _one_session never
# opens the wake stream, so the mic is genuinely released (the macOS mic
# indicator turns off). Recovery is non-voice by design (the mic is off): the
# unmute_mic tool, or a daemon restart, which resets this in-memory flag.
_muted: bool = False


_SNOOZE_MAX_MIN = 24 * 60  # a snooze is a nap, not a permanent mic-off (DoS guard)


def snooze_listening(minutes: int) -> float:
    """Pause wake detection for ``minutes`` (then auto-resume). Returns the deadline."""
    global _snooze_until
    minutes = max(1, min(int(minutes), _SNOOZE_MAX_MIN))
    _snooze_until = time.monotonic() + minutes * 60
    log.info("listening_snoozed", minutes=minutes)
    return _snooze_until


def snooze_remaining_s() -> float:
    return max(0.0, _snooze_until - time.monotonic())


def mute_mic() -> None:
    """Stop capturing audio entirely (indefinite). The wake stream stays closed."""
    global _muted
    _muted = True
    log.info("mic_muted")


def unmute_mic() -> None:
    """Resume listening after a mute."""
    global _muted
    _muted = False
    log.info("mic_unmuted")


def is_muted() -> bool:
    return _muted


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def enable_simulate_crash() -> None:
    """Called by emma/__main__.py when --simulate-crash is passed."""
    global _simulate_crash
    _simulate_crash = True


def request_shutdown() -> None:
    """Ask the main loop to stop after the current session.

    Called by emma.__main__'s SIGINT/SIGTERM handler IN ADDITION to cancelling
    the orchestrator task. Cancellation alone is not enough: while a Pipecat
    session is active, ``runner.run`` swallows the CancelledError and returns
    normally (the same path as an idle timeout), so the loop would otherwise
    spin back to wake-word listening. This flag makes it exit instead.
    """
    _shutdown.set()


def last_context() -> dict[str, str]:
    """Snapshot used by the crash handler. Transcripts wire in Prompt 14."""
    return {"turn_id": _last_turn_id, "last_transcript": "", "response_text": ""}


def preflight() -> None:
    """No-op shim. Wake-word validation runs lazily in core.wake_word."""
    return


# 22.1-B39: voice-energy threshold + frame count for "the user kept talking
# right after the wake word." RMS 1200 is well above room tone, well below
# speech; 3 voiced 80ms frames ≈ 240ms of actual speech (a syllable or two).
_IMMEDIATE_RMS = 1200.0
_IMMEDIATE_FRAMES = 3


async def _detect_immediate_speech(window_s: float = 1.0) -> bool:
    """True if voice energy arrives within ``window_s`` of the wake chime.

    Doubles as the ack-tone decay wait (replaces the old fixed 0.8s sleep).
    Energy-based on purpose: transcription would miss the window. Errors
    degrade to False — never block the session over a probe.
    """
    import numpy as np
    import sounddevice as sd

    from core import audio_devices

    voiced = 0
    try:
        stream = sd.RawInputStream(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            blocksize=1280,  # 80 ms frames
            device=audio_devices.test_input_device_index(),
        )
        stream.start()
        try:
            deadline = time.monotonic() + window_s
            while time.monotonic() < deadline:
                await asyncio.sleep(0.04)
                frames_available = stream.read_available
                if frames_available < 1280:
                    continue
                raw, _ = stream.read(1280)
                samples = np.frombuffer(bytes(raw), dtype=np.int16).astype(np.float64)
                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                if rms >= _IMMEDIATE_RMS:
                    voiced += 1
                    if voiced >= _IMMEDIATE_FRAMES:
                        return True
        finally:
            stream.stop()
            stream.close()
    except Exception as exc:  # probe failure must never block the session
        log.warning("immediate_speech_probe_failed", error=str(exc))
        await asyncio.sleep(0.3)  # keep a minimal ack-decay wait
    return False


# Don't nag: once we've said "you're out", stay quiet until minutes come back.
_out_of_minutes = False


def out_of_minutes() -> bool:
    """True while the paired account has no minutes left (read by the app)."""
    return _out_of_minutes


async def _check_out_of_minutes() -> bool:
    """After a session, ask the backend whether the minutes ran out. Speak once.

    Deliberately NOT driven by the WebSocket close code. The proxy calls
    ``ws.close(code=4402, reason="balance_zero")`` at
    ``backend/realtime_proxy.py:109,119`` — both BEFORE ``ws.accept()`` at
    ``:144``, so the handshake is rejected outright and the code never reaches
    the client; and a mid-session cut closes at ``:231`` with no code at all,
    which ``_ZOMBIE_MARKERS`` would read as an ordinary zombie. Until LAUNCH-6
    fixes that ordering, a close code cannot tell "out of minutes" from "bad
    token" — so we ask the balance route instead, which is authoritative and
    needs no close code at all.

    Best-effort and cheap: one GET per session end, managed mode only, never
    fatal. Returns True when it published the out-of-minutes state.
    """
    global _out_of_minutes
    if not settings._is_managed():
        return False
    try:
        from core import pairing

        client = await pairing.authed_client()
        async with client as c:
            r = await asyncio.wait_for(c.get("/api/device/balance"), timeout=8.0)
            if r.status_code != 200:
                return False
            left = float(r.json().get("total_left_min") or 0.0)
    except Exception as exc:  # offline / unpaired — never block the wake loop
        log.debug("balance_check_skipped", error=str(exc))
        return False

    if left > 0:
        if _out_of_minutes:
            log.info("minutes_restored", left_min=left)
        _out_of_minutes = False
        return False
    if _out_of_minutes:
        return True  # already announced; stay quiet, keep the state
    _out_of_minutes = True
    log.error("out_of_minutes", left_min=left)
    events_bus.publish("state", state="out_of_minutes")
    # `say`, not the Realtime API: the session that would have spoken this is
    # exactly the one that just got cut off.
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Mónica",
            "Se acabaron tus minutos gratis del mes. Abre la app para conseguir más.",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    return True


async def _one_session() -> None:
    """Wake → Pipecat session → return. One iteration of the main loop."""
    global _last_turn_id, _last_session_end_mono
    turn_id = uuid.uuid4().hex[:8]
    _last_turn_id = turn_id
    structlog.contextvars.bind_contextvars(turn_id=turn_id)
    try:
        # Watchdog (B1): how long since the prior session ended? Normally ~0.
        # A large gap means the loop was wedged before it could resume listening.
        if _last_session_end_mono:
            gap = time.monotonic() - _last_session_end_mono
            if gap > _LOOPBACK_WATCHDOG_S:
                log.error("daemon_stuck", reason="slow_loopback_to_wake", gap_s=round(gap, 1))
        # Keep the out-of-minutes state visible across loop iterations. She stays
        # listening (so she recovers the moment minutes are topped up), but the
        # menubar and the app must not flip back to a reassuring "en espera"
        # while she can't actually answer.
        events_bus.publish(
            "state", state="out_of_minutes" if _out_of_minutes else "waiting_for_wake"
        )
        # Park before opening the wake stream while muted (indefinite, mic off) or
        # snoozed (timed). Both keep the RawInputStream closed, so the mic is
        # genuinely released — the macOS mic indicator stays off during either.
        while _muted or snooze_remaining_s() > 0:
            events_bus.publish("state", state="muted" if _muted else "snoozing")
            nap = 1.0 if _muted else min(5.0, snooze_remaining_s())
            await asyncio.sleep(max(0.5, nap))
        t_start = time.monotonic()
        await listen_for_wake_word()
        t_wake = time.monotonic()
        log.info("wake_detected", elapsed_ms=int((t_wake - t_start) * 1000))
        events_bus.publish("wake_detected")
        events_bus.publish("session_started", id=turn_id, ts=_now_iso())
        events_bus.publish("state", state="listening")
        # Let the ack tone fully decay before Pipecat opens the mic — and USE
        # that window (22.1-B39): if the user chained "hey jarvis, abre X" in
        # one breath, raw voice ENERGY lands here. Detect it (energy, not
        # STT — transcription is far too slow for this window) and tell the
        # session to skip the greeting preamble.
        immediate = await _detect_immediate_speech(window_s=1.0)
        if immediate:
            log.info("immediate_command_detected")
        if _simulate_crash:
            raise RuntimeError("test crash (--simulate-crash)")
        try:
            await asyncio.wait_for(
                conversation.run_session(immediate_command=immediate),
                timeout=float(settings.SESSION_MAX_S) + 30.0,
            )
        except TimeoutError:
            # The Pipecat session overran even its grace window. Reset fast so the
            # next iteration is back listening within ~1s (B1).
            log.info("session_timeout")
            log.info("session_reset", reason="outer_timeout")
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Cooperative daemon shutdown (SIGINT/SIGTERM) — NEVER swallow.
            # (Zombie recovery does NOT come through here: the watcher cancels
            # the PIPELINE task, which pipecat unwinds as a clean return.)
            raise
        except SystemExit:
            raise  # terminal auth error — ops escalation path, propagate
        except Exception as exc:  # 22.1-B35.2: last-resort session tolerance
            # An unexpected session error must cost ONE session, not the
            # daemon. Log loudly, recover to wake-word listening.
            log.error("session_cancelled_recovered", reason=f"{type(exc).__name__}: {exc}"[:200])
            await asyncio.sleep(0.5)
        duration_s = round(time.monotonic() - t_wake, 1)
        log.info("session_close", duration_s=int(duration_s))
        events_bus.publish("session_ended", id=turn_id, duration_s=duration_s)
        if await _check_out_of_minutes():
            return  # state already published; skip the waiting_for_wake reset
        events_bus.publish("state", state="waiting_for_wake")
    finally:
        _last_session_end_mono = time.monotonic()
        structlog.contextvars.unbind_contextvars("turn_id")


_onboarding_needed = False


def onboarding_needed() -> bool:
    """True while the daemon is parked waiting for the app to pair this Mac
    (managed mode, no device token yet). Read by the dashboard's onboarding-state
    endpoint so the app knows to show the onboarding flow instead of the steady
    state (PAID-ONBOARDING Part 2/3)."""
    return _onboarding_needed


async def _ensure_paired() -> None:
    """PAIR-DEVICE-1 — in managed/client mode, PARK ALIVE until the app pairs this Mac.

    The app owns onboarding now (PAID-ONBOARDING). A fresh managed install boots
    with no device token; instead of pairing in the terminal or exiting, we publish
    ``state=needs_onboarding`` — the menubar UI + onboarding window (running in this
    process under EMMA_DASHBOARD) show the flow — and poll the Keychain until the app
    writes a device token (its browser pairing calls core.pairing), then load it and
    fall through to the wake loop. Never opens a Realtime session while unpaired
    (there's no account yet). A dev/BYOK daemon (EMMA_REQUIRE_PAIRING unset) returns
    immediately and is unaffected. Cancellable: a shutdown unwinds the wait cleanly."""
    global _onboarding_needed

    # Same resolution as settings.openai_api_key()/_credential_preflight. If these
    # ever disagree the daemon either parks with a usable key or, worse, runs
    # unparked with none — so there is exactly one definition of "managed".
    if not settings._is_managed():
        return
    from core import pairing

    try:
        if await pairing.is_paired():
            await pairing.load_token_cache()  # Phase 2B: managed OpenAI calls read this
            return
    except Exception as exc:
        # A locked/hung Keychain at login raises here too (core/secrets.py's 5s
        # timeout). The in-loop probe below degrades; this initial one must as well,
        # or an uncaught raise propagates to main_loop -> handle_crash -> exit 1 ->
        # a permanent launchd boot loop. Fall through into the park loop, which
        # retries with backoff and completes once the Keychain unlocks.
        log.warning("pairing_probe_failed", error=str(exc), phase="initial")

    _onboarding_needed = True
    log.info("awaiting_onboarding")
    # Ramp 2s -> 30s. Every tick is a `security` subprocess plus a log line, and
    # a Mac can sit unpaired for days: at a flat 2s that was ~43k Keychain
    # spawns/day. Stay responsive for the first minute (the user is looking at
    # the onboarding window right now), then relax.
    delay = _PAIR_POLL_MIN_S
    try:
        while not _shutdown.is_set():
            try:
                paired = await pairing.is_paired()
            except Exception as exc:
                # A locked/hung Keychain raises (core/secrets.py's 5s timeout).
                # Uncaught, that propagated to main_loop -> handle_crash -> exit 1
                # -> launchd restart, i.e. a locked keychain became a permanent
                # boot loop. Degrade: keep parking and try again.
                log.warning("pairing_probe_failed", error=str(exc), retry_in_s=delay)
                paired = False
            if paired:
                await pairing.load_token_cache()
                log.info("onboarding_complete")
                events_bus.publish("state", state="waiting_for_wake")
                return
            # Re-publish each tick: the events bus doesn't replay to subscribers, so
            # a late/reconnecting UI still learns it should show onboarding.
            events_bus.publish("state", state="needs_onboarding")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, _PAIR_POLL_MAX_S)
    finally:
        _onboarding_needed = False


async def main_loop() -> None:
    """Entry point called by ``emma.__main__``.

    Signal handling lives in ``emma.__main__``, which cancels this task on
    SIGINT/SIGTERM. We must let ``CancelledError`` propagate (cooperative
    shutdown) rather than swallow it, and run cleanup in ``finally``. A
    ``SystemExit`` (terminal auth error raised by ``run_session``) likewise
    propagates so ``__main__`` can exit non-zero. Only ordinary
    ``RuntimeError``/``Exception`` per-session faults are caught and retried.
    """
    # Touch the background-task registry once: loads ~/.emma/tasks.jsonl and
    # marks any in-flight rows from a previous run as "aborted" on disk.
    background.registry()
    try:
        # Managed mode: park alive until the app pairs this Mac (no-op for a paired
        # or dev/BYOK daemon). Runs after the dashboard + menubar UI are up (started
        # as sibling tasks in emma.__main__) so there's somewhere to onboard.
        await _ensure_paired()
        if _shutdown.is_set():
            return
        log.info("waiting_for_wake")
        while not _shutdown.is_set():
            try:
                await _one_session()
            except RuntimeError as exc:
                if "test crash" in str(exc):
                    raise
                log.error("session_error", error=str(exc))
                await asyncio.sleep(0.5)
            except Exception as exc:
                log.error("session_error", error=str(exc))
                await asyncio.sleep(0.5)
            if dev_state.shutdown_requested.is_set():
                log.info("dev_mode_exit")
                break
    finally:
        # Cancel any in-flight background tasks so Python exits without
        # "pending task" warnings (their detached subprocesses outlive us if
        # started with start_new_session — by design).
        with contextlib.suppress(Exception):
            await background.registry().cancel_all()
        try:
            from tools.browser import shutdown_browser

            await shutdown_browser()
        except Exception:
            pass
        log.info("shutdown_complete")


run = main_loop  # backward-compat: emma/__main__.py historically used run()
