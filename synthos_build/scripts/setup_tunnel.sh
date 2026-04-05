#!/usr/bin/env bash
# setup_tunnel.sh — Configure Cloudflare Tunnel for portal.synth-cloud.com
#
# Usage:
#   bash setup_tunnel.sh                     # interactive full setup
#   bash setup_tunnel.sh --status            # show tunnel and service status
#   bash setup_tunnel.sh --tunnel-name NAME  # specify tunnel name (skip prompt)
#
# Prerequisites:
#   - synth-cloud.com domain added to Cloudflare
#   - Tunnel already created in the Zero Trust dashboard
#     (https://one.dash.cloudflare.com → Networks → Tunnels)
#   - Portal running on localhost:5001
#
# What this script does:
#   1. Installs cloudflared if not present
#   2. Authenticates with Cloudflare (opens browser once)
#   3. Looks up your tunnel by name and extracts its ID
#   4. Writes ~/.cloudflared/config.yml pointing to port 5001
#   5. Routes portal.synth-cloud.com DNS to the tunnel
#   6. Installs cloudflared as a systemd service (auto-starts on boot)

set -euo pipefail

REAL_USER="${SUDO_USER:-$(whoami)}"
REAL_HOME=$(eval echo "~${REAL_USER}")
SYNTHOS_ROOT="${REAL_HOME}/synthos/synthos_build"
CF_DIR="${REAL_HOME}/.cloudflared"
HOSTNAME="portal.synth-cloud.com"
LOCAL_SERVICE="http://localhost:5001"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
hdr()  { echo -e "\n${CYAN}=== $* ===${NC}"; }
ask()  { echo -e "${YELLOW}  → $*${NC}"; }

TUNNEL_NAME=""
for arg in "$@"; do
  case $arg in
    --tunnel-name=*) TUNNEL_NAME="${arg#*=}" ;;
    --tunnel-name)   shift; TUNNEL_NAME="$1" ;;
  esac
done

# ── STATUS MODE ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
  hdr "CLOUDFLARE TUNNEL STATUS"
  if command -v cloudflared &>/dev/null; then
    ok "cloudflared installed: $(cloudflared --version 2>&1 | head -1)"
    echo ""
    echo "  Tunnels:"
    sudo -u "${REAL_USER}" cloudflared tunnel list 2>/dev/null || warn "Not authenticated yet"
    echo ""
    echo "  systemd service:"
    systemctl is-active cloudflared 2>/dev/null && ok "cloudflared service running" || warn "cloudflared service not running"
    systemctl is-enabled cloudflared 2>/dev/null && ok "cloudflared service enabled" || warn "cloudflared service not enabled"
  else
    warn "cloudflared not installed"
  fi
  echo ""
  echo "  Portal target: ${HOSTNAME} → ${LOCAL_SERVICE}"
  exit 0
fi

hdr "SYNTHOS CLOUDFLARE TUNNEL SETUP"
echo "  User:     ${REAL_USER}"
echo "  Home:     ${REAL_HOME}"
echo "  Hostname: ${HOSTNAME}"
echo "  Target:   ${LOCAL_SERVICE}"

# ── STEP 1: Install cloudflared ───────────────────────────────────────────────
hdr "STEP 1: Install cloudflared"
if command -v cloudflared &>/dev/null; then
  ok "cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
else
  echo "  Installing cloudflared..."
  ARCH=$(uname -m)
  case "${ARCH}" in
    aarch64|arm64) CF_ARCH="arm64" ;;
    armv7l)        CF_ARCH="arm"   ;;
    x86_64)        CF_ARCH="amd64" ;;
    *)             err "Unsupported architecture: ${ARCH}" ;;
  esac

  CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
  echo "  Downloading ${CF_URL}"
  curl -fsSL -o /usr/local/bin/cloudflared "${CF_URL}"
  chmod +x /usr/local/bin/cloudflared
  ok "cloudflared installed at /usr/local/bin/cloudflared"
fi

# ── STEP 2: Authenticate ──────────────────────────────────────────────────────
hdr "STEP 2: Authenticate with Cloudflare"
CERT_FILE="${CF_DIR}/cert.pem"
if [[ -f "${CERT_FILE}" ]]; then
  ok "Already authenticated (cert.pem found)"
