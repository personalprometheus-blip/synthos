"""
retail_health_check.py — Post-Reboot Health Verification
Synthos v3.0

Runs: called by retail_boot_sequence.py during boot sequence.
  Cron is registered automatically by install_retail.py — do not edit manually.

Checks:
  1. Database integrity
  2. All required tables present
  3. Alpaca connection
  4. Position reconciliation (orphans and ghosts)
  5. Writes heartbeat to Google Sheets
  6. Sends SMS alert if any check fails

Safe to run manually at any time:
  python3 health_check.py
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'user', '.env'))

ALPACA_API_KEY  = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET   = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_BASE_URL = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
ALERT_FROM      = os.environ.get('ALERT_FROM', '')
ALERT_TO        = os.environ.get('ALERT_TO', os.environ.get('USER_EMAIL', ''))
TRADING_MODE    = os.environ.get('TRADING_MODE', 'PAPER')

REQUIRED_TABLES = [
    'portfolio', 'positions', 'ledger', 'signals',
    'handshakes', 'scan_log', 'system_log', 'outcomes', 'urgent_flags',
    'pending_approvals',   # added Phase 03B — DB-backed approval queue
]

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('health_check')


def _enqueue_alert(subject: str, message: str, priority: int,
                    event_type: str) -> bool:
    """
    POST a health alert to the company Pi Scoop queue via /api/enqueue.
    Returns True if enqueue succeeded.

    Retries transient connection failures with 10s backoff (up to 3 tries,
    ~30s total patience).  This is specifically to cover the Saturday
    04:00 boot race: pi5 and pi4b both reboot at the same minute, and
    pi5's boot_sequence fires health_check before pi4b is listening on
    5050.  Without retries the alert was silently dropped.

    Retries are ONLY applied to transport-level failures (connection
    refused, timeout, DNS).  4xx/5xx responses are passed through without
    retry — those indicate auth/payload/server-logic issues that more
    retries won't fix.
    """
    monitor_url   = os.environ.get('MONITOR_URL', '').rstrip('/')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    pi_id         = os.environ.get('PI_ID', 'synthos-pi')

    if not monitor_url:
        log.debug("MONITOR_URL not set — alert enqueue skipped")
        return False

    payload = {
        "event_type":   event_type,
        "priority":     priority,
        "subject":      subject,
        "body":         message,
        "source_agent": "health_check",
        "pi_id":        pi_id,
        "audience":     "internal",
        "payload":      {"pi_id": pi_id, "message": message[:200]},
    }

    import requests as _req
    endpoint = f"{monitor_url}/api/enqueue"
    headers  = {"X-Token": monitor_token, "Content-Type": "application/json"}

    # Up to 3 attempts with 10s backoff — covers ~30s of pi4b unavailability.
    # Total worst-case latency to "give up and fall through to direct
    # send": ~35 seconds (3 tries × 5s timeout + 2 × 10s waits).
    max_attempts  = 3
    backoff_secs  = 10

    for attempt in range(1, max_attempts + 1):
        try:
            r = _req.post(endpoint, json=payload, headers=headers, timeout=5)
            # Non-200 response: do NOT retry — it's a server-side
            # rejection (bad auth, malformed payload, etc.) that further
            # retries won't fix.
            if r.status_code == 200:
                if attempt > 1:
                    log.info(
                        f"Health alert queued for Scoop: {event_type} "
                        f"P{priority} (succeeded on attempt {attempt}/{max_attempts})"
                    )
                else:
                    log.info(f"Health alert queued for Scoop: {event_type} P{priority}")
                return True
            log.warning(
                f"Enqueue returned HTTP {r.status_code} on attempt "
                f"{attempt}/{max_attempts} — not retrying (non-transient)"
            )
            return False
        except (_req.exceptions.ConnectionError,
                _req.exceptions.Timeout) as e:
            if attempt < max_attempts:
                log.warning(
                    f"Enqueue attempt {attempt}/{max_attempts} failed "
                    f"({type(e).__name__}: {str(e)[:80]}) — retrying in "
                    f"{backoff_secs}s"
                )
                time.sleep(backoff_secs)
                continue
            log.warning(
                f"Enqueue failed after {max_attempts} attempts "
                f"({type(e).__name__}) — falling back to direct send"
            )
            return False
        except Exception as e:
            # Non-network unexpected error — fail fast.
            log.warning(f"Enqueue failed with unexpected error ({e}) — "
                        f"falling back to direct send")
            return False

    return False


def send_alert(message: str) -> bool:
    """
    Send health alert. Primary: Scoop enqueue (P1).
    Fallback: Resend direct send if enqueue fails.
    Gmail SMTP path available — uncomment in .env and below when configured.
    """
    subject    = "Synthos Health Alert"
    event_type = "VALIDATION_FAILURE"
    priority   = 1   # P1 — important operational

    # Primary: Scoop queue
    if _enqueue_alert(subject, message, priority, event_type):
        return True

    # Fallback: Resend direct
    if RESEND_API_KEY and ALERT_FROM and ALERT_TO:
        try:
            import urllib.request as _urlreq, json as _json
            _payload = _json.dumps({
                "from":    ALERT_FROM,
                "to":      [ALERT_TO],
                "subject": subject,
                "text":    message,
            }).encode()
            _req = _urlreq.Request(
                "https://api.resend.com/emails", data=_payload,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                         "Content-Type": "application/json"})
            with _urlreq.urlopen(_req, timeout=10) as r:
                if r.status in (200, 201):
                    log.info(f"Health alert sent via Resend → {ALERT_TO}")
                    return True
                else:
                    log.error(f"Resend returned {r.status}")
        except Exception as e:
            log.error(f"Resend fallback failed: {e}")

    # ── Gmail SMTP path (uncomment when GMAIL_USER / GMAIL_APP_PASSWORD set) ──
    # GMAIL_USER     = os.environ.get('GMAIL_USER', '')
    # GMAIL_APP_PASS = os.environ.get('GMAIL_APP_PASSWORD', '')
    # if GMAIL_USER and GMAIL_APP_PASS and ALERT_TO:
    #     try:
    #         import smtplib
    #         from email.mime.text import MIMEText
    #         msg = MIMEText(message)
    #         msg['Subject'] = subject
    #         msg['From']    = GMAIL_USER
    #         msg['To']      = ALERT_TO
    #         with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
    #             s.login(GMAIL_USER, GMAIL_APP_PASS)
    #             s.send_message(msg)
    #         log.info(f"Health alert sent via Gmail → {ALERT_TO}")
    #         return True
    #     except Exception as e:
    #         log.error(f"Gmail fallback failed: {e}")

    log.warning(
        f"Alert not delivered — all paths failed or unconfigured. "
        f"Message: {message[:120]}"
    )
    return False


def check_db_integrity(db):
    ok = db.integrity_check()
    if ok:
        log.info("✓ Database integrity: OK")
    else:
        log.error("✗ Database integrity: FAILED")
    return ok


def check_required_tables(db):
    import sqlite3
    missing = []
    try:
        with sqlite3.connect(db.path) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if not missing:
            log.info(f"✓ All {len(REQUIRED_TABLES)} required tables present")
        else:
            log.error(f"✗ Missing tables: {', '.join(missing)}")
    except Exception as e:
        log.error(f"✗ Table check error: {e}")
        missing = REQUIRED_TABLES
    return missing


def check_alpaca(db):
    if not ALPACA_API_KEY:
        log.warning("⚠ ALPACA_API_KEY not set — skipping Alpaca check")
        return True, 0.0

    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        r = requests.get(
            f"{ALPACA_BASE_URL}/v2/account",
            headers=headers, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        cash = float(data.get('cash', 0))
        log.info(f"✓ Alpaca connected — cash: ${cash:.2f} ({TRADING_MODE})")
        return True, cash
    except Exception as e:
        log.error(f"✗ Alpaca connection failed: {e}")
        return False, 0.0


def check_positions(db):
    """Reconcile DB positions against Alpaca.

    In the single-tenant design that pre-dated the fleet, the master
    signals.db held all positions and a single Alpaca account was
    authoritative, so orphans/ghosts were well-defined.

    In the current multi-tenant design positions live in per-customer
    DBs (data/customers/<uuid>/signals.db), and each customer has
    their own Alpaca credentials.  The master DB's `positions` table
    is vestigial and stays empty.  A blind comparison therefore
    flags every real position in the admin's Alpaca account as an
    "orphan in master DB" — which it is, technically, but not in any
    useful sense.  customer_health_check.py (called by the auditor)
    owns per-customer reconciliation.

    Detect the multi-tenant case by master-DB emptiness and skip
    cleanly.  This keeps the boot-time Alpaca connectivity check
    intact (which IS useful — it catches DNS / auth / network
    problems) while suppressing the false-positive orphan flood.
    """
    issues = []
    if not ALPACA_API_KEY:
        return issues

    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        r = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=headers, timeout=10
        )
        r.raise_for_status()
        alpaca_tickers = {p['symbol'] for p in r.json()}
        db_tickers     = db.get_open_tickers()

        # Multi-tenant mode — master DB positions table is unused.
        # Don't compare against an intentionally-empty set.
        if not db_tickers:
            log.info(
                f"✓ Position reconciliation: skipped — master DB has no "
                f"positions (multi-tenant mode; {len(alpaca_tickers)} positions "
                f"reported by Alpaca, reconciled per-customer by customer_health_check)"
            )
            return issues

        orphans = alpaca_tickers - db_tickers
        ghosts  = db_tickers - alpaca_tickers

        if orphans:
            msg = f"ORPHAN positions in Alpaca (not in DB): {', '.join(orphans)}"
            log.error(f"✗ {msg}")
            issues.append(msg)
        if ghosts:
            msg = f"GHOST positions in DB (not in Alpaca): {', '.join(ghosts)}"
            log.error(f"✗ {msg}")
            issues.append(msg)
        if not orphans and not ghosts:
            log.info(f"✓ Position reconciliation: clean ({len(alpaca_tickers)} positions)")

    except Exception as e:
        log.warning(f"⚠ Position reconciliation skipped: {e}")

    return issues


def run():
    log.info("=" * 50)
    log.info("SYNTHOS — POST-REBOOT HEALTH CHECK")
    log.info("=" * 50)

    issues = []

    # Import DB
    try:
        from retail_database import get_db
        db = get_db()
        log.info("✓ Database module loaded")
    except Exception as e:
        log.error(f"✗ Cannot load database module: {e}")
        send_alert(f"Synthos reboot FAILED: Cannot load database — {e}")
        sys.exit(1)

    # 1. Integrity
    if not check_db_integrity(db):
        issues.append("DATABASE INTEGRITY FAILED — manual intervention required")

    # 2. Tables
    missing = check_required_tables(db)
    if missing:
        issues.append(f"Missing DB tables: {', '.join(missing)}")

    # 3. Alpaca
    alpaca_ok, alpaca_cash = check_alpaca(db)
    if not alpaca_ok:
        issues.append("Alpaca connection failed — check API keys")

    # 4. Positions
    pos_issues = check_positions(db)
    issues.extend(pos_issues)

    # 5. Write heartbeat to monitor server
    try:
        portfolio    = db.get_portfolio() or {}
        open_pos     = db.get_open_positions() or []
        total        = round(portfolio.get('cash', 0.0) + sum(p['entry_price'] * p['shares'] for p in open_pos), 2)
        from retail_heartbeat import write_heartbeat
        write_heartbeat(
            agent_name="health_check",
            status="REBOOT_OK" if not issues else "REBOOT_ISSUES"
        )
        log.info(f"✓ Heartbeat sent — portfolio: ${total:.2f}")
    except Exception as e:
        log.warning(f"⚠ Heartbeat send failed: {e}")

    # 6. Log to DB
    db.log_event(
        "HEALTH_CHECK",
        agent="health_check",
        details="PASSED" if not issues else f"ISSUES: {'; '.join(issues)}"
    )

    # 7. Report
    log.info("=" * 50)
    if issues:
        alert = "Synthos reboot health check FAILED:\n" + "\n".join(f"• {i}" for i in issues)
        log.error(alert)
        send_alert(alert)
        log.info("Health alert sent")
        sys.exit(1)
    else:
        log.info("✓ All checks passed — system ready for market open")
        log.info("=" * 50)


if __name__ == '__main__':
    # Wait for network to stabilize if called from @reboot cron
    # (cron handles the 60s sleep, but add a small buffer here too)
    if '--boot' in sys.argv:
        log.info("Boot mode — waiting 10s for services to stabilize")
        time.sleep(10)

    try:
        run()
    except Exception as e:
        log.error(f"Health check crashed: {e}", exc_info=True)
        sys.exit(1)
