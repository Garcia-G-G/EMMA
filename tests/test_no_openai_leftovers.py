"""Step 40 Part 3: on the cascade, no OpenAI leftover can act or block.

The credential half (a leftover OPENAI_API_KEY hiding other Keychain items)
lives in test_byok_keychain_boot.py; the tier half in test_mode_resolution.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cascade(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-leftover-" + "x" * 30)


@pytest.mark.asyncio
async def test_proactive_speech_uses_core_voice_never_a_realtime_session(cascade, monkeypatch):
    from core import voice
    from core.proactive import voice as proactive_voice

    spoken: list[tuple[str, bool]] = []

    async def fake_speak(text: str, *, keep: bool = True) -> bool:
        spoken.append((text, keep))
        return True

    monkeypatch.setattr(voice, "speak", fake_speak)
    # Any attempt to build a Realtime pipeline fails the test.
    monkeypatch.setitem(sys.modules, "core.conversation", SimpleNamespace(
        build_pipeline=lambda: pytest.fail("opened a Realtime session on the cascade")))

    await proactive_voice.speak_unprompted("Tu recordatorio: llamar a mamá.")
    assert spoken == [("Tu recordatorio: llamar a mamá.", False)]  # one-off: not cached


@pytest.mark.asyncio
async def test_proactive_speech_failure_is_swallowed(cascade, monkeypatch):
    from core import voice
    from core.proactive import voice as proactive_voice

    async def boom(text: str, *, keep: bool = True) -> bool:
        raise RuntimeError("no network")

    monkeypatch.setattr(voice, "speak", boom)
    await proactive_voice.speak_unprompted("hola")  # no raise


def test_memory_boot_backfill_never_calls_openai_on_the_cascade(cascade, monkeypatch, tmp_path):
    from memory import long_term

    monkeypatch.setattr(long_term, "_connect", lambda: _NullCtx())

    async def forbidden(*a, **k):
        pytest.fail("memory backfill ran on the cascade (it embeds through OpenAI)")

    monkeypatch.setattr(long_term, "backfill_embeddings", forbidden)
    long_term.initialize()


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("argv", [["--first-run"], ["--pair"], ["--first-run", "--pair"]])
def test_pairing_and_wizard_flags_say_not_used_and_exit_zero(cascade, monkeypatch, capsys, argv):
    import emma.__main__ as m

    monkeypatch.setattr(sys, "argv", ["emma", *argv])
    monkeypatch.setattr(m, "_run_pairing", lambda log: pytest.fail("pairing ran"))
    assert m.main() == 0
    assert "not used on this version" in capsys.readouterr().out


def test_openai_wizard_does_not_serve_on_the_cascade(cascade, capsys):
    from installer.firstrun import wizard

    wizard.run(open_browser=False)  # would block forever if it served
    assert "not used on this version" in capsys.readouterr().out


def test_menubar_has_no_openai_item_or_minutes_state() -> None:
    src = (ROOT / "emma" / "ui" / "__main__.py").read_text(encoding="utf-8")
    assert "out_of_minutes" not in src and "Sin minutos" not in src
    # The OpenAI item only exists behind the Realtime backend.
    i = src.index('"Mi API key de OpenAI…"')
    assert 'EMMA_VOICE_BACKEND") == "realtime"' in src[i - 200:i]


def test_status_line_names_a_newer_release() -> None:
    from emma.ui.__main__ import status_line

    assert status_line("idle") == "Emma · En espera"
    assert status_line("idle", "v0.5.1") == "Emma · En espera · v0.5.1 disponible"
