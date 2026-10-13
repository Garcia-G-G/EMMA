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
