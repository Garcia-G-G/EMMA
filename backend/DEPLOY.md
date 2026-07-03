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

## Backups (recommended)

`/root/emma-data/backend_emma.db` is the single source of truth for accounts.
Snapshot it periodically:
```sh
ssh root@5.78.216.62 'cp /root/emma-data/backend_emma.db /root/emma-data/backup-$(date +%F).db'
```
