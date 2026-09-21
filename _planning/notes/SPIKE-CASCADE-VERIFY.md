# SPIKE-CASCADE — Verify

Measurement spike: should Emma move from audio-to-audio (OpenAI Realtime) to a
cascade (STT → LLM → TTS), or to Gemini Live? Nothing here ships. Throwaway code
lives in `scripts/spike_cascade/`. `core/` is untouched.

---

## 1. Thresholds — fixed 2026-09-21T19:56:58Z, before any data was collected

This section is not edited after the first measurement is recorded. If a
threshold later looks wrong, the correction goes in §8 as a note, and the verdict
is still scored against the numbers below.

### Constraint in force: zero API spend

The OpenAI account has no credit, so the Realtime baseline cannot be measured.
The spike prompt's relative thresholds ("within N points of Realtime") are
reframed as **absolute** thresholds. Derivation, so the numbers aren't arbitrary:

- The prompt names **≥ 90 %** as actionable for tool selection. The original
  table put *migrate* at 5 points below baseline and *don't* at 10 points below.
  Read 90 as "baseline − 5", which implies a baseline of 95, so *don't* sits at
  **< 85**. The 5-point band between the two columns is the same as the
  original table's.
- Spanish accuracy had "within 3 points of today". "Today" is audio-native and
  produces no transcript of its own to score. The absolute replacement puts the
  same 3-point band against a 95 % word-accuracy bar.

| Metric | Migrate if | Don't migrate if | Between = inconclusive |
|---|---|---|---|
| **Tool-selection accuracy**, text model, full registry (184 tools), labelled set | **≥ 90 %** | **< 85 %** | 85–90 % |
| **End-to-end latency**, end-of-speech → first audio out, p50, this Mac | **≤ 1.5 s** | **> 2.5 s** | 1.5–2.5 s |
| **Spanish word accuracy** (1 − WER), the maker's own recordings, Mexican Spanish | **≥ 95 %** | **< 92 %** | 92–95 % |
| **Code-switch fidelity**: share of code-switched utterances where the transcript keeps both languages (no whole-utterance flip, no translation) | **≥ 90 %** | **< 80 %** | 80–90 % |
| **Cost per turn** vs Realtime at published pricing + measured/assumed cache state | **≥ 5× cheaper** | **< 2× cheaper** | 2–5× |

Scoring rules, also fixed now:

1. **Measurement 1 is the gate.** If tool selection lands in *don't*, the verdict
   is *don't migrate* regardless of every other row. Cost alone never produces
   *migrate*.
2. **The local model and the cloud models are scored separately.** A frontier
   cloud model passing does not make the $0 story true; only a local pass does.
3. **Spanish is a veto.** A *don't* on either Spanish row kills the migration
   even if everything else passes.
4. **Any *inconclusive* row makes the overall verdict *inconclusive*,** with the
   specific next measurement named.
5. **Accuracy scoring**: a prediction is correct if the model's first tool call
   names the labelled tool, or, for a "no tool" label, if it calls no tool.
   Arguments are not scored. For utterances labelled with an acceptable-set
   (e.g. two tools that genuinely both satisfy the request), any member counts;
   the label set is frozen before the first model run.
6. **Latency** is p50 over ≥ 20 runs on this Mac, same day, and includes
   end-of-speech detection (VAD tail), not just model time.
7. **Gemini Live** (audio-to-audio, not a cascade) is scored on the latency,
   Spanish-fidelity and cost rows. Its tool selection is scored on the same
   labelled set if it can be driven with text or audio input.

### Protocol details, fixed 2026-09-21T20:02Z (still before any model run)

