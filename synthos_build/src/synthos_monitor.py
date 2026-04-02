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

import os
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
ALERT_FROM           = os.getenv("ALERT_FROM", "alerts@example.com")
ALERT_TO             = os.getenv("ALERT_TO", "you@example.com")
# SECRET_TOKEN is the server-side env var name.
# MONITOR_TOKEN is the client-side env var name — accept both so
# operators who set only one side don't get silent 401s.
SECRET_TOKEN         = os.getenv("SECRET_TOKEN") or os.getenv("MONITOR_TOKEN", "changeme")
PORT                 = int(os.getenv("PORT", 5000))
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

# ── State ─────────────────────────────────────────────────────────────────────
pi_registry   = {}
registry_lock = threading.Lock()


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
            # History — keep last 48 heartbeat values for sparkline
            "history":           (existing.get("history", []) + [{
                "t": now_utc().isoformat(),
                "v": data.get("portfolio_value", data.get("portfolio", 0.0)),
            }])[-48:],
        }
        save_registry()

    return jsonify({"status": "ok"}), 200


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
    Receive a Scoop queue event from a retail Pi agent.
    If COMPANY_URL is configured, forwards the event to the Company Node.
    Otherwise logs receipt and returns 200 (graceful no-op — events are not
    persisted on the Monitor Node).

    Set COMPANY_URL=http://<company-pi-ip>:5010 on retail Pis to route events.
    """
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    # Forward to Company Node if configured
    if COMPANY_URL:
        try:
            import requests as _req
            r = _req.post(
                f"{COMPANY_URL}/api/enqueue",
                headers={"X-Token": SECRET_TOKEN, "Content-Type": "application/json"},
                json=request.get_json(silent=True) or {},
                timeout=5,
            )
            return jsonify(r.json()), r.status_code
        except Exception as e:
            print(f"[ENQUEUE] Forward to company node failed: {e}")
            return jsonify({"ok": False, "error": f"Company node unreachable: {e}"}), 502

    # No COMPANY_URL — log and acknowledge (not persisted on monitor node)
    data = request.get_json(silent=True) or {}
    print(
        f"[ENQUEUE] Received (no COMPANY_URL set — not persisted): "
        f"{data.get('event_type', '?')} P{data.get('priority', '?')} "
        f"from {data.get('source_agent', '?')}"
    )
    return jsonify({
        "ok": True,
        "queued": False,
        "note": "COMPANY_URL not set — event logged but not persisted",
    }), 200


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
.two-col{display:grid;grid-template-columns:1fr 340px;gap:16px;margin-bottom:20px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

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
  font-size:14px;font-weight:800;letter-spacing:-0.3px;
  position:relative;overflow:hidden;
}
.pi-avatar::after{content:'';position:absolute;inset:0;
  background:linear-gradient(145deg,rgba(255,255,255,0.18) 0%,transparent 55%)}
.av-teal{background:linear-gradient(135deg,rgba(0,245,212,0.3),rgba(0,245,212,0.1));
         border:1px solid rgba(0,245,212,0.25);color:var(--teal)}
.av-purple{background:linear-gradient(135deg,rgba(123,97,255,0.3),rgba(123,97,255,0.1));
           border:1px solid rgba(123,97,255,0.25);color:#a78bfa}
.av-amber{background:linear-gradient(135deg,rgba(255,179,71,0.3),rgba(255,179,71,0.1));
          border:1px solid rgba(255,179,71,0.25);color:var(--amber)}
.av-pink{background:linear-gradient(135deg,rgba(255,75,110,0.3),rgba(255,75,110,0.1));
         border:1px solid rgba(255,75,110,0.25);color:var(--pink)}

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
              justify-content:center;font-size:16px;font-weight:800;flex-shrink:0;
              position:relative;overflow:hidden}
.modal-avatar::after{content:'';position:absolute;inset:0;
  background:linear-gradient(145deg,rgba(255,255,255,0.2) 0%,transparent 55%)}
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
</style>
</head>
<body>

<!-- HEADER -->
<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="header-sub">Command Console</div>
  <div class="header-right">
    <div class="clock" id="clock">--:--:-- ET</div>
    <a href="/audit" style="padding:5px 12px;border-radius:8px;font-size:11px;font-weight:600;
       background:rgba(123,97,255,0.1);border:1px solid rgba(123,97,255,0.3);color:var(--purple);
       text-decoration:none;letter-spacing:0.04em">Agent 4</a>
    <div class="live-pill"><div class="live-dot"></div><span id="pi-count">0 Pis</span></div>
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
    <div class="fleet-card fc-teal">
      <div class="fleet-label">Pis Online</div>
      <div class="fleet-val" id="fl-online">0</div>
      <div class="fleet-sub" id="fl-total">of 0 registered</div>
    </div>
    <div class="fleet-card fc-purple">
      <div class="fleet-label">Fleet Portfolio</div>
      <div class="fleet-val" id="fl-portfolio">$0</div>
      <div class="fleet-sub">Combined value</div>
    </div>
    <div class="fleet-card fc-amber">
      <div class="fleet-label">Pending Approvals</div>
      <div class="fleet-val" id="fl-pending">0</div>
      <div class="fleet-sub">Across all Pis</div>
    </div>
    <div class="fleet-card fc-pink">
      <div class="fleet-label">Open Issues</div>
      <div class="fleet-val" id="fl-issues">0</div>
      <div class="fleet-sub">Needs attention</div>
    </div>
    <div class="fleet-card" style="border-color:rgba(255,255,255,0.07)">
      <div class="fleet-label">Open Positions</div>
      <div class="fleet-val" id="fl-positions" style="color:var(--text)">0</div>
      <div class="fleet-sub">Fleet-wide</div>
    </div>
    <div class="fleet-card" style="border-color:rgba(255,255,255,0.07)">
      <div class="fleet-label">Trades Today</div>
      <div class="fleet-val" id="fl-trades" style="color:var(--text)">0</div>
      <div class="fleet-sub">All Pis</div>
    </div>
  </div>

  <!-- TWO COLUMN: PI GRID + TODOS -->
  <div class="two-col">

    <!-- PI GRID -->
    <div>
      <div class="sec-title">Customer Pis <span id="sync-label" style="font-size:9px;color:var(--dim);font-weight:400;letter-spacing:0;text-transform:none">syncing...</span></div>
      <div class="pi-grid" id="pi-grid">
        <div style="color:var(--muted);font-size:12px;padding:20px;grid-column:1/-1">No Pis registered yet. Waiting for first heartbeat...</div>
      </div>
    </div>

    <!-- TODO LIST -->
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
    </div>

  </div>
</div>

<script>
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
function statusClass(s) { return s === 'online' ? 'online' : s === 'offline' ? 'offline' : 'warning'; }
function dotClass(s) { return s === 'online' ? 'online' : s === 'offline' ? 'offline' : s === 'warning' ? 'warning' : 'unknown'; }
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

// ── FETCH STATUS ──
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    piData = await r.json();
    renderPiGrid();
    updateFleetStats();
    document.getElementById('sync-label').textContent = 'synced ' + new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'});
  } catch(e) {}
}

// ── FLEET STATS ──
function updateFleetStats() {
  const pis = Object.values(piData);
  const total    = pis.length;
  const online   = pis.filter(p => p.status === 'online').length;
  const portfolio = pis.reduce((s,p) => s + (p.portfolio_value||0), 0);
  const pending  = pis.reduce((s,p) => s + (p.pending_approvals||0), 0);
  const positions = pis.reduce((s,p) => s + (p.open_positions||0), 0);
  const trades   = pis.reduce((s,p) => s + (p.trades_today||0), 0);

  const sv = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
  sv('fl-online', online);
  sv('fl-total', 'of ' + total + ' registered');
  sv('fl-portfolio', '$' + portfolio.toFixed(2));
  sv('fl-pending', pending);
  sv('fl-positions', positions);
  sv('fl-trades', trades);
  sv('pi-count', total + ' Pi' + (total===1?'':'s'));

  // Issues count from todos
  sv('fl-issues', allTodos.filter(t=>!t.resolved).length);
}

// ── PI GRID ──
function renderPiGrid() {
  const grid = document.getElementById('pi-grid');
  const pis  = Object.values(piData);
  if (!pis.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:20px;grid-column:1/-1">No Pis registered yet. Waiting for first heartbeat...</div>';
    return;
  }
  // Sort: online first, then offline
  pis.sort((a,b) => {
    const order = {online:0,warning:1,offline:2};
    return (order[a.status]??3) - (order[b.status]??3);
  });
  grid.innerHTML = pis.map(pi => {
    const sc   = statusClass(pi.status);
    const dc   = dotClass(pi.status);
    const av   = avatarColor(pi.pi_id);
    const ini  = initials(pi.label);
    const pnl  = pi.realized_gains || 0;
    const pnlCls = pnl >= 0 ? 'teal' : 'pink';
    const pnlStr = (pnl>=0?'+$':'-$') + Math.abs(pnl).toFixed(2);
    const modeBadge = pi.kill_switch ? '<span class="pi-badge pb-kill">HALTED</span>'
      : pi.operating_mode === 'AUTONOMOUS' ? '<span class="pi-badge pb-auto">AUTO</span>'
      : '<span class="pi-badge pb-supervised">SUPERVISED</span>';
    const tradeBadge = (pi.trading_mode||'PAPER') === 'PAPER'
      ? '<span class="pi-badge pb-paper">PAPER</span>' : '';
    const pendBadge = (pi.pending_approvals||0) > 0
      ? '<span class="pi-badge pb-pend">' + pi.pending_approvals + ' pending</span>' : '';

    return '<div class="pi-card ' + sc + '" onclick="openModal('' + pi.pi_id + '')">'
      + '<div class="pi-card-top">'
        + '<div class="pi-avatar ' + av + '">' + ini + '</div>'
        + '<div class="pi-info">'
          + '<div class="pi-name">' + (pi.label||pi.pi_id) + '</div>'
          + '<div class="pi-email">' + (pi.email||'No email') + '</div>'
          + '<div class="pi-id-tag">' + pi.pi_id + '</div>'
        + '</div>'
        + '<div class="status-dot-wrap">'
          + '<div class="sdot ' + dc + '"></div>'
          + '<span class="status-text st-' + sc + '">' + pi.status + '</span>'
        + '</div>'
      + '</div>'
      + '<div class="pi-stats">'
        + '<div class="pi-stat"><div class="psl">Portfolio</div><div class="psv teal">$' + (pi.portfolio_value||0).toFixed(2) + '</div></div>'
        + '<div class="pi-stat"><div class="psl">Positions</div><div class="psv ' + ((pi.open_positions||0)>0?'amber':'') + '">' + (pi.open_positions||0) + '</div></div>'
        + '<div class="pi-stat"><div class="psl">P&L</div><div class="psv ' + pnlCls + '">' + pnlStr + '</div></div>'
      + '</div>'
      + '<div class="pi-footer">'
        + modeBadge + tradeBadge + pendBadge
        + '<span class="pi-uptime">' + (pi.uptime||'—') + ' up · ' + ageSince(pi.last_seen) + '</span>'
      + '</div>'
    + '</div>';
  }).join('');
}

// ── MODAL ──
async function openModal(piId) {
  modalPiId = piId;
  currentModalTab = 'overview';
  document.getElementById('modal-overlay').classList.add('show');
  document.body.style.overflow = 'hidden';

  // Set header from cached data immediately
  const pi = piData[piId] || {};
  const av = avatarColor(piId);
  document.getElementById('modal-avatar').className = 'modal-avatar ' + av;
  document.getElementById('modal-avatar').textContent = initials(pi.label||piId);
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
  if (tab === 'overview') {
    const pnl = pi.realized_gains || 0;
    const pos = pi.positions || [];
    body.innerHTML =
      '<div class="modal-stats">'
        + '<div class="mstat"><div class="mstat-label">Portfolio</div><div class="mstat-val mv-teal">$' + (pi.portfolio_value||0).toFixed(2) + '</div><div class="mstat-sub">$' + (pi.cash||0).toFixed(2) + ' cash</div></div>'
        + '<div class="mstat"><div class="mstat-label">Realized P&L</div><div class="mstat-val ' + (pnl>=0?'mv-teal':'mv-pink') + '">' + (pnl>=0?'+$':'-$') + Math.abs(pnl).toFixed(2) + '</div><div class="mstat-sub">This month</div></div>'
        + '<div class="mstat"><div class="mstat-label">Positions</div><div class="mstat-val ' + ((pi.open_positions||0)>0?'mv-amber':'') + '">' + (pi.open_positions||0) + '</div><div class="mstat-sub">' + (pi.trades_today||0) + ' trades today</div></div>'
        + '<div class="mstat"><div class="mstat-label">Pending</div><div class="mstat-val ' + ((pi.pending_approvals||0)>0?'mv-amber':'') + '">' + (pi.pending_approvals||0) + '</div><div class="mstat-sub">Approvals</div></div>'
        + '<div class="mstat"><div class="mstat-label">Flags</div><div class="mstat-val ' + ((pi.urgent_flags||0)>0?'mv-pink':'') + '">' + (pi.urgent_flags||0) + '</div><div class="mstat-sub">Urgent</div></div>'
        + '<div class="mstat"><div class="mstat-label">Uptime</div><div class="mstat-val" style="font-size:16px">' + (pi.uptime||'N/A') + '</div><div class="mstat-sub">Since last reboot</div></div>'
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Open Positions</div>'
      + '<div style="margin-bottom:14px">'
      + (pos.length ? pos.map(p => {
          const pnl2 = p.pnl || 0;
          return '<div class="pos-row">'
            + '<div class="pos-chip">' + (p.ticker||'?').slice(0,4) + '</div>'
            + '<div><div class="pos-ticker-t">' + p.ticker + '</div><div class="pos-shares-t">' + (p.shares||0).toFixed(2) + ' @ $' + (p.entry_price||0).toFixed(2) + '</div></div>'
            + '<div class="pos-pnl-t ' + (pnl2>=0?'mv-teal':'mv-pink') + '">' + (pnl2>=0?'+$':'-$') + Math.abs(pnl2).toFixed(2) + '</div>'
            + '</div>';
        }).join('') : '<div style="color:var(--muted);font-size:12px;padding:12px 0">No open positions</div>')
      + '</div>'
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Agent Status</div>'
      + renderAgents(pi.agents||{});

  } else if (tab === 'performance') {
    body.innerHTML =
      '<div class="modal-graph-wrap">'
        + '<div class="modal-graph-title">Portfolio History</div>'
        + '<div class="modal-graph-canvas"><canvas id="modal-chart"></canvas></div>'
      + '</div>'
      + '<div class="modal-stats">'
        + '<div class="mstat"><div class="mstat-label">Mode</div><div class="mstat-val" style="font-size:14px">' + (pi.operating_mode||'SUPERVISED') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">Trading</div><div class="mstat-val" style="font-size:14px">' + (pi.trading_mode||'PAPER') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">Kill Switch</div><div class="mstat-val ' + (pi.kill_switch?'mv-pink':'mv-teal') + '" style="font-size:14px">' + (pi.kill_switch?'ACTIVE':'CLEAR') + '</div></div>'
      + '</div>';

    // Draw chart
    setTimeout(() => {
      const hist = pi.history || [];
      const ctx = document.getElementById('modal-chart');
      if (!ctx || !hist.length) return;
      const labels = hist.map(h => new Date(h.t).toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit'}));
      const values = hist.map(h => h.v);
      const grad = ctx.getContext('2d').createLinearGradient(0,0,0,100);
      grad.addColorStop(0, 'rgba(0,245,212,0.2)');
      grad.addColorStop(1, 'rgba(0,245,212,0)');
      if (modalChartInst) modalChartInst.destroy();
      modalChartInst = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets: [{
          data: values, borderColor: '#00f5d4', borderWidth: 2,
          fill: true, backgroundColor: grad, tension: 0.4,
          pointRadius: 0, pointHitRadius: 8,
        }]},
        options: {
          responsive:true, maintainAspectRatio:false,
          plugins:{legend:{display:false},tooltip:{
            backgroundColor:'rgba(13,17,32,0.95)',borderColor:'rgba(0,245,212,0.3)',borderWidth:1,
            titleColor:'rgba(255,255,255,0.5)',bodyColor:'#00f5d4',bodyFont:{weight:'bold'},
            callbacks:{label:c=>'$'+c.parsed.y.toFixed(2)}
          }},
          scales:{
            x:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},maxTicksLimit:6}},
            y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:9},callback:v=>'$'+v.toFixed(0)},position:'right'}
          }
        }
      });
    }, 50);

  } else if (tab === 'logs') {
    const errors = pi.last_errors || [];
    body.innerHTML =
      '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Recent Errors</div>'
      + (errors.length
          ? '<div class="error-log">' + errors.map(e => escHtml(e)).join('\n') + '</div>'
          : '<div class="error-log empty">✓ No recent errors</div>')
      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin:14px 0 8px">Agent Status</div>'
      + renderAgents(pi.agents||{})
      + '<div style="font-size:10px;color:var(--muted);margin-top:12px">Full logs available in customer portal at ' + (pi.pi_id||'') + ':5001/logs</div>';

  } else if (tab === 'admin') {
    body.innerHTML =
      '<div class="modal-stats" style="grid-template-columns:1fr 1fr;margin-bottom:16px">'
        + '<div class="mstat"><div class="mstat-label">Customer</div><div class="mstat-val" style="font-size:14px;word-break:break-all">' + (pi.label||'—') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">Email</div><div class="mstat-val" style="font-size:12px;word-break:break-all"><a href="mailto:' + (pi.email||'') + '" style="color:var(--teal)">' + (pi.email||'—') + '</a></div></div>'
        + '<div class="mstat"><div class="mstat-label">Pi ID</div><div class="mstat-val" style="font-size:11px;font-family:var(--mono)">' + (pi.pi_id||'—') + '</div></div>'
        + '<div class="mstat"><div class="mstat-label">First Seen</div><div class="mstat-val" style="font-size:12px">' + (pi.first_seen||'—').slice(0,10) + '</div></div>'
      + '</div>'

      // Key update form — pushes to Pi portal which writes to .env
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
          + '<input id="adm-alert-to" type="email" placeholder="customer@email.com" style="width:100%;padding:7px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;outline:none"></div>'
      + '</div>'
      + '<div style="display:flex;gap:8px;margin-bottom:16px">'
        + '<button onclick="pushKeysToPi(\'' + (pi.pi_id||'') + '\')" style="padding:9px 18px;border-radius:10px;background:var(--teal2);border:1px solid rgba(0,245,212,0.3);color:var(--teal);font-size:11px;font-weight:700;cursor:pointer;font-family:var(--sans)">Push Keys to Pi</button>'
        + '<div id="adm-key-result-' + (pi.pi_id||'') + '" style="font-size:11px;color:var(--muted);align-self:center"></div>'
      + '</div>'

      + '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Danger Zone</div>'
      + '<div style="display:flex;flex-direction:column;gap:8px">'
        + '<button onclick="promptDelete(\'' + pi.pi_id + '\')" style="padding:10px 16px;border-radius:10px;background:var(--pink2);border:1px solid rgba(255,75,110,0.25);color:var(--pink);font-size:12px;font-weight:600;cursor:pointer;text-align:left;font-family:var(--sans)">Remove Pi from Registry</button>'
      + '</div>';
  }
}

function renderAgents(agents) {
  const names = {'trade_logic_agent':'Trade Logic','news_agent':'News','market_sentiment_agent':'Market Sentiment'};
  const list = Object.keys(names);
  return '<div>' + list.map(k => {
    const status = agents[k];
    const hasStatus = !!status;
    return '<div class="agent-row">'
      + '<div class="agent-dot" style="background:' + (hasStatus?'var(--teal)':'var(--muted)') + ';box-shadow:' + (hasStatus?'0 0 5px var(--teal)':'none') + '"></div>'
      + '<span class="agent-name">' + names[k] + '</span>'
      + '<span class="agent-status">' + (status||'No data') + '</span>'
    + '</div>';
  }).join('') + '</div>';
}

function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── DELETE ──
function promptDelete(piId) {
  pendingDelete = piId;
  document.getElementById('confirm-msg').textContent = 'Remove "' + piId + '" from the registry?';
  document.getElementById('confirm-overlay').classList.add('show');
}
function cancelDelete() { pendingDelete=null; document.getElementById('confirm-overlay').classList.remove('show'); }
async function confirmDelete() {
  if (!pendingDelete) return;
  try {
    await fetch('/api/delete/' + encodeURIComponent(pendingDelete), {
      method:'DELETE', headers:{'X-Token':SECRET_TOKEN}
    });
    toast('✓ Pi removed', 'ok');
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
      + '<button class="resolve-btn" onclick="resolveTodo('' + CSS.escape(t.id) + '',event)">Done</button>'
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

// ── COUNTDOWN ──
let countdown = 30;
function tickCountdown() {
  countdown--;
  if (countdown <= 0) { countdown = 30; fetchStatus(); }
}

// ── INIT ──
fetchStatus();
fetchTodos();
setInterval(tickCountdown, 1000);
setInterval(fetchTodos, 120000);
</script>
</body>
</html>"""

