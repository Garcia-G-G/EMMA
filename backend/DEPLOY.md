# Backend deploy — READ THIS FIRST

The backend runs as a **plain `docker run`** container on the Hetzner host
(`root@5.78.216.62`), fronted by Cloudflare → `127.0.0.1:8000`.

## ⚠️ Data durability — the one rule that matters

The SQLite database (`backend_emma.db`: **all user accounts**, device tokens,
sessions) is **not** inside the image. It lives on a host volume:

- Host dir: `/root/emma-data/`
- Mounted into the container at `/data`
- `DATABASE_URL=/data/backend_emma.db` (set in `/root/emma.env`)

**The container is ephemeral.** Recreating it (`docker rm` + `docker run`)
without `-v /root/emma-data:/data` **wipes every account**. This actually
happened once (2026-07-03): several deploys silently reset the user table
because the DB was still the in-image default `backend_emma.db` with no mount.

## How to deploy

1. Copy changed files into the build context on the host:
   ```sh
   scp backend/*.py backend/static/* root@5.78.216.62:/root/backend/…   # (as appropriate)
   ```
2. Build + swap the container **via the canonical script** (guarantees the mount):
   ```sh
   ssh root@5.78.216.62 /root/run-backend.sh
   ```

`/root/run-backend.sh` does `docker build` → stop/rm → `docker run` with
`--env-file /root/emma.env -v /root/emma-data:/data -p 127.0.0.1:8000:8000`.

**Never** hand-write a `docker run` for emma-backend without the `-v` mount.

## Pages on the bare domain (Step 40.1)

Every HTML page is served by this backend but reached through the landing's
nginx on `theemmafamily.com` (it proxies `/entrar`, `/en/login`, … `/api/*`,
`/auth/*`, `/static/*`, `/admin` to `http://emma-backend:8000` over the
`kamal` docker network). `api.theemmafamily.com` keeps working for `/api/*`
(the daemon's licence activation) via cloudflared.

Server changes this needs (once):
1. `/root/run-backend.sh`: `--network kamal` and `-v /root/secrets:/run/secrets:ro`
   on the `docker run` line (keep `-p 127.0.0.1:8000:8000` for cloudflared and
   `-v /root/emma-data:/data`). Done 2026-10-15.
2. `/root/emma.env`: `PUBLIC_URL=https://theemmafamily.com` (CSRF origin check,
   reset links, OAuth callbacks and the `__Host-` session cookie all follow it).
   Done 2026-10-15.

## Secrets

Every key and signing secret is a file `/root/secrets/<NAME>` (dir 0700, files
0600), mounted read-only at `/run/secrets`; `backend/config.py:_secret` reads it.
`/root/emma.env` holds only non-secret settings. Add or rotate one:

```sh
ssh root@5.78.216.62 'umask 077; cat > /root/secrets/RESEND_API_KEY'   # paste, Enter, Ctrl-D
ssh root@5.78.216.62 docker restart emma-backend
```
3. OAuth apps (only if their keys are set): add the redirect URIs
   `https://theemmafamily.com/auth/google/callback` and
   `https://theemmafamily.com/auth/github/callback`.

Check mail from the server: `docker exec emma-backend python -m backend.mail --test <addr>`.

## Backups (recommended)

`/root/emma-data/backend_emma.db` is the single source of truth for accounts.
Snapshot it periodically:
```sh
ssh root@5.78.216.62 'cp /root/emma-data/backend_emma.db /root/emma-data/backup-$(date +%F).db'
```