- **Labelled set** frozen at 2026-09-21T20:00:57Z,
  `scripts/spike_cascade/data/labelled.json`, sha256 `c1ef5b5d…7711`
  (140 items: 70 from `tests/acceptance/scenarios.yaml`, 50 real utterances from
  Emma's logs, 20 synthetic). **Accuracy is reported on the 120 real items;
  synthetic items are reported separately and never enter the verdict.**
  Generator: `scripts/spike_cascade/build_labelled.py` (the data dir is gitignored
  because it holds real speech).
- `_planning/` is gitignored, so git cannot timestamp this file. The file's
  mtime and the ordering of this section are the only evidence that the thresholds
  predate the data.
- **"184 tools"** = every registered tool (`specs_all.json`), including the 12
  that are unavailable on this Mac. The live registry advertises **172** here.
- **40-tool conditions**, both run:
  - **(a) static-40**: the first 40 tools in the registry's own trim order. Core
    alone is **73 tools**, so a 40 budget cannot be met under the current
    ranking without amputating core. Static-40 is therefore a slice of core, and
    items whose correct tool falls outside it are scored wrong (a coverage loss
    is real). Accuracy is also reported on the in-set items only.
  - **(b) retrieved-40**: per-utterance BM25 top-40 over tool name +
    description, which simulates the strategy doc's `find_tool`/lazy-load design.
    Retrieval recall@40 (was a correct tool in the 40?) is reported separately
    from selection accuracy.
- **Local model settings**: Ollama native `/api/chat`, `think: false`,
  temperature 0, same real system prompt (27,918 chars rendered today, including
  the live memory priming block). The prompt estimated ~33k chars; the measured
  figure is what was used.

### Baseline status

- **Realtime baseline: not measured (no credit).** Filling it in would cost
  roughly **$5**: ~100 utterances × ~18k input tokens (at `gpt-realtime-2`
  text-input pricing, mostly cached after the first turn) plus short outputs.
  This is not a guess at the *result*, only at what measuring it would cost.

---

## 2. Tool-selection accuracy

Measured 2026-09-21 on this Mac: Apple M3 (base), 16 GB RAM, macOS 26.6.2,
~10–12 GB of swap in use during the runs, and 8–12 GB of disk free. Raw rows are
in `scripts/spike_cascade/results/m1_*.jsonl`; scorer: `score.py`.

### Results (real items = 120; synthetic reported separately, never in the verdict)

| Candidate | Condition | Real items | Scenario | Log | Synthetic |
|---|---|---|---|---|---|
| **Realtime (today)** | 172–183 advertised | **not measured (no credit)** | — | — | — |
| Realtime, *historical* | as shipped at the time | — | — | **58.0 %** (29/50) ¹ | — |
| **Qwen3 8B, local** (Ollama, `think:false`) | **full, 184** | **71.7 %** (86/120) | 71.4 % | 72.0 % | 65.0 % |
| Qwen3 8B, local | static-40 | 41.7 % (50/120) | 40.0 % | 44.0 % | 55.0 % |
| Qwen3 8B, local | static-40, **correct tool was offered** | 78.6 % (44/56) | | | |
| Qwen3 8B, local | full-184 **on those same 56 items** | 71.4 % (40/56) | | | |
| Qwen3 8B, local | retrieved-40, **partial: 44/140, stopped** | 63.6 % (28/44) vs full-184 **75.0 % (33/44)** on the same items | | | |
| Gemini Flash / Flash-Lite (free tier) | — | **not run — see §8, privacy** | | | |
| Llama 3.3 70B via Groq (free tier) | — | **not run — needs a Groq key** | | | |
| Frontier / mid-tier paid models | — | not run (zero-spend constraint) | | | |

¹ Scored from the logs: the first tool Realtime fired within 25 s of each
utterance. This is **not** a like-for-like baseline. The utterance text is the
side-channel ASR transcript, while the model heard the *audio*, so a garbled
transcript ("KMO", "Hmm", "install") may be a real request that Realtime answered
correctly. On the 42 non-noise log items Realtime's historical hit rate is
27/42 = 64 %. That's weak evidence that today's number is closer to Qwen's than
to the 95 % that §1 implied, which is exactly why the baseline would be worth
the ~$5.

### Confusion: what the local model gets wrong

On the full registry, **26 of the 34 real misses are "no tool call"**, not a
wrong pick. The model answers in text instead of acting. Examples:

- "Hey Emma, pon música de Bad Bunny." → replies *"Pon música de Bad Bunny."*
  (echo), no call.
- "Hey Emma, recuérdame mañana a las nueve llamar al dentista." → *"Listo, lo
  dejo."* with **no `add_reminder` call**. It claims an action it didn't take,
  which is the worst failure mode for a voice assistant.
- `remember_*` cluster: 7/18 correct. Most misses are again "no call"
  (`remember_fact`, `remember_stt_correction`, `remember_term`,
  `remember_contact`, `remember_user_profile`, `remember_secret`).
- Screen cluster: 8/10 correct; the one confusion was `describe_screen` → `where_am_i`.
- True wrong-tool confusions are rare: `open_my_page → my_repos` (2×),
  `open_my_page → open_url`, `open_application → app_focus`,
  `browser_do → browser_navigate`.
- Noise, "none", and safety items were near-perfect (8/8, 12/13, 6/7), but
  that's easy for a text model because the text it sees is already clean.
- The model addressed the user as **"Claude"** in several replies. It's
  picking a vocabulary fact out of the memory priming block and treating it as
  the user's name.

### 184 vs 40 tools

On the same 56 items where static-40 offered the right tool, cutting 184 → 40
raised accuracy **71.4 % → 78.6 %**, a 4-item difference on n=56 and not
decisive. **Static-40 as a real budget is a coverage disaster:** the right tool
was offered for only 47 % of real utterances, because 40 < the 73-tool core.
Tool reduction is only viable with per-turn retrieval (§8, retrieved-40).

### Retrieved-40 (partial, stopped at 44/140 by the maker to relieve memory and disk)

On the first 44 items, all real, per-utterance BM25 retrieval **lost to sending
all 184 tools**: 28/44 vs 33/44. **Retrieval recall@40 was only 75 %**, so the
right tool wasn't among the 40 for 11 of 44 utterances. When it was offered,
selection was 81.8 % (27/33). Lazy loading is only as good as its retriever, and
naive BM25 over Emma's docstrings isn't good enough. Resume with
`eval_tools.py --cond retr40` (it skips finished ids) if an embedding retriever
is ever worth testing.

### Against the threshold

Qwen3 8B, full registry: **71.7 % < 85 % → *don't migrate*.** Even the best
local slice (78.6 % on 40 in-set tools) is below 85 %.

## 3. Latency

> **⚠ Every latency number in this section is contaminated.** All of them were
> taken with the disk at 96–99 % full and **~10–12 GB of swap in use** on a
> 16 GB machine. Local generation (~5 tok/s) and prefill (~37 tok/s) are
> probably depressed by paging, and Kokoro's CPU timings may be too. The ElevenLabs
> numbers are network-bound and least affected. Treat every row as an
> *upper bound under memory pressure*, not a clean measurement. **No further
> latency measurements until the disk is freed** (maker's instruction,
> 2026-09-21). The *qualitative* conclusions are unaffected, because they're
> architectural: two LLM passes per tool turn, a cold prefill in minutes, and
> retrieval breaking the prefix cache. Re-run the numbers on a clean machine
> before quoting any of them.

Same Mac, same day. Per-stage p50 / p95.

| Stage | Engine | p50 | p95 | n | Note |
|---|---|---|---|---|---|
| STT, end-of-speech → final | Parakeet-MLX / whisper.cpp | **not measured** | | | needs the maker's recordings (§4); models not downloaded (disk) |
| LLM, full 184 tools, **warm** (prefix cached) | Qwen3 8B | **6.19 s** | **15.46 s** | 139 | complete response; tool-call turns p50 6.7 s, text turns 5.9 s |
| LLM, full 184 tools, **cold** | Qwen3 8B | **> 10 min** | | 1 | 22.8k-token prompt, prefill **~37 tok/s** |
| LLM, static-40, warm | Qwen3 8B | 5.34 s | 9.69 s | 140 | 10.6k-token prompt |
| TTS first audio | ElevenLabs Flash v2.5 (network) | **525 ms** | 1,014 ms | 20 | 780 chars of prepaid quota |
| TTS first audio | Kokoro fp32 ONNX, local CPU | **1,137 ms** | 1,294 ms | 20 | |
| TTS first audio | Kokoro int8 ONNX, local CPU | 1,920 ms | 2,365 ms | 20 | int8 is *slower* on Apple Silicon |
| **Total**, end-of-speech → first audio | Qwen3 8B + Kokoro | **≥ 7.3 s** p50 before STT and VAD | | | lower bound |
| Realtime, same measure | gpt-realtime | **not measured (no credit)** | | | |

What drives it:

- **Generation runs at ~5 tok/s** (median), a memory-bandwidth and pressure
  ceiling on a 16 GB base M3 holding an 8 GB model with a 23k-token KV cache.
  A 22-token tool call costs ~4–5 s by itself.
- **A tool turn needs two LLM passes** (decide the call, then speak the result),
  so a realistic spoken answer after a tool is ~2× the table above.
- **Cold start is unusable.** The full prompt takes over 10 minutes to prefill
  from scratch. Every session would need the model kept resident and warm; the
  system prompt changes per session because of memory priming, which
  invalidates the cache.
- **Retrieval breaks the prefix cache.** Retrieved-40 changes the tool block on
  every turn, so each call re-prefills ~4k tokens at ~40 tok/s. See §8.
- The **~1.5 s wake window** (chime + probe + pipeline build) could hide STT
  model load and cache warm-up, but it can't hide a 6 s decode.

Against the threshold: the local LLM stage alone is **> 2.5 s p50**, so
end-to-end is *don't migrate* for Qwen3 8B on this Mac, whatever STT measures.
STT can only add time. **Caveat:** that result is from a swapping machine
(see the warning above). It would need to improve by more than 2.4× to reach
2.5 s, which is plausible for generation on a clean 16 GB M3 but not certain. The
latency row is therefore **provisional**. The accuracy gate (§2), which memory
pressure doesn't affect, carries the local verdict on its own.

## 4. Spanish and code-switching

**Not measured. Blocked on the maker's own voice**, and deliberately not
substituted with TTS samples. What is ready: the spike venv has
`parakeet-mlx` and `jiwer` installed and whisper.cpp via brew; the STT harness
itself is not written yet, and the Parakeet v3 and whisper
large-v3-turbo weights (~2 GB) are not downloaded (disk at 99 %).

One thing already visible in the logs: Realtime's *side-channel* transcript of
the maker's speech is badly degraded ("Subtítulos por la comunidad de
Amara.org", "Hey Mycroft", "AYEMA ABRE MI GITCHUB", Hebrew and Portuguese
hallucinations). Today that transcript is cosmetic, because the model hears the
audio. **In a cascade, that quality of transcript would be the model's only
input.** This is the specific risk the prompt names, and the existing evidence
leans toward it being real.

