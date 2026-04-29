#!/usr/bin/env python3
"""
node_watchdog.py — External Node Monitor (v2, 2026-04-24)
=========================================================
Runs on pi2w_monitor. Pings pi4b and pi5 every minute. If either fails
to respond for 3 consecutive checks (3 minutes), fires a single alert
via an ESCALATING notification chain.

Alert chain (tries in order, stops at first success):
    Tier 1 — Command portal  (pi4b synthos_monitor /api/enqueue)
    Tier 2 — Retail portal   (pi5 /api/admin/alert — writes to owner's
                              notifications table with category='admin')
    Tier 3 — SMS via carrier gateway  (ALERT_PHONE @ CARRIER_GATEWAY)
    Tier 4 — Email via Resend         (last-resort fallback)

Keeps alerts OUT of email unless all portal channels are down — addresses
the "this always goes straight to inbox" complaint.

Recovery notices follow the same chain.

IGNORE_LIST nodes are deliberately disabled; pings + log entries skip
them entirely (prior version was logging 8000+ FAIL entries for
pi2w_sentinel which is intentionally off).

Cron: * * * * * /home/pi-02w/synthos/venv/bin/python3 \
      /home/pi-02w/synthos/node_watchdog.py
"""

import os
import sys
import json
import socket
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Config ──
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / '.watchdog_state.json'
LAST_RUN_FILE = BASE_DIR / '.watchdog_last_run.json'
# 2026-04-28 — heartbeat log line cadence. The watchdog only logs on
# FAIL/RECOVERY transitions; when both nodes are healthy for hours,
# the log file looks frozen and operator can't tell working-quietly
# from crashed. A heartbeat every N minutes proves the cron tick is
# still firing the script. 30 min keeps log noise low (~50 lines/day).
HEARTBEAT_LOG_INTERVAL_MIN = 30

from dotenv import load_dotenv
load_dotenv(BASE_DIR / '.env')

# Tier 1 — command portal (pi4b)
COMMAND_PORTAL_URL = os.environ.get('COMMAND_PORTAL_URL', 'http://10.0.0.10:5050')
MONITOR_TOKEN      = os.environ.get('MONITOR_TOKEN', '')

# Tier 2 — retail portal (pi5)
RETAIL_PORTAL_URL  = os.environ.get('RETAIL_PORTAL_URL', 'http://10.0.0.11:5001')

# Tier 3 — SMS via carrier gateway
ALERT_PHONE        = os.environ.get('ALERT_PHONE', '').strip()
CARRIER_GATEWAY    = os.environ.get('CARRIER_GATEWAY', '').strip()  # e.g. 'tmomail.net'

# Tier 4 — direct email (last-resort)
RESEND_API_KEY     = os.environ.get('RESEND_API_KEY', '')
ALERT_FROM         = os.environ.get('ALERT_FROM', 'alerts@synth-cloud.com')
ALERT_TO           = os.environ.get('ALERT_TO', '')

PI_ID              = os.environ.get('PI_ID', 'pi2w-monitor')

# Nodes to monitor (full health-check pings)
NODES = {
    'pi4b': {
        'url': 'http://10.0.0.10:5050/health',
        'label': 'Company Node (pi4b)',
        'timeout': 8,
    },
    'pi5': {
        'url': 'http://10.0.0.11:5001/login',
        'label': 'Retail Node (pi5)',
        'timeout': 8,
    },
}

# Deliberately-disabled nodes — skipped entirely (no ping, no log spam).
# Move a node out of NODES and into this set when taking it offline
# on purpose, so the watchdog doesn't fire 8000+ false FAIL entries.
IGNORE_LIST = {
    'pi2w_sentinel',  # disabled 2026-04-20, display node
}

# Alert after N consecutive failures
FAIL_THRESHOLD = 3

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s watchdog: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('watchdog')


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_node(node_id, cfg):
    """Ping a node. Returns True if reachable. Supports http:// and tcp:// URLs."""
    url = cfg['url']
    try:
        if url.startswith('tcp://'):
            host, port = url.replace('tcp://', '').split(':')
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(cfg['timeout'])
            s.connect((host, int(port)))
            s.close()
            return True
        r = requests.get(url, timeout=cfg['timeout'])
        return r.status_code < 500
    except Exception:
        return False


