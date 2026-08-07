#!/bin/sh
# Verification shortcut — builds EmmaDaemon.app against an EXISTING ~/.emma
# install, without re-running install.sh.
#
# This is a faithful extraction of the step-5.5 block from _landing/install.sh
# (lines 141-215). It exists because `_landing/` is gitignored, so installer
# changes live on disk but are not deployed — you can't get the bundle via
# `curl install.sh | sh` until the landing deploy happens.
#
# NOT a replacement for the real install path. Use it to verify the bundle and
# the TCC identity today; the real E2E still has to go through install.sh once
# the landing is deployed.
set -eu

EMMA_HOME="${HOME}/.emma"
EMMA_SRC="${EMMA_HOME}/src"
EMMA_VENV="${EMMA_HOME}/.venv"
LOG="${EMMA_HOME}/bundle-build.log"
: > "$LOG"

[ -x "${EMMA_VENV}/bin/python" ] || {
  echo "✗ No hay venv en ${EMMA_VENV}. ¿Corriste el instalador alguna vez?" >&2
  exit 1
}

DAEMON_APP="${EMMA_HOME}/EmmaDaemon.app"
BASE_PY="$("$EMMA_VENV/bin/python" -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
PY_XY="$("$EMMA_VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
PY_FULL="$("$EMMA_VENV/bin/python" -c 'import sys; print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null || true)"

if [ -z "$BASE_PY" ] || [ ! -x "$BASE_PY" ]; then
  echo "✗ No pude resolver el intérprete base (sys._base_executable)." >&2
  exit 1
fi

echo "→ intérprete base: $BASE_PY  (python ${PY_FULL})"
BASE_HOME="$(dirname "$(dirname "$BASE_PY")")"

rm -rf "$DAEMON_APP"
mkdir -p "$DAEMON_APP/Contents/MacOS" "$DAEMON_APP/Contents/lib"

cat > "$DAEMON_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Emma</string>
  <key>CFBundleDisplayName</key><string>Emma</string>
  <key>CFBundleIdentifier</key><string>com.emma.daemon</string>
  <key>CFBundleExecutable</key><string>emma-daemon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Emma escucha tu palabra clave y tus peticiones de voz.</string>
</dict></plist>
EOF

# Sign the interpreter copy OUTSIDE the bundle, then move it in — a plain copy
# keeps the uv interpreter's cdhash (the very subject we're escaping), and
# signing in place makes codesign try to seal the bundle, which fails on
# Contents/pyvenv.cfg.
cp "$BASE_PY" "${DAEMON_APP}.staged-bin"
codesign --force --sign - --identifier com.emma.daemon "${DAEMON_APP}.staged-bin" >>"$LOG" 2>&1 || true
mv "${DAEMON_APP}.staged-bin" "$DAEMON_APP/Contents/MacOS/emma-daemon"
chmod +x "$DAEMON_APP/Contents/MacOS/emma-daemon"

cat > "$DAEMON_APP/Contents/pyvenv.cfg" <<EOF
home = ${BASE_HOME}/bin
include-system-site-packages = false
version = ${PY_FULL}
EOF

ln -sfn "${BASE_HOME}/lib/libpython${PY_XY}.dylib" "$DAEMON_APP/Contents/lib/libpython${PY_XY}.dylib"
ln -sfn "${EMMA_VENV}/lib/python${PY_XY}"          "$DAEMON_APP/Contents/lib/python${PY_XY}"

if ( cd "$EMMA_SRC" && "$DAEMON_APP/Contents/MacOS/emma-daemon" -c 'import emma, config.settings' ) >>"$LOG" 2>&1; then
  echo "✓ Bundle construido y arranca: $DAEMON_APP"
else
  rm -rf "$DAEMON_APP"
  echo "✗ El bundle no pudo importar emma — revertido. Log: $LOG" >&2
  exit 1
fi

# Repoint the LaunchAgent at the bundle — the whole point. Building the .app is
# useless if the daemon keeps running ~/.emma/.venv/bin/python: THAT interpreter,
# not the bundle, is then the TCC subject, and the Accessibility row reads
# "Python". install.sh already writes the bundle path into the plist; this
# shortcut has to do the same, or the row never changes. We only swap
# ProgramArguments[0] (the executable = the TCC subject); every other key — env,
# WorkingDirectory, wake engine, pairing — is preserved.
AGENT_LABEL="com.emma.daemon"
PLIST="${HOME}/Library/LaunchAgents/${AGENT_LABEL}.plist"
BUNDLE_BIN="$DAEMON_APP/Contents/MacOS/emma-daemon"
echo ""
if [ -f "$PLIST" ]; then
  if /usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 ${BUNDLE_BIN}" "$PLIST" >>"$LOG" 2>&1; then
    launchctl bootout "gui/$(id -u)/${AGENT_LABEL}" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$PLIST" >>"$LOG" 2>&1; then
      echo "✓ LaunchAgent repointed al bundle y reiniciado — el daemon ahora corre como EmmaDaemon.app."
    else
      echo "! Reescribí el plist pero no recargó. Hazlo a mano:" >&2
      echo "    launchctl bootout gui/$(id -u)/${AGENT_LABEL}; launchctl bootstrap gui/$(id -u) \"$PLIST\"" >&2
    fi
  else
    echo "! No pude reescribir ProgramArguments en $PLIST — el daemon seguirá corriendo el intérprete (fila TCC 'Python'). Log: $LOG" >&2
  fi
else
  echo "! No hay LaunchAgent en $PLIST. Corre el instalador para registrar el servicio; sin eso el bundle existe pero nada lo ejecuta." >&2
fi

echo ""
echo "── identidad ──────────────────────────────────────────"
codesign -dvvv "$DAEMON_APP/Contents/MacOS/emma-daemon" 2>&1 | grep -E "Identifier|CDHash" || true
echo "  (compara con el intérprete de uv:)"
codesign -dvvv "$BASE_PY" 2>&1 | grep -E "Identifier|CDHash" || true
echo ""
echo "Verifica que el daemon corre como el bundle:"
echo "  launchctl print gui/$(id -u)/${AGENT_LABEL} | grep -i program"
echo "  $DAEMON_APP/Contents/MacOS/emma-daemon -m emma.permissions check"
