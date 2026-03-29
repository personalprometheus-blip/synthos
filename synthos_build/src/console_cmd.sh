#!/bin/bash
# console — start command console and open browser
cd ~/synthos 2>/dev/null || { echo "Error: ~/synthos not found"; exit 1; }

pkill -f synthos_monitor.py 2>/dev/null
sleep 0.5

PORT=5005 nohup python3 ~/synthos/synthos_monitor.py >> ~/synthos/logs/monitor.log 2>&1 &
sleep 1.5

open http://localhost:5005/console 2>/dev/null || xdg-open http://localhost:5005/console 2>/dev/null

echo "✓ Console started → http://localhost:5005/console"
