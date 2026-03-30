"""
boot_sequence_v1.0.py — Synthos Boot Sequence Coordinator
Synthos · v1.0

Runs once after every Pi reboot via cron @reboot.
Starts all Synthos systems in the correct order, verifies each
step before proceeding, and halts with an alert if anything
critical fails.

Boot order:
  1. Wait for network
  2. Verify project integrity (.env, files, DB)
  3. Run health_check.py
  4. Start watchdog in background
  5. Write boot heartbeat
  6. Log boot complete — cron takes over

CRON ENTRY (replaces individual @reboot lines):
  @reboot sleep 60 && python3 /home/pi/synthos/synthos_build/src/boot_sequence.py >> /home/pi/synthos/synthos_build/logs/boot.log 2>&1

Developer:  Patrick McGuire
Support:    synthos.signal@gmail.com
Version:    1.0
"""

import os
import sys
import time
import socket
import logging
import subprocess
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv

SYNTHOS_VERSION = "1.2"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # src/
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)                  # synthos_build/
PROJECT_DIR = _SCRIPT_DIR                                   # sibling scripts live here
LOG_DIR     = os.path.join(_ROOT_DIR, 'logs')
ENV_PATH    = os.path.join(_ROOT_DIR, 'user', '.env')

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s boot: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'boot.log')),
    ]
)
log = logging.getLogger('boot')

# Required files — if any are missing boot halts
# cleanup.py omitted: runs via cron, absence is non-fatal at boot
REQUIRED_FILES = [
    'database.py',
    'agent1_trader.py',
    'agent2_research.py',
    'agent3_sentiment.py',
    'heartbeat.py',
    'health_check.py',
    'shutdown.py',
    'watchdog.py',
    'portal.py',
]

BOOT_STEPS = []   # records pass/fail for each step
watchdog_process = None


# ── HELPERS ───────────────────────────────────────────────────────────────

def step(name, passed, detail=""):
    icon = "✓" if passed else "✗"
    msg  = f"{icon} {name}"
    if detail:
        msg += f" — {detail}"
    if passed:
        log.info(msg)
    else:
        log.error(msg)
    BOOT_STEPS.append({"name": name, "passed": passed, "detail": detail})
    return passed


def wait_for_network(timeout=60):
    """Wait until internet is reachable. Returns True when ready."""
    log.info("Waiting for network...")
    start = time.time()
    while time.time() - start < timeout:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(("8.8.8.8", 53))
            s.close()
            elapsed = int(time.time() - start)
            log.info(f"Network ready after {elapsed}s")
            return True
        except Exception:
            s.close()
            time.sleep(3)
    return False


def send_sms_alert(message):
    """Send SMS alert via Gmail gateway."""
    try:
        load_dotenv(ENV_PATH, override=True)
        gmail_user  = os.environ.get('GMAIL_USER', '')
        gmail_pass  = os.environ.get('GMAIL_APP_PASSWORD', '')
        phone       = os.environ.get('ALERT_PHONE', '')
        gateway     = os.environ.get('CARRIER_GATEWAY', 'tmomail.net')

        if not all([gmail_user, gmail_pass, phone]):
            log.warning("SMS not configured — skipping alert")
            return False

        msg            = MIMEText(message)
        msg['Subject'] = 'Synthos Boot Alert'
        msg['From']    = gmail_user
        msg['To']      = f"{phone}@{gateway}"

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(gmail_user, gmail_pass)
            s.send_message(msg)
        log.info("Boot alert SMS sent")
        return True
    except Exception as e:
        log.error(f"SMS failed: {e}")
        return False


# ── BOOT STEPS ────────────────────────────────────────────────────────────

def step1_network():
    """Step 1 — Verify network connectivity."""
    log.info("Step 1/9 — Network")
    ok = wait_for_network(timeout=90)
    return step("Network connectivity", ok,
                "internet reachable" if ok else "no internet after 90s — continuing anyway")


def step2_env():
    """Step 2 — Verify .env exists and has required keys."""
    log.info("Step 2/9 — Environment")
    if not os.path.exists(ENV_PATH):
        return step(".env file", False, "not found — run installer")

    load_dotenv(ENV_PATH, override=True)
    required_keys = ['ANTHROPIC_API_KEY', 'ALPACA_API_KEY', 'TRADING_MODE']
    missing = [k for k in required_keys if not os.environ.get(k)]

    if missing:
        return step(".env keys", False, f"missing: {', '.join(missing)}")

    return step(".env file", True, "all required keys present")


