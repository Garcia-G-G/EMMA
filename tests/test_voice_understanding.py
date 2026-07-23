"""Phase 19.4 — voice understanding (identity + global biasing + GitHub paths).

Covers B11-B15. Dictionary/vocabulary tests run against temp copies of the real
TOML files (paths monkeypatched) so the committed config is never mutated.
GitHub tests mock ``httpx.AsyncClient`` — no network, no auth.
"""

from __future__ import annotations

import shutil
import tomllib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import dictionary, vocabulary


@pytest.fixture
def temp_dict(tmp_path, monkeypatch):
    """Point core.dictionary at a temp copy of the real dictionary.toml."""
    p = tmp_path / "dictionary.toml"
    shutil.copy(dictionary._DICT_PATH, p)
    monkeypatch.setattr(dictionary, "_DICT_PATH", p)
    dictionary.reload()
    yield p
    monkeypatch.undo()
    dictionary.reload()


@pytest.fixture
def temp_vocab(tmp_path, monkeypatch):
    p = tmp_path / "vocabulary.toml"
    shutil.copy(vocabulary._VOCAB_PATH, p)
    monkeypatch.setattr(vocabulary, "_VOCAB_PATH", p)
    vocabulary.reload()
    yield p
    monkeypatch.undo()
    vocabulary.reload()


# ---- B11: identity in the dictionary ---------------------------------------


class TestB11UserProfile:
    def test_user_profile_has_all_fields(self, temp_dict):
        prof = dictionary.user_profile()
        for f in ("display_name", "full_name", "github_username", "preferred_lang"):
            assert f in prof

    def test_set_user_field_round_trips(self, temp_dict):
        assert dictionary.set_user_field("github_username", "examplehandle") is True
        assert dictionary.user_profile()["github_username"] == "examplehandle"
        # raw TOML still parses and other sections survive
        data = tomllib.loads(temp_dict.read_text())
        assert data["user"]["github_username"] == "examplehandle"
        assert "github" in data.get("pages", {})  # pages block untouched

    def test_unknown_field_rejected(self, temp_dict):
        assert dictionary.set_user_field("ssn", "123") is False

    @pytest.mark.asyncio
    async def test_remember_user_profile_tool(self, temp_dict):
        from tools.dictionary_tool import remember_user_profile

        r = await remember_user_profile("github_username", "examplehandle")
        assert r.success is True
        assert dictionary.user_profile()["github_username"] == "examplehandle"
        bad = await remember_user_profile("password", "x")
        assert bad.success is False


# ---- B12: global Whisper biasing -------------------------------------------


class TestB12BiasWordsGlobal:
    def test_bias_includes_identity_contacts_glossary(self, temp_dict, temp_vocab):
        dictionary.set_user_field("github_username", "examplehandle")
        bw = vocabulary.bias_words()
        assert "examplehandle" in bw  # identity
        assert "Ana García" in bw and "mamá" in bw  # contact name + alias (seeded)
        assert "MCP" in bw  # glossary acronym
        assert " ".join(bw)[:500] == " ".join(bw)  # already ≤500, no slicing needed

    def test_priority_truncation_keeps_identity_and_contacts(self, monkeypatch):
        from core.dictionary import Contact

        monkeypatch.setattr(
            dictionary,
            "user_profile",
            lambda: {
                "display_name": "IDENT_NAME",
                "full_name": "",
                "github_username": "IDENT_HANDLE",
            },
        )
        monkeypatch.setattr(
            dictionary,
            "contacts",
            lambda: {"c": Contact("c", "CONTACT_PERSON", "", "", ["CONTACT_ALIAS"])},
        )
        monkeypatch.setattr(dictionary, "terms", lambda: {})
        monkeypatch.setattr(dictionary, "pages", lambda: {})
        monkeypatch.setattr(dictionary, "apps_preferences", lambda: {})
        # Flood vocabulary so the union blows past the 480-char budget.
        flood = {f"e{i}": {"canonical": f"VocabWord{i:03d}", "stt_aliases": []} for i in range(200)}
        monkeypatch.setattr(vocabulary, "_entries", flood)

        bw = vocabulary.bias_words()
        assert "IDENT_NAME" in bw and "IDENT_HANDLE" in bw
        assert "CONTACT_PERSON" in bw and "CONTACT_ALIAS" in bw
        assert len(" ".join(bw)) <= 500
        assert any(w.startswith("VocabWord") for w in bw)  # some vocab made it
        assert not all(f"VocabWord{i:03d}" in bw for i in range(200))  # but it truncated


class TestB12BiasNoCircularImport:
    def test_vocabulary_imports_without_dictionary_preloaded(self):
        import importlib

        mod = importlib.import_module("core.vocabulary")
        # vocab-only path must work even if called in isolation
        assert isinstance(mod.bias_words(), list)


# ---- B13: my_repos ----------------------------------------------------------


def _mk_client(side_effect):
    cli = MagicMock()
    cli.get = AsyncMock(side_effect=side_effect)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cli)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cli, cm


