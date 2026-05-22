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

Emma listens for the custom phrase **"Hey Emma"**, detected by a
locally-loaded ONNX model produced via
[openWakeWord](https://github.com/dscripka/openWakeWord). The model is
trained once by the user via a Google Colab notebook - it does not ship
with the repo.

### Train your "Hey Emma" model

1. Open the official custom-model training notebook:
   `https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb`
2. Set the target wake word to `hey emma`. Lowercase, single space.
3. Run all cells. The notebook will:
   - Generate ~10k synthetic samples of the phrase via Piper TTS with varied voices.
   - Generate ~10k negative samples (other speech, ambient noise).
   - Train an ONNX model for ~50 epochs (~30-45 min on the free Colab GPU).
4. Download the resulting `hey_emma.onnx` file.
5. Place it at `config/wake_words/hey_emma.onnx` in this repo (the
   folder is gitignored).
6. In `.env`, set `WAKE_WORD_PATH=config/wake_words/hey_emma.onnx`.
   Leave `WAKE_WORD_NAME=hey_emma` and `WAKE_WORD_THRESHOLD=0.5` unless
   you have a reason to tune them.

### Tuning notes

- If you get false positives (Emma triggers when you weren't talking to
  her), raise `WAKE_WORD_THRESHOLD` to 0.6 or 0.7.
- If you get false negatives (you said "Hey Emma" and nothing
  happened), lower it to 0.4. If that still misses, retrain with more
  synthetic samples or with audio of your own voice mixed in
  (advanced).
- The `.onnx` is platform-independent. The same file works on Mac,
  Linux, Windows.

See `CLAUDE.md` for the full architecture and conventions.
