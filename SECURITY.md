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
- Network adversaries (mitigated by HTTPS to OpenAI/etc.).
- The conversation audio itself going to OpenAI during inference — that is an
  architectural acceptance (see "What goes to OpenAI"). This layer restricts
  what is in the *priming block*; it cannot eliminate the streaming audio path.

## The three trust tiers

| Tier | Examples | Storage |
|------|----------|---------|
| **Public** | tool registry, `self/capabilities.md`, version | plaintext on disk |
| **Personal** | preferences, profile facts ("prefers Zed", "lives in Monterrey"), schedule patterns | `~/.emma/memory.db` — cold-disk protection relies on **FileVault** |
| **Secret** | passwords, API keys, account numbers, government IDs (CURP/RFC/SSN), credit cards, IBAN | **macOS Keychain only** (`com.garcia.emma` service). `memory.db` may hold a `vault_ref` label, **never** the value |

**The architectural rule:** *no Secret-tier value ever lands in `memory.db`, in
logs, or in the system prompt sent to OpenAI.*

Enforcement:
- `core/secrets.py` is the only writer/reader of the Keychain.
- `memory/reflection.py` classifies each extracted fact (deterministic
  `core/redaction.py` patterns first, then a `gpt-4o-mini` fallback). Secrets
  route to Keychain; only `content="(stored as secret: <label>)"` + `vault_ref`
  hit `memory.db`. The secret value is never embedded (so it never reaches the
  embedding API).
- `memory/long_term.py:priming_block` filters `WHERE vault_ref IS NULL`.
- `core/redaction.py:redaction_processor` redacts every string in every log
  event (credit cards via Luhn, CURP, RFC, SSN, IBAN, phone, API-key shapes).

## Where each tier lives on disk

- **Public:** `self/capabilities.md`, source tree — `~/Documents/EMMA/`.
- **Personal:** `~/.emma/memory.db` (SQLite + sqlite-vec). Protect with FileVault.
- **Secret:** login Keychain, service `com.garcia.emma`. Migrated credentials use
  the env-var name as the label (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`,
  `PICOVOICE_ACCESS_KEY` — the Porcupine wake-word key added in Prompt 16 —
  `BRAVE_API_KEY`, `TAVILY_API_KEY`, `YOUTUBE_API_KEY`, `SPOTIFY_CLIENT_*`,
  `POSTGRES_DSN`); secret facts use `fact_<uuid>`; user secrets use whatever
  label you choose via the `remember_secret` tool. The canonical credential
  list lives in `config/settings.py:_CREDENTIAL_FIELDS`.

## How to wipe everything

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

## What goes to OpenAI vs. stays local

Be honest about the boundary:
- **Goes to OpenAI:** the live conversation audio (the Realtime API hears you
  directly), and the priming block of Personal facts injected into the system
  prompt. If you explicitly invoke `recall_secret`, that one value is spoken and
  therefore reaches the Realtime model — it is gated to "use only when alone."
- **Stays local:** all Secret-tier values at rest (Keychain), the full
  `memory.db`, and anything `redact()` catches in logs. The priming block never
  contains Secret-tier values (`vault_ref IS NULL` filter).

## Known limitations
- The `security add-generic-password -w <value>` call briefly exposes the value
  in the process's argument list — visible to other processes of the **same
  user** (out of scope per the threat model).
- The unstructured-secret classifier (e.g. "my password is hunter2", no PII
  pattern) depends on the `gpt-4o-mini` call; on classifier failure such a fact
  defaults to the Personal tier. Pattern-based secrets (cards, IDs, keys) are
  always caught deterministically by `redact()`.
- Log redaction requires `core.redaction.redaction_processor` to be wired into
  the structlog config (in `emma/__main__.py`).
