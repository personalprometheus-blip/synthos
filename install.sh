#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Synthos Unified Installer v1.0
# Configures any Raspberry Pi as a retail, company, or monitor node.
# Run on a fresh Pi OS: git clone <repo> && cd synthos && ./install.sh
# Safe to rerun — preserves .env, databases, and customer data.
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/tmp/synthos_install_$(id -un).log"
TEAL='\033[0;36m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
RED='\033[0;31m'
DIM='\033[0;90m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${TEAL}[SYNTHOS]${NC} $*" | tee -a "$LOG_FILE"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${AMBER}  ⚠${NC} $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}  ✗${NC} $*" | tee -a "$LOG_FILE"; }
step() { echo -e "\n${BOLD}── $* ──${NC}" | tee -a "$LOG_FILE"; }

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: BANNER + NODE SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${TEAL}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${TEAL}║${NC}  ${BOLD}SYNTHOS INSTALLER${NC}                            ${TEAL}║${NC}"
echo -e "${TEAL}║${NC}  ${DIM}Unified setup for all node types${NC}              ${TEAL}║${NC}"
echo -e "${TEAL}╚═══════════════════════════════════════════════╝${NC}"
echo ""

# Parse flags
REPAIR=false
RESTORE_FILE=""
VERIFY_ONLY=false
NODE_TYPE_ARG=""
for arg in "$@"; do
    case "$arg" in
        --repair) REPAIR=true ;;
        --restore=*) RESTORE_FILE="${arg#*=}" ;;
        --verify) VERIFY_ONLY=true ;;
        --retail) NODE_TYPE_ARG="retail" ;;
        --company) NODE_TYPE_ARG="company" ;;
        --monitor) NODE_TYPE_ARG="monitor" ;;
    esac
done

# Check if running as root for system-level changes
if [ "$EUID" -ne 0 ] && ! $VERIFY_ONLY; then
    log "This installer needs sudo for system packages and systemd."
    log "Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

CURRENT_USER="${SUDO_USER:-$(whoami)}"
log "Running as: $CURRENT_USER (effective: root)"
log "Repo directory: $SCRIPT_DIR"

# Node selection
echo -e "${BOLD}Which node is this?${NC}"
echo ""
echo -e "  ${TEAL}1)${NC} retail    — Pi 5, trading stack + customer portal"
echo -e "  ${AMBER}2)${NC} company   — Pi 4B, admin portal + company agents"
echo -e "  ${DIM}3)${NC} monitor   — Pi 2W, heartbeat receiver"
echo ""
if [ -n "$NODE_TYPE_ARG" ]; then
    node_choice=""
    case "$NODE_TYPE_ARG" in
        retail)  node_choice=1 ;;
        company) node_choice=2 ;;
        monitor) node_choice=3 ;;
    esac
else
    read -p "Select [1-3]: " node_choice
fi

case "$node_choice" in
    1) NODE_TYPE="retail";  NODE_IP="10.0.0.11"; NODE_LABEL="Retail Node (Pi 5)" ;;
    2) NODE_TYPE="company"; NODE_IP="10.0.0.10"; NODE_LABEL="Company Node (Pi 4B)" ;;
    3) NODE_TYPE="monitor"; NODE_IP="10.0.0.12"; NODE_LABEL="Monitor Node (Pi 2W)" ;;
    *) err "Invalid selection"; exit 1 ;;
esac

log "Node type: ${BOLD}$NODE_TYPE${NC} ($NODE_LABEL)"
log "Target IP: $NODE_IP"

# Set home directory based on node type
case "$NODE_TYPE" in
    retail)  HOME_DIR="/home/$CURRENT_USER/synthos/synthos_build" ;;
    company) HOME_DIR="/home/$CURRENT_USER/synthos-company" ;;
    monitor) HOME_DIR="/home/$CURRENT_USER/synthos" ;;
esac

log "Home directory: $HOME_DIR"

# If --verify, skip straight to verification
if $VERIFY_ONLY; then
    ENV_FILE=""
    case "$NODE_TYPE" in
        retail)  ENV_FILE="$HOME_DIR/user/.env" ;;
        company) ENV_FILE="$HOME_DIR/company.env" ;;
        monitor) ENV_FILE="$HOME_DIR/.env" ;;
    esac
    # Jump to verification (function defined below gets sourced)
fi

if ! $VERIFY_ONLY; then
# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: SYSTEM PACKAGES
# ═══════════════════════════════════════════════════════════════════════════════

step "Installing system packages"

apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv sqlite3 curl git \
    python3-dev libffi-dev libssl-dev 2>&1 | tail -2
ok "System packages installed"

# Set timezone
timedatectl set-timezone America/New_York 2>/dev/null || true
ok "Timezone: America/New_York"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: PYTHON PACKAGES
# ═══════════════════════════════════════════════════════════════════════════════

step "Installing Python packages"

# Common packages
COMMON_PKGS="flask requests python-dotenv cryptography psutil itsdangerous feedparser"

case "$NODE_TYPE" in
    retail)
        PKGS="$COMMON_PKGS anthropic alpaca-trade-api"
        ;;
    company)
        PKGS="$COMMON_PKGS anthropic boto3"
        ;;
    monitor)
        PKGS="$COMMON_PKGS"
        ;;
esac

pip3 install --break-system-packages $PKGS 2>&1 | tail -3
ok "Python packages installed"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4: DIRECTORY STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════

step "Creating directories"

mkdir -p "$HOME_DIR"/{data,logs,config}

case "$NODE_TYPE" in
    retail)
        mkdir -p "$HOME_DIR"/{src,agents,user,backups/staging,.known_good}
        mkdir -p "$HOME_DIR"/data/customers/default
        mkdir -p "$HOME_DIR"/logs/logic_audits
        ;;
    company)
        mkdir -p "$HOME_DIR"/{agents,utils,reference,data/archives,login_server}
        ;;
    monitor)
        # Minimal structure
        ;;
esac

chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR"
ok "Directories created"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5: CODE DEPLOYMENT
# ═══════════════════════════════════════════════════════════════════════════════

step "Deploying code"

case "$NODE_TYPE" in
    retail)
        # Copy src/ and agents/ from repo
        if [ -d "$SCRIPT_DIR/synthos_build/src" ]; then
            cp -u "$SCRIPT_DIR/synthos_build/src/"*.py "$HOME_DIR/src/" 2>/dev/null || true
            cp -u "$SCRIPT_DIR/synthos_build/agents/"*.py "$HOME_DIR/agents/" 2>/dev/null || true
            ok "Retail code deployed (src/ + agents/)"
        else
            warn "Source directory not found at $SCRIPT_DIR/synthos_build/src — skip code deploy"
            warn "On a fresh install, ensure the repo has synthos_build/src/ and agents/"
        fi

        # Copy default template DB if not exists
        if [ -f "$SCRIPT_DIR/synthos_build/data/customers/default/signals.db" ] && \
           [ ! -f "$HOME_DIR/data/customers/default/signals.db" ]; then
            cp "$SCRIPT_DIR/synthos_build/data/customers/default/signals.db" \
               "$HOME_DIR/data/customers/default/signals.db"
            ok "Default customer template DB deployed"
        fi
        ;;
    company)
        if [ -d "$SCRIPT_DIR" ]; then
            # Company files live at repo root and agents/
            for f in company_server.py company_archivist.py company_auditor.py synthos_monitor.py; do
                [ -f "$SCRIPT_DIR/$f" ] && cp -u "$SCRIPT_DIR/$f" "$HOME_DIR/" 2>/dev/null || true
            done
            # Agents
            for f in company_sentinel.py company_vault.py company_fidget.py company_librarian.py \
                     company_scoop.py company_strongbox.py; do
                [ -f "$SCRIPT_DIR/agents/$f" ] && cp -u "$SCRIPT_DIR/agents/$f" "$HOME_DIR/agents/" 2>/dev/null || true
            done
            # Utils
            for f in db_helpers.py company_lock.py synthos_paths.py; do
                [ -f "$SCRIPT_DIR/utils/$f" ] && cp -u "$SCRIPT_DIR/utils/$f" "$HOME_DIR/utils/" 2>/dev/null || true
            done
            # Node heartbeat
            [ -f "$SCRIPT_DIR/node_heartbeat.py" ] && cp -u "$SCRIPT_DIR/node_heartbeat.py" "$HOME_DIR/" 2>/dev/null || true
            ok "Company code deployed"
        fi
        ;;
    monitor)
        if [ -f "$SCRIPT_DIR/synthos_monitor.py" ]; then
            cp -u "$SCRIPT_DIR/synthos_monitor.py" "$HOME_DIR/"
        fi
        if [ -f "$SCRIPT_DIR/node_heartbeat.py" ]; then
            cp -u "$SCRIPT_DIR/node_heartbeat.py" "$HOME_DIR/"
        fi
        ok "Monitor code deployed"
        ;;
