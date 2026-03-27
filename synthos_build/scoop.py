"""
scoop.py -- Scoop Mail Agent
Synthos Company Pi

Role:
  Single delivery channel for ALL outbound communication.
  No other agent sends email, SMS, or external notifications directly.
  Everything goes through Scoop.

  Scoop is accountable for:
    - Every alert reaching its intended recipient
    - Priority dispatch (P0 first, always)
    - Queuing and retrying failed sends (max 5 attempts)
    - Formatting messages correctly for internal vs. customer audiences
    - Tracking delivery success rate

  Event sources:
    - scoop_queue table in company.db (primary -- DB-backed, priority-ordered)
    - Legacy scoop_trigger.json shim (backward compat -- drained once at startup)

  Priority classes:
    P0 = emergency    (PROTECTIVE_EXIT_TRIGGERED, CASCADE_DETECTED, ...)
    P1 = important    (HEARTBEAT_SILENCE_ALERT, VALIDATION_FAILURE, ...)
    P2 = transactional (TRADE_NOTIFICATION, APPROVAL_REQUEST, ...)
    P3 = background   (DAILY_REPORT, MORNING_DIGEST, ...)

  Transport:
    Primary:  SendGrid API
    Fallback: Gmail SMTP (uncomment .env keys + code block below when ready)
    Toggle:   Command portal controls active transport (future)

Schedule:
  Poll DB queue every 30s
  Retry failures every 5 minutes
  Morning report: daily at 8am ET

USAGE:
  python3 scoop.py           # start polling loop
  python3 scoop.py --status  # show delivery queue status
  python3 scoop.py --test    # send test email to project lead
"""

import os
import sys
import json
import uuid
import time
import signal
import logging
import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# -- CONFIG -------------------------------------------------------------------

_HERE    = Path(__file__).resolve().parent
BASE_DIR = _HERE.parent

DB_PATH        = BASE_DIR / "data" / "company.db"
LEGACY_TRIGGER = BASE_DIR / "data" / "scoop_trigger.json"
MORNING_REPORT = BASE_DIR / "data" / "morning_report.json"
LOG_FILE       = BASE_DIR / "logs" / "mail_agent.log"

ET = ZoneInfo("America/New_York")

load_dotenv(BASE_DIR / ".env", override=True)

SENDGRID_API_KEY   = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM      = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@synthos.app")
SENDGRID_FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "Synthos")
PROJECT_LEAD_EMAIL = os.environ.get("PROJECT_LEAD_EMAIL", "")

# Gmail SMTP config -- uncomment when command portal transport toggle is ready
# GMAIL_USER         = os.environ.get("GMAIL_USER", "")
# GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

POLL_INTERVAL_SEC   = 30
RETRY_INTERVAL_SEC  = 300
MAX_RETRIES         = 5
MORNING_REPORT_HOUR = 8

SYNTHOS_VERSION = "2.0"

# -- LOGGING ------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s scoop: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("scoop")


# -- TIME HELPERS -------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_et() -> datetime:
    return datetime.now(ET)

def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


# -- DATABASE -----------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_customer_email(pi_id: str):
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT email, mail_alerts_enabled FROM customers WHERE pi_id=?",
                (pi_id,)
            ).fetchone()
            if row and row["email"] and row["mail_alerts_enabled"]:
                return row["email"]
    except Exception as e:
        log.warning(f"Could not look up email for {pi_id}: {e}")
    return None


def db_get_next_event():
    """Fetch single highest-priority PENDING or RETRY event."""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT * FROM scoop_queue
                WHERE status IN ('PENDING', 'RETRY')
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None
    except Exception as e:
        log.error(f"db_get_next_event error: {e}")
        return None


def db_mark_processing(event_id: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE scoop_queue SET status='PROCESSING', started_at=? WHERE id=?",
                (now_iso(), event_id)
            )
    except Exception as e:
        log.error(f"db_mark_processing error: {e}")


def db_mark_sent(event_id: str) -> None:
    now = now_iso()
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE scoop_queue SET status='SENT', sent_at=?, completed_at=? WHERE id=?",
                (now, now, event_id)
            )
    except Exception as e:
        log.error(f"db_mark_sent error: {e}")


