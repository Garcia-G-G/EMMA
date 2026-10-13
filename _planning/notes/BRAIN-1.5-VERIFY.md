# BRAIN-1.5 — verify log

Every number for BRAIN-1.5. The exam is the 140 labelled utterances; nothing
from them enters the phrasebook (test-enforced).

## Headline — local % at 0 wrong on the 140

| | Served without an LLM (local + templates) | Local tool decisions | Wrong |
|---|---:|---:|---:|
| **Before** (BRAIN-1, `be86cfc`) | **34.3 %** (48/140) | **15.7 %** (22/140) | 0 |
| **After** (BRAIN-1.5: phrasebook + gate D + site table) | **55.7 %** (78/140) | **37.1 %** (52/140) | **0** |

"Wrong" = a local tool the label doesn't accept, a wrong argument value against
the slot fixtures, or declining (none/clarify/refuse) a request that wanted a
tool. Baseline re-verified before building: the BRAIN-1 pins pass unchanged.

## Constraint: nothing Realtime reads changes

`tests/test_realtime_surface_frozen.py` pins, taken before any BRAIN-1.5 change:
the SHA-256 of all 185 tool specs as `registry._spec` renders them for the LLM
(docstrings + `tools/guidance.py`), of `tool_selection.CORE_TOOLS`, and of
`core/tool_selection.py`'s source. Any drift fails.

## Part 1 — the phrasebook

`core/brain/phrasebook.toml` + loader `core/brain/phrasebook.py`. Brain-only.

- **83 tools**: all 77 tools any of the 140 accepts, plus 6 common confusers
  (`mute_mic`, `unmute`, `pause`, `next_track`, `now_playing`, `list_timers`).
  Covering only the 40 tools with local templates would leave the brain blind
  to the rest, and a request for an unlisted tool ("pon un timer…") would drift
  to the nearest listed one — a wrong-answer risk. Tools that can't run
  locally (L3/READ/LLM) are still recognized, so the gate defers them honestly.
- **1,252 ES + 1,245 EN phrasings** (15–18 per tool per language) + 93 STT
  manglings. Imperative, question, bare-noun forms; `tú` and `usted`.
  Placeholders are the tool's parameter names. **4,032 kNN rows** after filling
  each placeholder 2 ways with generic values not taken from the 140 (e.g.
  `{timezone}` → Madrid / Londres, not Tokio).
- Tests (`tests/test_phrasebook.py`): ≥ 15 ES + ≥ 15 EN per tool; covers every
  tool the 140 route to; every tool registered and every placeholder a real
  parameter; **no phrasing under two tools**; **no labelled utterance as a
  template or any filled expansion**; neutral language against a closed list of
  regional words.

What the tests caught while writing:
- **4 exam leaks** — the most canonical phrasings *are* labelled items: "qué
  hora es" (`sc_A11`), "apágate" (`log_30`), "hazlo otra vez" (`log_34`), "en
  qué panel estoy" (`syn_13`). Replaced.
- **Regionalisms I wrote and then removed** ("if unsure, leave it out"):
  *súbele/bájale*, *picar* (click), *brincar* (skip), *junta* (meeting),
  *regrésame*, *computadora*. Now in the test's closed list.

## Part 2 — both retrievers index the phrasebook

- **BM25**: the brain builds its own index over each tool's phrasebook text
  (placeholders stripped) + the tool name split on underscores. It imports the
  BM25 *implementation* from `core.tool_selection`; Realtime's *index* there is
  untouched (frozen-surface test green). Tools outside the phrasebook are
  indexed by name only. **The brain no longer indexes LLM descriptions at
  all** — BRAIN-1's "canonical phrases from docstrings" seeding is removed.
- **kNN**: `brain.db` holds 140 labelled rows + **4,032 phrasebook rows** (each
  placeholder filled 2 ways), every row tagged `source = phrasebook | labelled |
  learned`. A BRAIN-1 `brain.db` (no `source`) is rebuilt on open — it held no
  learned data. Build: 20.4 s for 4,172 rows on this Mac (background thread in
  the daemon).
- **Honest evaluation**: each item is excluded from its own neighbors, AND any
  phrasebook row within cosine 0.97 of it is held out *for that item*
  (`brain_host.holdouts`): 14 items had a near-copy (29 rows). BRAIN-1 held
  seeds out globally; per-item is stricter where it matters and doesn't throw
  away coverage for other items.

