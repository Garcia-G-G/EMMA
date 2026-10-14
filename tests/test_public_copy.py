"""Public-copy rules (AGENTS.md) over every tracked file.

The repo is public. The maker's name or handle may appear only inside the
frozen identifiers that predate the rule: the Keychain service and the legacy
launchd label (``com.<name>.emma``). No country flags; no hard-coded regional
locale outside the places that genuinely need a language code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Built from pieces so this file doesn't itself match.
_NAME = re.compile("gar" + "cia|gil" + "ber", re.IGNORECASE)
_IDENTIFIER = re.compile(r"com\.gar" + r"cia\.emma", re.IGNORECASE)
_FLAG = re.compile("[\U0001f1e6-\U0001f1ff]{2}")
_LOCALE = re.compile(r"\b(?:es|en)[-_](?:MX|ES|US|GB|AR|CO|CL|PE|419)\b")

# Language codes an OS API needs, not a user-facing locale assumption.
_LOCALE_ALLOWED = {
    "core/visual_screen.py",  # Vision OCR recognition languages
    "scripts/wake_word_data.py",  # Piper voice model names (several accents on purpose)
}


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


def test_no_country_flags() -> None:
    hits = [rel for rel, text in _texts() if _FLAG.search(text)]
    assert not hits, hits


def test_no_regional_locale_literals() -> None:
    hits = [
        f"{rel}:{n}"
        for rel, text in _texts()
        if rel not in _LOCALE_ALLOWED and rel != "tests/test_public_copy.py"
        for n, line in enumerate(text.splitlines(), 1)
        if _LOCALE.search(line)
    ]
    assert not hits, hits


def test_planning_is_not_tracked() -> None:
    assert not [p for p in _tracked() if p.relative_to(ROOT).parts[0] == "_planning"]