def db_requeue(event_id: str, error_msg: str) -> None:
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE scoop_queue
                SET status='RETRY', retry_count=retry_count+1,
                    last_attempt=?, error_msg=?
                WHERE id=?
            """, (now_iso(), error_msg[:200], event_id))
    except Exception as e:
        log.error(f"db_requeue error: {e}")


def db_mark_failed(event_id: str, error_msg: str) -> None:
    now = now_iso()
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE scoop_queue
                SET status='FAILED', failed_at=?, last_attempt=?, error_msg=?
                WHERE id=?
            """, (now, now, error_msg[:200], event_id))
    except Exception as e:
        log.error(f"db_mark_failed error: {e}")


def db_queue_stats() -> dict:
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            p0 = conn.execute("""
                SELECT COUNT(*) FROM scoop_queue
                WHERE priority=0 AND status IN ('PENDING', 'RETRY')
            """).fetchone()[0]
            stats["P0_pending"] = p0
            return stats
    except Exception as e:
        log.error(f"db_queue_stats error: {e}")
        return {}


# -- TRANSPORT ----------------------------------------------------------------

def send_via_sendgrid(to_email: str, subject: str, body_text: str,
                      body_html: str = None) -> bool:
    if not SENDGRID_API_KEY:
        log.warning("SENDGRID_API_KEY not configured")
        return False
    if not to_email:
        log.warning(f"No recipient for: {subject[:60]}")
        return False

    import urllib.request
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from":    {"email": SENDGRID_FROM, "name": SENDGRID_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body_text}]
        + ([{"type": "text/html", "value": body_html}] if body_html else []),
    }).encode()

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type":  "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 202):
                log.info(f"[SENDGRID] Sent: {subject[:60]} -> {to_email}")
                return True
            log.warning(f"[SENDGRID] Returned {resp.status}")
            return False
    except Exception as e:
        log.error(f"[SENDGRID] Error: {e}")
        return False


# Gmail SMTP transport -- uncomment when command portal toggle is ready
# def send_via_gmail(to_email: str, subject: str, body_text: str) -> bool:
#     if not GMAIL_USER or not GMAIL_APP_PASSWORD:
#         return False
#     try:
#         import smtplib
#         from email.mime.text import MIMEText
#         msg = MIMEText(body_text)
#         msg['Subject'] = subject
#         msg['From']    = GMAIL_USER
#         msg['To']      = to_email
#         with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
#             s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
#             s.send_message(msg)
#         log.info(f"[GMAIL] Sent: {subject[:60]} -> {to_email}")
#         return True
#     except Exception as e:
#         log.error(f"[GMAIL] Error: {e}")
#         return False


def send_email(to_email: str, subject: str, body_text: str,
               body_html: str = None) -> bool:
    """Unified send. SendGrid primary. Gmail secondary (uncomment above)."""
    if send_via_sendgrid(to_email, subject, body_text, body_html):
        return True
    # if send_via_gmail(to_email, subject, body_text):
    #     return True
    return False


# -- RECIPIENT RESOLUTION -----------------------------------------------------

def resolve_recipient(event: dict):
    audience = event.get("audience", "internal")
    if audience == "customer":
        pi_id = event.get("pi_id")
        if pi_id:
            email = get_customer_email(pi_id)
            if email:
                return email
        log.warning(
            f"audience=customer but no email for pi_id={event.get('pi_id')} "
            f"-- falling back to project lead"
        )
    return PROJECT_LEAD_EMAIL or None


# -- DISPATCH -----------------------------------------------------------------

def dispatch_event(event: dict) -> bool:
    """
    Attempt to send a single queued event.
    Uses subject/body DB columns if present; falls back to payload formatting.
    Returns True on success.
    """
    event_id   = event["id"]
    event_type = event["event_type"]
    priority   = event.get("priority", 2)

    db_mark_processing(event_id)

    recipient = resolve_recipient(event)
    if not recipient:
        log.error(f"No recipient for {event_id} ({event_type}) -- marking failed")
        db_mark_failed(event_id, "no_recipient")
        return False

    subject = event.get("subject") or ""
    body    = event.get("body") or ""

    if not subject or not body:
        try:
            payload = json.loads(event.get("payload") or "{}")
        except Exception:
            payload = {}
        subject, body = _format_legacy_event(event_type, payload)

    log.info(
        f"Dispatching P{priority} {event_type} -> {recipient} "
        f"(retry={event.get('retry_count', 0)})"
    )
    success = send_email(recipient, subject, body)

    if success:
        db_mark_sent(event_id)
    else:
        retry_count = (event.get("retry_count") or 0) + 1
        if retry_count >= MAX_RETRIES:
            db_mark_failed(event_id, f"max_retries_exceeded ({MAX_RETRIES})")
            log.error(
                f"Permanently failed after {MAX_RETRIES} attempts: "
                f"{event_type} -> {recipient}"
            )
        else:
            db_requeue(event_id, "send_failed")
            log.warning(
                f"Send failed, requeued ({retry_count}/{MAX_RETRIES}): {event_type}"
            )

    return success


