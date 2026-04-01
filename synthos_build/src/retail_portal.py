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


def _fetch_alpaca_positions():
    """Fetch live positions from Alpaca. Returns dict keyed by symbol, or {} on failure."""
    import requests as _req
    alpaca_key    = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    alpaca_url    = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
    if not alpaca_key:
        return {}
    try:
        headers = {
            "APCA-API-KEY-ID":     alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }
        r = _req.get(f"{alpaca_url}/v2/positions", headers=headers, timeout=6)
        r.raise_for_status()
        return {p['symbol']: p for p in r.json()}
    except Exception as e:
        log.warning(f"Alpaca position fetch failed: {e}")
        return {}


def _enrich_positions(db_positions, alpaca_pos_map):
    """
    Merge DB positions with live Alpaca data.
    Returns (enriched_list, orphan_list).
      enriched: DB positions with current_price / market_value / pl fields populated
      orphans:  Alpaca positions not in DB (shown as warnings)
    """
    db_tickers = set()
    enriched = []
    for p in db_positions:
        ticker = p.get('ticker', '')
        db_tickers.add(ticker)
        ap = alpaca_pos_map.get(ticker, {})
        entry = float(p.get('entry_price', 0) or 0)
        shares = float(p.get('shares', 0) or 0)
        cost = round(entry * shares, 2)
        if ap:
            cur_price   = float(ap.get('current_price', entry) or entry)
            mkt_value   = float(ap.get('market_value', cost) or cost)
            unreal_pl   = float(ap.get('unrealized_pl', 0) or 0)
            unreal_plpc = float(ap.get('unrealized_plpc', 0) or 0)
            day_pl      = float(ap.get('unrealized_intraday_pl', 0) or 0)
            day_plpc    = float(ap.get('unrealized_intraday_plpc', 0) or 0)
            avg_entry   = float(ap.get('avg_entry_price', entry) or entry)
        else:
            cur_price   = entry
            mkt_value   = cost
            unreal_pl   = 0.0
            unreal_plpc = 0.0
            day_pl      = 0.0
            day_plpc    = 0.0
            avg_entry   = entry
        enriched.append({
            **p,
            'current_price':    round(cur_price, 4),
            'market_value':     round(mkt_value, 2),
            'unrealized_pl':    round(unreal_pl, 2),
            'unrealized_plpc':  round(unreal_plpc * 100, 2),
            'day_pl':           round(day_pl, 2),
            'day_plpc':         round(day_plpc * 100, 2),
            'avg_entry_price':  round(avg_entry, 4),
            'cost_basis':       round(cost, 2),
            'is_orphan':        False,
        })

    orphans = []
    for sym, ap in alpaca_pos_map.items():
        if sym in db_tickers:
            continue
        shares    = float(ap.get('qty', 0) or 0)
        cur_price = float(ap.get('current_price', 0) or 0)
        mkt_value = float(ap.get('market_value', 0) or 0)
        unreal_pl = float(ap.get('unrealized_pl', 0) or 0)
        day_pl    = float(ap.get('unrealized_intraday_pl', 0) or 0)
        day_plpc  = float(ap.get('unrealized_intraday_plpc', 0) or 0)
        avg_entry = float(ap.get('avg_entry_price', cur_price) or cur_price)
        orphans.append({
            'ticker':           sym,
            'company':          sym,
            'shares':           shares,
            'current_price':    round(cur_price, 4),
            'market_value':     round(mkt_value, 2),
            'avg_entry_price':  round(avg_entry, 4),
            'cost_basis':       round(avg_entry * shares, 2),
            'unrealized_pl':    round(unreal_pl, 2),
            'unrealized_plpc':  round(float(ap.get('unrealized_plpc', 0) or 0) * 100, 2),
            'day_pl':           round(day_pl, 2),
            'day_plpc':         round(day_plpc * 100, 2),
            'entry_price':      round(avg_entry, 4),
            'status':           'OPEN',
            'is_orphan':        True,
            'pnl':              round(unreal_pl, 2),
        })
    return enriched, orphans


def _enrich_flags(raw_flags, db_path):
    """
    Enrich raw urgent_flags rows with:
      - human-readable title + description
      - severity label (CRITICAL / WARNING / INFO / LOW)
      - context from scan_log if available
      - clearable flag (non-critical flags can be dismissed)
    """
    import sqlite3 as _sq

    # Build a lookup: for each flag find the scan_log entry closest to (but not after)
    # the flag's detected_at — this captures what actually triggered it.
    # We index all cascade scans by ticker then match per flag below.
    all_scans = {}   # ticker -> list of (scanned_at, event_summary)
    try:
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        rows = conn.execute(
            "SELECT ticker, event_summary, scanned_at FROM scan_log "
            "WHERE cascade_detected=1 ORDER BY scanned_at ASC"
        ).fetchall()
        for r in rows:
            all_scans.setdefault(r['ticker'], []).append(
                {'summary': r['event_summary'], 'at': r['scanned_at']}
            )
        conn.close()
    except Exception:
        pass

    def _best_scan(ticker, detected_at):
        """Return the scan closest in time to when the flag was raised."""
        candidates = all_scans.get(ticker, [])
        if not candidates:
            return {}
        # prefer the scan at or just before detected_at
        before = [s for s in candidates if s['at'] <= detected_at]
        if before:
            return before[-1]   # most recent before flag raised
        return candidates[0]    # fallback: earliest available

    scan_context = {}  # kept for compatibility — populated per-flag below

    TIER_META = {
        1: {'label': 'CRITICAL', 'color': 'critical',
            'clearable': False, 'btn': 'Acknowledge'},
        2: {'label': 'WARNING',  'color': 'warning',
            'clearable': True,  'btn': 'Clear'},
        3: {'label': 'INFO',     'color': 'info',
            'clearable': True,  'btn': 'Dismiss'},
        4: {'label': 'LOW',      'color': 'low',
            'clearable': True,  'btn': 'Dismiss'},
    }

    def _parse_event(summary):
        """Extract key fields from event_summary string."""
        if not summary:
            return {}
        parts = {}
        for seg in summary.split('|'):
            seg = seg.strip()
            if '=' in seg:
                k, _, v = seg.partition('=')
                parts[k.strip().lower()] = v.strip()
            elif ':' in seg:
                k, _, v = seg.partition(':')
                parts[k.strip().lower()] = v.strip()
        return parts

    def _human_title(ticker, tier, parsed):
        classification = parsed.get('classification', '').lower()
        market_state   = parsed.get('market_sentiment', parsed.get('market_state', '')).lower()
        if ticker == 'MARKET':
            if 'panic' in classification or 'panic' in market_state:
                return 'Market Panic Condition'
            if 'strong_bear' in classification or 'strong_bear' in market_state:
                return 'Strong Bearish Market'
            if 'mild_bear' in classification or 'mild_bear' in market_state:
                return 'Mild Bearish Market'
            if 'conflict' in classification:
                return 'Conflicted Market Signal'
            if tier == 1:
                return 'Market Risk Event'
            return 'Market Sentiment Alert'
        else:
            if tier == 1:
                return f'{ticker} — Cascade Alert'
            if tier == 2:
                return f'{ticker} — Elevated Activity'
            return f'{ticker} — Monitoring Flag'

    def _human_desc(ticker, tier, parsed, raw_summary):
        classification = parsed.get('classification', '').lower()
        regime         = parsed.get('regime', '')
        score          = parsed.get('score', '')
        confidence     = parsed.get('confidence', '')
        warning        = parsed.get('warning', 'none')

        if ticker == 'MARKET':
            if 'panic' in classification:
                return (
                    f"A market-wide panic or stress condition was detected (regime: {regime}). "
                    f"Agents are in RISK_OFF mode — no new positions will open until this flag is acknowledged. "
                    f"Sentiment score: {score}, confidence: {confidence}."
                )
            if 'strong_bear' in classification:
                return (
                    f"Strong bearish market conditions detected (regime: {regime}). "
                    f"SPY is in significant drawdown. Position sizing is reduced. "
                    f"Sentiment score: {score}."
                )
            if 'mild_bear' in classification:
                return (
                    f"Mild bearish conditions detected (regime: {regime}). "
                    f"Risk appetite is reduced. Score: {score}, confidence: {confidence}."
                )
            if 'conflict' in classification:
                return (
                    f"Mixed signals — market data is internally conflicted (regime: {regime}). "
                    f"Agents are monitoring but not acting aggressively. "
                    f"Score: {score}, confidence: {confidence}."
                )
            return raw_summary or f"Market flag detected. Tier {tier}. Regime: {regime}."
        else:
            return (
                f"Unusual activity detected for {ticker}. "
                f"Cascade conditions triggered at tier {tier}. "
                + (f"Details: {raw_summary}" if raw_summary else "Review position and recent scan data.")
            )

    enriched = []
    for f in raw_flags:
        ticker      = f.get('ticker', '?')
        tier        = f.get('tier', 3)
        detected_at = f.get('detected_at', '')
        meta        = TIER_META.get(tier, TIER_META[3])
        ctx         = _best_scan(ticker, detected_at)
        raw_s       = ctx.get('summary') or f.get('label') or ''
        parsed      = _parse_event(raw_s)

        enriched.append({
            **f,
            'severity':     meta['label'],
            'color':        meta['color'],
            'clearable':    meta['clearable'],
            'btn_label':    meta['btn'],
            'title':        _human_title(ticker, tier, parsed),
            'description':  _human_desc(ticker, tier, parsed, raw_s),
            'scan_summary': raw_s,
            'scan_at':      ctx.get('at', f.get('detected_at', '')),
        })

    # Sort: critical first, then by detected_at desc
    enriched.sort(key=lambda x: (x['tier'], x['detected_at']), reverse=False)
    return enriched


