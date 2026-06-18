#!/usr/bin/env python3
"""Generate wake-word training data via ElevenLabs (Prompt 16.2.1).

Drop-in alternative to scripts/wake_word_data.py: instead of Piper TTS it
synthesizes the positive (and negative) clips through ElevenLabs, reusing the
SAME Keychain-backed key the 19.7 voice-acceptance harness uses
(settings.ELEVENLABS_API_KEY → tests/acceptance/audio_gen.py). The on-disk
layout is byte-for-byte what train_wake_word.py expects, so the trainer reads
this data unchanged:

    <out>/positive/*.wav     # "hey emma" / "oye emma" / "hola emma"
    <out>/negative/*.wav     # speech that is NOT the wake word (TTS)

ElevenLabs emits pcm_16000 directly (settings.ELEVENLABS_OUTPUT_FORMAT), i.e.
16 kHz mono 16-bit — openWakeWord's exact input format — so there is NO
resampling step (unlike the Piper path).

ElevenLabs bills per character. There are no Piper-style length/noise knobs, so
acoustic variety comes from (a) multiple voice IDs and (b) a small grid of
voice_settings (stability / similarity_boost / style) cycled per clip. The run
prints a cost estimate and refuses to exceed --max-cost-usd.

Run (training venv — needs only httpx, already a runtime dep):
    python scripts/wake_word_data_eleven.py --out scripts/wake_data \
        --n-per-voice 50 --max-cost-usd 5

NOTE: this makes real, billed API calls. Run it yourself after the audit.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path

# Anchor imports to the project root so `python scripts/wake_word_data_eleven.py`
# works (sys.path[0] would otherwise be scripts/, hiding config/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from config.settings import settings

# Mirror the Piper script's phrases so a model trained on either dataset behaves
# the same (Mexican-Spanish + English wake phrasing).
DEFAULT_PHRASES = ["hey emma", "oye emma", "hola emma"]

# Negatives: things Emma must NOT wake on. A mix of general speech and a few
# near-misses (so the model learns the boundary, not just "any speech").
DEFAULT_NEGATIVE_PHRASES = [
    "hola, ¿cómo estás?", "oye, ¿qué hora es?", "necesito un café",
    "abre el navegador", "pon música por favor", "¿qué tiempo hace hoy?",
    "gracias, eso es todo", "espérame un momento", "vamos a la tienda",
    "hey there, how are you", "open the terminal", "what time is it",
    "play some music please", "see you tomorrow", "let me think about it",
    # near-misses (deliberately close but NOT the wake word):
    "emma", "ey", "hey amma", "oye ema", "hola elena",
]

_SAMPLE_RATE = 16_000  # pcm_16000 → openWakeWord's native format, no resample
_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"

# Cycled per clip for acoustic diversity (ElevenLabs has no Piper-style knobs).
_SETTINGS_GRID = [
    {"stability": 0.30, "similarity_boost": 0.75, "style": 0.00},
    {"stability": 0.50, "similarity_boost": 0.75, "style": 0.30},
    {"stability": 0.65, "similarity_boost": 0.85, "style": 0.15},
    {"stability": 0.40, "similarity_boost": 0.60, "style": 0.45},
]

# Flash v2.5: 0.5 credits/char ≈ $0.00011/char (matches audio_gen.USD_PER_CHAR).
USD_PER_CHAR = 0.00011


class VoiceGenError(RuntimeError):
    """Friendly synthesis failure — never a raw httpx stacktrace."""


def default_voices() -> list[str]:
    """The ES + EN voice IDs from settings (Keychain/.env), de-duplicated."""
    seen: list[str] = []
    for vid in (settings.ELEVENLABS_VOICE_ID_ES, settings.ELEVENLABS_VOICE_ID_EN):
        if vid and vid not in seen:
            seen.append(vid)
    return seen


def _pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw 16 kHz mono 16-bit PCM in a WAV container (stdlib, no ffmpeg)."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


_MAX_RETRIES = 4  # 429 backoff attempts before giving up


class OutOfCreditsError(VoiceGenError):
    """ElevenLabs 402 — distinct so the caller can show a 'recarga' message."""


def _fetch(text: str, voice_id: str, voice_settings: dict,
           on_retry: Callable[[float], None] | None = None) -> bytes:
    """One ElevenLabs synthesis → raw pcm_16000 bytes. The only network seam.

    Retries 429 (rate limit) with exponential backoff rather than failing — the
    run keeps going, just slower. 402 (out of credits) raises OutOfCreditsError.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            resp = httpx.post(
                f"{_API_BASE}/{voice_id}",
                params={"output_format": f"pcm_{_SAMPLE_RATE}"},
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
                json={
                    "text": text,
                    "model_id": settings.ELEVENLABS_MODEL_ID,
                    "voice_settings": voice_settings,
                },
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise VoiceGenError(f"No pude conectar con ElevenLabs: {exc}") from exc
        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 401:
            raise VoiceGenError("ElevenLabs rechazó la API key (401). Revisa ELEVENLABS_API_KEY.")
        if resp.status_code == 402:
            raise OutOfCreditsError("Sin créditos en ElevenLabs. Recarga o usa menos muestras.")
        if resp.status_code == 429:
            if attempt < _MAX_RETRIES - 1:
                wait = float(2**attempt)  # 1, 2, 4 s
                if on_retry:
                    on_retry(wait)
                time.sleep(wait)
                continue
            raise VoiceGenError("ElevenLabs siguió limitando la tasa (429). Intenta más tarde.")
        raise VoiceGenError(f"ElevenLabs devolvió {resp.status_code}: {resp.text[:200]}")
    raise VoiceGenError("ElevenLabs siguió limitando la tasa (429). Intenta más tarde.")


def _synthesize_to(text: str, voice_id: str, voice_settings: dict, out_wav: Path,
                   on_retry: Callable[[float], None] | None = None) -> None:
    out_wav.write_bytes(_pcm_to_wav(_fetch(text, voice_id, voice_settings, on_retry)))


def _plan(phrases: list[str], voices: list[str], n_per_voice: int):
    """Yield (phrase, voice_id, voice_settings, index) for every clip to make."""
    for vname in voices:
        for phrase in phrases:
            for i in range(n_per_voice):
                yield phrase, vname, _SETTINGS_GRID[i % len(_SETTINGS_GRID)], i


def estimate(phrases: list[str], neg_phrases: list[str], voices: list[str],
             n_pos: int, n_neg: int) -> tuple[int, int]:
    """(clip_count, char_count) the run would synthesize — for the cost guard."""
    clips = chars = 0
    for ph, _v, _s, _i in _plan(phrases, voices, n_pos):
        clips += 1
        chars += len(ph)
    for ph, _v, _s, _i in _plan(neg_phrases, voices, n_neg):
        clips += 1
        chars += len(ph)
    return clips, chars


def generate(out: Path, phrases: list[str], neg_phrases: list[str], voices: list[str],
             n_pos: int, n_neg: int,
             progress_cb: Callable[[str, int, int, int], None] | None = None,
             on_retry: Callable[[float], None] | None = None) -> tuple[int, int]:
    """Synthesize every clip into <out>/{positive,negative}. Returns (pos, neg).

    progress_cb(kind, done, total, chars) fires after each clip — kind is
    "positive" or "negative", chars is the cumulative character spend so the
    caller can convert it to a live dollar figure.
    """
    pos_dir = out / "positive"
    neg_dir = out / "negative"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    total_pos = len(voices) * len(phrases) * n_pos
    total_neg = len(voices) * len(neg_phrases) * n_neg
    chars = 0

    n_positive = 0
    for phrase, vid, vset, i in _plan(phrases, voices, n_pos):
        slug = phrase.replace(" ", "_")
        _synthesize_to(phrase, vid, vset, pos_dir / f"{vid}_{slug}_{i:04d}.wav", on_retry)
        n_positive += 1
        chars += len(phrase)
        if progress_cb:
            progress_cb("positive", n_positive, total_pos, chars)

    n_negative = 0
    for phrase, vid, vset, _i in _plan(neg_phrases, voices, n_neg):
        _synthesize_to(phrase, vid, vset, neg_dir / f"neg_{vid}_{n_negative:05d}.wav", on_retry)
        n_negative += 1
        chars += len(phrase)
        if progress_cb:
            progress_cb("negative", n_negative, total_neg, chars)
    return n_positive, n_negative


def main() -> int:
    ap = argparse.ArgumentParser(description="Datos de wake-word vía ElevenLabs (16.2.1).")
    ap.add_argument("--out", type=Path, default=Path("scripts/wake_data"))
    ap.add_argument("--phrases", nargs="+", default=DEFAULT_PHRASES)
    ap.add_argument("--neg-phrases", nargs="+", default=DEFAULT_NEGATIVE_PHRASES)
    ap.add_argument("--voices", nargs="+", default=None, help="voice IDs (default: ES+EN de settings)")
    ap.add_argument("--n-per-voice", type=int, default=50, help="positivos por frase por voz")
    ap.add_argument("--n-neg-per-voice", type=int, default=8, help="negativos por frase por voz")
    ap.add_argument("--max-cost-usd", type=float, default=5.0, help="aborta si excede este costo")
    args = ap.parse_args()

    voices = args.voices or default_voices()
    if not voices:
        print("✗ Sin voces. Define ELEVENLABS_VOICE_ID_ES/_EN o pasa --voices.", file=sys.stderr)
        return 1

    clips, chars = estimate(args.phrases, args.neg_phrases, voices,
                            args.n_per_voice, args.n_neg_per_voice)
    cost = chars * USD_PER_CHAR
    print(f"Plan: {clips} clips, {chars} caracteres ≈ ${cost:.2f} (ElevenLabs cobra por carácter).")
    if cost > args.max_cost_usd:
        print(f"✗ Excede --max-cost-usd (${args.max_cost_usd:.2f}). Baja --n-per-voice.",
              file=sys.stderr)
        return 1

    try:
        n_pos, n_neg = generate(args.out, args.phrases, args.neg_phrases, voices,
                                args.n_per_voice, args.n_neg_per_voice)
    except VoiceGenError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ {n_pos} positivos + {n_neg} negativos en {args.out}")
    print("Siguiente: python scripts/train_wake_word.py --data", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