## 5. Cost per turn

Published pricing, fetched 2026-09-21 (developers.openai.com/api/docs/pricing),
`gpt-realtime-2 / 2.1`: text in **$4.00/M**, cached text in **$0.40/M**, text out
$24/M, audio in $32/M, audio out $64/M.

Representative turn = one utterance, one tool call, one spoken answer, which is
**two model responses**, each re-reading the context. Context uses the audit's
18,229 text tokens/turn at ~183 tools (Qwen's tokenizer measured 22,808 for the
same prompt and schemas). Audio for a ~4 s utterance and ~5 s answer is bounded
at ≤ $0.02 per turn. Conversation rate is assumed at 3 tool turns/min, and
history growth is ignored, so these are floors.

| | Per turn | Per 10 min | Per 60 min |
|---|---|---|---|
| Realtime, tool block **uncached** | ~$0.15–0.17 | ~$4.5–5.0 | ~$27–30 |
| Realtime, tool block **cached** | ~$0.02–0.035 | ~$0.6–1.0 | ~$3.6–6.3 |
| Cascade, all local (Qwen3 8B + Kokoro + local STT) | **$0** marginal | $0 | $0 |
| Cascade, Groq free tier LLM + Kokoro | $0 within free limits | $0 | $0, but rate limits bite |

