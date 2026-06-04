"""Tests for the capability-gap ledger (core/capability_gaps.py)."""

from __future__ import annotations

import json

import pytest

from core import capability_gaps


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Point the ledger at a temp file for each test."""
    path = tmp_path / "capability_gaps.jsonl"
    monkeypatch.setattr(capability_gaps, "_LEDGER", path)
    return path


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


class TestIsGap:
    def test_failure_is_always_a_gap(self):
        assert capability_gaps.is_gap(False, "lo que sea") is True

    def test_plain_success_is_not_a_gap(self):
        assert capability_gaps.is_gap(True, "Reproduciendo Bad Bunny.") is False

    def test_success_with_obstacle_phrase_is_a_gap(self):
        # Apple Music / Spotify "open it first" style message.
        assert capability_gaps.is_gap(True, "Dile al usuario que abra Spotify.") is True

    def test_none_message_on_success_is_not_a_gap(self):
        assert capability_gaps.is_gap(True, None) is False


class TestRecord:
    def test_plain_success_writes_nothing(self, _ledger):
        capability_gaps.record(
            name="now_playing",
            args_keys=[],
            success=True,
            user_message="Suena Bad Bunny.",
            elapsed_ms=12,
        )
        assert _lines(_ledger) == []

    def test_echo_tool_success_not_flagged(self, _ledger):
        # list_notes echoes note titles that may contain obstacle words; a
        # SUCCESS from an echo tool must never be heuristically recorded.
        capability_gaps.record(
            name="list_notes",
            args_keys=["query"],
            success=True,
            user_message="Tus notas: 'No pude completar X'; 'Errores de Emma'.",
            elapsed_ms=20,
        )
        capability_gaps.record(
            name="remember_fact",
            args_keys=["text"],
            success=True,
            user_message="Anotado: abre la app cuando diga 'anota'.",
            elapsed_ms=5,
        )
        assert _lines(_ledger) == []

    def test_echo_tool_real_failure_still_recorded(self, _ledger):
        capability_gaps.record(
            name="search_github",
            args_keys=["query"],
            success=False,
            user_message="No pude buscar en GitHub: rate limit.",
            elapsed_ms=400,
        )
        assert len(_lines(_ledger)) == 1

    def test_no_device_failure_is_recorded_and_classified(self, _ledger):
        capability_gaps.record(
            name="play_track",
            args_keys=["query"],
            success=False,
            user_message="Spotify no tiene ningún dispositivo activo. Abra Spotify.",
            elapsed_ms=340,
        )
        rows = _lines(_ledger)
        assert len(rows) == 1
        assert rows[0]["tool"] == "play_track"
        assert rows[0]["category"] == "no_active_device"
        assert rows[0]["args_keys"] == ["query"]

    def test_ide_not_configured_is_recorded(self, _ledger):
        capability_gaps.record(
            name="open_in_ide",
            args_keys=["path"],
            success=False,
            user_message="No tengo un IDE configurado.",
            elapsed_ms=5,
        )
        rows = _lines(_ledger)
        assert rows[0]["category"] == "not_installed_or_configured"

    def test_timeout_recorded_even_on_neutral_message(self, _ledger):
        capability_gaps.record(
            name="clone_and_open",
            args_keys=["repo_url", "ide"],
            success=False,
            user_message="…",
            elapsed_ms=20000,
            timed_out=True,
        )
        rows = _lines(_ledger)
        assert rows[0]["category"] == "timeout"

    def test_message_is_redacted(self, _ledger):
        capability_gaps.record(
            name="some_tool",
            args_keys=[],
            success=False,
            user_message="falló con token sk-ABCD1234ABCD1234ABCD1234ABCD1234",
            elapsed_ms=1,
        )
        rows = _lines(_ledger)
        assert "sk-ABCD1234" not in rows[0]["message"]

    def test_raw_data_values_never_written_only_keys(self, _ledger):
        capability_gaps.record(
            name="play_track",
            args_keys=["query"],
            success=False,
            user_message="No encontré nada.",
            data={"uri": "spotify:track:secret", "title": "x"},
            elapsed_ms=10,
        )
        rows = _lines(_ledger)
        assert sorted(rows[0]["data_keys"]) == ["title", "uri"]
        assert "spotify:track:secret" not in json.dumps(rows[0])

    def test_record_never_raises(self, _ledger, monkeypatch):
        # Even if writing blows up, the dispatch path must survive.
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(capability_gaps.Path, "mkdir", _boom)
        # Should not raise.
        capability_gaps.record(
            name="x",
            args_keys=[],
            success=False,
            user_message="boom",
            elapsed_ms=1,
        )
