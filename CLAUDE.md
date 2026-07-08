# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Public-copy rules (mandatory — do NOT violate)

These apply to anything that ships to the public web (landing, backend HTML
pages, OG images, README, social posts):

1. **Never reference the maker by name.** No "Garcia", no "made by Garcia",
   no "soy Garcia", no first-person maker paragraph.
2. **Never assert a geographic origin.** No "Monterrey, MX", no "MTY", no
   "made in <city>". The maker has not consented to that being public.
3. **Footer is generic.** `© 2026 emma` (or equivalent in EN). No personal
   attribution line.
4. **No invented personal narrative.** Copy is product-focused: what Emma
   does, how she runs, what she costs. Not who built her.
5. **Visual identity is the ATOM** — clean line-art orbit rings + a small
   glowing nucleus (+ orbiting electrons), rendered as inline SVG/CSS.
   Emma must be PRESENT and beautiful in the hero, and the same atom is the
   nav mark. REJECTED directions (do not bring back): the WebGL/Three.js
   triangulated wireframe ("orb inside a cage"), and a plain glossy sphere/
   orb as the logo. The mark is the atom, not a sphere.
6. **Don't assume the maker's identity or location anywhere in code
   comments, prompts, or generated assets either.** If a feature needs an
   origin (e.g. system prompt examples), use generic placeholders.

If you find any existing copy violating these rules, remove it. If you're
unsure what to put in place, leave a TODO and ask — do not invent.

## What Emma is

Emma is a bilingual (Spanish/English) voice-activated AI assistant for macOS. She listens for a wake word, opens an audio-to-audio session via the OpenAI Realtime API through a Pipecat pipeline, dispatches tool calls against a registry of ~120 tools, and maintains long-term memory in a local SQLite store.

## Commands

```sh
# Run Emma (debug mode, console logging)
.venv/bin/python -m emma --debug

# Real-time dashboard (localhost:3200, WebSocket on 3201)
.venv/bin/python dashboard/server.py

# Run tests
.venv/bin/python -m pytest tests/ -v

# Run a single test file
.venv/bin/python -m pytest tests/test_shell.py -v

# Run a single test
.venv/bin/python -m pytest tests/test_memory.py::TestLongTerm::test_remember_and_recall -v

# Lint
.venv/bin/ruff check .

# Auto-fix lint issues
.venv/bin/ruff check . --fix

# Format
.venv/bin/ruff format .

# Type check (strict on core/, tools/, memory/, config/)
.venv/bin/mypy .

# Acceptance suite (mock mode, no APIs needed)
.venv/bin/python tests/acceptance/runner.py --mock-external

# Install dependencies
uv sync
```

## Architecture

### Runtime flow

```
Wake word (local openWakeWord) → ack chime → 0.4s delay →
  Pipecat pipeline (OpenAI Realtime WebSocket) → idle timeout → loop back
```

The orchestrator (`core/orchestrator.py`) runs an infinite loop: wait for wake word, open a Pipecat session, return to listening when the session idles out. There is no separate STT or TTS — the Realtime API handles audio-in → reasoning → audio-out in a single WebSocket.

### Key modules

**`core/conversation.py`** — Builds the Pipecat pipeline: `LocalAudioTransport.input → OpenAIRealtimeLLMService → LLMAssistantAggregator → EchoGateProcessor → LocalAudioTransport.output`. The `LLMAssistantAggregator` is critical — it captures `FunctionCallResultFrame`s and pushes updated context back upstream to the LLM, which triggers tool-result audio responses. An `LLMContext` frame is queued at session start to initialize the function-call pipeline. Constructs session properties (voice, VAD, tools, system prompt with memory priming). Registers all tool functions with the LLM service.

**`core/orchestrator.py`** — Wake → session → loop. Signal handling, crash delegation, dev-mode exit.

**`core/echo_gate.py`** — `BaseAudioFilter` that silences mic input while the bot speaks (prevents self-interruption on MacBook speakers) with an energy-based barge-in threshold so the user can still interrupt.

**`core/wake_word.py`** — Lazy-loads an openWakeWord model (built-in or custom .onnx), listens on a 16kHz `RawInputStream`, signals detection via `asyncio.Event`. Resets model state after each detection to prevent afterglow re-triggers.

