"""ElevenLabs TTS → cached WAVs for the voice acceptance harness (19.7-VAH1).

Each unique (text, voice_id, model_id) synthesizes EXACTLY once; the WAV
lands in ``audio_cache/<lang>/<sha16>.wav`` and every later run reuses it
(ElevenLabs bills per character — the cache IS the cost control).

Format: we request ``output_format=pcm_24000`` (raw 16-bit PCM, 24 kHz mono)
and wrap it in a WAV header with stdlib ``wave`` — 24 kHz matches Pipecat's
``SAMPLE_RATE_HZ`` exactly, so no resampling, no mp3, no ffmpeg.

Sources: elevenlabs.io/docs/api-reference/text-to-speech (xi-api-key header,
voice_settings), elevenlabs.io/docs/models/flash-v2-5 (eleven_flash_v2_5,
0.5 credits/char). Bulk pre-warm: ``python -m tests.acceptance.audio_gen
--prewarm``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import wave
from pathlib import Path

import httpx

from config.settings import settings

CACHE_DIR = Path(__file__).parent / "audio_cache"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
_VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}
_SAMPLE_RATE = 24_000  # == core.conversation.SAMPLE_RATE_HZ

# Flash v2.5 bills 0.5 credits/char; on the Creator tier ($22 / 100k credits)
# that's ≈ $0.00011 per character → $0.11 per 1k chars. Used for the cost
# guard + the per-run report (elevenlabs.io/docs/models/flash-v2-5 + pricing).
USD_PER_CHAR = 0.00011


class VoiceGenError(RuntimeError):
    """Friendly synthesis failure — never a raw httpx stacktrace."""


def _voice_for(lang: str, voice_id: str | None) -> str:
    if voice_id:
        return voice_id
    return settings.ELEVENLABS_VOICE_ID_EN if lang == "en" else settings.ELEVENLABS_VOICE_ID_ES


def _sha(text: str, voice_id: str, model_id: str) -> str:
    return hashlib.sha256(f"{text}|{voice_id}|{model_id}".encode()).hexdigest()[:16]


def cache_path(text: str, lang: str = "es", voice_id: str | None = None) -> Path:
    """Where this utterance lives (or would live) in the cache."""
    vid = _voice_for(lang, voice_id)
    return CACHE_DIR / lang / f"{_sha(text, vid, settings.ELEVENLABS_MODEL_ID)}.wav"


def _pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _fetch(text: str, voice_id: str) -> bytes:
    try:
        resp = httpx.post(
            f"{_API_BASE}/{voice_id}",
            params={"output_format": f"pcm_{_SAMPLE_RATE}"},
            headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
            json={
                "text": text,
                "model_id": settings.ELEVENLABS_MODEL_ID,
                "voice_settings": _VOICE_SETTINGS,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise VoiceGenError(f"No pude conectar con ElevenLabs: {exc}") from exc
    if resp.status_code == 401:
        raise VoiceGenError("ElevenLabs rechazó la API key (401). Revisa ELEVENLABS_API_KEY.")
    if resp.status_code != 200:
        raise VoiceGenError(f"ElevenLabs devolvió {resp.status_code}: {resp.text[:200]}")
    return _pcm_to_wav(resp.content)


def _manifest_load() -> dict[str, dict[str, object]]:
    if MANIFEST_PATH.exists():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _manifest_add(sha: str, text: str, voice_id: str, lang: str, scenario_id: str) -> None:
    manifest = _manifest_load()
    manifest[sha] = {
        "text": text,
        "voice_id": voice_id,
        "model_id": settings.ELEVENLABS_MODEL_ID,
        "lang": lang,
        "scenario_id": scenario_id,
        "chars": len(text),
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def synthesize(
    text: str, lang: str = "es", voice_id: str | None = None, scenario_id: str = ""
) -> Path:
    """WAV path for ``text`` — from cache, or synthesized once and cached."""
    vid = _voice_for(lang, voice_id)
    path = cache_path(text, lang, voice_id)
    if path.exists():
        return path
    audio = _fetch(text, vid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    _manifest_add(path.stem, text, vid, lang, scenario_id)
    return path


# ---- wake clip (shared with the voice runner) -------------------------------

WAKE_PHRASE = "Hey Jarvis."  # what the active openWakeWord model listens for

# 19.7 spec deviation (measured): the spec wanted one combined
# "Hey Jarvis, <utterance>" clip, but (a) the ES voice saying "Hey Jarvis"
# peaks at 0.385 on the wake model vs 0.989 for the EN voice, and (b) Emma's
# chime + Realtime connect (~1s) after wake would swallow a concatenated
# utterance. The runner therefore plays TWO clips: this EN wake clip, waits
# for `conversation_start`, then the bare utterance in the scenario's voice.


def wake_clip() -> Path:
    """The shared English 'Hey Jarvis.' clip (cached once, used by every run)."""
    return synthesize(WAKE_PHRASE, lang="en", scenario_id="wake-prefix")


# ---- bulk pre-warm + cost estimate ------------------------------------------


def _corpus_texts() -> list[tuple[str, str, str | None, str]]:
    """(text, lang, voice_id, scenario_id) for every playable corpus entry.

    Bare utterances — the wake phrase is its own shared clip (see wake_clip).
    """
    from tests.acceptance.runner import load_scenarios

    out: list[tuple[str, str, str | None, str]] = [(WAKE_PHRASE, "en", None, "wake-prefix")]
    for s in load_scenarios():
        out.append((s["utterance"], s.get("language", "es"), s.get("voice_id"), s["id"]))
        follow = s.get("followup")
        if follow:
            out.append(
                (
                    follow["utterance"],
                    follow.get("language", "es"),
                    follow.get("voice_id"),
                    f"{s['id']}+followup",
                )
            )
    return out


def estimate_missing() -> tuple[int, int]:
    """(missing_count, missing_chars) for everything not yet cached."""
    missing = [
        (text, lang, vid, sid)
        for text, lang, vid, sid in _corpus_texts()
        if not cache_path(text, lang, vid).exists()
    ]
    return len(missing), sum(len(t) for t, _, _, _ in missing)


def prewarm() -> tuple[int, int]:
    """Generate every missing corpus WAV. Returns (generated, reused)."""
    generated = reused = 0
    for text, lang, vid, sid in _corpus_texts():
        if cache_path(text, lang, vid).exists():
            reused += 1
            continue
        synthesize(text, lang, vid, scenario_id=sid)
        generated += 1
    return generated, reused


def main() -> int:
    parser = argparse.ArgumentParser(description="ElevenLabs audio cache for the voice harness")
    parser.add_argument("--prewarm", action="store_true", help="generate all missing corpus WAVs")
    args = parser.parse_args()
    if not args.prewarm:
        parser.print_help()
        return 2
    n, chars = estimate_missing()
    print(f"missing: {n} clips, {chars} chars ≈ ${chars * USD_PER_CHAR:.2f}")
    try:
        generated, reused = prewarm()
    except VoiceGenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"generated {generated}, reused {reused}, skipped 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
