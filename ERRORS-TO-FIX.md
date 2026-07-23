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
start date ≥ …` is O(all events). the user's calendars: main "Calendar" 39.6s,
"US Holidays" 5.8s, others <0.5s → ~57s total, hopeless against the 20s
timeout. The "dialog" in the error message was a red herring.

**✅ FIXED (Prompt 24, 2026-06-08) — EventKit migration.** Reads now go through
`actions/calendar_store.py`, which binds EventKit via `objc.loadBundle` (zero new
pip — pyobjc-core only) and queries Apple's indexed store with
`predicateForEventsWithStartDate:endDate:calendars:`. Measured live on the user's
calendars: `today_events` **75.9 ms cold / <1 ms warm**, `events_in_range` over
a **full year 0.2 ms** — vs the 20 s timeout / 57 s scan. `tools/calendar_tool.py`
(`today_events`/`next_event`/`events_in_range`) and the proactive reader
(`core/proactive/calendar_events.py`) both migrated; the 90 s timeout + hidden
Calendar.app launch + AppleScript scan are gone (10 min TTL cache kept).

The Calendars TCC pane is now in the bootstrap (`core/permissions.py`:
`_MANUAL_PANES` + `check_calendar()` probe) per the permissions convention.

**Writes stay on AppleScript** (`create_event`/`delete_event`): they're single
fast ops, never the slow scan, and EventKit writes silently fail from Emma's
non-bundled Python process (TCC grants reads but not writes without an app-bundle
`NSCalendarsFullAccessUsageDescription` — `saveEvent:` returns success yet
nothing persists; verified across 4 probes). Bundling Emma for EventKit writes is
a separate packaging task if ever wanted.

Tests: `tests/test_calendar_eventkit.py` (store marshalling, auth, tool layer),
`tests/test_calendar_events.py` (proactive, EventKit-mocked),
`tests/test_permissions_fixes.py` (`check_calendar` + Calendars pane).

*Historical (the dead path, now closed):* O(all events) AppleScript scan —
main "Calendar" 39.6 s, "US Holidays" 5.8 s → ~57 s total vs the 20 s timeout.
Confirmed live 2026-06-04 20:57: voice "qué tengo hoy" → `today_events` →
`tool_timed_out` at 20000 ms ×2 (auto-retry doubled the stall).

## 2. 🟡 "She doesn't listen" — diagnosis 2026-06-04

Not one bug — three stacked causes, confirmed from the session log:

1. **Wake word (biggest):** session ends (idle or after a tool) → re-wake needs
   `hey_jarvis`, which took **45s of attempts** to fire for the user's accent.
   Real fix = Picovoice "Emma" .ppn (code ready, blocked on Console signup —
   see `picovoice-support-email-v2.md`). Mitigations applied: threshold
   0.5 → 0.35 in `.env` + new `wake_score_near_miss` logging
   (`core/wake_word.py:_make_near_miss_logger`) to tune with data.
2. **Echo gate starvation:** Emma spoke **58% of the 4-min session** (21
   utterances) + 600ms tail each; `barge_in_rms=18000` ≈ unreachable by
   normal voice → the user's speech onsets land in gated (zeroed) audio.
   ✅ FIXED 22.1-B38: rolling-window barge-in (sustained ≥6000 RMS over
   250ms) + the 18000 spike shortcut kept; opener still absolute.
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
2. ✅ **FIXED (22.1-B36):** reflection triggers from _BotTextTap; live
   evidence: facts 11→13 during the 22.1 voice runs (learned 'classical
   music' + 'notes in Spanish' from real conversations).
3. ✅ **FIXED (21-B26):** strict-mirror prompt rule + web.py summarize
   answers in preferred_lang. V56 voice-verified (full Spanish reply).
4. ✅ **FIXED (21-B27):** correction directive with 3 examples — V57
   voice-verified: Emma called remember_stt_correction unprompted and
   vocabulary.toml now carries [NillOjeda] learned from voice.
5. ✅ **FIXED (19.7/21):** brittle corpus patterns loosened to intent.

## 5. ✅ Zombie session — FIXED 22.1-B35 (DeadSessionWatcher: debounced cancel → back to wake; 3-in-60s → 30s cooldown; unit-tested, V66 documented)

**Symptom:** the user: "she's not talking." Wake fired, session opened, then
OpenAI threw a SERVER-side error ("The server had an error… retry",
non-fatal) and closed the WS cleanly (code 1000). Pipecat kept pushing mic
audio into the dead socket at ~50 errors/second ("Error sending client
event: received 1000 (OK)") — **no reconnect, no session teardown**. Emma
deaf+mute until the idle timeout (or forever if ErrorFrames count as
activity). Daemon stayed up; only a manual restart recovered.

**Root cause:** `AuthErrorWatcher` only terminates on TERMINAL auth markers;
a clean remote close (1000) after a transient server error matches nothing
→ nobody cancels the pipeline task → the orchestrator never loops back to
wake-word listening.

**Fix shape (next prompt):** extend the watcher (or add a dead-session
watcher): on ErrorFrames matching "Error sending client event" /
"received 1000" (debounced, e.g. 3 within 1s), CANCEL the pipeline task —
NOT SystemExit — so the orchestrator loops back to wake and the next
"hey jarvis" gets a fresh session. Log `session_zombie_recovered`.

## 6. ✅ Playlists — FIXED 22.1-B37 (scopes + drift-eviction; -1700 → music:// catalog search; V67 live-PASS)

```
spotify_playlist_lookup_failed: 403 Insufficient client scope
GET /v1/me/playlists
```

The OAuth token in `~/.emma/spotify_token.json` was minted without
`playlist-read-private` / `playlist-read-collaborative`. Playing tracks
works; listing/playing the user's own playlists doesn't. Fix = add the scopes
to the auth flow's scope list and re-run the Spotify authorization (token
refresh won't add scopes — needs a fresh consent). Check what scopes the
flow requests in the spotify tool/action module.

**Same turn, fallback also failed:** Apple Music
`play playlist "Classical Essentials"` → AppleScript -1700: editorial
playlists aren't in the local library; `play playlist "X"` only resolves
LIBRARY playlists. The fallback should detect the miss and either search
Apple Music ("search playlist") or tell the user it needs the playlist saved
to his library — not die with a type error. Both halves belong to one
"playlists actually work" fix.

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
3. ✅ FIXED 22.1-B40 (setup/teardown hooks; A13×3 verified no growth). Was: **Repeated voice runs pollute real app state** (3 duplicate 'Compras'
   notes accumulated). The harness needs per-scenario setup/teardown hooks
   (e.g. `setup_script` / `teardown_script` fields) — next harness prompt.
4. ✅ FIXED 22.1-B39 (energy-detected immediate command skips the greeting; live-verified). Was: **Emma greets on wake now** ("Hola, soy ema…"), consuming a turn before
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
