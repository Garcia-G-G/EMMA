# LAUNCH-12 — per-turn payload: verify log

Spec: `_planning/prompts/LAUNCH-12-PAYLOAD.md`. Every number here comes from a
tokenizer, not from character counts.

## Pre-flight: premises checked against the code (2026-09-21)

| Premise in the spec | Reality |
|---|---|
| `AUDIT-2026-08-03.md` §3 is "P2-9", the prompt-size finding | §3 is **audio** (echo gate, wake word). No "P2-9" exists in `_planning/`. The audit's prompt number is at §1 line 121: "~26,437 chars ≈ 6–7.5k tokens". |
| `core/conversation.py:514-966` ≈ 33k chars ≈ 8–9k tokens | Measured live: **27,918 chars = 6,994 tokens** (o200k), 44 `#` sections. The spec overstated it by ~25%. The conclusion holds. |
| Keep the "ask when unsure" invariant from LAUNCH-9 | **LAUNCH-9 was never implemented** (no commits; only the prompt file exists). There is no such invariant in code. What does exist and is kept: "If unsure, default to Spanish", the vague-search guard ("ask the user to specify"), and the "¿Quisiste decir A, B o C? → wait for the pick" rule. |
| 184 tools always-on | 184 **registered**, **172 available** on this Mac. 12 are filtered by `available()` (Linear, Jira, Notion, Postman, TablePlus, GitHub ×3, speaker ID ×3, brightness). The prompt still carries routing guidance for those 12 on every turn. |
| Realtime per-turn `session.update` | Pipecat 1.2.1 supports it: `LLMSetToolsFrame` → `_send_session_update()` (`pipecat/services/openai/realtime/llm.py:596`). **But** Emma uses `server_vad` with the default `create_response=true` (`conversation.py:1449`), so the server starts answering before the user's transcript exists. See Part 2. |
| Latency blocked by disk | Disk is now 57 % (was 99 %). **Swap is still 6.3 GB / 7.2 GB used**, so local latency remains contaminated. |

## Part 0 — Baseline (live, 2026-09-21)

**Method.** `scripts/spike_cascade/dump_payload.py` (project venv) calls the
real `_build_instructions()`, `registry.openai_tool_specs()` and
`_adapt_tool_specs_for_realtime()`, i.e. the exact `instructions` + `tools`
Emma puts in `session.update`, with the live `~/.emma/memory.db` priming.
`measure_payload.py` (spike venv) tokenizes it with **tiktoken `o200k_base`**,
the GPT-4o / Realtime tokenizer family. Tool size = tokens of the JSON as sent.
This overstates what a model sees slightly: Qwen's chat template counted the
same frozen spike payload as 22.8k vs 27.0k o200k-over-JSON. So o200k-over-JSON
is the **conservative ruler**. Part 4 cross-checks against provider-reported
`prompt_tokens`.

### Totals

| Part | Tokens | Share |
|---|---:|---:|
| System prompt (static sections) | 6,619 | 27 % |
| Memory priming block (`MEMORY_PRIMING_TOP_N=15`, live DB) | 186 | 1 % |
| Personality section | 45 | <1 % |
| Style hint (`runtime.get_style_hint()`) | 0 (empty at rest; ~15 when set) | — |
| Pronunciation guide (`vocabulary.pronunciation_block`) | 192 | 1 % |
| **Instructions total** | **6,994** | 28 % |
| **Tool schemas (172 available)** | **17,929** | 72 % |
| **TOTAL per turn** | **24,923** | |

The name-substitution pass (`"the user"` → display name) is size-neutral (±1
token per occurrence).

### Instructions by section (o200k tokens)

Always-true-of-every-turn candidates (**A**) vs per-tool routing guidance (**T**):