def _format_legacy_event(event_type: str, payload: dict):
    """Format subject/body for events that predate subject/body columns."""
    if event_type == "silence_alert":
        pi_id     = payload.get("pi_id", "unknown")
        age_hours = payload.get("age_hours", 0)
        severity  = payload.get("severity", "WARN")
        subject = f"[Synthos {severity}] {pi_id} -- silent {age_hours:.1f}h"
        market_note = (
            "Market hours -- trades may be missed."
            if payload.get("market_hours")
            else "Outside market hours."
        )
        body = (
            f"Synthos Alert\n\nPi ID: {pi_id}\n"
            f"Status: SILENT -- {age_hours:.1f} hours since last heartbeat\n"
            f"Severity: {severity}\n\n{market_note}\n\n"
            f"Check Pi power, network, and agent logs."
        )
        return subject, body

    if event_type == "agent_error":
        pi_id = payload.get("pi_id", "unknown")
        error = payload.get("error", "Unknown error")
        subject = f"[Synthos WARN] Agent error on {pi_id}"
        body = (
            f"Synthos Alert -- Agent Error\n\nPi ID: {pi_id}\nError: {error}\n\n"
            f"Agent statuses:\n{json.dumps(payload.get('agents', {}), indent=2)}"
        )
        return subject, body

    # Generic fallback
    subject = f"[Synthos] {event_type.replace('_', ' ').title()}"
    body    = json.dumps(payload, indent=2)
    return subject, body


# -- LEGACY TRIGGER DRAIN -----------------------------------------------------

_legacy_drained = False

