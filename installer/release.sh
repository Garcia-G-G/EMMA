#!/usr/bin/env bash
# One-shot release: build → sign → pkg → notarize → Sparkle-sign → publish (Part D3).
#
#   ./installer/release.sh 1.0.1
#
# Env (see installer/README.md): EMMA_SIGN_IDENTITY, EMMA_INSTALLER_IDENTITY,
# APPLE_ID, TEAM_ID, APP_SPECIFIC_PASSWORD, SPARKLE_PRIVATE_KEY (EdDSA),
# and `gh` authenticated for the GitHub Release upload.
set -euo pipefail

VERSION="${1:?usage: release.sh <version>  e.g. 1.0.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PKG="dist/Emma-Installer.pkg"

echo "==> 1/6  Bump version → $VERSION"
sed -i '' -E "s/(version *= *\")[0-9.]+(\")/\1$VERSION\2/" pyproject.toml || true
sed -i '' -E "s/(CFBundle(Short)?Version(String)?\": *\")[0-9.]+(\")/\1$VERSION\4/g" setup_py2app.py || true

echo "==> 2/6  Build Emma.app (py2app)"
rm -rf build dist/Emma.app
# py2app 0.28 chokes on PEP-621 [project].dependencies; hide pyproject for the build
# (the venv already has every dep installed, so imports are unaffected). Always restore.
mv pyproject.toml /tmp/emma-pyproject.bak
trap 'mv /tmp/emma-pyproject.bak pyproject.toml 2>/dev/null || true' EXIT
.venv/bin/python setup_py2app.py py2app >/dev/null
mv /tmp/emma-pyproject.bak pyproject.toml; trap - EXIT

echo "==> 3/6  Sign (Hardened Runtime)"
./installer/sign.sh dist/Emma.app

echo "==> 4/6  Build + sign installer pkg"
./installer/build_pkg.sh "$VERSION"

echo "==> 5/6  Notarize + staple"
./installer/notarize.sh "$PKG"

echo "==> 6/6  Sparkle-sign + publish"
# Sparkle's sign_update emits the EdDSA signature + byte length for the appcast.
SIGINFO="$("${SPARKLE_BIN:-./Sparkle/bin}/sign_update" "$PKG" 2>/dev/null || true)"
echo "    appcast enclosure attrs: $SIGINFO"
echo "    → paste into installer/appcast.xml, commit, and push to the Pages branch."

# Publish the pkg as a GitHub Release asset (default distribution, Part E).
if command -v gh >/dev/null 2>&1; then
  gh release create "v$VERSION" "$PKG" --title "Emma $VERSION" --notes "Emma $VERSION" || \
  gh release upload "v$VERSION" "$PKG" --clobber
fi
echo "✓ Release $VERSION ready. Update appcast.xml with the printed signature, then push."
