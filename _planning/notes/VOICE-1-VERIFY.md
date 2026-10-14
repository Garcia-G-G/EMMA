# VOICE-1 — verification

Emma now hears, decides and speaks with **no LLM**:

```
wake (sherpa) → Scribe v2 Realtime → core/brain → template / local tool → ElevenLabs v4 Turbo → speaker
```

Released as **v0.4.0** (`23ddc23`) and **v0.4.1** (`2124e6b`), a fix found while
verifying the in-place upgrade. This Mac runs v0.4.1, verified from the
installation, not the repo.

## Premises checked before writing (Pipecat 1.2.1 + the live API)

| Premise (research doc) | What actually exists | Used |
|---|---|---|
| `ElevenLabsRealtimeSTTService`, Scribe v2 | Exists; model `scribe_v2_realtime`; MANUAL commit on `VADUserStoppedSpeakingFrame`; maps 16 kHz → `pcm_16000` | yes, language unset |
| VAD via the transport | **No.** `LocalAudioTransportParams` has no `vad_analyzer` field (silently ignored). Silero only takes 8/16 kHz | explicit `VADProcessor`, mic at 16 kHz |
| `ElevenLabsTTSService` + "v4 Turbo" | `eleven_v4_turbo` exists in the account, but the TTS WebSocket rejects it: `unsupported_model … use the text-to-dialogue websocket` | **`ElevenLabsHttpTTSService`** (`stream/with-timestamps`, word timestamps kept), 24 kHz |
| One transcript per utterance | **No.** The server sends `committed_transcript_with_timestamps` even with timestamps off, and 1.2.1 pushes it unconditionally: two brain turns per utterance (seen live) | `ScribeSTT` subclass drops it |

HTTP TTS first audio, median of 5 lines: v4 Turbo 308 ms, flash v2.5 243 ms,
turbo v2.5 273 ms. Emma speaks whole template sentences, so token streaming
buys nothing.

## Parts

| Part | Commit | What |
|---|---|---|
| 0 | `9074943` | Readiness = mic + wake + ElevenLabs key + embedder (+ voice, Part 4). No OpenAI anywhere in it; the cascade parks on readiness, not pairing |
| 1 | `9c1fa50`, fix `433e258` | The cascade pipeline (`core/cascade.py`); echo reference resampled 24→16 kHz at the gate; barge-in broadcasts an interruption; `_SpokenTap` keeps only the words actually played. The fix: the menubar "Parar" was raising UnboundLocalError on both backends |
| 2 | `9876b80` | `BrainProcessor` executes through a `Host` seam (dispatch + every injection guard); one-word confirmation; `Thinker` protocol + `NoThinker`; A4 BM25 facts, A5 pruned context |
| 3 | `3c31c4c` | `reversible` on `@tool()` (31 tools, each with a reason, set pinned); slot completeness hard-coded on; LLM-backed tool modules unavailable on the cascade; corrections logged and reported |
| — | `4276146` | STT quotes: `'Home'` vanished from the tokens and hid a safety refusal (seen live) |
| 4 | `999664d` | `core/voice.py`: cached clips + draining player; 50 pregenerated; `say` and every regional macOS voice retired |
| 5 | `d245ff7`, fix `2ae4b55` | Onboarding asks for the ElevenLabs key + voice (native fields), OpenRouter optional and labelled "Todavía no se usa"; `~/.emma/settings.env`; installer step 5.6 |
| 6 | `9b3eb77` | Per-turn latency log + `python -m emma.voice_report` |
| — | `23ddc23` | No test can open a live cascade session (it hung the release gate) |
| — | `2124e6b` | v0.4.1: a leftover `OPENAI_API_KEY` short-circuited the Keychain fill and hid the ElevenLabs key |

Full suite at release: **1548 passed**, ruff and mypy clean. The 140 numbers
are unchanged: **55.0 % served / 36.4 % local / 0 wrong**.

## The voice (Part 4, chosen by audition)

Ten candidates from the account, cloned voices excluded. Each spoke Spanish and
English lines through v4 Turbo, and Scribe transcribed them back. Mishearings
below exclude Scribe writing numbers as digits.

| Voice | Labels | Misheard (20 lines) | Language detected | First audio |
|---|---|---|---|---|
| **River** | neutral · american | **0** | 20/20 | ~250 ms |
| Alice | female · british | 1 | 20/20 | ~290 ms |
| Sarah | female · american | 1 | 19/20 | ~250 ms |
| Jessica | female · american | 1 | 19/20 (one as `zh`) | ~260 ms |
| Louise | female · american | 2 | 19/20 (one as `pt`) | ~250 ms |
| Maya (previous ES voice) | female · latin american | 2 | 18/20 (`es` for an EN line, `pt`) | ~250 ms |

**Chosen: River** (`SAz9YHcvj6GT2YYXdXww`), in `~/.emma/settings.env` on this
Mac. It is not shipped: `VOICE_ID` has no default. Runner-up: Alice.

