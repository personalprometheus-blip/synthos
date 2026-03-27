"""
sentinel.py — Sentinel > Interface Agent
Synthos Company Pi | /home/pi/synthos-company/agents/sentinel.py

Role:
  Receive heartbeats from retail Pis, monitor customer liveness,
  detect silence, and escalate to Scoop when customers go dark.

  Sentinel runs as a Flask HTTP server on port 5004.
  Retail Pis POST their heartbeat JSON here every hour during market hours.
  Sentinel is accountable for knowing if any customer Pi goes silent.

  Sentinel does NOT send emails. It writes alerts to suggestions.json
  and notifies Scoop via a trigger file. Scoop delivers the message.

Heartbeat endpoint:
  POST /heartbeat
  Headers: Authorization: Bearer <MONITOR_TOKEN>
  Body: {
    "pi_id": "retail-pi-01",
    "timestamp": "2026-03-24T14:30:00Z",
    "portfolio_value": 50000.00,
    "agents": {
      "trader":    {"status": "SUCCESS", "last_run": "..."},
      "research":  {"status": "SUCCESS", "last_run": "..."},
      "sentiment": {"status": "SUCCESS", "last_run": "..."}
    },
    "license_key": "synthos-retail-pi-01-...",
    "uptime_seconds": 864000
  }

Silence rules:
  >30 min   → Yellow (stale) in portal
  >4h       → WARN — check if market hours
  >4h during market hours → CRITICAL — alert Scoop immediately
  >45 days  → INACTIVE transition (Vault handles this)

Runs: Always-on Flask server
  @reboot sleep 60 && python3 /home/pi/synthos-company/agents/sentinel.py &

USAGE:
  python3 sentinel.py           # start server
  python3 sentinel.py --status  # show customer liveness summary
"""

import os
import sys
import json
import uuid
import shutil
import logging
import argparse
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Paths resolved dynamically — works for any username or install directory.
# Override with SYNTHOS_BASE_DIR env var if needed.

import sys as _sys
import os.path as _osp
_AGENTS_DIR = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in _sys.path:
    _sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import (
    BASE_DIR, DATA_DIR, LOGS_DIR, DB_PATH,
    SUGGESTIONS_FILE, SCOOP_TRIGGER, ENV_PATH,
)

LOG_FILE = LOGS_DIR / "interface_agent.log"

PORT = int(os.environ.get("SENTINEL_PORT", 5004))
ET   = ZoneInfo("America/New_York")

load_dotenv(ENV_PATH, override=True)
MONITOR_TOKEN = os.environ.get("MONITOR_TOKEN", "")

# Silence thresholds
STALE_MINUTES    = 30
WARN_HOURS       = 4
INACTIVE_DAYS    = 45

# Market hours (ET) — silence during these hours is more urgent
MARKET_OPEN  = (9, 30)
MARKET_CLOSE = (16, 0)

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s sentinel: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("sentinel")

# ── FLASK APP ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.logger.setLevel(logging.WARNING)   # suppress Flask request logs from console

# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id                TEXT UNIQUE NOT NULL,
                license_key          TEXT,
                customer_name        TEXT,
                email                TEXT,
                status               TEXT DEFAULT 'ACTIVE',
                created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_heartbeat       DATETIME,
                payment_status       TEXT DEFAULT 'PAID',
                github_fork_access   BOOLEAN DEFAULT 1,
                mail_alerts_enabled  BOOLEAN DEFAULT 1,
                archived_at          DATETIME
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heartbeats (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id            TEXT NOT NULL,
                timestamp        DATETIME NOT NULL,
                portfolio_value  REAL,
                agent_statuses   TEXT,
                uptime_seconds   INTEGER,
                received_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_heartbeats_pi_id
            ON heartbeats(pi_id, timestamp)
        """)
        conn.commit()
    log.info("Schema verified")


# ── TIME HELPERS ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def parse_ts(ts_str: str) -> datetime:
    """Parse ISO timestamp, always return UTC-aware datetime."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── HEARTBEAT PROCESSING ──────────────────────────────────────────────────────

def validate_token(auth_header: str) -> bool:
    if not MONITOR_TOKEN:
        log.warning("MONITOR_TOKEN not configured — all requests accepted (insecure)")
        return True
    if not auth_header:
        return False
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return parts[1].strip() == MONITOR_TOKEN


def register_customer_if_new(conn: sqlite3.Connection, pi_id: str,
                              license_key: str) -> None:
    """Auto-register a Pi on first heartbeat if not already in customers table."""
    existing = conn.execute(
        "SELECT id FROM customers WHERE pi_id=?", (pi_id,)
    ).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO customers (pi_id, license_key, status, created_at)
            VALUES (?, ?, 'ACTIVE', ?)
        """, (pi_id, license_key, now_iso()))
        conn.commit()
        log.info(f"Auto-registered new customer Pi: {pi_id}")


def write_heartbeat(conn: sqlite3.Connection, pi_id: str,
                    payload: dict) -> None:
    """Write heartbeat record and update customer last_heartbeat."""
    ts = payload.get("timestamp", now_iso())

    conn.execute("""
        INSERT INTO heartbeats (pi_id, timestamp, portfolio_value, agent_statuses, uptime_seconds)
        VALUES (?, ?, ?, ?, ?)
    """, (
        pi_id,
        ts,
        payload.get("portfolio_value"),
        json.dumps(payload.get("agents", {})),
        payload.get("uptime_seconds"),
    ))

    conn.execute("""
        UPDATE customers SET last_heartbeat=?, status='ACTIVE'
        WHERE pi_id=?
    """, (ts, pi_id))

    conn.commit()


def check_agent_errors(pi_id: str, agents: dict) -> list[str]:
    """
    Scan heartbeat agent statuses for errors.
    Returns list of error descriptions.
    """
    errors = []
    for agent_name, status in agents.items():
        if isinstance(status, dict):
            s = status.get("status", "")
            if s in ("ERROR", "FAILED", "CRASHED"):
                errors.append(
                    f"{agent_name} reported {s} on {pi_id}"
                )
    return errors


# ── SILENCE MONITORING ────────────────────────────────────────────────────────

def check_all_silence() -> None:
    """
    Background thread: check all active customers for silence.
    Runs every 15 minutes.
    """
    while True:
        try:
            with get_db() as conn:
                customers = conn.execute("""
                    SELECT pi_id, last_heartbeat, status, mail_alerts_enabled
                    FROM customers
                    WHERE status IN ('ACTIVE', 'INACTIVE')
                """).fetchall()

            now = now_utc()
            for customer in customers:
                pi_id          = customer["pi_id"]
                last_hb_str    = customer["last_heartbeat"]
                mail_enabled   = customer["mail_alerts_enabled"]

                if not last_hb_str:
                    continue   # never sent a heartbeat — not yet active

                last_hb = parse_ts(last_hb_str)
                age_hours = (now - last_hb).total_seconds() / 3600

                if age_hours > WARN_HOURS:
                    severity = "CRITICAL" if is_market_hours() else "WARN"
                    _alert_silence(pi_id, age_hours, severity, mail_enabled)

        except Exception as e:
            log.error(f"Silence check error: {e}")

        import time
        time.sleep(900)   # check every 15 minutes


def _alert_silence(pi_id: str, age_hours: float, severity: str,
                   mail_enabled: bool) -> None:
    """
    Write a silence alert to suggestions.json.
    Scoop picks it up and sends the email.
    Deduplicates — won't re-alert for the same Pi within 4 hours.
    """
    # Check if we already alerted for this Pi recently
    alert_cache_path = BASE_DIR / "data" / f".sentinel_alerted_{pi_id}"
    if alert_cache_path.exists():
        last_alert = datetime.fromisoformat(alert_cache_path.read_text().strip())
        if (now_utc() - last_alert.replace(tzinfo=timezone.utc)).total_seconds() < 14400:
            return   # already alerted in last 4 hours

    try:
        if SUGGESTIONS_FILE.exists():
            data = json.loads(SUGGESTIONS_FILE.read_text())
        else:
            data = {"version": "1.0", "suggestions": []}

        message = (
            f"{pi_id} has been silent for {age_hours:.1f} hours. "
            f"{'Market hours — trades may be missed.' if is_market_hours() else 'Outside market hours.'} "
            f"Last heartbeat recorded in company.db."
        )

        suggestion = {
            "id":        str(uuid.uuid4()),
            "timestamp": now_iso(),
            "agent":     "Sentinel",
            "category":  "automation",
            "title":     f"{severity}: {pi_id} silent {age_hours:.1f}h",
            "description": message,
            "impact": {
                "tokens_saved_per_week":    None,
                "execution_time_saved":     None,
                "risk_level":               severity,
                "affected_component":       f"Retail Pi — {pi_id}",
                "affected_customers_count": 1,
                "estimated_improvement":    "Customer Pi restored to active monitoring",
            },
            "implementation": {
                "effort":                "Immediate — customer outreach or Pi check",
                "complexity":           "SIMPLE",
                "approver_needed":      "you",
                "trial_run_recommended": False,
                "breaking_changes":     False,
                "rollback_difficulty":  "EASY",
            },
            "details": {
                "root_cause":           f"{pi_id} last heartbeat was {age_hours:.1f}h ago",
                "solution_approach":    "Check Pi power, network, and agent logs",
                "alternative_approaches": [],
                "dependencies":         [],
                "metrics_to_track":     ["Heartbeat resumed"],
            },
            "status":            "pending",
            "status_updated_at": now_iso(),
            "approver_notes":    None,
            "implementation_status": None,
            "implementation_notes":  None,
        }

        data["suggestions"].append(suggestion)
        data["last_updated"] = now_iso()

        if SUGGESTIONS_FILE.exists():
            shutil.copy2(SUGGESTIONS_FILE, SUGGESTIONS_FILE.with_suffix(".json.backup"))
        SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2))

        # Trigger Scoop if mail alerts are enabled for this customer
        if mail_enabled:
            _trigger_scoop("silence_alert", {
                "pi_id":      pi_id,
                "age_hours":  age_hours,
                "severity":   severity,
                "message":    message,
            })

        # Cache alert timestamp
        alert_cache_path.write_text(now_utc().isoformat())

        log.warning(f"Silence alert filed: {pi_id} — {age_hours:.1f}h silent [{severity}]")

    except Exception as e:
        log.error(f"Failed to file silence alert for {pi_id}: {e}")


def _trigger_scoop(event_type: str, payload: dict) -> None:
    """
    Write an event to scoop_trigger.json for Scoop to pick up.
    Scoop polls this file and sends emails for each pending event.
    """
    try:
        if SCOOP_TRIGGER.exists():
            events = json.loads(SCOOP_TRIGGER.read_text())
        else:
            events = []

        events.append({
            "id":         str(uuid.uuid4()),
            "type":       event_type,
            "payload":    payload,
            "created_at": now_iso(),
            "status":     "pending",
        })

        SCOOP_TRIGGER.write_text(json.dumps(events, indent=2))
    except Exception as e:
        log.warning(f"Could not trigger Scoop: {e}")


# ── INACTIVE TRANSITION ───────────────────────────────────────────────────────

def check_inactive_transitions() -> None:
    """
    Background thread: transition customers to INACTIVE after 45 days silence.
    Runs once per day. Vault handles the formal archival process.
    Sentinel just sets the status flag and notifies Vault.
    """
    import time
    while True:
        time.sleep(86400)   # once per day
        try:
            cutoff = (now_utc() - timedelta(days=INACTIVE_DAYS)).isoformat()
            with get_db() as conn:
                going_inactive = conn.execute("""
                    SELECT pi_id FROM customers
                    WHERE status='ACTIVE'
                    AND last_heartbeat < ?
                """, (cutoff,)).fetchall()

                for row in going_inactive:
                    conn.execute("""
                        UPDATE customers SET status='INACTIVE' WHERE pi_id=?
                    """, (row["pi_id"],))
                    log.info(f"Transitioned to INACTIVE: {row['pi_id']} (45 days silence)")

                if going_inactive:
                    conn.commit()
                    # Notify Vault to handle formal archival tracking
                    _trigger_scoop("customer_inactive", {
                        "pi_ids": [r["pi_id"] for r in going_inactive],
                        "reason": f"No heartbeat for {INACTIVE_DAYS} days",
                    })

        except Exception as e:
            log.error(f"Inactive transition check error: {e}")


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/heartbeat", methods=["POST"])
def receive_heartbeat():
    # Auth
    auth = request.headers.get("Authorization", "")
    if not validate_token(auth):
        log.warning(f"Rejected heartbeat — invalid token from {request.remote_addr}")
        return jsonify({"error": "Unauthorized"}), 401

    # Parse
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    pi_id = payload.get("pi_id", "").strip()
    if not pi_id:
        return jsonify({"error": "pi_id required"}), 400

    license_key = payload.get("license_key", "")
    agents      = payload.get("agents", {})

    # Write to database
    try:
        with get_db() as conn:
            register_customer_if_new(conn, pi_id, license_key)
            write_heartbeat(conn, pi_id, payload)
    except sqlite3.OperationalError as e:
        log.error(f"DB error writing heartbeat from {pi_id}: {e}")
        return jsonify({"error": "Service temporarily unavailable"}), 503

    log.info(
        f"Heartbeat received: {pi_id} | "
        f"portfolio=${payload.get('portfolio_value', 0):,.0f} | "
        f"uptime={payload.get('uptime_seconds', 0)//3600}h"
    )

    # Check for agent errors in the heartbeat payload
    errors = check_agent_errors(pi_id, agents)
    for err in errors:
        log.warning(f"Agent error in heartbeat: {err}")
        _trigger_scoop("agent_error_in_heartbeat", {
            "pi_id":   pi_id,
            "error":   err,
            "agents":  agents,
        })

    # Clear any stale silence alert cache for this Pi (it's back online)
    alert_cache = BASE_DIR / "data" / f".sentinel_alerted_{pi_id}"
    if alert_cache.exists():
        alert_cache.unlink()
        log.info(f"Silence cleared: {pi_id} is back online")

    return jsonify({"status": "ok", "pi_id": pi_id}), 200


@app.route("/health", methods=["GET"])
def health():
    """Simple liveness probe for monitoring."""
    return jsonify({
        "status":      "ok",
        "agent":       "Sentinel",
        "timestamp":   now_iso(),
        "market_hours": is_market_hours(),
    }), 200


@app.route("/customers", methods=["GET"])
def list_customers():
    """
    Internal endpoint — list all customers with liveness status.
    Used by the command portal left sidebar.
    Requires MONITOR_TOKEN.
    """
    auth = request.headers.get("Authorization", "")
    if not validate_token(auth):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        now = now_utc()
        with get_db() as conn:
            customers = conn.execute("""
                SELECT pi_id, status, last_heartbeat, portfolio_value,
                       mail_alerts_enabled, payment_status
                FROM customers
                WHERE status != 'ARCHIVED'
                ORDER BY last_heartbeat DESC
            """).fetchall()

        result = []
        for c in customers:
            lh = c["last_heartbeat"]
            if lh:
                age_min = (now - parse_ts(lh)).total_seconds() / 60
                if age_min < STALE_MINUTES:
                    liveness = "online"
                elif age_min < WARN_HOURS * 60:
                    liveness = "stale"
                else:
                    liveness = "offline"
            else:
                liveness = "never"
                age_min  = None

            result.append({
                "pi_id":          c["pi_id"],
                "status":         c["status"],
                "liveness":       liveness,
                "last_heartbeat": lh,
                "age_minutes":    round(age_min, 1) if age_min else None,
                "payment_status": c["payment_status"],
            })

        return jsonify({"customers": result, "count": len(result)}), 200

    except Exception as e:
        log.error(f"Error listing customers: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status() -> None:
    """Print customer liveness summary to terminal."""
    now = now_utc()
    try:
        with get_db() as conn:
            customers = conn.execute("""
                SELECT pi_id, status, last_heartbeat, payment_status
                FROM customers WHERE status != 'ARCHIVED'
                ORDER BY last_heartbeat DESC
            """).fetchall()

        print(f"\n{'=' * 60}")
        print(f"SENTINEL STATUS — {now.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"Market hours: {'YES' if is_market_hours() else 'NO'}")
        print(f"{'=' * 60}")

        for c in customers:
            lh = c["last_heartbeat"]
            if lh:
                age_min = (now - parse_ts(lh)).total_seconds() / 60
                if age_min < STALE_MINUTES:
                    icon, label = "●", f"{age_min:.0f}m ago"
                elif age_min < WARN_HOURS * 60:
                    icon, label = "◐", f"{age_min/60:.1f}h ago (stale)"
                else:
                    icon, label = "○", f"{age_min/60:.1f}h ago (SILENT)"
            else:
                icon, label = "—", "never"

            print(f"  {icon} {c['pi_id']:20} {c['status']:10} {label}")

        print(f"{'=' * 60}\n")

    except Exception as e:
        print(f"Error: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sentinel — Interface Agent")
    parser.add_argument("--status", action="store_true",
                        help="Show customer liveness summary")
    args = parser.parse_args()

    ensure_schema()

    if args.status:
        show_status()
    else:
        # Start background threads
        silence_thread = threading.Thread(
            target=check_all_silence, daemon=True, name="silence-monitor"
        )
        inactive_thread = threading.Thread(
            target=check_inactive_transitions, daemon=True, name="inactive-monitor"
        )
        silence_thread.start()
        inactive_thread.start()

        log.info(f"Sentinel starting on port {PORT}")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
