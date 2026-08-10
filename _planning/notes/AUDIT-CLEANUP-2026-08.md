# Audit + cleanup pass — 2026-08-10

Full audit of the recent body of work (wake-word→sherpa, PAID-ONBOARDING 1–6,
LAUNCH-2.1, plus the boot-hardening / app-window / out-of-minutes commits that
landed after). Three subsystem audits (backend, daemon/boot, dashboard/UI) + a
staleness sweep. Baseline before changes: **1128 passed, ruff + mypy clean**.

## Fixed

| # | Sev | What | Where |
|---|---|---|---|
| 1 | HIGH | Initial `is_paired()` probe was unprotected — a locked Keychain at login propagated uncaught → `handle_crash` → exit 1 → permanent 30s launchd loop (a regression of the c478a63 fix, in my own `_ensure_paired`). Now wrapped like the in-loop probe; falls through to the park loop. | `core/orchestrator.py:345` |
| 2 | MED-HIGH | `wake_sherpa` raises `SystemExit` on a missing model — `BaseException` slips past `main_loop`'s `except Exception` → 30s loop forever. Added `_wake_preflight()` so it goes through `_terminal_exit` (boot_guard backoff → stay-down) at boot, before the lazy raise. | `emma/__main__.py` |
| 3 | MED | "No bg task fails invisibly" callback was only on the dashboard task. New `_spawn_bg()` helper attaches `_report_bg_failure` to UI-supervisor / proactive / conditionals too. | `emma/__main__.py` |
| 4 | MED | `boot_guard.clear()` was never called on a good boot, and `clear(None)`'s filter (`endswith("_boot")`) matched no real kind — so a near-threshold streak survived a successful boot and could trip stay-down on one later transient. Filter fixed (keep only `said:` markers) + `clear()` now called once both preflights pass. | `core/boot_guard.py`, `emma/__main__.py` |
| 5 | MED | Anonymous landing demo inherited the free daemon-trial's `cost_cap_cents` (my Part 1 lowered it 40→15) → a ~30¢ 60s realtime demo hard-stopped at ~half. Decoupled: the public demo uses its own `DEMO_COST_CAP_CENTS` (40). The 90s daemon trial is unaffected (gated by `monthly_seconds`, not cost cap). | `backend/demo_session.py` |
| 6 | MED | Dashboard `_wake_word_card()` still described openWakeWord/`hey_jarvis`/Picovoice and had NO sherpa branch — every shipped install showed a false card. Rewritten sherpa-first. `_known_issues()` was stale dev telemetry (MEM-01 flatly false: reflection wired since 22.1); gutted to just the live wake status. | `dashboard/server.py` |
| 7 | LOW (cleanup) | `regenerate_capabilities_md()` claimed "idempotent" but rewrote a fresh `Generated:` timestamp every startup → churned the tracked file on every boot (I'd reverted it ~4× this session). Now truly idempotent: skips the write when tool content is unchanged. | `tools/self_tool.py` |

All changes covered by tests; **1128→ green, ruff + mypy clean**. Backend tests
(demo/plans/account/credits) green.

## Audited and CONFIRMED sound (no change)
- **`GET /api/device/balance`** — `require_device` bearer, no IDOR (user derived only from the device row), byte-identical shape to `/api/balance`. Free never charges (explicit `plan=="free"` guard + the CAPA-5 close is clean, no Stripe).
- **/control channel security** — `_origin_ok` gates every socket before dispatch (CSWSH blocked); memory-delete is parameterized (no SQLi); no XSS in the app window (all daemon/user text via `textContent`/`esc()`); the WKWebView loads `http://127.0.0.1`, never `file://`.
- **Managed park / preflight-skip / out-of-minutes latch / wake_sherpa threading** — all correct (poll ramps 2s→30s, no CPU spin; worker exits on `stop`; speaks once).

## Deferred / noted (low, not fixed this pass)
- **`_is_managed()` masks a BYOK misconfig** (empty `OPENAI_API_KEY` → treated as managed → parks instead of erroring). Intentional per LAUNCH-4 ("one missing env var must not kill the daemon"); converts a clear failure into a silent hang. Acceptable trade-off; flagged.
- **`version.staleness()`** can report a false "stale" for a `git describe` tag like `v1.2.3-5-gabc` (dev-facing only — Cuenta panel / `permissions check`, never boot).
- **Residual control-channel risk**: a non-browser LOCAL process (no Origin header) can send unconfirmed `shutdown`/`unpair`/`forget`. Matches the documented "trusted-because-local" model; the shutdown NSAlert is client-side only.
- **`/api/account` month vs `/api/balance`** month can disagree (lifetime counter vs windowed) — display-only, pre-existing.

## Housekeeping
- `AGENTS.md` (Codex guidance mirror of CLAUDE.md) was untracked → committed as a real project file (public-copy clean).
- `self/capabilities.md` committed at the current 184-tool set; the idempotency fix stops it churning on every boot.
