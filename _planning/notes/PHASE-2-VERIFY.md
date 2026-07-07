# Phase 2C — managed-daemon E2E verification (run on a test Mac)

Phase 2A (backend `/v1/*` proxy + WS metering) and Phase 2B (daemon gated to use
the proxy) are code-complete and unit-tested. This is the **on-hardware** check —
it can't be automated from the backend. Run it on a test Mac (not your daily) or
your own Mac with `EMMA_REQUIRE_PAIRING=1`.

## What "managed mode" flips
Setting `EMMA_REQUIRE_PAIRING=1` makes the daemon:
- require pairing at boot (`_ensure_paired`),
- resolve `settings.openai_api_key()` → the paired **device token** (cached at pairing),
- resolve `settings.openai_base_url()` → `https://api.theemmafamily.com/v1`,
- resolve `settings.realtime_base_url()` → `wss://api.theemmafamily.com/realtime`.

Without the flag, the daemon is unchanged (BYOK: your `OPENAI_API_KEY` + OpenAI direct).

## Steps

```sh
cd /Users/go/Documents/EMMA
# 1. Wipe any prior pairing on this Mac
security delete-generic-password -a device_token -s com.garcia.emma 2>/dev/null || true

# 2. Confirm the resolvers read managed values
EMMA_REQUIRE_PAIRING=1 .venv/bin/python -c "from config.settings import settings; \
print(settings.openai_base_url(), settings.realtime_base_url())"
# Expect: https://api.theemmafamily.com/v1  wss://api.theemmafamily.com/realtime

# 3. Start the daemon in managed mode
EMMA_REQUIRE_PAIRING=1 .venv/bin/python -m emma --debug
```

Expected: Emma dictates a pair code and prints `theemmafamily.com/pair`.

On the Mac's browser: log in at theemmafamily.com, open **/pair**, enter the code → "Listo".

Back in the daemon: it prints `device_paired_at_boot` and enters the wake loop.

## Verify the managed loop
1. **Voice (Realtime via proxy):** say "Hey Emma, dime la hora" → she answers.
2. **HTTP via proxy:** "Hey Emma, resume esta URL: <any article>" → `url_summary_tool` →
   chat/completions through the proxy.
3. **Embeddings via proxy:** "Hey Emma, recuerda que mi color favorito es azul" →
   reflection → embeddings through the proxy.

## Confirm metering landed (backend)
```sh
ssh root@5.78.216.62 'docker exec emma-backend python -c "
import sqlite3; from backend.config import settings
c=sqlite3.connect(settings.DATABASE_URL)
for r in c.execute(\"SELECT kind,model,input_tokens,output_tokens,seconds FROM usage_events ORDER BY id DESC LIMIT 10\"):
    print(r)"'
```
Expect rows with `kind=realtime` (seconds + audio tokens), `kind=http` /
`http-stream` (chat/embeddings), all for your user.

## Notes / known gaps to check
- The **coding agent** uses the Responses API; the proxy forwards it fine, but token
  metering keys differ (`input_tokens`/`output_tokens` vs `prompt_tokens`) — its rows
  may show 0 tokens until Phase 5 refines Responses metering. The CALL still works.
- If voice fails to connect in managed mode, check the daemon log for an
  `HTTP 401/403` at the Realtime handshake → the device token didn't resolve
  (pairing/cache issue), not a proxy bug.
- Backend proxy already smoke-verified live 2026-07-07: non-stream chat, embeddings,
  and streaming (first chunk ~634ms) all returned 200 + metered correctly.