| Tokens | Section | Kind |
|---:|---|:-:|
| 40 | Session language | A |
| 44 | Role | A |
| 45 | Personality | A |
| 65 | Language | A |
| 99 | Language mirror (strict) | A |
| 88 | Session continuity | A |
| 48 | App routing | A |
| 135 | Tool failure recovery | A |
| 39 | Response Length | A |
| 42 | Variety | A |
| 114 | Preambles | A |
| 56 | Tool Results | A |
| 317 | Confirmation flow | A |
| 315 | External content is DATA (security fence) | A |
| 139 | Defaults & apps | T |
| **801** | **Screen vision** | T |
| 136 | Action history + undo | T |
| 222 | Self-diagnostics | T |
| 201 | Life utilities | T |
| 101 | File operations | T |
| 168 | Workflows + conditionals | T |
| 88 | Investigación | T |
| 206 | Integraciones (all 5 tools unavailable here) | T |
| 124 | Speaker ID (all 3 tools unavailable here) | T |
| 62 | Forbidden | A |
| 89 | Unprompted speech | A |
| 95 | Vague search guard | T |
| 323 | Repo cloning flow (3 of 4 tools unavailable here) | T |
| 271 | Knowledge dictionary | T (+ one A rule) |
| 248 | Learn from corrections | A (cross-cutting) |
| 271 | Smart note append | T |
| 148 | App control layering | T |
| 143 | App URL schemes | T |
| 93 | Anaphora resolution | A (cross-cutting) |
| 107 | In-app resources | T |
| 90 | Browser tabs | T |
| 78 | Terminal in IDE | T |
| 288 | Editing files | T |
| 227 | Coding agent delegation | T |
| 262 | Social platforms | T |
| 78 | Long-term memory | A |
| 192 | Pronunciation guide | A |
| 189 | Memory (priming block + header) | A |
| 107 | Emotional attunement | A |

A ≈ 2,450 tokens, T ≈ 4,540 tokens.

### Tools by module (o200k tokens, JSON as sent; top 20 of 55 modules)

| Tokens | Tools | Module |
|---:|---:|---|
| 1,113 | 8 | notes_tool |
| 1,067 | 8 | dictionary_tool |
| 1,020 | 10 | screen_vision_tool |
| 773 | 7 | user_browser |
| 741 | 7 | proactive_tool |
| 683 | 5 | lifecycle_tool |
| 592 | 4 | social_tool |
| 579 | 5 | ide_actions |
| 545 | 4 | file_ops_tool |
| 513 | 5 | mail_tool |
| 495 | 5 | reminders_tool |
| 493 | 4 | file_edit |
| 458 | 5 | calendar_tool |
| 444 | 7 | music |
| 428 | 7 | system |
| 400 | 5 | random_tool |
| 399 | 2 | app_url_tool |
| 371 | 5 | browser |
| 354 | 4 | safari_tool |
| 353 | 4 | memory_tool |

Mean **104 tokens/tool**; largest `open_in_app` 256, `append_to_note` 210.

### What the baseline implies for the 8k target

8,000 − ~2.5k always-on prompt − ~0.2k memory ≈ **5.3k tokens for tools, all
included**. At ~104 tokens/tool that is **~50 tools total, core included.**
Moved guidance makes some descriptions longer. So "core 20–30 + retrieved
top-40" (the spec's framing) **cannot fit under 8k.** The workable shape is
core ≈ 20–25 + retrieved ≈ 15–20, and the retriever's recall@15–20 is what
matters. Part 2 measures it.

## Part 1 — System prompt: 6,994 → 2,662 tokens

**Mechanism.** Per-tool routing guidance moved to `tools/guidance.py`
(`GUIDANCE: dict[tool_name, str]`), appended by `registry._spec()` to the
description the model receives. It's a separate map, not a third docstring
paragraph, because `tools/base.py:_docstring_summary` keeps only the first two
paragraphs, and many docstrings already use both. A third paragraph would have
been silently truncated, which is exactly the loss the spec warned about.

**What stayed always-on** (true of every turn): Session language, Role,
Personality, Language, Language mirror, Session continuity, App routing, Tool
failure recovery, Response length, Variety, Preambles, Tool results,
Confirmation flow, the untrusted-content fence, Forbidden, Unprompted speech,
Vague search guard (the closest existing "ask when unsure" rule; LAUNCH-9's
invariant was never implemented), **Clarification picks** (the cross-tool
"¿Quisiste decir A, B o C? → wait → `picked=`" rule and identity resolution,
kept from Knowledge dictionary), Learn from corrections, Anaphora, Long-term
memory, Pronunciation guide, Memory, Emotional attunement.

**What moved** (21 sections, ~4.3k tokens): every section marked **T** in the
Part 0 table. Split sentence by sentence onto the tool each sentence is about
(93 tools carry guidance). Cross-tool sentences are repeated on each tool they
govern, e.g. the edit-flow contract on all four `edit_file_*` tools, and
"READ THE TEXT BACK" on all four social tools.

| | Before | After |
|---|---:|---:|
| Instructions (o200k) | 6,994 | **2,662** |
| of which memory priming | 186 | 186 |
| Tool schemas, all 172 (o200k) | 17,929 | 22,189 |
| Total, all tools sent | 24,923 | 24,851 |

