# PAID-ONBOARDING — verification log

Pre-flight audit + implementation against real code (2026-07-21). Status per part:
**DONE** (committed + tested) · **REMAINING** (needs the browser-verify loop / live
proxy / on-device). One commit per part.

## Premise corrections found (file:line)

- **`free` plan shape** — the prompt's stated current values were already partly
  wrong. Real `backend/config.py:31-33` was `daily_seconds:60, monthly_seconds:0,
  cost_cap_cents:40, daemon_session_max_seconds:900, overage_per_min_usd:0.30`
  (+ `concurrent_sessions:1, reconnect_min_seconds:30`, which the prompt's `…`
  dropped — preserved).
- **`_ensure_paired()` is DEAD CODE** — defined at `core/orchestrator.py:238` but
  **never called anywhere**. The prompt's "it may hard-exit when unpaired" was
  false; today pairing happens in the terminal via `emma --first-run --pair`
  (install.sh step 7), *before* the daemon boots. Rewrote it into the park loop and
  wired it into `main_loop`.
- **`_credential_preflight` would exit 2 in managed mode** — it reads the raw
  `settings.OPENAI_API_KEY` (empty in managed mode), so a fresh unpaired managed
  daemon could never boot. Added a managed-mode skip.
- **`emma/ui/__main__.py` Part 3 already applied** — the menubar control channel +
  menu items exist (289 lines); the prompt undersold it as "Part 2/3".
- **`/api/balance` is cookie-authed only** (`credits_routes.py:35`, `require_user`) —
  the daemon's device **bearer cannot read it**. Part 4's Uso panel needs a NEW
  `require_device`-authed balance route; none exists today. (Gap, see REMAINING.)
- **`trigger_auto_refill` had no explicit `plan=="free"` guard** — free was safe only
  transitively (no payment method). Added an explicit guard.

## Part 1 — free = 90s/month trial — **DONE** (commit a91e4e2)
`PLAN_CAPS["free"]`: monthly_seconds 0→90, daily 60→90, cost_cap 40→15,
daemon_session_max 900→90, overage 0.30→0.0. Explicit `plan=="free"` no-charge guard
in `trigger_auto_refill`. Tests: new trial-contract test; updated the two assertions
that pinned free's old numbers (demo cost cap 15¢, account daily cap 1.5 min).
**Backend plan/credits/account/demo tests green** (3 pre-existing demo tool-exec
failures need network; 2 pre-existing: `test_warning_ticker_monthly_autorefill_message`,
`test_run_402_out_of_credits` — unrelated, but the ticker one overlaps Part 5).

## Part 2 — daemon parks unpaired-but-alive — **DONE** (commit 06b6f8d + landing 0a0dcb4)
`_ensure_paired` rewritten: managed mode publishes `state=needs_onboarding`, runs the
UI, polls the Keychain until the app writes a token, then loads it and enters the wake
loop. `onboarding_needed()` accessor for Part 3. Preflight skips managed. install.sh:
dropped terminal pairing, plist sets `EMMA_REQUIRE_PAIRING=1` + `EMMA_DASHBOARD=1`,
opens Emma.app. **6 new daemon tests + 29 boot/pairing tests green; sh -n clean.**

## Part 6 — honest download/landing copy — **DONE** (commit dacb97f + landing ab9762f)
download.html: "Descarga gratis … 90 segundos al mes … desde $19/mes"; steps now
describe app-owned onboarding + "Di Emma". Landing: "precio: por anunciar" →
"gratis para probar · desde $19/mes" (ES/EN). Public-copy safe.

## Part 3 — onboarding window — **REMAINING**
Needs the mandatory browser-verify loop (running daemon + Chrome), which is exactly
why EMMA-APP DEFERRED this same window HTML. Plan (infra is ready — Part 2 gives
`onboarding_needed()`, the device-code flow is `core/pairing.py` + backend
`/api/device/*`, verified present):
1. Dashboard HTTP: add `GET /api/onboarding-state` → `{paired, needs_onboarding}` (read
   `orchestrator.onboarding_needed()` / `pairing.is_paired()`), and a `/control` cmd
   `start_onboarding` that runs `pairing.start_pairing()`, returns the `user_code` +
   `verification_uri`, and kicks off `poll_until_authorized` in the daemon (writes the
   token → Part 2's park loop sees `is_paired()` and proceeds).
2. Onboarding HTML/JS in `dashboard/`: welcome → "create account / log in" button that
   opens `theemmafamily.com/pair?code=XXXX` in the browser (reuse PAIR-DEVICE-1, no
   password field in the WebView) → poll `/api/onboarding-state` → "Listo, 90s gratis,
   di Emma". Add a `needs_onboarding` icon bucket to `emma/ui/__main__.py`.
3. Browser-verify in Chrome against a locally-run daemon before commit.

## Part 4 — steady-state sections (Ahora/Personalidad/Memoria/Uso/Cuenta) — **REMAINING**
Same browser-verify requirement. Plus a **backend gap**: Uso reads balance via
`pairing.authed_client()`, but `/api/balance` is cookie-only — must add a
`require_device`-authed balance route (e.g. `GET /api/device/balance`) returning the
same shape, then the daemon proxies it to a local dashboard endpoint the Uso panel
fetches. "Comprar minutos" opens `theemmafamily.com/dashboard` (no in-app payment).
Personalidad/Memoria wire to the existing control cmds (`set_personality`, `forget`).

## Part 5 — out-of-minutes moment — **REMAINING (on-device verify)**
The proxy already hard-stops free at 90s: CAPA-5 closes the WS `code=4402
reason="balance_zero"` (`realtime_proxy.py:103-110`; with overage 0.0 + the free guard,
no charge). The daemon side needs: detect that close and (a) `say` the upsell, (b)
publish `state=out_of_minutes`. Hook: `core/conversation.py:447` already inspects
`ErrorFrame`s (`_TERMINAL_AUTH_MARKERS`, line 118). **Open question — not headlessly
verifiable:** whether Pipecat's OpenAIRealtime service surfaces the WS close *reason*
("balance_zero") in the ErrorFrame message. Confirm against the live proxy / on-device,
then add a `balance_zero`/`daily_cap` marker + an out-of-minutes handler (speak +
publish). Also update `_warning_ticker`'s 90%-monthly message for free (currently a
paid "activa la auto-recarga" line) → a trial upsell; this is the pre-existing
`test_warning_ticker_monthly_autorefill_message` overlap.

## Part 7 — clean-Mac E2E — **PENDING** (after Parts 3-5 + landing deploy)
`wipe → curl install → app onboarding → talk → hit 90s → upsell → buy → works again`.
On-device (GUI + voice + live proxy), Garcia confirms.

---

_Status: Parts 1, 2, 6 landed (main pushed after each). Parts 3, 4, 5 are the coupled
browser-verified + live-proxy remainder — the same window-HTML the mandatory
Chrome-verify convention caused EMMA-APP to defer, now with a clear plan and the
backend gap (device-bearer balance route) identified. Landing install.sh + index +
download page committed in their repos, NOT deployed (Kamal/run-backend after approval)._
