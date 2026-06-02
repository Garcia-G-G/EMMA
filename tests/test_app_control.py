"""Phase 19: app resolver + generic app-control (keystroke/menu/confirm)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import apps
from tools import app_control, terminal_actions


class TestResolve:
    def test_dictionary_first(self, monkeypatch):
        monkeypatch.setattr(
            apps.dictionary, "app_for", lambda c: "MyEditor" if c == "editor" else ""
        )
        assert apps.resolve("editor") == "MyEditor"

    def test_detect_preferred_fallback_maps_key_to_display(self, monkeypatch):
        monkeypatch.setattr(apps.dictionary, "app_for", lambda c: "")
        monkeypatch.setattr(
            apps.environment,
            "detect_preferred",
            lambda cat, **k: SimpleNamespace(app_name="cursor"),
        )
        assert apps.resolve("editor") == "Cursor"  # key 'cursor' -> display 'Cursor'

    def test_none_when_unresolved(self, monkeypatch):
        monkeypatch.setattr(apps.dictionary, "app_for", lambda c: "")
        monkeypatch.setattr(
            apps.environment, "detect_preferred", lambda cat, **k: SimpleNamespace(app_name=None)
        )
        assert apps.resolve("editor") is None

    def test_unknown_category(self):
        assert apps.resolve("toaster") is None


class TestKeystrokeParse:
    def test_cmd_shift_p(self):
        assert (
            app_control._keystroke_action("Cmd+Shift+P")
            == 'keystroke "p" using {command down, shift down}'
        )

    def test_cmd_t(self):
        assert app_control._keystroke_action("Cmd+T") == 'keystroke "t" using {command down}'

    def test_escape_uses_key_code(self):
        assert app_control._keystroke_action("Escape") == "key code 53"

    def test_return_uses_keyword(self):
        assert app_control._keystroke_action("Return") == "keystroke return"

    def test_cmd_up_uses_key_code(self):
        assert app_control._keystroke_action("Cmd+Up") == "key code 126 using {command down}"

    def test_unknown_key_returns_none(self):
        assert app_control._keystroke_action("Cmd+Frobnicate") is None


class TestMenuRef:
    def test_two_level(self):
        assert app_control._menu_ref(["File", "New Window"]) == (
            'menu item "New Window" of menu "File" of menu bar item "File" of menu bar 1'
        )

    def test_three_level(self):
        assert app_control._menu_ref(["File", "New", "Window"]) == (
            'menu item "Window" of menu "New" of menu item "New" '
            'of menu "File" of menu bar item "File" of menu bar 1'
        )


class TestConfirmation:
    @pytest.mark.asyncio
    async def test_run_in_terminal_confirms_first(self):
        r = await terminal_actions.run_in_terminal("ls -la")
        assert r.requires_confirmation is True

    @pytest.mark.asyncio
    async def test_app_keystroke_confirms_then_sends_correct_script(self, monkeypatch):
        r1 = await app_control.app_keystroke("Cursor", "Cmd+T")
        assert r1.requires_confirmation is True

        captured = {}

        async def _fake_osa(script, **kwargs):
            captured["script"] = script
            return (True, "")

        monkeypatch.setattr(app_control.macos, "osascript_or_friendly", _fake_osa)
        r2 = await app_control.app_keystroke("Cursor", "Cmd+T", confirmed=True)
        assert r2.success is True
        assert 'keystroke "t" using {command down}' in captured["script"]

    @pytest.mark.asyncio
    async def test_app_keystroke_unknown_combo_rejected(self):
        r = await app_control.app_keystroke("Cursor", "Cmd+Frobnicate", confirmed=True)
        assert r.success is False
