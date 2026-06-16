#!/usr/bin/env bash
# Uninstall Emma (Prompt 29.0). Removes the service, app, and logs. Your memory
# (~/.emma, incl. memory.db) is PRESERVED by default — pass --wipe to delete it too.
#
#   ./uninstall_macos.sh           # remove app + service, keep ~/.emma
#   ./uninstall_macos.sh --wipe    # also delete ~/.emma (memory, tokens, profile)
set -euo pipefail

GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
step() { printf "${BLUE}==>${RESET} %s\n" "$*"; }
ok()   { printf "${GREEN}\xe2\x9c\x93${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }

WIPE=0
[[ "${1:-}" == "--wipe" ]] && WIPE=1

UID_NUM="$(id -u)"
SERVICE_TARGET="gui/${UID_NUM}/com.garcia.emma"
PLIST="${HOME}/Library/LaunchAgents/com.garcia.emma.plist"
APP_DIR="${HOME}/Library/Application Support/Emma"
LOG_DIR="${HOME}/Library/Logs/Emma"
EMMA_DIR="${HOME}/.emma"

step "Stopping Emma"
launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
ok "Stopped"

[[ -f "${PLIST}" ]] && { rm -f "${PLIST}"; ok "Removed LaunchAgent"; }
[[ -d "${APP_DIR}" ]] && { rm -rf "${APP_DIR}"; ok "Removed ${APP_DIR}"; }
[[ -d "${LOG_DIR}" ]] && { rm -rf "${LOG_DIR}"; ok "Removed logs"; }

if [[ "${WIPE}" -eq 1 ]]; then
    [[ -d "${EMMA_DIR}" ]] && { rm -rf "${EMMA_DIR}"; ok "Wiped ${EMMA_DIR} (memory deleted)"; }
else
    [[ -d "${EMMA_DIR}" ]] && warn "Kept ${EMMA_DIR} (your memory). Use --wipe to delete it."
fi

ok "Uninstall complete."
