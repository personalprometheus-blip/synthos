"""
Synthos Monitor Server
=====================
Runs on a dedicated Pi. Receives heartbeats from all Synthos instances,
serves a command console dashboard, and sends Resend alerts when a Pi goes silent.

.env required:
    RESEND_API_KEY=re_...
    ALERT_FROM=alerts@yourdomain.com
    ALERT_TO=you@youremail.com
    SECRET_TOKEN=some_random_string
    PORT=5000

Client Pi .env:
    MONITOR_URL=http://your-monitor-ip:5000
    MONITOR_TOKEN=same_random_string_as_above
    PI_ID=synthos-pi-1

Heartbeat POST body (JSON):
    {
        "pi_id": "synthos-pi-1",
        "portfolio": 1042.50,
        "agents": { "trend": "active", "momentum": "idle" },
        "email": "customer@example.com",       # optional, stored on first seen
        "label": "John's Pi"                   # optional display name
    }
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, render_template_string, redirect, session, url_for, make_response
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload limit

# ── Config ────────────────────────────────────────────────────────────────────
RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
ALERT_FROM           = os.getenv("ALERT_FROM", "alerts@example.com")
ALERT_TO             = os.getenv("ALERT_TO", "you@example.com")
# SECRET_TOKEN is the server-side env var name.
# MONITOR_TOKEN is the client-side env var name — accept both so
# operators who set only one side don't get silent 401s.
SECRET_TOKEN         = os.getenv("SECRET_TOKEN") or os.getenv("MONITOR_TOKEN", "")
PORT                 = int(os.getenv("PORT", 5050))
CF_ADMIN_EMAIL = os.getenv("OPERATOR_EMAIL", "").lower().strip()
ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", CF_ADMIN_EMAIL).lower().strip()
ADMIN_PW_HASH  = os.getenv("ADMIN_PASSWORD_HASH", "")
app.secret_key = os.getenv("FLASK_SECRET_KEY", SECRET_TOKEN or __import__('os').urandom(24).hex())
COMPANY_URL          = os.getenv("COMPANY_URL", "").rstrip("/")
SILENCE_WINDOW_HOURS = 4
ALERT_START_HOUR     = 8
ALERT_END_HOUR       = 20
ET                   = ZoneInfo("America/New_York")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
REGISTRY_FILE = os.path.join(DATA_DIR, ".monitor_registry.json")

# ── Company DB Path ──────────────────────────────────────────────────────────
DB_PATH  = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))
LOG_DIR  = os.path.join(os.path.dirname(_HERE), "logs")   # synthos_build/logs/

# ── Sentinel Display Bridge ───────────────────────────────────────────────────
_display_bridge = None
try:
    import sentinel_bridge as _display_bridge
    _display_bridge.start_watcher()  # Start drop folder monitor
except ImportError:
    pass  # sentinel_bridge.py not present — display features disabled

DISPLAY_DROP_DIR = os.path.join(os.path.dirname(_HERE), "data", "display_uploads")
os.makedirs(DISPLAY_DROP_DIR, exist_ok=True)


# ── Company Database ──────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    """Thread-safe SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create company node database schema. Idempotent — safe to call on every startup."""
    with _db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scoop_queue (
                id                TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                priority          INTEGER NOT NULL DEFAULT 1,
                subject           TEXT NOT NULL,
                body              TEXT NOT NULL,
                source_agent      TEXT NOT NULL,
                pi_id             TEXT,
                audience          TEXT NOT NULL DEFAULT 'customer',
                correlation_id    TEXT,
                related_ticker    TEXT,
                related_signal_id TEXT,
                payload           TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                queued_at         TEXT NOT NULL,
                dispatched_at     TEXT,
                dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                error_msg         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scoop_status   ON scoop_queue(status);
            CREATE INDEX IF NOT EXISTS idx_scoop_priority ON scoop_queue(priority, queued_at);
            CREATE INDEX IF NOT EXISTS idx_scoop_pi       ON scoop_queue(pi_id, queued_at);

            CREATE TABLE IF NOT EXISTS pi_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id           TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                portfolio_value REAL,
                cash            REAL,
                realized_gains  REAL,
                open_positions  INTEGER,
                trades_today    INTEGER,
                operating_mode  TEXT,
                trading_mode    TEXT,
                kill_switch     INTEGER,
                payload         TEXT,
                recorded_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pi_events_pi   ON pi_events(pi_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_pi_events_type ON pi_events(event_type, recorded_at);
        """)
    print(f"[Monitor] Company DB initialized: {DB_PATH}")


# ── State ─────────────────────────────────────────────────────────────────────
pi_registry   = {}
registry_lock = threading.Lock()

# ── Global Commands ──────────────────────────────────────────────────────────
# Pending commands are stored per-pi_id and popped on next heartbeat response.
pending_commands = {}          # {pi_id: [{"type": "...", "value": "..."}]}
commands_lock    = threading.Lock()


def save_registry():
    """Persist registry to disk so Pi state survives monitor restarts."""
    try:
        import json as _json
        serializable = {}
        for pi_id, data in pi_registry.items():
            entry = dict(data)
            entry['last_seen']  = data['last_seen'].isoformat()
            entry['first_seen'] = data.get('first_seen', data['last_seen']).isoformat()
            if 'last_report' in entry:
                entry['last_report'] = entry['last_report']  # already serializable
            serializable[pi_id] = entry
        with open(REGISTRY_FILE, 'w') as f:
            _json.dump(serializable, f, indent=2)
    except Exception as e:
        print(f"[Registry] Save failed: {e}")


def load_registry():
    """Load persisted registry on startup — restores Pi list after reboot."""
    import json as _json
    if not os.path.exists(REGISTRY_FILE):
        return
    try:
        with open(REGISTRY_FILE, 'r') as f:
            data = _json.load(f)
        for pi_id, entry in data.items():
            pi_registry[pi_id] = {
                **entry,
                'last_seen':  datetime.fromisoformat(entry['last_seen']).replace(tzinfo=timezone.utc)
                              if entry['last_seen'].endswith('+00:00') or 'Z' in entry['last_seen']
                              else datetime.fromisoformat(entry['last_seen']).replace(tzinfo=timezone.utc),
                'first_seen': datetime.fromisoformat(entry.get('first_seen', entry['last_seen'])).replace(tzinfo=timezone.utc),
                'alerted':    False,  # reset on restart — re-evaluate silence fresh
            }
        print(f"[Registry] Loaded {len(pi_registry)} Pi(s) from disk")
    except Exception as e:
        print(f"[Registry] Load failed (starting fresh): {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)

def in_alert_window():
    now_et = datetime.now(ET)
    return ALERT_START_HOUR <= now_et.hour < ALERT_END_HOUR

def send_alert(pi_id, last_seen):
    if not RESEND_API_KEY:
        print(f"[ALERT] No Resend key — would have alerted for {pi_id}")
        return
    import json as _json
    elapsed = round((now_utc() - last_seen).total_seconds() / 3600, 1)
    try:
        import requests as _req
        r = _req.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'from':    ALERT_FROM,
                'to':      [ALERT_TO],
                'subject': f"⚠️ Synthos Alert — {pi_id} is silent",
                'html': (
                    f"<h2>Synthos Monitor Alert</h2>"
                    f"<p><strong>{pi_id}</strong> has not sent a heartbeat in "
                    f"<strong>{elapsed} hours</strong>.</p>"
                    f"<p>Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
                    f"<p>Check your Pi.</p>"
                ),
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            print(f"[ALERT] Sent alert for {pi_id}")
        else:
            print(f"[ALERT] Resend error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[ALERT] Resend error: {e}")

def pi_status(data):
    """Returns 'active', 'fault', or 'offline'"""
    age = (now_utc() - data["last_seen"]).total_seconds()
    if age > SILENCE_WINDOW_HOURS * 3600:
        return "offline"
    agents = data.get("agents", {})
    if any(v == "fault" or v == "error" for v in agents.values()):
        return "fault"
    return "active"


# ── Company Auth Helpers ─────────────────────────────────────────────────────
def _cf_authorized():
    """Trust Cloudflare Access — checks Cf-Access-Authenticated-User-Email header."""
    if not CF_ADMIN_EMAIL:
        return False
    cf_email = request.headers.get("Cf-Access-Authenticated-User-Email", "").lower().strip()
    return cf_email == CF_ADMIN_EMAIL


def _token_authorized():
    """Check X-Token header, ?token= query param, or cookie. Timing-safe."""
    import hmac as _hmac_mod
    token = (
        request.headers.get("X-Token", "")
        or request.args.get("token", "")
        or request.cookies.get("company_token", "")
    )
    if not SECRET_TOKEN or not token:
        return False
    return _hmac_mod.compare_digest(token, SECRET_TOKEN)


def _session_authorized():
    """Check Flask session login."""
    return session.get("logged_in") is True

def _authorized():
    """Browser routes: accept session login, Cloudflare Access, or SECRET_TOKEN."""
    return _session_authorized() or _cf_authorized() or _token_authorized()


# ── Silence detection loop ────────────────────────────────────────────────────
def silence_detector():
    while True:
        time.sleep(300)
        if not in_alert_window():
            continue
        with registry_lock:
            for pi_id, data in pi_registry.items():
                age_hours = (now_utc() - data["last_seen"]).total_seconds() / 3600
                if age_hours >= SILENCE_WINDOW_HOURS and not data["alerted"]:
                    send_alert(pi_id, data["last_seen"])
                    data["alerted"] = True
                elif age_hours < SILENCE_WINDOW_HOURS and data["alerted"]:
                    data["alerted"] = False


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    pi_id = data.get("pi_id", "unknown")

    with registry_lock:
        existing = pi_registry.get(pi_id, {})
        pi_registry[pi_id] = {
            "last_seen":         now_utc(),
            "first_seen":        existing.get("first_seen", now_utc()),
            "alerted":           False,
            # Identity
            "label":             data.get("label",          existing.get("label", pi_id)),
            "email":             data.get("email",          existing.get("email", "")),
            "pi_id":             pi_id,
            # Summary stats
            "portfolio_value":   data.get("portfolio_value", data.get("portfolio", existing.get("portfolio_value", 0.0))),
            "cash":              data.get("cash",            existing.get("cash", 0.0)),
            "realized_gains":    data.get("realized_gains",  existing.get("realized_gains", 0.0)),
            "open_positions":    data.get("open_positions",  existing.get("open_positions", 0)),
            "positions":         data.get("positions",       existing.get("positions", [])),
            "pending_approvals": data.get("pending_approvals", existing.get("pending_approvals", 0)),
            "urgent_flags":      data.get("urgent_flags",   existing.get("urgent_flags", 0)),
            "trades_today":      data.get("trades_today",   existing.get("trades_today", 0)),
            # System
            "agents":            data.get("agents",         existing.get("agents", {})),
            "uptime":            data.get("uptime",         existing.get("uptime", None)),
            "uptime_secs":       data.get("uptime_secs",    existing.get("uptime_secs", 0)),
            "operating_mode":    data.get("operating_mode", existing.get("operating_mode", "SUPERVISED")),
            "trading_mode":      data.get("trading_mode",   existing.get("trading_mode", "PAPER")),
            "kill_switch":       data.get("kill_switch",    existing.get("kill_switch", False)),
            "last_errors":       data.get("last_errors",    existing.get("last_errors", [])),
            # Hardware metrics
            "cpu_percent":    data.get("cpu_percent",    existing.get("cpu_percent")),
            "cpu_count":      data.get("cpu_count",      existing.get("cpu_count")),
            "load_avg":       data.get("load_avg",        existing.get("load_avg")),
            "ram_percent":    data.get("ram_percent",    existing.get("ram_percent")),
            "ram_total_gb":   data.get("ram_total_gb",   existing.get("ram_total_gb")),
            "ram_used_gb":    data.get("ram_used_gb",    existing.get("ram_used_gb")),
            "ram_avail_gb":   data.get("ram_avail_gb",   existing.get("ram_avail_gb")),
            "ram_cached_gb":  data.get("ram_cached_gb",  existing.get("ram_cached_gb")),
            "disk_percent":   data.get("disk_percent",   existing.get("disk_percent")),
            "disk_total_gb":  data.get("disk_total_gb",  existing.get("disk_total_gb")),
            "disk_used_gb":   data.get("disk_used_gb",   existing.get("disk_used_gb")),
            "disk_free_gb":   data.get("disk_free_gb",   existing.get("disk_free_gb")),
            "net_bytes_sent": data.get("net_bytes_sent", existing.get("net_bytes_sent")),
            "net_bytes_recv": data.get("net_bytes_recv", existing.get("net_bytes_recv")),
            "cpu_temp":       data.get("cpu_temp",       existing.get("cpu_temp")),
            # History — keep last 48 heartbeat samples for time-series graphs
            "history":           (existing.get("history", []) + [{
                "t":   now_utc().isoformat(),
                "v":   data.get("portfolio_value", data.get("portfolio", 0.0)),
                "cpu": data.get("cpu_percent"),
                "ram": data.get("ram_percent"),
            }])[-48:],
        }
        save_registry()

    # Deliver any pending global commands to this Pi
    with commands_lock:
        cmds = pending_commands.pop(pi_id, [])

    return jsonify({"status": "ok", "commands": cmds}), 200


@app.route("/api/pi/<pi_id>", methods=["GET"])
def api_pi_detail(pi_id):
    """Full detail for a single Pi — used by modal on click."""
    with registry_lock:
        data = pi_registry.get(pi_id)
    if not data:
        return jsonify({"error": "Pi not found"}), 404
    age_secs = int((now_utc() - data["last_seen"]).total_seconds())
    return jsonify({
        **data,
        "last_seen":  data["last_seen"].isoformat(),
        "first_seen": data["first_seen"].isoformat(),
        "age_secs":   age_secs,
        "status":     pi_status(data),
    }), 200


@app.route("/api/status", methods=["GET"])
def api_status():
    with registry_lock:
        out = {}
        for pi_id, data in pi_registry.items():
            age_secs = int((now_utc() - data["last_seen"]).total_seconds())
            out[pi_id] = {
                "pi_id":             pi_id,
                "label":             data.get("label", pi_id),
                "email":             data.get("email", ""),
                "last_seen":         data["last_seen"].isoformat(),
                "age_secs":          age_secs,
                "status":            pi_status(data),
                "portfolio_value":   data.get("portfolio_value", data.get("portfolio", 0.0)),
                "cash":              data.get("cash", 0.0),
                "realized_gains":    data.get("realized_gains", 0.0),
                "open_positions":    data.get("open_positions", 0),
                "pending_approvals": data.get("pending_approvals", 0),
                "urgent_flags":      data.get("urgent_flags", 0),
                "trades_today":      data.get("trades_today", 0),
                "agents":            data.get("agents", {}),
                "uptime":            data.get("uptime", None),
                "operating_mode":    data.get("operating_mode", "SUPERVISED"),
                "trading_mode":      data.get("trading_mode", "PAPER"),
                "kill_switch":       data.get("kill_switch", False),
                "cpu_percent":    data.get("cpu_percent"),
                "cpu_count":      data.get("cpu_count"),
                "load_avg":       data.get("load_avg"),
                "ram_percent":    data.get("ram_percent"),
                "ram_total_gb":   data.get("ram_total_gb"),
                "ram_used_gb":    data.get("ram_used_gb"),
                "ram_avail_gb":   data.get("ram_avail_gb"),
                "ram_cached_gb":  data.get("ram_cached_gb"),
                "disk_percent":   data.get("disk_percent"),
                "disk_total_gb":  data.get("disk_total_gb"),
                "disk_used_gb":   data.get("disk_used_gb"),
                "disk_free_gb":   data.get("disk_free_gb"),
                "net_bytes_sent": data.get("net_bytes_sent"),
                "net_bytes_recv": data.get("net_bytes_recv"),
                "cpu_temp":       data.get("cpu_temp"),
                "history":        data.get("history", []),
            }
    return jsonify(out), 200


@app.route("/api/delete/<pi_id>", methods=["DELETE"])
def delete_pi(pi_id):
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    with registry_lock:
        if pi_id in pi_registry:
            del pi_registry[pi_id]
            save_registry()
            return jsonify({"deleted": pi_id}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/report", methods=["POST"])
def receive_report():
    """
    Receive a daily performance report POST from a Synthos Pi.
    Stores the latest report per pi_id for display in the console.
    Client Pi posts this at end of trading day with portfolio summary.

    Expected JSON body:
    {
        "pi_id": "synthos-pi-1",
        "date": "2026-03-22",
        "portfolio_value": 107.34,
        "realized_pnl": 4.21,
        "open_positions": 2,
        "trades_today": 1,
        "wins": 1,
        "losses": 0,
        "summary": "Free-text summary from agent"
    }
    """
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    pi_id = data.get("pi_id", "unknown")

    with registry_lock:
        if pi_id not in pi_registry:
            pi_registry[pi_id] = {
                "last_seen":  now_utc(),
                "portfolio":  data.get("portfolio_value", 0.0),
                "agents":     {},
                "email":      "",
                "label":      pi_id,
                "alerted":    False,
                "first_seen": now_utc(),
            }
        pi_registry[pi_id]["last_report"] = {
            "received_at":    now_utc().isoformat(),
            "date":           data.get("date", now_utc().strftime("%Y-%m-%d")),
            "portfolio_value": data.get("portfolio_value", 0.0),
            "realized_pnl":   data.get("realized_pnl", 0.0),
            "open_positions": data.get("open_positions", 0),
            "trades_today":   data.get("trades_today", 0),
            "wins":           data.get("wins", 0),
            "losses":         data.get("losses", 0),
            "summary":        data.get("summary", ""),
        }

    return jsonify({"status": "ok"}), 200


@app.route("/api/reports", methods=["GET"])
def api_reports():
    """Return latest daily report for each Pi."""
    with registry_lock:
        out = {}
        for pi_id, data in pi_registry.items():
            if "last_report" in data:
                out[pi_id] = data["last_report"]
    return jsonify(out), 200


@app.route("/api/enqueue", methods=["POST"])
def api_enqueue():
    """
    Receive a Scoop queue event from a retail Pi agent or monitor proxy.
    Auth: X-Token header must match SECRET_TOKEN.
    Required fields: event_type, priority, subject, body, source_agent
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    required = ["event_type", "priority", "subject", "body", "source_agent"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        priority = int(data["priority"])
    except (ValueError, TypeError):
        return jsonify({"error": "priority must be an integer 0-3"}), 400

    if priority not in (0, 1, 2, 3):
        return jsonify({"error": "priority must be 0, 1, 2, or 3"}), 400

    eid       = str(uuid.uuid4())
    queued_at = datetime.now(timezone.utc).isoformat()
    payload   = data.get("payload", {})

    try:
        with _db_conn() as conn:
            conn.execute(
                """INSERT INTO scoop_queue
                   (id, event_type, priority, subject, body, source_agent,
                    pi_id, audience, correlation_id, related_ticker,
                    related_signal_id, payload, status, queued_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eid,
                    str(data["event_type"]),
                    priority,
                    str(data["subject"]),
                    str(data["body"]),
                    str(data["source_agent"]),
                    data.get("pi_id"),
                    data.get("audience", "customer"),
                    data.get("correlation_id"),
                    data.get("related_ticker"),
                    data.get("related_signal_id"),
                    json.dumps(payload) if isinstance(payload, dict) else "{}",
                    "pending",
                    queued_at,
                ),
            )
        print(
            f"[ENQUEUE] {data['event_type']} P{priority} from {data['source_agent']} "
            f"pi={data.get('pi_id', '?')} id={eid[:8]}"
        )
        return jsonify({"ok": True, "id": eid, "priority": priority}), 200

    except Exception as e:
        print(f"[ENQUEUE] DB write failed: {e}")
        return jsonify({"ok": False, "error": f"DB write failed: {str(e)[:120]}"}), 500

# ── Global Command Routes ────────────────────────────────────────────────────
def _queue_command(cmd_type, value, targets="all"):
    """Queue a command for target Pis. Delivered on next heartbeat response."""
    cmd = {"type": cmd_type, "value": value, "queued_at": now_utc().isoformat()}
    with commands_lock:
        if targets == "all":
            with registry_lock:
                target_ids = list(pi_registry.keys())
        else:
            target_ids = targets if isinstance(targets, list) else [targets]
        for pid in target_ids:
            pending_commands.setdefault(pid, []).append(cmd)
    return target_ids


@app.route("/api/command/trading-mode", methods=["POST"])
def cmd_trading_mode():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").upper()
    if mode not in ("PAPER", "LIVE"):
        return jsonify({"error": "mode must be PAPER or LIVE"}), 400
    targets = _queue_command("set_trading_mode", mode, data.get("targets", "all"))
    return jsonify({"ok": True, "command": "set_trading_mode", "value": mode,
                    "queued_for": targets}), 200


@app.route("/api/command/kill-switch", methods=["POST"])
def cmd_kill_switch():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    active = bool(data.get("active", True))
    targets = _queue_command("set_kill_switch", active, data.get("targets", "all"))
    return jsonify({"ok": True, "command": "set_kill_switch", "value": active,
                    "queued_for": targets}), 200


@app.route("/api/command/operating-mode", methods=["POST"])
def cmd_operating_mode():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").upper()
    if mode not in ("SUPERVISED", "AUTONOMOUS"):
        return jsonify({"error": "mode must be SUPERVISED or AUTONOMOUS"}), 400
    targets = _queue_command("set_operating_mode", mode, data.get("targets", "all"))
    return jsonify({"ok": True, "command": "set_operating_mode", "value": mode,
                    "queued_for": targets}), 200


@app.route("/api/commands/pending", methods=["GET"])
def cmd_pending():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    with commands_lock:
        return jsonify(dict(pending_commands)), 200


# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Command Console</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --green:#00f5d4;--red:#ff4b6e;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

/* HEADER */
.header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:56px;
  display:flex;align-items:center;gap:12px;
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
          color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4);flex-shrink:0}
.header-sub{font-size:11px;color:var(--muted);font-family:var(--mono)}
.header-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
           background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
           font-size:10px;font-weight:600;color:var(--teal);letter-spacing:0.05em}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);
          box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(0.8)}}

/* PAGE */
.page{max-width:1400px;margin:0 auto;padding:20px 24px}

/* FLEET STATS */
.fleet-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.fleet-card{
  padding:14px 16px;border-radius:14px;
  border:1px solid var(--border);background:var(--surface);
  position:relative;overflow:hidden;
}
.fleet-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.fc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.fc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.fc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.fc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.fleet-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.fleet-val{font-size:24px;font-weight:700;letter-spacing:-0.5px}
.fc-teal .fleet-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.fc-purple .fleet-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.fc-amber .fleet-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.fc-pink .fleet-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.fleet-sub{font-size:10px;color:var(--muted);margin-top:3px}

/* TWO COLUMN */
.two-col{display:grid;grid-template-columns:1fr 380px;gap:16px;margin-bottom:20px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* GLOBAL COMMANDS */
.cmd-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-top:14px}
.cmd-panel-hdr{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.cmd-panel-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.cmd-section{padding:10px 14px;border-bottom:1px solid var(--border)}
.cmd-section:last-child{border-bottom:none}
.cmd-label{font-size:10px;font-weight:600;color:var(--muted);letter-spacing:0.04em;text-transform:uppercase;margin-bottom:6px}
.cmd-row{display:flex;gap:6px}
.cmd-btn{flex:1;padding:6px 10px;font-size:10px;font-weight:700;font-family:var(--mono);
         border:1px solid var(--border);border-radius:8px;background:var(--surface2);color:var(--muted);
         cursor:pointer;transition:all 0.15s;text-transform:uppercase;letter-spacing:0.05em}
.cmd-btn:hover{border-color:var(--teal);color:var(--teal);background:rgba(0,245,212,0.06)}
.cmd-btn.active-teal{border-color:var(--teal);color:var(--teal);background:rgba(0,245,212,0.1);box-shadow:0 0 8px rgba(0,245,212,0.15)}
.cmd-btn.active-amber{border-color:var(--amber);color:var(--amber);background:rgba(255,179,71,0.1);box-shadow:0 0 8px rgba(255,179,71,0.15)}
.cmd-btn.active-pink{border-color:var(--pink);color:var(--pink);background:rgba(255,75,110,0.1);box-shadow:0 0 8px rgba(255,75,110,0.15)}
.cmd-btn.danger{border-color:rgba(255,75,110,0.3);color:var(--pink)}
.cmd-btn.danger:hover{background:rgba(255,75,110,0.12);border-color:var(--pink)}
.cmd-btn.danger.active-pink{background:rgba(255,75,110,0.18);animation:pulse-pink 2s infinite}
@keyframes pulse-pink{0%,100%{box-shadow:0 0 8px rgba(255,75,110,0.15)}50%{box-shadow:0 0 16px rgba(255,75,110,0.35)}}

/* AGENT FLEET TABLE */
.aft-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-top:14px}
.aft-hdr{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.aft-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.aft-count{font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--teal2);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.aft-scroll{max-height:320px;overflow-y:auto}
.aft-row{display:grid;grid-template-columns:1fr 100px 70px 70px;gap:4px;padding:7px 14px;border-bottom:1px solid var(--border);align-items:center;font-size:11px}
.aft-row:last-child{border-bottom:none}
.aft-row.aft-thead{position:sticky;top:0;background:var(--surface);z-index:1;font-size:9px;font-weight:700;
                   letter-spacing:0.06em;text-transform:uppercase;color:var(--dim);padding:8px 14px}
.aft-agent{font-weight:600;font-family:var(--mono);color:var(--text)}
.aft-node{font-size:10px;color:var(--muted);font-family:var(--mono)}
.aft-status{display:flex;align-items:center;gap:5px}
.aft-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.aft-dot.s-active{background:var(--teal);box-shadow:0 0 5px var(--teal)}
.aft-dot.s-idle{background:var(--amber);box-shadow:0 0 4px var(--amber)}
.aft-dot.s-fault{background:var(--pink);box-shadow:0 0 5px var(--pink)}
.aft-dot.s-inactive{background:var(--dim)}
.aft-st{font-size:10px;font-family:var(--mono)}
.aft-st.s-active{color:var(--teal)}.aft-st.s-idle{color:var(--amber)}.aft-st.s-fault{color:var(--pink)}.aft-st.s-inactive{color:var(--dim)}
.aft-time{font-size:10px;color:var(--dim);font-family:var(--mono)}

/* PI GRID */
.pi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}

/* PI CARD */
.pi-card{
  border-radius:18px;border:1px solid var(--border);
  background:var(--surface);
  cursor:pointer;transition:transform 0.18s,box-shadow 0.18s;
  position:relative;overflow:hidden;
}
.pi-card:hover{transform:translateY(-3px);box-shadow:0 12px 40px rgba(0,0,0,0.3)}
.pi-card.online{border-color:rgba(0,245,212,0.15)}
.pi-card.online::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(0,245,212,0.5),transparent);
  box-shadow:0 0 8px rgba(0,245,212,0.3)}
.pi-card.offline{border-color:rgba(255,75,110,0.15)}
.pi-card.offline::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,75,110,0.4),transparent)}
.pi-card.warning{border-color:rgba(255,179,71,0.15)}
.pi-card.warning::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,179,71,0.4),transparent)}

