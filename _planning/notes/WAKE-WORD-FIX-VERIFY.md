# WAKE-WORD-FIX — verification

**Date:** 2026-07-21 · **Host:** macOS 26.3.1, arm64, Python 3.12.11
**Change:** Vosk → sherpa-onnx `KeywordSpotter` as the shipped wake engine.
See `WAKE-WORD-RESEARCH.md` for the engine decision.

## Headless-verifiable (done on this machine)

| # | Check | Result |
|---|---|---|
| 1 | `vosk` gone from base `pyproject` deps (moved to optional `[vosk]`, Linux-gated) | ✅ |
| 2 | Root `uv sync` completes, `sherpa-onnx==1.13.4` in, `vosk` out | ✅ EXIT 0 |
| 3 | **Clean-venv** `uv sync --frozen` (the fresh-install path) — zero "no wheel" errors | ✅ EXIT 0 |
| 4 | `import sherpa_onnx` works, `KeywordSpotter` present; `import vosk` → ModuleNotFoundError in the clean venv | ✅ |
| 5 | `sherpa-onnx-core` resolved into the lock (the dropped-dynamic-dep fix) | ✅ (3 refs in `uv.lock`) |
| 6 | Real `core/wake_sherpa.py` builds the spotter, generates the tokenized keywords file, decodes wavs | ✅ ("hey emma" fires; all `-ema-` negatives rejected) |
| 7 | `pytest tests/` | ✅ **1036 passed** |
| 8 | `ruff check .` | ✅ All checks passed |
| 9 | `mypy .` | ✅ no issues (172 files) |
| 10 | Backend image unaffected (own `backend/requirements.txt`, no wake deps) | ✅ decoupled |

Reproduce the fresh-install pre-check:

```sh
rm -rf /tmp/emma-cleanvenv
UV_PROJECT_ENVIRONMENT=/tmp/emma-cleanvenv uv sync --frozen   # must exit 0
/tmp/emma-cleanvenv/bin/python -c "import sherpa_onnx; print('OK')"
```

## On-device — Garcia confirms (NOT headless-verifiable)

Detection quality against a live mic + real voice is the acceptance gate, same as
barge-in latency. To run the full E2E that found the bug:

```sh
launchctl bootout gui/$UID/com.emma.daemon 2>/dev/null || true
rm -rf ~/.emma
rm -f ~/Library/LaunchAgents/com.emma.daemon.plist
security delete-generic-password -a default -s com.emma.device-token 2>/dev/null || true

curl -fsSL https://theemmafamily.com/install.sh | sh   # (after landing deploys the fixed install.sh)
```

- [ ] Step 4 (`uv sync`) completes — no "no wheel for this platform".
- [ ] Step 5 downloads the sherpa KWS model to `~/.emma/sherpa-kws/…gigaspeech…`.
- [ ] `say "Emma"` (or "Oye Emma" / "Hey Emma") wakes her.

**Tuning if the bare "Emma" is missed** (expected to need it — the English KWS
model under-detects a pure Spanish /ˈe.ma/; the smoke test fired "hey emma" and
rejected `problema`/`sistema`/`tema` cleanly, but bare "emma" needs real-voice
tuning):
1. Lower the bare "emma" `#threshold` in `core/wake_sherpa.py:WAKE_PHRASES`
   (already the most sensitive at `#0.15`; try `#0.10`).
2. If a word false-triggers, raise that phrase's `#threshold`.
3. Keep `:boost` at `1.0` — higher made it match "problema" in testing.
4. Last resort: A/B the zh-en **phoneme** KWS model (`EMMA → EH1 M AH0`, closer
   to Spanish vowels). See `WAKE-WORD-RESEARCH.md`.

> ⚠️ `_landing/install.sh` lives in the separate landing repo (own Kamal deploy).
> The fix is written but **must be deployed through that channel** before the
> `curl | sh` reinstall picks it up. The daemon source (this repo) ships via the
> GitHub `main` tarball and is already correct.

_Result of the on-device reinstall:_ _(pending Garcia)_
