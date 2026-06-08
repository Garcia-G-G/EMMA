"""Cross-thread runtime flags shared between the asyncio event loop and the
PortAudio callback thread (HOTFIX Layer B — echo feedback loop).

Pipecat fires ``BotStarted/StoppedSpeakingFrame`` on the asyncio loop; the
wake-word model runs inside a ``sounddevice`` callback on a PortAudio thread.
An ``asyncio.Event`` is unsafe to set from that thread, so this module uses
``threading`` primitives only.

``bot_speaking`` is SET while Emma speaks and stays set for a short tail after
she stops (speaker decay). The wake-word listener consults
``suppress_wake()`` — which ALSO enforces a stream-open *warmup* — so Emma's
own "hola soy Emma" bleeding into the mic at a session boundary cannot re-fire
the wake detector.

Architecture note (audit, HOTFIX): wake-listen and the Pipecat session are
*sequential* (orchestrator awaits ``listen_for_wake_word`` to completion, then
runs the session), so ``bot_speaking`` is effectively dormant during normal
wake listening — the *warmup* is what actually suppresses boundary echo. The
flag is kept as defense-in-depth (and for any future concurrent wake path),
and ``force_clear()`` guarantees a session that dies mid-speech can never
leave the gate stuck closed.
"""

from __future__ import annotations

import threading
import time

# SET while Emma is (recently) speaking. Read from the wake callback thread.
bot_speaking = threading.Event()

_lock = threading.Lock()
_tail_timer: threading.Timer | None = None


def mark_started() -> None:
    """Emma started speaking — close the gate (cancel any pending tail)."""
    global _tail_timer
    with _lock:
        if _tail_timer is not None:
            _tail_timer.cancel()
            _tail_timer = None
    bot_speaking.set()


def mark_stopped(tail_ms: int = 800) -> None:
    """Emma stopped — clear the gate after ``tail_ms`` of speaker-decay grace.

    ``tail_ms <= 0`` clears immediately. A pending tail is replaced.
    """
    global _tail_timer
    with _lock:
        if _tail_timer is not None:
            _tail_timer.cancel()
            _tail_timer = None
        if tail_ms <= 0:
            bot_speaking.clear()
            return
        timer = threading.Timer(tail_ms / 1000.0, bot_speaking.clear)
        timer.daemon = True
        _tail_timer = timer
        timer.start()


def force_clear() -> None:
    """Hard reset: cancel any tail and clear the flag.

    Called at wake-listen start so a session that died mid-speech (the tail
    never fired) cannot leave the wake listener permanently deaf. Boundary
    echo is handled by the warmup, not by this flag, so clearing here is safe.
    """
    global _tail_timer
    with _lock:
        if _tail_timer is not None:
            _tail_timer.cancel()
            _tail_timer = None
    bot_speaking.clear()


def suppress_wake(open_monotonic: float, warmup_s: float, now: float | None = None) -> bool:
    """True if the wake listener should skip prediction on this frame.

    Suppressed when within the post-open warmup window (residual echo of
    Emma's last utterance still decaying) OR while ``bot_speaking`` is set.
    """
    current = time.monotonic() if now is None else now
    if current - open_monotonic < warmup_s:
        return True
    return bot_speaking.is_set()
