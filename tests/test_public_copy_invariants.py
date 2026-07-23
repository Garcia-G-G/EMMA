"""Repository-wide privacy and visual-identity invariants."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    "actions",
    "backend",
    "config",
    "core",
    "dashboard",
    "data",
    "emma",
    "installer",
    "memory",
    "scripts",
    "self",
    "tools",
    "tests",
)
ROOT_FILES = ("README.md", "SECURITY.md", "ERRORS-TO-FIX.md", "pyproject.toml")
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".md",
    ".plist",
    ".py",
    ".sh",
    ".toml",
    ".xml",
}

# Keep the private values out of source as complete literals. The repository
# guidance is the only document allowed to spell them out.
PRIVATE_NAME = "Gar" + "cia"
PRIVATE_CITY = "Monte" + "rrey"

# Upgrade and uninstall logic must still recognize these exact legacy machine
# identifiers. They are compatibility keys, not product copy.
ALLOWED_LEGACY_IDENTIFIERS = ("com." + PRIVATE_NAME.casefold() + ".emma",)


def _shipping_text_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", *ROOT_FILES, *SCAN_ROOTS],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [
        ROOT / relative
        for raw_path in tracked.split(b"\0")
        if raw_path
        and (relative := raw_path.decode("utf-8")).endswith(tuple(TEXT_SUFFIXES))
    ]


def test_shipping_surfaces_do_not_assume_maker_identity_or_location() -> None:
    violations: list[str] = []
    for path in _shipping_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        scrubbed = text
        for identifier in ALLOWED_LEGACY_IDENTIFIERS:
            scrubbed = scrubbed.replace(identifier, "")
        lowered = scrubbed.casefold()
        for private_value in (PRIVATE_NAME, PRIVATE_CITY):
            if private_value.casefold() in lowered:
                violations.append(f"{path.relative_to(ROOT)}: {private_value}")
    assert violations == []