@app.route("/console")
def console():
    return render_template_string(DASHBOARD, secret_token=SECRET_TOKEN)

# keep old / route as JSON redirect
@app.route("/")
def index():
    return jsonify({"status": "Synthos Monitor online", "console": "/console", "api": "/api/status"})


@app.route("/api/todos", methods=["GET"])
def api_todos_fallback():
    """
    Fallback todos endpoint — used when digest_agent blueprint is not loaded.
    Returns empty list so console JS doesn't break.
    """
    # If digest_agent blueprint is registered it will handle this route first.
    # This only fires if the blueprint isn't loaded.
    TODO_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.triage_todos.json')
    import json as _json
    if os.path.exists(TODO_STORE):
        try:
            todos = _json.load(open(TODO_STORE))
            unresolved = [t for t in todos if not t.get('resolved')]
            return jsonify(unresolved), 200
        except Exception:
            pass
    return jsonify([]), 200


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
<title>Synthos — Audit Agent</title>
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
.page{max-width:1100px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,var(--purple),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:12px;color:var(--muted);margin-bottom:24px}

/* PI selector */
.pi-tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.pi-tab{padding:6px 14px;border-radius:10px;font-size:11px;font-weight:600;cursor:pointer;
        background:transparent;border:1px solid var(--border);color:var(--muted);
        font-family:var(--sans);transition:all 0.15s}
