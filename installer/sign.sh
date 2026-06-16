#!/usr/bin/env bash
# Code-sign dist/Emma.app with Hardened Runtime (Prompt 29, Part B1).
#
# Requires a "Developer ID Application" cert in the Keychain (Apple Developer
# Program enrollment). Team ID + signing identity come from the environment —
# NEVER hardcode them in the repo.
#
#   export EMMA_SIGN_IDENTITY="Developer ID Application: Gilberto Garcia (TEAMID)"
#   ./installer/sign.sh
set -euo pipefail

APP="${1:-dist/Emma.app}"
IDENTITY="${EMMA_SIGN_IDENTITY:?Set EMMA_SIGN_IDENTITY to your 'Developer ID Application: …' cert}"
ENTITLEMENTS="installer/entitlements.plist"

[[ -d "$APP" ]] || { echo "✗ $APP not found — run setup_py2app.py first." >&2; exit 1; }

echo "==> Signing $APP with: $IDENTITY"
# --options=runtime enables the Hardened Runtime (required for notarization).
codesign --deep --force --options=runtime --timestamp \
  ${ENTITLEMENTS:+--entitlements "$ENTITLEMENTS"} \
  --sign "$IDENTITY" "$APP"

echo "==> Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign --display --verbose=4 "$APP" 2>&1 | grep -E "Authority|Identifier|Runtime" || true
echo "✓ Signed."