.pi-card-top{padding:14px 14px 10px;display:flex;align-items:flex-start;gap:10px}
.pi-avatar{
  width:42px;height:42px;border-radius:12px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  position:relative;overflow:hidden;
  background:rgba(255,255,255,0.03);
  border:1px solid rgba(255,255,255,0.09);
}
.pi-avatar::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(145deg,rgba(255,255,255,0.13) 0%,transparent 50%)}
/* glass cloud fleet decorations */
.fleet-cloud{position:absolute;bottom:-4px;right:4px;opacity:0.14;pointer-events:none}

.pi-info{flex:1;min-width:0}
.pi-name{font-size:13px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-email{font-size:10px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-id-tag{font-size:9px;font-family:var(--mono);color:var(--dim);margin-top:2px}

.status-dot-wrap{display:flex;align-items:center;gap:4px;flex-shrink:0}
.sdot{width:7px;height:7px;border-radius:50%}
.sdot.online{background:var(--teal);box-shadow:0 0 6px var(--teal)}
.sdot.offline{background:var(--pink);box-shadow:0 0 6px var(--pink)}
.sdot.warning{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.sdot.unknown{background:var(--muted)}
.status-text{font-size:9px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase}
.st-online{color:var(--teal)}
.st-offline{color:var(--pink)}
.st-warning{color:var(--amber)}

.pi-stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border);border-top:1px solid var(--border)}
.pi-stat{padding:9px 12px;background:var(--surface)}
.psl{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.psv{font-size:14px;font-weight:700;color:var(--text)}
.psv.teal{color:var(--teal)}
.psv.amber{color:var(--amber)}
.psv.pink{color:var(--pink)}

.pi-footer{padding:8px 14px;display:flex;align-items:center;gap:8px;
           border-top:1px solid var(--border);background:rgba(255,255,255,0.02)}
.pi-badge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:99px;
          letter-spacing:0.05em;border:1px solid}
.pb-supervised{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.pb-auto{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.2);color:var(--amber)}
.pb-paper{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--muted)}
.pb-kill{background:rgba(255,75,110,0.12);border-color:rgba(255,75,110,0.3);color:var(--pink)}
.pb-pend{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.pi-uptime{margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)}

/* TODO PANEL */
.todo-panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden}
.todo-header{padding:14px 16px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.todo-title{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);flex:1}
.todo-count{font-size:9px;font-weight:700;padding:2px 8px;border-radius:99px;
            background:var(--pink2);border:1px solid rgba(255,75,110,0.25);color:var(--pink)}
.todo-count.clear{background:var(--teal2);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.todo-scroll{max-height:400px;overflow-y:auto}
.todo-item{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:8px}
.todo-item:last-child{border-bottom:none}
.tsev{width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:4px}
.ts-crit{background:var(--pink);box-shadow:0 0 4px var(--pink)}
.ts-high{background:var(--amber);box-shadow:0 0 4px var(--amber)}
.ts-med{background:var(--purple)}
.ts-low{background:var(--muted)}
.todo-body{flex:1;min-width:0}
.todo-title-t{font-size:11px;font-weight:600;color:var(--text);margin-bottom:2px}
.todo-meta{font-size:9px;color:var(--muted);font-family:var(--mono)}
.todo-action{font-size:10px;color:rgba(255,255,255,0.45);margin-top:3px;font-style:italic}
.resolve-btn{font-size:9px;font-weight:700;padding:2px 8px;border-radius:6px;
             background:transparent;border:1px solid var(--border);color:var(--muted);
             cursor:pointer;font-family:var(--sans);transition:all 0.15s;flex-shrink:0}
.resolve-btn:hover{border-color:rgba(0,245,212,0.4);color:var(--teal)}
.todo-empty{padding:24px;text-align:center;font-size:11px;color:var(--muted)}

/* SECTION TITLE */
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:var(--muted);margin-bottom:12px;
           display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* MODAL */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);
  z-index:500;display:flex;align-items:center;justify-content:center;
  opacity:0;pointer-events:none;transition:opacity 0.2s;
}
.modal-overlay.show{opacity:1;pointer-events:all}
.modal{
  background:var(--surface);border:1px solid var(--border2);border-radius:24px;
  width:min(860px,95vw);max-height:88vh;overflow:hidden;
  display:flex;flex-direction:column;
  box-shadow:0 24px 80px rgba(0,0,0,0.6);
  transform:scale(0.95);transition:transform 0.2s;
}
.modal-overlay.show .modal{transform:scale(1)}

.modal-header{padding:18px 22px 0;display:flex;align-items:flex-start;gap:14px;flex-shrink:0}
.modal-avatar{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;
              justify-content:center;flex-shrink:0;
              position:relative;overflow:hidden;
              background:rgba(255,255,255,0.03);
              border:1px solid rgba(255,255,255,0.09)}
.modal-avatar::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(145deg,rgba(255,255,255,0.16) 0%,transparent 50%)}
.modal-title-wrap{flex:1}
.modal-name{font-size:18px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}
.modal-email{font-size:12px;color:var(--muted);margin-top:2px}
.modal-id{font-size:10px;font-family:var(--mono);color:var(--dim);margin-top:1px}
.modal-status-row{display:flex;align-items:center;gap:6px;margin-top:6px}
.modal-close{width:32px;height:32px;border-radius:8px;background:rgba(255,255,255,0.06);
             border:1px solid var(--border);color:var(--muted);font-size:16px;
             cursor:pointer;display:flex;align-items:center;justify-content:center;
             flex-shrink:0;transition:all 0.15s}
.modal-close:hover{background:rgba(255,255,255,0.1);color:var(--text)}

.modal-tabs{display:flex;gap:2px;padding:14px 22px 0;border-bottom:1px solid var(--border);flex-shrink:0}
.mtab{padding:7px 14px;border-radius:8px 8px 0 0;font-size:11px;font-weight:600;
      cursor:pointer;border:none;background:transparent;color:var(--muted);
      font-family:var(--sans);transition:all 0.15s;border-bottom:2px solid transparent}
.mtab.active{color:var(--teal);border-bottom-color:var(--teal);background:rgba(0,245,212,0.05)}
.mtab:hover:not(.active){color:var(--text);background:rgba(255,255,255,0.04)}

.modal-body{flex:1;overflow-y:auto;padding:18px 22px}

/* Modal stats */
.modal-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.mstat{padding:12px 14px;border-radius:12px;background:var(--surface2);border:1px solid var(--border)}
.mstat-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.mstat-val{font-size:20px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}
.mstat-sub{font-size:10px;color:var(--muted);margin-top:2px}
.mv-teal{color:var(--teal);text-shadow:0 0 16px rgba(0,245,212,0.3)}
.mv-pink{color:var(--pink);text-shadow:0 0 16px rgba(255,75,110,0.3)}
.mv-amber{color:var(--amber)}

/* Modal graph */
.modal-graph-wrap{border-radius:12px;background:var(--surface2);border:1px solid var(--border);
                  padding:14px 16px;margin-bottom:14px}
.modal-graph-title{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.06em;
                   text-transform:uppercase;margin-bottom:10px}
.modal-graph-canvas{height:100px;position:relative}

/* Positions */
.pos-row{display:flex;align-items:center;gap:10px;padding:8px 0;
         border-bottom:1px solid var(--border)}
.pos-row:last-child{border-bottom:none}
.pos-chip{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;
          justify-content:center;font-size:9px;font-weight:800;flex-shrink:0;
          background:rgba(123,97,255,0.2);border:1px solid rgba(123,97,255,0.25);color:#a78bfa}
.pos-ticker-t{font-size:12px;font-weight:700;color:var(--text)}
.pos-shares-t{font-size:10px;color:var(--muted)}
.pos-pnl-t{margin-left:auto;font-size:13px;font-weight:700}

/* Agent status */
.agent-row{display:flex;align-items:center;gap:8px;padding:7px 0;
           border-bottom:1px solid var(--border)}
.agent-row:last-child{border-bottom:none}
.agent-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.agent-name{font-size:11px;font-weight:600;color:var(--text);flex:1;font-family:var(--mono)}
.agent-status{font-size:10px;color:var(--muted)}

/* Error log */
.error-log{background:rgba(0,0,0,0.3);border-radius:10px;padding:12px;font-family:var(--mono);
           font-size:10px;line-height:1.7;color:#ff9999;max-height:180px;overflow-y:auto;
           border:1px solid rgba(255,75,110,0.15)}
.error-log.empty{color:var(--teal);font-size:11px;text-align:center;padding:20px}

/* NODE ROSTER TABLE */
/* ROSTER + COMMANDS SIDE-BY-SIDE */
.roster-cmd-row{display:grid;grid-template-columns:1fr 256px;gap:16px;margin-bottom:20px;align-items:start}
@media(max-width:960px){.roster-cmd-row{grid-template-columns:1fr}}
.roster-col{min-width:0}
.cmd-col{min-width:0}
.cmd-panel{margin-top:14px}

.node-table-wrap{overflow-x:auto;border-radius:14px;border:1px solid var(--border);background:var(--surface);margin-bottom:0}
.node-thead{display:grid;grid-template-columns:180px 88px 58px 58px 62px 58px 58px 80px 72px;
            padding:8px 14px;background:rgba(255,255,255,0.025);min-width:680px;border-bottom:1px solid var(--border)}
.node-th{font-size:9px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;color:var(--muted)}
.node-row{display:grid;grid-template-columns:180px 88px 58px 58px 62px 58px 58px 80px 72px;
          padding:10px 14px;border-top:1px solid var(--border);align-items:center;
          cursor:pointer;transition:background 0.15s;min-width:680px}
.node-row:hover{background:rgba(255,255,255,0.025)}
.node-cell{font-size:12px;font-family:var(--mono)}
.node-name-cell{display:flex;align-items:center;gap:8px}
.node-micro-av{width:28px;height:28px;border-radius:8px;flex-shrink:0;
               display:flex;align-items:center;justify-content:center;
               background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.09)}
.node-lbl{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:116px}
.node-id-tag{font-size:9px;color:var(--dim);font-family:var(--mono)}
.mc-ok{color:var(--teal)}.mc-warn{color:var(--amber)}.mc-crit{color:var(--pink)}.mc-na{color:var(--dim)}
/* GRAPH CARDS */
.graph-card{border-radius:14px;border:1px solid var(--border);background:var(--surface);
            padding:16px 16px 10px;margin-bottom:14px}
.graph-card-title{font-size:10px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;
                  color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.graph-canvas-wrap{height:85px;position:relative}

/* Toast */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(60px);
       padding:10px 20px;border-radius:12px;font-size:12px;font-weight:600;
       background:var(--surface);border:1px solid var(--border2);color:var(--text);
       z-index:1000;transition:transform 0.25s;pointer-events:none;
       box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:rgba(0,245,212,0.4);color:var(--teal)}
.toast.err{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* Confirm overlay */
.confirm-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);
                 z-index:600;display:none;align-items:center;justify-content:center}
.confirm-overlay.show{display:flex}
.confirm-box{background:var(--surface);border:1px solid var(--border2);border-radius:16px;
             padding:24px;width:340px;text-align:center}
.confirm-msg{font-size:13px;color:var(--text);margin-bottom:16px;line-height:1.5}
.confirm-btns{display:flex;gap:10px;justify-content:center}
.cbtn{padding:8px 20px;border-radius:9px;font-size:12px;font-weight:600;cursor:pointer;
      font-family:var(--sans);transition:all 0.15s}
.cbtn-cancel{background:transparent;border:1px solid var(--border2);color:var(--muted)}
.cbtn-cancel:hover{color:var(--text)}
.cbtn-confirm{background:var(--pink2);border:1px solid rgba(255,75,110,0.3);color:var(--pink)}
.cbtn-confirm:hover{background:rgba(255,75,110,0.2)}
.hamburger-wrap{position:relative}
.hamburger-btn{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;width:32px;height:32px;background:transparent;border:1px solid var(--border);border-radius:8px;cursor:pointer;padding:0}
.hamburger-btn span{display:block;width:14px;height:1.5px;background:var(--muted);border-radius:2px;transition:all .2s}
.hamburger-btn:hover{border-color:var(--border2)}
.hamburger-btn:hover span{background:var(--text)}
.hamburger-menu{display:none;position:absolute;top:calc(100% + 8px);right:0;min-width:180px;background:var(--surface);border:1px solid var(--border2);border-radius:12px;padding:6px;z-index:999;box-shadow:0 12px 40px rgba(0,0,0,0.5)}
.hamburger-menu.open{display:flex;flex-direction:column}
.hmenu-item{padding:8px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s;letter-spacing:0.03em}
.hmenu-item:hover{background:rgba(255,255,255,0.05);color:var(--text)}
</style>
</head>
<body>

<!-- DEBUG BANNER — remove after console is confirmed working -->
<div id="dbg-banner" style="background:#1a0a2e;border:2px solid #7b61ff;color:#fff;padding:10px 16px;font-family:monospace;font-size:13px;position:fixed;bottom:0;left:0;right:0;z-index:99999">
  <b>DEBUG</b> | Server rendered: {{ build_ts }} |
  JS status: <span id="dbg-js" style="color:#ff4b6e">NOT RUNNING</span> |
  Fetch status: <span id="dbg-fetch" style="color:#ff4b6e">NOT CALLED</span> |
  piData keys: <span id="dbg-keys" style="color:#ff4b6e">—</span>
</div>
<script>
document.getElementById('dbg-js').textContent = 'RUNNING';
document.getElementById('dbg-js').style.color = '#00f5d4';
</script>

<!-- HEADER -->
<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="header-sub">Command Console</div>
  <div class="header-right">
    <div class="clock" id="clock">--:--:-- ET</div>
    <div class="live-pill"><div class="live-dot"></div><span id="pi-count">No Nodes</span></div>
    <div class="hamburger-wrap">
      <button class="hamburger-btn" onclick="toggleMenu()" id="hbtn" aria-label="Menu">
        <span></span><span></span><span></span>
      </button>
      <div class="hamburger-menu" id="hmenu">
        <a href="/console" class="hmenu-item">Scoop Queue</a>
        <a href="/project-status" class="hmenu-item">Project Status</a>
        <a href="/display" class="hmenu-item">Sentinel Display</a>
        <a href="/audit" class="hmenu-item">Auditor</a>
        <a href="/logs" class="hmenu-item">Logs</a>
      </div>
    </div>
  </div>
</header>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- CONFIRM -->
<div class="confirm-overlay" id="confirm-overlay">
  <div class="confirm-box">
    <div class="confirm-msg" id="confirm-msg"></div>
    <div class="confirm-btns">
      <button class="cbtn cbtn-cancel" onclick="cancelDelete()">Cancel</button>
      <button class="cbtn cbtn-confirm" onclick="confirmDelete()">Remove</button>
    </div>
  </div>
</div>

<!-- MODAL -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal" id="modal">
    <div class="modal-header" id="modal-header">
      <div class="modal-avatar av-teal" id="modal-avatar">--</div>
      <div class="modal-title-wrap">
        <div class="modal-name" id="modal-name">Loading...</div>
        <div class="modal-email" id="modal-email"></div>
        <div class="modal-id" id="modal-id"></div>
        <div class="modal-status-row" id="modal-status-row"></div>
      </div>
      <button class="modal-close" onclick="closeModalBtn()">✕</button>
    </div>
    <div class="modal-tabs">
      <button class="mtab active" onclick="switchTab('overview',this)">Overview</button>
      <button class="mtab" onclick="switchTab('performance',this)">Performance</button>
      <button class="mtab" onclick="switchTab('logs',this)">Logs</button>
      <button class="mtab" onclick="switchTab('admin',this)">Admin</button>
    </div>
    <div class="modal-body" id="modal-body">
      <div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>
    </div>
  </div>
</div>

<!-- PAGE -->
<div class="page">

  <!-- FLEET STATS -->
  <div class="fleet-grid">
    <!-- Nodes Online -->
    <div class="fleet-card fc-teal">
      <div class="fleet-label">Nodes Online</div>
      <div class="fleet-val" id="fl-online">0</div>
      <div class="fleet-sub" id="fl-total">of 0 registered</div>
      <svg class="fleet-cloud" viewBox="0 0 54 38" xmlns="http://www.w3.org/2000/svg" width="54" height="38">
        <circle cx="38" cy="12" r="9" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.2"/>
        <g stroke="rgba(255,255,255,1)" stroke-width="1.1" stroke-linecap="round">
          <line x1="38" y1="1" x2="38" y2="0"/><line x1="44.5" y1="5.5" x2="45.5" y2="4.5"/>
          <line x1="48" y1="12" x2="50" y2="12"/><line x1="31.5" y1="5.5" x2="30.5" y2="4.5"/>
          <line x1="28" y1="12" x2="26" y2="12"/>
        </g>
        <path d="M3,29 Q3,22 10,22 Q9,15 18,15 Q24,15 26,19 Q33,18 33,24 Q37,24 37,29 Q37,33 33,33 L7,33 Q3,33 3,29 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
      </svg>
    </div>
    <!-- Active Alerts -->
    <div class="fleet-card fc-pink">
      <div class="fleet-label">Active Alerts</div>
      <div class="fleet-val" id="fl-issues">0</div>
      <div class="fleet-sub">Open issues</div>
      <svg class="fleet-cloud" viewBox="0 0 44 40" xmlns="http://www.w3.org/2000/svg" width="44" height="40">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <path d="M21,28 L18,35 L21,34.5 L19.5,40 L25.5,32 L22,32.5 Z" fill="rgba(255,255,255,1)" stroke="none"/>
      </svg>
    </div>
    <!-- Avg CPU -->
    <div class="fleet-card fc-amber">
      <div class="fleet-label">Avg CPU</div>
      <div class="fleet-val" id="fl-cpu">—</div>
      <div class="fleet-sub" id="fl-cpu-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <g stroke="rgba(255,255,255,0.7)" stroke-width="1.1" stroke-linecap="round">
          <line x1="11" y1="19" x2="11" y2="23"/><line x1="15" y1="16" x2="15" y2="23"/>
          <line x1="19" y1="18" x2="19" y2="23"/><line x1="23" y1="14" x2="23" y2="23"/>
        </g>
      </svg>
    </div>
    <!-- Avg RAM -->
    <div class="fleet-card fc-purple">
      <div class="fleet-label">Avg RAM</div>
      <div class="fleet-val" id="fl-ram">—</div>
      <div class="fleet-sub" id="fl-ram-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <rect x="9" y="17" width="5" height="5" rx="1" fill="rgba(255,255,255,0.7)" stroke="none"/>
        <rect x="16" y="17" width="5" height="5" rx="1" fill="rgba(255,255,255,0.4)" stroke="none"/>
        <rect x="23" y="17" width="5" height="5" rx="1" fill="rgba(255,255,255,0.7)" stroke="none"/>
      </svg>
    </div>
    <!-- Fleet Agents -->
    <div class="fleet-card fc-teal">
      <div class="fleet-label">Fleet Agents</div>
      <div class="fleet-val" id="fl-agents">—</div>
      <div class="fleet-sub" id="fl-agents-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <g fill="rgba(255,255,255,0.85)" stroke="none">
          <circle cx="14" cy="18" r="2.5"/><circle cx="22" cy="18" r="2.5"/><circle cx="30" cy="18" r="2.5"/>
        </g>
      </svg>
    </div>
    <!-- Trading Mode -->
    <div class="fleet-card fc-amber">
      <div class="fleet-label">Trading Mode</div>
      <div class="fleet-val" id="fl-trading" style="font-size:20px">—</div>
      <div class="fleet-sub" id="fl-trading-sub">Awaiting data</div>
      <svg class="fleet-cloud" viewBox="0 0 44 32" xmlns="http://www.w3.org/2000/svg" width="44" height="32">
        <path d="M3,23 Q3,16 10,16 Q9,9 18,9 Q24,9 26,13 Q33,12 33,18 Q37,18 37,23 Q37,27 33,27 L7,27 Q3,27 3,23 Z" fill="none" stroke="rgba(255,255,255,1)" stroke-width="1.3" stroke-linejoin="round"/>
        <g stroke="rgba(255,255,255,0.9)" stroke-width="1.2" fill="none" stroke-linecap="round">
          <polyline points="11,24 16,17 21,21 29,13"/><circle cx="29" cy="13" r="1.5" fill="rgba(255,255,255,0.9)"/>
        </g>
      </svg>
    </div>
  </div>

  <!-- ROSTER + COMMANDS ROW -->
  <div class="roster-cmd-row">

    <!-- NODE ROSTER -->
    <div class="roster-col">
      <div class="sec-title">Node Roster <span id="sync-label" style="font-size:9px;color:var(--dim);font-weight:400;letter-spacing:0;text-transform:none">syncing...</span></div>
      <div id="node-roster">
        <div class="node-table-wrap">
          <div style="color:var(--muted);font-size:12px;padding:24px;text-align:center">Waiting for first heartbeat…</div>
        </div>
      </div>
    </div>

    <!-- GLOBAL COMMANDS -->
    <div class="cmd-col">
      <div class="sec-title">Commands</div>
      <div class="cmd-panel" style="margin-top:0">
        <div class="cmd-panel-hdr">
          <span class="cmd-panel-title">Global Commands</span>
          <span style="font-size:9px;color:var(--dim);font-family:var(--mono)" id="cmd-status"></span>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Trading Mode Gate</div>
          <div class="cmd-row">
            <button class="cmd-btn" id="cmd-paper" onclick="sendGlobalCmd('trading-mode','PAPER')">Paper</button>
            <button class="cmd-btn" id="cmd-live" onclick="confirmCmd('trading-mode','LIVE','Switch ALL nodes to LIVE trading?')">Live</button>
          </div>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Operating Mode</div>
          <div class="cmd-row">
            <button class="cmd-btn" id="cmd-supervised" onclick="sendGlobalCmd('operating-mode','SUPERVISED')">Supervised</button>
            <button class="cmd-btn" id="cmd-autonomous" onclick="confirmCmd('operating-mode','AUTONOMOUS','Grant AUTONOMOUS mode to ALL nodes?')">Autonomous</button>
          </div>
        </div>
        <div class="cmd-section">
          <div class="cmd-label">Emergency</div>
          <div class="cmd-row">
            <button class="cmd-btn danger" id="cmd-kill-on" onclick="confirmCmd('kill-switch',true,'ACTIVATE kill switch on ALL nodes?')">Kill Switch ON</button>
            <button class="cmd-btn" id="cmd-kill-off" onclick="sendGlobalCmd('kill-switch',false)">Kill Switch OFF</button>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- TWO COLUMN: GRAPHS + ISSUES -->
  <div class="two-col">

    <!-- SYSTEM HEALTH GRAPHS -->
    <div>
      <div class="sec-title">System Health Over Time</div>
      <div class="graph-card">
        <div class="graph-card-title">CPU Usage %</div>
        <div class="graph-canvas-wrap"><canvas id="cpu-chart"></canvas></div>
      </div>
      <div class="graph-card">
        <div class="graph-card-title">Memory Usage %</div>
        <div class="graph-canvas-wrap"><canvas id="ram-chart"></canvas></div>
      </div>
    </div>

    <!-- RIGHT COLUMN: ISSUES + AGENT FLEET -->
    <div>
      <div class="sec-title">Open Issues</div>
      <div class="todo-panel">
        <div class="todo-header">
          <span class="todo-title">AI Triage</span>
          <span class="todo-count clear" id="todo-badge">Loading</span>
        </div>
        <div class="todo-scroll" id="todo-list">
          <div class="todo-empty">Loading issues...</div>
        </div>
      </div>

      <!-- AGENT FLEET OVERVIEW -->
      <div class="aft-panel">
        <div class="aft-hdr">
          <span class="aft-title">Agent Fleet</span>
          <span class="aft-count" id="aft-badge">0</span>
        </div>
        <div class="aft-scroll" id="aft-body">
          <div style="color:var(--muted);font-size:11px;padding:16px;text-align:center">Waiting for heartbeat data...</div>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
/* DBG */ try { document.getElementById('dbg-fetch').textContent = 'MAIN SCRIPT STARTED'; } catch(e){}
const SECRET_TOKEN = '{{ secret_token }}';
let piData = {};
let allTodos = [];
let pendingDelete = null;
let modalPiId = null;
let modalChartInst = null;
let currentModalTab = 'overview';
const AVATAR_COLORS = ['av-teal','av-purple','av-amber','av-pink'];
const SEV_ORDER = {CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3};

// ── CLOCK ──
function updateClock() {
  const t = new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false});
  document.getElementById('clock').textContent = t + ' ET';
}
updateClock();
setInterval(updateClock, 1000);

