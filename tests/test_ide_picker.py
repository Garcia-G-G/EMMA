"""23.1-B41: first-time IDE pick + single-store preference write.

Covers app_router.preferred_or_ask (when to ask vs. pick silently),
dictionary.set_app_preference (writes the [apps.*] block the router reads
first), and the remember_app_preference tool (loose name → canonical display).
"""

from __future__ import annotations

import shutil

import pytest

from core import app_router, dictionary
from core.app_router import RouteDecision


@pytest.fixture
def temp_dict(monkeypatch, tmp_path):
    """A throwaway copy of dictionary.toml so writes never touch the repo file.

    Anchors to the REAL repo path (not the live ``_DICT_PATH``, which a sibling
    test may have left pointing at its own temp) and restores it on teardown.
    """
    from pathlib import Path

    real = Path(dictionary.__file__).resolve().parent.parent / "config" / "dictionary.toml"
    p = tmp_path / "dictionary.toml"
    shutil.copy(real, p)
    monkeypatch.setattr(dictionary, "_DICT_PATH", p)
    dictionary.reload()
    yield p
    dictionary._DICT_PATH = real
    dictionary.reload()


class TestPreferredOrAsk:
    def test_asks_when_no_pref_and_multiple_installed(self, monkeypatch):
        monkeypatch.setattr(
            app_router, "inspect", lambda _c: RouteDecision(picked="Cursor", source="fallback")
        )
        monkeypatch.setattr(app_router.dictionary, "app_for", lambda _c: "")
        monkeypatch.setattr(
            app_router,
            "_installed_editor_displays",
            lambda: ["Cursor", "Visual Studio Code", "Zed"],
        )
        picked, candidates = app_router.preferred_or_ask("editor")
        assert picked is None
        assert candidates == ["Cursor", "Visual Studio Code", "Zed"]

    def test_single_install_is_not_ambiguous(self, monkeypatch):
        monkeypatch.setattr(
            app_router, "inspect", lambda _c: RouteDecision(picked="Zed", source="fallback")
        )
        monkeypatch.setattr(app_router.dictionary, "app_for", lambda _c: "")
        monkeypatch.setattr(app_router, "_installed_editor_displays", lambda: ["Zed"])
        assert app_router.preferred_or_ask("editor") == ("Zed", [])

    def test_explicit_preference_never_asks(self, monkeypatch):
        monkeypatch.setattr(
            app_router, "inspect", lambda _c: RouteDecision(picked="Cursor", source="preferred")
        )
        monkeypatch.setattr(app_router.dictionary, "app_for", lambda c: "Cursor" if c else "")
        monkeypatch.setattr(app_router, "_installed_editor_displays", lambda: ["Cursor", "Zed"])
        assert app_router.preferred_or_ask("editor") == ("Cursor", [])

    def test_frontmost_wins_over_ask(self, monkeypatch):
        monkeypatch.setattr(
            app_router, "inspect", lambda _c: RouteDecision(picked="Zed", source="frontmost")
        )
        monkeypatch.setattr(app_router.dictionary, "app_for", lambda _c: "")
        assert app_router.preferred_or_ask("editor") == ("Zed", [])

    def test_non_editor_category_never_asks(self, monkeypatch):
        monkeypatch.setattr(
            app_router, "inspect", lambda _c: RouteDecision(picked="Safari", source="fallback")
        )
        monkeypatch.setattr(app_router.dictionary, "app_for", lambda _c: "")
        assert app_router.preferred_or_ask("browser") == ("Safari", [])


class TestSetAppPreference:
    def test_round_trips_to_apps_block(self, temp_dict):
        assert dictionary.app_for("editor") == "Cursor"  # seed from the copy
        assert dictionary.set_app_preference("editor", "Visual Studio Code") is True
        assert dictionary.app_for("editor") == "Visual Studio Code"
        # Sibling categories are untouched.
        assert dictionary.app_for("browser") == "Brave Browser"

    def test_empty_input_refused(self, temp_dict):
        assert dictionary.set_app_preference("editor", "") is False
        assert dictionary.set_app_preference("", "Zed") is False

    def test_new_category_is_appended(self, temp_dict):
        assert dictionary.set_app_preference("notes", "Obsidian") is True
        assert dictionary.app_for("notes") == "Obsidian"


class TestRememberAppPreferenceTool:
    @pytest.mark.asyncio
    async def test_maps_loose_name_to_display(self, temp_dict):
        from tools.dictionary_tool import remember_app_preference

        r = await remember_app_preference("editor", "vscode")
        assert r.success
        assert dictionary.app_for("editor") == "Visual Studio Code"

    @pytest.mark.asyncio
    async def test_ide_alias_accepted(self, temp_dict):
        from tools.dictionary_tool import remember_app_preference

        r = await remember_app_preference("ide", "Zed")
        assert r.success
        assert dictionary.app_for("editor") == "Zed"

    @pytest.mark.asyncio
    async def test_unsupported_app_refused(self, temp_dict):
        from tools.dictionary_tool import remember_app_preference

        r = await remember_app_preference("editor", "Notepad++")
        assert r.success is False
        assert dictionary.app_for("editor") == "Cursor"  # unchanged

    @pytest.mark.asyncio
    async def test_unknown_category_refused(self, temp_dict):
        from tools.dictionary_tool import remember_app_preference

        r = await remember_app_preference("spaceship", "Cursor")
        assert r.success is False
