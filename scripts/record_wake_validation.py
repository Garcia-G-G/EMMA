#!/usr/bin/env python3
"""Record real "Hey Emma" utterances for validation (Prompt 16.2).

The synthetic positives train the model; YOUR real voice validates it and tunes the
threshold. This records a handful of clips from your mic so ``train_wake_word.py`` can
measure detection rate vs. false-wakes on real audio and recommend WAKE_WORD_THRESHOLD.

Run (training venv):
    python scripts/record_wake_validation.py --out scripts/wake_data/validation --n 15

Spanish prompts — say "Hey Emma" (o "Oye Emma") each time it cuenta.
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

_SR = 16000
_SECONDS = 2.0


def _record(seconds: float):
    import numpy as np
    import sounddevice as sd

    rec = sd.rec(int(seconds * _SR), samplerate=_SR, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(rec, dtype="int16").reshape(-1)


def _save(samples, path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(samples.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description="Grabar 'Hey Emma' para validar (16.2).")
    ap.add_argument("--out", type=Path, default=Path("scripts/wake_data/validation"))
    ap.add_argument("--n", type=int, default=15, help="cuántas grabaciones")
    ap.add_argument("--seconds", type=float, default=_SECONDS)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"\nVoy a grabar {args.n} veces «Hey Emma» ({args.seconds:.0f}s cada una).")
    print("Habla normal, como le hablarías a Emma. Enter para empezar cada toma.\n")
    for i in range(args.n):
        input(f"  [{i + 1}/{args.n}] Enter, y di «Hey Emma»…")
        for c in (3, 2, 1):
            print(f"    {c}…", end="", flush=True)
            time.sleep(0.4)
        print(" 🎙  grabando")
        samples = _record(args.seconds)
        _save(samples, args.out / f"real_{i:03d}.wav")
    print(f"\n✓ {args.n} grabaciones en {args.out}")
    print("Úsalas con: python scripts/train_wake_word.py --data scripts/wake_data --validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