// ── TOAST ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── STATUS HELPERS ──
function statusClass(s) { return (s === 'online' || s === 'active') ? 'online' : s === 'offline' ? 'offline' : (s === 'fault' || s === 'warning') ? 'warning' : 'warning'; }
function dotClass(s) { return (s === 'online' || s === 'active') ? 'online' : s === 'offline' ? 'offline' : (s === 'fault' || s === 'warning') ? 'warning' : 'unknown'; }
function ageSince(isoStr) {
  const secs = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs/60) + 'm ago';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ago';
  return Math.floor(secs/86400) + 'd ago';
}
function initials(label) {
  return label.split(' ').filter(Boolean).map(w=>w[0]||'').join('').toUpperCase().slice(0,2) || '??';
}
function avatarColor(piId) {
  let h = 0;
  for (let i=0; i<piId.length; i++) h = (h*31 + piId.charCodeAt(i)) & 0xFFFFFF;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

// ── GLASS CLOUD WEATHER ICONS ──
function weatherIcon(status) {
  const cp = 'M3,21 Q3,14 10,14 Q9,7 18,7 Q24,7 26,11 Q33,10 33,16 Q37,16 37,21 Q37,25 33,25 L7,25 Q3,25 3,21 Z';
  const hl = 'M7,14 Q7,9 13,9 Q12,4 19,4';
  if (status === 'online' || status === 'active') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 7px rgba(0,245,212,0.4))">
      <circle cx="27" cy="10" r="8" fill="rgba(0,245,212,0.07)"/>
      <circle cx="27" cy="10" r="5.5" fill="rgba(0,245,212,0.15)" stroke="rgba(0,245,212,0.72)" stroke-width="1.3"/>
      <g stroke="rgba(0,245,212,0.62)" stroke-width="1.2" stroke-linecap="round">
        <line x1="27" y1="2.5" x2="27" y2="0.5"/>
        <line x1="32.5" y1="4.5" x2="34" y2="3"/>
        <line x1="36" y1="10" x2="38" y2="10"/>
        <line x1="21.5" y1="4.5" x2="20" y2="3"/>
        <line x1="18" y1="10" x2="16" y2="10"/>
      </g>
      <path d="${cp}" fill="rgba(255,255,255,0.08)" stroke="rgba(255,255,255,0.75)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.28)" stroke-width="1" stroke-linecap="round"/>
    </svg>`;
  }
  if (status === 'fault' || status === 'warning') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 7px rgba(255,179,71,0.4))">
      <path d="${cp}" fill="rgba(255,179,71,0.05)" stroke="rgba(255,255,255,0.68)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.22)" stroke-width="1" stroke-linecap="round"/>
      <path d="M21,27 L18,34 L21,33.5 L19.5,39 L25.5,31 L22,31.5 Z" fill="rgba(255,179,71,0.95)" stroke="rgba(255,179,71,0.4)" stroke-width="0.5" stroke-linejoin="round"/>
    </svg>`;
  }
  if (status === 'offline') {
    return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 5px rgba(255,75,110,0.25))">
      <path d="${cp}" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.52)" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.18)" stroke-width="1" stroke-linecap="round"/>
      <g stroke="rgba(140,200,255,0.65)" stroke-width="1.6" stroke-linecap="round">
        <line x1="13" y1="28" x2="11.5" y2="37"/>
        <line x1="20" y1="28" x2="18.5" y2="37"/>
        <line x1="27" y1="28" x2="25.5" y2="37"/>
      </g>
    </svg>`;
  }
  return `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;filter:drop-shadow(0 0 4px rgba(255,255,255,0.1))">
    <path d="${cp}" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.38)" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="${hl}" fill="none" stroke="rgba(255,255,255,0.14)" stroke-width="1" stroke-linecap="round"/>
  </svg>`;
}

// ── FETCH STATUS ──
async function fetchStatus() {
  const dbgFetch = document.getElementById('dbg-fetch');
  const dbgKeys  = document.getElementById('dbg-keys');
  try {
    if (dbgFetch) dbgFetch.textContent = 'FETCHING...';
    const r = await fetch('/api/status');
    if (!r.ok) { if (dbgFetch) dbgFetch.textContent = 'HTTP ' + r.status; return; }
    piData = await r.json();
    if (dbgFetch) { dbgFetch.textContent = 'OK (' + Object.keys(piData).length + ' nodes)'; dbgFetch.style.color = '#00f5d4'; }
    if (dbgKeys) dbgKeys.textContent = Object.keys(piData).join(', ') || 'empty';
    renderNodeRoster();
    updateFleetStats();
    buildFleetCharts();
    renderAgentFleet();
    document.getElementById('sync-label').textContent = 'synced ' + new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'});
  } catch(e) {
    console.error('[fetchStatus]', e);
    if (dbgFetch) { dbgFetch.textContent = 'ERROR: ' + e.message; dbgFetch.style.color = '#ff4b6e'; }
  }
}

// ── METRIC COLOR HELPERS ──
function metricClass(v, warn, crit) {
  if (v == null) return 'mc-na';
  if (v >= crit) return 'mc-crit';
  if (v >= warn) return 'mc-warn';
  return 'mc-ok';
}
function fmtMetric(v, unit, dec=0) {
  return v != null ? v.toFixed(dec) + unit : '—';
}
function colorWithAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1,3),16);
  const g = parseInt(hex.slice(3,5),16);
  const b = parseInt(hex.slice(5,7),16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// ── FLEET STATS ──
function updateFleetStats() {
  const pis   = Object.values(piData);
  const total  = pis.length;
  const online = pis.filter(p => p.status === 'online' || p.status === 'active').length;
  const notOk  = pis.filter(p => p.status !== 'online' && p.status !== 'active').length;

  const cpuNodes  = pis.filter(p => p.cpu_percent != null);
  const ramNodes  = pis.filter(p => p.ram_percent != null);

  const avg = (arr, key) => arr.length ? arr.reduce((s,p) => s + p[key], 0) / arr.length : null;
  const avgCpu  = avg(cpuNodes,  'cpu_percent');
  const avgRam  = avg(ramNodes,  'ram_percent');

  // Agent counts across all nodes (including expected but unreported)
  let agActive = 0, agIdle = 0, agFault = 0, agInactive = 0, agTotal = 0;
  pis.forEach(p => {
    const reported = p.agents || {};
    const ageSecs = p.age_secs || 0;
    const role = detectNodeRole(p);
    const merged = {};
    if (role && EXPECTED_AGENTS[role]) {
      EXPECTED_AGENTS[role].forEach(k => { merged[k] = null; });
    }
    Object.entries(reported).forEach(([k, v]) => { merged[k] = v; });
    Object.values(merged).forEach(s => {
      agTotal++;
      const cls = agentStatusClass(s, ageSecs);
      if (cls === 'fault') agFault++;
      else if (cls === 'idle') agIdle++;
      else if (cls === 'inactive') agInactive++;
      else agActive++;
    });
  });

  // Trading mode counts
  const paperCount = pis.filter(p => (p.trading_mode||'PAPER') === 'PAPER').length;
  const liveCount  = pis.filter(p => (p.trading_mode||'PAPER') === 'LIVE').length;

  const sv = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };

  sv('fl-online',  online + (total ? ' / ' + total : ''));
  sv('fl-total',   total === 0 ? 'No nodes registered' : notOk === 0 ? 'All reporting' : notOk + ' not reporting');
  sv('fl-issues',  allTodos.filter(t=>!t.resolved).length);
  sv('fl-cpu',     avgCpu  != null ? avgCpu.toFixed(1)  + '%'  : '—');
  sv('fl-ram',     avgRam  != null ? avgRam.toFixed(1)  + '%'  : '—');
  sv('fl-agents',  agTotal > 0 ? agActive + ' / ' + agTotal : '—');
  sv('fl-agents-sub', agTotal > 0 ? agActive + ' active' + (agIdle ? ', ' + agIdle + ' idle' : '') + (agFault ? ', ' + agFault + ' fault' : '') + (agInactive ? ', ' + agInactive + ' off' : '') : 'Awaiting data');
  sv('fl-trading',    total > 0 ? paperCount + 'P / ' + liveCount + 'L' : '—');
  sv('fl-trading-sub', total > 0 ? paperCount + ' paper, ' + liveCount + ' live' : 'Awaiting data');

  // Header pill
  if (total === 0)       sv('pi-count', 'No Nodes');
  else if (notOk === 0)  sv('pi-count', 'All Nodes Online');
  else                   sv('pi-count', notOk + ' not reporting');

  // Update command button highlights
  updateCommandState(pis);
}

// ── NODE ROSTER TABLE ──
function renderNodeRoster() {
  const wrap = document.getElementById('node-roster');
  const pis  = Object.values(piData);

  if (!pis.length) {
    wrap.innerHTML = '<div class="node-table-wrap"><div style="color:var(--muted);font-size:12px;padding:24px;text-align:center">No nodes registered yet. Waiting for first heartbeat\u2026</div></div>';
    return;
  }

  pis.sort((a,b) => {
    const ord = {online:0,warning:1,fault:1,offline:2};
    return (ord[a.status]??3) - (ord[b.status]??3);
  });

  const rows = pis.map(pi => {
    const sc   = statusClass(pi.status);
    const dc   = dotClass(pi.status);
    const load1 = pi.load_avg && pi.load_avg[0] != null ? pi.load_avg[0] : null;
    return '<div class="node-row" data-piid="' + pi.pi_id + '" onclick="openModal(this.dataset.piid)">'
      + '<div class="node-name-cell">'
          + '<div class="node-micro-av">' + weatherIcon(pi.status) + '</div>'
          + '<div><div class="node-lbl">' + escHtml(pi.label || pi.pi_id) + '</div>'
              + '<div class="node-id-tag">' + pi.pi_id + '</div></div>'
      + '</div>'
      + '<div><div class="status-dot-wrap">'
          + '<div class="sdot ' + dc + '"></div>'
          + '<span class="status-text st-' + sc + '">' + pi.status + '</span>'
      + '</div></div>'
      + '<div class="node-cell ' + metricClass(pi.cpu_percent, 70, 90) + '">'
          + fmtMetric(pi.cpu_percent, '%') + '</div>'
      + '<div class="node-cell ' + metricClass(pi.ram_percent, 75, 90) + '">'
          + fmtMetric(pi.ram_percent, '%') + '</div>'
      + '<div class="node-cell ' + metricClass(load1, 1.5, 3.0) + '">'
          + fmtMetric(load1, '', 2) + '</div>'
      + '<div class="node-cell ' + metricClass(pi.cpu_temp, 65, 80) + '">'
          + fmtMetric(pi.cpu_temp, '\u00b0', 1) + '</div>'
      + '<div class="node-cell ' + metricClass(pi.disk_percent, 75, 90) + '">'
          + fmtMetric(pi.disk_percent, '%') + '</div>'
      + '<div class="node-cell mc-na">' + (pi.uptime || '\u2014') + '</div>'
      + '<div class="node-cell mc-na">' + ageSince(pi.last_seen) + '</div>'
    + '</div>';
  }).join('');

  wrap.innerHTML =
    '<div class="node-table-wrap">'
    + '<div class="node-thead">'
        + '<div class="node-th">Node</div>'
        + '<div class="node-th">Status</div>'
        + '<div class="node-th">CPU</div>'
        + '<div class="node-th">RAM</div>'
        + '<div class="node-th">Load</div>'
        + '<div class="node-th">Temp</div>'
        + '<div class="node-th">Disk</div>'
        + '<div class="node-th">Uptime</div>'
        + '<div class="node-th">Last Seen</div>'
    + '</div>'
    + rows
    + '</div>';
}


// ── MODAL ──
async function openModal(piId) {
  modalPiId = piId;
  currentModalTab = 'overview';
  document.getElementById('modal-overlay').classList.add('show');
  document.body.style.overflow = 'hidden';

  // Set header from cached data immediately
  const pi = piData[piId] || {};
  document.getElementById('modal-avatar').className = 'modal-avatar';
  document.getElementById('modal-avatar').innerHTML = weatherIcon(pi.status || 'unknown');
  document.getElementById('modal-name').textContent = pi.label || piId;
  document.getElementById('modal-email').textContent = pi.email || 'No email';
  document.getElementById('modal-id').textContent = piId;

  const sc = statusClass(pi.status||'unknown');
  document.getElementById('modal-status-row').innerHTML =
    '<div class="sdot ' + dotClass(pi.status||'unknown') + '"></div>'
    + '<span style="font-size:10px;color:var(--' + (sc==='online'?'teal':sc==='offline'?'pink':'amber') + ')">'
    + (pi.status||'unknown').toUpperCase() + '</span>'
    + '<span style="font-size:10px;color:var(--muted);margin-left:4px">· last seen ' + ageSince(pi.last_seen||new Date().toISOString()) + '</span>';

  // Reset tabs
  document.querySelectorAll('.mtab').forEach((b,i) => b.classList.toggle('active', i===0));

  // Fetch full detail
  try {
    const r = await fetch('/api/pi/' + encodeURIComponent(piId));
    const detail = r.ok ? await r.json() : pi;
    renderModalTab('overview', detail);
  } catch(e) {
    renderModalTab('overview', pi);
  }
}

function closeModal(e) {
  if (e.target.id === 'modal-overlay') closeModalBtn();
}

function closeModalBtn() {
  document.getElementById('modal-overlay').classList.remove('show');
  document.body.style.overflow = '';
  if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
  modalPiId = null;
}

async function switchTab(tab, btn) {
  currentModalTab = tab;
  document.querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('modal-body').innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">Loading...</div>';
  if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
  try {
    const r = await fetch('/api/pi/' + encodeURIComponent(modalPiId));
    const detail = r.ok ? await r.json() : (piData[modalPiId] || {});
    renderModalTab(tab, detail);
  } catch(e) {}
}

function renderModalTab(tab, pi) {
  const body = document.getElementById('modal-body');

  const mc  = (v,w,c) => v==null?'mc-na':v>=c?'mc-crit':v>=w?'mc-warn':'mc-ok';
  const fmt = (v,u,d=0) => v!=null ? v.toFixed(d)+u : '\u2014';
  const gb  = (v) => v!=null ? v.toFixed(2)+' GB' : '\u2014';

  if (tab === 'overview') {
    // ── Processor panel data ──
    const cpuCls  = mc(pi.cpu_percent, 70, 90);
    const load    = pi.load_avg || [];
    const cores   = pi.cpu_count || '\u2014';
    // ── Memory panel data ──
    const ramTot  = pi.ram_total_gb  || 0;
    const ramUsed = pi.ram_used_gb   || 0;
    const ramCach = pi.ram_cached_gb || 0;
    const ramFree = pi.ram_avail_gb  || 0;
    const ramUsedPct  = ramTot ? Math.round(ramUsed / ramTot * 100) : 0;
    const ramCachPct  = ramTot ? Math.round(ramCach / ramTot * 100) : 0;
    const ramFreePct  = Math.max(0, 100 - ramUsedPct - ramCachPct);
    // ── Disk panel data ──
    const dskCls  = mc(pi.disk_percent, 75, 90);
    const dskUsed = pi.disk_used_gb  || 0;
    const dskTot  = pi.disk_total_gb || 0;
    const dskFree = pi.disk_free_gb  || 0;
    const dskPct  = pi.disk_percent  || 0;
    // ── Temp panel data ──
    const tmpCls  = mc(pi.cpu_temp, 65, 80);

    body.innerHTML =
      // ── 2x2 Panel Grid ──────────────────────────────────────────────────
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">'

        // PROCESSOR panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Processor</div>'
            + '<div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:8px">'
                + '<div class="' + cpuCls + '" style="font-size:32px;font-weight:700;line-height:1;font-family:var(--mono)">' + fmt(pi.cpu_percent,'%') + '</div>'
                + '<div style="padding-bottom:4px">'
                    + '<div style="font-size:10px;color:var(--muted)">' + cores + ' cores</div>'
                    + '<div style="font-size:10px;color:var(--dim)">load ' + (load[0]!=null?load[0].toFixed(2):'\u2014') + '</div>'
                + '</div>'
            + '</div>'
            + '<div style="height:40px;position:relative"><canvas id="mc-cpu-spark"></canvas></div>'
            + '<div style="display:flex;gap:16px;margin-top:6px">'
                + '<div><div style="font-size:8px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em">5m avg</div>'
                    + '<div style="font-size:11px;font-family:var(--mono);color:var(--muted)">' + (load[1]!=null?load[1].toFixed(2):'\u2014') + '</div></div>'
                + '<div><div style="font-size:8px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em">15m avg</div>'
                    + '<div style="font-size:11px;font-family:var(--mono);color:var(--muted)">' + (load[2]!=null?load[2].toFixed(2):'\u2014') + '</div></div>'
            + '</div>'
        + '</div>'

        // MEMORY panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Memory</div>'
            + '<div style="display:flex;align-items:center;gap:12px">'
                // Donut chart
                + '<div style="position:relative;width:72px;height:72px;flex-shrink:0">'
                    + '<canvas id="mc-ram-donut" width="72" height="72"></canvas>'
                    + '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none">'
                        + '<div style="font-size:13px;font-weight:700;font-family:var(--mono);color:var(--teal)">' + ramUsedPct + '%</div>'
                    + '</div>'
                + '</div>'
                // Breakdown table
                + '<div style="flex:1;display:flex;flex-direction:column;gap:4px">'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:var(--teal);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Used</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--teal)">' + gb(pi.ram_used_gb) + '</div>'
                    + '</div>'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:var(--purple);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Cached</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--purple)">' + gb(pi.ram_cached_gb) + '</div>'
                    + '</div>'
                    + '<div style="display:flex;align-items:center;gap:6px">'
                        + '<div style="width:8px;height:8px;border-radius:2px;background:rgba(255,255,255,0.1);flex-shrink:0"></div>'
                        + '<div style="font-size:10px;color:var(--muted);flex:1">Free</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--dim)">' + gb(pi.ram_avail_gb) + '</div>'
                    + '</div>'
                    + '<div style="margin-top:2px;padding-top:4px;border-top:1px solid var(--border);display:flex;justify-content:space-between">'
                        + '<div style="font-size:9px;color:var(--dim)">Total</div>'
                        + '<div style="font-size:10px;font-family:var(--mono);color:var(--muted)">' + gb(pi.ram_total_gb) + '</div>'
                    + '</div>'
                + '</div>'
            + '</div>'
        + '</div>'

        // STORAGE panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Storage</div>'
            + '<div style="display:flex;justify-content:space-between;margin-bottom:8px">'
                + '<div class="' + dskCls + '" style="font-size:28px;font-weight:700;font-family:var(--mono);line-height:1">' + fmt(pi.disk_percent,'%') + '</div>'
                + '<div style="text-align:right">'
                    + '<div style="font-size:10px;color:var(--muted)">Used: ' + gb(pi.disk_used_gb) + '</div>'
                    + '<div style="font-size:10px;color:var(--dim)">Free: ' + gb(pi.disk_free_gb) + '</div>'
                    + '<div style="font-size:10px;color:var(--dim)">Total: ' + gb(pi.disk_total_gb) + '</div>'
                + '</div>'
            + '</div>'
            // Fill bar
            + '<div style="height:6px;border-radius:99px;background:rgba(255,255,255,0.07);overflow:hidden;margin-bottom:4px">'
                + '<div style="height:100%;width:' + dskPct + '%;border-radius:99px;background:' + (dskPct>=90?'var(--pink)':dskPct>=75?'var(--amber)':'var(--teal)') + ';transition:width 0.4s"></div>'
            + '</div>'
            + '<div style="font-size:9px;color:var(--dim)">/ (root filesystem)</div>'
        + '</div>'

        // THERMAL & UPTIME panel
        + '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px">'
            + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Thermal &amp; Uptime</div>'
            + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'
                + '<div style="width:36px;height:36px;border-radius:9px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center">'
                    + '<svg viewBox="0 0 20 20" width="18" height="18" fill="none" xmlns="http://www.w3.org/2000/svg">'
                        + '<rect x="8.5" y="2" width="3" height="11" rx="1.5" fill="rgba(255,255,255,0.15)"/>'
                        + '<rect x="9" y="2.5" width="2" height="' + (pi.cpu_temp!=null?Math.min(10,pi.cpu_temp/10).toFixed(1):'5') + '" rx="1" fill="' + (tmpCls==='mc-crit'?'#ff4b6e':tmpCls==='mc-warn'?'#ffb347':'#00f5d4') + '"/>'
                        + '<circle cx="10" cy="14.5" r="2.5" fill="' + (tmpCls==='mc-crit'?'#ff4b6e':tmpCls==='mc-warn'?'#ffb347':'#00f5d4') + '"/>'
                    + '</svg>'
                + '</div>'
                + '<div>'
                    + '<div class="' + tmpCls + '" style="font-size:22px;font-weight:700;font-family:var(--mono);line-height:1">' + fmt(pi.cpu_temp,'\u00b0C',1) + '</div>'
                    + '<div style="font-size:9px;color:var(--dim);margin-top:2px">CPU Temperature</div>'
                + '</div>'
            + '</div>'
            + '<div style="border-top:1px solid var(--border);padding-top:10px">'
                + '<div style="font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:4px">Uptime</div>'
                + '<div style="font-size:16px;font-weight:600;color:var(--text);font-family:var(--mono)">' + (pi.uptime||'N/A') + '</div>'
                + '<div style="font-size:9px;color:var(--dim);margin-top:6px">Mode: ' + (pi.operating_mode||'SUPERVISED') + ' &nbsp;&middot;&nbsp; ' + (pi.trading_mode||'PAPER') + '</div>'
            + '</div>'
        + '</div>'

      + '</div>'

      // ── Agents / Process List ──────────────────────────────────────────────
      + '<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">Agents</div>'
      + renderAgents(pi.agents||{});

    // Draw micro charts after DOM ready
    setTimeout(() => {
      // CPU sparkline
      const cpuCtx = document.getElementById('mc-cpu-spark');
      if (cpuCtx) {
        const hist = (pi.history||[]).filter(h=>h.cpu!=null);
        const vals = hist.map(h=>h.cpu);
        if (vals.length > 1) {
          const g = cpuCtx.getContext('2d').createLinearGradient(0,0,0,40);
          g.addColorStop(0,'rgba(0,245,212,0.25)'); g.addColorStop(1,'rgba(0,245,212,0)');
          new Chart(cpuCtx, { type:'line', data:{ labels:vals.map((_,i)=>i),
            datasets:[{data:vals,borderColor:'#00f5d4',borderWidth:1.5,fill:true,
              backgroundColor:g,tension:0.4,pointRadius:0}]},
            options:{animation:false,responsive:true,maintainAspectRatio:false,
              plugins:{legend:{display:false},tooltip:{enabled:false}},
              scales:{x:{display:false},y:{display:false,min:0,max:100}}}});
        }
      }
      // RAM donut
      const ramCtx = document.getElementById('mc-ram-donut');
      if (ramCtx) {
        new Chart(ramCtx, { type:'doughnut',
          data:{ datasets:[{
            data:[ramUsedPct, ramCachPct, ramFreePct],
            backgroundColor:['rgba(0,245,212,0.85)','rgba(123,97,255,0.75)','rgba(255,255,255,0.07)'],
            borderWidth:0, hoverOffset:0,
          }]},
          options:{cutout:'68%',animation:false,
            plugins:{legend:{display:false},tooltip:{enabled:false}}}});
      }
    }, 30);

  } else if (tab === 'performance') {
    body.innerHTML =
      '<div class="modal-graph-wrap">'
        + '<div class="modal-graph-title">CPU Usage %</div>'
        + '<div class="modal-graph-canvas"><canvas id="modal-chart-cpu"></canvas></div>'
      + '</div>'
      + '<div class="modal-graph-wrap" style="margin-top:12px">'
        + '<div class="modal-graph-title">Memory Usage %</div>'
        + '<div class="modal-graph-canvas"><canvas id="modal-chart-ram"></canvas></div>'
      + '</div>';

    setTimeout(() => {
      const hist = (pi.history || []).filter(h => h.cpu != null || h.ram != null);
      if (!hist.length) {
        body.innerHTML += '<div style="color:var(--muted);font-size:11px;text-align:center;padding:12px">No metric history yet \u2014 awaiting next heartbeat</div>';
        return;
      }
      const labels = hist.map(h => new Date(h.t).toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'}));
      const mkChart = (canvasId, data, color, unit) => {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return null;
        const grad = ctx.getContext('2d').createLinearGradient(0,0,0,100);
        grad.addColorStop(0, colorWithAlpha(color, 0.18));
        grad.addColorStop(1, colorWithAlpha(color, 0.0));
        return new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{
            data, borderColor: color, borderWidth: 2,
            fill: true, backgroundColor: grad, tension: 0.4,
            pointRadius: 0, pointHitRadius: 8, spanGaps: true,
          }]},
          options: {
            responsive:true, maintainAspectRatio:false,
            plugins:{legend:{display:false},tooltip:{
              backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
              titleColor:'rgba(255,255,255,0.5)',bodyColor:color,bodyFont:{weight:'bold'},
              callbacks:{label:c=>c.parsed.y.toFixed(1)+unit}
            }},
            scales:{
              x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:6}},
              y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:v=>v+unit},min:0,max:100,position:'right'}
            }
          }
        });
      };
      if (modalChartInst) { modalChartInst.destroy(); modalChartInst = null; }
      modalChartInst = mkChart('modal-chart-cpu', hist.map(h=>h.cpu), '#00f5d4', '%');
      mkChart('modal-chart-ram', hist.map(h=>h.ram), '#7b61ff', '%');
    }, 50);

  } else if (tab === 'logs') {
    const errors = pi.last_errors || [];
    body.innerHTML =
      '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Recent Errors</div>'
      + (errors.length
          ? '<div class="error-log">' + errors.map(e => escHtml(e)).join('\\n') + '</div>'
          : '<div class="error-log empty">\u2713 No recent errors</div>')
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin:14px 0 8px">Agent Status</div>'
      + renderAgents(pi.agents||{})
      + '<div style="font-size:10px;color:var(--muted);margin-top:12px">Full logs: ' + (pi.pi_id||'') + ':5001/logs</div>';

  } else if (tab === 'admin') {
    body.innerHTML =
      '<div class="modal-stats" style="grid-template-columns:1fr 1fr;margin-bottom:16px">'
        + '<div class="mstat"><div class="mstat-label">Node</div><div class="mstat-val" style="font-size:14px;word-break:break-all">' + (pi.label||'\u2014') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">Contact</div><div class="mstat-val" style="font-size:12px;word-break:break-all"><a href="mailto:' + (pi.email||'') + '" style="color:var(--teal)">' + (pi.email||'\u2014') + '</a></div></div>'
        + '<div class="mstat"><div class="mstat-label">Pi ID</div><div class="mstat-val" style="font-size:11px;font-family:var(--mono)">' + (pi.pi_id||'\u2014') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">First Seen</div><div class="mstat-val" style="font-size:12px">' + (pi.first_seen||'\u2014').slice(0,10) + '</div></div>'
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Update Keys on Pi</div>'
      + '<div style="font-size:11px;color:var(--amber);background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.15);border-radius:8px;padding:8px 10px;margin-bottom:12px">'
        + '&#9888; Keys are sent directly to the Pi portal at ' + (pi.pi_id||'?') + ':5001 and written to .env'
      + '</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Anthropic API Key</div>'
          + '<input id="adm-anthropic" type="password" placeholder="sk-ant-..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alpaca API Key</div>'
          + '<input id="adm-alpaca-key" type="password" placeholder="PK..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alpaca Secret</div>'
          + '<input id="adm-alpaca-secret" type="password" placeholder="Secret..." style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
        + '<div><div style="font-size:10px;color:var(--muted);margin-bottom:4px">Alert Email</div>'
          + '<input id="adm-alert-to" type="email" placeholder="node@email.com" style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
      + '</div>'
      + '<div style="display:flex;gap:8px;margin-bottom:16px">'
        + '<button data-piid="' + (pi.pi_id||'') + '" onclick="pushKeysToPi(this.dataset.piid)" style="padding:9px 18px;border-radius:10px;background:var(--teal2);border:1px solid rgba(0,245,212,0.3);color:var(--teal);font-size:11px;font-weight:700;cursor:pointer;font-family:var(--sans)">Push Keys to Pi</button>'
        + '<div id="adm-key-result-' + (pi.pi_id||'') + '" style="font-size:11px;color:var(--muted);align-self:center"></div>'
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Danger Zone</div>'
      + '<div style="display:flex;flex-direction:column;gap:8px">'
        + '<button data-piid="' + pi.pi_id + '" onclick="promptDelete(this.dataset.piid)" style="padding:10px 16px;border-radius:10px;background:var(--pink2);border:1px solid rgba(255,75,110,0.25);color:var(--pink);font-size:12px;font-weight:600;cursor:pointer;text-align:left;font-family:var(--sans)">Remove Node from Registry</button>'
      + '</div>';
  }
}


function renderAgents(agents) {
  // Known agent descriptive names — add entries as agents report in
  const knownNames = {
    'retail_trade_logic_agent':     'Trade Logic',
    'retail_news_agent':            'News',
    'retail_market_sentiment_agent':'Market Sentiment',
    'retail_sector_screener':       'Screener',
    'retail_scheduler':             'Scheduler',
    'retail_heartbeat':             'Heartbeat',
    'retail_watchdog':              'Watchdog',
    'retail_health_check':          'Health Check',
    // Legacy names (pre-rename)
    'trade_logic_agent':            'Trade Logic',
    'news_agent':                   'News',
    'market_sentiment_agent':       'Market Sentiment',
  };
  // Render whatever agents are reported (fall back to raw key if name unknown)
  const keys = Object.keys(agents);
  if (!keys.length) return '<div style="color:var(--muted);font-size:11px;padding:12px 0;text-align:center">No agent data received yet</div>';
  return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;overflow:hidden">'
    + keys.map((k,i) => {
      const status = agents[k];
      const label  = knownNames[k] || k;
      const isOk   = status && status !== 'fault' && status !== 'error';
      const isFault= status === 'fault' || status === 'error';
      const dotClr = isFault ? 'var(--pink)' : isOk ? 'var(--teal)' : 'var(--muted)';
      const dotGlw = isFault ? '0 0 5px var(--pink)' : isOk ? '0 0 5px var(--teal)' : 'none';
      const stClr  = isFault ? 'var(--pink)' : isOk ? 'rgba(255,255,255,0.45)' : 'var(--dim)';
      return '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;'
          + (i > 0 ? 'border-top:1px solid var(--border);' : '')
          + '">'
        + '<div style="width:7px;height:7px;border-radius:50%;flex-shrink:0;background:' + dotClr + ';box-shadow:' + dotGlw + '"></div>'
        + '<span style="font-size:11px;font-weight:600;font-family:var(--mono);color:var(--text);flex:1">' + label + '</span>'
        + '<span style="font-size:10px;font-family:var(--mono);color:' + stClr + '">' + (status||'—') + '</span>'
      + '</div>';
    }).join('')
    + '</div>';
}

function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── AGENT FLEET OVERVIEW ──
const AGENT_NAMES = {
  'retail_trade_logic_agent':'Trade Logic','retail_news_agent':'News',
  'retail_market_sentiment_agent':'Market Sentiment','retail_sector_screener':'Screener',
  'retail_scheduler':'Scheduler','retail_heartbeat':'Heartbeat',
  'retail_watchdog':'Watchdog','retail_health_check':'Health Check',
  'retail_boot_sequence':'Boot Sequence','retail_shutdown':'Shutdown',
  'retail_interrogation_listener':'Listener','retail_patch':'Patcher',
  'retail_backup':'Backup',
  'trade_logic_agent':'Trade Logic','news_agent':'News','market_sentiment_agent':'Market Sentiment',
  'synthos_monitor':'Monitor','scoop':'Scoop','strongbox':'Strongbox',
  'company_server':'Server','company_vault':'Vault','company_archivist':'Librarian',
  'company_sentinel':'Sentinel','company_keepalive':'Keepalive','company_auditor':'Auditor'
};

// Expected agents per node role — used to show inactive agents that haven't reported
const EXPECTED_AGENTS = {
  retail: [
    'retail_trade_logic_agent','retail_news_agent','retail_market_sentiment_agent',
    'retail_sector_screener','retail_scheduler','retail_heartbeat',
    'retail_watchdog','retail_health_check','retail_boot_sequence',
    'retail_shutdown','retail_interrogation_listener','retail_patch','retail_backup'
  ],
  company: [
    'company_server','scoop','strongbox','company_vault',
    'company_archivist','company_sentinel','company_keepalive','company_auditor'
  ],
  monitor: ['synthos_monitor']
};

function detectNodeRole(pi) {
  const agents = Object.keys(pi.agents || {});
  const id = (pi.pi_id || '').toLowerCase();
  const label = (pi.label || '').toLowerCase();
  if (agents.some(a => a.startsWith('retail_')) || id.includes('retail') || label.includes('retail'))
    return 'retail';
  if (agents.some(a => a.startsWith('company_') || a === 'scoop' || a === 'strongbox')
      || id.includes('company') || id.includes('pi4b') || label.includes('company'))
    return 'company';
  if (agents.includes('synthos_monitor') || id.includes('monitor') || label.includes('monitor'))
    return 'monitor';
  // Fallback: check for legacy agent names
  if (agents.some(a => a.includes('trade') || a.includes('news') || a.includes('sentiment')))
    return 'retail';
  return null;  // unknown role — only show reported agents
}

function agentStatusClass(s, ageSecs) {
  if (s === 'fault' || s === 'error') return 'fault';
  if (!s) return 'inactive';
  if (ageSecs > 900) return 'idle';  // >15 min since last heartbeat
  return 'active';
}

function renderAgentFleet() {
  const body  = document.getElementById('aft-body');
  const badge = document.getElementById('aft-badge');
  if (!body) return;

  const pis = Object.values(piData);
  const rows = [];
  pis.forEach(pi => {
    const reported = pi.agents || {};
    const ageSecs = pi.age_secs || 0;
    const lastSeen = pi.last_seen || '';
    const role = detectNodeRole(pi);

    // Start with all expected agents for this node role (marked inactive)
    const merged = {};
    if (role && EXPECTED_AGENTS[role]) {
      EXPECTED_AGENTS[role].forEach(k => { merged[k] = null; });
    }
    // Overlay reported agents on top
    Object.entries(reported).forEach(([k, v]) => { merged[k] = v; });

    Object.entries(merged).forEach(([key, status]) => {
      rows.push({
        agent: AGENT_NAMES[key] || key,
        agentKey: key,
        node: pi.label || pi.pi_id,
        status: agentStatusClass(status, ageSecs),
        rawStatus: status,
        lastSeen: status ? lastSeen : ''
      });
    });
  });

  if (!rows.length) {
    body.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:16px;text-align:center">No nodes registered yet</div>';
    badge.textContent = '0';
    return;
  }

  // Sort: fault first, then active, idle, inactive
  const sord = {fault:0, active:1, idle:2, inactive:3};
  rows.sort((a,b) => (sord[a.status]||9) - (sord[b.status]||9) || a.node.localeCompare(b.node));

  const activeCount = rows.filter(r => r.status === 'active').length;
  badge.textContent = activeCount + ' / ' + rows.length;
  body.innerHTML =
    '<div class="aft-row aft-thead"><div>Agent</div><div>Node</div><div>Status</div><div>Last</div></div>'
    + rows.map(r =>
      '<div class="aft-row">'
        + '<div class="aft-agent">' + escHtml(r.agent) + '</div>'
        + '<div class="aft-node">' + escHtml(r.node) + '</div>'
        + '<div class="aft-status"><div class="aft-dot s-' + r.status + '"></div><span class="aft-st s-' + r.status + '">' + r.status + '</span></div>'
        + '<div class="aft-time">' + (r.lastSeen ? ageSince(r.lastSeen) : '\u2014') + '</div>'
      + '</div>'
    ).join('');
}

// ── GLOBAL COMMANDS ──
let cmdConfirmType = null;
let cmdConfirmValue = null;

function confirmCmd(type, value, msg) {
  cmdConfirmType = type;
  cmdConfirmValue = value;
  document.getElementById('confirm-msg').textContent = msg;
  document.getElementById('confirm-overlay').classList.add('show');
}

async function sendGlobalCmd(type, value) {
  const statusEl = document.getElementById('cmd-status');
  try {
    statusEl.textContent = 'sending...';
    statusEl.style.color = 'var(--amber)';
    const body = type === 'kill-switch' ? {active: value} : {mode: value};
    const r = await fetch('/api/command/' + type, {
      method: 'POST',
      headers: {'X-Token': SECRET_TOKEN, 'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    if (r.ok) {
      const d = await r.json();
      toast('\u2713 ' + type + ' \u2192 ' + value + ' queued for ' + (d.queued_for||[]).length + ' nodes', 'ok');
      statusEl.textContent = 'queued';
      statusEl.style.color = 'var(--teal)';
    } else {
      toast('Command failed: HTTP ' + r.status, 'err');
      statusEl.textContent = 'failed';
      statusEl.style.color = 'var(--pink)';
    }
  } catch(e) {
    toast('Command error: ' + e.message, 'err');
    statusEl.textContent = 'error';
    statusEl.style.color = 'var(--pink)';
  }
  setTimeout(() => { statusEl.textContent = ''; }, 5000);
}

function updateCommandState(pis) {
  // Highlight buttons based on current fleet majority state
  const tm = pis.map(p => (p.trading_mode||'PAPER'));
  const allPaper = tm.every(m => m === 'PAPER');
  const allLive  = tm.every(m => m === 'LIVE');
  const om = pis.map(p => (p.operating_mode||'SUPERVISED'));
  const allSup   = om.every(m => m === 'SUPERVISED');
  const allAuto  = om.every(m => m === 'AUTONOMOUS');
  const ks = pis.map(p => !!p.kill_switch);
  const anyKill  = ks.some(k => k);
  const noKill   = ks.every(k => !k);

  const cls = (id, c, on) => { const el=document.getElementById(id); if(el){el.classList.remove('active-teal','active-amber','active-pink'); if(on) el.classList.add(c);} };
  cls('cmd-paper',      'active-teal',  allPaper && pis.length > 0);
  cls('cmd-live',       'active-amber', allLive  && pis.length > 0);
  cls('cmd-supervised', 'active-teal',  allSup   && pis.length > 0);
  cls('cmd-autonomous', 'active-amber', allAuto  && pis.length > 0);
  cls('cmd-kill-on',    'active-pink',  anyKill);
  cls('cmd-kill-off',   'active-teal',  noKill && pis.length > 0);
}

// ── DELETE ──
function promptDelete(piId) {
  pendingDelete = piId;
  document.getElementById('confirm-msg').textContent = 'Remove "' + piId + '" from the registry?';
  document.getElementById('confirm-overlay').classList.add('show');
}
function cancelDelete() {
  pendingDelete=null;
  cmdConfirmType=null;
  cmdConfirmValue=null;
  document.getElementById('confirm-overlay').classList.remove('show');
}
async function confirmDelete() {
  // Handle global command confirmation
  if (cmdConfirmType) {
    const t = cmdConfirmType, v = cmdConfirmValue;
    cmdConfirmType = null; cmdConfirmValue = null;
    document.getElementById('confirm-overlay').classList.remove('show');
    await sendGlobalCmd(t, v);
    return;
  }
  // Handle Pi delete confirmation
  if (!pendingDelete) return;
  try {
    await fetch('/api/delete/' + encodeURIComponent(pendingDelete), {
      method:'DELETE', headers:{'X-Token':SECRET_TOKEN}
    });
    toast('\u2713 Pi removed', 'ok');
  } catch(e) {}
  cancelDelete();
  closeModalBtn();
  fetchStatus();
}

// ── TODOS ──
async function fetchTodos() {
  try {
    const r = await fetch('/api/todos');
    if (!r.ok) return;
    allTodos = await r.json();
    allTodos.sort((a,b) => (SEV_ORDER[a.severity]??9) - (SEV_ORDER[b.severity]??9));
    renderTodos();
    updateFleetStats();
  } catch(e) {}
}

function renderTodos() {
  const el    = document.getElementById('todo-list');
  const badge = document.getElementById('todo-badge');
  const open  = allTodos.filter(t=>!t.resolved);
  badge.textContent = open.length > 0 ? open.length + ' open' : 'All clear';
  badge.className   = 'todo-count ' + (open.length > 0 ? '' : 'clear');
  if (!open.length) { el.innerHTML = '<div class="todo-empty">✓ No open issues</div>'; return; }
  const sevDot = {CRITICAL:'ts-crit',HIGH:'ts-high',MEDIUM:'ts-med',LOW:'ts-low'};
  el.innerHTML = open.slice(0,15).map(t =>
    '<div class="todo-item">'
      + '<div class="tsev ' + (sevDot[t.severity]||'ts-low') + '"></div>'
      + '<div class="todo-body">'
        + '<div class="todo-title-t">' + escHtml(t.title||'') + '</div>'
        + '<div class="todo-meta">' + (t.pi_id||'') + ' · ' + (t.date||'') + ' · ' + (t.category||'') + '</div>'
        + (t.action ? '<div class="todo-action">→ ' + escHtml(t.action) + '</div>' : '')
      + '</div>'
      + '<button class="resolve-btn" data-todoid="' + CSS.escape(t.id) + '" onclick="resolveTodo(this.dataset.todoid,event)">Done</button>'
    + '</div>'
  ).join('');
}

// ── PUSH KEYS TO PI ──
async function pushKeysToPi(piId) {
  const pi = piData[piId] || {};
  // Build Pi portal URL from known port
  const piUrl = 'http://' + (pi.pi_ip || piId.replace('synthos-','').replace(/-/g,'.')) + ':5001';

  const fields = {
    'ANTHROPIC_API_KEY': document.getElementById('adm-anthropic')?.value || '',
    'ALPACA_API_KEY':    document.getElementById('adm-alpaca-key')?.value || '',
    'ALPACA_SECRET_KEY': document.getElementById('adm-alpaca-secret')?.value || '',
    'ALERT_TO':          document.getElementById('adm-alert-to')?.value || '',
  };
  const data = Object.fromEntries(Object.entries(fields).filter(([,v]) => v.trim()));
  if (!Object.keys(data).length) {
    toast('Fill in at least one key field', 'err');
    return;
  }

  const result = document.getElementById('adm-key-result-' + piId);
  if (result) result.textContent = 'Pushing...';

  try {
    // POST directly to Pi portal's /api/keys endpoint
    const r = await fetch(piUrl + '/api/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
      mode: 'cors',
    });
    const d = await r.json();
    if (d.ok) {
      if (result) { result.textContent = '✓ Updated: ' + d.updated.join(', '); result.style.color = 'var(--teal)'; }
      toast('✓ Keys pushed to ' + (pi.label||piId), 'ok');
      // Clear fields
      ['adm-anthropic','adm-alpaca-key','adm-alpaca-secret','adm-alert-to'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
    } else {
      if (result) { result.textContent = '✗ ' + d.errors.join(', '); result.style.color = 'var(--pink)'; }
      toast('Push failed: ' + d.errors.join(', '), 'err');
    }
  } catch(e) {
    if (result) { result.textContent = '✗ Could not reach Pi portal'; result.style.color = 'var(--pink)'; }
    toast('Could not reach ' + piUrl, 'err');
  }
}

async function resolveTodo(id, e) {
  e.stopPropagation();
  await fetch('/api/todos/' + encodeURIComponent(id) + '/resolve', {
    method:'POST', headers:{'X-Token':SECRET_TOKEN}
  });
  await fetchTodos();
  toast('✓ Issue resolved', 'ok');
}

// ── FLEET CHARTS ──
const CHART_COLORS = ['#00f5d4','#7b61ff','#ffb347','#ff4b6e','#a78bfa','#67e8f9'];
let cpuChartInst = null;
let ramChartInst = null;

function buildFleetCharts() {
  const pis  = Object.values(piData).filter(p => p.history && p.history.length > 1);
  if (!pis.length) return;

  const cpuCtx = document.getElementById('cpu-chart');
  const ramCtx = document.getElementById('ram-chart');
  if (!cpuCtx || !ramCtx) return;

  // Use the longest history for labels
  const refPi = pis.reduce((a,b) => a.history.length >= b.history.length ? a : b);
  const labels = refPi.history.map(h =>
    new Date(h.t).toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'})
  );

  const mkDatasets = (histKey, alpha) => pis
    .filter(p => p.history.some(h => h[histKey] != null))
    .map((pi, i) => {
      const color = CHART_COLORS[i % CHART_COLORS.length];
      return {
        label: pi.label || pi.pi_id,
        data:  pi.history.map(h => h[histKey] != null ? h[histKey] : null),
        borderColor: color, borderWidth: 2,
        fill: true, backgroundColor: colorWithAlpha(color, alpha),
        tension: 0.4, pointRadius: 0, pointHitRadius: 8, spanGaps: true,
      };
    });

  const chartOpts = unit => ({
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { display: pis.length > 1, position: 'bottom',
                labels:{color:'rgba(255,255,255,0.4)',font:{size:9},boxWidth:8,padding:8}},
      tooltip: {
        backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(255,255,255,0.1)',borderWidth:1,
        titleColor:'rgba(255,255,255,0.5)',bodyColor:'rgba(255,255,255,0.85)',
        callbacks:{label:c=>(c.dataset.label||'')+': '+c.parsed.y.toFixed(1)+unit}
      }
    },
    scales: {
      x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:8}},
      y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:v=>v+unit},min:0,max:100,position:'right'}
    }
  });

  if (cpuChartInst) cpuChartInst.destroy();
  if (ramChartInst) ramChartInst.destroy();

  const cpuDs = mkDatasets('cpu', 0.1);
  const ramDs = mkDatasets('ram', 0.1);
  if (cpuDs.length) cpuChartInst = new Chart(cpuCtx, {type:'line', data:{labels,datasets:cpuDs}, options:chartOpts('%')});
  if (ramDs.length) ramChartInst = new Chart(ramCtx, {type:'line', data:{labels,datasets:ramDs}, options:chartOpts('%')});
}

// ── COUNTDOWN ──
let countdown = 30;
function tickCountdown() {
  countdown--;
  if (countdown <= 0) { countdown = 30; fetchStatus(); }
}

// ── INIT ──
/* DBG */ try { document.getElementById('dbg-keys').textContent = 'INIT REACHED'; } catch(e){}
fetchStatus();
fetchTodos();
setInterval(tickCountdown, 1000);
setInterval(fetchTodos, 120000);
function toggleMenu(){const m=document.getElementById('hmenu');m.classList.toggle('open')}
document.addEventListener('click',function(e){if(!document.getElementById('hbtn').contains(e.target)&&!document.getElementById('hmenu').contains(e.target)){document.getElementById('hmenu').classList.remove('open')}});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# COMPANY SERVER ROUTES (merged from company_server.py)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/queue", methods=["GET"])
def api_queue():
    """
    Inspect the scoop_queue.

    Query params:
      status  — filter by status (default: pending)
      pi_id   — filter by source Pi
      limit   — max rows (default: 50, max: 200)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    status = request.args.get("status", "pending")
    pi_id  = request.args.get("pi_id")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    try:
        with _db_conn() as conn:
            if pi_id:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? AND pi_id=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, pi_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, limit),
                ).fetchall()

            counts = {
                r["status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
                ).fetchall()
            }

        return jsonify({
            "queue":  [dict(r) for r in rows],
            "counts": counts,
            "filter": {"status": status, "pi_id": pi_id, "limit": limit},
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/skip", methods=["POST"])
def api_queue_skip(item_id):
    """Mark a pending queue item as skipped (won't be dispatched by Scoop)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='skipped', dispatched_at=? "
                "WHERE id=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(), item_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in pending state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "skipped"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/retry", methods=["POST"])
def api_queue_retry(item_id):
    """Reset a failed item back to pending so Scoop will retry it."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='pending', dispatch_attempts=0, "
                "error_msg=NULL, dispatched_at=NULL "
                "WHERE id=? AND status='failed'",
                (item_id,),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in failed state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "pending"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Node</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --green:#00f5d4;--red:#ff4b6e;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:56px;
  display:flex;align-items:center;gap:12px;
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
          color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
              padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);
              background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
           background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
           font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);
          box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* STAT CARDS */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.stat-card{
  padding:16px;border-radius:14px;
  border:1px solid var(--border);background:var(--surface);
  position:relative;overflow:hidden;
}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* TOOLBAR */
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:var(--muted);display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}
.tab-row{display:flex;gap:4px}
.tab-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
         cursor:pointer;border:1px solid var(--border);background:transparent;
         color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.tab-btn.active,.tab-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal);background:rgba(0,245,212,0.06)}
.ml-auto{margin-left:auto}
.refresh-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
             cursor:pointer;border:1px solid var(--border);background:transparent;
             color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.refresh-btn:hover{border-color:var(--border2);color:var(--text)}

/* TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{
  padding:10px 14px;text-align:left;
  font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);border-bottom:1px solid var(--border);
  white-space:nowrap;
}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:10px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px}
.empty-row td{text-align:center;color:var(--muted);padding:32px;font-style:italic}

/* BADGES */
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
       padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-pending{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-sent{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.b-failed{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.25);color:var(--pink)}
.b-skipped{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}
.b-p0{background:rgba(255,75,110,0.15);border-color:rgba(255,75,110,0.35);color:var(--pink)}
.b-p1{background:rgba(255,179,71,0.12);border-color:rgba(255,179,71,0.3);color:var(--amber)}
.b-p2{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-p3{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* ACTION BUTTONS */
.act-btn{font-size:9px;font-weight:700;padding:2px 8px;border-radius:6px;
         background:transparent;border:1px solid var(--border);color:var(--muted);
         cursor:pointer;font-family:var(--sans);transition:all 0.15s}
.act-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal)}
.act-btn.danger:hover{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* TOAST */
#toast{
  position:fixed;bottom:20px;right:20px;z-index:999;
  padding:10px 16px;border-radius:10px;font-size:12px;font-weight:600;
  background:var(--surface);border:1px solid var(--border2);color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,0.4);
  transform:translateY(60px);opacity:0;transition:all 0.3s;
  pointer-events:none;
}
#toast.show{transform:translateY(0);opacity:1}
#toast.ok{border-color:rgba(0,245,212,0.3);color:var(--teal)}
#toast.err{border-color:rgba(255,75,110,0.3);color:var(--pink)}
</style>
</head>
<body>

<div class="header">
  <span class="wordmark">SYNTHOS</span>
  <span class="header-badge">Company Node</span>
  <div class="header-right">
    <a href="/display" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="Display control">Display</a>
    <a href="/project-status" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="Project status">Status</a>
    <a href="/logs" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="View logs">Logs</a>
    <span class="clock" id="clock">--:--:-- ET</span>
    <div class="live-pill"><div class="live-dot"></div>LIVE</div>
  </div>
</div>

<div class="page">

  <!-- STAT CARDS -->
  <div class="stat-grid" id="stat-grid">
    <div class="stat-card sc-purple">
      <div class="stat-label">Pending</div>
      <div class="stat-val" id="cnt-pending">—</div>
      <div class="stat-sub">awaiting Scoop</div>
    </div>
    <div class="stat-card sc-teal">
      <div class="stat-label">Sent</div>
      <div class="stat-val" id="cnt-sent">—</div>
      <div class="stat-sub">dispatched ok</div>
    </div>
    <div class="stat-card sc-pink">
      <div class="stat-label">Failed</div>
      <div class="stat-val" id="cnt-failed">—</div>
      <div class="stat-sub">dispatch errors</div>
    </div>
    <div class="stat-card sc-muted">
      <div class="stat-label">Skipped</div>
      <div class="stat-val" id="cnt-skipped">—</div>
      <div class="stat-sub">manually resolved</div>
    </div>
    <div class="stat-card sc-amber">
      <div class="stat-label">Total</div>
      <div class="stat-val" id="cnt-total">—</div>
      <div class="stat-sub">all time</div>
    </div>
  </div>

  <!-- QUEUE TABLE -->
  <div style="margin-bottom:12px">
    <div class="sec-title" style="margin-bottom:12px">Scoop Queue</div>
    <div class="toolbar">
      <div class="tab-row" id="status-tabs">
        <button class="tab-btn active" onclick="setStatus('pending',this)">Pending</button>
        <button class="tab-btn" onclick="setStatus('failed',this)">Failed</button>
        <button class="tab-btn" onclick="setStatus('sent',this)">Sent</button>
        <button class="tab-btn" onclick="setStatus('skipped',this)">Skipped</button>
      </div>
      <button class="refresh-btn ml-auto" onclick="refresh()">↻ Refresh</button>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Priority</th>
          <th>Event Type</th>
          <th>Subject</th>
          <th>Source Agent</th>
          <th>Pi</th>
          <th>Audience</th>
          <th>Status</th>
          <th>Queued</th>
          <th>Attempts</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="queue-body">
        <tr class="empty-row"><td colspan="10">Loading…</td></tr>
      </tbody>
    </table>
  </div>

</div>

<div id="toast"></div>

<script>
const TOKEN = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('company_token='))?.split('=')[1] || '';
let currentStatus = 'pending';

function clock(){
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false}) + ' ET';
}
clock(); setInterval(clock,1000);

function toast(msg, type='ok'){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + type;
  setTimeout(()=>{ t.className = ''; }, 3000);
}

function priorityBadge(p){
  const labels = {0:'P0 CRIT',1:'P1 HIGH',2:'P2 MED',3:'P3 LOW'};
  const cls    = {0:'b-p0',1:'b-p1',2:'b-p2',3:'b-p3'};
  return `<span class="badge ${cls[p]||'b-p3'}">${labels[p]||'P'+p}</span>`;
}

function statusBadge(s){
  const cls = {pending:'b-pending',sent:'b-sent',failed:'b-failed',skipped:'b-skipped'};
  return `<span class="badge ${cls[s]||''}">${s.toUpperCase()}</span>`;
}

function fmtTime(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-US',{timeZone:'America/New_York',hour12:false,
    month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function actionBtns(item){
  const id = item.id;
  if(item.status === 'pending'){
    return `<button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  if(item.status === 'failed'){
    return `<button class="act-btn" onclick="retryItem('${id}')">Retry</button>
            <button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  return '—';
}

async function fetchQueue(status){
  const r = await fetch(`/api/queue?status=${status}&limit=100`,{
    headers:{'X-Token': TOKEN}
  });
  return r.json();
}

async function fetchHealth(){
  const r = await fetch('/health');
  return r.json();
}

async function refresh(){
  // Update counts
  try {
    const h = await fetchHealth();
    const counts = h.queue || {};
    ['pending','sent','failed','skipped'].forEach(s=>{
      const el = document.getElementById('cnt-'+s);
      if(el) el.textContent = counts[s] || 0;
    });
    const total = Object.values(counts).reduce((a,b)=>a+b,0);
    document.getElementById('cnt-total').textContent = total;
  } catch(e){}

  // Update queue table
  try {
    const data = await fetchQueue(currentStatus);
    const items = data.queue || [];
    const tbody = document.getElementById('queue-body');
    if(!items.length){
      tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No ${currentStatus} items</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(item=>`
      <tr>
        <td>${priorityBadge(item.priority)}</td>
        <td class="mono">${item.event_type||'—'}</td>
        <td style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
            title="${(item.subject||'').replace(/"/g,'&quot;')}">${item.subject||'—'}</td>
        <td>${item.source_agent||'—'}</td>
        <td class="mono" style="font-size:10px">${item.pi_id||'—'}</td>
        <td>${item.audience||'customer'}</td>
        <td>${statusBadge(item.status)}</td>
        <td class="mono" style="font-size:10px">${fmtTime(item.queued_at)}</td>
        <td style="text-align:center">${item.dispatch_attempts||0}</td>
        <td>${actionBtns(item)}</td>
      </tr>
    `).join('');
  } catch(e){
    document.getElementById('queue-body').innerHTML =
      `<tr class="empty-row"><td colspan="10">Failed to load queue</td></tr>`;
  }
}

async function skipItem(id){
  const r = await fetch(`/api/queue/${id}/skip`,{method:'POST',headers:{'X-Token':TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item skipped'); refresh(); }
  else toast(j.error||'Skip failed','err');
}

async function retryItem(id){
  const r = await fetch(`/api/queue/${id}/retry`,{method:'POST',headers:{'X-Token':TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item queued for retry'); refresh(); }
  else toast(j.error||'Retry failed','err');
}

function setStatus(status, btn){
  currentStatus = status;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  refresh();
}

// Auto-refresh every 15 seconds
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


# ── Logs page ─────────────────────────────────────────────────────────────────
_COMPANY_LOGS_CSS = (
    '<style>'
    '*{box-sizing:border-box;margin:0;padding:0}'
    'body{background:#080b12;color:#e0ddd8;font-family:sans-serif;min-height:100vh}'
    'header{background:#0e1220;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;'
    '       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;'
    '       border-bottom:1px solid #1a2030}'
    '.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}'
    '.nav{display:flex;gap:1rem;align-items:center}'
    '.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}'
    '.nav a:hover{color:#aaa}'
    '.tabs{display:flex;gap:0;border-bottom:1px solid #1a2030;padding:0 2rem;'
    '      background:#0e1220;overflow-x:auto;flex-wrap:nowrap}'
    '.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;'
    '          background:#0e1220;border-bottom:1px solid #1a2030}'
    '.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}'
    'select{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1a2030;'
    '       border-radius:6px;color:#e0ddd8}'
    '.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;'
    '         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;'
    '         min-height:calc(100vh - 140px)}'
    '.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;'
    '             padding:0.3rem 0.75rem;border:1px solid #1a2030;'
    '             border-radius:6px;cursor:pointer;background:transparent;color:#556}'
    '.refresh-btn:hover{background:#1a2030;color:#e0ddd8}'
    '</style>'
)

_COMPANY_LOG_FILES = {
    'scoop':       'scoop.log',
    'server':      'company_server.log',
    'monitor':     'monitor.log',
    'node_health': 'node_health.log',
}


# ── Retail backup receiver ────────────────────────────────────────────────────
_BUILD_DIR    = os.path.dirname(_HERE)   # synthos_build/ (parent of src/)
_STAGING_ROOT = os.path.join(_BUILD_DIR, ".backup_staging")


@app.route("/receive_backup", methods=["POST"])
def receive_backup():
    """
    Accept a .tar.gz backup archive from a retail Pi and stage it for Strongbox.
    Auth: X-Token header or token cookie (same as /console).
    Form fields: pi_id (str), archive (file)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    pi_id = (request.form.get("pi_id") or "").strip()
    if not pi_id or "/" in pi_id or ".." in pi_id:
        return jsonify({"error": "valid pi_id required"}), 400

    f = request.files.get("archive")
    if not f:
        return jsonify({"error": "archive file required"}), 400

    staging_dir = os.path.join(_STAGING_ROOT, pi_id)
    os.makedirs(staging_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = f"synthos_backup_{pi_id}_{date_str}.tar.gz"
    fpath = os.path.join(staging_dir, fname)
    f.save(fpath)

    size_kb = os.path.getsize(fpath) / 1024
    print(f"[Company] Staged backup: {fname} ({size_kb:.1f} KB) from pi_id={pi_id}")
    return jsonify({"ok": True, "staged": fname, "size_kb": round(size_kb, 1)}), 200


@app.route("/logs")
def company_logs():
    """Tail company-side log files — same token auth as console."""
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos Company Logs</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> "
            "or set <code>X-Token</code> header to access logs.</p>"
            "</body></html>"
        ), 401

    selected = request.args.get('file', 'scoop')
    try:
        lines = int(request.args.get('lines', 100))
    except (ValueError, TypeError):
        lines = 100
    fname    = _COMPANY_LOG_FILES.get(selected, 'scoop.log')
    fpath    = os.path.join(LOG_DIR, fname)

    content = ''
    if os.path.exists(fpath):
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            content = ''.join(all_lines[-lines:])
        except Exception as e:
            content = f'Error reading log: {e}'
    else:
        content = f'Log file not found: {fpath}'

    tabs = ''.join(
        f'<a href="/logs?file={k}&lines={lines}" '
        f'style="padding:6px 14px;font-family:monospace;font-size:0.72rem;'
        f'letter-spacing:0.08em;text-transform:uppercase;text-decoration:none;'
        f'border-bottom:2px solid {"#00f5d4" if k == selected else "transparent"};'
        f'color:{"#00f5d4" if k == selected else "#556"};display:inline-block">{k}</a>'
        for k in _COMPANY_LOG_FILES
    )

    line_opts = ''.join(
        f'<option value="{n}" {"selected" if n == lines else ""}>{n} lines</option>'
        for n in [50, 100, 200, 500]
    )

    log_escaped = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Logs</title>
{_COMPANY_LOGS_CSS}
</head>
<body>
<header>
  <div class="wordmark">SYNTHOS · COMPANY LOGS</div>
  <div class="nav">
    <a href="/console">&#8592; Console</a>
    <a href="/logs?file={selected}&lines={lines}" onclick="location.reload();return false">&#8635; Refresh</a>
  </div>
</header>
<div class="tabs">{tabs}</div>
<div class="controls">
  <label>Lines</label>
  <select onchange="window.location='/logs?file={selected}&lines='+this.value">{line_opts}</select>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  <span style="font-size:0.72rem;color:#556;margin-left:auto">{fname}</span>
</div>
<div class="log-box" id="log-content">{log_escaped}</div>
<script>
  document.getElementById('log-content').scrollIntoView({{block:'end'}});
</script>
</body>
</html>"""

    return html


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Command</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c14;--surface:#111520;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.13);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;--mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:2rem}
.card{width:100%;max-width:340px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:2rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:20%;right:20%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,245,212,0.3),transparent)}
.logo{font-family:var(--mono);font-size:0.85rem;font-weight:500;letter-spacing:0.18em;color:var(--teal);text-shadow:0 0 18px rgba(0,245,212,0.35);margin-bottom:0.25rem}
.sub{font-size:0.75rem;color:var(--muted);margin-bottom:1.75rem}
label{display:block;font-size:0.62rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem}
input{font-family:var(--mono);font-size:0.82rem;width:100%;padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:7px;color:var(--text);margin-bottom:0.9rem;transition:border-color .15s}
input:focus{outline:none;border-color:rgba(0,245,212,0.4);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
input::placeholder{color:var(--dim)}
.btn{font-family:var(--mono);font-size:0.75rem;font-weight:500;letter-spacing:0.07em;width:100%;padding:0.55rem;background:rgba(0,245,212,0.1);color:var(--teal);border:1px solid rgba(0,245,212,0.25);border-radius:7px;cursor:pointer;transition:all .15s}
.btn:hover{background:rgba(0,245,212,0.18);box-shadow:0 0 14px rgba(0,245,212,0.15)}
.err{font-size:0.72rem;color:#ff4b6e;margin-bottom:0.8rem;padding:0.4rem 0.6rem;background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);border-radius:6px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">SYNTHOS</div>
  <div class="sub">Command Node</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" autocomplete="current-password" required>
    <button class="btn" type="submit">Sign In →</button>
  </form>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if _authorized():
        return redirect(url_for("console"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        # Validate credentials
        email_ok = email == ADMIN_EMAIL
        pw_ok = (ADMIN_PW_HASH and check_password_hash(ADMIN_PW_HASH, password)) or \
                (not ADMIN_PW_HASH and password == SECRET_TOKEN)
        if email_ok and pw_ok:
            session.clear()
            session["logged_in"] = True
            session["email"] = email
            session.permanent = True
            return redirect(url_for("console"))
        return render_template_string(_LOGIN_HTML, error="Incorrect email or password")
    return render_template_string(_LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@app.route("/console")
def console():
    """Ops dashboard — requires login, Cloudflare Access, or SECRET_TOKEN."""
    # Legacy token-in-URL still works (sets cookie for API calls)
    if request.args.get("token"):
        resp = redirect(url_for("console"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return redirect(url_for("login"))
    return render_template_string(DASHBOARD_HTML)


# ── Project Status ────────────────────────────────────────────────────────────
_STATUS_JSON        = os.path.join(os.path.dirname(_HERE), "data", "project_status.json")
_GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "")
_GITHUB_OWNER       = os.getenv("GITHUB_REPO_OWNER", "personalprometheus-blip")
_GITHUB_STATUS_REPO = os.getenv("GITHUB_STATUS_REPO", "synthos-company")
_GITHUB_STATUS_PATH = os.getenv("GITHUB_STATUS_PATH", "data/project_status.json")
_STATUS_CACHE_TTL   = int(os.getenv("PROJECT_STATUS_TTL", "300"))   # seconds (default 5 min)

_status_cache: dict = {"data": None, "fetched_at": None, "source": "none"}


def _fetch_status_from_github():
    """
    Fetch project_status.json from the GitHub API.
    Returns the parsed dict on success, None on failure.
    Requires GITHUB_TOKEN in .env (read:contents scope is sufficient).
    Works for both public and private repos.
    """
    import urllib.request as _urllib_req
    import base64 as _b64
    import time as _time

    if not _GITHUB_TOKEN:
        return None

    url = (
        f"https://api.github.com/repos/{_GITHUB_OWNER}"
        f"/{_GITHUB_STATUS_REPO}/contents/{_GITHUB_STATUS_PATH}"
    )
    req = _urllib_req.Request(url, headers={
        "Authorization": f"token {_GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "synthos-company-server/1.0",
    })
    try:
        with _urllib_req.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        content = _b64.b64decode(payload["content"]).decode("utf-8")
        data = json.loads(content)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "github"
        print(f"[Company] project_status.json refreshed from GitHub")
        return data
    except Exception as exc:
        print(f"[Company] GitHub status fetch failed: {exc}")
        return None


def _get_status_data():
    """
    Return (data, source, cache_age_seconds).
    Priority: warm cache → GitHub API → local file → None.
    """
    import time as _time

    # Serve warm cache if within TTL
    if _status_cache["data"] and _status_cache["fetched_at"]:
        age = _time.time() - _status_cache["fetched_at"]
        if age < _STATUS_CACHE_TTL:
            return _status_cache["data"], _status_cache["source"], age

    # Try GitHub
    data = _fetch_status_from_github()
    if data:
        return data, "github", 0

    # Fall back to local file
    try:
        with open(_STATUS_JSON, "r") as fh:
            data = json.load(fh)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "local"
        print("[Company] project_status.json loaded from local file (GitHub unavailable)")
        return data, "local", 0
    except Exception as exc:
        print(f"[Company] project_status.json load failed: {exc}")
        return None, "error", 0

_STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Project Status</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{position:sticky;top:0;z-index:200;background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:16px}
.nav-link{font-size:11px;letter-spacing:0.06em;color:var(--muted);text-decoration:none;transition:color 0.15s}
.nav-link:hover{color:var(--text)}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
  background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* HERO PROGRESS */
.hero{padding:28px;border-radius:16px;border:1px solid var(--border);background:var(--surface);margin-bottom:24px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.hero-title{font-size:1.4rem;font-weight:700;letter-spacing:-0.3px}
.hero-meta{font-size:11px;color:var(--muted);margin-top:4px}
.phase-badge{font-family:var(--mono);font-size:11px;font-weight:600;padding:6px 14px;border-radius:8px;
  background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.milestone-chip{font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:4px 10px;border-radius:99px;background:rgba(255,179,71,0.1);border:1px solid rgba(255,179,71,0.25);color:var(--amber)}
.progress-label{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.progress-label span{font-size:11px;color:var(--muted)}
.progress-label strong{font-family:var(--mono);font-size:13px;color:var(--teal)}
.progress-track{height:6px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden}
.progress-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),rgba(0,245,212,0.6));transition:width 0.6s ease}

/* STAT GRID */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
.stat-card{padding:16px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* SECTION */
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);
  display:flex;align-items:center;gap:8px;margin-bottom:12px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* PHASE GRID */
.phase-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin-bottom:24px}
.phase-card{padding:18px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.phase-card.active{border-color:rgba(0,245,212,0.2);background:rgba(0,245,212,0.03)}
.phase-card.active::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.phase-card.not_started{opacity:0.5}
.phase-header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.phase-num{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 7px;border-radius:6px;
  background:rgba(255,255,255,0.05);color:var(--muted)}
.phase-name{font-size:13px;font-weight:600;flex:1}
.phase-status{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 8px;border-radius:99px;border:1px solid}
.ps-complete{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.ps-in_progress{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.ps-not_started{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.task-list{list-style:none;display:flex;flex-direction:column;gap:5px}
.task-item{display:flex;align-items:flex-start;gap:8px;font-size:11px;line-height:1.4}
.task-check{flex-shrink:0;width:14px;height:14px;margin-top:1px;border-radius:3px;border:1px solid;
  display:flex;align-items:center;justify-content:center;font-size:8px}
.task-check.done{background:rgba(0,245,212,0.15);border-color:rgba(0,245,212,0.4);color:var(--teal)}
.task-check.pending{background:rgba(255,255,255,0.03);border-color:var(--dim);color:transparent}
.task-text.done{color:var(--muted)}
.task-text.pending{color:var(--text)}
.phase-prog{margin-top:12px;padding-top:10px;border-top:1px solid var(--border)}
.phase-prog-track{height:3px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;margin-top:4px}
.phase-prog-fill{height:100%;border-radius:99px}
.ppf-complete{background:var(--teal)}
.ppf-in_progress{background:linear-gradient(90deg,var(--teal),var(--amber))}
.ppf-not_started{background:rgba(255,255,255,0.1)}
.phase-prog-label{font-size:10px;color:var(--dim)}

/* AGENT TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{padding:10px 14px;text-align:left;font-size:9px;font-weight:700;letter-spacing:0.1em;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:9px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
  padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-built{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-planned{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.b-done{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-pending{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.b-hold{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* SECURITY GRID */
.sec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-bottom:24px}
.sec-item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;
  border:1px solid var(--border);background:var(--surface)}
.sec-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sec-dot.done{background:var(--teal);box-shadow:0 0 6px rgba(0,245,212,0.5)}
.sec-dot.pending{background:var(--amber);box-shadow:0 0 6px rgba(255,179,71,0.4)}
.sec-dot.hold{background:var(--dim)}
.sec-info{flex:1;min-width:0}
.sec-label{font-size:11px;font-weight:500}
.sec-meta{font-size:10px;color:var(--muted);margin-top:1px}

/* BLOCKERS */
.blocker-empty{padding:24px;text-align:center;color:var(--muted);font-size:12px;font-style:italic}

/* FOOTER */
.footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.footer-left{display:flex;flex-direction:column;gap:3px}
.footer-note{font-size:10px;color:var(--dim)}
.source-bar{display:flex;align-items:center;gap:8px}
.source-chip{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;border:1px solid}
.source-github{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.source-local{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.source-error{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.3);color:var(--pink)}
.source-age{font-size:10px;color:var(--dim)}
.footer-right{display:flex;align-items:center;gap:10px}
.refresh-gh-btn{font-size:10px;font-weight:600;padding:5px 12px;border-radius:7px;cursor:pointer;
  border:1px solid rgba(0,245,212,0.25);background:rgba(0,245,212,0.05);color:var(--teal);
  font-family:var(--sans);transition:all 0.15s}
.refresh-gh-btn:hover{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.4)}
.refresh-gh-btn:disabled{opacity:0.4;cursor:not-allowed}
.footer-link{font-size:10px;color:var(--muted);text-decoration:none}
.footer-link:hover{color:var(--text)}
</style>
</head>
<body>

<div class="header">
  <span class="wordmark">SYNTHOS</span>
  <span class="header-badge">Project Status</span>
  <div class="header-right">
    <a href="/console" class="nav-link">Console</a>
    <a href="/logs" class="nav-link">Logs</a>
    <span class="clock" id="clock">--:--:-- ET</span>
    <div class="live-pill"><div class="live-dot"></div>LIVE</div>
  </div>
</div>

<div class="page" id="root">
  <p style="color:var(--muted);font-size:12px;padding:40px;text-align:center">Loading…</p>
</div>

<script>
const ET_ZONE = 'America/New_York';

function fmtClock(){
  const s=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:ET_ZONE,hour12:false});
  document.getElementById('clock').textContent=s+' ET';
}
setInterval(fmtClock,1000); fmtClock();

function statusCls(s){return{complete:'ps-complete',in_progress:'ps-in_progress',not_started:'ps-not_started'}[s]||'ps-not_started'}
function statusLabel(s){return{complete:'Complete',in_progress:'In Progress',not_started:'Not Started'}[s]||s}

function phaseCard(p){
  const done=p.tasks.filter(t=>t.done).length, total=p.tasks.length;
  const pct=total?Math.round(done/total*100):0;
  const active=p.status==='in_progress';
  const tasks=p.tasks.map(t=>`
    <li class="task-item">
      <span class="task-check ${t.done?'done':'pending'}">${t.done?'✓':''}</span>
      <span class="task-text ${t.done?'done':'pending'}">${t.label}</span>
    </li>`).join('');
  return `
    <div class="phase-card ${p.status}${active?' active':''}">
      <div class="phase-header">
        <span class="phase-num">P${p.id}</span>
        <span class="phase-name">${p.name}</span>
        <span class="phase-status ${statusCls(p.status)}">${statusLabel(p.status)}</span>
      </div>
      <ul class="task-list">${tasks}</ul>
      <div class="phase-prog">
        <div class="phase-prog-label">${done} / ${total} tasks · ${pct}%</div>
        <div class="phase-prog-track">
          <div class="phase-prog-fill ppf-${p.status}" style="width:${pct}%"></div>
        </div>
      </div>
    </div>`;
}

function agentRows(agents, label){
  return agents.map(a=>`
    <tr>
      <td><span class="badge ${a.status==='built'?'b-built':'b-planned'}">${a.status}</span></td>
      <td class="mono">${a.alias}</td>
      <td class="mono">${a.file}</td>
      <td style="color:var(--muted);font-size:11px">${a.job}</td>
    </tr>`).join('');
}

function secItems(items){
  return items.map(s=>`
    <div class="sec-item">
      <div class="sec-dot ${s.status}"></div>
      <div class="sec-info">
        <div class="sec-label">${s.item}</div>
        <div class="sec-meta">${s.repo} &middot; <span class="badge b-${s.status}" style="font-size:8px">${s.status}</span></div>
      </div>
    </div>`).join('');
}

function fmtAge(s){
  if(s<60) return `${Math.round(s)}s ago`;
  if(s<3600) return `${Math.round(s/60)}m ago`;
  return `${Math.round(s/3600)}h ago`;
}

async function refreshFromGitHub(){
  const btn=document.getElementById('gh-refresh-btn');
  if(btn){btn.disabled=true;btn.textContent='Refreshing…'}
  try{
    const r=await fetch('/api/project-status/refresh',{method:'POST',headers:{'X-Token':window._token||''}});
    const d=await r.json();
    if(d.ok) await render();
    else console.warn('Refresh failed:',d.error);
  }catch(e){console.error(e)}
  finally{if(btn){btn.disabled=false;btn.textContent='↻ Refresh from GitHub'}}
}

// Pull token from cookie for XHR auth
window._token=(document.cookie.split(';').find(c=>c.trim().startsWith('company_token='))||'').split('=')[1]||'';

async function render(){
  const d=await fetch('/api/project-status',{headers:{'X-Token':window._token}}).then(r=>r.json());
  const m=d.meta;
  const phases=d.phases;
  const phasesComplete=phases.filter(p=>p.status==='complete').length;
  const phasePct=Math.round(phasesComplete/m.total_phases*100);
  const currentPhase=phases.find(p=>p.status==='in_progress')||phases[m.current_phase-1];
  const allTasks=phases.flatMap(p=>p.tasks);
  const tasksDone=allTasks.filter(t=>t.done).length;
  const agentBuilt=[...d.agents.retail_pi,...d.agents.company_pi].filter(a=>a.status==='built').length;
  const agentTotal=[...d.agents.retail_pi,...d.agents.company_pi].length;
  const secDone=d.security.filter(s=>s.status==='done').length;
  const secTotal=d.security.length;
  const blockers=d.blockers||[];

  document.getElementById('root').innerHTML = `
    <!-- HERO -->
    <div class="hero">
      <div class="hero-top">
        <div>
          <div class="hero-title">Synthos Build Progress</div>
          <div class="hero-meta">Last updated ${m.last_updated} &middot; v${m.version}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          ${m.next_milestone?`<span class="milestone-chip">Next: ${m.next_milestone}</span>`:''}
          <span class="phase-badge">Phase ${m.current_phase} of ${m.total_phases}</span>
        </div>
      </div>
      <div class="progress-label">
        <span>Phase progress</span>
        <strong>${phasesComplete} / ${m.total_phases} phases complete</strong>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${phasePct}%"></div>
      </div>
    </div>

    <!-- STATS -->
    <div class="stat-grid">
      <div class="stat-card sc-teal">
        <div class="stat-label">Phases Done</div>
        <div class="stat-val">${phasesComplete}</div>
        <div class="stat-sub">of ${m.total_phases} total</div>
      </div>
      <div class="stat-card sc-purple">
        <div class="stat-label">Tasks Done</div>
        <div class="stat-val">${tasksDone}</div>
        <div class="stat-sub">of ${allTasks.length} total</div>
      </div>
      <div class="stat-card sc-amber">
        <div class="stat-label">Agents Built</div>
        <div class="stat-val">${agentBuilt}</div>
        <div class="stat-sub">of ${agentTotal} total</div>
      </div>
      <div class="stat-card ${secDone===secTotal?'sc-teal':'sc-pink'}">
        <div class="stat-label">Security</div>
        <div class="stat-val">${secDone}</div>
        <div class="stat-sub">of ${secTotal} items done</div>
      </div>
      <div class="stat-card ${blockers.length?'sc-pink':'sc-muted'}">
        <div class="stat-label">Blockers</div>
        <div class="stat-val">${blockers.length}</div>
        <div class="stat-sub">${blockers.length?'active':'all clear'}</div>
      </div>
    </div>

    <!-- PHASES -->
    <div class="sec-title">Build Phases</div>
    <div class="phase-grid">${phases.map(phaseCard).join('')}</div>

    <!-- SECURITY -->
    <div class="sec-title">Security Checklist</div>
    <div class="sec-grid">${secItems(d.security)}</div>

    <!-- AGENTS -->
    <div class="sec-title">Agent Registry — Retail Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.retail_pi)}</tbody>
      </table>
    </div>

    <div class="sec-title">Agent Registry — Company Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.company_pi)}</tbody>
      </table>
    </div>

    <!-- BLOCKERS -->
    <div class="sec-title">Active Blockers</div>
    <div class="table-wrap">
      ${blockers.length?`<table><thead><tr><th>ID</th><th>Severity</th><th>Description</th></tr></thead><tbody>
        ${blockers.map(b=>`<tr><td class="mono">${b.id}</td><td><span class="badge b-pending">${b.severity}</span></td><td>${b.description}</td></tr>`).join('')}
      </tbody></table>`:`<div class="blocker-empty">No active blockers</div>`}
    </div>

    <div class="footer">
      <div class="footer-left">
        <div class="source-bar">
          <span class="source-chip source-${d._source||'error'}">${d._source==='github'?'GitHub live':d._source==='local'?'Local file':'Error'}</span>
          <span class="source-age">${d._cache_age_s>0?'cached '+fmtAge(d._cache_age_s):'just fetched'}</span>
        </div>
        <span class="footer-note">Auto-refreshes every 60s &middot; Push <code>data/project_status.json</code> to GitHub to update</span>
      </div>
      <div class="footer-right">
        <a href="/api/project-status" class="footer-link">Raw JSON</a>
        <button class="refresh-gh-btn" id="gh-refresh-btn" onclick="refreshFromGitHub()">↻ Refresh from GitHub</button>
      </div>
    </div>
  `;
}

render();
setInterval(render, 60000);
</script>
</body>
</html>"""


@app.route("/project-status")
def project_status_dashboard():
    """Project build progress dashboard — requires token auth."""
    if request.args.get("token"):
        resp = redirect(url_for("project_status_dashboard"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos — Project Status</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> to access.</p>"
            "</body></html>"
        ), 401
    return render_template_string(_STATUS_HTML)


@app.route("/api/project-status")
def api_project_status():
    """
    Return project_status.json merged with cache metadata.
    Source priority: warm cache → GitHub API → local file.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data, source, age = _get_status_data()
    if data is None:
        return jsonify({"error": "status data unavailable — check GITHUB_TOKEN or local file"}), 503
    return jsonify({**data, "_source": source, "_cache_age_s": round(age)}), 200


@app.route("/api/project-status/refresh", methods=["POST"])
def api_project_status_refresh():
    """
    Force an immediate re-fetch from GitHub, bypassing the cache.
    Returns the source used and last_updated from the freshly fetched data.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    # Bust the cache so _get_status_data() goes straight to GitHub
    _status_cache["fetched_at"] = None
    data, source, _ = _get_status_data()
    if data is None:
        return jsonify({"ok": False, "error": "fetch failed — check GITHUB_TOKEN or local file"}), 503
    return jsonify({
        "ok":          True,
        "source":      source,
        "last_updated": data.get("meta", {}).get("last_updated"),
    }), 200


# ── Sentinel Display Bridge ──────────────────────────────────────────────────
_display_bridge = None
try:
    import sentinel_bridge as _display_bridge
    _display_bridge.start_watcher()  # Start drop folder monitor
except ImportError:
    pass  # sentinel_bridge.py not present — display features disabled

DISPLAY_DROP_DIR = os.path.join(os.path.dirname(_HERE), "data", "display_uploads")
os.makedirs(DISPLAY_DROP_DIR, exist_ok=True)

# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    """Thread-safe SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create company node database schema. Idempotent — safe to call on every startup."""
    with _db_conn() as conn:
        conn.executescript("""
            -- ── SCOOP QUEUE ──────────────────────────────────────────────────
            -- Incoming alert/event packets from retail Pi agents.
            -- Scoop drains this table and dispatches via Resend.
            CREATE TABLE IF NOT EXISTS scoop_queue (
                id                TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                priority          INTEGER NOT NULL DEFAULT 1,
                subject           TEXT NOT NULL,
                body              TEXT NOT NULL,
                source_agent      TEXT NOT NULL,
                pi_id             TEXT,
                audience          TEXT NOT NULL DEFAULT 'customer',
                correlation_id    TEXT,
                related_ticker    TEXT,
                related_signal_id TEXT,
                payload           TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                queued_at         TEXT NOT NULL,
                dispatched_at     TEXT,
                dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                error_msg         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scoop_status   ON scoop_queue(status);
            CREATE INDEX IF NOT EXISTS idx_scoop_priority ON scoop_queue(priority, queued_at);
            CREATE INDEX IF NOT EXISTS idx_scoop_pi       ON scoop_queue(pi_id, queued_at);

            -- ── PI EVENTS ────────────────────────────────────────────────────
            -- Durable log of all heartbeat and report data received from
            -- retail Pis (forwarded from Monitor Node or direct).
            CREATE TABLE IF NOT EXISTS pi_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id           TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                portfolio_value REAL,
                cash            REAL,
                realized_gains  REAL,
                open_positions  INTEGER,
                trades_today    INTEGER,
                operating_mode  TEXT,
                trading_mode    TEXT,
                kill_switch     INTEGER,
                payload         TEXT,
                recorded_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pi_events_pi   ON pi_events(pi_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_pi_events_type ON pi_events(event_type, recorded_at);
        """)
    print(f"[Company] DB initialized: {DB_PATH}")


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _cf_authorized():
    """Trust Cloudflare Access — checks Cf-Access-Authenticated-User-Email header."""
    if not CF_ADMIN_EMAIL:
        return False
    cf_email = request.headers.get("Cf-Access-Authenticated-User-Email", "").lower().strip()
    return cf_email == CF_ADMIN_EMAIL


def _token_authorized():
    """Check X-Token header, ?token= query param, or cookie. Timing-safe."""
    import hmac as _hmac_mod
    token = (
        request.headers.get("X-Token", "")
        or request.args.get("token", "")
        or request.cookies.get("company_token", "")
    )
    if not SECRET_TOKEN or not token:
        return False
    return _hmac_mod.compare_digest(token, SECRET_TOKEN)


def _session_authorized():
    """Check Flask session login."""
    return session.get("logged_in") is True

def _authorized():
    """Browser routes: accept session login, Cloudflare Access, or SECRET_TOKEN."""
    return _session_authorized() or _cf_authorized() or _token_authorized()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """Unauthenticated health check — returns DB queue counts."""
    try:
        with _db_conn() as conn:
            counts = {
                r["status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
                ).fetchall()
            }
        return jsonify({"ok": True, "queue": counts}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/enqueue", methods=["POST"])
def api_enqueue():
    """
    Receive a Scoop queue event from a retail Pi agent or monitor proxy.

    Auth: X-Token header must match SECRET_TOKEN.

    Required fields: event_type, priority, subject, body, source_agent
    Optional fields: payload, correlation_id, related_ticker,
                     related_signal_id, pi_id, audience
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    required = ["event_type", "priority", "subject", "body", "source_agent"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        priority = int(data["priority"])
    except (ValueError, TypeError):
        return jsonify({"error": "priority must be an integer 0-3"}), 400

    if priority not in (0, 1, 2, 3):
        return jsonify({"error": "priority must be 0, 1, 2, or 3"}), 400

    eid       = str(uuid.uuid4())
    queued_at = datetime.now(timezone.utc).isoformat()
    payload   = data.get("payload", {})

    try:
        with _db_conn() as conn:
            conn.execute(
                """INSERT INTO scoop_queue
                   (id, event_type, priority, subject, body, source_agent,
                    pi_id, audience, correlation_id, related_ticker,
                    related_signal_id, payload, status, queued_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eid,
                    str(data["event_type"]),
                    priority,
                    str(data["subject"]),
                    str(data["body"]),
                    str(data["source_agent"]),
                    data.get("pi_id"),
                    data.get("audience", "customer"),
                    data.get("correlation_id"),
                    data.get("related_ticker"),
                    data.get("related_signal_id"),
                    json.dumps(payload) if isinstance(payload, dict) else "{}",
                    "pending",
                    queued_at,
                ),
            )
        print(
            f"[ENQUEUE] {data['event_type']} P{priority} from {data['source_agent']} "
            f"pi={data.get('pi_id', '?')} id={eid[:8]}"
        )
        return jsonify({"ok": True, "id": eid, "priority": priority}), 200

    except Exception as e:
        print(f"[ENQUEUE] DB write failed: {e}")
        return jsonify({"ok": False, "error": f"DB write failed: {str(e)[:120]}"}), 500


@app.route("/api/queue", methods=["GET"])
def api_queue():
    """
    Inspect the scoop_queue.

    Query params:
      status  — filter by status (default: pending)
      pi_id   — filter by source Pi
      limit   — max rows (default: 50, max: 200)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    status = request.args.get("status", "pending")
    pi_id  = request.args.get("pi_id")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    try:
        with _db_conn() as conn:
            if pi_id:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? AND pi_id=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, pi_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scoop_queue WHERE status=? "
                    "ORDER BY priority ASC, queued_at ASC LIMIT ?",
                    (status, limit),
                ).fetchall()

            counts = {
                r["status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM scoop_queue GROUP BY status"
                ).fetchall()
            }

        return jsonify({
            "queue":  [dict(r) for r in rows],
            "counts": counts,
            "filter": {"status": status, "pi_id": pi_id, "limit": limit},
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/skip", methods=["POST"])
def api_queue_skip(item_id):
    """Mark a pending queue item as skipped (won't be dispatched by Scoop)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='skipped', dispatched_at=? "
                "WHERE id=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(), item_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in pending state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "skipped"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<item_id>/retry", methods=["POST"])
def api_queue_retry(item_id):
    """Reset a failed item back to pending so Scoop will retry it."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        with _db_conn() as conn:
            cur = conn.execute(
                "UPDATE scoop_queue SET status='pending', dispatch_attempts=0, "
                "error_msg=NULL, dispatched_at=NULL "
                "WHERE id=? AND status='failed'",
                (item_id,),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Item not found or not in failed state"}), 404
        return jsonify({"ok": True, "id": item_id, "status": "pending"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Node</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --green:#00f5d4;--red:#ff4b6e;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;height:56px;
  display:flex;align-items:center;gap:12px;
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
          color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
              padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);
              background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
           background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
           font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);
          box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* STAT CARDS */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.stat-card{
  padding:16px;border-radius:14px;
  border:1px solid var(--border);background:var(--surface);
  position:relative;overflow:hidden;
}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* TOOLBAR */
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:var(--muted);display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}
.tab-row{display:flex;gap:4px}
.tab-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
         cursor:pointer;border:1px solid var(--border);background:transparent;
         color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.tab-btn.active,.tab-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal);background:rgba(0,245,212,0.06)}
.ml-auto{margin-left:auto}
.refresh-btn{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;
             cursor:pointer;border:1px solid var(--border);background:transparent;
             color:var(--muted);font-family:var(--sans);transition:all 0.15s}
.refresh-btn:hover{border-color:var(--border2);color:var(--text)}

/* TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{
  padding:10px 14px;text-align:left;
  font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);border-bottom:1px solid var(--border);
  white-space:nowrap;
}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:10px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px}
.empty-row td{text-align:center;color:var(--muted);padding:32px;font-style:italic}

/* BADGES */
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
       padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-pending{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-sent{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.b-failed{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.25);color:var(--pink)}
.b-skipped{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}
.b-p0{background:rgba(255,75,110,0.15);border-color:rgba(255,75,110,0.35);color:var(--pink)}
.b-p1{background:rgba(255,179,71,0.12);border-color:rgba(255,179,71,0.3);color:var(--amber)}
.b-p2{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.b-p3{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* ACTION BUTTONS */
.act-btn{font-size:9px;font-weight:700;padding:2px 8px;border-radius:6px;
         background:transparent;border:1px solid var(--border);color:var(--muted);
         cursor:pointer;font-family:var(--sans);transition:all 0.15s}
.act-btn:hover{border-color:rgba(0,245,212,0.3);color:var(--teal)}
.act-btn.danger:hover{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* TOAST */
#toast{
  position:fixed;bottom:20px;right:20px;z-index:999;
  padding:10px 16px;border-radius:10px;font-size:12px;font-weight:600;
  background:var(--surface);border:1px solid var(--border2);color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,0.4);
  transform:translateY(60px);opacity:0;transition:all 0.3s;
  pointer-events:none;
}
#toast.show{transform:translateY(0);opacity:1}
#toast.ok{border-color:rgba(0,245,212,0.3);color:var(--teal)}
#toast.err{border-color:rgba(255,75,110,0.3);color:var(--pink)}
</style>
</head>
<body>

<div class="header">
  <span class="wordmark">SYNTHOS</span>
  <span class="header-badge">Company Node</span>
  <div class="header-right">
    <a href="/display" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="Display control">Display</a>
    <a href="/project-status" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="Project status">Status</a>
    <a href="/logs" style="font-size:0.72rem;letter-spacing:0.08em;color:#556;text-decoration:none;margin-right:1rem" title="View logs">Logs</a>
    <span class="clock" id="clock">--:--:-- ET</span>
    <div class="live-pill"><div class="live-dot"></div>LIVE</div>
  </div>
</div>

<div class="page">

  <!-- STAT CARDS -->
  <div class="stat-grid" id="stat-grid">
    <div class="stat-card sc-purple">
      <div class="stat-label">Pending</div>
      <div class="stat-val" id="cnt-pending">—</div>
      <div class="stat-sub">awaiting Scoop</div>
    </div>
    <div class="stat-card sc-teal">
      <div class="stat-label">Sent</div>
      <div class="stat-val" id="cnt-sent">—</div>
      <div class="stat-sub">dispatched ok</div>
    </div>
    <div class="stat-card sc-pink">
      <div class="stat-label">Failed</div>
      <div class="stat-val" id="cnt-failed">—</div>
      <div class="stat-sub">dispatch errors</div>
    </div>
    <div class="stat-card sc-muted">
      <div class="stat-label">Skipped</div>
      <div class="stat-val" id="cnt-skipped">—</div>
      <div class="stat-sub">manually resolved</div>
    </div>
    <div class="stat-card sc-amber">
      <div class="stat-label">Total</div>
      <div class="stat-val" id="cnt-total">—</div>
      <div class="stat-sub">all time</div>
    </div>
  </div>

  <!-- QUEUE TABLE -->
  <div style="margin-bottom:12px">
    <div class="sec-title" style="margin-bottom:12px">Scoop Queue</div>
    <div class="toolbar">
      <div class="tab-row" id="status-tabs">
        <button class="tab-btn active" onclick="setStatus('pending',this)">Pending</button>
        <button class="tab-btn" onclick="setStatus('failed',this)">Failed</button>
        <button class="tab-btn" onclick="setStatus('sent',this)">Sent</button>
        <button class="tab-btn" onclick="setStatus('skipped',this)">Skipped</button>
      </div>
      <button class="refresh-btn ml-auto" onclick="refresh()">↻ Refresh</button>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Priority</th>
          <th>Event Type</th>
          <th>Subject</th>
          <th>Source Agent</th>
          <th>Pi</th>
          <th>Audience</th>
          <th>Status</th>
          <th>Queued</th>
          <th>Attempts</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="queue-body">
        <tr class="empty-row"><td colspan="10">Loading…</td></tr>
      </tbody>
    </table>
  </div>

</div>

<div id="toast"></div>

<script>
const TOKEN = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('company_token='))?.split('=')[1] || '';
let currentStatus = 'pending';

function clock(){
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false}) + ' ET';
}
clock(); setInterval(clock,1000);

function toast(msg, type='ok'){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + type;
  setTimeout(()=>{ t.className = ''; }, 3000);
}

function priorityBadge(p){
  const labels = {0:'P0 CRIT',1:'P1 HIGH',2:'P2 MED',3:'P3 LOW'};
  const cls    = {0:'b-p0',1:'b-p1',2:'b-p2',3:'b-p3'};
  return `<span class="badge ${cls[p]||'b-p3'}">${labels[p]||'P'+p}</span>`;
}

function statusBadge(s){
  const cls = {pending:'b-pending',sent:'b-sent',failed:'b-failed',skipped:'b-skipped'};
  return `<span class="badge ${cls[s]||''}">${s.toUpperCase()}</span>`;
}

function fmtTime(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-US',{timeZone:'America/New_York',hour12:false,
    month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function actionBtns(item){
  const id = item.id;
  if(item.status === 'pending'){
    return `<button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  if(item.status === 'failed'){
    return `<button class="act-btn" onclick="retryItem('${id}')">Retry</button>
            <button class="act-btn danger" onclick="skipItem('${id}')">Skip</button>`;
  }
  return '—';
}