def get_system_status():
    """Read live status from database, enriched with Alpaca real-time prices."""
    import time as _time

    # Check for agent lock — return cached/skeleton if locked
    lock = agent_lock_status()
    if lock:
        log.debug(f"Agent lock held by {lock['agent']} — portal backing off")
        return {
            "portfolio_value": 0,
            "cash":            0,
            "realized_gains":  0,
            "open_positions":  0,
            "positions":       [],
            "orphan_positions": [],
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
            from retail_database import get_db
            db = get_db()
            portfolio  = db.get_portfolio()
            positions  = db.get_open_positions()
            last_hb    = db.get_last_heartbeat()
            flags      = db.get_urgent_flags()

            # Enrich with Alpaca real-time data
            alpaca_map           = _fetch_alpaca_positions()
            enriched, orphans    = _enrich_positions(positions, alpaca_map)
            all_positions        = enriched + orphans

            # Enrich flags with human-readable context
            enriched_flags = _enrich_flags([dict(f) for f in flags], db.path)
            critical_count = sum(1 for f in enriched_flags if f['tier'] == 1)

            # Portfolio value: cash + sum of current market values
            market_value_total = sum(p['market_value'] for p in all_positions)
            total = round(portfolio['cash'] + market_value_total, 2)

            return {
                "portfolio_value":  total,
                "cash":             round(portfolio['cash'], 2),
                "realized_gains":   round(portfolio.get('realized_gains', 0), 2),
                "open_positions":   len(enriched),
                "orphan_count":     len(orphans),
                "positions":        all_positions,
                "urgent_flags":     len(flags),
                "critical_flags":   critical_count,
                "flags_detail":     enriched_flags,
                "last_heartbeat":   last_hb['timestamp'] if last_hb else "Never",
                "kill_switch":      kill_switch_active(),
                "operating_mode":   OPERATING_MODE,
                "trading_mode":     os.environ.get('TRADING_MODE', 'PAPER'),
                "max_trade_usd":    float(os.environ.get('MAX_TRADE_USD', '0')),
                "pi_id":            PI_ID,
                "agent_running":    None,
            }
        except Exception as e:
            if 'locked' in str(e).lower() and attempt < 2:
                _time.sleep(1.5)
                continue
            log.error(f"Status read failed: {e}")
            return {
                "error":            str(e),
                "portfolio_value":  0,
                "cash":             0,
                "realized_gains":   0,
                "open_positions":   0,
                "orphan_count":     0,
                "positions":        [],
                "urgent_flags":     0,
                "flags_detail":     [],
                "last_heartbeat":   "Unavailable",
                "kill_switch":      kill_switch_active(),
                "operating_mode":   OPERATING_MODE,
                "trading_mode":     os.environ.get('TRADING_MODE', 'PAPER'),
                "max_trade_usd":    float(os.environ.get('MAX_TRADE_USD', '0')),
                "pi_id":            PI_ID,
                "agent_running":    None,
            }

def load_pending_approvals():
    """Read approval queue from DB. Returns all rows (portal filters by status in JS)."""
    try:
        from retail_database import get_db
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

/* ── SIGNAL MODAL ── */
.sig-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:500;display:none;align-items:center;justify-content:center}
.sig-modal-overlay.open{display:flex}
.sig-modal{background:#111520;border:1px solid var(--border2);border-radius:16px;width:min(560px,95vw);max-height:85vh;overflow-y:auto;padding:0}
.sig-modal-head{padding:18px 20px 14px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:12px}
.sig-modal-icon{width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;flex-shrink:0}
.sig-modal-body{padding:16px 20px}
.sig-modal-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:12px}
.sig-modal-row:last-child{border-bottom:none}
.sig-modal-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.06em}
.sig-modal-val{color:var(--text);font-family:var(--mono);font-size:11px}
.sig-modal-close{margin-left:auto;cursor:pointer;color:var(--muted);font-size:18px;line-height:1;padding:2px 6px}
.sig-modal-close:hover{color:var(--text)}

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
    <div class="status-pill sp-dim" id="pill-datafeed">
      <div class="status-dot dot-dim"></div>Data Feed
    </div>
    <div class="status-pill sp-dim" id="pill-uptime">
      <div class="status-dot dot-dim dot-blink"></div><span id="uptime-val">Loading</span>
    </div>
    <div class="status-pill sp-dim" id="pill-memory" title="RAM usage">
      <div class="status-dot dot-dim" id="mem-dot"></div><span id="mem-val">RAM</span>
    </div>
  </div>
  <div class="header-nav">
    <button class="nav-btn active" onclick="showTab('dashboard')">Dashboard</button>
    <button class="nav-btn" onclick="showTab('intel')">Intelligence</button>
    <button class="nav-btn" onclick="showTab('news')">News</button>
    <button class="nav-btn" onclick="showTab('screening')">Screening</button>
    <button class="nav-btn" onclick="showTab('settings')">Settings</button>
    <button class="nav-btn" onclick="window.location='/logs'">Logs</button>
    <button class="nav-btn" onclick="window.location='/files'">Files</button>
    <button class="nav-btn danger {% if kill_active %}engaged{% endif %}" id="kill-nav-btn"
            onclick="toggleKill()">
      {% if kill_active %}⛔ Halted{% else %}Kill Switch{% endif %}
    </button>
    <a href="/logout" class="nav-btn" style="text-decoration:none;font-size:11px">Sign Out</a>
  </div>
</header>

<!-- SIGNAL MODAL -->
<div class="sig-modal-overlay" id="sig-modal-overlay" onclick="closeSigModal(event)">
  <div class="sig-modal" id="sig-modal">
    <div class="sig-modal-head">
      <div class="sig-modal-icon" id="smi-icon"></div>
      <div style="flex:1">
        <div style="font-size:13px;font-weight:700;color:var(--text);line-height:1.4" id="smi-headline"></div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px" id="smi-source"></div>
      </div>
      <span class="sig-modal-close" onclick="closeSigModal()">✕</span>
    </div>
    <div class="sig-modal-body" id="smi-body"></div>
  </div>
</div>

<!-- NEWS ARTICLE MODAL -->
<div class="sig-modal-overlay" id="news-modal-overlay" onclick="closeNewsModal(event)" style="display:none">
  <div class="sig-modal" id="news-modal" style="max-width:640px;width:94vw;max-height:88vh;overflow-y:auto">
    <div class="sig-modal-head" style="align-items:flex-start;gap:10px">
      <div style="flex:1">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px" id="nmi-cat-label"></div>
        <div style="font-size:15px;font-weight:700;color:var(--text);line-height:1.45" id="nmi-headline"></div>
        <div style="font-size:11px;color:var(--muted);margin-top:5px" id="nmi-meta"></div>
      </div>
      <span class="sig-modal-close" onclick="closeNewsModal()" style="flex-shrink:0">✕</span>
    </div>
    <div id="nmi-img-wrap" style="margin:0 -20px;display:none">
      <img id="nmi-img" src="" alt="" style="width:100%;max-height:260px;object-fit:cover;display:block">
    </div>
    <div class="sig-modal-body" id="nmi-body"></div>
    <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border);text-align:center">
      <a id="nmi-link" href="#" target="_blank" rel="noopener"
         style="display:inline-flex;align-items:center;gap:6px;padding:9px 20px;border-radius:8px;background:var(--teal);color:#fff;font-size:12px;font-weight:700;text-decoration:none;letter-spacing:0.03em">
        Read full article ↗
      </a>
    </div>
  </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- ══════════════ DASHBOARD TAB ══════════════ -->
<div class="page" id="tab-dashboard">

  <!-- MARKET INDICES -->
  <div id="market-indices-bar" style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap"></div>

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
      <div class="stat-sub" id="stat-positions-sub">Open</div>
    </div>
    <div class="stat-card" id="stat-flags-card" style="cursor:pointer" onclick="toggleFlagsModal()">
      <div class="stat-label">Flags</div>
      <div class="stat-val" id="stat-flags">0</div>
      <div class="stat-sub" id="stat-flags-sub">click to view</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Heartbeat</div>
      <div class="stat-val" style="font-size:13px;letter-spacing:0" id="stat-heartbeat">—</div>
      <div class="stat-sub">Last ping</div>
    </div>
    <div class="stat-card" id="stat-mode-card" style="{% if status.operating_mode == 'AUTONOMOUS' %}border-color:rgba(255,179,71,0.25){% endif %}">
      <div style="display:flex;align-items:center;gap:6px">
        <div class="stat-label">Mode</div>
        <div id="stat-mode-badge" style="padding:1px 7px;border-radius:99px;font-size:8px;font-weight:700;letter-spacing:0.05em;
          {% if status.operating_mode == 'AUTONOMOUS' %}background:rgba(255,179,71,0.12);border:1px solid rgba(255,179,71,0.3);color:#ffb347
          {% else %}background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.2);color:var(--teal){% endif %}">
          {% if status.operating_mode == 'AUTONOMOUS' %}ACTIVE{% else %}DEFAULT{% endif %}
        </div>
      </div>
      <div class="stat-val" style="font-size:13px;display:flex;align-items:center;gap:5px" id="stat-mode">
        <span>{% if status.operating_mode == 'AUTONOMOUS' %}⚡{% else %}🎯{% endif %}</span>
        <span>{{ status.operating_mode }}</span>
      </div>
      <div class="stat-sub" style="line-height:1.4">
        {{ 'Paper' if status.get('trading_mode','PAPER') == 'PAPER' else 'Live' }} ·
        <span id="auto-cap-label">{% if status.operating_mode == 'AUTONOMOUS' %}loading cap{% else %}approval required{% endif %}</span>
      </div>
    </div>
  </div>

  <!-- MULTI-SERIES MARKET CHART -->
  <div class="glass teal-glow" style="margin-bottom:14px">
    <div style="padding:12px 16px 8px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Market Overview</div>
      <div style="display:flex;gap:4px;flex-wrap:wrap" id="chart-series-toggles">
        <button class="graph-tab series-btn active" data-idx="0" onclick="toggleSeries(0,this)"
          style="border-left:3px solid #00f5d4;padding-left:7px">Portfolio</button>
        <button class="graph-tab series-btn active" data-idx="1" onclick="toggleSeries(1,this)"
          style="border-left:3px solid #7b61ff;padding-left:7px">Nasdaq</button>
        <button class="graph-tab series-btn active" data-idx="2" onclick="toggleSeries(2,this)"
          style="border-left:3px solid #ffb347;padding-left:7px">Dow</button>
        <button class="graph-tab series-btn active" data-idx="3" onclick="toggleSeries(3,this)"
          style="border-left:3px solid #22d3ee;padding-left:7px">Bonds</button>
        <button class="graph-tab series-btn active" data-idx="4" onclick="toggleSeries(4,this)"
          style="border-left:3px solid #ff4b6e;padding-left:7px">Positions</button>
      </div>
      <div style="display:flex;gap:4px" id="chart-time-tabs">
        <button class="graph-tab active" onclick="loadMarketChart(36,this)">36H</button>
        <button class="graph-tab" onclick="loadMarketChart(168,this)">7D</button>
        <button class="graph-tab" onclick="loadMarketChart(720,this)">30D</button>
      </div>
    </div>
    <div style="height:260px;padding:8px 8px 4px;position:relative">
      <canvas id="market-chart"></canvas>
      <div id="market-chart-loading" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--muted)">Loading chart…</div>
    </div>
    <div style="padding:4px 16px 10px;display:flex;gap:16px;font-size:10px;color:var(--dim);flex-wrap:wrap" id="chart-legend"></div>
  </div>

  <!-- COMPACT POSITIONS -->
  <div class="glass" style="margin-bottom:14px">
    <div style="padding:10px 16px 8px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Open Positions</div>
      <div style="display:flex;align-items:center;gap:10px">
        <div style="font-size:10px;color:var(--muted)" id="positions-count">Loading</div>
        <div style="font-size:9px;color:var(--dim);font-family:var(--mono)" id="positions-refresh-ts"></div>
      </div>
    </div>
    <div id="positions-list" style="padding:4px 0 2px">
      <div class="empty-state"><div class="empty-icon">📊</div>Loading positions...</div>
    </div>
  </div>

  <!-- URGENT FLAGS MODAL -->
  <div id="flags-modal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.75);backdrop-filter:blur(6px)" onclick="if(event.target===this)toggleFlagsModal()">
    <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:min(580px,94vw);background:var(--surface);border:1px solid var(--border);border-radius:18px;overflow:hidden;max-height:85vh;display:flex;flex-direction:column">
      <!-- header -->
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0">
        <div style="font-size:13px;font-weight:700;color:var(--text)">System Flags</div>
        <div id="flags-modal-summary" style="font-size:10px;color:var(--muted)"></div>
        <button onclick="toggleFlagsModal()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:0 2px;margin-left:auto;line-height:1">×</button>
      </div>
      <!-- body -->
      <div id="flags-modal-body" style="padding:14px 20px 20px;overflow-y:auto;display:flex;flex-direction:column;gap:10px">
        <div style="color:var(--muted);font-size:12px;padding:20px 0;text-align:center">No active flags</div>
      </div>
    </div>
  </div>

  <!-- TWO COLUMN: APPROVALS + WATCHLIST -->
  <!-- APPROVAL QUEUE / TRADE LOG -->
  <div class="glass purple-glow" style="margin-bottom:16px">
    <div style="padding:14px 16px 10px;display:flex;align-items:center;gap:8px">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase" id="queue-label">{% if status.operating_mode == 'AUTONOMOUS' %}Recent Signals{% else %}Approval Queue{% endif %}</div>
      <div style="padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;background:var(--purple2);border:1px solid rgba(123,97,255,0.3);color:var(--purple)" id="pending-badge">0 pending</div>
    </div>
    <div style="padding:0 16px 14px" id="approval-list">
      <div class="empty-state"><div class="empty-icon">✅</div>{% if status.operating_mode == 'AUTONOMOUS' %}No recent signals{% else %}No pending approvals{% endif %}</div>
    </div>
  </div>

  <!-- TRADER ACTIVITY -->
  <div class="glass" style="margin-bottom:16px">
    <div style="padding:14px 16px 10px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Trader Activity</div>
      <div style="font-size:10px;color:var(--dim);margin-left:auto" id="trader-activity-ts"></div>
    </div>
    <div id="trader-activity-list" style="padding:8px 16px 12px">
      <div class="empty-state"><div class="empty-icon">⚡</div>Loading...</div>
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
    <div style="padding:14px 16px 0;font-size:11px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Unlock Autonomous Mode</div>
    <div class="unlock-wrap">
      <div class="unlock-note">Set <code>OPERATING_MODE=AUTONOMOUS</code> and <code>AUTONOMOUS_UNLOCK_KEY</code> in .env, then restart portal and agents.</div>
      <input type="password" class="unlock-input" id="unlock-key" placeholder="Enter unlock key">
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
    <button class="graph-tab" onclick="filterIntel('alert',this)">Corroborated</button>
    <button class="graph-tab" onclick="filterIntel('bull',this)">Fresh</button>
    <button class="graph-tab" onclick="filterIntel('bear',this)">Archive</button>
  </div>
  <div class="intel-grid" id="intel-grid">
    <div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--muted);font-size:13px">Loading intelligence...</div>
  </div>
