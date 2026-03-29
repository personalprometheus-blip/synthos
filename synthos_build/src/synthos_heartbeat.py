"""
synthos_heartbeat.py
-------------------
Drop this into your Synthos Pi. Call send_heartbeat() from your main loop.

Add to .env:
    MONITOR_URL=http://your-monitor-pi-ip:5000
    MONITOR_TOKEN=same_token_as_monitor_server
    PI_ID=synthos-pi-1   # unique name for this Pi
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

MONITOR_URL   = os.getenv("MONITOR_URL")
MONITOR_TOKEN = os.getenv("MONITOR_TOKEN", "changeme")
PI_ID         = os.getenv("PI_ID", "synthos-pi")

def send_heartbeat(portfolio_value=0.0, agents=None):
    """
    Call this from your main loop.
    agents = { "trend": "active", "momentum": "idle", "mean_reversion": "active" }
    """
    if not MONITOR_URL:
        return

    try:
        requests.post(
            f"{MONITOR_URL}/heartbeat",
            json={
                "pi_id":     PI_ID,
                "portfolio": portfolio_value,
                "agents":    agents or {},
            },
            headers={"X-Token": MONITOR_TOKEN},
            timeout=5,
        )
    except Exception as e:
        print(f"[Heartbeat] Failed to reach monitor: {e}")