async function fetchQueue(status){
  const r = await fetch(`/api/queue?status=${status}&limit=100`,{
    headers:{'X-Token': TOKEN}
  });
  return r.json();
}

async function fetchHealth(){
  const r = await fetch('/health');
  return r.json();
}

async function refresh(){
  // Update counts
  try {
    const h = await fetchHealth();
    const counts = h.queue || {};
    ['pending','sent','failed','skipped'].forEach(s=>{
      const el = document.getElementById('cnt-'+s);
      if(el) el.textContent = counts[s] || 0;
    });
    const total = Object.values(counts).reduce((a,b)=>a+b,0);
    document.getElementById('cnt-total').textContent = total;
  } catch(e){}

  // Update queue table
  try {
    const data = await fetchQueue(currentStatus);
    const items = data.queue || [];
    const tbody = document.getElementById('queue-body');
    if(!items.length){
      tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No ${currentStatus} items</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(item=>`
      <tr>
        <td>${priorityBadge(item.priority)}</td>
        <td class="mono">${item.event_type||'—'}</td>
        <td style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
            title="${(item.subject||'').replace(/"/g,'&quot;')}">${item.subject||'—'}</td>
        <td>${item.source_agent||'—'}</td>
        <td class="mono" style="font-size:10px">${item.pi_id||'—'}</td>
        <td>${item.audience||'customer'}</td>
        <td>${statusBadge(item.status)}</td>
        <td class="mono" style="font-size:10px">${fmtTime(item.queued_at)}</td>
        <td style="text-align:center">${item.dispatch_attempts||0}</td>
        <td>${actionBtns(item)}</td>
      </tr>
    `).join('');
  } catch(e){
    document.getElementById('queue-body').innerHTML =
      `<tr class="empty-row"><td colspan="10">Failed to load queue</td></tr>`;
  }
}

async function skipItem(id){
  const r = await fetch(`/api/queue/${id}/skip`,{method:'POST',headers:{'X-Token':TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item skipped'); refresh(); }
  else toast(j.error||'Skip failed','err');
}

async function retryItem(id){
  const r = await fetch(`/api/queue/${id}/retry`,{method:'POST',headers:{'X-Token':TOKEN}});
  const j = await r.json();
  if(j.ok){ toast('Item queued for retry'); refresh(); }
  else toast(j.error||'Retry failed','err');
}

function setStatus(status, btn){
  currentStatus = status;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  refresh();
}

// Auto-refresh every 15 seconds
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


# ── Logs page ─────────────────────────────────────────────────────────────────
_COMPANY_LOGS_CSS = (
    '<style>'
    '*{box-sizing:border-box;margin:0;padding:0}'
    'body{background:#080b12;color:#e0ddd8;font-family:sans-serif;min-height:100vh}'
    'header{background:#0e1220;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;'
    '       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;'
    '       border-bottom:1px solid #1a2030}'
    '.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}'
    '.nav{display:flex;gap:1rem;align-items:center}'
    '.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}'
    '.nav a:hover{color:#aaa}'
    '.tabs{display:flex;gap:0;border-bottom:1px solid #1a2030;padding:0 2rem;'
    '      background:#0e1220;overflow-x:auto;flex-wrap:nowrap}'
    '.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;'
    '          background:#0e1220;border-bottom:1px solid #1a2030}'
    '.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}'
    'select{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1a2030;'
    '       border-radius:6px;color:#e0ddd8}'
    '.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;'
    '         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;'
    '         min-height:calc(100vh - 140px)}'
    '.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;'
    '             padding:0.3rem 0.75rem;border:1px solid #1a2030;'
    '             border-radius:6px;cursor:pointer;background:transparent;color:#556}'
    '.refresh-btn:hover{background:#1a2030;color:#e0ddd8}'
    '</style>'
)

_COMPANY_LOG_FILES = {
    'scoop':       'scoop.log',
    'server':      'company_server.log',
    'monitor':     'monitor.log',
    'node_health': 'node_health.log',
}


# ── Retail backup receiver ────────────────────────────────────────────────────
_BUILD_DIR    = os.path.dirname(_HERE)   # synthos_build/ (parent of src/)
_STAGING_ROOT = os.path.join(_BUILD_DIR, ".backup_staging")


@app.route("/receive_backup", methods=["POST"])
def receive_backup():
    """
    Accept a .tar.gz backup archive from a retail Pi and stage it for Strongbox.
    Auth: X-Token header or token cookie (same as /console).
    Form fields: pi_id (str), archive (file)
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    pi_id = (request.form.get("pi_id") or "").strip()
    if not pi_id or "/" in pi_id or ".." in pi_id:
        return jsonify({"error": "valid pi_id required"}), 400

    f = request.files.get("archive")
    if not f:
        return jsonify({"error": "archive file required"}), 400

    staging_dir = os.path.join(_STAGING_ROOT, pi_id)
    os.makedirs(staging_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = f"synthos_backup_{pi_id}_{date_str}.tar.gz"
    fpath = os.path.join(staging_dir, fname)
    f.save(fpath)

    size_kb = os.path.getsize(fpath) / 1024
    print(f"[Company] Staged backup: {fname} ({size_kb:.1f} KB) from pi_id={pi_id}")
    return jsonify({"ok": True, "staged": fname, "size_kb": round(size_kb, 1)}), 200


@app.route("/logs")
def company_logs():
    """Tail company-side log files — same token auth as console."""
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos Company Logs</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> "
            "or set <code>X-Token</code> header to access logs.</p>"
            "</body></html>"
        ), 401

    selected = request.args.get('file', 'scoop')
    try:
        lines = int(request.args.get('lines', 100))
    except (ValueError, TypeError):
        lines = 100
    fname    = _COMPANY_LOG_FILES.get(selected, 'scoop.log')
    fpath    = os.path.join(LOG_DIR, fname)

    content = ''
    if os.path.exists(fpath):
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            content = ''.join(all_lines[-lines:])
        except Exception as e:
            content = f'Error reading log: {e}'
    else:
        content = f'Log file not found: {fpath}'

    tabs = ''.join(
        f'<a href="/logs?file={k}&lines={lines}" '
        f'style="padding:6px 14px;font-family:monospace;font-size:0.72rem;'
        f'letter-spacing:0.08em;text-transform:uppercase;text-decoration:none;'
        f'border-bottom:2px solid {"#00f5d4" if k == selected else "transparent"};'
        f'color:{"#00f5d4" if k == selected else "#556"};display:inline-block">{k}</a>'
        for k in _COMPANY_LOG_FILES
    )

    line_opts = ''.join(
        f'<option value="{n}" {"selected" if n == lines else ""}>{n} lines</option>'
        for n in [50, 100, 200, 500]
    )

    log_escaped = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Logs</title>
{_COMPANY_LOGS_CSS}
</head>
<body>
<header>
  <div class="wordmark">SYNTHOS · COMPANY LOGS</div>
  <div class="nav">
    <a href="/console">&#8592; Console</a>
    <a href="/logs?file={selected}&lines={lines}" onclick="location.reload();return false">&#8635; Refresh</a>
  </div>
</header>
<div class="tabs">{tabs}</div>
<div class="controls">
  <label>Lines</label>
  <select onchange="window.location='/logs?file={selected}&lines='+this.value">{line_opts}</select>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  <span style="font-size:0.72rem;color:#556;margin-left:auto">{fname}</span>
</div>
<div class="log-box" id="log-content">{log_escaped}</div>
<script>
  document.getElementById('log-content').scrollIntoView({{block:'end'}});
</script>
</body>
</html>"""

    return html


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Command</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c14;--surface:#111520;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.13);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;--mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:2rem}
.card{width:100%;max-width:340px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:2rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:20%;right:20%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,245,212,0.3),transparent)}
.logo{font-family:var(--mono);font-size:0.85rem;font-weight:500;letter-spacing:0.18em;color:var(--teal);text-shadow:0 0 18px rgba(0,245,212,0.35);margin-bottom:0.25rem}
.sub{font-size:0.75rem;color:var(--muted);margin-bottom:1.75rem}
label{display:block;font-size:0.62rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem}
input{font-family:var(--mono);font-size:0.82rem;width:100%;padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:7px;color:var(--text);margin-bottom:0.9rem;transition:border-color .15s}
input:focus{outline:none;border-color:rgba(0,245,212,0.4);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
input::placeholder{color:var(--dim)}
.btn{font-family:var(--mono);font-size:0.75rem;font-weight:500;letter-spacing:0.07em;width:100%;padding:0.55rem;background:rgba(0,245,212,0.1);color:var(--teal);border:1px solid rgba(0,245,212,0.25);border-radius:7px;cursor:pointer;transition:all .15s}
.btn:hover{background:rgba(0,245,212,0.18);box-shadow:0 0 14px rgba(0,245,212,0.15)}
.err{font-size:0.72rem;color:#ff4b6e;margin-bottom:0.8rem;padding:0.4rem 0.6rem;background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.2);border-radius:6px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">SYNTHOS</div>
  <div class="sub">Command Node</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label>Email</label>
    <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
    <label>Password</label>
    <input type="password" name="password" placeholder="••••••••" autocomplete="current-password" required>
    <button class="btn" type="submit">Sign In →</button>
  </form>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if _authorized():
        return redirect(url_for("console"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        # Validate credentials
        email_ok = email == ADMIN_EMAIL
        pw_ok = (ADMIN_PW_HASH and check_password_hash(ADMIN_PW_HASH, password)) or \
                (not ADMIN_PW_HASH and password == SECRET_TOKEN)
        if email_ok and pw_ok:
            session.clear()
            session["logged_in"] = True
            session["email"] = email
            session.permanent = True
            return redirect(url_for("console"))
        return render_template_string(_LOGIN_HTML, error="Incorrect email or password")
    return render_template_string(_LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@app.route("/console")
def console():
    """Ops dashboard — requires login, Cloudflare Access, or SECRET_TOKEN."""
    # Legacy token-in-URL still works (sets cookie for API calls)
    if request.args.get("token"):
        resp = redirect(url_for("console"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return redirect(url_for("login"))
    return render_template_string(DASHBOARD_HTML)


# ── Project Status ────────────────────────────────────────────────────────────
_STATUS_JSON        = os.path.join(os.path.dirname(_HERE), "data", "project_status.json")
_GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "")
_GITHUB_OWNER       = os.getenv("GITHUB_REPO_OWNER", "personalprometheus-blip")
_GITHUB_STATUS_REPO = os.getenv("GITHUB_STATUS_REPO", "synthos-company")
_GITHUB_STATUS_PATH = os.getenv("GITHUB_STATUS_PATH", "data/project_status.json")
_STATUS_CACHE_TTL   = int(os.getenv("PROJECT_STATUS_TTL", "300"))   # seconds (default 5 min)

_status_cache: dict = {"data": None, "fetched_at": None, "source": "none"}


def _fetch_status_from_github():
    """
    Fetch project_status.json from the GitHub API.
    Returns the parsed dict on success, None on failure.
    Requires GITHUB_TOKEN in .env (read:contents scope is sufficient).
    Works for both public and private repos.
    """
    import urllib.request as _urllib_req
    import base64 as _b64
    import time as _time

    if not _GITHUB_TOKEN:
        return None

    url = (
        f"https://api.github.com/repos/{_GITHUB_OWNER}"
        f"/{_GITHUB_STATUS_REPO}/contents/{_GITHUB_STATUS_PATH}"
    )
    req = _urllib_req.Request(url, headers={
        "Authorization": f"token {_GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "synthos-company-server/1.0",
    })
    try:
        with _urllib_req.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        content = _b64.b64decode(payload["content"]).decode("utf-8")
        data = json.loads(content)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "github"
        print(f"[Company] project_status.json refreshed from GitHub")
        return data
    except Exception as exc:
        print(f"[Company] GitHub status fetch failed: {exc}")
        return None


def _get_status_data():
    """
    Return (data, source, cache_age_seconds).
    Priority: warm cache → GitHub API → local file → None.
    """
    import time as _time

    # Serve warm cache if within TTL
    if _status_cache["data"] and _status_cache["fetched_at"]:
        age = _time.time() - _status_cache["fetched_at"]
        if age < _STATUS_CACHE_TTL:
            return _status_cache["data"], _status_cache["source"], age

    # Try GitHub
    data = _fetch_status_from_github()
    if data:
        return data, "github", 0

    # Fall back to local file
    try:
        with open(_STATUS_JSON, "r") as fh:
            data = json.load(fh)
        _status_cache["data"]       = data
        _status_cache["fetched_at"] = _time.time()
        _status_cache["source"]     = "local"
        print("[Company] project_status.json loaded from local file (GitHub unavailable)")
        return data, "local", 0
    except Exception as exc:
        print(f"[Company] project_status.json load failed: {exc}")
        return None, "error", 0

_STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Project Status</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--teal2:rgba(0,245,212,0.1);
  --pink:#ff4b6e;--pink2:rgba(255,75,110,0.1);
  --purple:#7b61ff;--purple2:rgba(123,97,255,0.1);
  --amber:#ffb347;--amber2:rgba(255,179,71,0.1);
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}

.header{position:sticky;top:0;z-index:200;background:rgba(8,11,18,0.92);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.header-badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);background:rgba(123,97,255,0.1);color:#a78bfa}
.header-right{margin-left:auto;display:flex;align-items:center;gap:16px}
.nav-link{font-size:11px;letter-spacing:0.06em;color:var(--muted);text-decoration:none;transition:color 0.15s}
.nav-link:hover{color:var(--text)}
.clock{font-family:var(--mono);font-size:11px;color:var(--muted)}
.live-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
  background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);font-size:10px;font-weight:600;color:var(--teal)}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--teal);box-shadow:0 0 6px var(--teal);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

.page{max-width:1300px;margin:0 auto;padding:24px}

/* HERO PROGRESS */
.hero{padding:28px;border-radius:16px;border:1px solid var(--border);background:var(--surface);margin-bottom:24px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.hero-title{font-size:1.4rem;font-weight:700;letter-spacing:-0.3px}
.hero-meta{font-size:11px;color:var(--muted);margin-top:4px}
.phase-badge{font-family:var(--mono);font-size:11px;font-weight:600;padding:6px 14px;border-radius:8px;
  background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.milestone-chip{font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:4px 10px;border-radius:99px;background:rgba(255,179,71,0.1);border:1px solid rgba(255,179,71,0.25);color:var(--amber)}
.progress-label{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.progress-label span{font-size:11px;color:var(--muted)}
.progress-label strong{font-family:var(--mono);font-size:13px;color:var(--teal)}
.progress-track{height:6px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden}
.progress-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),rgba(0,245,212,0.6));transition:width 0.6s ease}

/* STAT GRID */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
.stat-card{padding:16px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0}
.sc-teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.sc-amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.sc-pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.sc-purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.sc-muted::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent)}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-val{font-size:28px;font-weight:700;letter-spacing:-0.5px}
.sc-teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.sc-amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.sc-pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.sc-purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.sc-muted .stat-val{color:var(--muted)}
.stat-sub{font-size:10px;color:var(--dim);margin-top:4px}

/* SECTION */
.sec-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);
  display:flex;align-items:center;gap:8px;margin-bottom:12px}