esac

chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 6: SYSTEMD SERVICES
# ═══════════════════════════════════════════════════════════════════════════════

step "Writing systemd services"

case "$NODE_TYPE" in
    retail)
        cat > /etc/systemd/system/synthos-portal.service << SVCEOF
[Unit]
Description=Synthos Retail Portal (Flask — port 5001)
After=network.target
Wants=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR/src
ExecStart=/usr/bin/python3 retail_portal.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        cat > /etc/systemd/system/synthos-watchdog.service << SVCEOF
[Unit]
Description=Synthos Retail Watchdog (crash monitor)
After=synthos-portal.service
Wants=synthos-portal.service

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR/src
ExecStart=/usr/bin/python3 retail_watchdog.py
Restart=on-failure
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        systemctl daemon-reload
        systemctl enable synthos-portal synthos-watchdog
        ok "Retail services: synthos-portal, synthos-watchdog"
        ;;

    company)
        cat > /etc/systemd/system/synthos-login-server.service << SVCEOF
[Unit]
Description=Synthos Command Portal (monitor dashboard)
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR
ExecStart=/usr/bin/python3 synthos_monitor.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        cat > /etc/systemd/system/synthos-company-server.service << SVCEOF
[Unit]
Description=Synthos Company Admin Server
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR
ExecStart=/usr/bin/python3 company_server.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        cat > /etc/systemd/system/synthos-archivist.service << SVCEOF
[Unit]
Description=Synthos Data Archivist (nightly DB archiver)
After=synthos-company-server.service

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR
ExecStart=/usr/bin/python3 company_archivist.py
Restart=on-failure
RestartSec=60
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        cat > /etc/systemd/system/synthos-auditor.service << SVCEOF
[Unit]
Description=Synthos Operations Auditor (log scanner + morning reports)
After=synthos-company-server.service

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR
ExecStart=/usr/bin/python3 company_auditor.py --daemon
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        systemctl daemon-reload
        systemctl enable synthos-login-server synthos-company-server synthos-archivist synthos-auditor
        ok "Company services: login-server, company-server, archivist, auditor"
        ;;

    monitor)
        cat > /etc/systemd/system/synthos-monitor.service << SVCEOF
[Unit]
Description=Synthos Monitor Node
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME_DIR
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/python3 synthos_monitor.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

        systemctl daemon-reload
        systemctl enable synthos-monitor
        ok "Monitor service: synthos-monitor"
        ;;
esac

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 7: CRON ENTRIES
# ═══════════════════════════════════════════════════════════════════════════════

step "Registering cron entries"

CRON_TMP=$(mktemp)
crontab -u "$CURRENT_USER" -l 2>/dev/null > "$CRON_TMP" || true

# Only add entries if they don't already exist
add_cron() {
    local entry="$1"
    if ! grep -qF "$entry" "$CRON_TMP" 2>/dev/null; then
        echo "$entry" >> "$CRON_TMP"
    fi
}

# Clear any existing synthos entries and rewrite clean
grep -v 'synthos\|retail_\|company_\|node_heartbeat\|price_poller' "$CRON_TMP" > "${CRON_TMP}.clean" 2>/dev/null || true
mv "${CRON_TMP}.clean" "$CRON_TMP"

echo "# SYNTHOS $(echo $NODE_TYPE | tr '[:lower:]' '[:upper:]') NODE — installed $(date +%Y-%m-%d)" >> "$CRON_TMP"
echo "# All times America/New_York" >> "$CRON_TMP"

case "$NODE_TYPE" in
    retail)
        cat >> "$CRON_TMP" << CRONEOF
