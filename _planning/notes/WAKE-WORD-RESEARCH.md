# Wake-word engine research — replacing Vosk (uninstallable on Apple Silicon)

**Date:** 2026-07-21 · **Trigger:** fresh `install.sh` E2E on macOS 26.3.1 arm64
failed at `uv sync` — `vosk==0.3.45` has no macOS wheel and no sdist.

**Verdict up front:** ship **sherpa-onnx `KeywordSpotter`** (gigaspeech English
KWS model). It is the only installable engine that faithfully reproduces Vosk's
grammar-mode discipline — a fixed keyword list the decoder may only ever emit —
with **per-keyword thresholds**, **no training**, and **no keys**, and it has
native macOS arm64 wheels. Detection quality for a one-word Spanish "Emma" is
empirically tunable on-device (as it was for Vosk); no installable engine ships a
Spanish-native acoustic model, so that limitation is shared by every option.

---

## The blocker (verified against live PyPI, 2026-07-21)

`vosk` latest = 0.3.45. Its only artifacts:

```
vosk-0.3.45-py3-none-linux_armv7l.whl
vosk-0.3.45-py3-none-manylinux2014_aarch64.whl        (Linux ARM, NOT mac)
vosk-0.3.45-py3-none-manylinux_2_12_x86_64...whl
vosk-0.3.45-py3-none-win_amd64.whl
```

No `macosx_*` wheel, **no `.tar.gz` sdist** → pip/uv cannot build it → `uv sync`
hard-fails on Apple Silicon. `required-environments` cannot fabricate a wheel; it
only changes the error. This is confirmed on-device and by the package index.

### Premises checked against the code (as the prompt required)

| Prompt claim | Verdict | Evidence |
|---|---|---|
| `pyproject.toml:10` pins `vosk>=0.3.44` | ✅ true | was line 10 |
| `core/speech_wake.py` imports vosk | ✅ true | lazy imports inside `_get_model`/`listen` |
| `WAKE_WORD_ENGINE` is already pluggable | ✅ true | `core/wake_word.py:listen_for_wake_word` dispatches |
| `WAKE_WORD_PATH` → `models/hey_emma.onnx` "does not exist" | ⚠️ **partly** | it exists on the maker's disk (untracked) but `.gitignore:9 = models/` excludes it, so it does **not** ship in the tarball → true for a fresh install |
| `pvporcupine` is a "stub" in settings | ⚠️ **false** | `_listen_porcupine` is fully implemented and `pvporcupine>=4.0.2` is even a base dep (`pyproject.toml:38`) |
| Backend uses vosk | ✅ false (as expected) | `grep -rn vosk backend/` → nothing |
| CLAUDE.md says openWakeWord | ✅ true, **and self-contradictory** | arch note said openWakeWord; Distribution note said Vosk — the code had drifted to Vosk |

---

## Candidate comparison

| | install clean arm64? | one-word "Emma" **without training**? | Spanish accent | FP on continuous Spanish speech | code churn | 100% local, no keys |
|---|---|---|---|---|---|---|
| **Vosk** (current) | ❌ no wheel/sdist | ✅ grammar list | ✅ Spanish acoustic model | ✅ grammar rejects | — (incumbent) | ✅ |
| **sherpa-onnx KWS** ✅ **winner** | ✅ native `macosx_11_0_arm64` wheels (cp38–cp314) | ✅ keyword list, open-vocab, no retrain | ⚠️ English/zh-en model; tune per-keyword `#threshold`/`:boost` | ✅ beam search fires only on keywords; tune to reject `-ema-` | low–med (mirrors the Vosk module) | ✅ |
| **openWakeWord** | ✅ pure-Python (`py3-none-any`) + onnxruntime arm64 | ❌ must **train** a custom `.onnx` (Linux + GPU/Colab, Piper synthetic data) | ⚠️ English pipeline; es_MX Piper is off the supported path, less speaker diversity | ⚠️ VAD + threshold + per-user verifier; 2-syllable word is a documented false-*reject* risk | high (train + ship model, per-user verifier) | ✅ |
| **Porcupine** | ✅ `pvporcupine` arm64 | ❌ must train a `.ppn` in the Picovoice Console | ⚠️ English keyword model | ✅ dedicated KWS, tunable sensitivity | zero (already implemented) | ❌ **needs `PICOVOICE_ACCESS_KEY`** |

Weighting per the ask — **detection quality for a bare Spanish "Emma" first**,
then installability, then churn:

- **Porcupine** is ruled out by the hard "100% local, no keys" constraint (it
  requires an AccessKey/account), and it still needs a trained `.ppn`.
- **openWakeWord** could reach the best accent handling *only* with a well-trained
  es_MX model, but: bare 2-syllable "Emma" is the documented weak case (higher
  false-*reject*), training is Linux+GPU+Colab, and you ship a static model that
  can't adapt without a per-user verifier. High friction, uncertain single-word
  quality. It stays available (`WAKE_WORD_ENGINE=openwakeword`) for anyone who
  trains one.
