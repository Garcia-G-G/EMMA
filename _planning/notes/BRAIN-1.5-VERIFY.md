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