def _resp(status, body=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.json = MagicMock(return_value=body if body is not None else {})
    return m


def _repo(name, private=False, stars=3):
    return {
        "name": name,
        "full_name": f"u/{name}",
        "html_url": f"http://x/{name}",
        "clone_url": f"http://x/{name}.git",
        "description": "d",
        "stargazers_count": stars,
        "language": "Python",
        "private": private,
    }


class TestB13MyRepos:
    @pytest.mark.asyncio
    async def test_token_path_hits_user_repos(self, monkeypatch):
        import tools.github_tool as gh

        monkeypatch.setattr(
            gh.dictionary, "user_profile", lambda: {"github_username": "examplehandle"}
        )
        cli, cm = _mk_client([_resp(200, [_repo("a"), _repo("b", private=True), _repo("c")])])
        with (
            patch.object(gh.settings, "GITHUB_TOKEN", "tok"),
            patch.object(gh.httpx, "AsyncClient", return_value=cm),
        ):
            r = await gh.my_repos()
        assert cli.get.call_args[0][0] == "https://api.github.com/user/repos"
        assert cli.get.call_args.kwargs["params"]["affiliation"] == "owner"
        assert len(r.data["matches"]) == 3
        assert r.data["matches"][1]["private"] is True

    @pytest.mark.asyncio
    async def test_no_token_path_hits_users_username_repos(self, monkeypatch):
        import tools.github_tool as gh

        monkeypatch.setattr(
            gh.dictionary, "user_profile", lambda: {"github_username": "examplehandle"}
        )
        cli, cm = _mk_client([_resp(200, [_repo("a")])])
        with (
            patch.object(gh.settings, "GITHUB_TOKEN", ""),
            patch.object(gh.httpx, "AsyncClient", return_value=cm),
        ):
            await gh.my_repos()
        assert cli.get.call_args[0][0] == "https://api.github.com/users/examplehandle/repos"

    @pytest.mark.asyncio
    async def test_empty_username_asks(self, monkeypatch):
        import tools.github_tool as gh

        monkeypatch.setattr(gh.dictionary, "user_profile", lambda: {"github_username": ""})
        r = await gh.my_repos()
        assert r.success is False
        assert "usuario de GitHub" in r.user_message


# ---- B14: search_github zero-result retry ----------------------------------


class TestB14SearchGithubRetry:
    @pytest.mark.asyncio
    async def test_handle_hit_falls_back_to_user_repos(self, monkeypatch):
        import tools.github_tool as gh

        seq = [
            _resp(200, {"items": []}),  # search empty
            _resp(200, {"login": "examplehandle"}),  # user exists
            _resp(200, [_repo("x"), _repo("y")]),  # their repos
        ]
        _, cm = _mk_client(seq)
        with patch.object(gh.httpx, "AsyncClient", return_value=cm):
            r = await gh.search_github("examplehandle")
        assert len(r.data["matches"]) == 2
        assert r.user_message.startswith("Encontré 2 repos del usuario examplehandle")

    @pytest.mark.asyncio
    async def test_multiword_query_no_retry(self, monkeypatch):
        import tools.github_tool as gh

        cli, cm = _mk_client([_resp(200, {"items": []})])
        with patch.object(gh.httpx, "AsyncClient", return_value=cm):
            r = await gh.search_github("react native")
        assert cli.get.await_count == 1  # no extra hops
        assert "No encontré repos para 'react native'" in r.user_message

    @pytest.mark.asyncio
    async def test_handle_404_returns_friendly_hint(self, monkeypatch):
        import tools.github_tool as gh

        _, cm = _mk_client([_resp(200, {"items": []}), _resp(404, {})])
        with patch.object(gh.httpx, "AsyncClient", return_value=cm):
            r = await gh.search_github("nonexistentuser123abc")
        assert r.success is True
        assert "mis repos" in r.user_message


# ---- B15: learn from corrections -------------------------------------------


class TestB15RememberCorrection:
    @pytest.mark.asyncio
    async def test_new_entry_when_no_match(self, temp_vocab):
        from tools.dictionary_tool import remember_stt_correction

        r = await remember_stt_correction("example handle", "examplehandle")
        assert r.success is True
        assert vocabulary.corrections("busca example handle") == "busca examplehandle"
        data = tomllib.loads(temp_vocab.read_text())
        assert any(b.get("canonical") == "examplehandle" for b in data.values())

    @pytest.mark.asyncio
    async def test_appends_alias_to_existing_entry(self, temp_vocab):
        from tools.dictionary_tool import remember_stt_correction

        await remember_stt_correction("example handle", "examplehandle")
        await remember_stt_correction("gilbert ata", "examplehandle")  # same canonical
        # both aliases now fold to the one canonical
        assert vocabulary.corrections("gilbert ata") == "examplehandle"
        assert vocabulary.corrections("example handle") == "examplehandle"

    @pytest.mark.asyncio
    async def test_refuses_noop(self, temp_vocab):
        from tools.dictionary_tool import remember_stt_correction

        r = await remember_stt_correction("Tacuba", "tacuba")  # same, case-insensitive
        assert r.success is False
