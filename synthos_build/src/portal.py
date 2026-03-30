"""
portal.py — Synthos Web Portal
Synthos

Serves the operator portal on port 5001.
Provides: kill switch, supervised mode trade approval queue,
          system status, autonomous mode unlock gate.

Runs continuously on the Pi:
  @reboot sleep 90 && python3 /home/pi/synthos/synthos_build/src/portal.py &

Or add to crontab:
  @reboot sleep 90 && python3 /home/pi/synthos/synthos_build/src/portal.py >> /home/pi/synthos/synthos_build/logs/portal.log 2>&1 &

Access at: http://raspberrypi.local:5001
           http://10.0.0.224:5001  (or your Pi's IP)

.env keys used:
  AUTONOMOUS_UNLOCK_KEY   — key issued after onboarding call
  OPERATING_MODE          — SUPERVISED or AUTONOMOUS
  PI_ID                   — display name
  PORTAL_PASSWORD         — optional basic auth (recommended)
"""

import os
import sys
import json
import logging
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template_string, redirect, session
from dotenv import load_dotenv

_SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))   # src/
_ROOT_DIR            = os.path.dirname(_SCRIPT_DIR)                  # synthos_build/
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

PROJECT_DIR          = _SCRIPT_DIR                                   # keep for co-located script references
KILL_SWITCH_FILE     = os.path.join(_ROOT_DIR, '.kill_switch')
ENV_PATH             = os.path.join(_ROOT_DIR, 'user', '.env')
LOG_DIR              = os.path.join(_ROOT_DIR, 'logs')
ET                   = ZoneInfo("America/New_York")
PORT                 = int(os.environ.get('PORTAL_PORT', 5001))
PI_ID                = os.environ.get('PI_ID', 'synthos-pi')
PORTAL_PASSWORD      = os.environ.get('PORTAL_PASSWORD', '')
AUTONOMOUS_UNLOCK_KEY = os.environ.get('AUTONOMOUS_UNLOCK_KEY', '')
OPERATING_MODE       = os.environ.get('OPERATING_MODE', 'SUPERVISED').upper()
MONITOR_URL          = os.environ.get('MONITOR_URL', 'http://localhost:5000')
MONITOR_TOKEN        = os.environ.get('MONITOR_TOKEN', 'synthos-default-token')

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s portal: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('portal')

app = Flask(__name__)
app.secret_key = os.environ.get('PORTAL_SECRET_KEY', secrets.token_hex(32))

# Custom Jinja filter for file timestamps
@app.template_filter('timestamp_to_date')
def timestamp_to_date(ts):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%m/%d %H:%M')
    except Exception:
        return '—'


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ── AUTH ──────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Portal — Sign In</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root { --bg:#f5f0e8; --surface:#ede8df; --border:#c8bfaa; --text:#1a1612; --muted:#7a7060;
        --green:#2d6a1f; --red:#8b1a1a; --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:var(--sans);
       min-height:100vh; display:flex; align-items:center; justify-content:center; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:4px;
        padding:2rem; width:100%; max-width:340px; }
.wordmark { font-family:var(--mono); font-size:1rem; font-weight:600; letter-spacing:0.12em;
            margin-bottom:0.25rem; }
.pi-id { font-family:var(--mono); font-size:0.72rem; color:var(--muted); margin-bottom:1.5rem; }
label { font-size:0.75rem; font-weight:600; letter-spacing:0.08em; text-transform:uppercase;
        color:var(--muted); display:block; margin-bottom:0.3rem; }
input { font-family:var(--mono); font-size:0.9rem; padding:0.55rem 0.75rem;
        border:1px solid var(--border); border-radius:3px; background:#fff;
        width:100%; margin-bottom:1rem; }
input:focus { outline:none; border-color:var(--text); }
button { font-family:var(--mono); font-size:0.82rem; font-weight:600;
         letter-spacing:0.1em; text-transform:uppercase; padding:0.6rem 1rem;
         background:var(--text); color:var(--bg); border:none; border-radius:3px;
         width:100%; cursor:pointer; }
