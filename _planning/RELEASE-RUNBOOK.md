# Release runbook — publishing Emma

This is the first real release of the full stack. It touches **two independent
git repos and one deploy channel**, and getting the order wrong breaks every new
install. Follow it in order; verify after each step.

Written 2026-08-07, when local `main` sat **28 commits ahead** of the public head
`install.sh` fetches — including the stale-code detector built to catch exactly
that.

---

## The three moving parts

| Artifact | Lives in | Reaches users via |
|---|---|---|
| Emma source | `theemmafamily/emma` (remote `public`) | `install.sh` fetches a tarball of `main` |
| `install.sh` / `uninstall.sh` | `_landing/` — **its own git repo** | nginx container on Hetzner, via Kamal |
| Backend | this repo's `backend/` | Docker on Hetzner (`/root/run-backend.sh`) |

They are versioned independently. Nothing enforces that they agree. That
independence is the root cause of every version-skew bug this project has hit.

---

## Change the release model first (do this once)

`_landing/install.sh:98-103` defaults to the **`main` branch head**:

```sh
latest|main) url="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
```

So every new install takes whatever is on `main` at that instant, including
half-finished work. Publishing stops being a deliberate act — which is precisely
the failure this project keeps paying for.

**Change the default to a pinned tag.** Then:
- Publishing is an explicit decision (cut a tag), not a side effect of pushing.
- The version stamp in `emma.permissions check` compares against something
  meaningful.
- A bad release is one retag away from rollback instead of a scramble.

Keep `main` reachable via `EMMA_VERSION=main` for testing. Do this before the
release below, so the release itself is tagged.

---

## Order matters — source before installer

**Source first, then `install.sh`.** Reasoning:

- **New source + old installer** → the installer skips the bundle, the plist
  points at the venv python, no `ThrottleInterval`. The daemon still runs new
  code: it parks correctly, the exemption works, the version stamp logs.
  **Degraded but functional.**
- **New installer + old source** → the installer builds the bundle and runs
  `emma.permissions bootstrap` from it, against a module with no request APIs and
  no `_await_grant`. **Broken or silently degraded, in the permission flow that
  LAUNCH-2 exists to fix.**

There is a window between the two steps where new users get the mismatch. Keep it
to minutes; don't start step 1 unless you can finish step 2.

---

## Step 0 — Know what's about to ship

```sh
cd ~/Documents/EMMA
git fetch public
git log --oneline public/main..main            # everything users have never seen
git diff --stat public/main..main
git status --short                             # must be clean
make test && make lint && make type            # must be green
```

Read the log. 28 commits is a lot of surface. Anything here you would not
personally support at 3am belongs on a branch, not in this release.

```sh
cd ~/Documents/EMMA/_landing
git status --short                             # must be clean
git log --oneline -5                           # confirm 6e87a40 (install.sh) is in
```

## Step 1 — Publish the source

```sh
cd ~/Documents/EMMA
git tag -a v0.2.0 -m "Launch hardening: tool budget, screen vision AX, app window, boot"
git push public main
git push public v0.2.0
```

**Verify what GitHub actually serves** — not what you pushed:

```sh
curl -fsSL -H "Accept: application/vnd.github.sha" \
  https://api.github.com/repos/theemmafamily/emma/commits/main
# must equal: git rev-parse main

curl -fsSL https://github.com/theemmafamily/emma/archive/refs/tags/v0.2.0.tar.gz \
  | tar -tz | head -3
```

## Step 2 — Deploy the installer

`_landing/` deploys through Kamal, which **builds from a git clone — uncommitted
changes are ignored.** That has bitten this project twice. Commit first, always.

```sh
cd ~/Documents/EMMA/_landing
git status --short          # must be empty
git push                    # _landing has a remote (LAUNCH-10); it exists on
                            # more than one disk now, so push before deploying
# then your normal Kamal deploy
```

### REQUIRED GATE — verify the LIVE artifact, never the repo

**This step is not optional and it is not "read `_landing/install.sh` and
satisfy yourself".** The repo having a fix says nothing about what nginx
serves. On 2026-09-08 every one of these checks returned 0 against production
while `_landing/` had all of them committed — the whole of LAUNCH-2's
permission work, the respawn throttle, and the version stamp existed for
zero users. That gap has now cost three separate investigations.

Run this block verbatim. **Every count must be non-zero and the content-type
must be `text/x-shellscript`. If any line prints 0, the deploy did not happen
— do not proceed, and do not tag.**