.pi-tab.active{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.3);color:var(--purple)}
.pi-tab:hover:not(.active){border-color:var(--border2);color:var(--text)}

/* Health score */
.health-bar{border-radius:16px;padding:16px 20px;margin-bottom:20px;
            display:flex;align-items:center;gap:16px;border:1px solid;
            background:var(--surface)}
.health-score{font-size:40px;font-weight:800;letter-spacing:-2px;flex-shrink:0;width:80px}
.health-info{flex:1}
.health-label{font-size:13px;font-weight:700;margin-bottom:3px}
.health-summary{font-size:12px;color:var(--muted);line-height:1.5}
.health-meta{font-size:10px;color:var(--dim);font-family:var(--mono);margin-top:4px}
.score-ring{position:relative;width:64px;height:64px;flex-shrink:0}

/* Two column */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:800px){.two-col{grid-template-columns:1fr}}

/* Panels */
.panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden}
.panel-header{padding:14px 16px;border-bottom:1px solid var(--border);
              display:flex;align-items:center;gap:8px}
.panel-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);flex:1}
.panel-badge{padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;border:1px solid}
.pb-purple{background:rgba(123,97,255,0.08);border-color:rgba(123,97,255,0.25);color:var(--purple)}
.pb-teal{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.pb-amber{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.2);color:var(--amber)}
.pb-pink{background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.2);color:var(--pink)}
.panel-scroll{max-height:420px;overflow-y:auto}

