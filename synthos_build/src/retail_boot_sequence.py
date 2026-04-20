"""
retail_boot_sequence.py — Synthos Boot Sequence Coordinator
Synthos · v3.0

Runs once after every Pi reboot via cron @reboot.
Starts all Synthos systems in the correct order, verifies each
step before proceeding, and halts with an alert if anything
critical fails.

Boot order:
  1. Wait for network
  2. Verify project integrity (.env, files, DB)
  3. Run retail_health_check.py
  4. Start watchdog in background
  5. Write boot heartbeat
  6. Log boot complete — cron takes over

CRON ENTRY:
  Registered automatically by install_retail.py. Do not edit manually.
  (@reboot sleep 60 && python3 <SYNTHOS_HOME>/src/retail_boot_sequence.py >> <SYNTHOS_HOME>/logs/boot.log 2>&1)

Developer:  Patrick McGuire
Support:    synthos.signal@gmail.com
Version:    3.0
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
PROJECT_DIR = _SCRIPT_DIR                                   # supporting scripts live here
AGENTS_DIR  = os.path.join(_ROOT_DIR, 'agents')             # trading agents live here
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
    'retail_database.py',
    'retail_heartbeat.py',
    'retail_health_check.py',
    'retail_shutdown.py',
    'retail_watchdog.py',
    'retail_portal.py',
]

# Trading agents live in agents/ — checked separately
REQUIRED_AGENT_FILES = [
    'retail_trade_logic_agent.py',
    'retail_news_agent.py',
    'retail_market_sentiment_agent.py',
]

BOOT_STEPS = []   # records pass/fail for each step


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


def _check_systemd_service(unit, label):
    """Check whether a systemd unit is active. Replaces the legacy
    'Popen a second copy and see if it exits' pattern used by step6 /
    step7 / step8: that pattern collided with systemd-owned services
    (portal, watchdog) because the subprocess could not bind the same
    port, exited immediately, and logged ~180 false ERROR/day. Moving
    to a status check means the boot sequence now agrees with reality.

    Returns the result of step() — pass if the unit reports 'active',
    fail otherwise with the actual state (inactive, failed, etc.) in
    the detail field so the operator can `systemctl status` into it.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip() or 'unknown'
        if state == 'active':
            return step(label, True, f"active (systemd unit {unit})")
        return step(label, False,
                    f"{unit} is '{state}' — run `systemctl status {unit}`")
    except FileNotFoundError:
        # systemctl missing on some minimal images — not fatal, just inconclusive.
        return step(label, False, "systemctl not available on this host")
    except subprocess.TimeoutExpired:
        return step(label, False, "systemctl is-active timed out")
    except Exception as e:
        return step(label, False, str(e))


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
    """Send SMS alert via Gmail gateway.

    ARCHITECTURAL EXCEPTION: This function uses smtplib directly rather than
    routing through scoop.py. This is intentional — retail_boot_sequence.py runs before
    any agents are started, so scoop.py is not yet available. Direct SMTP is the
    only viable path for a boot-time alert. This is the sole permitted exception
    to the scoop-only outbound email rule.
    """
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
    """Step 2 — Verify .env exists and has required structural keys.

    API keys (ANTHROPIC_API_KEY, ALPACA_API_KEY, etc.) are intentionally blank
    on a fresh install — they arrive via backup restore after install.
    Only keys that must be present for the system to boot are checked here.
    """
    log.info("Step 2/9 — Environment")
    if not os.path.exists(ENV_PATH):
        return step(".env file", False, "not found — run installer")

    load_dotenv(ENV_PATH, override=True)

    # Keys required for the system to boot at all
    required_keys = ['TRADING_MODE', 'OPERATING_MODE', 'ENCRYPTION_KEY', 'PORTAL_SECRET_KEY']
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        return step(".env keys", False, f"missing: {', '.join(missing)}")

    # Warn if API keys are blank — expected on fresh install, must be restored before trading
    blank_api_keys = [k for k in ['ANTHROPIC_API_KEY', 'ALPACA_API_KEY'] if not os.environ.get(k)]
    if blank_api_keys:
        log.warning("API keys not yet set (%s) — restore from backup before trading sessions run",
                    ', '.join(blank_api_keys))

    return step(".env file", True, "structural keys present"
                + (" — API keys pending restore" if blank_api_keys else ""))


def step3_files():
    """Step 3 — Verify all required files are present."""
    log.info("Step 3/9 — Files")
    missing = [f for f in REQUIRED_FILES
               if not os.path.exists(os.path.join(PROJECT_DIR, f))]
    missing += [f for f in REQUIRED_AGENT_FILES
                if not os.path.exists(os.path.join(AGENTS_DIR, f))]
    if missing:
        return step("Agent files", False, f"missing: {', '.join(missing)}")
    total = len(REQUIRED_FILES) + len(REQUIRED_AGENT_FILES)
    return step("Agent files", True, f"all {total} files present")