### Retrievers, before / after (115 items with a tool)

| | Before (BRAIN-1) | After (phrasebook) |
|---|---:|---:|
| BM25 top-1 | 60 (52.2 %) | **80 (69.6 %)** |
| BM25 top-5 | 78 (67.8 %) | **102 (88.7 %)** |
| kNN top-1 (LOO) | 46 (40.0 %) | **89 (77.4 %)** |
| kNN top-5 (LOO) | 66 (57.4 %) | **107 (93.0 %)** |
| **Agreement** (BM25 top-1 == kNN top-1) | **20 (17.4 %)** | **70 (60.9 %)** |
| …correct | 18 (90 %) | 65 (93 %) |

Agreement moved from 17 % to 61 %, as the prompt expected — the retrievers were
the bottleneck.

### One wrong answer appeared — and the rule that removes it

With the shipped gate on the new retrievers: 46.4 % served, 1 wrong.
`log_11` "¿Puedes decirme qué repositorios tiene el usuario examplehandle en
GitHub?" — both retrievers agreed on `remember_user_profile` (it *talks* like
a profile phrasing) and the residue put the whole question into `value`. **A
question never stores**: the gate now defers a `PERSIST` tool when the
utterance is interrogative (`?`/`¿`, or opens with a question word or "puedes
/ podrías"), reason `question_not_statement`. Fails closed: a stored fact said
as a question defers too.

### Gate A (as shipped, margin 0.0) after Part 2

**Served 64/140 = 45.7 %, local 38/140 = 27.1 %, wrong 0.** Defer reasons:
disagree 46, slots 27, complex 2, question_not_statement 1. (Part 3 studies
the gate; these are the "A" numbers.)

Pinned: retriever floors re-pinned upward in `tests/test_brain_retrieve.py`;
gate floors unchanged until Part 3. Gates: 1436 passed, 2 xfailed; ruff, mypy clean.

## Part 3 — honest gate study

Step 3 of the gate now *chooses* a tool and records how (`choice_path`):
`agree`, `relaxed:knn_in_bm25_top3`, `relaxed:bm25_in_knn_top3`,
`single:knn`, `single:bm25`. If kNN's best is the no-tool pseudo-label
`__none__`, the gate defers (`knn_none`) — declining a real request is a wrong
answer, and kNN alone can't tell. τ (novelty) applies to the *chosen* tool's
kNN score, except on a BM25-only pick. Reproduce: `scripts/brain/gate_study.py`.

### Thresholds, and the distributions they came from (all 140, LOO + holdout)

| Source, top-1 | Right (quartiles) | Wrong (quartiles) |
|---|---|---|
| BM25 score | 5.8 / 11.7 / 13.6 / 16.8 / **27.0** | 2.5 / 4.9 / 7.9 / 13.6 / **51.4** |
| BM25 relative gap | 0.004 / 0.30 / 0.42 / 0.55 / 1.0 | 0.003 / 0.01 / 0.11 / 0.25 / 1.0 |
| kNN cosine | 0.874 / 0.925 / 0.947 / 0.962 / 0.997 | 0.819 / 0.877 / 0.899 / 0.928 / 0.963 |

- **θ₁ (BM25 alone): off.** The highest-scoring *wrong* BM25 top-1 (51.4,
  `log_16` → delegate_to_claude_code; 33.6 `log_46`) beats every *right* one
  (max 27.0), and the gap doesn't separate them either. Any θ₁ that admits no
  wrong pick admits no right one.
- **θ₂ (kNN alone): 0.94.** Among items where the sources disagree, before the
  argument check: θ₂ = 0.97 admits 1 right / 0 wrong, 0.96 → 5 / 1, 0.95 → 11 / 2.
  Through the full gate, local % is **flat from 0.94 down to the 0.85 novelty
  floor, all at 0 wrong** (51 local) — the extra kNN-only picks all fail the
  argument check. 0.94 is the most conservative value on that plateau.
- **δ (margin) stays 0.0** (BRAIN-1 finding unchanged).

### The study (each variant ± the argument-completeness check)

| Variant | Args check | Served | Local | Wrong | Defer reasons |
|---|---|---:|---:|---:|---|
| A. agreement required (BRAIN-1) | on | 64 (45.7 %) | 38 (27.1 %) | **0** | disagree 46, slots 27, complex 2, question_not_statement 1 |
| A | off | 91 (65.0 %) | 65 (46.4 %) | 4: sc_V41, sc_V42, sc_V69, log_27 | disagree 46, complex 2, question 1 |
| B. relaxed agreement (top-3) | on | 72 (51.4 %) | 46 (32.9 %) | **0** | slots 43, disagree 21, complex 2, question 2 |
| B | off | 115 (82.1 %) | 89 (63.6 %) | 12 | disagree 21, complex 2, question 2 |
| C. single source (θ₁ off, θ₂ = 0.94) | on | 73 (52.1 %) | 47 (33.6 %) | **0** | slots 37, disagree 27, complex 2, question 1 |
| C | off | 110 (78.6 %) | 84 (60.0 %) | 8 | disagree 27, complex 2, question 1 |
| **D. B + C — shipped** | **on** | **77 (55.0 %)** | **51 (36.4 %)** | **0** | slots 46, disagree 13, complex 2, question 2 |
| D | off | 123 (87.9 %) | 97 (69.3 %) | 12 | disagree 13, complex 2, question 2 |

(Wrong with the check off, B/D: sc_A14, sc_A21, sc_A23, sc_A26, sc_V26, sc_V34,
sc_V41, sc_V42, sc_V69, log_27, syn_00, syn_02.)

**Decision rule** (fixed before running): highest local % among variants with
0 wrong on the 140 → **D**. Settings: `BRAIN_RELAXED_AGREEMENT=True`,
`BRAIN_BM25_SINGLE_MIN=None`, `BRAIN_BM25_SINGLE_GAP=0.0`,
`BRAIN_KNN_SINGLE_MIN=0.94`. Argument completeness is never a setting
(`Thresholds.require_slots` exists for this study only; a test asserts it's on).

**What each layer is worth:** relaxing step 3 adds 13 local decisions; the
argument check removes 12 wrong ones. Every relaxation is only as safe as that
check — including its rule that a tool with no slot/frame spec can never go
local (e.g. "silencia el audio": kNN picks `mute_mic` at 0.963, the check
defers it). **This was tuned on the eval set and we know it**: the shadow week
is the real test, and every feature is still logged.

### Shipped, per path

| Path | n | Verdicts |
|---|---:|---|
| L1 | 24 | **local 21**, defer 3 |
| L2 | 44 | **local 30**, defer 14 |
| L3 | 4 | defer 4 |
| TPL | 28 | none 14, refuse 7, clarify 4, defer 3 |
| READ | 12 | none 1, defer 11 |
| LLM | 28 | defer 28 |

How the 51 local decisions were chosen: agree 38, relaxed (kNN in BM25 top-3)
5, kNN alone 5, relaxed (BM25 in kNN top-3) 3.

Pinned (`tests/test_brain_eval.py`): coverage 77, local 51, wrong 0. Gates:
1436 passed, 2 xfailed; ruff, mypy clean.

## Part 4 — ship the embedder

**Installer** (`_landing/install.sh`, step 5.2, commit in the `_landing` repo):
pinned revision `614241f6…` of `intfloat/multilingual-e5-small`, the int8 ONNX
model + tokenizer, each file's SHA-256 verified (a mismatch is deleted, never
loaded), into `~/.emma/models/multilingual-e5-small/`. **Non-fatal**: if it
fails, Emma installs and works; the brain defers every tool turn with
`embedder_missing`. Tested by extracting the step verbatim against a fresh
`EMMA_HOME`:

| Case | Result |
|---|---|
| Fresh install | downloaded + verified, **69.1 s** on this connection (~2 MB/s) |
| Re-run | **0.39 s**, checksums only — no re-download |
| Corrupted file | detected, re-downloaded, verified (`0b44a9d7…`) |

**Not deployed.** The live installer pins v0.3.1, which has no `core/brain`;
deploying now would make every new install download 135 MB that nothing uses.
It goes live with the release that ships the brain (commit + push done).

| Model | |
|---|---|
| Name | `intfloat/multilingual-e5-small`, int8 ONNX (`onnx/model_qint8_avx512_vnni.onnx`) |
| Size on disk | 118.3 MB model + 17.1 MB tokenizer = 135.4 MB (145 MB `du`) |
| Download time (this connection) | 69.1 s |
| Load time (first call, cold) | 1.1–1.6 s |
| Decision latency, 4,172-row index, 140 items | p50 9.8–17.4 ms, p95 22.9–129 ms across 3 rounds on a heavily loaded Mac (best round p95 22.9 ms; pinned < 100 ms best-of-3) |

**Degrades honestly without it.** The gate's kNN check now reports *why* it
can't vote: `embedder_missing` or `index_empty` (was a vague `no_knn`), as the
decision's reason and a `knn_status` feature. Closed lists (refuse / clarify /
small talk / noise) still answer without it. The daemon logs
`brain_embedder_missing` **once**, at brain load, with the fix (re-run the
installer, or `python -m emma.brain fetch-model`) — never per turn (test: 5
decisions, 1 warning).