button:hover { background:#333; }
.error { background:#fdf0ee; border:1px solid #f0b8b2; color:#8b1a1a;
         font-size:0.78rem; padding:0.5rem 0.75rem; border-radius:3px;
         margin-bottom:1rem; font-family:var(--mono); }
</style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="pi-id">{{ pi_id }} · Portal</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label>Password</label>
    <input type="password" name="password" autofocus autocomplete="current-password">
    <button type="submit">SIGN IN →</button>
  </form>
</div>
</body>
</html>"""


def is_authenticated():
    if not PORTAL_PASSWORD:
        return True  # no password set — open access (local network only)
    from flask import session
    return session.get('authenticated') is True

def require_login():
    from flask import redirect
    return redirect('/login')

@app.before_request
def check_auth():
    if request.path in ('/login', '/logout'):
        return  # always allow login page
    if not is_authenticated():
        return require_login()


@app.route('/login', methods=['GET', 'POST'])
def login():
    from flask import session, redirect
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == PORTAL_PASSWORD:
            session['authenticated'] = True
            session.permanent = True
            return redirect('/')
        return render_template_string(LOGIN_HTML, pi_id=PI_ID, error="Incorrect password")
    if is_authenticated():
        return redirect('/')
    return render_template_string(LOGIN_HTML, pi_id=PI_ID, error=None)


@app.route('/logout')
def logout():
    from flask import session, redirect
    session.clear()
    return redirect('/login')


@app.route('/sso')
def sso_login():
    """
    SSO entry point — called by the company node login server after successful auth.
    Validates a short-lived signed token, creates a session, and redirects to dashboard.
    Token is issued by login_server/app.py using the shared SSO_SECRET.
    """
    from flask import session, redirect, request as freq
    from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

    SSO_SECRET   = os.environ.get('SSO_SECRET', '')
    LOGIN_URL    = os.environ.get('LOGIN_SERVER_URL', 'https://portal.synth-cloud.com')
    SSO_TOKEN_TTL = 900  # 15 minutes — must match login_server/app.py

    token = freq.args.get('t', '')

    if not SSO_SECRET:
        log.error('SSO_SECRET not configured on retail node — rejecting SSO attempt')
        return redirect(f'{LOGIN_URL}/login?error=sso_not_configured')

    if not token:
        return redirect(f'{LOGIN_URL}/login?error=missing_token')

    try:
        s = URLSafeTimedSerializer(SSO_SECRET)
        email = s.loads(token, salt='sso-login', max_age=SSO_TOKEN_TTL)
    except SignatureExpired:
        log.warning('SSO token expired')
        return redirect(f'{LOGIN_URL}/login?error=token_expired')
    except BadSignature:
        log.warning('SSO token invalid signature')
        return redirect(f'{LOGIN_URL}/login?error=invalid_token')
    except Exception as e:
        log.error(f'SSO token error: {e}')
        return redirect(f'{LOGIN_URL}/login?error=token_error')

    session['authenticated'] = True
    session.permanent = True
    log.info(f'SSO login: {email}')
    return redirect('/')


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_et():
    return datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')

def kill_switch_active():
    return os.path.exists(KILL_SWITCH_FILE)

def agent_lock_status():
    """Check if an agent currently holds the DB lock."""
    lock_file = os.path.join(PROJECT_DIR, '.agent_lock')
    if not os.path.exists(lock_file):
        return None
    try:
        import time as _t
        parts = open(lock_file).read().strip().split('\n')
        agent = parts[0] if parts else 'unknown'
        lock_time = float(parts[2]) if len(parts) > 2 else 0
        age = int(_t.time() - lock_time)
        if age > 900:  # stale
            return None
        return {'agent': agent, 'age_secs': age}
    except Exception:
        return None


def get_system_status():
    """Read live status from database. Backs off if agent holds lock."""
    import time as _time

    # Check for agent lock — return cached/skeleton if locked
    lock = agent_lock_status()
    if lock:
        log.debug(f"Agent lock held by {lock['agent']} — portal backing off")
        # Return skeleton with lock info — portal shows "agent running" state
        return {
            "portfolio_value": 0,
            "cash":            0,
            "realized_gains":  0,
            "open_positions":  0,
            "positions":       [],
            "urgent_flags":    0,
            "last_heartbeat":  "Agent running",
            "kill_switch":     kill_switch_active(),
            "operating_mode":  OPERATING_MODE,
            "pi_id":           PI_ID,
            "agent_running":   lock['agent'],
            "agent_running_secs": lock['age_secs'],
        }

    for attempt in range(3):
        try:
            sys.path.insert(0, PROJECT_DIR)
            from database import get_db
            db = get_db()
            portfolio  = db.get_portfolio()
            positions  = db.get_open_positions()
            total      = round(portfolio['cash'] + sum(
                p['entry_price'] * p['shares'] for p in positions), 2)
            last_hb    = db.get_last_heartbeat()
            flags      = db.get_urgent_flags()
            return {
                "portfolio_value": total,
                "cash":            round(portfolio['cash'], 2),
                "realized_gains":  round(portfolio.get('realized_gains', 0), 2),
                "open_positions":  len(positions),
                "positions":       positions,
                "urgent_flags":    len(flags),
                "last_heartbeat":  last_hb['timestamp'] if last_hb else "Never",
                "kill_switch":     kill_switch_active(),
                "operating_mode":  OPERATING_MODE,
                "pi_id":           PI_ID,
                "agent_running":   None,
            }
        except Exception as e:
            if 'locked' in str(e).lower() and attempt < 2:
                _time.sleep(1.5)
                continue
            log.error(f"Status read failed: {e}")
            return {
                "error":           str(e),
                "portfolio_value": 0,
                "cash":            0,
                "realized_gains":  0,
                "open_positions":  0,
                "positions":       [],
                "urgent_flags":    0,
                "last_heartbeat":  "Unavailable",
                "kill_switch":     kill_switch_active(),
                "operating_mode":  OPERATING_MODE,
                "pi_id":           PI_ID,
                "agent_running":   None,
            }

def load_pending_approvals():
    """Read approval queue from DB. Returns all rows (portal filters by status in JS)."""
    try:
        from database import get_db
        return get_db().get_pending_approvals()
    except Exception as e:
        log.error(f"load_pending_approvals DB error: {e}")
        return []

def update_env(key, value):
    """Update a single key in .env file."""
    lines = []
    found = False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r') as f:
            lines = f.readlines()
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(ENV_PATH, 'w') as f:
        f.writelines(new_lines)


# ── PORTAL HTML ───────────────────────────────────────────────────────────

PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — {{ pi_id }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0c14;
  --surface:#111520;
  --surface2:#161b28;
  --border:rgba(255,255,255,0.07);
  --border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);
  --muted:rgba(255,255,255,0.35);
  --dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;
  --teal2:rgba(0,245,212,0.12);
  --pink:#ff4b6e;
  --pink2:rgba(255,75,110,0.12);
  --purple:#7b61ff;
  --purple2:rgba(123,97,255,0.12);
  --amber:#ffb347;
  --amber2:rgba(255,179,71,0.12);
  --mono:'JetBrains Mono',monospace;
  --sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;line-height:1.5}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:99px}

/* ── HEADER ── */
.header{
  position:sticky;top:0;z-index:100;
  background:rgba(10,12,20,0.85);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 24px;
  height:56px;
  display:flex;align-items:center;gap:16px;
}
.wordmark{
  font-family:var(--mono);font-size:1rem;font-weight:600;
  letter-spacing:0.15em;color:var(--teal);
  text-shadow:0 0 20px rgba(0,245,212,0.4);
  flex-shrink:0;
}
.header-status{display:flex;align-items:center;gap:8px;flex:1}
.status-pill{
  display:flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:99px;
  font-size:11px;font-weight:600;letter-spacing:0.04em;
  border:1px solid;
}
.sp-ok{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.sp-warn{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.25);color:var(--amber)}
.sp-err{background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.25);color:var(--pink)}
.sp-dim{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--muted)}
.status-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.dot-on{background:var(--teal);box-shadow:0 0 6px var(--teal)}
.dot-warn{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.dot-off{background:var(--pink);box-shadow:0 0 6px var(--pink)}
.dot-dim{background:rgba(255,255,255,0.2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.4}}
.dot-blink{animation:blink 2s infinite}
.header-nav{display:flex;align-items:center;gap:4px;margin-left:auto}
.nav-btn{
  padding:5px 12px;border-radius:8px;font-size:11px;font-weight:600;
  letter-spacing:0.04em;cursor:pointer;
  background:transparent;border:1px solid var(--border);color:var(--muted);
  font-family:var(--sans);transition:all 0.15s;
}
.nav-btn:hover{background:var(--surface2);color:var(--text);border-color:var(--border2)}
.nav-btn.active{background:var(--teal2);border-color:rgba(0,245,212,0.3);color:var(--teal)}
.nav-btn.danger{border-color:rgba(255,75,110,0.3);color:var(--pink)}
.nav-btn.danger:hover{background:var(--pink2)}
.nav-btn.danger.engaged{background:var(--pink2);border-color:rgba(255,75,110,0.5);color:var(--pink)}

/* ── LAYOUT ── */
.page{max-width:1200px;margin:0 auto;padding:20px 24px}
.section{margin-bottom:20px}
.section-title{
  font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:10px;
  display:flex;align-items:center;gap:8px;
}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── GLASS CARD ── */
.glass{
  border-radius:16px;
  border:1px solid var(--border);
  background:var(--surface);
  position:relative;overflow:hidden;
}
.glass::before{
  content:'';position:absolute;top:0;left:20%;right:20%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.08),transparent);
}
.glass.teal-glow{
  border-color:rgba(0,245,212,0.15);
  background:linear-gradient(160deg,rgba(0,245,212,0.05) 0%,var(--surface) 40%);
}
.glass.teal-glow::before{background:linear-gradient(90deg,transparent,rgba(0,245,212,0.3),transparent);box-shadow:0 0 8px rgba(0,245,212,0.2)}
.glass.pink-glow{
  border-color:rgba(255,75,110,0.15);
  background:linear-gradient(160deg,rgba(255,75,110,0.05) 0%,var(--surface) 40%);
}
.glass.pink-glow::before{background:linear-gradient(90deg,transparent,rgba(255,75,110,0.3),transparent);box-shadow:0 0 8px rgba(255,75,110,0.2)}
.glass.purple-glow{
  border-color:rgba(123,97,255,0.15);
  background:linear-gradient(160deg,rgba(123,97,255,0.05) 0%,var(--surface) 40%);
}
.glass.purple-glow::before{background:linear-gradient(90deg,transparent,rgba(123,97,255,0.3),transparent);box-shadow:0 0 8px rgba(123,97,255,0.2)}
.glass.amber-glow{
  border-color:rgba(255,179,71,0.15);
  background:linear-gradient(160deg,rgba(255,179,71,0.05) 0%,var(--surface) 40%);
}
.glass.amber-glow::before{background:linear-gradient(90deg,transparent,rgba(255,179,71,0.3),transparent);box-shadow:0 0 8px rgba(255,179,71,0.2)}

/* ── STAT CARDS ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.stat-card{
  padding:14px 16px;border-radius:14px;
  border:1px solid var(--border);
  background:var(--surface);
  position:relative;overflow:hidden;
}
.stat-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.stat-val{font-size:22px;font-weight:700;letter-spacing:-0.5px;color:var(--text)}
.stat-sub{font-size:11px;color:var(--muted);margin-top:3px}
.stat-card.teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.3)}
.stat-card.pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.3)}
.stat-card.amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(255,179,71,0.3)}
.stat-card.purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.3)}
.stat-card::after{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0;
}
.stat-card.teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.stat-card.pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.stat-card.amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.stat-card.purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}

/* ── KILL SWITCH ── */
.kill-bar{
  border-radius:14px;padding:14px 18px;
  display:flex;align-items:center;gap:14px;
  border:1px solid rgba(255,75,110,0.2);
  background:linear-gradient(135deg,rgba(255,75,110,0.06) 0%,var(--surface) 60%);
  margin-bottom:16px;
}
.kill-bar.clear{
  border-color:rgba(0,245,212,0.15);
  background:linear-gradient(135deg,rgba(0,245,212,0.04) 0%,var(--surface) 60%);
}
.kill-label{font-size:12px;font-weight:700;letter-spacing:0.04em}
.kill-desc{font-size:11px;color:var(--muted);margin-top:1px}
.kill-btn{
  margin-left:auto;padding:8px 20px;border-radius:10px;
  font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  cursor:pointer;font-family:var(--sans);transition:all 0.18s;
  border:1px solid rgba(255,75,110,0.4);
  background:rgba(255,75,110,0.1);color:var(--pink);
}
.kill-btn:hover{background:rgba(255,75,110,0.2);box-shadow:0 0 16px rgba(255,75,110,0.2)}
.kill-btn.resume{border-color:rgba(0,245,212,0.4);background:rgba(0,245,212,0.1);color:var(--teal)}
.kill-btn.resume:hover{background:rgba(0,245,212,0.2);box-shadow:0 0 16px rgba(0,245,212,0.2)}

/* ── MODE BANNER ── */
.mode-banner{
  border-radius:14px;padding:12px 16px;
  display:flex;align-items:center;gap:12px;
  border:1px solid var(--border);
  background:var(--surface);
  margin-bottom:16px;
}
.mode-icon{font-size:20px;flex-shrink:0}
.mode-title{font-size:12px;font-weight:700;letter-spacing:0.04em}
.mode-desc{font-size:11px;color:var(--muted);margin-top:1px}
.mode-badge{
  margin-left:auto;padding:4px 12px;border-radius:99px;
  font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  border:1px solid;flex-shrink:0;
}
.mb-supervised{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.mb-autonomous{background:rgba(255,179,71,0.08);border-color:rgba(255,179,71,0.25);color:var(--amber)}

/* ── GRAPH ── */
.graph-card{padding:18px 20px}
.graph-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.graph-title{font-size:13px;font-weight:600;color:var(--text)}
.graph-tabs{display:flex;gap:4px}
.graph-tab{
  padding:4px 10px;border-radius:7px;font-size:10px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;border:1px solid var(--border);
  background:transparent;color:var(--muted);font-family:var(--sans);transition:all 0.15s;
}
.graph-tab.active{background:var(--teal2);border-color:rgba(0,245,212,0.3);color:var(--teal)}
.graph-wrap{height:140px;position:relative}

/* ── APPROVAL QUEUE ── */
.approval-card{padding:16px 18px}
.trade-item{
  border-radius:12px;border:1px solid var(--border);
  background:var(--surface2);
  padding:12px 14px;margin-bottom:10px;
}
.trade-item:last-child{margin-bottom:0}
.trade-header{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.trade-ticker-icon{
  width:38px;height:38px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;letter-spacing:-0.2px;
  background:linear-gradient(135deg,rgba(123,97,255,0.3),rgba(123,97,255,0.1));
  border:1px solid rgba(123,97,255,0.25);color:#a78bfa;
  position:relative;overflow:hidden;
}
.trade-ticker-icon::after{
  content:'';position:absolute;inset:0;
  background:linear-gradient(145deg,rgba(255,255,255,0.15) 0%,transparent 60%);
}
.trade-meta{flex:1}
.trade-headline{font-size:12px;font-weight:600;color:var(--text)}
.trade-sub{font-size:10px;color:var(--muted);margin-top:2px}
.conf-chip{
  padding:3px 8px;border-radius:99px;font-size:9px;font-weight:700;
  letter-spacing:0.06em;text-transform:uppercase;border:1px solid;flex-shrink:0;
}
.conf-high{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.3);color:var(--teal)}
.conf-med{background:rgba(255,179,71,0.1);border-color:rgba(255,179,71,0.3);color:var(--amber)}
.conf-low{background:rgba(255,255,255,0.05);border-color:var(--border);color:var(--muted)}
.trade-reasoning{
  font-size:11px;color:var(--muted);line-height:1.55;
  padding:8px 10px;border-radius:8px;
  background:rgba(255,255,255,0.03);border:1px solid var(--border);
  margin-bottom:10px;
}
.trade-actions{display:flex;gap:8px}
.btn-approve{
  flex:1;padding:8px;border-radius:9px;font-size:11px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;font-family:var(--sans);
  background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.3);color:var(--teal);
  transition:all 0.15s;
}
.btn-approve:hover{background:rgba(0,245,212,0.2);box-shadow:0 0 12px rgba(0,245,212,0.15)}
.btn-reject{
  flex:1;padding:8px;border-radius:9px;font-size:11px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;font-family:var(--sans);
  background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.25);color:var(--pink);
  transition:all 0.15s;
}
.btn-reject:hover{background:rgba(255,75,110,0.18)}
.empty-state{
  text-align:center;padding:28px 0;
  font-size:12px;color:var(--muted);
}
.empty-icon{font-size:24px;margin-bottom:8px}

/* ── POSITIONS ── */
.position-item{
  display:flex;align-items:center;gap:12px;
  padding:12px 0;border-bottom:1px solid var(--border);
}
.position-item:last-child{border-bottom:none}
.pos-icon{
  width:36px;height:36px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:800;letter-spacing:-0.2px;
  flex-shrink:0;position:relative;overflow:hidden;
}
.pos-icon::after{content:'';position:absolute;inset:0;background:linear-gradient(145deg,rgba(255,255,255,0.18) 0%,transparent 55%)}
.pos-info{flex:1}
.pos-ticker{font-size:13px;font-weight:700;color:var(--text)}
.pos-shares{font-size:10px;color:var(--muted);margin-top:1px}
.pos-pnl{text-align:right}
.pos-pnl-val{font-size:14px;font-weight:700}
.pos-pnl-pct{font-size:10px;color:var(--muted);margin-top:1px}
.pnl-pos{color:var(--teal);text-shadow:0 0 12px rgba(0,245,212,0.3)}
.pnl-neg{color:var(--pink);text-shadow:0 0 12px rgba(255,75,110,0.3)}

/* ── WATCHLIST ── */
.watch-item{
  padding:10px 0;border-bottom:1px solid var(--border);
  display:flex;align-items:flex-start;gap:10px;
}
.watch-item:last-child{border-bottom:none}
.watch-conf{
  width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:5px;
}
.wc-high{background:var(--teal);box-shadow:0 0 6px var(--teal)}
.wc-med{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.wc-low{background:var(--muted)}
.watch-ticker{font-size:12px;font-weight:700;color:var(--text);width:44px;flex-shrink:0}
.watch-headline{font-size:11px;color:var(--muted);line-height:1.45;flex:1}
.watch-meta{font-size:9px;color:var(--dim);margin-top:2px;font-family:var(--mono)}

/* ── INTELLIGENCE GRID ── */
.intel-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:14px;
}
.charm{
  border-radius:18px;overflow:hidden;cursor:pointer;position:relative;
  transition:transform 0.2s,box-shadow 0.2s;
  border:1px solid rgba(255,255,255,0.08);
}
.charm:hover{transform:translateY(-4px) scale(1.01)}
.charm.bull{
  background:linear-gradient(160deg,rgba(0,245,212,0.1) 0%,rgba(17,21,32,0.95) 45%);
  box-shadow:0 0 0 1px rgba(0,245,212,0.12),0 6px 24px rgba(0,245,212,0.06);
}
.charm.bull:hover{box-shadow:0 0 0 1px rgba(0,245,212,0.3),0 12px 40px rgba(0,245,212,0.12)}
.charm.bear{
  background:linear-gradient(160deg,rgba(255,75,110,0.1) 0%,rgba(17,21,32,0.95) 45%);
  box-shadow:0 0 0 1px rgba(255,75,110,0.12),0 6px 24px rgba(255,75,110,0.06);
}
.charm.bear:hover{box-shadow:0 0 0 1px rgba(255,75,110,0.3),0 12px 40px rgba(255,75,110,0.12)}
.charm.neut{
  background:linear-gradient(160deg,rgba(123,97,255,0.1) 0%,rgba(17,21,32,0.95) 45%);
  box-shadow:0 0 0 1px rgba(123,97,255,0.12),0 6px 24px rgba(123,97,255,0.06);
}
.charm.neut:hover{box-shadow:0 0 0 1px rgba(123,97,255,0.3),0 12px 40px rgba(123,97,255,0.12)}
.charm.bull::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,245,212,0.6),transparent);box-shadow:0 0 8px rgba(0,245,212,0.4)}
.charm.bear::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(255,75,110,0.6),transparent);box-shadow:0 0 8px rgba(255,75,110,0.4)}
.charm.neut::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(123,97,255,0.6),transparent);box-shadow:0 0 8px rgba(123,97,255,0.4)}
.charm-top{padding:12px 12px 0;display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:9px}
.stock-icon{
  width:42px;height:42px;border-radius:11px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  font-size:10px;font-weight:800;letter-spacing:-0.2px;
  position:relative;overflow:hidden;
}
.stock-icon::after{content:'';position:absolute;inset:0;background:linear-gradient(145deg,rgba(255,255,255,0.22) 0%,transparent 55%)}
.sent-badge{
  display:flex;align-items:center;gap:3px;
  padding:3px 8px;border-radius:99px;font-size:9px;font-weight:700;
  letter-spacing:0.04em;border:1px solid;
}
.sb-bull{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.25);color:var(--teal)}
.sb-bear{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.25);color:var(--pink)}
.sb-neut{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.25);color:#a78bfa}
.charm-body{padding:0 12px 12px}
.charm-source{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:rgba(255,255,255,0.25);margin-bottom:5px}
.charm-headline{font-size:12.5px;font-weight:600;color:rgba(255,255,255,0.88);line-height:1.38;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;letter-spacing:-0.1px}
.charm-snippet{font-size:11px;color:rgba(255,255,255,0.35);line-height:1.55;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:10px}
.opinion-bar{
  background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.07);
  border-radius:9px;padding:7px 9px;display:flex;flex-direction:column;gap:5px;
}
.op-row{display:flex;align-items:center;gap:6px}
.op-label{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;width:50px;flex-shrink:0}
.ol-agent{color:var(--purple)}
.ol-market{color:rgba(255,255,255,0.25)}
.op-track{flex:1;height:3px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;position:relative}
.op-fill{height:100%;border-radius:99px;position:absolute;left:0}
.of-ab{background:linear-gradient(90deg,var(--purple),var(--teal))}
.of-ar{background:linear-gradient(90deg,var(--purple),var(--pink))}
.of-an{background:linear-gradient(90deg,var(--purple),#a78bfa)}
.of-mb{background:linear-gradient(90deg,rgba(255,255,255,0.15),var(--teal))}
.of-mr{background:linear-gradient(90deg,rgba(255,255,255,0.15),var(--pink))}
.of-mn{background:linear-gradient(90deg,rgba(255,255,255,0.15),#a78bfa)}
.op-val{font-size:9px;font-weight:800;width:22px;text-align:right;flex-shrink:0}
.oval-b{color:var(--teal)}
.oval-r{color:var(--pink)}
.oval-n{color:#a78bfa}
.alert-strip{
  background:linear-gradient(90deg,rgba(255,75,110,0.12),rgba(255,75,110,0.04));
  border-top:1px solid rgba(255,75,110,0.15);
  padding:5px 12px;display:flex;align-items:center;gap:5px;
  font-size:9px;font-weight:700;color:var(--pink);letter-spacing:0.05em;text-transform:uppercase;
}
.alert-dot{width:4px;height:4px;border-radius:50%;background:var(--pink);box-shadow:0 0 5px var(--pink);animation:blink 1.5s infinite}

/* ── SETTINGS ── */
.settings-section{padding:16px 18px}
.setting-row{
  display:flex;align-items:center;gap:12px;
  padding:10px 0;border-bottom:1px solid var(--border);
}
.setting-row:last-child{border-bottom:none}
.setting-label{font-size:12px;font-weight:500;color:var(--text);flex:1}
.setting-desc{font-size:10px;color:var(--muted);margin-top:2px}
.glass-select{
  padding:6px 10px;border-radius:8px;font-size:11px;
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--sans);
  cursor:pointer;outline:none;
}
.glass-select:focus{border-color:rgba(0,245,212,0.4)}
.glass-input{
  padding:6px 10px;border-radius:8px;font-size:11px;width:100px;
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--sans);outline:none;
}
.glass-input:focus{border-color:rgba(0,245,212,0.4)}
.save-btn{
  padding:8px 20px;border-radius:10px;font-size:11px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;font-family:var(--sans);
  background:var(--teal2);border:1px solid rgba(0,245,212,0.3);color:var(--teal);
  transition:all 0.15s;
}
.save-btn:hover{background:rgba(0,245,212,0.2);box-shadow:0 0 12px rgba(0,245,212,0.15)}

/* ── UNLOCK FORM ── */
.unlock-wrap{padding:16px 18px}
.unlock-input{
  width:100%;padding:10px 14px;border-radius:10px;font-size:13px;
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--mono);outline:none;
  margin:10px 0;
}
.unlock-input:focus{border-color:rgba(0,245,212,0.4)}
.unlock-note{font-size:11px;color:var(--muted);line-height:1.6}

/* ── TOAST ── */
.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
  padding:10px 20px;border-radius:12px;font-size:12px;font-weight:600;
  background:var(--surface);border:1px solid var(--border2);color:var(--text);
  z-index:1000;transition:transform 0.3s;pointer-events:none;
  box-shadow:0 8px 32px rgba(0,0,0,0.4);
}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:rgba(0,245,212,0.4);color:var(--teal)}
.toast.err{border-color:rgba(255,75,110,0.4);color:var(--pink)}

/* ── RESPONSIVE ── */
@media(max-width:640px){
  .header{padding:0 14px}
  .page{padding:14px}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .intel-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- HEADER -->
<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="header-status" id="header-status">
    <div class="status-pill sp-dim" id="pill-monitor">
      <div class="status-dot dot-dim"></div>Monitor
    </div>
    <div class="status-pill sp-dim" id="pill-claude">
      <div class="status-dot dot-dim"></div>Claude API
    </div>
    <div class="status-pill sp-dim" id="pill-uptime">
      <div class="status-dot dot-dim dot-blink"></div><span id="uptime-val">Loading</span>
    </div>
  </div>
  <div class="header-nav">
    <button class="nav-btn active" onclick="showTab('dashboard')">Dashboard</button>
    <button class="nav-btn" onclick="showTab('intel')">Intelligence</button>
    <button class="nav-btn" onclick="showTab('settings')">Settings</button>
    <button class="nav-btn" onclick="window.location='/logs'">Logs</button>
    <button class="nav-btn" onclick="window.location='/files'">Files</button>
    <button class="nav-btn danger {% if kill_active %}engaged{% endif %}" id="kill-nav-btn"
            onclick="toggleKill()">
      {% if kill_active %}⛔ Halted{% else %}Kill Switch{% endif %}
    </button>
  </div>
</header>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- ══════════════ DASHBOARD TAB ══════════════ -->
<div class="page" id="tab-dashboard">

  <!-- AGENT RUNNING BANNER -->
  <div id="agent-running-banner" style="display:none;align-items:center;gap:8px;
    padding:10px 16px;border-radius:12px;margin-bottom:12px;
    background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.2)"></div>

  <!-- KILL SWITCH BAR -->
  <div class="kill-bar {% if not kill_active %}clear{% endif %}" id="kill-bar">
    <div>
      <div class="kill-label">
        {% if kill_active %}⛔ System Halted{% else %}● System Running{% endif %}
      </div>
      <div class="kill-desc" id="kill-desc">
        {% if kill_active %}All agents suspended. No new trades will execute.{% else %}Agents active · Supervised mode · No new entries without approval{% endif %}
      </div>
    </div>
    <button class="kill-btn {% if kill_active %}resume{% endif %}" id="kill-btn" onclick="toggleKill()">
      {% if kill_active %}Resume System{% else %}Halt All Agents{% endif %}
    </button>
  </div>

  <!-- STATS GRID -->
  <div class="stats-grid">
    <div class="stat-card teal">
      <div class="stat-label">Portfolio</div>
      <div class="stat-val" id="stat-portfolio">$0.00</div>
      <div class="stat-sub" id="stat-gains-sub">Loading...</div>
    </div>
    <div class="stat-card purple">
      <div class="stat-label">Cash</div>
      <div class="stat-val" id="stat-cash">$0.00</div>
      <div class="stat-sub">Available</div>
    </div>
    <div class="stat-card" id="stat-positions-card">
      <div class="stat-label">Positions</div>
      <div class="stat-val" id="stat-positions">0</div>
      <div class="stat-sub">Open</div>
    </div>
    <div class="stat-card" id="stat-flags-card">
      <div class="stat-label">Flags</div>
      <div class="stat-val" id="stat-flags">0</div>
      <div class="stat-sub">Urgent</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Heartbeat</div>
      <div class="stat-val" style="font-size:13px;letter-spacing:0" id="stat-heartbeat">—</div>
      <div class="stat-sub">Last ping</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Mode</div>
      <div class="stat-val" style="font-size:14px" id="stat-mode">{{ status.operating_mode }}</div>
      <div class="stat-sub">{{ 'Paper' if status.get('trading_mode','PAPER') == 'PAPER' else 'Live' }} trading</div>
    </div>
  </div>

  <!-- TWO COLUMN: GRAPH + POSITIONS -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px">

    <!-- PORTFOLIO GRAPH -->
    <div class="glass teal-glow graph-card">
      <div class="graph-header">
        <div class="graph-title">Portfolio Value</div>
        <div class="graph-tabs">
          <button class="graph-tab active" onclick="loadGraph(30,this)">30D</button>
          <button class="graph-tab" onclick="loadGraph(0,this)">All</button>
        </div>
      </div>
      <div class="graph-wrap">
        <canvas id="portfolio-chart"></canvas>
      </div>
    </div>

    <!-- OPEN POSITIONS -->
    <div class="glass">
      <div style="padding:14px 16px 8px;display:flex;align-items:center;justify-content:space-between">
        <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Open Positions</div>
        <div style="font-size:10px;color:var(--muted)" id="positions-count">Loading</div>
      </div>
      <div style="padding:0 16px 14px" id="positions-list">
        <div class="empty-state"><div class="empty-icon">📊</div>Loading positions...</div>
      </div>
    </div>

  </div>

  <!-- MODE BANNER -->
  <div class="mode-banner" id="mode-banner">
    {% if status.operating_mode == 'SUPERVISED' %}
    <div class="mode-icon">🎯</div>
    <div>
      <div class="mode-title">Supervised Mode</div>
      <div class="mode-desc">Claude proposes trades — you approve each one before execution</div>
    </div>
    <div class="mode-badge mb-supervised">Default</div>
    {% else %}
    <div class="mode-icon">⚡</div>
    <div>
      <div class="mode-title">Autonomous Mode</div>
      <div class="mode-desc">Trades execute automatically per pre-authorized rules</div>
    </div>
    <div class="mode-badge mb-autonomous">Active</div>
    {% endif %}
  </div>

  <!-- TWO COLUMN: APPROVALS + WATCHLIST -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px">

    <!-- APPROVAL QUEUE -->
    <div class="glass purple-glow">
      <div style="padding:14px 16px 10px;display:flex;align-items:center;gap:8px">
        <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Approval Queue</div>
        <div style="padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;background:var(--purple2);border:1px solid rgba(123,97,255,0.3);color:var(--purple)" id="pending-badge">0 pending</div>
      </div>
      <div style="padding:0 16px 14px" id="approval-list">
        <div class="empty-state"><div class="empty-icon">✅</div>No pending approvals</div>
      </div>
    </div>

    <!-- WHAT CLAUDE IS WATCHING -->
    <div class="glass amber-glow">
      <div style="padding:14px 16px 10px;display:flex;align-items:center;gap:8px">
        <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Claude Watching</div>
        <div style="font-size:10px;color:var(--muted)" id="watch-count"></div>
      </div>
      <div style="padding:0 16px 14px" id="watch-list">
        <div class="empty-state"><div class="empty-icon">👁</div>Loading watchlist...</div>
      </div>
    </div>

  </div>

  <!-- AUDIT PANEL -->
  <div class="glass" id="audit-panel" style="margin-bottom:16px">
    <div style="padding:14px 16px 10px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">System Audit</div>
      <div id="audit-score-badge" style="padding:2px 10px;border-radius:99px;font-size:10px;font-weight:700;background:rgba(255,255,255,0.05);border:1px solid var(--border);color:var(--muted)">Loading...</div>
      <div id="audit-timestamp" style="font-size:9px;color:var(--dim);margin-left:auto;font-family:var(--mono)"></div>
    </div>
    <div style="padding:10px 16px" id="audit-summary-text" style="font-size:11px;color:var(--muted)"></div>
    <div id="audit-issues-list" style="padding:0 16px 12px"></div>
  </div>

  <!-- AUTONOMOUS UNLOCK (supervised only) -->
  {% if status.operating_mode == 'SUPERVISED' %}
  <div class="glass" style="margin-bottom:16px">
    <div style="padding:14px 16px 0;font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Autonomous Mode</div>
    <div class="unlock-wrap">
      <div class="unlock-note">Requires a live onboarding call with Synthos support. Contact <strong style="color:var(--text)">synthos.signal@gmail.com</strong> to schedule.</div>
      <input type="password" class="unlock-input" id="unlock-key" placeholder="Enter unlock key from onboarding call">
      <button class="save-btn" onclick="submitUnlockKey()">Submit Key</button>
    </div>
  </div>
  {% endif %}

</div>

<!-- ══════════════ INTELLIGENCE TAB ══════════════ -->
<div class="page" id="tab-intel" style="display:none">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
    <div style="font-size:24px;font-weight:700;letter-spacing:-0.5px;color:var(--text)">
      Market <span style="background:linear-gradient(90deg,var(--teal),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Intelligence</span>
    </div>
    <div style="display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:99px;background:rgba(255,255,255,0.04);border:1px solid var(--border);font-size:11px;font-weight:600;color:var(--muted);letter-spacing:0.05em;text-transform:uppercase">
      <div class="status-dot dot-on" style="width:5px;height:5px"></div>
      <span id="intel-count">Loading</span>
    </div>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:16px">Today · Synthos Agent + Public Analyst Consensus</div>
  <div style="display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap" id="intel-filters">
    <button class="graph-tab active" onclick="filterIntel('all',this)">All</button>
    <button class="graph-tab" onclick="filterIntel('high',this)">High signal</button>
    <button class="graph-tab" onclick="filterIntel('alert',this)">Alerts</button>
    <button class="graph-tab" onclick="filterIntel('bull',this)">Bullish</button>
    <button class="graph-tab" onclick="filterIntel('bear',this)">Bearish</button>
  </div>
  <div class="intel-grid" id="intel-grid">
    <div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--muted);font-size:13px">Loading intelligence...</div>
  </div>
</div>

<!-- ══════════════ SETTINGS TAB ══════════════ -->
<div class="page" id="tab-settings" style="display:none">

  <div class="section-title">API Keys</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div style="font-size:11px;color:var(--muted);margin-bottom:12px;line-height:1.6;padding:8px 10px;background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.15);border-radius:8px">
        &#9888; Keys are written directly to <code style="font-size:10px">.env</code> on this Pi. Leave a field blank to keep the existing value.
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Anthropic API Key</div><div class="setting-desc">Used by all three agents for Claude calls</div></div>
        <input class="glass-input" type="password" id="k-anthropic" placeholder="sk-ant-..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Alpaca API Key</div><div class="setting-desc">Paper or live trading account</div></div>
        <input class="glass-input" type="password" id="k-alpaca-key" placeholder="PK..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Alpaca Secret Key</div><div class="setting-desc">Keep this private</div></div>
        <input class="glass-input" type="password" id="k-alpaca-secret" placeholder="Secret..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Alpaca Base URL</div><div class="setting-desc">Paper: paper-api.alpaca.markets</div></div>
        <input class="glass-input" type="text" id="k-alpaca-url" placeholder="https://paper-api.alpaca.markets" style="width:260px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Congress API Key</div><div class="setting-desc">api.congress.gov disclosure feed</div></div>
        <input class="glass-input" type="password" id="k-congress" placeholder="Key..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">SendGrid API Key</div><div class="setting-desc">For email alerts and digests</div></div>
        <input class="glass-input" type="password" id="k-sendgrid" placeholder="SG..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Monitor URL</div><div class="setting-desc">Heartbeat destination</div></div>
        <input class="glass-input" type="text" id="k-monitor-url" placeholder="http://localhost:5000" style="width:220px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Monitor Token</div><div class="setting-desc">Shared secret with monitor server</div></div>
        <input class="glass-input" type="password" id="k-monitor-token" placeholder="Token..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Portal Password</div><div class="setting-desc">Login password for this portal</div></div>
        <input class="glass-input" type="password" id="k-portal-pw" placeholder="New password..." style="width:160px">
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Alert Email</div><div class="setting-desc">Where to send error alerts</div></div>
        <input class="glass-input" type="email" id="k-alert-to" placeholder="you@email.com" style="width:200px">
      </div>
    </div>
    <div style="padding:0 18px 16px;display:flex;align-items:center;gap:10px">
      <button class="save-btn" onclick="saveKeys()">Save Keys</button>
      <span style="font-size:10px;color:var(--muted)">Only filled fields will be updated</span>
    </div>
  </div>

  <div class="section-title">Trading Parameters</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div class="setting-row">
        <div><div class="setting-label">Max Position Size</div><div class="setting-desc">% of tradeable capital per position</div></div>
        <input class="glass-input" type="number" id="s-max-pos" min="1" max="100" value="{{ settings.max_position_pct }}">
        <span style="font-size:11px;color:var(--muted)">%</span>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Max Sector Concentration</div><div class="setting-desc">% in any one sector before penalty</div></div>
        <input class="glass-input" type="number" id="s-max-sector" min="1" max="100" value="{{ settings.max_sector_pct }}">
        <span style="font-size:11px;color:var(--muted)">%</span>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Minimum Confidence</div><div class="setting-desc">Only act on signals at or above this level</div></div>
        <select class="glass-select" id="s-min-conf">
          <option value="HIGH" {% if settings.min_confidence == 'HIGH' %}selected{% endif %}>HIGH only</option>
          <option value="MEDIUM" {% if settings.min_confidence != 'HIGH' and settings.min_confidence != 'LOW' %}selected{% endif %}>MEDIUM and above</option>
          <option value="LOW" {% if settings.min_confidence == 'LOW' %}selected{% endif %}>LOW and above</option>
        </select>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Staleness Cutoff</div><div class="setting-desc">Maximum disclosure age to act on</div></div>
        <select class="glass-select" id="s-staleness">
          <option value="Fresh" {% if settings.max_staleness == 'Fresh' %}selected{% endif %}>Fresh (≤3 days)</option>
          <option value="Aging" {% if settings.max_staleness == 'Aging' %}selected{% endif %}>Aging (≤7 days)</option>
          <option value="Stale" {% if settings.max_staleness == 'Stale' %}selected{% endif %}>Stale (≤14 days)</option>
          <option value="Expired" {% if settings.max_staleness == 'Expired' %}selected{% endif %}>All (up to 45 days)</option>
        </select>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Close Session Mode</div><div class="setting-desc">3:30pm session behavior</div></div>
        <select class="glass-select" id="s-close-mode">
          <option value="conservative" {% if settings.close_session_mode == 'conservative' %}selected{% endif %}>Conservative</option>
          <option value="normal" {% if settings.close_session_mode == 'normal' %}selected{% endif %}>Normal</option>
        </select>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Spousal Filings</div><div class="setting-desc">How to weight spouse/dependent disclosures</div></div>
        <select class="glass-select" id="s-spousal">
          <option value="reduced" {% if settings.spousal_weight == 'reduced' %}selected{% endif %}>Reduced confidence</option>
          <option value="skip" {% if settings.spousal_weight == 'skip' %}selected{% endif %}>Skip spousal trades</option>
          <option value="equal" {% if settings.spousal_weight == 'equal' %}selected{% endif %}>Equal weight</option>
        </select>
      </div>
    </div>
    <div style="padding:0 18px 16px">
      <button class="save-btn" onclick="saveSettings()">Save Settings</button>
    </div>
  </div>

  <div class="section-title">System Update</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div class="setting-row">
        <div>
          <div class="setting-label">Pull Latest from GitHub</div>
          <div class="setting-desc">Downloads the latest files and restarts the portal. Takes ~10 seconds. Page will reload automatically.</div>
        </div>
        <button class="save-btn" id="update-btn" onclick="selfUpdate()" style="white-space:nowrap;flex-shrink:0">
          ↓ Update Now
        </button>
      </div>
      <div id="update-status" style="display:none;font-size:11px;color:var(--teal);padding:8px 0;font-family:var(--mono)"></div>
    </div>
  </div>

  <div class="section-title">RSS Feed Sources</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;line-height:1.6">
        One feed per line: <code style="background:rgba(255,255,255,0.06);padding:1px 5px;border-radius:4px;font-size:10px">Name | URL | Tier</code>
        (tier: 2=wire, 3=press). Leave blank for built-in defaults.
      </div>
      <textarea id="s-rss" rows="5" style="width:100%;background:var(--surface2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--mono);font-size:11px;resize:vertical;outline:none"
        placeholder="Reuters RSS | https://feeds.reuters.com/reuters/politicsNews | 2">{{ rss_display }}</textarea>
      <div style="margin-top:10px">
        <button class="save-btn" onclick="saveFeeds()">Save Feeds</button>
      </div>
    </div>
  </div>

</div>

<script>
// ── STATE ──
const PI_ID   = '{{ pi_id }}';
const IS_KILL = {{ 'true' if kill_active else 'false' }};
let killState = IS_KILL;
let chartInst = null;
let allSignals = [];

// ── TABS ──
function showTab(t) {
  ['dashboard','intel','settings'].forEach(id => {
    document.getElementById('tab-'+id).style.display = id===t ? '' : 'none';
  });
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  if (t === 'intel') loadIntel();
}

// ── TOAST ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── KILL SWITCH ──
async function toggleKill() {
  const engage = !killState;
  const r = await fetch('/api/kill-switch', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({engage})
  });
  const d = await r.json();
  if (d.ok) {
    killState = engage;
    document.getElementById('kill-bar').className = 'kill-bar' + (engage ? '' : ' clear');
    document.getElementById('kill-btn').textContent = engage ? 'Resume System' : 'Halt All Agents';
    document.getElementById('kill-btn').className = 'kill-btn' + (engage ? ' resume' : '');
    document.getElementById('kill-desc').textContent = engage
      ? 'All agents suspended. No new trades will execute.'
      : 'Agents active · Supervised mode · No new entries without approval';
    const navBtn = document.getElementById('kill-nav-btn');
    navBtn.textContent = engage ? '⛔ Halted' : 'Kill Switch';
    navBtn.className = 'nav-btn danger' + (engage ? ' engaged' : '');
    toast(engage ? '⛔ System halted' : '✓ System resumed', engage ? 'err' : 'ok');
  }
}

// ── APPROVAL ──
async function actionTrade(id, status) {
  const r = await fetch('/api/approval', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, status})
  });
  const d = await r.json();
  if (d.ok) {
    toast(status === 'APPROVED' ? '✓ Trade approved' : '✗ Trade rejected', status === 'APPROVED' ? 'ok' : 'err');
    loadLiveStatus();
  }
}

// ── UNLOCK ──
async function submitUnlockKey() {
  const key = document.getElementById('unlock-key').value.trim();
  if (!key) { toast('Enter your unlock key', 'err'); return; }
  const r = await fetch('/api/unlock-autonomous', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key})
  });
  const d = await r.json();
  if (d.ok) toast('✓ Autonomous mode activated — reload to see changes', 'ok');
  else toast('Invalid key — contact synthos.signal@gmail.com', 'err');
}

// ── SETTINGS ──
async function saveKeys() {
  const fields = {
    'ANTHROPIC_API_KEY': document.getElementById('k-anthropic').value,
    'ALPACA_API_KEY':    document.getElementById('k-alpaca-key').value,
    'ALPACA_SECRET_KEY': document.getElementById('k-alpaca-secret').value,
    'ALPACA_BASE_URL':   document.getElementById('k-alpaca-url').value,
    'CONGRESS_API_KEY':  document.getElementById('k-congress').value,
    'SENDGRID_API_KEY':  document.getElementById('k-sendgrid').value,
    'MONITOR_URL':       document.getElementById('k-monitor-url').value,
    'MONITOR_TOKEN':     document.getElementById('k-monitor-token').value,
    'PORTAL_PASSWORD':   document.getElementById('k-portal-pw').value,
    'ALERT_TO':          document.getElementById('k-alert-to').value,
  };
  // Only send fields that have values
  const data = Object.fromEntries(Object.entries(fields).filter(([,v]) => v.trim()));
  if (!Object.keys(data).length) { toast('No keys to save — fill in at least one field', 'err'); return; }
  const r = await fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const d = await r.json();
  if (d.ok) {
    toast('✓ Keys saved: ' + d.updated.join(', '), 'ok');
    // Clear fields after save
    Object.keys(fields).forEach(k => {
      const el = document.querySelector('[id^="k-"]');
    });
    document.querySelectorAll('[id^="k-"]').forEach(el => el.value = '');
  } else {
    toast('Errors: ' + d.errors.join(', '), 'err');
  }
}

async function saveSettings() {
  const data = {
    max_position_pct: document.getElementById('s-max-pos').value,
    max_sector_pct:   document.getElementById('s-max-sector').value,
    min_confidence:   document.getElementById('s-min-conf').value,
    max_staleness:    document.getElementById('s-staleness').value,
    close_session_mode: document.getElementById('s-close-mode').value,
    spousal_weight:   document.getElementById('s-spousal').value,
  };
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const d = await r.json();
  toast(d.ok ? '✓ Settings saved' : 'Save failed: '+d.error, d.ok ? 'ok' : 'err');
}

async function saveFeeds() {
  const raw = document.getElementById('s-rss').value.trim();
  const feeds = raw ? raw.split('\n').filter(Boolean).map(l => {
    const p = l.split('|').map(s => s.trim());
    return [p[0]||'', p[1]||'', parseInt(p[2]||3)];
  }) : [];
  const r = await fetch('/api/feeds', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({feeds})});
  const d = await r.json();
  toast(d.ok ? '✓ Feeds saved' : 'Save failed', d.ok ? 'ok' : 'err');
}

async function selfUpdate() {
  const btn = document.getElementById('update-btn');
  const status = document.getElementById('update-status');
  btn.disabled = true;
  btn.textContent = '↓ Updating...';
  status.style.display = 'block';
  status.textContent = 'Pulling from GitHub...';
  try {
    const r = await fetch('/api/update', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      status.textContent = '✓ ' + d.message;
      toast('✓ Update started — reloading in 8s', 'ok');
      // Reload after portal restarts
      setTimeout(() => location.reload(), 8000);
    } else {
      status.textContent = '✗ Update failed';
      toast('Update failed', 'err');
      btn.disabled = false;
      btn.textContent = '↓ Update Now';
    }
  } catch(e) {
    // Portal restarted — that's expected, just reload
    status.textContent = '✓ Restarting...';
    setTimeout(() => location.reload(), 6000);
  }
}

// ── GRAPH ──
async function loadGraph(days, btn) {
  if (btn) {
    document.querySelectorAll('.graph-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  try {
    const r = await fetch('/api/portfolio-history?days='+(days||365));
    const d = await r.json();
    const hist = d.history || [];
    const labels = hist.map(p => p.date.slice(5));
    const values = hist.map(p => p.value);
    const ctx = document.getElementById('portfolio-chart').getContext('2d');
    if (chartInst) chartInst.destroy();
    const grad = ctx.createLinearGradient(0,0,0,140);
    grad.addColorStop(0, 'rgba(0,245,212,0.25)');
    grad.addColorStop(1, 'rgba(0,245,212,0)');
    chartInst = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: '#00f5d4',
          borderWidth: 2,
          fill: true,
          backgroundColor: grad,
          tension: 0.4,
          pointRadius: 0,
          pointHitRadius: 8,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: {duration: 600, easing:'easeInOutQuart'},
        plugins: {legend:{display:false}, tooltip:{
          backgroundColor:'rgba(17,21,32,0.95)',
          borderColor:'rgba(0,245,212,0.3)',
          borderWidth:1,
          titleColor:'rgba(255,255,255,0.5)',
          bodyColor:'#00f5d4',
          bodyFont:{weight:'bold'},
          callbacks:{label: ctx => '$' + ctx.parsed.y.toFixed(2)}
        }},
        scales: {
          x: {grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:10},maxTicksLimit:6}},
          y: {grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.3)',font:{size:10},callback:v=>'$'+v.toFixed(0)},position:'right'}
        }
      }
    });
  } catch(e) { console.log('Graph error:', e); }
}

// ── STATUS LOAD ──
function renderPositions(positions) {
  const el = document.getElementById('positions-list');
  const ct = document.getElementById('positions-count');
  if (!positions || !positions.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div>No open positions</div>';
    ct.textContent = '0 open';
    return;
  }
  ct.textContent = positions.length + ' open';
  const colors = [
    'background:linear-gradient(135deg,rgba(0,245,212,0.3),rgba(0,245,212,0.1));border:1px solid rgba(0,245,212,0.25);color:#00f5d4',
    'background:linear-gradient(135deg,rgba(123,97,255,0.3),rgba(123,97,255,0.1));border:1px solid rgba(123,97,255,0.25);color:#a78bfa',
    'background:linear-gradient(135deg,rgba(255,179,71,0.3),rgba(255,179,71,0.1));border:1px solid rgba(255,179,71,0.25);color:#ffb347',
    'background:linear-gradient(135deg,rgba(255,75,110,0.3),rgba(255,75,110,0.1));border:1px solid rgba(255,75,110,0.25);color:#ff4b6e',
  ];
  el.innerHTML = positions.map((p,i) => {
    const pnl = (p.pnl || 0);
    const pnlPct = p.entry_price ? ((pnl / (p.entry_price * p.shares)) * 100).toFixed(1) : '0.0';
    const pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pnlSign = pnl >= 0 ? '+' : '';
    const c = colors[i % colors.length];
    return `<div class="position-item">
      <div class="pos-icon" style="${c}">${(p.ticker||'?').slice(0,4)}</div>
      <div class="pos-info">
        <div class="pos-ticker">${p.ticker||'?'}</div>
        <div class="pos-shares">${(p.shares||0).toFixed(2)} shares · $${(p.entry_price||0).toFixed(2)}</div>
      </div>
      <div class="pos-pnl">
        <div class="pos-pnl-val ${pnlCls}">${pnlSign}$${Math.abs(pnl).toFixed(2)}</div>
        <div class="pos-pnl-pct">${pnlSign}${pnlPct}%</div>
      </div>
    </div>`;
  }).join('');
}

function renderApprovals(approvals) {
  const pending = approvals.filter(a => a.status === 'PENDING_APPROVAL');
  const el = document.getElementById('approval-list');
  const badge = document.getElementById('pending-badge');
  badge.textContent = pending.length + ' pending';
  if (!pending.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">✅</div>No pending approvals</div>';
    return;
  }
  el.innerHTML = pending.map(t => {
    const conf = (t.confidence||'').toUpperCase();
    const confCls = conf === 'HIGH' ? 'conf-high' : conf === 'MEDIUM' ? 'conf-med' : 'conf-low';
    const reasoning = t.reasoning ? t.reasoning.slice(0,180) + (t.reasoning.length > 180 ? '...' : '') : '';
    return `<div class="trade-item">
      <div class="trade-header">
        <div class="trade-ticker-icon">${(t.ticker||'?').slice(0,4)}</div>
        <div class="trade-meta">
          <div class="trade-headline">${t.ticker||'?'} · ${t.politician||'Unknown'}</div>
          <div class="trade-sub">$${(t.amount_range||'?')} · ${t.staleness||'?'} · Queued ${(t.queued_at||'').slice(0,10)}</div>
        </div>
        <div class="conf-chip ${confCls}">${conf}</div>
      </div>
      ${reasoning ? `<div class="trade-reasoning">${reasoning}</div>` : ''}
      <div class="trade-actions">
        <button class="btn-approve" onclick="actionTrade(${t.id},'APPROVED')">✓ Approve</button>
        <button class="btn-reject" onclick="actionTrade(${t.id},'REJECTED')">✗ Reject</button>
      </div>
    </div>`;
  }).join('');
}

// ── WATCHLIST ──
async function loadWatchlist() {
  try {
    const r = await fetch('/api/watchlist');
    const d = await r.json();
    const signals = d.signals || [];
    const el = document.getElementById('watch-list');
    const ct = document.getElementById('watch-count');
    ct.textContent = signals.length + ' signals';
    if (!signals.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">👁</div>No signals being watched</div>';
      return;
    }
    el.innerHTML = signals.map(s => {
      const conf = (s.confidence||'').toUpperCase();
      const dotCls = conf === 'HIGH' ? 'wc-high' : conf === 'MEDIUM' ? 'wc-med' : 'wc-low';
      return `<div class="watch-item">
        <div class="watch-conf ${dotCls}"></div>
        <div style="width:44px;flex-shrink:0;font-size:12px;font-weight:700;color:var(--text)">${s.ticker||'?'}</div>
        <div>
          <div class="watch-headline">${s.headline||s.politician||'No headline'}</div>
          <div class="watch-meta">${conf} · ${s.staleness||'?'} · ${(s.created_at||'').slice(0,10)}</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

// ── SYSTEM HEALTH ──
async function loadHealth() {
  try {
    const r = await fetch('/api/system-health');
    const d = await r.json();
    // Monitor pill
    const mp = document.getElementById('pill-monitor');
    if (d.monitor.status === 'online') {
      mp.className = 'status-pill sp-ok';
      mp.innerHTML = '<div class="status-dot dot-on"></div>Monitor';
    } else if (d.monitor.status === 'offline') {
      mp.className = 'status-pill sp-err';
      mp.innerHTML = '<div class="status-dot dot-off"></div>Monitor offline';
    } else {
      mp.className = 'status-pill sp-dim';
      mp.innerHTML = '<div class="status-dot dot-dim"></div>Monitor';
    }
    // Claude pill
    const cp = document.getElementById('pill-claude');
    if (d.claude_api.status === 'ok') {
      cp.className = 'status-pill sp-ok';
      cp.innerHTML = '<div class="status-dot dot-on"></div>Claude API';
    }
    // Uptime
    document.getElementById('uptime-val').textContent = d.uptime || 'N/A';
    document.getElementById('pill-uptime').className = 'status-pill sp-dim';
  } catch(e) {}
}

// ── LIVE STATUS ──
async function loadLiveStatus() {
  try {
    const [sr, ar] = await Promise.all([fetch('/api/status'), fetch('/api/approvals')]);
    if (!sr.ok || !ar.ok) return;
    const s = await sr.json();
    const a = await ar.json();
    // Stats
    const sv = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
    sv('stat-portfolio', '$'+(s.portfolio_value||0).toFixed(2));
    sv('stat-cash', '$'+(s.cash||0).toFixed(2));
    sv('stat-positions', s.open_positions||0);
    sv('stat-flags', s.urgent_flags||0);
    sv('stat-heartbeat', (s.last_heartbeat||'Never').slice(0,16));
    sv('stat-mode', s.operating_mode||'SUPERVISED');
    // Gains sub
    const gains = s.realized_gains||0;
    const gainEl = document.getElementById('stat-gains-sub');
    if (gainEl) {
      gainEl.textContent = (gains>=0?'+':'') + '$'+gains.toFixed(2)+' realized';
      gainEl.style.color = gains>=0 ? 'rgba(0,245,212,0.6)' : 'rgba(255,75,110,0.6)';
    }
    // Agent running banner
    const agentEl = document.getElementById('agent-running-banner');
    if (s.agent_running) {
      const names = {'trade_logic_agent.py':'Trade Logic','news_agent.py':'News',
                     'market_sentiment_agent.py':'Market Sentiment','agent4_audit.py':'Audit Agent'};
      const name = names[s.agent_running] || s.agent_running;
      const mins = Math.floor((s.agent_running_secs||0) / 60);
      const secs = (s.agent_running_secs||0) % 60;
      if (agentEl) {
        agentEl.style.display = 'flex';
        agentEl.innerHTML = '<div class="status-dot dot-on" style="background:var(--amber);box-shadow:0 0 6px var(--amber)"></div>'
          + '<span style="font-size:11px;font-weight:600;color:var(--amber)">'
          + name + ' running</span>'
          + '<span style="font-size:10px;color:var(--muted);margin-left:6px">'
          + (mins > 0 ? mins + 'm ' : '') + secs + 's · portal in read-only mode</span>';
      }
    } else {
      if (agentEl) agentEl.style.display = 'none';
    }
    if (fc) fc.className = 'stat-card ' + ((s.urgent_flags||0) > 0 ? 'pink' : '');
    renderPositions(s.positions||[]);
    renderApprovals(a);
  } catch(e) {}
}

// ── INTELLIGENCE ──
async function loadIntel() {
  try {
    const r = await fetch('/api/watchlist');
    const d = await r.json();
    const signals = d.signals || [];
    allSignals = signals;
    document.getElementById('intel-count').textContent = signals.length + ' signals today';
    renderIntelGrid(signals);
  } catch(e) {}
}

function filterIntel(type, btn) {
  document.querySelectorAll('#intel-filters .graph-tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  let filtered = allSignals;
  if (type === 'high') filtered = allSignals.filter(s=>s.confidence==='HIGH');
  else if (type === 'bull') filtered = allSignals.filter(s=>!s.is_spousal);
  else if (type === 'bear') filtered = allSignals.filter(s=>s.is_spousal);
  renderIntelGrid(filtered);
}

function renderIntelGrid(signals) {
  const grid = document.getElementById('intel-grid');
  if (!signals.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:60px 0;color:var(--muted);font-size:13px"><div style="font-size:32px;margin-bottom:12px">📡</div>No signals yet today.<br>The Daily fetches hourly during market hours.</div>';
    return;
  }
  const colors = ['background:linear-gradient(135deg,rgba(0,245,212,0.3),rgba(0,245,212,0.1));border:1px solid rgba(0,245,212,0.25);color:#00f5d4','background:linear-gradient(135deg,rgba(123,97,255,0.3),rgba(123,97,255,0.1));border:1px solid rgba(123,97,255,0.25);color:#a78bfa','background:linear-gradient(135deg,rgba(255,179,71,0.3),rgba(255,179,71,0.1));border:1px solid rgba(255,179,71,0.25);color:#ffb347','background:linear-gradient(135deg,rgba(255,75,110,0.3),rgba(255,75,110,0.1));border:1px solid rgba(255,75,110,0.25);color:#ff4b6e'];
  const sentiment = s => s.corroborated ? 'bull' : s.confidence === 'LOW' ? 'bear' : 'neut';
  const sentLabel = s => s.corroborated ? '↑ Bullish' : s.confidence === 'LOW' ? '↓ Bearish' : '— Neutral';
  const sentBadge = s => s.corroborated ? 'sb-bull' : s.confidence === 'LOW' ? 'sb-bear' : 'sb-neut';
  const agentScore = s => s.confidence === 'HIGH' ? 85+Math.floor(Math.random()*10) : s.confidence === 'MEDIUM' ? 55+Math.floor(Math.random()*20) : 25+Math.floor(Math.random()*20);
  const marketScore = s => Math.max(20, agentScore(s) - 5 - Math.floor(Math.random()*15));
  const sentClass = s => agentScore(s) > 60 ? 'of-ab' : agentScore(s) < 40 ? 'of-ar' : 'of-an';
  const mSentClass = s => agentScore(s) > 60 ? 'of-mb' : agentScore(s) < 40 ? 'of-mr' : 'of-mn';
  const valClass = s => agentScore(s) > 60 ? 'oval-b' : agentScore(s) < 40 ? 'oval-r' : 'oval-n';
  grid.innerHTML = signals.map((s,i) => {
    const as = agentScore(s); const ms = marketScore(s);
    const sent = sentiment(s);
    const col = colors[i%colors.length];
    return `<div class="charm ${sent}">
      <div class="charm-top">
        <div class="stock-icon" style="${col}">${(s.ticker||'?').slice(0,4)}</div>
        <div class="sent-badge ${sentBadge(s)}">${sentLabel(s)}</div>
      </div>
      <div class="charm-body">
        <div class="charm-source">${s.politician||'Unknown'} · ${(s.disc_date||s.created_at||'').slice(0,10)}</div>
        <div class="charm-headline">${s.headline||s.ticker+' — Congressional disclosure'}</div>
        <div class="charm-snippet">${s.amount_range||'Amount not disclosed'} · ${s.staleness||'Unknown age'} · ${s.sector||'Unknown sector'}</div>
        <div class="opinion-bar">
          <div class="op-row">
            <span class="op-label ol-agent">Synthos</span>
            <div class="op-track"><div class="op-fill ${sentClass(s)}" style="width:${as}%"></div></div>
            <span class="op-val ${valClass(s)}">${as}</span>
          </div>
          <div class="op-row">
            <span class="op-label ol-market">Market</span>
            <div class="op-track"><div class="op-fill ${mSentClass(s)}" style="width:${ms}%"></div></div>
            <span class="op-val ${valClass(s)}">${ms}</span>
          </div>
        </div>
      </div>
      ${s.corroborated ? `<div class="alert-strip"><div class="alert-dot"></div>Corroborated signal</div>` : ''}
    </div>`;
  }).join('');
}

// ── AUDIT PANEL ──
async function loadAudit() {
  try {
    const r = await fetch('/api/audit');
    const d = await r.json();
    const badge = document.getElementById('audit-score-badge');
    const summary = document.getElementById('audit-summary-text');
    const issues = document.getElementById('audit-issues-list');
    const ts = document.getElementById('audit-timestamp');

    if (!badge) return;

    const score = d.health_score;
    if (score === null) {
      badge.textContent = 'No data';
      summary.textContent = d.summary || '';
      return;
    }

    const color = score >= 90 ? 'teal' : score >= 70 ? 'amber' : 'pink';
    const label = score >= 90 ? 'HEALTHY' : score >= 70 ? 'DEGRADED' : score >= 50 ? 'POOR' : 'CRITICAL';
    badge.style.background = `rgba(var(--${color}-rgb,0,245,212),0.08)`;
    badge.style.borderColor = `rgba(var(--${color}-rgb,0,245,212),0.3)`;
    badge.style.color = `var(--${color})`;
    badge.textContent = `${score}/100 — ${label}`;

    summary.style.cssText = 'font-size:11px;color:var(--muted);padding:8px 0';
    summary.textContent = d.summary || '';

    if (ts && d.timestamp) {
      ts.textContent = 'Last run: ' + new Date(d.timestamp).toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'}) + ' ET';
    }

    let html = '';
    (d.critical||[]).forEach(f => {
      html += `<div style="display:flex;gap:6px;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px">
        <span style="color:var(--pink);font-weight:700;flex-shrink:0">✗</span>
        <div><span style="color:var(--text)">[${f.category}]</span> <span style="color:var(--muted)">${f.message}</span>
        ${f.fix ? `<div style="font-size:10px;color:var(--teal);margin-top:2px">→ ${f.fix}</div>` : ''}</div>
      </div>`;
    });
    (d.warnings||[]).forEach(f => {
      html += `<div style="display:flex;gap:6px;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px">
        <span style="color:var(--amber);font-weight:700;flex-shrink:0">⚠</span>
        <div><span style="color:var(--text)">[${f.category}]</span> <span style="color:var(--muted)">${f.message}</span>
        ${f.fix ? `<div style="font-size:10px;color:var(--teal);margin-top:2px">→ ${f.fix}</div>` : ''}</div>
      </div>`;
    });
    if (!d.critical?.length && !d.warnings?.length) {
      html = `<div style="font-size:11px;color:var(--teal);padding:6px 0">✓ All ${d.info_count||0} checks passed</div>`;
    }
    issues.innerHTML = html;
  } catch(e) {}
}

// ── INIT ──
updateClock();
setInterval(updateClock, 1000);

function updateClock() {
  const now = new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false});
  // Could add a clock element if needed
}

loadLiveStatus();
loadGraph(30);
loadWatchlist();
loadHealth();
loadAudit();
setInterval(loadLiveStatus, 30000);
setInterval(loadHealth, 60000);
setInterval(loadWatchlist, 120000);
setInterval(loadAudit, 300000);
</script>
</body>
</html>"""

# ── ROUTES ────────────────────────────────────────────────────────────────

def get_current_settings():
    """Read current portal-configurable settings from .env."""
    return {
        'max_position_pct':   int(float(os.environ.get('MAX_POSITION_PCT', '0.10')) * 100),
        'max_sector_pct':     int(float(os.environ.get('MAX_SECTOR_PCT', '25'))),
        'min_confidence':     os.environ.get('MIN_CONFIDENCE', 'MEDIUM'),
        'max_staleness':      os.environ.get('MAX_STALENESS', 'Aging'),
        'close_session_mode': os.environ.get('CLOSE_SESSION_MODE', 'conservative'),
        'spousal_weight':     os.environ.get('SPOUSAL_WEIGHT', 'reduced'),
        'rss_feeds_json':     os.environ.get('RSS_FEEDS_JSON', ''),
    }


@app.route('/')
def index():
    # Serve page shell immediately — JS loads live data async via /api/status
    # This means the page renders in <100ms regardless of DB state
    settings  = get_current_settings()

    # Convert RSS_FEEDS_JSON back to human-readable lines for textarea
    rss_display = ''
    if settings['rss_feeds_json']:
        try:
            import json as _json
            feeds = _json.loads(settings['rss_feeds_json'])
            rss_display = '\n'.join(f"{f[0]} | {f[1]} | {f[2]}" for f in feeds)
        except Exception:
            rss_display = ''

    # Safe skeleton status — JS will overwrite with live data
    skeleton_status = {
        "portfolio_value": 0,
        "cash":            0,
        "realized_gains":  0,
        "open_positions":  0,
        "positions":       [],
        "urgent_flags":    0,
        "last_heartbeat":  "Loading...",
        "kill_switch":     kill_switch_active(),
        "operating_mode":  OPERATING_MODE,
        "pi_id":           PI_ID,
    }

    return render_template_string(
        PORTAL_HTML,
        status=skeleton_status,
        approvals=[],
        pending_count=0,
        kill_active=kill_switch_active(),
        pi_id=PI_ID,
        settings=settings,
        rss_display=rss_display,
        portal_password_set=bool(PORTAL_PASSWORD),
        async_load=True,
    )


@app.route('/api/kill-switch', methods=['POST'])
def api_kill_switch():
    data   = request.get_json(silent=True) or {}
    engage = data.get('engage', True)
    try:
        if engage:
            with open(KILL_SWITCH_FILE, 'w') as f:
                f.write(f"Kill switch engaged at {now_et()}\n")
            log.warning("KILL SWITCH ENGAGED via portal")
            try:
                from database import get_db
                get_db().log_event("KILL_SWITCH_ENGAGED", agent="portal",
                                   details=f"Engaged via web portal at {now_et()}")
            except Exception:
                pass
        else:
            if os.path.exists(KILL_SWITCH_FILE):
                os.remove(KILL_SWITCH_FILE)
            log.info("Kill switch cleared via portal")
            try:
                from database import get_db
                get_db().log_event("KILL_SWITCH_CLEARED", agent="portal",
                                   details=f"Cleared via web portal at {now_et()}")
            except Exception:
                pass
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Kill switch error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/approval', methods=['POST'])
def api_approval():
    data      = request.get_json(silent=True) or {}
    signal_id = data.get('id')
    status    = data.get('status', '').upper()

    if not signal_id:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    if status not in ('APPROVED', 'REJECTED'):
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    try:
        from database import get_db
        db      = get_db()
        updated = db.update_approval_status(
            signal_id    = signal_id,
            status       = status,
            decided_by   = 'portal',
            decision_note= data.get('note'),
        )
    except Exception as e:
        log.error(f"api_approval DB error: {e}")
        return jsonify({"ok": False, "error": "Database error"}), 500

    if not updated:
        return jsonify({"ok": False, "error": "Trade not found or already actioned"}), 404

    try:
        db.log_event(
            f"TRADE_{status}", agent="portal",
            details=f"Signal {signal_id} {status.lower()} via portal at {now_et()}"
        )
    except Exception:
        pass

    log.info(f"Trade {signal_id} {status} via portal")
    return jsonify({"ok": True})


@app.route('/api/unlock-autonomous', methods=['POST'])
def api_unlock_autonomous():
    """
    Validate the autonomous mode unlock key and update .env.
    Key is issued after the live onboarding call (framing 4.2 / C4).
    """
    data = request.get_json(silent=True) or {}
    key  = data.get('key', '').strip()

    if not AUTONOMOUS_UNLOCK_KEY:
        return jsonify({"ok": False, "error": "No unlock key configured on server"}), 400

    if key != AUTONOMOUS_UNLOCK_KEY:
        log.warning(f"Failed autonomous unlock attempt at {now_et()}")
        try:
            from database import get_db
            get_db().log_event("AUTONOMOUS_UNLOCK_FAILED", agent="portal",
                               details=f"Bad key attempt at {now_et()}")
        except Exception:
            pass
        return jsonify({"ok": False}), 403

    # Key matches — update OPERATING_MODE in .env
    update_env('OPERATING_MODE', 'AUTONOMOUS')
    log.info(f"Autonomous mode unlocked via portal at {now_et()}")

    try:
        from database import get_db
        get_db().log_event("AUTONOMOUS_UNLOCKED", agent="portal",
                           details=f"Autonomous mode activated at {now_et()}")
    except Exception:
        pass

    return jsonify({"ok": True})


@app.route('/api/feeds', methods=['GET'])
def api_feeds_get():
    """Return current RSS feed list from env."""
    import json
    rss_json = os.environ.get('RSS_FEEDS_JSON', '')
    if rss_json:
        try:
            feeds = json.loads(rss_json)
            return jsonify({"feeds": feeds})
        except Exception:
            pass
    # Return defaults
    defaults = [
        ["Reuters RSS",          "https://feeds.reuters.com/reuters/politicsNews", 2],
        ["Associated Press RSS", "https://apnews.com/rss",                         2],
        ["Politico RSS",         "https://www.politico.com/rss/politicopicks.xml", 3],
        ["The Hill RSS",         "https://thehill.com/feed",                       3],
        ["Roll Call RSS",        "https://rollcall.com/feed",                      3],
        ["Bloomberg RSS",        "https://feeds.bloomberg.com/politics/news.rss",  3],
    ]
    return jsonify({"feeds": defaults})


@app.route('/api/feeds', methods=['POST'])
def api_feeds_save():
    """Save RSS feed list to .env as JSON."""
    import json
    data  = request.get_json(silent=True) or {}
    feeds = data.get('feeds', [])
    if not isinstance(feeds, list):
        return jsonify({"ok": False, "error": "feeds must be a list"}), 400
    try:
        update_env('RSS_FEEDS_JSON', json.dumps(feeds))
        # Reload env so agent2 picks it up on next run
        os.environ['RSS_FEEDS_JSON'] = json.dumps(feeds)
        log.info(f"RSS feeds updated: {len(feeds)} feeds saved")
        try:
            from database import get_db
            get_db().log_event("RSS_FEEDS_UPDATED", agent="portal",
                               details=f"{len(feeds)} feeds saved")
        except Exception:
            pass
        return jsonify({"ok": True, "count": len(feeds)})
    except Exception as e:
        log.error(f"RSS feed save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/keys', methods=['POST'])
@login_required
def api_keys():
    """Update API keys in .env — writes to disk and reloads env."""
    data = request.get_json(silent=True) or {}

    # Whitelist of keys that can be updated via portal
    ALLOWED_KEYS = {
        'ANTHROPIC_API_KEY',
        'ALPACA_API_KEY',
        'ALPACA_SECRET_KEY',
        'ALPACA_BASE_URL',
        'CONGRESS_API_KEY',
        'SENDGRID_API_KEY',
        'MONITOR_TOKEN',
        'MONITOR_URL',
        'PORTAL_PASSWORD',
        'PI_LABEL',
        'PI_EMAIL',
        'OPERATOR_EMAIL',
        'ALERT_TO',
        'TRADING_MODE',
        'OPERATING_MODE',
    }

    updated = []
    errors  = []

    for key, value in data.items():
        if key not in ALLOWED_KEYS:
            errors.append(f"{key}: not allowed")
            continue
        if not isinstance(value, str):
            errors.append(f"{key}: value must be a string")
            continue
        # Don't write empty values unless it's a URL or label
        if not value.strip() and key not in ('MONITOR_URL', 'PI_LABEL', 'PI_EMAIL'):
            errors.append(f"{key}: value is empty")
            continue
        try:
            update_env(key, value.strip())
            os.environ[key] = value.strip()
            updated.append(key)
        except Exception as e:
            errors.append(f"{key}: {str(e)}")

    if updated:
        log.info(f"Keys updated via portal: {updated}")
        try:
            from database import get_db
            get_db().log_event("KEYS_UPDATED", agent="portal",
                               details=f"Updated: {', '.join(updated)}")
        except Exception:
            pass

    return jsonify({'ok': len(errors) == 0, 'updated': updated, 'errors': errors})
def api_settings():
    """Save advanced settings to .env."""
    data = request.get_json(silent=True) or {}
    mapping = {
        'max_sector_pct':     'MAX_SECTOR_PCT',
        'min_confidence':     'MIN_CONFIDENCE',
        'max_staleness':      'MAX_STALENESS',
        'close_session_mode': 'CLOSE_SESSION_MODE',
        'spousal_weight':     'SPOUSAL_WEIGHT',
        'rss_feeds_json':     'RSS_FEEDS_JSON',
    }
    try:
        # MAX_POSITION_PCT: form sends integer percent (10), agent reads decimal (0.10)
        if 'max_position_pct' in data:
            decimal_val = round(float(data['max_position_pct']) / 100, 4)
            update_env('MAX_POSITION_PCT', str(decimal_val))
        for form_key, env_key in mapping.items():
            if form_key in data:
                update_env(env_key, str(data[form_key]))
        log.info(f"Settings updated: {list(data.keys())}")
        try:
            from database import get_db
            get_db().log_event("SETTINGS_UPDATED", agent="portal",
                               details=str(data))
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Settings save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/status')
def api_status():
    return jsonify(get_system_status())


@app.route('/api/approvals')
def api_approvals():
    return jsonify(load_pending_approvals())


@app.route('/api/portfolio-history')
def api_portfolio_history():
    """Portfolio value over time for the graph."""
    days = int(request.args.get('days', 30))
    try:
        from database import get_db
        data = get_db().get_portfolio_history(days=days)
        # If we have less than 2 points, synthesize from current portfolio
        if len(data) < 2:
            p = get_db().get_portfolio()
            from datetime import datetime, timedelta
            today = datetime.now().strftime('%Y-%m-%d')
            start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            cash = p.get('cash', 100)
            data = [
                {'date': start, 'value': round(p.get('month_start', cash), 2)},
                {'date': today, 'value': round(cash + p.get('realized_gains', 0), 2)},
            ]
        return jsonify({'history': data, 'days': days})
    except Exception as e:
        return jsonify({'history': [], 'days': days, 'error': str(e)})


@app.route('/api/watchlist')
def api_watchlist():
    """Signals Claude is watching but has not acted on yet."""
    try:
        from database import get_db
        signals = get_db().get_watching_signals(limit=10)
        return jsonify({'signals': signals})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})


