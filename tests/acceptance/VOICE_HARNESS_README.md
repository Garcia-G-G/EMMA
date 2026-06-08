# Emma voice acceptance harness (19.7)

## 1. What this is

An unattended voice-driven test corpus: ElevenLabs synthesizes each
scenario's utterance, the runner plays it at a **real Emma subprocess**
(wake word included), and collects everything she did — STT transcript,
tool calls with arguments, capability gaps, spoken response, timings —
into a per-run report. Future bug-fix prompts ground themselves in these
recordings instead of one-off live calls.

```
ElevenLabs WAV ──▶ output device ──▶ (BlackHole | speakers→mic) ──▶ Emma
   cached                                                            │
voice_run_<ts>.md ◀── runner ◀── structlog JSON (subprocess) ◀───────┘
```

## 2. One-time setup — BlackHole 2ch (recommended)

[BlackHole](https://existential.audio/blackhole/) is a free, open-source
virtual audio cable: anything written to its output appears on its input.

1. `brew install blackhole-2ch`
2. Leave your normal mic/speakers as system defaults — BlackHole just
   sits beside them in Audio MIDI Setup. *(screenshot placeholder)*
3. Verify:
   `.venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"`
   → "BlackHole 2ch" must appear as both input (2 in) and output (2 out).
4. Add to `.env`:
   ```
   EMMA_TEST_INPUT_DEVICE="BlackHole"
   EMMA_TEST_OUTPUT_DEVICE="BlackHole"
   ```
   (Or pass `--input-device/--output-device BlackHole` per run.)

**Fallback (no BlackHole):** leave both device settings empty. Audio
plays through the speakers and the physical mic picks it up — works, but
it's audible, noisier, and unsuitable for CI. These docs assume the
fallback until BlackHole is installed on this Mac.

> Production note: with `EMMA_TEST_MODE` unset, none of this exists —
> Emma always uses the system default devices.

## 3. Pre-warm the audio cache

```sh
.venv/bin/python -m tests.acceptance.audio_gen --prewarm
# missing: 68 clips, 3210 chars ≈ $0.35
# generated 68, reused 13, skipped 0
```

Idempotent: each unique (text, voice, model) synthesizes exactly once,
then lives at `tests/acceptance/audio_cache/<lang>/<sha16>.wav` forever
(`manifest.json` maps hashes back to text; WAVs are gitignored).

## 4. Run

```sh
# everything
.venv/bin/python -m tests.acceptance.runner --voice

# one scenario / a glob
.venv/bin/python -m tests.acceptance.runner --voice --filter A05
.venv/bin/python -m tests.acceptance.runner --voice --filter 'V4*'

# explicit devices, skip the prewarm step, skip the cost prompt
.venv/bin/python -m tests.acceptance.runner --voice \
    --input-device BlackHole --output-device BlackHole --skip-prewarm --yes
```

Sequential by design (`--max-parallel` must stay 1): the mic / virtual
cable is a singleton. Each scenario spawns a fresh `emma --debug --test`;
`follows:` chains keep the session alive for confirmation flows.

## 5. Reading a report

`tests/acceptance/voice_run_<timestamp>.md`:

- **Summary table** — passed / failed / errored / skipped / total + the
  dollar cost of any fresh synthesis in this run.
- Per scenario: the audio file used, **wake latency**, **STT heard**
  (diff vs the input text = Whisper accuracy snapshot), tool calls with
  real arguments, capability gaps, Emma's spoken reply, turn time
  (measured from utterance-playback end), subprocess exit code, and the
  failed assertions if any.

Statuses: `PASS` · `FAIL` (assertion missed) · `ERROR` (daemon never came
up / playback failed) · `SKIP` (`live_blocked_by`).

## 6. Cost

Flash v2.5 (`eleven_flash_v2_5`) bills **0.5 credits/char** — on the
Creator tier that's ≈ **$0.11 per 1 000 characters**
(elevenlabs.io/docs/models/flash-v2-5). The full 80-scenario corpus is
~3.5k chars ≈ **$0.40 once**; after that every run is **$0.00** thanks to
the cache. The runner estimates uncached chars before synthesizing and
**asks for confirmation when the estimate exceeds $1** (`--yes` skips) —
so an accidentally emptied cache can never silently bill the account.

## 7. Coding-agent scenarios (Prompt 23)

V68 (delegation) and V69 (cost guard) cover `delegate_to_codex` in **mock
mode only** — they assert the routing + the confirmation/cost-guard shape
without spending tokens or touching a repo. A real agent run is its own
thing: drive it manually in a throwaway git repo (`/tmp/agent-smoke`) so no
real project is at risk, e.g. `core.coding_agent.run_agent(task, workdir)`
directly, or by voice "Emma, en /tmp/agent-smoke agrega un párrafo al
README". A trivial task lands around **$0.01** and a handful of iterations.
The transcript is written to `<workdir>/.emma_agent/<task_id>.jsonl`.
