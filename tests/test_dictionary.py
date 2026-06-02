"""Phase 18.5: knowledge dictionary loader, append helpers, and seeder."""

from __future__ import annotations

import tomllib
from unittest.mock import AsyncMock

import pytest

from core import dictionary, vocabulary

_SAMPLE = """\
[pages.github]
url = "https://github.com/garcia"
title = "Mi GitHub"

[contacts.mom]
name = "Ana García"
email = "ana@example.com"
relation = "madre"
aliases = ["mami", "mamá"]

[terms.MCP]
expansion = "Model Context Protocol"
context = "Estándar de Anthropic."

[apps.editor]
default = "Cursor"

[facts.001]
text = "Garcia vive en Monterrey."
kind = "profile"
confidence = 0.99

[facts.002]
text = "Garcia es desarrollador."
kind = "profile"
confidence = 0.9
"""


@pytest.fixture
def temp_dict(tmp_path, monkeypatch):
    path = tmp_path / "dictionary.toml"
    path.write_text(_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(dictionary, "_DICT_PATH", path)
    dictionary.reload()
    yield path
    dictionary._DICT_PATH = path  # keep pointing at temp during teardown
    dictionary.reload()


class TestLoad:
    def test_five_sections_parse(self, temp_dict):
        data = tomllib.loads(temp_dict.read_text())
        assert {"pages", "contacts", "terms", "apps", "facts"} <= set(data)

    def test_find_page(self, temp_dict):
        assert dictionary.find_page("github").url == "https://github.com/garcia"
        assert dictionary.find_page("nope") is None

    def test_find_contact_by_alias(self, temp_dict):
        assert dictionary.find_contact("mamá").name == "Ana García"
        assert dictionary.find_contact("madre").email == "ana@example.com"

    def test_expand_term_case_insensitive(self, temp_dict):
        assert dictionary.expand_term("mcp").expansion == "Model Context Protocol"

    def test_app_for(self, temp_dict):
        assert dictionary.app_for("editor") == "Cursor"


class TestAppend:
    def test_append_page_roundtrips(self, temp_dict):
        dictionary.append_page("portfolio", "https://garcia.example.com", title="Portfolio")
        # File still valid TOML, entry visible after reload.
        tomllib.loads(temp_dict.read_text())
        assert dictionary.find_page("portfolio").url == "https://garcia.example.com"

    def test_append_contact_and_term(self, temp_dict):
        dictionary.append_contact("dad", "Luis", email="luis@example.com", relation="padre")
        dictionary.append_term("RAG", "Retrieval Augmented Generation")
        assert dictionary.find_contact("padre").name == "Luis"
        assert dictionary.expand_term("RAG").expansion == "Retrieval Augmented Generation"


class TestInjection:
    def test_toml_escape_strips_control_chars(self):
        out = dictionary._toml_escape('a"\nb\t[x]')
        assert "\n" not in out and "\t" not in out
        assert '\\"' in out  # the quote is escaped

    def test_append_resists_injection(self, temp_dict):
        dictionary.append_page(
            "evil",
            'http://x"\nfake\n[pages.bad]\nurl="http://evil"',
            title='t"\n[X]\ny="2"',
        )
        data = tomllib.loads(temp_dict.read_text())  # must still parse
        assert "bad" not in data["pages"]
        assert "X" not in data


class TestSeeder:
    @pytest.mark.asyncio
    async def test_seed_counts_and_idempotent_vocab(self, temp_dict, tmp_path, monkeypatch):
        from emma import dictionary as seeder

        # Temp vocabulary so the term seeding doesn't touch the real file.
        vpath = tmp_path / "vocabulary.toml"
        vpath.write_text("", encoding="utf-8")
        monkeypatch.setattr(vocabulary, "_VOCAB_PATH", vpath)
        vocabulary.reload()

        # No network: stub memory.
        monkeypatch.setattr(seeder, "initialize", lambda: None)
        monkeypatch.setattr(seeder, "remember", AsyncMock(return_value=1))

        res1 = await seeder.seed()
        # 2 facts + 1 contact-with-email = 3 facts; 1 term = 1 vocab.
        assert res1 == {"facts": 3, "vocab": 1}

        res2 = await seeder.seed()
        # Term already in vocab → skipped; facts re-sent (dedup is memory's job).
        assert res2["vocab"] == 0
        assert res2["facts"] == 3
        # Restore real vocabulary cache for other tests.
        monkeypatch.undo()
        vocabulary.reload()
