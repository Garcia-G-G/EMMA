"""Emma's one voice outside a session: cached ElevenLabs clips, played to the end (VOICE-1 Part 4).

Replaces every ``say -v Mónica`` (and its regional default). Three pieces:

* **Cache** — ``~/.emma/voice_cache/<sha256(model|voice|text)>.wav`` (24 kHz
  mono PCM) plus a ``.json`` with the word timings, so the in-session TTS can
  serve a cached template with the same "what she actually said" record a
  streamed one has. The key includes voice and model: changing either never
  plays a stale clip.
* **Player** — ``afplay`` on the cached WAV. It returns only after the audio
  has drained, so a blocking caller (the permissions bootstrap) can never cut a
  sentence off mid-word: the "dale perm.t." bug. A crashing process can
  ``Popen`` it and die; the clip still plays.
* **Pregeneration** — the 50 most frequent phrasings (``pregenerated()``),
  rendered once with the chosen voice. Cache misses synthesize and fill.

Only fixed phrasings are cached: system lines and slot-free templates. A tool
result ("Hoy tienes: ...", a note's text) is spoken but never written to disk.

No ElevenLabs key or no ``VOICE_ID`` (a fresh install before onboarding): no
clip can exist, so ``speak`` prints the line instead and returns False. There is
deliberately no ``say`` fallback: no second voice, no assumed accent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from base64 import b64decode
from collections.abc import Iterable, Sequence
from pathlib import Path

import structlog

from config.settings import settings

log = structlog.get_logger("emma.voice")

CACHE_DIR = Path.home() / ".emma" / "voice_cache"
RATE_HZ = 24000
PREGENERATE_N = 50
NOT_CONFIGURED = 3  # `pregenerate` exit code: no key/voice yet (the installer tells them apart)
_TIMEOUT_S = 20.0

# Spoken outside a session by the crash handler. Fixed text, so it pregenerates:
# a crashing daemon must not depend on the network to say why.
CRASH_LINE = "Tuve un error, dejé los detalles en una terminal."


def _key(text: str, voice: str, model: str) -> str:
    return hashlib.sha256(f"{model}|{voice}|{text}".encode()).hexdigest()


def clip_path(text: str, voice: str | None = None, model: str | None = None) -> Path:
    return CACHE_DIR / f"{_key(text, voice or settings.VOICE_ID, model or settings.ELEVENLABS_TTS_MODEL)}.wav"


def ready() -> bool:
    """Can Emma synthesize at all? (A cached clip can still play without this.)"""
    return bool(settings.ELEVENLABS_API_KEY and settings.VOICE_ID)


def cached(text: str) -> tuple[bytes, list[tuple[str, float]]] | None:
    """(24 kHz PCM, word timings) for ``text`` in the current voice, or None."""
    path = clip_path(text)
    try:
        with wave.open(str(path), "rb") as w:
            pcm = w.readframes(w.getnframes())
        words = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        return pcm, [(str(a), float(b)) for a, b in words]
    except (OSError, ValueError, wave.Error):
        return None


def store(text: str, pcm: bytes, words: Sequence[tuple[str, float]]) -> Path:
    """Write a clip atomically (tmp + rename): a half-written clip never plays."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        CACHE_DIR.chmod(0o700)  # personal tier
    path = clip_path(text)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE_HZ)
        w.writeframes(pcm)
    path.with_suffix(".json").write_text(json.dumps(list(words)), encoding="utf-8")
    tmp.replace(path)
    return path