Cache: 50 clips rendered in 64 s (7.4 MB, 0700); a re-run renders 0. Drain:
`speak_sync` on a 2.72 s clip returned after 3.98 s, so nothing is cut off.

## Measured on the live services (synthesized speech, `scripts/voice/probe.py`)

| Turn | Verdict | End of speech → first audio |
|---|---|---|
| "Emma, abre Safari." (executed: `open_application`, 104 ms) | local | 682 ms |
| "Emma, abre Safari." (Part 1, not executed) | local | 496 ms |
| "Hey Emma, ¿cómo estás?" | none (template) | 614 ms |
| "Open Spotify." | defer (`disagree`) | 863 ms |
| "Emma, borra mi carpeta home completa." | refuse | 581 ms |
| "Emma, resume mis correos de hoy." (cached "not yet" line) | defer → NoThinker | 348 ms |

A later run under heavy swap (16 of 17 GB used) took 0.8–2.7 s. In isolation
the same decisions took 17–73 ms, so that slowdown is memory contention, not
the pipeline.

**DoD 2's "under 1 s wake→audio" can't be measured with synthesized input.**
Wake → first audio includes the time you spend saying the command. End of
speech → first audio was 0.35–0.86 s on a machine that wasn't swapping. The
number that counts comes from your own turns (below).

## Release (runbook, in place)

- Source: `public/main` = `2124e6b`; the tag tarballs serve the new files.
- Edge gate, with no cache-busting: the step 5.2 and 5.6 lines are present,
  `ThrottleInterval`, `EmmaDaemon`, `install.json` and `tccutil` (uninstall) are
  all non-zero, `text/x-shellscript`, `cf-cache-status: DYNAMIC`, and it pins
  `EMMA_DEFAULT_VERSION="v0.4.1"`.
- This Mac: backed up (`~/Desktop/emma-backup-2026-10-13-1929.tgz`, with
  dictionary.toml, memory.db and brain.db), upgraded in place, and
  `emma.permissions check` shows:
  `Voice: cascade … Ready: yes` and commit `2124e6b`, "up to date: yes". The
  daemon went from `awaiting_onboarding` (parked since v0.3.x) to
  `waiting_for_wake`.

## Definition of done

| # | Item | Status |
|---|---|---|
| 1 | Key in Keychain; readiness without OpenAI; leaves the park | **met.** Live: `waiting_for_wake` with no OpenAI key |
| 2 | "abre Safari" → opens → says so, <1 s | **met for the action and the speech** (probe, executed); latency pending your turns |
| 3 | Delete with spoken confirmation; "no" cancels | **met in tests** through the real dispatch + guard; not run live (it would delete a real note) |
| 4 | Deferred → "not yet" + full record | **met** (probe + tests) |
| 5 | Reversible set, availability, slots undisableable | **met** (`tests/test_local_safety.py`) |
| 6 | `say` gone; cached player; no cutoff | **met** (drain measured; a test bans `say`) |
| 7 | Release, edge, upgrade in place | **met** (v0.4.0 + v0.4.1) |
| 8 | Latency, spend, first-day report | **pending: needs a day of your real use** |

## Pending: Part 6 with your voice (it's measured automatically)

1. Use Emma normally. Every turn writes to `~/.emma/voice_latency.jsonl` and
   `~/.emma/brain_decisions.jsonl`.
2. After 30 turns, and again after a day:
   ```sh
   cd ~/.emma/src && ~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon -m emma.voice_report --since 2026-10-14T00:00
   ```
   It prints wake → audio and end of speech → audio (p50/p95), minutes of use,
   ElevenLabs credits and $/min (at $0.22/1k, Creator list price; pass
   `--usd-per-1k-credits` with your plan's price), and the brain report with
   corrections.
3. Start `--since` after today: today's credits include the voice auditions
   and the probes.

## Known gaps and risks

- **Remembering doesn't run locally yet.** `memory_tool` calls OpenAI
  embeddings and a gpt-4o-mini contradiction check, so on the cascade it's
  unavailable and "recuerda que…" defers. Memory needs the local e5 embedder.
- **Scribe misdetected a short synthesized line** ("¿cómo estás?" came back as
  Dutch "hoe is het"), and Emma answered the "not yet" line in English. The
  language is left unset on purpose (code-switching); watch it with a real
  voice.
- "Open Spotify." deferred (`disagree`): a retrieval gap in the brain, not a
  pipeline one.
- Barge-in is tested with frames, not acoustically: interrupting Emma
  mid-sentence on the speakers needs a person.
- The native dialogs (secure field, voice picker) weren't clicked through; the
  logic behind them is tested.
- After the in-place install the daemon logged `ax_denied` /
  `screen_recording_denied` (the app bundle is rebuilt at step 5.5). It was
  there before the cascade; screen tools are LLM-backed and unavailable anyway.
- Finder automation and Full Disk Access are still pending (as on v0.3.1).