/* Task items */
.task-item{padding:12px 16px;border-bottom:1px solid var(--border);
           display:flex;align-items:flex-start;gap:10px}
.task-item:last-child{border-bottom:none}
.task-status{width:22px;height:22px;border-radius:6px;flex-shrink:0;margin-top:1px;
             display:flex;align-items:center;justify-content:center;font-size:11px}
.ts-pending{background:rgba(255,179,71,0.12);border:1px solid rgba(255,179,71,0.2);color:var(--amber)}
.ts-done{background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.2);color:var(--teal)}
.ts-fail{background:rgba(255,75,110,0.1);border:1px solid rgba(255,75,110,0.2);color:var(--pink)}
.ts-active{background:rgba(123,97,255,0.15);border:1px solid rgba(123,97,255,0.3);color:var(--purple);
           animation:spin 2s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.task-body{flex:1;min-width:0}
.task-title{font-size:12px;font-weight:600;color:var(--text);margin-bottom:3px}
.task-file{font-size:10px;font-family:var(--mono);color:var(--purple);margin-bottom:2px}
.task-desc{font-size:11px;color:var(--muted);line-height:1.5}
.task-result{font-size:10px;color:var(--teal);margin-top:3px;font-style:italic}
.task-result.fail{color:var(--pink)}
.task-meta{font-size:9px;color:var(--dim);margin-top:3px;font-family:var(--mono)}
.cat-badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;
           letter-spacing:0.04em;margin-bottom:4px}
