#!/usr/bin/env bash
# Build the UNSIGNED Emma.pkg (Prompt 29.0). Reproducible from `make pkg`.
#
#   ./installer/macos_pkg/build_pkg.sh [VERSION]
#
# Produces dist/Emma.pkg — a productbuild distribution (GUI) wrapping a component
# pkg whose payload is the Emma source tree. The post-install provisions the venv
# with uv and loads the LaunchAgent. Signing/notarization is a separate, deferred
# step (installer/sign_and_notarize.sh); see installer/BUILD.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/installer/macos_pkg"
VERSION="${1:-$(grep -m1 -E '^version' "$ROOT/pyproject.toml" | sed -E 's/.*"([0-9.]+)".*/\1/')}"
VERSION="${VERSION:-1.0.0}"
STAGE="$(mktemp -d)/payload"
DIST="$ROOT/dist"
COMPONENT="$DIST/emma-component.pkg"
OUT="$DIST/Emma.pkg"

echo "==> Staging Emma source payload (v$VERSION)"
mkdir -p "$STAGE" "$DIST"
export COPYFILE_DISABLE=1  # no AppleDouble ._ files in the payload
# Ship the source + manifests; the venv is provisioned on the target by uv sync.
# Exclude dev/build cruft, the heavy landing repo, and anything user-specific.
rsync -a --delete \
  --exclude '.git' --exclude '.venv*' --exclude 'dist' --exclude 'build' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '_landing' --exclude '_shots' \
  --exclude '.env' --exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
  --exclude 'node_modules' --exclude '*.egg-info' --exclude '.claude' \
  --exclude '_planning' --exclude '.DS_Store' --exclude '.idea' --exclude '.vscode' \
  --exclude '*.log' --exclude 'data' \
  "$ROOT"/ "$STAGE"/
xattr -cr "$STAGE" 2>/dev/null || true  # strip quarantine/xattrs → no ._ forks

echo "==> pkgbuild component"
pkgbuild \
  --root "$STAGE" \
  --identifier com.garcia.emma \
  --version "$VERSION" \
  --install-location "/Library/Application Support/Emma" \
  --scripts "$HERE/scripts" \
  "$COMPONENT"

echo "==> productbuild distribution (GUI, unsigned)"
productbuild \
  --distribution "$HERE/Distribution.xml" \
  --resources "$HERE/resources" \
  --package-path "$DIST" \
  "$OUT"

rm -rf "$(dirname "$STAGE")"
echo "✓ Built $OUT (UNSIGNED — beta). Size: $(du -h "$OUT" | cut -f1)"
echo "  Local install: sudo xattr -r -d com.apple.quarantine '$OUT' && sudo installer -pkg '$OUT' -target /"
