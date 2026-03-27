#!/bin/bash
# portal — start customer portal and open browser
cd ~/synthos 2>/dev/null || { echo "Error: ~/synthos not found"; exit 1; }

# Kill any existing portal
pkill -f portal.py 2>/dev/null
sleep 0.5

# Start portal in background
nohup python3 ~/synthos/portal.py >> ~/synthos/logs/portal.log 2>&1 &
sleep 1.5

# Open browser
if command -v open &>/dev/null; then
  open http://localhost:5001
elif command -v xdg-open &>/dev/null; then
  xdg-open http://10.0.0.224:5001
fi

echo "✓ Portal started → http://localhost:5001"
