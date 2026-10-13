"""BRAIN-1 Part 0a — the user's dictionary lives in ~/.emma, not in the source tree.

install.sh re-extracts the release tarball over ~/.emma/src on every upgrade.
Before 0a the dictionary was written inside that tree, so an upgrade erased the
user's contacts, personality and facts. These tests pin the split: the shipped
file is a read-only template, the user file wins on read and takes every write.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core import dictionary

TEMPLATE = """\
[user]
display_name = ""

[apps.browser]
default = "Safari"

[apps.editor]
default = "TextEdit"
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    template = tmp_path / "src" / "config" / "dictionary.toml"
    template.parent.mkdir(parents=True)
    template.write_text(TEMPLATE, encoding="utf-8")
    user = tmp_path / "home" / ".emma" / "dictionary.toml"
    monkeypatch.setattr(dictionary, "_TEMPLATE_PATH", template)
    monkeypatch.setattr(dictionary, "_DICT_PATH", user)
    dictionary.reload()
    yield template, user
    monkeypatch.undo()
    dictionary.reload()


def test_migrate_seeds_the_user_file_from_the_in_tree_one(paths: tuple[Path, Path]) -> None:
    template, user = paths
    assert not user.exists()
    assert dictionary.migrate() is True
    assert user.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    assert user.stat().st_mode & 0o777 == 0o600
    assert dictionary.migrate() is False  # idempotent: never overwrites the user's file


def test_writes_go_to_the_user_file_never_the_template(paths: tuple[Path, Path]) -> None:
    template, user = paths
    dictionary.set_user_field("display_name", "Sam")
    dictionary.append_contact("ana", "Ana", aliases=["anita"])
    assert template.read_text(encoding="utf-8") == TEMPLATE
    assert 'display_name = "Sam"' in user.read_text(encoding="utf-8")
    assert dictionary.user_profile()["display_name"] == "Sam"
    assert "ana" in dictionary.contacts()


def test_user_wins_key_by_key_and_new_template_keys_still_appear(
    paths: tuple[Path, Path],
) -> None:
    template, _user = paths
    dictionary.set_app_preference("browser", "Brave Browser")
    # A later release adds a key the user file has never seen.
    template.write_text(TEMPLATE + '\n[apps.terminal]\ndefault = "Terminal"\n', encoding="utf-8")
    dictionary.reload()
    prefs = dictionary.apps_preferences()
    assert prefs["browser"] == "Brave Browser"  # user's choice survives
    assert prefs["editor"] == "TextEdit"  # untouched template key
    assert prefs["terminal"] == "Terminal"  # new template key arrives


def test_user_data_survives_an_installer_re_extract(paths: tuple[Path, Path]) -> None:
    """The DoD: user data survives a re-run of the installer."""
    template, _user = paths
    dictionary.set_personality_field("humor", 5)
    dictionary.append_fact("cafe", "Prefiere café sin azúcar")
    # install.sh: tar -xzf over ~/.emma/src — the template is replaced wholesale.
    template.write_text(TEMPLATE, encoding="utf-8")
    dictionary.reload()
    assert dictionary.personality_profile()["humor"] == 5
    assert "cafe" in {f.key for f in dictionary.facts()}


def test_a_pre_0a_install_keeps_what_it_learned(paths: tuple[Path, Path]) -> None:
    """Old installs wrote into the tree. First boot after upgrade must carry that over."""
    template, _user = paths
    template.write_text(
        TEMPLATE + '\n[contacts.mom]\nname = "Ana"\naliases = ["mamá"]\n', encoding="utf-8"
    )
    dictionary.migrate()
    template.write_text(TEMPLATE, encoding="utf-8")  # then the upgrade lands
    dictionary.reload()
    assert dictionary.contacts()["mom"].name == "Ana"


def test_tables_is_the_merged_view(paths: tuple[Path, Path]) -> None:
    _template, user = paths
    user.parent.mkdir(parents=True)
    user.write_text('[feeds.hn]\nurl = "https://news.example/rss"\n', encoding="utf-8")
    merged = dictionary.tables()
    assert merged["feeds"]["hn"]["url"] == "https://news.example/rss"
    assert merged["apps"]["browser"]["default"] == "Safari"
