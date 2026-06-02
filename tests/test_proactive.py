"""Proactive engine tests (Prompt 17): quiet hours, demotion, dispatch,
delivery routing, .env persistence, and the background-task subscriber."""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import settings
from core.proactive import delivery, engine, quiet, settings_writer
from core.proactive.quiet import _parse_windows, adjust_priority, in_quiet_hours
from core.proactive.types import Priority, ProactiveEvent


class TestQuietHours:
    def test_parse_windows(self):
        out = _parse_windows("22:30-07:30, 13:00-14:00")
        assert len(out) == 2
        assert out[0][0] == dt.time(22, 30)

    def test_in_quiet_hours_wraps_midnight(self, monkeypatch):
        monkeypatch.setattr(settings, "PROACTIVE_QUIET_HOURS", "22:30-07:30")
        assert in_quiet_hours(dt.datetime(2026, 6, 1, 23, 0)) is True
        assert in_quiet_hours(dt.datetime(2026, 6, 1, 6, 0)) is True
        assert in_quiet_hours(dt.datetime(2026, 6, 1, 12, 0)) is False

    def test_empty_windows_never_quiet(self, monkeypatch):
        monkeypatch.setattr(settings, "PROACTIVE_QUIET_HOURS", "")
        assert in_quiet_hours(dt.datetime(2026, 6, 1, 3, 0)) is False


class TestDemotion:
    def test_speak_demotes_per_suppressor(self):
        assert adjust_priority(Priority.SPEAK, False, False) == Priority.SPEAK
        assert adjust_priority(Priority.SPEAK, True, False) == Priority.NOTIFY
        assert adjust_priority(Priority.SPEAK, True, True) == Priority.AMBIENT

    def test_urgent_always_bypasses(self):
        assert adjust_priority(Priority.URGENT, True, True) == Priority.URGENT

    def test_never_below_silent(self):
        assert adjust_priority(Priority.AMBIENT, True, True) == Priority.SILENT


class TestEngineDispatch:
    def test_cron_fires_now(self):
        monday_8am = dt.datetime(2026, 6, 1, 8, 0)  # Monday
        assert engine._cron_fires_now("0 8 * * 1-5", monday_8am) is True
        assert engine._cron_fires_now("0 8 * * 1-5", dt.datetime(2026, 6, 1, 8, 1)) is False

    def test_snooze_blocks_tick(self, monkeypatch):
        engine.snooze(30)
        assert engine.is_snoozed() is True
        # Reset so other tests aren't affected.
        monkeypatch.setattr(engine, "_snoozed_until", None)
        assert engine.is_snoozed() is False

    @pytest.mark.asyncio
    async def test_run_one_demotes_then_delivers(self, monkeypatch):
        delivered = {}

        async def fake_deliver(event, effective):
            delivered["event"] = event
            delivered["effective"] = effective

        monkeypatch.setattr(delivery, "deliver", fake_deliver)
        monkeypatch.setattr(settings, "PROACTIVE_QUIET_HOURS", "22:30-07:30")
        monkeypatch.setattr(settings, "PROACTIVE_RESPECT_MEETINGS", False)
        # Make quiet-hours active regardless of wall clock.
        monkeypatch.setattr(quiet, "in_quiet_hours", lambda: True)

        async def job():
            return ProactiveEvent("t", Priority.SPEAK, "hola")

        await engine._run_one("t", job())
        assert delivered["effective"] == Priority.NOTIFY  # demoted from SPEAK


class TestDeliveryRouting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prio, notify, speak",
        [
            (Priority.SILENT, False, False),
            (Priority.AMBIENT, False, False),
            (Priority.NOTIFY, True, False),
            (Priority.SPEAK, True, True),
        ],
    )
    async def test_routes_by_priority(self, monkeypatch, prio, notify, speak):
        notify_mock = MagicMock()  # macos.notify is sync (called via to_thread)
        speak_mock = AsyncMock()
        monkeypatch.setattr(delivery.macos, "notify", notify_mock)
        monkeypatch.setattr(delivery.proactive_voice, "speak_unprompted", speak_mock)

        ev = ProactiveEvent("t", prio, "resumen", detail="detalle")
        await delivery.deliver(ev, prio)

        assert notify_mock.called is notify
        assert speak_mock.called is speak


class TestPersistence:
    @pytest.mark.asyncio
    async def test_enable_proactivity_persists_to_env(self, monkeypatch, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OPENAI_API_KEY=sk-x\n")
        monkeypatch.setattr(settings_writer, "_ENV_PATH", env)
        monkeypatch.setattr(settings, "PROACTIVE_FRIDAY_RECAP", False)

        from tools.proactive_tool import enable_proactivity

        r = await enable_proactivity("friday_recap")
        assert r.success is True
        assert settings.PROACTIVE_FRIDAY_RECAP is True  # live override applied
        assert "PROACTIVE_FRIDAY_RECAP=True" in env.read_text()  # persisted

    @pytest.mark.asyncio
    async def test_unknown_proactivity_rejected(self):
        from tools.proactive_tool import enable_proactivity

        r = await enable_proactivity("does_not_exist")
        assert r.success is False


class TestBackgroundSubscriber:
    @pytest.mark.asyncio
    async def test_emits_on_task_completed(self, monkeypatch):
        from core import events_bus
        from core.proactive import proactivities

        captured = []

        async def fake_deliver(event, effective):
            captured.append(event)

        monkeypatch.setattr(proactivities, "_meeting_fired", set(), raising=False)
        monkeypatch.setattr("core.proactive.delivery.deliver", fake_deliver)
        monkeypatch.setattr(settings, "PROACTIVE_BACKGROUND_TASK_DONE", True)
        monkeypatch.setattr(engine, "_snoozed_until", None)

        sub = asyncio.create_task(proactivities.background_task_subscriber())
        await asyncio.sleep(0.05)  # let it subscribe
        events_bus.publish("task_completed", name="build", elapsed_s=3, status="ok")
        await asyncio.sleep(0.05)  # let it process
        sub.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sub

        assert any(e.source == "background_task_done" for e in captured)
        assert captured[0].priority == Priority.AMBIENT
