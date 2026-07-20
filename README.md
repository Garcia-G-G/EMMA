# Emma

> A bilingual (Spanish 🇲🇽 / English 🇺🇸), voice-activated AI assistant for macOS — fully local wake word, real actions on your machine, long-term memory, and a privacy-first secret store.

![platform](https://img.shields.io/badge/platform-macOS%2014%2B-black)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![voice](https://img.shields.io/badge/voice-OpenAI%20Realtime%20API-10a37f)
![privacy](https://img.shields.io/badge/secrets-macOS%20Keychain-green)

Say **"Hey Emma"** in Spanish or English. Emma opens a low-latency audio-to-audio session, reasons over a registry of **180+ tools**, executes real actions on your Mac — open apps, play music, search the web, manage Calendar/Mail/Notes/Reminders, run shell commands, control the browser — and replies in a natural voice. She remembers what matters across conversations and can describe her own capabilities out loud.

## Install

```sh
curl -fsSL https://theemmafamily.com/install.sh | sh
```

macOS 14+ (Apple Silicon). The installer downloads the wake-word model, sets up
the Python environment, and requests every macOS permission Emma needs up front.
Then pair your Mac at [theemmafamily.com/pair](https://theemmafamily.com/pair).

To uninstall: `curl -fsSL https://theemmafamily.com/uninstall.sh | sh`.

---

## Highlights

- 🎙️ **Local wake word** — "Hey Emma" / "Oye Emma" detected on-device, offline, via a [Vosk](https://alphacephei.com/vosk/) Spanish model. No audio leaves the machine until the wake phrase fires.
- ⚡ **Audio-to-audio** — a single [Pipecat](https://github.com/pipecat-ai/pipecat) pipeline over the **OpenAI Realtime API**: audio in → reasoning + tool calls → audio out, no separate STT/TTS hop.
- 🧰 **180+ tools** — apps, music (Spotify), web/YouTube search, browser automation (Playwright), shell, screen brightness, and native macOS integrations (Calendar, Mail, Messages, Notes, Reminders, Safari, Finder, Music) via AppleScript.
- 🧠 **Long-term memory** — a local SQLite fact store with semantic recall (sqlite-vec embeddings) and automatic deduplication. Emma primes each session with what she knows about you.
- 🔐 **Privacy-first by design** — secrets (API keys, tokens) live **only in the macOS Keychain**; personal data sits in `~/.emma/` behind FileVault and owner-only permissions; nothing secret ever reaches logs or the model prompt. See [`SECURITY.md`](SECURITY.md).
- ✅ **Safe destructive actions** — anything irreversible (delete, send, overwrite) goes through a spoken two-phase confirmation flow.
- 🪄 **Background tasks** — long-running work (installs, builds, delegated coding jobs) runs in the background and notifies you when done, so you can walk away.
- 🛰️ **Live dashboard + 3D visualizer** — a real-time web dashboard and an optional JARVIS-style wireframe HUD wired to live pipeline events.
- 🔁 **Runs as a service** — installs as a launchd agent that survives reboots, with a voice-triggered dev mode and self-recovering crash handling.

## How it works

```
"Hey Emma"  ──►  local wake word (Vosk, offline)  ──►  ack chime
                                                          │
                                                          ▼
        ┌──────────────── Pipecat pipeline ────────────────┐
        │  mic ─► OpenAI Realtime (audio + reasoning) ─► speaker │
        │                     │                              │
        │                     ▼                              │
        │              tool registry (180+)                  │
        │         shell · apps · web · macOS · memory         │
        └────────────────────────────────────────────────────┘
                                                          │
                                       idle timeout  ◄────┘
                                            │
                                            ▼
                                  back to listening for "Hey Emma"
```

Emma runs an infinite loop: wait for the wake word, open a Realtime session, dispatch tool calls as they arrive, and return to listening when the session goes idle. Tool results are pushed back into the conversation so Emma can speak a natural follow-up.

## Prerequisites

- macOS 14+ (Apple Silicon)
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- `ffmpeg` (`brew install ffmpeg`)
- `brightness` CLI (`brew install brightness`) — for the `set_brightness` tool
- Chromium for Playwright (`uv run playwright install chromium`)
- An **OpenAI API key** with Realtime API access

### Optional integrations

| Integration | Needs |
| --- | --- |
| Music (Spotify) | `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, plus a one-time browser consent on first call. Token cached in `~/.emma/spotify_token.json`. |
| Web search | `BRAVE_API_KEY` (preferred) or `TAVILY_API_KEY`. |
| YouTube | `YOUTUBE_API_KEY` (Google Cloud → YouTube Data API v3). |
| Browser tools | Run `uv run playwright install chromium` once; profile lives at `~/.emma/playwright-profile/`. |

## Run from source (dev/manual)

The hosted installer above is the supported path. To run from a clone instead:

```sh
cp .env.example .env        # then fill in the keys
uv sync                     # create the venv, install deps
uv run python -m emma --debug
```

The installer is idempotent — re-running it upgrades in place, reloads the
LaunchAgent, and re-requests every macOS permission Emma needs **up front**
(Microphone, Automation, Accessibility, Full Disk Access) so nothing surprises
you mid-conversation.

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

Emma answers to **"Hey Emma"** and **"Oye Emma"** (also "Hola Emma", "Ey Emma",
or a bare "Emma"), detected **fully offline** by a [Vosk](https://alphacephei.com/vosk/)
Spanish model. The installer downloads it to `~/.emma/vosk` on first run — there
is nothing to train and no model ships in the repo. The recognizer is
grammar-constrained to the wake phrases (tuned for Mexican-Spanish and English
phrasing), so it rejects anything that isn't a wake phrase instead of forcing a
match. **No audio leaves the Mac until a wake phrase fires.**

### Advanced: swap in a custom openWakeWord model

If you'd rather use a trained [openWakeWord](https://github.com/dscripka/openWakeWord)
ONNX model, set `WAKE_WORD_ENGINE=openwakeword` and point `WAKE_WORD_PATH` at your
`.onnx` in `.env`. Tune `WAKE_WORD_THRESHOLD` (0.4 = more sensitive, 0.6–0.7 =
fewer false positives). This path is optional; the shipped default is Vosk.

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

Unhandled exceptions write a markdown crash report to `~/Library/Logs/Emma/crashes/`, auto-open a Terminal that `cat`s the report, and `say` a short failure message. Three crashes within 60 seconds suppress the auto-open (no terminal storms during crash loops). Exit code 1 lets launchd restart Emma on the next throttle interval.

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