**`tools/base.py`** — `@tool()` decorator, `ToolResult` dataclass, automatic Python-type-to-JSON-schema conversion. The `confirmed`/`cancelled` parameters are hidden from the LLM schema and used for the two-phase confirmation flow on destructive actions.

**`tools/registry.py`** — Auto-discovers all `tools/*.py` modules at first call. `openai_tool_specs()` returns Chat Completions format; `_adapt_tool_specs_for_realtime()` in conversation.py flattens them for the Realtime API. `dispatch()` does lookup + call + exception wrapping.

**`memory/long_term.py`** — SQLite fact store at `~/.emma/memory.db`. Deduplicates on exact content match, bumps confidence on repeat observations. `priming_block()` returns the top-N facts formatted for injection into the system prompt.

**`memory/reflection.py`** — Calls gpt-4o-mini on a short conversation window to extract durable facts about Garcia. Reflection is wired to the live session as of 22.1: the function-call/transcript path in `core/conversation.py` fires `schedule_reflection(last_turns(4))` (conversation.py:245) after a turn, so durable facts are extracted automatically in addition to explicit `remember_fact` tool calls. The DB connection runs in WAL mode with a busy timeout so this background write can't lose facts to a lock collision with an explicit `remember`.

**`actions/environment.py`** — Detects installed apps (IDE, terminal, music, browser) from a hardcoded shortlist. Caches results in `~/.emma/environment_cache.json` (24h TTL). User overrides via voice ("prefiero Zed") persist as preferences in the same cache.

### Tool registration pattern

Every tool is a decorated function in `tools/*.py` returning `ToolResult`:

```python
@tool()
async def search_web(query: str) -> ToolResult:
    """Search the web for `query`."""
    # ... implementation
    return ToolResult(success=True, data={...}, user_message="short spoken answer", requires_confirmation=False)
```

The decorator auto-generates the JSON schema from type hints. `confirmed: bool = False` / `cancelled: bool = False` parameters are reserved for the confirmation flow and excluded from the schema. The docstring (first two paragraphs) becomes the tool description.

### Session properties flow

`_build_session_properties()` is async because it awaits `_build_instructions()` which awaits `priming_block()` (reads memory from SQLite). The chain: `run_session → build_pipeline → _build_session_properties → _build_instructions → priming_block`.

### Audio paths

- **Wake word**: 16kHz mono int16 via `sounddevice.RawInputStream` (in `core/wake_word.py`)
- **Conversation**: 24kHz mono PCM via Pipecat's `LocalAudioTransport` (in `core/conversation.py`)
- **Echo gate**: Filters mic audio at 24kHz, zeros out frames below `barge_in_rms` while bot is speaking

### Configuration

All settings load from `.env` via pydantic-settings (`config/settings.py`). Key active settings:
- `REALTIME_MODEL`, `REALTIME_VOICE` — Realtime API model and voice
- `WAKE_WORD_PATH`, `WAKE_WORD_NAME`, `WAKE_WORD_THRESHOLD` — wake word config
- `SESSION_MAX_S` — Pipecat idle timeout before returning to wake-word listening
- `MEMORY_DB_PATH` — SQLite store location

Many settings are marked DEPRECATED (STT, TTS, barge-in) — they exist for `.env` file compat but are not read by any live code after the Prompt 13 Realtime API migration.

## Conventions

- **Language**: Garcia speaks Mexican Spanish (Monterrey) and English. Tool error messages and spoken responses are in Spanish by default. `core/runtime.py` tracks `SpokenLang` per-turn.
- **System prompt**: Lives in `core/conversation.py:_build_instructions()`. Structured sections: Role, Personality, Language, Response Length, Variety, Preambles, Tool Results, Forbidden. Memory facts appended at the end.
- **Confirmation flow**: Destructive tools (`destructive=True`) use `requires_confirmation=True` in their first `ToolResult`. The orchestrator re-calls with `confirmed=True` after user assent. `cancelled=True` is opt-in for cleanup on decline.
- **Self-awareness**: `tools/self_tool.py` regenerates `self/capabilities.md` from the live tool registry at startup. The `describe_capabilities` tool reads this file.
- **Crash reports**: Written to `~/Library/Logs/Emma/crashes/`. Rate-limited Terminal auto-open (3 per 60s). The `say` command (not the Realtime API) speaks the failure.
- **Service lifecycle**: Runs as a launchd agent (`com.garcia.emma`). Dev mode disables the agent and opens a Terminal with resume instructions. Exit 0 = stay stopped; exit 1 = launchd restarts.