@app.route('/api/system-health')
def api_system_health():
    """Monitor server connectivity, Claude API status, Pi uptime."""
    health = {
        'pi_id':        PI_ID,
        'uptime':       None,
        'monitor':      {'status': 'unconfigured', 'url': MONITOR_URL},
        'claude_api':   {'status': 'unknown', 'last_call': None},
        'trading_mode': os.environ.get('TRADING_MODE', 'PAPER'),
    }
    # Pi uptime
    try:
        with open('/proc/uptime', 'r') as f:
            secs = float(f.readline().split()[0])
        h, rem = divmod(int(secs), 3600)
        m = rem // 60
        health['uptime'] = f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        health['uptime'] = 'N/A'

    # Monitor server ping — try /api/status first, fallback to /
    monitor_url = MONITOR_URL or 'http://localhost:5000'
    try:
        import requests as _req
        start = __import__('time').time()
        r = _req.get(f"{monitor_url.rstrip('/')}/api/status",
                     timeout=3, headers={'X-Token': MONITOR_TOKEN})
        latency = round((__import__('time').time() - start) * 1000)
        health['monitor'] = {
            'status':  'online' if r.status_code < 400 else 'error',
            'url':      monitor_url,
            'latency':  f"{latency}ms",
            'pi_count': len(r.json()) if r.status_code == 200 else 0,
        }
    except Exception:
        health['monitor'] = {'status': 'offline', 'url': monitor_url}

    # Claude API — check last successful agent run from DB
    try:
        from database import get_db
        db = get_db()
        hb = db.get_last_heartbeat('trade_logic_agent')
        if not hb:
            hb = db.get_last_heartbeat('news_agent')
        if hb:
            health['claude_api'] = {
                'status':    'ok',
                'last_call': hb.get('timestamp', 'Unknown'),
            }
        else:
            # Check if anthropic key is set
            if os.environ.get('ANTHROPIC_API_KEY'):
                health['claude_api'] = {'status': 'configured', 'last_call': 'No runs yet'}
            else:
                health['claude_api'] = {'status': 'unconfigured', 'last_call': None}
    except Exception:
        pass
    return jsonify(health)


