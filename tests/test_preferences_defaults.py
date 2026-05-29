"""Tests for installation-aware app preferences / defaults.

Emma must be able to set a preferred app AND ground it in what's actually
installed — never claim/open an app the user doesn't have. detect_preferred
is mocked so the tests don't depend on what's installed on the test machine.
"""

from __future__ import annotations

from unittest.mock import patch

from actions.environment import DetectionResult
from tools.preferences import get_preferred_app, list_apps, set_preferred_app


def _det(app, available, override=False):
    return DetectionResult(
        app_name=app,
        bundle_id=None,
        binary_path=None,
        available_alternatives=available,
        is_user_override=override,
    )


# --- set_preferred_app -----------------------------------------------------


def test_set_preferred_installed_confirms():
    with (
        patch("tools.preferences.set_preference") as sp,
        patch("tools.preferences.detect_preferred", return_value=_det("code", ["cursor", "code"])),
    ):
        r = set_preferred_app("ide", "VS Code")
    assert r.success
    assert r.data["app"] == "code"
    assert r.data["installed"] is True
    sp.assert_called_once_with("ide", "code")


def test_set_preferred_not_installed_warns_but_records():
    with (
        patch("tools.preferences.set_preference") as sp,
        patch("tools.preferences.detect_preferred", return_value=_det("cursor", ["cursor"])),
    ):
        r = set_preferred_app("ide", "zed")
    assert r.success
    assert r.data["installed"] is False
    assert "no la tienes instalada" in r.user_message
    assert "cursor" in r.user_message  # tells the user what they DO have
    sp.assert_called_once_with("ide", "zed")


def test_set_preferred_unknown_app_refused():
    r = set_preferred_app("ide", "Notepad++")
    assert not r.success
    assert "No soporto" in r.user_message


def test_set_preferred_unknown_category_refused():
    r = set_preferred_app("widget", "whatever")
    assert not r.success
    assert "categoría" in r.user_message


# --- list_apps -------------------------------------------------------------


def test_list_apps_single_category():
    with patch("tools.preferences.detect_preferred", return_value=_det("chrome", ["chrome", "safari"])):
        r = list_apps("browser")
    assert r.success
    assert r.data["browser"]["installed"] == ["chrome", "safari"]
    assert r.data["browser"]["default"] == "chrome"
    assert "chrome" in r.user_message


def test_list_apps_all_categories_when_empty():
    with patch("tools.preferences.detect_preferred", return_value=_det("x", ["x"])):
        r = list_apps()
    assert r.success
    assert set(r.data.keys()) == {"ide", "terminal", "music", "browser"}


def test_list_apps_unknown_category_refused():
    r = list_apps("foo")
    assert not r.success


def test_list_apps_reports_nothing_installed():
    with patch("tools.preferences.detect_preferred", return_value=_det(None, [])):
        r = list_apps("ide")
    assert r.success
    assert r.data["ide"]["installed"] == []
    assert "ninguna" in r.user_message


# --- get_preferred_app exposes installed alternatives ----------------------


def test_get_preferred_includes_available():
    with patch(
        "tools.preferences.detect_preferred",
        return_value=_det("code", ["code", "cursor"], override=True),
    ):
        r = get_preferred_app("ide")
    assert r.success
    assert r.data["available"] == ["code", "cursor"]
    assert r.data["is_user_override"] is True