.cat-UX{background:rgba(0,245,212,0.1);color:var(--teal)}
.cat-Utility{background:rgba(123,97,255,0.1);color:var(--purple)}
.cat-Performance{background:rgba(255,179,71,0.1);color:var(--amber)}
.cat-Docs{background:rgba(255,255,255,0.06);color:var(--muted)}
.pri-bar{display:flex;align-items:center;gap:4px;margin-top:4px}
.pri-dot{width:5px;height:5px;border-radius:50%;background:var(--border)}
.pri-dot.active{background:var(--purple)}

/* Audit issues */
.issue-item{padding:10px 16px;border-bottom:1px solid var(--border);
            display:flex;gap:8px;font-size:11px}
.issue-item:last-child{border-bottom:none}
.issue-icon{flex-shrink:0;font-weight:700;width:14px}
.ii-crit{color:var(--pink)}
.ii-warn{color:var(--amber)}
.ii-info{color:var(--dim)}
.issue-body{flex:1}
.issue-cat{font-weight:600;color:var(--text)}
.issue-msg{color:var(--muted)}
.issue-fix{font-size:10px;color:var(--teal);margin-top:2px}

/* Stats row */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.stat-mini{padding:12px 14px;border-radius:12px;border:1px solid var(--border);background:var(--surface)}
.sm-label{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.sm-val{font-size:20px;font-weight:700;color:var(--text)}
.sm-sub{font-size:10px;color:var(--muted);margin-top:2px}

/* Empty state */
.empty{padding:32px;text-align:center;color:var(--muted);font-size:12px}
.empty-icon{font-size:28px;margin-bottom:10px}

/* Current task highlight */
.current-task{border-radius:14px;border:1px solid rgba(123,97,255,0.3);
              background:linear-gradient(135deg,rgba(123,97,255,0.08) 0%,var(--surface) 50%);
              padding:16px 18px;margin-bottom:20px;position:relative;overflow:hidden}
.current-task::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(123,97,255,0.6),transparent)}
.ct-label{font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
          color:var(--purple);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.ct-pulse{width:6px;height:6px;border-radius:50%;background:var(--purple);
          box-shadow:0 0 6px var(--purple);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.ct-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px}
.ct-file{font-size:11px;font-family:var(--mono);color:var(--purple);margin-bottom:6px}
.ct-desc{font-size:12px;color:var(--muted);line-height:1.5}
</style>
</head>
<body>

<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div style="font-size:11px;color:var(--muted);font-family:var(--mono)">Audit Agent</div>
  <a href="/console" class="nav-back">&#8592; Console</a>
</header>

<div class="page">
  <div class="title">Agent 4 — <span>Self-Improvement</span></div>
  <div class="subtitle" id="page-sub">Loading audit data...</div>

  <!-- PI SELECTOR -->
  <div class="pi-tabs" id="pi-tabs"></div>

  <!-- CURRENT TASK -->
  <div id="current-task-wrap" style="display:none">
    <div class="current-task" id="current-task-card">
      <div class="ct-label"><div class="ct-pulse"></div>Currently Working On</div>
      <div class="ct-title" id="ct-title">—</div>
      <div class="ct-file" id="ct-file">—</div>
      <div class="ct-desc" id="ct-desc">—</div>
    </div>
  </div>

  <!-- HEALTH + STATS -->
  <div class="health-bar" id="health-bar" style="border-color:rgba(255,255,255,0.1)">
    <div class="health-score" id="health-score" style="color:var(--muted)">—</div>
    <div class="health-info">
      <div class="health-label" id="health-label">No audit data yet</div>
      <div class="health-summary" id="health-summary">Run: python3 agent4_audit.py on the Pi</div>
      <div class="health-meta" id="health-meta"></div>
    </div>
  </div>

  <div class="stats-row">
    <div class="stat-mini">
      <div class="sm-label">Tasks Done</div>
      <div class="sm-val" id="stat-done" style="color:var(--teal)">0</div>
      <div class="sm-sub">Completed</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">Pending</div>
      <div class="sm-val" id="stat-pending" style="color:var(--amber)">0</div>
      <div class="sm-sub">In backlog</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">Failed</div>
      <div class="sm-val" id="stat-failed" style="color:var(--pink)">0</div>
      <div class="sm-sub">Skipped</div>
    </div>
    <div class="stat-mini">
      <div class="sm-label">Lines Changed</div>
      <div class="sm-val" id="stat-lines" style="color:var(--purple)">0</div>
      <div class="sm-sub">Total edits</div>
    </div>
  </div>

  <!-- TWO COLUMN: BACKLOG + AUDIT ISSUES -->
  <div class="two-col">

    <!-- BACKLOG -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Improvement Backlog</span>
        <span class="panel-badge pb-purple" id="backlog-badge">Loading</span>
      </div>
      <div class="panel-scroll" id="backlog-list">
        <div class="empty"><div class="empty-icon">🤖</div>No tasks yet — runs after first clean audit pass</div>
      </div>
    </div>

    <!-- COMPLETED -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Completed Improvements</span>
        <span class="panel-badge pb-teal" id="completed-badge">0</span>
      </div>
      <div class="panel-scroll" id="completed-list">
        <div class="empty"><div class="empty-icon">✓</div>No completed tasks yet</div>
      </div>
    </div>

  </div>

  <!-- AUDIT FINDINGS -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Last Audit Findings</span>
      <span class="panel-badge pb-amber" id="findings-badge">—</span>
    </div>
    <div id="findings-list">
      <div class="empty">Select a Pi above to load audit data</div>
    </div>
  </div>

</div>

<script>
let activePiId = null;
let piData = {};

// ── INIT ──
async function init() {
  try {
    const r = await fetch('/api/status');
    piData = await r.json();
    renderPiTabs();
    const pis = Object.keys(piData);
    if (pis.length) loadPiData(pis[0]);
  } catch(e) {
    document.getElementById('page-sub').textContent = 'Could not load Pi list';
  }
}

function renderPiTabs() {
  const wrap = document.getElementById('pi-tabs');
  const pis  = Object.values(piData);
  if (!pis.length) {
    wrap.innerHTML = '<div style="font-size:12px;color:var(--muted)">No Pis registered yet</div>';
    return;
  }
  wrap.innerHTML = pis.map(pi =>
    '<button class="pi-tab" id="tab-' + pi.pi_id + '" onclick="loadPiData(\'' + pi.pi_id + '\')">'
    + (pi.label || pi.pi_id)
    + '</button>'
  ).join('');
}

async function loadPiData(piId) {
  activePiId = piId;
  document.querySelectorAll('.pi-tab').forEach(b => b.classList.remove('active'));
  const tab = document.getElementById('tab-' + piId);
  if (tab) tab.classList.add('active');

  const pi = piData[piId] || {};
  document.getElementById('page-sub').textContent =
    (pi.label || piId) + ' · ' + (pi.email || 'No email') + ' · Last seen ' + ageSince(pi.last_seen);

  // Load audit + backlog in parallel
  const [auditData, backlogData] = await Promise.all([
    fetchAudit(piId),
    fetchBacklog(piId),
  ]);

  renderHealth(auditData);
  renderFindings(auditData);
  renderBacklog(backlogData);
}

async function fetchAudit(piId) {
  try {
    const r = await fetch('/api/audit/' + encodeURIComponent(piId));
    return r.ok ? await r.json() : null;
  } catch(e) { return null; }
}

async function fetchBacklog(piId) {
  try {
    const r = await fetch('/api/backlog/' + encodeURIComponent(piId));
    return r.ok ? await r.json() : {tasks:[]};
  } catch(e) { return {tasks:[]}; }
}

// ── RENDER HEALTH ──
function renderHealth(d) {
  if (!d || !d.health_score) {
    document.getElementById('health-label').textContent = 'No audit data yet';
    document.getElementById('health-summary').textContent = 'Run: python3 agent4_audit.py on the Pi';
    return;
  }
  const score = d.health_score;
  const color = score >= 90 ? '--teal' : score >= 70 ? '--amber' : '--pink';
  document.getElementById('health-score').textContent = score;
  document.getElementById('health-score').style.color = 'var(' + color + ')';
  document.getElementById('health-label').textContent =
    d.health_label + ' · ' + (d.deep ? 'Deep pass' : 'Light pass');
  document.getElementById('health-summary').textContent = d.summary || '';
  const bar = document.getElementById('health-bar');
  bar.style.borderColor = score >= 90 ? 'rgba(0,245,212,0.2)'
    : score >= 70 ? 'rgba(255,179,71,0.2)' : 'rgba(255,75,110,0.2)';
  if (d.timestamp) {
    document.getElementById('health-meta').textContent =
      'Last run: ' + new Date(d.timestamp).toLocaleString('en-US',{timeZone:'America/New_York',hour12:false}) + ' ET'
      + ' · ' + (d.elapsed||0) + 's runtime';
  }
}

// ── RENDER FINDINGS ──
function renderFindings(d) {
  const list  = document.getElementById('findings-list');
  const badge = document.getElementById('findings-badge');
  if (!d) {
    list.innerHTML = '<div class="empty">Could not reach Pi portal — make sure it\'s running on port 5001</div>';
    badge.textContent = '—';
    return;
  }
  const all = [...(d.critical||[]).map(f=>({...f,level:'CRITICAL'})),
               ...(d.warnings||[]).map(f=>({...f,level:'WARN'}))];
  badge.textContent = all.length ? all.length + ' issues' : 'All clear';
  badge.className   = 'panel-badge ' + (all.length ? 'pb-pink' : 'pb-teal');
  if (!all.length) {
    list.innerHTML = '<div class="empty"><div class="empty-icon">✓</div>No issues — system healthy</div>';
    return;
  }
  list.innerHTML = all.map(f => {
    const icon = f.level === 'CRITICAL' ? '✗' : '⚠';
    const cls  = f.level === 'CRITICAL' ? 'ii-crit' : 'ii-warn';
    return '<div class="issue-item">'
      + '<div class="issue-icon ' + cls + '">' + icon + '</div>'
      + '<div class="issue-body">'
        + '<span class="issue-cat">[' + f.category + ']</span> '
        + '<span class="issue-msg">' + escHtml(f.message) + '</span>'
        + (f.fix ? '<div class="issue-fix">→ ' + escHtml(f.fix) + '</div>' : '')
      + '</div>'
    + '</div>';
  }).join('');
}

// ── RENDER BACKLOG ──
function renderBacklog(d) {
  const tasks = (d && d.tasks) ? d.tasks : [];

  const pending   = tasks.filter(t => t.status === 'pending');
  const completed = tasks.filter(t => t.status === 'completed');
  const failed    = tasks.filter(t => t.status === 'failed');

  // Stats
  document.getElementById('stat-done').textContent    = completed.length;
  document.getElementById('stat-pending').textContent = pending.length;
  document.getElementById('stat-failed').textContent  = failed.length;
  document.getElementById('stat-lines').textContent   =
    tasks.reduce((s,t) => s + (t.lines_changed||0), 0);

  document.getElementById('backlog-badge').textContent  = pending.length + ' pending';
  document.getElementById('completed-badge').textContent = completed.length;

  // Current task (highest priority pending)
  const sorted = [...pending].sort((a,b) => -((a.priority||0)-(b.priority||0)));
  const current = sorted[0];
  const wrap = document.getElementById('current-task-wrap');
  if (current) {
    wrap.style.display = 'block';
    document.getElementById('ct-title').textContent = current.title || '—';
    document.getElementById('ct-file').textContent  = current.file || '—';
    document.getElementById('ct-desc').textContent  = current.description || '—';
  } else {
    wrap.style.display = 'none';
  }

  // Pending list
  const backlogEl = document.getElementById('backlog-list');
  if (!pending.length) {
    backlogEl.innerHTML = '<div class="empty"><div class="empty-icon">✓</div>Backlog empty — new ideas generated on next clean pass</div>';
  } else {
    backlogEl.innerHTML = pending.map(t => taskCard(t, 'pending')).join('');
  }

  // Completed list
  const completedEl = document.getElementById('completed-list');
  const allDone = [...completed, ...failed].sort((a,b) =>
    new Date(b.completed||0) - new Date(a.completed||0));
  if (!allDone.length) {
    completedEl.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div>No completed tasks yet</div>';
  } else {
    completedEl.innerHTML = allDone.map(t =>
      taskCard(t, t.status === 'completed' ? 'done' : 'fail')
    ).join('');
  }
}

function taskCard(t, statusType) {
  const icons = {pending:'·', done:'✓', fail:'✗', active:'↻'};
  const classes = {pending:'ts-pending', done:'ts-done', fail:'ts-fail', active:'ts-active'};
  const catClass = 'cat-' + (t.category||'Docs').replace(/[^a-zA-Z]/g,'');

  // Priority dots (1-10 → 5 dots)
  const dots = Array.from({length:5}, (_,i) =>
    '<div class="pri-dot' + (i < Math.round((t.priority||5)/2) ? ' active' : '') + '"></div>'
  ).join('');

  return '<div class="task-item">'
    + '<div class="task-status ' + classes[statusType] + '">' + icons[statusType] + '</div>'
    + '<div class="task-body">'
      + '<div class="cat-badge ' + catClass + '">' + (t.category||'?') + '</div>'
      + '<div class="task-title">' + escHtml(t.title||'Untitled') + '</div>'
      + '<div class="task-file">' + (t.file||'—') + '</div>'
      + '<div class="task-desc">' + escHtml(t.description||'') + '</div>'
      + (t.result ? '<div class="task-result' + (statusType==='fail'?' fail':'') + '">'
          + (statusType==='done'?'✓ ':'✗ ') + escHtml(t.result) + '</div>' : '')
      + '<div class="task-meta">'
        + '<div class="pri-bar">' + dots + '<span style="font-size:9px;color:var(--dim);margin-left:4px">Priority ' + (t.priority||5) + '/10</span></div>'
        + (t.lines_changed ? ' · ' + t.lines_changed + ' lines changed' : '')
        + (t.completed ? ' · ' + new Date(t.completed).toLocaleDateString() : '')
      + '</div>'
    + '</div>'
  + '</div>';
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function ageSince(isoStr) {
  if (!isoStr) return 'unknown';
  const secs = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs/60) + 'm ago';
  return Math.floor(secs/3600) + 'h ago';
}

init();
setInterval(() => { if (activePiId) loadPiData(activePiId); }, 60000);
</script>
</body>
</html>"""


@app.route("/audit")
def audit_page():
    return AUDIT_PAGE_HTML


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
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
    print(f"[Synthos Monitor] Running on port {PORT}")
    print(f"[Synthos Monitor] Console at http://0.0.0.0:{PORT}/console")
    if COMPANY_URL:
        print(f"[Synthos Monitor] Scoop events → Company Node at {COMPANY_URL}")
    else:
        print(f"[Synthos Monitor] COMPANY_URL not set — enqueue events will not be persisted")
    print(f"[Synthos Monitor] Tracking {len(pi_registry)} Pi(s) from persistent state")
    app.run(host="0.0.0.0", port=PORT)
