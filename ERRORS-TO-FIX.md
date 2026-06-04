# Errors caught during debug session — 2026-06-04

Source log: `emma-debug-session.log` (full raw output of `.venv/bin/python -m emma --debug`)

Status legend: 🔴 open · 🟡 investigating · ✅ fixed

---

## 1. ✅ `calendar_fetch_failed` — Calendar app not running (-600)

**FIXED 2026-06-04:** `core/proactive/calendar_events.py` now launches
Calendar hidden (`open -gja`, once per run), shares one fetch across the 3
startup proactivities via a 30s TTL cache + lock, and debounces the warning
(first occurrence WARN, rest DEBUG). `actions/macos.py:launch_app` gained a
`background=True` flag. Tests: `tests/test_calendar_events.py` (5).
Verified live: `calendar_launching_hidden` fired once, no more -600.

## 1b. ✅ `calendar_fetch_failed` — AppleScript `whose` scan slower than timeout

**When:** every poll, even with Calendar running and no dialog.

```
app_dialog_blocked: osascript timed out after 20.0s (an app likely opened a confirmation dialog)
```

**Root cause (measured 2026-06-04):** Calendar.app's `every event of cal whose
start date ≥ …` is O(all events). Garcia's calendars: main "Calendar" 39.6s,
"US Holidays" 5.8s, others <0.5s → ~57s total, hopeless against the 20s
timeout. The "dialog" in the error message was a red herring.

**FIXED (interim):** `_FETCH_TIMEOUT_S = 90s` + cache TTL 30s → 600s (failures
cached too), so the scan grinds Calendar ≤6×/hour. Tests in
`tests/test_calendar_events.py::TestSlowScanHandling`.

**Follow-up (proper fix, next prompt):** migrate calendar reads to **EventKit**
(`pyobjc-framework-eventkit`, indexed → ms-fast). Requires the Calendars TCC
pane in the permissions bootstrap (`core/permissions.py`) per the mandatory
permissions convention. Also benefits `tools/calendar_tool.py`, whose voice
queries hit the same 20s timeout (`_CAL_TIMEOUT_S`) against a 57s scan —
i.e. "¿qué tengo hoy?" currently fails too.
**Confirmed live 2026-06-04 20:57:** asked Emma "qué tengo en el calendario
hoy" by voice → `today_events` → `tool_timed_out` at exactly 20000ms, twice
(she auto-retried, doubling the stall to 40s). `capability_gap_recorded`
fired. The voice calendar read path is effectively dead until EventKit.

## 2. 🟡 "She doesn't listen" — diagnosis 2026-06-04

Not one bug — three stacked causes, confirmed from the session log:

1. **Wake word (biggest):** session ends (idle or after a tool) → re-wake needs
   `hey_jarvis`, which took **45s of attempts** to fire for Garcia's accent.
   Real fix = Picovoice "Emma" .ppn (code ready, blocked on Console signup —
   see `picovoice-support-email-v2.md`). Mitigations applied: threshold
   0.5 → 0.35 in `.env` + new `wake_score_near_miss` logging
   (`core/wake_word.py:_make_near_miss_logger`) to tune with data.
2. **Echo gate starvation:** Emma spoke **58% of the 4-min session** (21
   utterances) + 600ms tail each; `barge_in_rms=18000` ≈ unreachable by
   normal voice → Garcia's speech onsets land in gated (zeroed) audio.
   Not changed yet — lowering the threshold risks the old self-interruption
   bug. Candidate: rolling-window barge-in instead of single-frame RMS.
3. **`session_end_after_tool`:** after `play_track` Emma closed the session
   ("after_speech") — by design, but combined with (1) it reads as "she
   stopped listening". Revisit once wake is reliable.

**Observability gap:** ~~input audio transcription is NOT enabled~~ —
WRONG, it IS enabled (19.5); the websockets trace just truncates the event
names. Real gap found in 19.7 (see §3).

## 3. Findings from the 19.7 voice harness (2026-06-04) — STATUS post-Prompt 21

1. ✅ **FIXED (21-B24):** self-confirmation — confirmation invariant in the
   function handler (event-ordered, VAD-onset user-turn marker). V13/V14
   voice-verified: question + silence → no delete; real "sí" → delete.
2. 🟡 **PARTIALLY ADDRESSED (21-B24):** session-memory taps now capture
   user/assistant text in production — but the REFLECTION pipeline still
   reads the old dead collector. Wiring reflection onto the taps = next.
3. ✅ **FIXED (21-B26):** strict-mirror prompt rule + web.py summarize
   answers in preferred_lang. V56 voice-verified (full Spanish reply).
4. ✅ **FIXED (21-B27):** correction directive with 3 examples — V57
   voice-verified: Emma called remember_stt_correction unprompted and
   vocabulary.toml now carries [NillOjeda] learned from voice.
5. ✅ **FIXED (19.7/21):** brittle corpus patterns loosened to intent.

## 4. 🔴 New findings from the Prompt 21 voice runs — for the next prompt

1. **Realtime-acts-before-transcript race (root-caused + worked around).**
   The model reacts to AUDIO; `input_audio_transcription.completed` lands
   AFTER the resulting tool call. Anything keyed on transcripts (reflection,
   future features) must treat them as trailing metadata, never as the
   action trigger. The B24 invariant uses VAD onset for this reason.
2. **Speaker→mic fallback is noise-vulnerable.** Two runs were contaminated
   by real notification sounds (STT heard "iMessage iMessage iMessage…").
   BlackHole install removes the whole class. Until then, runs need Do Not
   Disturb.
3. **Repeated voice runs pollute real app state** (3 duplicate 'Compras'
   notes accumulated). The harness needs per-scenario setup/teardown hooks
   (e.g. `setup_script` / `teardown_script` fields) — next harness prompt.
4. **Emma greets on wake now** ("Hola, soy ema…"), consuming a turn before
   the utterance. Harmless for humans, adds latency for the harness; the
   B20 first-sentence pin made it more consistent. Consider a no-greeting
   directive when the session opens from a barge-in-style immediate command.

1. **SAFETY — destructive self-confirmation from STT noise.** Voice run
   V13: Whisper transcribed a trailing artifact as "See" ("…borra mi nota
   compras See"), and Emma chained `delete_note` →
   `delete_note(confirmed=true)` in the SAME turn ("¿Borro…? Hecho.") —
   the question was asked and self-answered. Confirmation of destructive
   tools must require assent from a SEPARATE user turn.
2. **Reflection gap root-caused.** `transcript_captured` has fired 0 times
   in ALL history: user `TranscriptionFrame`s travel UPSTREAM of the LLM
   (never reach the downstream `TranscriptCollector`), and assistant
   `LLMTextFrame`s are absorbed by `LLMAssistantAggregator` before the
   collector. Fix = move/duplicate the collector taps (the 19.7
   `_TestTranscriptTap` shows exactly where the frames exist) — then
   memory reflection finally runs.
3. **Language-mirroring violation.** V46 (Spanish ask) → Emma's decline
   drifted into English mid-response ("I can't post to Twitter…").
   B20 pinned the greeting; per-turn mirroring still slips after tools.
4. **STT proper-noun snapshot.** "Nill Ojeda" → "Neil Ojeda" (A01);
   wake+content in one breath risks artifacts (V13's "See"). Candidates
   for `remember_stt_correction` / vocabulary aliases.
5. **Brittle corpus patterns** (fixed in 19.7): exact phrasing asserts
   fail against the Variety rule — assert intent, not wording.

**When:** Startup, fired 3× within ~2s (20:17:11–20:17:12)

```
{"error": "43:55: execution error: Calendar got an error: Application isn't running. (-600)", "event": "calendar_fetch_failed", "level": "warning"}
```

**Diagnosis (preliminary):** AppleScript `tell application "Calendar"` fails with
-600 when Calendar.app isn't open. The proactive engine fetches calendar events at
startup without launching/activating the app first (or without
`launch`/`run` fallback). Also note it fired 3 times — possibly duplicate
subscribers or no backoff on retry.

**Fix later:**
- Find the calendar fetch in the proactive engine / calendar tool.
- Either `launch` the app silently before the query, use EventKit instead of
  AppleScript, or degrade gracefully (skip + retry with backoff).
- Check why it fires 3× (duplicate calls?).