def step3_files():
    """Step 3 — Verify all agent files are present."""
    log.info("Step 3/9 — Files")
    missing = [f for f in REQUIRED_FILES
               if not os.path.exists(os.path.join(PROJECT_DIR, f))]
    if missing:
        return step("Agent files", False, f"missing: {', '.join(missing)}")
    return step("Agent files", True, f"all {len(REQUIRED_FILES)} files present")


def step4_database():
    """Step 4 — Database integrity check."""
    log.info("Step 4/9 — Database")
    try:
        import sqlite3
        db_path = os.path.join(_ROOT_DIR, 'data', 'signals.db')

        if not os.path.exists(db_path):
            # Cold start — database will be created on first agent run
            return step("Database", True, "not found — will be created on first run (cold start)")

        conn   = sqlite3.connect(db_path, timeout=10)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()

        if result[0] == 'ok':
            size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 2)
            return step("Database integrity", True, f"OK · {size_mb}MB")
        else:
            return step("Database integrity", False, f"FAILED: {result[0]}")
    except Exception as e:
        return step("Database", False, str(e))


def step5_health_check():
    """
    Step 5 — Run health_check.py.
    Non-fatal — if it times out or fails, boot continues.
    Uses 30s timeout since Alpaca can be slow on cold boot.
    """
    log.info("Step 5/9 — Health check")
    hc_path = os.path.join(PROJECT_DIR, 'health_check.py')
    if not os.path.exists(hc_path):
        return step("Health check", True, "health_check.py not found — skipping")
    try:
        result = subprocess.run(
            [sys.executable, hc_path],
            capture_output=True, text=True,
            timeout=90, cwd=PROJECT_DIR,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return step("Health check", True, "all checks passed")
        else:
            failures = [l.strip() for l in output.split('\n')
                       if '✗' in l or 'FAILED' in l or 'ERROR' in l][:3]
            detail = " | ".join(failures) or "non-zero exit"
            log.warning(f"Health check issues (non-fatal): {detail}")
            return step("Health check", True, f"issues noted (non-fatal): {detail}")
    except subprocess.TimeoutExpired:
        log.warning("Health check timed out after 30s — continuing boot (non-fatal)")
        return step("Health check", True, "timed out — skipped, boot continues")
    except Exception as e:
        log.warning(f"Health check error (non-fatal): {e}")
        return step("Health check", True, f"error skipped: {str(e)[:60]}")


def step6_watchdog():
    """Step 6 — Start watchdog in background."""
    log.info("Step 6/9 — Watchdog")
    wd_path = os.path.join(PROJECT_DIR, 'watchdog.py')
    if not os.path.exists(wd_path):
        return step("Watchdog", False, "watchdog.py not found")
    try:
        global watchdog_process
        log_path = os.path.join(LOG_DIR, 'watchdog.log')
        with open(log_path, 'a') as logf:
            watchdog_process = subprocess.Popen(
                [sys.executable, wd_path],
                stdout=logf, stderr=logf,
                cwd=PROJECT_DIR,
            )
        time.sleep(2)  # give watchdog a moment to start
        if watchdog_process.poll() is None:
            return step("Watchdog", True, f"started (pid={watchdog_process.pid})")
        else:
            return step("Watchdog", False, "process exited immediately — check watchdog.log")
    except Exception as e:
        return step("Watchdog", False, str(e))


def step7_portal():
    """Step 7 — Start portal web server in background."""
    log.info("Step 7/9 — Portal")
    portal_path = os.path.join(PROJECT_DIR, 'portal.py')
    if not os.path.exists(portal_path):
        return step("Portal", False, "portal.py not found — portal unavailable")
    try:
        log_path = os.path.join(LOG_DIR, 'portal.log')
        with open(log_path, 'a') as logf:
            portal_proc = subprocess.Popen(
                [sys.executable, portal_path],
                stdout=logf, stderr=logf,
                cwd=PROJECT_DIR,
            )
        time.sleep(2)
        if portal_proc.poll() is None:
            port = os.environ.get('PORTAL_PORT', '5001')
            return step("Portal", True,
                        f"started (pid={portal_proc.pid}) — http://raspberrypi.local:{port}")
        else:
            return step("Portal", False, "exited immediately — check portal.log")
    except Exception as e:
        return step("Portal", False, str(e))


def step8_monitor():
    """Step 8 — Start monitor server in background (if not already running)."""
    log.info("Step 8/9 — Monitor server")
    monitor_path = os.path.join(PROJECT_DIR, 'synthos_monitor.py')
    if not os.path.exists(monitor_path):
        return step("Monitor", False, "synthos_monitor.py not found — skipping")
    try:
        # Check if already running
        result = subprocess.run(['pgrep', '-f', 'synthos_monitor.py'],
                                capture_output=True, text=True)
        if result.stdout.strip():
            return step("Monitor", True, f"already running (pid={result.stdout.strip()})")

        log_path = os.path.join(LOG_DIR, 'monitor.log')
        env = os.environ.copy()
        env['PORT'] = '5000'
        with open(log_path, 'a') as logf:
            mon_proc = subprocess.Popen(
                [sys.executable, monitor_path],
                stdout=logf, stderr=logf,
                cwd=PROJECT_DIR, env=env,
            )
        time.sleep(2)
        if mon_proc.poll() is None:
            return step("Monitor", True, f"started (pid={mon_proc.pid}) — http://localhost:5000/console")
        else:
            return step("Monitor", False, "exited immediately — check monitor.log")
    except Exception as e:
        return step("Monitor", False, str(e))


def step9_initial_seed():
    """
    Step 9 — Seed intelligence data on first boot or if signals DB is empty.
    Runs agent2_research.py to fetch last 45 days of congressional disclosures.
    Only runs if signals table is empty to avoid duplicating data on normal reboots.
    """
    log.info("Step 9/9 — Initial data seed")
    try:
        sys.path.insert(0, PROJECT_DIR)
        from database import get_db
        db = get_db()
        # Check if signals table has data
        with db.conn() as c:
            count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        if count > 0:
            return step("Data seed", True, f"skipped — {count} signals already in DB")

        # Empty DB — run initial seed
        log.info("Empty signals DB — running initial 45-day seed...")
        research_path = os.path.join(PROJECT_DIR, 'agent2_research.py')
        if not os.path.exists(research_path):
            return step("Data seed", False, "agent2_research.py not found")

        log_path = os.path.join(LOG_DIR, 'daily.log')
        with open(log_path, 'a') as logf:
            result = subprocess.run(
                [sys.executable, research_path, '--session=seed'],
                stdout=logf, stderr=logf,
                cwd=PROJECT_DIR, timeout=300,  # 5 min max
            )
        if result.returncode == 0:
            return step("Data seed", True, "initial 45-day seed complete")
        else:
            return step("Data seed", False, "seed returned non-zero — check daily.log")
    except subprocess.TimeoutExpired:
        return step("Data seed", True, "seed still running in background — check daily.log")
    except Exception as e:
        return step("Data seed", False, str(e))


def step_interrogation_listener():
    """
    Step — Start interrogation listener in background.
    Listens on UDP 5556 for Scout's HAS_DATA_FOR_INTERROGATION broadcasts.
    Sends INTERROGATION_ACK on port 5557 when a signal passes validation.
    Non-fatal: if listener fails to start, Scout marks all signals UNVALIDATED
    and Bolt falls back to WATCH — trading continues safely.
    """
    log.info("Step — Interrogation listener")
    listener_path = os.path.join(PROJECT_DIR, 'interrogation_listener.py')
    if not os.path.exists(listener_path):
        return step("Interrogation listener", True,
                    "interrogation_listener.py not found — skipping (signals will be UNVALIDATED)")
    try:
        # Check if already running
        result = subprocess.run(
            ['pgrep', '-f', 'interrogation_listener.py'],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            return step("Interrogation listener", True,
                        f"already running (pid={result.stdout.strip().splitlines()[0]})")

        log_path = os.path.join(LOG_DIR, 'interrogation.log')
        with open(log_path, 'a') as logf:
            proc = subprocess.Popen(
                [sys.executable, listener_path],
                stdout=logf, stderr=logf,
                cwd=PROJECT_DIR,
            )
        time.sleep(1)
        if proc.poll() is None:
            return step("Interrogation listener", True,
                        f"started (pid={proc.pid}) — UDP {os.environ.get('INTERROGATION_PORT', 5556)}")
        else:
            return step("Interrogation listener", False,
                        "exited immediately — check interrogation.log")
    except Exception as e:
        return step("Interrogation listener", False, str(e))


def step10_seed_suggestions():
    """
    Step 10 — Seed suggestions.json backlog on company node first boot.
    Only runs when COMPANY_MODE=true. Skipped silently on retail nodes.
    No-op if suggestions.json already has entries.
    """
    log.info("Step 10/10 — Suggestions backlog (company node only)")

    if os.environ.get('COMPANY_MODE', '').lower() != 'true':
        return step("Suggestions backlog", True, "skipped — not company node")

    suggestions_path = os.path.join(_ROOT_DIR, 'data', 'suggestions.json')
    seed_path        = os.path.join(PROJECT_DIR, 'seed_backlog.py')

    if not os.path.exists(seed_path):
        return step("Suggestions backlog", True, "seed_backlog.py not found — skipping")

    # Check if already seeded
    if os.path.exists(suggestions_path):
        try:
            import json as _json
            with open(suggestions_path) as _f:
                existing = _json.load(_f)
            if isinstance(existing, list) and len(existing) > 0:
                return step("Suggestions backlog", True,
                            f"already seeded ({len(existing)} suggestions)")
        except Exception:
            pass  # unreadable file — let seed_backlog handle it

    # File missing or empty — seed it
    log.info("suggestions.json empty or missing — running seed_backlog.py --write")
    try:
        result = subprocess.run(
            [sys.executable, seed_path, '--write'],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=30,
        )
        if result.returncode == 0:
            return step("Suggestions backlog", True, "seeded successfully")
        else:
            detail = result.stderr.strip().split('\n')[-1][:80] if result.stderr else "non-zero exit"
            return step("Suggestions backlog", False, f"seed_backlog failed: {detail}")
    except subprocess.TimeoutExpired:
        return step("Suggestions backlog", False, "seed_backlog timed out after 30s")
    except Exception as e:
        return step("Suggestions backlog", False, str(e))


def write_boot_heartbeat():
    """Write boot event to database and Google Sheets if configured."""
    try:
        sys.path.insert(0, PROJECT_DIR)
        from database import get_db
        db = get_db()
        db.log_event(
            "BOOT_COMPLETE",
            agent="boot_sequence",
            details=f"v{SYNTHOS_VERSION} — {sum(1 for s in BOOT_STEPS if s['passed'])}/{len(BOOT_STEPS)} checks passed",
        )
    except Exception as e:
        log.warning(f"Could not write to DB: {e}")

    try:
        hb_path = os.path.join(PROJECT_DIR, 'heartbeat.py')
        if os.path.exists(hb_path):
            subprocess.run(
                [sys.executable, hb_path, '--agent', 'boot_sequence', '--status', 'BOOT_OK'],
                cwd=PROJECT_DIR, capture_output=True, timeout=30,
            )
            log.info("Boot heartbeat written")
    except Exception as e:
        log.warning(f"Heartbeat failed: {e}")


# ── MAIN BOOT SEQUENCE ────────────────────────────────────────────────────

def run():
    log.info("=" * 55)
    log.info(f"SYNTHOS v{SYNTHOS_VERSION} — BOOT SEQUENCE")
    log.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    log.info("=" * 55)

    # Run all steps
    net_ok    = step1_network()
    env_ok    = step2_env()
    files_ok  = step3_files()
    db_ok     = step4_database()
    hc_ok     = step5_health_check()
    wd_ok     = step6_watchdog()
    portal_ok = step7_portal()
    mon_ok    = step8_monitor()
    intg_ok   = step_interrogation_listener()
    seed_ok   = step9_initial_seed()
    sugg_ok   = step10_seed_suggestions()

    # Tally results
    passed = sum(1 for s in BOOT_STEPS if s['passed'])
    total  = len(BOOT_STEPS)
    failed = [s for s in BOOT_STEPS if not s['passed']]

    log.info("=" * 55)
    log.info(f"Boot complete: {passed}/{total} checks passed")

    if failed:
        log.warning(f"Issues: {', '.join(s['name'] for s in failed)}")

    # Critical failures — halt and alert
    critical_failed = [s for s in failed
                       if s['name'] in ('Agent files', 'Database integrity', '.env file')]

    if critical_failed:
        alert = (
            f"Synthos BOOT FAILED: "
            f"{', '.join(s['name'] + ': ' + s['detail'] for s in critical_failed)}. "
            f"Manual intervention required before market open."
        )
        log.critical(alert)
        send_sms_alert(alert)
        # Don't sys.exit — let cron still try to run agents
        # They will fail gracefully and log their own errors
    else:
        log.info("✓ All critical checks passed — Synthos is ready")
        if not hc_ok or not wd_ok:
            log.warning("Non-critical issues detected — check boot.log")

    # Write heartbeat regardless
    if net_ok:
        write_boot_heartbeat()

    log.info("=" * 55)
    log.info("Cron schedule now controls agent execution")
    log.info("=" * 55)


if __name__ == '__main__':
    run()
