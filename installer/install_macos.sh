#!/usr/bin/env bash
# Install Emma as a launchd LaunchAgent. Idempotent: safe to re-run.
set -euo pipefail

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
step()  { printf "${BLUE}==>${RESET} %s\n" "$*"; }
ok()    { printf "${GREEN}\xe2\x9c\x93${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${RESET} %s\n" "$*"; }
fail()  { printf "${RED}\xe2\x9c\x97${RESET} %s\n" "$*" >&2; exit 1; }

EMMA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${HOME}"
UID_NUM="$(id -u)"
PLIST_NAME="com.garcia.emma.plist"
PLIST_SRC="${EMMA_ROOT}/installer/${PLIST_NAME}"
PLIST_DST="${HOME_DIR}/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="${HOME_DIR}/Library/Logs/Emma"
VENV_PYTHON="${EMMA_ROOT}/.venv/bin/python"
SERVICE_TARGET="gui/${UID_NUM}/com.garcia.emma"

# 1. macOS version + arch
step "Checking macOS version and architecture"
os_major="$(sw_vers -productVersion | cut -d. -f1)"
[[ "${os_major}" -ge 14 ]] || fail "macOS 14 (Sonoma) or newer required. Found $(sw_vers -productVersion)."
[[ "$(uname -m)" == "arm64" ]] || fail "Apple Silicon (arm64) required. Found $(uname -m)."
ok "macOS $(sw_vers -productVersion) on $(uname -m)"

# 2. Python 3.11+
step "Checking Python"
command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install with: brew install python@3.11"
py_ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
py_major="$(echo "${py_ver}" | cut -d. -f1)"
py_minor="$(echo "${py_ver}" | cut -d. -f2)"
if (( py_major < 3 )) || { (( py_major == 3 )) && (( py_minor < 11 )); }; then
    fail "Python 3.11+ required. Found ${py_ver}. Install with: brew install python@3.11"
fi
ok "Python ${py_ver}"

# 3. ffmpeg
step "Checking ffmpeg"
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg not found. Install with: brew install ffmpeg"
ok "ffmpeg"

# 4. uv + dependencies
step "Checking uv"
command -v uv >/dev/null 2>&1 || fail "uv not found. Install with: brew install uv"
ok "uv $(uv --version | head -n1)"

step "Installing Python dependencies"
( cd "${EMMA_ROOT}" && uv sync )
ok "Dependencies installed"

# 5. Playwright Chromium (one-time, ~180MB)
step "Installing Playwright Chromium (one-time, idempotent)"
( cd "${EMMA_ROOT}" && uv run playwright install chromium >/dev/null 2>&1 ) || \
    warn "Playwright install exited non-zero; browser tools may not work."
ok "Playwright Chromium ready"

# 6. .env handling
ENV_FILE="${EMMA_ROOT}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    step "Creating .env from template"
    cp "${EMMA_ROOT}/.env.example" "${ENV_FILE}"
    warn "Opening .env. Fill in the API keys, save the file, then come back here."
    open "${ENV_FILE}"
    printf "Press Enter once you have saved your .env file..."
    read -r _
fi
ok ".env present"

# 7. Validate .env
step "Validating .env"
if ! ( cd "${EMMA_ROOT}" && uv run python -c "from config.settings import settings" 2>/dev/null ); then
    printf "${RED}.env validation failed.${RESET} Missing or invalid fields:\n"
    ( cd "${EMMA_ROOT}" && uv run python -c "from config.settings import settings" || true )
    fail "Fix .env and re-run this script."
fi
ok ".env validated"

# 7.5 Pre-request macOS permissions (must run BEFORE the LaunchAgent so dialogs
# appear under the install-time Terminal session, not in the background daemon).
step "Requesting macOS permissions"
if [ -t 0 ] && [ -t 1 ]; then
    ( cd "${EMMA_ROOT}" && uv run python -m emma.permissions bootstrap ) || \
        warn "Permission bootstrap exited non-zero (some dialogs may need manual approval)."
    ok "Permission bootstrap finished"
else
    warn "Non-interactive shell; skipping permission bootstrap. Run manually:"
    echo "    cd ${EMMA_ROOT} && uv run python -m emma.permissions bootstrap"
fi

# 7.6 Provision Keychain entries: master sentinel + .env credential migration
step "Provisioning Keychain"
if [ -t 0 ] && [ -t 1 ]; then
    ( cd "${EMMA_ROOT}" && uv run python -m emma.security bootstrap ) || \
        warn "Keychain bootstrap exited non-zero."
    ok "Keychain provisioned"
else
    warn "Non-interactive shell; skipping Keychain bootstrap. Run manually:"
    echo "    cd ${EMMA_ROOT} && uv run python -m emma.security bootstrap"
fi

# 8. Install plist with paths substituted
step "Installing LaunchAgent"
mkdir -p "${HOME_DIR}/Library/LaunchAgents" "${LOG_DIR}"
sed \
    -e "s|@EMMA_ROOT@|${EMMA_ROOT}|g" \
    -e "s|@VENV_PYTHON@|${VENV_PYTHON}|g" \
    -e "s|@HOME@|${HOME_DIR}|g" \
    "${PLIST_SRC}" > "${PLIST_DST}"
ok "Plist written to ${PLIST_DST}"

# 9. Bootstrap into launchd. Idempotent: bootout first if a stale copy is loaded.
step "Loading service into launchd"
launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DST}"
launchctl enable "${SERVICE_TARGET}" 2>/dev/null || true
launchctl kickstart -k "${SERVICE_TARGET}" 2>/dev/null || true
ok "Service loaded"

# 10. Confirm startup
step "Verifying Emma is running"
sleep 3
if launchctl print "${SERVICE_TARGET}" 2>/dev/null | grep -qE "state = (running|spawn scheduled)"; then
    ok "Emma is running. Say 'Hey Emma' to begin."
    printf "\nLogs live at:\n  ${LOG_DIR}/emma.log\n  ${LOG_DIR}/stderr.log\n\n"
else
    warn "Service loaded but state could not be confirmed."
    echo "Inspect with:"
    echo "  launchctl print ${SERVICE_TARGET}"
    echo "  tail -f ${LOG_DIR}/stderr.log"
    exit 1
fi
