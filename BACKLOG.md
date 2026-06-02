# Emma backlog

Items deferred from earlier prompts that may or may not get scheduled.
Each entry has a date, the source file, and a one-line description.

## Deferred

- 2026-06-01 — tools/browser.py — Replace the scripted `browser_do` (currently
  Amazon-only) with a general LLM-driven agentic browser controller: free-form
  intent, turn-by-turn driving via screenshots + DOM context with GPT-4o, a
  structured action grammar, retries, and a hard step cap (~15). Until then,
  non-Amazon intents return a clean "not yet available" error so the LLM falls
  back to lower-level tools.
