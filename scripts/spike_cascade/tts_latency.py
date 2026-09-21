"""Measurement 2 (TTS stage): time to FIRST audio, not to completion.

  .venv/bin/python tts_latency.py kokoro 20
  ELEVENLABS_API_KEY=... .venv/bin/python tts_latency.py eleven 20

Kokoro: local int8 ONNX, time until the first streamed chunk is ready.
ElevenLabs: streaming endpoint, time until the first audio byte arrives.
Short Emma-style replies; the character budget for ElevenLabs is printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPLIES = [
    "Listo, abrí Spotify y puse Bad Bunny.",
    "Son las tres y cuarto en Tokio.",
    "Ya quedó la nota Compras con leche y pan.",
    "Tienes dos eventos hoy; el siguiente es a las cinco.",
    "Done, I opened the repo in Cursor.",
]


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


async def kokoro(n: int) -> list[float]:
    from kokoro_onnx import Kokoro

    m = HERE / "data" / "models"
    t0 = time.perf_counter()
    k = Kokoro(
        str(m / os.environ.get("KOKORO_MODEL", "kokoro-v1.0.int8.onnx")),
        str(m / "voices-v1.0.bin"),
    )
    print(f"load {time.perf_counter() - t0:.2f}s")
    out = []
    for i in range(n + 1):  # first run is warmup, discarded
        text = REPLIES[i % len(REPLIES)]
        lang, voice = (
            ("en-us", "af_heart") if text.startswith("Done") else ("es", "ef_dora")
        )
        t = time.perf_counter()
        async for _samples, _sr in k.create_stream(
            text, voice=voice, speed=1.0, lang=lang
        ):
            dt = time.perf_counter() - t
            break
        if i:
            out.append(dt)
    return out


def eleven(n: int) -> list[float]:
    key = os.environ["ELEVENLABS_API_KEY"]
    voice = "EXAVITQu4vr4xnSDxMaL"  # premade female voice
    out, chars = [], 0
    for i in range(n):
        text = REPLIES[i % len(REPLIES)]
        chars += len(text)
        body = json.dumps({"text": text, "model_id": "eleven_flash_v2_5"}).encode()
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_24000",
            data=body,
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        t = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read(1)
            out.append(time.perf_counter() - t)
            r.read()
    print(f"characters spent: {chars}")
    return out


if __name__ == "__main__":
    eng, n = sys.argv[1], int(sys.argv[2])
    xs = asyncio.run(kokoro(n)) if eng == "kokoro" else eleven(n)
    print(
        f"{eng}: first-audio p50 {q(xs, 0.5) * 1000:.0f} ms, p95 {q(xs, 0.95) * 1000:.0f} ms, "
        f"min {min(xs) * 1000:.0f}, max {max(xs) * 1000:.0f} (n={len(xs)})"
    )
    res = HERE / "results"
    res.mkdir(exist_ok=True)
    json.dump(xs, open(res / f"m2_tts_{eng}.json", "w"))