**Whether Realtime's tool block is actually cached: STILL UNKNOWN. Production
has no data to answer it.** Checked 2026-09-21 with the maker's go-ahead,
strictly read-only (`sqlite3` URI `mode=ro` via the host's python, since the
host has no `sqlite3` binary; aggregates only, no row contents):

- The live `emma-backend` container mounts `/root/emma-data → /data` with
  `DATABASE_URL=/data/backend_emma.db`, so this is the right file.
- `usage_events` holds **3 rows total**, all `kind = http`/`http-stream`
  (gpt-4o-mini ×2, text-embedding-3-small ×1), created within the same second
  in July: a smoke test. **Zero `realtime` rows. No Realtime session has ever
  been metered in production.** The DB file's mtime is 2026-07-17.

**The managed billing path has zero production validation.** Metering
(`backend/metering.py`), the Realtime proxy's `response.done` parsing
(`backend/openai_proxy.py`), `cached_tokens` capture, balance reservations and
per-second charging have **never processed a real Realtime session in
production**. Any claim about managed-tier margins, COGS/min, or "the meter is
correct" rests on tests and local runs only. Before taking real money, one
end-to-end paid session through the production proxy should be checked row by
row against OpenAI's own usage dashboard.

So the audit's 18,229 tokens/turn has never been checked against a real
`cached_tokens`. **The only way to answer it is one paid Realtime session**,
which reads `usage.input_token_details.cached_tokens` from `response.done` on
turn 2+. That's a few cents, and it comes free with the ~$5 baseline in §1. Until
then the cost table above carries both cases, a ~7× spread.