**The index is cached.** Building 4,172 rows took 20–157 s on this Mac
depending on load — on every daemon boot. `brain.db` now stores a fingerprint
of its inputs (phrasebook, labelled set, model revision, the embed-text rule)
and is rebuilt only when it changes: `python -m emma.brain decide` cold 19.6 s,
then **2.3 s** for the whole process. On this Mac "Emma, abre Safari" now
resolves locally (`open_application`); in BRAIN-1 it deferred on disagreement.

Gates: 1440 passed, 2 xfailed; ruff, mypy clean.

## Part 5 — loose ends from BRAIN-1

### The two "known-hard" L2 items — none stays expected-to-fail

| Item | Why it failed | Resolution |
|---|---|---|
| `sc_V26` "Hey Emma, abre yutub." → `open_url` | "yutub" resolved to YouTube, but `open_url` needs a domain and nothing mapped a site's name to one | **Fixed**: a closed `SITES` table (YouTube, GitHub, Google, Gmail, Wikipedia, LinkedIn, X/Twitter, Reddit, Netflix) used only when no literal URL is spoken, through the STT alias table or fuzzy match. A possessive blocks it: "abre **mi** github" is the user's page, not github.com (otherwise `sc_V34`, which accepts only `open_my_page`, could have gone wrong). Now **local**, `url=youtube.com` |
| `log_26` "…música relajante, música para trabajar en Spotify?" | The query a person means merges two fragments; a closed residue can only return what was said | **Reclassified L2 → LLM**, reason in `labelled.json` → `brain_path_note` (written by `brain_path_labels.py`) |

