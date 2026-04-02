"""
Scoop — Synthos Queue Drain Daemon
===================================
Runs on the Company Pi alongside company_server.py.
Polls scoop_queue in company.db, dispatches outbound alerts via Resend,
then marks items sent/failed.

Priority model:
    P0 (critical)  — dispatched immediately on next tick (5s poll)
    P1 (high)      — dispatched after 30s hold (allows brief dedup window)
    P2 (medium)    — dispatched after 5 min
    P3 (low)       — dispatched after 15 min

Recipient resolution (in order):
    1. payload.to_email         — agent explicitly set a recipient
    2. payload.customer_email   — agent included resolved customer email
    3. OPS_EMAIL env var        — falls back to ops/admin address
    4. ALERT_TO env var         — final fallback

Retry policy:
    Up to MAX_ATTEMPTS (default 3) tries before marking 'failed'.
    Exponential-ish back-off via MIN_RETRY_DELAY_S (default 60s between attempts).

.env required on Company Pi:
    RESEND_API_KEY=re_...
    ALERT_FROM=alerts@yourdomain.com
    ALERT_TO=ops@yourdomain.com

Optional:
    OPS_EMAIL=ops@yourdomain.com
    COMPANY_DB_PATH=data/company.db
    SCOOP_POLL_S=5
    SCOOP_MAX_ATTEMPTS=3
    SCOOP_RETRY_DELAY_S=60
    SCOOP_DRY_RUN=true         # log dispatch without actually sending

Run:
    python scoop.py
    # or daemonized:
    nohup python scoop.py >> logs/scoop.log 2>&1 &
"""

import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
RESEND_API_KEY    = os.getenv("RESEND_API_KEY", "")
ALERT_FROM        = os.getenv("ALERT_FROM", "alerts@example.com")
ALERT_TO          = os.getenv("ALERT_TO", "")
OPS_EMAIL         = os.getenv("OPS_EMAIL", ALERT_TO)
POLL_S            = int(os.getenv("SCOOP_POLL_S", 5))
MAX_ATTEMPTS      = int(os.getenv("SCOOP_MAX_ATTEMPTS", 3))
RETRY_DELAY_S     = int(os.getenv("SCOOP_RETRY_DELAY_S", 60))
DRY_RUN           = os.getenv("SCOOP_DRY_RUN", "").lower() in ("1", "true", "yes")

# Age (seconds) a pending item must be before Scoop will dispatch it.
# P0 dispatches immediately; higher priorities get a small hold window.
PRIORITY_HOLD_S = {
    0:    0,   # P0 CRITICAL — immediate
    1:   30,   # P1 HIGH     — 30s dedup window
    2:  300,   # P2 MED      — 5 min
    3:  900,   # P3 LOW      — 15 min
}

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))

# ── Shutdown flag ─────────────────────────────────────────────────────────────
_running = True