Against the threshold: a fully local cascade is ≥ 5× cheaper under any cache
assumption, so the cost row says *migrate*. By rule 1 in §1 that can't carry
the verdict.

## 6. What a cascade gives up (barge-in, feel)

**Not measured.** An honest barge-in description needs a running prototype with
local STT and someone talking over it for a few minutes. That needs the STT
weights and the maker at the mic. It's also the step closest to "building the
cascade", so it's deferred until Measurement 1 gives a reason to do it. So far
it doesn't.

## 7. Verdict (provisional — Qwen3 8B local only)

| Row | Measured | Threshold call |
|---|---|---|
| Tool selection, local | 71.7 % | **don't** (< 85 %) |
| Latency, local | LLM stage alone 6.2 s p50 | **don't** (> 2.5 s) |
| Spanish / code-switch | not measured | — |
| Cost | local ≥ 5× cheaper | migrate, but can't decide alone |

**For the zero-cost local cascade on this hardware: don't migrate.** It fails
both the gate (Measurement 1) and latency, independently, and neither result
depends on the unmeasured rows.

**For a cloud cascade (Groq): inconclusive, pending. Gemini and Gemini Live:
excluded** (maker's decision 2026-09-21: free tier trains on content, consistent
with the Mistral exclusion).

Next measurements, in order:

1. ~~Is Realtime's tool block cached? Read-only production query.~~ **Done, and
   there's no data** (§5). It's only answerable with a paid Realtime session.