## Permissions convention (mandatory)

Every TCC permission Emma needs is requested **upfront at install time** via
`python -m emma.permissions bootstrap` (installer step 7.5, before the
LaunchAgent loads). No permission may surface as a surprise pop-up during use.

When adding a tool or feature that needs a new permission:
1. Add the app/pane to the bootstrap list in `core/permissions.py`
   (`_AUTOMATION_APPS` or `_MANUAL_PANES`).
2. Add or extend the corresponding probe function (`check_*`).
3. Verify the install script re-runs cleanly end-to-end.
4. Document the new permission in the prompt that adds the feature.

Permissions covered today: Microphone, Automation (Calendar, Mail, Messages,
Notes, Reminders, Safari, Finder, Music, Terminal), Accessibility, Full Disk
Access.

## Security convention (mandatory)

Emma stores data in three trust tiers:

- **Public** — `self/capabilities.md`, version info. Plaintext on disk.
- **Personal** — preferences, profile facts, schedule patterns. `~/.emma/memory.db`. Cold-disk protection relies on FileVault.
- **Secret** — passwords, API keys, account numbers, government IDs, credit cards. **macOS Keychain only** (`com.garcia.emma` service). `memory.db` may carry a `vault_ref` label but never the value.

When adding a tool or feature:
1. Classify each datum it stores into one of the three tiers.
2. Secrets MUST route through `core/secrets.py`. No exceptions.
3. New PII patterns get added to `core/redaction.py` AND covered by tests.
4. New credential env vars get added to the migration list in `core/secrets.py:bootstrap_from_env` (`_CRED_SUFFIXES`) and `config/settings.py:_CREDENTIAL_FIELDS`.
5. Anything sent to OpenAI must be filterable; the priming block excludes `vault_ref IS NOT NULL`.

The architectural rule: **no secret-tier value ever lands in `memory.db`, in logs, or in the system prompt.** See `SECURITY.md` for the full threat model.

## Background tasks convention (mandatory)

Any tool that may take more than ~3 seconds, or that fires a subprocess Garcia
might want to walk away from, MUST be a background task — not a synchronous tool
call. The pattern:

1. Tool dispatches to `core/background.py:registry().start(...)` and returns
   immediately with a user-facing "te aviso cuando termine" message.
2. `core/background.py` owns the asyncio.Task, captures last 8KB of output,
   persists to `~/.emma/tasks.jsonl`, fires `events_bus.publish("task_started")`
   and `events_bus.publish("task_completed")`, plus a macOS notification on
   completion.
3. Voice queries (list_my_tasks / task_status / wait_for_my_task / cancel_my_task)
   read from the same registry.

When adding a new "do thing X" tool:
- Synchronous + fast (< 3s): regular `@tool()` function.
- Long-running or potentially blocking: register the work via
  `background.registry().start(...)` and return an "I started it" ToolResult.
- Destructive long-running: combine `destructive=True` + the background pattern.

## Distribution (mandatory)

Emma is distributed via `curl -fsSL https://theemmafamily.com/install.sh | sh`.
No .pkg, no notarization, no Apple Developer Program. The installer
(`_landing/install.sh`, served by nginx):
- Fetches Emma source from the public GitHub release tarball
  (`github.com/theemmafamily/emma`, tag or `main`)
- Uses `uv` standalone for Python 3.12 (no Homebrew forced); pins the venv
  to `~/.emma/.venv` via `UV_PROJECT_ENVIRONMENT`
- Downloads the Vosk Spanish wake-word model to `~/.emma/vosk`
- Pairs interactively via `emma --first-run --pair` (RFC 8628), FOREGROUND
  and BEFORE the LaunchAgent loads so failures surface in the terminal
- Registers a LaunchAgent labelled `com.emma.daemon` (generic, public-copy
  safe), booting out any legacy `com.garcia.emma` agent first

Uninstall: `curl -fsSL https://theemmafamily.com/uninstall.sh | sh` — removes
`~/.emma`, both LaunchAgent labels, the logs, and the Secret-tier Keychain
entries (`device_token`, `OPENAI_API_KEY` under the `com.garcia.emma` service).

The installer is idempotent — re-running upgrades in place.
