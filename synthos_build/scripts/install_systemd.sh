#!/usr/bin/env bash
# install_systemd.sh — Install Synthos systemd services and timers
# Run as root (sudo) on the Pi 5.
#
# Usage:
#   sudo bash install_systemd.sh
#   sudo bash install_systemd.sh --uninstall
#   sudo bash install_systemd.sh --status
#
# What it does:
#   1. Detects the real user/home dir (even under sudo)
#   2. Patches all service/timer files with the correct paths
#   3. Copies them to /etc/systemd/system/
#   4. Enables and starts portal + watchdog services
#   5. Enables all timers (they fire on schedule)

set -euo pipefail

# ── DETECT REAL USER ──────────────────────────────────────────────────────────
REAL_USER="${SUDO_USER:-$(whoami)}"
REAL_HOME=$(eval echo "~${REAL_USER}")
SYNTHOS_ROOT="${REAL_HOME}/synthos/synthos_build"
VENV_PYTHON="${REAL_HOME}/synthos/venv/bin/python3"
SYSTEMD_DIR="/etc/systemd/system"
CONFIG_DIR="${SYNTHOS_ROOT}/config/systemd"

# ── COLOURS ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; }
hdr()  { echo -e "\n${GREEN}=== $* ===${NC}"; }

# ── SERVICE / TIMER NAMES ─────────────────────────────────────────────────────
SERVICES=(
    synthos-portal.service
    synthos-watchdog.service
    "synthos-scheduler@.service"
)

TIMERS=(
    synthos-session-open.timer
    synthos-session-midday.timer
    synthos-session-close.timer
    synthos-news.timer
    synthos-sentiment.timer
)

# ── GUARD ─────────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Run with sudo: sudo bash install_systemd.sh"
    exit 1
fi

if [[ ! -d "${SYNTHOS_ROOT}" ]]; then
    err "Synthos root not found: ${SYNTHOS_ROOT}"
    err "Check that REAL_USER=${REAL_USER} and the repo is at ~/synthos/synthos_build"
    exit 1
fi

if [[ ! -f "${VENV_PYTHON}" ]]; then
    warn "venv Python not found at ${VENV_PYTHON}"
    warn "Falling back to system python3 — run install_retail.py first for a proper venv"
    VENV_PYTHON=$(which python3)
fi

# ── STATUS MODE ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
    hdr "SERVICE STATUS"
    for svc in synthos-portal.service synthos-watchdog.service; do
        echo -n "  ${svc}: "
        systemctl is-active "${svc}" 2>/dev/null || true
    done
    hdr "TIMER STATUS"
    systemctl list-timers 'synthos-*' --no-pager 2>/dev/null || warn "No timers found"
    hdr "RECENT LOGS (portal)"
    journalctl -u synthos-portal.service -n 20 --no-pager 2>/dev/null || true
    exit 0
fi

# ── UNINSTALL MODE ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    hdr "UNINSTALLING SYNTHOS SYSTEMD UNITS"
    for timer in "${TIMERS[@]}"; do
        systemctl disable --now "${timer}" 2>/dev/null && ok "Disabled ${timer}" || true
        rm -f "${SYSTEMD_DIR}/${timer}"
    done
    for svc in "${SERVICES[@]}"; do
        systemctl disable --now "${svc}" 2>/dev/null && ok "Disabled ${svc}" || true
        rm -f "${SYSTEMD_DIR}/${svc}"
    done
    systemctl daemon-reload
    ok "Uninstall complete"
    exit 0
fi

# ── INSTALL ───────────────────────────────────────────────────────────────────
hdr "SYNTHOS SYSTEMD INSTALL"
echo "  User:       ${REAL_USER}"
echo "  Home:       ${REAL_HOME}"
echo "  Root:       ${SYNTHOS_ROOT}"
echo "  Python:     ${VENV_PYTHON}"
echo "  Config dir: ${CONFIG_DIR}"

if [[ ! -d "${CONFIG_DIR}" ]]; then
    err "systemd config dir not found: ${CONFIG_DIR}"
    exit 1
fi

hdr "PATCHING AND COPYING SERVICE FILES"
for svc in "${SERVICES[@]}"; do
    src="${CONFIG_DIR}/${svc}"
    dst="${SYSTEMD_DIR}/${svc}"
    if [[ ! -f "${src}" ]]; then
        warn "Skipping ${svc} — source file not found"
        continue
    fi
    # Patch placeholders for real user/paths
    sed \
        -e "s|/home/pi|${REAL_HOME}|g" \
        -e "s|User=pi|User=${REAL_USER}|g" \
        -e "s|Group=pi|Group=${REAL_USER}|g" \
        -e "s|/home/pi/synthos/venv/bin/python3|${VENV_PYTHON}|g" \
        "${src}" > "${dst}"
    ok "Installed ${svc}"
done

hdr "PATCHING AND COPYING TIMER FILES"
for timer in "${TIMERS[@]}"; do
    src="${CONFIG_DIR}/${timer}"
    dst="${SYSTEMD_DIR}/${timer}"
    if [[ ! -f "${src}" ]]; then
        warn "Skipping ${timer} — source file not found"
        continue
    fi
    cp "${src}" "${dst}"
    ok "Installed ${timer}"
done

hdr "RELOADING SYSTEMD"
systemctl daemon-reload
ok "daemon-reload complete"

hdr "ENABLING AND STARTING SERVICES"
for svc in synthos-portal.service synthos-watchdog.service; do
    systemctl enable "${svc}"
    systemctl restart "${svc}"
    sleep 2
    status=$(systemctl is-active "${svc}" 2>/dev/null)
    if [[ "${status}" == "active" ]]; then
        ok "${svc} — running"
    else
        err "${svc} — failed to start (check: journalctl -u ${svc} -n 30)"
    fi
done

hdr "ENABLING TIMERS"
for timer in "${TIMERS[@]}"; do
    systemctl enable "${timer}"
    systemctl start  "${timer}"
    ok "Enabled ${timer}"
done

hdr "TIMER SCHEDULE"
systemctl list-timers 'synthos-*' --no-pager 2>/dev/null || warn "Could not list timers"

hdr "DONE"
echo "  Services running:  synthos-portal, synthos-watchdog"
echo "  Timers active:     $(echo "${TIMERS[@]}" | wc -w) timers"
echo ""
echo "  Check status:      sudo bash install_systemd.sh --status"
echo "  View portal logs:  journalctl -u synthos-portal.service -f"
echo "  View watchdog:     journalctl -u synthos-watchdog.service -f"
echo "  View scheduler:    tail -f ${SYNTHOS_ROOT}/logs/scheduler.log"
echo "  Test a session:    sudo -u ${REAL_USER} python3 ${SYNTHOS_ROOT}/src/retail_scheduler.py --session open --dry-run"
echo ""
