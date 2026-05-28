# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Emma is

Emma is a bilingual (Spanish/English) voice-activated AI assistant for macOS. She listens for a wake word, opens an audio-to-audio session via the OpenAI Realtime API through a Pipecat pipeline, dispatches tool calls against a registry of ~36 tools, and maintains long-term memory in a local SQLite store.

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

**`memory/reflection.py`** — Calls gpt-4o-mini on a short conversation window to extract durable facts about Garcia. Currently the reflection scheduling is **not wired** to the Pipecat session — explicit `remember_fact` tool calls work, but automatic fact extraction after each turn requires Pipecat transcript event hooks (not yet implemented).

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