The total doesn't drop yet: the guidance is paid only when its tool is sent,
and until Part 2 every tool is sent. The 12 unavailable tools' guidance
(Integraciones, Speaker ID, 3/4 of Repo cloning ≈ 0.6k) is now actually gone,
because it travels with tools that aren't offered.

**Nothing lost: verified.**
- 165 quoted trigger phrases were extracted from the removed sections of the
  pre-LAUNCH-12 prompt. All 165 are present verbatim in some tool description.
  The first pass found 7 I had re-cased while splitting ("Dame" vs "dame",
  "¿Cómo va?" vs "¿cómo va?"), and those were restored verbatim. I had also
  dropped "¿Cómo estás?" from `diagnose_self`'s triggers. That phrase is
  labelled `__none__` in the frozen set, so dropping it would have quietly
  tuned the guidance to the test set; it was restored.
- `tests/test_tool_guidance.py`: every guidance key is a registered tool (no
  orphans on rename); guidance reaches `_spec` untruncated and the Realtime
  payload; all moved phrases reach a description; no moved section header
  remains in the prompt; prompt ≤ 12,500 chars (≈ 3k tokens at the measured
  4.17 chars/token).
- `test_screen_vision_routing.py::test_system_prompt_carries_the_web_content_routing_rule`
  pinned the rule to the prompt. It's now
  `test_web_content_routing_rule_reaches_the_model`, with the same three
  assertions against the `describe_screen` / `look_at_screen` specs.

**Suites.** `pytest tests/`: 1196 passed, 4 failed. The 4 are
`test_memory_semantic.py`, live `text-embedding-3-small` calls rejected with
401 (invalid key). They fail identically at HEAD with this change stashed, so
they predate this work. Personality byte-identity, prompt-injection,
confirmation, language and continuity tests: green. Acceptance
`--mock-external`: **99/99**. Caveat: mock mode doesn't run a model, so it
can't show routing regressions; Part 4 does. `ruff check .` clean; `mypy`
clean on touched files. (`ruff format --check .` fails on ~150 files at HEAD
already; not touched.)

## Part 2 — Tools: 172 always-on → core + retrieval

### The gate: retrieved-40 finished on the frozen set (140/140)

Run before building anything, as the spec requires. Frozen payload (pre-Part-1
prompt + pre-guidance descriptions), Qwen3 8B local, BM25 top-40, no core:

| Condition | REAL (120 items) | Correct tool offered | Selection when offered |
|---|---:|---:|---:|
| full-184 | **71.7 %** (86/120) | 100 % | 71.7 % |
| static-40 | 41.7 % (50/120) | 46.7 % | 78.6 % |
| **retrieved-40** | **60.0 %** (72/120) | 83.3 % | 69.0 % |
| Realtime, historical (log items) | 58.0 % (29/50) | — | — |

**Verdict: retrieval passes the gate** — it beats the fixed 40 by 18.3 points,
which is what the spec said to check. It still trails full-184 by 11.7, and the
cause is recall: the right tool was absent from 17 % of requests. Latency for
this condition is not reported (swap was 6.3/7.2 GB; contaminated).

### Retriever choice (offline, no LLM: recall of an acceptable tool)

Core of 23 + top-k from the rest, over the **new** guided descriptions:

| Retriever | recall@10 | recall@20 | Ships? |
|---|---:|---:|---|
| BM25 (name + description + moved trigger phrases) | 90.4 % | 93.9 % | **yes** — no dependency |
| nomic-embed-text (local, 274 MB) | 76.5 % | 82.6 % | no — worse than BM25 |
| qwen3-embedding 0.6b (local, 639 MB) | 93.0 % | 94.8 % | no — needs Ollama + 639 MB |
| BM25 + qwen3-embedding (RRF hybrid) | **95.7 %** | 95.7 % | not yet — same dependency |

BM25 alone over the *old* descriptions was 72.2 %@10 / 79.1 %@20, so moving the
trigger phrases into the tools (Part 1) is worth ~18 points at k=10. The spec
was right not to assume embeddings win: the small one loses outright, and the
good one costs a 639 MB model and an Ollama runtime the installer doesn't ship,
for +5.3 points. `memory/embeddings.py` is not an option at all — it's the paid
OpenAI API (zero-spend rule, and the key has no credit). **Shipping BM25**;
`find_tools` covers its misses, and the hybrid is the measured upgrade path.

