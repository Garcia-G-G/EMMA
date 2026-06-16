# Building the Emma installer

## Now — unsigned `.pkg` (Prompt 29.0)

```sh
make pkg                      # → dist/Emma.pkg (UNSIGNED, beta)
```

The build (`installer/macos_pkg/build_pkg.sh`):
1. Stages the Emma source tree (no `.venv`, no `.git`, no landing/dev cruft).
2. `pkgbuild` → component pkg installing the source to `/Library/Application Support/Emma`.
3. `productbuild` → `dist/Emma.pkg` with the GUI (Welcome / License / Conclusion, arm64, macOS 14+).

On install, `scripts/preinstall` gates macOS 14+ / Apple Silicon / disk / `uv` + `ffmpeg`;
`scripts/postinstall` relocates the app to the user's `~/Library/Application Support/Emma`,
runs `uv sync` to provision the venv, installs the per-user LaunchAgent, surfaces the TCC
prompts (`emma.permissions bootstrap`), and opens the first-run wizard.

### Local install test (no certs needed)

```sh
sudo xattr -r -d com.apple.quarantine dist/Emma.pkg     # clear the download quarantine
sudo installer -pkg dist/Emma.pkg -target /
launchctl list | grep com.garcia.emma                    # agent loaded?
```

Uninstall: `~/Library/Application\ Support/Emma/installer/uninstall_macos.sh`
(add `--wipe` to also delete `~/.emma`, which holds your memory; preserved by default).

### Beta caveat (unsigned)

Until the installer is signed, macOS Gatekeeper shows an "unidentified developer"
warning. Bypass: right-click the `.pkg` → Open → "Open Anyway" in System Settings →
Privacy & Security. The Welcome screen and the first-run window say this is temporary.
The unsigned beta also assumes the build/host Mac's `uv`-managed Python — a fully
self-contained bundle is a later improvement.

## Later — sign + notarize (Prompt 29.1, deferred)

Blocked on Apple Developer Program enrollment. One-time setup:

1. **Enroll** in the Apple Developer Program ($99/yr, ~24–48h approval).
2. In **Xcode → Settings → Accounts**, create **Developer ID Application** + **Developer
   ID Installer** certificates; they land in your login keychain.
3. Generate an **app-specific password** at appleid.apple.com.
4. Export to a gitignored `.env.sign`:
   ```sh
   export DEVELOPER_ID_INSTALLER="Developer ID Installer: Gilberto Garcia (TEAMID)"
   export AC_TEAM_ID="TEAMID"
   export AC_APPLE_ID="garcia@example.com"
   export AC_APP_PASSWORD="abcd-efgh-ijkl-mnop"
   ```
5. Run it:
   ```sh
   source .env.sign && make sign-and-notarize     # → dist/Emma-signed.pkg
   ```

`installer/sign_and_notarize.sh` does `productsign` → `notarytool submit --wait` →
`stapler staple`. After that the `.pkg` installs with no Gatekeeper warning. Auto-update
(Sparkle) also ships in 29.1 — there's no point auto-updating to an unsigned binary.

## Distribution

Default: host the `.pkg` at `https://theemmafamily.com/dl/Emma-v{VERSION}.pkg`
(copy to the Hetzner VPS or `_landing/public/dl/`). The landing download page
(LANDING-25.0) links to it and documents the Gatekeeper bypass for the beta.