.sec-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* PHASE GRID */
.phase-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin-bottom:24px}
.phase-card{padding:18px;border-radius:14px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.phase-card.active{border-color:rgba(0,245,212,0.2);background:rgba(0,245,212,0.03)}
.phase-card.active::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.phase-card.not_started{opacity:0.5}
.phase-header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.phase-num{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 7px;border-radius:6px;
  background:rgba(255,255,255,0.05);color:var(--muted)}
.phase-name{font-size:13px;font-weight:600;flex:1}
.phase-status{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 8px;border-radius:99px;border:1px solid}
.ps-complete{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.ps-in_progress{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.ps-not_started{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.task-list{list-style:none;display:flex;flex-direction:column;gap:5px}
.task-item{display:flex;align-items:flex-start;gap:8px;font-size:11px;line-height:1.4}
.task-check{flex-shrink:0;width:14px;height:14px;margin-top:1px;border-radius:3px;border:1px solid;
  display:flex;align-items:center;justify-content:center;font-size:8px}
.task-check.done{background:rgba(0,245,212,0.15);border-color:rgba(0,245,212,0.4);color:var(--teal)}
.task-check.pending{background:rgba(255,255,255,0.03);border-color:var(--dim);color:transparent}
.task-text.done{color:var(--muted)}
.task-text.pending{color:var(--text)}
.phase-prog{margin-top:12px;padding-top:10px;border-top:1px solid var(--border)}
.phase-prog-track{height:3px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;margin-top:4px}
.phase-prog-fill{height:100%;border-radius:99px}
.ppf-complete{background:var(--teal)}
.ppf-in_progress{background:linear-gradient(90deg,var(--teal),var(--amber))}
.ppf-not_started{background:rgba(255,255,255,0.1)}
.phase-prog-label{font-size:10px;color:var(--dim)}

/* AGENT TABLE */
.table-wrap{border-radius:14px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{padding:10px 14px;text-align:left;font-size:9px;font-weight:700;letter-spacing:0.1em;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);transition:background 0.1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:rgba(255,255,255,0.02)}
td{padding:9px 14px;font-size:12px;color:var(--text);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:11px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
  padding:2px 7px;border-radius:99px;letter-spacing:0.05em;border:1px solid}
.b-built{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-planned{background:rgba(255,255,255,0.03);border-color:var(--border);color:var(--dim)}
.b-done{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.b-pending{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.b-hold{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--dim)}

/* SECURITY GRID */
.sec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-bottom:24px}
.sec-item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;
  border:1px solid var(--border);background:var(--surface)}
.sec-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sec-dot.done{background:var(--teal);box-shadow:0 0 6px rgba(0,245,212,0.5)}
.sec-dot.pending{background:var(--amber);box-shadow:0 0 6px rgba(255,179,71,0.4)}
.sec-dot.hold{background:var(--dim)}
.sec-info{flex:1;min-width:0}
.sec-label{font-size:11px;font-weight:500}
.sec-meta{font-size:10px;color:var(--muted);margin-top:1px}

/* BLOCKERS */
.blocker-empty{padding:24px;text-align:center;color:var(--muted);font-size:12px;font-style:italic}

/* FOOTER */
.footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.footer-left{display:flex;flex-direction:column;gap:3px}
.footer-note{font-size:10px;color:var(--dim)}
.source-bar{display:flex;align-items:center;gap:8px}
.source-chip{font-size:9px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:2px 7px;border-radius:99px;border:1px solid}
.source-github{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.source-local{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.source-error{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.3);color:var(--pink)}
.source-age{font-size:10px;color:var(--dim)}
.footer-right{display:flex;align-items:center;gap:10px}
.refresh-gh-btn{font-size:10px;font-weight:600;padding:5px 12px;border-radius:7px;cursor:pointer;
  border:1px solid rgba(0,245,212,0.25);background:rgba(0,245,212,0.05);color:var(--teal);
  font-family:var(--sans);transition:all 0.15s}
.refresh-gh-btn:hover{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.4)}
.refresh-gh-btn:disabled{opacity:0.4;cursor:not-allowed}
.footer-link{font-size:10px;color:var(--muted);text-decoration:none}
.footer-link:hover{color:var(--text)}
</style>
</head>
<body>

<div class="header">
  <span class="wordmark">SYNTHOS</span>
  <span class="header-badge">Project Status</span>
  <div class="header-right">
    <a href="/console" class="nav-link">Console</a>
    <a href="/logs" class="nav-link">Logs</a>
    <span class="clock" id="clock">--:--:-- ET</span>
    <div class="live-pill"><div class="live-dot"></div>LIVE</div>
  </div>
</div>

<div class="page" id="root">
  <p style="color:var(--muted);font-size:12px;padding:40px;text-align:center">Loading…</p>
</div>

<script>
const ET_ZONE = 'America/New_York';

function fmtClock(){
  const s=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:ET_ZONE,hour12:false});
  document.getElementById('clock').textContent=s+' ET';
}
setInterval(fmtClock,1000); fmtClock();

function statusCls(s){return{complete:'ps-complete',in_progress:'ps-in_progress',not_started:'ps-not_started'}[s]||'ps-not_started'}
function statusLabel(s){return{complete:'Complete',in_progress:'In Progress',not_started:'Not Started'}[s]||s}

function phaseCard(p){
  const done=p.tasks.filter(t=>t.done).length, total=p.tasks.length;
  const pct=total?Math.round(done/total*100):0;
  const active=p.status==='in_progress';
  const tasks=p.tasks.map(t=>`
    <li class="task-item">
      <span class="task-check ${t.done?'done':'pending'}">${t.done?'✓':''}</span>
      <span class="task-text ${t.done?'done':'pending'}">${t.label}</span>
    </li>`).join('');
  return `
    <div class="phase-card ${p.status}${active?' active':''}">
      <div class="phase-header">
        <span class="phase-num">P${p.id}</span>
        <span class="phase-name">${p.name}</span>
        <span class="phase-status ${statusCls(p.status)}">${statusLabel(p.status)}</span>
      </div>
      <ul class="task-list">${tasks}</ul>
      <div class="phase-prog">
        <div class="phase-prog-label">${done} / ${total} tasks · ${pct}%</div>
        <div class="phase-prog-track">
          <div class="phase-prog-fill ppf-${p.status}" style="width:${pct}%"></div>
        </div>
      </div>
    </div>`;
}

function agentRows(agents, label){
  return agents.map(a=>`
    <tr>
      <td><span class="badge ${a.status==='built'?'b-built':'b-planned'}">${a.status}</span></td>
      <td class="mono">${a.alias}</td>
      <td class="mono">${a.file}</td>
      <td style="color:var(--muted);font-size:11px">${a.job}</td>
    </tr>`).join('');
}

function secItems(items){
  return items.map(s=>`
    <div class="sec-item">
      <div class="sec-dot ${s.status}"></div>
      <div class="sec-info">
        <div class="sec-label">${s.item}</div>
        <div class="sec-meta">${s.repo} &middot; <span class="badge b-${s.status}" style="font-size:8px">${s.status}</span></div>
      </div>
    </div>`).join('');
}

function fmtAge(s){
  if(s<60) return `${Math.round(s)}s ago`;
  if(s<3600) return `${Math.round(s/60)}m ago`;
  return `${Math.round(s/3600)}h ago`;
}

async function refreshFromGitHub(){
  const btn=document.getElementById('gh-refresh-btn');
  if(btn){btn.disabled=true;btn.textContent='Refreshing…'}
  try{
    const r=await fetch('/api/project-status/refresh',{method:'POST',headers:{'X-Token':window._token||''}});
    const d=await r.json();
    if(d.ok) await render();
    else console.warn('Refresh failed:',d.error);
  }catch(e){console.error(e)}
  finally{if(btn){btn.disabled=false;btn.textContent='↻ Refresh from GitHub'}}
}

// Pull token from cookie for XHR auth
window._token=(document.cookie.split(';').find(c=>c.trim().startsWith('company_token='))||'').split('=')[1]||'';

async function render(){
  const d=await fetch('/api/project-status',{headers:{'X-Token':window._token}}).then(r=>r.json());
  const m=d.meta;
  const phases=d.phases;
  const phasesComplete=phases.filter(p=>p.status==='complete').length;
  const phasePct=Math.round(phasesComplete/m.total_phases*100);
  const currentPhase=phases.find(p=>p.status==='in_progress')||phases[m.current_phase-1];
  const allTasks=phases.flatMap(p=>p.tasks);
  const tasksDone=allTasks.filter(t=>t.done).length;
  const agentBuilt=[...d.agents.retail_pi,...d.agents.company_pi].filter(a=>a.status==='built').length;
  const agentTotal=[...d.agents.retail_pi,...d.agents.company_pi].length;
  const secDone=d.security.filter(s=>s.status==='done').length;
  const secTotal=d.security.length;
  const blockers=d.blockers||[];

  document.getElementById('root').innerHTML = `
    <!-- HERO -->
    <div class="hero">
      <div class="hero-top">
        <div>
          <div class="hero-title">Synthos Build Progress</div>
          <div class="hero-meta">Last updated ${m.last_updated} &middot; v${m.version}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          ${m.next_milestone?`<span class="milestone-chip">Next: ${m.next_milestone}</span>`:''}
          <span class="phase-badge">Phase ${m.current_phase} of ${m.total_phases}</span>
        </div>
      </div>
      <div class="progress-label">
        <span>Phase progress</span>
        <strong>${phasesComplete} / ${m.total_phases} phases complete</strong>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${phasePct}%"></div>
      </div>
    </div>

    <!-- STATS -->
    <div class="stat-grid">
      <div class="stat-card sc-teal">
        <div class="stat-label">Phases Done</div>
        <div class="stat-val">${phasesComplete}</div>
        <div class="stat-sub">of ${m.total_phases} total</div>
      </div>
      <div class="stat-card sc-purple">
        <div class="stat-label">Tasks Done</div>
        <div class="stat-val">${tasksDone}</div>
        <div class="stat-sub">of ${allTasks.length} total</div>
      </div>
      <div class="stat-card sc-amber">
        <div class="stat-label">Agents Built</div>
        <div class="stat-val">${agentBuilt}</div>
        <div class="stat-sub">of ${agentTotal} total</div>
      </div>
      <div class="stat-card ${secDone===secTotal?'sc-teal':'sc-pink'}">
        <div class="stat-label">Security</div>
        <div class="stat-val">${secDone}</div>
        <div class="stat-sub">of ${secTotal} items done</div>
      </div>
      <div class="stat-card ${blockers.length?'sc-pink':'sc-muted'}">
        <div class="stat-label">Blockers</div>
        <div class="stat-val">${blockers.length}</div>
        <div class="stat-sub">${blockers.length?'active':'all clear'}</div>
      </div>
    </div>

    <!-- PHASES -->
    <div class="sec-title">Build Phases</div>
    <div class="phase-grid">${phases.map(phaseCard).join('')}</div>

    <!-- SECURITY -->
    <div class="sec-title">Security Checklist</div>
    <div class="sec-grid">${secItems(d.security)}</div>

    <!-- AGENTS -->
    <div class="sec-title">Agent Registry — Retail Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.retail_pi)}</tbody>
      </table>
    </div>

    <div class="sec-title">Agent Registry — Company Pi</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Status</th><th>Alias</th><th>File</th><th>Job</th></tr></thead>
        <tbody>${agentRows(d.agents.company_pi)}</tbody>
      </table>
    </div>

    <!-- BLOCKERS -->
    <div class="sec-title">Active Blockers</div>
    <div class="table-wrap">
      ${blockers.length?`<table><thead><tr><th>ID</th><th>Severity</th><th>Description</th></tr></thead><tbody>
        ${blockers.map(b=>`<tr><td class="mono">${b.id}</td><td><span class="badge b-pending">${b.severity}</span></td><td>${b.description}</td></tr>`).join('')}
      </tbody></table>`:`<div class="blocker-empty">No active blockers</div>`}
    </div>

    <div class="footer">
      <div class="footer-left">
        <div class="source-bar">
          <span class="source-chip source-${d._source||'error'}">${d._source==='github'?'GitHub live':d._source==='local'?'Local file':'Error'}</span>
          <span class="source-age">${d._cache_age_s>0?'cached '+fmtAge(d._cache_age_s):'just fetched'}</span>
        </div>
        <span class="footer-note">Auto-refreshes every 60s &middot; Push <code>data/project_status.json</code> to GitHub to update</span>
      </div>
      <div class="footer-right">
        <a href="/api/project-status" class="footer-link">Raw JSON</a>
        <button class="refresh-gh-btn" id="gh-refresh-btn" onclick="refreshFromGitHub()">↻ Refresh from GitHub</button>
      </div>
    </div>
  `;
}

render();
setInterval(render, 60000);
</script>
</body>
</html>"""


@app.route("/project-status")
def project_status_dashboard():
    """Project build progress dashboard — requires token auth."""
    if request.args.get("token"):
        resp = redirect(url_for("project_status_dashboard"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos — Project Status</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> to access.</p>"
            "</body></html>"
        ), 401
    return render_template_string(_STATUS_HTML)


@app.route("/api/project-status")
def api_project_status():
    """
    Return project_status.json merged with cache metadata.
    Source priority: warm cache → GitHub API → local file.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data, source, age = _get_status_data()
    if data is None:
        return jsonify({"error": "status data unavailable — check GITHUB_TOKEN or local file"}), 503
    return jsonify({**data, "_source": source, "_cache_age_s": round(age)}), 200


@app.route("/api/project-status/refresh", methods=["POST"])
def api_project_status_refresh():
    """
    Force an immediate re-fetch from GitHub, bypassing the cache.
    Returns the source used and last_updated from the freshly fetched data.
    Requires token auth.
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    # Bust the cache so _get_status_data() goes straight to GitHub
    _status_cache["fetched_at"] = None
    data, source, _ = _get_status_data()
    if data is None:
        return jsonify({"ok": False, "error": "fetch failed — check GITHUB_TOKEN or local file"}), 503
    return jsonify({
        "ok":          True,
        "source":      source,
        "last_updated": data.get("meta", {}).get("last_updated"),
    }), 200


# ── Sentinel Display ──────────────────────────────────────────────────────────

DISPLAY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sentinel Display — Synthos Company</title>
<style>
:root{--bg:#0e0f11;--card:#16181d;--border:#23262e;--text:#c9cdd5;--dim:#556;
--accent:#4fc3f7;--accent2:#81c784;--warn:#ffb74d;--err:#ef5350;--radius:10px;
--font:'SF Mono',ui-monospace,'Cascadia Code','Fira Code',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:0.82rem;line-height:1.6;padding:1.5rem}
a{color:var(--accent);text-decoration:none}
h1{font-size:1.1rem;font-weight:600;margin-bottom:1rem;letter-spacing:0.03em}
h2{font-size:0.88rem;font-weight:600;margin:1.5rem 0 0.5rem;letter-spacing:0.05em;text-transform:uppercase;color:var(--dim)}

.topbar{display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}
.topbar a{font-size:0.72rem;letter-spacing:0.08em;color:var(--dim)}
.topbar a:hover{color:var(--accent)}
.topbar a.active{color:var(--accent);border-bottom:1px solid var(--accent)}

.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1rem;margin-bottom:1rem}
.status-row{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
.status-pill{display:inline-flex;align-items:center;gap:0.4rem;padding:0.3rem 0.7rem;border-radius:20px;font-size:0.75rem;background:var(--bg);border:1px solid var(--border)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.green{background:#4caf50}.dot.red{background:#ef5350}.dot.yellow{background:#ffb74d}.dot.blue{background:var(--accent)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}
.scene-grid{display:flex;gap:0.5rem;flex-wrap:wrap}
.scene-btn{padding:0.4rem 0.9rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:var(--font);font-size:0.78rem;transition:all 0.2s}
.scene-btn:hover{border-color:var(--accent);color:var(--accent)}
.scene-btn.active{background:var(--accent);color:var(--bg);border-color:var(--accent);font-weight:600}

.theme-dots{display:flex;gap:0.6rem;align-items:center;margin-top:0.5rem}
.theme-dot{width:28px;height:28px;border-radius:50%;cursor:pointer;border:2px solid var(--border);transition:all 0.2s}
.theme-dot:hover{transform:scale(1.15)}.theme-dot.active{border-color:var(--accent);box-shadow:0 0 8px var(--accent)}

.slider-row{display:flex;align-items:center;gap:0.8rem;margin:0.5rem 0}
.slider-row label{min-width:60px;font-size:0.75rem;color:var(--dim)}
.slider-row input[type=range]{flex:1;accent-color:var(--accent)}
.slider-row .val{min-width:30px;text-align:right;font-size:0.78rem}

.chip{display:inline-block;padding:0.25rem 0.6rem;border-radius:4px;border:1px solid var(--border);background:var(--bg);font-size:0.75rem;cursor:pointer;margin:0.2rem}
.chip:hover{border-color:var(--accent)}.chip.active{background:var(--accent);color:var(--bg);border-color:var(--accent)}

.dropzone{border:2px dashed var(--border);border-radius:var(--radius);padding:2rem;text-align:center;color:var(--dim);transition:all 0.3s;cursor:pointer;margin:0.5rem 0}
.dropzone.dragover{border-color:var(--accent);background:rgba(79,195,247,0.05);color:var(--accent)}
.dropzone input{display:none}

.asset-table{width:100%;border-collapse:collapse;margin-top:0.5rem}
.asset-table th,.asset-table td{text-align:left;padding:0.4rem 0.6rem;border-bottom:1px solid var(--border);font-size:0.78rem}
.asset-table th{color:var(--dim);font-weight:500}

.btn{padding:0.35rem 0.8rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer;font-family:var(--font);font-size:0.75rem;transition:all 0.2s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.danger{color:var(--err)}.btn.danger:hover{border-color:var(--err)}

.log-box{background:#0a0b0d;border:1px solid var(--border);border-radius:6px;padding:0.8rem;max-height:200px;overflow-y:auto;font-size:0.72rem;line-height:1.5;white-space:pre-wrap;color:#8a8}
.toast{position:fixed;bottom:1.5rem;right:1.5rem;padding:0.6rem 1.2rem;border-radius:8px;background:#1e1e1e;border:1px solid var(--border);color:var(--text);font-size:0.78rem;opacity:0;transition:opacity 0.3s;z-index:999;pointer-events:none}
.toast.show{opacity:1}
select{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:0.3rem 0.5rem;font-family:var(--font);font-size:0.78rem}
</style>
</head>
<body>
<div class="topbar">
  <strong style="font-size:0.9rem;letter-spacing:0.06em">SYNTHOS</strong>
  <a href="/console">Console</a>
  <a href="/project-status">Project Status</a>
  <a href="/display" class="active">Display</a>
</div>

<h1>Sentinel Display Control</h1>

<div class="status-row" id="statusRow">
  <span class="status-pill"><span class="dot" id="connDot"></span><span id="connLabel">Checking…</span></span>
  <span class="status-pill"><span class="dot" id="svcDot"></span><span id="svcLabel">Service: —</span></span>
  <span class="status-pill"><span class="dot blue"></span><span id="sceneLabel">Scene: —</span></span>
  <span class="status-pill"><span class="dot blue"></span><span id="themeLabel">Theme: —</span></span>
</div>

<div class="grid">
  <!-- Scene Selector -->
  <div class="card">
    <h2>Active Scene</h2>
    <div class="scene-grid" id="sceneGrid"></div>
  </div>

  <!-- Theme Selector -->
  <div class="card">
    <h2>Theme</h2>
    <div class="theme-dots" id="themeDots"></div>
  </div>

  <!-- Brightness -->
  <div class="card">
    <h2>Brightness</h2>
    <div class="slider-row">
      <label>Day</label>
      <input type="range" min="0" max="100" id="dayBri" oninput="deBri('day',this.value)">
      <span class="val" id="dayBriVal">—</span>
    </div>
    <div class="slider-row">
      <label>Night</label>
      <input type="range" min="0" max="100" id="nightBri" oninput="deBri('night',this.value)">
      <span class="val" id="nightBriVal">—</span>
    </div>
  </div>

  <!-- Day/Night Mode -->
  <div class="card">
    <h2>Day / Night Mode</h2>
    <div id="dnChips" style="margin-top:0.4rem"></div>
  </div>

  <!-- Animations -->
  <div class="card">
    <h2>Boot Animation</h2>
    <div id="bootChips" style="margin-top:0.3rem"></div>
    <h2>Idle Animation</h2>
    <div id="idleChips" style="margin-top:0.3rem"></div>
  </div>
</div>

<!-- Upload -->
<div class="card" style="margin-top:0.5rem">
  <h2>Upload Display Assets</h2>
  <div style="display:flex;align-items:center;gap:0.8rem;margin-bottom:0.5rem">
    <label style="font-size:0.75rem;color:var(--dim)">Category:</label>
    <select id="uploadCat">
      <option value="auto">Auto-detect</option>
      <option value="boot">Boot</option>
      <option value="idle">Idle</option>
      <option value="informational">Informational</option>
      <option value="theme">Theme</option>
    </select>
  </div>
  <div class="dropzone" id="dropzone">
    <div>Drop files here or <strong>click to browse</strong></div>
    <div style="font-size:0.7rem;color:var(--dim);margin-top:0.3rem">PNG, JPG, BMP, GIF, JSON, PY — max 8 MB</div>
    <input type="file" id="fileInput" multiple accept=".png,.jpg,.jpeg,.bmp,.gif,.json,.py">
  </div>
</div>

<!-- Assets Table -->
<div class="card">
  <h2>Installed Assets</h2>
  <table class="asset-table">
    <thead><tr><th>File</th><th>Category</th><th>Size</th><th></th></tr></thead>
    <tbody id="assetBody"><tr><td colspan="4" style="color:var(--dim)">Loading…</td></tr></tbody>
  </table>
</div>

<!-- Service & Logs -->
<div class="card">
  <h2>Service</h2>
  <div style="display:flex;gap:0.5rem;margin-bottom:0.8rem">
    <button class="btn" onclick="svcAction('restart')">Restart Display</button>
    <button class="btn" onclick="loadLogs()">Refresh Logs</button>
  </div>
  <div class="log-box" id="logBox">No logs loaded.</div>
</div>

<div class="toast" id="toast"></div>

<script>
const T='"""+"""{{TOKEN}}"""+"""';
const H={headers:{'X-Token':T,'Content-Type':'application/json'}};
let _briTimer=null;

function toast(msg,err){const t=document.getElementById('toast');t.textContent=msg;t.style.borderColor=err?'var(--err)':'var(--accent)';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}

async function api(path,opts){try{const r=await fetch(path,{...opts,headers:{...opts?.headers,'X-Token':T}});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}catch(e){toast(e.message,true);throw e}}

async function loadStatus(){
  try{
    const s=await api('/api/display/status');
    document.getElementById('connDot').className='dot '+(s.display_detected?'green':'red');
    document.getElementById('connLabel').textContent=s.display_detected?'Connected':'No Display';
    document.getElementById('svcDot').className='dot '+(s.service_active?'green':'yellow');
    document.getElementById('svcLabel').textContent='Service: '+(s.service_active?'Running':'Stopped');
    document.getElementById('sceneLabel').textContent='Scene: '+(s.current_scene||'—');
    document.getElementById('themeLabel').textContent='Theme: '+(s.config?.theme?.active||'—');

    // Scenes
    const scenes=['idle','boot','trade','news','weather','alert','settings'];
    const sg=document.getElementById('sceneGrid');
    sg.innerHTML=scenes.map(sc=>`<button class="scene-btn ${sc===s.current_scene?'active':''}" onclick="setScene('${sc}')">${sc}</button>`).join('');

    // Themes
    const colors={retro:'#00ff41',amber:'#ffb300',arctic:'#4fc3f7',ghost:'#e0e0e0'};
    const td=document.getElementById('themeDots');
    const active=s.config?.theme?.active||'retro';
    td.innerHTML=Object.entries(colors).map(([n,c])=>`<div class="theme-dot ${n===active?'active':''}" style="background:${c}" title="${n}" onclick="setTheme('${n}')"></div>`).join('');

    // Brightness
    const dayB=s.config?.day_brightness??90, nightB=s.config?.night_brightness??40;
    document.getElementById('dayBri').value=dayB;document.getElementById('dayBriVal').textContent=dayB;
    document.getElementById('nightBri').value=nightB;document.getElementById('nightBriVal').textContent=nightB;

    // Day/Night
    const dnMode=s.config?.daynight_mode||'auto';
    const dnEl=document.getElementById('dnChips');
    dnEl.innerHTML=['auto','force_day','force_night'].map(m=>`<span class="chip ${m===dnMode?'active':''}" onclick="setDN('${m}')">${m.replace('_',' ')}</span>`).join('');

    // Animations
    const bootAnims=s.config?.animations?.available_boot||['retro_terminal'];
    const idleAnims=s.config?.animations?.available_idle||['retro_matrix'];
    const curBoot=s.config?.animations?.boot||'';
    const curIdle=s.config?.animations?.idle||'';
    document.getElementById('bootChips').innerHTML=bootAnims.map(a=>`<span class="chip ${a===curBoot?'active':''}" onclick="setAnim('boot','${a}')">${a}</span>`).join('');
    document.getElementById('idleChips').innerHTML=idleAnims.map(a=>`<span class="chip ${a===curIdle?'active':''}" onclick="setAnim('idle','${a}')">${a}</span>`).join('');

    // Assets
    loadAssets();
  }catch(e){
    document.getElementById('connDot').className='dot red';
    document.getElementById('connLabel').textContent='Error';
  }
}

async function loadAssets(){
  try{
    const a=await api('/api/display/assets');
    const tb=document.getElementById('assetBody');
    if(!a.assets||!a.assets.length){tb.innerHTML='<tr><td colspan="4" style="color:var(--dim)">No assets installed</td></tr>';return}
    tb.innerHTML=a.assets.map(f=>`<tr><td>${f.name}</td><td>${f.category}</td><td>${f.size_kb} KB</td><td><button class="btn danger" onclick="removeAsset('${f.name}')">Remove</button></td></tr>`).join('');
  }catch(e){}
}

async function setScene(sc){await api('/api/display/scene',{method:'POST',body:JSON.stringify({scene:sc}),...H});toast('Scene → '+sc);loadStatus()}
async function setTheme(th){await api('/api/display/theme',{method:'POST',body:JSON.stringify({theme:th}),...H});toast('Theme → '+th);loadStatus()}
async function setDN(mode){await api('/api/display/daynight',{method:'POST',body:JSON.stringify({mode}),...H});toast('Day/Night → '+mode);loadStatus()}
async function setAnim(kind,name){await api('/api/display/animation',{method:'POST',body:JSON.stringify({kind,name}),...H});toast(kind+' → '+name);loadStatus()}

function deBri(which,val){
  document.getElementById(which+'BriVal').textContent=val;
  clearTimeout(_briTimer);
  _briTimer=setTimeout(()=>{
    api('/api/display/brightness',{method:'POST',body:JSON.stringify({[which+'_brightness']:parseInt(val)}),...H});
    toast('Brightness updated');
  },400);
}

async function removeAsset(name){if(!confirm('Remove '+name+'?'))return;await api('/api/display/assets/'+encodeURIComponent(name),{method:'DELETE'});toast('Removed '+name);loadStatus()}
async function svcAction(act){await api('/api/display/restart',{method:'POST',...H});toast('Service restarting…');setTimeout(loadStatus,3000)}

async function loadLogs(){
  try{const d=await api('/api/display/logs');document.getElementById('logBox').textContent=d.logs||'No logs available.'}catch(e){document.getElementById('logBox').textContent='Failed to load logs.'}
}

// Drag-and-drop upload
const dz=document.getElementById('dropzone'), fi=document.getElementById('fileInput');
dz.addEventListener('click',()=>fi.click());
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover')});
dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('dragover');uploadFiles(e.dataTransfer.files)});
fi.addEventListener('change',()=>{uploadFiles(fi.files);fi.value=''});

async function uploadFiles(files){
  const cat=document.getElementById('uploadCat').value;
  for(const f of files){
    const fd=new FormData();fd.append('file',f);fd.append('category',cat);
    try{
      const r=await fetch('/api/display/upload',{method:'POST',headers:{'X-Token':T},body:fd});
      const j=await r.json();if(!r.ok)throw new Error(j.error||'Upload failed');
      toast('Uploaded: '+f.name);
    }catch(e){toast('Upload failed: '+e.message,true)}
  }
  loadStatus();
}

// Auto-refresh
loadStatus();
setInterval(loadStatus,10000);
</script>
</body>
</html>
"""

# Inject the token into DISPLAY_HTML at render time
def _render_display_html():
    token = request.args.get("token", "") or request.cookies.get("company_token", "")
    return DISPLAY_HTML.replace("{{TOKEN}}", token)


@app.route("/display")
def display_page():
    """Render the Sentinel display control panel."""
    return _render_display_html()


@app.route("/api/display/status")
def api_display_status():
    """Full display status JSON."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        status = _display_bridge.get_display_status()
        return jsonify(status), 200
    except Exception as e:
        return jsonify({"error": str(e), "display_detected": False}), 200


@app.route("/api/display/scene", methods=["POST"])
def api_display_scene():
    """Change the active scene."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    scene = data.get("scene")
    if not scene:
        return jsonify({"error": "missing 'scene'"}), 400
    try:
        _display_bridge.set_scene(scene)
        return jsonify({"ok": True, "scene": scene}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/brightness", methods=["POST"])
def api_display_brightness():
    """Adjust day/night brightness."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        cfg = _display_bridge.read_config()
        changed = False
        if "day_brightness" in data:
            cfg["day_brightness"] = int(data["day_brightness"])
            changed = True
        if "night_brightness" in data:
            cfg["night_brightness"] = int(data["night_brightness"])
            changed = True
        if changed:
            _display_bridge.write_config(cfg)
            _display_bridge.set_brightness(cfg.get("day_brightness", 90))
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/theme", methods=["POST"])
def api_display_theme():
    """Change the display theme."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    theme = data.get("theme")
    if not theme:
        return jsonify({"error": "missing 'theme'"}), 400
    try:
        _display_bridge.set_theme(theme)
        return jsonify({"ok": True, "theme": theme}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/daynight", methods=["POST"])
def api_display_daynight():
    """Set day/night mode: auto, force_day, force_night."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("auto", "force_day", "force_night"):
        return jsonify({"error": "invalid mode — use auto, force_day, or force_night"}), 400
    try:
        _display_bridge.set_daynight_mode(mode)
        return jsonify({"ok": True, "mode": mode}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/animation", methods=["POST"])
def api_display_animation():
    """Set boot or idle animation."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    name = data.get("name")
    if kind not in ("boot", "idle") or not name:
        return jsonify({"error": "need 'kind' (boot|idle) and 'name'"}), 400
    try:
        if kind == "boot":
            _display_bridge.set_boot_animation(name)
        else:
            _display_bridge.set_idle_animation(name)
        return jsonify({"ok": True, "kind": kind, "name": name}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/assets")
def api_display_assets():
    """List installed display assets."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        assets = _display_bridge.list_assets()
        return jsonify({"assets": assets}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


DISPLAY_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".json", ".py"}

@app.route("/api/display/upload", methods=["POST"])
def api_display_upload():
    """Upload a display asset (drag-and-drop or form)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    safe = secure_filename(f.filename)
    ext = os.path.splitext(safe)[1].lower()
    if ext not in DISPLAY_ALLOWED_EXT:
        return jsonify({"error": f"file type {ext} not allowed"}), 400
    category = request.form.get("category", "auto")
    # Save to drop dir — watcher will pick it up, or install directly
    dest = os.path.join(DISPLAY_DROP_DIR, safe)
    f.save(dest)
    try:
        result = _display_bridge.install_asset(dest, category=category if category != "auto" else None)
        return jsonify({"ok": True, "installed": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/assets/<filename>", methods=["DELETE"])
def api_display_remove_asset(filename):
    """Remove an installed display asset."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    safe = secure_filename(filename)
    try:
        _display_bridge.remove_asset(safe)
        return jsonify({"ok": True, "removed": safe}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/restart", methods=["POST"])
def api_display_restart():
    """Restart the Sentinel display service."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        _display_bridge.restart_display_service()
        return jsonify({"ok": True, "message": "restart requested"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display/logs")
def api_display_logs():
    """Get recent display log output."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    lines = int(request.args.get("lines", 50))
    try:
        logs = _display_bridge.get_display_logs(lines=lines)
        return jsonify({"logs": logs}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Auditor Findings ──────────────────────────────────────────────────────────
_AUDITOR_DB_PATH = os.getenv('AUDITOR_DB_PATH', os.path.join(_HERE, 'data', 'auditor.db'))

@app.route("/api/auditor/findings")
def api_auditor_findings():
    """
    Return live auditor findings from auditor.db.
    Called by the monitor dashboard — no auth required (internal network only).
    """
    try:
        conn = sqlite3.connect(_AUDITOR_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row

        issues = conn.execute(
            "SELECT id, first_seen, last_seen, source_file, severity, pattern, "
            "       context, hit_count "
            "FROM detected_issues WHERE resolved = 0 "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "         WHEN 'medium' THEN 2 ELSE 3 END, hit_count DESC LIMIT 200"
        ).fetchall()

        by_sev: dict = {}
        for row in issues:
            by_sev[row['severity']] = by_sev.get(row['severity'], 0) + 1

        scan_state = conn.execute(
            "SELECT log_file, last_offset, file_size, last_scanned "
            "FROM scan_state ORDER BY last_scanned DESC"
        ).fetchall()

        report_row = conn.execute(
            "SELECT report FROM morning_reports ORDER BY date DESC LIMIT 1"
        ).fetchone()

        conn.close()

        return jsonify({
            'issues':          [dict(r) for r in issues],
            'by_severity':     by_sev,
            'total_unresolved': len(issues),
            'scan_state':      [dict(r) for r in scan_state],
            'morning_report':  json.loads(report_row['report']) if report_row else None,
        })
    except FileNotFoundError:
        return jsonify({
            'issues': [], 'by_severity': {}, 'total_unresolved': 0,
            'scan_state': [], 'morning_report': None,
            'error': 'Auditor DB not found — auditor may not have run yet',
        })
    except Exception as e:
        return jsonify({
            'issues': [], 'by_severity': {}, 'total_unresolved': 0,
            'scan_state': [], 'morning_report': None,
            'error': str(e),
        })


# ── Retention ─────────────────────────────────────────────────────────────────
_PI_EVENTS_RETAIN_DAYS = int(os.getenv("PI_EVENTS_RETAIN_DAYS", "30"))

def trim_pi_events():
    """
    Delete pi_events rows older than PI_EVENTS_RETAIN_DAYS (default 30 days).
    Run on startup to prevent unbounded table growth.
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=_PI_EVENTS_RETAIN_DAYS)).isoformat()
    try:
        with _db_conn() as conn:
            result  = conn.execute(
                "DELETE FROM pi_events WHERE recorded_at < ?", (cutoff_iso,)
            )
            deleted = result.rowcount
        if deleted:
            print(f"[Company] Trimmed {deleted} pi_events rows older than {_PI_EVENTS_RETAIN_DAYS} days")
    except Exception as e:
        print(f"[Company] Warning: pi_events trim failed: {e}")


@app.route("/console")
def console():
    import datetime as _dt
    from flask import make_response
    resp = make_response(render_template_string(DASHBOARD, secret_token=SECRET_TOKEN, build_ts=_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# keep old / route as JSON redirect
@app.route("/health")
def health():
    """
    Unauthenticated health check for the monitor node itself.
    Returns a compact status snapshot used by retail_patch.py --check-nodes
    and any external uptime monitor.
    """
    with registry_lock:
        pi_count = len(pi_registry)
        pis      = []
        for pi_id, data in pi_registry.items():
            age_s = int((now_utc() - data["last_seen"]).total_seconds())
            pis.append({
                "pi_id":   pi_id,
                "label":   data.get("label", pi_id),
                "status":  pi_status(data),
                "age_secs": age_s,
            })
    return jsonify({
        "status":   "ok",
        "pi_count": pi_count,
        "pis":      pis,
    }), 200


@app.route("/")
def index():
    return jsonify({"status": "Synthos Monitor online", "console": "/console", "api": "/api/status"})


_TODO_PATH = os.path.join(os.path.dirname(_HERE), 'TODO.md')


@app.route("/api/todos", methods=["GET"])
def api_todos():
    """Parse TODO.md from the repo and return unresolved items as JSON."""
    import re as _re
    try:
        with open(_TODO_PATH, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return jsonify([])

    todos = []
    section = 'Pending'
    item_re = _re.compile(r'^\s*-\s+\[([ xX])\]\s+(?:\[([^\]]+)\]\s+)?(.+)')

    for line in lines:
        heading = _re.match(r'^#+\s+(.+)', line.strip())
        if heading:
            section = heading.group(1).strip()
            continue
        m = item_re.match(line)
        if not m:
            continue
        checked  = m.group(1).lower() == 'x'
        category = (m.group(2) or section).strip()
        rest     = m.group(3).strip()
        if ' — ' in rest:
            title, action = rest.split(' — ', 1)
        else:
            title, action = rest, ''
        todos.append({
            'id':       str(len(todos)),
            'title':    title.strip(),
            'category': category,
            'action':   action.strip(),
            'section':  section,
            'resolved': checked,
            'date':     '',
            'pi_id':    '',
        })

    section_order = {'Pending': 0, 'In Progress': 1}
    unresolved = [t for t in todos if not t['resolved']]
    unresolved.sort(key=lambda t: section_order.get(t['section'], 99))
    return jsonify(unresolved)


@app.route("/api/auditor")
def api_auditor():
    """Proxy auditor findings from the Company Pi's company_server."""
    if not COMPANY_URL:
        return jsonify({'error': 'COMPANY_URL not configured', 'issues': [],
                        'by_severity': {}, 'total_unresolved': 0,
                        'scan_state': [], 'morning_report': None})
    try:
        import requests as _req
        r = _req.get(f"{COMPANY_URL}/api/auditor/findings", timeout=8)
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({'error': f'Company server returned {r.status_code}',
                        'issues': [], 'by_severity': {}, 'total_unresolved': 0,
                        'scan_state': [], 'morning_report': None})
    except Exception as e:
        return jsonify({'error': str(e), 'issues': [], 'by_severity': {},
                        'total_unresolved': 0, 'scan_state': [], 'morning_report': None})


@app.route("/api/audit/<pi_id>")
def api_audit_for_pi(pi_id):
    """
    Fetch audit data from a Pi's portal directly.
    The Pi's portal exposes /api/audit which reads .audit_latest.json
    """
    with registry_lock:
        pi = pi_registry.get(pi_id)
    if not pi:
        return jsonify({"error": "Pi not found"}), 404

    # Try to fetch from Pi portal
    pi_ip = None
    try:
        # Extract IP from last_seen or use pi_id heuristic
        import requests as _req
        portal_url = f"http://{pi_id.replace('synthos-','').replace('-','.')}:5001/api/audit"
        r = _req.get(portal_url, timeout=5)
        if r.status_code == 200:
            return jsonify(r.json())
    except Exception:
        pass

    return jsonify({"error": "Could not reach Pi portal", "pi_id": pi_id}), 503


@app.route("/api/backlog/<pi_id>")
def api_backlog_for_pi(pi_id):
    """Fetch improvement backlog from a Pi's portal."""
    with registry_lock:
        pi = pi_registry.get(pi_id)
    if not pi:
        return jsonify({"error": "Pi not found"}), 404
    try:
        import requests as _req
        portal_url = f"http://{pi_id.replace('synthos-','').replace('-','.')}:5001/api/improvement-backlog"
        r = _req.get(portal_url, timeout=5)
        if r.status_code == 200:
            return jsonify(r.json())
    except Exception:
        pass
    return jsonify({"tasks": [], "error": "Could not reach Pi portal"}), 200


AUDIT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Auditor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--pink:#ff4b6e;--purple:#7b61ff;--amber:#ffb347;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.header{position:sticky;top:0;z-index:100;background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
        border-bottom:1px solid var(--border);padding:0 24px;height:56px;
        display:flex;align-items:center;gap:12px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal)}
.nav-back{color:var(--muted);font-size:11px;text-decoration:none;padding:5px 12px;
          border-radius:8px;border:1px solid var(--border);margin-left:auto;transition:all 0.15s}
.nav-back:hover{color:var(--text);border-color:var(--border2)}
.page{max-width:1200px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,var(--purple),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:12px;color:var(--muted);margin-bottom:24px}

/* Stats row */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat-mini{padding:14px 16px;border-radius:12px;border:1px solid var(--border);background:var(--surface)}
.sm-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.sm-val{font-size:26px;font-weight:800;letter-spacing:-1px}
.sm-sub{font-size:10px;color:var(--muted);margin-top:3px}

/* Two column layout */
.two-col{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* Panels */
.panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:16px}
.panel:last-child{margin-bottom:0}
.panel-header{padding:14px 16px;border-bottom:1px solid var(--border);
              display:flex;align-items:center;gap:8px}
.panel-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);flex:1}
.panel-badge{padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;border:1px solid}
.pb-purple{background:rgba(123,97,255,0.08);border-color:rgba(123,97,255,0.25);color:var(--purple)}
.pb-teal{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.pb-amber{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.2);color:var(--amber)}
.pb-pink{background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.2);color:var(--pink)}
.panel-scroll{max-height:480px;overflow-y:auto}

/* Issue rows */
.issue-row{padding:11px 16px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start}
.issue-row:last-child{border-bottom:none}
.sev-badge{flex-shrink:0;padding:2px 7px;border-radius:5px;font-size:9px;font-weight:800;
           letter-spacing:0.06em;text-transform:uppercase;margin-top:1px}
.sev-critical{background:rgba(255,75,110,0.12);color:var(--pink);border:1px solid rgba(255,75,110,0.25)}
.sev-high{background:rgba(255,179,71,0.12);color:var(--amber);border:1px solid rgba(255,179,71,0.25)}
.sev-medium{background:rgba(123,97,255,0.12);color:var(--purple);border:1px solid rgba(123,97,255,0.25)}
.sev-low{background:rgba(255,255,255,0.05);color:var(--muted);border:1px solid var(--border)}
.issue-body{flex:1;min-width:0}
.issue-file{font-size:10px;font-family:var(--mono);color:var(--purple);margin-bottom:3px}
.issue-ctx{font-size:11px;color:var(--text);line-height:1.5;word-break:break-all}
.issue-meta{font-size:9px;color:var(--dim);font-family:var(--mono);margin-top:4px}

/* Scan state rows */
.scan-row{padding:8px 16px;border-bottom:1px solid var(--border);font-size:10px;
          display:flex;gap:8px;align-items:center;font-family:var(--mono)}
.scan-row:last-child{border-bottom:none}
.scan-file{color:var(--text);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scan-age{color:var(--muted);flex-shrink:0}

/* Morning report */
.report-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:14px 16px}
.rg-cell{text-align:center}
.rg-val{font-size:22px;font-weight:800;letter-spacing:-1px}
.rg-lab{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--muted);margin-top:2px}

/* Error / empty */
.empty{padding:32px;text-align:center;color:var(--muted);font-size:12px}
.empty-icon{font-size:28px;margin-bottom:10px}
.error-bar{padding:12px 16px;font-size:11px;color:var(--pink);background:rgba(255,75,110,0.06);
           border-bottom:1px solid rgba(255,75,110,0.15)}
</style>
</head>
<body>

<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div style="font-size:11px;color:var(--muted);font-family:var(--mono)">Auditor</div>
  <a href="/console" class="nav-back">&#8592; Console</a>
</header>

<div class="page">
  <div class="title">Auditor — <span>Log Monitor</span></div>
  <div class="subtitle" id="page-sub">Loading findings...</div>

  <!-- STATS -->
  <div class="stats-row">
    <div class="stat-mini">
      <div class="sm-label">Critical</div>
      <div class="sm-val" id="stat-crit" style="color:var(--pink)">—</div>
      <div class="sm-sub">Unresolved</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">High</div>
      <div class="sm-val" id="stat-high" style="color:var(--amber)">—</div>
      <div class="sm-sub">Unresolved</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">Medium</div>
      <div class="sm-val" id="stat-med" style="color:var(--purple)">—</div>
      <div class="sm-sub">Unresolved</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">Total</div>
      <div class="sm-val" id="stat-total" style="color:var(--text)">—</div>
      <div class="sm-sub">Unresolved</div>
    </div>
  </div>

  <div class="two-col">
    <!-- LEFT: ISSUES LIST -->
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Unresolved Issues</span>
          <span class="panel-badge pb-pink" id="issues-badge">Loading</span>
        </div>
        <div class="panel-scroll" id="issues-list">
          <div class="empty"><div class="empty-icon">⏳</div>Fetching findings...</div>
        </div>
      </div>
    </div>

    <!-- RIGHT: SCAN COVERAGE + MORNING REPORT -->
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Scan Coverage</span>
          <span class="panel-badge pb-teal" id="scan-badge">—</span>
        </div>
        <div id="scan-list">
          <div class="empty" style="padding:20px">Loading...</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Last Morning Report</span>
          <span class="panel-badge pb-purple" id="report-badge">—</span>
        </div>
        <div id="report-body">
          <div class="empty" style="padding:20px">No reports yet</div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
function escHtml(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function ageSince(isoStr){
  if(!isoStr) return '—';
  const secs=Math.floor((Date.now()-new Date(isoStr).getTime())/1000);
  if(secs<60) return secs+'s ago';
  if(secs<3600) return Math.floor(secs/60)+'m ago';
  if(secs<86400) return Math.floor(secs/3600)+'h ago';
  return Math.floor(secs/86400)+'d ago';
}
function fmtSize(bytes){
  if(bytes==null) return '—';
  if(bytes<1024) return bytes+'B';
  if(bytes<1048576) return (bytes/1024).toFixed(0)+'K';
  return (bytes/1048576).toFixed(1)+'M';
}

async function load(){
  try{
    const r=await fetch('/api/auditor');
    const d=await r.json();
    render(d);
  }catch(e){
    document.getElementById('page-sub').textContent='Could not reach company server';
    document.getElementById('issues-list').innerHTML=
      '<div class="empty"><div class="empty-icon">⚠</div>'+escHtml(e.message)+'</div>';
  }
}

function render(d){
  const sev=d.by_severity||{};
  const issues=d.issues||[];
  const total=d.total_unresolved||0;

  // Subtitle
  if(d.error && !issues.length){
    document.getElementById('page-sub').textContent='Error: '+d.error;
  } else {
    document.getElementById('page-sub').textContent=
      total+' unresolved issue'+(total!==1?'s':'')+
      (d.scan_state&&d.scan_state.length ? ' · '+d.scan_state.length+' log files monitored' : '')+
      ' · refreshes every 60s';
  }

  // Stats
  document.getElementById('stat-crit').textContent  = sev.critical||0;
  document.getElementById('stat-high').textContent  = sev.high||0;
  document.getElementById('stat-med').textContent   = sev.medium||0;
  document.getElementById('stat-total').textContent = total;

  // Issues panel
  const issuesEl=document.getElementById('issues-list');
  const badge=document.getElementById('issues-badge');

  if(d.error && !issues.length){
    issuesEl.innerHTML='<div class="error-bar">'+escHtml(d.error)+'</div>'
      +'<div class="empty">Check that the company server is reachable and the auditor has run.</div>';
    badge.textContent='Error';
    badge.className='panel-badge pb-pink';
  } else if(!issues.length){
    issuesEl.innerHTML='<div class="empty"><div class="empty-icon">✓</div>No unresolved issues — system healthy</div>';
    badge.textContent='All clear';
    badge.className='panel-badge pb-teal';
  } else {
    badge.textContent=total+' issue'+(total!==1?'s':'');
    badge.className='panel-badge '+(sev.critical?'pb-pink':sev.high?'pb-amber':'pb-purple');
    issuesEl.innerHTML=issues.map(iss=>{
      const sevClass='sev-'+(iss.severity||'low');
      const hits=iss.hit_count>1?' <span style="color:var(--dim)">×'+iss.hit_count+'</span>':'';
      const firstSeen=iss.first_seen?ageSince(iss.first_seen):'?';
      const lastSeen=iss.last_seen?ageSince(iss.last_seen):'?';
      return '<div class="issue-row">'
        +'<div class="sev-badge '+sevClass+'">'+escHtml(iss.severity)+'</div>'
        +'<div class="issue-body">'
          +'<div class="issue-file">'+escHtml(iss.source_file)+hits+'</div>'
          +'<div class="issue-ctx">'+escHtml(iss.context||'')+'</div>'
          +'<div class="issue-meta">first: '+firstSeen+' · last: '+lastSeen+'</div>'
        +'</div>'
      +'</div>';
    }).join('');
  }

  // Scan coverage
  const scanEl=document.getElementById('scan-list');
  const scanBadge=document.getElementById('scan-badge');
  const scanState=d.scan_state||[];
  scanBadge.textContent=scanState.length+' files';
  if(!scanState.length){
    scanEl.innerHTML='<div class="empty" style="padding:16px">No log files tracked yet</div>';
  } else {
    scanEl.innerHTML=scanState.map(s=>{
      const fname=s.log_file?s.log_file.split('/').pop():s.log_file;
      const pct=s.file_size>0?Math.round(s.last_offset/s.file_size*100):100;
      return '<div class="scan-row">'
        +'<span class="scan-file">'+escHtml(fname)+'</span>'
        +'<span class="scan-age" style="color:'+(pct<100?'var(--amber)':'var(--teal)')+'">'+pct+'%</span>'
        +'<span class="scan-age">'+ageSince(s.last_scanned)+'</span>'
      +'</div>';
    }).join('');
  }

  // Morning report
  const rpt=d.morning_report;
  const reportBadge=document.getElementById('report-badge');
  const reportBody=document.getElementById('report-body');
  if(!rpt){
    reportBadge.textContent='None yet';
    reportBody.innerHTML='<div class="empty" style="padding:16px">Daily report generated at 6 AM ET</div>';
  } else {
    const status=rpt.status||'unknown';
    reportBadge.textContent=rpt.date||'?';
    reportBadge.className='panel-badge '+(status==='healthy'?'pb-teal':'pb-pink');
    const last24=rpt.last_24h||{};
    reportBody.innerHTML='<div class="report-grid">'
      +'<div class="rg-cell"><div class="rg-val" style="color:var(--pink)">'+(last24.critical&&last24.critical.unique!=null?last24.critical.unique:(last24.critical||0))+'</div><div class="rg-lab">Critical</div></div>'
      +'<div class="rg-cell"><div class="rg-val" style="color:var(--amber)">'+(last24.high&&last24.high.unique!=null?last24.high.unique:(last24.high||0))+'</div><div class="rg-lab">High</div></div>'
      +'<div class="rg-cell"><div class="rg-val" style="color:var(--purple)">'+(last24.medium&&last24.medium.unique!=null?last24.medium.unique:(last24.medium||0))+'</div><div class="rg-lab">Medium</div></div>'
      +'<div class="rg-cell"><div class="rg-val" style="color:var(--text)">'+(rpt.total_unresolved||0)+'</div><div class="rg-lab">Unresolved</div></div>'
    +'</div>';
  }
}

load();
setInterval(load, 60000);
</script>
</body>
</html>"""


@app.route("/audit")
def audit_page():
    return AUDIT_PAGE_HTML


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    trim_pi_events()
    if not SECRET_TOKEN:
        print("[Synthos Monitor] ✗ FATAL: SECRET_TOKEN is not set in .env — refusing to start.")
        print("[Synthos Monitor]   Run install_monitor.py to generate one.")
        raise SystemExit(1)

    load_registry()  # restore Pi state from last run

    # Register digest agent blueprint
    try:
        from digest_agent import digest_bp
        app.register_blueprint(digest_bp)
        print(f"[Synthos Monitor] Digest agent registered — /digest endpoint active")
    except ImportError:
        print(f"[Synthos Monitor] digest_agent.py not found — /digest endpoint unavailable")

    t = threading.Thread(target=silence_detector, daemon=True)
    t.start()

    # ── Self-heartbeat: monitor node reports its own metrics to itself ─────────
    def _self_heartbeat_loop():
        """
        Post this monitor node's own system metrics to /heartbeat every 5 minutes.
        Allows the node roster to show pi2w_monitor_node's CPU/RAM/temp inline
        with all other nodes — no external agent needed.
        """
        self_pi_id    = os.getenv("PI_ID",    "pi2w-monitor")
        self_pi_label = os.getenv("PI_LABEL", "Monitor Node")
        self_url      = f"http://127.0.0.1:{PORT}/heartbeat"
        interval      = int(os.getenv("SELF_HB_INTERVAL", "300"))  # default 5 min

        time.sleep(10)  # let Flask finish starting
        while True:
            try:
                import psutil as _ps
                vm   = _ps.virtual_memory()
                du   = _ps.disk_usage('/')
                net  = _ps.net_io_counters()
                load = os.getloadavg()
                gb   = 1024 ** 3

                cpu_t = None
                try:
                    with open('/sys/class/thermal/thermal_zone0/temp') as _f:
                        cpu_t = round(int(_f.read().strip()) / 1000, 1)
                except Exception:
                    pass

                cached_bytes = getattr(vm, 'cached', 0) + getattr(vm, 'buffers', 0)

                payload = {
                    "pi_id":          self_pi_id,
                    "label":          self_pi_label,
                    "agents":         {"synthos_monitor": "active"},
                    "operating_mode": "SUPERVISED",
                    "trading_mode":   "PAPER",
                    "kill_switch":    False,
                    # CPU
                    "cpu_percent":    round(_ps.cpu_percent(interval=0.5), 1),
                    "cpu_count":      _ps.cpu_count(logical=True),
                    "load_avg":       [round(load[0],2), round(load[1],2), round(load[2],2)],
                    # RAM
                    "ram_percent":    round(vm.percent, 1),
                    "ram_total_gb":   round(vm.total     / gb, 2),
                    "ram_used_gb":    round(vm.used      / gb, 2),
                    "ram_avail_gb":   round(vm.available / gb, 2),
                    "ram_cached_gb":  round(cached_bytes / gb, 2),
                    # Disk
                    "disk_percent":   round(du.percent, 1),
                    "disk_total_gb":  round(du.total / gb, 1),
                    "disk_used_gb":   round(du.used  / gb, 1),
                    "disk_free_gb":   round(du.free  / gb, 1),
                    # Network
                    "net_bytes_sent": net.bytes_sent,
                    "net_bytes_recv": net.bytes_recv,
                    # Temp
                    "cpu_temp":       cpu_t,
                }
                import requests as _req
                _req.post(self_url, json=payload,
                          headers={"X-Token": SECRET_TOKEN}, timeout=5)
                print(f"[SelfHB] Posted — CPU {payload['cpu_percent']}%  "
                      f"RAM {payload['ram_percent']}%  Temp {cpu_t}°C")
            except Exception as _e:
                print(f"[SelfHB] Failed: {_e}")
            time.sleep(interval)

    sh = threading.Thread(target=_self_heartbeat_loop, daemon=True)
    sh.start()
    # ──────────────────────────────────────────────────────────────────────────

    print(f"[Synthos Monitor] Running on port {PORT}")
    print(f"[Synthos Monitor] Console at http://0.0.0.0:{PORT}/console")
    if COMPANY_URL:
        print(f"[Synthos Monitor] Scoop events → Company Node at {COMPANY_URL}")
    else:
        print(f"[Synthos Monitor] COMPANY_URL not set — enqueue events will not be persisted")
    print(f"[Synthos Monitor] Tracking {len(pi_registry)} Pi(s) from persistent state")
    app.run(host="0.0.0.0", port=PORT)