# Boot services
@reboot sleep 60 && /usr/bin/python3 $HOME_DIR/src/retail_boot_sequence.py >> $HOME_DIR/logs/boot.log 2>&1
@reboot sleep 90 && /usr/bin/python3 $HOME_DIR/src/retail_watchdog.py &
@reboot sleep 90 && /usr/bin/python3 $HOME_DIR/src/retail_portal.py &
# News — hourly during market + overnight
5 9-15 * * 1-5    /usr/bin/python3 $HOME_DIR/src/retail_scheduler.py --session news >> $HOME_DIR/logs/scheduler.log 2>&1
5 0-8,16-23 * * 1-5 /usr/bin/python3 $HOME_DIR/src/retail_scheduler.py --session overnight >> $HOME_DIR/logs/scheduler.log 2>&1
# Trade — 20 past every hour weekdays
20 * * * 1-5      /usr/bin/python3 $HOME_DIR/src/retail_scheduler.py --session trade >> $HOME_DIR/logs/scheduler.log 2>&1
21 * * * 1-5      /usr/bin/python3 $HOME_DIR/src/retail_heartbeat.py --session trade >> $HOME_DIR/logs/heartbeat.log 2>&1
# Sentiment — every 30min market hours
0,30 10-15 * * 1-5  /usr/bin/python3 $HOME_DIR/src/retail_scheduler.py --session sentiment >> $HOME_DIR/logs/scheduler.log 2>&1
# Price poller — every minute extended market hours
* 8-17 * * 1-5    /usr/bin/python3 $HOME_DIR/agents/retail_price_poller.py >> $HOME_DIR/logs/price_poller.log 2>&1
# Node heartbeat — every minute
* * * * * /usr/bin/python3 $HOME_DIR/src/node_heartbeat.py $HOME_DIR/user/.env >> $HOME_DIR/logs/heartbeat.log 2>&1
# Backup — nightly 1:30am
30 1 * * * /usr/bin/python3 $HOME_DIR/src/retail_backup.py >> $HOME_DIR/logs/backup.log 2>&1
# Sunday prep session
0 20 * * 0 /usr/bin/python3 $HOME_DIR/src/retail_scheduler.py --session prep >> $HOME_DIR/logs/scheduler.log 2>&1
# Saturday maintenance
55 3 * * 6 /usr/bin/python3 $HOME_DIR/src/retail_shutdown.py
0 4 * * 6 sudo reboot
CRONEOF
        ;;
    company)
        cat >> "$CRON_TMP" << CRONEOF
# Agents
*/15 9-16 * * 1-5  /usr/bin/python3 $HOME_DIR/agents/company_sentinel.py >> $HOME_DIR/logs/sentinel.log 2>&1
0 * * * *          /usr/bin/python3 $HOME_DIR/agents/company_vault.py >> $HOME_DIR/logs/vault.log 2>&1
0 8 * * *          /usr/bin/python3 $HOME_DIR/agents/company_fidget.py >> $HOME_DIR/logs/fidget.log 2>&1
0 9 * * 0          /usr/bin/python3 $HOME_DIR/agents/company_librarian.py >> $HOME_DIR/logs/librarian.log 2>&1
# Strongbox — nightly encrypt + R2 upload
0 2 * * *          /usr/bin/python3 $HOME_DIR/agents/company_strongbox.py >> $HOME_DIR/logs/strongbox.log 2>&1
# Node heartbeat
*/5 * * * * /usr/bin/python3 $HOME_DIR/node_heartbeat.py $HOME_DIR/company.env >> $HOME_DIR/logs/heartbeat.log 2>&1
# Saturday maintenance
0 4 * * 6 sudo reboot
CRONEOF
        ;;
    monitor)
        cat >> "$CRON_TMP" << CRONEOF
# Node heartbeat — every minute
* * * * * /usr/bin/python3 $HOME_DIR/node_heartbeat.py $HOME_DIR/.env >> $HOME_DIR/logs/heartbeat.log 2>&1
CRONEOF
        ;;
esac

crontab -u "$CURRENT_USER" "$CRON_TMP"
rm -f "$CRON_TMP"
ok "Cron entries registered"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 8: ENVIRONMENT FILE
# ═══════════════════════════════════════════════════════════════════════════════

step "Configuring environment"

