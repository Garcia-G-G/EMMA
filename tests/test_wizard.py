"""Prompt 29 — first-run wizard API logic (no browser, subsystems mocked)."""

from __future__ import annotations

import subprocess

import pytest

import installer.firstrun.wizard as wiz


def test_check_permissions_maps_and_survives_errors(monkeypatch) -> None:
    from core import permissions

    monkeypatch.setattr(permissions, "check_microphone", lambda: True)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_calendar", lambda: True)

    def boom() -> bool:
        raise RuntimeError("tcc error")

    monkeypatch.setattr(permissions, "check_automation", boom)
    assert wiz.check_permissions() == {
        "microphone": True, "accessibility": False, "calendar": True, "automation": False,
    }


@pytest.mark.asyncio
async def test_validate_openai_key_empty_is_false() -> None:
    assert await wiz.validate_openai_key("   ") is False


@pytest.mark.asyncio
async def test_save_keys_rejects_invalid_openai(monkeypatch) -> None:
    async def bad(_k):
        return False

    monkeypatch.setattr(wiz, "validate_openai_key", bad)
    res = await wiz.save_keys("sk-bad")
    assert res["ok"] is False and "OpenAI" in res["error"]


@pytest.mark.asyncio
async def test_save_keys_stores_to_keychain_not_env(monkeypatch) -> None:
    from core import secrets

    async def good(_k):
        return True

    stored: list[tuple[str, str, str]] = []

    async def fake_store(label, value, kind="secret"):
        stored.append((label, value, kind))

    monkeypatch.setattr(wiz, "validate_openai_key", good)
    monkeypatch.setattr(secrets, "store", fake_store)
    res = await wiz.save_keys("sk-good", "el-optional")
    assert res["ok"] is True
    labels = {s[0] for s in stored}
    assert labels == {"OPENAI_API_KEY", "ELEVENLABS_API_KEY"}  # both → Keychain
    assert all(s[2] == "secret" for s in stored)


def test_run_service_setup_rejects_unknown_service() -> None:
    res = wiz.run_service_setup("evilcorp")
    assert res["ok"] is False and "desconocido" in res["error"]


def test_run_service_setup_invokes_orchestrator(monkeypatch) -> None:
    class _Proc:
        returncode = 0
        stdout = "Linear: autorizado"
        stderr = ""

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = wiz.run_service_setup("linear")
    assert res["ok"] is True and "Linear" in res["output"]
    assert "emma.setup" in captured["cmd"] and "--only" in captured["cmd"] and "linear" in captured["cmd"]


def test_mic_test_never_crashes() -> None:
    res = wiz.mic_test(0.01)
    assert "ok" in res  # structured result even with no mic / sounddevice failure