def step4_database():
    """Step 4 — Database integrity check."""
    log.info("Step 4/9 — Database")
    try:
        import sqlite3
        db_path = os.path.join(_ROOT_DIR, 'user', 'signals.db')

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
    Step 5 — Run retail_health_check.py.
    Non-fatal — if it times out or fails, boot continues.
    Uses 30s timeout since Alpaca can be slow on cold boot.
    """
    log.info("Step 5/9 — Health check")
    hc_path = os.path.join(PROJECT_DIR, 'retail_health_check.py')
    if not os.path.exists(hc_path):
        return step("Health check", True, "retail_health_check.py not found — skipping")
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
    """Step 6 — Verify watchdog service is running (systemd-managed).

    Previous behavior attempted to Popen a second watchdog directly,
    which collided with the synthos-watchdog.service unit: the second
    instance would fail to acquire the watchdog lock and exit, logging
    a false ERROR on every boot. Switched to a systemctl status check.
    """
    log.info("Step 6/9 — Watchdog")
    return _check_systemd_service('synthos-watchdog', 'Watchdog')


def step7_portal():
    """Step 7 — Verify portal service is running (systemd-managed).

    Previous behavior attempted to Popen a second gunicorn on port 5001,
    which collided with the synthos-portal.service unit: port 5001 was
    already bound, the subprocess exited, false ERROR logged. Switched
    to a systemctl status check.
    """
    log.info("Step 7/9 — Portal")
    return _check_systemd_service('synthos-portal', 'Portal')


def step8_monitor():
    """Step 8 — Monitor server.

    The monitor / command portal lives on the company node (pi4b), not
    the retail node. Retail boots skip entirely — it was a no-op on pi5
    that only generated "synthos_monitor.py not found — skipping" noise
    in boot.log. On company nodes (COMPANY_MODE=true) we verify the
    monitor systemd unit instead of spawning a competing subprocess.
    """
    log.info("Step 8/9 — Monitor server")
    if os.environ.get('COMPANY_MODE', '').lower() != 'true':
        return step("Monitor", True, "skipped — not company node")
    # Company node: verify the monitor systemd unit. Unit name is
    # synthos-login-server per the pi4b service list. If the unit is
    # renamed later, update here.
    return _check_systemd_service('synthos-login-server', 'Monitor')


def step9_initial_seed():
    """
    Step 9 — Seed intelligence data on first boot or if signals DB is empty.
    Runs retail_news_agent.py to fetch last 45 days of congressional disclosures.
    Only runs if signals table is empty to avoid duplicating data on normal reboots.
    """
    log.info("Step 9/9 — Initial data seed")
    try:
        sys.path.insert(0, PROJECT_DIR)
        from retail_database import get_db
        db = get_db()
        # Check if signals table has data
        with db.conn() as c:
            count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        if count > 0:
            return step("Data seed", True, f"skipped — {count} signals already in DB")

        # Empty DB — run initial seed
        log.info("Empty signals DB — running initial 45-day seed...")
        research_path = os.path.join(AGENTS_DIR, 'retail_news_agent.py')
        if not os.path.exists(research_path):
            return step("Data seed", False, "retail_news_agent.py not found")

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
    Step — Report interrogation-listener status.

    The retail-watchdog service owns starting and restarting this
    listener — see retail_watchdog.py, which pgrep's and spawns it on
    its own cycle. Boot sequence's job is to observe and report, not to
    launch a second copy. Previous behavior also used a wrong path
    (src/src/retail_interrogation_listener.py) so it always reported
    "not found — skipping" even when the listener was running fine
    under the watchdog.

    Non-fatal: if the listener isn't up yet when this runs, the
    watchdog's own @reboot fire (~30s after this step) will spin it.
    """
    log.info("Step — Interrogation listener")
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'retail_interrogation_listener.py'],
            capture_output=True, text=True, timeout=5,
        )
        pid_line = result.stdout.strip().splitlines()
    except Exception as e:
        return step("Interrogation listener", True,
                    f"status check failed ({e}) — watchdog will manage")

    if pid_line:
        return step("Interrogation listener", True,
                    f"running (pid={pid_line[0]}) — UDP "
                    f"{os.environ.get('INTERROGATION_PORT', 5556)}")
    # Not up yet — watchdog fires slightly after boot_sequence.  Pass
    # the step and let watchdog handle the spin-up.
    return step("Interrogation listener", True,
                "not yet running — watchdog will start it shortly")


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
        from retail_database import get_db
        db = get_db()
        db.log_event(
            "BOOT_COMPLETE",
            agent="boot_sequence",
            details=f"v{SYNTHOS_VERSION} — {sum(1 for s in BOOT_STEPS if s['passed'])}/{len(BOOT_STEPS)} checks passed",
        )
    except Exception as e:
        log.warning(f"Could not write to DB: {e}")

    try:
        hb_path = os.path.join(PROJECT_DIR, 'retail_heartbeat.py')
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

    # Run all steps. Each step appends its result to BOOT_STEPS (shared
    # module list) which the summary below tallies. Three of these
    # locals (net_ok, hc_ok, wd_ok) ARE read later — don't rename.
    # The rest are effect-only; '_' prefix signals intentional unused.
    net_ok     = step1_network()
    _env_ok    = step2_env()
    _files_ok  = step3_files()
    _db_ok     = step4_database()
    hc_ok      = step5_health_check()
    wd_ok      = step6_watchdog()
    _portal_ok = step7_portal()
    _mon_ok    = step8_monitor()
    _intg_ok   = step_interrogation_listener()
    _seed_ok   = step9_initial_seed()
    _sugg_ok   = step10_seed_suggestions()

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