LOGS_CSS = '<style>\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:#0a0c14;color:#e0ddd8;font-family:sans-serif;min-height:100vh}\nheader{background:#111520;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;\n       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;\n       border-bottom:1px solid #1e2535}\n.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}\n.nav{display:flex;gap:1rem;align-items:center}\n.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}\n.nav a:hover{color:#aaa}\n.tabs{display:flex;gap:0;border-bottom:1px solid #1e2535;padding:0 2rem;\n      background:#111520;overflow-x:auto;flex-wrap:nowrap}\n.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;\n          background:#111520;border-bottom:1px solid #1e2535}\n.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}\nselect{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1e2535;\n       border-radius:6px;color:#e0ddd8}\n.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;\n         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;\n         min-height:calc(100vh - 160px)}\n.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;\n             padding:0.3rem 0.75rem;border:1px solid #1e2535;\n             border-radius:6px;cursor:pointer;background:transparent;color:#556}\n.refresh-btn:hover{background:#1e2535;color:#e0ddd8}\n</style>'

@app.route('/logs')
def logs_page():
    """Tail log files from the browser."""
    log_files = {
        'trader':    'trader.log',
        'daily':     'daily.log',
        'pulse':     'pulse.log',
        'heartbeat': 'heartbeat.log',
        'boot':      'boot.log',
        'portal':    'portal.log',
        'watchdog':  'watchdog.log',
        'cleanup':   'cleanup.log',
        'audit':     'audit.log',
    }
    selected = request.args.get('file', 'trader')
    lines    = int(request.args.get('lines', 100))
    fname    = log_files.get(selected, 'trader.log')
    fpath    = os.path.join(LOG_DIR, fname)

    content = ''
    if os.path.exists(fpath):
        try:
            with open(fpath, 'r') as f:
                all_lines = f.readlines()
            content = ''.join(all_lines[-lines:])
        except Exception as e:
            content = f'Error reading log: {e}'
    else:
        content = f'Log file not found: {fname}'

    tabs = ''.join(
        f'<a href="/logs?file={k}&lines={lines}" '
        f'style="padding:6px 14px;font-family:var(--mono);font-size:0.72rem;'
        f'letter-spacing:0.08em;text-transform:uppercase;text-decoration:none;'
        f'border-bottom:2px solid {"#1a1612" if k == selected else "transparent"};'
        f'color:{"#1a1612" if k == selected else "#7a7060"}">{k}</a>'
        for k in log_files
    )

    line_opts = ''.join(
        f'<option value="{n}" {"selected" if n == lines else ""}>{n} lines</option>'
        for n in [50, 100, 200, 500]
    )

    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Synthos Logs</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">"""
    html += LOGS_CSS
    log_content_escaped = content.replace('<', '&lt;').replace('>', '&gt;')
    html += f"""
</head>
<body>
<header>
  <div class="wordmark">SYNTHOS LOGS</div>
  <div class="nav">
    <a href="/">&#8592; Portal</a>
    <a href="/logout">Sign out</a>
  </div>
</header>
<div class="tabs">{tabs}</div>
<div class="controls">
  <label>Lines</label>
  <select onchange="window.location='/logs?file={selected}&lines='+this.value">{line_opts}</select>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  <span style="font-size:0.72rem;color:#556;margin-left:auto">
    {fname} &middot; auto-refresh off
  </span>
</div>
<div class="log-box">{log_content_escaped}</div>
<script>window.scrollTo(0, document.body.scrollHeight);</script>
</body></html>"""
    return html


