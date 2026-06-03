# Capability gaps — things Emma tried but couldn't actually do

This is a living, human-curated companion to the raw ledger at
`~/.emma/capability_gaps.jsonl` (written automatically by
`core/capability_gaps.py` on every tool dispatch). It is **not** a crash log —
it tracks unmet *goals*: the tool ran, but the real-world effect never happened.
Use it to decide what to harden next.

How to refresh the raw data:

```sh
.venv/bin/python - <<'PY'
import json, pathlib, collections
rows=[json.loads(l) for l in (pathlib.Path.home()/".emma/capability_gaps.jsonl").read_text().splitlines() if l.strip()]
for c,n in collections.Counter(r["category"] for r in rows).most_common(): print(n,c)
PY
```

---

## Session 2026-06-02 (6 wake sessions, ~19 min of live testing)

6 gaps recorded by the ledger + 2 systemic issues found in the debug log.

### 1. `play_track` → no active Spotify device  ·  `no_active_device`
- **What happened:** "pon una canción" → Spotify Web API returned
  `404 Player command failed: No active device found`.
- **Message:** *"Spotify no tiene ningún dispositivo activo. Dile al usuario que
  abra Spotify primero."*
- **Root cause:** the Spotify Web API can't start playback when no Spotify
  client is open/active anywhere. `play_track` already fails cleanly, but the
  user goal ("play music") still goes unmet.
- **Fix options (pick one):**
  1. **Auto-transfer to a device:** call `sp.devices()`; if any device exists,
     `sp.transfer_playback(device_id, force_play=True)` then retry. Only error if
     the list is truly empty.
  2. **Fall back to Apple Music** when Spotify has no active device (Apple Music
     is always installed) — mirror the `_apple_music()` path already in
     `tools/music.py`.
  3. **Launch Spotify first:** `open -a Spotify`, wait for it to register as a
     device (~2–3s), then play. Best UX but slowest.
  - Recommendation: **(1) then (2)** — try to transfer, fall back to Apple Music.

### 2. `run_in_terminal` → couldn't open a terminal in Cursor  ·  `reported_failure`
- **What happened:** "abre una terminal en Cursor" → *"No pude lanzar el
  comando."* (84 ms — failed immediately).
- **Root cause:** likely the IDE-terminal launch path in `tools/terminal_actions.py`
  / `tools/ide_actions.py` couldn't drive Cursor (missing CLI, wrong app name, or
  AppleScript target). 84 ms means it bailed before doing real work.
- **Fix:** confirm the Cursor CLI (`cursor`) is on PATH and the
  open-integrated-terminal path is wired for Cursor specifically (it's a VS Code
  fork — `cursor --new-window`/`code`-style flags differ). Surface the *real*
  error instead of the generic "No pude lanzar el comando."

### 3. `append_to_note` → target note didn't exist  ·  `not_found`
- **Message:** *"No pude agregar a la nota: …no encontré esa nota (-2700)"*.
- **Root cause:** `append_to_note` assumes the note exists; AppleScript `-2700`
  = note not found.
- **Fix:** create-or-append — if the note doesn't exist, create it with the given
  title, then append. (Emma was trying to keep a "Debug - errores" test log and
  the note wasn't there yet.)

### 4. GitHub repo lookups returned nothing  ·  `not_found` ×2 + `reported_failure`
- **What happened:** searches for `garcia_g_g/feedback-mind` /
  `garcia_g_g feedback-mind` found nothing; one variant hit
  `422 The listed users and repositories cannot be searched…`.
- **Root cause:** mixed — the repo may be private/nonexistent, and the query
  built an invalid `user:` qualifier (422). Note: a prior commit already added
  422 strip-and-retry (`7e9ba93`); this query still slipped through.
- **Fix:** when a `user:`/`repo:` qualifier 422s, retry as a plain keyword search
  (broaden the existing strip-and-retry). Also: if the user is logged in via
  `gh`, search **their** repos (including private) before public search.

### 5. SYSTEMIC — tool calls dropped on barge-in  ·  (not in ledger; Pipecat-level)
- **What happened:** `Failed to process function call arguments: Unterminated
  string` (1× this session, 1× the prior session). When the user speaks while
  Emma is mid-streaming a tool call's JSON arguments, the interruption truncates
  the JSON → parse fails → **the tool call is silently dropped** (the action the
  user asked for never runs).
- **Why the ledger misses it:** this fails inside
  `pipecat...realtime.llm._handle_evt_function_call_arguments_done`, *before*
  `dispatch()` is ever called.
- **Fix options:**
  1. In `core/conversation.py`, wrap/observe the function-call path and, on a
     parse failure, log a `dropped_tool_call` capability gap so it's tracked.
  2. Consider not interrupting (`broadcast_interruption`) while a
     `function_call_arguments` stream is in flight, so the args finish arriving.
  3. Upstream: the partial-JSON handling is a Pipecat bug worth reporting.

### 6. NOISE — Calendar `-600` warnings ×22  ·  (not a capability gap)
- **What happened:** the proactive engine's AppleScript calendar fetch logs
  `Calendar got an error: Application isn't running. (-600)` 22× because
  Calendar.app is closed.
- **Fix:** treat `-600` as "no events / app closed" and skip silently (or launch
  Calendar once with `open -gj -a Calendar`). Pure log-noise reduction, but it
  buries real warnings.

---

## Priority order

1. **Spotify no-device fallback** (#1) — most-requested action, clean fix.
2. **Barge-in dropped tool calls** (#5) — silently eats *any* action; affects
   everything, hardest to notice.
3. **Cursor terminal launch** (#2) — IDE control is a headline feature.
4. **append_to_note create-or-append** (#3) — small, self-contained.
5. **GitHub 422 broaden** (#4) — partly done already.
6. **Calendar -600 noise** (#6) — trivial, do alongside anything.
