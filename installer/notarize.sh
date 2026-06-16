#!/usr/bin/env bash
# Notarize + staple the installer .pkg (Prompt 29, Part B3).
#
# Credentials from the environment — NEVER hardcoded:
#   export APPLE_ID="garcia@example.com"
#   export TEAM_ID="XXXXXXXXXX"
#   export APP_SPECIFIC_PASSWORD="abcd-efgh-ijkl-mnop"   # appleid.apple.com → app-specific password
#   ./installer/notarize.sh dist/Emma-Installer.pkg
#
# Tip: store the creds once with `xcrun notarytool store-credentials emma-notary`
# and pass --keychain-profile emma-notary instead of the three env vars.
set -euo pipefail

PKG="${1:-dist/Emma-Installer.pkg}"
: "${APPLE_ID:?Set APPLE_ID}"
: "${TEAM_ID:?Set TEAM_ID}"
: "${APP_SPECIFIC_PASSWORD:?Set APP_SPECIFIC_PASSWORD (app-specific, not your Apple ID password)}"

[[ -f "$PKG" ]] || { echo "✗ $PKG not found." >&2; exit 1; }

echo "==> Submitting $PKG to Apple notary service (5–15 min)…"
xcrun notarytool submit "$PKG" \
  --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APP_SPECIFIC_PASSWORD" \
  --wait

echo "==> Stapling the notarization ticket into the pkg"
xcrun stapler staple "$PKG"
xcrun stapler validate "$PKG"

echo "==> Gatekeeper assessment"
spctl --assess --type install --verbose=4 "$PKG" || true
echo "✓ Notarized + stapled. Safe to distribute."