# ── BOOT ──────────────────────────────────────────────────────────────────

@app.route('/api/improvement-backlog')
def api_improvement_backlog():
    """Return the improvement backlog for the audit page."""
    backlog_path = os.path.join(PROJECT_DIR, '.improvement_backlog.json')
    try:
        tasks = json.load(open(backlog_path))
        return jsonify({'tasks': tasks})
    except Exception:
        return jsonify({'tasks': []})
    """Latest audit result from agent4_audit.py."""
    audit_path = os.path.join(PROJECT_DIR, '.audit_latest.json')
    try:
        data = json.load(open(audit_path))
        return jsonify(data)
    except Exception:
        return jsonify({
            'health_score': None,
            'summary': 'No audit run yet — drag agent4_audit.py onto the file manager',
            'critical': [], 'warnings': [], 'info_count': 0,
            'timestamp': None,
        })
def api_update():
    """
    Pull latest from GitHub and restart portal.
    Runs qpull.sh if available, otherwise git pull directly.
    Requires portal password or no-password mode.
    """
    import subprocess, threading

    def do_update():
        try:
            qpull = os.path.join(PROJECT_DIR, 'qpull.sh')
            if os.path.exists(qpull):
                result = subprocess.run(
                    ['bash', qpull, '--no-restart'],
                    capture_output=True, text=True,
                    timeout=60, cwd=PROJECT_DIR,
                )
            else:
                result = subprocess.run(
                    ['git', 'pull'],
                    capture_output=True, text=True,
                    timeout=60, cwd=PROJECT_DIR,
                )
            output = (result.stdout + result.stderr).strip()
            log.info(f"Self-update result: {output[:200]}")
            try:
                from database import get_db
                get_db().log_event("SELF_UPDATE", agent="portal",
                                   details=output[:200])
            except Exception:
                pass
            # Restart portal after short delay
            import time
            time.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f"Self-update failed: {e}")

    # Run in background so we can return response first
    threading.Thread(target=do_update, daemon=True).start()
    return jsonify({"ok": True, "message": "Update started - portal will restart in ~5 seconds"})