gen_key() { python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"; }
gen_token() { python3 -c "import secrets; print(secrets.token_hex(32))"; }

case "$NODE_TYPE" in
    retail)
        ENV_FILE="$HOME_DIR/user/.env"
        if [ ! -f "$ENV_FILE" ]; then
            ENCRYPTION_KEY=$(gen_key)
            read -p "Admin email: " ADMIN_EMAIL
            read -sp "Admin password: " ADMIN_PASSWORD; echo
            read -p "Monitor URL [http://10.0.0.10:5050]: " MONITOR_URL
            MONITOR_URL=${MONITOR_URL:-http://10.0.0.10:5050}
            MONITOR_TOKEN=$(gen_token)

            cat > "$ENV_FILE" << ENVEOF
# ── SYNTHOS RETAIL NODE ──
ENCRYPTION_KEY=$ENCRYPTION_KEY
PI_ID=synthos-pi-retail
PORTAL_DOMAIN=portal.synth-cloud.com

# ── ADMIN ACCOUNT ──
ADMIN_NAME=Admin
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD

# ── OWNER CUSTOMER ACCOUNT ──
OWNER_CUSTOMER_ID=
OWNER_EMAIL=$ADMIN_EMAIL
OWNER_PRICING_TIER=standard

# ── TRADING DEFAULTS ──
STARTING_CAPITAL=1000
OPERATING_MODE=SUPERVISED
TRADING_MODE=PAPER

# ── ALPACA (add after signup) ──
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# ── MONITORING ──
MONITOR_URL=$MONITOR_URL
MONITOR_TOKEN=$MONITOR_TOKEN

# ── EMAIL (optional) ──
RESEND_API_KEY=
ALERT_FROM=alerts@synth-cloud.com
ENVEOF
            chmod 600 "$ENV_FILE"
            ok "Retail .env created (ENCRYPTION_KEY auto-generated)"
            warn "Save this encryption key — losing it means losing all encrypted customer data"
        else
            ok "Retail .env exists — preserved"
        fi
        ;;

    company)
        ENV_FILE="$HOME_DIR/company.env"
        if [ ! -f "$ENV_FILE" ]; then
            SECRET_TOKEN=$(gen_token)
            BACKUP_KEY=$(gen_key)
            read -p "Admin email: " ADMIN_EMAIL
            read -sp "Admin password: " ADMIN_PASSWORD; echo
            read -p "Operator email (for alerts): " OPERATOR_EMAIL

            cat > "$ENV_FILE" << ENVEOF
# ── SYNTHOS COMPANY NODE ──
COMPANY_MODE=true
SECRET_TOKEN=$SECRET_TOKEN
MONITOR_TOKEN=$SECRET_TOKEN

# ── ADMIN ──
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD
OPERATOR_EMAIL=$OPERATOR_EMAIL

# ── SERVICES ──
RETAIL_PORTAL_URL=http://10.0.0.11:5001
LOGIN_SERVER_PORT=5050

# ── EMAIL ──
RESEND_API_KEY=
ALERT_FROM=Synth_Alerts@synth-cloud.com

# ── R2 BACKUP (configure after Cloudflare setup) ──
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=synthos-backups
BACKUP_ENCRYPTION_KEY=$BACKUP_KEY

# ── MARKET ──
MARKET_TIMEZONE=US/Eastern
ENVEOF
            chmod 600 "$ENV_FILE"
            ok "Company .env created (SECRET_TOKEN + BACKUP_KEY auto-generated)"
            echo ""
            warn "BACKUP ENCRYPTION KEY: $BACKUP_KEY"
            warn "Write this down and store it safely outside the system."
            warn "Without it, R2 backups cannot be decrypted."
            echo ""
        else
            ok "Company .env exists — preserved"
        fi
        ;;

    monitor)
        ENV_FILE="$HOME_DIR/.env"
        if [ ! -f "$ENV_FILE" ]; then
            read -p "Monitor token (must match company SECRET_TOKEN): " MONITOR_TOKEN

            cat > "$ENV_FILE" << ENVEOF
# ── SYNTHOS MONITOR NODE ──
PORT=5000
PI_ID=pi2w-monitor
PI_LABEL=pi2w Monitor Node
MONITOR_URL=http://10.0.0.10:5050
MONITOR_TOKEN=$MONITOR_TOKEN
SECRET_TOKEN=synthos-default-token
ENVEOF
            chmod 600 "$ENV_FILE"
            ok "Monitor .env created"
        else
            ok "Monitor .env exists — preserved"
        fi
        ;;
esac

chown "$CURRENT_USER:$CURRENT_USER" "$ENV_FILE"

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 9: NETWORK CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

step "Configuring network"

DHCPCD_CONF="/etc/dhcpcd.conf"
if grep -q "static ip_address=$NODE_IP" "$DHCPCD_CONF" 2>/dev/null; then
    ok "Static IP already configured: $NODE_IP"
else
    read -p "Set static IP to $NODE_IP? [Y/n]: " set_ip
    if [ "${set_ip:-Y}" != "n" ] && [ "${set_ip:-Y}" != "N" ]; then
        cat >> "$DHCPCD_CONF" << NETEOF

# Synthos static IP
interface eth0
static ip_address=$NODE_IP/24
static routers=10.0.0.1
static domain_name_servers=8.8.8.8 1.1.1.1
NETEOF
        ok "Static IP configured: $NODE_IP"
        warn "Reboot required for network changes"
    else
        warn "Static IP skipped"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 10: DATABASE BOOTSTRAP
# ═══════════════════════════════════════════════════════════════════════════════

step "Bootstrapping database"

