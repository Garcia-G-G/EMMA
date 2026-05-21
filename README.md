# Emma

Emma is a locally-installed, voice-activated AI assistant for macOS. Say "Hey Emma" in Spanish or English; she transcribes, reasons with GPT-4o over a tool registry, executes real actions on the machine, and replies with a streamed ElevenLabs voice. She remembers what happens across conversations and can describe her own capabilities.

## Prerequisites

- macOS 14+ (Apple Silicon)
- Python 3.11+
- `ffmpeg` (`brew install ffmpeg`)
- `brightness` CLI (`brew install brightness`) - needed for the
  `set_brightness` tool
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Chromium for Playwright (`uv run playwright install chromium`)

### Optional integrations

| Tool | Needs |
| --- | --- |
| Music (Spotify) | `SPOTIFY_CLIENT_ID`/`_SECRET`, plus a one-time browser consent at first call. Token cached in `~/.emma/spotify_token.json`. |
| Web search | `BRAVE_API_KEY` (preferred) or `TAVILY_API_KEY`. |
| YouTube | `YOUTUBE_API_KEY` (Google Cloud > YouTube Data API v3). |
| Browser tools | Run `uv run playwright install chromium` once; profile lives at `~/.emma/playwright-profile/`. |

## Install (run-once)

```sh
./installer/install_macos.sh
```

The installer is idempotent: re-run it any time to reload the
LaunchAgent or re-validate `.env`. It prompts you to fill in `.env`
on first run and only proceeds after the file validates.

To uninstall:

```sh
./installer/uninstall_macos.sh
```

## Dev mode (voice-triggered)

Say *"Hey Emma, te voy a debuggear"* (or "abre tu workspace", "dev
mode", "open your codebase", ...). Emma opens a Terminal at the repo
with the current branch, last commit, and the resume command. The
launchd service is disabled until you run that resume command - this
is deliberate, there is no voice-resume tool.

Resume command (also printed in the banner Emma opens):

```sh
launchctl enable gui/$UID/com.garcia.emma && launchctl kickstart -k gui/$UID/com.garcia.emma
```

## Crash handling

Unhandled exceptions write a markdown crash report to
`~/Library/Logs/Emma/crashes/`, auto-open a Terminal that `cat`s the
report, and `say` a short failure message. Three crashes within 60
seconds suppress the auto-open (no terminal storms during crash loops).
Exit code 1 lets launchd restart Emma on the next throttle interval.

For a one-shot test of the crash path:

```sh
uv run python -m emma --simulate-crash
```

## Dev/manual run

If you want to run Emma in a terminal instead of via launchd:

```sh
cp .env.example .env   # then fill in the keys
./installer/bootstrap.sh
uv run python -m emma --debug
```

## Wake word

Emma listens for the custom phrase **"Hey Emma"**. The `.ppn` keyword file
is generated per-user in the Picovoice console - it is not committed.

1. Sign in at https://console.picovoice.ai/
2. Open *Porcupine -> Wake Word -> Train Wake Word*.
3. Phrase: `Hey Emma`. Platform: **macOS (arm64)**. Train and download.
4. Drop the file at `config/wake_words/hey_emma_mac.ppn`.
5. Point `WAKE_WORD_PATH` in `.env` at it (absolute path or
   `config/wake_words/hey_emma_mac.ppn`).

Regenerate the file whenever you move to a new platform - PPNs are
platform-specific.

See `CLAUDE.md` for the full architecture and conventions.
