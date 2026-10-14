# Emma

> A bilingual (Spanish / English) voice assistant for macOS. The wake word and the decisions run on your Mac; ElevenLabs, with your own key, is how she hears and speaks.

![platform](https://img.shields.io/badge/platform-macOS%2014%2B%20·%20Apple%20Silicon-black)
![voice](https://img.shields.io/badge/voice-ElevenLabs%20(your%20key)-blue)
![privacy](https://img.shields.io/badge/secrets-macOS%20Keychain-green)

Say **"Emma"** in Spanish or English and ask for something. Emma transcribes you with ElevenLabs Scribe, decides on your Mac with her own local brain (no language model), runs the matching action (open an app, set a timer, add a note or reminder, play music, check your calendar) and answers in the voice you chose. What she can't do yet, she says so. See [what changed](CHANGELOG.md).

## Install

```sh
curl -fsSL https://theemmafamily.com/install.sh | sh
```

You need macOS 14+ on Apple Silicon and an **ElevenLabs API key**
(elevenlabs.io → Developers → API Keys). You pay ElevenLabs for what you use.

The installer sets up Python, downloads the wake-word and brain models, and asks
for every macOS permission Emma needs up front. Emma's window then asks for your
ElevenLabs key (stored in the macOS Keychain) and lets you pick her voice. There
is no account and nothing to pair.

To uninstall: `curl -fsSL https://theemmafamily.com/uninstall.sh | sh`.

---

## What works locally

The brain resolves a request with no model at all, or it doesn't act:

- **Runs on your Mac:** opening and switching apps, timers, notes, reminders,
  calendar reads, music, the date and time, your files (find and open), GitHub
  lookups (with a token), Emma's own settings and history. Anything that can't be
  undone asks you first, out loud, and a one-word "sí" / "no" answers it.
- **Not yet:** anything that needs a model to think: remembering new facts by
  voice, summarizing, web research, reading your screen. Emma tells you it's not
  something she can do yet, and the request is logged so the gaps are visible.

## How it works

```
"Emma"  ──►  local wake word (sherpa-onnx, offline)  ──►  chime
                                   │
                                   ▼
   mic ─► Scribe v2 Realtime ─► local brain ─► template or local tool
          (ElevenLabs, STT)     (no LLM)               │
                                                       ▼
   speaker ◄── cached clip or ElevenLabs TTS ◄── what to say
                                   │
                    idle timeout ──┘──► back to listening
```

No audio leaves the Mac until the wake word fires. After that, audio goes to
ElevenLabs with your key, and text comes back; nothing passes through a server of
ours. Secrets live only in the Keychain, personal data in `~/.emma/`. See
[`SECURITY.md`](SECURITY.md).

## Prerequisites

- macOS 14+ on Apple Silicon
- An **ElevenLabs API key**
- To run from source: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)

### Optional integrations

| Integration | Needs |
| --- | --- |
| Music (Spotify) | `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, plus a one-time browser consent on first call. Token cached in `~/.emma/spotify_token.json`. |
| Web search | `BRAVE_API_KEY` (preferred) or `TAVILY_API_KEY`. Realtime backend only for now: on the default voice it needs a model to read the results. |
| YouTube | `YOUTUBE_API_KEY` (Google Cloud → YouTube Data API v3). |
| Browser tools | Run `uv run playwright install chromium` once; profile lives at `~/.emma/playwright-profile/`. |

## Run from source (dev/manual)

The hosted installer above is the supported path. To run from a clone instead:

```sh
uv sync                                   # create the venv, install deps
uv run python -m emma.brain fetch-model   # the local brain's embedder
uv run python -m emma --debug             # the menubar window asks for the key + voice
```

The installer is idempotent — re-running it upgrades in place, reloads the
LaunchAgent, and re-requests every macOS permission Emma needs **up front**
(Microphone, Automation, Accessibility, Screen Recording; Full Disk Access and
Calendars open their Settings pane) so nothing surprises you mid-conversation.

Real-time dashboard (optional):

```sh
uv run python dashboard/server.py   # http://localhost:3200
```

## Dev mode (voice-triggered)

Say *"Hey Emma, te voy a debuggear"* (or "abre tu workspace", "dev mode", "open your codebase", …). Emma opens a Terminal at the repo with the current branch, last commit, and the resume command. The launchd service stays disabled until you run that resume command — this is deliberate; there is no voice-resume tool.

Resume command (also printed in the banner Emma opens):

```sh
launchctl enable gui/$UID/com.emma.daemon && launchctl kickstart -k gui/$UID/com.emma.daemon
```

## Wake word

Say **"Emma"** — also **"Oye Emma"**, **"Hey Emma"**, "Hola Emma", or "Ey Emma"
— detected **fully offline** by a [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
`KeywordSpotter`. The installer downloads a small keyword-spotting model to
`~/.emma/sherpa-kws` on first run — there is nothing to train, no account, and no
API key. The spotter runs a streaming beam search restricted to the wake phrases,
so it only ever emits a wake phrase or stays silent: ordinary speech, including
Spanish words that share the `-ema-` sound (tema, sistema, problema), is rejected
without a loose substring match. **No audio leaves the Mac until a wake phrase
fires.**

The wake phrases live in `core/wake_sherpa.py` (`WAKE_PHRASES`), each with its own
trigger threshold — the bare one-word "Emma" (the primary target, and the hardest
to spot cleanly) gets the most sensitive setting. Accent detection is empirical:
if the bare "Emma" is missed or a word false-triggers, tune the per-phrase
thresholds there, or `SHERPA_KWS_*` in `.env`.

### Advanced: other engines

`WAKE_WORD_ENGINE` also accepts `openwakeword` (point `WAKE_WORD_PATH` at a trained
`.onnx`; tune `WAKE_WORD_THRESHOLD`), `pvporcupine` (needs a `.ppn` +
`PICOVOICE_ACCESS_KEY`), or the legacy `vosk` — **Linux/Intel only**, since Vosk
publishes no macOS Apple-Silicon wheel (install it explicitly with `.[vosk]`). The
shipped default is `sherpa`.

## Security & privacy

Emma keeps your data in three trust tiers:

| Tier | Examples | Where it lives |
| --- | --- | --- |
| **Public** | capabilities, version | plaintext on disk |
| **Personal** | preferences, profile facts, schedule | `~/.emma/memory.db` (FileVault + owner-only perms) |
| **Secret** | API keys, tokens, passwords | **macOS Keychain only** (`com.garcia.emma`[^kc]) |

[^kc]: A frozen internal Keychain service identifier, not a personal reference — renaming it would orphan every existing user's stored secrets.

No secret value ever lands in `memory.db`, in logs, or in the model prompt. Full threat model and conventions in [`SECURITY.md`](SECURITY.md).

## Crash handling

Unhandled exceptions write a markdown crash report to `~/Library/Logs/Emma/crashes/`, auto-open a Terminal that `cat`s the report, and play a short failure line in Emma's own voice (a cached clip). Three crashes within 60 seconds suppress the auto-open (no terminal storms during crash loops). Exit code 1 lets launchd restart Emma on the next throttle interval.

One-shot test of the crash path:

```sh
uv run python -m emma --simulate-crash
```

## Development

```sh
uv sync                                          # install deps
uv run python -m pytest tests/ -v                # run tests
uv run python tests/acceptance/runner.py --mock-external   # acceptance suite, no APIs needed
uv run ruff check . && uv run ruff format .      # lint + format
uv run mypy .                                    # type check
```
