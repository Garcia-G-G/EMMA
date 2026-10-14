"""Public-copy rules (AGENTS.md) over every tracked file.

The repo is public. The maker's name or handle may appear only inside the
frozen identifiers that predate the rule: the Keychain service and the legacy
launchd label (``com.<name>.emma``). Rule 2 (no country, city, region,
nationality, flag, regional locale or place timezone) is checked by
``scripts/check_public_copy.py`` over the tree and every commit message; the
pre-commit and commit-msg hooks run the same check.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_public_copy

ROOT = Path(__file__).resolve().parents[1]

# Built from pieces so this file doesn't itself match.
_NAME = re.compile("gar" + "cia|gil" + "ber", re.IGNORECASE)
_IDENTIFIER = re.compile(r"com\.gar" + r"cia\.emma", re.IGNORECASE)


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.decode().split("\0") if p]


def _texts() -> list[tuple[str, str]]:
    out = []
    for p in _tracked():
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        out.append((p.relative_to(ROOT).as_posix(), raw.decode("utf-8", "replace")))
    return out


def test_no_maker_name_outside_identifiers() -> None:
    hits = []
    for rel, text in _texts():
        for n, line in enumerate(text.splitlines(), 1):
            if _NAME.search(_IDENTIFIER.sub("", line)):
                hits.append(f"{rel}:{n}")
    assert not hits, hits


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_copy.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_tree_names_no_place() -> None:
    r = _run("--tree")
    assert r.returncode == 0, r.stdout


def test_commit_messages_name_no_place() -> None:
    r = _run("--history")
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize(
    "text",
    [
        "vivo en " + "Monte" + "rrey",
        "una voz " + "mexi" + "cana",
        "a " + "Bri" + "tish accent",
        "locale " + "es" + "-" + "MX",
        "tz " + "Asia" + "/" + "Tok" + "yo",
        "\U0001f1f2\U0001f1fd",
        "the " + "U" + "SA",
    ],
)
def test_checker_catches(text: str) -> None:
    assert check_public_copy.hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "Emma usa tu voz",  # Spanish verb, not the country
        "habla Spanish y English",  # languages are allowed
        "locale es, en; placeholder es-XX",
        "UTC and Etc/GMT+5",
        "Library/Logs/Emma",
    ],
)
def test_checker_spares(text: str) -> None:
    assert not check_public_copy.hits(text)


def test_planning_is_not_tracked() -> None:
    assert not [p for p in _tracked() if p.relative_to(ROOT).parts[0] == "_planning"]
