"""No secret reaches a remote: the tracked tree is clean and the scanner bites (Step 40 Part 4.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "scripts" / "check_secrets.sh"
FIXTURE = "sk-" + "fixture" * 4  # built, so the tree scan never sees it


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def test_tracked_tree_has_no_secrets() -> None:
    r = subprocess.run([str(SCAN), "--tree"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_gitignore_keeps_secret_files_out() -> None:
    lines = set((ROOT / ".gitignore").read_text().split())
    assert {".env", ".env.*", "*.pem", "*.key", ".emma/"} <= lines


def test_only_env_example_is_tracked() -> None:
    tracked = _git(ROOT, "ls-files").stdout.splitlines()
    assert [f for f in tracked if Path(f).name.startswith(".env")] == [".env.example"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".secrets-allow").write_text(FIXTURE + "\n")
    return tmp_path


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("a.py", 'KEY = "sk-' + "A1b2" * 8 + '"\n'),
        ("b.py", 'k = "sk_' + "0f" * 24 + '"\n'),
        ("c.py", "re_" + "Ab3" * 10 + "\n"),
        ("d.pem", "-----BEGIN RSA " + "PRIVATE KEY-----\n"),
        ("e.py", "pass" + 'word = "hunter22"\n'),
        (".env.local", "X=1\n"),
    ],
)
def test_staged_secret_is_rejected(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    _git(repo, "add", "-f", name)
    r = subprocess.run([str(SCAN)], cwd=repo, capture_output=True, text=True, check=False)
    assert r.returncode == 1
    assert name in r.stdout


def test_allowlisted_fixture_passes_and_nothing_else_hides(repo: Path) -> None:
    (repo / "t.py").write_text(f'a = "{FIXTURE}"\n')
    _git(repo, "add", "t.py")
    assert subprocess.run([str(SCAN)], cwd=repo, capture_output=True, check=False).returncode == 0
    (repo / "t.py").write_text(f'a = "{FIXTURE}X"\n')
    _git(repo, "add", "t.py")
    assert subprocess.run([str(SCAN)], cwd=repo, capture_output=True, check=False).returncode == 1
