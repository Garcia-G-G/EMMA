# BRAIN-1 — verify log

Every pinned number for BRAIN-1, per part. The 140 labelled utterances in
`scripts/spike_cascade/data/labelled.json` are the regression set; nothing here
is measured on invented utterances.

## Pre-flight: premises checked against the code (2026-10-13)

| Premise in the prompt | In the code | Consequence |
|---|---|---|
| `core/dictionary.py:24` writes inside the source tree | True: `config/dictionary.toml`, which `install.sh` re-extracts | 0a needed |
| 0b: Qwen core10 run "unscored" | **Already scored** in `LAUNCH-12-VERIFY.md` Part 4a (2026-10-08): 67.5 % of 120, **71.7 % of the 113 whose tool exists here** vs full-184 69.9 % | Not redone; cited |
| 0c: a per-item local/LLM mapping exists | Not in the repo | Written from CORE-LOCAL §1 (below) — the one mapping |
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

- **Ceiling (everything but LLM): 112/140 = 80.0 %.**
- **This prompt's reach (L1 + L2 + TPL): 96/140 = 68.6 %.** L3 and READ defer by design.
- L2 is 31.4 points under this strict rule. The "two free-text fields" and
  "slot roles" items would move to L2 with a deterministic field-pattern
  extractor ("que se llame X con el texto Y",
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

## Part 3 — dual retriever

- **BM25**: `core.tool_selection`'s scorer and tokenizers, now exported as
  `BM25` / `doc_tokens` / `query_tokens` (imported by the brain, not copied),
  ranked over **all 185 registered tools** with their guided descriptions. The
  session retriever keeps the core out of its pool; the brain can't.
- **kNN**: `multilingual-e5-small` int8 ONNX (`intfloat`, pinned revision
  `614241f6…`, SHA-256 pinned in `core/brain/embed.py`), run on `onnxruntime` +
  `tokenizers` — both already locked deps. No LLM, no Ollama. Index:
  `~/.emma/brain.db`, raw sqlite3 + `sqlite-vec` (cosine `vec0`), rows
  `(id, text, normalized, brain_path, accept_tools, lang_hint)`.
- `core/brain_host.py` (outside the brain, so it may import `tools`) turns the
  registry into plain `ToolDoc`s and the labelled set into index rows.

### Embedder choice (measured, this Mac)

| Embedder | warm p50 / p95 | kNN r@1 (LOO) | kNN r@5 | Shippable |
|---|---:|---:|---:|---|
| **multilingual-e5-small int8 ONNX** | **2.5 / 5.2 ms** (load 0.7 s) | 39.1 % | 59.1 % | yes |
| Qwen3-Embedding-0.6B (Ollama) | 38.4 / 60.7 ms | 41.7 % | 56.5 % | no — needs Ollama |

Within noise on accuracy; e5 is 15× faster and installable. **The embedder is
not the bottleneck — the data is**: most tools have one or two labelled examples.

### Pinned (115 items whose label includes a real tool; kNN leave-one-out)

| Metric | Count | Rate |
|---|---:|---:|
| BM25 recall@1 | 60 | 52.2 % |
| BM25 recall@5 | 78 | 67.8 % |
| kNN recall@1 | 45 | 39.1 % |
| kNN recall@5 | 68 | 59.1 % |
| **Agreement** (BM25 top-1 == kNN top-1) | **20** | **17.4 %** |
| …of which correct | 18 | 90 % precision |
| Retrieval latency (BM25 + embed + kNN) | p50 6.6 ms, p95 12.8 ms | |

(LAUNCH-12's "90.4 % recall@10" was BM25 over the non-core pool, with core
tools always present; this is top-1/top-5 over every tool, a harder question.)

**What this means for Part 5:** the gate requires agreement first, so with this
index the local verdicts are capped near 20 items. Two measured levers, neither
built into the pinned retriever:

1. **Seeding kNN with the canonical phrases Emma already ships** (CORE-LOCAL §1
   defines L1 as "aliases + canonical phrases"): 283 whole phrases quoted in the
   tools' own descriptions (≥3 content tokens) → kNN r@1 53.9 %, r@5 72.2 %,
   agreement **47 (40.9 %)**, but agreement precision drops to **83 %** (39/47).
   And **13 labelled items have a near-identical shipped phrase** (cosine
   ≥ 0.97 — mostly `syn_*`, which look written from the docstrings), so part of
   that gain is leakage, not generalization.
2. **Retrievability of the descriptions themselves.** 14 of the 77 tools the
   140 accept have **no Spanish trigger phrase** in their description, against
   CLAUDE.md's tool-budget rule 2: `set_volume` ("Set the system output
   volume…") can't be found by "sube el volumen". Also `current_time`, `mute`,
   `play_track`, `play_playlist`, `resume`, `remember_fact`, `recall_facts`,
   `run_command`, `describe_capabilities`, `latest_video_from_creator`,
   `set_preferred_app`, `set_brightness`, `browser_do`. Not changed here:
   editing descriptions while looking at the test set would inflate the pin.

Tests (`tests/test_brain_retrieve.py`, 6): the pinned floors (any drop fails;
agreement may only grow if it stays correct); LOO never returns the item
itself; both sources populated on every candidate (absent = 0); retrieval
p95 < 100 ms; embedder p95 < 100 ms; without the model, kNN is absent and BM25
still ranks (no crash). The kNN tests **skip** with an explicit reason when the
model isn't fetched, rather than passing on BM25 alone.

## Part 4 — closed-vocabulary slots

`core/brain/slots.py`. Slot kinds: `app`/`ide` (vocabulary alias table, then
rapidfuzz against the installed apps + their Spanish display names), `page`
(dictionary keys + titles), `note`/`playlist`/`connection`/`contact` (the
tool's own listing, handed in as a plain list), `number` (digits + ES/EN number
words, compounds, "la mitad", trailing "por ciento"), `url` ("github punto com"
→ `github.com`), `url_scheme`, `profile_field` / `app_category` (closed
tables), and `residue` — only for a tool with exactly one free-text field.
Every `SlotValue` carries `source`, `confidence`, `raw_span`. **Below the floor
(0.85) a slot is unresolved, not guessed.** No time parsing (L3).

Each tool has a **frame** — its command words ("borra la nota", "pon música
de") — consumed before the residue; what nothing explains comes back as
`leftover` for the gate. Frames exist for every tool on an L1/L2 item's
route. They were written knowing the labelled set; whether they generalize is
what shadow mode measures.

**Fixtures written before the resolver** (`tests/brain_slot_fixtures.py`):
expected values for all 44 L2 items, from the utterances — canonical names
for enumerated things, lowercase display text for residue, ints, bare URLs.
Two are declared hard, `xfail(strict=True)` so a surprise pass fails:
`sc_V26` ("abre yutub" → open_url needs youtube.com; no site gazetteer) and
`log_26` (the intended query paraphrases two fragments the user said).

### Pinned

| | Result |
|---|---|
| L2 items resolving every expected value exactly | **42 / 44** (the 2 declared hard xfail) |
| L1 items with no required parameter missing | **24 / 24** |
| L1/L2 items with a route (≥1 accepted tool has a spec) | 68 / 68 |

### Bugs the fixtures caught before commit

1. **Normalization re-joined tokens**: "analytics-prod" became
   "analytics- prod", "example.com/portfolio" became "example. com/ portfolio".
   `normalize` now cuts the wake run out of the original string and never
   rebuilds it.
2. **A repeat mention ate content**: "agrega **comprar** café a mi nota
   Compras" — "comprar" scored 0.857 against the note "Compras" and was taken
   as a second mention. Repeats now need 0.95.

### Re-pin of Part 3 (deliberate, explained)

Fix 1 changed the text the embedder sees, and the Part 3 floor caught it:
kNN r@5 68 → 66. kNN now embeds the words only (`retrieve.embed_text`), so
punctuation and spacing can't move a neighbor. Net through the retriever:
**r@1 45 → 46, r@5 68 → 66** — flips on near-ties (lost `sc_V40`, `sc_V69`,
`log_12`; gained `log_15`, `log_22`). Part 3's pin was measured on text a
bug had mangled, so the new floor is the honest one.

### Data added

- `rapidfuzz>=3.9` (uv lock: one package added, nothing upgraded).
- `config/vocabulary.toml`: `[WhatsApp]` with `stt_aliases = ["guasap",
  "wasap", "whatsap"]` — the prompt names "guasap" → WhatsApp as a case and the
  vocabulary had no WhatsApp entry. Also feeds STT bias.
- `core/vocabulary.alias_map()` (public copy of the alias index) for the host.

Gates: 1363 passed, 2 xfailed; ruff, mypy clean.

## Part 5 — the gate

`core/brain/gate.py` (pure: retrieval + normalized text + plain tool metadata
→ `Decision`), `core/brain/brain.py` (`Brain.decide`: normalize → retrieve →
gate, per-stage timings). Order, each step recorded as a `Feature`:
1. noise → `none` (empty, closed STT-artifact list, one repeated token, a single
   word nothing explains); 2. closed lists → `refuse` (destroy home, `rm -rf`,
   secret aloud / sent out, total control; unsupported: a phone call — the
   pattern is **anchored**, see below), `clarify` (an action with only a generic
   object: "búscame algo"), `none` (small talk, when it is ALL small talk);
3. BM25 top-1 == kNN top-1; 4. kNN cosine ≥ τ; 5. margins on both sources ≥ δ;
6. "y luego"/"después"/"and then", **or a conjunction followed by a second
   imperative** ("… y dime qué encontraste"), or > 12 content tokens → `complex`;
7. every required slot above the floor, no unexplained content word, no
   dangling pronoun; 8. destructive or sink → `local` with `needs_confirmation`.
Thresholds: `BRAIN_KNN_MIN`, `BRAIN_MARGIN_MIN`, `BRAIN_MAX_CONTENT_TOKENS`,
`BRAIN_SLOT_FLOOR` in `config/settings.py`; the host builds `Thresholds` from
them (the brain never imports settings).

### Starting thresholds: τ kept, δ changed — measured, not tuned for score

At the prompt's τ = 0.85, δ = 0.15: **coverage 19.3 %, zero local verdicts** —
every one of the 20 agreements failed the margin. On the agreed items:

- correct agreements: kNN margin 0.000–0.097, BM25 relative margin 0.058–1.000;
- the two wrong agreements (`sc_A27`, `syn_02`): 0.036 / 0.221 and 0.000 / 0.245
  — inside the correct items' range on both sources.

δ separated nothing and rejected everything, so **`BRAIN_MARGIN_MIN = 0.0`**:
computed and logged on every decision, not gating, until shadow data says
otherwise. τ = 0.85 rejects no correct agreement (min 0.879) and stays above
e5's "unrelated" level (~0.82), so it stays. **What actually rejects wrong
agreements here is step 7 (slots)** — both wrong agreements died there.

### The index: 140 + Emma's shipped canonical phrases (leak-checked)

`build_index(seed_docs=…)` adds the 293 whole phrases quoted in the tools' own
descriptions (CORE-LOCAL §1: L1 = aliases + canonical phrases). For
evaluation, any phrase within cosine 0.97 of a labelled item is held out (14
of them) — the score can't come from a docstring that quotes a test item.
Production keeps all of them.

| Index | Coverage | Local | Wrong |
|---|---:|---:|---:|
| 140 only | 28.6 % | 14 | 0 |
| 140 + phrases (all) | 35.0 % | 23 | 0 |
| 140 + phrases (14 leaked held out) | 35.0 % | 23 | 0 |

### Pinned (`tests/test_brain_eval.py`)

**Coverage 48 / 140 = 34.3 %, local 22, wrong 0 / 140 = 0 %** — wrong counting
a wrong tool, a wrong argument value against the Part 4 fixtures, or declining a
request that wanted a tool. Decision latency p50 10.1 ms, p95 46.8 ms (Part 7
pins it).

| Path | n | Verdicts |
|---|---:|---|
| L1 | 24 | local 9, defer 15 |
| L2 | 44 | local 13, defer 31 |
| L3 | 4 | defer 4 |
| TPL | 28 | none 14, refuse 7, clarify 4, defer 3 |
| READ | 12 | none 1 (`sc_A08`, accepts `__none__`), defer 11 |
| LLM | 28 | defer 28 |

Defer reasons: `disagree` 75, `slots` 16, `complex` 1.

**Every local decision**: `sc_A11` current_datetime_speak · `sc_A20`
search_github(pytorch) · `sc_A23` ide_terminal_send(ls, Cursor) · `sc_A28`
list_browser_tabs · `sc_V13`/`log_02` delete_note(Compras) · `sc_V17`
close_duplicate_tabs · `sc_V31` my_repos · `sc_V34` open_my_page(github) ·
`sc_V39` remember_connection(analytics-prod, TablePlus) · `sc_V40`
open_in_app(general, Slack) · `sc_V62`/`log_06`/`log_32` close_current_tab ·
`sc_V67`/`log_09` play_playlist(Classical Essentials, Music) · `log_03`
open_application(Spotify) · `log_05` open_in_app(learning-rots-local) ·
`log_30` shutdown_emma · `syn_07` remember_app(Linear, linear://) · `syn_09`
repeat_last · `syn_16` click_button(cancelar). 13 of the 22 carry
`needs_confirmation` (destructive or sink).

**Mutation check:** with step 7 disabled, 5 wrong local decisions appear —
including "silencia el audio" → `mute_mic` (would cut Emma's microphone) and
"abre cursor" → `toggle_ide_terminal`. The wrong-ceiling catches them.

### Bugs found while building the gate

- My phone-call refusal matched "**llamar** al dentista" inside a reminder
  (`sc_A16`) — 1 wrong. The pattern is now anchored: a call request opens with
  the verb.
- `sc_V56` went local with `query="pytorch y qué encontraste"` — right tool,
  wrong value; the original "wrong" metric (tool only) missed it. Fixed twice:
  the conjunction-plus-second-verb rule defers it as `complex`, and the eval now
  counts wrong VALUES as wrong.

### The bar: ≥ 50 % local at ≤ 1 % wrong — **not met (34.3 % at 0 %)**

This prompt's reach is 68.6 % (L1 + L2 + TPL). The gap is step 3: **75 items
defer on disagreement**. On the L1/L2 ones, BM25 is right 23 times and kNN 20
times; both are wrong 12 times. Two causes, measured:

1. **kNN has too little data.** One or two labelled phrasings per tool.
   Seeding with shipped phrases moved coverage 28.6 → 35.0 %. More phrasings per
   tool is exactly BRAIN-2's learning loop.
2. **14 tools have no Spanish trigger** (Part 3). A counterfactual — generic
   Spanish triggers on all 14, in memory, never shipped — moves coverage only
   **35.0 → 35.7 %**: once BM25 is right, kNN is the binding source. Editing
   descriptions would also change what the live Realtime model reads, which
   BRAIN-1 must not; it's not worth doing for this.
3. Three disagreements are both right (`current_time` vs
   `current_datetime_speak`, `sc_A11`/`log_07`/`log_08`). An equivalence group
   would serve them; not built (a product judgement, not a measurement).

**Not done to hit the number:** loosening agreement, tuning τ on these items,
or writing frames until the test set passes. Coverage at 0 % wrong is the
honest starting point; shadow data decides the rest.

## Part 6 — templates, confirmation, clarification (no LLM)

`core/brain/respond.py`, text only (TTS is VOICE-1).

- **Templates**: 40 tools — every tool on an L1/L2 route — with 4–6 phrasings
  per language (326 total), plus phrasings per verdict: confirm, cancelled,
  unclear answer, clarify (by verb: search / send / close / generic / numbered
  options), refuse (safety / no phone calls), small talk. Weighted random,
  never the same phrasing twice in a row per key (60 draws: no repeat, ≥ 4
  distinct). Language follows the utterance (stopword hint; type `SpokenLang`
  from `core/runtime.py`). Neutral wording, no regionalisms. Noise answers with
  silence (""); a `defer` raises — it is the LLM's to answer.
- **Confirmation questions name the concrete action and its target** ("¿Borrar
  la nota Compras? Dime sí o no.") — 18 action phrases cover every destructive
  or sink tool on an L1/L2 route; a test fails if a risky tool would read its
  code name. Found while building: the first draft said `delete_note(Compras)`.
- **Confirmation turn** (`answer_confirmation`): closed yes/no/cancel lists from
  Part 2 — the answer must OPEN with yes; "borra la nota compras, sí" is
  `unclear`, not a yes. `execute` is the brain's only producer of
  `confirmed=True`; the dispatch guard still independently requires the spoken
  yes after the question.
- **Clarification turn** (`answer_clarification`): ordinals ES/EN ("el
  primero", "la segunda", "el 3", "the last one") or fuzzy on the option label,
  matched on content words ("el de la oficina" → "Juan García (oficina)" — the
  first draft scored the filler words and missed it). Ties ("Juan" against two
  Juans and a Juana) → None: ask again, don't guess.
- Refusals: one calm sentence, no lecture (test: one sentence, < 40 chars).

Pinned: 31 tests (`tests/test_brain_respond.py`). Gates: 1417 passed, 2 xfailed.

## Part 7 — shadow mode in the live session

`core/brain_shadow.py`, wired into `core/conversation.py` behind
`BRAIN_SHADOW` (default on): a fresh `Shadow` per wake session (next to the
injection guard), VAD onset → new turn, transcript → decision, each Realtime
tool call → recorded on the turn, session end → flush. **It executes nothing**:
no dispatch, no tool, no audio (test: `registry.dispatch` replaced by a function
that fails the test if called, across a 140-turn session).

- **Turns start at VAD onset, not at the transcript** — Realtime can call a tool
  before its transcript lands. A decision is attached to the turn its words came
  from even if it finishes after the user started the next turn (tested with a
  deliberately slow brain).
- **Never hurts the live path**: the decision runs in a worker thread as a
  detached task (the frame tap never awaits it); every hook swallows its own
  errors. **No record, no decision**: a brain still loading logs `defer /
  brain_not_ready`; a brain that raises logs `defer / brain_error:<Type>`; a VAD
  onset with no words (a cough) logs nothing.
- The live brain loads once per daemon in a background thread: available tools,
  installed apps (a listing of the Applications folders — the environment cache
  only holds one app per category), the dictionary's pages/contacts/connections,
  `~/.emma/brain.db` (labelled set + canonical phrases, created 0600 — it becomes
  personal-tier when BRAIN-2 appends learned utterances).
- Each log line: the full `Decision` (features, slots, timings) + `session`,
  `turn`, `brain_verdict`, `brain_tool`, `realtime_tool`, `realtime_args`
  (redacted, `confirmed` dropped), `realtime_tools_in_turn`, `agree`
  (local: same tool; none/clarify/refuse: live path used no tool; defer: null).
- `python -m emma.brain report` — turns, served-without-LLM rate, local rate,
  agreement on local verdicts, no-tool agreement, every disagreement with both
  sides, defer-reason histogram, p50/p95 latency. Also `decide "<text>"` and
  `fetch-model`.

**The live comparison can't start yet**: there is no OpenAI key, so Realtime
doesn't run. The wiring ships and is tested against the 140; the week of data
starts when a voice path is live (BRAIN-2 / VOICE-1), as the prompt allows.

### Simulated session over the 140 (live path = each label's first tool)

```
turns logged: 140
served without an LLM:  34.3% (48/140)   clarify 4  defer 92  local 22  none 15  refuse 7
local verdicts:  15.7% (22/140)
agreement with the live path on local verdicts:  95.5% (21/22)
no-tool verdicts where the live path also used no tool:  88.5% (23/26)
decision latency: p50 7.6 ms   p95 18.4 ms   (n=140)
```

All 4 "disagreements" are the brain choosing a *different acceptable* answer
than the label's first tool (`current_datetime_speak` for `current_time`;
small talk / refuse where the label also accepts `__none__`) — the simulation
can't tell; real Realtime data will.

**On this Mac** (`python -m emma.brain decide "Hey Emma, abre espotify"`):
`defer / disagree` — BM25 `toggle_ide_terminal` (the missing Spanish trigger on
`open_application`, Part 3) vs kNN `open_application`. The gate fails closed,
exactly as Part 5 measured.

### Pinned

- **Decision latency p95 < 100 ms** (`test_decision_latency_p95_under_100ms`,
  best of 3 rounds over the 140): measured p95 18.4–46.8 ms across runs on this
  Mac under load; p50 7.6–10.1 ms.
- 8 tests (`tests/test_brain_shadow.py`), incl. the REAL `_UserSpeechTap`
  driving the hooks.

Gates: 1425 passed, 14 deselected, 2 xfailed; acceptance `--mock-external`
99/99; ruff, mypy clean.

---

## Definition of done — status

| # | Item | Status |
|---|---|---|
| 1 | User data in `~/.emma/`, survives an installer re-run; Qwen run scored; `brain_path` on all 140 | **Done** (0a tested; 0b in LAUNCH-12 Part 4a; 0c 140/140) |
| 2 | `core/brain/` exists; import guard green; `mypy --strict` clean | **Done** (guard is transitive and proven to catch a 2-hop violation) |
| 3 | Taint + sink policy + mic-only confirmation in dispatch; 20 injection tests green | **Done** (20 attacks; 25/33 fail with the guard off) |
| 4 | BM25 + kNN, recall and agreement pinned | **Done** (BM25 r@1 52.2 %, kNN r@1 40.0 % LOO, agreement 17.4 %) |
| 5 | Every L1/L2 item resolves its expected values; fixtures pinned | **42 / 44 L2 + 24 / 24 L1**; 2 declared hard (xfail strict) |
| 6 | Gate ≥ 50 % local, ≤ 1 % wrong; every defer has a reason; thresholds in settings | **Not met: 34.3 % at 0 % wrong.** Reasons, thresholds: done. δ changed 0.15 → 0.0 on measurement |
| 7 | Templates, confirmation, clarification without an LLM | **Done** |
| 8 | Shadow mode live; report works; decision p95 < 100 ms | **Wired + tested; live data waits for a voice path** (no OpenAI key). p95 18–47 ms |
| 9 | Full suite + ruff + mypy green; this doc records every pinned number and the per-item results | **Done** |

> **BRAIN-1.5 update:** `log_26` was reclassified L2 → LLM (reason in
> `labelled.json` → `brain_path_note`), and `sc_V26` now resolves (closed site
> table). Split today: L1 24, L2 43, L3 4, TPL 28, READ 12, LLM 29. This file's
> label mapping is the only one — see BRAIN-1.5-VERIFY.
