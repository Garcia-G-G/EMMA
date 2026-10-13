# BRAIN-1.5 — verify log

Every number for BRAIN-1.5. The exam is the 140 labelled utterances; nothing
from them enters the phrasebook (test-enforced).

## Headline — local % at 0 wrong on the 140

| | Served without an LLM (local + templates) | Local tool decisions | Wrong |
|---|---:|---:|---:|
| **Before** (BRAIN-1, `be86cfc`) | **34.3 %** (48/140) | **15.7 %** (22/140) | 0 |
| **After** (BRAIN-1.5) | *(Part 3)* | *(Part 3)* | |

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
