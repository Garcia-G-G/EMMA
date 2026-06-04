#!/usr/bin/env python3
"""Offline A/B for transcription models — run by Garcia, never in CI / the daemon.

Sends one reference clip through two transcription models and prints both
transcripts plus a word-level diff, so Garcia can judge which handles his
accented Spanish (names, numbers) better before we flip
``REALTIME_TRANSCRIPTION_MODEL`` (Bug 19.5-A4).

    # record ~30s of representative speech first, then:
    .venv/bin/python scripts/transcription_ab.py whisper-1 gpt-realtime-whisper

Uses the non-streaming Audio API (audio.transcriptions.create) to stay simple;
the live daemon uses the Realtime path. Reads OPENAI_API_KEY from the env.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

from openai import OpenAI

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "transcription_ab_sample.wav"


def _transcribe(client: OpenAI, model: str, audio: Path) -> str:
    with audio.open("rb") as fh:
        resp = client.audio.transcriptions.create(model=model, file=fh)
    return (resp.text or "").strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: transcription_ab.py <model_a> <model_b>", file=sys.stderr)
        return 2
    model_a, model_b = argv
    if not SAMPLE.exists():
        print(
            f"missing reference clip: {SAMPLE}\nRecord ~30s of speech there first.", file=sys.stderr
        )
        return 1

    client = OpenAI()
    text_a = _transcribe(client, model_a, SAMPLE)
    text_b = _transcribe(client, model_b, SAMPLE)

    print(f"=== {model_a} ===\n{text_a}\n")
    print(f"=== {model_b} ===\n{text_b}\n")
    print("=== word diff (a → b) ===")
    diff = difflib.unified_diff(text_a.split(), text_b.split(), lineterm="", n=2)
    print("\n".join(diff) or "(identical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
