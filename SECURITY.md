# Emma — Security & Privacy

Emma is a local-first voice assistant. This document is the binding description
of what Emma protects, how, and what it explicitly does **not** protect.

> Platform note: the protections here are **macOS-only** (Keychain + FileVault).
> Windows/Linux equivalents are future work.

## Threat model

**In scope (Emma defends against these):**
- Cold disk — laptop lost/stolen, drive removed.
- Backups — Time Machine / iCloud backup of `~/Documents/EMMA` or `~/.emma/` leaking facts.
- Logs — `emma.log` / stderr accidentally containing secrets verbatim.
- Replay — a future Emma session repeating a previously-stored secret out loud unprompted.
- Multi-user — another user account on the same Mac cannot read this user's Emma data.

**Out of scope (do not assume protection):**
- Malware running as the same user with Accessibility entitlements.
- Hardware attacks (cold boot, Secure Enclave bypass).
- Network adversaries (mitigated by HTTPS to ElevenLabs and every other provider).
- The conversation audio itself going to ElevenLabs for transcription, and the
  reply text going to ElevenLabs for speech — that is an architectural acceptance
  (see "What leaves the Mac"). This layer controls what is stored and logged; it
  cannot eliminate the audio path.

## The three trust tiers

| Tier | Examples | Storage |
|------|----------|---------|
| **Public** | tool registry, `self/capabilities.md`, version | plaintext on disk |
| **Personal** | preferences, profile facts ("prefers Zed", "lives in <city>"), schedule patterns | `~/.emma/memory.db` — cold-disk protection relies on **FileVault** |
| **Secret** | passwords, API keys, account numbers, government IDs (any national format), credit cards, IBAN | **macOS Keychain only** (`com.garcia.emma` service). `memory.db` may hold a `vault_ref` label, **never** the value |

**The architectural rule:** *no Secret-tier value ever lands in `memory.db`, in
logs, or in anything sent to a provider (a prompt, a transcript, a TTS request).*

Enforcement:
- `core/secrets.py` is the only writer/reader of the Keychain.
- `memory/reflection.py` (Realtime backend only) classifies each extracted fact (deterministic
  `core/redaction.py` patterns first, then a `gpt-4o-mini` fallback). Secrets
  route to Keychain; only `content="(stored as secret: <label>)"` + `vault_ref`
  hit `memory.db`. The secret value is never embedded (so it never reaches the
  embedding API).
- `memory/long_term.py:priming_block` filters `WHERE vault_ref IS NULL`.
- `core/redaction.py:redaction_processor` redacts every string in every log
  event (credit cards via Luhn, government-ID shapes plus any format the user adds
  under `[redaction] id_patterns` in their profile, IBAN, phone, API-key shapes).

## Where each tier lives on disk

- **Public:** `self/capabilities.md`, source tree — `~/Documents/EMMA/`.
- **Personal:** `~/.emma/memory.db` (SQLite + sqlite-vec). Protect with FileVault.
- **Secret:** login Keychain, service `com.garcia.emma`. Migrated credentials use
  the env-var name as the label (`ELEVENLABS_API_KEY` — the cascade's one
  required credential — `OPENROUTER_API_KEY`, `OPENAI_API_KEY`,
  `PICOVOICE_ACCESS_KEY` — the Porcupine wake-word key added in Prompt 16 —
  `BRAVE_API_KEY`, `TAVILY_API_KEY`, `YOUTUBE_API_KEY`, `SPOTIFY_CLIENT_*`,
  `POSTGRES_DSN`); secret facts use `fact_<uuid>`; user secrets use whatever
  label you choose via the `remember_secret` tool. The canonical credential
  list lives in `config/settings.py:_CREDENTIAL_FIELDS`.
- **Social posting credentials (Prompt 26)** are Secret-tier and live in the
  Keychain only — never in `.env` or `memory.db`. Discord per-channel webhook
  URLs (label `discord_webhook_<channel>`, set via `remember_secret`) grant
  posting rights, so the URL itself is the secret; an optional X OAuth2
  user-context token uses label `X_ACCESS_TOKEN`. `core/secrets.py:_CRED_SUFFIXES`
  gained `_WEBHOOK` so any `*_WEBHOOK` env var also migrates to Keychain.

## How to wipe everything

Uninstalling does all of it: `curl -fsSL https://theemmafamily.com/uninstall.sh | sh`
deletes every Keychain item under the service (each fixed label by name, then
any user-named ones), `~/.emma`, the logs, and the TCC rows of both Emma bundles.

Per item:
```sh
security delete-generic-password -s com.garcia.emma -a <label>
```
Everything Emma stored in the Keychain:
```sh
python -m emma.security wipe-all
```
Personal tier:
```sh
rm ~/.emma/memory.db
```

## Recovery

