"""VOICE-1 Part 5 — onboarding asks for the ElevenLabs key and a voice; the backend never sees a key."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from config import settings as settings_mod
from config.settings import settings
from core import readiness, voice_keys

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk_" + "a1B2c3D4" * 6


@pytest.mark.asyncio
async def test_validation_goes_straight_to_elevenlabs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_get(url: str, headers: dict[str, str], timeout_s: float) -> int:
        seen.append(url)
        return 200

    monkeypatch.setattr(voice_keys, "_get", fake_get)
    assert (await voice_keys.validate_elevenlabs(KEY))[0]
    assert (await voice_keys.validate_openrouter(KEY))[0]
    assert seen == ["https://api.elevenlabs.io/v1/user", "https://openrouter.ai/api/v1/key"]


@pytest.mark.asyncio
async def test_rejected_and_offline_say_different_things(monkeypatch: pytest.MonkeyPatch) -> None:
    async def status(code: int | None) -> Any:
        async def fake(url: str, headers: dict[str, str], timeout_s: float) -> int | None:
            return code
        return fake

    monkeypatch.setattr(voice_keys, "_get", await status(401))
    ok, msg = await voice_keys.validate_elevenlabs(KEY)
    assert not ok and "rechazó" in msg
    monkeypatch.setattr(voice_keys, "_get", await status(None))
    ok, msg = await voice_keys.validate_elevenlabs(KEY)
    assert not ok and "conectarme" in msg
    assert not (await voice_keys.validate_elevenlabs("short"))[0]


@pytest.mark.asyncio
async def test_keys_go_to_keychain_and_only_known_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import secrets

    stored: list[tuple[str, str, str]] = []

    async def fake_store(label: str, value: str, kind: str = "") -> None:
        stored.append((label, value, kind))

    monkeypatch.setattr(secrets, "store", fake_store)
    await voice_keys.store(voice_keys.ELEVENLABS, KEY)
    assert stored == [("ELEVENLABS_API_KEY", KEY, "secret")]
    with pytest.raises(ValueError):
        await voice_keys.store("OPENAI_API_KEY", KEY)


def test_masked_hint_never_reveals_the_key() -> None:
    hint = voice_keys.masked(KEY)
    assert hint.startswith("sk_") and hint.endswith(KEY[-4:]) and len(hint) < 12


def test_the_voice_persists_outside_the_source_tree(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "settings.env"
    f.write_text("OTHER=1\nVOICE_ID=old\n")
    monkeypatch.setattr(settings, "VOICE_ID", "")
    voice_keys.save_voice("SAz9YHcvj6GT2YYXdXww", f)
    assert f.read_text() == "OTHER=1\nVOICE_ID=SAz9YHcvj6GT2YYXdXww\n"
    assert oct(os.stat(f).st_mode)[-3:] == "600"
    assert voice_keys.saved_voice(f) == "SAz9YHcvj6GT2YYXdXww"
    assert settings.VOICE_ID == "SAz9YHcvj6GT2YYXdXww"
    for bad in ("", "a b", "x=y", 'q"'):
        with pytest.raises(ValueError):
            voice_keys.save_voice(bad, f)


def test_settings_read_the_per_user_file_after_dotenv() -> None:
    files = settings_mod.Settings.model_config["env_file"]
    assert files == (".env", str(Path.home() / ".emma" / "settings.env"))


@pytest.mark.asyncio
async def test_a_parked_daemon_picks_up_the_chosen_voice(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "settings.env"
    f.write_text("VOICE_ID=v123\n")
    monkeypatch.setattr(voice_keys, "USER_SETTINGS", f)
    monkeypatch.setattr(settings, "VOICE_ID", "")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", KEY)
    await readiness.refresh()
    assert settings.VOICE_ID == "v123" and readiness.has_voice()


@pytest.mark.asyncio
async def test_voice_status_reports_masked_hints_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import secrets
    from dashboard import server

    async def fake_retrieve(label: str) -> str | None:
        return KEY if label == "ELEVENLABS_API_KEY" else None

    monkeypatch.setattr(secrets, "retrieve", fake_retrieve)
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    r = await server.dispatch_control({"cmd": "voice_status"})
    assert r["ok"] and r["backend"] == "cascade"
    assert r["elevenlabs_hint"] == voice_keys.masked(KEY) and r["openrouter_hint"] is None
    assert KEY not in repr(r)


@pytest.mark.asyncio
async def test_voice_changed_renders_the_clips_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import voice
    from dashboard import server

    ran = threading.Event()
    monkeypatch.setattr(voice, "pregenerate", lambda: ran.set())
    r = await server.dispatch_control({"cmd": "voice_changed"})
    assert r["ok"] and ran.wait(2)


def test_the_onboarding_page_has_no_key_field() -> None:
    """Keys go through native secure fields (emma/ui), never this page."""
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    pane = html[html.index('<div id="ob-voice"'):html.index('<div id="ob-license"')]
    assert "<input" not in pane
    ui = (ROOT / "emma" / "ui" / "__main__.py").read_text(encoding="utf-8")
    for action in ("enter_voice_key", "choose_voice"):
        assert f'"{action}"' in html
        assert action in ui
    # Step 40: OpenRouter has no use yet, so onboarding doesn't ask for it; the
    # native path stays behind OPENROUTER_MENU for BRAIN-2.
    assert "enter_openrouter_key" not in html and "enter_openrouter_key" in ui


def test_the_backend_never_handles_a_cascade_key() -> None:
    hits = [
        str(p.relative_to(ROOT)) for p in (ROOT / "backend").rglob("*.py")
        # Key names only: backend/wake_* (a dev studio) synthesizes with voice
        # ids on its own side; no backend code reads, receives or stores a key.
        if re.search(r"ELEVENLABS_API_KEY|OPENROUTER_API_KEY", p.read_text(encoding="utf-8"))
    ]
    assert hits == []
