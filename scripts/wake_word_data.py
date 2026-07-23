#!/usr/bin/env python3
"""Generate wake-word training data (Prompt 16.2) — positives via Piper TTS.

Local-only, no Picovoice, no Colab. Synthesizes many "Hey Emma" / "Oye Emma"
utterances across Piper voices with pitch/speed/noise variation (the positive class),
then prepares the negative/background clips into the layout the trainer expects:

    <out>/positive/*.wav     # synthetic wake-word utterances (16 kHz mono)
    <out>/negative/*.wav     # background / speech that is NOT the wake word

Run (in the training venv — see requirements-train.txt):
    python scripts/wake_word_data.py --out scripts/wake_data \
        --n-per-voice 150 --noise-dir ~/Downloads/background_audio

The negative corpus is yours to point at (--noise-dir): any folder of speech/ambient
audio that does NOT contain "Hey Emma" (podcasts, music, room noise). More + more
varied negatives = fewer false wakes. ``train_wake_word.py`` consumes this layout.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

# Phrases to cover the user's Mexican-Spanish pronunciation as well as English.
DEFAULT_PHRASES = ["hey emma", "oye emma", "hola emma"]
# Piper voices (downloaded on first use). Mix en + es for accent robustness.
DEFAULT_VOICES = ["en_US-amy-medium", "es_MX-claude-high", "es_ES-davefx-medium"]
_SR = 16000


def _ensure_voice(name: str, voices_dir: Path):
    """Download a Piper voice (.onnx + .json) if missing; return the model path."""
    from piper.download import ensure_voice_exists, get_voices

    voices_dir.mkdir(parents=True, exist_ok=True)
    info = get_voices(str(voices_dir))
    ensure_voice_exists(name, [str(voices_dir)], str(voices_dir), info)
    return voices_dir / f"{name}.onnx"


def _synthesize(phrase: str, voice_path: Path, length_scale: float, noise_scale: float,
                noise_w: float, out_wav: Path) -> None:
    from piper.voice import PiperVoice

    voice = PiperVoice.load(str(voice_path))
    with wave.open(str(out_wav), "wb") as wf:
        voice.synthesize(
            phrase, wf,
            length_scale=length_scale, noise_scale=noise_scale, noise_w=noise_w,
        )


def _to_16k_mono(path: Path) -> None:
    """Resample a wav in place to 16 kHz mono int16 (openWakeWord's input format)."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    data, sr = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != _SR:
        data = resample_poly(data, _SR, sr)
    sf.write(str(path), (np.clip(data, -1, 1) * 32767).astype("int16"), _SR, subtype="PCM_16")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate wake-word training data (16.2).")
    ap.add_argument("--out", type=Path, default=Path("scripts/wake_data"))
    ap.add_argument("--phrases", nargs="+", default=DEFAULT_PHRASES)
    ap.add_argument("--voices", nargs="+", default=DEFAULT_VOICES)
    ap.add_argument("--n-per-voice", type=int, default=150, help="positives per voice per phrase")
    ap.add_argument("--noise-dir", type=Path, default=None, help="folder of negative/background audio")
    args = ap.parse_args()

    import random

    pos_dir = args.out / "positive"
    neg_dir = args.out / "negative"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)
    voices_dir = args.out / "_voices"

    # ---- positives: synthesize the phrase with per-utterance prosody variation.
    n = 0
    for vname in args.voices:
        try:
            vpath = _ensure_voice(vname, voices_dir)
        except Exception as exc:  # a missing voice shouldn't sink the whole run
            print(f"⚠ no pude preparar la voz {vname}: {exc}")
            continue
        for phrase in args.phrases:
            for i in range(args.n_per_voice):
                out = pos_dir / f"{vname}_{phrase.replace(' ', '_')}_{i:04d}.wav"
                _synthesize(
                    phrase, vpath,
                    length_scale=random.uniform(0.85, 1.25),   # speed
                    noise_scale=random.uniform(0.55, 0.75),     # timbre jitter
                    noise_w=random.uniform(0.6, 0.9),           # phoneme-length jitter
                    out_wav=out,
                )
                _to_16k_mono(out)
                n += 1
        print(f"✓ {vname}: positivos generados (total {n})")
    print(f"✓ {n} clips positivos en {pos_dir}")

    # ---- negatives: copy/convert the user's background corpus.
    if args.noise_dir and args.noise_dir.is_dir():
        import shutil
        m = 0
        for src in list(args.noise_dir.rglob("*.wav")) + list(args.noise_dir.rglob("*.mp3")):
            dst = neg_dir / f"neg_{m:05d}.wav"
            shutil.copy(src, dst)
            try:
                _to_16k_mono(dst)
                m += 1
            except Exception:
                dst.unlink(missing_ok=True)
        print(f"✓ {m} clips negativos en {neg_dir}")
    else:
        print("⚠ Sin --noise-dir: agrega audio de fondo a "
              f"{neg_dir} antes de entrenar (habla/música/ruido que NO diga 'Hey Emma').")
    print("\nSiguiente: python scripts/train_wake_word.py --data", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