def _handle_signal(sig, frame):
    global _running
    print(f"\n[Scoop] Signal {sig} — draining current batch then stopping…")
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _eligible_items():
    """
    Return pending items whose priority hold window has elapsed,
    that haven't exceeded MAX_ATTEMPTS, ordered by priority then age.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    results = []
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM scoop_queue
                   WHERE status = 'pending'
                   AND   dispatch_attempts < ?
                   ORDER BY priority ASC, queued_at ASC
                   LIMIT 50""",
                (MAX_ATTEMPTS,),
            ).fetchall()
    except Exception as e:
        print(f"[Scoop] DB fetch error: {e}")
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    for row in rows:
        priority = row["priority"]
        hold_s   = PRIORITY_HOLD_S.get(priority, 900)
        try:
            queued_ts = datetime.fromisoformat(
                row["queued_at"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            queued_ts = 0
        age_s = now_ts - queued_ts
        if age_s >= hold_s:
            results.append(dict(row))

    return results


def _mark_sent(item_id: str):
    with _db_conn() as conn:
        conn.execute(
            "UPDATE scoop_queue SET status='sent', dispatched_at=?, error_msg=NULL "
            "WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), item_id),
        )


def _mark_failed(item_id: str, error: str, attempts: int):
    """Mark failed if max attempts reached, otherwise increment counter and stay pending."""
    if attempts >= MAX_ATTEMPTS:
        with _db_conn() as conn:
            conn.execute(
                "UPDATE scoop_queue "
                "SET status='failed', dispatch_attempts=?, dispatched_at=?, error_msg=? "
                "WHERE id=?",
                (attempts, datetime.now(timezone.utc).isoformat(), error[:500], item_id),
            )
        print(f"[Scoop] ✗ FAILED (max attempts) id={item_id[:8]} error={error[:80]}")
    else:
        with _db_conn() as conn:
            conn.execute(
                "UPDATE scoop_queue SET dispatch_attempts=?, error_msg=? WHERE id=?",
                (attempts, error[:500], item_id),
            )
        retry_in = RETRY_DELAY_S * attempts
        print(f"[Scoop] ↺ attempt {attempts}/{MAX_ATTEMPTS} — retry eligible in {retry_in}s  id={item_id[:8]}")


# ── Email dispatch ────────────────────────────────────────────────────────────
def _resolve_recipient(item: dict) -> str | None:
    """
    Resolve the To address for this queue item.

    Check order:
      1. payload.to_email         (agent-supplied, highest priority)
      2. payload.customer_email   (agent pre-resolved customer email)
      3. OPS_EMAIL env var        (ops override)
      4. ALERT_TO env var         (system default)
    """
    payload = {}
    try:
        raw = item.get("payload", "{}")
        payload = json.loads(raw) if raw else {}
    except Exception:
        pass

    for key in ("to_email", "customer_email"):
        addr = payload.get(key, "").strip()
        if addr:
            return addr

    return OPS_EMAIL.strip() or ALERT_TO.strip() or None


def _dispatch(item: dict) -> tuple[bool, str]:
    """
    Send item via Resend REST API.
    Returns (success: bool, error_message: str).
    """
    recipient = _resolve_recipient(item)
    if not recipient:
        return False, "No recipient resolved — set to_email in payload or ALERT_TO env var"

    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY not set"

    subject = item.get("subject", "(no subject)")
    body    = item.get("body", "")
    pi_id   = item.get("pi_id") or ""

    # Append a small footer with routing context
    footer = (
        f"\n\n---\n"
        f"Source: {item.get('source_agent','?')}  |  "
        f"Pi: {pi_id or 'unknown'}  |  "
        f"Priority: P{item.get('priority','?')}  |  "
        f"Event: {item.get('event_type','?')}"
    )
    text_body = body + footer

    if DRY_RUN:
        print(
            f"[Scoop] DRY RUN — would send to={recipient} "
            f"subject='{subject[:60]}' id={item['id'][:8]}"
        )
        return True, ""

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    f"Synthos Alerts <{ALERT_FROM}>",
                "to":      [recipient],
                "subject": subject,
                "text":    text_body,
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            return True, ""
        # Resend error body is JSON: {"name":"...", "message":"...", "statusCode":...}
        try:
            err_detail = r.json().get("message", r.text[:200])
        except Exception:
            err_detail = r.text[:200]
        return False, f"Resend HTTP {r.status_code}: {err_detail}"

    except requests.Timeout:
        return False, "Resend request timed out"
    except Exception as e:
        return False, f"Dispatch exception: {str(e)[:200]}"


# ── Main loop ─────────────────────────────────────────────────────────────────
def _drain_batch():
    """Fetch and dispatch one batch of eligible items."""
    items = _eligible_items()
    if not items:
        return 0

    sent = failed = skipped = 0
    for item in items:
        iid      = item["id"]
        attempts = item["dispatch_attempts"] + 1
        priority = item["priority"]
        label    = f"P{priority} {item['event_type']} from {item['source_agent']} id={iid[:8]}"

        ok, err = _dispatch(item)

        if ok:
            _mark_sent(iid)
            print(f"[Scoop] ✓ sent  {label}  → {_resolve_recipient(item)}")
            sent += 1
        else:
            _mark_failed(iid, err, attempts)
            failed += 1

    if sent or failed:
        print(f"[Scoop] batch complete — sent={sent} failed={failed}")

    return sent + failed


def run():
    """Main daemon loop."""
    print(f"[Scoop] Starting — poll={POLL_S}s  max_attempts={MAX_ATTEMPTS}  db={DB_PATH}")
    if DRY_RUN:
        print(f"[Scoop] ⚠  DRY RUN mode — emails will NOT be sent")
    if not RESEND_API_KEY:
        print(f"[Scoop] ⚠  RESEND_API_KEY not set — all dispatches will fail")
    if not ALERT_TO and not OPS_EMAIL:
        print(f"[Scoop] ⚠  ALERT_TO not set — items without payload.to_email will fail")

    # Validate DB is reachable before entering loop
    try:
        with _db_conn() as conn:
            conn.execute("SELECT 1 FROM scoop_queue LIMIT 1")
        print(f"[Scoop] DB connected OK")
    except Exception as e:
        print(f"[Scoop] ✗ Cannot reach DB at {DB_PATH}: {e}")
        print(f"[Scoop] Is company_server.py running? Start it first to initialise the DB.")
        sys.exit(1)

    tick = 0
    while _running:
        try:
            processed = _drain_batch()
            if processed == 0 and tick % 60 == 0:
                # Heartbeat log every ~5 minutes (60 ticks × 5s) when idle
                print(f"[Scoop] idle — queue clear")
        except Exception as e:
            print(f"[Scoop] Unhandled error in drain loop: {e}")

        tick += 1
        # Sleep in small increments so SIGTERM is caught quickly
        for _ in range(POLL_S):
            if not _running:
                break
            time.sleep(1)

    print(f"[Scoop] Stopped.")


if __name__ == "__main__":
    run()
