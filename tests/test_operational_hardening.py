"""Prompt 22.1: B35-B40 unit shapes (zombie recovery, reflection wiring,
playlist fallbacks, rolling barge-in, greeting suppression, harness hooks)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from core import session_memory


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import core.conversation as conv

    session_memory.clear()
    monkeypatch.setattr(conv, "_zombie_recoveries", [])
    yield
    session_memory.clear()


# ---- B35: zombie session recovery -------------------------------------------


def _error_frame(message: str):
    from pipecat.frames.frames import ErrorFrame

    return ErrorFrame(error=message)


def _dead_watcher():
    import core.conversation as conv

    w = conv.DeadSessionWatcher()
    task = MagicMock()
    task.cancel = AsyncMock()
    w.set_task(task)
    # FrameProcessor.process_frame needs pipeline plumbing; bypass the base
    # class by stubbing push_frame + super().process_frame side effects.
    w.push_frame = AsyncMock()
    return w, task


async def _feed(watcher, frame):
    # Call the class logic directly (base-class bookkeeping stubbed out).
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    orig = FrameProcessor.process_frame
    FrameProcessor.process_frame = AsyncMock()  # type: ignore[method-assign]
    try:
        await type(watcher).process_frame(watcher, frame, FrameDirection.DOWNSTREAM)
    finally:
        FrameProcessor.process_frame = orig  # type: ignore[method-assign]


class TestDeadSessionWatcher:
    def test_three_zombie_frames_within_window_cancel(self):
        w, task = _dead_watcher()
        for _ in range(3):
            asyncio.run(_feed(w, _error_frame("Error sending client event: received 1000 (OK)")))
        task.cancel.assert_awaited_once()

    def test_spread_out_frames_do_not_cancel(self, monkeypatch):
        import core.conversation as conv

        w, task = _dead_watcher()
        clock = {"t": 100.0}
        monkeypatch.setattr(conv.time, "monotonic", lambda: clock["t"])
        for _ in range(3):
            asyncio.run(_feed(w, _error_frame("received 1000")))
            clock["t"] += 2.5  # outside the 1s debounce window
        task.cancel.assert_not_awaited()

    def test_unrelated_errors_ignored(self):
        w, task = _dead_watcher()
        for _ in range(5):
            asyncio.run(_feed(w, _error_frame("some transient network blip")))
        task.cancel.assert_not_awaited()

    def test_repeated_zombies_trigger_cooldown(self, monkeypatch):
        import core.conversation as conv

        monkeypatch.setattr(conv, "_zombie_recoveries", [])
        for _ in range(3):
            conv._record_zombie_recovery()
        assert conv._zombie_cooldown_s() == conv._ZOMBIE_COOLDOWN_S

    def test_single_zombie_no_cooldown(self, monkeypatch):
        import core.conversation as conv

        monkeypatch.setattr(conv, "_zombie_recoveries", [])
        conv._record_zombie_recovery()
        assert conv._zombie_cooldown_s() == 0.0


class TestOrchestratorTolerance:
    @pytest.mark.asyncio
    async def test_unexpected_session_error_recovers(self, monkeypatch):
        from core import orchestrator

        monkeypatch.setattr(orchestrator, "listen_for_wake_word", AsyncMock())
        monkeypatch.setattr(orchestrator, "_detect_immediate_speech", AsyncMock(return_value=False))
        monkeypatch.setattr(
            orchestrator.conversation, "run_session", AsyncMock(side_effect=RuntimeError("boom"))
        )
        monkeypatch.setattr(orchestrator.asyncio, "sleep", AsyncMock())
        await orchestrator._one_session()  # must NOT raise — one lost session, not the daemon

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self, monkeypatch):
        """Cooperative shutdown must never be swallowed."""
        from core import orchestrator

        monkeypatch.setattr(orchestrator, "listen_for_wake_word", AsyncMock())
        monkeypatch.setattr(orchestrator, "_detect_immediate_speech", AsyncMock(return_value=False))
        monkeypatch.setattr(
            orchestrator.conversation,
            "run_session",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            await orchestrator._one_session()


# ---- B36: reflection wiring ---------------------------------------------------


class TestReflectionWiring:
    @pytest.mark.asyncio
    async def test_bot_text_flush_schedules_reflection(self, monkeypatch):
        from pipecat.frames.frames import BotStoppedSpeakingFrame, LLMTextFrame
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

        import core.conversation as conv

        scheduled = MagicMock()
        appended = MagicMock()
        monkeypatch.setattr(conv, "schedule_reflection", scheduled)
        monkeypatch.setattr(conv, "append_turn", appended)
        monkeypatch.setattr(conv, "last_turns", lambda n: ["t"] * n)

        session_memory.push_event("user", "speech", "recuérdame que soy alérgico al kiwi")
        tap = conv._BotTextTap()
        tap.push_frame = AsyncMock()
        orig = FrameProcessor.process_frame
        FrameProcessor.process_frame = AsyncMock()  # type: ignore[method-assign]
        try:
            await type(tap).process_frame(
                tap, LLMTextFrame("Listo, anotado."), FrameDirection.DOWNSTREAM
            )
            await type(tap).process_frame(tap, BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        finally:
            FrameProcessor.process_frame = orig  # type: ignore[method-assign]

        appended.assert_called_once_with("recuérdame que soy alérgico al kiwi", "Listo, anotado.")
        scheduled.assert_called_once()

    def test_old_collector_no_longer_schedules(self):
        import inspect

        import core.conversation as conv

        src = inspect.getsource(conv.TranscriptCollector)
        assert "schedule_reflection" not in src  # dead trigger removed (B36.2)


# ---- B37: playlist fallbacks ----------------------------------------------------


class TestPlaylistFallbacks:
    def test_scope_drift_evicts_token(self, monkeypatch, tmp_path):
        from tools import music

        monkeypatch.setattr(music.settings, "EMMA_HOME", tmp_path)
        token = tmp_path / "spotify_token.json"
        token.write_text("{}")
        (tmp_path / "spotify_scopes.hash").write_text("old-hash")
        assert music._evict_token_on_scope_drift() is True
        assert not token.exists()
        # Second call: hash now current → no drift.
        token.write_text("{}")
        assert music._evict_token_on_scope_drift() is False
        assert token.exists()

    def test_scopes_include_playlists(self):
        from tools import music

        assert "playlist-read-private" in music._SCOPE_TUPLE
        assert "playlist-read-collaborative" in music._SCOPE_TUPLE

    @pytest.mark.asyncio
    async def test_dash1700_falls_back_to_music_search(self, monkeypatch):
        from core import app_router
        from tools import music

        monkeypatch.setattr(
            app_router,
            "inspect",
            lambda c: app_router.RouteDecision(
                picked="Music", source="running", candidates=["Music"]
            ),
        )
        monkeypatch.setattr(music, "_ensure_running", AsyncMock())

        def boom(script):
            raise music.macos.AppleScriptError(
                "execution error: Can't make some data into the expected type. (-1700)"
            )

        monkeypatch.setattr(music.macos, "run_applescript", boom)
        opened = AsyncMock()
        monkeypatch.setattr(music, "_open_music_search", opened)
        await music.play_playlist("Classical Essentials")
        opened.assert_awaited_once_with("Classical Essentials")

    @pytest.mark.asyncio
    async def test_music_search_failure_is_structured(self, monkeypatch):
        from tools import music

        async def bad_exec(*a, **k):
            raise OSError("no open")

        monkeypatch.setattr(music.asyncio, "create_subprocess_exec", bad_exec)
        res = await music._open_music_search("Classical Essentials")
        assert res.success is False
        assert res.data["failure_reason"] == "playlist_not_in_library"


# ---- B38: rolling-window barge-in ----------------------------------------------


def _chunk(level: int, samples: int = 480) -> bytes:
    return (np.ones(samples, dtype=np.int16) * level).tobytes()


class TestRollingBargeIn:
    def _gate(self):
        from core.echo_gate import EchoGateFilter

        return EchoGateFilter(
            tail_ms=600, barge_in_rms=18_000.0, barge_in_rms_window=6_000.0, window_ms=250
        )

    def test_sustained_normal_voice_barges(self, monkeypatch):
        import core.echo_gate as eg

        g = self._gate()
        g.set_bot_speaking(True)
        clock = {"t": 10.0}
        monkeypatch.setattr(eg.time, "monotonic", lambda: clock["t"])
        out = b""
        for _ in range(20):  # 7000 RMS sustained, 20ms steps → window fills
            clock["t"] += 0.02
            out = asyncio.run(g.filter(_chunk(7000)))
        assert out == _chunk(7000)  # window signal fired

    def test_single_moderate_frame_does_not_barge(self):
        g = self._gate()
        g.set_bot_speaking(True)
        out = asyncio.run(g.filter(_chunk(7000)))
        assert out == b"\x00" * len(_chunk(7000))  # window not filled yet

    def test_spike_shortcut_still_instant(self):
        g = self._gate()
        g.set_bot_speaking(True)
        out = asyncio.run(g.filter(_chunk(25_000)))
        assert out == _chunk(25_000)

    def test_quiet_echo_stays_gated(self, monkeypatch):
        import core.echo_gate as eg

        g = self._gate()
        g.set_bot_speaking(True)
        clock = {"t": 10.0}
        monkeypatch.setattr(eg.time, "monotonic", lambda: clock["t"])
        for _ in range(20):
            clock["t"] += 0.02
            out = asyncio.run(g.filter(_chunk(3000)))  # below the 6000 window mean
        assert out == b"\x00" * len(_chunk(3000))

    def test_opener_blocks_both_signals(self):
        from core.echo_gate import EchoGateFilter

        g = EchoGateFilter(
            tail_ms=600,
            barge_in_rms=18_000.0,
            phase_provider=lambda: "opener",
            barge_in_rms_window=6_000.0,
        )
        g.set_bot_speaking(True)
        assert asyncio.run(g.filter(_chunk(25_000))) == b"\x00" * len(_chunk(25_000))


# ---- B39: greeting suppression ---------------------------------------------------


class TestGreetingSuppression:
    def test_immediate_flag_adds_skip_directive(self, monkeypatch):
        import core.conversation as conv

        seed = conv._session_seed_messages(immediate_command=True)
        contents = [m["content"] for m in seed if m["role"] == "system"]
        assert any("Skip any greeting" in c for c in contents)

    def test_cold_wake_keeps_greeting(self):
        import core.conversation as conv

        seed = conv._session_seed_messages(immediate_command=False)
        contents = [m["content"] for m in seed if m["role"] == "system"]
        assert not any("Skip any greeting" in c for c in contents)


# ---- B40: harness lifecycle hooks -------------------------------------------------


class TestHarnessLifecycle:
    def test_shell_script_runs(self, monkeypatch):
        import subprocess

        from tests.acceptance import runner

        calls = []
        monkeypatch.setattr(
            runner.__dict__.setdefault("subprocess", subprocess),
            "run",
            lambda cmd, **k: calls.append(cmd) or MagicMock(returncode=0),
            raising=False,
        )
        runner._run_lifecycle_script("shell: echo hi", label="setup", scenario_id="T1")
        assert calls and calls[0][:2] == ["/bin/bash", "-c"]

    def test_applescript_prefix_selects_osascript(self, monkeypatch):
        import subprocess as sp

        from tests.acceptance import runner

        recorded = {}

        def fake_run(cmd, **k):
            recorded["cmd"] = cmd
            return MagicMock(returncode=0)

        monkeypatch.setattr(sp, "run", fake_run)
        runner._run_lifecycle_script(
            'applescript: tell application "Notes" to count notes',
            label="teardown",
            scenario_id="T2",
        )
        assert recorded["cmd"][0] == "osascript"

    def test_failure_never_raises(self, monkeypatch):
        import subprocess as sp

        from tests.acceptance import runner

        def explode(cmd, **k):
            raise OSError("nope")

        monkeypatch.setattr(sp, "run", explode)
        runner._run_lifecycle_script("shell: whatever", label="setup", scenario_id="T3")
