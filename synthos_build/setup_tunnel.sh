#!/bin/bash
# setup_tunnel.sh — Cloudflare Tunnel for remote access
# No domain needed. Free. HTTPS out of the box.
#
# Usage:
#   bash setup_tunnel.sh          # start tunnel for portal (port 5001)
#   bash setup_tunnel.sh console  # start tunnel for console (port 5000)
#   bash setup_tunnel.sh both     # start both tunnels
#
# Gives you public URLs like:
#   https://random-words.trycloudflare.com
#
# URLs change every restart unless you create a free Cloudflare account
# and use a named tunnel. Add to todo: named tunnel with real domain later.

SERVICE=${1:-portal}

install_cloudflared() {
  if command -v cloudflared &>/dev/null; then
    echo "cloudflared already installed"
    return
  fi
  echo "Installing cloudflared..."
  if [[ "$(uname -m)" == "aarch64" ]]; then
    # Pi 5 (ARM64)
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
      -o /tmp/cloudflared
  elif [[ "$(uname -m)" == "armv7l" ]]; then
    # Pi 4 (ARM32)
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm \
      -o /tmp/cloudflared
  elif [[ "$(uname)" == "Darwin" ]]; then
    brew install cloudflared && return
  else
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      -o /tmp/cloudflared
  fi
  sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
  sudo chmod +x /usr/local/bin/cloudflared
  echo "✓ cloudflared installed"
}

start_tunnel() {
  local port=$1
  local name=$2
  local logfile=~/synthos/logs/tunnel_${name}.log

  echo "Starting $name tunnel on port $port..."
  nohup cloudflared tunnel --url http://localhost:$port \
    >> $logfile 2>&1 &

  # Wait for URL to appear in log
  echo "Waiting for tunnel URL..."
  for i in $(seq 1 20); do
    sleep 1
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' $logfile 2>/dev/null | tail -1)
    if [ -n "$URL" ]; then
      echo ""
      echo "  ✓ $name tunnel: $URL"
      # Open in browser if on Mac
      [[ "$(uname)" == "Darwin" ]] && open "$URL"
      break
    fi
    printf "."
  done
  if [ -z "$URL" ]; then
    echo ""
    echo "  · Tunnel starting — check: tail $logfile"
  fi
}

install_cloudflared

case $SERVICE in
  portal)
    # Make sure portal is running
    pgrep -f portal.py > /dev/null || (
      nohup python3 ~/synthos/portal.py >> ~/synthos/logs/portal.log 2>&1 &
      sleep 2
    )
    start_tunnel 5001 portal
    ;;
  console)
    pgrep -f synthos_monitor.py > /dev/null || (
      PORT=5000 nohup python3 ~/synthos/synthos_monitor.py >> ~/synthos/logs/monitor.log 2>&1 &
      sleep 2
    )
    start_tunnel 5000 console
    ;;
  both)
    pgrep -f portal.py > /dev/null || (
      nohup python3 ~/synthos/portal.py >> ~/synthos/logs/portal.log 2>&1 &
      sleep 2
    )
    PORT=5000 pgrep -f synthos_monitor.py > /dev/null || (
      PORT=5000 nohup python3 ~/synthos/synthos_monitor.py >> ~/synthos/logs/monitor.log 2>&1 &
      sleep 2
    )
    start_tunnel 5001 portal
    start_tunnel 5000 console
    ;;
  *)
    echo "Usage: bash setup_tunnel.sh [portal|console|both]"
    exit 1
    ;;
esac

echo ""
echo "URLs change each restart. For permanent URLs:"
echo "  1. Create free account at cloudflare.com"
echo "  2. Run: cloudflared tunnel login"
echo "  3. We'll set up named tunnels when you have a domain"
echo ""
echo "To stop tunnels: pkill cloudflared"
