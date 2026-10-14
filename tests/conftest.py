"""Suite-wide isolation.

BRAIN-1 0a moved the user dictionary to ~/.emma/dictionary.toml. Without this,
every test that reads the dictionary would merge in the developer's real user
file (contacts, personality, pages) — tests that pass or fail by whose machine
they run on. Point the user file at an empty temp path, so reads see the shipped
template exactly as before 0a. Tests that patch ``_DICT_PATH`` themselves win.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _sandbox_user_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    from core import dictionary

    real = dictionary._DICT_PATH
    dictionary._DICT_PATH = tmp_path_factory.mktemp("emma_home") / "dictionary.toml"
    dictionary.reload()
    yield
    dictionary._DICT_PATH = real
    dictionary.reload()



@pytest.fixture(autouse=True)
def _no_live_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """VOICE-1: the cascade is the default backend, and a dev .env can carry a real
    VOICE_ID and ElevenLabs key. A test that reaches cascade.run_session must never
    open a live session (mic + network): it returns at once with no voice. Tests
    that need a voice or key set their own (monkeypatch after this runs)."""
    from config.settings import settings

    monkeypatch.setattr(settings, "VOICE_ID", "")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "")