case "$NODE_TYPE" in
    retail)
        if [ ! -f "$HOME_DIR/data/auth.db" ]; then
            cd "$HOME_DIR/src"
            sudo -u "$CURRENT_USER" python3 -c "
import sys; sys.path.insert(0, '.')
from auth import _auth_conn
print('auth.db schema bootstrapped')
" 2>&1 || warn "auth.db bootstrap needs portal first run"
            ok "Database initialized"
        else
            ok "auth.db exists — preserved"
        fi
        ;;
    company)
        cd "$HOME_DIR"
        sudo -u "$CURRENT_USER" python3 -c "
import sys; sys.path.insert(0, 'utils')
from db_helpers import bootstrap_schema
bootstrap_schema()
print('company.db schema bootstrapped')
" 2>&1 || warn "company.db bootstrap needs first run"
        ok "Database initialized"
        ;;
    monitor)
        ok "No database needed for monitor node"
        ;;
esac

fi  # end of if ! $VERIFY_ONLY

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 11: VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

step "Verification"

echo ""
PASS=0
FAIL=0

check() {
    local desc="$1" cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        ok "$desc"
        PASS=$((PASS+1))
    else
        err "$desc"
        FAIL=$((FAIL+1))
    fi
}

# Common checks
check "Python 3 available" "python3 --version"
check "pip3 available" "pip3 --version"
check "Timezone is ET" "timedatectl | grep -q 'America/New_York'"
check "Home directory exists" "[ -d '$HOME_DIR' ]"
check "Environment file exists" "[ -f '$ENV_FILE' ]"
check "Cron registered" "crontab -u $CURRENT_USER -l 2>/dev/null | grep -q synthos"

# Node-specific checks
case "$NODE_TYPE" in
    retail)
        check "src/ directory" "[ -d '$HOME_DIR/src' ]"
        check "agents/ directory" "[ -d '$HOME_DIR/agents' ]"
        check "retail_portal.py exists" "[ -f '$HOME_DIR/src/retail_portal.py' ]"
        check "retail_scheduler.py exists" "[ -f '$HOME_DIR/src/retail_scheduler.py' ]"
        check "Portal service enabled" "systemctl is-enabled synthos-portal"
        check "Watchdog service enabled" "systemctl is-enabled synthos-watchdog"
        check "flask installed" "python3 -c 'import flask'"
        check "cryptography installed" "python3 -c 'from cryptography.fernet import Fernet'"
        ;;
    company)
        check "synthos_monitor.py exists" "[ -f '$HOME_DIR/synthos_monitor.py' ]"
        check "company_auditor.py exists" "[ -f '$HOME_DIR/company_auditor.py' ]"
        check "agents/ directory" "[ -d '$HOME_DIR/agents' ]"
        check "Login server enabled" "systemctl is-enabled synthos-login-server"
        check "Company server enabled" "systemctl is-enabled synthos-company-server"
        check "flask installed" "python3 -c 'import flask'"
        check "boto3 installed" "python3 -c 'import boto3'"
        ;;
    monitor)
        check "synthos_monitor.py exists" "[ -f '$HOME_DIR/synthos_monitor.py' ]"
        check "Monitor service enabled" "systemctl is-enabled synthos-monitor"
        ;;
esac

echo ""
echo -e "${TEAL}═══════════════════════════════════════════════${NC}"
echo -e "  ${BOLD}Installation Complete${NC}"
echo -e "  Node: ${BOLD}$NODE_LABEL${NC}"
echo -e "  IP:   ${BOLD}$NODE_IP${NC}"
echo -e "  Home: ${DIM}$HOME_DIR${NC}"
echo -e "  Checks: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo -e "${TEAL}═══════════════════════════════════════════════${NC}"
echo ""

if [ $FAIL -gt 0 ]; then
    warn "Some checks failed. Run './install.sh --verify' to recheck."
    warn "Run './install.sh --repair' to reinstall packages + services."
fi

case "$NODE_TYPE" in
    retail)
        log "Start services: sudo systemctl start synthos-portal synthos-watchdog"
        log "Portal URL: http://$NODE_IP:5001"
        ;;
    company)
        log "Start services: sudo systemctl start synthos-login-server synthos-company-server synthos-archivist synthos-auditor"
        log "Monitor URL: http://$NODE_IP:5050"
        ;;
    monitor)
        log "Start service: sudo systemctl start synthos-monitor"
        log "Monitor URL: http://$NODE_IP:5000"
        ;;
esac

echo ""
log "Log saved to: $LOG_FILE"
