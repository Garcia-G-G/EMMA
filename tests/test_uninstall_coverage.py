"""Step 40 Part 2: the uninstaller clears every Keychain label Emma writes and
every TCC row it can hold.

``uninstall.sh`` lives in the separate ``_landing`` repo, so this skips in a
checkout without it (a fresh public clone).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config.settings import _CREDENTIAL_FIELDS

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "_landing" / "uninstall.sh"
pytestmark = pytest.mark.skipif(not UNINSTALL.is_file(), reason="_landing repo not present")

_STORE_LITERAL = re.compile(r"\.store\(\s*\"([^\"]+)\"")
_LABEL_CONST = re.compile(r"^_(?:INSTALL|TOKEN)_LABEL\s*=\s*\"([^\"]+)\"", re.MULTILINE)


def _labels_in_code() -> set[str]:
    labels = set(_CREDENTIAL_FIELDS)
    for d in ("core", "emma", "tools", "memory", "installer"):
        for py in (ROOT / d).rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            labels.update(_STORE_LITERAL.findall(text))
            labels.update(_LABEL_CONST.findall(text))
    return labels


def _uninstall_var(name: str) -> set[str]:
    m = re.search(rf'^{name}="([^"]*)"', UNINSTALL.read_text(), re.MULTILINE)
    assert m, f"{name} missing from uninstall.sh"
    return set(m.group(1).split())


def test_every_fixed_keychain_label_is_deleted() -> None:
    code = _labels_in_code()
    assert {"ELEVENLABS_API_KEY", "OPENROUTER_API_KEY", "install_id", "device_token"} <= code
    missing = code - _uninstall_var("KC_LABELS")
    assert not missing, f"uninstall.sh does not delete: {sorted(missing)}"


def test_user_named_labels_are_swept() -> None:
    """secrets_tool vault labels and memory's fact_* labels have no fixed name."""
    text = UNINSTALL.read_text()
    assert re.search(r'while .*security delete-generic-password -s "\$KC_SERVICE" >', text)


def test_every_tcc_row_is_reset_for_both_bundles() -> None:
    services = _uninstall_var("TCC_SERVICES")
    assert {"Accessibility", "ScreenCapture", "Microphone", "AppleEvents",
            "Calendar", "SystemPolicyAllFiles"} <= services
    assert {"com.emma.daemon", "com.emma.ui"} <= _uninstall_var("TCC_BUNDLES")


def test_no_account_copy() -> None:
    assert "cuenta" not in UNINSTALL.read_text().lower()
