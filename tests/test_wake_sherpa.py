"""sherpa-onnx KWS wake engine — pure matcher, keyword gen, dispatch (no audio)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core import wake_sherpa as ws
from core import wake_word


@pytest.mark.parametrize("text", ["emma", "oye emma", "hey emma", "EMMA", "óye émma", "hola_emma"])
def test_matches_wake_accepts_phrases(text: str) -> None:
    assert ws.matches_wake(text) is True


@pytest.mark.parametrize("text", ["", "tema", "sistema", "hola que tal", "problema"])
def test_matches_wake_rejects_non_wake(text: str) -> None:
    assert ws.matches_wake(text) is False


def test_normalize_strips_accents_and_case() -> None:
    assert ws._normalize("ÓYE ÉMMA") == "oye emma"


def test_wake_phrases_include_bare_emma_first() -> None:
    # Bare one-word "emma" is the primary target and must be the most sensitive
    # (lowest threshold) keyword.
    phrases = {p[0]: p for p in ws.WAKE_PHRASES}
    assert "emma" in phrases
    assert {"emma", "oye emma", "hey emma", "hola emma", "ey emma"} <= set(phrases)
    bare_threshold = phrases["emma"][1]
    assert bare_threshold <= min(p[1] for p in ws.WAKE_PHRASES)


def test_find_one_prefers_full_precision(tmp_path) -> None:
    # int8 builds ship alongside fp32; detection quality wants the fp32 one.
    (tmp_path / "encoder-epoch-12-chunk-16.onnx").write_bytes(b"x")
    (tmp_path / "encoder-epoch-12-chunk-16.int8.onnx").write_bytes(b"x")
    picked = ws._find_one(tmp_path, "encoder")
    assert picked.endswith("encoder-epoch-12-chunk-16.onnx")
    assert ".int8." not in picked


def test_find_one_missing_raises(tmp_path) -> None:
    with pytest.raises(SystemExit):
        ws._find_one(tmp_path, "encoder")


def test_write_keywords_file_tokenizes_and_annotates(tmp_path, monkeypatch) -> None:
    """Keywords file carries BPE tokens + per-phrase :boost #threshold @label.

    sentencepiece is stubbed so the test needs no model on disk.
    """
    import sys
    import types

    fake_spm = types.ModuleType("sentencepiece")

    class _Proc:
        def load(self, path: str) -> None:
            self._loaded = path

        def encode(self, text: str, out_type=str):
            # Deterministic fake tokenization: one ▁-prefixed piece per word.
            return [f"▁{w}" for w in text.split()]

    fake_spm.SentencePieceProcessor = _Proc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentencepiece", fake_spm)
    (tmp_path / "bpe.model").write_bytes(b"fake")

    out = ws._write_keywords_file(tmp_path)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == len(ws.WAKE_PHRASES)
    # bare "emma" line: tokens then annotations, threshold matches WAKE_PHRASES.
    emma_line = next(ln for ln in lines if ln.endswith("@emma"))
    assert emma_line.startswith("▁EMMA ")
    assert ":1.0" in emma_line and "#0.15" in emma_line
    # a two-word phrase keeps its underscore label and both tokens.
    hey_line = next(ln for ln in lines if ln.endswith("@hey_emma"))
    assert "▁HEY ▁EMMA" in hey_line


def test_write_keywords_file_missing_bpe_raises(tmp_path) -> None:
    with pytest.raises(SystemExit):
        ws._write_keywords_file(tmp_path)


@pytest.mark.asyncio
async def test_dispatch_routes_sherpa_engine(monkeypatch) -> None:
    monkeypatch.setattr(wake_word.settings, "WAKE_WORD_ENGINE", "sherpa")
    called = AsyncMock()
    monkeypatch.setattr(ws, "listen", called)
    await wake_word.listen_for_wake_word()
    called.assert_awaited_once()


@pytest.mark.asyncio
async def test_sherpa_is_the_default_engine(monkeypatch) -> None:
    # Empty engine (broken .env) falls back to the shipped default, sherpa.
    monkeypatch.setattr(wake_word.settings, "WAKE_WORD_ENGINE", "")
    called = AsyncMock()
    monkeypatch.setattr(ws, "listen", called)
    await wake_word.listen_for_wake_word()
    called.assert_awaited_once()