> **The gate tests the EDGE, not the origin. Never add a cache-busting query
> string to it (added 2026-09-08).**
>
> On 2026-09-08 the deploy landed correctly — `install.sh?cachebust=X` returned
> the new installer — while the **plain URL, the one every user actually runs,
> served the previous one from Cloudflare's edge.** Same version-skew failure as
> the git one, one layer down, and it would have shipped a release nobody
> received.
>
> `?x=123` is a cache miss by definition, so it tests the origin and tells you
> nothing about what a user gets. That is the entire point of this step.
>
> `nginx.conf` already sets `Cache-Control: no-store` on both scripts, so if the
> edge still serves stale the cause is upstream of nginx:
>
> ```sh
> curl -sI https://theemmafamily.com/install.sh | grep -iE "cache-control|cf-cache-status|age"
> ```
> - `cf-cache-status: DYNAMIC` or `BYPASS` → no rule; a one-time purge fixes it
>   permanently (the stale entry predates `no-store`).
> - `cf-cache-status: HIT` or `MISS` → a Cloudflare **Cache Rule / Page Rule
>   doing "Cache Everything"** is overriding the origin header. Add a
>   Bypass-cache rule for `URI Path equals /install.sh` and `/uninstall.sh`,
>   ordered **above** the catch-all. A purge alone will not hold.
>
> **Ordering hazard:** purging while the pinned tag does not yet exist turns
> "users get the old installer" into "every install 404s". Confirm the tag first:
> ```sh
> curl -sI https://github.com/theemmafamily/EMMA/archive/refs/tags/vX.Y.Z.tar.gz | head -1
> # 200 or 302 = the tag exists. 404 = publish before you purge.
> ```

```sh
for p in ThrottleInterval EmmaDaemon install.json; do
  printf "%-18s %s\n" "$p" \
    "$(curl -fsSL https://theemmafamily.com/install.sh | grep -c "$p")"
done
printf "%-18s %s\n" "tccutil" \
  "$(curl -fsSL https://theemmafamily.com/uninstall.sh | grep -c tccutil)"
curl -sI https://theemmafamily.com/install.sh | grep -i content-type
curl -fsSL https://theemmafamily.com/install.sh | grep -n "EMMA_DEFAULT_VERSION"
```

What each one proves, and what its absence means for a real user:

| Check | Proves | If 0 |
|---|---|---|
| `ThrottleInterval` | the respawn throttle is live | launchd retries every 10 s, forever |
| `EmmaDaemon` | step 5.5 builds the app bundle | TCC grants belong to the shared uv python |
| `install.json` | the version stamp is written | `core/version.py`'s primary signal is missing |
| `tccutil` (uninstall) | grants are reset on removal | permission rows outlive the app |
| content-type | nginx serves it as a script | `curl … | sh` may get `text/html` |
| `EMMA_DEFAULT_VERSION` | pins a tag, not a branch head | every install takes whatever is on `main` |

## Step 3 — Clean-machine E2E

This is what the last four prompts have been waiting on. It closes LAUNCH-2
DoD #10, LAUNCH-4 DoD #10 and LAUNCH-7 DoD #2 in one pass.

### Back up FIRST — and back up more than `memory.db`

`rm -rf ~/.emma` destroys more than the fact store, because
`core/dictionary.py:24` resolves `_DICT_PATH` to
`~/.emma/src/config/dictionary.toml` — **user data lives inside the source tree
the wipe deletes.** That file holds `[user]`, `[personality]` (the five axes),
`[facts]`, `[pages]`, `[terms]`, `[apps]`, `[contacts]`. Also loose in `~/.emma`:
`calibration.json`, `tasks.jsonl`, `conditionals.db`, `boot_state.json`,
`spotify_token.json`.

A `memory.db`-only backup silently loses the personality profile and every
voice-taught fact. Take the lot:

```sh
tar -czf ~/Desktop/emma-backup-$(date +%F-%H%M).tgz \
  --exclude='.venv' -C ~ .emma

# Verify before deleting anything — no output means DO NOT PROCEED
tar -tzf ~/Desktop/emma-backup-*.tgz | grep -E "dictionary.toml|memory.db"
```

`.venv` is excluded because `uv sync` regenerates it. Everything else is not
regenerable — there is no backup system yet (`LAUNCH-8` Part 1).

**This is also the write-path bug from `CONFIG-DEPERSONALIZE-VERIFY.md` biting
for real:** an idempotent re-run of `install.sh` re-extracts the tarball over
`~/.emma/src`, so a plain upgrade can clobber user data too. The wipe just makes
it obvious. `CONFIG-USERDIR` (moving the write path to `~/.emma/dictionary.toml`)
is still unbuilt and should not stay that way much longer.

```sh
# Wipe
launchctl bootout gui/$UID/com.emma.daemon 2>/dev/null || true
rm -rf ~/.emma
rm -f ~/Library/LaunchAgents/com.emma.daemon.plist
security delete-generic-password -a device_token -s com.garcia.emma 2>/dev/null || true
tccutil reset Accessibility com.emma.daemon 2>/dev/null || true
tccutil reset ScreenCapture com.emma.daemon 2>/dev/null || true

# Install
curl -fsSL https://theemmafamily.com/install.sh | sh
```

Then walk the arc, checking each:

