#!/usr/bin/env python3
"""
node_heartbeat.py — Standalone node heartbeat sender
Synthos · Company / General Node Edition

Collects system metrics and POSTs a heartbeat to the Synthos Monitor.
Designed to run from cron or a systemd timer — exits after a single POST.

Usage:
    python3 node_heartbeat.py              # uses .env in same dir or parent
    python3 node_heartbeat.py /path/to/.env  # explicit env path

.env keys read:
    MONITOR_URL     — http://pi2w-monitor-ip:5000
    MONITOR_TOKEN   — shared secret (must match SECRET_TOKEN on monitor)
    PI_ID           — unique node identifier, e.g. "pi4b-company"
    PI_LABEL        — display name shown in console, e.g. "pi4b Company Node"

Cron example (every 5 minutes):
    */5 * * * * /usr/bin/python3 /home/pi/synthos-company/node_heartbeat.py >> /home/pi/synthos-company/logs/heartbeat.log 2>&1
"""

import os
import sys
import time

# ── Load .env ─────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))

# Accept optional explicit path as first arg
_env_path = sys.argv[1] if len(sys.argv) > 1 else None

if not _env_path:
    # Search: same dir, parent dir
    for _candidate in [
        os.path.join(_here, '.env'),
        os.path.join(_here, 'company.env'),
        os.path.join(os.path.dirname(_here), '.env'),
    ]:
        if os.path.exists(_candidate):
            _env_path = _candidate
            break

if _env_path and os.path.exists(_env_path):
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)

# ── Config ────────────────────────────────────────────────────────────────────
MONITOR_URL   = os.getenv("MONITOR_URL",   "").rstrip("/")
MONITOR_TOKEN = os.getenv("MONITOR_TOKEN", "")
PI_ID         = os.getenv("PI_ID",         "pi4b-company")
PI_LABEL      = os.getenv("PI_LABEL",      "pi4b Company Node")

if not MONITOR_URL:
    print("[HB] MONITOR_URL not set — heartbeat skipped", flush=True)
    sys.exit(0)


def _collect_metrics() -> dict:
    """Collect system metrics. Returns dict with None values if psutil unavailable."""
    m = dict(
        cpu_percent=None, cpu_count=None, load_avg=None,
        ram_percent=None, ram_total_gb=None, ram_used_gb=None,
        ram_avail_gb=None, ram_cached_gb=None,
        disk_percent=None, disk_total_gb=None, disk_used_gb=None, disk_free_gb=None,
        net_bytes_sent=None, net_bytes_recv=None,
        cpu_temp=None,
    )
    try:
        import psutil
        gb = 1024 ** 3

        m['cpu_percent'] = round(psutil.cpu_percent(interval=0.5), 1)
        m['cpu_count']   = psutil.cpu_count(logical=True)
        load = os.getloadavg()
        m['load_avg'] = [round(load[0], 2), round(load[1], 2), round(load[2], 2)]

        vm = psutil.virtual_memory()
        m['ram_percent']   = round(vm.percent, 1)
        m['ram_total_gb']  = round(vm.total     / gb, 2)
        m['ram_used_gb']   = round(vm.used      / gb, 2)
        m['ram_avail_gb']  = round(vm.available / gb, 2)
        cached = getattr(vm, 'cached', 0) + getattr(vm, 'buffers', 0)
        m['ram_cached_gb'] = round(cached / gb, 2)

        du = psutil.disk_usage('/')
        m['disk_percent']  = round(du.percent, 1)
        m['disk_total_gb'] = round(du.total / gb, 1)
        m['disk_used_gb']  = round(du.used  / gb, 1)
        m['disk_free_gb']  = round(du.free  / gb, 1)

        net = psutil.net_io_counters()
        m['net_bytes_sent'] = net.bytes_sent
        m['net_bytes_recv'] = net.bytes_recv
    except Exception as e:
        print(f"[HB] psutil error (non-fatal): {e}", flush=True)

    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            m['cpu_temp'] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    return m


def _detect_agents() -> dict:
    """Return running Synthos agent names → 'active' by scanning process list.

    The `known` dict lists every long-running daemon we'd want to see in the
    heartbeat payload on any node.  Keys are substrings to match against the
    process cmdline; values are the canonical name reported to the monitor.
    An agent that isn't running on this node simply doesn't match — no harm
    in listing both company and retail agents here, so the same script works
    unchanged on both pi4b and pi5.

    Short-lived cron-spawned scripts (retail_backup.py, rotate_logs.py,
    retail_heartbeat.py, retail_scheduler.py, rebuild_default_template.py)
    are intentionally NOT listed here — by the time the heartbeat fires they
    have typically exited, and reporting their absence as "not live" would
    be misleading noise.
    """
    agents = {}
    try:
        import psutil
        known = {
            # ── Company node (pi4b) long-running agents ───────────────────
            'scoop.py':             'scoop',
            'strongbox.py':         'strongbox',
            'company_server.py':    'company_server',
            'company_vault.py':     'company_vault',
            'company_sentinel.py':  'company_sentinel',
            'company_archivist.py': 'company_archivist',
            'company_keepalive.py': 'company_keepalive',
            'company_auditor.py':   'company_auditor',

            # ── Retail node (pi5) long-running agents ─────────────────────
            # retail_portal is matched on the substring 'retail_portal'
            # (not .py) because it runs under gunicorn with cmdline like
            # "gunicorn ... retail_portal:app" — no literal .py token.
            'retail_portal':                    'retail_portal',
            'retail_watchdog.py':               'retail_watchdog',
            'retail_interrogation_listener.py': 'retail_interrogation_listener',
            'retail_market_daemon.py':          'retail_market_daemon',
            'retail_price_poller.py':           'retail_price_poller',
        }
        for proc in psutil.process_iter(['cmdline']):
            try:
                cmd = ' '.join(proc.info['cmdline'] or [])
                for script, name in known.items():
                    if script in cmd:
                        agents[name] = 'active'
            except Exception:
                pass
    except Exception:
        pass
    return agents or {'node_heartbeat': 'active'}


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[HB] {ts}  {PI_ID} → {MONITOR_URL}", flush=True)

    metrics = _collect_metrics()
    agents  = _detect_agents()

    payload = {
        "pi_id":          PI_ID,
        "label":          PI_LABEL,
        "agents":         agents,
        "operating_mode": "MANAGED",
        "trading_mode":   "PAPER",
        "kill_switch":    False,
        **metrics,
    }

    try:
        import requests
        r = requests.post(
            f"{MONITOR_URL}/heartbeat",
            json=payload,
            headers={"X-Token": MONITOR_TOKEN},
            timeout=8,
        )
        if r.status_code == 200:
            print(
                f"[HB] OK — CPU {metrics['cpu_percent']}%  "
                f"RAM {metrics['ram_percent']}%  "
                f"Temp {metrics['cpu_temp']}°C  "
                f"Agents: {list(agents.keys())}",
                flush=True,
            )
        elif r.status_code == 401:
            print(f"[HB] 401 Unauthorized — check MONITOR_TOKEN matches SECRET_TOKEN on monitor", flush=True)
        else:
            print(f"[HB] Monitor returned {r.status_code}: {r.text[:80]}", flush=True)
    except Exception as e:
        print(f"[HB] POST failed: {e}", flush=True)


if __name__ == "__main__":
    main()