**By design, losing the Keychain means Secret-tier values are unrecoverable.**
There is no escrow, no backup key, no recovery phrase — that is the point. If you
care about a stored secret beyond Emma's convenience, **export it to your password
manager**. Migrated API keys also remain retrievable from their original provider
dashboards. The sentinel `emma.master_present` lets `emma.security audit` detect a
wiped Keychain.

If you ever suspect a security regression (a secret in a log, a credential not
loading, a destructive tool acting without confirmation), re-run the live audit
from **Prompt 16.1** — it checks log redaction, the `vault_ref` priming filter,
the Keychain credential roundtrip in a launchd-like env, destructive-tool
confirmation gating, and `vocabulary.append_entry` injection resistance.

## What leaves the Mac

Be honest about the boundary. On the shipped voice (the cascade, v0.4.0+):

- **ElevenLabs, with your own key, and nothing else in the loop.** After the
  wake word fires, microphone audio streams to ElevenLabs Scribe and text comes
  back; Emma's reply text goes to ElevenLabs TTS and audio comes back. Your key
  goes from the Keychain straight to `api.elevenlabs.io`; no server of ours sees
  it, the audio or the text.
- **No model sees your memory.** The brain decides locally; nothing builds a
  prompt. Facts in `memory.db` are not sent anywhere on the cascade, and the
  boot-time embedding backfill (OpenAI) does not run there.
- **Before the wake word: nothing.** The wake word is detected offline.
- **Stays local:** Secret-tier values at rest (Keychain), the whole `memory.db`,
  the brain's decisions log, the voice clip cache.

What the Emma backend (`api.theemmafamily.com`) stores, and when a cascade
install talks to it:

- A cascade install contacts it **only if you activate a license** from the
  menubar: it sends the license key, a random `install_id` (Keychain) and the
  Mac's name. The backend keeps them in `licenses` / `license_activations`.
- Accounts made on the website (email, name, sign-in provider) and anything left
  from the old managed tier: device tokens with the Mac's name and the last raw
  IP, session minutes and usage rows. **Step 46** removes the managed proxy
  (`/realtime`, `/v1/*`), the demo, the raw `last_ip` and the identifying
  `device_name`.

On the gated Realtime backend (`VOICE_BACKEND=realtime`), OpenAI hears the
conversation audio and receives the priming block of Personal facts
(`vault_ref IS NULL` only). Text sent for summarization or reflection passes
through `redact()` first, so a card, key or ID number seen on screen or spoken
aloud becomes `[REDACTED:…]` before it crosses the network.

## Known limitations
- The `security add-generic-password -w <value>` call briefly exposes the value
  in the process's argument list — visible to other processes of the **same
  user** (out of scope per the threat model).
- The unstructured-secret classifier (e.g. "my password is hunter2", no PII
  pattern) depends on a `gpt-4o-mini` call (Realtime backend only); on classifier failure such a fact
  defaults to the Personal tier. Pattern-based secrets (cards, IDs, keys) are
  always caught deterministically by `redact()`.
- Log redaction requires `core.redaction.redaction_processor` to be wired into
  the structlog config (in `emma/__main__.py`).

