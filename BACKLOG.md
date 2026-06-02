# Emma backlog

Items deferred from earlier prompts that may or may not get scheduled.
Each entry has a date, the source file, and a one-line description.

## Deferred

- 2026-06-01 — tools/music.py — Hook the music tool into
  `actions.environment.detect_preferred("music")` so the active/preferred
  music app is auto-selected and an install prompt surfaces when the user
  prefers Spotify but it isn't installed. Originally tagged Phase 06; in
  practice `detect_preferred("music")` never returns None (Apple Music ships
  with macOS), so the existing two-tier Spotify→Apple Music fallback already
  covers realistic configs. Low priority.
- 2026-06-01 — tools/browser.py — Replace the scripted `browser_do` (currently
  Amazon-only) with a general LLM-driven agentic browser controller: free-form
  intent, turn-by-turn driving via screenshots + DOM context with GPT-4o, a
  structured action grammar, retries, and a hard step cap (~15). Until then,
  non-Amazon intents return a clean "not yet available" error so the LLM falls
  back to lower-level tools.