# ── Escalating alert chain ─────────────────────────────────────────────

def _try_command_portal(subject, body, priority):
    """Tier 1: pi4b synthos_monitor /api/enqueue."""
    if not COMMAND_PORTAL_URL or not MONITOR_TOKEN:
        return False
    try:
        r = requests.post(
            f"{COMMAND_PORTAL_URL}/api/enqueue",
            headers={'X-Token': MONITOR_TOKEN, 'Content-Type': 'application/json'},
            json={
                'event_type':   'WATCHDOG_ALERT',
                'priority':     2 if priority in ('high', 'critical') else 1,
                'subject':      subject,
                'body':         body,
                'source_agent': 'pi2w_watchdog',
                'pi_id':        PI_ID,
                'audience':     'internal',
                'payload':      json.dumps({'priority': priority}),
            },
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log.debug(f"tier1 command portal fail: {e}")
        return False


def _try_retail_portal(subject, body, priority):
    """Tier 2: pi5 retail portal /api/admin/alert → owner's notifications."""
    if not RETAIL_PORTAL_URL or not MONITOR_TOKEN:
        return False
    try:
        r = requests.post(
            f"{RETAIL_PORTAL_URL}/api/admin/alert",
            headers={'X-Token': MONITOR_TOKEN, 'Content-Type': 'application/json'},
            json={'subject': subject, 'body': body, 'priority': priority},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log.debug(f"tier2 retail portal fail: {e}")
        return False


def _try_sms(subject, body):
    """Tier 3: SMS via carrier-gateway email (phone@gateway). Uses Resend
    to send because it's email-delivery under the hood."""
    if not (ALERT_PHONE and CARRIER_GATEWAY and RESEND_API_KEY):
        return False
    try:
        sms_to = f"{ALERT_PHONE}@{CARRIER_GATEWAY}"
        # SMS via email gateway — keep under 160 char for single segment
        combined = f"{subject} | {body}"[:160]
        r = requests.post(
            'https://api.resend.com/emails',
            json={
                'from': f'Synthos <{ALERT_FROM}>',
                'to':   [sms_to],
                'subject': subject[:60],
                'text':    combined,
            },
            headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.debug(f"tier3 sms fail: {e}")
        return False


def _try_email(subject, body):
    """Tier 4: direct Resend email (last resort)."""
    if not (RESEND_API_KEY and ALERT_TO):
        return False
    try:
        r = requests.post(
            'https://api.resend.com/emails',
            json={
                'from': f'Synthos Watchdog <{ALERT_FROM}>',
                'to':   [ALERT_TO],
                'subject': subject,
                'text':    body,
            },
            headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.debug(f"tier4 email fail: {e}")
        return False


def send_alert(subject, body, priority='normal'):
    """Escalating alert — tries portals first, falls back to SMS/email only
    if portals are unreachable. Returns the tier that succeeded, or None."""
    for tier_name, fn in (
        ('command_portal', lambda: _try_command_portal(subject, body, priority)),
        ('retail_portal',  lambda: _try_retail_portal(subject, body, priority)),
        ('sms',            lambda: _try_sms(subject, body)),
        ('email',          lambda: _try_email(subject, body)),
    ):
        try:
            if fn():
                log.info(f"Alert delivered via tier: {tier_name} — {subject[:60]}")
                return tier_name
        except Exception as e:
            log.debug(f"tier {tier_name} exception: {e}")
    log.error(f"All alert tiers failed: {subject[:80]}")
    return None


# ── Main loop ──────────────────────────────────────────────────────────

def run():
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    # Per-run summary — populated as we check each node so we can
    # write a status file + optionally a heartbeat log line at the end.
    run_summary = {}

    # Purge state entries for nodes in IGNORE_LIST so the state file
    # doesn't keep stale counters for disabled nodes.
    for dead in list(state.keys()):
        if dead in IGNORE_LIST:
            state.pop(dead, None)

    for node_id, cfg in NODES.items():
        if node_id in IGNORE_LIST:
            continue  # deliberately skipped, no ping, no log entry

        node_state = state.setdefault(node_id, {
            'consecutive_fails': 0,
            'alerted': False,
            'last_check': None,
            'last_ok': None,
        })

        alive = check_node(node_id, cfg)
        node_state['last_check'] = now
        run_summary[node_id] = 'OK' if alive else f'FAIL#{node_state["consecutive_fails"]+1}'

        if alive:
            if node_state['alerted']:
                send_alert(
                    f'[RECOVERED] {cfg["label"]} is back online',
                    f'{cfg["label"]} ({node_id}) is responding again.\n'
                    f'Was down for {node_state["consecutive_fails"]} check(s).\n'
                    f'Recovered at: {now}',
                    priority='normal',
                )
                log.info(f"{node_id}: RECOVERED after {node_state['consecutive_fails']} failures")
            node_state['consecutive_fails'] = 0
            node_state['alerted'] = False
            node_state['last_ok'] = now
        else:
            node_state['consecutive_fails'] += 1
            log.warning(f"{node_id}: FAIL #{node_state['consecutive_fails']} "
                       f"(threshold: {FAIL_THRESHOLD})")

            if node_state['consecutive_fails'] >= FAIL_THRESHOLD and not node_state['alerted']:
                send_alert(
                    f'[DOWN] {cfg["label"]} is not responding',
                    f'{cfg["label"]} ({node_id}) has failed {FAIL_THRESHOLD} consecutive '
                    f'health checks.\n\n'
                    f'URL: {cfg["url"]}\n'
                    f'Last OK: {node_state.get("last_ok", "never")}\n'
                    f'First failure: {now}\n\n'
                    f'Check the node immediately. This alert will not repeat until the '
                    f'node recovers.',
                    priority='high',
                )
                node_state['alerted'] = True

    # 2026-04-28 — periodic heartbeat log + always-fresh status file.
    # The original watchdog logged ONLY on FAIL/RECOVERY transitions,
    # so a healthy run was indistinguishable from a crashed cron job
    # when scanning the log file. Two additions, both purely
    # observational (no impact on alerting):
    #
    # 1. Status file (.watchdog_last_run.json) — overwritten every
    #    run with timestamp + per-node summary. `stat` confirms the
    #    cron tick fired; `cat` confirms what it found. Always fresh.
    #
    # 2. Heartbeat log line — written every N minutes (default 30)
    #    so an operator scanning watchdog.log sees evidence of life
    #    even when nothing's failing. ~48 lines/day at 30-min cadence.
    try:
        last_hb_iso = state.get('_last_heartbeat_log', '')
        write_hb = True
        if last_hb_iso:
            try:
                last_hb = datetime.fromisoformat(last_hb_iso)
                now_dt = datetime.now(timezone.utc)
                if (now_dt - last_hb).total_seconds() < HEARTBEAT_LOG_INTERVAL_MIN * 60:
                    write_hb = False
            except Exception:
                pass
        if write_hb:
            summary_str = ' '.join(f'{k}={v}' for k, v in run_summary.items()) or 'no-nodes'
            log.info(f"heartbeat — {summary_str}")
            state['_last_heartbeat_log'] = now
    except Exception as _e:
        log.debug(f"heartbeat log skipped: {_e}")

    # Status file — always written, always overwrites. Uses tmp-then-
    # rename so a partial write never leaves a corrupt file.
    try:
        import json as _json
        tmp = LAST_RUN_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            _json.dump({
                'last_run_utc': now,
                'nodes':        run_summary,
                'ignore_list':  list(IGNORE_LIST),
            }, f, indent=2)
        tmp.replace(LAST_RUN_FILE)
    except Exception as _e:
        log.debug(f"status file write skipped: {_e}")

    save_state(state)


if __name__ == '__main__':
    run()