## Secret rotation (24.6-A4)
All secrets live in the macOS Keychain (`com.garcia.emma` service), keyed by
label (e.g. `OPENAI_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `X_BEARER_TOKEN`). To
rotate one:
1. Revoke the old value at the provider and issue a new one.
2. Store the new value: `python -c "from core.secrets import store; store('<LABEL>', '<new>')"`
   — never put it in `.env` (it would only be re-migrated, and lingers in shell history).
3. Restart Emma (`launchctl kickstart -k …`). Settings re-read from Keychain.

Cadence + blast radius:
- **ELEVENLABS_API_KEY** — the cascade's one required key. Rotate at
  elevenlabs.io, then paste the new one from the menubar ("Mi key de
  ElevenLabs…"), which validates and stores it. Compromise = character spend
  on your ElevenLabs account; its usage page shows abuse.
- **OPENAI_API_KEY** (Realtime backend only) — manual; compromise = API spend + transcript exposure. Rotate
  immediately on suspicion; OpenAI usage dashboard shows abuse.
- **STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET** — manual via Stripe dashboard;
  compromise = forged billing events. Webhook signature verification limits this.
- **X / ElevenLabs / Brave / Resend / GitHub tokens** — manual at each provider;
  compromise is scoped to that one service (least-privilege tokens).
- **SESSION_SECRET / JWT_SECRET (backend)** — rotating invalidates all live
  sessions (users re-login); do it on any backend-host compromise.

## Incident response (key compromised)
1. Rotate the affected secret (above) FIRST — stops the bleeding.
2. Check the provider's usage/audit log for abuse during the exposure window.
3. If a key was ever committed to git: it is **permanently** compromised even
   after removal — rotate, then confirm `gitleaks detect` and the pre-flight
   audit (section P) are clean.
4. If the Mac itself was accessed: rotate everything, verify FileVault is on,
   and check `~/Library/Logs/Emma/` for anomalous tool calls.

## Reporting a vulnerability
Email **security@theemmafamily.com** (or open a private security advisory on the
repo). Please don't file public issues for exploitable bugs.

## 24.6 audit follow-up (adversarial review fixes)
A 5-agent adversarial audit of the session's changes found and we fixed:
- **CRITICAL — egress via tool results:** tool `data` + `user_message` reach the
  Realtime model verbatim; redaction was only on secondary summarize calls. Now
  redacted at the single `result_callback` seam (`core/conversation.py`), so any
  secret/PII a tool surfaces (OCR, AX screen text, web snippets) is scrubbed
  before egress regardless of the tool. (Trade-off: a long hex/base64 string in
  tool data may be over-redacted — acceptable for security-first.)
- **CRITICAL — prompt-injection → action:** the confirm gate let any prior user
  turn unlock a cold `confirmed=True`. Now a destructive cold-confirm is refused
  (converted to a spoken yes/no) when a tool ran since the user last spoke — the
  injection signature ("lee la página" → read tool → injected delete). The legit
  "borra X, sí" preemptive flow (no tool between words and call) still passes.
- **HIGH — lifecycle tools** (`shutdown`/`restart`/`snooze_listening`) are now
  `destructive=True` + two-phase confirmed, so injected content can't DoS Emma.
- **HIGH — `harden_local_files`** skips symlinks (was a startup chmod primitive).
- **HIGH — shell**: single-digit fd redirects (`1>`), `cp`/`truncate`/`ln -f`/
  `tee`/`osascript` now confirmation-gated; blocklist regex is DOTALL.
- **HIGH — Wake Studio backend**: concurrent-job cap (429) + epochs/sample ceilings.
- **MED**: redact-before-truncate on web/url/research egress; redact user query;
  redact recall/forget embed query. Dep CVEs cleared (aiohttp, setuptools,
  pydantic-settings).

## 24.6 hardening sweep (summary)
Egress redaction (screen/OCR/web/transcripts → OpenAI), prompt-injection
resistance (external content is inert data; runtime confirmation gate doesn't
trust LLM intent), shell hard-blocklist extended (SIP/kext/AppleScript ladder),
`~/.emma` perms enforced at startup (0700/0600), backend session cookie `Secure`
on HTTPS, Stripe webhook signature verified, SSRF guard on web fetches,
dependency CVEs cleared (pip-audit), bandit/gitleaks in `requirements-sec.txt`.

## Public surface (24.7 — the live demo)
Since LANDING-25.0/25.0.1, Emma is internet-facing at `api.theemmafamily.com`
(Fly). The demo is a SANDBOX, isolated from the daemon:
- **Zero daemon tools.** The demo bridge runs exactly 4 server-side tools
  (`web_search`, `current_time`, `translate`, `explain_install`) — the backend
  never imports the daemon's registry, so nothing the visitor says can touch
  the user's Mac. Every `function_call` is re-validated server-side; a
  non-whitelisted name is rejected, never executed.
- **Server-injected config.** The bridge sends the demo persona + voice + tools
  and DROPS any client `session.update` — a tampered client can't widen scope.
- **Wallet brakes.** Per-session cost cap ($0.40) + a daily ceiling
  (`DEMO_DAILY_USD_CEILING`, default $50) that 503s the demo once the rolling
  24h spend is reached — the brake against VPN-rotation abuse that sidesteps the
  per-IP/24h limit. An optional `OPS_ALERT_WEBHOOK` fires when the ceiling trips.
- **WS limits.** 256KB max inbound frame (anti memory-bomb), 50MB total session
  bandwidth (anti slow-drain), hard close at duration+5s.
- **Privacy.** IPs are stored/logged only as `sha256(ip + DEMO_IP_SALT)`; the
  attempt log keeps just the first 8 hex (non-deanonymizing). No audio bytes,
  no transcript, and no conversation content are ever persisted server-side.
- **`/demo/config`** exposes only public values (Turnstile SITE key + durations);
  `/demo/admin/daily-report` is auth-gated (`require_user`) and returns aggregates
  only — no IPs.

## Voice data hygiene (24.7-C1)
the user's wake-word training set is **biometric PII** (their actual voice):
- `scripts/wake_data/positive_real/*.wav` + `validation/*.wav` — local only,
  **gitignored** (never committed, verified absent from history), dir is `0700`.
- `~/.emma/wake_models/*.onnx` — derived from their voice (Personal tier), `0700`.
  These are that user's PERSONAL model and must NOT ship in any future installer payload;
  the `.pkg` builder (29.1) leaves an empty model dir for users to train their own.
- **Wipe:** `python scripts/wipe_wake_data.py` overwrites every `*.wav` with
  random bytes, then deletes (best-effort secure erase; FileVault is the real
  at-rest guarantee).