Production selector, counting only tools available on this Mac: **90.7 %**
(98/108) with core + top-10.

**Query stopwords.** The wake phrase opens nearly every utterance, and "emma"
matched `restart_emma`, `shutdown_emma`, `add_vocabulary_word`… so those were
pre-loaded on almost every turn. Wake words are dropped from *queries* only
(`_QUERY_STOP`; descriptions untouched). Recall is unchanged at 90.7 % — this
buys no score, it stops wasting sticky slots.

### Design as built

* **Core: 24 tools** (`core/tool_selection.py:CORE_TOOLS`), fixed order (part of
  the cache key). Justification per group: **4** named by the always-on prompt,
  so the rule breaks without them (`remember_stt_correction`,
  `recall_last_action`, `recall_facts`, `set_conversation_tone`); **2** session
  lifecycle; **1** memory write; **5** screen vision (audit 2026-08-03 §2 calls
  it non-negotiable, and the AX→screenshot fallback needs both layers); **2**
  background-task status (CLAUDE.md convention); **9** everyday actions, chosen
  by frequency in the maker's production logs; **1** the loader.
  *Caveat:* 50 of the 140 frozen items were mined from those same logs, so the
  frequency evidence and the test set overlap. The core was picked by category
  rule, not by fitting the frozen set.
* **Loader**: `find_tools(need)` (`tools/tool_loader_tool.py`). The model
  describes the need in its own words; BM25 retrieves; the enlarged list is
  pushed to the live session; the model calls the real tool next. One extra
  round-trip, only on turns the core can't serve.
* **Sticky**: each finished user transcript pre-loads its matches for following
  turns (`_UserSpeechTap` → `tool_selection.load`), LRU, `STICKY_MAX=12` **and**
  `STICKY_CHAR_BUDGET=5000` chars. A count alone can't bound tokens (the 12
  largest tools are 3.5k); the size bound can. Best-ranked tools are inserted
  last so eviction drops the weakest first.