def drain_legacy_trigger_file() -> int:
    """
    One-time drain of scoop_trigger.json into DB queue at startup.
    After draining, the file is ignored permanently.
    """
    global _legacy_drained
    if _legacy_drained or not LEGACY_TRIGGER.exists():
        _legacy_drained = True
        return 0

    try:
        events = json.loads(LEGACY_TRIGGER.read_text())
    except Exception:
        _legacy_drained = True
        return 0

    pending = [e for e in events if e.get("status") in ("pending", "retry")]
    if not pending:
        _legacy_drained = True
        return 0

    migrated = 0
    for e in pending:
        try:
            event_type = e.get("type", "legacy_event")
            payload    = e.get("payload", {})
            pi_id      = e.get("pi_id") or (
                payload.get("pi_id") if isinstance(payload, dict) else None
            )
            eid = str(uuid.uuid4())
            with get_db() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO scoop_queue
                    (id, event_type, priority, audience, pi_id, payload,
                     source_agent, status)
                    VALUES (?, ?, 2, ?, ?, ?, 'legacy_trigger', 'PENDING')
                """, (eid, event_type, e.get("audience", "internal"),
                      pi_id, json.dumps(payload)))
            migrated += 1
        except Exception as ex:
            log.warning(f"Could not migrate legacy event: {ex}")

    if migrated:
        log.info(f"Drained {migrated} legacy event(s) from scoop_trigger.json -> DB")

    _legacy_drained = True
    return migrated


# -- MORNING REPORT -----------------------------------------------------------

_morning_report_sent_date = None

def maybe_send_morning_report() -> bool:
    global _morning_report_sent_date
    now   = now_et()
    today = now.date()

    if now.hour != MORNING_REPORT_HOUR:
        return False
    if _morning_report_sent_date == today:
        return False
    if not MORNING_REPORT.exists():
        return False

    try:
        report = json.loads(MORNING_REPORT.read_text())
    except Exception as e:
        log.error(f"Could not read morning_report.json: {e}")
        return False

    if report.get("date", "") != today.isoformat():
        return False

    subject, body = _format_morning_report(report)
    success = send_email(PROJECT_LEAD_EMAIL, subject, body)
    if success:
        _morning_report_sent_date = today
        log.info(f"Morning report delivered for {today}")
    return success


def _format_morning_report(report: dict):
    date    = report.get("date", "?")
    pv      = report.get("portfolio_value", 0)
    pnl     = report.get("realized_pnl", 0)
    wins    = report.get("wins", 0)
    losses  = report.get("losses", 0)
    trades  = report.get("trades_today", 0)
    summary = report.get("summary", "")
    sign    = "+" if pnl >= 0 else ""
    subject = f"[Synthos] Morning Report -- {date}"
    body = (
        f"Synthos Daily Report -- {date}\n\n"
        f"Portfolio:  ${pv:,.2f}\n"
        f"P&L today:  {sign}${pnl:,.2f}\n"
        f"Trades:     {trades} ({wins}W / {losses}L)\n\n"
        f"{summary}\n"
    )
    return subject, body


# -- MAIN LOOP ----------------------------------------------------------------

def run_loop() -> None:
    log.info(f"Scoop v{SYNTHOS_VERSION} started -- polling DB every {POLL_INTERVAL_SEC}s")

    if not PROJECT_LEAD_EMAIL:
        log.warning("PROJECT_LEAD_EMAIL not set -- internal alerts will not be delivered")
    if not SENDGRID_API_KEY:
        log.warning("SENDGRID_API_KEY not set -- no emails will be sent")

    # One-time legacy drain
    drain_legacy_trigger_file()

    while True:
        try:
            # Dispatch next highest-priority event
            event = db_get_next_event()
            if event:
                priority = event.get("priority", 2)
                log.info(
                    f"Processing P{priority} {event['event_type']} "
                    f"(retry={event.get('retry_count', 0)})"
                )
                dispatch_event(event)

                # Drain all remaining P0s without sleeping
                if priority == 0:
                    next_event = db_get_next_event()
                    while next_event and next_event.get("priority") == 0:
                        log.info(f"P0 drain: {next_event['event_type']}")
                        dispatch_event(next_event)
                        next_event = db_get_next_event()

            # Morning report
            maybe_send_morning_report()

            # Recover stuck PROCESSING events (crash recovery -- >2 min)
            try:
                stale = (
                    now_utc() - timedelta(minutes=2)
                ).isoformat().replace("+00:00", "Z")
                with get_db() as conn:
                    conn.execute("""
                        UPDATE scoop_queue
                        SET status='RETRY', retry_count=retry_count+1,
                            error_msg='stuck_in_processing'
                        WHERE status='PROCESSING' AND started_at < ?
                    """, (stale,))
            except Exception:
                pass

        except Exception as e:
            log.error(f"Scoop loop error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL_SEC)


# -- CLI ----------------------------------------------------------------------

def show_status() -> None:
    stats = db_queue_stats()
    print(f"\n{'='*50}")
    print(f"SCOOP v{SYNTHOS_VERSION} STATUS")
    print(f"{'='*50}")
    for status, count in sorted(stats.items()):
        print(f"  {status:<20} {count}")
    print(f"  {'SendGrid':<20} {'configured' if SENDGRID_API_KEY else 'NOT CONFIGURED'}")
    print(f"  {'Lead email':<20} {PROJECT_LEAD_EMAIL or 'NOT SET'}")
    print(f"{'='*50}\n")


def send_test_email() -> None:
    if not PROJECT_LEAD_EMAIL:
        print("PROJECT_LEAD_EMAIL not set -- cannot send test")
        return
    success = send_email(
        PROJECT_LEAD_EMAIL,
        "[Synthos] Test email from Scoop",
        f"Scoop v{SYNTHOS_VERSION} is configured and working.\n\n"
        f"Company Pi: {os.environ.get('PI_ID', 'unknown')}\n"
        f"Timestamp:  {now_iso()}\n"
    )
    print(f"Test email {'sent' if success else 'FAILED'} to {PROJECT_LEAD_EMAIL}")


# -- GRACEFUL SHUTDOWN --------------------------------------------------------

def handle_shutdown(signum, frame):
    log.info(f"Scoop received signal {signum} -- shutting down")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)


# -- ENTRY POINT --------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scoop -- Mail Agent")
    parser.add_argument("--status", action="store_true",
                        help="Show delivery queue status")
    parser.add_argument("--test", action="store_true",
                        help="Send test email to project lead")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.test:
        send_test_email()
    else:
        run_loop()
