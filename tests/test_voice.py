"""VOICE-1 Part 4 — speech that doesn't cost tokens: cached clips, one player, no `say`."""

from __future__ import annotations

import ast
import struct
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from pipecat.frames.frames import TTSAudioRawFrame

from config.settings import Settings, settings
from core import cascade, permissions, voice
from core.brain.respond import fixed_phrasings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(voice, "CACHE_DIR", tmp_path / "voice_cache")
    monkeypatch.setattr(settings, "VOICE_ID", "voiceA")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "sk_test")
    return tmp_path / "voice_cache"


def _pcm(ms: int = 100) -> bytes:
    return struct.pack(f"<{24 * ms}h", *([1000] * 24 * ms))


def test_the_cache_key_is_text_voice_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    a = voice.clip_path("Listo.")
    monkeypatch.setattr(settings, "VOICE_ID", "voiceB")
    assert voice.clip_path("Listo.") != a  # another voice never plays a stale clip
    monkeypatch.setattr(settings, "ELEVENLABS_TTS_MODEL", "other_model")
    assert voice.clip_path("Listo.") not in (a,)
    assert voice.clip_path("Hecho.") != voice.clip_path("Listo.")


def test_store_and_read_back(_cache: Path) -> None:
    voice.store("Listo.", _pcm(), [("Listo.", 0.0)])
    pcm, words = voice.cached("Listo.") or (b"", [])
    assert pcm == _pcm() and words == [("Listo.", 0.0)]
    assert oct(_cache.stat().st_mode)[-3:] == "700"  # personal tier
    assert not list(_cache.glob("*.tmp"))  # atomic: no half-written clip left behind


def test_a_miss_synthesizes_once_then_plays_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    played: list[list[str]] = []

    def synth(text: str) -> tuple[bytes, list[tuple[str, float]]]:
        calls.append(text)
        return _pcm(), [("Hecho.", 0.0)]

    monkeypatch.setattr(voice, "synthesize", synth)
    monkeypatch.setattr(voice.subprocess, "run", lambda cmd, **k: played.append(cmd))
    assert voice.speak_sync("Hecho.") and voice.speak_sync("Hecho.")
    assert calls == ["Hecho."]  # the second time is the cache
    assert played[0][0] == "afplay" and played[0] == played[1]


