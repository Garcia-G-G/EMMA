# Release v0.2.0 — verify log

Started from the release runbook. **The runbook's premise was stale** (written
2026-08-07): it assumed `public/main` was 28 behind and `v0.2.0` un-cut. Reality on
2026-08-10/11:

- `v0.2.0` was **already tagged @ `858cef6` and pushed to `public`** — but *before*
  the 5 audit-cleanup commits, which include a **HIGH-severity crash-loop fix** (the
  unprotected `is_paired()` in `_ensure_paired` → locked-Keychain-at-login boot loop)
  and the wake-`SystemExit` crash-loop fix.
- The live installer still fetches `main` head (old installer, no bundle/throttle), so
  **no install ever resolved the `v0.2.0` tag** — it's effectively unconsumed.
- Decision (Garcia): **re-cut `v0.2.0`** to include the fixes.

## Prep done (reversible / local)
- Backup: `~/Desktop/emma-backup-2026-08-10-1427.tgz` (18MB) — verified to contain
  `memory.db` + `dictionary.toml` (+ personality/calibration/tasks). `.venv` excluded.
- Step 0 gate: working tree clean; `make lint` + `make type` clean (176 files);
  `make test` **1132 passed**, 10 deselected.
- Local `v0.2.0` tag moved `a2360a3 (858cef6)` → **`0a163a2`**; `public/main` FFs
  cleanly to it. The 5 newly-included commits: `3c1cff1 b284789 7c35708 9041fde 0a163a2`.
- `_landing/install.sh` already pins `EMMA_DEFAULT_VERSION="v0.2.0"` (committed
  `2e377d0`/`7a6c8e0`), `main` reachable via `EMMA_VERSION=main`. `_landing` tree clean.

## Step 1 — publish source  ☐ PENDING (Garcia runs the push)
```sh
cd ~/Documents/EMMA
git push public main            # FF public/main 858cef6 → 0a163a2
git push public -f v0.2.0       # move the unconsumed tag to include the fixes
# verify GitHub serves it:
curl -fsSL -H "Accept: application/vnd.github.sha" \
  https://api.github.com/repos/theemmafamily/emma/commits/main   # == git rev-parse main (0a163a2…)
curl -fsSL https://github.com/theemmafamily/emma/archive/refs/tags/v0.2.0.tar.gz | tar -tz | head -3
```

## Step 2 — deploy installer  ☐ PENDING (Kamal — needs approval; Garcia deploys)
`_landing` is committed + clean; the fixed `install.sh` (tag-pin + `EmmaDaemon`
bundle + `ThrottleInterval` + sherpa + app onboarding) is in but **not deployed** —
the live installer is still the pre-LAUNCH-2 one. After the Kamal deploy, verify what
nginx serves:
```sh
curl -sI https://theemmafamily.com/install.sh | grep -i content-type      # text/x-shellscript
curl -fsSL https://theemmafamily.com/install.sh | grep -c EmmaDaemon       # >0
curl -fsSL https://theemmafamily.com/install.sh | grep -c ThrottleInterval # >0
curl -fsSL https://theemmafamily.com/install.sh | grep -n EMMA_DEFAULT_VERSION  # pins v0.2.0
```

## Step 3 — clean-machine E2E  ☐ PENDING (destructive + on-device — Garcia)
Backup is already taken (above). Wipe + reinstall per the runbook, then walk the arc:
1. ☐ `uv sync` completes, no wheel errors
2. ☐ permission prompts WAIT; Accessibility row reads **Emma**
3. ☐ `…/EmmaDaemon.app/Contents/MacOS/emma-daemon -m emma.permissions check` → `AccessibilitySmoke: ok`; commit line matches v0.2.0
4. ☐ terminal exits without a pair code; `Emma.app` opens on onboarding
5. ☐ browser login → pairing completes → wake loop
6. ☐ say **Emma** → wakes (bare "Emma" may be silent = LAUNCH-3, not a release failure)
7. ☐ "¿Qué ves en la pantalla?" → real content
8. ☐ move a personality slider → next wake reflects it
9. ☐ talk ~90s → clean cutoff + spoken upsell + Uso buy button

## Rollback (know before Step 1)
- Bad source → `git push public :refs/tags/v0.2.0` (delete tag) → installs fail loud;
  retag a good commit. (Or move the tag back to `858cef6`.)
- Bad installer → revert in `_landing`, redeploy (one file behind nginx).
- Already installed → re-run `install.sh` (idempotent upgrade).

## Notes / open
- Repo casing: `_landing` uses `theemmafamily/EMMA`; GitHub URLs are case-insensitive.
- `CONFIG-USERDIR` still unbuilt: an idempotent `install.sh` re-extracts over
  `~/.emma/src`, so an upgrade can clobber `dictionary.toml` user data. Backup-before-
  upgrade until that lands (LAUNCH-8 Part 1).
