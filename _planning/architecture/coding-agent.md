# Coding sub-agent: why native, not the Codex CLI

**Decision (Prompt 23):** Emma's `delegate_to_codex` runs a coding agent as a
Python-native tool-calling loop against the OpenAI **Responses API**
(`core/coding_agent.py`), NOT by shelling out to OpenAI's `@openai/codex` CLI.

## The forcing function: distribution trust

The original plan was a thin subprocess wrapper around the official `codex`
CLI. It was rejected for a concrete, non-theoretical reason:

Between **March–May 2026**, OpenAI rotated its Apple Developer signing
certificate after a supply-chain incident (a malicious **Axios 1.14.1**
pulled into their macOS app-signing workflow). The rotation **revoked older
Codex CLI builds**. macOS Gatekeeper now flags any install below
**v0.119.0** as *"malware"* — a false positive, but a lethal one for
distribution: a new Emma user installing Emma + Codex CLI and seeing
**"MALWARE BLOCKED"** on first launch loses trust instantly, and that trust
doesn't come back.

A second binary on a second update channel is a second thing that can break,
get revoked, or diverge from the version Emma was tested against. Every
Gatekeeper rotation is an outage we don't control.

## What native buys us

| | Codex CLI subprocess | Native Responses-API loop |
|---|---|---|
| Gatekeeper popup | first install + after each cert rotation | never |
| Distribution | npm + notarization race | `uv sync` ships everything |
| Update path | two channels | one — Emma owns it |
| Model | `gpt-5.3-codex` | **same** `gpt-5.3-codex` |
| Cost | per-token | **same** per-token |
| Sandbox | CLI flags | Emma defines the exact tool surface |
| events_bus | parse stdout | native `publish()` per tool call |

The model *is* the agent — the loop pattern (respond → call tools → feed
results → repeat until `finish`) is the same one Cursor / Cline / Aider run
internally. We give it six tools confined to the workdir
(`read_file`/`write_file`/`edit_file`/`list_files`/`run_command`/`finish`),
a `run_command` allowlist (no shells, no `rm`, no network), and guardrails
(max iters, 30-min wall clock, 2× budget hard-kill).

The cost is ~250 LOC more than a CLI wrapper. That's the price of a
single trust domain — `OPENAI_API_KEY` + `uv sync` and the coding agent
works for anyone, with nothing for Gatekeeper to revoke.

## One real-API lesson (live smoke, Prompt 23)

The first smoke 404'd: with `store=false`, the model's **reasoning items**
(`rs_…`) can't be echoed back as input ("Items are not persisted"). The fix
is the canonical reasoning-agent loop — `store=true` + `previous_response_id`,
sending only each turn's *new* input (the user task, then tool outputs) and
letting reasoning persist server-side. Cheaper too: no full-transcript
resend per turn.

Refs: developers.openai.com/api/docs/guides/tools ·
openai.com/index/unrolling-the-codex-agent-loop/ ·
developers.openai.com/api/docs/models/gpt-5.3-codex
