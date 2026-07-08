# Scaling notes

## ConnectionManager is single-container ONLY

`backend/connection_manager.py` (ABUSE-PROTECTION-2, Capas 2 + 3 + the kill-switch
fast path) keeps all its state in a **process-local** in-memory dict:

- `active_ws` — concurrent-session counts per user
- `connect_history` — sliding-window reconnect timestamps per user
- `disabled_users` — the instant kill-switch set
- `_ws_refs` — live WebSocket handles, so a disable can cut sessions

This is correct **only while Emma runs as one container with one uvicorn worker**
(the current deploy: a single `docker run` on the Hetzner host, fronted by
Cloudflare → `127.0.0.1:8000`). With one process, this dict is the whole truth.

### What breaks with multiple workers/containers

Each process would have its own `ConnectionManager`, so:

- **Concurrent cap leaks** — a user could open N sessions, one per worker, each
  seeing count 0.
- **Rate limit leaks** — reconnect history is per-process; a load balancer
  spreading reconnects defeats the window.
- **Kill switch goes partial** — `disable_and_cut` only closes sockets held by the
  process that served the admin request; sessions on other workers survive until
  their next DB check (`user_flags.disabled` is still enforced at connect time, so
  a *new* connection is blocked everywhere — but an *in-flight* one on another
  worker isn't cut instantly).

The DB-backed layers (kill switch via `user_flags`, daily/monthly caps, anomaly
throttle, audit chain) remain correct across workers — only the **in-memory** layers
degrade. That's the intended failure mode: caps still hold, just less tightly on the
concurrency/rate dimensions.

### Migration path (when it's time)

Move the three in-memory structures to Redis:

- Concurrent cap → `SADD`/`SCARD` on `sessions:{user_id}` with per-`ws_id` members,
  cleaned up on unregister (or a TTL heartbeat).
- Rate limit → `INCR` + `EXPIRE` on `rate:{user_id}:{window}`, or a sorted-set
  sliding window (`ZADD` now, `ZREMRANGEBYSCORE` older-than-window, `ZCARD`).
- Kill switch → pub/sub: publish `disable:{user_id}`; every worker subscribes and
  cuts its own local sockets. Keep `disabled_users` as a local cache primed from the
  channel.

Use `SET key val NX EX <ttl>` for atomic acquire where a hard single-owner lock is
needed. Until then: **do not add a second worker** without doing this migration.
