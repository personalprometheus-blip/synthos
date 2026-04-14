"""
heartbeat.py — Synthos Monitor Heartbeat Writer
Synthos · v1.1

Called at the end of every agent session to POST live system state
to the Synthos Monitor server. Falls back gracefully if the monitor
is unreachable — never blocks or crashes the calling agent.

Usage (from any agent):
    from retail_heartbeat import write_heartbeat
    write_heartbeat(agent_name="agent1_trader", status="OK")

.env keys used:
    MONITOR_URL    — http://your-monitor-pi-ip:5000
    MONITOR_TOKEN  — shared secret (must match SECRET_TOKEN on monitor)
    PI_ID          — unique Pi identifier, e.g. "synthos-pi-01"
    PI_LABEL       — display name shown in console, e.g. "John's Pi" (optional)
    PI_EMAIL       — customer email shown in monitor console (optional)
    OPERATING_MODE — SUPERVISED or AUTONOMOUS
    TRADING_MODE   — PAPER or LIVE

Monitor endpoint: POST /heartbeat
    Header: X-Token: <MONITOR_TOKEN>
    Body: JSON payload (see _build_payload)
"""

import os
import logging
from dotenv import load_dotenv

def _system_metrics() -> dict:
    """
    Collect CPU, RAM, load average, CPU temperature, and disk usage.
    Returns a dict with float values or None for each metric.
    Requires psutil (install: pip install psutil). Fails silently if unavailable.
    """
    metrics = {
        'cpu_percent':  None,
        'ram_percent':  None,
        'load_avg':     None,
        'cpu_temp':     None,
        'disk_percent': None,
    }
    try:
        import psutil
        metrics['cpu_percent']  = round(psutil.cpu_percent(interval=0.5), 1)
        metrics['ram_percent']  = round(psutil.virtual_memory().percent, 1)
        metrics['disk_percent'] = round(psutil.disk_usage('/').percent, 1)
        load = os.getloadavg()
        metrics['load_avg'] = [round(load[0], 2), round(load[1], 2), round(load[2], 2)]
    except Exception:
        pass
    try:
        # Raspberry Pi thermal zone — /sys/class/thermal/thermal_zone0/temp (millidegrees C)
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            metrics['cpu_temp'] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass
    return metrics

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

log = logging.getLogger('heartbeat')

_PROJECT_DIR      = os.path.dirname(os.path.abspath(__file__))
_KILL_SWITCH_FILE = os.path.join(_PROJECT_DIR, '.kill_switch')


def _kill_switch_active() -> bool:
    """Check kill switch state without importing any other module."""
    return os.path.exists(_KILL_SWITCH_FILE)


def _build_payload(agent_name: str, status: str) -> dict:
    """
    Assemble the heartbeat payload.
    Reads live portfolio/position data from DB when available.
    Falls back to zeros if DB is unavailable — never raises.
    """
    pi_id          = os.environ.get('PI_ID', 'synthos-pi')
    pi_label       = os.environ.get('PI_LABEL', pi_id)
    pi_email       = os.environ.get('PI_EMAIL', '')
    operating_mode = os.environ.get('OPERATING_MODE', 'SUPERVISED').upper()
    trading_mode   = os.environ.get('TRADING_MODE', 'PAPER').upper()

    # Collect system metrics first (non-blocking)
    sys_metrics = _system_metrics()

    payload = {
        'pi_id':             pi_id,
        'label':             pi_label,
        'email':             pi_email,
        'operating_mode':    operating_mode,
        'trading_mode':      trading_mode,
        'agents':            {agent_name: status},
        'kill_switch':       _kill_switch_active(),
        'portfolio_value':   0.0,
        'cash':              0.0,
        'realized_gains':    0.0,
        'open_positions':    0,
        'pending_approvals': 0,
        'urgent_flags':      0,
        'positions':         [],
        # System health metrics
        'cpu_percent':       sys_metrics['cpu_percent'],
        'ram_percent':       sys_metrics['ram_percent'],
        'load_avg':          sys_metrics['load_avg'],
        'cpu_temp':          sys_metrics['cpu_temp'],
        'disk_percent':      sys_metrics['disk_percent'],
    }

    try:
        from retail_database import get_db
        db        = get_db()
        portfolio = db.get_portfolio()
        positions = db.get_open_positions()
        total     = round(
            portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions),
            2
        )
        pending = len(db.get_pending_approvals(status_filter=['PENDING_APPROVAL']))
        flags   = db.get_urgent_flags()

        payload['portfolio_value']   = total
        payload['cash']              = round(portfolio['cash'], 2)
        payload['realized_gains']    = round(portfolio.get('realized_gains', 0.0), 2)
        payload['open_positions']    = len(positions)
        payload['pending_approvals'] = pending
        payload['urgent_flags']      = len(flags)
        payload['positions']         = [
            {
                'ticker':      p['ticker'],
                'shares':      p['shares'],
                'entry_price': p['entry_price'],
            }
            for p in positions
        ]
    except Exception as e:
        log.debug(f"DB read skipped in heartbeat (non-fatal): {e}")

    return payload


def write_heartbeat(agent_name: str = "unknown", status: str = "OK") -> bool:
    """
    POST current system state to the Synthos Monitor server.

    Non-fatal: all network and import errors are caught and logged as
    warnings. The calling agent always continues regardless of outcome.

    Returns True if POST succeeded, False otherwise.
    """
    monitor_url   = os.environ.get('MONITOR_URL', '').rstrip('/')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')

    if not monitor_url:
        log.debug("MONITOR_URL not set — heartbeat skipped")
        return False

    try:
        import requests
        payload = _build_payload(agent_name, status)
        r = requests.post(
            f"{monitor_url}/heartbeat",
            json=payload,
            headers={'X-Token': monitor_token},
            timeout=8,
        )
        if r.status_code == 200:
            log.info(
                f"[HB] Heartbeat sent — agent={agent_name} status={status} "
                f"portfolio=${payload.get('portfolio_value', 0):.2f}"
            )
            return True
        elif r.status_code == 401:
            log.warning(
                "[HB] Monitor rejected heartbeat (401) — "
                "verify MONITOR_TOKEN matches server SECRET_TOKEN"
            )
            return False
        else:
            log.warning(f"[HB] Monitor returned {r.status_code} — {r.text[:80]}")
            return False
    except Exception as e:
        # Covers ConnectionError, Timeout, ImportError, anything else
        # Logged at debug for expected cases (monitor offline), warning for unexpected
        err_str = str(e)
        if any(x in err_str.lower() for x in ('connection', 'timeout', 'refused')):
            log.debug(f"[HB] Monitor unreachable at {monitor_url} — skipped (non-fatal)")
        else:
            log.warning(f"[HB] Heartbeat failed: {e}")
        return False