def synthesize(text: str) -> tuple[bytes, list[tuple[str, float]]]:
    """One HTTP call to ElevenLabs (stdlib only: usable from a crash path or a thread)."""
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{settings.VOICE_ID}/with-timestamps"
           f"?output_format=pcm_{RATE_HZ}")
    body = json.dumps({"text": text, "model_id": settings.ELEVENLABS_TTS_MODEL}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": settings.ELEVENLABS_API_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        data = json.loads(r.read())
    pcm = b64decode(data["audio_base64"])
    return pcm, _words(data.get("alignment") or {})


def _words(alignment: dict[str, list[object]]) -> list[tuple[str, float]]:
    chars = [str(c) for c in alignment.get("characters", [])]
    starts = [float(str(t)) for t in alignment.get("character_start_times_seconds", [])]
    out: list[tuple[str, float]] = []
    word, t0 = "", 0.0
    for c, t in zip(chars, starts, strict=False):
        if c.isspace():
            if word:
                out.append((word, t0))
            word = ""
        else:
            if not word:
                t0 = t
            word += c
    if word:
        out.append((word, t0))
    return out


def ensure(text: str) -> Path | None:
    """The clip for ``text``: from the cache, or synthesized into it. None if impossible."""
    path = clip_path(text)
    if path.exists():
        return path
    if not ready():
        return None
    try:
        pcm, words = synthesize(text)
    except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
        log.warning("voice_synth_failed", error=str(exc)[:200])
        return None
    return store(text, pcm, words)


def _play(path: Path, wait: bool) -> bool:
    try:
        if wait:
            subprocess.run(["afplay", str(path)], check=False, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        log.warning("voice_play_failed", error=str(exc)[:200])
        return False
    return True


def speak_sync(text: str, *, wait: bool = True, keep: bool = True) -> bool:
    """Speak ``text`` in Emma's voice. ``wait=True`` returns after the audio DRAINED.

    ``keep=False`` for a line that must not land on disk past its playback (a
    pairing code, a timer's label): synthesized to a temp clip, played, deleted.
    False if no clip could be had (no key/voice yet, offline on a miss): the
    line is printed instead, so an installer terminal still shows it.
    """
    text = text.strip()
    if not text:
        return False
    if keep or clip_path(text).exists():
        path = ensure(text)
        if path is None:
            print(f"  Emma: {text}", file=sys.stderr)
            return False
        return _play(path, wait)
    if not ready():
        print(f"  Emma: {text}", file=sys.stderr)
        return False
    import tempfile

    try:
        pcm = synthesize(text)[0]
    except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
        log.warning("voice_synth_failed", error=str(exc)[:200])
        print(f"  Emma: {text}", file=sys.stderr)
        return False
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "line.wav"
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE_HZ)
            w.writeframes(pcm)
        return _play(tmp, wait=True)  # the temp dir goes away: must finish first


def play_cached(text: str) -> bool:
    """Fire-and-forget a clip ONLY if it's already cached — no network, no wait.

    For the crash path: a dying process can't afford a 20 s HTTP timeout, and
    the detached afplay outlives it.
    """
    path = clip_path(text.strip())
    return path.exists() and _play(path, wait=False)


async def speak(text: str, *, keep: bool = True) -> bool:
    """Async ``speak_sync``: off the event loop, still returns after the drain."""
    return await asyncio.to_thread(lambda: speak_sync(text, keep=keep))


def pregenerated() -> list[str]:
    """The 50 phrasings worth rendering ahead of time, most frequent first.

    Frequency proxy until the live log has data: the system lines first (they
    play when no session or network is guaranteed), then the slot-free template
    phrasings in the order of their verdict's share on the 140 (defer -> "not
    yet" first), Spanish before English within a key.
    """
    from core.brain.respond import fixed_phrasings
    from core.permissions import spoken_lines

    lines: list[str] = [*spoken_lines(), CRASH_LINE]
    lines += [text for _key_, _lang, text in fixed_phrasings()]
    seen: set[str] = set()
    out = [t for t in lines if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
    return out[:PREGENERATE_N]


def pregenerate(texts: Iterable[str] | None = None) -> tuple[int, int]:
    """Render missing clips. Returns (already cached, newly rendered)."""
    have = made = 0
    for t in texts if texts is not None else pregenerated():
        if clip_path(t).exists():
            have += 1
        elif ensure(t) is not None:
            made += 1
    return have, made


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="core.voice", description="Emma's cached voice clips.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pregenerate", help=f"render the {PREGENERATE_N} most frequent phrasings")
    speak_p = sub.add_parser("speak", help="speak a line in Emma's voice (cached)")
    speak_p.add_argument("text")
    a = ap.parse_args(argv)
    if a.cmd == "pregenerate":
        if not ready():
            print("No ElevenLabs key or VOICE_ID yet; skipping (onboarding renders them).")
            return NOT_CONFIGURED
        have, made = pregenerate()
        print(f"voice clips: {have} cached, {made} rendered ({have + made}/{PREGENERATE_N})")
        return 0
    return 0 if speak_sync(a.text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