else
  ask "A browser window will open to authenticate with Cloudflare."
  ask "Log in with the account that owns synth-cloud.com, then return here."
  echo ""
  sudo -u "${REAL_USER}" cloudflared tunnel login
  if [[ -f "${CERT_FILE}" ]]; then
    ok "Authentication successful"
  else
    err "Authentication failed — cert.pem not created. Check browser flow."
  fi
fi

# ── STEP 3: Find tunnel ────────────────────────────────────────────────────────
hdr "STEP 3: Find your tunnel"
echo ""
echo "  Your existing tunnels:"
sudo -u "${REAL_USER}" cloudflared tunnel list 2>/dev/null || true
echo ""

if [[ -z "${TUNNEL_NAME}" ]]; then
  ask "Enter your tunnel name (as shown above, e.g. 'synthos-portal'):"
  read -r TUNNEL_NAME
fi

if [[ -z "${TUNNEL_NAME}" ]]; then
  err "No tunnel name provided. Run: bash setup_tunnel.sh --tunnel-name YOUR_TUNNEL_NAME"
fi

# Extract tunnel ID from the list output
TUNNEL_ID=$(sudo -u "${REAL_USER}" cloudflared tunnel list 2>/dev/null \
  | grep -i "${TUNNEL_NAME}" \
  | awk '{print $1}' \
  | head -1)

if [[ -z "${TUNNEL_ID}" ]]; then
  err "Could not find tunnel '${TUNNEL_NAME}'. Check the name and try again."
fi
ok "Tunnel found: ${TUNNEL_NAME} (${TUNNEL_ID})"

# ── STEP 4: Write config ──────────────────────────────────────────────────────
hdr "STEP 4: Write cloudflared config"
mkdir -p "${CF_DIR}"
CONFIG_FILE="${CF_DIR}/config.yml"

cat > "${CF_DIR}/config.yml" <<EOF
# Synthos Cloudflare Tunnel — auto-generated by setup_tunnel.sh
tunnel: ${TUNNEL_ID}
credentials-file: ${CF_DIR}/${TUNNEL_ID}.json

ingress:
  - hostname: ${HOSTNAME}
    service: ${LOCAL_SERVICE}
    originRequest:
      connectTimeout: 30s
      noTLSVerify: false
  - service: http_status:404
EOF

chown "${REAL_USER}:${REAL_USER}" "${CF_DIR}/config.yml"
ok "Config written to ${CONFIG_FILE}"

# Also update the repo copy so it's tracked
REPO_CONFIG="${SYNTHOS_ROOT}/config/cloudflared/config.yml"
if [[ -f "${REPO_CONFIG}" ]]; then
  sed -i "s/TUNNEL_ID_HERE/${TUNNEL_ID}/g" "${REPO_CONFIG}"
  ok "Repo config updated: ${REPO_CONFIG}"
fi

# ── STEP 5: Route DNS ─────────────────────────────────────────────────────────
hdr "STEP 5: Route DNS"
echo "  Routing ${HOSTNAME} → tunnel ${TUNNEL_NAME}..."
if sudo -u "${REAL_USER}" cloudflared tunnel route dns "${TUNNEL_NAME}" "${HOSTNAME}" 2>/dev/null; then
  ok "DNS route created: ${HOSTNAME} → ${TUNNEL_NAME}"
else
  warn "DNS route may already exist — check Cloudflare dashboard if needed"
fi

# ── STEP 6: Install systemd service ──────────────────────────────────────────
hdr "STEP 6: Install systemd service"
if systemctl is-active cloudflared &>/dev/null; then
  warn "cloudflared service already running — restarting with new config"
  cloudflared service uninstall 2>/dev/null || true
fi

cloudflared --config "${CF_DIR}/config.yml" service install
systemctl enable cloudflared
systemctl start  cloudflared
sleep 2

if systemctl is-active cloudflared &>/dev/null; then
  ok "cloudflared service running"
else
  err "cloudflared service failed to start. Check: journalctl -u cloudflared -n 30"
fi

# ── DONE ─────────────────────────────────────────────────────────────────────
hdr "DONE"
echo ""
echo -e "  ${GREEN}Portal is now accessible at: https://${HOSTNAME}${NC}"
echo ""
echo "  Check status:     bash setup_tunnel.sh --status"
echo "  View logs:        journalctl -u cloudflared -f"
echo "  Test connection:  curl -I https://${HOSTNAME}/login"
echo ""
