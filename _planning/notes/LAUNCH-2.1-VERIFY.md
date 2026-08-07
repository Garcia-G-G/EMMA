# LAUNCH-2.1 — verify log

Both items found by Garcia on-device after LAUNCH-2. Every premise checked against
the code AND the live bundle on this Mac before writing.

---

## Item 1 — "Python" in the Accessibility row — DIAGNOSED

**Verdict: neither prompt hypothesis. The daemon was not running as the bundle at
all — it ran `~/.emma/.venv/bin/python`, so the interpreter (not EmmaDaemon.app)
was the TCC subject.** The prompt's own tell nails it: a filename fallback would
read "emma-daemon"; the row read "Python", which is only possible if the *interpreter*
is the subject.

### Hypothesis A (embedded `__TEXT,__info_plist` naming Python) — RULED OUT
```
$ otool -s __TEXT __info_plist ~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon
  (empty — no such section)
$ otool -s __TEXT __info_plist <uv base python3.12>
  (empty — no such section)
```
Neither binary carries an embedded Info.plist. Not this.

### Re-confirmed the re-sign did NOT silently fail (install.sh `|| true`)
```
$ codesign -dvvv .../MacOS/emma-daemon
  Identifier=com.emma.daemon
  CDHash=81e88cd9…           # vs the uv interpreter's ccd32863…
  Signature=adhoc
```
The binary carries `com.emma.daemon`, not Python. The `|| true` swallowed nothing.
`Contents/Info.plist` is also correct: `CFBundleName` = `CFBundleDisplayName` = `Emma`.

### Hypothesis B (unsealed bundle) — TRUE that it's unsealed, but NOT the cause
The bundle is unsealed, and sealing is genuinely impossible (below). But unsealing
is a red herring for the row string:
```
$ codesign --force --sign - --identifier com.emma.daemon ~/.emma/EmmaDaemon.app
  code object is not signed at all
  In subcomponent: /Users/go/.emma/EmmaDaemon.app/Contents/pyvenv.cfg     # rc=1
```
That is the real sealing error — codesign treats the stray `pyvenv.cfg` at the
bundle root as unsignable nested code.

I initially suspected `mdls`/`lsappinfo` "could not find" meant macOS rejects the
unsealed bundle. **That was wrong** — a *properly sealed* minimal `Emma.app` is
equally "could not find":
```
$ codesign --force --sign - --identifier com.emma.daemon /tmp/Min.app   # rc=0, SEALED
$ mdls -name kMDItemDisplayName /tmp/Min.app  → "could not find"
```
So `mdls`/`lsappinfo` reflect LaunchServices *registration*, not TCC bundle
resolution. Meanwhile Security DOES resolve the binary to its bundle:
```
$ codesign -dvvv .../MacOS/emma-daemon
  Format=app bundle with Mach-O thin (arm64)
  Info.plist=not bound          # ← standalone-signed, so the seal doesn't bind Info.plist
  Sealed Resources=none
```

### The actual cause — the LaunchAgent ran the interpreter, not the bundle
```
$ launchctl print gui/$(id -u)/com.emma.daemon | grep program
  program = /Users/go/.emma/.venv/bin/python           # NOT the bundle
$ plutil -p ~/Library/LaunchAgents/com.emma.daemon.plist   # ProgramArguments[0]
  0 => "/Users/go/.emma/.venv/bin/python"
```
`install.sh` (LAUNCH-2) *does* write `${EMMA_DAEMON_BIN}` (the bundle) into the
plist — but `_landing/` is gitignored and undeployed, so Garcia's machine never got
that plist. He built the bundle with `scripts/build_daemon_bundle.sh`, which
**built EmmaDaemon.app but never rewrote the plist** — so the daemon kept running
the venv python, and any `emma.permissions bootstrap` he ran (via that python) made
the TCC row "Python".

### Why sealing can't be the fix (documented trade-off)
The three constraints are mutually incompatible, verified empirically:
- A `.app`'s executable must live at `Contents/MacOS/<CFBundleExecutable>`.
- CPython detects a venv only via `pyvenv.cfg` **one level up from the executable's
  dir** = `Contents/`. Placing it in `Contents/MacOS/` is NOT detected — tested:
  `sys.prefix` stayed at the base interpreter and site-packages (pydantic) vanished.
  The libpython load is also pinned: `@executable_path/../lib/libpython3.12.dylib`.
- codesign refuses to seal a bundle with a stray file (`pyvenv.cfg`) at `Contents/`.

