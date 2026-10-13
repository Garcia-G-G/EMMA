# BRAIN-1 — verify log

Every pinned number for BRAIN-1, per part. The 140 labelled utterances in
`scripts/spike_cascade/data/labelled.json` are the regression set; nothing here
is measured on invented utterances.

## Pre-flight: premises checked against the code (2026-10-13)

| Premise in the prompt | In the code | Consequence |
|---|---|---|
| `core/dictionary.py:24` writes inside the source tree | True: `config/dictionary.toml`, which `install.sh` re-extracts | 0a needed |
| 0b: Qwen core10 run "unscored" | **Already scored** in `LAUNCH-12-VERIFY.md` Part 4a (2026-10-08): 67.5 % of 120, **71.7 % of the 113 whose tool exists here** vs full-184 69.9 % | Not redone; cited |
| 0c: the 79.5 % per-item mapping "the owner has it" | **Not in the repo** anywhere | Reproduced from CORE-LOCAL §1 (below); the split differs |
| Fixtures `set_volume(level=70)`, `open_my_page(page="github")` | Real schemas: `set_volume(percent)`, `open_my_page(name)` | Fixtures use the real names |
| `labelled.json` carries argument values | It does not: `id, text, accept, src, cat, rt_hist` only | Slot fixtures are new labels, written from the utterance text |
| `_QUERY_STOP` in `tool_selection.py` | True, `{"emma","hey","oye","hola"}` | Reused |
| `sqlite-vec` is a dependency | True, `sqlite-vec>=0.1.9` | Used for `brain.db` |
| `rapidfuzz` | **Not a dependency** | Added in Part 4 |
| A local embedder | None shipped. The spike's `qwen3-embedding` ran via Ollama, which `install.sh` does not install. `onnxruntime`, `tokenizers`, `numpy` ARE locked deps | Part 3 uses `multilingual-e5-small` ONNX on those — no new heavy dep, no Ollama |
| 49 destructive tools | True, 49 | Part 2 sink list starts from them |
| `returns_untrusted_content` exists | True, 41 tools set it | Part 2 taint source |
| `run_command` is destructive | **False** (`destructive=False`) | It is a sink by the prompt's own definition (shell); listed in `sinks.py` explicitly |
| `_UserSpeechTap`, `SpokenLang` | Both exist | Part 7 / Part 6 reuse them |

## Part 0 — prerequisites

### 0a. User data out of the source tree

`core/dictionary.py` now reads two files layered: the shipped template
(`config/dictionary.toml`, read-only) and the user file
(`~/.emma/dictionary.toml`, every write). Tables merge key by key, user wins;
keys a later release adds to the template still appear. `migrate()` seeds the
user file from the in-tree one the first time (that file holds whatever a pre-0a
install was taught) and runs at daemon boot and before every write. It never
overwrites an existing user file. `tools/rss_tool.py` reads `[feeds]` through the
merged view instead of opening the in-tree file.

Tests (`tests/test_dictionary_userdir.py`, 6): seeding + 0600 + idempotence;
writes never touch the template; user wins key-by-key while new template keys
arrive; **user data survives the template being replaced wholesale** (what
`tar -xzf` over `~/.emma/src` does); a pre-0a install's in-tree contact survives
its first upgrade; the merged `tables()` view.