</div>

<!-- ══════════════ NEWS TAB ══════════════ -->
<div class="page" id="tab-news" style="display:none">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
    <div style="font-size:24px;font-weight:700;letter-spacing:-0.5px;color:var(--text)">
      Market <span style="background:linear-gradient(90deg,var(--teal),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">News</span>
    </div>
    <div style="display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:99px;background:rgba(255,255,255,0.04);border:1px solid var(--border);font-size:11px;font-weight:600;color:var(--muted);letter-spacing:0.05em;text-transform:uppercase">
      <div class="status-dot dot-on" style="width:5px;height:5px"></div>
      <span id="news-count">Loading</span>
    </div>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:16px">Live headlines · MarketWatch · Display only — not used in signal calculations</div>
  <div style="display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap" id="news-filters">
    <button class="graph-tab active" onclick="switchNews('all',this)">All</button>
    <button class="graph-tab" onclick="switchNews('Breaking',this)">Breaking</button>
    <button class="graph-tab" onclick="switchNews('Markets',this)">Markets</button>
    <button class="graph-tab" onclick="switchNews('US',this)">US</button>
    <button class="graph-tab" onclick="switchNews('Global',this)">Global</button>
  </div>
  <div class="intel-grid" id="news-grid">
    <div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--muted);font-size:13px">Loading news...</div>
  </div>
</div>

<!-- ══════════════ SCREENING TAB ══════════════ -->
<div class="page" id="tab-screening" style="display:none">

  <div class="section-title">Sector Screening</div>
  <div style="padding:0 4px 16px">
    <div id="screening-meta" style="font-size:12px;color:var(--muted);margin-bottom:12px"></div>
    <div id="screening-grid" style="display:grid;gap:10px"></div>
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
        <div><div class="setting-label">Max Trade Size (USD)</div><div class="setting-desc">Hard dollar cap per trade · 0 = no cap</div></div>
        <input class="glass-input" type="number" id="s-max-trade-usd" min="0" value="{{ settings.max_trade_usd|int }}">
        <span style="font-size:11px;color:var(--muted)">$</span>
      </div>
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
          <option value="aggressive" {% if settings.close_session_mode == 'aggressive' %}selected{% endif %}>Aggressive</option>
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
  ['dashboard','intel','news','screening','settings'].forEach(id => {
    document.getElementById('tab-'+id).style.display = id===t ? '' : 'none';
  });
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  if (t === 'intel') loadIntel();
  if (t === 'news') loadNews('all');
  if (t === 'screening') loadScreening();
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
    max_trade_usd:    document.getElementById('s-max-trade-usd').value,
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
// ── MULTI-SERIES MARKET CHART ──
let marketChart = null;
let _chartHours = 36;
let _seriesVisible = [true, true, true, true, true]; // portfolio, nasdaq, dow, bonds, positions

