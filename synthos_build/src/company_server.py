"""
Company Server
==============
Runs on the Company Pi (Pi 4B, port 5010).
Receives Scoop queue events from retail Pi agents (directly or proxied via
the Monitor Node), persists them to company.db, and serves an ops dashboard.

Scoop (scoop.py) runs separately and drains the queue — dispatching alerts
via Resend.

.env required:
    SECRET_TOKEN=some_random_string       # shared with retail Pis + monitor
    PORT=5010
    COMPANY_DB_PATH=data/company.db       # optional override

Optional:
    RESEND_API_KEY=re_...                 # used only for health-check alerts
    ALERT_FROM=alerts@yourdomain.com
    ALERT_TO=ops@yourdomain.com

Retail Pi .env additions:
    COMPANY_URL=http://<company-pi-ip>:5010

Routes:
    POST /api/enqueue          — receive a Scoop event (X-Token auth)
    GET  /api/queue            — inspect queue (X-Token auth)
    POST /api/queue/<id>/skip  — mark a pending item skipped (X-Token auth)
    GET  /health               — unauthenticated health check
    GET  /console              — ops dashboard (X-Token cookie or header)
"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload limit

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_TOKEN  = os.getenv("SECRET_TOKEN") or os.getenv("COMPANY_TOKEN", "")
CF_ADMIN_EMAIL = os.getenv("OPERATOR_EMAIL", "").lower().strip()
PORT          = int(os.getenv("PORT", 5010))
ET            = ZoneInfo("America/New_York")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))
LOG_DIR  = os.path.join(os.path.dirname(_HERE), "logs")   # synthos_build/logs/

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


def _authorized():
    """Browser routes: accept either Cloudflare Access or SECRET_TOKEN."""
    return _cf_authorized() or _token_authorized()


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


@app.route("/")
@app.route("/console")
def console():
    """Ops dashboard — requires X-Token header, query param, or cookie."""
    # If token provided as query param, set cookie and redirect clean
    if request.args.get("token"):
        resp = redirect(url_for("console"))
        resp.set_cookie("company_token", request.args["token"], httponly=True, samesite="Lax")
        return resp
    if not _authorized():
        return (
            "<html><body style='font-family:monospace;background:#080b12;color:#fff;padding:40px'>"
            "<h2>Synthos Company Node</h2>"
            "<p style='color:rgba(255,255,255,0.5)'>Pass <code>?token=SECRET_TOKEN</code> "
            "or set <code>X-Token</code> header to access the console.</p>"
            "</body></html>"
        ), 401
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


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    trim_pi_events()
    if not SECRET_TOKEN:
        print("[Company] ✗ FATAL: SECRET_TOKEN is not set in .env — refusing to start.")
        print("[Company]   Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        raise SystemExit(1)
    print(f"[Company] Running on port {PORT}")
    print(f"[Company] Console at http://0.0.0.0:{PORT}/console?token=<SECRET_TOKEN>")
    print(f"[Company] Project status at http://0.0.0.0:{PORT}/project-status?token=<SECRET_TOKEN>")
    print(f"[Company] Display panel at http://0.0.0.0:{PORT}/display?token=<SECRET_TOKEN>")
    print(f"[Company] DB at {DB_PATH}")
    app.run(host="0.0.0.0", port=PORT)