`tests/conftest.py` now points the user file at an empty temp path for every
test, so no test reads the developer's real `~/.emma/dictionary.toml`. That
isolation exposed one **pre-existing order-dependent test**:
`test_bias_includes_identity_contacts_glossary` asserted a contact ("Ana
García") that it never created. It only passed in the full suite because an
earlier test leaked it through the shared cache; at `ed70bc1` it fails when run
alone. It now seeds its own contact.

### 0b. Qwen run

See `LAUNCH-12-VERIFY.md` Part 4a. Not re-scored.

### 0c. `brain_path` on all 140

Labels and a one-line reason per item: `scripts/spike_cascade/brain_path_labels.py`
(the script writes the field; the frozen `accept`/`text`/`cat` fields are
verified byte-identical before and after). Rule, applied to each accepted tool's
**required** parameters:

| Label | Rule | Items | Share |
|---|---|---:|---:|
| L1 | no required argument | 24 | 17.1 % |
| L2 | every required argument from a closed set (app, contact, note/playlist/page/connection name, number, URL) + at most one residue free-text field | 44 | 31.4 % |
| L3 | needs the time grammar (date, time, duration, timezone) | 4 | 2.9 % |
| TPL | no tool: noise → none, safety → refuse, object-less → clarify, chit-chat | 28 | 20.0 % |
| READ | tool is local, the spoken answer needs a model to read the result | 12 | 8.6 % |
| LLM | anaphora, multi-step, two free-text fields, slot roles, messy dictation, code | 28 | 20.0 % |

- **Ceiling (everything but LLM): 112/140 = 80.0 %**, vs the owner's 79.5 %.
- **This prompt's reach (L1 + L2 + TPL): 96/140 = 68.6 %.** L3 and READ defer by design.
- The owner put L2 at 42 points; this strict rule gives 31.4. The difference is
  almost certainly the "two free-text fields" and "slot roles" items, which a
  deterministic field-pattern extractor ("que se llame X con el texto Y",
  "X dentro de Y", "X significa Y") would move to L2: `sc_A13`, `log_00`,
  `sc_A29`, `sc_V19`, `syn_04`, `syn_00` (+6, to 35.7 %). Not built here; the
  label says what Part 4 can actually fill.
- Five L2 items need a tool this Mac does not have (`set_brightness`,
  `my_repos`/`search_github`). The brain's regression suite scores against
  **every registered tool**, not this machine's available set, so the pinned
  numbers don't depend on whose Mac runs them.

## Part 1 — the package and the decision log

`core/brain/`: `decision.py` (`Decision`, `SlotValue`, `Feature`, `Candidate`,
`Verdict`), `log.py` (append-only JSONL, redacted, 0600, rotates at 50 MB),
`normalize.py` (wake/vocative removal, accent folding for matching only).

- **The brain never imports `config.settings`.** Settings lazily import
  `core.secrets`, which shells out to the Keychain; that would put `subprocess`
  in the brain's closure. Thresholds arrive as a plain dataclass built by the
  caller (Part 5). Same for tool metadata and app lists (plain data in).
- **Import guard is transitive** (`tests/test_brain_imports.py`): every import
  inside `core/brain` (function-level too), then the module-level imports of
  everything it reaches. Forbidden: `actions`, `core.platform._mac`, `tools`,
  `subprocess`, PyObjC frameworks. Its self-test proves the walk is transitive:
  `core.orchestrator` has no platform import of its own and is still caught
  through `core.conversation`.
- **Wake stripping** removes a *leading* run of wake forms only (the live
  `_QUERY_STOP` plus the STT renderings observed in the labelled logs: `ayema`,
  `aima`, `ay`, `ey`), and `emma` anywhere as a vocative. "hola" inside a
  dictated post (`log_21`) survives.
- `mypy --strict` clean on `core/brain` (already covered by `[mypy-core.*]`).

Pinned: 16 tests (`test_brain_imports.py` 3, `test_brain_skeleton.py` 13).

## Part 2 — injection defense in dispatch

Enforced in `tools/registry.py:dispatch` through a per-session guard
(`core/injection.py`), installed by `core/conversation.py` when a wake session
starts and removed at teardown. Dispatch with no guard installed (background
tasks, tests, the acceptance runner) behaves exactly as before. The tools array
is untouched (byte-stable).

| Rule | Implementation |
|---|---|
| 1. Taint | Any result from a `returns_untrusted_content` tool (41 of them) taints the session. Cleared by a new wake session (`injection.install()`) or a transcript matching the closed FORGET list ("olvida eso", "forget that", …) |
| 2a. Sinks confirm while tainted | `core/brain/sinks.py`: 49 DESTRUCTIVE + 24 EGRESS (URLs, web queries, drafts, shell/terminal/SQL/request runners) + 15 PERSIST (memory/vocabulary/notes/reminder writes — poisoning targets) = **88 sinks**, by name. Two-phase confirmation regardless of the tool's flag; a tool with no `confirmed` parameter is never passed one |
| 2b. Provenance while tainted | Every string argument of a sink must appear (folded, spoken-URL words joined, scheme/`www.` ignored) in this session's user transcripts or a trusted tool result. Closed-choice args (`browser`, `ide`, `kind`, …) exempt. Plus: no secret-shaped value in a sink while tainted (`core/redaction.contains_secret`) |
| 3. Mic-only confirmation | `confirmed=True` runs only if a closed-list yes was transcribed AFTER the newest question for that tool — tainted or not. The yes must OPEN the answer ("borra la nota, sí" is a request, not an answer) and is spent once. If the user started speaking after the question but the words haven't landed, the guard waits up to 2.5 s instead of rejecting a real yes |

**Behaviour change (intended by the spec):** the old handler allowed a "cold"
`confirmed=True` with no prior question ("borra X, sí, seguro" in one breath)
and relied on the system prompt. That now gets one question first. Two
handler-level tests that mock `dispatch` still pass and were re-worded to say
they cover the handler layer only; the end-to-end contract is
`test_preemptive_yes_now_gets_a_question_first`.

**Tests (`tests/test_injection.py`, 33):** 20 attacks on the real registry
(email → send_to / iMessage / WhatsApp / draft; web → delete_note /
overwrite ~/.zshrc / post / plant URL scheme / forget secret; OCR & screen →
shell / background shell / IDE terminal / type password / Discord; page → URL
exfil with a key in the query; note → memory poisoning; calendar → create
event; tabs → close bank tabs; RSS → terminal; and the model self-confirming).
Each asserts the sink did not run. Plus: a spoken yes can't launder an
attacker's address; legit paths (untainted sink, user-dictated tainted sink
asks then runs, "olvida eso", new session); mic rules (self-confirm refused,
preemptive yes asks first, a yes is spent once, a late transcript is waited
for, no guard = no change); the sink list (every destructive tool is a sink;
every sink name is registered).

**Mutation check:** with `check()` forced to approve everything, **25 of 33
fail** (all 20 attacks + the mic rules). The tests measure the guard.

Gates: full suite 1277 passed; acceptance `--mock-external` 99/99; ruff, mypy clean.
