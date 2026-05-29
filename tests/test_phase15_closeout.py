"""Phase 15 closeout: destructive-action confirmation + vocabulary library.

TestDestructiveConfirmation verifies the six macOS-app destructive tools gate
their AppleScript call behind the two-phase confirmation flow: the first call
(no ``confirmed``) returns ``requires_confirmation=True`` and never shells out;
the second call (``confirmed=True``) does invoke ``osascript``.

TestVocabulary covers the vocabulary library: STT corrections, the
pronunciation block, bias words, hot reload, and the ``add_vocabulary_word``
round-trip — all against a temp TOML so the real file is never touched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core import vocabulary
from tools.calendar_tool import delete_event
from tools.finder_tool import move_item
from tools.mail_tool import send_to
from tools.messages_tool import send_imessage
from tools.notes_tool import delete_note
from tools.reminders_tool import complete_reminder

# All six destructive tools route their AppleScript through actions.macos.osascript.
_OSASCRIPT = "actions.macos.osascript"

# (function, kwargs) — kwargs are the first-call args; confirmed=True is added
# for the second call. Targets are deliberately nonexistent / fake.
_DESTRUCTIVE_CASES = [
    pytest.param(delete_event, {"title": "EMMA_TEST_NONEXISTENT"}, id="calendar.delete_event"),
    pytest.param(
        send_to,
        {"recipient": "nobody@example.invalid", "subject": "EMMA_TEST", "body": "x"},
        id="mail.send_to",
    ),
    pytest.param(
        send_imessage,
        {"recipient": "+10000000000", "body": "EMMA_TEST"},
        id="messages.send_imessage",
    ),
    pytest.param(delete_note, {"title": "EMMA_TEST_NONEXISTENT"}, id="notes.delete_note"),
    pytest.param(
        complete_reminder, {"title": "EMMA_TEST_NONEXISTENT"}, id="reminders.complete_reminder"
    ),
    pytest.param(
        move_item,
        {"src": "/tmp/emma-doesnt-exist-A", "dst": "/tmp/emma-doesnt-exist-B"},
        id="finder.move_item",
    ),
]


class TestDestructiveConfirmation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn, kwargs", _DESTRUCTIVE_CASES)
    async def test_first_call_asks_without_osascript(self, fn, kwargs):
        with patch(_OSASCRIPT, new=AsyncMock(return_value="0")) as mock_osa:
            result = await fn(**kwargs)
        assert result.requires_confirmation is True, "first call must ask for confirmation"
        assert mock_osa.await_count == 0, "first call must NOT invoke osascript"
        assert result.user_message.strip().endswith("?"), (
            "confirmation message is a yes/no question"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn, kwargs", _DESTRUCTIVE_CASES)
    async def test_second_call_invokes_osascript(self, fn, kwargs):
        with patch(_OSASCRIPT, new=AsyncMock(return_value="0")) as mock_osa:
            result = await fn(**kwargs, confirmed=True)
        assert mock_osa.await_count >= 1, "confirmed call must invoke osascript"
        assert result.requires_confirmation is False, "confirmed call must not re-ask"


# Minimal TOML exercising every code path in core/vocabulary.py.
_TEMP_TOML = """\
[Claude]
canonical = "Claude"
description = "test"
stt_aliases = ["cloud", "clod"]
say_es = "clod"

[ClaudeCode]
canonical = "Claude Code"
stt_aliases = ["cloud code"]
say_es = "clod cod"

[Pipecat]
canonical = "Pipecat"
stt_aliases = ["pipe cat", "paypa cat"]

[NoHint]
canonical = "NoHint"
stt_aliases = ["no hint"]
"""


@pytest.fixture(autouse=True)
def _restore_real_vocab():
    """After each test, force the module cache back to the real file."""
    real_path = vocabulary._VOCAB_PATH
    yield
    vocabulary._VOCAB_PATH = real_path
    vocabulary.reload()


@pytest.fixture
def temp_vocab(tmp_path, monkeypatch):
    path = tmp_path / "vocabulary.toml"
    path.write_text(_TEMP_TOML, encoding="utf-8")
    monkeypatch.setattr(vocabulary, "_VOCAB_PATH", path)
    vocabulary.reload()
    return path


class TestVocabulary:
    def test_corrections_multi_alias_case_insensitive_whole_word(self, temp_vocab):
        # Multiple aliases, mixed case; "cloud code" wins over "cloud".
        out = vocabulary.corrections("Abre CLOUD CODE y usa Cloud")
        assert out == "Abre Claude Code y usa Claude"

    def test_corrections_respects_word_boundary(self, temp_vocab):
        # "cloud" inside "cloudy" must NOT be rewritten.
        assert vocabulary.corrections("un dia cloudy") == "un dia cloudy"

    def test_pronunciation_block_multiline_when_say_es_present(self, temp_vocab):
        block = vocabulary.pronunciation_block("es")
        assert block.startswith("# Pronunciation guide (mandatory)")
        assert "\n" in block
        assert "Claude" in block
        # NoHint has no say_es → excluded.
        assert "NoHint" not in block

    def test_pronunciation_block_empty_when_no_hints(self, temp_vocab):
        # "en" has no say_en entries in the temp file → empty string.
        assert vocabulary.pronunciation_block("en") == ""

    def test_bias_words_are_canonical_names(self, temp_vocab):
        words = vocabulary.bias_words()
        assert words == ["Claude", "Claude Code", "Pipecat", "NoHint"]

    def test_hot_reload_picks_up_new_entries(self, temp_vocab):
        assert "Letta" not in vocabulary.bias_words()
        temp_vocab.write_text(
            _TEMP_TOML + '\n[Letta]\ncanonical = "Letta"\nstt_aliases = ["leta"]\n',
            encoding="utf-8",
        )
        count = vocabulary.reload()
        assert count == 5
        assert "Letta" in vocabulary.bias_words()
        assert vocabulary.corrections("usa leta") == "usa Letta"

    @pytest.mark.asyncio
    async def test_add_vocabulary_word_roundtrip(self, temp_vocab):
        from tools.vocabulary_tool import add_vocabulary_word

        result = await add_vocabulary_word(
            canonical="Letta",
            say_es="leta",
            aliases=["leta", "letta ai"],
            description="memory layer",
        )
        assert result.success is True
        assert "Letta" in vocabulary.bias_words()
        assert vocabulary.corrections("prueba leta hoy") == "prueba Letta hoy"

    @pytest.mark.asyncio
    async def test_add_vocabulary_word_rejects_empty(self, temp_vocab):
        from tools.vocabulary_tool import add_vocabulary_word

        result = await add_vocabulary_word(canonical="   ")
        assert result.success is False