So the venv marker must sit exactly where the seal forbids it. Options 1 (seal
anyway) and 2 (executable deeper) are both blocked by these; option 3
(PYTHONHOME/PYTHONPATH in the plist) is version-pinning and was rejected in LAUNCH-2
for good reason. **Chosen trade-off: leave the bundle unsealed.** It is unnecessary
for the row — Security already resolves the binary as `Format=app bundle`, and
LAUNCH-2 designed the unsealed bundle to surface `CFBundleDisplayName`. The fix is
to make the daemon actually *run* as that bundle.

### Fix
`scripts/build_daemon_bundle.sh` now, after building + smoke-testing the bundle,
repoints the LaunchAgent at it and reloads — the step it was missing:
`PlistBuddy -c "Set :ProgramArguments:0 <bundle-bin>"` (surgical: only the
executable/TCC-subject changes; env, WorkingDirectory, wake engine, pairing all
preserved) → `launchctl bootout` + `bootstrap`. Verified on-device:
```
$ launchctl print gui/$(id -u)/com.emma.daemon | grep program
  program = /Users/go/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon   # ✓ the bundle
```
`install.sh` already did this correctly (ProgramArguments[0] = `${EMMA_DAEMON_BIN}`,
line 245). The import smoke test + delete-and-fall-back path are untouched.

**Residual for Garcia (DoD #6, not headless-verifiable):** run
`~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon -m emma.permissions bootstrap`
and confirm the Accessibility row reads **Emma**. If it reads "emma-daemon"
instead, the only remaining lever is the version-pinned PYTHONHOME route (documented
trade-off above). Also note: the old "Python" TCC row (against the venv python)
lingers until reset — `tccutil reset Accessibility com.emma.daemon` won't touch it
(different subject); it can be removed by hand or it simply sits unused.

> Note: the installed `~/.emma/src` is a stale checkout predating the PAID-ONBOARDING
> managed-park changes, so the daemon currently exits under managed mode there
> (pre-existing, independent of this fix; resolves when install.sh pulls latest main).
> The TCC-identity fix stands regardless — it governs *which* binary runs, not whether
> that binary's app-code is current.

---

## Item 2 — permission flow too fast — FIXED

**Cause:** `core/permissions.py:bootstrap()` was timer-paced — mic `sleep(2)`,
automation `sleep(4)`/app, manual panes `sleep(6)`. Since LAUNCH-2 a real system
alert appears per permission, so those fixed dwells raced past the user ("hace el
proceso muy rápido"), and a missed Accessibility grant fails silently and
permanently.

**Fix — wait on the user, not a timer:**
- New `_await_grant(check_fn, ceiling_s, tty)` polls the permission's own probe and
  returns `granted` the instant it passes (no fixed sleep), or `skipped` on a TTY
  keypress, or `timeout` at a generous ceiling (120s; then auto-skip — the grant can
  be given later, bootstrap is idempotent). `_open_tty()` reaches the real terminal
  via `/dev/tty` (bootstrap's stdin is the piped script under `curl … | sh`), so the
  skip works when there's a TTY and falls back to the ceiling when there isn't.
- `bootstrap()` rewritten: one permission at a time, each with a spoken + printed
  **why** line *before* its dialog; mic → automation (the osascript consent dialog
  already blocks on the user's answer) → manual panes (poll the pane's probe, or a
  fixed 25s dwell for Full Disk Access, which has no read-back probe). Ends with a
  summary marking ✓/• per permission, listing what's still missing and how to finish
  (`emma.permissions retry`), and a spoken line that reflects whether anything is
  pending. `retry` already re-runs it (idempotent).
- **Accessibility recoverable from the app** (not only a shell re-run): new
  `/control` command `request_accessibility` → the *daemon* (as EmmaDaemon.app, the
  correct TCC subject after Item 1) calls `request_accessibility_trust()` + opens the
  pane; a menubar item "Dar acceso a la pantalla…" (`emma/ui`) sends it.

**LAUNCH-4 interaction:** untouched. LAUNCH-4 rate-limits the *daemon's runtime*
AX-denied spoken notice (`preflight()`); this is the *install-time* walkthrough's own
per-permission `_say`, a distinct path — no competing notifier added.

**Tests:** `_await_grant` granted-instantly / timeout / skip; bootstrap advances on
grant, opens every pane, speaks each why, and flags missing in the summary; the
`request_accessibility` control command. Full suite **1111 passed, 1 skipped**; ruff
+ mypy clean.

**Garcia confirms on-device (DoD #6):** run
`~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon -m emma.permissions bootstrap` —
each prompt waits for you (grant it and it advances immediately), the row reads
**Emma**, and the summary names anything skipped.
