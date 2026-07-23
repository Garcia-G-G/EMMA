#!/usr/bin/env bash
# Sign + notarize dist/Emma.pkg (Prompt 29.1 — DEFERRED, not run during 29.0).
#
# This script EXISTS but is only meaningful once the user enrolls in the Apple
# Developer Program and exports the certs. It is NOT invoked by `make pkg`. When
# the env vars below are set, the already-built unsigned .pkg becomes
# Gatekeeper-clean in minutes. See installer/BUILD.md for the full walkthrough.
set -euo pipefail

: "${DEVELOPER_ID_INSTALLER:?set the 'Developer ID Installer: …' cert name}"
: "${AC_TEAM_ID:?set the Apple Connect team id}"
: "${AC_APPLE_ID:?set the Apple ID used for notarization}"
: "${AC_APP_PASSWORD:?set the app-specific password (appleid.apple.com)}"

IN="${1:-dist/Emma.pkg}"
SIGNED="dist/Emma-signed.pkg"
[[ -f "$IN" ]] || { echo "✗ $IN not found — run 'make pkg' first." >&2; exit 1; }

echo "==> productsign"
productsign --sign "$DEVELOPER_ID_INSTALLER" "$IN" "$SIGNED"

echo "==> notarytool submit --wait (5–15 min)"
xcrun notarytool submit "$SIGNED" \
  --apple-id "$AC_APPLE_ID" \
  --password "$AC_APP_PASSWORD" \
  --team-id "$AC_TEAM_ID" \
  --wait

echo "==> stapler staple"
xcrun stapler staple "$SIGNED"
xcrun stapler validate "$SIGNED"
spctl --assess --type install --verbose=4 "$SIGNED" || true
echo "✓ Signed + notarized → $SIGNED (ship this one)."
