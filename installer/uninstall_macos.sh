#!/usr/bin/env bash
# Uninstall Emma. Asks before deleting user data.
set -euo pipefail

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
step()    { printf "${BLUE}==>${RESET} %s\n" "$*"; }
ok()      { printf "${GREEN}\xe2\x9c\x93${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}!${RESET} %s\n" "$*"; }

confirm() {
    printf "%s [y/N] " "$1"
    read -r ans
    [[ "${ans:-}" =~ ^[yY] ]]
}

UID_NUM="$(id -u)"
SERVICE_TARGET="gui/${UID_NUM}/com.garcia.emma"
PLIST="${HOME}/Library/LaunchAgents/com.garcia.emma.plist"
EMMA_DIR="${HOME}/.emma"
PROFILE_DIR="${HOME}/.emma/playwright-profile"
LOG_DIR="${HOME}/Library/Logs/Emma"

step "Stopping Emma"
launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
launchctl enable "${SERVICE_TARGET}" 2>/dev/null || true  # clear any disable flag
ok "Stopped"

if [[ -f "${PLIST}" ]]; then
    step "Removing LaunchAgent plist"
    rm -f "${PLIST}"
    ok "Removed ${PLIST}"
fi

if [[ -d "${EMMA_DIR}" ]]; then
    if confirm "Delete ${EMMA_DIR}/ (memory, Spotify token, Playwright profile)?"; then
        rm -rf "${EMMA_DIR}"
        ok "Removed ${EMMA_DIR}"
    elif [[ -d "${PROFILE_DIR}" ]]; then
        if confirm "Delete just the Playwright profile (${PROFILE_DIR})?"; then
            rm -rf "${PROFILE_DIR}"
            ok "Removed Playwright profile"
        else
            warn "Kept Playwright profile"
        fi
    else
        warn "Kept ${EMMA_DIR}"
    fi
fi

if [[ -d "${LOG_DIR}" ]]; then
    if confirm "Delete ${LOG_DIR}/ (logs and crash reports)?"; then
        rm -rf "${LOG_DIR}"
        ok "Removed ${LOG_DIR}"
    else
        warn "Kept ${LOG_DIR}"
    fi
fi

ok "Uninstall complete."