`HARD` and every `xfail` are gone from the suite (0 xfailed). Label split now:
L1 24, L2 43, L3 4, TPL 28, READ 12, LLM 29.

### The confirmation costs one word

BRAIN-1 Part 2's rule stays — confirmation comes from the microphone — but:
- **The question is short and names the action**: `¿Borrar la nota compras?`
  (was "Antes de hacerlo: delete_note (title: compras). ¿Lo hago? Dime sí o
  no."). It reuses the brain's action phrases (`respond.action_phrase`).
- **Only the user's very next utterance is matched** against the yes list:
  one "sí" confirms; anything else closes the window and the next attempt
  asks again — a stray "sí" said later can't confirm an old question.
- Tests: the exact question text; "espera, ¿qué nota era?" then "sí" does not
  confirm, a "sí" right after the question does.

### One labelled mapping

The docs no longer mention a second mapping: `BRAIN-1-VERIFY.md` and
`brain_path_labels.py` now describe the in-repo labels as the only ones.

### Pinned after Part 5

Shipped gate on the 140: **served 78 (55.7 %), local 52 (37.1 %), wrong 0**.
Gates: 1443 passed, 0 xfailed; acceptance `--mock-external` 99/99; ruff, mypy clean.

---

## Definition of done

| # | Item | Status |
|---|---|---|
| 1 | Phrasebook ≥ 15 ES + ≥ 15 EN per brain-reachable tool; tests green | **Done** — 83 tools, 1,252 ES + 1,245 EN, 0 exam leaks |
| 2 | Retrievers re-pinned, before/after table | **Done** — agreement 17.4 → 60.9 % |
| 3 | Gate study A–D ± argument completeness; variant by the rule; thresholds in settings with provenance | **Done** — D shipped; θ₁ off, θ₂ 0.94 |
| 4 | Embedder in the installer; honest defer without it; latency re-pinned | **Done** — `_landing` committed + pushed, **not deployed** (needs the release with `core/brain`) |
| 5 | Known-hard items resolved or reclassified; one mapping | **Done** — `sc_V26` fixed, `log_26` → LLM |
| 6 | This doc, with the headline | **Done** — served 34.3 → 55.7 %, local 15.7 → 37.1 %, wrong 0 → 0 |