async function loadMarketChart(hours, btn) {
  if (hours !== undefined) _chartHours = hours;
  if (btn) {
    document.querySelectorAll('#chart-time-tabs .graph-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const loading = document.getElementById('market-chart-loading');
  if (loading) loading.style.display = 'flex';
  try {
    const r = await fetch('/api/market-chart-data?hours=' + _chartHours);
    const d = await r.json();
    buildMarketChart(d);
  } catch(e) {
    const loading2 = document.getElementById('market-chart-loading');
    if (loading2) loading2.textContent = 'Chart data unavailable';
  }
}

function buildMarketChart(d) {
  const loading = document.getElementById('market-chart-loading');
  const ctx = document.getElementById('market-chart');
  if (!ctx) return;
  if (loading) loading.style.display = 'none';

  const labels = d.labels || [];
  // Series definitions: [{label, color, data, type, fill, pointStyle}]
  const seriesDef = [
    {label:'Portfolio', color:'#00f5d4', fill:true,  pointRadius:0},
    {label:'Nasdaq',    color:'#7b61ff', fill:false, pointRadius:0},
    {label:'Dow',       color:'#ffb347', fill:false, pointRadius:0},
    {label:'Bonds',     color:'#22d3ee', fill:false, pointRadius:0},
    {label:'Positions', color:'#ff4b6e', fill:false, pointRadius:6, pointStyle:'triangle', showLine:false, type:'scatter'},
  ];
  const rawSeries = d.series || [];

  const datasets = seriesDef.map((def, i) => {
    const src = rawSeries[i] || [];
    const cg  = ctx.getContext ? ctx.getContext('2d') : null;
    let bg = def.color + '18';
    if (def.fill && cg) {
      const grad = cg.createLinearGradient(0,0,0,200);
      grad.addColorStop(0, def.color + '30');
      grad.addColorStop(1, def.color + '00');
      bg = grad;
    }
    return {
      label:           def.label,
      data:            def.type === 'scatter'
                         ? src.map((v,j) => v !== null ? {x: labels[j], y: v} : null).filter(Boolean)
                         : src,
      borderColor:     def.color,
      backgroundColor: bg,
      borderWidth:     def.type === 'scatter' ? 0 : 2,
      fill:            def.fill ? 'origin' : false,
      tension:         0.35,
      pointRadius:     def.pointRadius,
      pointHitRadius:  8,
      pointStyle:      def.pointStyle || 'circle',
      pointBackgroundColor: def.color,
      showLine:        def.showLine !== false,
      type:            def.type || 'line',
      hidden:          !_seriesVisible[i],
    };
  });

  if (marketChart) marketChart.destroy();
  marketChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: {duration:400, easing:'easeInOutQuart'},
      interaction: {mode:'index', intersect:false},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor: 'rgba(17,21,32,0.97)',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          titleColor: 'rgba(255,255,255,0.45)',
          titleFont: {size:10},
          bodyColor: '#e0ddd8',
          bodyFont: {size:11},
          padding: 10,
          callbacks: {
            label: ctx => {
              if (ctx.datasetIndex === 4) return '  ▲ Position entry';
              const v = ctx.parsed.y;
              return `  ${ctx.dataset.label}: ${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
            }
          }
        }
      },
      scales: {
        x: {
          grid: {color:'rgba(255,255,255,0.04)'},
          ticks: {color:'rgba(255,255,255,0.3)', font:{size:10}, maxTicksLimit:8, maxRotation:0},
        },
        y: {
          position: 'right',
          grid: {color:'rgba(255,255,255,0.04)'},
          ticks: {color:'rgba(255,255,255,0.3)', font:{size:10}, callback: v => (v>=0?'+':'')+v.toFixed(1)+'%'},
        }
      }
    }
  });

  // Update toggle button states from hidden state
  document.querySelectorAll('.series-btn').forEach((btn,i) => {
    const hidden = marketChart.getDatasetMeta(i).hidden;
    btn.classList.toggle('active', !hidden);
  });

  // Legend row
  const legend = document.getElementById('chart-legend');
  if (legend) {
    legend.innerHTML = seriesDef.map((s,i) =>
      `<span style="display:flex;align-items:center;gap:4px;opacity:${_seriesVisible[i]?1:0.4}">
        <span style="width:16px;height:2px;background:${s.color};display:inline-block;border-radius:2px"></span>
        ${s.label}
      </span>`
    ).join('');
  }
}

function toggleSeries(idx, btn) {
  if (!marketChart) return;
  const meta = marketChart.getDatasetMeta(idx);
  meta.hidden = !meta.hidden;
  _seriesVisible[idx] = !meta.hidden;
  marketChart.update();
  btn.classList.toggle('active', !meta.hidden);
  // Update legend opacity
  const legend = document.getElementById('chart-legend');
  if (legend) {
    const spans = legend.querySelectorAll('span');
    if (spans[idx]) spans[idx].style.opacity = _seriesVisible[idx] ? '1' : '0.4';
  }
}

// keep loadGraph as alias for portfolio-only backward compat (no longer called)
async function loadGraph(days, btn) { loadMarketChart(days <= 30 ? 36 : 720, btn); }

// ── STATUS LOAD ──
function renderPositions(positions) {
  const el = document.getElementById('positions-list');
  const ct = document.getElementById('positions-count');
  const ts = document.getElementById('positions-refresh-ts');
  const tracked = (positions||[]).filter(p => !p.is_orphan);
  const orphans = (positions||[]).filter(p => p.is_orphan);
  if (!positions || !positions.length) {
    el.innerHTML = '<div class="empty-state" style="padding:12px 0"><div class="empty-icon">📊</div>No open positions</div>';
    ct.textContent = '0 open';
    return;
  }
  ct.textContent = tracked.length + ' tracked' + (orphans.length ? ' · ' + orphans.length + ' orphan' : '');
  if (ts) ts.textContent = 'Live · ' + new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'}) + ' ET';

  const accentColors = ['#00f5d4','#7b61ff','#22d3ee','#a78bfa'];

  const renderRow = (p, i, isOrphan) => {
    const unreal   = p.unrealized_pl || 0;
    const urealPct = p.unrealized_plpc || 0;
    const dayPl    = p.day_pl || 0;
    const dayPct   = p.day_plpc || 0;
    const mktVal   = p.market_value || (p.current_price * p.shares) || 0;
    const curPrice = p.current_price || p.entry_price || 0;
    const avgEntry = p.avg_entry_price || p.entry_price || 0;
    const accent   = isOrphan ? '#ffb347' : accentColors[i % accentColors.length];
    const plCol    = unreal >= 0 ? '#00f5d4' : '#ff4b6e';
    const dayCol   = dayPl >= 0  ? 'rgba(0,245,212,0.7)' : 'rgba(255,75,110,0.7)';
    const plSign   = unreal >= 0 ? '+' : '';
    const daySign  = dayPl >= 0  ? '+' : '';
    return `<div style="display:grid;grid-template-columns:32px 1fr auto auto auto;align-items:center;gap:10px;
              padding:7px 16px;border-bottom:1px solid rgba(255,255,255,0.04);
              ${isOrphan ? 'background:rgba(255,179,71,0.03)' : ''}">
      <div style="width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;
           font-size:9px;font-weight:800;letter-spacing:0.02em;
           background:${accent}18;border:1px solid ${accent}40;color:${accent}">
        ${(p.ticker||'?').slice(0,4)}
      </div>
      <div style="min-width:0">
        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:12px;font-weight:700;color:var(--text)">${p.ticker||'?'}</span>
          ${isOrphan ? '<span style="padding:1px 5px;border-radius:99px;font-size:8px;font-weight:700;background:rgba(255,179,71,0.12);border:1px solid rgba(255,179,71,0.3);color:#ffb347">ORPHAN</span>' : ''}
        </div>
        <div style="font-size:10px;color:var(--muted)">${(p.shares||0).toFixed(p.shares>=1?2:4)} sh · avg $${avgEntry.toFixed(2)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:12px;font-weight:700;color:var(--text)">$${mktVal.toFixed(2)}</div>
        <div style="font-size:10px;color:var(--dim)">@ $${curPrice.toFixed(2)}</div>
      </div>
      <div style="text-align:right;min-width:72px">
        <div style="font-size:10px;color:${dayCol}">${daySign}$${Math.abs(dayPl).toFixed(2)}</div>
        <div style="font-size:9px;color:var(--dim)">Today ${daySign}${Math.abs(dayPct).toFixed(2)}%</div>
      </div>
      <div style="text-align:right;min-width:68px">
        <div style="font-size:11px;font-weight:700;color:${plCol}">${plSign}$${Math.abs(unreal).toFixed(2)}</div>
        <div style="font-size:9px;color:var(--dim)">${plSign}${Math.abs(urealPct).toFixed(2)}% total</div>
      </div>
    </div>`;
  };

  el.innerHTML = [
    ...tracked.map((p,i) => renderRow(p, i, false)),
    ...orphans.map((p,i) => renderRow(p, i, true)),
  ].join('') || '<div class="empty-state" style="padding:12px 0"><div class="empty-icon">📊</div>No open positions</div>';
}

function renderApprovals(approvals) {
  const isAuto  = (document.getElementById('stat-mode')||{}).textContent === 'AUTONOMOUS';
  // In AUTONOMOUS mode show recent EXECUTED/APPROVED; in SUPERVISED show PENDING
  const relevant = isAuto
    ? approvals.filter(a => ['EXECUTED','APPROVED'].includes(a.status)).slice(-5).reverse()
    : approvals.filter(a => a.status === 'PENDING_APPROVAL');
  const el = document.getElementById('approval-list');
  const badge = document.getElementById('pending-badge');
  const qLabel = document.getElementById('queue-label');
  if (isAuto) {
    if (qLabel) qLabel.textContent = 'Recent Signals';
    badge.textContent = relevant.length + ' recent';
    if (!relevant.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">⚡</div>No signals executed yet</div>';
      return;
    }
  } else {
    badge.textContent = relevant.length + ' pending';
    if (!relevant.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">✅</div>No pending approvals</div>';
      return;
    }
  }
  el.innerHTML = relevant.map(t => {
    const conf    = (t.confidence||'').toUpperCase();
    const confCls = conf === 'HIGH' ? 'conf-high' : conf === 'MEDIUM' ? 'conf-med' : 'conf-low';
    const reasoning = t.reasoning ? t.reasoning.slice(0,180) + (t.reasoning.length > 180 ? '...' : '') : '';
    const statusBadge = isAuto
      ? `<div style="font-size:8px;padding:2px 6px;border-radius:99px;background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.25);color:#00f5d4">${t.status}</div>`
      : '';
    const price   = t.price ? ` · $${parseFloat(t.price).toFixed(2)}` : '';
    const shares  = t.shares ? ` · ${parseFloat(t.shares).toFixed(4)} sh` : '';
    return `<div class="trade-item">
      <div class="trade-header">
        <div class="trade-ticker-icon">${(t.ticker||'?').slice(0,4)}</div>
        <div class="trade-meta">
          <div class="trade-headline">${t.ticker||'?'} · ${t.politician||'Unknown'}${statusBadge}</div>
          <div class="trade-sub">${(t.queued_at||t.executed_at||'').slice(0,16)}${price}${shares}</div>
        </div>
        <div class="conf-chip ${confCls}">${conf}</div>
      </div>
      ${reasoning ? `<div class="trade-reasoning">${reasoning}</div>` : ''}
      ${!isAuto ? `<div class="trade-actions">
        <button class="btn-approve" onclick="actionTrade(${t.id},'APPROVED')">✓ Approve</button>
        <button class="btn-reject" onclick="actionTrade(${t.id},'REJECTED')">✗ Reject</button>
      </div>` : ''}
    </div>`;
  }).join('');
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
    // Data feed pill
    const cp = document.getElementById('pill-datafeed');
    if (d.trading_mode && d.trading_mode !== 'unconfigured') {
      cp.className = 'status-pill sp-ok';
      cp.innerHTML = '<div class="status-dot dot-on"></div>Data Feed';
    } else {
      cp.className = 'status-pill sp-dim';
      cp.innerHTML = '<div class="status-dot dot-dim"></div>Data Feed';
    }
    // Uptime
    document.getElementById('uptime-val').textContent = d.uptime || 'N/A';
    document.getElementById('pill-uptime').className = 'status-pill sp-dim';
    // Memory pill
    if (d.memory) {
      const pct  = d.memory.ram_pct;
      const used = d.memory.ram_used_mb;
      const tot  = d.memory.ram_total_mb;
      const mp   = document.getElementById('pill-memory');
      const dot  = document.getElementById('mem-dot');
      const lbl  = document.getElementById('mem-val');
      lbl.textContent = `RAM ${pct}%`;
      mp.title = `${used} MB / ${tot} MB used  ·  Swap ${d.memory.swap_pct}%  ·  CPU ${d.memory.cpu_pct}%`;
      if (pct >= 85) {
        mp.className = 'status-pill sp-err';
        dot.className = 'status-dot dot-off';
      } else if (pct >= 70) {
        mp.className = 'status-pill sp-warn';
        dot.className = 'status-dot dot-warn';
      } else {
        mp.className = 'status-pill sp-ok';
        dot.className = 'status-dot dot-on';
      }
    }
  } catch(e) {}
}

// ── FLAGS MODAL ──
let _flagsData = [];

const FLAG_COLOR = {
  critical: {bar:'#ff4b6e', bg:'rgba(255,75,110,0.08)',  border:'rgba(255,75,110,0.25)',  badge:'rgba(255,75,110,0.15)',  text:'#ff4b6e'},
  warning:  {bar:'#ffb347', bg:'rgba(255,179,71,0.08)',  border:'rgba(255,179,71,0.25)',  badge:'rgba(255,179,71,0.15)',  text:'#ffb347'},
  info:     {bar:'#00f5d4', bg:'rgba(0,245,212,0.06)',   border:'rgba(0,245,212,0.2)',    badge:'rgba(0,245,212,0.12)',   text:'#00f5d4'},
  low:      {bar:'#556',    bg:'rgba(255,255,255,0.03)', border:'rgba(255,255,255,0.08)', badge:'rgba(255,255,255,0.06)', text:'var(--muted)'},
};

function renderFlagsModal() {
  const body    = document.getElementById('flags-modal-body');
  const summary = document.getElementById('flags-modal-summary');
  if (!body) return;

  if (!_flagsData || !_flagsData.length) {
    body.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:28px 0;text-align:center"><div style="font-size:28px;margin-bottom:8px">✓</div>No active flags — system is clean.</div>';
    if (summary) summary.textContent = '';
    return;
  }

  const critical = _flagsData.filter(f => f.severity === 'CRITICAL').length;
  const warnings = _flagsData.filter(f => f.severity === 'WARNING').length;
  const other    = _flagsData.filter(f => !['CRITICAL','WARNING'].includes(f.severity)).length;
  const parts = [];
  if (critical) parts.push(`${critical} critical`);
  if (warnings) parts.push(`${warnings} warning`);
  if (other)    parts.push(`${other} info`);
  if (summary)  summary.textContent = parts.join(' · ');

  body.innerHTML = _flagsData.map(f => {
    const c = FLAG_COLOR[f.color] || FLAG_COLOR.low;
    const ts = (f.detected_at || '').slice(0, 16).replace('T', ' ');
    const scanTs = f.scan_at ? f.scan_at.slice(0, 16) : '';
    const clearBtn = f.clearable
      ? `<button onclick="acknowledgeFlag(${f.id}, this)"
           style="padding:5px 14px;border-radius:8px;border:1px solid ${c.border};
                  background:${c.badge};color:${c.text};font-size:10px;font-weight:700;
                  cursor:pointer;letter-spacing:0.05em;text-transform:uppercase">
           ${f.btn_label || 'Clear'}
         </button>`
      : `<button onclick="acknowledgeFlag(${f.id}, this)"
           style="padding:5px 14px;border-radius:8px;border:1px solid rgba(255,75,110,0.3);
                  background:rgba(255,75,110,0.08);color:#ff4b6e;font-size:10px;font-weight:700;
                  cursor:pointer;letter-spacing:0.05em;text-transform:uppercase">
           Acknowledge
         </button>`;
    return `
      <div id="flag-card-${f.id}" style="border-radius:12px;border:1px solid ${c.border};
           background:${c.bg};overflow:hidden;position:relative">
        <!-- left severity bar -->
        <div style="position:absolute;left:0;top:0;bottom:0;width:3px;background:${c.bar}"></div>
        <div style="padding:12px 14px 12px 18px">
          <!-- title row -->
          <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:6px">
            <div style="padding:2px 8px;border-radius:99px;font-size:9px;font-weight:800;
                        letter-spacing:0.1em;background:${c.badge};color:${c.text};
                        border:1px solid ${c.border};flex-shrink:0;margin-top:1px">
              ${f.severity}
            </div>
            <div style="font-size:13px;font-weight:700;color:var(--text);flex:1">${f.title}</div>
            <div style="font-size:9px;color:var(--dim);font-family:var(--mono);flex-shrink:0;text-align:right">
              ${ts}<br>Tier ${f.tier}
            </div>
          </div>
          <!-- description -->
          <div style="font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:10px">
            ${f.description}
          </div>
          <!-- source row -->
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div style="font-size:10px;color:var(--dim);font-family:var(--mono)">
              Source: ${f.ticker}
              ${scanTs ? '· Last scan: ' + scanTs : ''}
            </div>
            ${clearBtn}
          </div>
        </div>
      </div>`;
  }).join('');
}

async function acknowledgeFlag(id, btn) {
  btn.disabled = true;
  btn.textContent = '...';
  try {
    const r = await fetch('/api/flags/acknowledge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})
    });
    const d = await r.json();
    if (d.ok) {
      // Remove card from modal and data array
      const card = document.getElementById('flag-card-' + id);
      if (card) card.style.opacity = '0.4';
      _flagsData = _flagsData.filter(f => f.id !== id);
      // Small delay then re-render
      setTimeout(() => {
        renderFlagsModal();
        loadLiveStatus();
      }, 400);
    } else {
      btn.disabled = false;
      btn.textContent = 'Error';
      toast('Failed to clear flag', 'err');
    }
  } catch(e) {
    btn.disabled = false;
    btn.textContent = 'Error';
  }
}

function toggleFlagsModal() {
  const modal = document.getElementById('flags-modal');
  if (!modal) return;
  if (modal.style.display !== 'none') {
    modal.style.display = 'none';
    return;
  }
  renderFlagsModal();
  modal.style.display = 'block';
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
    // Position sub: show orphan warning
    const posSub = document.getElementById('stat-positions-sub');
    if (posSub) {
      const orp = s.orphan_count || 0;
      posSub.textContent = orp > 0 ? `Open · ${orp} orphan` : 'Open';
      posSub.style.color = orp > 0 ? 'rgba(255,179,71,0.8)' : '';
    }
    // Gains sub
    const gains = s.realized_gains||0;
    const gainEl = document.getElementById('stat-gains-sub');
    if (gainEl) {
      gainEl.textContent = (gains>=0?'+':'') + '$'+gains.toFixed(2)+' realized';
      gainEl.style.color = gains>=0 ? 'rgba(0,245,212,0.6)' : 'rgba(255,75,110,0.6)';
    }
    // Autonomous mode cap label
    const capEl = document.getElementById('auto-cap-label');
    if (capEl) {
      const cap = s.max_trade_usd || 0;
      capEl.textContent = cap > 0 ? '$' + cap.toFixed(0) + ' max per trade' : 'no dollar cap';
    }
    // Kill switch bar description
    const descEl = document.getElementById('kill-desc');
    if (descEl && !killState) {
      const mode = s.operating_mode || 'SUPERVISED';
      const cap  = s.max_trade_usd || 0;
      descEl.textContent = mode === 'AUTONOMOUS'
        ? 'Agents active · Autonomous · ' + (cap > 0 ? '$' + cap.toFixed(0) + ' cap per trade' : 'no trade cap')
        : 'Agents active · Supervised mode · No new entries without approval';
    }
    // Flags — update data and stat card appearance
    _flagsData = s.flags_detail || [];
    const flagCount    = s.urgent_flags || 0;
    const criticalCount= s.critical_flags || 0;
    const fc = document.getElementById('stat-flags-card');
    if (fc) fc.className = 'stat-card ' + (criticalCount > 0 ? 'pink' : flagCount > 0 ? '' : '');
    const flagSub = document.getElementById('stat-flags-sub');
    if (flagSub) {
      if (criticalCount > 0) {
        flagSub.textContent = criticalCount + ' critical · click to view';
        flagSub.style.color = 'rgba(255,75,110,0.8)';
      } else if (flagCount > 0) {
        flagSub.textContent = 'warnings · click to view';
        flagSub.style.color = 'rgba(255,179,71,0.8)';
      } else {
        flagSub.textContent = 'all clear';
        flagSub.style.color = '';
      }
    }
    // Re-render modal if it's open
    if (document.getElementById('flags-modal').style.display !== 'none') renderFlagsModal();
    // Agent running banner
    const agentEl = document.getElementById('agent-running-banner');
    if (s.agent_running) {
      const names = {'retail_trade_logic_agent.py':'Bolt (Trader)','retail_news_agent.py':'Scout (News)',
                     'retail_market_sentiment_agent.py':'Pulse (Sentiment)','agent4_audit.py':'Audit Agent'};
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
    renderPositions(s.positions||[]);
    renderApprovals(a);
  } catch(e) { console.log('Status load error:', e); }
}

// ── INTELLIGENCE ──
async function loadIntel() {
  try {
    const r = await fetch('/api/watchlist');
    const d = await r.json();
    const signals = d.signals || [];
    allSignals = signals;
    const freshCount = signals.filter(s=>!s.is_stale).length;
    const staleCount = signals.length - freshCount;
    document.getElementById('intel-count').textContent =
      freshCount + ' fresh' + (staleCount ? ' · ' + staleCount + ' archive' : '');
    renderIntelGrid(signals);
  } catch(e) {}
}

function filterIntel(type, btn) {
  document.querySelectorAll('#intel-filters .graph-tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  let filtered = allSignals;
  if (type === 'high') filtered = allSignals.filter(s=>s.confidence==='HIGH');
  else if (type === 'alert') filtered = allSignals.filter(s=>s.corroborated);
  else if (type === 'bull') filtered = allSignals.filter(s=>!s.is_stale);
  else if (type === 'bear') filtered = allSignals.filter(s=>s.is_stale);
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
  const agentScore = s => {
    const base = s.confidence === 'HIGH' ? 87 : s.confidence === 'MEDIUM' ? 63 : s.confidence === 'NOISE' ? 15 : 30;
    const sent = s.sentiment_score ? Math.round(Math.abs(s.sentiment_score || 0) * 20) : 0;
    return Math.min(99, Math.max(5, base + sent));
  };
  const marketScore = s => {
    const as = agentScore(s);
    const adj = s.is_stale ? -12 : (s.corroborated ? 8 : 0);
    return Math.min(99, Math.max(5, as - 8 + adj));
  };
  const sentClass = s => agentScore(s) > 60 ? 'of-ab' : agentScore(s) < 40 ? 'of-ar' : 'of-an';
  const mSentClass = s => agentScore(s) > 60 ? 'of-mb' : agentScore(s) < 40 ? 'of-mr' : 'of-mn';
  const valClass = s => agentScore(s) > 60 ? 'oval-b' : agentScore(s) < 40 ? 'oval-r' : 'oval-n';
  grid.innerHTML = signals.map((s,i) => {
    const as = agentScore(s); const ms = marketScore(s);
    const sent = sentiment(s);
    const col = colors[i%colors.length];
    return `<div class="charm ${sent}" style="cursor:pointer" onclick="openSigModal(${JSON.stringify(s).replace(/'/g,'&#39;')})">
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
      ${s.is_stale ? `<div style="padding:4px 12px 8px;font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:0.08em">Archive · ${s.staleness||'stale'}</div>` : ''}
    </div>`;
  }).join('');
}

function openSigModal(s) {
  const colors = {HIGH:'rgba(0,245,212,0.15)',MEDIUM:'rgba(123,97,255,0.15)',LOW:'rgba(255,179,71,0.15)',NOISE:'rgba(85,86,102,0.2)'};
  const conf = (s.confidence||'NOISE').toUpperCase();
  const icon = document.getElementById('smi-icon');
  icon.textContent = (s.ticker||'?').slice(0,4);
  icon.style.background = colors[conf]||colors.NOISE;
  icon.style.color = conf==='HIGH'?'var(--teal)':conf==='MEDIUM'?'var(--purple)':conf==='LOW'?'var(--amber)':'var(--muted)';
  document.getElementById('smi-headline').textContent = s.headline||'No headline';
  document.getElementById('smi-source').textContent = (s.source||'Unknown source') + ' · ' + (s.disc_date||'').slice(0,10);
  const ascore = s.confidence==='HIGH'?87:s.confidence==='MEDIUM'?63:s.confidence==='LOW'?30:15;
  const aAdj = Math.min(99,Math.max(5,ascore + (s.sentiment_score ? Math.round(Math.abs(s.sentiment_score)*20):0)));
  const mAdj = Math.min(99,Math.max(5,aAdj - 8 + (s.is_stale?-12:s.corroborated?8:0)));
  document.getElementById('smi-body').innerHTML = `
    <div class="sig-modal-row"><span class="sig-modal-label">Signal</span><span class="sig-modal-val">${conf}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Ticker</span><span class="sig-modal-val">${s.ticker||'Unresolved'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Politician</span><span class="sig-modal-val">${s.politician||'—'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Staleness</span><span class="sig-modal-val">${s.staleness||'unknown'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Sentiment Score</span><span class="sig-modal-val">${s.sentiment_score != null ? s.sentiment_score.toFixed(3) : '—'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Synthos Score</span><span class="sig-modal-val">${aAdj}/100</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Market Alignment</span><span class="sig-modal-val">${mAdj}/100</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Status</span><span class="sig-modal-val">${s.is_stale?'Archive (stale)':s.corroborated?'Corroborated':'Active'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Source</span><span class="sig-modal-val">${s.source||'—'}</span></div>
    <div class="sig-modal-row"><span class="sig-modal-label">Detected</span><span class="sig-modal-val">${(s.created_at||'').slice(0,16)}</span></div>
  `;
  document.getElementById('sig-modal-overlay').classList.add('open');
}
function closeSigModal(e) {
  if (!e || e.target === document.getElementById('sig-modal-overlay')) {
    document.getElementById('sig-modal-overlay').classList.remove('open');
  }
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


// -- SCREENING --
async function loadScreening() {
  const meta = document.getElementById('screening-meta');
  const grid = document.getElementById('screening-grid');
  meta.textContent = 'Loading...';
  grid.innerHTML = '';
  try {
    const r = await fetch('/api/screening');
    const d = await r.json();
    const candidates = d.candidates || [];
    if (!candidates.length) {
      meta.textContent = 'No screening data yet. Run retail_sector_screener.py to populate.';
      return;
    }
    const c0 = candidates[0];
    const ret5 = c0.etf_5yr_return != null ? ((c0.etf_5yr_return*100).toFixed(1)+'%') : 'N/A';
    const retSign = c0.etf_5yr_return > 0 ? '+' : '';
    meta.textContent = 'Sector: '+(c0.sector||'')+' | ETF: '+(c0.etf||'')+' | 5-Year Return: '+retSign+ret5+' | Run: '+(c0.run_id||'').slice(0,16)+' | '+candidates.length+' candidates';
    const sigColor = s => s==='bullish'?'#00f5d4':s==='bearish'?'#ff4b6e':'#a0a0b0';
    const sigLabel = s => s==='bullish'?'Bullish':s==='bearish'?'Bearish':s==='pending'?'Pending':'Neutral';
    const pct = v => v!=null?(v*100).toFixed(0):'--';
    const bar = (v,color) => v!=null ? '<div style="background:rgba(255,255,255,0.07);border-radius:4px;height:6px;width:100%;margin-top:4px"><div style="height:6px;border-radius:4px;width:'+pct(v)+'%;background:'+color+'"></div></div>' : '';
    const congBadge = f => f==='recent_buy' ? '<span style="font-size:10px;background:rgba(0,245,212,0.15);color:#00f5d4;padding:2px 6px;border-radius:4px;margin-top:4px;display:inline-block">Congress: Buy</span>' : f==='recent_sell' ? '<span style="font-size:10px;background:rgba(255,75,110,0.15);color:#ff4b6e;padding:2px 6px;border-radius:4px;margin-top:4px;display:inline-block">Congress: Sell</span>' : '';
    const scoreColor = cs => cs>=0.6?'#00f5d4':cs>=0.4?'#ffb347':'#ff4b6e';
    grid.innerHTML = candidates.map((cd,i) => {
      const ns=cd.news_signal||'pending', ss=cd.sentiment_signal||'pending', cs=cd.combined_score;
      const nc=sigColor(ns), sc=sigColor(ss);
      return '<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:14px 16px;display:grid;grid-template-columns:40px 1fr 1fr 1fr 70px;gap:14px;align-items:start">'
        +'<div style="font-size:20px;font-weight:700;color:var(--teal)">'+(i+1)+'</div>'
        +'<div><div style="font-size:14px;font-weight:600">'+cd.ticker+'</div>'
        +'<div style="font-size:11px;color:var(--muted)">'+(cd.company||'')+'</div>'
        +'<div style="font-size:11px;color:var(--muted)">Weight: '+(cd.etf_weight_pct||0).toFixed(1)+'%</div>'
        +congBadge(cd.congressional_flag)+'</div>'
        +'<div><div style="font-size:10px;color:var(--muted)">NEWS</div>'
        +'<div style="font-size:13px;font-weight:600;color:'+nc+'">'+sigLabel(ns)+'</div>'
        +bar(cd.news_score,nc)
        +'<div style="font-size:10px;color:var(--muted);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px">'+(cd.news_headline||'')+'</div></div>'
        +'<div><div style="font-size:10px;color:var(--muted)">SENTIMENT</div>'
        +'<div style="font-size:13px;font-weight:600;color:'+sc+'">'+sigLabel(ss)+'</div>'
        +bar(cd.sentiment_score,sc)
        +'<div style="font-size:10px;color:var(--muted);margin-top:4px">'+(cd.sentiment_score!=null?'Score: '+pct(cd.sentiment_score)+'/100':'')+'</div></div>'
        +'<div style="text-align:right"><div style="font-size:10px;color:var(--muted)">COMBINED</div>'
        +'<div style="font-size:22px;font-weight:700;color:'+scoreColor(cs)+'">'+' '+(cs!=null?pct(cs):'--')+'</div>'
        +'<div style="font-size:10px;color:var(--muted)">/100</div></div></div>';
    }).join('');
  } catch(e) {
    meta.textContent = 'Error loading screening data.';
  }
}

// ── MARKET INDICES ──
async function loadMarketIndices() {
  try {
    const r = await fetch('/api/market-indices');
    const d = await r.json();
    const bar = document.getElementById('market-indices-bar');
    if (!bar || !d.indices || !d.indices.length) return;
    bar.innerHTML = d.indices.map(idx => {
      const color = idx.up ? 'var(--teal)' : 'var(--pink)';
      const arrow = idx.up ? '▲' : '▼';
      return `<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:6px 14px;display:flex;align-items:center;gap:10px">
        <span style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em">${idx.label}</span>
        <span style="font-size:13px;font-weight:700;color:var(--text);font-family:var(--mono)">$${idx.price.toFixed(2)}</span>
        <span style="font-size:11px;font-weight:700;color:${color}">${arrow} ${Math.abs(idx.chg_pct).toFixed(2)}%</span>
      </div>`;
    }).join('');
  } catch(e) {}
}

// ── TRADER ACTIVITY ──
async function loadTraderActivity() {
  try {
    const r = await fetch('/api/trader-activity');
    const d = await r.json();
    const el = document.getElementById('trader-activity-list');
    const ts = document.getElementById('trader-activity-ts');
    if (!el) return;
    const items = [];
    // Recent scans
    (d.scans||[]).forEach(s => {
      const csc = s.cascade_detected ? ' <span style="color:var(--pink);font-size:9px">CASCADE</span>' : '';
      items.push(`<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.03);font-size:11px">
        <div style="width:44px;font-weight:700;color:var(--teal);font-family:var(--mono);flex-shrink:0">${s.ticker||'?'}</div>
        <div style="color:var(--muted);flex:1">${(s.event_summary||'Scanned').slice(0,60)}${csc}</div>
        <div style="font-size:9px;color:var(--dim);white-space:nowrap">${(s.scanned_at||'').slice(11,16)}</div>
      </div>`);
    });
    if (!items.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">⚡</div>No recent trader scans</div>';
    } else {
      el.innerHTML = items.join('');
      if (ts) ts.textContent = 'updated ' + new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
    }
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
loadMarketChart(36);
loadHealth();
loadAudit();
loadMarketIndices();
loadTraderActivity();
loadNews('all');
setInterval(loadLiveStatus, 30000);
setInterval(loadHealth, 60000);
setInterval(loadAudit, 300000);
setInterval(loadMarketIndices, 120000);
setInterval(loadTraderActivity, 60000);

// ── NEWS ──
let _newsCat = 'all';
const _metaCache = {};   // url → {image, description}
let   _newsArticles = [];

async function loadNews(category) {
  if (category !== undefined) _newsCat = category;
  const grid = document.getElementById('news-grid');
  const cnt  = document.getElementById('news-count');
  if (!grid) return;
  try {
    const r = await fetch('/api/news-headlines?category=' + encodeURIComponent(_newsCat));
    const d = await r.json();
    _newsArticles = d.articles || [];
    cnt.textContent = _newsArticles.length + ' article' + (_newsArticles.length===1?'':'s');
    if (!_newsArticles.length) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--muted);font-size:13px">No news articles yet — Scout will populate on next run</div>';
      return;
    }
    grid.innerHTML = _newsArticles.map((a, idx) => {
      const stale  = a.staleness && a.staleness !== 'fresh';
      const cat    = a.category || 'Markets';
      const catCol = cat==='Breaking' ? 'var(--red)' : cat==='US' ? 'var(--teal)' : cat==='Global' ? 'var(--purple)' : 'var(--muted)';
      const age    = a.pub_date ? timeSince(a.pub_date) : (a.staleness || '');
      const cached = a.link && _metaCache[a.link];
      const imgHtml = cached && cached.image
        ? `<div style="margin:-12px -16px 12px;border-radius:10px 10px 0 0;overflow:hidden;height:140px"><img src="${cached.image}" alt="" style="width:100%;height:100%;object-fit:cover" onerror="this.parentElement.style.display='none'"></div>`
        : `<div class="news-img-placeholder" id="nip-${idx}" style="margin:-12px -16px 12px;border-radius:10px 10px 0 0;overflow:hidden;height:140px;background:rgba(255,255,255,0.04);display:flex;align-items:center;justify-content:center"><span style="font-size:22px;opacity:0.18">📰</span></div>`;
      return `<div class="charm-card" style="cursor:pointer;opacity:${stale?0.65:1};padding:12px 16px 14px;position:relative"
                   onclick="openNewsModal(${idx})">
        ${imgHtml}
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px">
          <span style="font-size:10px;font-weight:700;color:${catCol};text-transform:uppercase;letter-spacing:0.06em">${cat}</span>
          <span style="font-size:10px;color:var(--muted)">${stale?'Archive · ':''}${age}</span>
        </div>
        <div style="font-size:12px;font-weight:600;color:var(--text);line-height:1.5;margin-bottom:5px">${escHtml(a.headline)}</div>
        <div style="font-size:11px;color:var(--muted)">${a.source || 'MarketWatch'}</div>
      </div>`;
    }).join('');
    // Lazy-load OG images for cards without cached data
    _newsArticles.forEach((a, idx) => {
      if (a.link && !_metaCache[a.link]) {
        fetch('/api/article-meta?url=' + encodeURIComponent(a.link))
          .then(r => r.json()).then(m => {
            _metaCache[a.link] = m;
            const ph = document.getElementById('nip-' + idx);
            if (ph && m.image) {
              ph.outerHTML = `<div style="margin:-12px -16px 12px;border-radius:10px 10px 0 0;overflow:hidden;height:140px"><img src="${m.image}" alt="" style="width:100%;height:100%;object-fit:cover" onerror="this.parentElement.style.display='none'"></div>`;
            }
          }).catch(() => {});
      }
    });
  } catch(e) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--red);font-size:13px">Error loading news</div>';
  }
}

function switchNews(cat, btn) {
  document.querySelectorAll('#news-filters .graph-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadNews(cat);
}

async function openNewsModal(idx) {
  const a = _newsArticles[idx];
  if (!a) return;
  const cat    = a.category || 'Markets';
  const catCol = cat==='Breaking' ? 'var(--red)' : cat==='US' ? 'var(--teal)' : cat==='Global' ? 'var(--purple)' : 'var(--muted)';
  const age    = a.pub_date ? timeSince(a.pub_date) : '';
  // Populate static fields immediately
  document.getElementById('nmi-cat-label').innerHTML = `<span style="color:${catCol}">${cat}</span>`;
  document.getElementById('nmi-headline').textContent = a.headline;
  document.getElementById('nmi-meta').textContent = (a.source || 'MarketWatch') + (age ? '  ·  ' + age : '');
  document.getElementById('nmi-link').href = a.link || '#';
  document.getElementById('nmi-body').innerHTML = '<div style="color:var(--muted);font-size:12px;padding:20px 0;text-align:center">Loading…</div>';
  const imgWrap = document.getElementById('nmi-img-wrap');
  imgWrap.style.display = 'none';
  document.getElementById('nmi-img').src = '';
  document.getElementById('news-modal-overlay').style.display = 'flex';
  document.body.style.overflow = 'hidden';
  // Fetch OG meta (use cache if available)
  let meta = a.link && _metaCache[a.link] ? _metaCache[a.link] : null;
  if (!meta && a.link) {
    try {
      const r = await fetch('/api/article-meta?url=' + encodeURIComponent(a.link));
      meta = await r.json();
      _metaCache[a.link] = meta;
    } catch(e) { meta = {}; }
  }
  if (meta && meta.image) {
    const img = document.getElementById('nmi-img');
    img.src = meta.image;
    img.onerror = () => { imgWrap.style.display = 'none'; };
    imgWrap.style.display = '';
  }
  const desc = meta && meta.description ? meta.description : '';
  document.getElementById('nmi-body').innerHTML = desc
    ? `<p style="font-size:13px;color:var(--text);line-height:1.7;margin:0">${escHtml(desc)}</p>`
    : `<p style="font-size:12px;color:var(--muted);line-height:1.6;margin:0">No preview available — click the button below to read the full article on MarketWatch.</p>`;
}

function closeNewsModal(e) {
  if (e && e.target !== document.getElementById('news-modal-overlay')) return;
  document.getElementById('news-modal-overlay').style.display = 'none';
  document.body.style.overflow = '';
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function timeSince(ts) {
  if (!ts) return '';
  const d = new Date(ts.replace(' ','T')+'Z');
  if (isNaN(d)) return ts;
  const sec = Math.floor((Date.now() - d) / 1000);
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec/60) + 'm ago';
  if (sec < 86400) return Math.floor(sec/3600) + 'h ago';
  return Math.floor(sec/86400) + 'd ago';
}
setInterval(() => { if (document.getElementById('tab-news') && document.getElementById('tab-news').style.display !== 'none') loadNews(); }, 120000);
</script>
</body>
</html>"""

# ── ROUTES ────────────────────────────────────────────────────────────────

def get_current_settings():
    """Read current portal-configurable settings from .env."""
    return {
        'max_position_pct':   int(float(os.environ.get('MAX_POSITION_PCT', '0.10')) * 100),
        'max_sector_pct':     int(float(os.environ.get('MAX_SECTOR_PCT', '25'))),
        'max_trade_usd':      float(os.environ.get('MAX_TRADE_USD', '0')),
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
    _rss_src = settings['rss_feeds_json']
    if _rss_src:
        try:
            import json as _json
            feeds = _json.loads(_rss_src)
            rss_display = '\n'.join(f"{f[0]} | {f[1]} | {f[2]}" for f in feeds)
        except Exception:
            rss_display = ''
    if not rss_display:
        # Show built-in defaults so user can see and edit them
        import sys as _sys
        _agent_dir = os.path.join(PROJECT_DIR, 'agents')
        if _agent_dir not in _sys.path:
            _sys.path.insert(0, _agent_dir)
        try:
            import retail_news_agent as _na
            _feeds = _na.get_rss_feeds()
            rss_display = '\n'.join(f"{f[0]} | {f[1]} | {f[2]}" for f in _feeds)
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
                from retail_database import get_db
                get_db().log_event("KILL_SWITCH_ENGAGED", agent="portal",
                                   details=f"Engaged via web portal at {now_et()}")
            except Exception:
                pass
        else:
            if os.path.exists(KILL_SWITCH_FILE):
                os.remove(KILL_SWITCH_FILE)
            log.info("Kill switch cleared via portal")
            try:
                from retail_database import get_db
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
        from retail_database import get_db
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
            from retail_database import get_db
            get_db().log_event("AUTONOMOUS_UNLOCK_FAILED", agent="portal",
                               details=f"Bad key attempt at {now_et()}")
        except Exception:
            pass
        return jsonify({"ok": False}), 403

    # Key matches — update OPERATING_MODE in .env
    update_env('OPERATING_MODE', 'AUTONOMOUS')
    log.info(f"Autonomous mode unlocked via portal at {now_et()}")

    try:
        from retail_database import get_db
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
            from retail_database import get_db
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
            from retail_database import get_db
            get_db().log_event("KEYS_UPDATED", agent="portal",
                               details=f"Updated: {', '.join(updated)}")
        except Exception:
            pass

    return jsonify({'ok': len(errors) == 0, 'updated': updated, 'errors': errors})


@app.route('/api/settings', methods=['POST'])
@login_required
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
        if 'max_trade_usd' in data:
            update_env('MAX_TRADE_USD', str(float(data['max_trade_usd'])))
        for form_key, env_key in mapping.items():
            if form_key in data:
                update_env(env_key, str(data[form_key]))
        log.info(f"Settings updated: {list(data.keys())}")
        try:
            from retail_database import get_db
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
        from retail_database import get_db
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
        from retail_database import get_db
        signals = get_db().get_watching_signals(limit=10)
        return jsonify({'signals': signals})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})


@app.route('/api/screening')
def api_screening():
    """Latest sector screening run — candidates with news, sentiment, and congressional signals."""
    try:
        from retail_database import get_db
        candidates = get_db().get_latest_screening_run()
        return jsonify({'candidates': candidates})
    except Exception as e:
        return jsonify({'candidates': [], 'error': str(e)})


@app.route('/api/market-indices')
def api_market_indices():
    """Fetch intraday quote for SPY (S&P 500), QQQ (Nasdaq), DIA (Dow)."""
    import requests as _req
    alpaca_key    = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    if not alpaca_key:
        return jsonify({'indices': [], 'error': 'no_key'})
    symbols = ['SPY', 'QQQ', 'DIA']
    labels  = {'SPY': 'S&P 500', 'QQQ': 'Nasdaq', 'DIA': 'Dow'}
    headers = {'APCA-API-KEY-ID': alpaca_key, 'APCA-API-SECRET-KEY': alpaca_secret}
    result  = []
    try:
        r = _req.get(
            'https://data.alpaca.markets/v2/stocks/bars',
            headers=headers,
            params={'symbols': ','.join(symbols), 'timeframe': '1Day', 'limit': 2},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json().get('bars', {})
        for sym in symbols:
            bars = data.get(sym, [])
            if len(bars) >= 2:
                prev_close = bars[-2]['c']
                curr_close = bars[-1]['c']
                chg_pct    = (curr_close - prev_close) / prev_close * 100
            elif len(bars) == 1:
                curr_close = bars[0]['c']
                prev_close = bars[0]['o']
                chg_pct    = (curr_close - prev_close) / prev_close * 100
            else:
                continue
            result.append({
                'symbol':  sym,
                'label':   labels[sym],
                'price':   round(curr_close, 2),
                'chg_pct': round(chg_pct, 2),
                'up':      chg_pct >= 0,
            })
    except Exception as e:
        return jsonify({'indices': [], 'error': str(e)})
    return jsonify({'indices': result})


@app.route('/api/market-chart-data')
@login_required
def api_market_chart_data():
    """
    Multi-series % change chart data for the last N hours.
    Series: Portfolio (0), Nasdaq/QQQ (1), Dow/DIA (2), Bonds/BIL (3), Positions markers (4).
    All normalized to % change from the first data point in the window.
    """
    hours = min(int(request.args.get('hours', 36)), 720)
    import requests as _req
    from datetime import timezone, timedelta
    from dateutil import parser as _dp

    alpaca_key    = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    headers_alp   = {'APCA-API-KEY-ID': alpaca_key, 'APCA-API-SECRET-KEY': alpaca_secret}

    now_utc  = datetime.utcnow()
    start_dt = now_utc - timedelta(hours=hours)
    start_s  = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Choose bar timeframe based on window
    if hours <= 48:
        timeframe, limit = '1Hour', hours + 4
    elif hours <= 200:
        timeframe, limit = '4Hour', (hours // 4) + 8
    else:
        timeframe, limit = '1Day', (hours // 24) + 5

    symbols = ['QQQ', 'DIA', 'BIL']
    market_data = {}
    try:
        r = _req.get(
            'https://data.alpaca.markets/v2/stocks/bars',
            headers=headers_alp,
            params={
                'symbols': ','.join(symbols),
                'timeframe': timeframe,
                'start': start_s,
                'limit': limit,
                'feed': 'iex',
            },
            timeout=8,
        )
        if r.ok:
            bars_resp = r.json().get('bars', {})
            for sym in symbols:
                market_data[sym] = bars_resp.get(sym, [])
    except Exception:
        pass

    # Build shared time labels from QQQ (most bars) or whichever is longest
    base_sym = max(symbols, key=lambda s: len(market_data.get(s, [])))
    base_bars = market_data.get(base_sym, [])
    if not base_bars:
        return jsonify({'labels': [], 'series': [[], [], [], [], []]})

    def fmt_label(ts_str):
        try:
            dt = _dp.parse(ts_str).astimezone(timezone.utc)
            if hours <= 48:
                return dt.strftime('%b %d %H:%M')
            elif hours <= 200:
                return dt.strftime('%b %d %H:%M')
            else:
                return dt.strftime('%b %d')
        except Exception:
            return ts_str[:13]

    labels = [fmt_label(b['t']) for b in base_bars]
    base_ts = [b['t'] for b in base_bars]

    def normalize_bars(bars):
        """Map bars to base_ts, return % change from first value."""
        if not bars:
            return [None] * len(base_ts)
        # Build ts→close lookup
        ts_map = {b['t'][:13]: b['c'] for b in bars}
        # Also try exact match
        ts_exact = {b['t']: b['c'] for b in bars}
        vals = []
        for bt in base_ts:
            v = ts_exact.get(bt) or ts_map.get(bt[:13])
            vals.append(v)
        # Forward-fill nulls
        last = None
        filled = []
        for v in vals:
            if v is not None:
                last = v
            filled.append(last)
        # Normalize to % change from first non-null
        first = next((v for v in filled if v is not None), None)
        if first is None or first == 0:
            return [None] * len(filled)
        return [round((v / first - 1) * 100, 4) if v is not None else None for v in filled]

    qqq_series = normalize_bars(market_data.get('QQQ', []))
    dia_series = normalize_bars(market_data.get('DIA', []))
    bil_series = normalize_bars(market_data.get('BIL', []))

    # Portfolio series from system_log heartbeats
    portfolio_series = [None] * len(base_ts)
    try:
        from retail_database import get_db
        db = get_db()
        cutoff_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
        with db.conn() as c:
            port_rows = c.execute("""
                SELECT timestamp, portfolio_value FROM system_log
                WHERE portfolio_value IS NOT NULL AND portfolio_value > 0
                  AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_str,)).fetchall()
        if port_rows:
            # Build hourly lookup
            port_map = {}
            for row in port_rows:
                try:
                    ts_key = row['timestamp'][:13]  # YYYY-MM-DD HH
                    port_map[ts_key] = row['portfolio_value']
                except Exception:
                    pass
            # Map to base_ts (convert UTC bar times to local key)
            port_vals = []
            for bt in base_ts:
                # bt is like "2026-03-31T14:00:00Z" → key "2026-03-31 10" (ET is UTC-4)
                try:
                    dt_utc = _dp.parse(bt).replace(tzinfo=timezone.utc)
                    # Try UTC key first, then ET
                    key_utc = dt_utc.strftime('%Y-%m-%d %H')
                    from datetime import timezone as _tz
                    import zoneinfo
                    try:
                        et = dt_utc.astimezone(zoneinfo.ZoneInfo('America/New_York'))
                        key_et = et.strftime('%Y-%m-%d %H')
                    except Exception:
                        key_et = key_utc
                    v = port_map.get(key_et) or port_map.get(key_utc)
                    port_vals.append(v)
                except Exception:
                    port_vals.append(None)
            # Forward-fill
            last = None
            for i, v in enumerate(port_vals):
                if v is not None:
                    last = v
                elif last is not None:
                    port_vals[i] = last
            # Normalize
            first = next((v for v in port_vals if v is not None), None)
            if first and first > 0:
                portfolio_series = [round((v / first - 1) * 100, 4) if v is not None else None for v in port_vals]
    except Exception:
        pass

    # Position entry markers — scatter points at entry timestamp
    position_markers = [None] * len(base_ts)
    try:
        from retail_database import get_db
        db = get_db()
        with db.conn() as c:
            pos_rows = c.execute("""
                SELECT ticker, entry_price, created_at FROM positions
                WHERE created_at >= ?
                ORDER BY created_at ASC
            """, (start_dt.strftime('%Y-%m-%d %H:%M:%S'),)).fetchall()
        for row in pos_rows:
            try:
                entry_key = row['created_at'][:13]
                # Find nearest base_ts index
                for j, bt in enumerate(base_ts):
                    if bt[:13].replace('T', ' ') == entry_key:
                        # Place marker at 0 (start of chart)
                        position_markers[j] = 0
                        break
            except Exception:
                pass
    except Exception:
        pass

    return jsonify({
        'labels':  labels,
        'series':  [portfolio_series, qqq_series, dia_series, bil_series, position_markers],
        'hours':   hours,
    })


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
        from retail_database import get_db
        db = get_db()
        hb = db.get_last_heartbeat('retail_trade_logic_agent')
        if not hb:
            hb = db.get_last_heartbeat('retail_news_agent')
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

    # Memory / CPU
    try:
        import psutil
        vm   = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu  = psutil.cpu_percent(interval=0.2)
        health['memory'] = {
            'ram_pct':    round(vm.percent, 1),
            'ram_used_mb': round((vm.total - vm.available) / 1024 / 1024),
            'ram_total_mb': round(vm.total / 1024 / 1024),
            'swap_pct':   round(swap.percent, 1),
            'cpu_pct':    round(cpu, 1),
        }
        # Raise an urgent flag if RAM is critically high
        if vm.percent >= 85:
            try:
                from retail_database import get_db as _gdb
                _db = _gdb()
                severity = 'CRITICAL' if vm.percent >= 92 else 'WARNING'
                _db.write_urgent_flag(
                    flag_type  = 'HIGH_MEMORY',
                    severity   = severity,
                    message    = f"RAM usage at {vm.percent:.1f}% ({health['memory']['ram_used_mb']} MB / {health['memory']['ram_total_mb']} MB)",
                    details    = health['memory'],
                    clearable  = True,
                )
            except Exception:
                pass
    except ImportError:
        health['memory'] = None

    return jsonify(health)


LOGS_CSS = '<style>\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:#0a0c14;color:#e0ddd8;font-family:sans-serif;min-height:100vh}\nheader{background:#111520;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;\n       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;\n       border-bottom:1px solid #1e2535}\n.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}\n.nav{display:flex;gap:1rem;align-items:center}\n.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}\n.nav a:hover{color:#aaa}\n.tabs{display:flex;gap:0;border-bottom:1px solid #1e2535;padding:0 2rem;\n      background:#111520;overflow-x:auto;flex-wrap:nowrap}\n.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;\n          background:#111520;border-bottom:1px solid #1e2535}\n.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}\nselect{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1e2535;\n       border-radius:6px;color:#e0ddd8}\n.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;\n         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;\n         min-height:calc(100vh - 160px)}\n.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;\n             padding:0.3rem 0.75rem;border:1px solid #1e2535;\n             border-radius:6px;cursor:pointer;background:transparent;color:#556}\n.refresh-btn:hover{background:#1e2535;color:#e0ddd8}\n</style>'

@app.route('/logs')
def logs_page():
    """Tail log files from the browser."""
    log_files = {
        'trader':    'trade_logic_agent.log',
        'scout':     'news_agent.log',
        'pulse':     'market_sentiment_agent.log',
        'portal':    'portal.log',
        'watchdog':  'watchdog.log',
        'boot':      'boot.log',
        'monitor':   'monitor.log',
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


@app.route('/api/flags/acknowledge', methods=['POST'])
@login_required
def api_flags_acknowledge():
    """Acknowledge (clear/silence) an urgent flag by id."""
    data    = request.get_json(silent=True) or {}
    flag_id = data.get('id')
    if not flag_id:
        return jsonify({'ok': False, 'error': 'Missing id'}), 400
    try:
        sys.path.insert(0, PROJECT_DIR)
        from retail_database import get_db
        db = get_db()
        db.acknowledge_urgent_flag(flag_id)
        db.log_event("FLAG_ACKNOWLEDGED", agent="portal",
                     details=f"Flag id={flag_id} acknowledged via portal")
        log.info(f"Flag {flag_id} acknowledged via portal")
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f"Flag acknowledge error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/trader-activity')
@login_required
def api_trader_activity():
    """Recent trader decisions — what Bolt has been scanning and acting on."""
    try:
        from retail_database import get_db
        db = get_db()
        with db.conn() as c:
            scans = c.execute("""
                SELECT ticker, cascade_detected, event_summary, tier, scanned_at
                FROM scan_log ORDER BY scanned_at DESC LIMIT 10
            """).fetchall()
            recent = c.execute("""
                SELECT event, agent, details, timestamp
                FROM system_log
                WHERE event IN ('TRADE_EXECUTED','TRADE_QUEUED','SIGNAL_QUEUED',
                                'NEWS_CLASSIFIED','AGENT_COMPLETE')
                ORDER BY timestamp DESC LIMIT 20
            """).fetchall()
        return jsonify({
            'scans':  [dict(r) for r in scans],
            'recent': [dict(r) for r in recent],
        })
    except Exception as e:
        return jsonify({'scans': [], 'recent': [], 'error': str(e)})


@app.route('/api/audit')
def api_audit():
    """Latest audit result from agent4_audit.py."""
    audit_path = os.path.join(PROJECT_DIR, '.audit_latest.json')
    try:
        data = json.load(open(audit_path))
        return jsonify(data)
    except Exception:
        return jsonify({
            'health_score': None,
            'summary': 'No audit run yet — agent4_audit.py has not executed',
            'critical': [], 'warnings': [], 'info_count': 0,
            'timestamp': None,
        })


@app.route('/api/update', methods=['POST'])
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
                from retail_database import get_db
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
    'retail_trade_logic_agent.py', 'retail_news_agent.py', 'retail_market_sentiment_agent.py',
    # Infrastructure
    'retail_database.py', 'retail_heartbeat.py', 'retail_boot_sequence.py', 'retail_watchdog.py',
    'retail_cleanup.py', 'retail_shutdown.py', 'retail_health_check.py', 'retail_portal.py',
    'daily_digest.py', 'digest_agent.py', 'retail_patch.py', 'retail_sync.py',
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
                from retail_database import get_db
                get_db().log_event("FILE_UPLOADED", agent="portal", details=fname)
            except Exception:
                pass
            if fname == 'retail_portal.py':
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
        from retail_database import get_db
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


_article_meta_cache: dict = {}

@app.route('/api/article-meta')
@login_required
def api_article_meta():
    """Fetch OG image + description for a news article URL. Cached in memory."""
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'image': None, 'description': None})
    if url in _article_meta_cache:
        return jsonify(_article_meta_cache[url])
    result = {'image': None, 'description': None}
    try:
        import requests as _req
        from bs4 import BeautifulSoup
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; Synthos/1.0; +https://synth-cloud.com)',
            'Accept': 'text/html,application/xhtml+xml',
        }
        resp = _req.get(url, headers=headers, timeout=8, allow_redirects=True)
        if resp.ok:
            soup = BeautifulSoup(resp.text, 'html.parser')
            def og(prop):
                t = soup.find('meta', property=prop) or soup.find('meta', attrs={'name': prop})
                return t['content'].strip() if t and t.get('content') else None
            result['image']       = og('og:image') or og('twitter:image')
            result['description'] = og('og:description') or og('twitter:description') or og('description')
    except Exception:
        pass
    _article_meta_cache[url] = result
    return jsonify(result)


@app.route('/api/news-headlines')
@login_required
def api_news_headlines():
    """Display-only news headlines from MarketWatch RSS (source='NEWS')."""
    category = request.args.get('category')
    if category == 'all':
        category = None
    from retail_database import get_db
    db = get_db()
    articles = db.get_news_headlines(category=category, limit=100, min_floor=30)
    return jsonify({'articles': articles, 'count': len(articles)})


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
