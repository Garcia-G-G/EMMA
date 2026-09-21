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
