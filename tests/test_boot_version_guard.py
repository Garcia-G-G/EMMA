"""LAUNCH-4 — the version stamp and the boot-failure guard.

Between them these answer the two questions that cost this project two
investigations: "which code is actually running?" and "why is it restarting
forever?".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core import boot_guard, version


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never let a test write the real ~/.emma."""
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))


# --- version ---------------------------------------------------------------


def test_fingerprint_is_stable_and_short() -> None:
    a, b = version.fingerprint(), version.fingerprint()
    assert a == b and len(a) == 12


def test_fingerprint_survives_a_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial install must still produce a comparable value, not an exception."""
    monkeypatch.setattr(version, "_FINGERPRINT_FILES", ("does/not/exist.py",))
    assert len(version.fingerprint()) == 12


def test_report_reads_the_installer_stamp(tmp_path) -> None:
    (tmp_path / "install.json").write_text(json.dumps({
        "ref": "v1.2.3", "commit": "abc123def456",
        "channel": "https://github.com/x/y", "installed_at": "2026-08-07T00:00:00Z",
    }))
    r = version.report()
    assert r["commit"] == "abc123def456"
    assert r["ref"] == "v1.2.3"
    assert r["installed_at"] == "2026-08-07T00:00:00Z"


def test_report_survives_a_corrupt_stamp(tmp_path) -> None:
    (tmp_path / "install.json").write_text("{not json")
    assert version.report()["fingerprint"]  # no raise


@pytest.mark.asyncio
async def test_staleness_says_unknown_rather_than_fine(monkeypatch) -> None:
    """The whole point: never report "up to date" when we cannot tell."""
    async def _none(_t: float = 0) -> None:
        return None

    monkeypatch.setattr(version, "upstream_head", _none)
    assert (await version.staleness())["stale"] is None


@pytest.mark.asyncio
async def test_staleness_detects_a_stale_install(tmp_path, monkeypatch) -> None:
    (tmp_path / "install.json").write_text(json.dumps({"commit": "0000000aaaa"}))

    async def _head(_t: float = 0) -> str:
        return "ffffffbbbb"

    monkeypatch.setattr(version, "upstream_head", _head)
    assert (await version.staleness())["stale"] is True


@pytest.mark.asyncio
async def test_staleness_accepts_a_matching_install(tmp_path, monkeypatch) -> None:
    (tmp_path / "install.json").write_text(json.dumps({"commit": "abcdef123456"}))

    async def _head(_t: float = 0) -> str:
        return "abcdef123456"

    monkeypatch.setattr(version, "upstream_head", _head)
    assert (await version.staleness())["stale"] is False


def test_boot_line_is_small_and_needs_no_network() -> None:
    line = version.boot_line()
    assert "fingerprint" in line
    assert "upstream" not in line and "stale" not in line


# --- boot_guard ------------------------------------------------------------


def test_failures_accumulate_then_stay_down() -> None:
    for _ in range(3):
        boot_guard.record_failure("k")
        assert boot_guard.should_stay_down("k") is False, "gave up too early"
    boot_guard.record_failure("k")
    assert boot_guard.should_stay_down("k") is True


def test_a_good_boot_clears_the_streak() -> None:
    for _ in range(4):
        boot_guard.record_failure("k")
    assert boot_guard.should_stay_down("k") is True
    boot_guard.clear("k")
    assert boot_guard.should_stay_down("k") is False


def test_streaks_are_per_kind() -> None:
    for _ in range(4):
        boot_guard.record_failure("credentials")
    assert boot_guard.should_stay_down("credentials") is True
    assert boot_guard.should_stay_down("permissions") is False


def test_clear_all_forgets_streaks_but_keeps_speak_markers() -> None:
    # A good boot calls clear() (kind=None): it must forget the real failure kinds
    # (credentials/permissions/wake_model — none of which end in "_boot", the bug
    # the old filter had) yet keep the "said:" speak-rate markers so a
    # declined-permission notice doesn't start repeating after every good boot.
    for _ in range(4):
        boot_guard.record_failure("credentials")
        boot_guard.record_failure("wake_model")
    assert boot_guard.should_speak("permissions", interval_s=9_999) is True  # records said:permissions

    boot_guard.clear()

    assert boot_guard.should_stay_down("credentials") is False  # streak forgotten
    assert boot_guard.should_stay_down("wake_model") is False
    # still rate-limited → the marker survived the clear
    assert boot_guard.should_speak("permissions", interval_s=9_999) is False


def test_an_old_failure_is_not_part_of_the_streak(monkeypatch) -> None:
    """A user who fixed something and rebooted hours later starts fresh."""
    boot_guard.record_failure("k")
    monkeypatch.setattr(boot_guard, "_STREAK_WINDOW_S", -1.0)
    assert boot_guard.record_failure("k") == 1


def test_should_speak_is_once_per_window() -> None:
    assert boot_guard.should_speak("notice", interval_s=10_000) is True
    for _ in range(5):
        assert boot_guard.should_speak("notice", interval_s=10_000) is False


def test_should_speak_recovers_after_the_window() -> None:
    assert boot_guard.should_speak("notice", interval_s=10_000) is True
    assert boot_guard.should_speak("notice", interval_s=-1.0) is True


def test_unwritable_state_never_breaks_boot(monkeypatch) -> None:
    """Bookkeeping must never be the reason a daemon fails to start."""
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("read-only")

    monkeypatch.setattr(boot_guard.Path, "write_text", _boom)
    boot_guard.record_failure("k")           # no raise
    assert boot_guard.should_speak("k") is True  # fails open: speak rather than go mute
