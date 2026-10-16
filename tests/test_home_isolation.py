"""Step 42 Part 0 — no test reads or writes the real ~/.emma (or anything in the real home)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import HOME_VIOLATIONS, REAL_HOME, TEST_HOME


@pytest.fixture
def probe() -> object:
    """These tests trip the guard on purpose; their refusals don't fail the session."""
    n = len(HOME_VIOLATIONS)
    yield
    assert len(HOME_VIOLATIONS) > n, "the guard did not see the probe"
    del HOME_VIOLATIONS[n:]


def test_home_and_every_emma_path_point_at_the_test_home() -> None:
    from config.settings import settings

    assert Path.home().resolve() == TEST_HOME
    for p in (settings.EMMA_HOME, settings.MEMORY_DB_PATH):
        assert TEST_HOME in Path(p).resolve().parents, p


def test_writing_into_the_real_home_is_refused(probe: object) -> None:
    target = REAL_HOME / ".emma" / "isolation-probe.txt"
    with pytest.raises(PermissionError):
        target.write_text("no")
    assert not target.exists()


def test_a_sqlite_connection_to_the_real_memory_db_is_refused(probe: object) -> None:
    with pytest.raises(PermissionError):
        sqlite3.connect(str(REAL_HOME / ".emma" / "memory.db"))


def test_writing_through_the_model_link_is_refused(probe: object) -> None:
    link = TEST_HOME / ".emma" / "models"
    if not link.exists():
        HOME_VIOLATIONS.append("skip")  # keep the probe fixture's bookkeeping honest
        pytest.skip("no local models to link")
    with pytest.raises(PermissionError):
        (link / "isolation-probe.txt").write_text("no")


def test_the_test_home_itself_is_writable(tmp_path: Path) -> None:
    (TEST_HOME / ".emma").mkdir(parents=True, exist_ok=True)
    (TEST_HOME / ".emma" / "probe.txt").write_text("ok")


def test_secrets_use_a_throwaway_keychain_and_service() -> None:
    import os

    from core import secrets

    assert secrets.SERVICE == "com.emma.test"
    kc = os.environ.get("EMMA_KEYCHAIN_PATH")
    if kc is not None:
        assert TEST_HOME in Path(kc).parents