def test_keep_false_never_writes_the_line(_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played: list[list[str]] = []
    monkeypatch.setattr(voice, "synthesize", lambda t: (_pcm(), []))
    monkeypatch.setattr(voice.subprocess, "run", lambda cmd, **k: played.append(cmd))
    assert voice.speak_sync("Tu código es A B C", keep=False)
    assert played and not (_cache.exists() and any(_cache.iterdir()))


def test_no_voice_yet_prints_and_never_falls_back_to_say(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "VOICE_ID", "")
    ran: list[Any] = []
    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(voice.subprocess, "Popen", lambda *a, **k: ran.append(a))
    assert voice.speak_sync("Primero necesito el micrófono.") is False
    assert ran == [] and "Primero necesito el micrófono." in capsys.readouterr().err


def test_the_crash_path_plays_only_a_cached_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import crash_handler

    popen: list[list[str]] = []
    monkeypatch.setattr(voice.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    monkeypatch.setattr(voice, "synthesize", lambda t: pytest.fail("network in a crash"))
    crash_handler._say_failure()
    assert popen == []  # nothing cached: silent, and no network
    voice.store(voice.CRASH_LINE, _pcm(), [])
    crash_handler._say_failure()
    assert popen and popen[0][0] == "afplay"  # detached: outlives the dying process


def test_pregenerated_is_fifty_fixed_lines_system_first() -> None:
    lines = voice.pregenerated()
    assert len(lines) == voice.PREGENERATE_N == 50 and len(set(lines)) == 50
    assert lines[: len(permissions.spoken_lines())] == permissions.spoken_lines()
    assert voice.CRASH_LINE in lines
    assert not any("{" in t for t in lines)
    assert lines.index("Eso todavía no lo hago sola; lo apunté.") < lines.index("Listo.")


def test_fixed_phrasings_have_no_slots() -> None:
    rows = fixed_phrasings()
    assert rows and all("{" not in t for _k, _l, t in rows)
    assert rows[0][0] == "not_yet"  # the most frequent verdict on the 140 (defer)


def test_voice_id_is_required_with_no_shipped_default() -> None:
    assert Settings.model_fields["VOICE_ID"].default == ""


def test_say_is_gone_from_the_product() -> None:
    """No `say` subprocess and no macOS regional voice anywhere Emma runs."""
    offenders = []
    for d in ("core", "emma", "tools", "actions", "dashboard"):
        for p in (ROOT / d).rglob("*.py"):
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                where = f"{p.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}"
                if isinstance(node, ast.Constant) and node.value in ("Mónica", "Paulina", "Samantha"):
                    offenders.append(where)
                # ["say", ...] / ("say", ...) / exec("say", ...) / _spawn("say", ...)
                elems = node.elts if isinstance(node, ast.List | ast.Tuple) else (
                    node.args if isinstance(node, ast.Call) else [])
                if elems and isinstance(elems[0], ast.Constant) and elems[0].value == "say":
                    offenders.append(where)
    assert offenders == []


# ---- in-session: the TTS serves cached templates and fills the cache --------------------


@pytest.fixture
def words(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """What reaches the base TTS's word-timestamp path (CachedTTS calls super)."""
    got: list[tuple[str, float]] = []

    async def capture(self: Any, word_times: list[tuple[str, float]], context_id: Any = None,
                      includes_inter_frame_spaces: Any = None) -> None:
        got.extend(word_times)

    monkeypatch.setattr(cascade.ElevenLabsHttpTTSService, "add_word_timestamps", capture)
    return got


def _tts(http: aiohttp.ClientSession, cacheable: frozenset[str]) -> cascade.CachedTTS:
    tts = cascade.CachedTTS(api_key="sk_test", aiohttp_session=http, sample_rate=24000,
                            settings=cascade.ElevenLabsHttpTTSService.Settings(
                                voice="voiceA", model=settings.ELEVENLABS_TTS_MODEL),
                            cacheable=cacheable)
    tts._sample_rate = 24000  # set by StartFrame in a pipeline
    return tts


@pytest.mark.asyncio
async def test_a_cached_template_plays_with_no_network(
    monkeypatch: pytest.MonkeyPatch, words: list[tuple[str, float]]
) -> None:
    voice.store("Listo.", _pcm(100), [("Listo.", 0.0)])

    async def network(self: Any, text: str, context_id: str) -> Any:
        pytest.fail("a cached line went to the network")
        yield None

    monkeypatch.setattr(cascade.ElevenLabsHttpTTSService, "run_tts", network)
    async with aiohttp.ClientSession() as http:
        frames = [f async for f in _tts(http, frozenset({"Listo."})).run_tts("Listo.", "ctx")]
    audio = b"".join(f.audio for f in frames if isinstance(f, TTSAudioRawFrame))
    assert audio == _pcm(100)
    assert words == [("Listo.", 0.0)]  # the "what she said" record survives the cache


@pytest.mark.asyncio
async def test_a_miss_streams_then_fills_the_cache_only_for_fixed_lines(
    monkeypatch: pytest.MonkeyPatch, words: list[tuple[str, float]]
) -> None:
    async def network(self: Any, text: str, context_id: str) -> Any:
        await self.add_word_timestamps([("w", 0.0)], context_id)
        yield TTSAudioRawFrame(_pcm(20), 24000, 1, context_id=context_id)

    monkeypatch.setattr(cascade.ElevenLabsHttpTTSService, "run_tts", network)
    async with aiohttp.ClientSession() as http:
        tts = _tts(http, frozenset({"Hecho."}))
        [f async for f in tts.run_tts("Hecho.", "c1")]
        [f async for f in tts.run_tts("Hoy tienes: dentista a las 4.", "c2")]
    assert voice.cached("Hecho.") == (_pcm(20), [("w", 0.0)])
    assert voice.cached("Hoy tienes: dentista a las 4.") is None  # a tool result never lands on disk


def test_the_cascade_caches_exactly_the_fixed_lines() -> None:
    lines = cascade.cacheable_lines()
    assert "Listo." in lines and voice.CRASH_LINE in lines
    assert not any("{" in t for t in lines)


def test_pregenerate_without_a_voice_says_so_with_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installer prints "Voz lista" only on 0; 3 means onboarding will render them."""
    monkeypatch.setattr(settings, "VOICE_ID", "")
    assert voice.main(["pregenerate"]) == voice.NOT_CONFIGURED == 3