# ── FILE MANAGER ──────────────────────────────────────────────────────────

# Files allowed to be uploaded/managed
MANAGED_FILES = {
    # Core agents
    'trade_logic_agent.py', 'news_agent.py', 'market_sentiment_agent.py',
    # Infrastructure
    'database.py', 'heartbeat.py', 'boot_sequence.py', 'watchdog.py',
    'cleanup.py', 'shutdown.py', 'health_check.py', 'portal.py',
    'daily_digest.py', 'digest_agent.py', 'patch.py', 'sync.py',
    'synthos_monitor.py', 'install.py',
    # Scripts
    'qpush.sh', 'qpull.sh', 'portal_cmd.sh', 'console_cmd.sh',
    'setup_tunnel.sh', 'first_run.sh', 'migrate_to_synthos.sh',
    # Docs
    'README.md', 'MIGRATION_GUIDE.md', 'user_guide.html',
    'VERSION_MANIFEST.txt',
}


def update_version_manifest(uploaded_files):
    """
    Auto-update VERSION_MANIFEST.txt when files are uploaded.
    Adds new files to the manifest if not already listed.
    Appends an upload session entry to the version history.
    """
    manifest_path = os.path.join(PROJECT_DIR, 'VERSION_MANIFEST.txt')
    if not os.path.exists(manifest_path):
        return

    try:
        with open(manifest_path, 'r') as f:
            content = f.read()

        ts  = datetime.now().strftime('%Y-%m-%d %H:%M')
        entry = f"\n  Portal upload {ts}:\n"
        for fname in uploaded_files:
            fsize = os.path.getsize(os.path.join(PROJECT_DIR, fname))
            entry += f"    {fname:<35} updated via file manager ({fsize} bytes)\n"

        # Append to version history section
        if '----------------------------------------------------------------\nVERSION HISTORY' in content:
            content = content.replace(
                '----------------------------------------------------------------\nVERSION HISTORY',
                f'----------------------------------------------------------------\nVERSION HISTORY\n{entry}'
            )
        else:
            content += f"\n{entry}"

        with open(manifest_path, 'w') as f:
            f.write(content)

        log.info(f"VERSION_MANIFEST.txt updated: {uploaded_files}")
    except Exception as e:
        log.warning(f"Could not update VERSION_MANIFEST.txt: {e}")


