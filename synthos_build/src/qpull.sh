#!/bin/bash
# qpull — pull latest from GitHub and restart portal
# Usage: qpull
#        qpull --no-restart

cd ~/synthos 2>/dev/null || { echo "Error: ~/synthos not found"; exit 1; }

echo "Pulling from GitHub..."
if ! git pull; then
  echo "✗ Pull failed"
  exit 1
fi

if [ "$1" = "--no-restart" ]; then
  echo "✓ Files updated — skipping restart"
  exit 0
fi

if pgrep -f portal.py > /dev/null; then
  echo "Restarting portal..."
  pkill -f portal.py
  sleep 1
  nohup python3 ~/synthos/portal.py >> ~/synthos/logs/portal.log 2>&1 &
  echo "✓ Portal restarted"
fi

echo ""
echo "✓ Synthos updated"
