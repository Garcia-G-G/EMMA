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

# Voice "duérmete N minutos" pauses wake listening until this monotonic deadline.
# 0 = listening normally. Set by the snooze_listening tool; honored in _one_session.
_snooze_until: float = 0.0


def snooze_listening(minutes: int) -> float:
    """Pause wake detection for ``minutes`` (then auto-resume). Returns the deadline."""
    global _snooze_until
    _snooze_until = time.monotonic() + max(1, int(minutes)) * 60
    log.info("listening_snoozed", minutes=minutes)
    return _snooze_until


def snooze_remaining_s() -> float:
    return max(0.0, _snooze_until - time.monotonic())


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


# 22.1-B39: voice-energy threshold + frame count for "Garcia kept talking
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
        events_bus.publish("state", state="waiting_for_wake")
        # Voice "duérmete N min": pause wake detection until the snooze expires.
        while snooze_remaining_s() > 0:
            events_bus.publish("state", state="snoozing")
            await asyncio.sleep(min(5.0, snooze_remaining_s()))
        t_start = time.monotonic()
        await listen_for_wake_word()
        t_wake = time.monotonic()
        log.info("wake_detected", elapsed_ms=int((t_wake - t_start) * 1000))
        events_bus.publish("wake_detected")
        events_bus.publish("session_started", id=turn_id, ts=_now_iso())
        events_bus.publish("state", state="listening")
        # Let the ack tone fully decay before Pipecat opens the mic — and USE
        # that window (22.1-B39): if Garcia chained "hey jarvis, abre X" in
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
        events_bus.publish("state", state="waiting_for_wake")
    finally:
        _last_session_end_mono = time.monotonic()
        structlog.contextvars.unbind_contextvars("turn_id")


async def _ensure_paired() -> None:
    """PAIR-DEVICE-1 (Part G) — gate the daemon on device pairing in managed/client
    mode. Enabled only when EMMA_REQUIRE_PAIRING is set; a dev/BYOK daemon (own
    OpenAI key in .env) leaves it unset and is unaffected. On first run it fetches a
    user_code, speaks + prints it, and blocks until the user authorizes on the web.
    If pairing can't complete, exits cleanly (SystemExit) so launchd doesn't
    loop-restart into a broken pairing spin."""
    import os

    if os.environ.get("EMMA_REQUIRE_PAIRING", "").lower() not in ("1", "true", "yes"):
        return
    from core import pairing

    if await pairing.is_paired():
        return
    try:
        info = await pairing.start_pairing()
    except Exception as exc:
        log.error("pairing_start_failed", error=str(exc))
        print("No pude conectar con el servidor de Emma. Revisa tu internet y reinicia.", flush=True)
        raise SystemExit(2) from exc

    code = info["user_code"]
    spoken = ("Hola. Para empezar, visita tu cuenta en theemmafamily.com slash pair "
              f"y pon este código: {' '.join(code.replace('-', ' guion '))}")
    import subprocess

    subprocess.run(["say", "-v", "Paulina", spoken], check=False)  # `say`, not Realtime (unpaired)
    print(f"\n  Pair code: {code}", flush=True)
    print(f"  Visit:      {info['verification_uri']}\n", flush=True)

    token = await pairing.poll_until_authorized(
        info["device_code"], info["interval"], info["expires_in"])
    if not token:
        print("La vinculación expiró. Reinicia Emma para intentar de nuevo.", flush=True)
        raise SystemExit(2)
    log.info("device_paired_at_boot")


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
    log.info("waiting_for_wake")
    try:
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