1. `uv sync` completes (no wheel errors)
2. Permission prompts **wait for you**; the Accessibility row reads **Emma**
3. `~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon -m emma.permissions check`
   → `AccessibilitySmoke: ok`, and the commit line matches the release channel
4. Terminal exits without asking for a pair code; `Emma.app` opens on onboarding
5. Browser login → pairing completes → daemon enters the wake loop
6. Say **Emma** → she wakes (the accent test — the repo's own measurement says
   bare "Emma" may be silent; if so, that's LAUNCH-3, not a release failure)
7. "¿Qué ves en la pantalla?" → real content
8. Move a personality slider → next wake reflects it
9. Talk ~90 s → clean cutoff + spoken upsell + Uso shows the buy button

Record it in `_planning/notes/RELEASE-v0.2.0-VERIFY.md`. **A step that fails is
information, not a reason to skip it** — note it and continue where you can.

---

## Learned on v0.3.0 / v0.3.1 (2026-10-13)

- **Upgrade in place vs wipe are different procedures.** Step 3 above is the
  clean-machine E2E — it wipes `~/.emma`, the Keychain token and the TCC
  grants. A normal release verification is an **in-place upgrade**: back up,
  re-run `install.sh`, verify the `Source:` block. Don't wipe unless you are
  testing first-run. The v0.3.1 reinstall was done in place; `dictionary.toml`
  survived.
- **Every tag push since v0.1.0 fired the `.pkg`/`.msi` workflows, which
  could never pass** (no signing secrets in either repo; macOS runner had
  Python 3.14 and no PortAudio; Windows job asserted the host was a Mac).
  As of cd48de6 they run only on manual dispatch. Red runs on a tag are now a
  real signal.
- **`harden_local_files()` chmod'd the daemon binary to 0600 on every boot**
  (42f84ff). First boot fine; every respawn exited 78 (EX_CONFIG) with
  nothing logged. This was the "stuck daemon" since June — not an unloaded
  agent. Lesson: a daemon that was "unloaded" in a report is a guess until
  `launchctl print` says so; read the exit code.
- **Upgrading over a running daemon** raced the old process (`Bootstrap
  failed: 5`, installer exit 1). dc1981d waits and retries once.
- **Backticks in a plist comment were executed by the shell** (dc1981d).
  Templates that pass through `sh` are shell, comments included.
- The public repo is `theemmafamily/EMMA` (capital). `_landing` has a remote
  (`Garcia-G-G/emma-landing`).
- Tests that need an external key are marked (`openai_live`, like
  `macos_live`) and deselected by default. A revoked key is not a red gate.

## Learned on v0.4.0 / v0.4.1 (2026-10-13)

- **The installer inherits the shell that runs it.** A leftover
  `OPENAI_API_KEY` exported by `~/.bash_profile` short-circuited the Settings
  Keychain fill, so the new ElevenLabs key was never read (v0.4.1 fixes it).
  When an upgrade misbehaves, run the failing step again with `env -u` on
  each credential variable before blaming the code.
- **A suite that hangs looks like one that ran out of memory.** The gate was
  OOM-killed twice before `-o faulthandler_timeout=180` showed the cause: a
  test reached a live cascade session (mic + network) because the dev `.env`
  had a real voice. `tests/conftest.py` now blanks the voice and key for every
  test. Run the gate with `faulthandler_timeout`.
- **`curl … | sh < /dev/null` runs nothing**: the redirect replaces the
  pipe. To run the live installer non-interactively, download it, then
  `sh install.sh < /dev/null`.
- **Verify the voice from the installation**: `emma.permissions check` prints
  `Voice: cascade … Ready: yes|NO — missing …` above the `Source:` block.

## Rollback

**Blast radius is new installs only.** Existing users don't auto-upgrade; nothing
re-runs `install.sh` on their behalf. That is the one piece of luck in this
architecture — use it.

- **Bad source** → **delete the tag.** Now that `install.sh` pins
  `EMMA_DEFAULT_VERSION`, a missing tarball makes installs **fail loudly** instead
  of silently taking bad code. That's the better kill: a 404 is a support email,
  bad code running unnoticed is not. Retag to the previous good commit when
  you're ready. (Force-pushing `main` also works but is slower and quieter —
  prefer the tag.)
- **Bad installer** → revert in `_landing/` and redeploy. Faster than the source
  path because it's one file behind nginx.
- **Someone already installed the bad version** → they re-run `install.sh`; it's
  idempotent and upgrades in place.

Know which one you'd do *before* you start step 1.

---

## After the release

- Backend and source are still versioned independently. The daemon talks to the
  proxy over a wire format neither side asserts. Worth a version handshake
  eventually — note it, don't build it now.
- `LAUNCH-8` Part 1 (backup) is independent of everything and protects the users
  you already have. It's the cheapest high-value thing left.
- The two LAUNCH-4 follow-ups stay open: a proactive job 401s against the proxy
  on every unpaired boot, and `env_file=".env"` is CWD-relative so `emma-ui` can
  silently load a different settings object than the daemon.