- **sherpa-onnx KWS** is the structural twin of Vosk grammar-mode: a streaming
  beam search restricted to a keyword list, **per-keyword thresholds**, decode
  "a keyword or nothing" (no loose transcript to substring-match), **no training,
  no keys**, native arm64 wheels. It preserves exactly the rejection discipline
  that made a bare "Emma" viable, and gives the most on-device tuning levers.

The one thing no installable engine has is a **Spanish-native acoustic model**
(sherpa KWS models are English gigaspeech / Chinese wenetspeech / zh-en phone).
That is a real downgrade from Vosk-Spanish for accent — but it is common to every
option, and sherpa lets you tune per-keyword thresholds and A/B the phoneme model.

---

## Empirical smoke test (synthetic — NOT the acceptance gate)

Verified the winner end-to-end before adopting it: installed `sherpa-onnx` +
`sherpa-onnx-core` on this arm64 Mac, downloaded the gigaspeech KWS model,
tokenized the five phrases (`▁E M MA`, `▁O Y E ▁E M MA`, …), built a
`KeywordSpotter`, and ran detection on macOS **Mexican-Spanish TTS (voice
"Paulina")**:

| clip | default `#0.25` | notes |
|---|---|---|
| "hey emma" | **FIRED** | — |
| "emma" (bare) | silent | English BPE model + ultra-clean Spanish TTS under-detects; not recovered by lowering `#threshold` alone |
| "oye emma" | silent | as above |
| "…el problema con el sistema" | silent ✅ | `-ema-` negative correctly rejected |
| "…un buen tema para el examen" | silent ✅ | rejected |
| "hola qué tal cómo estás hoy" | silent ✅ | rejected |

**Reading of this:** the pipeline works and the false-positive discipline holds
(all `-ema-` negatives rejected), but a *bare* "Emma" spoken as pure Spanish
/ˈe.ma/ by a clean TTS does not fire on the English acoustic model. This is the
expected hard case — both the openWakeWord and sherpa research say a one-word
Spanish target is the difficult one. Synthetic TTS is a poor proxy for a human
wake word (no consonant onset/breath), and per the prompt, **detection quality is
on-device-verified, not headless**. The bare "Emma" ships with the most sensitive
per-keyword threshold (`#0.15`) and a documented tuning path; "Oye/Hey Emma" are
the reliable fallbacks. Boosting all keywords (`:2.0`/`:3.0`) made it *worse* (it
started matching "problema"), so the shipped boost stays `:1.0`.

The **zh-en phoneme** model (`EMMA → EH1 M AH0`, closer to Spanish vowels) was
also tested but fired nothing in the time-boxed attempt (likely an
encoder-variant/token-format setup issue). It is recorded here as the **first A/B
to try** if on-device tuning of the gigaspeech model can't lift the bare "Emma".

---

## Packaging gotcha found & fixed (would have shipped broken)

`sherpa-onnx`'s arm64 wheel is glue-only (2.1 MB, just `_sherpa_onnx.*.so`); the
native libs — including the `libonnxruntime.1.27.0.dylib` the `.so` dlopens via
`@rpath` — live in a companion package **`sherpa-onnx-core`**. `sherpa-onnx`
declares `Requires-Dist: sherpa-onnx-core==<ver>` but marks it **`Dynamic`**, and
**uv drops that dynamic requirement** — so a plain `sherpa-onnx` dep installs the
glue wheel alone and `import sherpa_onnx` dies with *"Library not loaded:
@rpath/libonnxruntime.1.27.0.dylib"*. Fix: depend on **both** `sherpa-onnx` and
`sherpa-onnx-core` (equal `>=` floors; both have wheels on every platform incl.
the Linux backend). Verified: clean-venv `uv sync --frozen` → import OK.

---

## Decision → implementation

Ship `WAKE_WORD_ENGINE=sherpa` as the default, implemented in
`core/wake_sherpa.py` behind the existing seam. Keep `WAKE_PHRASES` in code as the
source of truth; BPE-tokenize at load with `sentencepiece` (no committed,
drift-prone token file; no pypinyin). Per-keyword thresholds, bare "emma" most
sensitive. Vosk is retained as a Linux/Intel-only optional engine
(`pip install -e .[vosk]`), never a base dependency.

### Sources
- Vosk PyPI artifacts — https://pypi.org/pypi/vosk/json
- sherpa-onnx KWS — https://k2-fsa.github.io/sherpa/onnx/kws/index.html · pretrained: …/kws/pretrained_models/index.html
- KWS python examples — github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/keyword-spotter-from-microphone.py
- KWS model — github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
- openWakeWord — github.com/dscripka/openWakeWord (README, `notebooks/automatic_model_training.ipynb`, `docs/models/hey_jarvis.md`)
- Picovoice Porcupine — requires an AccessKey (console.picovoice.ai) → fails the no-keys constraint