2. **Llama 3.3 70B on Groq, full 184 tools, same frozen set**, run twice:
   `tool_choice: auto` and `tool_choice: required`. The second tests whether the
   "answers in text instead of acting" failure (26/34 of Qwen's misses) goes
   away when the model is told it must act. `required` makes the 25 none-only
   items unwinnable by construction, so it's scored on the 95 tool-expected
   real items and reported beside `auto` on the same subset. **Blocked:**
   `GROQ_API_KEY` is not in the environment Claude Code's shell inherits (§8).
3. **Realtime baseline**, ~$5, if credit is ever added. It also answers item 1.
4. Only if 2 passes ≥ 90 %: the maker's recordings, then the ~2 GB of STT
   weights, once the disk is freed.

## 8. Notes and corrections

- **Gemini free tier was not run, and neither was Gemini Live.** Google's pricing
  page, fetched 2026-09-21, marks free-tier content "used to improve our
  products": *Yes*, for Flash, Flash-Lite **and** the Live native-audio model.
  That is the same objection that excluded Mistral, and it applies with more
  force to the real system prompt, which includes the user's memory priming
  block. **The Live API *is* on the free tier**
  (`gemini-2.5-flash-native-audio-preview-12-2025`, free audio in and out), so
  the prompt's open question is answered: yes, but only by giving up training
  opt-out. That's the maker's call, not the spike's.
- **Cerebras** now appears to require a card-backed trial instead of a no-card
  free tier (conflicting third-party sources), so it doesn't meet "free to run".
  Not verified first-hand.
- **Groq** does not train on free-tier inputs or outputs, and zero data
  retention is a self-serve toggle. It's the only free cloud candidate that
  satisfies the privacy rule.
- **Retrieved-40** was run in the background (later stopped at 44/140). Each
  call re-prefills the tool block (~1–2 min per call on this Mac), and its
  partial result is in §2. That it takes hours is itself the finding:
  retrieval-per-turn and local prefix caching are at odds.
- **No larger local model was tried.** Qwen3 14B (~9 GB) or 30B-A3B (~18 GB) will
  not fit alongside a 23k-token context in 16 GB, and disk was at 99 %. So the
  local ceiling here is the hardware, not the model family. A 32 GB+ Mac is a
  different experiment.
- **Qwen3 thinking mode was off** (latency). It might recover some "no call"
  misses at the cost of hundreds of generated tokens at ~5 tok/s, i.e. tens of
  seconds per turn. Not worth measuring on this hardware.
- **Side effect found during the spike:** the installed daemon
  (`~/.emma/src`, older than local `main`) crash-looped 964 times in ~3 hours
  with `credentials_invalid` → exit 2, because it predates `_terminal_exit`.
  The LaunchAgent was booted out. It's another instance of the release-drift
  problem.
- **Maker's decisions, 2026-09-21 (second round):** (1) read-only production
  query approved, done (§5); (2) Groq Llama 3.3 70B approved, with forced
  `tool_choice` as a second condition; (3) **no Gemini**, Live included; (4)
  recordings and the ~2 GB STT download wait for the Groq result; (5) no new
  latency measurements until the disk is freed; (6) **the daemon stays
  unloaded**. The installed `~/.emma/src` predates v0.2.1, which is what the
  964-restart loop proves, and the maker will reinstall once there's disk.
- **Groq key handling:** the harness reads it only via `os.environ[--key-env]`
  and exits if it's absent. It's never written, logged or echoed; error rows
  record the provider's error body, which doesn't contain the key. As of this
  writing `GROQ_API_KEY` is **not set** in the environment Claude Code's shell
  inherits. It's not in `~/.zshrc`, `~/.zprofile`, `~/.zshenv`,
  `~/.bash_profile`, `~/.bashrc`, `~/.profile` or `launchctl getenv` (checked by
  name only). A variable exported in a separate terminal doesn't reach this
  process.
- **Groq free-tier limits for `llama-3.3-70b-versatile` are not in Groq's public
  rate-limit table** (checked 2026-09-21; the model is still listed as
  production, 131k context). A single 184-tool request is ~20k prompt tokens. If
  the free-tier TPM is below that, every full-registry call fails on size, and
  *that* becomes the finding.
- **Retrieved-40 was stopped at 44/140** at the maker's request to free
  memory and disk; partial result in §2. Its timings are not reported.

### Groq passes: blocked before the first scored call (2026-09-21)

`GROQ_API_KEY` is now present. Neither approved pass (auto, forced) produced a
scored row. Four blockers, found in order:

1. **Cloudflare 1010** on urllib's default User-Agent. Harness bug, fixed
   (`eval_tools.py:_post` sends `User-Agent: emma-spike/0.1`).
2. **`'tools': maximum number of items is 128`.** Groq rejects the full 184-tool
   registry outright, so the `full` condition can't run on Groq at any tier.
   A `static128` condition (first 128 in registry trim order) was added.
3. **`llama-3.3-70b-versatile` → `model_not_found`** for this key. The approved
   model is gone. Available tool-capable models: `openai/gpt-oss-120b`,
   `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`.
4. **Free-tier token limit is 8,000 TPM on every one of them**
   (`x-ratelimit-limit-tokens`). Measured prompt sizes are **22.8k** (full),
   **10.6k** (static40), ~14k+ (static128). **Not even static40 fits in a
   single minute's budget**, so every condition would 413/429 on every call.

**Finding:** Groq's free tier can't host Emma's tool-calling turn in any
configuration measured. The 128-tool cap also rules out full-registry parity
on Groq's paid tiers. No scores are reported and nothing was substituted: a
model other than the approved one is the maker's call.

**Closed by the maker, 2026-09-21: Groq free tier not viable — 8k TPM vs
22.8k prompt; hard 128-tool limit.** No paid tier. No substitute model.

### The spike's actual finding

The deciding number isn't model accuracy. It's the **per-turn payload**:
~22.8k prompt tokens with the full registry, 10.6k even with 40 tools (the
system prompt and memory priming alone are several thousand). That is too big
for every free option measured (Groq 8k TPM, local 8B prefill at 1–2 min/call on
16 GB), and it costs every architecture, the current Realtime one included, on
every turn. Shrinking the per-turn payload is the follow-up and gets its own
prompt. The cascade-vs-Realtime question is closed until then.

Repro note: the harness is committed under `scripts/spike_cascade/`, but
`data/system_prompt.txt` (live priming block), `data/mined.json` (raw log
mine) and the `results/m1_*.jsonl` rows (model replies echo priming facts) are
Personal tier and stay local (`scripts/spike_cascade/.gitignore`). The frozen
set (`build_labelled.py` → `data/labelled.json`) is committed; the scores above
are the record of the M1 runs.
