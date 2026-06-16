# Emma macOS installer (Prompt 29)

A signed + notarized `.pkg` with a first-run wizard, so distribution is: **send the
`.pkg`, double-click, done.** No terminal, no clone, no env vars by hand.

## What's here

| File | Role |
|---|---|
| `../setup_py2app.py` | Build `dist/Emma.app` (py2app). Self-contained Python + deps + Emma. |
| `sign.sh` | Code-sign the `.app` with Hardened Runtime (Part B1). |
| `entitlements.plist` | Hardened-Runtime entitlements (JIT/dyld for the bundled Python, mic, Apple Events). |
| `build_pkg.sh` | `pkgbuild` + `productbuild` → signed `dist/Emma-Installer.pkg` (Part B2). |
| `distribution.xml` / `component.plist` | Installer GUI (welcome + license, /Applications, arm64, 14+). |
| `scripts/postinstall` | Drop the per-user LaunchAgent + launch the first-run wizard. |
| `notarize.sh` | `notarytool submit --wait` + `stapler staple` (Part B3). |
| `firstrun/wizard.py` + `wizard.html` | 5-step Spanish first-run wizard, served on `127.0.0.1:8724` (Part C). |
| `appcast.xml` | Sparkle update feed (Part D1). |
| `release.sh` | build → sign → pkg → notarize → Sparkle-sign → GitHub Release (Part D3 / E). |
| `assets/` | `Emma.icns`, `welcome.html`, `license.txt`. |

## Build (no signing) — works today

```sh
uv pip install py2app
# py2app 0.28 trips on PEP-621 [project].dependencies; hide pyproject for the build:
mv pyproject.toml /tmp/p.bak && .venv/bin/python setup_py2app.py py2app ; mv /tmp/p.bak pyproject.toml
open dist/Emma.app --args --first-run   # launches the wizard
```
`release.sh` already wraps the pyproject dance.

## What still needs Garcia's manual action (the blocked half)

Signing, notarization, distribution, and auto-update are **hard-blocked on Apple
Developer enrollment** — none of it can run until these one-time steps are done:

1. **Enroll in the Apple Developer Program** ($99/yr, ~24–48h approval).
2. In Keychain Access / developer.apple.com, create + download **Developer ID
   Application** and **Developer ID Installer** certificates.
3. Generate an **app-specific password** at appleid.apple.com (for notarytool).
4. Generate the **Sparkle EdDSA key pair** (`Sparkle/bin/generate_keys`); paste the
   public key into `setup_py2app.py`'s `SUPublicEDKey`, keep the private key out of git.
5. Pick a host for the `.pkg` + `appcast.xml` (default: **GitHub Releases** +
   **GitHub Pages** for the feed — free, `gh` CLI in `release.sh`).

Then set the env and ship:

```sh
export EMMA_SIGN_IDENTITY="Developer ID Application: Gilberto Garcia (TEAMID)"
export EMMA_INSTALLER_IDENTITY="Developer ID Installer: Gilberto Garcia (TEAMID)"
export APPLE_ID=... TEAM_ID=... APP_SPECIFIC_PASSWORD=...
./installer/release.sh 1.0.0
```

After that the DoD smoke (fresh Mac → double-click → wizard → "hola Emma") and the
1.0.0→1.0.1 auto-update become verifiable. Until enrollment, the scripts are
written, parameterized, and ready — the bundle builds and the wizard runs.