def get_managed_files():
    """
    Dynamic MANAGED_FILES — returns hardcoded set plus any .py/.sh
    files that exist in the project directory.
    This means any file dropped into the file manager is automatically tracked.
    """
    base = set(MANAGED_FILES)
    try:
        for fname in os.listdir(PROJECT_DIR):
            if fname.endswith(('.py', '.sh')) and not fname.startswith('.'):
                base.add(fname)
    except Exception:
        pass
    return base


@app.route('/files')
@login_required
def files_page():
    """File manager — list and upload files."""
    managed = get_managed_files()
    files = []
    for fname in sorted(os.listdir(PROJECT_DIR)):
        fpath = os.path.join(PROJECT_DIR, fname)
        if not os.path.isfile(fpath) or fname.startswith('.'):
            continue
        stat       = os.stat(fpath)
        is_py      = fname.endswith('.py')
        is_sh      = fname.endswith('.sh')
        is_managed = fname in managed
        files.append({
            'name':     fname,
            'size':     stat.st_size,
            'mtime':    stat.st_mtime,
            'managed':  is_managed,
            'type':     'python' if is_py else 'shell' if is_sh else 'other',
        })
    return render_template_string(FILE_MANAGER_HTML,
                                  files=files,
                                  pi_id=PI_ID,
                                  project_dir=PROJECT_DIR)


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_files_upload():
    """
    Upload files directly to the Pi, then sync to GitHub.
    Replaces the Mac -> GitHub -> Pi workflow entirely.
    """
    import threading

    uploaded      = []
    errors        = []
    restart_portal = False

    for file in request.files.getlist('files'):
        fname = file.filename
        if not fname:
            continue

        fname = os.path.basename(fname)
        ext   = os.path.splitext(fname)[1].lower()
        if ext not in ('.py', '.sh', '.md', '.html', '.txt', '.json', '.env'):
            errors.append(f"{fname}: file type not allowed")
            continue

        dest = os.path.join(PROJECT_DIR, fname)
        try:
            file.save(dest)
            if ext == '.sh':
                os.chmod(dest, 0o755)
            uploaded.append(fname)
            log.info(f"File uploaded via portal: {fname} ({os.path.getsize(dest)} bytes)")
            try:
                from database import get_db
                get_db().log_event("FILE_UPLOADED", agent="portal", details=fname)
            except Exception:
                pass
            if fname == 'portal.py':
                restart_portal = True
        except Exception as e:
            errors.append(f"{fname}: {str(e)}")

    # Update version manifest
    if uploaded:
        update_version_manifest(uploaded)

    # Sync to GitHub
    git_result = {'ok': False, 'message': 'No files uploaded'}
    if uploaded:
        git_result = sync_to_github(uploaded)

    if restart_portal and uploaded:
        def delayed_restart():
            import time
            time.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=delayed_restart, daemon=True).start()

    return jsonify({
        'ok':       len(errors) == 0,
        'uploaded': uploaded,
        'errors':   errors,
        'restart':  restart_portal,
        'git':      git_result,
    })


def sync_to_github(files):
    """
    Stage uploaded files and push to GitHub.
    Uses GITHUB_TOKEN from .env if available.
    Returns dict with ok, message.
    """
    import subprocess

    github_token = os.environ.get('GITHUB_TOKEN', '')

    try:
        # Check git is available and we're in a repo
        check = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=5
        )
        if check.returncode != 0:
            return {'ok': False, 'message': 'Not a git repository'}

        # Configure token-based auth if available
        if github_token:
            # Get remote URL and inject token
            remote = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, cwd=PROJECT_DIR, timeout=5
            )
            if remote.returncode == 0:
                url = remote.stdout.strip()
                if 'github.com' in url and 'https://' in url and '@' not in url:
                    authed = url.replace('https://', f'https://{github_token}@')
                    subprocess.run(
                        ['git', 'remote', 'set-url', 'origin', authed],
                        capture_output=True, cwd=PROJECT_DIR, timeout=5
                    )

        # Stage files
        subprocess.run(
            ['git', 'add', '-f'] + files,
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=10
        )

        # Check if anything to commit
        status = subprocess.run(
            ['git', 'diff', '--cached', '--stat'],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=5
        )
        if not status.stdout.strip():
            return {'ok': True, 'message': 'Already up to date on GitHub'}

        # Commit
        msg = f"Portal upload: {', '.join(files)}"
        subprocess.run(
            ['git', 'commit', '-m', msg],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=15
        )

        # Push — short timeout, fail gracefully
        if not github_token:
            return {
                'ok': False,
                'message': 'Saved to Pi only — add GITHUB_TOKEN to .env to enable GitHub sync'
            }

        push = subprocess.run(
            ['git', 'push'],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=20
        )

        if push.returncode == 0:
            log.info(f"GitHub sync OK: {files}")
            return {'ok': True, 'message': f"Pushed to GitHub: {', '.join(files)}"}
        else:
            err = (push.stderr or push.stdout or '').strip()[:200]
            log.warning(f"GitHub push failed: {err}")
            return {'ok': False, 'message': f"Saved to Pi. GitHub push failed: {err[:80]}"}

    except subprocess.TimeoutExpired:
        return {'ok': False, 'message': 'Saved to Pi only — GitHub sync timed out (check GITHUB_TOKEN in .env)'}
    except Exception as e:
        return {'ok': False, 'message': f'Saved to Pi only — git error: {str(e)[:80]}'}


@app.route('/api/files/list')
@login_required
def api_files_list():
    """List all files with metadata."""
    files = {}
    for fname in os.listdir(PROJECT_DIR):
        fpath = os.path.join(PROJECT_DIR, fname)
        if not os.path.isfile(fpath) or fname.startswith('.'):
            continue
        stat = os.stat(fpath)
        files[fname] = {
            'size':    stat.st_size,
            'mtime':   stat.st_mtime,
            'managed': fname in MANAGED_FILES,
        }
    return jsonify(files)


FILE_MANAGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Files</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c14;color:rgba(255,255,255,0.88);font-family:'Inter',sans-serif;font-size:14px;min-height:100vh}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:99px}
.header{position:sticky;top:0;z-index:100;background:rgba(8,11,18,0.9);backdrop-filter:blur(20px);
        border-bottom:1px solid rgba(255,255,255,0.07);padding:0 24px;height:56px;
        display:flex;align-items:center;gap:16px}
.wordmark{font-family:'JetBrains Mono',monospace;font-size:1rem;font-weight:600;
          letter-spacing:0.15em;color:#00f5d4;text-shadow:0 0 20px rgba(0,245,212,0.4)}
.nav a{color:rgba(255,255,255,0.35);font-size:11px;text-decoration:none;margin-left:auto;
       padding:5px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.07)}
.nav a:hover{color:rgba(255,255,255,0.8);background:rgba(255,255,255,0.05)}
.page{max-width:900px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,#00f5d4,#7b61ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:12px;color:rgba(255,255,255,0.35);margin-bottom:24px}

/* DROP ZONE */
.drop-zone{
  border:2px dashed rgba(0,245,212,0.3);border-radius:20px;
  background:rgba(0,245,212,0.03);
  padding:40px 24px;text-align:center;margin-bottom:24px;
  cursor:pointer;transition:all 0.2s;position:relative;
}
.drop-zone.drag-over{
  border-color:rgba(0,245,212,0.8);background:rgba(0,245,212,0.08);
  box-shadow:0 0 30px rgba(0,245,212,0.15);
}
.drop-icon{font-size:36px;margin-bottom:12px}
.drop-title{font-size:15px;font-weight:600;color:rgba(255,255,255,0.8);margin-bottom:6px}
.drop-sub{font-size:12px;color:rgba(255,255,255,0.35);line-height:1.6}
.drop-btn{
  display:inline-block;margin-top:14px;padding:9px 22px;border-radius:10px;
  background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.3);
  color:#00f5d4;font-size:12px;font-weight:600;cursor:pointer;
  font-family:'Inter',sans-serif;transition:all 0.15s;
}
.drop-btn:hover{background:rgba(0,245,212,0.2)}
#file-input{display:none}

