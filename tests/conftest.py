"""Suite-wide isolation.

BRAIN-1 0a moved the user dictionary to ~/.emma/dictionary.toml. Without this,
every test that reads the dictionary would merge in the developer's real user
file (contacts, personality, pages) — tests that pass or fail by whose machine
they run on. Point the user file at an empty temp path, so reads see the shipped
template exactly as before 0a. Tests that patch ``_DICT_PATH`` themselves win.
"""

from __future__ import annotations

# ---- the real home is off limits (Step 42 Part 0) -------------------------------------
# Runs at conftest import, before any test module imports config/ or core/: every
# ``Path.home()`` in the codebase (settings paths, ~/.emma/*, ~/Library/Logs/Emma)
# resolves into a throwaway home. The suite once wrote 575 rows into the owner's real
# ~/.emma/memory.db. Read-only model data the brain tests need is linked in; an audit
# hook refuses any write, delete, rename or SQLite connection that resolves into the
# real home outside this repo, and the session fails if one was attempted.
import contextlib
import os
import pwd
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
REPO = Path(__file__).resolve().parent.parent
TEST_HOME = Path(tempfile.mkdtemp(prefix="emma-test-home-")).resolve()
for _ro in (".emma/models", ".emma/sherpa-kws", "Library/Caches/ms-playwright"):  # read-only data
    if (REAL_HOME / _ro).exists():
        (TEST_HOME / _ro).parent.mkdir(parents=True, exist_ok=True)
        (TEST_HOME / _ro).symlink_to(REAL_HOME / _ro)
# The Keychain is part of the real home too: secrets tests store and delete items
# (one of them OPENAI_API_KEY under the real service). They get a throwaway keychain.
os.environ["HOME"] = str(TEST_HOME)  # before `security` runs: its preferences land here too
os.environ["EMMA_KEYCHAIN_SERVICE"] = "com.emma.test"
_KEYCHAIN = TEST_HOME / "test.keychain-db"
if Path("/usr/bin/security").exists():
    import subprocess

    for _cmd in ("create-keychain", "unlock-keychain"):
        subprocess.run(["/usr/bin/security", _cmd, "-p", "", str(_KEYCHAIN)],
                       check=False, capture_output=True, timeout=10)
    os.environ["EMMA_KEYCHAIN_PATH"] = str(_KEYCHAIN)
os.environ.pop("EMMA_HOME", None)
os.environ.pop("MEMORY_DB_PATH", None)

HOME_VIOLATIONS: list[str] = []
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _in_real_home(path: object) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)) or isinstance(path, int):
        return False
    raw = os.fsdecode(path)
    if raw.startswith("file:"):  # sqlite URI
        raw = raw[5:].split("?", 1)[0]
    if not raw or raw == ":memory:":
        return False
    p = Path(os.path.abspath(os.path.expanduser(raw)))
    with contextlib.suppress(OSError):
        p = p.resolve()  # follows the read-only links back to the real home
    inside = p == REAL_HOME or REAL_HOME in p.parents
    return inside and not (p == REPO or REPO in p.parents) and TEST_HOME not in p.parents


def _guard(event: str, args: tuple[object, ...]) -> None:
    target: object = None
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS)
        target = path if writes else None
    elif event == "sqlite3.connect":
        target = args[0]
    elif event in ("os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.truncate",
                   "os.chmod", "os.symlink", "os.link", "shutil.rmtree", "shutil.move",
                   "shutil.copyfile"):
        target = args[1] if event in ("os.symlink", "os.link", "shutil.move", "shutil.copyfile") \
            and len(args) > 1 else args[0]
    if target is not None and _in_real_home(target):
        HOME_VIOLATIONS.append(f"{event}: {os.fsdecode(target)}")  # type: ignore[arg-type]
        raise PermissionError(f"test tried to touch the real home: {event} {target!r}")


sys.addaudithook(_guard)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if HOME_VIOLATIONS:
        print("\nREAL HOME TOUCHED BY TESTS:\n  " + "\n  ".join(sorted(set(HOME_VIOLATIONS))))
        session.exitstatus = 1



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
