#!/usr/bin/env bash
# Build the signed installer .pkg with a GUI (Prompt 29, Part B2).
#
# Produces dist/Emma-Installer.pkg: a productbuild distribution wrapping the
# component pkg (Emma.app → /Applications) + the postinstall (LaunchAgent + first
# run). Requires a "Developer ID Installer" cert; identity from the environment.
#
#   export EMMA_INSTALLER_IDENTITY="Developer ID Installer: Gilberto Garcia (TEAMID)"
#   ./installer/build_pkg.sh 1.0.0
set -euo pipefail

VERSION="${1:-1.0.0}"
APP="dist/Emma.app"
COMPONENT="dist/emma-component.pkg"
OUT="dist/Emma-Installer.pkg"
ID_INSTALLER="${EMMA_INSTALLER_IDENTITY:?Set EMMA_INSTALLER_IDENTITY to your 'Developer ID Installer: …' cert}"

[[ -d "$APP" ]] || { echo "✗ $APP not found — build + sign it first." >&2; exit 1; }

echo "==> pkgbuild component ($VERSION)"
pkgbuild --root dist/ --component-plist installer/component.plist \
  --identifier com.garcia.emma --version "$VERSION" \
  --install-location /Applications \
  --scripts installer/scripts \
  "$COMPONENT"

echo "==> productbuild distribution + signing"
productbuild --distribution installer/distribution.xml \
  --resources installer/assets \
  --package-path dist \
  --sign "$ID_INSTALLER" \
  "$OUT"

echo "✓ Built $OUT"
pkgutil --check-signature "$OUT" || true