/* PROGRESS */
.progress-wrap{display:none;margin-bottom:20px}
.progress-item{
  display:flex;align-items:center;gap:10px;padding:10px 14px;
  border-radius:10px;background:rgba(255,255,255,0.04);
  border:1px solid rgba(255,255,255,0.07);margin-bottom:6px;
}
.prog-name{flex:1;font-size:12px;font-family:'JetBrains Mono',monospace}
.prog-bar{flex:2;height:3px;background:rgba(255,255,255,0.08);border-radius:99px;overflow:hidden}
.prog-fill{height:100%;border-radius:99px;transition:width 0.3s;
           background:linear-gradient(90deg,#7b61ff,#00f5d4)}
.prog-status{font-size:10px;font-weight:700;width:60px;text-align:right}
.ps-ok{color:#00f5d4}
.ps-err{color:#ff4b6e}
.ps-wait{color:rgba(255,255,255,0.35)}

/* RESULT BANNER */
.result-banner{
  border-radius:12px;padding:12px 16px;margin-bottom:20px;
  display:none;font-size:12px;font-weight:600;
}
.rb-ok{background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.25);color:#00f5d4}
.rb-err{background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.25);color:#ff4b6e}

/* FILE TABLE */
.sec-label{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
           color:rgba(255,255,255,0.25);margin-bottom:10px;
           display:flex;align-items:center;gap:8px}
.sec-label::after{content:'';flex:1;height:1px;background:rgba(255,255,255,0.06)}
.file-grid{display:grid;grid-template-columns:1fr auto auto;gap:0;
           border-radius:16px;border:1px solid rgba(255,255,255,0.07);overflow:hidden}
.fh{padding:8px 14px;font-size:9px;font-weight:700;letter-spacing:0.08em;
    text-transform:uppercase;color:rgba(255,255,255,0.2);
    background:rgba(255,255,255,0.03);border-bottom:1px solid rgba(255,255,255,0.06)}
.fr{display:contents}
.fr:hover .fc{background:rgba(255,255,255,0.025)}
.fc{padding:9px 14px;font-size:12px;border-bottom:1px solid rgba(255,255,255,0.04);
    display:flex;align-items:center;gap:8px;transition:background 0.1s}
.fr:last-child .fc{border-bottom:none}
.ftype{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.ft-py{background:#7b61ff;box-shadow:0 0 4px #7b61ff}
.ft-sh{background:#00f5d4;box-shadow:0 0 4px #00f5d4}
.ft-other{background:rgba(255,255,255,0.2)}
.fname{font-family:'JetBrains Mono',monospace;font-size:11px;color:rgba(255,255,255,0.7)}
.fmanaged{font-size:9px;font-weight:700;padding:1px 6px;border-radius:99px;
          background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);
          color:rgba(0,245,212,0.7);margin-left:4px}
.fsize{font-size:10px;color:rgba(255,255,255,0.25);font-family:'JetBrains Mono',monospace;
       padding:9px 14px;border-bottom:1px solid rgba(255,255,255,0.04)}
.fmtime{font-size:10px;color:rgba(255,255,255,0.2);font-family:'JetBrains Mono',monospace;
        padding:9px 14px;border-bottom:1px solid rgba(255,255,255,0.04)}
.restart-note{margin-top:12px;padding:10px 14px;border-radius:10px;font-size:11px;
              background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.2);
              color:rgba(255,179,71,0.8)}
</style>
</head>
<body>
<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="nav">
    <a href="/">&#8592; Portal</a>
  </div>
</header>

<div class="page">
  <div class="title">File <span>Manager</span></div>
  <div class="subtitle">{{ pi_id }} &middot; {{ project_dir }} &middot; Drop files here to update the Pi directly &mdash; no git required</div>

  <!-- DROP ZONE -->
  <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
    <div class="drop-icon">&#8659;</div>
    <div class="drop-title">Drop files here to upload to Pi</div>
    <div class="drop-sub">
      Supports .py .sh .md .html .txt .json files<br>
      Multiple files OK &mdash; portal.py will restart the portal automatically
    </div>
    <button class="drop-btn" onclick="event.stopPropagation();document.getElementById('file-input').click()">
      Choose Files
    </button>
    <input type="file" id="file-input" multiple accept=".py,.sh,.md,.html,.txt,.json">
  </div>

  <!-- PROGRESS -->
  <div class="progress-wrap" id="progress-wrap">
    <div class="sec-label">Uploading</div>
    <div id="progress-list"></div>
  </div>

  <!-- RESULT -->
  <div class="result-banner" id="result-banner"></div>
  <div class="result-banner" id="git-note" style="display:none;margin-top:6px"></div>

  <!-- RESTART NOTE -->
  <div class="restart-note" id="restart-note" style="display:none">
    &#9881; portal.py was updated &mdash; portal is restarting. Page will reload in 8 seconds.
  </div>

  <!-- FILE LIST -->
  <div class="sec-label" style="margin-top:8px">Current files on Pi</div>
  <div class="file-grid">
    <div class="fh">File</div>
    <div class="fh">Size</div>
    <div class="fh">Modified</div>
    {% for f in files %}
    <div class="fr">
      <div class="fc">
        <div class="ftype ft-{{ f.type }}"></div>
        <span class="fname">{{ f.name }}</span>
        {% if f.managed %}<span class="fmanaged">managed</span>{% endif %}
      </div>
      <div class="fsize">{{ (f.size / 1024) | round(1) }}k</div>
      <div class="fmtime" title="{{ f.mtime }}">
        {{ f.mtime | int | timestamp_to_date }}
      </div>
    </div>
    {% endfor %}
  </div>
</div>

<script>
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

// Drag and drop handlers
dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  uploadFiles(Array.from(e.dataTransfer.files));
});
fileInput.addEventListener('change', () => uploadFiles(Array.from(fileInput.files)));

async function uploadFiles(files) {
  if (!files.length) return;

  const wrap  = document.getElementById('progress-wrap');
  const list  = document.getElementById('progress-list');
  const banner = document.getElementById('result-banner');
  const note  = document.getElementById('restart-note');

  wrap.style.display = 'block';
  banner.style.display = 'none';
  note.style.display = 'none';
  list.innerHTML = '';

  // Show progress items
  files.forEach(f => {
    list.innerHTML += '<div class="progress-item" id="prog-' + CSS.escape(f.name) + '">'
      + '<div class="ftype ft-' + (f.name.endsWith('.py')?'py':f.name.endsWith('.sh')?'sh':'other') + '"></div>'
      + '<div class="prog-name">' + f.name + '</div>'
      + '<div class="prog-bar"><div class="prog-fill" id="fill-' + CSS.escape(f.name) + '" style="width:0%"></div></div>'
      + '<div class="prog-status ps-wait" id="stat-' + CSS.escape(f.name) + '">waiting</div>'
    + '</div>';
  });

  // Animate to 70% while uploading
  files.forEach(f => {
    const fill = document.getElementById('fill-' + CSS.escape(f.name));
    const stat = document.getElementById('stat-' + CSS.escape(f.name));
    if (fill) fill.style.width = '70%';
    if (stat) { stat.textContent = 'uploading'; stat.className = 'prog-status ps-wait'; }
  });

  const form = new FormData();
  files.forEach(f => form.append('files', f));

  try {
    const r = await fetch('/api/files/upload', { method: 'POST', body: form });
    const d = await r.json();

    // Update progress items
    files.forEach(f => {
      const fill = document.getElementById('fill-' + CSS.escape(f.name));
      const stat = document.getElementById('stat-' + CSS.escape(f.name));
      const ok   = d.uploaded.includes(f.name);
      if (fill) fill.style.width = '100%';
      if (fill) fill.style.background = ok ? 'linear-gradient(90deg,#7b61ff,#00f5d4)' : '#ff4b6e';
      if (stat) { stat.textContent = ok ? 'done' : 'error'; stat.className = 'prog-status ' + (ok ? 'ps-ok' : 'ps-err'); }
    });

    // Result banner
    banner.style.display = 'block';
    if (d.ok) {
      banner.className = 'result-banner rb-ok';
      banner.textContent = '\\u2713 ' + d.uploaded.length + ' file' + (d.uploaded.length===1?'':'s') + ' uploaded to Pi: ' + d.uploaded.join(', ');
    } else {
      banner.className = 'result-banner rb-err';
      banner.textContent = d.uploaded.length + ' uploaded, ' + d.errors.length + ' failed: ' + d.errors.join('; ');
    }

    // GitHub sync result
    const gitNote = document.getElementById('git-note');
    if (gitNote && d.git) {
      gitNote.style.display = 'block';
      gitNote.className = d.git.ok ? 'result-banner rb-ok' : 'result-banner rb-err';
      gitNote.textContent = (d.git.ok ? '\\u2197 GitHub: ' : '\\u26a0 GitHub: ') + d.git.message;
    }

    // Portal restart
    if (d.restart) {
      note.style.display = 'block';
      setTimeout(() => location.href = '/', 8000);
    }

    // Reset input
    fileInput.value = '';

  } catch(e) {
    banner.style.display = 'block';
    banner.className = 'result-banner rb-err';
    banner.textContent = 'Upload failed: ' + e.message;
    if (e.message.includes('Failed to fetch')) {
      note.style.display = 'block';
      setTimeout(() => location.href = '/', 6000);
    }
  }
}
</script>
</body>
</html>"""


# ── NEWS FEED ──────────────────────────────────────────────────────────────

def get_news_feed_data(limit=100):
    """Fetch recent news feed entries from the database."""
    try:
        from database import get_db
        return get_db().get_news_feed(limit=limit)
    except Exception as e:
        log.warning(f"get_news_feed_data error: {e}")
        return []


NEWS_FEED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Synthos — News Feed</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#faf8f4;--card:#fff;--border:#e8e0d0;--text:#1a1612;--muted:#7a7060;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:0.9rem;min-height:100vh}
  header{display:flex;justify-content:space-between;align-items:center;padding:14px 28px;border-bottom:1px solid var(--border);background:var(--card)}
  .wordmark{font-family:var(--mono);font-size:0.8rem;letter-spacing:0.15em;text-transform:uppercase;font-weight:600}
  .nav a{font-family:var(--mono);font-size:0.72rem;letter-spacing:0.08em;text-decoration:none;color:var(--muted);margin-left:20px}
  .nav a:hover{color:var(--text)}
  .page-title{padding:20px 28px 8px;font-family:var(--mono);font-size:0.85rem;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted)}
  .subtitle{padding:0 28px 16px;font-size:0.78rem;color:var(--muted)}
  .table-wrap{overflow-x:auto;padding:0 28px 40px}
  table{width:100%;border-collapse:collapse;font-size:0.83rem}
  th{font-family:var(--mono);font-size:0.68rem;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);padding:8px 12px;text-align:left;border-bottom:2px solid var(--border);white-space:nowrap}
  td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top}
  tr:hover td{background:#f5f2ec}
  .score-HIGH{color:#1a6b3c;font-weight:600}
  .score-MEDIUM{color:#7a5c00;font-weight:600}
  .score-LOW{color:#7a3020;font-weight:600}
  .score-NOISE{color:var(--muted)}
  .ticker{font-family:var(--mono);font-weight:600}
  .ts{font-family:var(--mono);font-size:0.75rem;color:var(--muted);white-space:nowrap}
  .empty{text-align:center;padding:60px 0;color:var(--muted);font-family:var(--mono);font-size:0.8rem;letter-spacing:0.1em}
  .refresh-note{font-family:var(--mono);font-size:0.7rem;color:var(--muted);text-align:right;padding:0 28px 8px}
</style>
</head>
<body>
<header>
  <div class="wordmark">SYNTHOS NEWS FEED</div>
  <div class="nav">
    <a href="/">&#8592; Portal</a>
    <a href="/news">&#8635; Refresh</a>
    <a href="/logout">Sign out</a>
  </div>
</header>
<div class="page-title">Signal Activity Feed</div>
<div class="subtitle">All signals evaluated by Scout — including WATCH and DISCARD decisions. Auto-refreshes every 60s.</div>
<div class="refresh-note">Showing last {count} entries &middot; last updated {updated}</div>
<div class="table-wrap">
{table_content}
</div>
</body></html>"""


@app.route('/news')
@login_required
def news_feed_page():
    """News feed — all signals evaluated by Scout (QUEUE, WATCH, DISCARD)."""
    rows = get_news_feed_data(limit=100)
    now_str = datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')

    if not rows:
        table_content = '<div class="empty">NO SIGNAL ACTIVITY YET</div>'
    else:
        header = (
            '<table>'
            '<thead><tr>'
            '<th>Timestamp</th>'
            '<th>Member</th>'
            '<th>Ticker</th>'
            '<th>Signal Score</th>'
            '<th>Sentiment</th>'
            '<th>Headline</th>'
            '</tr></thead><tbody>'
        )
        body_rows = []
        for r in rows:
            ts        = (r.get('timestamp') or r.get('created_at') or '')[:16]
            member    = r.get('congress_member') or '—'
            ticker    = r.get('ticker') or '—'
            score     = (r.get('signal_score') or 'NOISE').upper()
            sentiment = r.get('sentiment_score')
            headline  = r.get('raw_headline') or '—'
            sent_str  = f"{sentiment:+.2f}" if sentiment is not None else '—'
            body_rows.append(
                f'<tr>'
                f'<td class="ts">{ts}</td>'
                f'<td>{member}</td>'
                f'<td class="ticker">{ticker}</td>'
                f'<td class="score-{score}">{score}</td>'
                f'<td>{sent_str}</td>'
                f'<td>{headline[:120]}</td>'
                f'</tr>'
            )
        table_content = header + ''.join(body_rows) + '</tbody></table>'

    html = NEWS_FEED_HTML.replace('{count}', str(len(rows)))
    html = html.replace('{updated}', now_str)
    html = html.replace('{table_content}', table_content)
    return html


@app.route('/api/news-feed')
@login_required
def api_news_feed():
    """JSON endpoint — Scout writes here; also readable by company Pi."""
    rows = get_news_feed_data(limit=100)
    return jsonify({'entries': rows, 'count': len(rows)})


# ── START ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info(f"Synthos Portal starting on port {PORT}")
    log.info(f"Pi: {PI_ID} | Mode: {OPERATING_MODE}")
    log.info(f"Kill switch: {'ACTIVE' if kill_switch_active() else 'clear'}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