* **Realtime wiring**: `LLMUpdateSettingsFrame` with a `session_properties`
  delta (pipecat's public path) → `_update_settings` → `session.update`. Only
  `tools` changes; instructions stay byte-identical, so the cached prefix
  survives. Chosen over transcript-gating because `server_vad` +
  `create_response=true` means the server answers before the transcript exists,
  and gating would add transcription latency to every turn.
* **`TOOL_RETRIEVAL=false`** restores the old full-set session, now capped at 128.

### Measured payload (o200k, live)

| | Tokens |
|---|---:|
| Session opens (prompt 2,662 + 24 core tools 3,429) | **6,091** |
| 140 frozen utterances as consecutive turns | median **7,338**, max **7,422** |
| Worst case (prompt + core + largest sticky set the size cap allows) | **6,945** |

Every turn is under 8,000. Tools per turn: median 32, max 36 — far under 128.

### The 128 ceiling

`registry.openai_tool_specs()` applies `min(REALTIME_TOOL_BUDGET, 128)`;
`tool_selection.cap()` applies the same and logs every dropped name at ERROR.
`tests/test_tool_selection.py` pins the constant in both modules, the fallback
at budgets 175/0/10000, the session-open set, and the worst case.
`REALTIME_TOOL_BUDGET` (175) survives as the registry-growth alarm; CLAUDE.md's
tool-budget convention is rewritten to match, including the "no 128-tool
ceiling" claim, which was true of Realtime and is false of Groq.

### Tests

`tests/test_tool_selection.py` (23): ceiling, core membership and stability,
retrieval quality, LRU + size bounds, best-match survival, loader behaviour,
a failed `session.update` not breaking the turn, the session opening with the
core rather than the registry, the fallback, wake-phrase stopwords, and
**every available tool reachable** (nothing silently dropped).

`tests/test_tool_budget.py`: three tests probed availability through
`openai_tool_specs()`, which is now only the capped fallback — GitHub and
Notion sit in unlisted modules and are trimmed past 128, so they failed. They
now probe `registry.available_specs()`, the pool selection draws from, which is
what those tests mean by "advertisable". `test_budget_zero_disables_the_cap`
became `test_budget_zero_leaves_only_the_hard_ceiling`: 0 used to mean "no cap",
and the ceiling is now absolute.

**Suites:** 1218 passed, 4 failed (the pre-existing live-embeddings 401s).
Acceptance `--mock-external`: 99/99. `ruff check .` clean. `mypy .` clean
(182 files).

**Not verified live.** The `session.update` tool swap is exercised against a
fake applier, never the real Realtime API: zero spend, no credit. What Pipecat
and the API do with a mid-session tool change is argued from source
(`pipecat/services/openai/realtime/llm.py:682-748`), not observed.

### Environment note (not caused by this work)

This Mac's CoreAudio capture hangs: a bare `sounddevice` record + `sd.wait()`
never returns, outside Emma entirely. Two tests block on it forever
(`test_diagnose_self_explains_a_missing_accessibility_grant`,
`test_wizard.py::test_mic_test_never_crashes`); runs above stub `_mic_rms` and
deselect the wizard one. **Latent product bug it exposes:**
`core/diagnostics.py:_mic_rms` calls `sd.wait()` with no timeout, so a stuck
mic hangs `diagnose_self` — and therefore a voice turn — forever. Out of scope
here; worth its own fix.

## Part 3 — Memory priming, the tail of the prompt, and cache order

**Memory priming is already relevance-ranked and already small.**
`priming_block(context=…)` ranks semantically against the live conversation
(the last 6 user turns) and falls back to confidence order — the spec's
suggestion is implemented (25-A). Measured on the live DB at
`MEMORY_PRIMING_TOP_N=15`: **186 tokens**, 2.3 % of the 8k budget. Ranking it
harder buys nothing worth the risk, so `TOP_N` is unchanged.

**Everything else appended after the static prompt, audited:**

| Appended | Tokens | Varies |
|---|---:|---|
| Pronunciation guide (`vocabulary.pronunciation_block`) | 192 | only when the user teaches a word |
| Memory block | 186 | per session |
| Style hint (`runtime.get_style_hint`) | 0 at rest, ~15 when set | per session |
| Name substitution (`"the user"` → display name) | ±1 per occurrence | per user, never per turn |

**Cache order fixed.** The memory block used to sit *before* the static
"Emotional attunement" section, and the style hint was appended inside it, so a
changed fact or a detected mood invalidated the cached prefix from that point
on. Both now come last, after every static section: the prompt is
stable-prefix-first, per-session material at the tail. Tool order already
follows the same rule — fixed core first, retrieved tools appended (pinned by
`test_core_is_a_stable_prefix`).

The style hint also got its own `# Tone for this conversation` header; it used
to be a loose bullet dangling under "Emotional attunement", which made the
static section's bytes depend on it.

**Still unverifiable:** whether the provider actually reports
`cached_tokens > 0`. Production has never recorded a Realtime session and there
is no credit, so cacheability here is a property of the bytes we send, argued
from ordering — not an observation.

**Suites:** 1218 passed, 4 failed (the pre-existing live-embeddings 401s).
Acceptance `--mock-external` 99/99. `ruff check .` clean.

## Part 4 — The spike re-run on the new payload (2026-10-08)

Same frozen 140-item set (`labelled.json`), same scorer (`score.py`: first tool
call vs the acceptable set), same thresholds as SPIKE-CASCADE §1, unchanged:
**migrate ≥ 90 %, don't < 85 %**. Payload = the shipped one at `9bfac43`:
`system_prompt_v2.txt` (Part 1+3 prompt) and `specs_avail_guided.json` (the 173
tools *available on this Mac*, with their guidance). Tools are picked by
`core.tool_selection` itself, not a copy. Qwen3 8B, Ollama, `think:false`,
temperature 0. Raw rows: `results/m1_ollama_qwen3_8b_{core10,loader}.jsonl`.

### A comparability correction, found during this run

The SPIKE full-184 baseline offered **all 184 registered tools**, availability
ignored. Production offers only what passes the availability probes, and on
this Mac `my_repos`, `search_github` (no `gh`) and `set_brightness` don't. Seven
of the 120 real items are labelled *only* with those tools, so core10 could
never get them right, and neither could Emma on this machine. Both scopes are
reported; the like-for-like one is the 113.

### 4a. Qwen3 8B, production selection (core + BM25 top-10): 140/140, 0 errors

| Condition | Prompt tokens (median) | REAL, all 120 | REAL, 113 with an available label | Scenario | Log |
|---|---:|---:|---:|---:|---:|
| full-184, old payload (SPIKE baseline) | 22,808 | **71.7 %** (86/120) | 69.9 % (79/113) | 71.4 % | 72.0 % |
| **core10, new payload** | **6,723** | 67.5 % (81/120) | **71.7 % (81/113)** | 70.0 % | 64.0 % |

**Against the threshold: < 85 % either way → *don't migrate*, unchanged.**

**Accuracy held at 30 % of the tokens, and on the like-for-like set it rose
1.8 points.** That is within noise at n=113, so the defensible claim is "no
loss", not "a gain". The paired breakdown shows where the two effects are:

| Same 113 items | n | full-184 | core10 |
|---|---:|---:|---:|
| right tool was offered by retrieval | 103 | 74 (71.8 %) | **80 (77.7 %)** |
| right tool missed by retrieval | 10 | 5 | 1 |

- **Where retrieval found the tool, the smaller payload selects 5.9 points
  better** than the full registry on the same items. That is the audit's
  prediction, confirmed where it can be tested.
- **Real retrieval misses: 10 of 113 (8.8 %)**, not the 14–17 % the raw offer
  recall suggests: `current_time`, `add_reminder`, `mute`, `open_my_page` ×2,
  `list_notes`, `post_to_x`, `start_timer`, `recall_secret`, `delegate_to_codex`.
- **`find_tools` was on offer in all 120 turns and called 0 times**, including
  on the 10 misses. When the right tool is missing, Qwen calls a neighbour or
  nothing (23 of 39 misses are "no tool call", the same failure mode as
  full-184's 26 of 34).

### 4b. The loader round-trip: core only, then `find_tools`: 140/140, 0 errors

The `find_tools` path had never been exercised. Shape, matching production:
turn 1 offers the 24 core tools (incl. `find_tools`) only; if the model's first
call is `find_tools`, its call and the tool's result, serialized like
`tools/tool_loader_tool.py` returns it, go back into the conversation, the
tools retrieved on the model's own `need` are added, and the second choice is
scored. This is what a Realtime first turn sees when the transcript pre-load
hasn't landed yet.

| Condition | REAL, all 120 | REAL, 113 available | Prompt tokens (median) | Warm p50 |
|---|---:|---:|---:|---:|
| core10 (pre-load before the call) | 67.5 % | 71.7 % | 6,723 | (contaminated) |
| **loader (core only + `find_tools`)** | **45.8 %** (55/120) | **48.7 %** (55/113) | 5,808 | 5.4 s |

**The loader does not close the recall gap for Qwen; it opens a much bigger
one.** About 59 real items need a tool outside the core; Qwen called
`find_tools` on **7** of them (5.8 % of real turns). On the other ~52 it called
a core neighbour (`open_application` for `open_in_app`, `play_track` for
`play_playlist`, `run_command` for IDE/file edits, `read_pane_text` for
`read_note`) or nothing (34 of 65 misses are "no tool call"). Of the 7
round-trips, 2 ended correct (`close_duplicate_tabs`, `close_current_tab`); 4
were the unavailable GitHub tools; 1 was a `__none__` item where loading was
itself the mistake. The second hop works mechanically: the model reads the
result and calls a loaded tool every time.

What this means for each architecture:

- **Cascade (any text model):** the transcript exists *before* the LLM call, so
  pre-loading is always on time and **core10 is the honest shape**. The loader
  result doesn't apply.
- **Realtime:** turn 1 can race the pre-load, so it may see the loader shape.
  Whether *Realtime's* model calls `find_tools` more readily than Qwen is
  unmeasured (no credit). Qwen's 7/59 is a warning, not a measurement of
  Realtime. Measuring it is the first thing to do with credit, and the cheap
  fix if it's bad is to await the pre-load before the response is created,
  rather than to rely on the model asking.

Latency: 4a's 38 s warm p50 is contaminated (5.9 GB disk free, heavy swap; it
is physically backwards against SPIKE's 6.2 s at 3.4× the tokens). 4b's 5.4 s
p50 at 5.8k tokens is plausible but ran on the same machine state; neither is
reported against the 1.5 / 2.5 s threshold.

### 4c. Groq free (gpt-oss-120b), core10: 46/140 scored, 94 pending

Stopped by Groq's free-tier **daily** limit (200k tokens/day, HTTP 429 TPD),
not the per-minute one. At ~6.6k tokens a call that is ~30 items/day, so the
remaining 94 take ~3 days of resumed runs. Scored when complete; not reported
on 46 items (45 scenario + 1 log, the head of the file: not a sample).

### Suites at this commit

`mypy .` clean (182 files). The test/acceptance/ruff numbers are Part 3's: this
part changes only `scripts/spike_cascade/` and this doc.
