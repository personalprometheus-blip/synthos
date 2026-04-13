"""
retail_portal.py — Synthos Multi-Tenant Web Portal
Synthos · v3.0

Serves the customer portal at portal.synth-cloud.com (Cloudflare tunnel → port 5001).

Access control layers (in order):
  1. login_required      — must have a valid session
  2. is_access_allowed() — email verified + subscription active (admin exempt)
  3. construction_required — admin 2FA OTP gate for under-construction routes

.env keys (v3.0):
  PORTAL_SECRET_KEY       — Flask session secret
  PORTAL_PORT             — default 5001
  ENCRYPTION_KEY          — Fernet key for auth.db encryption
  ADMIN_EMAIL             — admin account email
  ADMIN_PASSWORD          — admin account password
  CONSTRUCTION_MODE       — 'true' locks public signup routes behind admin 2FA
  RESEND_API_KEY          — for approval emails, verification emails (Resend.com)
  ALERT_FROM              — verified sender email (must be from a Resend-verified domain)
  STRIPE_SECRET_KEY       — Stripe backend key (wired when Stripe integration is added)
  STRIPE_WEBHOOK_SECRET   — Stripe webhook signing secret
  STRIPE_PRICE_ID         — Stripe Price ID for standard subscription
  STRIPE_EARLY_ADOPTER_PRICE_ID — Stripe Price ID for early-adopter rate
"""

import os
import sys
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template_string, redirect, session, flash
from dotenv import load_dotenv
import auth

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
AUTONOMOUS_UNLOCK_KEY = os.environ.get('AUTONOMOUS_UNLOCK_KEY', '')
OPERATING_MODE       = os.environ.get('OPERATING_MODE', 'SUPERVISED').upper()
MONITOR_URL          = os.environ.get('MONITOR_URL', 'http://localhost:5000')
MONITOR_TOKEN        = os.environ.get('MONITOR_TOKEN', 'synthos-default-token')
RESEND_API_KEY           = os.environ.get('RESEND_API_KEY', '')
ALERT_FROM               = os.environ.get('ALERT_FROM', '')
ADMIN_EMAIL              = os.environ.get('ADMIN_EMAIL', '')
STRIPE_WEBHOOK_SECRET    = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_SECRET_KEY        = os.environ.get('STRIPE_SECRET_KEY', '')
# Base URL used to build /setup-account links in setup emails.
# Defaults to localhost for dev; set to https://portal.synth-cloud.com in prod.
PORTAL_BASE_URL          = os.environ.get('PORTAL_BASE_URL', '').rstrip('/')

# ── CONSTRUCTION MODE ─────────────────────────────────────────────────────
# When true, public-facing signup/subscription routes show a "coming soon" page.
# Admin users must pass a one-time email OTP to access construction-locked routes.
# Set CONSTRUCTION_MODE=false in .env to open the gates when ready to launch.
CONSTRUCTION_MODE    = os.environ.get('CONSTRUCTION_MODE', 'true').lower() == 'true'

# ── TERMS OF SERVICE ──────────────────────────────────────────────────────
# Bump this version string when ToS content changes to force re-acceptance.
TOS_CURRENT_VERSION  = "1.0"
# Per-customer agreement files go here: data/customers/<id>/agreements/
_CUSTOMERS_DIR       = os.path.join(_ROOT_DIR, 'data', 'customers')

# In-memory OTP store — one slot, TTL enforced on use
# {'otp': str, 'expires_at': datetime, 'session_key': str}
_construction_otp: dict = {}

# ── RATE LIMITING ─────────────────────────────────────────────────────────
# Simple in-memory sliding window. Keyed by IP address.
# _login_attempts: {ip: [(timestamp, ...), ...]}
# _otp_attempts:   {ip: [(timestamp, ...), ...]}
import collections as _collections
_login_attempts: dict = _collections.defaultdict(list)
_otp_attempts:   dict = _collections.defaultdict(list)

_LOGIN_MAX     = 10   # max attempts per window
_LOGIN_WINDOW  = 300  # seconds (5 minutes)
_OTP_MAX       = 5    # max attempts per window
_OTP_WINDOW    = 300  # seconds (5 minutes)


def _rate_limited(store: dict, ip: str, max_attempts: int, window_s: int) -> bool:
    """Return True if IP has exceeded max_attempts in the last window_s seconds."""
    import time as _time
    now = _time.monotonic()
    # Prune old entries
    store[ip] = [t for t in store[ip] if now - t < window_s]
    if len(store[ip]) >= max_attempts:
        return True
    store[ip].append(now)
    return False

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s portal: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('portal')

app = Flask(__name__)
app.secret_key = os.environ.get('PORTAL_SECRET_KEY', secrets.token_hex(32))

# ── SESSION COOKIE SECURITY ────────────────────────────────────────────────
# Secure=True: browser only sends cookie over HTTPS (Cloudflare Tunnel handles TLS).
# Set HTTPS_ONLY=false in .env only for local HTTP testing.
app.config['SESSION_COOKIE_SECURE']      = os.environ.get('HTTPS_ONLY', 'true').lower() != 'false'
app.config['SESSION_COOKIE_HTTPONLY']    = True          # JS cannot read session cookie
app.config['SESSION_COOKIE_SAMESITE']   = 'Strict'      # blocks cross-site request forgery
app.config['SESSION_COOKIE_NAME']        = 'synthos_s'  # non-default name
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# ── SESSION ACTIVITY TRACKING ──────────────────────────────────────────────
import threading as _threading
from collections import deque as _deque

_session_activity = {}           # {customer_id: {last_activity: datetime, ip: str}}
_session_activity_lock = _threading.Lock()
_CUSTOMER_TIMEOUT = timedelta(minutes=15)
_session_hourly = _deque(maxlen=1440)   # (iso_minute, active_count) per-minute for 24h



# ── SECURITY HEADERS ───────────────────────────────────────────────────────
@app.after_request
def _security_headers(response):
    """
    Apply security headers to every response.

    CSP uses 'unsafe-inline' for scripts/styles because the portal renders
    all HTML via render_template_string with inline JS/CSS. Refactor to
    nonce-based CSP when the frontend is componentised.
    SameSite=Strict on the session cookie is the primary CSRF mitigation.
    """
    h = response.headers
    h['X-Frame-Options']         = 'DENY'
    h['X-Content-Type-Options']  = 'nosniff'
    h['X-XSS-Protection']        = '1; mode=block'
    h['Referrer-Policy']         = 'strict-origin-when-cross-origin'
    h['Permissions-Policy']      = 'geolocation=(), microphone=(), camera=()'
    h['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://cdn.benzinga.com https://*.benzinga.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    # HSTS: only emit if running in HTTPS mode (default true in production).
    if app.config.get('SESSION_COOKIE_SECURE'):
        h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


# ── CONSTRUCTION LOCK HELPERS ─────────────────────────────────────────────

def _send_construction_otp() -> bool:
    """
    Generate a 6-digit OTP, store it in memory with a 10-minute TTL,
    and email it to ADMIN_EMAIL via Resend.
    Returns True if sent, False if Resend not configured.
    """
    if not RESEND_API_KEY or not ADMIN_EMAIL or not ALERT_FROM:
        log.warning("Construction OTP: Resend not configured — cannot send OTP email")
        return False

    otp = str(secrets.randbelow(900000) + 100000)  # 100000–999999
    _construction_otp['otp']        = otp
    _construction_otp['expires_at'] = datetime.now(timezone.utc) + timedelta(minutes=10)

    try:
        import urllib.request, json as _json
        payload = _json.dumps({
            "from":    ALERT_FROM,
            "to":      [ADMIN_EMAIL],
            "subject": "Synthos Construction Access Code",
            "text": (
                f"Your Synthos construction access code is: {otp}\n\n"
                f"Valid for 10 minutes.\n"
                f"Enter this code at synth-cloud.com/admin/construction-verify\n\n"
                f"If you did not request this, someone with admin credentials is "
                f"attempting to access construction routes — check immediately."
            ),
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log.info(f"Construction OTP sent to {ADMIN_EMAIL}")
        return True
    except Exception as e:
        log.error(f"Construction OTP email failed: {e}")
        return False


def _verify_construction_otp(entered: str) -> bool:
    """Validate entered OTP against stored value. Clears OTP on success."""
    stored     = _construction_otp.get('otp', '')
    expires_at = _construction_otp.get('expires_at')
    if not stored or not expires_at:
        return False
    if datetime.now(timezone.utc) > expires_at:
        _construction_otp.clear()
        return False
    if secrets.compare_digest(entered.strip(), stored):
        _construction_otp.clear()
        return True
    return False


def construction_required(f):
    """
    Decorator for routes that are under construction.
    Non-admin users: shown the construction / coming-soon page.
    Admin users: must pass a one-time email OTP challenge for this session.
    When CONSTRUCTION_MODE=false: passes through to the route with no gate.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not CONSTRUCTION_MODE:
            return f(*args, **kwargs)
        if not is_admin():
            return render_template_string(_CONSTRUCTION_PAGE_HTML), 200
        if not session.get('construction_unlocked'):
            session['construction_redirect'] = request.path
            return redirect('/admin/construction-verify')
        return f(*args, **kwargs)
    return decorated


def _send_setup_email(email: str, setup_link: str, display_name: str = '') -> bool:
    """
    Send a password-setup / account-activation email to a new customer via Resend.
    Called after account creation (admin-triggered or Stripe webhook).
    """
    if not RESEND_API_KEY or not ALERT_FROM:
        log.warning(f"Setup email: Resend not configured — skipping email to {email}")
        return False
    name = display_name or 'there'
    try:
        import requests as _req
        r = _req.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"Synthos <{ALERT_FROM}>",
                "to": [email],
                "subject": "Your Synthos account is ready",
                "text": (
                    f"Hi {name},\n\n"
                    f"Your Synthos account has been approved and is ready to use.\n\n"
                    f"Click the link below to activate your account:\n\n"
                    f"{setup_link}\n\n"
                    f"This link expires in 48 hours.\n\n"
                    f"Once activated, log in at synth-cloud.com\n\n"
                    f"\u2014 The Synthos Team"
                ),
            }, timeout=10)
        if r.status_code in (200, 201):
            log.info(f"Setup email sent to {email}")
            return True
        log.warning(f"Setup email: Resend returned {r.status_code}")
        return False
    except Exception as e:
        log.error(f"Setup email failed for {email}: {e}")
        return False




def _send_verification_email(email, name, token):
    """Send email verification link to new signup via Resend."""
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set — skipping verification email")
        return False

    verify_url = f"https://synth-cloud.com/verify-email/{token}"
    alert_from = os.getenv("ALERT_FROM", "Synth_Alerts@synth-cloud.com")

    import requests as _req
    try:
        r = _req.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"Synthos <{alert_from}>",
                "to": [email],
                "subject": "Verify your Synthos email",
                "text": (
                    f"Hi {name},\n\n"
                    f"Thank you for signing up for Synthos.\n\n"
                    f"Please click the link below to verify your email address:\n\n"
                    f"{verify_url}\n\n"
                    f"This link expires in 48 hours.\n\n"
                    f"If you did not create this account, you can ignore this email.\n\n"
                    f"— Synthos"
                ),
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            log.info(f"Verification email sent to {email}")
            return True
        log.warning(f"Resend returned {r.status_code} for verification: {r.text[:100]}")
        return False
    except Exception as e:
        log.warning(f"Verification email failed: {e}")
        return False

# Custom Jinja filter for file timestamps
@app.template_filter('timestamp_to_date')

def _send_approval_email(email, name):
    """Send account approval notification email via Resend."""
    if not RESEND_API_KEY or not email:
        return False
    alert_from = os.getenv("ALERT_FROM", "Synth_Alerts@synth-cloud.com")
    import requests as _req
    try:
        r = _req.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"Synthos <{alert_from}>",
                "to": [email],
                "subject": "Your Synthos account has been approved",
                "text": (
                    f"Hi {name or 'there'},\n\n"
                    f"Your Synthos account has been approved and is ready to use.\n\n"
                    f"Log in at synth-cloud.com to get started.\n\n"
                    f"Once logged in, follow the setup guide to connect your "
                    f"trading account and configure your agent.\n\n"
                    f"Welcome to Synthos!\n\n"
                    f"\u2014 The Synthos Team"
                ),
            }, timeout=10)
        if r.status_code in (200, 201):
            log.info(f"Approval email sent to {email}")
            return True
        log.warning(f"Approval email: Resend returned {r.status_code}")
        return False
    except Exception as e:
        log.warning(f"Approval email failed: {e}")
        return False


def timestamp_to_date(ts):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%m/%d %H:%M')
    except Exception:
        return '—'


def login_required(f):
    """Full access gate: requires valid session AND accepted current ToS."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return redirect('/login')
        if session.get('tos_version') != TOS_CURRENT_VERSION:
            return redirect('/terms')
        return f(*args, **kwargs)
    return decorated


def authenticated_only(f):
    """Session gate only — does NOT check ToS. Used for /terms itself to avoid redirect loop."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return redirect('/login')
        if not is_admin():
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


# ── AUTH ──────────────────────────────────────────────────────────────────

# Shared design tokens used across landing and login pages
_SHARED_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:90%;color-scheme:dark}
:root{
  /* ── Surfaces ── */
  --bg:      #0a0c14;
  --surface: #111520;
  --surface2:#161b28;
  --surface3:#1c2235;

  /* ── Borders ── */
  --border:  rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.13);
  --border3: rgba(255,255,255,0.20);

  /* ── Text ── */
  --text:    rgba(255,255,255,0.88);
  --muted:   rgba(255,255,255,0.40);
  --dim:     rgba(255,255,255,0.18);

  /* ── Existing color names (preserved for compatibility) ── */
  --teal:    #00f5d4;
  --teal2:   rgba(0,245,212,0.08);
  --pink:    #ff4b6e;
  --pink2:   rgba(255,75,110,0.08);
  --purple:  #7b61ff;
  --purple2: rgba(123,97,255,0.08);
  --amber:   #f5a623;
  --amber2:  rgba(245,166,35,0.08);
  --green:   #22c55e;

  /* ── Semantic aliases (new — use for all new code) ── */
  --cyan:        #00f5d4;
  --cyan-dim:    rgba(0,245,212,0.08);
  --cyan-mid:    rgba(0,245,212,0.09);
  --cyan-glow:   rgba(0,245,212,0.22);
  --violet:      #7b61ff;
  --violet-dim:  rgba(123,97,255,0.08);
  --violet-mid:  rgba(123,97,255,0.09);
  --violet-glow: rgba(123,97,255,0.18);
  --signal:      #f5a623;
  --signal-dim:  rgba(245,166,35,0.08);
  --signal-mid:  rgba(245,166,35,0.15);
  --signal-glow: rgba(245,166,35,0.18);
  --red:         #ff4b6e;
  --red-dim:     rgba(255,75,110,0.08);
  --red-mid:     rgba(255,75,110,0.09);

  /* ── Glow scale (use these, not ad-hoc box-shadows) ── */
  --glow-hero:   0 0 20px rgba(0,245,212,0.22);
  --glow-active: 0 0 8px  rgba(0,245,212,0.12);
  --glow-dot:    0 0 5px  currentColor;

  /* ── Shadow scale ── */
  --shadow-card:  0 4px 16px rgba(0,0,0,0.3);
  --shadow-modal: 0 24px 80px rgba(0,0,0,0.6);

  /* ── Typography scale ── */
  --text-hero: 28px;
  --text-xl:   20px;
  --text-lg:   15px;
  --text-base: 13px;
  --text-sm:   11px;
  --text-xs:   10px;
  --text-xxs:  9px;

  /* ── Fonts ── */
  --sans: 'Inter',system-ui,sans-serif;
  --mono: 'JetBrains Mono',monospace;

  /* ── Motion ── */
  --pulse-live:  2s;
  --pulse-alert: 3s;
  --transition:  0.15s;
}
body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
"""

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Algorithmic Trading Platform</title>
<style>
""" + _SHARED_CSS + """

/* ── NAV ── */
nav{
  position:fixed;top:0;left:0;right:0;z-index:100;
  display:flex;align-items:center;justify-content:space-between;
  padding:0.45rem 2rem;
  background:rgba(10,12,20,0.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
}
.nav-left{display:flex;align-items:center;gap:0.7rem}
.hamburger{
  display:flex;flex-direction:column;gap:4px;
  background:none;border:none;cursor:pointer;padding:5px;opacity:0.4;transition:opacity .15s;
}
.hamburger:hover{opacity:0.9}
.hamburger span{display:block;width:16px;height:1.5px;background:var(--text);border-radius:2px}
.nav-logo{
  font-family:var(--mono);font-size:0.85rem;font-weight:500;letter-spacing:0.18em;
  color:var(--teal);text-shadow:0 0 18px rgba(0,245,212,0.20);
}
/* portrait dropdown */
.profile-btn{
  position:relative;width:30px;height:30px;border-radius:50%;
  background:var(--surface2);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:border-color .15s;flex-shrink:0;
}
.profile-btn:hover{border-color:var(--teal);box-shadow:0 0 10px rgba(0,245,212,0.09)}
.profile-btn svg{color:rgba(255,255,255,0.45);width:15px;height:15px}
.auth-drop{
  display:none;position:absolute;top:calc(100% + 8px);right:0;
  width:252px;
  background:var(--surface);border:1px solid var(--border2);
  border-radius:12px;padding:1.1rem;
  box-shadow:0 20px 50px rgba(0,0,0,0.6),0 0 0 1px rgba(0,245,212,0.05);
  z-index:200;
}
.auth-drop.open{display:block}
.auth-drop-title{font-size:0.78rem;font-weight:600;margin-bottom:0.15rem;color:var(--text)}
.auth-drop-sub{font-size:0.68rem;color:var(--muted);margin-bottom:0.9rem}
.auth-drop label{
  display:block;font-size:0.62rem;font-weight:500;letter-spacing:0.09em;
  text-transform:uppercase;color:var(--muted);margin-bottom:0.28rem;
}
.auth-drop input{
  font-family:var(--mono);font-size:0.78rem;width:100%;
  padding:0.45rem 0.65rem;background:rgba(255,255,255,0.03);
  border:1px solid var(--border);border-radius:6px;
  color:var(--text);margin-bottom:0.65rem;transition:border-color .15s;
}
.auth-drop input:focus{outline:none;border-color:rgba(0,245,212,0.22);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
.auth-drop input::placeholder{color:rgba(255,255,255,0.18)}
.auth-drop-btn{
  font-family:var(--mono);font-size:0.72rem;font-weight:500;letter-spacing:0.07em;
  width:100%;padding:0.5rem;
  background:rgba(0,245,212,0.12);color:var(--teal);
  border:1px solid rgba(0,245,212,0.14);border-radius:6px;
  cursor:pointer;transition:all .15s;
}
.auth-drop-btn:hover{background:rgba(0,245,212,0.12);box-shadow:0 0 14px rgba(0,245,212,0.09)}

/* ── HERO ── */
.hero{
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  padding:7rem 2rem 4rem;text-align:center;position:relative;overflow:hidden;
}
.hero-bg{
  position:absolute;inset:0;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 45% at 50% 0%, rgba(0,245,212,0.07) 0%, transparent 65%),
    radial-gradient(ellipse 40% 30% at 15% 60%, rgba(123,97,255,0.05) 0%, transparent 60%),
    radial-gradient(ellipse 40% 30% at 85% 50%, rgba(255,75,110,0.04) 0%, transparent 60%);
}
.hero-grid{
  position:absolute;inset:0;pointer-events:none;opacity:0.03;
  background-image:linear-gradient(var(--teal) 1px,transparent 1px),linear-gradient(90deg,var(--teal) 1px,transparent 1px);
  background-size:60px 60px;
}
.hero-eyebrow{
  font-family:var(--mono);font-size:0.65rem;letter-spacing:0.22em;
  color:var(--teal);text-transform:uppercase;margin-bottom:1.3rem;opacity:0.8;
}
.hero-title{
  font-size:clamp(2.16rem,5.4vw,3.78rem);font-weight:600;line-height:1.1;
  letter-spacing:-0.025em;margin-bottom:1.2rem;color:var(--text);
}
.hero-title span{
  background:linear-gradient(135deg,var(--teal) 0%,#00c4a8 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.hero-sub{
  font-size:0.95rem;color:var(--muted);max-width:440px;margin:0 auto;
  font-weight:400;line-height:1.75;
}

/* ── STATS ── */
.stats{display:flex;flex-wrap:wrap;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.stat{
  flex:1;min-width:150px;padding:1.4rem 1.5rem;text-align:center;
  border-right:1px solid var(--border);position:relative;overflow:hidden;
}
.stat:last-child{border-right:none}
.stat::after{
  content:'';position:absolute;bottom:0;left:15%;right:15%;height:1px;
}
.stat.teal::after{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.stat.pink::after{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.stat.purple::after{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.stat.amber::after{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.stat-value{
  font-family:var(--mono);font-size:1.45rem;font-weight:500;margin-bottom:0.2rem;
}
.stat.teal .stat-value{color:var(--teal);text-shadow:0 0 18px rgba(0,245,212,0.18)}
.stat.pink .stat-value{color:var(--pink);text-shadow:0 0 18px rgba(255,75,110,0.18)}
.stat.purple .stat-value{color:var(--purple);text-shadow:0 0 18px rgba(123,97,255,0.18)}
.stat.amber .stat-value{color:var(--amber);text-shadow:0 0 18px rgba(245,166,35,0.3)}
.stat-label{font-size:0.68rem;color:var(--muted);letter-spacing:0.06em;text-transform:uppercase}

/* ── FEATURES ── */
.features{padding:5rem 2rem;max-width:1040px;margin:0 auto}
.section-eyebrow{
  font-family:var(--mono);font-size:0.65rem;letter-spacing:0.2em;
  color:var(--teal);text-transform:uppercase;margin-bottom:0.75rem;text-align:center;opacity:0.8;
}
.section-title{
  font-size:1.65rem;font-weight:600;text-align:center;
  margin-bottom:0.6rem;letter-spacing:-0.02em;color:var(--text);
}
.section-sub{color:var(--muted);text-align:center;max-width:400px;margin:0 auto 2.75rem;font-size:0.85rem;line-height:1.7}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:1px;background:var(--border)}
.feature-card{
  background:var(--surface);padding:1.5rem;position:relative;overflow:hidden;
  transition:background .2s;
}
.feature-card:hover{background:var(--surface2)}
.feature-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  opacity:0;transition:opacity .2s;
}
.feature-card:hover::before{opacity:1}
.fc-teal::before{background:linear-gradient(90deg,transparent,var(--teal),transparent)}
.fc-pink::before{background:linear-gradient(90deg,transparent,var(--pink),transparent)}
.fc-purple::before{background:linear-gradient(90deg,transparent,var(--purple),transparent)}
.fc-amber::before{background:linear-gradient(90deg,transparent,var(--amber),transparent)}
.feature-tag{
  font-family:var(--mono);font-size:0.6rem;letter-spacing:0.12em;text-transform:uppercase;
  margin-bottom:0.75rem;opacity:0.7;
}
.fc-teal .feature-tag{color:var(--teal)}
.fc-pink .feature-tag{color:var(--pink)}
.fc-purple .feature-tag{color:var(--purple)}
.fc-amber .feature-tag{color:var(--amber)}
.feature-name{font-weight:600;font-size:0.88rem;margin-bottom:0.45rem;color:var(--text)}
.feature-desc{font-size:0.78rem;color:var(--muted);line-height:1.65}

/* ── HOW IT WORKS ── */
.how{padding:4.5rem 2rem;border-top:1px solid var(--border)}
.how-inner{max-width:820px;margin:0 auto}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0;margin-top:2.5rem;position:relative}
.steps::before{
  content:'';position:absolute;top:1.1rem;left:10%;right:10%;height:1px;
  background:linear-gradient(90deg,transparent,var(--border2),var(--border2),transparent);
}
.step{text-align:center;padding:0 1rem}
.step-dot{
  width:10px;height:10px;border-radius:50%;border:1px solid var(--teal);
  background:rgba(0,245,212,0.09);margin:0 auto 0.9rem;
  box-shadow:0 0 8px rgba(0,245,212,0.12);
}
.step-num{font-family:var(--mono);font-size:0.6rem;letter-spacing:0.15em;color:var(--teal);margin-bottom:0.5rem;opacity:0.7}
.step-title{font-weight:600;font-size:0.85rem;margin-bottom:0.4rem;color:var(--text)}
.step-desc{font-size:0.75rem;color:var(--muted);line-height:1.6}

/* ── FOOTER ── */
footer{
  border-top:1px solid var(--border);padding:1.4rem 2rem;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem;
}
.footer-logo{font-family:var(--mono);font-size:0.75rem;letter-spacing:0.18em;color:rgba(0,245,212,0.18)}
.footer-note{font-size:0.68rem;color:var(--dim)}
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div class="nav-left">
    <button class="hamburger" onclick="toggleSidebar()" aria-label="Menu">
      <span></span><span></span><span></span>
    </button>
    <div class="nav-logo">SYNTHOS</div>
  </div>
  <div class="profile-btn" id="profile-btn" onclick="toggleAuthDrop(event)" aria-label="Sign in">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
    </svg>
    <div class="auth-drop" id="auth-drop" onclick="event.stopPropagation()">
      <div class="auth-drop-title">Welcome back</div>
      <div class="auth-drop-sub">Sign in to your Synthos account</div>
      <form method="POST" action="/login">
        <label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" autocomplete="email" required>
        <label>Password</label>
        <input type="password" name="password" placeholder="••••••••" autocomplete="current-password" required>
        <button class="auth-drop-btn" type="submit">Sign In →</button>
      </form>
      <div style="text-align:right;margin-top:0.4rem">
        <a href="/forgot-password" style="font-size:0.62rem;color:var(--muted);text-decoration:none;letter-spacing:0.03em">Forgot password?</a>
      </div>
      <div style="border-top:1px solid var(--border);margin-top:0.8rem;padding-top:0.75rem;text-align:center">
        <div style="font-size:0.65rem;color:var(--muted);margin-bottom:0.4rem">Don't have an account?</div>
        <a href="/signup" style="font-family:var(--mono);font-size:0.72rem;color:var(--teal);text-decoration:none;letter-spacing:0.04em">Create Account →</a>
      </div>
    </div>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-bg"></div>
  <div class="hero-grid"></div>
  <div class="hero-eyebrow">Algorithmic Trading Platform</div>
  <h1 class="hero-title">Your portfolio.<br><span>Systematically managed.</span></h1>
  <p class="hero-sub">Synthos runs a disciplined, rules-based trading strategy on your behalf — continuously, consistently, and without emotion.</p>
</section>

<!-- STATS -->
<div class="stats">
  <div class="stat teal">
    <div class="stat-value">14</div>
    <div class="stat-label">Risk Gates Per Trade</div>
  </div>
  <div class="stat pink">
    <div class="stat-value">3×</div>
    <div class="stat-label">Daily Market Sessions</div>
  </div>
  <div class="stat purple">
    <div class="stat-value">24/7</div>
    <div class="stat-label">System Monitoring</div>
  </div>
  <div class="stat amber">
    <div class="stat-value">0</div>
    <div class="stat-label">Emotional Decisions</div>
  </div>
</div>

<!-- FEATURES -->
<section class="features">
  <div class="section-eyebrow">Platform</div>
  <h2 class="section-title">Built for discipline, not instinct</h2>
  <p class="section-sub">Every trade passes through a structured gate system before execution. No gut calls.</p>
  <div class="feature-grid">
    <div class="feature-card fc-teal">
      <div class="feature-tag">Execution</div>
      <div class="feature-name">Rule-Based Trading</div>
      <div class="feature-desc">A 14-gate decision spine covers momentum, sentiment, risk exposure, and market regime before any order is placed.</div>
    </div>
    <div class="feature-card fc-purple">
      <div class="feature-tag">Intelligence</div>
      <div class="feature-name">Multi-Source Signals</div>
      <div class="feature-desc">News sentiment, sector momentum, and congressional trade data feed into every session before the open.</div>
    </div>
    <div class="feature-card fc-amber">
      <div class="feature-tag">Control</div>
      <div class="feature-name">Supervised or Autonomous</div>
      <div class="feature-desc">Approve every trade manually, or let the system execute fully within your defined risk parameters.</div>
    </div>
    <div class="feature-card fc-pink">
      <div class="feature-tag">Safety</div>
      <div class="feature-name">Instant Kill Switch</div>
      <div class="feature-desc">Halt all trading from your dashboard immediately. Your override is always respected, no exceptions.</div>
    </div>
    <div class="feature-card fc-teal">
      <div class="feature-tag">Visibility</div>
      <div class="feature-name">Live Portfolio View</div>
      <div class="feature-desc">Real-time positions, P&L, and pending approvals visible from your account dashboard at any time.</div>
    </div>
    <div class="feature-card fc-purple">
      <div class="feature-tag">Reliability</div>
      <div class="feature-name">Crash-Proof Infrastructure</div>
      <div class="feature-desc">Auto-restart on failure, encrypted backups, and 24/7 health monitoring keep the system running.</div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<div class="how">
  <div class="how-inner">
    <div class="section-eyebrow" style="text-align:center">Process</div>
    <h2 class="section-title" style="text-align:center">How each session works</h2>
    <div class="steps">
      <div class="step">
        <div class="step-dot"></div>
        <div class="step-num">01 — PRE-MARKET</div>
        <div class="step-title">Market Intelligence</div>
        <div class="step-desc">News, sentiment, and sector data gathered and scored before the session opens.</div>
      </div>
      <div class="step">
        <div class="step-dot"></div>
        <div class="step-num">02 — GATE CHECK</div>
        <div class="step-title">Risk Screening</div>
        <div class="step-desc">Each trade filtered through 14 sequential gates. One failure halts the trade.</div>
      </div>
      <div class="step">
        <div class="step-dot"></div>
        <div class="step-num">03 — EXECUTION</div>
        <div class="step-title">Order Placement</div>
        <div class="step-desc">Approved trades routed to market. You're notified of any action taken.</div>
      </div>
      <div class="step">
        <div class="step-dot"></div>
        <div class="step-num">04 — REPORTING</div>
        <div class="step-title">Session Summary</div>
        <div class="step-desc">Results, positions, and performance metrics updated in your dashboard in real time.</div>
      </div>
    </div>
  </div>
</div>

<!-- FOOTER -->
<footer>
  <div class="footer-logo">SYNTHOS</div>
  <div class="footer-note">Algorithmic trading involves risk. Past performance does not guarantee future results.</div>
</footer>

<script>
function toggleAuthDrop(e){
  e.stopPropagation();
  document.getElementById('auth-drop').classList.toggle('open');
}
function toggleSidebar(){ /* wired in dashboard script */ }
document.addEventListener('click',function(){
  document.getElementById('auth-drop').classList.remove('open');
});
</script>
</body>
</html>"""


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Sign In</title>
<style>
""" + _SHARED_CSS + """
body{display:flex;flex-direction:column;min-height:100vh}
.login-nav{
  padding:1.1rem 2.5rem;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}
.login-nav-logo{font-family:var(--mono);font-size:0.95rem;font-weight:500;letter-spacing:0.18em}
.login-nav-back{font-size:0.78rem;color:var(--muted);font-family:var(--mono)}
.login-nav-back:hover{color:var(--text)}
.login-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:3rem 1.5rem}
.card{
  width:100%;max-width:360px;
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:2.25rem;
}
.card-title{font-size:1.15rem;font-weight:600;margin-bottom:0.3rem;letter-spacing:-0.01em}
.card-sub{font-size:0.82rem;color:var(--muted);margin-bottom:2rem}
label{
  display:block;font-size:0.72rem;font-weight:500;letter-spacing:0.08em;
  text-transform:uppercase;color:var(--muted);margin-bottom:0.35rem;
}
input{
  font-family:var(--mono);font-size:0.88rem;
  width:100%;padding:0.6rem 0.85rem;
  background:#0d0d18;border:1px solid var(--border);border-radius:7px;
  color:var(--text);margin-bottom:1.1rem;transition:border-color .15s;
}
input:focus{outline:none;border-color:var(--accent)}
input::placeholder{color:var(--dim)}
.btn-submit{
  font-family:var(--mono);font-size:0.8rem;font-weight:500;letter-spacing:0.08em;
  width:100%;padding:0.7rem 1rem;
  background:var(--accent);color:#fff;border:none;border-radius:7px;
  cursor:pointer;transition:opacity .15s;margin-top:0.25rem;
}
.btn-submit:hover{opacity:0.88}
.error{
  font-size:0.8rem;font-family:var(--mono);
  background:#2a0f0f;border:1px solid #5c1f1f;color:var(--red);
  padding:0.6rem 0.85rem;border-radius:7px;margin-bottom:1.1rem;
}
footer{
  padding:1.25rem 2.5rem;border-top:1px solid var(--border);
  text-align:center;font-size:0.72rem;color:var(--dim);
}
</style>
</head>
<body>
<nav class="login-nav">
  <a href="/"><div class="login-nav-logo">SYNTHOS</div></a>
  <a href="/" class="login-nav-back">← Back</a>
</nav>
<div class="login-wrap">
  <div class="card">
    <div class="card-title">Welcome back</div>
    <div class="card-sub">Sign in to your Synthos account</div>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="POST" action="/login">
      <label>Email</label>
      <input type="email" name="email" autofocus autocomplete="email" placeholder="you@example.com">
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" placeholder="••••••••">
      <button class="btn-submit" type="submit">Sign In →</button>
    </form>
  </div>
</div>
<footer>Algorithmic trading involves risk. Past performance does not guarantee future results.</footer>
</body>
</html>"""


def is_authenticated():
    """Returns True if the current session has a valid customer_id."""
    return session.get('customer_id') is not None


def is_admin():
    """Returns True if the current session belongs to an admin account."""
    return session.get('role') == 'admin'


@app.before_request
def check_auth():
    # Routes that are always public — no session required
    public_routes = {'/', '/login', '/logout', '/signup', '/verify-email', '/forgot-password', '/sso', '/check-email',
                     '/admin/construction-verify'}
    if request.path in public_routes:
        return
    # Token-based routes are public (the token IS the auth)
    if request.path.startswith('/setup-account/') or request.path.startswith('/verify-email/'):
        return
    # Monitor-callable endpoints — bearer token handled inside the function
    if request.path in {'/api/logs-audit', '/api/get-keys'}:
        return
    # Stripe webhook — authenticated by Stripe signature, not session
    if request.path == '/webhook/stripe':
        return
    if not is_authenticated():
        return redirect('/login')

    # ── Session activity tracking + non-admin auto-logout ──
    _cid = session.get('customer_id')
    if _cid:
        _now = datetime.utcnow()
        with _session_activity_lock:
            _prev = _session_activity.get(_cid)
            # Non-admin: auto-logout after 15 min inactivity
            if _prev and session.get('role') != 'admin':
                _elapsed = (_now - _prev['last_activity']).total_seconds()
                if _elapsed > _CUSTOMER_TIMEOUT.total_seconds():
                    _session_activity.pop(_cid, None)
                    session.clear()
                    return redirect('/login')
            # Update activity timestamp
            _session_activity[_cid] = {
                'last_activity': _now,
                'ip': request.remote_addr or '',
            }


# ── SIGNUP ────────────────────────────────────────────────────────────────────

_SIGNUP_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Create Account</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0c14;--surface:#111520;--surface2:rgba(255,255,255,0.04);
    --border:#1e2535;--border2:rgba(255,255,255,0.08);
    --text:#e0ddd8;--muted:#556;--dim:rgba(255,255,255,0.18);
    --teal:#00f5d4;--pink:#ff4b6e;--amber:#f5a623;
    --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
  .signup-wrap{
    display:flex;align-items:center;justify-content:center;
    min-height:100vh;padding:40px 20px;
    background:
      radial-gradient(ellipse 60% 40% at 50% 0%, rgba(0,245,212,0.05) 0%, transparent 60%),
      radial-gradient(ellipse 40% 30% at 80% 60%, rgba(123,97,255,0.04) 0%, transparent 60%);
  }
  .signup-card{
    width:100%;max-width:420px;
    background:var(--surface);border:1px solid var(--border2);border-radius:16px;
    padding:2rem;box-shadow:0 20px 60px rgba(0,0,0,0.5);
  }
  .signup-logo{
    font-family:var(--mono);font-size:0.85rem;font-weight:600;
    letter-spacing:0.18em;color:var(--teal);text-align:center;margin-bottom:0.5rem;
    text-shadow:0 0 18px rgba(0,245,212,0.20);
  }
  .signup-title{font-size:1.1rem;font-weight:600;text-align:center;margin-bottom:0.3rem}
  .signup-sub{font-size:0.78rem;color:var(--muted);text-align:center;margin-bottom:1.5rem}
  .field{margin-bottom:1rem}
  .field label{
    display:block;font-size:0.62rem;font-weight:500;letter-spacing:0.09em;
    text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem;
  }
  .field input{
    font-family:var(--mono);font-size:0.82rem;width:100%;
    padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);
    border:1px solid var(--border);border-radius:8px;
    color:var(--text);transition:border-color .15s;
  }
  .field input:focus{outline:none;border-color:rgba(0,245,212,0.25);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
  .field input::placeholder{color:var(--dim)}
  .field-row{display:flex;gap:12px}
  .field-row .field{flex:1}
  .submit-btn{
    font-family:var(--mono);font-size:0.78rem;font-weight:600;letter-spacing:0.05em;
    width:100%;padding:0.65rem;margin-top:0.5rem;
    background:rgba(0,245,212,0.12);color:var(--teal);
    border:1px solid rgba(0,245,212,0.18);border-radius:8px;
    cursor:pointer;transition:all .15s;
  }
  .submit-btn:hover{background:rgba(0,245,212,0.18);box-shadow:0 0 14px rgba(0,245,212,0.09)}
  .submit-btn:disabled{opacity:0.4;cursor:not-allowed}
  .error-msg{
    background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.18);
    border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:1rem;
    font-size:0.78rem;color:var(--pink);
  }
  .success-msg{
    background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.14);
    border-radius:8px;padding:0.75rem;margin-bottom:1rem;
    font-size:0.82rem;color:var(--teal);text-align:center;line-height:1.6;
  }
  .back-link{
    display:block;text-align:center;margin-top:1.2rem;
    font-size:0.72rem;color:var(--muted);text-decoration:none;
  }
  .back-link:hover{color:var(--text)}
  .code-note{font-size:0.68rem;color:var(--dim);margin-top:4px}
</style>
</head>
<body>
<div class="signup-wrap">
  <div class="signup-card">
    <div class="signup-logo">SYNTHOS</div>
    <div class="signup-title">Create Account</div>
    <div class="signup-sub">Enter your details to request access</div>

    {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}
    {% if success %}
      <div class="success-msg">
        Your signup request has been submitted.<br>
        You will receive access once an administrator approves your account.
      </div>
    {% else %}
    <form method="POST" action="/signup" autocomplete="off">
      <div class="field">
        <label>Access Code</label>
        <input type="text" name="access_code" placeholder="Enter your invite code" required
               value="{{ request.form.get('access_code', '') }}">
        <div class="code-note">Contact the operator for an invite code</div>
      </div>
      <div class="field">
        <label>Full Name</label>
        <input type="text" name="name" placeholder="Jane Smith" required
               value="{{ request.form.get('name', '') }}">
      </div>
      <div class="field">
        <label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" required
               value="{{ request.form.get('email', '') }}">
      </div>
      <div class="field">
        <label>Phone <span style="font-size:9px;color:rgba(255,255,255,0.3)">(optional)</span></label>
        <input type="tel" name="phone" placeholder="+1 (555) 000-0000"
               value="{{ request.form.get('phone', '') }}">
      </div>
      <div class="field-row">
        <div class="field">
          <label>State</label>
          <select name="state" required style="width:100%;padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:7px;color:rgba(255,255,255,0.88);font-family:inherit;font-size:0.82rem">
            <option value="">Select state...</option>
            <option value="GA" {{ 'selected' if request.form.get('state') == 'GA' else '' }}>Georgia</option>
          </select>
        </div>
        <div class="field">
          <label>Zip Code</label>
          <input type="text" name="zip_code" placeholder="30301" required pattern="[0-9]{5}"
                 maxlength="5" value="{{ request.form.get('zip_code', '') }}">
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Password</label>
          <input type="password" name="password" placeholder="Min 8 characters" required minlength="8">
        </div>
        <div class="field">
          <label>Confirm</label>
          <input type="password" name="confirm_password" placeholder="Re-enter" required minlength="8">
        </div>
      </div>
      <button class="submit-btn" type="submit">Request Access &rarr;</button>
    </form>
    {% endif %}
    <a href="/" class="back-link">&larr; Back to Synthos</a>
  </div>
</div>
</body>
</html>
"""



@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    """Placeholder forgot-password page — collects email. Reset mechanism TBD."""
    submitted = False
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if email:
            log.info(f"Password reset requested for: {email}")
            submitted = True

    return render_template_string("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Reset Password</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0c14;--surface:#111520;--surface2:rgba(255,255,255,0.04);
    --border:#1e2535;--border2:rgba(255,255,255,0.08);
    --text:#e0ddd8;--muted:#556;--dim:rgba(255,255,255,0.18);
    --teal:#00f5d4;--pink:#ff4b6e;
    --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
  .wrap{
    display:flex;align-items:center;justify-content:center;
    min-height:100vh;padding:40px 20px;
    background:radial-gradient(ellipse 60% 40% at 50% 0%, rgba(0,245,212,0.05) 0%, transparent 60%);
  }
  .card{
    width:100%;max-width:380px;
    background:var(--surface);border:1px solid var(--border2);border-radius:16px;
    padding:2rem;box-shadow:0 20px 60px rgba(0,0,0,0.5);
  }
  .logo{font-family:var(--mono);font-size:0.85rem;font-weight:600;letter-spacing:0.18em;color:var(--teal);text-align:center;margin-bottom:0.5rem;text-shadow:0 0 18px rgba(0,245,212,0.20)}
  .title{font-size:1rem;font-weight:600;text-align:center;margin-bottom:0.3rem}
  .sub{font-size:0.78rem;color:var(--muted);text-align:center;margin-bottom:1.5rem;line-height:1.6}
  label{display:block;font-size:0.62rem;font-weight:500;letter-spacing:0.09em;text-transform:uppercase;color:var(--muted);margin-bottom:0.3rem}
  input{font-family:var(--mono);font-size:0.82rem;width:100%;padding:0.5rem 0.7rem;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:8px;color:var(--text);margin-bottom:1rem}
  input:focus{outline:none;border-color:rgba(0,245,212,0.25);box-shadow:0 0 0 3px rgba(0,245,212,0.06)}
  input::placeholder{color:var(--dim)}
  .btn{font-family:var(--mono);font-size:0.78rem;font-weight:600;width:100%;padding:0.65rem;background:rgba(0,245,212,0.12);color:var(--teal);border:1px solid rgba(0,245,212,0.18);border-radius:8px;cursor:pointer;transition:all .15s}
  .btn:hover{background:rgba(0,245,212,0.18)}
  .success{background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.14);border-radius:8px;padding:0.75rem;font-size:0.82rem;color:var(--teal);text-align:center;line-height:1.6}
  .back{display:block;text-align:center;margin-top:1.2rem;font-size:0.72rem;color:var(--muted);text-decoration:none}
  .back:hover{color:var(--text)}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="logo">SYNTHOS</div>
    <div class="title">Reset Password</div>
    <div class="sub">Enter your email address and we'll send you instructions to reset your password.</div>
    {% if submitted %}
      <div class="success">
        If an account exists for that email, you'll receive reset instructions shortly.
      </div>
    {% else %}
    <form method="POST" action="/forgot-password">
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required>
      <button class="btn" type="submit">Send Reset Instructions &rarr;</button>
    </form>
    {% endif %}
    <a href="/" class="back">&larr; Back to Synthos</a>
  </div>
</div>
</body>
</html>""", submitted=submitted)


@app.route('/signup', methods=['GET', 'POST'])
def signup_page():
    """Public signup page — validates access code, stores pending signup for admin approval."""
    error   = None
    success = False

    if request.method == 'POST':
        code     = request.form.get('access_code', '').strip()
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip()
        phone    = request.form.get('phone', '').strip()
        state    = request.form.get('state', '').strip().upper()
        zip_code = request.form.get('zip_code', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')

        if not auth.verify_signup_access_code(code):
            error = "Invalid access code. Contact the operator for an invite code."
        elif not name or not email:
            error = "Name and email are required."
        elif state != 'GA':
            error = "Synthos is currently available to Georgia residents only. More states coming soon."
        elif not zip_code or len(zip_code) != 5 or not zip_code.isdigit():
            error = "Please enter a valid 5-digit zip code."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            try:
                _sid = auth.create_pending_signup(name, email, phone, password, state=state, zip_code=zip_code)
                # Send email verification link
                try:
                    _vtoken = auth.generate_signup_verify_token(_sid)
                    _send_verification_email(email, name, _vtoken)
                except Exception as _ve:
                    log.warning(f"Verification email failed: {_ve}")
                success = True
                log.info(f"Signup submitted: {email} (verification email sent)")
            except ValueError as e:
                error = str(e)
            except Exception as e:
                log.error(f"Signup error: {e}")
                error = "An unexpected error occurred. Please try again."

    return render_template_string(_SIGNUP_PAGE_HTML, error=error, success=success)


# ── SIGNUP MANAGEMENT API (admin only) ────────────────────────────────────────

@app.route('/api/pending-signups', methods=['GET'])
@login_required
def api_pending_signups():
    """List pending signups for admin approval."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    status_filter = request.args.get('status')
    signups = auth.list_pending_signups(status_filter)
    return jsonify({"signups": signups})


@app.route('/api/approve-signup', methods=['POST'])
@login_required
def api_approve_signup():
    """Approve a pending signup — creates customer account + signals.db."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(force=True)
    signup_id = data.get('signup_id')
    if not signup_id:
        return jsonify({"error": "signup_id required"}), 400
    try:
        result = auth.approve_signup(int(signup_id), reviewed_by=session.get('customer_id', 'admin'))
        from retail_database import get_customer_db
        get_customer_db(result['customer_id'])
        log.info(f"Admin approved signup #{signup_id} -> customer {result['customer_id']}")

        # Send approval email
        try:
            _send_approval_email(result.get('email', ''), result.get('name', ''))
        except Exception as _ae:
            log.warning(f"Approval email failed: {_ae}")

        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error(f"Approve signup error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/reject-signup', methods=['POST'])
@login_required
def api_reject_signup():
    """Reject a pending signup."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(force=True)
    signup_id = data.get('signup_id')
    if not signup_id:
        return jsonify({"error": "signup_id required"}), 400
    try:
        auth.reject_signup(int(signup_id), reviewed_by=session.get('customer_id', 'admin'))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/generate-invite', methods=['POST'])
@login_required
def api_generate_invite():
    """Generate a one-time invite code (admin only)."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    try:
        code = auth.generate_invite_code(created_by=session.get('customer_id', 'admin'))
        return jsonify({"ok": True, "code": code})
    except Exception as e:
        log.error(f"Generate invite error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/invite-codes', methods=['GET'])
@login_required
def api_invite_codes():
    """List all invite codes (admin only)."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    codes = auth.list_invite_codes()
    return jsonify({"codes": codes})



# ── NOTIFICATION API ──────────────────────────────────────────────────────────

@app.route('/api/notifications', methods=['GET'])
@login_required
def api_notifications():
    """Fetch notifications for the current customer."""
    db = _customer_db()
    unread_only = request.args.get('unread_only') == '1'
    category = request.args.get('category')
    limit = min(int(request.args.get('limit', 50)), 200)
    notifs = db.get_notifications(limit=limit, unread_only=unread_only, category=category)
    return jsonify({"notifications": notifs})


@app.route('/api/notifications/unread-count', methods=['GET'])
@login_required
def api_notifications_unread_count():
    """Lightweight unread count for badge polling."""
    db = _customer_db()
    count = db.get_unread_count()
    return jsonify({"count": count})


@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_notifications_read():
    """Mark notification(s) as read."""
    db = _customer_db()
    data = request.get_json(force=True)
    if data.get('all'):
        category = data.get('category')
        db.mark_all_notifications_read(category=category)
        return jsonify({"ok": True})
    notif_id = data.get('id')
    if not notif_id:
        return jsonify({"error": "id or all required"}), 400
    db.mark_notification_read(int(notif_id))
    return jsonify({"ok": True})


@app.route('/api/notifications/send', methods=['POST'])
@login_required
def api_notifications_send():
    """Send a notification to a specific customer. Admin only."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(force=True)
    customer_id = data.get('customer_id')
    category    = data.get('category', 'system')
    title       = data.get('title', '')
    body        = data.get('body', '')
    meta        = data.get('meta')
    if not customer_id or not title:
        return jsonify({"error": "customer_id and title required"}), 400
    from retail_database import get_customer_db
    db = get_customer_db(customer_id)
    nid = db.add_notification(category, title, body, meta)
    return jsonify({"ok": True, "id": nid})


@app.route('/api/notifications/broadcast', methods=['POST'])
@login_required
def api_notifications_broadcast():
    """Send a system notification to ALL active customers. Admin only."""
    if not is_admin():
        return jsonify({"error": "admin only"}), 403
    data = request.get_json(force=True)
    category = data.get('category', 'system')
    title    = data.get('title', '')
    body     = data.get('body', '')
    meta     = data.get('meta')
    if not title:
        return jsonify({"error": "title required"}), 400
    from retail_database import get_customer_db
    customers = auth.list_customers()
    sent = 0
    for c in customers:
        if c.get('is_active'):
            try:
                db = get_customer_db(c['id'])
                db.add_notification(category, title, body, meta)
                sent += 1
            except Exception as e:
                log.warning(f"Broadcast skip {c['id']}: {e}")
    log.info(f"Broadcast notification to {sent} customers: {title[:60]}")
    return jsonify({"ok": True, "sent": sent})



@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_authenticated():
        return redirect('/admin' if is_admin() else '/')

    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
        if _rate_limited(_login_attempts, ip, _LOGIN_MAX, _LOGIN_WINDOW):
            log.warning("Login rate limit hit for IP %s", ip)
            return render_template_string(LOGIN_HTML,
                error="Too many login attempts — please wait a few minutes.")

        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        # ── Primary: account-based auth via auth.db ──
        if email:
            try:
                customer = auth.get_customer_by_email(email)
                if customer and auth.verify_password(password, customer['password_hash']):
                    # ── Access gate: subscription + email verification ──────────
                    allowed, reason = auth.is_access_allowed(customer['id'], customer['role'])
                    if not allowed:
                        if reason == 'unverified':
                            # Account created but setup link not yet completed
                            return redirect('/check-email?reason=unverified')
                        elif reason in ('past_due', 'inactive', 'cancelled'):
                            return redirect('/subscribe?reason=' + reason)
                        else:
                            log.warning(f"Login denied: {customer['id']} reason={reason}")
                            return render_template_string(LOGIN_HTML, error="Account access denied. Contact support.")

                    session.clear()
                    session['customer_id']  = customer['id']
                    session['role']         = customer['role']
                    session['display_name'] = auth.get_display_name(customer)
                    session['access_reason']= reason   # 'active'|'trialing'|'grace_period'|'admin'
                    session['tos_version']  = customer['tos_version']
                    session.permanent       = True
                    auth.record_login(customer['id'])
                    log.info(f"Login: {customer['id']} (role={customer['role']} access={reason})")
                    return redirect('/')
            except Exception as e:
                log.error(f"Auth error during login: {e}")

        return render_template_string(LOGIN_HTML, error="Incorrect email or password")

    return render_template_string(LOGIN_HTML, error=None)


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── TERMS OF SERVICE ──────────────────────────────────────────────────────

_TERMS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — Terms of Service</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f5f0e8;color:#1a1612;font-family:'IBM Plex Sans',sans-serif;
     min-height:100vh;display:flex;flex-direction:column;align-items:center;
     justify-content:flex-start;padding:40px 20px}
.wordmark{font-family:'IBM Plex Mono',monospace;font-size:0.9rem;font-weight:600;
          letter-spacing:0.12em;color:#1a1612;margin-bottom:32px;opacity:0.5}
.card{background:#ede8df;border:1px solid #c8bfaa;border-radius:4px;
      padding:2rem;width:100%;max-width:620px}
h1{font-family:'IBM Plex Mono',monospace;font-size:1rem;font-weight:600;
   letter-spacing:0.06em;margin-bottom:6px}
.version{font-size:0.72rem;color:#7a7060;margin-bottom:24px;
         font-family:'IBM Plex Mono',monospace;letter-spacing:0.04em}
.tos-body{background:#f5f0e8;border:1px solid #c8bfaa;border-radius:2px;
          padding:1.25rem 1.5rem;max-height:340px;overflow-y:auto;
          font-size:0.82rem;line-height:1.8;color:#2a2420;margin-bottom:24px}
.tos-body p{margin-bottom:1rem}
.tos-body p:last-child{margin-bottom:0}
.tos-body strong{font-weight:600;color:#1a1612}
.accept-row{display:flex;align-items:flex-start;gap:10px;margin-bottom:20px}
.accept-row input[type=checkbox]{margin-top:3px;accent-color:#1a1612;
                                  width:15px;height:15px;flex-shrink:0;cursor:pointer}
.accept-row label{font-size:0.82rem;color:#2a2420;line-height:1.5;cursor:pointer}
button[type=submit]{width:100%;background:#1a1612;color:#f5f0e8;border:none;
                    border-radius:2px;padding:11px 20px;
                    font-family:'IBM Plex Mono',monospace;font-size:0.82rem;
                    font-weight:600;letter-spacing:0.1em;cursor:pointer;
                    text-transform:uppercase;transition:opacity 0.15s}
button[type=submit]:hover{opacity:0.85}
button[type=submit]:disabled{opacity:0.35;cursor:not-allowed}
.meta{font-size:0.7rem;color:#7a7060;margin-top:16px;text-align:center;
      font-family:'IBM Plex Mono',monospace;letter-spacing:0.04em}
</style>
</head>
<body>
<div class="wordmark">SYNTHOS</div>
<div class="card">
  <h1>Terms of Service</h1>
  <div class="version">Version {{ version }} &nbsp;·&nbsp; Review before continuing</div>

  <div class="tos-body">
    <p><strong>PLACEHOLDER — Terms of Service content will be added here.</strong></p>
    <p>This document will contain the full Synthos Terms of Service, including
    provisions covering acceptable use, risk disclosure, limitations of liability,
    and operator responsibilities.</p>
    <p>By accepting, you confirm you have read and agree to the terms as they will
    appear in the final document. This placeholder acceptance is recorded with a
    timestamp and version number.</p>
    <p>The document content is intentionally left blank during this development phase.
    Do not distribute to end customers until final terms are drafted and reviewed.</p>
  </div>

  {% if error %}
  <div style="color:#8b2200;font-size:0.78rem;margin-bottom:14px;padding:8px 12px;
              background:#fdf0ed;border:1px solid #c8bfaa;border-radius:2px">
    {{ error }}
  </div>
  {% endif %}

  <form method="POST" action="/terms">
    <div class="accept-row">
      <input type="checkbox" id="accepted" name="accepted" value="yes" required>
      <label for="accepted">
        I have read and agree to the Synthos Terms of Service (v{{ version }}).
        I understand that trading involves risk of financial loss and that Synthos
        is not a licensed financial advisor.
      </label>
    </div>
    <button type="submit">Accept &amp; Continue →</button>
  </form>

  <div class="meta">Acceptance is recorded with timestamp and version number.</div>
</div>
</body>
</html>"""


def _write_tos_acceptance_file(customer_id: str, version: str, ip: str, ua: str) -> None:
    """
    Write a JSON acceptance record to data/customers/<id>/agreements/tos_v<version>.json.
    Directory is created if it does not exist.
    File is chmod 600.
    """
    import json as _json
    agreements_dir = os.path.join(_CUSTOMERS_DIR, customer_id, 'agreements')
    os.makedirs(agreements_dir, exist_ok=True)
    filename = f"tos_v{version}.json"
    filepath = os.path.join(agreements_dir, filename)
    record = {
        "document":    "synthos_terms_of_service",
        "version":     version,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
        "ip_address":  ip or "unknown",
        "user_agent":  (ua or "unknown")[:200],
    }
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            _json.dump(record, f, indent=2)
        os.chmod(filepath, 0o600)
        log.info("ToS acceptance filed: %s", filepath)
    except OSError as exc:
        log.error("Failed to write ToS acceptance file: %s", exc)


@app.route('/terms', methods=['GET'])
@authenticated_only
def terms_get():
    """Show Terms of Service page. Accessible to authenticated users regardless of ToS status."""
    # If already accepted current version, skip straight through
    if session.get('tos_version') == TOS_CURRENT_VERSION:
        return redirect('/')
    from flask import render_template_string
    return render_template_string(_TERMS_HTML, version=TOS_CURRENT_VERSION, error=None)


@app.route('/terms', methods=['POST'])
@authenticated_only
def terms_post():
    """Record ToS acceptance, write agreement file, update session."""
    from flask import render_template_string
    if request.form.get('accepted') != 'yes':
        return render_template_string(
            _TERMS_HTML, version=TOS_CURRENT_VERSION,
            error="You must check the box to continue."
        )

    customer_id = session['customer_id']
    ip  = request.headers.get('X-Forwarded-For', request.remote_addr or '')
    ua  = request.headers.get('User-Agent', '')

    # 1. Write to auth.db
    auth.mark_tos_accepted(customer_id, TOS_CURRENT_VERSION)

    # 2. Write agreement file to customer folder
    _write_tos_acceptance_file(customer_id, TOS_CURRENT_VERSION, ip, ua)

    # 3. Update session so login_required passes on next request
    session['tos_version'] = TOS_CURRENT_VERSION

    log.info("ToS v%s accepted by customer %s", TOS_CURRENT_VERSION, customer_id)
    return redirect('/')


# ── CUSTOMER ACQUISITION PIPELINE ROUTES ─────────────────────────────────
# These routes form the customer onboarding flow:
#   /subscribe              → pricing / sign-up gate (construction-locked until launch)
#   /check-email            → holding page shown after account creation
#   /setup-account/<token>  → password setup form (token from welcome email)
#   /verify-email/<token>   → alias for setup-account (legacy compatibility)
#   /webhook/stripe         → Stripe payment webhook (wired when Stripe is integrated)
#
# The grace-period warning banner is injected into the customer dashboard
# when session['access_reason'] == 'grace_period'.


_CONSTRUCTION_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synthos — Coming Soon</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; }
    .card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 56px 48px; max-width: 480px; width: 100%; text-align: center; }
    .wordmark { font-size: 28px; font-weight: 700; letter-spacing: 6px;
                color: #fff; margin-bottom: 12px; }
    .tagline { color: #888; font-size: 13px; letter-spacing: 2px; margin-bottom: 40px; }
    .status { background: #1e1e2e; border: 1px solid #333; border-radius: 8px;
              padding: 24px; margin-bottom: 32px; }
    .dot { width: 10px; height: 10px; background: #f59e0b; border-radius: 50%;
           display: inline-block; margin-right: 8px; animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
    .status-text { font-size: 15px; color: #ccc; }
    .sub { color: #666; font-size: 12px; margin-top: 24px; line-height: 1.6; }
  </style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="tagline">Algorithmic Trading Platform</div>
  <div class="status">
    <span class="dot"></span>
    <span class="status-text">Platform launching soon</span>
  </div>
  <p class="sub">
    We're putting the finishing touches on things.<br>
    Check back shortly — or email us at synthos.signal@gmail.com<br>
    to be notified when we go live.
  </p>
</div>
</body>
</html>"""


_CHECK_EMAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synthos — Check Your Email</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 56px 48px; max-width: 480px; width: 100%; text-align: center; }
    .wordmark { font-size: 28px; font-weight: 700; letter-spacing: 6px;
                color: #fff; margin-bottom: 12px; }
    .tagline { color: #888; font-size: 13px; letter-spacing: 2px; margin-bottom: 40px; }
    .icon { font-size: 48px; margin-bottom: 24px; }
    h2 { font-size: 18px; font-weight: 600; margin-bottom: 12px; color: #e0e0e0; }
    p { color: #999; font-size: 13px; line-height: 1.7; margin-bottom: 12px; }
    .note { background: #1e1e2e; border: 1px solid #333; border-radius: 8px;
            padding: 16px; margin-top: 24px; font-size: 12px; color: #666; }
    a { color: #6b8cff; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="tagline">Algorithmic Trading Platform</div>
  <div class="icon">📬</div>
  <h2>Check your inbox</h2>
  <p>We've sent a setup link to your email address.<br>
     Click the link to activate your account.</p>
  <p>The link expires in 48 hours.</p>
  <div class="note">
    Didn't receive it? Check your spam folder, or
    <a href="mailto:synthos.signal@gmail.com">contact support</a>.
  </div>
</div>
</body>
</html>"""


_SETUP_ACCOUNT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synthos — Set Your Password</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 56px 48px; max-width: 420px; width: 100%; }
    .wordmark { font-size: 28px; font-weight: 700; letter-spacing: 6px;
                color: #fff; margin-bottom: 8px; text-align: center; }
    .tagline { color: #888; font-size: 13px; letter-spacing: 2px;
               text-align: center; margin-bottom: 40px; }
    h2 { font-size: 17px; font-weight: 600; margin-bottom: 8px; }
    p { color: #888; font-size: 12px; margin-bottom: 28px; line-height: 1.6; }
    label { display: block; font-size: 11px; letter-spacing: 1px; color: #666;
            text-transform: uppercase; margin-bottom: 6px; }
    input { width: 100%; background: #1e1e2e; border: 1px solid #333; border-radius: 6px;
            padding: 12px 14px; color: #e0e0e0; font-family: inherit; font-size: 14px;
            margin-bottom: 20px; outline: none; }
    input:focus { border-color: #6b8cff; }
    button { width: 100%; background: #4f46e5; color: #fff; border: none;
             border-radius: 6px; padding: 13px; font-family: inherit; font-size: 14px;
             font-weight: 600; cursor: pointer; letter-spacing: 1px; }
    button:hover { background: #6b8cff; }
    .error { background: #2a1515; border: 1px solid #7f2020; border-radius: 6px;
             padding: 12px; color: #f87171; font-size: 13px; margin-bottom: 20px; }
    .rule { font-size: 11px; color: #555; margin-top: -12px; margin-bottom: 20px; }
  </style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="tagline">Algorithmic Trading Platform</div>
  <h2>Set your password</h2>
  <p>Choose a strong password to complete your account setup.
     You'll use this to log in at synth-cloud.com</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>Password</label>
    <input type="password" name="password" autofocus autocomplete="new-password"
           placeholder="At least 12 characters" minlength="12">
    <div class="rule">Minimum 12 characters</div>
    <label>Confirm Password</label>
    <input type="password" name="confirm" autocomplete="new-password"
           placeholder="Re-enter your password">
    <button type="submit">ACTIVATE ACCOUNT →</button>
  </form>
</div>
</body>
</html>"""


_SUBSCRIBE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synthos — Subscribe</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 56px 48px; max-width: 480px; width: 100%; text-align: center; }
    .wordmark { font-size: 28px; font-weight: 700; letter-spacing: 6px;
                color: #fff; margin-bottom: 12px; }
    .tagline { color: #888; font-size: 13px; letter-spacing: 2px; margin-bottom: 40px; }
    .notice { background: #1e1e2e; border: 1px solid #333; border-radius: 8px;
              padding: 24px; margin-bottom: 24px; }
    .notice p { color: #ccc; font-size: 14px; line-height: 1.7; }
    .reason { background: #2a1a00; border: 1px solid #7a4a00; border-radius: 8px;
              padding: 14px 18px; font-size: 13px; color: #f59e0b; margin-bottom: 24px; }
    a { color: #6b8cff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .sub { color: #555; font-size: 12px; margin-top: 24px; }
  </style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="tagline">Algorithmic Trading Platform</div>
  {% if reason == 'past_due' %}
  <div class="reason">⚠ Payment required — your grace period has ended</div>
  {% elif reason == 'cancelled' %}
  <div class="reason">Your subscription has been cancelled</div>
  {% else %}
  <div class="reason">A subscription is required to access the platform</div>
  {% endif %}
  <div class="notice">
    <p>Subscription management is coming soon.<br>
       Contact <a href="mailto:synthos.signal@gmail.com">synthos.signal@gmail.com</a>
       to reactivate your account or for billing questions.</p>
  </div>
  <p class="sub"><a href="/login">← Back to login</a></p>
</div>
</body>
</html>"""


_CONSTRUCTION_VERIFY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Synthos — Construction Access</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
            padding: 56px 48px; max-width: 420px; width: 100%; }
    .wordmark { font-size: 28px; font-weight: 700; letter-spacing: 6px;
                color: #fff; margin-bottom: 8px; text-align: center; }
    .tagline { color: #888; font-size: 13px; letter-spacing: 2px;
               text-align: center; margin-bottom: 40px; }
    h2 { font-size: 16px; font-weight: 600; margin-bottom: 8px; }
    p { color: #888; font-size: 12px; margin-bottom: 28px; line-height: 1.6; }
    label { display: block; font-size: 11px; letter-spacing: 1px; color: #666;
            text-transform: uppercase; margin-bottom: 6px; }
    input { width: 100%; background: #1e1e2e; border: 1px solid #333; border-radius: 6px;
            padding: 12px 14px; color: #e0e0e0; font-family: inherit; font-size: 20px;
            letter-spacing: 8px; text-align: center; margin-bottom: 20px; outline: none; }
    input:focus { border-color: #f59e0b; }
    .send-btn { background: none; border: 1px solid #444; border-radius: 6px;
                padding: 10px 16px; color: #888; font-family: inherit; font-size: 12px;
                cursor: pointer; margin-bottom: 20px; width: 100%; }
    .send-btn:hover { border-color: #666; color: #ccc; }
    button[type=submit] { width: 100%; background: #92400e; color: #fff; border: none;
             border-radius: 6px; padding: 13px; font-family: inherit; font-size: 14px;
             font-weight: 600; cursor: pointer; letter-spacing: 1px; }
    button[type=submit]:hover { background: #b45309; }
    .error { background: #2a1515; border: 1px solid #7f2020; border-radius: 6px;
             padding: 12px; color: #f87171; font-size: 13px; margin-bottom: 20px; }
    .info { background: #1a1a2e; border: 1px solid #333; border-radius: 6px;
            padding: 12px; color: #6b8cff; font-size: 13px; margin-bottom: 20px; }
  </style>
</head>
<body>
<div class="card">
  <div class="wordmark">SYNTHOS</div>
  <div class="tagline">Construction Access</div>
  <h2>Admin verification required</h2>
  <p>This route is under construction. An access code has been sent to the admin
     email address. Enter the 6-digit code below to proceed.</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  {% if sent %}<div class="info">✓ Code sent to admin email — valid for 10 minutes</div>{% endif %}
  <form method="POST">
    <label>Access Code</label>
    <input type="text" name="otp" autofocus maxlength="6" placeholder="000000"
           inputmode="numeric" autocomplete="one-time-code">
    <button type="submit">VERIFY →</button>
  </form>
  <br>
  <form method="POST" action="/admin/construction-send-otp">
    <button type="submit" class="send-btn">Resend code to admin email</button>
  </form>
</div>
</body>
</html>"""


@app.route('/check-email')
def check_email_page():
    """Holding page shown after account creation — instructs customer to check inbox."""
    return render_template_string(_CHECK_EMAIL_HTML), 200


@app.route('/setup-account/<token>', methods=['GET', 'POST'])
def setup_account(token):
    """
    Password setup page. Token is single-use, 48h expiry.
    GET  — shows password setup form
    POST — validates passwords, calls auth.activate_account(), redirects to login
    """
    customer = auth.consume_verify_token(token)
    if not customer:
        return render_template_string(_SETUP_ACCOUNT_HTML,
                                      error="This setup link is invalid or has expired. "
                                            "Contact support for a new link."), 400

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')

        if len(password) < 12:
            return render_template_string(_SETUP_ACCOUNT_HTML,
                                          error="Password must be at least 12 characters.")
        if password != confirm:
            return render_template_string(_SETUP_ACCOUNT_HTML,
                                          error="Passwords do not match.")

        try:
            auth.activate_account(customer['id'], password)
            log.info(f"Account activated via setup link: {customer['id']}")
            return redirect('/login?activated=1')
        except Exception as e:
            log.error(f"Account activation failed: {e}")
            return render_template_string(_SETUP_ACCOUNT_HTML,
                                          error="Activation failed — please try again or contact support.")

    return render_template_string(_SETUP_ACCOUNT_HTML, error=None)


# Email verification result pages
_VERIFY_SUCCESS_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Email Verified</title>'
    '<style>*{box-sizing:border-box;margin:0;padding:0}'
    'body{min-height:100vh;background:#0a0c14;color:rgba(255,255,255,0.88);'
    'font-family:Inter,sans-serif;display:flex;align-items:center;'
    'justify-content:center;padding:2rem}'
    '.card{max-width:400px;background:#111520;border:1px solid rgba(255,255,255,0.07);'
    'border-radius:14px;padding:2.5rem;text-align:center}'
    '.icon{font-size:48px;margin-bottom:1rem;color:#00f5d4}'
    '.t{font-size:1.2rem;font-weight:700;margin-bottom:.5rem;color:#00f5d4}'
    '.s{font-size:.85rem;color:rgba(255,255,255,0.5);line-height:1.6}'
    '</style></head><body><div class="card">'
    '<div class="icon">&#10003;</div>'
    '<div class="t">Email Verified</div>'
    '<div class="s">Your email has been confirmed. Your signup is pending admin review.</div>'
    '</div></body></html>'
)

_VERIFY_ERROR_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Verification Failed</title>'
    '<style>*{box-sizing:border-box;margin:0;padding:0}'
    'body{min-height:100vh;background:#0a0c14;color:rgba(255,255,255,0.88);'
    'font-family:Inter,sans-serif;display:flex;align-items:center;'
    'justify-content:center;padding:2rem}'
    '.card{max-width:400px;background:#111520;border:1px solid rgba(255,255,255,0.07);'
    'border-radius:14px;padding:2.5rem;text-align:center}'
    '.icon{font-size:48px;margin-bottom:1rem;color:#ff4b6e}'
    '.t{font-size:1.2rem;font-weight:700;margin-bottom:.5rem;color:#ff4b6e}'
    '.s{font-size:.85rem;color:rgba(255,255,255,0.5);line-height:1.6}'
    '</style></head><body><div class="card">'
    '<div class="icon">&#10007;</div>'
    '<div class="t">Verification Failed</div>'
    '<div class="s">ERROR_MSG</div>'
    '</div></body></html>'
)


@app.route('/verify-email/<token>')
def verify_email(token):
    """Public route — verifies signup email from the link sent via Resend."""
    try:
        import auth as _auth
        result = _auth.verify_signup_email(token)
        return _VERIFY_SUCCESS_HTML
    except ValueError as e:
        # Not a signup verification token — try setup-account redirect (legacy)
        return redirect(f'/setup-account/{token}')
    except Exception as e:
        return _VERIFY_ERROR_HTML.replace("ERROR_MSG", "An unexpected error occurred."), 500


@app.route('/subscribe')
def subscribe_page():
    """
    Subscription gate page. Shown when a customer's access is denied due to
    inactive/cancelled/past_due subscription.
    Construction-locked: admin must pass OTP to see the full version.
    Public visitors in construction mode see the coming-soon page.
    """
    reason = request.args.get('reason', 'inactive')
    if CONSTRUCTION_MODE and not (is_authenticated() and is_admin()):
        return render_template_string(_CONSTRUCTION_PAGE_HTML), 200
    return render_template_string(_SUBSCRIBE_HTML, reason=reason)


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """
    Stripe webhook receiver.

    Verifies the Stripe-Signature header using STRIPE_WEBHOOK_SECRET (stdlib
    HMAC-SHA256 — no stripe SDK required).

    Events handled:
      checkout.session.completed    → create_unverified_customer()
                                      + generate_verify_token()
                                      + _send_setup_email()
      invoice.payment_succeeded     → update_subscription(status='active')
      invoice.payment_failed        → mark_grace_period() (7-day window)
      customer.subscription.deleted → update_subscription(status='cancelled')

    All other event types are acknowledged with 200 and ignored.
    Stripe requires a 200 within 30s or it retries — heavy work runs inline
    (Pi is idle during non-market hours; acceptable latency).
    """
    import hmac
    import hashlib

    payload   = request.data          # raw bytes — must not use request.json
    sig_header = request.headers.get('Stripe-Signature', '')

    # ── Signature verification ────────────────────────────────────────────────
    if not STRIPE_WEBHOOK_SECRET:
        log.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET not set — rejecting")
        return jsonify({"error": "webhook secret not configured"}), 500

    try:
        # Stripe-Signature format: t=<timestamp>,v1=<sig>[,v1=<sig2>...]
        parts     = {k: v for k, v in (p.split('=', 1) for p in sig_header.split(',')
                                        if '=' in p)}
        timestamp = parts.get('t', '')
        v1_sigs   = [v for k, v in parts.items() if k == 'v1']

        if not timestamp or not v1_sigs:
            log.warning("Stripe webhook: malformed Stripe-Signature header")
            return jsonify({"error": "invalid signature header"}), 400

        # Guard against replay attacks (5-minute tolerance)
        import time as _time
        try:
            ts_age = abs(_time.time() - int(timestamp))
            if ts_age > 300:
                log.warning(f"Stripe webhook: timestamp too old ({ts_age:.0f}s) — replay?")
                return jsonify({"error": "timestamp too old"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "invalid timestamp"}), 400

        signed_payload = f"{timestamp}.".encode() + payload
        expected_sig = hmac.new(
            STRIPE_WEBHOOK_SECRET.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        if not any(hmac.compare_digest(expected_sig, sig) for sig in v1_sigs):
            log.warning("Stripe webhook: signature mismatch — rejecting")
            return jsonify({"error": "signature verification failed"}), 400

    except Exception as e:
        log.error(f"Stripe webhook signature check error: {e}")
        return jsonify({"error": "signature check failed"}), 400

    # ── Parse event ───────────────────────────────────────────────────────────
    try:
        event      = json.loads(payload)
        event_type = event.get('type', '')
        obj        = event.get('data', {}).get('object', {})
    except Exception as e:
        log.error(f"Stripe webhook: could not parse JSON body: {e}")
        return jsonify({"error": "invalid JSON"}), 400

    log.info(f"Stripe webhook: {event_type} id={event.get('id','?')[:20]}")

    # ── Event handlers ────────────────────────────────────────────────────────

    if event_type == 'checkout.session.completed':
        # New subscriber — create account, email setup link
        stripe_customer_id = obj.get('customer', '')
        stripe_sub_id      = obj.get('subscription', '')
        customer_email     = (
            obj.get('customer_details', {}).get('email')
            or obj.get('customer_email', '')
        )

        if not customer_email or not stripe_customer_id:
            log.error(
                f"checkout.session.completed missing email or customer id — "
                f"email={bool(customer_email)} cid={bool(stripe_customer_id)}"
            )
            return jsonify({"error": "missing customer data"}), 400

        # Idempotency: if account already exists, just resend setup email
        existing = auth.get_customer_by_stripe_id(stripe_customer_id)
        if existing:
            customer_id   = existing['id']
            display_name  = auth.get_display_name(existing) or ''
            log.info(f"checkout.session.completed: customer already exists {customer_id} — resending setup email")
        else:
            try:
                customer_id = auth.create_unverified_customer(
                    email=customer_email,
                    stripe_customer_id=stripe_customer_id,
                    pricing_tier='standard',
                )
                display_name = ''
                log.info(f"New customer created: {customer_id} via checkout.session.completed")
            except ValueError as e:
                # Email already registered (duplicate webhook delivery)
                log.warning(f"checkout.session.completed duplicate: {e}")
                row = auth.get_customer_by_email(customer_email)
                if row:
                    customer_id  = row['id']
                    display_name = auth.get_display_name(row) or ''
                else:
                    return jsonify({"error": str(e)}), 409

        # Generate setup token and build link
        token      = auth.generate_verify_token(customer_id)
        base       = PORTAL_BASE_URL or f"http://localhost:{PORTAL_PORT}"
        setup_link = f"{base}/setup-account/{token}"

        email_ok = _send_setup_email(customer_email, setup_link, display_name)
        if not email_ok:
            log.warning(
                f"Setup email failed for {customer_id} — token={token[:8]}… "
                f"link={setup_link}"
            )

        # Update subscription record if we have a subscription ID
        if stripe_sub_id:
            auth.update_subscription(
                customer_id=customer_id,
                stripe_customer_id=stripe_customer_id,
                subscription_id=stripe_sub_id,
                status='inactive',   # becomes 'active' on invoice.payment_succeeded
            )

        return jsonify({"ok": True, "customer_id": customer_id}), 200

    elif event_type == 'invoice.payment_succeeded':
        stripe_customer_id = obj.get('customer', '')
        stripe_sub_id      = obj.get('subscription', '')

        customer = auth.get_customer_by_stripe_id(stripe_customer_id)
        if not customer:
            log.warning(f"invoice.payment_succeeded: no customer for stripe_id={stripe_customer_id}")
            return jsonify({"ok": True, "note": "customer not found — ignored"}), 200

        auth.update_subscription(
            customer_id=customer['id'],
            stripe_customer_id=stripe_customer_id,
            subscription_id=stripe_sub_id,
            status='active',
        )
        log.info(f"Subscription activated: {customer['id']}")
        return jsonify({"ok": True}), 200

    elif event_type == 'invoice.payment_failed':
        stripe_customer_id = obj.get('customer', '')

        customer = auth.get_customer_by_stripe_id(stripe_customer_id)
        if not customer:
            log.warning(f"invoice.payment_failed: no customer for stripe_id={stripe_customer_id}")
            return jsonify({"ok": True, "note": "customer not found — ignored"}), 200

        auth.mark_grace_period(customer['id'], days=7)
        log.info(f"Grace period started for {customer['id']} (invoice.payment_failed)")
        return jsonify({"ok": True}), 200

    elif event_type == 'customer.subscription.deleted':
        stripe_customer_id = obj.get('customer', '')
        stripe_sub_id      = obj.get('id', '')

        customer = auth.get_customer_by_stripe_id(stripe_customer_id)
        if not customer:
            log.warning(f"subscription.deleted: no customer for stripe_id={stripe_customer_id}")
            return jsonify({"ok": True, "note": "customer not found — ignored"}), 200

        auth.update_subscription(
            customer_id=customer['id'],
            stripe_customer_id=stripe_customer_id,
            subscription_id=stripe_sub_id,
            status='cancelled',
        )
        log.info(f"Subscription cancelled: {customer['id']}")
        return jsonify({"ok": True}), 200

    else:
        # Acknowledge all other event types — Stripe retries on non-2xx
        log.debug(f"Stripe webhook: unhandled event type '{event_type}' — acknowledged")
        return jsonify({"ok": True, "note": f"event type '{event_type}' not handled"}), 200


@app.route('/admin/construction-verify', methods=['GET', 'POST'])
def construction_verify():
    """
    Admin OTP verification for construction-locked routes.
    GET  — show form (sends OTP automatically on first visit)
    POST — validate entered OTP, unlock session if correct
    """
    if not is_authenticated() or not is_admin():
        return redirect('/login')

    sent  = False
    error = None

    if request.method == 'GET':
        # Auto-send OTP when admin first hits this page
        if not _construction_otp.get('otp'):
            sent = _send_construction_otp()
        else:
            sent = True  # Already sent this session

    elif request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
        if _rate_limited(_otp_attempts, ip, _OTP_MAX, _OTP_WINDOW):
            log.warning("OTP rate limit hit for IP %s", ip)
            error = "Too many attempts — please wait a few minutes before trying again."
        else:
            otp_entered = request.form.get('otp', '')
            if _verify_construction_otp(otp_entered):
                session['construction_unlocked'] = True
                redirect_to = session.pop('construction_redirect', '/subscribe')
                log.info(f"Construction access granted to admin {session.get('customer_id')}")
                return redirect(redirect_to)
            else:
                error = "Incorrect or expired code. Request a new one below."

    return render_template_string(_CONSTRUCTION_VERIFY_HTML, error=error, sent=sent)


@app.route('/admin/construction-send-otp', methods=['POST'])
def construction_send_otp():
    """Resend the construction OTP to admin email."""
    if not is_authenticated() or not is_admin():
        return redirect('/login')
    _send_construction_otp()
    return redirect('/admin/construction-verify?sent=1')


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
    LOGIN_URL    = os.environ.get('LOGIN_SERVER_URL', 'https://synth-cloud.com')
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

    customer = auth.get_customer_by_email(email)
    if not customer:
        log.warning(f'SSO login for unknown email: {email}')
        return redirect(f'{LOGIN_URL}/login?error=user_not_found')

    allowed, reason = auth.is_access_allowed(customer['id'], customer['role'])
    if not allowed:
        log.warning(f'SSO login denied: {customer["id"]} reason={reason}')
        return redirect(f'{LOGIN_URL}/login?error=access_denied&reason={reason}')

    session.clear()
    session['customer_id']  = customer['id']
    session['role']         = customer['role']
    session['display_name'] = auth.get_display_name(customer)
    session['access_reason']= reason
    session['tos_version']  = customer['tos_version']
    session.permanent       = True
    auth.record_login(customer['id'])
    log.info(f'SSO login: {customer["id"]} ({email}) access={reason}')
    return redirect('/admin' if customer['role'] == 'admin' else '/')


# ── HELPERS ───────────────────────────────────────────────────────────────



# Master customer ID — shared agents write here; all customers read market data from this DB
_MASTER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')

def _shared_db():
    """Return the master customer's signals.db for shared market data (news, signals, screening).
    All customers read intelligence from this single source."""
    sys.path.insert(0, PROJECT_DIR)
    from retail_database import get_customer_db
    return get_customer_db(_MASTER_CUSTOMER_ID)

def _customer_db():
    """Return the signals.db instance for the currently logged-in customer."""
    sys.path.insert(0, PROJECT_DIR)
    from retail_database import get_customer_db
    customer_id = session.get('customer_id', 'default')
    return get_customer_db(customer_id)


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


def _get_customer_alpaca_creds():
    """Return (api_key, secret_key, base_url) for the current session's customer.
    Reads from auth.db (encrypted). Falls back to env vars for backward compatibility."""
    alpaca_url = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
    customer_id = session.get('customer_id')
    if customer_id and customer_id != 'admin':
        try:
            api_key, secret_key = auth.get_alpaca_credentials(customer_id)
            if api_key:
                return api_key, secret_key, alpaca_url
        except Exception as e:
            log.warning(f"Could not load Alpaca creds from auth.db for {customer_id}: {e}")
    # Fallback: env — ONLY for admin sessions (customers must set their own keys)
    if not customer_id or customer_id == 'admin':
        return (
            os.environ.get('ALPACA_API_KEY', ''),
            os.environ.get('ALPACA_SECRET_KEY', ''),
            alpaca_url,
        )
    return ('', '', alpaca_url,
    )


def _fetch_alpaca_positions():
    """Fetch live prices from shared live_prices table (updated by price poller).
    Falls back to direct Alpaca API if no fresh data (>5 min stale)."""
    import time as _time

    # Try shared live_prices table first (fast, no Alpaca API call)
    try:
        shared = _shared_db()
        with shared.conn() as c:
            rows = c.execute("SELECT * FROM live_prices").fetchall()
        if rows:
            freshness = 999
            try:
                latest = max(r['updated_at'] for r in rows if r['updated_at'])
                from datetime import datetime
                age = (datetime.now() - datetime.fromisoformat(latest.replace('Z','+00:00').split('+')[0])).total_seconds()
                freshness = age
            except Exception:
                pass
            if freshness < 300:  # Fresh within 5 min
                result = {}
                for r in rows:
                    result[r['ticker']] = {
                        'symbol':                    r['ticker'],
                        'current_price':             str(r['price']),
                        'market_value':              '0',  # Will be calculated by _enrich_positions
                        'unrealized_pl':             '0',
                        'unrealized_plpc':           '0',
                        'unrealized_intraday_pl':    str(r['day_change']),
                        'unrealized_intraday_plpc':  str((r['day_change_pct'] or 0) / 100),
                        'avg_entry_price':           '0',
                        'lastday_price':             str(r['prev_close'] or 0),
                        'qty':                       '0',
                    }
                log.debug(f"Prices from shared live_prices ({len(result)} tickers, {freshness:.0f}s old)")
                return result
    except Exception as e:
        log.debug(f"live_prices read failed (OK — falling back to Alpaca): {e}")

    # Fallback: direct Alpaca API call
    import requests as _req
    alpaca_key, alpaca_secret, alpaca_url = _get_customer_alpaca_creds()
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
            avg_entry   = float(ap.get('avg_entry_price', entry) or entry)
            mkt_value   = round(cur_price * shares, 2) if cur_price else cost
            # Calculate P&L from prices (works for both Alpaca API and live_prices table)
            unreal_pl   = float(ap.get('unrealized_pl', 0) or 0)
            unreal_plpc = float(ap.get('unrealized_plpc', 0) or 0)
            if unreal_pl == 0.0 and cur_price and entry and cur_price != entry:
                unreal_pl   = round((cur_price - entry) * shares, 2)
                unreal_plpc = round((cur_price - entry) / entry, 4) if entry else 0.0
            day_pl      = float(ap.get('unrealized_intraday_pl', 0) or 0)
            day_plpc    = float(ap.get('unrealized_intraday_plpc', 0) or 0)
            if day_pl == 0.0 and cur_price:
                prev_close = float(ap.get('lastday_price', 0) or 0)
                if prev_close and prev_close != cur_price:
                    day_pl   = round((cur_price - prev_close) * shares, 2)
                    day_plpc = round((cur_price - prev_close) / prev_close, 4) if prev_close else 0.0
        else:
            cur_price   = float(p.get('current_price', 0) or 0) or entry
            mkt_value   = round(cur_price * shares, 2) if cur_price else cost
            unreal_pl   = round((cur_price - entry) * shares, 2) if cur_price and entry else 0.0
            unreal_plpc = round((cur_price - entry) / entry, 4) if entry and cur_price else 0.0
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
        # Agent running — skip Alpaca API calls but still show DB data
        log.debug(f"Agent lock held by {lock['agent']} — using DB-only data (no Alpaca enrichment)")
        try:
            db = _customer_db()
            portfolio = db.get_portfolio()
            positions = db.get_open_positions()
            last_hb   = db.get_last_heartbeat()
            # Use DB prices (last known) without live Alpaca enrichment
            basic_positions = []
            for p in positions:
                entry = float(p.get('entry_price', 0) or 0)
                shares = float(p.get('shares', 0) or 0)
                cur = float(p.get('current_price', 0) or 0) or entry
                basic_positions.append({
                    **p,
                    'current_price': round(cur, 4),
                    'market_value': round(cur * shares, 2),
                    'unrealized_pl': round((cur - entry) * shares, 2),
                    'unrealized_plpc': round(((cur - entry) / entry * 100) if entry else 0, 2),
                    'day_pl': 0.0, 'day_plpc': 0.0,
                    'avg_entry_price': round(entry, 4),
                    'cost_basis': round(entry * shares, 2),
                    'is_orphan': False,
                })
            mkt_total = sum(p['market_value'] for p in basic_positions)
            return {
                "portfolio_value": round(portfolio['cash'] + mkt_total, 2),
                "cash":            round(portfolio['cash'], 2),
                "realized_gains":  round(portfolio.get('realized_gains', 0), 2),
                "open_positions":  len(basic_positions),
                "orphan_count":    0,
                "positions":       basic_positions,
                "urgent_flags":    0,
                "critical_flags":  0,
                "flags_detail":    [],
                "last_heartbeat":  last_hb['timestamp'] if last_hb else "Never",
                "kill_switch":     db.get_setting('KILL_SWITCH') == '1' or kill_switch_active(),
                "operating_mode":  db.get_setting('OPERATING_MODE') or OPERATING_MODE,
                "trading_mode":    os.environ.get('TRADING_MODE', 'PAPER'),
                "max_trade_usd":   float(os.environ.get('MAX_TRADE_USD', '0')),
                "pi_id":           PI_ID,
                "agent_running":   lock['agent'],
                "agent_running_secs": lock['age_secs'],
            }
        except Exception as e:
            log.warning(f"Lock-mode status read failed: {e}")

    for attempt in range(3):
        try:
            db = _customer_db()
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

            # Per-customer operating mode (fallback to global)
            _cust_mode = db.get_setting('OPERATING_MODE') or OPERATING_MODE
            _cust_kill = db.get_setting('KILL_SWITCH') == '1' or kill_switch_active()

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
                "kill_switch":      _cust_kill,
                "operating_mode":   _cust_mode,
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
    """Read approval queue from customer's DB. Returns all rows (portal filters by status in JS)."""
    try:
        return _customer_db().get_pending_approvals()
    except Exception as e:
        log.error(f"load_pending_approvals DB error: {e}")
        return []

def update_env(key, value):
    """Update a single key in .env file.

    Security: newlines are stripped from value to prevent .env injection.
    Keys must be alphanumeric+underscore only.
    """
    # Sanitize key — must be a valid env var name
    import re as _re
    if not _re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
        raise ValueError(f"Invalid env key: {key!r}")
    # Strip newlines from value to prevent injection of additional keys
    value = str(value).replace('\n', '').replace('\r', '')

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
    os.chmod(ENV_PATH, 0o600)


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
  /* ── Surfaces ── */
  --bg:      #0a0c14;
  --surface: #111520;
  --surface2:#161b28;
  --surface3:#1c2235;

  /* ── Borders ── */
  --border:  rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.13);
  --border3: rgba(255,255,255,0.20);

  /* ── Text ── */
  --text:    rgba(255,255,255,0.88);
  --muted:   rgba(255,255,255,0.40);
  --dim:     rgba(255,255,255,0.18);

  /* ── Existing color names (preserved for compatibility) ── */
  --teal:    #00f5d4;
  --teal2:   rgba(0,245,212,0.08);
  --pink:    #ff4b6e;
  --pink2:   rgba(255,75,110,0.08);
  --purple:  #7b61ff;
  --purple2: rgba(123,97,255,0.08);
  --amber:   #f5a623;
  --amber2:  rgba(245,166,35,0.08);
  --green:   #22c55e;

  /* ── Semantic aliases (new — use for all new code) ── */
  --cyan:        #00f5d4;
  --cyan-dim:    rgba(0,245,212,0.08);
  --cyan-mid:    rgba(0,245,212,0.09);
  --cyan-glow:   rgba(0,245,212,0.22);
  --violet:      #7b61ff;
  --violet-dim:  rgba(123,97,255,0.08);
  --violet-mid:  rgba(123,97,255,0.09);
  --violet-glow: rgba(123,97,255,0.18);
  --signal:      #f5a623;
  --signal-dim:  rgba(245,166,35,0.08);
  --signal-mid:  rgba(245,166,35,0.15);
  --signal-glow: rgba(245,166,35,0.18);
  --red:         #ff4b6e;
  --red-dim:     rgba(255,75,110,0.08);
  --red-mid:     rgba(255,75,110,0.09);

  /* ── Glow scale (use these, not ad-hoc box-shadows) ── */
  --glow-hero:   0 0 20px rgba(0,245,212,0.22);
  --glow-active: 0 0 8px  rgba(0,245,212,0.12);
  --glow-dot:    0 0 5px  currentColor;

  /* ── Shadow scale ── */
  --shadow-card:  0 4px 16px rgba(0,0,0,0.3);
  --shadow-modal: 0 24px 80px rgba(0,0,0,0.6);

  /* ── Typography scale ── */
  --text-hero: 28px;
  --text-xl:   20px;
  --text-lg:   15px;
  --text-base: 13px;
  --text-sm:   11px;
  --text-xs:   10px;
  --text-xxs:  9px;

  /* ── Fonts ── */
  --sans: 'Inter',system-ui,sans-serif;
  --mono: 'JetBrains Mono',monospace;

  /* ── Motion ── */
  --pulse-live:  2s;
  --pulse-alert: 3s;
  --transition:  0.15s;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;line-height:1.5}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:99px}

/* ── HEADER ── */
.header{
  position:sticky;top:0;z-index:100;
  background:rgba(10,12,20,0.92);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 1rem;height:40px;
  display:flex;align-items:center;gap:10px;
}
.hdr-left{display:flex;align-items:center;gap:8px;flex-shrink:0}
.hdr-hamburger{
  display:flex;flex-direction:column;justify-content:center;gap:3.5px;
  background:none;border:none;cursor:pointer;padding:4px;opacity:0.45;
  transition:opacity .15s;width:28px;height:28px;flex-shrink:0;
}
.hdr-hamburger:hover{opacity:1}
.hdr-hamburger span{display:block;width:16px;height:1.5px;background:var(--text);border-radius:2px}
.wordmark{
  font-family:var(--mono);font-size:0.9rem;font-weight:600;
  letter-spacing:0.15em;color:var(--teal);
  text-shadow:0 0 20px rgba(0,245,212,0.22);flex-shrink:0;
}
.status-pill{
  display:flex;align-items:center;gap:5px;
  padding:3px 12px;border-radius:99px;
  font-size:11px;font-weight:600;letter-spacing:0.04em;
  border:1px solid;white-space:nowrap;min-width:200px;
}
.sp-ok{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.14);color:var(--teal)}
.sp-warn{background:rgba(245,166,35,0.08);border-color:rgba(245,166,35,0.25);color:var(--amber)}
.sp-err{background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.14);color:var(--pink)}
.sp-dim{background:rgba(255,255,255,0.04);border-color:var(--border);color:var(--muted)}
.status-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.dot-on{background:var(--teal);box-shadow:0 0 6px var(--teal)}
.dot-warn{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.dot-off{background:var(--pink);box-shadow:0 0 6px var(--pink)}
.dot-dim{background:rgba(255,255,255,0.2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.4}}
.dot-blink{animation:blink 2s infinite}
.hdr-right{display:flex;align-items:center;gap:6px;margin-left:auto;flex-shrink:0}
/* Mode pill */
.hdr-mode-pill{
  padding:3px 10px;border-radius:99px;font-size:10px;font-weight:700;
  letter-spacing:0.06em;cursor:pointer;
  border:1px solid var(--border);background:rgba(255,255,255,0.04);color:var(--muted);
  font-family:var(--sans);transition:all 0.15s;white-space:nowrap;
}
.hdr-mode-pill:hover{}
.hdr-mode-pill.mp-auto{
  background:rgba(245,166,35,0.08);border-color:rgba(245,166,35,0.3);color:var(--amber);
}
/* Avatar */
.hdr-avatar{
  position:relative;width:28px;height:28px;border-radius:50%;
  background:var(--surface2);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:border-color .15s;flex-shrink:0;
}
.hdr-avatar:hover{border-color:var(--teal);box-shadow:0 0 10px rgba(0,245,212,0.09)}
.hdr-avatar svg{color:rgba(255,255,255,0.45);width:14px;height:14px}
.avatar-drop{
  display:none;position:absolute;top:calc(100% + 8px);right:0;
  min-width:150px;
  background:var(--surface);border:1px solid var(--border2);
  border-radius:10px;overflow:hidden;
  box-shadow:0 20px 50px rgba(0,0,0,0.6),0 0 0 1px rgba(0,245,212,0.05);
  z-index:300;
}
.avatar-drop.open{display:block}
.avatar-drop-item{
  display:block;width:100%;text-align:left;padding:10px 16px;
  font-size:12px;font-weight:500;color:var(--text);background:none;border:none;
  cursor:pointer;font-family:var(--sans);transition:background .12s;text-decoration:none;
}
.avatar-drop-item:hover{background:var(--surface2)}
.avatar-drop-item.danger{color:var(--pink)}
.avatar-drop-sep{height:1px;background:var(--border)}

/* ── Notification Bell ── */
.hdr-bell{
  position:relative;width:28px;height:28px;border-radius:50%;
  background:var(--surface2);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:border-color .15s;flex-shrink:0;
}
.hdr-bell:hover{border-color:var(--signal);box-shadow:0 0 10px rgba(245,166,35,0.09)}
.hdr-bell svg{color:rgba(255,255,255,0.4);width:14px;height:14px;transition:color .15s}
.hdr-bell:hover svg{color:rgba(255,255,255,0.7)}
.bell-badge{
  position:absolute;top:-3px;right:-3px;
  min-width:16px;height:16px;border-radius:99px;
  background:var(--signal);color:#000;
  font-size:9px;font-weight:800;line-height:16px;text-align:center;
  padding:0 4px;display:none;
  box-shadow:0 0 8px rgba(245,166,35,0.4);
}
.bell-badge.show{display:block}
.bell-drop{
  display:none;position:absolute;top:calc(100% + 10px);right:-60px;
  width:340px;max-height:440px;
  background:var(--surface);border:1px solid var(--border2);
  border-radius:14px;overflow:hidden;
  box-shadow:0 20px 60px rgba(0,0,0,0.6),0 0 0 1px rgba(245,166,35,0.04);
  z-index:300;display:none;flex-direction:column;
}
.bell-drop.open{display:flex}
.bell-drop-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--border);flex-shrink:0;
}
.bell-drop-title{font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--text)}
.bell-mark-read{
  font-size:10px;color:var(--muted);background:none;border:none;
  cursor:pointer;font-family:var(--sans);transition:color .15s;
}
.bell-mark-read:hover{color:var(--teal)}
.bell-tabs{
  display:flex;gap:0;border-bottom:1px solid var(--border);flex-shrink:0;
}
.bell-tab{
  flex:1;padding:7px 0;font-size:10px;font-weight:600;text-align:center;
  color:var(--muted);background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;font-family:var(--sans);transition:all .15s;
}
.bell-tab:hover{color:var(--text)}
.bell-tab.active{color:var(--signal);border-bottom-color:var(--signal)}
.bell-list{flex:1;overflow-y:auto;max-height:340px}
.bell-item{
  display:flex;align-items:flex-start;gap:10px;padding:10px 14px;
  border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;
}
.bell-item:hover{background:rgba(255,255,255,0.02)}
.bell-item:last-child{border-bottom:none}
.bell-unread-dot{
  width:6px;height:6px;border-radius:50%;background:var(--signal);
  flex-shrink:0;margin-top:5px;
  box-shadow:0 0 6px rgba(245,166,35,0.3);
}
.bell-unread-dot.read{background:transparent}
.bell-item-body{flex:1;min-width:0}
.bell-item-title{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bell-item-preview{font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bell-item-meta{display:flex;align-items:center;gap:6px;margin-top:3px}
.bell-item-time{font-size:9px;color:var(--dim);font-family:var(--mono)}
.bell-cat-pill{
  font-size:8px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  padding:1px 6px;border-radius:99px;
}
.bell-cat-system{background:rgba(123,97,255,0.1);color:var(--violet)}
.bell-cat-daily{background:rgba(0,245,212,0.08);color:var(--teal)}
.bell-cat-account{background:rgba(245,166,35,0.08);color:var(--signal)}
.bell-cat-trade{background:rgba(0,245,212,0.08);color:var(--teal)}
.bell-cat-alert{background:rgba(255,75,110,0.08);color:var(--pink)}
.bell-empty{text-align:center;padding:30px 14px;color:var(--dim);font-size:11px}

/* ── Preserve .nav-btn for non-header uses (kill switch, command portal etc.) ── */
.nav-btn{
  padding:5px 12px;border-radius:8px;font-size:11px;font-weight:600;
  letter-spacing:0.04em;cursor:pointer;
  background:transparent;border:1px solid var(--border);color:var(--muted);
  font-family:var(--sans);transition:all 0.15s;
}
.nav-btn:hover{background:var(--surface2);color:var(--text);border-color:var(--border2)}
.nav-btn.active{background:var(--teal2);border-color:rgba(0,245,212,0.18);color:var(--teal)}
.nav-btn.danger{border-color:rgba(255,75,110,0.18);color:var(--pink)}
.nav-btn.danger:hover{background:var(--pink2)}
.nav-btn.danger.engaged{background:var(--pink2);border-color:rgba(255,75,110,0.14);color:var(--pink)}

/* ── LEFT SIDEBAR ── */
.sidebar-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);
  z-index:190;backdrop-filter:blur(2px);
}
.sidebar-overlay.open{display:block}
.sidebar{
  position:fixed;top:0;left:0;height:100%;width:240px;z-index:200;
  background:rgba(10,12,20,0.98);border-right:1px solid var(--border2);
  transform:translateX(-100%);transition:transform .25s cubic-bezier(.22,1,.36,1);
  display:flex;flex-direction:column;overflow:hidden;
}
.sidebar.open{transform:translateX(0)}
.sidebar-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 14px;height:40px;border-bottom:1px solid var(--border);flex-shrink:0;
}
.sidebar-close{
  background:none;border:none;cursor:pointer;color:var(--muted);
  font-size:18px;padding:4px;transition:color .15s;line-height:1;
}
.sidebar-close:hover{color:var(--text)}
.sidebar-nav{flex:1;padding:8px 0;overflow-y:auto}
.sidebar-nav-btn{
  display:block;width:100%;text-align:left;padding:11px 20px;
  font-size:13px;font-weight:500;color:var(--muted);
  background:none;border:none;cursor:pointer;font-family:var(--sans);
  transition:all .15s;letter-spacing:0.01em;
}
.sidebar-nav-btn:hover{background:rgba(255,255,255,0.04);color:var(--text)}
.sidebar-nav-btn.active{color:var(--teal);background:rgba(0,245,212,0.06)}
.sidebar-nav-sep{height:1px;background:var(--border);margin:6px 16px}

/* ── CONFIG PANEL (right slide-out) ── */
.cfg-tab{
  position:fixed;right:0;top:50%;transform:translateY(-50%);
  z-index:150;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:18px 9px;
  background:var(--surface);
  border:1px solid var(--border2);border-right:none;
  border-radius:10px 0 0 10px;
  box-shadow:-4px 0 24px rgba(0,0,0,0.35);
  transition:color .15s,background .15s,box-shadow .15s;
}
.cfg-tab:hover{background:var(--surface2);box-shadow:-4px 0 28px rgba(0,0,0,0.45)}
.cfg-tab-icon{font-size:13px;color:var(--muted);transition:color .15s}
.cfg-tab:hover .cfg-tab-icon{color:var(--teal)}
.cfg-tab-label{
  writing-mode:vertical-rl;text-orientation:mixed;
  font-size:9px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;
  color:var(--muted);transition:color .15s;
}
.cfg-tab:hover .cfg-tab-label{color:var(--teal)}
.cfg-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:155;
}
.cfg-overlay.open{display:block}
.cfg-panel{
  position:fixed;top:0;right:0;height:100%;width:340px;z-index:160;
  background:rgba(10,12,20,0.98);border-left:1px solid var(--border2);
  transform:translateX(100%);transition:transform .28s cubic-bezier(.22,1,.36,1);
  display:flex;flex-direction:column;overflow:hidden;
}
.cfg-panel.open{transform:translateX(0)}
.cfg-panel-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 18px;height:50px;border-bottom:1px solid var(--border);flex-shrink:0;
}
.cfg-panel-title{
  font-size:11px;font-weight:700;letter-spacing:0.08em;
  text-transform:uppercase;color:var(--text);
}
.cfg-panel-close{
  background:none;border:none;cursor:pointer;color:var(--muted);
  font-size:18px;padding:4px;transition:color .15s;
}
.cfg-panel-close:hover{color:var(--text)}

.sup-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:400}
.sup-overlay.open{display:block}
.sup-panel{position:fixed;top:0;right:0;width:380px;max-width:90vw;height:100vh;background:var(--surface);
  border-left:1px solid var(--border2);z-index:401;transform:translateX(100%);transition:transform .25s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;overflow:hidden}
.sup-panel.open{transform:translateX(0)}
.sup-head{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sup-title{font-size:13px;font-weight:700;letter-spacing:0.03em;flex:1}
.sup-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;padding:4px}
.sup-body{flex:1;overflow-y:auto;padding:18px}
.sup-cats{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.sup-cat{padding:5px 12px;border-radius:99px;font-size:10px;font-weight:600;border:1px solid var(--border2);
  background:transparent;color:var(--muted);cursor:pointer;font-family:var(--sans);transition:all .15s}
.sup-cat.active{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.2);color:var(--teal)}
.sup-input{width:100%;background:var(--surface2);border:1px solid var(--border2);border-radius:8px;
  padding:8px 12px;color:var(--text);font-family:var(--sans);font-size:12px;outline:none;margin-bottom:10px}
.sup-input:focus{border-color:var(--teal)}
.sup-textarea{min-height:100px;resize:vertical;font-family:var(--mono);font-size:11px;line-height:1.6}
.sup-submit{width:100%;padding:10px;border:none;border-radius:8px;background:var(--teal);color:#000;
  font-size:12px;font-weight:700;cursor:pointer;font-family:var(--sans);margin-top:8px}
.sup-submit:disabled{opacity:0.4;cursor:not-allowed}
.sup-divider{height:1px;background:var(--border);margin:18px 0}
.sup-ticket{padding:10px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;cursor:pointer;transition:border-color .15s}
.sup-ticket:hover{border-color:var(--border2)}
.sup-ticket-subj{font-size:12px;font-weight:600;margin-bottom:3px}
.sup-ticket-meta{font-size:10px;color:var(--muted)}
.sup-status{font-size:9px;font-weight:700;padding:2px 6px;border-radius:99px;text-transform:uppercase;letter-spacing:0.04em}
.sup-status.open{background:rgba(245,166,35,0.1);color:var(--amber)}
.sup-status.in_progress{background:rgba(123,97,255,0.1);color:var(--purple)}
.sup-status.resolved{background:rgba(0,245,212,0.1);color:var(--teal)}

.cfg-panel-body{flex:1;overflow-y:auto;padding:18px}
.cfg-section{
  font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:10px;margin-top:20px;
  display:flex;align-items:center;gap:8px;
}
.cfg-section:first-child{margin-top:0}
.cfg-section::after{content:'';flex:1;height:1px;background:var(--border)}

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
  border-color:rgba(0,245,212,0.09);
  background:linear-gradient(160deg,rgba(0,245,212,0.05) 0%,var(--surface) 40%);
}
.glass.teal-glow::before{background:linear-gradient(90deg,transparent,rgba(0,245,212,0.18),transparent);box-shadow:0 0 8px rgba(0,245,212,0.12)}
.glass.pink-glow{
  border-color:rgba(255,75,110,0.09);
  background:linear-gradient(160deg,rgba(255,75,110,0.05) 0%,var(--surface) 40%);
}
.glass.pink-glow::before{background:linear-gradient(90deg,transparent,rgba(255,75,110,0.18),transparent);box-shadow:0 0 8px rgba(255,75,110,0.12)}
.glass.purple-glow{
  border-color:rgba(123,97,255,0.09);
  background:linear-gradient(160deg,rgba(123,97,255,0.05) 0%,var(--surface) 40%);
}
.glass.purple-glow::before{background:linear-gradient(90deg,transparent,rgba(123,97,255,0.18),transparent);box-shadow:0 0 8px rgba(123,97,255,0.12)}
.glass.amber-glow{
  border-color:rgba(245,166,35,0.15);
  background:linear-gradient(160deg,rgba(245,166,35,0.05) 0%,var(--surface) 40%);
}
.glass.amber-glow::before{background:linear-gradient(90deg,transparent,rgba(245,166,35,0.3),transparent);box-shadow:0 0 8px rgba(245,166,35,0.2)}

/* ── STAT CARDS ── */
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.stat-card{
  padding:3px 10px;border-radius:10px;
  border:1px solid var(--border);
  background:var(--surface);
  position:relative;overflow:hidden;
}
.stat-label{font-size:8px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:1px}
.stat-val{font-size:13px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}
.stat-sub{font-size:10px;color:var(--muted);margin-top:2px}
.stat-card.teal .stat-val{color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.18)}
.stat-card.pink .stat-val{color:var(--pink);text-shadow:0 0 20px rgba(255,75,110,0.18)}
.stat-card.amber .stat-val{color:var(--amber);text-shadow:0 0 20px rgba(245,166,35,0.3)}
.stat-card.purple .stat-val{color:var(--purple);text-shadow:0 0 20px rgba(123,97,255,0.18)}
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
  border:1px solid rgba(255,75,110,0.12);
  background:linear-gradient(135deg,rgba(255,75,110,0.06) 0%,var(--surface) 60%);
  margin-bottom:16px;
}
.kill-bar.clear{
  border-color:rgba(0,245,212,0.09);
  background:linear-gradient(135deg,rgba(0,245,212,0.04) 0%,var(--surface) 60%);
}
.kill-label{font-size:12px;font-weight:700;letter-spacing:0.04em}
.kill-desc{font-size:11px;color:var(--muted);margin-top:1px}
.kill-btn{
  margin-left:auto;padding:8px 20px;border-radius:10px;
  font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
  cursor:pointer;font-family:var(--sans);transition:all 0.18s;
  border:1px solid rgba(255,75,110,0.22);
  background:rgba(255,75,110,0.1);color:var(--pink);
}
.kill-btn:hover{background:rgba(255,75,110,0.12);box-shadow:0 0 16px rgba(255,75,110,0.12)}
.kill-btn.resume{border-color:rgba(0,245,212,0.22);background:rgba(0,245,212,0.1);color:var(--teal)}
.kill-btn.resume:hover{background:rgba(0,245,212,0.12);box-shadow:0 0 16px rgba(0,245,212,0.12)}

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
.mb-supervised{background:rgba(0,245,212,0.08);border-color:rgba(0,245,212,0.14);color:var(--teal)}
.mb-autonomous{background:rgba(245,166,35,0.08);border-color:rgba(245,166,35,0.25);color:var(--amber)}

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
.graph-tab.active{background:var(--teal2);border-color:rgba(0,245,212,0.18);color:var(--teal)}
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
  background:linear-gradient(135deg,rgba(123,97,255,0.18),rgba(123,97,255,0.1));
  border:1px solid rgba(123,97,255,0.14);color:#a78bfa;
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
.conf-high{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.18);color:var(--teal)}
.conf-med{background:rgba(245,166,35,0.1);border-color:rgba(245,166,35,0.3);color:var(--amber)}
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
  background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.18);color:var(--teal);
  transition:all 0.15s;
}
.btn-approve:hover{background:rgba(0,245,212,0.12);box-shadow:0 0 12px rgba(0,245,212,0.09)}
.btn-reject{
  flex:1;padding:8px;border-radius:9px;font-size:11px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;font-family:var(--sans);
  background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.14);color:var(--pink);
  transition:all 0.15s;
}
.btn-reject:hover{background:rgba(255,75,110,0.18)}

/* ── APPROVAL QUEUE SECTIONS ── */
.queue-divider{
  font-size:8px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  color:var(--dim);padding:10px 0 6px;margin-top:4px;
  border-top:1px solid var(--border);
}
.queued-card{
  display:flex;align-items:center;gap:10px;
  padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04);
  font-size:11px;
}
.queued-card:last-child{border-bottom:none}
.qc-ticker{
  width:36px;height:36px;border-radius:9px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:9px;font-weight:800;letter-spacing:-0.2px;
}
.qc-approved .qc-ticker{
  background:linear-gradient(135deg,rgba(0,245,212,0.14),rgba(0,245,212,0.05));
  border:1px solid rgba(0,245,212,0.18);color:var(--teal);
}
.qc-rejected .qc-ticker{
  background:rgba(255,255,255,0.04);
  border:1px solid rgba(255,255,255,0.08);color:var(--dim);
}
.qc-info{flex:1;min-width:0}
.qc-name{font-weight:600;color:var(--text)}
.qc-rejected .qc-name{color:var(--muted)}
.qc-detail{font-size:9px;color:var(--muted);margin-top:1px}
.qc-rejected .qc-detail{color:var(--dim)}
.btn-revoke{
  font-size:9px;font-weight:600;padding:3px 10px;border-radius:6px;cursor:pointer;
  background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.18);
  color:var(--amber);white-space:nowrap;
}
.btn-revoke:hover{background:rgba(245,166,35,0.15)}
.btn-reapprove{
  font-size:9px;font-weight:600;padding:3px 10px;border-radius:6px;cursor:pointer;
  background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.14);
  color:var(--teal);white-space:nowrap;
}
.btn-reapprove:hover{background:rgba(0,245,212,0.12)}

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
.pnl-pos{color:var(--teal);text-shadow:0 0 12px rgba(0,245,212,0.18)}
.pnl-neg{color:var(--pink);text-shadow:0 0 12px rgba(255,75,110,0.18)}

/* ── POSITION DRAWER ── */
.drawer-overlay{position:fixed;inset:0;z-index:400;display:none}
.drawer-overlay.open{display:block}
.drawer{
  position:fixed;top:0;right:0;bottom:0;width:min(480px,95vw);z-index:401;
  background:var(--surface);border-left:1px solid var(--border2);
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform 0.25s cubic-bezier(0.4,0,0.2,1);
  overflow:hidden;
}
.drawer.open{transform:translateX(0)}
.drawer-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0}
.drawer-close{margin-left:auto;background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:2px 6px;line-height:1}
.drawer-close:hover{color:var(--text)}
.drawer-body{flex:1;overflow-y:auto;padding:16px 18px}
.drawer-section{margin-bottom:18px}
.drawer-section-title{font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.drawer-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.03);font-size:11px}
.drawer-row:last-child{border-bottom:none}
.drawer-label{color:var(--muted)}
.drawer-val{color:var(--text);font-family:var(--mono);font-weight:600}

/* ── SESSION TIMELINE ── */
.timeline{display:flex;align-items:flex-start;gap:0;margin-bottom:12px;position:relative}
.timeline::before{content:'';position:absolute;top:12px;left:20px;right:20px;height:1px;background:var(--border)}
.tl-step{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;position:relative;z-index:1}
.tl-dot{width:10px;height:10px;border-radius:50%;border:1px solid;background:var(--bg);flex-shrink:0}
.tl-dot.done{background:var(--teal);border-color:var(--teal);box-shadow:0 0 8px rgba(0,245,212,0.22)}
.tl-dot.active{background:var(--amber);border-color:var(--amber);box-shadow:0 0 8px rgba(245,166,35,0.5);animation:blink var(--pulse-alert) infinite}
.tl-dot.pending{background:transparent;border-color:var(--border2)}
.tl-label{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted)}
.tl-label.active{color:var(--amber)}
.tl-label.done{color:var(--teal)}
.tl-sub{font-size:8px;color:var(--dim);font-family:var(--mono)}

/* ── DAILY DIGEST ── */
.digest-card{padding:12px 14px}
.digest-date{font-size:9px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.digest-body{font-size:11px;color:var(--muted);line-height:1.7}
.digest-tag{display:inline-block;padding:2px 7px;border-radius:99px;font-size:9px;font-weight:700;margin-right:4px;margin-bottom:4px}
.dt-bull{background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.14);color:var(--teal)}
.dt-bear{background:rgba(255,75,110,0.1);border:1px solid rgba(255,75,110,0.14);color:var(--pink)}
.dt-neut{background:rgba(255,255,255,0.05);border:1px solid var(--border);color:var(--muted)}

/* ── GAUGE ── */
.gauge-wrap{padding:12px 14px}
.gauge-bar{height:6px;border-radius:99px;background:var(--surface2);border:1px solid var(--border);overflow:hidden;margin:6px 0}
.gauge-fill{height:100%;border-radius:99px;transition:width 0.6s ease}
.gauge-label{display:flex;justify-content:space-between;font-size:9px;color:var(--muted)}

/* ── GATE HEATMAP ── */
.heatmap-grid{display:grid;gap:2px}
.hm-cell{height:16px;border-radius:3px;transition:opacity 0.15s}
.hm-cell.pass{background:rgba(0,245,212,0.20)}
.hm-cell.fail{background:rgba(255,75,110,0.35)}
.hm-cell.skip{background:rgba(255,255,255,0.06)}
.hm-labels{display:flex;gap:2px;margin-bottom:4px}
.hm-lbl{font-size:8px;color:var(--dim);text-align:center;font-family:var(--mono)}

/* ── MILESTONE BADGES ── */
.badge-grid{display:flex;flex-wrap:wrap;gap:8px}
.badge{
  display:flex;align-items:center;gap:7px;
  padding:7px 12px;border-radius:10px;
  border:1px solid var(--border);background:var(--surface2);
  transition:border-color 0.15s;
}
.badge.earned{border-color:rgba(0,245,212,0.18);background:rgba(0,245,212,0.05)}
.badge.locked{opacity:0.4}
.badge-icon{font-size:16px}
.badge-name{font-size:10px;font-weight:700;color:var(--text)}
.badge-desc{font-size:9px;color:var(--muted)}

/* ── PERF TABLE ── */
.perf-table{width:100%;border-collapse:collapse;font-size:11px}
.perf-table th{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:6px 10px;border-bottom:1px solid var(--border);text-align:left}
.perf-table td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.03);color:var(--text);font-family:var(--mono);font-size:10px}
.perf-table tr:hover td{background:rgba(255,255,255,0.02)}
.perf-table tr:last-child td{border-bottom:none}

/* ── STREAK ── */
.streak-pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:99px;
  font-size:10px;font-weight:700;font-family:var(--mono);
}
.streak-win{background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.18);color:var(--teal)}
.streak-loss{background:rgba(255,75,110,0.1);border:1px solid rgba(255,75,110,0.18);color:var(--pink)}
.streak-neut{background:rgba(255,255,255,0.05);border:1px solid var(--border);color:var(--muted)}

/* ── TOGGLE SWITCH ── */
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle{position:relative;width:32px;height:18px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{
  position:absolute;inset:0;border-radius:99px;
  background:var(--surface2);border:1px solid var(--border2);
  cursor:pointer;transition:background 0.15s;
}
.toggle-slider::before{
  content:'';position:absolute;height:12px;width:12px;
  left:2px;top:2px;border-radius:50%;
  background:var(--muted);transition:transform 0.15s,background 0.15s;
}
.toggle input:checked + .toggle-slider{background:rgba(0,245,212,0.12);border-color:rgba(0,245,212,0.22)}
.toggle input:checked + .toggle-slider::before{transform:translateX(14px);background:var(--teal)}

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

/* ── HERO SIGNAL RAIL ── */
.hero-rail{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:24px}
.hero-card{
  border-radius:20px;overflow:hidden;position:relative;
  border:1px solid rgba(255,255,255,0.10);
  transition:transform 0.2s,box-shadow 0.2s;
}
.hero-card:hover{transform:translateY(-4px)}
.hero-card::before{
  content:'';position:absolute;top:0;left:10%;right:10%;height:1px;
}
.hero-card.hc-cyan{
  background:linear-gradient(160deg,rgba(0,245,212,0.08) 0%,var(--surface2) 55%);
  box-shadow:0 0 0 1px rgba(0,245,212,0.14),0 8px 32px rgba(0,245,212,0.07);
}
.hero-card.hc-cyan::before{background:linear-gradient(90deg,transparent,rgba(0,245,212,0.55),transparent)}
.hero-card.hc-cyan:hover{box-shadow:0 0 0 1px rgba(0,245,212,0.22),var(--glow-hero),0 18px 52px rgba(0,0,0,0.4)}
.hero-card.hc-violet{
  background:linear-gradient(160deg,rgba(123,97,255,0.08) 0%,var(--surface2) 55%);
  box-shadow:0 0 0 1px rgba(123,97,255,0.14),0 8px 32px rgba(123,97,255,0.07);
}
.hero-card.hc-violet::before{background:linear-gradient(90deg,transparent,rgba(123,97,255,0.55),transparent)}
.hero-card.hc-violet:hover{box-shadow:0 0 0 1px rgba(123,97,255,0.22),0 0 20px rgba(123,97,255,0.18),0 18px 52px rgba(0,0,0,0.4)}
.hero-rank-badge{
  position:absolute;top:14px;right:14px;
  font-size:8px;font-weight:800;letter-spacing:0.12em;text-transform:uppercase;
  padding:3px 9px;border-radius:99px;
}
.hrb-cyan{background:rgba(0,245,212,0.10);border:1px solid rgba(0,245,212,0.22);color:var(--teal)}
.hrb-violet{background:rgba(123,97,255,0.10);border:1px solid rgba(123,97,255,0.22);color:var(--purple)}
.hero-body{padding:18px 16px 14px}
.hero-ticker-row{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.hero-icon{
  width:50px;height:50px;border-radius:13px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;letter-spacing:-0.3px;
  position:relative;overflow:hidden;
}
.hero-icon::after{content:'';position:absolute;inset:0;background:linear-gradient(145deg,rgba(255,255,255,0.18) 0%,transparent 55%)}
.hi-cyan{
  background:linear-gradient(135deg,rgba(0,245,212,0.18),rgba(0,245,212,0.06));
  color:var(--teal);border:1px solid rgba(0,245,212,0.20);
}
.hi-violet{
  background:linear-gradient(135deg,rgba(123,97,255,0.18),rgba(123,97,255,0.06));
  color:var(--purple);border:1px solid rgba(123,97,255,0.20);
}
.hero-id{flex:1;min-width:0}
.hero-label{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.hero-ticker{font-size:20px;font-weight:700;letter-spacing:-0.4px;color:var(--text)}
.hero-conv{margin-bottom:12px}
.hero-conv-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.hero-conv-name{font-size:9px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted)}
.hero-conv-val{font-size:10px;font-weight:800}
.hcv-cyan{color:var(--teal)}
.hcv-violet{color:var(--purple)}
.hero-conv-track{height:3px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden}
.hero-conv-fill{height:100%;border-radius:99px}
.hcf-cyan{background:linear-gradient(90deg,var(--purple),var(--teal))}
.hcf-violet{background:linear-gradient(90deg,rgba(123,97,255,0.5),var(--purple))}
.hero-meta-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px}
.hm-item{
  background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);
  border-radius:8px;padding:6px 8px;text-align:center;
}
.hm-label{font-size:8px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.hm-val{font-size:11px;font-weight:700;color:var(--muted)}
.hero-pending{
  padding:5px 10px;border-radius:8px;text-align:center;
  background:rgba(255,255,255,0.025);border:1px solid rgba(255,255,255,0.05);
  font-size:8px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:var(--dim);
}
@media(max-width:600px){.hero-rail{grid-template-columns:1fr}}

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
.charm.bull:hover{box-shadow:0 0 0 1px rgba(0,245,212,0.18),0 12px 40px rgba(0,245,212,0.12)}
.charm.bear{
  background:linear-gradient(160deg,rgba(255,75,110,0.1) 0%,rgba(17,21,32,0.95) 45%);
  box-shadow:0 0 0 1px rgba(255,75,110,0.12),0 6px 24px rgba(255,75,110,0.06);
}
.charm.bear:hover{box-shadow:0 0 0 1px rgba(255,75,110,0.18),0 12px 40px rgba(255,75,110,0.12)}
.charm.neut{
  background:linear-gradient(160deg,rgba(123,97,255,0.1) 0%,rgba(17,21,32,0.95) 45%);
  box-shadow:0 0 0 1px rgba(123,97,255,0.12),0 6px 24px rgba(123,97,255,0.06);
}
.charm.neut:hover{box-shadow:0 0 0 1px rgba(123,97,255,0.18),0 12px 40px rgba(123,97,255,0.12)}
.charm.bull::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,245,212,0.6),transparent);box-shadow:0 0 8px rgba(0,245,212,0.22)}
.charm.bear::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(255,75,110,0.6),transparent);box-shadow:0 0 8px rgba(255,75,110,0.22)}
.charm.neut::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;background:linear-gradient(90deg,transparent,rgba(123,97,255,0.6),transparent);box-shadow:0 0 8px rgba(123,97,255,0.22)}
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
.sb-bull{background:rgba(0,245,212,0.1);border-color:rgba(0,245,212,0.14);color:var(--teal)}
.sb-bear{background:rgba(255,75,110,0.1);border-color:rgba(255,75,110,0.14);color:var(--pink)}
.sb-neut{background:rgba(123,97,255,0.1);border-color:rgba(123,97,255,0.14);color:#a78bfa}
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
  border-top:1px solid rgba(255,75,110,0.09);
  padding:5px 12px;display:flex;align-items:center;gap:5px;
  font-size:9px;font-weight:700;color:var(--pink);letter-spacing:0.05em;text-transform:uppercase;
}
.alert-dot{width:4px;height:4px;border-radius:50%;background:var(--pink);box-shadow:0 0 5px var(--pink);animation:blink var(--pulse-alert) infinite}

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
.glass-select:focus{border-color:rgba(0,245,212,0.22)}
.glass-input{
  padding:6px 10px;border-radius:8px;font-size:11px;width:100px;
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--sans);outline:none;
}
.glass-input:focus{border-color:rgba(0,245,212,0.22)}
.save-btn{
  padding:8px 20px;border-radius:10px;font-size:11px;font-weight:700;
  letter-spacing:0.04em;cursor:pointer;font-family:var(--sans);
  background:var(--teal2);border:1px solid rgba(0,245,212,0.18);color:var(--teal);
  transition:all 0.15s;
}
.save-btn:hover{background:rgba(0,245,212,0.12);box-shadow:0 0 12px rgba(0,245,212,0.09)}

/* ── UNLOCK FORM ── */
.unlock-wrap{padding:16px 18px}
.unlock-input{
  width:100%;padding:10px 14px;border-radius:10px;font-size:13px;
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--mono);outline:none;
  margin:10px 0;
}
.unlock-input:focus{border-color:rgba(0,245,212,0.22)}
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
.toast.ok{border-color:rgba(0,245,212,0.22);color:var(--teal)}
.toast.err{border-color:rgba(255,75,110,0.22);color:var(--pink)}

/* ── AGENT PANEL ROWS ── */
.agent-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background 0.12s}
.agent-row:last-child{border-bottom:none}
.agent-row:hover{background:rgba(255,255,255,0.03)}
.agent-row-icon{width:34px;height:34px;border-radius:9px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800}
.agent-row-body{flex:1;min-width:0}
.agent-row-ticker{font-size:12px;font-weight:700;color:var(--text)}
.agent-row-sub{font-size:10px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent-row-right{display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0}
.agent-row-time{font-size:9px;color:var(--dim);font-family:var(--mono)}

/* ── TRUST SUMMARY ── */
.trust-mode-pill{
  font-size:8px;font-weight:800;letter-spacing:0.12em;text-transform:uppercase;
  padding:3px 9px;border-radius:99px;
}
.tmp-live{background:rgba(245,166,35,0.12);border:1px solid rgba(245,166,35,0.25);color:var(--amber)}
.tmp-paper{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.10);color:var(--muted)}
.trust-params{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tp-item{
  background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);
  border-radius:8px;padding:7px 9px;
}
.tp-label{font-size:8px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.tp-val{font-size:12px;font-weight:700;color:var(--text);font-family:var(--mono)}
.trust-action-btn{
  width:100%;padding:6px;font-size:10px;font-weight:600;
  background:rgba(255,255,255,0.04);border:1px solid var(--border);
  border-radius:8px;color:var(--muted);cursor:pointer;letter-spacing:0.02em;
  transition:background 0.15s,color 0.15s;text-align:center;
}
.trust-action-btn:hover{background:rgba(255,255,255,0.07);color:var(--text)}

.qs-select,.qs-input{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:12px;padding:6px 10px;font-family:var(--sans);outline:none}
.qs-select:focus,.qs-input:focus{border-color:var(--border2)}
.qs-input{font-family:var(--mono)}
.intel-tooltip{position:fixed;z-index:600;background:var(--surface2);border:1px solid var(--border2);border-radius:12px;padding:10px 12px;width:220px;pointer-events:none;box-shadow:0 8px 32px rgba(0,0,0,0.5);opacity:0;transition:opacity 0.15s}
.intel-tooltip.visible{opacity:1}
.intel-tooltip-title{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.intel-point{display:flex;align-items:flex-start;gap:6px;font-size:10px;color:var(--text);margin-bottom:4px;line-height:1.4}
.intel-point:last-child{margin-bottom:0}
.intel-point-dot{width:4px;height:4px;border-radius:50%;background:var(--teal);flex-shrink:0;margin-top:5px}
.logic-overlay{position:fixed;inset:0;z-index:700;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center}
.logic-overlay.open{display:flex}
.logic-modal{background:var(--surface);border:1px solid var(--border2);border-radius:20px;width:min(560px,92vw);max-height:80vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 80px rgba(0,0,0,0.6)}
.logic-modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.logic-modal-title{font-size:13px;font-weight:700;color:var(--text);flex:1}
.logic-modal-close{background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:2px 4px;line-height:1}
.logic-modal-close:hover{color:var(--text)}
.logic-modal-body{padding:20px;overflow-y:auto;flex:1}
.logic-placeholder{text-align:center;padding:32px 0;color:var(--muted)}
.logic-placeholder-icon{font-size:32px;margin-bottom:10px}
.logic-placeholder-title{font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text)}
.logic-placeholder-sub{font-size:11px;color:var(--muted);line-height:1.6}

/* ── DASHBOARD PANELS ROW ── */
.dash-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:700px){.dash-row{grid-template-columns:1fr}}
.dash-panel-head{
  padding:8px 14px;display:flex;align-items:center;gap:8px;
  border-bottom:1px solid var(--border);
}
.dash-panel-title{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase}
.dash-panel-sub{font-size:9px;color:var(--dim)}
.dash-panel-right{margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)}

.status-strip{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-bottom:16px}
@media(max-width:700px){.status-strip{grid-template-columns:1fr 1fr}}
.agent-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}
@media(max-width:700px){.agent-grid{grid-template-columns:1fr}}
/* ── RESPONSIVE ── */
@media(max-width:640px){
  .header{padding:0 10px}
  .status-pill{min-width:160px}
  .page{padding:14px}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .intel-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- HEADER -->
<header class="header">
  <!-- Left: hamburger + wordmark -->
  <div class="hdr-left">
    <button class="hdr-hamburger" onclick="toggleSidebar()" aria-label="Open menu">
      <span></span><span></span><span></span>
    </button>
    <div class="wordmark">SYNTHOS</div>
  </div>

  <!-- Centre: market status pill -->
  <div class="status-pill sp-dim" id="pill-market-clock">
    <div class="status-dot dot-dim" id="clock-dot"></div>
    <span id="clock-label">Market</span>
    <span id="clock-countdown" style="font-family:var(--mono);font-size:10px;color:var(--dim);margin-left:4px"></span>
  </div>

  <!-- Right: mode pill + avatar -->
  <div class="hdr-right">
    <div class="hdr-mode-pill {% if operating_mode == 'AUTOMATIC' %}mp-auto{% endif %}"
            id="mode-nav-btn" style="cursor:default"
            title="{% if operating_mode == 'AUTOMATIC' %}Automatic — bot executes trades{% else %}Managed — you approve all trades{% endif %}">
      {% if operating_mode == 'AUTOMATIC' %}AUTO{% else %}MANAGED{% endif %}
    </div>
    <div class="hdr-bell" id="hdr-bell" onclick="toggleBellDrop(event)" aria-label="Notifications">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
      </svg>
      <div class="bell-badge" id="bell-badge"></div>
      <div class="bell-drop" id="bell-drop" onclick="event.stopPropagation()">
        <div class="bell-drop-head">
          <div class="bell-drop-title">Notifications</div>
          <button class="bell-mark-read" onclick="markAllNotifRead()">Mark all read</button>
        </div>
        <div class="bell-tabs">
          <button class="bell-tab active" data-cat="" onclick="switchBellTab(this)">All</button>
          <button class="bell-tab" data-cat="system" onclick="switchBellTab(this)">System</button>
          <button class="bell-tab" data-cat="daily" onclick="switchBellTab(this)">Daily</button>
          <button class="bell-tab" data-cat="account" onclick="switchBellTab(this)">Account</button>
        </div>
        <div class="bell-list" id="bell-list">
          <div class="bell-empty">No notifications</div>
        </div>
      </div>
    </div>
    <div class="hdr-avatar" id="hdr-avatar" onclick="toggleAvatarDrop(event)" aria-label="Account menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
           stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="8" r="4"/>
        <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
      </svg>
      <div class="avatar-drop" id="avatar-drop" onclick="event.stopPropagation()">
        <button class="avatar-drop-item" onclick="showTab('settings');closeAvatarDrop()">Settings</button>
        <div class="avatar-drop-sep"></div>
        <a href="/logout" class="avatar-drop-item danger">Sign Out</a>
      </div>
    </div>
  </div>
</header>

<!-- SIDEBAR OVERLAY -->
<div class="sidebar-overlay" id="sidebar-overlay" onclick="closeSidebar()"></div>

<!-- LEFT SIDEBAR (hamburger menu) -->
<div class="sidebar" id="sidebar">
  <div class="sidebar-head">
    <div class="wordmark">SYNTHOS</div>
    <button class="sidebar-close" onclick="closeSidebar()">&#x2715;</button>
  </div>
  <div class="sidebar-nav">
    <button class="sidebar-nav-btn active" id="snb-dashboard" onclick="showTab('dashboard');closeSidebar()">Dashboard</button>
    <button class="sidebar-nav-btn" id="snb-intel"       onclick="showTab('intel');closeSidebar()">Intelligence</button>
    <button class="sidebar-nav-btn" id="snb-news"        onclick="showTab('news');closeSidebar()">News</button>
    <button class="sidebar-nav-btn" id="snb-screening"   onclick="showTab('screening');closeSidebar()">Screening</button>
    <button class="sidebar-nav-btn" id="snb-performance" onclick="showTab('performance');closeSidebar()">Performance</button>
    <button class="sidebar-nav-btn" id="snb-risk"        onclick="showTab('risk');closeSidebar()">Risk</button>
    <div class="sidebar-nav-sep"></div>
    <button class="sidebar-nav-btn" id="snb-billing"    onclick="showTab('billing');closeSidebar()">Billing</button>
    <button class="sidebar-nav-btn" id="snb-settings"   onclick="showTab('settings');closeSidebar()">Settings</button>
    <button class="sidebar-nav-btn" id="snb-messages"   onclick="showTab('messages');closeSidebar()">Messages</button>
    <button class="sidebar-nav-btn" id="snb-support" onclick="toggleSupportPanel();closeSidebar()" style="color:var(--amber)">Support</button>
  </div>
</div>

<!-- CONFIG TAB (right edge) -->
<div class="cfg-tab" id="cfg-tab" onclick="toggleConfigPanel()" title="Configure agent">
  <span class="cfg-tab-icon">&#9881;</span>
  <span class="cfg-tab-label">Configure</span>
</div>

<!-- CONFIG OVERLAY -->
<div class="cfg-overlay" id="cfg-overlay" onclick="closeConfigPanel()"></div>

<!-- CONFIG PANEL (slides in from right) -->
<div class="cfg-panel" id="cfg-panel">
  <div class="cfg-panel-head">
    <div class="cfg-panel-title">Agent Configuration</div>
    <button class="cfg-panel-close" onclick="closeConfigPanel()">&#x2715;</button>
  </div>
  <div class="cfg-panel-body">

        <div class="cfg-section">Trading Parameters</div>
    <div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap">
      <button class="preset-btn" data-preset="conservative" onclick="applyPreset('conservative',this)"
        style="padding:6px 14px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid rgba(0,245,212,0.15);background:rgba(0,245,212,0.06);color:var(--teal);cursor:pointer;font-family:var(--sans);transition:all .15s">Conservative</button>
      <button class="preset-btn" data-preset="moderate" onclick="applyPreset('moderate',this)"
        style="padding:6px 14px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid rgba(123,97,255,0.15);background:rgba(123,97,255,0.06);color:var(--purple);cursor:pointer;font-family:var(--sans);transition:all .15s">Moderate</button>
      <button class="preset-btn" data-preset="aggressive" onclick="applyPreset('aggressive',this)"
        style="padding:6px 14px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid rgba(255,75,110,0.15);background:rgba(255,75,110,0.06);color:var(--pink);cursor:pointer;font-family:var(--sans);transition:all .15s">Aggressive</button>
      <button class="preset-btn" data-preset="custom" onclick="applyPreset('custom',this)"
        style="padding:6px 14px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.03);color:var(--muted);cursor:pointer;font-family:var(--sans);transition:all .15s">Custom</button>
    </div>
    <div id="cfg-controls-wrap" style="display:flex;flex-direction:column;gap:12px;opacity:0.35;pointer-events:none">

      <div>
        <div class="setting-label">Trading Mode</div>
        <div class="setting-desc">Paper mode simulates trades without real money</div>
        <select id="cfg-trading-mode" class="glass-select" style="margin-top:6px;width:100%">
          <option value="PAPER">PAPER — Simulated trades</option>
          <option value="LIVE">LIVE — Real money</option>
        </select>
      </div>

      <div>
        <div class="setting-label">Min Confidence</div>
        <div class="setting-desc">Minimum signal confidence to trade on</div>
        <select id="cfg-min-conf" class="glass-select" style="margin-top:6px;width:100%">
          <option value="LOW">LOW — Trade aggressively</option>
          <option value="MEDIUM">MEDIUM — Balanced</option>
          <option value="HIGH">HIGH — High confidence only</option>
        </select>
      </div>

      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <div style="flex:1;min-width:100px">
          <div class="setting-label">Max Position</div>
          <div class="setting-desc">% of portfolio per trade</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
            <input id="cfg-max-pos" type="number" min="1" max="50" class="glass-input" placeholder="10" style="width:60px">
            <span style="font-size:11px;color:var(--muted)">%</span>
          </div>
        </div>
        <div style="flex:1;min-width:100px">
          <div class="setting-label">Max Trade</div>
          <div class="setting-desc">Per-order dollar cap</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
            <span style="font-size:11px;color:var(--muted)">$</span>
            <input id="cfg-max-trade-usd" type="number" min="0" class="glass-input" placeholder="0" style="width:80px">
          </div>
        </div>
        <div style="flex:1;min-width:100px">
          <div class="setting-label">Max Positions</div>
          <div class="setting-desc">Open at once</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
            <input id="cfg-max-positions" type="number" min="1" max="50" class="glass-input" placeholder="10" style="width:60px">
          </div>
        </div>
      </div>

      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <div style="flex:1;min-width:100px">
          <div class="setting-label">Daily Loss Limit</div>
          <div class="setting-desc">Halt trading after this loss</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
            <span style="font-size:11px;color:var(--muted)">$</span>
            <input id="cfg-max-daily-loss" type="number" min="0" class="glass-input" placeholder="500" style="width:80px">
          </div>
        </div>
        <div style="flex:1;min-width:120px">
          <div class="setting-label">Close Mode</div>
          <div class="setting-desc">End-of-day behavior</div>
          <select id="cfg-close-mode" class="glass-select" style="margin-top:6px;width:100%">
            <option value="conservative">Conservative</option>
            <option value="moderate">Moderate</option>
            <option value="aggressive">Aggressive</option>
          </select>
        </div>
      </div>

      <div style="margin-top:6px">
        <button onclick="document.getElementById('cfg-advanced').style.display=document.getElementById('cfg-advanced').style.display==='none'?'block':'none'" style="background:none;border:none;color:var(--muted);font-size:11px;cursor:pointer;font-family:var(--sans);padding:0">
          ▶ Advanced Settings
        </button>
      </div>

      <div id="cfg-advanced" style="display:none;border-top:1px solid var(--border);padding-top:12px">
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
          <div style="flex:1;min-width:100px">
            <div class="setting-label">Max Drawdown</div>
            <div class="setting-desc">Portfolio drawdown halt %</div>
            <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
              <input id="cfg-max-drawdown" type="number" min="1" max="50" class="glass-input" placeholder="15" style="width:60px">
              <span style="font-size:11px;color:var(--muted)">%</span>
            </div>
          </div>
          <div style="flex:1;min-width:100px">
            <div class="setting-label">Max Sector</div>
            <div class="setting-desc">Concentration limit %</div>
            <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
              <input id="cfg-max-sector" type="number" min="1" max="100" class="glass-input" placeholder="25" style="width:60px">
              <span style="font-size:11px;color:var(--muted)">%</span>
            </div>
          </div>
          <div style="flex:1;min-width:100px">
            <div class="setting-label">Max Hold</div>
            <div class="setting-desc">Days before forced exit</div>
            <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
              <input id="cfg-max-hold-days" type="number" min="1" max="90" class="glass-input" placeholder="15" style="width:60px">
              <span style="font-size:11px;color:var(--muted)">days</span>
            </div>
          </div>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
          <div style="flex:1;min-width:100px">
            <div class="setting-label">Max Exposure</div>
            <div class="setting-desc">Portfolio % deployed</div>
            <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
              <input id="cfg-max-exposure" type="number" min="10" max="100" class="glass-input" placeholder="80" style="width:60px">
              <span style="font-size:11px;color:var(--muted)">%</span>
            </div>
          </div>
          <div style="flex:1;min-width:100px">
            <div class="setting-label">Profit Target</div>
            <div class="setting-desc">Take profit ratio (Nx risk)</div>
            <div style="display:flex;align-items:center;gap:5px;margin-top:6px">
              <input id="cfg-profit-target" type="number" min="1" max="10" step="0.5" class="glass-input" placeholder="2" style="width:60px">
              <span style="font-size:11px;color:var(--muted)">x</span>
            </div>
          </div>
          <div style="flex:1;min-width:120px">
            <div class="setting-label">Signal Staleness</div>
            <div class="setting-desc">Oldest signal to consider</div>
            <select id="cfg-staleness" class="glass-select" style="margin-top:6px;width:100%">
              <option value="Fresh">Fresh (≤3 days)</option>
              <option value="Aging">Aging (≤7 days)</option>
              <option value="Stale">Stale (≤14 days)</option>
              <option value="Expired">All (≤45 days)</option>
            </select>
          </div>
        </div>
      </div>
    </div>
    <div style="margin-top:14px">
      <button class="save-btn" onclick="saveCfgPanel()">Save Parameters</button>
    </div>

    <div class="cfg-section" style="margin-top:24px">Agent Mode</div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <div class="setting-row">
        <div>
          <div class="setting-label">Trading Mode</div>
          <div class="setting-desc">Automatic executes without approval; Managed requires your sign-off</div>
        </div>
      </div>
      <button class="save-btn" onclick="toggleMode();closeConfigPanel()"
              style="background:rgba(245,166,35,0.1);border-color:rgba(245,166,35,0.2);color:var(--amber)">
        Toggle Automatic / Managed
      </button>
    </div>

    <div class="cfg-section" style="margin-top:24px">Cash Management</div>
    <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:14px">
      <div class="setting-row" style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div class="setting-label">Treasury Reserve (BIL)</div>
          <div class="setting-desc">Automatically park idle cash in short-term Treasury ETF (BIL) for yield while waiting for trade signals. Disable to keep all idle funds as cash.</div>
        </div>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap">
          <input type="checkbox" id="cfg-bil-enabled" checked style="accent-color:var(--teal)">
          <span style="font-size:11px;color:var(--muted)" id="cfg-bil-label">Enabled</span>
        </label>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <div class="setting-label" style="margin:0">Reserve %</div>
        <input id="cfg-bil-reserve" type="number" min="0" max="50" class="glass-input" placeholder="20" style="width:60px">
        <span style="font-size:11px;color:var(--muted)">% of liquid capital</span>
      </div>
    </div>

    <div class="cfg-section" style="margin-top:24px">Kill Switch</div>
    <div style="font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:10px">
      Halt all trading immediately. Existing positions are held; no new orders will be placed.
    </div>
    <button class="save-btn" id="cfg-kill-btn"
            onclick="cfgKillToggle()"
            style="background:rgba(255,75,110,0.08);border-color:rgba(255,75,110,0.2);color:var(--pink)">
      Engage Kill Switch
    </button>

  </div>
</div>

{% if grace_warning %}
<!-- GRACE PERIOD WARNING BANNER -->
<div id="grace-banner" style="
  background:linear-gradient(90deg,rgba(245,158,11,0.18) 0%,rgba(245,158,11,0.08) 100%);
  border-bottom:2px solid var(--amber,#f59e0b);
  padding:10px 24px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
  flex-wrap:wrap;
">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:18px">⚠️</span>
    <div>
      <span style="font-size:13px;font-weight:700;color:var(--amber,#f59e0b)">Payment past due — your access expires soon.</span>
      <span style="font-size:12px;color:var(--muted);margin-left:8px">Update your payment method to keep your account active.</span>
    </div>
  </div>
  <a href="/subscribe?reason=past_due"
     style="display:inline-block;padding:7px 18px;border-radius:7px;background:var(--amber,#f59e0b);color:#1a1a2e;font-size:12px;font-weight:700;text-decoration:none;letter-spacing:0.03em;white-space:nowrap">
    Update Payment →
  </a>
</div>
{% endif %}


<div class="sup-overlay" id="sup-overlay" onclick="closeSupportPanel()"></div>
<div class="sup-panel" id="sup-panel">
  <div class="sup-head">
    <div class="sup-title">Contact Support</div>
    <button class="sup-close" onclick="closeSupportPanel()">&#x2715;</button>
  </div>
  <div class="sup-body">
    <div class="sup-cats">
      <button class="sup-cat active" data-cat="portal" onclick="supSetCat(this)">Portal Issue</button>
      <button class="sup-cat" data-cat="account" onclick="supSetCat(this)">Account Issue</button>
      <button class="sup-cat" data-cat="suggestion" onclick="supSetCat(this)">Suggestion</button>
    </div>
    <input class="sup-input" id="sup-subject" placeholder="Subject" maxlength="120">
    <textarea class="sup-input sup-textarea" id="sup-message" placeholder="Describe the issue or suggestion..."></textarea>
    <button class="sup-submit" id="sup-submit" onclick="supSubmitTicket()">Submit</button>

    <div class="sup-divider"></div>
    <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:10px">My Tickets</div>
    <div id="sup-tickets-list"><div style="font-size:11px;color:var(--dim);padding:12px 0;text-align:center">Loading...</div></div>
  </div>
</div>

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

<!-- NOTIFICATION MODAL -->
<div class="sig-modal-overlay" id="notif-modal-overlay" onclick="closeNotifModal(event)" style="display:none">
  <div class="sig-modal" id="notif-modal" style="max-width:520px;width:92vw">
    <div class="sig-modal-head" style="gap:10px">
      <div style="flex:1">
        <span class="bell-cat-pill" id="nmo-cat"></span>
        <div style="font-size:15px;font-weight:700;color:var(--text);line-height:1.45;margin-top:8px" id="nmo-title"></div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px;font-family:var(--mono)" id="nmo-time"></div>
      </div>
      <span class="sig-modal-close" onclick="closeNotifModal()" style="flex-shrink:0">&#x2715;</span>
    </div>
    <div class="sig-modal-body" id="nmo-body" style="font-size:13px;line-height:1.7;color:var(--text)"></div>
    <div id="nmo-beta-response" style="display:none;padding:0 20px 20px">
      <div style="height:1px;background:var(--border);margin-bottom:14px"></div>
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Beta Test Response</div>
      <div style="font-size:11px;color:var(--dim);line-height:1.6;margin-bottom:12px">
        Test the issue described above and report your findings below.<br>
        Select whether the issue is resolved, then provide a brief description for the backend team.
      </div>
      <div style="display:flex;gap:12px;margin-bottom:10px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--text)">
          <input type="radio" name="nmo-verdict" value="yes" onchange="nmoVerdictChanged()" style="accent-color:var(--teal)"> Yes, working correctly
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--text)">
          <input type="radio" name="nmo-verdict" value="no" onchange="nmoVerdictChanged()" style="accent-color:var(--pink)"> No, still broken
        </label>
      </div>
      <textarea id="nmo-beta-comment" style="width:100%;min-height:70px;background:var(--surface2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:11px;line-height:1.5;resize:vertical;outline:none" placeholder="Please add a brief response for the backend team..."></textarea>
      <button id="nmo-beta-submit" onclick="nmoSubmitBeta()" disabled style="width:100%;padding:10px;border:none;border-radius:8px;background:var(--teal);color:#000;font-size:12px;font-weight:700;cursor:pointer;margin-top:8px;opacity:0.4">Submit Response</button>
    </div>
  </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<!-- ══════════════ DASHBOARD TAB ══════════════ -->
<div class="page" id="tab-dashboard">

<!-- NEW ACCOUNT BANNER -->
<div id="new-account-banner" style="display:none;background:linear-gradient(90deg,rgba(245,166,35,0.12),rgba(245,166,35,0.04));border-bottom:2px solid var(--amber);padding:10px 24px;display:none;align-items:center;gap:10px">
  <span style="font-size:16px">&#x26A0;</span>
  <div style="flex:1">
    <span style="font-size:12px;font-weight:700;color:var(--amber)">New Account</span>
    <span style="font-size:11px;color:var(--muted);margin-left:8px">Dashboard values are defaults until the trading agent runs for the first time. Your account will update automatically.</span>
  </div>
</div>


  <!-- hidden compat elements JS writes to -->
  <span id="stat-mode" style="display:none">{{ status.operating_mode }}</span>
  <span id="stat-positions" style="display:none">0</span>
  <span id="stat-flags" style="display:none">0</span>
  <!-- positions-list moved to visible panel below -->
  <div id="agent-running-banner" style="display:none"></div>
  <div id="market-indices-bar" style="display:none"></div>
  <!-- market-chart moved to visible panel below -->

  <!-- STATUS STRIP -->
  <div class="status-strip">
    <div class="stat-card teal">
      <div class="stat-label">Portfolio</div>
      <div class="stat-val" id="stat-portfolio">$0.00</div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:3px">
        <span style="font-size:10px;font-weight:700" id="stat-day-pl">&#x2014;</span>
        <span style="font-size:9px;color:var(--dim)">today</span>
      </div>
    </div>
    <div class="stat-card purple">
      <div class="stat-label">Cash</div>
      <div class="stat-val" id="stat-cash">$0.00</div>
      <div style="display:flex;align-items:center;gap:5px;margin-top:3px">
        <span style="font-size:9px;color:var(--dim)">buying power</span>
        <span style="font-size:9px;font-weight:600;color:var(--muted)" id="stat-bp-pct"></span>
      </div>
    </div>
    <div class="stat-card amber">
      <div class="stat-label">Agent Mode</div>
      <div style="font-size:13px;font-weight:700;margin-top:4px;font-family:var(--mono)" id="mode-display">Loading</div>
      <div style="margin-top:5px"><button class="graph-tab" style="font-size:9px;padding:2px 10px" onclick="toggleMode()">Switch Mode</button></div>
    </div>
    <div class="stat-card pink">
      <div class="stat-label">Open Positions</div>
      <div class="stat-val" id="stat-pos-display">0</div>
      <div style="font-size:9px;color:var(--dim);margin-top:2px" id="stat-positions-sub">—</div>
    </div>
  </div>


  <!-- POSITIONS + CHART ROW -->
  <div class="dash-row">

    <!-- OPEN POSITIONS -->
    <div class="glass teal-glow">
      <div class="dash-panel-head">
        <div class="dash-panel-title">Open Positions</div>
        <div id="positions-count" class="dash-panel-sub">0 open</div>
        <div class="dash-panel-right" id="positions-refresh-ts"></div>
      </div>
      <div id="positions-list" style="max-height:260px;overflow-y:auto">
        <div class="empty-state" style="padding:16px 0"><div class="empty-icon">&#x1F4CA;</div>No open positions</div>
      </div>
    </div>

    <!-- PORTFOLIO GROWTH -->
    <div class="glass">
      <div class="dash-panel-head">
        <div class="dash-panel-title">Portfolio vs Benchmarks</div>
        <div class="dash-panel-sub" id="chart-period">36h</div>
        <div class="dash-panel-right">
          <button class="graph-tab" style="font-size:8px;padding:1px 6px" onclick="_chartHours=12;loadMarketChart()">12h</button>
          <button class="graph-tab" style="font-size:8px;padding:1px 6px" onclick="_chartHours=36;loadMarketChart()">36h</button>
          <button class="graph-tab" style="font-size:8px;padding:1px 6px" onclick="_chartHours=168;loadMarketChart()">7d</button>
        </div>
      </div>
      <div style="padding:8px 10px;position:relative;height:200px">
        <canvas id="market-chart" style="width:100%;height:100%"></canvas>
        <div id="market-chart-loading" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--muted)">Loading chart...</div>
      </div>
    </div>

  </div>

  <!-- 4-PANEL AGENT GRID -->
  <div class="agent-grid">

    <!-- PLANNING -->
    <div class="glass teal-glow">
      <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Planning</div>
        <div style="font-size:9px;color:var(--dim)">Signals under watch</div>
        <div style="margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)" id="planning-count"></div>
      </div>
      <div id="planning-list"><div class="empty-state"><div class="empty-icon">&#x1F50D;</div>Loading&#x2026;</div></div>
    </div>

    <!-- APPROVALS -->
    <div class="glass purple-glow">
      <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase" id="queue-label">Approvals</div>
        <div style="padding:1px 7px;border-radius:99px;font-size:9px;font-weight:700;background:var(--purple2);border:1px solid rgba(123,97,255,0.18);color:var(--purple)" id="pending-badge">0 pending</div>
      </div>
      <div id="approval-list" style="padding:0 14px 12px"><div class="empty-state"><div class="empty-icon">&#x2705;</div>No pending approvals</div></div>
    </div>

    <!-- HISTORY -->
    <div class="glass amber-glow">
      <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">History</div>
        <div style="font-size:9px;color:var(--dim)">Recent agent decisions</div>
        <div style="margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)" id="trader-activity-ts"></div>
      </div>
      <div id="history-list"><div class="empty-state"><div class="empty-icon">&#x26A1;</div>Loading&#x2026;</div></div>
    </div>



  </div>

</div>

<!-- INTEL TOOLTIP -->
<div class="intel-tooltip" id="intel-tooltip">
  <div class="intel-tooltip-title">Intelligence Points</div>
  <div id="intel-tooltip-body"></div>
</div>

<!-- LOGIC MODAL -->
<div class="logic-overlay" id="logic-overlay" onclick="closeLogicModal(event)">
  <div class="logic-modal">
    <div class="logic-modal-head">
      <div class="logic-modal-title" id="logic-modal-title">Agent Logic Breakdown</div>
      <div id="logic-modal-conf"></div>
      <button class="logic-modal-close" onclick="closeLogicModal()">&#xD7;</button>
    </div>
    <div class="logic-modal-body" id="logic-modal-body">
      <div class="logic-placeholder">
        <div class="logic-placeholder-icon">&#x1F9E0;</div>
        <div class="logic-placeholder-title">Logic Breakdown</div>
        <div class="logic-placeholder-sub">Full agent reasoning will appear here.</div>
      </div>
    </div>
  </div>
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
  <!-- ══ HERO SIGNAL RAIL — placeholder layout ══
       Wire to signals.db ranking when ready (see todo list):
       • Populate ticker + scores from top-2 ranked signals
       • Distance from consensus: analyst target delta
       • Sources: corroboration count from news agent
       • Confidence Δ: score change vs prior session
  -->
  <div class="hero-rail" id="hero-rail">

    <!-- Hero #1 — Highest conviction -->
    <div class="hero-card hc-cyan">
      <div class="hero-rank-badge hrb-cyan">#1 Conviction</div>
      <div class="hero-body">
        <div class="hero-ticker-row">
          <div class="hero-icon hi-cyan">—</div>
          <div class="hero-id">
            <div class="hero-label">Top Signal · Agent Score</div>
            <div class="hero-ticker">——</div>
          </div>
        </div>
        <div class="hero-conv">
          <div class="hero-conv-row">
            <span class="hero-conv-name">Synthos</span>
            <span class="hero-conv-val hcv-cyan">—</span>
          </div>
          <div class="hero-conv-track"><div class="hero-conv-fill hcf-cyan" style="width:0%"></div></div>
        </div>
        <div class="hero-conv" style="margin-bottom:12px">
          <div class="hero-conv-row">
            <span class="hero-conv-name">Market</span>
            <span class="hero-conv-val" style="color:var(--muted)">—</span>
          </div>
          <div class="hero-conv-track"><div class="hero-conv-fill hcf-cyan" style="width:0%;opacity:0.5"></div></div>
        </div>
        <div class="hero-meta-row">
          <div class="hm-item">
            <div class="hm-label">Consensus Δ</div>
            <div class="hm-val">—</div>
          </div>
          <div class="hm-item">
            <div class="hm-label">Sources</div>
            <div class="hm-val">—</div>
          </div>
          <div class="hm-item">
            <div class="hm-label">Score Δ</div>
            <div class="hm-val">—</div>
          </div>
        </div>
        <div class="hero-pending">Ranking integration pending</div>
      </div>
    </div>

    <!-- Hero #2 — Highest agent/market divergence -->
    <div class="hero-card hc-violet">
      <div class="hero-rank-badge hrb-violet">#1 Divergence</div>
      <div class="hero-body">
        <div class="hero-ticker-row">
          <div class="hero-icon hi-violet">—</div>
          <div class="hero-id">
            <div class="hero-label">Agent Edge · Market Spread</div>
            <div class="hero-ticker">——</div>
          </div>
        </div>
        <div class="hero-conv">
          <div class="hero-conv-row">
            <span class="hero-conv-name">Synthos</span>
            <span class="hero-conv-val hcv-violet">—</span>
          </div>
          <div class="hero-conv-track"><div class="hero-conv-fill hcf-violet" style="width:0%"></div></div>
        </div>
        <div class="hero-conv" style="margin-bottom:12px">
          <div class="hero-conv-row">
            <span class="hero-conv-name">Market</span>
            <span class="hero-conv-val" style="color:var(--muted)">—</span>
          </div>
          <div class="hero-conv-track"><div class="hero-conv-fill hcf-violet" style="width:0%;opacity:0.4"></div></div>
        </div>
        <div class="hero-meta-row">
          <div class="hm-item">
            <div class="hm-label">Agent − Mkt</div>
            <div class="hm-val">—</div>
          </div>
          <div class="hm-item">
            <div class="hm-label">Sources</div>
            <div class="hm-val">—</div>
          </div>
          <div class="hm-item">
            <div class="hm-label">Score Δ</div>
            <div class="hm-val">—</div>
          </div>
        </div>
        <div class="hero-pending">Ranking integration pending</div>
      </div>
    </div>

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

<!-- ══════════════ PERFORMANCE TAB ══════════════ -->
<div class="page" id="tab-performance" style="display:none">

  <!-- SUMMARY STATS -->
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px">
    <div class="stat-card teal">
      <div class="stat-label">Total Return</div>
      <div class="stat-val" id="perf-total-return">—</div>
      <div class="stat-sub" id="perf-total-sub">All time</div>
    </div>
    <div class="stat-card purple">
      <div class="stat-label">Win Rate</div>
      <div class="stat-val" id="perf-win-rate">—</div>
      <div class="stat-sub" id="perf-trades-sub">trades</div>
    </div>
    <div class="stat-card amber">
      <div class="stat-label">Avg Hold</div>
      <div class="stat-val" id="perf-avg-hold">—</div>
      <div class="stat-sub">per trade</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Sharpe Ratio</div>
      <div class="stat-val" id="perf-sharpe">—</div>
      <div class="stat-sub">risk-adjusted</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Max Drawdown</div>
      <div class="stat-val" id="perf-max-dd">—</div>
      <div class="stat-sub">from ATH</div>
    </div>
    <div class="stat-card teal">
      <div class="stat-label">vs S&P 500</div>
      <div class="stat-val" id="perf-vs-sp">—</div>
      <div class="stat-sub">alpha this month</div>
    </div>
  </div>

  <!-- P&L ATTRIBUTION + MILESTONES -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;align-items:start">

    <!-- P&L ATTRIBUTION -->
    <div class="glass">
      <div style="padding:10px 14px 8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">P&amp;L Attribution</div>
      </div>
      <div style="padding:12px 14px" id="pnl-attribution">
        <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Gains by sector this month</div>
        <div id="attr-bars">
          <div style="margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Technology</span><span style="color:var(--teal);font-family:var(--mono)" id="attr-tech">+$0.00</span></div>
            <div class="gauge-bar"><div class="gauge-fill" id="attr-tech-bar" style="width:0%;background:linear-gradient(90deg,var(--teal),rgba(0,245,212,0.22))"></div></div>
          </div>
          <div style="margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Healthcare</span><span style="color:var(--purple);font-family:var(--mono)" id="attr-health">+$0.00</span></div>
            <div class="gauge-bar"><div class="gauge-fill" id="attr-health-bar" style="width:0%;background:linear-gradient(90deg,var(--purple),rgba(123,97,255,0.22))"></div></div>
          </div>
          <div style="margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Financials</span><span style="color:var(--amber);font-family:var(--mono)" id="attr-fin">+$0.00</span></div>
            <div class="gauge-bar"><div class="gauge-fill" id="attr-fin-bar" style="width:0%;background:linear-gradient(90deg,var(--amber),rgba(245,166,35,0.4))"></div></div>
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Other</span><span style="color:var(--muted);font-family:var(--mono)" id="attr-other">+$0.00</span></div>
            <div class="gauge-bar"><div class="gauge-fill" id="attr-other-bar" style="width:0%;background:linear-gradient(90deg,var(--muted),rgba(255,255,255,0.2))"></div></div>
          </div>
        </div>
      </div>
    </div>

    <!-- MILESTONE BADGES -->
    <div class="glass">
      <div style="padding:10px 14px 8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Milestones</div>
      </div>
      <div style="padding:12px 14px">
        <div class="badge-grid" id="badge-grid">
          <div class="badge locked"><div class="badge-icon">🚀</div><div><div class="badge-name">First Trade</div><div class="badge-desc">Execute your first trade</div></div></div>
          <div class="badge locked"><div class="badge-icon">📈</div><div><div class="badge-name">First Win</div><div class="badge-desc">Close a profitable position</div></div></div>
          <div class="badge locked"><div class="badge-icon">🔥</div><div><div class="badge-name">3-Win Streak</div><div class="badge-desc">3 profitable sessions in a row</div></div></div>
          <div class="badge locked"><div class="badge-icon">💎</div><div><div class="badge-name">Beat the Market</div><div class="badge-desc">Outperform S&P for a week</div></div></div>
          <div class="badge locked"><div class="badge-icon">🛡️</div><div><div class="badge-name">Disciplined</div><div class="badge-desc">30 days without overriding the algo</div></div></div>
          <div class="badge locked"><div class="badge-icon">🏆</div><div><div class="badge-name">First $1k</div><div class="badge-desc">Realize $1,000 in gains</div></div></div>
        </div>
      </div>
    </div>

  </div>

  <!-- CLOSED TRADE HISTORY -->
  <div class="glass" style="margin-bottom:14px">
    <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Trade History</div>
      <div style="font-size:9px;color:var(--dim);margin-left:auto" id="history-count">Loading...</div>
    </div>
    <div style="overflow-x:auto">
      <table class="perf-table">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Side</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Hold</th>
            <th>P&amp;L</th>
            <th>Return</th>
            <th>vs S&amp;P</th>
          </tr>
        </thead>
        <tbody id="trade-history-body">
          <tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">No closed trades yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- TAX LOT VIEW -->
  <div class="glass" style="margin-bottom:14px">
    <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Tax Lots</div>
      <div style="padding:1px 7px;border-radius:99px;font-size:9px;font-weight:700;background:rgba(245,166,35,0.1);border:1px solid rgba(245,166,35,0.25);color:var(--amber)">Est. only · not tax advice</div>
    </div>
    <div style="padding:12px 14px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div style="padding:8px 12px;border-radius:8px;background:var(--surface2);border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.07em">Short-Term Gains</div>
          <div style="font-size:14px;font-weight:700;color:var(--amber);font-family:var(--mono)" id="tax-st">$0.00</div>
          <div style="font-size:9px;color:var(--dim)">Held &lt; 1 year · taxed as income</div>
        </div>
        <div style="padding:8px 12px;border-radius:8px;background:var(--surface2);border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.07em">Long-Term Gains</div>
          <div style="font-size:14px;font-weight:700;color:var(--teal);font-family:var(--mono)" id="tax-lt">$0.00</div>
          <div style="font-size:9px;color:var(--dim)">Held &gt; 1 year · reduced rate</div>
        </div>
      </div>
      <div style="font-size:10px;color:var(--dim);padding:6px 8px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid var(--border)">
        Consult a tax professional before making decisions based on this data. Synthos does not provide tax advice.
      </div>
    </div>
  </div>

</div>

<!-- ══════════════ RISK TAB ══════════════ -->
<div class="page" id="tab-risk" style="display:none">

  <!-- EXPOSURE METERS -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;align-items:start">

    <div class="glass">
      <div style="padding:10px 14px 8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Capital Exposure</div>
      </div>
      <div class="gauge-wrap">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
          <span style="font-size:11px;color:var(--muted)">Deployed</span>
          <span style="font-size:14px;font-weight:700;color:var(--text);font-family:var(--mono)" id="exposure-pct">—%</span>
        </div>
        <div class="gauge-bar"><div class="gauge-fill" id="exposure-bar" style="width:0%;background:linear-gradient(90deg,var(--teal),var(--purple))"></div></div>
        <div class="gauge-label"><span>0% cash</span><span>100% invested</span></div>
        <div style="margin-top:10px" id="sector-exposure">
          <div style="font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim);margin-bottom:6px">By Sector</div>
          <div style="font-size:10px;color:var(--muted);font-family:var(--mono)" id="sector-bars">Loading...</div>
        </div>
      </div>
    </div>

    <div class="glass">
      <div style="padding:10px 14px 8px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Risk Limits</div>
      </div>
      <div class="gauge-wrap">
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Max Position Size</span><span style="color:var(--text);font-family:var(--mono)">{{ settings.max_position_pct }}%</span></div>
          <div class="gauge-bar"><div class="gauge-fill" style="width:{{ settings.max_position_pct }}%;background:linear-gradient(90deg,var(--teal),rgba(0,245,212,0.22))"></div></div>
        </div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Max Sector Concentration</span><span style="color:var(--text);font-family:var(--mono)">{{ settings.max_sector_pct }}%</span></div>
          <div class="gauge-bar"><div class="gauge-fill" style="width:{{ settings.max_sector_pct }}%;background:linear-gradient(90deg,var(--purple),rgba(123,97,255,0.22))"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:3px"><span style="color:var(--muted)">Max Trade Size</span><span style="color:var(--text);font-family:var(--mono)" id="risk-max-trade">{{ settings.max_trade_usd|int }} USD</span></div>
          <div class="gauge-bar"><div class="gauge-fill" style="width:60%;background:linear-gradient(90deg,var(--amber),rgba(245,166,35,0.4))"></div></div>
        </div>
        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
          <a onclick="showTab('settings',event)" style="font-size:10px;color:var(--teal);cursor:pointer;text-decoration:none">Adjust risk parameters in Settings →</a>
        </div>
      </div>
    </div>

  </div>

  <!-- GATE SCORE HEATMAP -->
  <div class="glass" style="margin-bottom:14px">
    <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Gate Score Heatmap</div>
      <div style="font-size:9px;color:var(--dim)">Last 7 sessions · 14 gates per session</div>
      <div style="margin-left:auto;display:flex;gap:8px;align-items:center;font-size:9px;color:var(--dim)">
        <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:rgba(0,245,212,0.20);vertical-align:middle"></span>Pass
        <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:rgba(255,75,110,0.35);vertical-align:middle;margin-left:4px"></span>Fail
        <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:rgba(255,255,255,0.06);vertical-align:middle;margin-left:4px"></span>Skip
      </div>
    </div>
    <div style="padding:12px 14px;overflow-x:auto" id="gate-heatmap">
      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;display:flex;gap:4px" id="hm-session-labels"></div>
      <div style="font-size:9px;color:var(--dim);margin-bottom:6px">Gate →</div>
      <div id="hm-rows"></div>
      <div style="font-size:10px;color:var(--muted);margin-top:10px;font-family:var(--mono)" id="hm-stub">No session data yet — heatmap populates after first trading session</div>
    </div>
  </div>

  <!-- BACKTESTED vs LIVE -->
  <div class="glass" style="margin-bottom:14px">
    <div style="padding:10px 14px 8px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase">Strategy vs Benchmark</div>
      <div style="padding:1px 7px;border-radius:99px;font-size:9px;font-weight:700;background:rgba(123,97,255,0.1);border:1px solid rgba(123,97,255,0.14);color:var(--purple)">Live</div>
    </div>
    <div style="padding:12px 14px">
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px">
        <div style="text-align:center;padding:8px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.07em">Synthos</div>
          <div style="font-size:16px;font-weight:700;color:var(--teal);font-family:var(--mono)" id="bench-synthos">—%</div>
          <div style="font-size:9px;color:var(--dim)">this month</div>
        </div>
        <div style="text-align:center;padding:8px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.07em">S&amp;P 500</div>
          <div style="font-size:16px;font-weight:700;color:var(--muted);font-family:var(--mono)" id="bench-sp">—%</div>
          <div style="font-size:9px;color:var(--dim)">this month</div>
        </div>
        <div style="text-align:center;padding:8px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:0.07em">Alpha</div>
          <div style="font-size:16px;font-weight:700;font-family:var(--mono)" id="bench-alpha" style="color:var(--muted)">—%</div>
          <div style="font-size:9px;color:var(--dim)">outperformance</div>
        </div>
      </div>
      <div style="height:120px;position:relative;background:var(--surface2);border-radius:8px;border:1px solid var(--border);display:flex;align-items:center;justify-content:center">
        <span style="font-size:10px;color:var(--dim)">Comparison chart · Populates with trade history</span>
      </div>
    </div>
  </div>

</div>



<!-- ══════════════ SETUP GUIDE TAB ══════════════ -->
<div class="page" id="tab-guide" style="display:none">

  <div style="font-size:24px;font-weight:700;letter-spacing:-0.5px;color:var(--text);margin-bottom:4px">
    Getting <span style="background:linear-gradient(90deg,var(--teal),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Started</span>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:24px">Complete these steps to activate your trading agent</div>

  <!-- STEP 1: 2FA -->
  <div class="glass" style="padding:20px;margin-bottom:16px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <div style="width:28px;height:28px;border-radius:50%;background:rgba(0,245,212,0.1);color:var(--teal);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">1</div>
      <div style="font-size:15px;font-weight:700">Set Up Two-Factor Authentication</div>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:12px">
      Protect your account with an authenticator app. We recommend <strong>Microsoft Authenticator</strong> — it works with Synthos and your Alpaca trading account.
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
      <a href="https://apps.apple.com/us/app/microsoft-authenticator/id983156458" target="_blank" rel="noopener"
         style="display:flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:var(--text);font-size:11px;font-weight:600;text-decoration:none">
        &#xF8FF; iPhone (App Store)
      </a>
      <a href="https://play.google.com/store/apps/details?id=com.azure.authenticator" target="_blank" rel="noopener"
         style="display:flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:var(--text);font-size:11px;font-weight:600;text-decoration:none">
        &#x25B6; Android (Google Play)
      </a>
    </div>
    <div style="font-size:11px;color:var(--dim);line-height:1.6">
      After installing, open the app and add your Alpaca account using the QR code from Alpaca's security settings.
    </div>
  </div>

  <!-- STEP 2: Alpaca Account -->
  <div class="glass" style="padding:20px;margin-bottom:16px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <div style="width:28px;height:28px;border-radius:50%;background:rgba(123,97,255,0.1);color:var(--purple);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">2</div>
      <div style="font-size:15px;font-weight:700">Create Your Alpaca Trading Account</div>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:12px">
      Alpaca is the brokerage that executes your trades. Synthos connects to Alpaca through their API — a secure key that gives the trading agent permission to place orders on your behalf. <strong>You maintain full control of your Alpaca account at all times.</strong>
    </div>
    <a href="https://alpaca.markets" target="_blank" rel="noopener"
       style="display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;background:rgba(123,97,255,0.08);border:1px solid rgba(123,97,255,0.2);color:var(--purple);font-size:11px;font-weight:600;text-decoration:none;margin-bottom:12px">
      Visit alpaca.markets &rarr;
    </a>
    <div style="font-size:11px;color:var(--dim);line-height:1.6">
      Sign up for a free account. Start with <strong>Paper Trading</strong> (simulated) to test the system before using real money.
    </div>
  </div>

  <!-- STEP 3: Get API Keys -->
  <div class="glass" style="padding:20px;margin-bottom:16px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <div style="width:28px;height:28px;border-radius:50%;background:rgba(245,166,35,0.1);color:var(--amber);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">3</div>
      <div style="font-size:15px;font-weight:700">Find Your Alpaca API Keys</div>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:14px">
      API keys are like a password that lets Synthos communicate with your Alpaca account. You need two keys: an <strong>API Key</strong> and a <strong>Secret Key</strong>.
    </div>
    <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px;margin-bottom:12px">
      <div style="font-size:11px;font-weight:700;color:var(--amber);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.06em">How to find your keys:</div>
      <ol style="font-size:12px;color:var(--text);line-height:1.8;padding-left:18px;margin:0">
        <li>Log in to your Alpaca account at <a href="https://app.alpaca.markets" target="_blank" style="color:var(--teal)">app.alpaca.markets</a></li>
        <li>Click your profile icon (top right) &rarr; select <strong>API Keys</strong></li>
        <li>Click <strong>Generate New Key</strong> (or <strong>Regenerate</strong> if you already have one)</li>
        <li>Copy both the <strong>API Key ID</strong> (starts with PK...) and the <strong>Secret Key</strong></li>
        <li><span style="color:var(--pink)">Important:</span> The Secret Key is only shown once. Save it securely.</li>
      </ol>
    </div>
    <div style="font-size:11px;color:var(--dim);line-height:1.6">
      Make sure you are generating keys for <strong>Paper Trading</strong> (not Live) until you are ready to trade with real money.
    </div>
  </div>

  <!-- STEP 4: Enter Keys -->
  <div class="glass" style="padding:20px;margin-bottom:16px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <div style="width:28px;height:28px;border-radius:50%;background:rgba(255,75,110,0.1);color:var(--pink);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">4</div>
      <div style="font-size:15px;font-weight:700">Connect Your Keys to Synthos</div>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:14px">
      Paste your API keys into the Settings page. Synthos encrypts and stores them securely — they never leave this system.
    </div>
    <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:14px;margin-bottom:12px">
      <ol style="font-size:12px;color:var(--text);line-height:1.8;padding-left:18px;margin:0">
        <li>Go to <a href="#" onclick="showTab('settings');return false" style="color:var(--teal)">Settings</a> in the sidebar</li>
        <li>Find the <strong>API Keys</strong> section</li>
        <li>Paste your <strong>Alpaca API Key</strong> and click Update</li>
        <li>Paste your <strong>Alpaca Secret Key</strong> and click Update</li>
        <li>Click the <strong style="color:var(--purple)">Test</strong> button to verify the connection</li>
      </ol>
    </div>
    <button class="save-btn" onclick="showTab('settings')" style="margin-top:4px">
      Go to Settings &rarr;
    </button>
  </div>

  <!-- STEP 5: Configure -->
  <div class="glass" style="padding:20px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <div style="width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,0.06);color:var(--text);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">5</div>
      <div style="font-size:15px;font-weight:700">Choose Your Trading Style</div>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:12px">
      Open the <strong>Agent Configuration</strong> panel (gear icon on the right edge of the screen) and select a preset: <strong style="color:var(--teal)">Conservative</strong>, <strong style="color:var(--purple)">Moderate</strong>, or <strong style="color:var(--pink)">Aggressive</strong>. You can customize individual parameters later.
    </div>
    <div style="font-size:11px;color:var(--dim);line-height:1.6">
      The agent starts in <strong>Paper Trading</strong> mode by default. It will simulate trades using real market data without risking real money. Switch to <strong>Live</strong> only when you are confident in the system.
    </div>
  </div>

  <div style="text-align:center;padding:20px 0">
    <button class="save-btn" onclick="markSetupComplete()" style="padding:10px 24px;font-size:13px">
      I've completed setup &mdash; don't show this again
    </button>
  </div>

</div>


<!-- ══════════════ MESSAGES TAB ══════════════ -->
<div class="page" id="tab-messages" style="display:none">

  <div style="font-size:24px;font-weight:700;letter-spacing:-0.5px;color:var(--text);margin-bottom:4px">
    <span style="background:linear-gradient(90deg,var(--teal),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Messages</span>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:20px">Direct messages from the Synthos team</div>

  <div id="msg-list"><div style="text-align:center;padding:40px 0;color:var(--dim);font-size:12px">Loading...</div></div>
  <div id="msg-detail"></div>
</div>

<!-- ══════════════ BILLING TAB ══════════════ -->
<div class="page" id="tab-billing" style="display:none">

  <div style="font-size:24px;font-weight:700;letter-spacing:-0.5px;color:var(--text);margin-bottom:4px">
    <span style="background:linear-gradient(90deg,var(--teal),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Billing</span>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-bottom:24px">Subscription status, payment history, and invoices</div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:24px">
    <div class="glass" style="padding:20px;text-align:center">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Subscription</div>
      <div style="font-size:18px;font-weight:700;color:var(--teal)" id="bill-status">—</div>
      <div style="font-size:10px;color:var(--dim);margin-top:4px" id="bill-plan">Module pending</div>
    </div>
    <div class="glass" style="padding:20px;text-align:center">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Next Billing</div>
      <div style="font-size:18px;font-weight:700;color:var(--text)" id="bill-next">—</div>
      <div style="font-size:10px;color:var(--dim);margin-top:4px">Module pending</div>
    </div>
    <div class="glass" style="padding:20px;text-align:center">
      <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px">Pricing Tier</div>
      <div style="font-size:18px;font-weight:700;color:var(--amber)" id="bill-tier">—</div>
      <div style="font-size:10px;color:var(--dim);margin-top:4px">Module pending</div>
    </div>
  </div>

  <div class="glass" style="padding:20px;margin-bottom:16px">
    <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px">Payment History</div>
    <div style="text-align:center;padding:30px 0;color:var(--dim);font-size:12px">
      Payment history will appear here once Stripe integration is active.
    </div>
  </div>

  <div class="glass" style="padding:20px">
    <div style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px">Manage Subscription</div>
    <div style="text-align:center;padding:20px 0;color:var(--dim);font-size:12px">
      Subscription management coming soon.
    </div>
  </div>

</div>

<!-- ══════════════ SETTINGS TAB ══════════════ -->
<div class="page" id="tab-settings" style="display:none">


  <div style="padding:14px 16px;background:rgba(0,245,212,0.04);border:1px solid rgba(0,245,212,0.12);border-radius:10px;margin-bottom:16px;display:flex;align-items:center;gap:12px">
    <div style="font-size:24px">&#x1F6E0;</div>
    <div style="flex:1">
      <div style="font-size:13px;font-weight:700;color:var(--teal)">New to Synthos?</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px">Follow the setup guide to connect your trading account and secure your login.</div>
    </div>
    <button class="save-btn" onclick="showTab('guide')" style="white-space:nowrap;padding:6px 14px;font-size:11px">Setup Guide &rarr;</button>
  </div>

  <div class="section-title">API Keys</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div style="font-size:11px;color:var(--muted);margin-bottom:12px;line-height:1.6;padding:8px 10px;background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.15);border-radius:8px">
        &#9888; Keys are written to this Pi's secure store. Enter a new value and click <strong>Update</strong> to overwrite.
      </div>

      <!-- Alpaca API Key -->
      <div style="margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
          <div class="setting-label" style="margin:0">Alpaca API Key</div>
          <span id="alpaca-key-check" style="display:none;color:#00f5d4;font-size:14px">&#10003;</span>
        </div>
        <div style="font-size:10px;font-family:var(--mono);color:var(--dim);margin-bottom:6px;min-height:14px" id="obs-alpaca-key"></div>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="glass-input" type="text" id="k-alpaca-key" placeholder="Paste API Key here" autocomplete="off" style="width:100%;font-family:var(--mono);font-size:11px">
          <button type="button" class="save-btn" style="padding:5px 10px;font-size:10px;white-space:nowrap" onclick="updateKey('ALPACA_API_KEY','k-alpaca-key','obs-alpaca-key')">Update</button>
        </div>
      </div>

      <!-- Alpaca Secret Key -->
      <div style="margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
          <div class="setting-label" style="margin:0">Alpaca Secret Key</div>
          <span id="alpaca-secret-check" style="display:none;color:#00f5d4;font-size:14px">&#10003;</span>
        </div>
        <div style="font-size:10px;font-family:var(--mono);color:var(--dim);margin-bottom:6px;min-height:14px" id="obs-alpaca-secret"></div>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="glass-input" type="text" id="k-alpaca-secret" placeholder="Paste Secret Key here" autocomplete="off" style="width:100%;font-family:var(--mono);font-size:11px">
          <button type="button" class="save-btn" style="padding:5px 10px;font-size:10px;white-space:nowrap" onclick="updateKey('ALPACA_SECRET_KEY','k-alpaca-secret','obs-alpaca-secret')">Update</button>
        </div>
      </div>

      <div style="margin-bottom:8px">
        <button type="button" class="save-btn" style="padding:8px 16px;font-size:11px;width:100%;background:rgba(123,97,255,0.08);border-color:rgba(123,97,255,0.2);color:var(--purple)" onclick="testAlpacaKeys()">&#x1F50D; Test Alpaca Connection</button>
      </div>
      <div id="alpaca-test-result" style="display:none;padding:8px 12px;border-radius:8px;font-size:11px;margin-top:-4px;margin-bottom:8px"></div>

      <!-- Trading Mode -->
      <div class="setting-row" style="align-items:flex-start;gap:10px">
        <div style="flex:1">
          <div class="setting-label">Trading Mode</div>
          <div class="setting-desc">Paper trades safely; Live requires operator approval</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <select class="glass-input" id="k-trading-mode" style="width:140px">
            <option value="paper">Paper Trading</option>
            <option value="live" id="k-live-option" disabled>Live Trading</option>
          </select>
          <button class="save-btn" style="padding:5px 10px;font-size:10px;white-space:nowrap" onclick="updateTradingMode()">Update</button>
        </div>
      </div>

      <!-- Alert Email (To) -->
      <div class="setting-row" style="align-items:flex-start;gap:10px">
        <div style="flex:1">
          <div class="setting-label">Alert Email</div>
          <div class="setting-desc">Trade alerts destination &#x2014; if different from your account email</div>
          <div style="font-size:9px;font-family:var(--mono);color:var(--dim);margin-top:3px" id="obs-alert-to">Loading&#x2026;</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="glass-input" type="email" id="k-alert-to" placeholder="you@email.com" style="width:170px">
          <button class="save-btn" style="padding:5px 10px;font-size:10px;white-space:nowrap" onclick="updateKey('ALERT_TO','k-alert-to','obs-alert-to')">Update</button>
        </div>
      </div>

    </div>
  </div>

  <!-- KEY OVERWRITE CONFIRM POPUP -->
  <div id="key-confirm-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:900;align-items:center;justify-content:center">
    <div style="background:var(--surface);border:1px solid var(--border2);border-radius:16px;padding:24px;width:320px;text-align:center">
      <div style="font-size:13px;color:var(--text);margin-bottom:8px;font-weight:700" id="key-confirm-title">Overwrite Key?</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:16px;line-height:1.5" id="key-confirm-msg"></div>
      <div style="display:flex;gap:10px;justify-content:center">
        <button onclick="document.getElementById('key-confirm-overlay').style.display='none'" style="padding:8px 18px;border-radius:9px;background:transparent;border:1px solid var(--border2);color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;font-family:var(--sans)">Cancel</button>
        <button id="key-confirm-btn" style="padding:8px 18px;border-radius:9px;background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.18);color:var(--teal);font-size:12px;font-weight:700;cursor:pointer;font-family:var(--sans)">Confirm Update</button>
      </div>
    </div>
  </div>

  <!-- Old operator API Keys moved to Monitor Settings (/settings on command portal) -->

  <div class="section-title">Trading Parameters</div>
  <div class="glass" style="margin-bottom:16px">
    <div style="padding:24px;text-align:center">
      <div style="font-size:13px;color:var(--muted);margin-bottom:12px">
        Trading parameters are configured per-account in the Agent Configuration panel.
      </div>
      <button class="save-btn" onclick="toggleConfigPanel()" style="max-width:260px">
        Open Agent Configuration &#x2192;
      </button>
    </div>
  </div>

  <div class="section-title">Alert Preferences</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div class="setting-row">
        <div><div class="setting-label">Trade Executed</div><div class="setting-desc">Email when the algo enters or exits a position</div></div>
        <label class="toggle"><input type="checkbox" id="alert-trade" checked><div class="toggle-slider"></div></label>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Kill Switch Triggered</div><div class="setting-desc">Immediate alert when trading is halted</div></div>
        <label class="toggle"><input type="checkbox" id="alert-kill" checked><div class="toggle-slider"></div></label>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Daily Digest</div><div class="setting-desc">Morning summary of signals and planned activity</div></div>
        <label class="toggle"><input type="checkbox" id="alert-digest"><div class="toggle-slider"></div></label>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Weekly Performance Summary</div><div class="setting-desc">Friday recap — returns, trades, vs benchmark</div></div>
        <label class="toggle"><input type="checkbox" id="alert-weekly"><div class="toggle-slider"></div></label>
      </div>
      <div class="setting-row">
        <div><div class="setting-label">Portfolio Drop Alert</div><div class="setting-desc">Alert if portfolio falls more than X% in a day</div></div>
        <div style="display:flex;align-items:center;gap:6px">
          <input class="glass-input" type="number" id="alert-drop-pct" min="1" max="50" value="5" style="width:60px">
          <span style="font-size:11px;color:var(--muted)">%</span>
        </div>
      </div>
    </div>
    <div style="padding:0 18px 16px;display:flex;align-items:center;gap:10px">
      <button class="save-btn" onclick="saveAlertPrefs()">Save Preferences</button>
      <span style="font-size:10px;color:var(--muted)">Alerts sent to your registered email</span>
    </div>
  </div>

  <div class="section-title">My Account</div>
  <div class="glass" style="margin-bottom:16px">
    <div class="settings-section">
      <div style="margin-bottom:18px;padding-bottom:18px;border-bottom:1px solid var(--border)">
        <div style="font-size:10px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Change Email</div>
        <div style="display:flex;flex-direction:column;gap:8px;max-width:320px">
          <input class="glass-input" type="email" id="acct-new-email" placeholder="New email address" style="width:100%">
          <input class="glass-input" type="password" id="acct-email-pw" placeholder="Current password to confirm" style="width:100%">
          <button class="save-btn" onclick="changeEmail()">Update Email</button>
          <div id="acct-email-status" style="font-size:10px;color:var(--muted)"></div>
        </div>
      </div>
      <div>
        <div style="font-size:10px;font-weight:700;letter-spacing:0.07em;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Change Password</div>
        <div style="display:flex;flex-direction:column;gap:8px;max-width:320px">
          <input class="glass-input" type="password" id="acct-cur-pw" placeholder="Current password" style="width:100%">
          <input class="glass-input" type="password" id="acct-new-pw" placeholder="New password (min 8 characters)" style="width:100%">
          <input class="glass-input" type="password" id="acct-confirm-pw" placeholder="Confirm new password" style="width:100%">
          <button class="save-btn" onclick="changePassword()">Update Password</button>
          <div id="acct-pw-status" style="font-size:10px;color:var(--muted)"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="section-title">Notification Center</div>
  <div class="glass" style="margin-bottom:16px">
    <div style="padding:10px 14px 8px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:10px;color:var(--muted)">Recent system notifications</div>
      <div style="margin-left:auto;font-size:9px;color:var(--dim);font-family:var(--mono)" id="notif-ts"></div>
    </div>
    <div id="notif-list" style="padding:8px 14px 10px">
      <div style="font-size:10px;color:var(--dim);text-align:center;padding:12px 0">No notifications</div>
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


</div>

<!-- POSITION DETAIL DRAWER -->
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer(event)">
</div>
<div class="drawer" id="pos-drawer">
  <div class="drawer-head">
    <div id="drawer-icon" style="width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;flex-shrink:0"></div>
    <div>
      <div style="font-size:14px;font-weight:700;color:var(--text)" id="drawer-ticker">—</div>
      <div style="font-size:10px;color:var(--muted)" id="drawer-sub">—</div>
    </div>
    <button class="drawer-close" onclick="closeDrawer()">✕</button>
  </div>
  <div class="drawer-body">
    <div class="drawer-section">
      <div class="drawer-section-title">Position</div>
      <div class="drawer-row"><span class="drawer-label">Market Value</span><span class="drawer-val" id="dr-mktval">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Shares</span><span class="drawer-val" id="dr-shares">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Avg Entry</span><span class="drawer-val" id="dr-entry">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Current Price</span><span class="drawer-val" id="dr-price">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Cost Basis</span><span class="drawer-val" id="dr-basis">—</span></div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-title">Returns</div>
      <div class="drawer-row"><span class="drawer-label">Unrealized P&amp;L</span><span class="drawer-val" id="dr-unreal">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Unrealized %</span><span class="drawer-val" id="dr-unreal-pct">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Today's P&amp;L</span><span class="drawer-val" id="dr-day-pl">—</span></div>
      <div class="drawer-row"><span class="drawer-label">Today %</span><span class="drawer-val" id="dr-day-pct">—</span></div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-title">Entry Conditions</div>
      <div style="height:80px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;margin-bottom:8px">
        <span style="font-size:10px;color:var(--dim)">Gate scores at entry · Available after wiring</span>
      </div>
      <div id="dr-gate-scores" style="font-size:10px;color:var(--muted)"></div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-title">Price Chart</div>
      <div style="height:120px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center">
        <span style="font-size:10px;color:var(--dim)" id="drawer-chart-stub">Chart · Available after wiring</span>
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

// ── SIDEBAR ──
function toggleSidebar(){
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-overlay').classList.toggle('open');
}
function closeSidebar(){
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('open');
}

// ── AVATAR DROPDOWN ──
function toggleAvatarDrop(e){
  e.stopPropagation();
  document.getElementById('avatar-drop').classList.toggle('open');
}
function closeAvatarDrop(){
  document.getElementById('avatar-drop').classList.remove('open');
}
document.addEventListener('click', function(e){
  const d = document.getElementById('avatar-drop');
  if (d) d.classList.remove('open');
  // Close bell dropdown on outside click
  const bell = document.getElementById('hdr-bell');
  const bdrop = document.getElementById('bell-drop');
  if (bell && bdrop && !bell.contains(e.target)) bdrop.classList.remove('open');
});

// ── NOTIFICATION BELL ──
let _bellCategory = '';
let _bellCache = [];

function toggleBellDrop(e) {
  e.stopPropagation();
  const drop = document.getElementById('bell-drop');
  const isOpen = drop.classList.toggle('open');
  if (isOpen) loadBellNotifs();
}
function closeBellDrop() {
  document.getElementById('bell-drop').classList.remove('open');
}

function switchBellTab(btn) {
  document.querySelectorAll('.bell-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _bellCategory = btn.dataset.cat || '';
  loadBellNotifs();
}

async function loadBellNotifs() {
  try {
    let url = '/api/notifications?limit=30';
    if (_bellCategory) url += '&category=' + _bellCategory;
    const r = await fetch(url);
    const d = await r.json();
    _bellCache = d.notifications || [];
    const list = document.getElementById('bell-list');
    if (!_bellCache.length) {
      list.innerHTML = '<div class="bell-empty">No notifications</div>';
      return;
    }
    list.innerHTML = _bellCache.map(function(n) {
      var dotCls = n.is_read ? 'bell-unread-dot read' : 'bell-unread-dot';
      var catCls = 'bell-cat-pill bell-cat-' + (n.category || 'system');
      var ts = n.created_at ? _relTime(n.created_at) : '';
      var preview = (n.body || '').substring(0, 60);
      if (n.body && n.body.length > 60) preview += '...';
      return '<div class="bell-item" onclick="openNotifModal(' + n.id + ')">' 
        + '<div class="' + dotCls + '"></div>'
        + '<div class="bell-item-body">'
        + '<div class="bell-item-title">' + _esc(n.title) + '</div>'
        + (preview ? '<div class="bell-item-preview">' + _esc(preview) + '</div>' : '')
        + '<div class="bell-item-meta">'
        + '<span class="' + catCls + '">' + (n.category || 'system') + '</span>'
        + '<span class="bell-item-time">' + ts + '</span>'
        + '</div></div></div>';
    }).join('');
  } catch(e) { console.warn('loadBellNotifs', e); }
}

function _esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function _relTime(iso) {
  var d = new Date(iso + (iso.includes('Z') || iso.includes('+') ? '' : 'Z'));
  var secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  if (secs < 604800) return Math.floor(secs / 86400) + 'd ago';
  return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
}

async function pollUnreadCount() {
  try {
    var r = await fetch('/api/notifications/unread-count');
    var d = await r.json();
    var badge = document.getElementById('bell-badge');
    if (d.count > 0) {
      badge.textContent = d.count > 99 ? '99+' : d.count;
      badge.classList.add('show');
    } else {
      badge.classList.remove('show');
    }
  } catch(e) {}
}
pollUnreadCount();
setInterval(pollUnreadCount, 30000);

async function markAllNotifRead() {
  await fetch('/api/notifications/read', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({all: true, category: _bellCategory || undefined})
  });
  loadBellNotifs();
  pollUnreadCount();
}

async function openNotifModal(id) {
  var n = _bellCache.find(function(x) { return x.id === id; });
  if (!n) return;
  // Mark read
  if (!n.is_read) {
    fetch('/api/notifications/read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: id})
    }).then(function() { pollUnreadCount(); });
    n.is_read = 1;
  }
  var catEl = document.getElementById('nmo-cat');
  catEl.className = 'bell-cat-pill bell-cat-' + (n.category || 'system');
  catEl.textContent = n.category || 'system';
  document.getElementById('nmo-title').textContent = n.title;
  document.getElementById('nmo-time').textContent = n.created_at || '';
  document.getElementById('nmo-body').innerHTML = (n.body || '').replace(/\n/g, '<br>');
  // Show beta test response section if this is a beta test notification
  var betaPanel = document.getElementById('nmo-beta-response');
  var meta = {};
  try { meta = typeof n.meta === 'string' ? JSON.parse(n.meta) : (n.meta || {}); } catch(e) {}
  if (meta.type === 'beta_test' || (n.category === 'system' && n.title && n.title.indexOf('Beta Test') === 0)) {
    betaPanel.style.display = 'block';
    betaPanel.dataset.betaTestId = meta.beta_test_id || '';
    betaPanel.dataset.testTitle = n.title || '';
    document.getElementById('nmo-beta-comment').value = '';
    document.getElementById('nmo-beta-submit').disabled = true;
    document.getElementById('nmo-beta-submit').style.opacity = '0.4';
    var radios = document.querySelectorAll('input[name="nmo-verdict"]');
    radios.forEach(function(r) { r.checked = false; });
  } else {
    betaPanel.style.display = 'none';
  }
  document.getElementById('notif-modal-overlay').style.display = 'flex';
  closeBellDrop();
  loadBellNotifs();
}


function nmoVerdictChanged() {
  var radios = document.querySelectorAll('input[name="nmo-verdict"]');
  var selected = false;
  radios.forEach(function(r) { if (r.checked) selected = true; });
  var btn = document.getElementById('nmo-beta-submit');
  btn.disabled = !selected;
  btn.style.opacity = selected ? '1' : '0.4';
}

async function nmoSubmitBeta() {
  var panel = document.getElementById('nmo-beta-response');
  var testId = panel.dataset.betaTestId;
  var testTitle = panel.dataset.testTitle;
  var verdict = '';
  document.querySelectorAll('input[name="nmo-verdict"]').forEach(function(r) { if (r.checked) verdict = r.value; });
  var comment = document.getElementById('nmo-beta-comment').value.trim();
  if (!verdict) { alert('Please select Yes or No'); return; }
  if (!comment) { alert('Please provide a brief response for the backend team'); return; }
  var fullMsg = 'Verdict: ' + (verdict === 'yes' ? 'WORKING' : 'STILL BROKEN') + '\n\n' + comment;
  var btn = document.getElementById('nmo-beta-submit');
  btn.disabled = true; btn.textContent = 'Submitting...';
  try {
    var r = await fetch('/api/support/beta-response', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({beta_test_id: testId, message: fullMsg})
    });
    var d = await r.json();
    if (d.ok) {
      toast('Response submitted \u2014 thank you!', 'ok');
      closeNotifModal();
    } else { toast(d.error || 'Failed to submit', 'err'); }
  } catch(e) { toast('Error: ' + e.message, 'err'); }
  btn.disabled = false; btn.textContent = 'Submit Response';
}

function closeNotifModal(e) {
  if (!e || e.target === document.getElementById('notif-modal-overlay')) {
    document.getElementById('notif-modal-overlay').style.display = 'none';
  }
}

// ── CONFIG PANEL ──
function toggleConfigPanel(){
  const open = document.getElementById('cfg-panel').classList.toggle('open');
  document.getElementById('cfg-overlay').classList.toggle('open', open);
  const kb = document.getElementById('cfg-kill-btn');
  if (kb) {
    kb.textContent = killState ? 'Disengage Kill Switch' : 'Engage Kill Switch';
  }
}
function closeConfigPanel(){
  document.getElementById('cfg-panel').classList.remove('open');
  document.getElementById('cfg-overlay').classList.remove('open');
}

async function loadCfgPanel() {
  try {
    const r = await fetch('/api/customer-settings');
    const d = await r.json();
    if (d.error) return;
    const g = id => document.getElementById(id);
    if (g('cfg-min-conf'))      g('cfg-min-conf').value = d.MIN_CONFIDENCE || 'LOW';
    if (g('cfg-max-pos'))       g('cfg-max-pos').value = d.MAX_POSITION_PCT_DISPLAY || '10';
    if (g('cfg-max-trade-usd')) g('cfg-max-trade-usd').value = d.MAX_TRADE_USD || '0';
    if (g('cfg-max-sector'))    g('cfg-max-sector').value = d.MAX_SECTOR_PCT || '25';
    if (g('cfg-close-mode'))    g('cfg-close-mode').value = d.CLOSE_SESSION_MODE || 'aggressive';
    if (g('cfg-staleness'))      g('cfg-staleness').value = d.MAX_STALENESS || 'Aging';
    if (g('cfg-max-positions')) g('cfg-max-positions').value = d.MAX_POSITIONS || '10';
    if (g('cfg-max-daily-loss'))g('cfg-max-daily-loss').value = Math.abs(parseFloat(d.MAX_DAILY_LOSS||'500'));
    if (g('cfg-max-drawdown'))  g('cfg-max-drawdown').value = parseFloat(d.MAX_DRAWDOWN_PCT||'15');
    if (g('cfg-max-hold-days')) g('cfg-max-hold-days').value = d.MAX_HOLDING_DAYS || '15';
    if (g('cfg-max-exposure'))  g('cfg-max-exposure').value = parseFloat(d.MAX_GROSS_EXPOSURE||'80');
    if (g('cfg-profit-target')) g('cfg-profit-target').value = d.PROFIT_TARGET_MULTIPLE || '2';
    if (g('cfg-trading-mode'))  g('cfg-trading-mode').value = d.TRADING_MODE || 'PAPER';
    if (g('cfg-bil-enabled'))   g('cfg-bil-enabled').checked = d.ENABLE_BIL_RESERVE !== '0';
    if (g('cfg-bil-reserve'))   g('cfg-bil-reserve').value = d.IDLE_RESERVE_PCT || '20';
    if (g('cfg-bil-label'))     g('cfg-bil-label').textContent = (d.ENABLE_BIL_RESERVE !== '0') ? 'Enabled' : 'Disabled';
    // Restore saved preset from DB (not guessed from values)
    var wrap = document.getElementById('cfg-controls-wrap');
    var matched = d.PRESET_NAME || 'custom';
    _currentPreset = matched;
    // Reset all preset buttons to default state first
    document.querySelectorAll('.preset-btn').forEach(function(b){
      b.style.borderColor = 'rgba(255,255,255,0.1)';
      b.style.background = 'rgba(255,255,255,0.03)';
      b.style.color = 'rgba(255,255,255,0.35)';
    });
    if (matched && matched !== 'custom') {
      if (wrap) { wrap.style.opacity = '0.35'; wrap.style.pointerEvents = 'none'; }
      document.getElementById('cfg-advanced').style.display = 'none';
    } else {
      matched = 'custom';
      _currentPreset = 'custom';
      if (wrap) { wrap.style.opacity = '1'; wrap.style.pointerEvents = 'auto'; }
    }
    // Highlight the saved preset button
    document.querySelectorAll('.preset-btn').forEach(function(b){
      if (b.dataset.preset === matched) {
        var colors = {conservative:'rgba(0,245,212,',moderate:'rgba(123,97,255,',aggressive:'rgba(255,75,110,',custom:'rgba(255,255,255,'};
        var c = colors[matched] || colors.custom;
        b.style.borderColor = c + '0.3)'; b.style.background = c + '0.1)';
        b.style.color = matched==='conservative'?'var(--teal)':matched==='moderate'?'var(--purple)':matched==='aggressive'?'var(--pink)':'var(--text)';
      }
    });
    // Kill switch state
    var killBtn = g('cfg-kill-btn');
    if (killBtn) {
      var isKilled = d.KILL_SWITCH === '1';
      killBtn.textContent = isKilled ? 'Disengage Kill Switch' : 'Engage Kill Switch';
    }
  } catch(e) { console.error('loadCfgPanel:', e); }
}


var PRESETS = {
  conservative: {
    'cfg-min-conf': 'HIGH', 'cfg-max-pos': '5', 'cfg-max-trade-usd': '500',
    'cfg-max-positions': '5', 'cfg-max-daily-loss': '200', 'cfg-close-mode': 'conservative',
    'cfg-max-drawdown': '8', 'cfg-max-sector': '20', 'cfg-max-hold-days': '10',
    'cfg-max-exposure': '60', 'cfg-profit-target': '2.5', 'cfg-staleness': 'Fresh',
    'cfg-trading-mode': 'PAPER', 'cfg-bil-reserve': '30', 'cfg-bil-enabled': true
  },
  moderate: {
    'cfg-min-conf': 'MEDIUM', 'cfg-max-pos': '10', 'cfg-max-trade-usd': '2000',
    'cfg-max-positions': '10', 'cfg-max-daily-loss': '500', 'cfg-close-mode': 'moderate',
    'cfg-max-drawdown': '15', 'cfg-max-sector': '30', 'cfg-max-hold-days': '15',
    'cfg-max-exposure': '80', 'cfg-profit-target': '2', 'cfg-staleness': 'Aging',
    'cfg-trading-mode': 'PAPER', 'cfg-bil-reserve': '20', 'cfg-bil-enabled': true
  },
  aggressive: {
    'cfg-min-conf': 'LOW', 'cfg-max-pos': '20', 'cfg-max-trade-usd': '5000',
    'cfg-max-positions': '20', 'cfg-max-daily-loss': '1000', 'cfg-close-mode': 'aggressive',
    'cfg-max-drawdown': '25', 'cfg-max-sector': '50', 'cfg-max-hold-days': '30',
    'cfg-max-exposure': '95', 'cfg-profit-target': '1.5', 'cfg-staleness': 'Stale',
    'cfg-trading-mode': 'PAPER', 'cfg-bil-reserve': '10', 'cfg-bil-enabled': true
  }
};

function applyPreset(name, btn) {
  _currentPreset = name;
  document.querySelectorAll('.preset-btn').forEach(function(b){
    b.style.borderColor = 'rgba(255,255,255,0.1)';
    b.style.background = 'rgba(255,255,255,0.03)';
    b.style.color = 'rgba(255,255,255,0.35)';
  });
  var wrap = document.getElementById('cfg-controls-wrap');
  if (name !== 'custom') {
    var preset = PRESETS[name];
    for (var id in preset) {
      var el = document.getElementById(id);
      if (!el) continue;
      if (el.type === 'checkbox') {
        el.checked = preset[id];
      } else {
        el.value = preset[id];
      }
    }
    // Hide advanced, grey out controls (preset handles values)
    document.getElementById('cfg-advanced').style.display = 'none';
    if (wrap) { wrap.style.opacity = '0.35'; wrap.style.pointerEvents = 'none'; }
  } else {
    // Custom — enable all controls, show advanced
    document.getElementById('cfg-advanced').style.display = 'block';
    if (wrap) { wrap.style.opacity = '1'; wrap.style.pointerEvents = 'auto'; }
  }
  // Highlight selected
  var colors = {conservative:'rgba(0,245,212,',moderate:'rgba(123,97,255,',aggressive:'rgba(255,75,110,',custom:'rgba(255,255,255,'};
  var c = colors[name] || colors.custom;
  btn.style.borderColor = c + '0.3)';
  btn.style.background = c + '0.1)';
  btn.style.color = name==='conservative'?'var(--teal)':name==='moderate'?'var(--purple)':name==='aggressive'?'var(--pink)':'var(--text)';
  // Update BIL label
  var bilCb = document.getElementById('cfg-bil-enabled');
  var bilLbl = document.getElementById('cfg-bil-label');
  if (bilCb && bilLbl) bilLbl.textContent = bilCb.checked ? 'Enabled' : 'Disabled';
}

// BIL checkbox label update
document.addEventListener('change', function(e) {
  if (e.target && e.target.id === 'cfg-bil-enabled') {
    var lbl = document.getElementById('cfg-bil-label');
    if (lbl) lbl.textContent = e.target.checked ? 'Enabled' : 'Disabled';
  }
});

var _currentPreset = 'custom';

async function saveCfgPanel(){
  const data = {
    min_confidence:      document.getElementById('cfg-min-conf')?.value,
    max_position_pct:    parseFloat(document.getElementById('cfg-max-pos')?.value)||10,
    max_trade_usd:       parseFloat(document.getElementById('cfg-max-trade-usd')?.value)||0,
    max_positions:       parseInt(document.getElementById('cfg-max-positions')?.value)||10,
    max_daily_loss:      parseFloat(document.getElementById('cfg-max-daily-loss')?.value)||500,
    max_sector_pct:      parseFloat(document.getElementById('cfg-max-sector')?.value)||25,
    close_session_mode:  document.getElementById('cfg-close-mode')?.value,
    max_staleness:       document.getElementById('cfg-staleness')?.value,
    max_drawdown_pct:    parseFloat(document.getElementById('cfg-max-drawdown')?.value)||15,
    max_holding_days:    parseInt(document.getElementById('cfg-max-hold-days')?.value)||15,
    max_gross_exposure:  parseFloat(document.getElementById('cfg-max-exposure')?.value)||80,
    profit_target:       parseFloat(document.getElementById('cfg-profit-target')?.value)||2,
    trading_mode:        document.getElementById('cfg-trading-mode')?.value,
    enable_bil_reserve:  document.getElementById('cfg-bil-enabled')?.checked ? '1' : '0',
    idle_reserve_pct:    parseFloat(document.getElementById('cfg-bil-reserve')?.value)||20,
    preset_name:         _currentPreset,
  };
  const mirror = {
    // Mirror removed — settings tab uses config panel
  };
  Object.entries(mirror).forEach(([id,v])=>{ const el=document.getElementById(id); if(el) el.value=v; });
  const r = await fetch('/api/settings', {
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)
  });
  const d = await r.json();
  toast(d.ok ? 'Configuration saved' : 'Save failed', d.ok ? 'ok' : 'err');
}
async function cfgKillToggle(){
  const btn = document.getElementById('cfg-kill-btn');
  const engaged = btn.textContent.includes('Disengage');
  const r = await fetch('/api/kill-switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({engage:!engaged})});
  const d = await r.json();
  if (d.ok !== false) {
    killState = !engaged;
    btn.textContent = !engaged ? 'Disengage Kill Switch' : 'Engage Kill Switch';
    toast(!engaged ? 'Kill switch ENGAGED' : 'Kill switch disengaged', !engaged ? 'err' : 'ok');
  }
}



async function loadBilling() {
  try {
    var r = await fetch('/api/billing');
    var d = await r.json();
    if (d.error) return;
    var statusEl = document.getElementById('bill-status');
    var planEl = document.getElementById('bill-plan');
    var nextEl = document.getElementById('bill-next');
    var tierEl = document.getElementById('bill-tier');
    if (statusEl) {
      var st = d.subscription_status || 'inactive';
      var colors = {active:'#00f5d4',trialing:'#7b61ff',past_due:'#f5a623',cancelled:'#ff4b6e',inactive:'rgba(255,255,255,0.35)'};
      statusEl.textContent = st.charAt(0).toUpperCase() + st.slice(1);
      statusEl.style.color = colors[st] || colors.inactive;
      planEl.textContent = d.has_stripe ? 'Stripe connected' : 'Payment not configured';
    }
    if (nextEl) {
      nextEl.textContent = d.subscription_ends_at ? new Date(d.subscription_ends_at).toLocaleDateString() : 'N/A';
    }
    if (tierEl) {
      var tier = d.pricing_tier || 'standard';
      tierEl.textContent = tier === 'early_adopter' ? 'Early Adopter' : 'Standard';
    }
  } catch(e) {}
}



async function markSetupComplete() {
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({setup_complete: '1'})
    });
    showTab('dashboard');
    toast('Setup guide dismissed', 'ok');
  } catch(e) {
    toast('Error saving', 'err');
  }
}

async function testAlpacaKeys() {
  var el = document.getElementById('alpaca-test-result');
  if (!el) { alert('Test result element not found'); return; }
  el.style.display = 'block';
  el.style.background = 'rgba(255,255,255,0.04)';
  el.style.border = '1px solid rgba(255,255,255,0.08)';
  el.style.color = 'var(--muted)';
  el.textContent = 'Testing Alpaca connection...';
  try {
    var r = await fetch('/api/test-alpaca-keys', {method: 'GET', credentials: 'same-origin'});
    if (!r.ok) {
      el.style.background = 'rgba(255,75,110,0.06)';
      el.style.border = '1px solid rgba(255,75,110,0.15)';
      el.style.color = 'var(--pink)';
      el.textContent = 'Server error: HTTP ' + r.status;
      return;
    }
    var d = await r.json();
    if (d.ok) {
      el.style.background = 'rgba(0,245,212,0.06)';
      el.style.border = '1px solid rgba(0,245,212,0.15)';
      el.style.color = 'var(--teal)';
      var mode = d.paper ? 'Paper' : 'Live';
      // Show green checks on both keys
      var kc = document.getElementById('alpaca-key-check');
      var sc = document.getElementById('alpaca-secret-check');
      if (kc) kc.style.display = 'inline';
      if (sc) sc.style.display = 'inline';
      el.innerHTML = '&#10003; Connected — ' + mode + ' Account: ' + (d.account_id||'') + ' | Status: ' + (d.status||'') + ' | Cash: $' + (d.cash||'0');
    } else {
      el.style.background = 'rgba(255,75,110,0.06)';
      el.style.border = '1px solid rgba(255,75,110,0.15)';
      el.style.color = 'var(--pink)';
      el.textContent = '\u2717 ' + (d.error || 'Connection failed — check your API keys');
    }
  } catch(e) {
    el.style.background = 'rgba(255,75,110,0.06)';
    el.style.border = '1px solid rgba(255,75,110,0.15)';
    el.style.color = 'var(--pink)';
    el.textContent = '\u2717 Could not reach server — try refreshing the page';
    console.error('testAlpacaKeys error:', e);
  }
}


// ── MESSAGES TAB ──
async function loadMessages() {
  var el = document.getElementById('msg-list');
  try {
    var r = await fetch('/api/support/tickets?category=direct_message');
    var d = await r.json();
    var tickets = d.tickets || [];
    if (!tickets.length) {
      el.innerHTML = '<div style="text-align:center;padding:40px 0;color:var(--dim);font-size:12px"><div style="font-size:28px;margin-bottom:8px">&#x1F4AC;</div>No messages yet</div>';
      return;
    }
    el.innerHTML = tickets.map(function(t) {
      var last = t.last_message ? t.last_message.message.slice(0,80) : '';
      var sender = t.last_message ? t.last_message.sender : '';
      var age = t.updated_at ? t.updated_at.slice(0,16).replace('T',' ') : '';
      var unread = (sender === 'admin' && t.status === 'open') ? '<span style="width:6px;height:6px;border-radius:50%;background:var(--teal);display:inline-block;margin-right:6px"></span>' : '';
      return '<div style="padding:12px 14px;border:1px solid var(--border);border-radius:10px;margin-bottom:8px;cursor:pointer;transition:border-color .15s" data-tid="' + t.ticket_id + '" onclick="viewMessage(this.dataset.tid)" onmouseover="this.style.borderColor=&#39;rgba(255,255,255,0.15)&#39;" onmouseout="this.style.borderColor=&#39;var(--border)&#39;">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
        + '<div style="font-size:13px;font-weight:600">' + unread + t.subject + '</div>'
        + '<span style="font-size:9px;color:var(--dim)">' + age + '</span></div>'
        + '<div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + last + '</div>'
        + '<div style="font-size:10px;color:var(--dim);margin-top:3px">' + t.message_count + ' message' + (t.message_count!==1?'s':'') + '</div>'
        + '</div>';
    }).join('');
  } catch(e) { el.innerHTML = '<div style="color:var(--pink);padding:20px;text-align:center">Error loading messages</div>'; }
}

async function viewMessage(ticketId) {
  var el = document.getElementById('msg-detail');
  try {
    var r = await fetch('/api/support/tickets/' + ticketId);
    var d = await r.json();
    if (!d.ticket) return;
    var msgs = d.messages || [];
    var html = '<div style="margin-top:16px;padding:16px;border:1px solid var(--border);border-radius:10px">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">';
    html += '<div style="font-size:14px;font-weight:700">' + d.ticket.subject + '</div>';
    html += '<button style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:11px" onclick="document.getElementById(&#39;msg-detail&#39;).innerHTML=&#39;&#39;">Close</button></div>';
    msgs.forEach(function(m) {
      var isAdmin = m.sender === 'admin';
      html += '<div style="padding:8px 10px;border-radius:8px;margin-bottom:6px;background:' + (isAdmin ? 'rgba(123,97,255,0.06)' : 'rgba(0,245,212,0.04)') + ';border:1px solid ' + (isAdmin ? 'rgba(123,97,255,0.12)' : 'rgba(0,245,212,0.1)') + '">';
      html += '<div style="font-size:9px;color:var(--muted);margin-bottom:3px">' + (isAdmin ? 'Synthos Team' : 'You') + ' \u00b7 ' + (m.created_at||'').slice(0,16).replace('T',' ') + '</div>';
      html += '<div style="font-size:12px;color:var(--text);line-height:1.5;white-space:pre-wrap">' + m.message + '</div></div>';
    });
    html += '<textarea id="msg-reply-box" style="width:100%;min-height:60px;background:var(--surface2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:12px;resize:vertical;outline:none;margin-top:8px" placeholder="Write a reply..."></textarea>';
    html += '<button type="button" data-tid="' + ticketId + '" onclick="sendMessageReply(this.dataset.tid)" style="width:100%;padding:8px;border:none;border-radius:8px;background:var(--teal);color:#000;font-size:12px;font-weight:700;cursor:pointer;margin-top:6px">Reply</button>';
    html += '</div>';
    el.innerHTML = html;
    el.scrollIntoView({behavior:'smooth'});
  } catch(e) { console.error(e); }
}

async function sendMessageReply(ticketId) {
  var msg = document.getElementById('msg-reply-box').value.trim();
  if (!msg) return;
  await fetch('/api/support/tickets/' + ticketId + '/reply', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: msg, sender: 'customer'})
  });
  viewMessage(ticketId);
  toast('Reply sent', 'ok');
}


// New account banner — show until trade agent has run at least once
async function checkNewAccountBanner() {
  try {
    var r = await fetch('/api/trader-activity');
    var d = await r.json();
    var hasRun = (d.recent && d.recent.length > 0) || (d.scans && d.scans.length > 0);
    var banner = document.getElementById('new-account-banner');
    if (banner) {
      banner.style.display = hasRun ? 'none' : 'flex';
    }
  } catch(e) {}
}

// ── SUPPORT PANEL ──
var _supCat = 'portal';
function toggleSupportPanel() {
  var open = document.getElementById('sup-panel').classList.toggle('open');
  document.getElementById('sup-overlay').classList.toggle('open', open);
  if (open) supLoadTickets();
}
function closeSupportPanel() {
  document.getElementById('sup-panel').classList.remove('open');
  document.getElementById('sup-overlay').classList.remove('open');
}
function supSetCat(btn) {
  document.querySelectorAll('.sup-cat').forEach(function(b){b.classList.remove('active')});
  btn.classList.add('active');
  _supCat = btn.dataset.cat;
}
async function supSubmitTicket() {
  var subj = document.getElementById('sup-subject').value.trim();
  var msg = document.getElementById('sup-message').value.trim();
  if (!subj || !msg) { alert('Subject and message are required'); return; }
  var btn = document.getElementById('sup-submit');
  btn.disabled = true; btn.textContent = 'Sending...';
  try {
    var r = await fetch('/api/support/tickets', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({category: _supCat, subject: subj, message: msg})
    });
    var d = await r.json();
    if (d.ok) {
      document.getElementById('sup-subject').value = '';
      document.getElementById('sup-message').value = '';
      toast('Ticket submitted: ' + d.ticket_id, 'ok');
      supLoadTickets();
    } else { toast(d.error || 'Failed', 'err'); }
  } catch(e) { toast('Error: ' + e.message, 'err'); }
  btn.disabled = false; btn.textContent = 'Submit';
}
function supOpenBetaResponse(testId, title) {
  toggleSupportPanel();
  _supCat = 'beta_test';
  document.getElementById('sup-subject').value = 'Beta Test: ' + title;
  document.getElementById('sup-message').placeholder = 'Describe your test results...';
  document.getElementById('sup-message').focus();
  document.getElementById('sup-message').dataset.betaTestId = testId;
}
async function supLoadTickets() {
  var el = document.getElementById('sup-tickets-list');
  try {
    var r = await fetch('/api/support/tickets');
    var d = await r.json();
    var tickets = d.tickets || [];
    if (!tickets.length) { el.innerHTML = '<div style="font-size:11px;color:var(--dim);text-align:center;padding:12px 0">No tickets yet</div>'; return; }
    el.innerHTML = tickets.map(function(t) {
      var stCls = t.status.replace(' ','_');
      var last = t.last_message ? t.last_message.message.slice(0,60) : '';
      var age = t.updated_at ? t.updated_at.slice(0,10) : '';
      return '<div class="sup-ticket" data-tid="' + t.ticket_id + '" onclick="supViewTicket(this.dataset.tid)">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
        + '<div class="sup-ticket-subj">' + t.subject + '</div>'
        + '<span class="sup-status ' + stCls + '">' + t.status + '</span></div>'
        + '<div class="sup-ticket-meta">' + t.category + ' \u00b7 ' + age + ' \u00b7 ' + t.message_count + ' messages</div>'
        + (last ? '<div style="font-size:10px;color:var(--dim);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + last + '</div>' : '')
        + '</div>';
    }).join('');
  } catch(e) { el.innerHTML = '<div style="color:var(--pink);font-size:11px">Error loading tickets</div>'; }
}
async function supViewTicket(ticketId) {
  try {
    var r = await fetch('/api/support/tickets/' + ticketId);
    var d = await r.json();
    if (!d.ticket) return;
    var msgs = d.messages || [];
    var html = '<div style="margin-bottom:12px"><button class="sup-cat active" onclick="supLoadTickets();this.parentElement.parentElement.innerHTML=&#39;&#39;;" style="font-size:10px">&larr; Back</button></div>';
    html += '<div style="font-size:13px;font-weight:700;margin-bottom:4px">' + d.ticket.subject + '</div>';
    html += '<div style="font-size:10px;color:var(--muted);margin-bottom:12px">' + d.ticket.category + ' \u00b7 ' + d.ticket.status + '</div>';
    msgs.forEach(function(m) {
      var isAdmin = m.sender === 'admin';
      html += '<div style="padding:8px 10px;border-radius:8px;margin-bottom:6px;background:' + (isAdmin ? 'rgba(123,97,255,0.06)' : 'rgba(255,255,255,0.03)') + ';border:1px solid ' + (isAdmin ? 'rgba(123,97,255,0.12)' : 'var(--border)') + '">';
      html += '<div style="font-size:9px;color:var(--muted);margin-bottom:3px">' + (isAdmin ? 'Support Team' : 'You') + ' \u00b7 ' + (m.created_at||'').slice(0,16) + '</div>';
      html += '<div style="font-size:12px;color:var(--text);line-height:1.5;white-space:pre-wrap">' + m.message + '</div>';
      html += '</div>';
    });
    html += '<textarea class="sup-input sup-textarea" id="sup-reply-msg" placeholder="Write a reply..." style="margin-top:8px"></textarea>';
    html += '<button class="sup-submit" data-tid="' + ticketId + '" onclick="supReplyTicket(this.dataset.tid)">Reply</button>';
    document.getElementById('sup-tickets-list').innerHTML = html;
  } catch(e) { console.error(e); }
}
async function supReplyTicket(ticketId) {
  var msg = document.getElementById('sup-reply-msg').value.trim();
  if (!msg) return;
  await fetch('/api/support/tickets/' + ticketId + '/reply', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: msg, sender: 'customer'})
  });
  supViewTicket(ticketId);
}

// ── TABS ──
function showTab(t, e) {
  ['dashboard','intel','news','screening','performance','risk','billing','settings','guide','messages'].forEach(id => {
    const el = document.getElementById('tab-'+id);
    if (el) el.style.display = id===t ? '' : 'none';
    // Sidebar active state
    const sb = document.getElementById('snb-'+id);
    if (sb) sb.classList.toggle('active', id===t);
  });
  if (t === 'intel') loadIntel();
  if (t === 'news') loadNews('all');
  if (t === 'screening') loadScreening();
  if (t === 'performance') loadPerformance();
  if (t === 'risk') loadRisk();
  if (t === 'messages') loadMessages();
  if (t === 'billing') loadBilling();
}

// ── POSITION DRAWER ──
function openPositionDrawer(p) {
  const accent = p.unrealized_pl >= 0 ? 'var(--teal)' : 'var(--pink)';
  const plSign  = v => v >= 0 ? '+' : '';
  const fmt     = v => '$' + Math.abs(v).toFixed(2);

  document.getElementById('drawer-icon').textContent = (p.ticker||'?').slice(0,4);
  document.getElementById('drawer-icon').style.cssText =
    `width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;flex-shrink:0;background:${accent}18;border:1px solid ${accent}40;color:${accent}`;
  document.getElementById('drawer-ticker').textContent = p.ticker || '—';
  document.getElementById('drawer-sub').textContent =
    (p.shares||0).toFixed(2) + ' shares · ' + (p.is_orphan ? 'Orphan position' : 'Tracked');

  const sv = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v; };
  sv('dr-mktval',  fmt(p.market_value || 0));
  sv('dr-shares',  (p.shares||0).toFixed(4));
  sv('dr-entry',   '$' + (p.avg_entry_price||0).toFixed(2));
  sv('dr-price',   '$' + (p.current_price||0).toFixed(2));
  sv('dr-basis',   '$' + ((p.avg_entry_price||0) * (p.shares||0)).toFixed(2));

  const urEl = document.getElementById('dr-unreal');
  const urPctEl = document.getElementById('dr-unreal-pct');
  if (urEl) { urEl.textContent = plSign(p.unrealized_pl) + fmt(p.unrealized_pl||0); urEl.style.color = accent; }
  if (urPctEl) { urPctEl.textContent = plSign(p.unrealized_plpc) + (p.unrealized_plpc||0).toFixed(2) + '%'; urPctEl.style.color = accent; }

  const dayC = (p.day_pl||0) >= 0 ? 'var(--teal)' : 'var(--pink)';
  const dpEl = document.getElementById('dr-day-pl');
  const dpPEl = document.getElementById('dr-day-pct');
  if (dpEl) { dpEl.textContent = plSign(p.day_pl) + fmt(p.day_pl||0); dpEl.style.color = dayC; }
  if (dpPEl) { dpPEl.textContent = plSign(p.day_plpc) + (p.day_plpc||0).toFixed(2) + '%'; dpPEl.style.color = dayC; }

  document.getElementById('drawer-overlay').classList.add('open');
  document.getElementById('pos-drawer').classList.add('open');
}

function closeDrawer(e) {
  if (e && e.target !== document.getElementById('drawer-overlay')) return;
  document.getElementById('drawer-overlay').classList.remove('open');
  document.getElementById('pos-drawer').classList.remove('open');
}

// ── SESSION TIMELINE UPDATE ──
function updateSessionTimeline() {
  const now = new Date();
  const etStr = now.toLocaleString('en-US', {timeZone:'America/New_York'});
  const et = new Date(etStr);
  const mins = et.getHours() * 60 + et.getMinutes();
  const day = et.getDay();
  const isWeekend = day === 0 || day === 6;

  // Sessions: pre=240, open=570, mid=720, close=930, after=960
  const sessions = [
    {id:'pre',  start:240,  label:'Pre-Market'},
    {id:'open', start:570,  label:'Open'},
    {id:'mid',  start:720,  label:'Midday'},
    {id:'close',start:930,  label:'Close'},
    {id:'after',start:960,  label:'After'},
  ];

  sessions.forEach((s, i) => {
    const dot  = document.getElementById('tl-' + s.id);
    const lbl  = document.getElementById('tl-' + s.id + '-lbl');
    if (!dot || !lbl) return;
    const nextStart = sessions[i+1] ? sessions[i+1].start : 1200;
    if (isWeekend || mins < sessions[0].start) {
      dot.className = 'tl-dot pending';
      lbl.className = 'tl-label';
    } else if (mins >= nextStart) {
      dot.className = 'tl-dot done';
      lbl.className = 'tl-label done';
    } else if (mins >= s.start) {
      dot.className = 'tl-dot active';
      lbl.className = 'tl-label active';
    } else {
      dot.className = 'tl-dot pending';
      lbl.className = 'tl-label';
    }
  });
}

// ── PERFORMANCE TAB ──
async function loadPerformance() {
  // ── 1. Trade stats from closed positions ──
  try {
    const r = await fetch('/api/performance-summary');
    const d = await r.json();

    const sign = d.total_pnl >= 0 ? '+' : '';
    const retEl = document.getElementById('perf-total-return');
    if (retEl) {
      retEl.textContent = sign + '$' + Math.abs(d.total_pnl || 0).toFixed(2);
      retEl.style.color = d.total_pnl >= 0 ? 'var(--teal)' : 'var(--pink)';
    }
    const retSubEl = document.getElementById('perf-total-sub');
    if (retSubEl) retSubEl.textContent = (d.total_ret_pct >= 0 ? '+' : '') + (d.total_ret_pct || 0).toFixed(2) + '% all time';

    const wrEl = document.getElementById('perf-win-rate');
    if (wrEl) {
      wrEl.textContent = (d.win_rate || 0) + '%';
      wrEl.style.color = d.win_rate >= 50 ? 'var(--teal)' : 'var(--pink)';
    }
    const wrSub = document.getElementById('perf-trades-sub');
    if (wrSub) wrSub.textContent = (d.winning_trades || 0) + ' wins / ' + (d.total_trades || 0) + ' trades';

    const holdEl = document.getElementById('perf-avg-hold');
    if (holdEl) holdEl.textContent = d.avg_hold || '--';

    const stEl = document.getElementById('tax-st');
    const ltEl = document.getElementById('tax-lt');
    if (stEl) stEl.textContent = (d.tax_st >= 0 ? '+' : '') + '$' + Math.abs(d.tax_st || 0).toFixed(2);
    if (ltEl) ltEl.textContent = (d.tax_lt >= 0 ? '+' : '') + '$' + Math.abs(d.tax_lt || 0).toFixed(2);

    const sp = d.sector_pnl || {};
    const allVals = Object.values(sp).map(v => Math.abs(v));
    const maxVal  = allVals.length ? Math.max(...allVals) : 1;
    const techPnl  = Object.entries(sp).filter(([k])=>['technology','tech','information technology','software'].some(b=>k.toLowerCase().includes(b))).reduce((a,[,v])=>a+v,0);
    const healthPnl= Object.entries(sp).filter(([k])=>['healthcare','health care','biotechnology','pharma'].some(b=>k.toLowerCase().includes(b))).reduce((a,[,v])=>a+v,0);
    const finPnl   = Object.entries(sp).filter(([k])=>['financials','finance','financial services','banks'].some(b=>k.toLowerCase().includes(b))).reduce((a,[,v])=>a+v,0);
    const otherPnl = Object.values(sp).reduce((a,v)=>a+v,0) - techPnl - healthPnl - finPnl;
    const setBar = (id, barId, val) => {
      const el = document.getElementById(id); const barEl = document.getElementById(barId);
      if (el) { el.textContent = (val>=0?'+':'') + '$' + Math.abs(val).toFixed(2); el.style.color = val>=0?'var(--teal)':'var(--pink)'; }
      if (barEl) barEl.style.width = maxVal > 0 ? (Math.abs(val)/maxVal*100).toFixed(0)+'%' : '0%';
    };
    setBar('attr-tech',   'attr-tech-bar',   techPnl);
    setBar('attr-health', 'attr-health-bar', healthPnl);
    setBar('attr-fin',    'attr-fin-bar',    finPnl);
    setBar('attr-other',  'attr-other-bar',  otherPnl);

    const tbody  = document.getElementById('trade-history-body');
    const hCount = document.getElementById('history-count');
    if (tbody) {
      const trades = d.trades || [];
      if (hCount) hCount.textContent = trades.length + ' closed trades';
      if (!trades.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">No closed trades yet</td></tr>';
      } else {
        tbody.innerHTML = trades.map(t => {
          const pnlColor = t.pnl >= 0 ? 'var(--teal)' : 'var(--pink)';
          const pnlSign  = t.pnl >= 0 ? '+' : '';
          const retSign  = t.ret_pct >= 0 ? '+' : '';
          return '<tr>'
            + '<td style="font-weight:700;font-family:var(--mono)">' + t.ticker + '</td>'
            + '<td><span style="font-size:9px;padding:1px 6px;border-radius:99px;background:rgba(0,245,212,0.1);color:var(--teal);border:1px solid rgba(0,245,212,0.14)">' + t.side + '</span></td>'
            + '<td style="font-family:var(--mono)">$' + t.entry.toFixed(2) + '</td>'
            + '<td style="font-family:var(--mono)">$' + t.exit.toFixed(2) + '</td>'
            + '<td style="color:var(--muted)">' + t.hold + '</td>'
            + '<td style="font-family:var(--mono);color:' + pnlColor + ';font-weight:700">' + pnlSign + '$' + Math.abs(t.pnl).toFixed(2) + '</td>'
            + '<td style="font-family:var(--mono);color:' + pnlColor + '">' + retSign + Math.abs(t.ret_pct).toFixed(2) + '%</td>'
            + '<td style="color:var(--dim)">\u2014</td>'
            + '</tr>';
        }).join('');
      }
    }

    const badgeGrid = document.getElementById('badge-grid');
    if (badgeGrid && d.total_trades > 0) {
      const badges = badgeGrid.querySelectorAll('.badge');
      if (badges[0]) badges[0].classList.remove('locked');
      if (d.winning_trades > 0 && badges[1]) badges[1].classList.remove('locked');
    }

  } catch(e) { console.warn('loadPerformance error', e); }

  // ── 2. Max drawdown from portfolio history ──
  try {
    const r = await fetch('/api/portfolio-history');
    const d = await r.json();
    const hist = d.history || [];
    if (hist.length > 1) {
      const vals = hist.map(h => h.value || 0);
      const peak = Math.max(...vals);
      const curr = vals[vals.length - 1];
      const dd   = peak > 0 ? ((curr - peak) / peak * 100) : 0;
      const ddEl    = document.getElementById('drawdown-val');
      const ddSubEl = document.getElementById('drawdown-sub');
      if (ddEl) { ddEl.textContent = dd.toFixed(2) + '%'; ddEl.style.color = dd < 0 ? 'var(--pink)' : 'var(--teal)'; }
      if (ddSubEl) ddSubEl.textContent = 'from $' + peak.toFixed(0) + ' peak';
      const ddPerfEl = document.getElementById('perf-max-dd');
      if (ddPerfEl) { ddPerfEl.textContent = dd.toFixed(2) + '%'; ddPerfEl.style.color = dd < 0 ? 'var(--pink)' : 'var(--teal)'; }
    }
  } catch(e) {}
}

// ── RISK TAB ──
function loadRisk() {
  // Wire exposure meter from live status data
  const portVal = parseFloat((document.getElementById('stat-portfolio')||{}).textContent?.replace(/[$,]/g,'')) || 0;
  const cash    = parseFloat((document.getElementById('stat-cash')||{}).textContent?.replace(/[$,]/g,'')) || 0;
  if (portVal > 0) {
    const pct = Math.round((1 - cash / portVal) * 100);
    const bar = document.getElementById('exposure-bar');
    const lbl = document.getElementById('exposure-pct');
    if (bar) bar.style.width = pct + '%';
    if (lbl) lbl.textContent = pct + '%';
  }
}

// ── ALERT PREFS (stub save) ──
function saveAlertPrefs() {
  toast('Alert preferences saved', 'ok');
}

// ── TOAST ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── MODE TOGGLE ──
async function toggleMode() {
  const current = document.getElementById('mode-nav-btn').textContent.trim().includes('Automatic') ? 'AUTOMATIC' : 'MANAGED';
  const next    = current === 'AUTOMATIC' ? 'MANAGED' : 'AUTOMATIC';
  const r = await fetch('/api/set-mode', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({mode: next})
  });
  const d = await r.json();
  if (d.ok) {
    const isAuto = next === 'AUTOMATIC';
    const navBtn = document.getElementById('mode-nav-btn');
    navBtn.className   = 'hdr-mode-pill' + (isAuto ? ' mp-auto' : '');
    navBtn.textContent = isAuto ? 'AUTO' : 'MANAGED';
    navBtn.title       = isAuto ? 'Automatic — bot executes trades' : 'Managed — you approve all trades';
    document.getElementById('mode-bar').className = 'kill-bar' + (isAuto ? '' : ' clear');
    document.getElementById('mode-bar-label').textContent = isAuto ? '⚡ Automatic Mode' : '● Managed Mode';
    document.getElementById('mode-bar-desc').textContent  = isAuto
      ? 'Bot executes trades autonomously — no approval required'
      : 'All trade decisions require your approval before execution';
    document.getElementById('mode-bar-btn').textContent = isAuto ? 'Switch to Managed' : 'Switch to Automatic';
    document.getElementById('mode-bar-btn').className   = 'kill-btn' + (isAuto ? ' resume' : '');
    toast(isAuto ? '⚡ Switched to Automatic mode' : '🎯 Switched to Managed mode', isAuto ? 'warn' : 'ok');
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
    const msgs = {'APPROVED': '✓ Trade approved', 'REJECTED': '✗ Trade rejected', 'PENDING_APPROVAL': '↩ Decision revoked'};
    toast(msgs[status] || status, status === 'APPROVED' ? 'ok' : status === 'PENDING_APPROVAL' ? 'ok' : 'err');
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

// ── API KEYS — per-field update with overwrite confirmation ──
let _keyCurrentValues = {};

async function loadKeyValues() {
  try {
    const r = await fetch('/api/get-keys');
    const d = await r.json();
    _keyCurrentValues = d;
    const set = (obsId, val) => { const el = document.getElementById(obsId); if (el) el.textContent = val || 'Not set'; };
    set('obs-alpaca-key',    d.ALPACA_API_KEY);
    // Show green checks if keys are set
    var keyCheck = document.getElementById('alpaca-key-check');
    var secCheck = document.getElementById('alpaca-secret-check');
    if (keyCheck) keyCheck.style.display = (d.ALPACA_API_KEY && d.ALPACA_API_KEY !== 'Not set') ? 'inline' : 'none';
    if (secCheck) secCheck.style.display = (d.ALPACA_SECRET_KEY && d.ALPACA_SECRET_KEY !== 'Not set') ? 'inline' : 'none';
    set('obs-alpaca-secret', d.ALPACA_SECRET_KEY);
    // resend + license removed from customer settings
    set('obs-alert-to',      d.ALERT_TO);
    const modeEl = document.getElementById('k-trading-mode');
    const liveOpt = document.getElementById('k-live-option');
    if (modeEl) modeEl.value = d.trading_mode || 'paper';
    if (liveOpt) {
      if (d.live_enabled) { liveOpt.disabled = false; }
      else { liveOpt.disabled = true; liveOpt.textContent = 'Live Trading (locked — contact operator)'; }
    }
  } catch(e) { console.warn('loadKeyValues error', e); }
}

function updateKey(keyName, inputId, obsId) {
  const inputEl = document.getElementById(inputId);
  const val     = inputEl?.value?.trim();
  if (!val) { toast('Enter a value first', 'err'); return; }
  const currentObs = _keyCurrentValues[keyName] || '';
  const hasExisting = currentObs && currentObs !== 'Not set';
  const doSave = async () => {
    document.getElementById('key-confirm-overlay').style.display = 'none';
    const r = await fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({[keyName]: val})});
    const d = await r.json();
    if (d.ok) { toast('✓ ' + keyName + ' updated', 'ok'); if (inputEl) inputEl.value = ''; await loadKeyValues(); }
    else toast('Error: ' + (d.errors||[]).join(', '), 'err');
  };
  if (hasExisting) {
    document.getElementById('key-confirm-title').textContent = 'Overwrite existing key?';
    document.getElementById('key-confirm-msg').textContent   = keyName + ' already has a value (' + currentObs + '). This will permanently replace it.';
    document.getElementById('key-confirm-btn').onclick       = doSave;
    document.getElementById('key-confirm-overlay').style.display = 'flex';
  } else {
    document.getElementById('key-confirm-title').textContent = 'Save new key?';
    document.getElementById('key-confirm-msg').textContent   = 'Save ' + keyName + '?';
    document.getElementById('key-confirm-btn').onclick       = doSave;
    document.getElementById('key-confirm-overlay').style.display = 'flex';
  }
}

async function updateTradingMode() {
  const mode   = document.getElementById('k-trading-mode')?.value;
  const urlMap = { paper: 'https://paper-api.alpaca.markets', live: 'https://api.alpaca.markets' };
  const url    = urlMap[mode];
  if (!url) { toast('Select a mode first', 'err'); return; }
  const r = await fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({'ALPACA_BASE_URL': url})});
  const d = await r.json();
  toast(d.ok ? '✓ Trading mode set to ' + mode : 'Error: ' + (d.errors||[]).join(', '), d.ok ? 'ok' : 'err');
}

// ── MY ACCOUNT ──
async function changeEmail() {
  const newEmail  = document.getElementById('acct-new-email')?.value?.trim();
  const curPw     = document.getElementById('acct-email-pw')?.value?.trim();
  const statusEl  = document.getElementById('acct-email-status');
  if (!newEmail || !curPw) { if (statusEl) { statusEl.textContent = 'All fields required'; statusEl.style.color = 'var(--pink)'; } return; }
  const r = await fetch('/api/account/change-email', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({new_email: newEmail, current_password: curPw})});
  const d = await r.json();
  if (statusEl) { statusEl.textContent = d.ok ? '✓ Email updated' : '✗ ' + (d.error||'Error'); statusEl.style.color = d.ok ? 'var(--teal)' : 'var(--pink)'; }
  if (d.ok) { document.getElementById('acct-new-email').value = ''; document.getElementById('acct-email-pw').value = ''; }
}

async function changePassword() {
  const curPw     = document.getElementById('acct-cur-pw')?.value?.trim();
  const newPw     = document.getElementById('acct-new-pw')?.value?.trim();
  const confirmPw = document.getElementById('acct-confirm-pw')?.value?.trim();
  const statusEl  = document.getElementById('acct-pw-status');
  if (!curPw || !newPw || !confirmPw) { if (statusEl) { statusEl.textContent = 'All fields required'; statusEl.style.color = 'var(--pink)'; } return; }
  const r = await fetch('/api/account/change-password', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({current_password: curPw, new_password: newPw, confirm_password: confirmPw})});
  const d = await r.json();
  if (statusEl) { statusEl.textContent = d.ok ? '✓ Password updated' : '✗ ' + (d.error||'Error'); statusEl.style.color = d.ok ? 'var(--teal)' : 'var(--pink)'; }
  if (d.ok) { ['acct-cur-pw','acct-new-pw','acct-confirm-pw'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; }); }
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
    {label:'Dow',       color:'#f5a623', fill:false, pointRadius:0},
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
    const accent   = isOrphan ? '#f5a623' : accentColors[i % accentColors.length];
    const plCol    = unreal >= 0 ? '#00f5d4' : '#ff4b6e';
    const dayCol   = dayPl >= 0  ? 'rgba(0,245,212,0.7)' : 'rgba(255,75,110,0.7)';
    const plSign   = unreal >= 0 ? '+' : '';
    const daySign  = dayPl >= 0  ? '+' : '';
    return `<div style="display:grid;grid-template-columns:32px 1fr auto auto auto;align-items:center;gap:10px;
              padding:7px 16px;border-bottom:1px solid rgba(255,255,255,0.04);cursor:pointer;
              ${isOrphan ? 'background:rgba(245,166,35,0.03)' : ''}"
              onclick="openPositionDrawer(${JSON.stringify(p).replace(/"/g,'&quot;')})"
              onmouseenter="this.style.background='rgba(255,255,255,0.02)'"
              onmouseleave="this.style.background='${isOrphan ? 'rgba(245,166,35,0.03)' : 'transparent'}'">
      <div style="width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;
           font-size:9px;font-weight:800;letter-spacing:0.02em;
           background:${accent}18;border:1px solid ${accent}40;color:${accent}">
        ${(p.ticker||'?').slice(0,4)}
      </div>
      <div style="min-width:0">
        <div style="display:flex;align-items:center;gap:5px">
          <span style="font-size:12px;font-weight:700;color:var(--text)">${p.ticker||'?'}</span>
          ${isOrphan ? '<span style="padding:1px 5px;border-radius:99px;font-size:8px;font-weight:700;background:rgba(245,166,35,0.12);border:1px solid rgba(245,166,35,0.3);color:#f5a623">ORPHAN</span>' : ''}
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

  const headerRow = '<div style="display:grid;grid-template-columns:32px 1fr auto auto auto;align-items:center;gap:10px;padding:4px 16px 6px;border-bottom:1px solid rgba(255,255,255,0.06)">'
    + '<div></div><div style="font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim)">Position</div>'
    + '<div style="text-align:right;font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim)">Value</div>'
    + '<div style="text-align:right;min-width:72px;font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim)">Today</div>'
    + '<div style="text-align:right;min-width:68px;font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--dim)">Total P&L</div>'
    + '</div>';
  const rows = [
    ...tracked.map((p,i) => renderRow(p, i, false)),
    ...orphans.map((p,i) => renderRow(p, i, true)),
  ];
  el.innerHTML = rows.length ? (headerRow + rows.join('')) : '<div class="empty-state" style="padding:12px 0"><div class="empty-icon">📊</div>No open positions</div>';
}

function renderApprovals(approvals) {
  const isAuto  = (document.getElementById('stat-mode')||{}).textContent === 'AUTONOMOUS';
  const el = document.getElementById('approval-list');
  const badge = document.getElementById('pending-badge');
  const qLabel = document.getElementById('queue-label');

  if (isAuto) {
    // Autonomous mode — show recent executed signals
    const recent = approvals.filter(a => ['EXECUTED','APPROVED'].includes(a.status)).slice(-5).reverse();
    if (qLabel) qLabel.textContent = 'Recent Signals';
    badge.textContent = recent.length + ' recent';
    if (!recent.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">⚡</div>No signals executed yet</div>';
      return;
    }
    el.innerHTML = recent.map(t => {
      const conf = (t.confidence||'').toUpperCase();
      const confCls = conf === 'HIGH' ? 'conf-high' : conf === 'MEDIUM' ? 'conf-med' : 'conf-low';
      return `<div class="trade-item"><div class="trade-header">
        <div class="trade-ticker-icon">${(t.ticker||'?').slice(0,4)}</div>
        <div class="trade-meta">
          <div class="trade-headline">${t.ticker||'?'} <span style="font-size:8px;padding:2px 6px;border-radius:99px;background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.14);color:#00f5d4">${t.status}</span></div>
          <div class="trade-sub">${(t.executed_at||t.queued_at||'').slice(0,16)}</div>
        </div>
        <div class="conf-chip ${confCls}">${conf}</div>
      </div></div>`;
    }).join('');
    return;
  }

  // Supervised/Managed mode — three tiers
  const pending  = approvals.filter(a => a.status === 'PENDING_APPROVAL');
  const queued   = approvals.filter(a => a.status === 'APPROVED');
  const rejected = approvals.filter(a => a.status === 'REJECTED');

  badge.textContent = pending.length ? pending.length + ' pending' :
                      queued.length  ? queued.length + ' queued'  : '0';

  let html = '';

  // ── Pending review ──
  if (pending.length) {
    html += pending.map(t => {
      const conf    = (t.confidence||'').toUpperCase();
      const confCls = conf === 'HIGH' ? 'conf-high' : conf === 'MEDIUM' ? 'conf-med' : 'conf-low';
      const reasoning = t.reasoning ? t.reasoning.slice(0,180) + (t.reasoning.length > 180 ? '...' : '') : '';
      const price   = t.price ? ` · $${parseFloat(t.price).toFixed(2)}` : '';
      const shares  = t.shares ? ` · ${parseFloat(t.shares).toFixed(4)} sh` : '';
      return `<div class="trade-item">
        <div class="trade-header">
          <div class="trade-ticker-icon">${(t.ticker||'?').slice(0,4)}</div>
          <div class="trade-meta">
            <div class="trade-headline">${t.ticker||'?'} · ${t.politician||'Unknown'}</div>
            <div class="trade-sub">${(t.queued_at||'').slice(0,16)}${price}${shares}</div>
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

  // ── Queued for execution ──
  if (queued.length) {
    html += '<div class="queue-divider">Queued for next session</div>';
    html += queued.map(t => {
      const price  = t.price ? '$' + parseFloat(t.price).toFixed(2) : '';
      const shares = t.shares ? parseFloat(t.shares).toFixed(2) + ' sh' : '';
      return `<div class="queued-card qc-approved">
        <div class="qc-ticker">${(t.ticker||'?').slice(0,4)}</div>
        <div class="qc-info">
          <div class="qc-name">${t.ticker||'?'}</div>
          <div class="qc-detail">${shares} @ ${price} · ${t.session||'next'} session</div>
        </div>
        <button class="btn-revoke" onclick="actionTrade(${t.id},'PENDING_APPROVAL')">Revoke</button>
      </div>`;
    }).join('');
  }

  // ── Recently rejected ──
  if (rejected.length) {
    html += '<div class="queue-divider">Rejected</div>';
    html += rejected.map(t => {
      const price  = t.price ? '$' + parseFloat(t.price).toFixed(2) : '';
      const shares = t.shares ? parseFloat(t.shares).toFixed(2) + ' sh' : '';
      return `<div class="queued-card qc-rejected">
        <div class="qc-ticker">${(t.ticker||'?').slice(0,4)}</div>
        <div class="qc-info">
          <div class="qc-name">${t.ticker||'?'}</div>
          <div class="qc-detail">${shares} @ ${price}</div>
        </div>
        <button class="btn-reapprove" onclick="actionTrade(${t.id},'APPROVED')">Approve</button>
      </div>`;
    }).join('');
  }

  // ── Empty state ──
  if (!html) {
    html = '<div class="empty-state"><div class="empty-icon">✅</div>No pending approvals</div>';
  }

  el.innerHTML = html;
}

// ── SYSTEM HEALTH ──
async function loadHealth() {
  try {
    const r = await fetch('/api/system-health');
    const d = await r.json();
  } catch(e) {}
}

// ── FLAGS MODAL ──
let _flagsData = [];

const FLAG_COLOR = {
  critical: {bar:'#ff4b6e', bg:'rgba(255,75,110,0.08)',  border:'rgba(255,75,110,0.14)',  badge:'rgba(255,75,110,0.09)',  text:'#ff4b6e'},
  warning:  {bar:'#f5a623', bg:'rgba(245,166,35,0.08)',  border:'rgba(245,166,35,0.25)',  badge:'rgba(245,166,35,0.15)',  text:'#f5a623'},
  info:     {bar:'#00f5d4', bg:'rgba(0,245,212,0.06)',   border:'rgba(0,245,212,0.12)',    badge:'rgba(0,245,212,0.12)',   text:'#00f5d4'},
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
           style="padding:5px 14px;border-radius:8px;border:1px solid rgba(255,75,110,0.18);
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
    // Render approvals and positions immediately — don't let status processing errors block them
    try { renderApprovals(a); } catch(ae) { console.log('Approvals render error:', ae); }
    try { renderPositions(s.positions||[]); } catch(pe) { console.log('Positions render error:', pe); }
    // Stats
    const sv = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
    sv('stat-portfolio', '$'+(s.portfolio_value||0).toFixed(2));
    sv('stat-cash', '$'+(s.cash||0).toFixed(2));
    sv('stat-positions', s.open_positions||0);
    sv('stat-pos-display', s.open_positions||0);
    sv('stat-flags', s.urgent_flags||0);
    sv('stat-heartbeat', (s.last_heartbeat||'Never').slice(0,16));
    sv('stat-mode', s.operating_mode||'SUPERVISED');
    const modeEl = document.getElementById('mode-display');
    if (modeEl) {
      const m = s.operating_mode || 'SUPERVISED';
      modeEl.textContent = m;
      modeEl.style.color = m === 'AUTONOMOUS' ? 'var(--amber)' : 'var(--teal)';
    }
    const hbAge = document.getElementById('stat-hb-age');
    if (hbAge && s.last_heartbeat) {
      try {
        const ageSecs = Math.floor((Date.now() - new Date(s.last_heartbeat).getTime()) / 1000);
        hbAge.textContent = ageSecs < 120 ? ageSecs + 's ago' : Math.floor(ageSecs/60) + 'm ago';
        hbAge.style.color = ageSecs > 300 ? 'rgba(255,75,110,0.7)' : ageSecs > 90 ? 'rgba(245,166,35,0.7)' : '';
      } catch(e) {}
    }
    // Position sub: show orphan warning
    const posSub = document.getElementById('stat-positions-sub');
    if (posSub) {
      const orp = s.orphan_count || 0;
      posSub.textContent = orp > 0 ? `Open · ${orp} orphan` : 'Open';
      posSub.style.color = orp > 0 ? 'rgba(245,166,35,0.8)' : '';
    }
    // Today's P&L — sum day_pl across all positions
    const positions = s.positions || [];
    const dayPl  = positions.reduce((sum, p) => sum + (p.day_pl || 0), 0);
    const portVal = s.portfolio_value || 1;
    const dayPlPct = (dayPl / (portVal - dayPl)) * 100;
    const dayPlEl = document.getElementById('stat-day-pl');
    if (dayPlEl) {
      const sign = dayPl >= 0 ? '+' : '';
      dayPlEl.textContent = sign + '$' + Math.abs(dayPl).toFixed(2) + ' (' + sign + Math.abs(dayPlPct).toFixed(2) + '%)';
      dayPlEl.style.color = dayPl >= 0 ? 'var(--teal)' : 'var(--pink)';
    }
    // Benchmark delta vs S&P
    const vsEl = document.getElementById('stat-vs-sp');
    if (vsEl && _spyChangePct !== null) {
      const diff = dayPlPct - _spyChangePct;
      const diffSign = diff >= 0 ? '+' : '';
      vsEl.textContent = '· vs S&P ' + diffSign + diff.toFixed(2) + '%';
      vsEl.style.color = diff >= 0 ? 'rgba(0,245,212,0.14)' : 'rgba(255,75,110,0.14)';
    }
    // Buying power as % of portfolio
    const bpEl = document.getElementById('stat-bp-pct');
    if (bpEl && portVal > 0) {
      const bpPct = ((s.cash || 0) / portVal * 100).toFixed(0);
      bpEl.textContent = bpPct + '% of portfolio';
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
        flagSub.style.color = 'rgba(245,166,35,0.8)';
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
      const names = {'retail_trade_logic_agent.py':'Trade Logic','retail_news_agent.py':'News',
                     'retail_market_sentiment_agent.py':'Market Sentiment','retail_sector_screener.py':'Sector Screener'};
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
    // renderPositions moved to top of function
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
  } catch(e) { console.error('loadIntel error:', e); }
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
  const colors = ['background:linear-gradient(135deg,rgba(0,245,212,0.18),rgba(0,245,212,0.1));border:1px solid rgba(0,245,212,0.14);color:#00f5d4','background:linear-gradient(135deg,rgba(123,97,255,0.18),rgba(123,97,255,0.1));border:1px solid rgba(123,97,255,0.14);color:#a78bfa','background:linear-gradient(135deg,rgba(245,166,35,0.3),rgba(245,166,35,0.1));border:1px solid rgba(245,166,35,0.25);color:#f5a623','background:linear-gradient(135deg,rgba(255,75,110,0.18),rgba(255,75,110,0.1));border:1px solid rgba(255,75,110,0.14);color:#ff4b6e'];
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
    return `<div class="charm ${sent}" style="cursor:pointer" data-sig-idx="${i}" onclick="openSigModal(allSignals[this.dataset.sigIdx])">
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


function updateHeroCards(signals) {
  if (!signals || !signals.length) return;
  function _as(s) {
    var base = s.confidence==='HIGH'?87:s.confidence==='MEDIUM'?63:s.confidence==='NOISE'?15:30;
    var sent = s.sentiment_score ? Math.round(Math.abs(s.sentiment_score)*20) : 0;
    return Math.min(99, Math.max(5, base + sent));
  }
  function _ms(s) {
    var a = _as(s);
    var adj = s.is_stale ? -12 : (s.corroborated ? 8 : 0);
    return Math.min(99, Math.max(5, a - 8 + adj));
  }
  var fresh = signals.filter(function(s){ return !s.is_stale; });
  if (!fresh.length) fresh = signals;
  var ranked = fresh.slice().sort(function(a,b){
    return (_as(b)+(b.corroborated?10:0)) - (_as(a)+(a.corroborated?10:0));
  });
  var top = ranked[0];
  var diverged = fresh.slice().sort(function(a,b){
    return Math.abs(_as(b)-_ms(b)) - Math.abs(_as(a)-_ms(a));
  });
  var div = diverged[0];
  if (div === top && diverged.length > 1) div = diverged[1];
  var cards = document.querySelectorAll('.hero-card');
  // Hero #1 Conviction
  if (cards[0] && top) {
    var a1=_as(top), m1=_ms(top);
    var c1 = signals.filter(function(s){return s.ticker===top.ticker}).length;
    cards[0].querySelector('.hero-icon').textContent = (top.ticker||'?').slice(0,4);
    cards[0].querySelector('.hero-ticker').textContent = top.ticker||'??';
    cards[0].querySelector('.hero-label').textContent = (top.headline||'').slice(0,40)||'Top Signal';
    var cv=cards[0].querySelectorAll('.hero-conv');
    if(cv[0]){cv[0].querySelector('.hero-conv-val').textContent=a1;cv[0].querySelector('.hero-conv-fill').style.width=a1+'%';}
    if(cv[1]){cv[1].querySelector('.hero-conv-val').textContent=m1;cv[1].querySelector('.hero-conv-fill').style.width=m1+'%';}
    var hm=cards[0].querySelectorAll('.hm-val');
    if(hm[0])hm[0].textContent=(a1>m1?'+':'')+(a1-m1);
    if(hm[1])hm[1].textContent=c1+' hit'+(c1!==1?'s':'');
    if(hm[2])hm[2].textContent=top.corroborated?'Confirmed':'Pending';
    var p=cards[0].querySelector('.hero-pending');if(p)p.style.display='none';
  }
  // Hero #2 Divergence
  if (cards[1] && div) {
    var a2=_as(div), m2=_ms(div), sp=a2-m2;
    var c2 = signals.filter(function(s){return s.ticker===div.ticker}).length;
    cards[1].querySelector('.hero-icon').textContent = (div.ticker||'?').slice(0,4);
    cards[1].querySelector('.hero-ticker').textContent = div.ticker||'??';
    cards[1].querySelector('.hero-label').textContent = (div.headline||'').slice(0,40)||'Agent Edge';
    var cv2=cards[1].querySelectorAll('.hero-conv');
    if(cv2[0]){cv2[0].querySelector('.hero-conv-val').textContent=a2;cv2[0].querySelector('.hero-conv-fill').style.width=a2+'%';}
    if(cv2[1]){cv2[1].querySelector('.hero-conv-val').textContent=m2;cv2[1].querySelector('.hero-conv-fill').style.width=m2+'%';}
    var hm2=cards[1].querySelectorAll('.hm-val');
    if(hm2[0])hm2[0].textContent=(sp>=0?'+':'')+sp+' pts';
    if(hm2[1])hm2[1].textContent=c2+' hit'+(c2!==1?'s':'');
    if(hm2[2])hm2[2].textContent=div.corroborated?'Confirmed':'Pending';
    var p2=cards[1].querySelector('.hero-pending');if(p2)p2.style.display='none';
  }
}

function openSigModal(s) {
  const colors = {HIGH:'rgba(0,245,212,0.09)',MEDIUM:'rgba(123,97,255,0.09)',LOW:'rgba(245,166,35,0.15)',NOISE:'rgba(85,86,102,0.2)'};
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
    const congBadge = f => f==='recent_buy' ? '<span style="font-size:10px;background:rgba(0,245,212,0.09);color:#00f5d4;padding:2px 6px;border-radius:4px;margin-top:4px;display:inline-block">Congress: Buy</span>' : f==='recent_sell' ? '<span style="font-size:10px;background:rgba(255,75,110,0.09);color:#ff4b6e;padding:2px 6px;border-radius:4px;margin-top:4px;display:inline-block">Congress: Sell</span>' : '';
    const scoreColor = cs => cs>=0.6?'#00f5d4':cs>=0.4?'#f5a623':'#ff4b6e';
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
// ── MARKET CLOCK ──
let _spyChangePct = null;  // cached from indices load

function updateMarketClock() {
  const now = new Date();
  // Convert to ET
  const etStr = now.toLocaleString('en-US', {timeZone:'America/New_York'});
  const et = new Date(etStr);
  const day = et.getDay(); // 0=Sun 6=Sat
  const h   = et.getHours();
  const m   = et.getMinutes();
  const mins = h * 60 + m;

  const pill    = document.getElementById('pill-market-clock');
  const dot     = document.getElementById('clock-dot');
  const label   = document.getElementById('clock-label');
  const cdown   = document.getElementById('clock-countdown');
  if (!pill) return;

  const fmt = (totalMins) => {
    const hh = Math.floor(totalMins / 60);
    const mm = totalMins % 60;
    return hh > 0 ? `${hh}h ${mm}m` : `${mm}m`;
  };

  const isWeekend = day === 0 || day === 6;
  let status, dotCls, pillCls, nextLabel, minsToNext, nextOpenStr;

  // Compute next market open date+time for display when closed/weekend
  function getNextMarketOpen(etNow) {
    const d = new Date(etNow);
    const dow = d.getDay();
    if (dow === 6) d.setDate(d.getDate() + 2);       // Sat → Mon
    else if (dow === 0) d.setDate(d.getDate() + 1);   // Sun → Mon
    else if (d.getHours() * 60 + d.getMinutes() >= 960) {
      // After 4pm weekday → next business day
      d.setDate(d.getDate() + (dow === 5 ? 3 : 1));   // Fri → Mon, else +1
    }
    d.setHours(9, 30, 0, 0);
    const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return dayNames[d.getDay()] + ' ' + mon[d.getMonth()] + ' ' + d.getDate() + ' 9:30a ET';
  }

  if (isWeekend) {
    status = 'CLOSED'; dotCls = 'dot-dim'; pillCls = 'sp-dim';
    nextOpenStr = getNextMarketOpen(et);
  } else if (mins < 240) {                   // < 4:00 AM
    status = 'CLOSED'; dotCls = 'dot-dim'; pillCls = 'sp-dim';
    nextLabel = 'Pre'; minsToNext = 240 - mins;
  } else if (mins < 570) {                   // 4:00 – 9:30 AM
    status = 'PRE-MARKET'; dotCls = 'dot-warn'; pillCls = 'sp-warn';
    nextLabel = 'Open'; minsToNext = 570 - mins;
  } else if (mins < 960) {                   // 9:30 AM – 4:00 PM
    status = 'OPEN'; dotCls = 'dot-on'; pillCls = 'sp-ok';
    nextLabel = 'Close'; minsToNext = 960 - mins;
  } else if (mins < 1200) {                  // 4:00 – 8:00 PM
    status = 'AFTER-HOURS'; dotCls = 'dot-warn'; pillCls = 'sp-warn';
    nextLabel = 'Closed'; minsToNext = 1200 - mins;
  } else {                                    // 8pm+ weekday
    status = 'CLOSED'; dotCls = 'dot-dim'; pillCls = 'sp-dim';
    nextOpenStr = getNextMarketOpen(et);
  }

  pill.className  = 'status-pill ' + pillCls;
  dot.className   = 'status-dot ' + dotCls;
  label.textContent = status;
  if (nextOpenStr) {
    cdown.textContent = '· Opens ' + nextOpenStr;
  } else {
    cdown.textContent = minsToNext ? `· ${nextLabel} ${fmt(minsToNext)}` : '';
  }
}

async function loadMarketIndices() {
  try {
    const r = await fetch('/api/market-indices');
    const d = await r.json();
    const bar = document.getElementById('market-indices-bar');
    if (!bar || !d.indices || !d.indices.length) return;
    // Cache SPY for benchmark comparison
    const spy = d.indices.find(i => i.symbol === 'SPY' || i.label === 'S&P 500');
    if (spy) _spyChangePct = spy.chg_pct;

    bar.innerHTML = d.indices.map(idx => {
      const pct   = idx.chg_pct;
      const color = pct > 0.05 ? 'var(--teal)' : pct < -0.05 ? 'var(--pink)' : 'rgba(255,255,255,0.25)';
      const arrow = pct > 0.05 ? '▲' : pct < -0.05 ? '▼' : '—';
      return `<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;
        padding:6px 14px;display:flex;align-items:center;gap:10px;position:relative;overflow:hidden;">
        <div style="position:absolute;top:0;left:15%;right:15%;height:2px;
          background:linear-gradient(90deg,transparent,${color},transparent);
          box-shadow:0 0 6px ${color}"></div>
        <span style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em">${idx.label}</span>
        <span style="font-size:13px;font-weight:700;color:var(--text);font-family:var(--mono)">$${idx.price.toFixed(2)}</span>
        <span style="font-size:11px;font-weight:700;color:${color}">${arrow} ${Math.abs(pct).toFixed(2)}%</span>
      </div>`;
    }).join('');
  } catch(e) {}
}

// ── TRADER ACTIVITY ──
async function loadTraderActivity() {
  try {
    const r = await fetch('/api/trader-activity');
    const d = await r.json();
    const el = document.getElementById('history-list') || document.getElementById('trader-activity-list');
    const ts = document.getElementById('trader-activity-ts');
    if (!el) return;
    const items = [];
    (d.scans||[]).slice(0,8).forEach(s => {
      const _tierMap = {'1':'HIGH','2':'MEDIUM','3':'LOW','4':'QUIET'};
      const tier    = (_tierMap[String(s.tier)] || String(s.tier||'LOW')).toUpperCase();
      const tierCls = tier==='HIGH'?'conf-high':tier==='MEDIUM'?'conf-med':'conf-low';
      const cascade = s.cascade_detected ? '<span style="color:var(--pink);font-size:8px;margin-left:4px">CASCADE</span>' : '';
      const summary = (s.event_summary||'Scanned').slice(0,60);
      const time    = (s.scanned_at||'').slice(11,16);
      const ticker  = s.ticker||'?';
      items.push(`<div class="agent-row"
          data-ticker="${ticker.replace(/"/g,'')}"
          data-conf="${tier}"
          data-summary="${summary.replace(/"/g,'')}"
          data-type="SCAN"
          onmouseenter="showIntelTooltip(event,this)"
          onmouseleave="hideIntelTooltip()"
          onclick="openLogicModal(this)">
        <div class="agent-row-icon" style="background:linear-gradient(135deg,rgba(245,166,35,0.2),rgba(245,166,35,0.05));border:1px solid rgba(245,166,35,0.2);color:var(--amber)">${ticker.slice(0,4)}</div>
        <div class="agent-row-body">
          <div class="agent-row-ticker">${ticker}${cascade}</div>
          <div class="agent-row-sub">${summary}</div>
        </div>
        <div class="agent-row-right">
          <div class="conf-chip ${tierCls}">${tier}</div>
          <div class="agent-row-time">${time}</div>
        </div>
      </div>`);
    });
    // Add recent system_log entries (trade decisions, approvals, etc.)
    (d.recent||[]).slice(0,12).forEach(r => {
      const ev   = r.event||'EVENT';
      const agent = r.agent||'';
      const time = (r.timestamp||'').slice(11,16);
      let det = '';
      try { det = typeof r.details === 'string' ? r.details : JSON.stringify(r.details); } catch(e){ det=r.details||''; }
      det = (det||'').slice(0,70);

      const evColors = {
        'TRADE_DECISION':'var(--teal)','TRADE_APPROVED':'#00f5d4',
        'TRADE_PENDING_APPROVAL':'var(--amber)','TRADE_REJECTED':'var(--pink)',
        'BIL_BUY':'var(--purple)','ORPHAN_POSITION':'var(--pink)',
        'AGENT_COMPLETE':'var(--dim)'
      };
      const evColor = evColors[ev] || 'var(--muted)';
      const evShort = ev.replace('TRADE_','').replace('_',' ');

      items.push('<div class="agent-row" style="cursor:default">'
        +'<div class="agent-row-icon" style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);color:'+evColor+';font-size:8px;letter-spacing:0.02em">'+evShort.slice(0,4)+'</div>'
        +'<div class="agent-row-body">'
        +'<div class="agent-row-ticker" style="color:'+evColor+'">'+evShort+'<span style="font-size:9px;color:var(--dim);margin-left:6px">'+agent+'</span></div>'
        +'<div class="agent-row-sub">'+det+'</div>'
        +'</div>'
        +'<div class="agent-row-right"><div class="agent-row-time">'+time+'</div></div>'
        +'</div>');
    });

    if (!items.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">\u26a1</div>No recent activity</div>';
    } else {
      el.innerHTML = items.join('');
      if (ts) ts.textContent = 'updated ' + new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
    }
  } catch(e) {}
}

// ── PLANNING PANEL ──
async function loadPlanning() {
  try {
    const r = await fetch('/api/watchlist');
    const d = await r.json();
    const signals = d.signals || [];
    const el    = document.getElementById('planning-list');
    const cntEl = document.getElementById('planning-count');
    if (!el) return;
    const fresh = signals.filter(s => !s.is_stale);
    if (cntEl) cntEl.textContent = fresh.length + ' active';
    if (!signals.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">\U0001f50d</div>No signals under watch</div>';
      return;
    }
    el.innerHTML = signals.slice(0,8).map(sig => {
      const conf    = (sig.confidence||'LOW').toUpperCase();
      const confCls = conf==='HIGH'?'conf-high':conf==='MEDIUM'?'conf-med':'conf-low';
      const ticker  = sig.ticker||'?';
      const sigType = sig.signal_type||'WATCH';
      const summary = (sig.headline||sig.reason||sig.summary||'Signal detected').slice(0,60);
      const stale   = sig.is_stale ? '<span style="font-size:8px;color:var(--dim);margin-left:4px">archive</span>' : '';
      return `<div class="agent-row"
          data-ticker="${ticker.replace(/"/g,'')}"
          data-conf="${conf}"
          data-summary="${summary.replace(/"/g,'')}"
          data-type="${sigType}"
          onmouseenter="showIntelTooltip(event,this)"
          onmouseleave="hideIntelTooltip()"
          onclick="openLogicModal(this)">
        <div class="agent-row-icon" style="background:linear-gradient(135deg,rgba(0,245,212,0.12),rgba(0,245,212,0.05));border:1px solid rgba(0,245,212,0.12);color:var(--teal)">${ticker.slice(0,4)}</div>
        <div class="agent-row-body">
          <div class="agent-row-ticker">${ticker}<span style="font-size:9px;color:var(--muted);font-weight:400;margin-left:4px">${sigType}</span>${stale}</div>
          <div class="agent-row-sub">${summary}</div>
        </div>
        <div class="agent-row-right"><div class="conf-chip ${confCls}">${conf}</div></div>
      </div>`;
    }).join('');
  } catch(e) { console.log('Planning error:', e); }
}

// ── INTEL TOOLTIP ──
function showIntelTooltip(event, el) {
  const tt = document.getElementById('intel-tooltip');
  const body = document.getElementById('intel-tooltip-body');
  if (!tt||!body) return;
  const ticker  = el.dataset.ticker  || '?';
  const conf    = el.dataset.conf    || 'LOW';
  const summary = el.dataset.summary || 'Signal detected';
  const type    = el.dataset.type    || 'WATCH';
  body.innerHTML = [
    ticker + ' flagged as ' + type + ' \u00b7 confidence: ' + conf,
    summary.slice(0,80),
    'Click to view full agent logic breakdown'
  ].map(p => '<div class="intel-point"><div class="intel-point-dot"></div><span>' + p + '</span></div>').join('');
  tt.style.left = Math.min(event.clientX+14, window.innerWidth-234) + 'px';
  tt.style.top  = Math.min(event.clientY+10, window.innerHeight-130) + 'px';
  tt.classList.add('visible');
}
function hideIntelTooltip() {
  const tt = document.getElementById('intel-tooltip');
  if (tt) tt.classList.remove('visible');
}

// ── LOGIC MODAL ──
function openLogicModal(el) {
  hideIntelTooltip();
  const overlay = document.getElementById('logic-overlay');
  if (!overlay) return;
  const ticker  = el.dataset.ticker  || '?';
  const conf    = el.dataset.conf    || 'LOW';
  const summary = el.dataset.summary || '';
  const type    = el.dataset.type    || 'SIGNAL';
  const confCls = conf==='HIGH'?'conf-high':conf==='MEDIUM'?'conf-med':'conf-low';
  const t = document.getElementById('logic-modal-title');
  const c = document.getElementById('logic-modal-conf');
  const b = document.getElementById('logic-modal-body');
  if (t) t.textContent = ticker + ' \u2014 Agent Logic Breakdown';
  if (c) c.innerHTML = '<div class="conf-chip ' + confCls + '">' + conf + '</div>';
  if (b) b.innerHTML =
    '<div style="margin-bottom:16px;padding:12px;border-radius:10px;background:rgba(255,255,255,0.03);border:1px solid var(--border)">'
    + '<div style="font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">Signal Summary</div>'
    + '<div style="font-size:12px;color:var(--text)">' + ticker + ' \u00b7 ' + type + ' \u00b7 Confidence: ' + conf + '</div>'
    + (summary ? '<div style="font-size:11px;color:var(--muted);margin-top:4px">' + summary + '</div>' : '')
    + '</div>'
    + '<div class="logic-placeholder"><div class="logic-placeholder-icon">\U0001f9e0</div>'
    + '<div class="logic-placeholder-title">Full Logic Breakdown Coming Soon</div>'
    + '<div class="logic-placeholder-sub">Gate-by-gate analysis and agent reasoning<br>will appear here in the next update.</div></div>';
  overlay.classList.add('open');
}
function closeLogicModal(e) {
  if (e && e.target !== document.getElementById('logic-overlay')) return;
  const o = document.getElementById('logic-overlay');
  if (o) o.classList.remove('open');
}

// ── SETTINGS SAVE (dashboard + settings tab share one function) ──
async function saveQuickSettings() {
  const g = id => document.getElementById(id);
  const data = {
    min_confidence:    (g('qs-min-confidence') || g('s-min-conf'))?.value    || 'MEDIUM',
    max_position_pct:  (parseInt((g('qs-max-pos') || g('s-max-pos'))?.value) || 10) / 100,
    close_session_mode:(g('qs-close-mode') || g('s-close-mode'))?.value      || 'conservative',
    max_trade_usd:     parseFloat((g('qs-max-trade') || g('s-max-trade-usd'))?.value) || 0,
    max_sector_pct:    parseFloat((g('qs-max-sector') || g('s-max-sector'))?.value)   || 40,
    max_staleness:     (g('qs-staleness') || g('s-staleness'))?.value        || 'Fresh',
    spousal_weight:    (g('qs-spousal')   || g('s-spousal'))?.value          || 'reduced',
  };
  try {
    const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const d = await r.json();
    if (d.ok) toast('Settings saved', 'ok');
    else toast('Save failed: ' + (d.error||'unknown'), 'err');
  } catch(e) { toast('Settings save error', 'err'); }
}
const saveSettings = saveQuickSettings;

// ── INIT ──
updateClock();
setInterval(updateClock, 1000);

function updateClock() {
  const now = new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false});
  // Could add a clock element if needed
}

loadLiveStatus();
loadPlanning();
loadKeyValues();
loadMarketChart(36);
loadHealth();
loadAudit();
loadMarketIndices();
loadTraderActivity();
checkNewAccountBanner();
loadCfgPanel();
// First-time setup guide check
fetch('/api/customer-settings').then(r=>r.json()).then(function(d){
  if (d.SETUP_COMPLETE !== '1') {
    showTab('guide');
  }
});
loadNews('all');
updateMarketClock();
updateSessionTimeline();
loadPerformance();
setInterval(loadLiveStatus, 30000);
setInterval(loadPlanning, 60000);
setInterval(updateSessionTimeline, 60000);
setInterval(loadHealth, 60000);
setInterval(loadAudit, 300000);
setInterval(loadMarketIndices, 120000);
setInterval(loadTraderActivity, 60000);
setInterval(updateMarketClock, 60000);

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
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px 0;color:var(--muted);font-size:13px">No news articles yet — News agent will populate on next run</div>';
      return;
    }
    grid.innerHTML = _newsArticles.map((a, idx) => {
      const stale  = a.staleness && a.staleness !== 'fresh';
      const cat    = a.category || 'Markets';
      const catCol = cat==='Breaking' ? 'var(--red)' : cat==='US' ? 'var(--teal)' : cat==='Global' ? 'var(--purple)' : 'var(--muted)';
      const age    = a.pub_date ? timeSince(a.pub_date) : (a.staleness || '');
      const storedImg = a.image_url || '';
      const cachedImg = a.link && _metaCache[a.link] && _metaCache[a.link].image;
      const imgSrc    = storedImg || cachedImg || '';
      const imgHtml   = imgSrc
        ? `<div style="margin:-12px -16px 12px;border-radius:10px 10px 0 0;overflow:hidden;height:140px"><img src="${imgSrc}" alt="" style="width:100%;height:100%;object-fit:cover" onerror="this.parentElement.style.display='none'"></div>`
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
    // Lazy-load OG images only for articles with no stored image_url
    _newsArticles.forEach((a, idx) => {
      if (!a.image_url && a.link && !_metaCache[a.link]) {
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
    }


@app.route('/')
def index():
    # Unauthenticated visitors see the public landing page
    if not is_authenticated():
        return render_template_string(LANDING_HTML)

    # Serve page shell immediately — JS loads live data async via /api/status
    # This means the page renders in <100ms regardless of DB state
    settings  = get_current_settings()

    # Per-customer operating mode from auth.db (falls back to env for legacy admin)
    customer_id    = session.get('customer_id', 'admin')
    operating_mode = _customer_db().get_setting('OPERATING_MODE') or (auth.get_operating_mode(customer_id) if customer_id != 'admin' else OPERATING_MODE)

    # Safe skeleton status — JS will overwrite with live data
    skeleton_status = {
        "portfolio_value": 0,
        "cash":            0,
        "realized_gains":  0,
        "open_positions":  0,
        "positions":       [],
        "urgent_flags":    0,
        "last_heartbeat":  "Loading...",
        "operating_mode":  operating_mode,
        "pi_id":           PI_ID,
    }

    # Grace period banner — shown when customer is past_due but within the 7-day window
    grace_warning = (session.get('access_reason') == 'grace_period')

    return render_template_string(
        PORTAL_HTML,
        status=skeleton_status,
        approvals=[],
        pending_count=0,
        operating_mode=operating_mode,
        pi_id=PI_ID,
        settings=settings,
        async_load=True,
        grace_warning=grace_warning,
    )


@app.route('/api/set-mode', methods=['POST'])
@login_required
def api_set_mode():
    """Toggle between MANAGED (approve all trades) and AUTOMATIC (bot executes) per customer."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', '').upper()
    if mode not in ('MANAGED', 'AUTOMATIC'):
        return jsonify({"ok": False, "error": "mode must be MANAGED or AUTOMATIC"}), 400
    customer_id = session.get('customer_id', 'admin')
    try:
        auth.set_operating_mode(customer_id, mode)
        log.info(f"Operating mode set to {mode} for customer {customer_id}")
        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        log.error(f"set-mode error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/kill-switch', methods=['POST'])
@login_required
def api_kill_switch():
    """Per-customer kill switch stored in customer_settings DB.
    Global .kill_switch file remains as admin ALL-STOP override."""
    data   = request.get_json(silent=True) or {}
    engage = data.get('engage', True)
    try:
        db = _customer_db()
        db.set_setting('KILL_SWITCH', '1' if engage else '0')
        if engage:
            log.warning(f"KILL SWITCH ENGAGED for customer {session.get('customer_id','?')}")
            db.log_event("KILL_SWITCH_ENGAGED", agent="portal",
                         details=f"Per-customer kill switch engaged at {now_et()}")
        else:
            log.info(f"Kill switch cleared for customer {session.get('customer_id','?')}")
            db.log_event("KILL_SWITCH_CLEARED", agent="portal",
                         details=f"Per-customer kill switch cleared at {now_et()}")
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
    if status not in ('APPROVED', 'REJECTED', 'PENDING_APPROVAL'):
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    try:
        db      = _customer_db()
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


@app.route('/api/keys', methods=['POST'])
def api_keys():
    """Update API keys. Accepts portal session or monitor-token bearer."""
    auth_header   = request.headers.get('Authorization', '')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    token_ok = bool(monitor_token and auth_header == f'Bearer {monitor_token}')
    if not token_ok and not session.get('customer_id'):
        return jsonify({'ok': False, 'updated': [], 'errors': ['Not authenticated']}), 401
    data = request.get_json(silent=True) or {}

    # Alpaca credentials go to auth.db (encrypted), not .env
    ALPACA_KEYS = {'ALPACA_API_KEY', 'ALPACA_SECRET_KEY'}

    # Whitelist of keys that write to .env
    ALLOWED_KEYS = {
        'ANTHROPIC_API_KEY',
        'ALPACA_BASE_URL',
        'RESEND_API_KEY',
        'MONITOR_TOKEN',
        'MONITOR_URL',
        'COMPANY_URL',
        'LICENSE_KEY',
        'LIVE_TRADING_ENABLED',
        'PI_LABEL',
        'PI_EMAIL',
        'OPERATOR_EMAIL',
        'ALERT_FROM',
        'ALERT_TO',
        'ALERT_PHONE',
        'CARRIER_GATEWAY',
        'GMAIL_USER',
        'GMAIL_APP_PASSWORD',
        'TRADING_MODE',
        'OPERATING_MODE',
    }

    updated = []
    errors  = []

    # ── Handle Alpaca credentials → auth.db ──
    alpaca_key    = data.pop('ALPACA_API_KEY', None)
    alpaca_secret = data.pop('ALPACA_SECRET_KEY', None)
    if alpaca_key or alpaca_secret:
        customer_id = session.get('customer_id')
        if not customer_id or customer_id == 'admin':
            errors.append("ALPACA_API_KEY: no customer session to store credentials against")
        else:
            try:
                # Fetch existing values to avoid wiping one when only the other is submitted
                existing_key, existing_secret = auth.get_alpaca_credentials(customer_id)
                new_key    = alpaca_key.strip()    if alpaca_key    else existing_key
                new_secret = alpaca_secret.strip() if alpaca_secret else existing_secret
                auth.set_alpaca_credentials(customer_id, new_key, new_secret)
                if alpaca_key:
                    updated.append('ALPACA_API_KEY')
                if alpaca_secret:
                    updated.append('ALPACA_SECRET_KEY')
            except Exception as e:
                errors.append(f"ALPACA credentials: {str(e)}")

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



@app.route('/api/get-keys')
@login_required
def api_get_keys():
    """Return obfuscated current values of customer-visible keys."""
    def _obs(val):
        if not val: return ''
        s = str(val)
        return s[:4] + '••••••' + s[-4:] if len(s) > 8 else '••••••••'
    customer_id = session.get('customer_id', '')
    alpaca_key = alpaca_secret = ''
    try:
        alpaca_key, alpaca_secret = auth.get_alpaca_credentials(customer_id)
    except Exception:
        pass
    # Fallback to env vars — ONLY for admin sessions (not regular customers)
    if not alpaca_key and customer_id == 'admin':
        alpaca_key = os.environ.get('ALPACA_API_KEY', '')
    if not alpaca_secret and customer_id == 'admin':
        alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    base_url = os.environ.get('ALPACA_BASE_URL', '')
    trading_mode = 'live' if (base_url and 'paper' not in base_url.lower()) else 'paper'
    live_enabled = os.environ.get('LIVE_TRADING_ENABLED', 'false').lower() == 'true'
    return jsonify({
        'ALPACA_API_KEY':    _obs(alpaca_key),
        'ALPACA_SECRET_KEY': _obs(alpaca_secret),
        'RESEND_API_KEY':    _obs(os.environ.get('RESEND_API_KEY', '')),
        'LICENSE_KEY':       _obs(os.environ.get('LICENSE_KEY', '')),
        'ALERT_TO':          _obs(os.environ.get('ALERT_TO', '')),
        'trading_mode':      trading_mode,
        'live_enabled':      live_enabled,
    })


@app.route('/api/account/change-password', methods=['POST'])
@login_required
def api_change_password():
    """Change password — requires current password."""
    data        = request.get_json(silent=True) or {}
    current_pw  = data.get('current_password', '').strip()
    new_pw      = data.get('new_password', '').strip()
    confirm_pw  = data.get('confirm_password', '').strip()
    customer_id = session.get('customer_id', '')
    if not current_pw or not new_pw or not confirm_pw:
        return jsonify({'ok': False, 'error': 'All fields are required'})
    if new_pw != confirm_pw:
        return jsonify({'ok': False, 'error': 'New passwords do not match'})
    if len(new_pw) < 8:
        return jsonify({'ok': False, 'error': 'New password must be at least 8 characters'})
    try:
        auth.update_password(customer_id, current_pw, new_pw)
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'ok': False, 'error': 'Server error'})


@app.route('/api/account/change-email', methods=['POST'])
@login_required
def api_change_email():
    """Change email — requires current password."""
    data        = request.get_json(silent=True) or {}
    current_pw  = data.get('current_password', '').strip()
    new_email   = data.get('new_email', '').strip()
    customer_id = session.get('customer_id', '')
    if not current_pw or not new_email:
        return jsonify({'ok': False, 'error': 'All fields are required'})
    if '@' not in new_email or '.' not in new_email.split('@')[-1]:
        return jsonify({'ok': False, 'error': 'Invalid email address'})
    try:
        auth.update_email(customer_id, current_pw, new_email)
        session['customer_email'] = new_email
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'ok': False, 'error': 'Server error'})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_settings():
    """Save trading settings to per-customer DB (customer_settings table).
    Falls through to global .env only for system-level keys."""
    data = request.get_json(silent=True) or {}

    # Per-customer trading params → customer_settings table
    customer_keys = {
        'min_confidence':     'MIN_CONFIDENCE',
        'max_position_pct':   'MAX_POSITION_PCT',
        'max_trade_usd':      'MAX_TRADE_USD',
        'max_positions':      'MAX_POSITIONS',
        'max_daily_loss':     'MAX_DAILY_LOSS',
        'max_sector_pct':     'MAX_SECTOR_PCT',
        'close_session_mode': 'CLOSE_SESSION_MODE',
        'max_staleness':      'MAX_STALENESS',
        'max_drawdown_pct':   'MAX_DRAWDOWN_PCT',
        'max_holding_days':   'MAX_HOLDING_DAYS',
        'max_gross_exposure':  'MAX_GROSS_EXPOSURE',
        'profit_target':      'PROFIT_TARGET_MULTIPLE',
        'trading_mode':       'TRADING_MODE',
        'enable_bil_reserve': 'ENABLE_BIL_RESERVE',
        'setup_complete':     'SETUP_COMPLETE',
        'idle_reserve_pct':   'IDLE_RESERVE_PCT',
        'operating_mode':     'OPERATING_MODE',
        'preset_name':        'PRESET_NAME',
    }
    try:
        db = _customer_db()
        written = []
        for form_key, env_key in customer_keys.items():
            if form_key in data:
                val = data[form_key]
                # MAX_POSITION_PCT: form sends integer percent (10), store as decimal (0.10)
                if form_key == 'max_position_pct':
                    val = str(round(float(val) / 100, 4))
                elif form_key == 'max_trade_usd':
                    val = str(float(val))
                else:
                    val = str(val)
                db.set_setting(env_key, val)
                written.append(env_key)

        if written:
            log.info(f"Customer settings saved: {written}")
            db.log_event("SETTINGS_UPDATED", agent="portal",
                         details=str({k: data[k] for k in data if k in customer_keys}))

        return jsonify({"ok": True, "written": written})
    except Exception as e:
        log.error(f"Settings save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route('/api/customer-settings')
@login_required
def api_customer_settings():
    """Read per-customer settings with global .env fallback."""
    try:
        db = _customer_db()
        customer = db.get_all_settings()

        # Global defaults from .env
        global_defaults = {
            'OPERATING_MODE':     os.environ.get('OPERATING_MODE', 'SUPERVISED'),
            'MIN_CONFIDENCE':     os.environ.get('MIN_CONFIDENCE', 'LOW'),
            'MAX_POSITION_PCT':   os.environ.get('MAX_POSITION_PCT', '0.10'),
            'MAX_TRADE_USD':      os.environ.get('MAX_TRADE_USD', '1000'),
            'MAX_POSITIONS':      os.environ.get('MAX_POSITIONS', '10'),
            'MAX_DAILY_LOSS':     os.environ.get('MAX_DAILY_LOSS', '500'),
            'MAX_SECTOR_PCT':     os.environ.get('MAX_SECTOR_PCT', '25'),
            'CLOSE_SESSION_MODE': os.environ.get('CLOSE_SESSION_MODE', 'aggressive'),
            'MAX_STALENESS':      os.environ.get('MAX_STALENESS', 'Aging'),
            'MAX_DRAWDOWN_PCT':   os.environ.get('MAX_DRAWDOWN_PCT', '15'),
            'MAX_HOLDING_DAYS':   os.environ.get('MAX_HOLDING_DAYS', '15'),
            'MAX_GROSS_EXPOSURE': os.environ.get('MAX_GROSS_EXPOSURE', '80'),
            'PROFIT_TARGET_MULTIPLE': os.environ.get('PROFIT_TARGET_MULTIPLE', '2'),
            'TRADING_MODE':       os.environ.get('TRADING_MODE', 'PAPER'),
            'ENABLE_BIL_RESERVE': os.environ.get('ENABLE_BIL_RESERVE', '1'),
            'SETUP_COMPLETE':     '0',
            'IDLE_RESERVE_PCT':   os.environ.get('IDLE_RESERVE_PCT', '20'),
            'KILL_SWITCH':        '1' if kill_switch_active() else '0',
        }

        # Merge: customer overrides global
        merged = dict(global_defaults)
        merged.update(customer)

        # Convert MAX_POSITION_PCT from decimal to percent for display
        try:
            pct = float(merged.get('MAX_POSITION_PCT', '0.10'))
            if pct < 1:  # stored as decimal
                merged['MAX_POSITION_PCT_DISPLAY'] = str(int(pct * 100))
            else:
                merged['MAX_POSITION_PCT_DISPLAY'] = str(int(pct))
        except (ValueError, TypeError):
            merged['MAX_POSITION_PCT_DISPLAY'] = '10'

        merged['_source'] = {k: 'customer' if k in customer else 'global' for k in merged}
        return jsonify(merged)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/status')
def api_status():
    return jsonify(get_system_status())


@app.route('/api/approvals')
def api_approvals():
    data = load_pending_approvals()
    log.info(f'[DEBUG] api_approvals returning {len(data)} items, customer_id={session.get("customer_id", "MISSING")}')
    return jsonify(data)


@app.route('/api/portfolio-history')
def api_portfolio_history():
    """Portfolio value over time for the graph."""
    days = int(request.args.get('days', 30))
    try:
        db = _customer_db()
        data = db.get_portfolio_history(days=days)
        # If we have less than 2 points, synthesize from current portfolio
        if len(data) < 2:
            p = db.get_portfolio()
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


@app.route('/api/performance-summary')
def api_performance_summary():
    """Closed trade history + computed stats for the Performance tab."""
    try:
        from datetime import datetime
        db     = _customer_db()
        trades = db.get_closed_positions(limit=200)
        port   = db.get_portfolio()

        wins = losses = 0
        total_pnl = 0.0
        hold_hours_list = []
        tax_st = tax_lt = 0.0
        sector_pnl = {}
        rows = []

        for t in trades:
            pnl = t.get('pnl') or 0.0
            total_pnl += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1

            try:
                opened = datetime.fromisoformat(t['opened_at'])
                closed = datetime.fromisoformat(t['closed_at'])
                hrs    = (closed - opened).total_seconds() / 3600
                hold_hours_list.append(hrs)
                hold_label = f"{int(hrs)}h" if hrs < 48 else f"{int(hrs/24)}d"
            except Exception:
                hrs = 0
                hold_label = '--'

            try:
                hold_days = (datetime.fromisoformat(t['closed_at']) - datetime.fromisoformat(t['opened_at'])).days
                if hold_days < 365:
                    tax_st += pnl
                else:
                    tax_lt += pnl
            except Exception:
                tax_st += pnl

            sector = t.get('sector') or 'Other'
            sector_pnl[sector] = round(sector_pnl.get(sector, 0.0) + pnl, 2)

            cost    = (t.get('entry_price') or 0) * (t.get('shares') or 0)
            ret_pct = round(pnl / cost * 100, 2) if cost else 0.0

            rows.append({
                'ticker':      t.get('ticker', '--'),
                'side':        'LONG',
                'entry':       round(t.get('entry_price') or 0, 2),
                'exit':        round(t.get('current_price') or 0, 2),
                'hold':        hold_label,
                'pnl':         round(pnl, 2),
                'ret_pct':     ret_pct,
                'opened_at':   (t.get('opened_at') or '')[:10],
                'closed_at':   (t.get('closed_at') or '')[:10],
                'exit_reason': t.get('exit_reason') or '--',
            })

        total_trades  = wins + losses
        win_rate      = round(wins / total_trades * 100, 1) if total_trades else 0.0
        avg_hold_hrs  = round(sum(hold_hours_list) / len(hold_hours_list), 1) if hold_hours_list else 0.0
        avg_hold_lbl  = (f"{int(avg_hold_hrs)}h" if avg_hold_hrs < 48 else f"{round(avg_hold_hrs/24,1)}d") if avg_hold_hrs else '--'
        month_start   = port.get('month_start') or port.get('cash') or 1
        total_ret_pct = round(total_pnl / month_start * 100, 2) if month_start else 0.0

        return jsonify({
            'total_pnl':      round(total_pnl, 2),
            'total_ret_pct':  total_ret_pct,
            'win_rate':        win_rate,
            'total_trades':    total_trades,
            'winning_trades':  wins,
            'avg_hold':        avg_hold_lbl,
            'tax_st':          round(tax_st, 2),
            'tax_lt':          round(tax_lt, 2),
            'sector_pnl':      sector_pnl,
            'trades':          rows,
        })
    except Exception as e:
        return jsonify({'total_pnl': 0, 'win_rate': 0, 'total_trades': 0,
                        'trades': [], 'error': str(e)})


@login_required
@app.route('/api/watchlist')
def api_watchlist():
    """Signals from shared intelligence — all customers see the same market data."""
    try:
        signals = _shared_db().get_watching_signals(limit=10)
        return jsonify({'signals': signals})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})


@login_required
@app.route('/api/screening')
def api_screening():
    """Latest sector screening — shared across all customers."""
    try:
        candidates = _shared_db().get_latest_screening_run()
        return jsonify({'candidates': candidates})
    except Exception as e:
        return jsonify({'candidates': [], 'error': str(e)})


@login_required
@app.route('/api/market-indices')
def api_market_indices():
    """Fetch intraday quote for SPY (S&P 500), QQQ (Nasdaq), DIA (Dow)."""
    import requests as _req
    alpaca_key, alpaca_secret, _ = _get_customer_alpaca_creds()
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

    alpaca_key, alpaca_secret, _ = _get_customer_alpaca_creds()
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
        db = _customer_db()
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
        db = _customer_db()
        with db.conn() as c:
            pos_rows = c.execute("""
                SELECT ticker, entry_price, opened_at FROM positions
                WHERE opened_at >= ?
                ORDER BY opened_at ASC
            """, (start_dt.strftime('%Y-%m-%d %H:%M:%S'),)).fetchall()
        for row in pos_rows:
            try:
                entry_key = row['opened_at'][:13]
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
        db = _customer_db()
        hb = db.get_last_heartbeat('trade_logic_agent')
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


@app.route('/api/admin/system-metrics')
@admin_required
def api_admin_system_metrics():
    """Live system metrics for admin panel: CPU, RAM, disk, temperature, uptime."""
    import time as _time
    metrics = {
        'cpu_pct':    None,
        'ram':        None,
        'disk':       None,
        'temp_c':     None,
        'uptime':     None,
        'load_avg':   None,
        'customer_count': None,
    }

    try:
        import psutil

        # CPU — 0.5s sample
        metrics['cpu_pct'] = round(psutil.cpu_percent(interval=0.5), 1)

        # RAM
        vm = psutil.virtual_memory()
        metrics['ram'] = {
            'total_mb':  round(vm.total / 1024 / 1024),
            'used_mb':   round(vm.used  / 1024 / 1024),
            'pct':       vm.percent,
        }

        # Disk — measure the synthos_build root
        du = psutil.disk_usage(_ROOT_DIR)
        metrics['disk'] = {
            'total_gb': round(du.total / 1024 / 1024 / 1024, 1),
            'used_gb':  round(du.used  / 1024 / 1024 / 1024, 1),
            'pct':      du.percent,
        }

        # Temperature (Pi-specific via /sys; psutil fallback)
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                metrics['temp_c'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            try:
                temps = psutil.sensors_temperatures()
                if temps:
                    first = next(iter(temps.values()))
                    if first:
                        metrics['temp_c'] = round(first[0].current, 1)
            except Exception:
                metrics['temp_c'] = None

        # Load average (Unix only)
        try:
            la = psutil.getloadavg()
            metrics['load_avg'] = {'1m': round(la[0], 2), '5m': round(la[1], 2), '15m': round(la[2], 2)}
        except AttributeError:
            metrics['load_avg'] = None

    except ImportError:
        pass

    # Uptime via /proc/uptime (Pi) or boot_time (psutil fallback)
    try:
        with open('/proc/uptime') as f:
            secs = float(f.readline().split()[0])
        h, rem = divmod(int(secs), 3600)
        m = rem // 60
        metrics['uptime'] = f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        try:
            import psutil as _ps
            secs = int(_time.time() - _ps.boot_time())
            h, rem = divmod(secs, 3600)
            metrics['uptime'] = f"{h}h {rem//60}m"
        except Exception:
            metrics['uptime'] = 'N/A'

    # Customer count from auth.db
    try:
        metrics['customer_count'] = auth.customer_count()
    except Exception:
        pass

    return jsonify(metrics)


LOGS_CSS = '<style>\n*{box-sizing:border-box;margin:0;padding:0}\nbody{background:#0a0c14;color:#e0ddd8;font-family:sans-serif;min-height:100vh}\nheader{background:#111520;color:#e0ddd8;padding:0 2rem;height:52px;display:flex;\n       align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;\n       border-bottom:1px solid #1e2535}\n.wordmark{font-size:0.95rem;font-weight:600;letter-spacing:0.15em;color:#00f5d4}\n.nav{display:flex;gap:1rem;align-items:center}\n.nav a{color:#556;font-size:0.72rem;text-decoration:none;letter-spacing:0.08em}\n.nav a:hover{color:#aaa}\n.tabs{display:flex;gap:0;border-bottom:1px solid #1e2535;padding:0 2rem;\n      background:#111520;overflow-x:auto;flex-wrap:nowrap}\n.controls{padding:0.75rem 2rem;display:flex;gap:1rem;align-items:center;\n          background:#111520;border-bottom:1px solid #1e2535}\n.controls label{font-size:0.75rem;color:#556;font-weight:600;letter-spacing:0.08em;text-transform:uppercase}\nselect{font-size:0.8rem;padding:0.3rem 0.5rem;background:#161b28;border:1px solid #1e2535;\n       border-radius:6px;color:#e0ddd8}\n.log-box{font-family:monospace;font-size:0.75rem;line-height:1.7;color:#00f5d4;\n         padding:1rem 2rem;white-space:pre-wrap;word-break:break-all;\n         min-height:calc(100vh - 160px)}\n.refresh-btn{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;\n             padding:0.3rem 0.75rem;border:1px solid #1e2535;\n             border-radius:6px;cursor:pointer;background:transparent;color:#556}\n.refresh-btn:hover{background:#1e2535;color:#e0ddd8}\n</style>'

@app.route('/logs')
@admin_required
def logs_page():
    """Tail log files from the browser."""
    log_files = {
        'trader':      'trade_logic_agent.log',
        'scout':       'news_agent.log',
        'pulse':       'market_sentiment_agent.log',
        'portal':      'portal.log',
        'watchdog':    'watchdog.log',
        'boot':        'boot.log',
        'monitor':     'monitor.log',
        'node_health': 'node_health.log',
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
<div class="body-columns">
  <div class="log-col">
    <div class="log-box" id="log-content">{log_content_escaped}</div>
  </div>
  <div class="rss-col">
    <div class="rss-header">
      <span class="rss-title">Live RSS Feed</span>
      <span class="rss-dot"></span>
    </div>
    <div id="rss-stream"><div class="rss-empty">Loading&hellip;</div></div>
  </div>
</div>
<script>
document.getElementById('log-content').scrollIntoView({{block:'end'}});
function rssAge(ts) {{
  if (!ts) return '';
  const d = new Date(ts.replace(' ','T'));
  const s = Math.floor((Date.now() - d)/1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}}
async function loadRss() {{
  try {{
    const r = await fetch('/api/rss-stream');
    if (!r.ok) return;
    const d = await r.json();
    const items = d.items || [];
    const el = document.getElementById('rss-stream');
    if (!items.length) {{
      el.innerHTML = '<div class="rss-empty">No feed data yet &mdash; Scout populates on next run</div>';
      return;
    }}
    el.innerHTML = items.map(a => `
      <div class="rss-item">
        <div class="rss-source">${{a.source || 'RSS'}}</div>
        <div class="rss-headline">${{a.headline}}</div>
        <div class="rss-age">${{rssAge(a.created_at)}}</div>
      </div>`).join('');
  }} catch(e) {{}}
}}
loadRss();
setInterval(loadRss, 90000);
</script>
</body></html>"""
    return html


@app.route('/api/rss-stream')
@login_required
def api_rss_stream():
    """RSS headlines for the logs-page side panel. Reads news_feed source=NEWS,
    returns the 60 most recent items with source label and timestamp."""
    try:
        from retail_database import get_db as _gdb
        db = _gdb()
        with db.conn() as c:
            rows = c.execute("""
                SELECT raw_headline, metadata, created_at FROM news_feed
                WHERE source = 'NEWS'
                ORDER BY created_at DESC LIMIT 60
            """).fetchall()
        items = []
        seen = set()
        for r in rows:
            headline = r['raw_headline'] or ''
            key = headline.lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            meta = {}
            try:
                meta = json.loads(r['metadata'] or '{}')
            except Exception:
                pass
            items.append({
                'headline':   headline,
                'source':     meta.get('feed_name') or meta.get('source') or 'RSS',
                'created_at': r['created_at'],
                'link':       meta.get('link', ''),
            })
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'items': [], 'error': str(e)})


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
        db = _customer_db()
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
    """Recent trader decisions — what Trade Logic has been scanning and acting on."""
    try:
        db = _customer_db()
        with db.conn() as c:
            scans = c.execute("""
                SELECT ticker, cascade_detected, event_summary, tier, scanned_at
                FROM scan_log ORDER BY scanned_at DESC LIMIT 10
            """).fetchall()
            recent = c.execute("""
                SELECT event, agent, details, timestamp
                FROM system_log
                WHERE event IN ('TRADE_DECISION','TRADE_APPROVED','TRADE_PENDING_APPROVAL',
                                'TRADE_REJECTED','BIL_BUY','ORPHAN_POSITION',
                                'AGENT_COMPLETE','AGENT_START')
                ORDER BY
                    CASE WHEN event LIKE 'TRADE%' THEN 0
                         WHEN event IN ('BIL_BUY','ORPHAN_POSITION') THEN 1
                         ELSE 2 END,
                    timestamp DESC
                LIMIT 30
            """).fetchall()
        return jsonify({
            'scans':  [dict(r) for r in scans],
            'recent': [dict(r) for r in recent],
        })
    except Exception as e:
        return jsonify({'scans': [], 'recent': [], 'error': str(e)})









@app.route('/api/test-alpaca-keys')
@login_required
def api_test_alpaca_keys():
    try:
        alpaca_key, alpaca_secret, alpaca_url = _get_customer_alpaca_creds()
        if not alpaca_key or not alpaca_secret:
            return jsonify({'ok': False, 'error': 'No API keys configured. Add them in Settings.'})
        import requests as _req
        r = _req.get(
            f"{alpaca_url}/v2/account",
            headers={
                'APCA-API-KEY-ID': alpaca_key,
                'APCA-API-SECRET-KEY': alpaca_secret,
            },
            timeout=10,
        )
        if r.status_code == 200:
            acct = r.json()
            cash = f"{float(acct.get('cash', 0)):,.2f}"
            return jsonify({
                'ok': True,
                'account_id': acct.get('account_number', acct.get('id', ''))[:12],
                'status': acct.get('status', 'unknown'),
                'cash': cash,
                'paper': 'paper' in alpaca_url.lower(),
            })
        elif r.status_code == 401:
            return jsonify({'ok': False, 'error': 'Invalid API keys'})
        elif r.status_code == 403:
            return jsonify({'ok': False, 'error': 'Keys disabled or restricted'})
        else:
            return jsonify({'ok': False, 'error': f'Alpaca HTTP {r.status_code}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:100]})

@app.route('/api/billing')
@login_required
def api_billing():
    customer_id = session.get('customer_id')
    if not customer_id or customer_id == 'admin':
        return jsonify({'error': 'no customer context'}), 400
    try:
        import auth as _auth
        with _auth._auth_conn() as c:
            row = c.execute(
                "SELECT subscription_status, subscription_ends_at, grace_period_ends_at, "
                "pricing_tier, created_at, stripe_customer_id "
                "FROM customers WHERE id=?", (customer_id,)
            ).fetchone()
        if not row:
            return jsonify({'error': 'customer not found'}), 404
        return jsonify({
            'subscription_status': row['subscription_status'] or 'inactive',
            'subscription_ends_at': row['subscription_ends_at'],
            'grace_period_ends_at': row['grace_period_ends_at'],
            'pricing_tier': row['pricing_tier'] or 'standard',
            'created_at': row['created_at'],
            'stripe_customer_id': row['stripe_customer_id'],
            'has_stripe': bool(row['stripe_customer_id']),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/billing/all-customers')
def api_billing_all_customers():
    token = request.headers.get('X-Service-Token', '')
    svc_token = os.environ.get('PORTAL_SERVICE_TOKEN', '')
    is_service = svc_token and token == svc_token
    if not is_service:
        if not is_authenticated() or not is_admin():
            return jsonify({'error': 'unauthorized'}), 401
    try:
        import auth as _auth
        customers = _auth.list_customers()
        summary = {'active': 0, 'trialing': 0, 'past_due': 0, 'cancelled': 0, 'inactive': 0}
        for c in customers:
            st = c.get('subscription_status', 'inactive') or 'inactive'
            if st in summary:
                summary[st] += 1
            else:
                summary['inactive'] += 1
        return jsonify({
            'customers': [{
                'id': c['id'],
                'name': c.get('display_name', ''),
                'email': c.get('email', ''),
                'subscription_status': c.get('subscription_status', 'inactive') or 'inactive',
                'pricing_tier': c.get('pricing_tier', 'standard'),
                'created_at': c.get('created_at', ''),
                'has_alpaca': c.get('has_alpaca', False),
            } for c in customers],
            'summary': summary,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── SUPPORT TICKET API ────────────────────────────────────────────────────────

@app.route('/api/support/tickets', methods=['GET'])
@login_required
def api_support_tickets():
    status = request.args.get('status')
    category = request.args.get('category')
    db = _customer_db()
    tickets = db.get_tickets(status=status, category=category)
    return jsonify({'tickets': tickets})


@app.route('/api/support/tickets', methods=['POST'])
@login_required
def api_support_create_ticket():
    data = request.get_json(force=True)
    category = data.get('category', 'portal')
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    beta_test_id = data.get('beta_test_id')
    if not subject or not message:
        return jsonify({'error': 'Subject and message are required'}), 400
    db = _customer_db()
    ticket_id = db.create_ticket(category, subject, message, beta_test_id=beta_test_id)
    return jsonify({'ok': True, 'ticket_id': ticket_id})


@app.route('/api/support/tickets/<ticket_id>', methods=['GET'])
@login_required
def api_support_ticket_detail(ticket_id):
    target_customer = request.args.get('customer_id')
    if target_customer and is_admin():
        from retail_database import get_customer_db
        db = get_customer_db(target_customer)
    else:
        db = _customer_db()
    ticket, messages = db.get_ticket_messages(ticket_id)
    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404
    return jsonify({'ticket': ticket, 'messages': messages})


@app.route('/api/support/tickets/<ticket_id>/reply', methods=['POST'])
@login_required
def api_support_ticket_reply(ticket_id):
    data = request.get_json(force=True)
    message = data.get('message', '').strip()
    sender = data.get('sender', 'customer')
    target_customer = data.get('customer_id')
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    # If admin is replying to a specific customer's ticket, use their DB
    if target_customer and is_admin():
        from retail_database import get_customer_db
        db = get_customer_db(target_customer)
    else:
        db = _customer_db()
    db.add_ticket_message(ticket_id, sender, message)
    # If admin replied, send notification to customer
    if sender == 'admin':
        db.add_notification('account', 'Support Reply',
                           f'New reply on ticket {ticket_id}',
                           meta={'ticket_id': ticket_id})
    return jsonify({'ok': True})


@app.route('/api/support/tickets/<ticket_id>/status', methods=['POST'])
@login_required
def api_support_ticket_status(ticket_id):
    data = request.get_json(force=True)
    status = data.get('status', '')
    target_customer = data.get('customer_id')
    if status not in ('open', 'in_progress', 'resolved', 'closed'):
        return jsonify({'error': 'Invalid status'}), 400
    if target_customer and is_admin():
        from retail_database import get_customer_db
        db = get_customer_db(target_customer)
    else:
        db = _customer_db()
    db.update_ticket_status(ticket_id, status)
    if status in ('resolved', 'closed'):
        db.add_notification('account', f'Ticket {status.title()}',
                           f'Your support ticket {ticket_id} has been {status}.',
                           meta={'ticket_id': ticket_id})
    return jsonify({'ok': True})


@app.route('/api/support/beta-response', methods=['POST'])
@login_required
def api_support_beta_response():
    data = request.get_json(force=True)
    beta_test_id = data.get('beta_test_id', '')
    message = data.get('message', '').strip()
    if not beta_test_id or not message:
        return jsonify({'error': 'beta_test_id and message required'}), 400
    db = _customer_db()
    ticket_id = db.create_ticket(
        category='beta_test',
        subject=f'Beta Test Response: {beta_test_id}',
        message=message,
        beta_test_id=beta_test_id
    )
    return jsonify({'ok': True, 'ticket_id': ticket_id})




@app.route('/api/support/direct-message', methods=['POST'])
@login_required
def api_support_direct_message():
    if not is_admin():
        return jsonify({'error': 'admin only'}), 403
    data = request.get_json(force=True)
    customer_id = data.get('customer_id')
    title = data.get('title', '').strip()
    message = data.get('message', '').strip()
    if not customer_id or not title or not message:
        return jsonify({'error': 'customer_id, title, and message required'}), 400
    from retail_database import get_customer_db
    db = get_customer_db(customer_id)
    # Create ticket with admin as first sender
    import secrets
    ticket_id = 'DM-' + secrets.token_hex(4).upper()
    now = db.now()
    with db.conn() as c:
        c.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, category, subject, status, priority, created_at, updated_at) "
            "VALUES (?, 'direct_message', ?, 'open', 'normal', ?, ?)",
            (ticket_id, title, now, now)
        )
        c.execute(
            "INSERT INTO support_messages (ticket_id, sender, message, created_at) "
            "VALUES (?, 'admin', ?, ?)",
            (ticket_id, message, now)
        )
    # Also send notification so they see it in the bell
    db.add_notification('account', title, message[:100] + ('...' if len(message) > 100 else ''),
                        meta={'type': 'direct_message', 'ticket_id': ticket_id})
    return jsonify({'ok': True, 'ticket_id': ticket_id})

@app.route('/api/support/all-tickets', methods=['GET'])
def api_support_all_tickets():
    "Admin endpoint: get tickets across ALL customers."
    token = request.headers.get('X-Service-Token', '')
    svc_token = os.environ.get('PORTAL_SERVICE_TOKEN', '')
    is_service = svc_token and token == svc_token
    if not is_service:
        if not is_authenticated() or not is_admin():
            return jsonify({'error': 'unauthorized'}), 401
    import auth as _auth
    customers = _auth.list_customers()
    from retail_database import get_customer_db
    all_tickets = []
    for cust in customers:
        try:
            cdb = get_customer_db(cust['id'])
            tickets = cdb.get_tickets(
                status=request.args.get('status'),
                category=request.args.get('category')
            )
            for t in tickets:
                t['customer_id'] = cust['id']
                t['customer_name'] = cust.get('display_name', '')
                t['customer_email'] = cust.get('email', '')
            all_tickets.extend(tickets)
        except Exception:
            pass
    all_tickets.sort(key=lambda t: t.get('updated_at', ''), reverse=True)
    return jsonify({'tickets': all_tickets})

@app.route('/api/logs-audit')
def api_logs_audit():
    """
    Scan pi5 log directory for error/warning patterns.
    Returns findings in the same JSON shape as company_auditor's /api/auditor,
    so the monitor auditor page can render both nodes identically.
    Requires monitor bearer token.
    """
    import re as _re
    from datetime import datetime as _dt, timezone as _tz

    auth_header  = request.headers.get('Authorization', '')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    if not monitor_token or auth_header != f'Bearer {monitor_token}':
        return jsonify({'error': 'unauthorized'}), 401

    _log_dir = LOG_DIR  # module-level constant: _ROOT_DIR/logs
    IGNORE = [
        _re.compile(r'connection retry', _re.I),
        _re.compile(r'graceful shutdown', _re.I),
        _re.compile(r'no new issues', _re.I),
        _re.compile(r'Scan complete', _re.I),
        _re.compile(r'critical checks pass', _re.I),
        _re.compile(r'All \w+ checks', _re.I),
    ]
    # Match log-level tokens: ] LEVEL or line-start LEVEL (not mid-sentence words)
    PATTERNS = [
        (_re.compile(r'] CRITICAL\b|^CRITICAL\b'), 'critical'),
        (_re.compile(r'] ERROR\b|^ERROR\b'),       'high'),
        (_re.compile(r'Traceback'),                  'high'),
        (_re.compile(r'Exception:'),                 'high'),
        (_re.compile(r'] WARNING\b|^WARNING\b'),   'medium'),
        (_re.compile(r'] \w+ failed\b', _re.I),   'medium'),
        (_re.compile(r'\btimeout\b', _re.I),       'low'),
    ]

    issues   = []
    by_sev   = {}
    scan_state = []
    seen     = {}   # (source_file, context[:80]) → issue dict  (dedup)

    try:
        import glob as _glob, os as _os
        log_files = sorted(_glob.glob(_os.path.join(_log_dir, '*.log')))
    except Exception:
        log_files = []

    now_iso = _dt.now(_tz.utc).isoformat()

    for log_path in log_files:
        fname = _os.path.basename(log_path)
        try:
            size = _os.path.getsize(log_path)
            with open(log_path, 'r', errors='replace') as fh:
                lines = fh.readlines()
            offset = size

            for line in lines:
                line = line.rstrip()
                if not line:
                    continue
                if any(p.search(line) for p in IGNORE):
                    continue
                for pat, sev in PATTERNS:
                    if pat.search(line):
                        ctx = line[:120]
                        key = (fname, ctx[:80])
                        if key in seen:
                            seen[key]['hit_count'] += 1
                            seen[key]['last_seen']  = now_iso
                        else:
                            entry = {
                                'id':          len(issues) + 1,
                                'source_file': fname,
                                'severity':    sev,
                                'context':     ctx,
                                'hit_count':   1,
                                'first_seen':  now_iso,
                                'last_seen':   now_iso,
                            }
                            seen[key] = entry
                            issues.append(entry)
                            by_sev[sev] = by_sev.get(sev, 0) + 1
                        break

            scan_state.append({
                'log_file':     log_path,
                'last_offset':  offset,
                'file_size':    size,
                'last_scanned': now_iso,
            })
        except Exception:
            pass

    # Sort by severity
    sev_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    issues.sort(key=lambda x: sev_order.get(x['severity'], 9))
    issues = issues[:200]

    return jsonify({
        'issues':           issues,
        'by_severity':      by_sev,
        'total_unresolved': len(issues),
        'scan_state':       scan_state,
        'morning_report':   None,   # retail node has no morning report
        'node':             'retail',
    })

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
@admin_required
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
    Admin only — regular customers cannot upload code to the Pi.
    """
    if not is_admin():
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    import threading

    # Uploads are written to a staging directory, not directly to PROJECT_DIR.
    # This prevents a compromised customer session from overwriting live agent code.
    STAGING_DIR = os.path.join(os.path.dirname(PROJECT_DIR), 'upload_staging')
    os.makedirs(STAGING_DIR, exist_ok=True)

    uploaded      = []
    errors        = []
    restart_portal = False

    for file in request.files.getlist('files'):
        fname = file.filename
        if not fname:
            continue

        fname = os.path.basename(fname)
        ext   = os.path.splitext(fname)[1].lower()
        if ext not in ('.py', '.sh', '.md', '.html', '.txt', '.json'):
            errors.append(f"{fname}: file type not allowed")
            continue

        # Write to staging, not live PROJECT_DIR
        dest = os.path.join(STAGING_DIR, fname)
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
          letter-spacing:0.15em;color:#00f5d4;text-shadow:0 0 20px rgba(0,245,212,0.22)}
.nav a{color:rgba(255,255,255,0.35);font-size:11px;text-decoration:none;margin-left:auto;
       padding:5px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.07)}
.nav a:hover{color:rgba(255,255,255,0.8);background:rgba(255,255,255,0.05)}
.page{max-width:900px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}
.title span{background:linear-gradient(90deg,#00f5d4,#7b61ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{font-size:12px;color:rgba(255,255,255,0.35);margin-bottom:24px}

/* DROP ZONE */
.drop-zone{
  border:2px dashed rgba(0,245,212,0.18);border-radius:20px;
  background:rgba(0,245,212,0.03);
  padding:40px 24px;text-align:center;margin-bottom:24px;
  cursor:pointer;transition:all 0.2s;position:relative;
}
.drop-zone.drag-over{
  border-color:rgba(0,245,212,0.8);background:rgba(0,245,212,0.08);
  box-shadow:0 0 30px rgba(0,245,212,0.09);
}
.drop-icon{font-size:36px;margin-bottom:12px}
.drop-title{font-size:15px;font-weight:600;color:rgba(255,255,255,0.8);margin-bottom:6px}
.drop-sub{font-size:12px;color:rgba(255,255,255,0.35);line-height:1.6}
.drop-btn{
  display:inline-block;margin-top:14px;padding:9px 22px;border-radius:10px;
  background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.18);
  color:#00f5d4;font-size:12px;font-weight:600;cursor:pointer;
  font-family:'Inter',sans-serif;transition:all 0.15s;
}
.drop-btn:hover{background:rgba(0,245,212,0.12)}
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
.rb-ok{background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.14);color:#00f5d4}
.rb-err{background:rgba(255,75,110,0.08);border:1px solid rgba(255,75,110,0.14);color:#ff4b6e}

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
          background:rgba(0,245,212,0.08);border:1px solid rgba(0,245,212,0.12);
          color:rgba(0,245,212,0.7);margin-left:4px}
.fsize{font-size:10px;color:rgba(255,255,255,0.25);font-family:'JetBrains Mono',monospace;
       padding:9px 14px;border-bottom:1px solid rgba(255,255,255,0.04)}
.fmtime{font-size:10px;color:rgba(255,255,255,0.2);font-family:'JetBrains Mono',monospace;
        padding:9px 14px;border-bottom:1px solid rgba(255,255,255,0.04)}
.restart-note{margin-top:12px;padding:10px 14px;border-radius:10px;font-size:11px;
              background:rgba(245,166,35,0.06);border:1px solid rgba(245,166,35,0.2);
              color:rgba(245,166,35,0.8)}
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
    """Fetch recent news feed entries from the shared intelligence database."""
    try:
        return _shared_db().get_news_feed(limit=limit)
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
<div class="subtitle">All signals evaluated by the News agent — including WATCH and DISCARD decisions. Auto-refreshes every 60s.</div>
<div class="refresh-note">Showing last {count} entries &middot; last updated {updated}</div>
<div class="table-wrap">
{table_content}
</div>
</body></html>"""


@app.route('/news')
@login_required
def news_feed_page():
    """News feed — all signals evaluated by the News agent (QUEUE, WATCH, DISCARD)."""
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
    """Display-only news headlines — shared across all customers."""
    category = request.args.get('category')
    if category == 'all':
        category = None
    db = _shared_db()
    articles = db.get_news_headlines(category=category, limit=100, min_floor=30)
    return jsonify({'articles': articles, 'count': len(articles)})


@app.route('/api/news-feed')
@login_required
def api_news_feed():
    """JSON endpoint — News agent writes here; also readable by company Pi."""
    rows = get_news_feed_data(limit=100)
    return jsonify({'entries': rows, 'count': len(rows)})


# ── ADMIN PORTAL ───────────────────────────────────────────────────────────

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  /* ── Surfaces ── */
  --bg:      #0a0c14;
  --surface: #111520;
  --surface2:#161b28;
  --surface3:#1c2235;

  /* ── Borders ── */
  --border:  rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.13);
  --border3: rgba(255,255,255,0.20);

  /* ── Text ── */
  --text:    rgba(255,255,255,0.88);
  --muted:   rgba(255,255,255,0.40);
  --dim:     rgba(255,255,255,0.18);

  /* ── Existing color names (preserved for compatibility) ── */
  --teal:    #00f5d4;
  --teal2:   rgba(0,245,212,0.08);
  --pink:    #ff4b6e;
  --pink2:   rgba(255,75,110,0.08);
  --purple:  #7b61ff;
  --purple2: rgba(123,97,255,0.08);
  --amber:   #f5a623;
  --amber2:  rgba(245,166,35,0.08);
  --green:   #22c55e;

  /* ── Semantic aliases (new — use for all new code) ── */
  --cyan:        #00f5d4;
  --cyan-dim:    rgba(0,245,212,0.08);
  --cyan-mid:    rgba(0,245,212,0.09);
  --cyan-glow:   rgba(0,245,212,0.22);
  --violet:      #7b61ff;
  --violet-dim:  rgba(123,97,255,0.08);
  --violet-mid:  rgba(123,97,255,0.09);
  --violet-glow: rgba(123,97,255,0.18);
  --signal:      #f5a623;
  --signal-dim:  rgba(245,166,35,0.08);
  --signal-mid:  rgba(245,166,35,0.15);
  --signal-glow: rgba(245,166,35,0.18);
  --red:         #ff4b6e;
  --red-dim:     rgba(255,75,110,0.08);
  --red-mid:     rgba(255,75,110,0.09);

  /* ── Glow scale (use these, not ad-hoc box-shadows) ── */
  --glow-hero:   0 0 20px rgba(0,245,212,0.22);
  --glow-active: 0 0 8px  rgba(0,245,212,0.12);
  --glow-dot:    0 0 5px  currentColor;

  /* ── Shadow scale ── */
  --shadow-card:  0 4px 16px rgba(0,0,0,0.3);
  --shadow-modal: 0 24px 80px rgba(0,0,0,0.6);

  /* ── Typography scale ── */
  --text-hero: 28px;
  --text-xl:   20px;
  --text-lg:   15px;
  --text-base: 13px;
  --text-sm:   11px;
  --text-xs:   10px;
  --text-xxs:  9px;

  /* ── Fonts ── */
  --sans: 'Inter',system-ui,sans-serif;
  --mono: 'JetBrains Mono',monospace;

  /* ── Motion ── */
  --pulse-live:  2s;
  --pulse-alert: 3s;
  --transition:  0.15s;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.15);border-radius:99px}

/* HEADER */
.header{position:sticky;top:0;z-index:100;background:rgba(10,12,20,0.9);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;gap:16px}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;color:var(--teal);
  text-shadow:0 0 20px rgba(0,245,212,0.22);flex-shrink:0}
.admin-badge{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:0.12em;
  padding:2px 8px;border-radius:99px;background:rgba(123,97,255,0.09);
  border:1px solid rgba(123,97,255,0.35);color:#7b61ff;text-transform:uppercase}
.header-nav{display:flex;align-items:center;gap:4px;margin-left:auto}
.nav-btn{padding:5px 12px;border-radius:8px;font-size:11px;font-weight:600;letter-spacing:0.04em;
  cursor:pointer;background:transparent;border:1px solid var(--border);color:var(--muted);
  font-family:var(--sans);transition:all 0.15s}
.nav-btn:hover{background:var(--surface2);color:var(--text);border-color:var(--border2)}
.nav-btn.active{background:var(--teal2);border-color:rgba(0,245,212,0.18);color:var(--teal)}
.nav-btn.danger{border-color:rgba(255,75,110,0.18);color:var(--pink)}
.nav-btn.danger:hover{background:var(--pink2)}
.nav-btn.danger.engaged{background:var(--pink2);border-color:rgba(255,75,110,0.14);color:var(--pink)}

/* LAYOUT */
.page{max-width:1280px;margin:0 auto;padding:20px 24px}
.section-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.glass{border-radius:16px;border:1px solid var(--border);background:var(--surface);position:relative;overflow:hidden}
.glass::before{content:'';position:absolute;top:0;left:20%;right:20%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.06),transparent)}

/* METRIC CARDS */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.metric-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;position:relative}
.metric-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.metric-val{font-family:var(--mono);font-size:1.5rem;font-weight:600;color:var(--text);margin-bottom:4px}
.metric-sub{font-size:11px;color:var(--muted)}
.metric-bar{height:4px;border-radius:99px;background:rgba(255,255,255,0.07);margin-top:8px;overflow:hidden}
.metric-bar-fill{height:100%;border-radius:99px;transition:width 0.4s}
.bar-ok{background:var(--teal)}
.bar-warn{background:var(--amber)}
.bar-err{background:var(--pink)}
.temp-ok{color:var(--teal)}
.temp-warn{color:var(--amber)}
.temp-err{color:var(--pink)}

/* TABLE */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{border-bottom:1px solid var(--border2)}
th{padding:8px 12px;text-align:left;font-size:10px;font-weight:700;letter-spacing:0.08em;
  text-transform:uppercase;color:var(--muted);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,0.02)}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;font-weight:700;
  letter-spacing:0.05em;text-transform:uppercase}
.badge-managed{background:rgba(0,245,212,0.1);color:var(--teal);border:1px solid rgba(0,245,212,0.12)}
.badge-auto{background:var(--amber2);color:var(--amber);border:1px solid rgba(245,166,35,0.25)}
.badge-admin{background:rgba(123,97,255,0.12);color:#7b61ff;border:1px solid rgba(123,97,255,0.14)}
.badge-ok{background:rgba(34,197,94,0.1);color:#22c55e;border:1px solid rgba(34,197,94,0.2)}
.badge-warn{background:rgba(245,158,11,0.12);color:var(--amber,#f59e0b);border:1px solid rgba(245,158,11,0.25)}
.badge-off{background:rgba(255,255,255,0.04);color:var(--muted);border:1px solid var(--border)}
.action-btn{padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;
  background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);
  transition:all 0.15s;letter-spacing:0.03em;margin-right:4px}
.action-btn:hover{background:var(--surface2);color:var(--text);border-color:var(--border2)}
.action-btn.danger{border-color:rgba(255,75,110,0.18);color:var(--pink)}
.action-btn.danger:hover{background:var(--pink2)}

/* MODAL */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);
  z-index:200;display:none;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border2);border-radius:16px;
  padding:24px;width:100%;max-width:440px;position:relative}
.modal h3{font-size:14px;font-weight:700;margin-bottom:16px;color:var(--text)}
.modal label{font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;
  color:var(--muted);display:block;margin-bottom:4px;margin-top:12px}
.modal label:first-of-type{margin-top:0}
.modal input{font-family:var(--mono);font-size:13px;padding:8px 12px;border:1px solid var(--border2);
  border-radius:8px;background:var(--surface2);color:var(--text);width:100%}
.modal input:focus{outline:none;border-color:var(--teal)}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.btn-primary{padding:8px 20px;background:var(--teal);color:#0a0c14;border:none;border-radius:8px;
  font-size:12px;font-weight:700;letter-spacing:0.08em;cursor:pointer;font-family:var(--sans)}
.btn-primary:hover{background:#00d4b8}
.btn-cancel{padding:8px 16px;background:transparent;color:var(--muted);border:1px solid var(--border);
  border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--sans)}
.btn-cancel:hover{background:var(--surface2);color:var(--text)}

/* KILL BAR */
.kill-bar{background:rgba(255,75,110,0.08);border-bottom:1px solid rgba(255,75,110,0.12);
  padding:8px 24px;display:flex;align-items:center;justify-content:space-between}
.kill-bar.clear{display:none}

/* TABS */
.tab{display:none}.tab.active{display:block}
.toast{position:fixed;bottom:24px;right:24px;padding:10px 18px;border-radius:10px;font-size:13px;
  font-weight:600;z-index:999;opacity:0;transition:opacity 0.2s;pointer-events:none}
.toast.show{opacity:1}
.toast.ok{background:rgba(0,245,212,0.09);border:1px solid rgba(0,245,212,0.18);color:var(--teal)}
.toast.err{background:var(--pink2);border:1px solid rgba(255,75,110,0.18);color:var(--pink)}
</style>
</head>
<body>

<div id="toast" class="toast"></div>

<header class="header">
  <div class="wordmark">SYNTHOS</div>
  <div class="admin-badge">Admin</div>
  <div class="header-nav">
    <button class="nav-btn active" onclick="showTab('system')" id="tab-system-btn">System</button>
    <button class="nav-btn" onclick="showTab('scheduler')" id="tab-scheduler-btn">Scheduler</button>
    <button class="nav-btn" onclick="showTab('customers')" id="tab-customers-btn">Customers</button>
    <button class="nav-btn" onclick="window.location='/logs'">Logs</button>
    <button class="nav-btn" onclick="window.location='/files'">Files</button>
    <button class="nav-btn danger {% if kill_active %}engaged{% endif %}" id="kill-nav-btn" onclick="toggleKill()">
      {% if kill_active %}⛔ Halted{% else %}Kill Switch{% endif %}
    </button>
    <a href="/logout" class="nav-btn" style="text-decoration:none;font-size:11px">Sign Out</a>
  </div>
</header>

<div class="kill-bar {% if not kill_active %}clear{% endif %}" id="kill-bar">
  <span style="font-size:12px;font-weight:600;color:var(--pink)">⛔ System Halted — all agent trading suspended</span>
  <button class="action-btn" onclick="toggleKill()">Resume System</button>
</div>

<!-- ── SYSTEM TAB ── -->
<div class="page tab active" id="tab-system">

  <div class="section-title">Hardware</div>
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label">CPU</div>
      <div class="metric-val" id="m-cpu">—</div>
      <div class="metric-sub" id="m-load">Load avg —</div>
      <div class="metric-bar"><div class="metric-bar-fill bar-ok" id="m-cpu-bar" style="width:0%"></div></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Memory</div>
      <div class="metric-val" id="m-ram">—</div>
      <div class="metric-sub" id="m-ram-sub">— MB used</div>
      <div class="metric-bar"><div class="metric-bar-fill bar-ok" id="m-ram-bar" style="width:0%"></div></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Storage</div>
      <div class="metric-val" id="m-disk">—</div>
      <div class="metric-sub" id="m-disk-sub">— GB used</div>
      <div class="metric-bar"><div class="metric-bar-fill bar-ok" id="m-disk-bar" style="width:0%"></div></div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Temperature</div>
      <div class="metric-val temp-ok" id="m-temp">—</div>
      <div class="metric-sub">Processor °C</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Uptime</div>
      <div class="metric-val" id="m-uptime" style="font-size:1.1rem">—</div>
      <div class="metric-sub">Since last boot</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Customers</div>
      <div class="metric-val" id="m-customers">{{ customer_count }}</div>
      <div class="metric-sub">Active accounts</div>
    </div>
  </div>

  <div class="section-title">Processes</div>
  <div class="glass" style="padding:0">
    <table>
      <thead><tr>
        <th>Process</th><th>Status</th><th>PID</th><th>CPU %</th><th>RAM MB</th><th>Uptime</th>
      </tr></thead>
      <tbody id="proc-table"><tr><td colspan="6" style="color:var(--muted);padding:16px">Loading...</td></tr></tbody>
    </table>
  </div>

</div>

<!-- ── SCHEDULER TAB ── -->
<div class="page tab" id="tab-scheduler">

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
    <div class="section-title" style="margin-bottom:0;flex:1">Recent Session Runs</div>
    <button class="nav-btn" style="margin-left:16px" onclick="loadScheduler()">↻ Refresh</button>
  </div>

  <div class="glass" style="padding:0">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Session</th><th>Started</th><th>Duration</th><th>Status</th>
          <th>Pipeline</th><th>Customers OK</th><th>Failures</th>
        </tr></thead>
        <tbody id="scheduler-table"><tr><td colspan="7" style="color:var(--muted);padding:16px">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <div style="margin-top:20px">
    <div class="section-title">Per-Step Breakdown</div>
    <div id="scheduler-detail" style="color:var(--muted);font-size:13px;padding:8px 0">
      Click a row above to see step details.
    </div>
  </div>

</div>

<!-- ── CUSTOMERS TAB ── -->
<div class="page tab" id="tab-customers">

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
    <div class="section-title" style="margin-bottom:0;flex:1">Customer Accounts</div>
    <button class="nav-btn active" style="margin-left:16px" onclick="openCreateModal()">+ New Customer</button>
  </div>

  <div class="glass" style="padding:0">
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Name</th><th>Email</th><th>Mode</th><th>Role</th>
          <th>Alpaca</th><th>Verified</th><th>Subscription</th><th>Tier</th>
          <th>Last Login</th><th>Status</th><th>Actions</th>
        </tr></thead>
        <tbody id="customer-table"><tr><td colspan="11" style="color:var(--muted);padding:16px">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

</div>

<!-- ── CREATE CUSTOMER MODAL ── -->
<div class="modal-overlay" id="create-modal">
  <div class="modal">
    <h3>New Customer Account</h3>
    <label>Display Name</label>
    <input type="text" id="new-name" placeholder="e.g. Jane Smith">
    <label>Email</label>
    <input type="email" id="new-email" placeholder="customer@example.com">
    <label>Password</label>
    <input type="password" id="new-password" placeholder="Strong password">
    <label>Alpaca API Key <span style="font-weight:400;color:var(--muted)">(optional — set later)</span></label>
    <input type="text" id="new-alpaca-key" placeholder="PK...">
    <label>Alpaca Secret Key</label>
    <input type="password" id="new-alpaca-secret" placeholder="...">
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeCreateModal()">Cancel</button>
      <button class="btn-primary" onclick="createCustomer()">Create Account</button>
    </div>
  </div>
</div>

<!-- ── ALPACA MODAL ── -->
<div class="modal-overlay" id="alpaca-modal">
  <div class="modal">
    <h3 id="alpaca-modal-title">Update Alpaca Credentials</h3>
    <input type="hidden" id="alpaca-customer-id">
    <label>Alpaca API Key</label>
    <input type="text" id="edit-alpaca-key" placeholder="PK...">
    <label>Alpaca Secret Key</label>
    <input type="password" id="edit-alpaca-secret" placeholder="...">
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeAlpacaModal()">Cancel</button>
      <button class="btn-primary" onclick="saveAlpaca()">Save</button>
    </div>
  </div>
</div>

<script>
// ── TABS ──
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('[id^="tab-"][id$="-btn"]').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  const btn = document.getElementById('tab-' + name + '-btn');
  if (btn) btn.classList.add('active');
  if (name === 'customers')  loadCustomers();
  if (name === 'scheduler')  loadScheduler();
}

// ── TOAST ──
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 2800);
}

// ── KILL SWITCH ──
let killState = {{ 'true' if kill_active else 'false' }};
async function toggleKill() {
  const engage = !killState;
  const r = await fetch('/api/kill-switch', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({engage})});
  const d = await r.json();
  if (d.ok) {
    killState = engage;
    document.getElementById('kill-bar').className = 'kill-bar' + (engage ? '' : ' clear');
    const nb = document.getElementById('kill-nav-btn');
    nb.textContent = engage ? '⛔ Halted' : 'Kill Switch';
    nb.className = 'nav-btn danger' + (engage ? ' engaged' : '');
    toast(engage ? '⛔ System halted' : '✓ System resumed', engage ? 'err' : 'ok');
  }
}

// ── SYSTEM METRICS ──
function barClass(pct) { return pct >= 85 ? 'bar-err' : pct >= 65 ? 'bar-warn' : 'bar-ok'; }
function tempClass(t)  { return t >= 75 ? 'temp-err' : t >= 60 ? 'temp-warn' : 'temp-ok'; }

async function loadMetrics() {
  try {
    const d = await fetch('/api/admin/system-metrics').then(r => r.json());

    if (d.cpu_pct !== null) {
      document.getElementById('m-cpu').textContent = d.cpu_pct + '%';
      const bar = document.getElementById('m-cpu-bar');
      bar.style.width = d.cpu_pct + '%';
      bar.className = 'metric-bar-fill ' + barClass(d.cpu_pct);
    }
    if (d.load_avg) {
      document.getElementById('m-load').textContent = `Load ${d.load_avg['1m']} / ${d.load_avg['5m']} / ${d.load_avg['15m']}`;
    }
    if (d.ram) {
      document.getElementById('m-ram').textContent = d.ram.pct + '%';
      document.getElementById('m-ram-sub').textContent = `${d.ram.used_mb} / ${d.ram.total_mb} MB`;
      const bar = document.getElementById('m-ram-bar');
      bar.style.width = d.ram.pct + '%';
      bar.className = 'metric-bar-fill ' + barClass(d.ram.pct);
    }
    if (d.disk) {
      document.getElementById('m-disk').textContent = d.disk.pct + '%';
      document.getElementById('m-disk-sub').textContent = `${d.disk.used_gb} / ${d.disk.total_gb} GB`;
      const bar = document.getElementById('m-disk-bar');
      bar.style.width = d.disk.pct + '%';
      bar.className = 'metric-bar-fill ' + barClass(d.disk.pct);
    }
    if (d.temp_c !== null && d.temp_c !== undefined) {
      const el = document.getElementById('m-temp');
      el.textContent = d.temp_c + '°C';
      el.className = 'metric-val ' + tempClass(d.temp_c);
    }
    if (d.uptime) document.getElementById('m-uptime').textContent = d.uptime;
    if (d.customer_count !== null) document.getElementById('m-customers').textContent = d.customer_count;

    loadProcesses();
  } catch(e) { console.error('metrics error', e); }
}

async function loadProcesses() {
  try {
    const d = await fetch('/api/admin/processes').then(r => r.json());
    const tbody = document.getElementById('proc-table');
    if (!d.processes || !d.processes.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted);padding:16px">No monitored processes found</td></tr>';
      return;
    }
    tbody.innerHTML = d.processes.map(p => `
      <tr>
        <td style="font-family:var(--mono);font-size:12px">${p.name}</td>
        <td><span class="badge ${p.running ? 'badge-ok' : 'badge-off'}">${p.running ? 'Running' : 'Stopped'}</span></td>
        <td style="font-family:var(--mono);color:var(--muted)">${p.pid || '—'}</td>
        <td style="font-family:var(--mono)">${p.cpu_pct !== null ? p.cpu_pct + '%' : '—'}</td>
        <td style="font-family:var(--mono)">${p.ram_mb !== null ? p.ram_mb + ' MB' : '—'}</td>
        <td style="color:var(--muted);font-size:12px">${p.uptime || '—'}</td>
      </tr>`).join('');
  } catch(e) {}
}

// ── CUSTOMERS ──
async function loadCustomers() {
  try {
    const d = await fetch('/api/admin/customers').then(r => r.json());
    const tbody = document.getElementById('customer-table');
    if (!d.customers || !d.customers.length) {
      tbody.innerHTML = '<tr><td colspan="11" style="color:var(--muted);padding:16px">No customers yet</td></tr>';
      return;
    }
    tbody.innerHTML = d.customers.map(c => {
      const subStatus = c.subscription_status || 'inactive';
      const subBadge  = subStatus === 'active'    ? 'badge-ok'
                      : subStatus === 'past_due'  ? 'badge-warn'
                      : subStatus === 'trialing'  ? 'badge-managed'
                      : 'badge-off';
      const tierLabel = c.pricing_tier === 'early_adopter' ? '🌟 Early' : (c.pricing_tier || 'standard');
      return `<tr>
        <td style="font-weight:600">${esc(c.display_name || '—')}</td>
        <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${esc(c.email)}</td>
        <td><span class="badge ${c.operating_mode === 'AUTOMATIC' ? 'badge-auto' : 'badge-managed'}">${c.operating_mode}</span></td>
        <td><span class="badge ${c.role === 'admin' ? 'badge-admin' : 'badge-off'}">${c.role}</span></td>
        <td><span class="badge ${c.has_alpaca ? 'badge-ok' : 'badge-off'}">${c.has_alpaca ? '✓' : 'Not Set'}</span></td>
        <td><span class="badge ${c.email_verified ? 'badge-ok' : 'badge-off'}">${c.email_verified ? '✓' : 'Pending'}</span></td>
        <td><span class="badge ${subBadge}">${subStatus}</span></td>
        <td style="font-size:12px;color:var(--muted)">${tierLabel}</td>
        <td style="color:var(--muted);font-size:12px">${c.last_login ? c.last_login.substring(0,16).replace('T',' ') : 'Never'}</td>
        <td><span class="badge ${c.is_active ? 'badge-ok' : 'badge-off'}">${c.is_active ? 'Active' : 'Inactive'}</span></td>
        <td>
          <button class="action-btn" onclick="openAlpacaModal('${c.id}','${esc(c.display_name)}')">Alpaca</button>
          ${c.is_active && c.role !== 'admin' ? `<button class="action-btn danger" onclick="deactivateCustomer('${c.id}','${esc(c.display_name)}')">Deactivate</button>` : ''}
        </td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('customers error', e); }
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ── CREATE CUSTOMER ──
function openCreateModal()  { document.getElementById('create-modal').classList.add('open'); document.getElementById('new-name').focus(); }
function closeCreateModal() { document.getElementById('create-modal').classList.remove('open'); }

async function createCustomer() {
  const name    = document.getElementById('new-name').value.trim();
  const email   = document.getElementById('new-email').value.trim();
  const pass    = document.getElementById('new-password').value;
  const akey    = document.getElementById('new-alpaca-key').value.trim();
  const asecret = document.getElementById('new-alpaca-secret').value.trim();

  if (!email || !pass) { toast('Email and password required', 'err'); return; }

  const r = await fetch('/api/admin/customers', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email, password:pass, display_name:name, alpaca_key:akey, alpaca_secret:asecret})
  });
  const d = await r.json();
  if (d.ok) {
    closeCreateModal();
    ['new-name','new-email','new-password','new-alpaca-key','new-alpaca-secret'].forEach(id => document.getElementById(id).value='');
    toast('✓ Customer created');
    loadCustomers();
    document.getElementById('m-customers').textContent = parseInt(document.getElementById('m-customers').textContent||0) + 1;
  } else {
    toast(d.error || 'Failed to create customer', 'err');
  }
}

// ── ALPACA ──
function openAlpacaModal(id, name) {
  document.getElementById('alpaca-customer-id').value = id;
  document.getElementById('alpaca-modal-title').textContent = 'Alpaca — ' + name;
  document.getElementById('edit-alpaca-key').value = '';
  document.getElementById('edit-alpaca-secret').value = '';
  document.getElementById('alpaca-modal').classList.add('open');
  document.getElementById('edit-alpaca-key').focus();
}
function closeAlpacaModal() { document.getElementById('alpaca-modal').classList.remove('open'); }

async function saveAlpaca() {
  const id     = document.getElementById('alpaca-customer-id').value;
  const key    = document.getElementById('edit-alpaca-key').value.trim();
  const secret = document.getElementById('edit-alpaca-secret').value.trim();
  if (!key || !secret) { toast('Both fields required', 'err'); return; }
  const r = await fetch(`/api/admin/customers/${id}/alpaca`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({alpaca_key:key, alpaca_secret:secret})
  });
  const d = await r.json();
  if (d.ok) { closeAlpacaModal(); toast('✓ Alpaca credentials saved'); loadCustomers(); }
  else       { toast(d.error || 'Failed to save', 'err'); }
}

// ── DEACTIVATE ──
async function deactivateCustomer(id, name) {
  if (!confirm(`Deactivate ${name}? They will no longer be able to log in.`)) return;
  const r = await fetch(`/api/admin/customers/${id}/deactivate`, {method:'POST'});
  const d = await r.json();
  if (d.ok) { toast('Customer deactivated'); loadCustomers(); }
  else       { toast(d.error || 'Failed', 'err'); }
}

// ── SCHEDULER HISTORY ──
let _schedHistory = [];

async function loadScheduler() {
  const tbody = document.getElementById('scheduler-table');
  tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);padding:16px">Loading...</td></tr>';
  try {
    const d = await fetch('/api/admin/scheduler-history').then(r => r.json());
    _schedHistory = d.history || [];
    if (!_schedHistory.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);padding:16px">No runs recorded yet — scheduler hasn\'t fired</td></tr>';
      return;
    }
    tbody.innerHTML = _schedHistory.map((run, i) => {
      const start   = run.started_at.substring(0,16).replace('T',' ');
      const s_ms    = new Date(run.started_at).getTime();
      const e_ms    = new Date(run.finished_at).getTime();
      const dur     = isNaN(s_ms)||isNaN(e_ms) ? '—' : ((e_ms - s_ms)/1000).toFixed(1) + 's';
      const steps   = Object.values(run.steps || {});
      const pipeline = Object.keys(run.steps || {}).map(s=>s.replace('retail_','').replace('_agent.py','').replace('.py','')).join(' → ');
      const totalOk  = steps.reduce((a,s)=>a+(s.ok||0),0);
      const totalFail= steps.reduce((a,s)=>a+(s.failed||0)+(s.timeout||0),0);
      const badge    = run.status === 'ok'
        ? '<span class="badge badge-ok">OK</span>'
        : '<span class="badge badge-auto">Partial</span>';
      return `<tr style="cursor:pointer" onclick="showSchedulerDetail(${i})">
        <td style="font-family:var(--mono);font-size:12px;font-weight:600">${run.session}</td>
        <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${start}</td>
        <td style="font-family:var(--mono);font-size:12px">${dur}</td>
        <td>${badge}</td>
        <td style="font-size:12px;color:var(--muted)">${pipeline}</td>
        <td style="font-family:var(--mono);color:var(--green)">${totalOk}</td>
        <td style="font-family:var(--mono);color:${totalFail?'var(--pink)':'var(--muted)'}">${totalFail||'—'}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="7" style="color:var(--pink);padding:16px">Error loading history: ${e}</td></tr>`;
  }
}

function showSchedulerDetail(idx) {
  const run  = _schedHistory[idx];
  const el   = document.getElementById('scheduler-detail');
  if (!run || !run.steps) { el.innerHTML = 'No step data.'; return; }
  const rows = Object.entries(run.steps).map(([script, step]) => {
    const name    = script.replace('retail_','').replace('_agent.py','').replace('.py','');
    const cust    = step.customers || {};
    const custRows = Object.entries(cust).map(([cid, outcome]) =>
      `<span style="font-family:var(--mono);font-size:11px;margin-right:8px;color:${
        outcome==='ok'?'var(--teal)':outcome==='failed'?'var(--pink)':'var(--amber)'
      }">${cid.substring(0,8)} ${outcome}</span>`
    ).join('');
    return `<div style="margin-bottom:12px;padding:10px 14px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
      <div style="font-weight:600;margin-bottom:6px">${name}
        <span style="font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:8px">
          ${step.ok||0} ok · ${(step.failed||0)+(step.timeout||0)} fail
        </span>
      </div>
      <div>${custRows || '<span style="color:var(--muted);font-size:11px">No per-customer data</span>'}</div>
    </div>`;
  }).join('');
  const start = run.started_at.substring(0,16).replace('T',' ');
  el.innerHTML = `<div style="margin-bottom:8px;font-size:12px;color:var(--muted)">
    Session <strong style="color:var(--text)">${run.session}</strong> · ${start}
  </div>${rows}`;
}

// ── INIT ──
loadMetrics();
setInterval(loadMetrics, 12000);
</script>
</body>
</html>"""


@app.route('/admin')
@login_required
def admin_portal():
    """Archived admin dashboard — redirects to customer portal."""
    return redirect('/')


@app.route('/api/admin/customers')
@admin_required
def api_admin_customers():
    """List all customer accounts (decrypted display info, no secrets)."""
    try:
        return jsonify({'customers': auth.list_customers()})
    except Exception as e:
        log.error(f"admin customers list error: {e}")
        return jsonify({'customers': [], 'error': str(e)}), 500


@app.route('/api/admin/customers', methods=['POST'])
@admin_required
def api_admin_create_customer():
    """Create a new customer account."""
    data         = request.get_json(silent=True) or {}
    email        = data.get('email', '').strip()
    password     = data.get('password', '')
    display_name = data.get('display_name', '').strip()
    alpaca_key   = data.get('alpaca_key', '').strip()
    alpaca_secret= data.get('alpaca_secret', '').strip()

    if not email or not password:
        return jsonify({'ok': False, 'error': 'email and password required'}), 400
    try:
        # auto_activate=True: admin-provisioned accounts bypass email verification pipeline
        customer_id = auth.create_customer(email, password, display_name=display_name,
                                           auto_activate=True)
        if alpaca_key and alpaca_secret:
            auth.set_alpaca_credentials(customer_id, alpaca_key, alpaca_secret)
        log.info(f"Admin created customer {customer_id} ({email}) — auto-activated")
        return jsonify({'ok': True, 'id': customer_id})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    except Exception as e:
        log.error(f"create customer error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/customers/<customer_id>/alpaca', methods=['POST'])
@admin_required
def api_admin_set_alpaca(customer_id):
    """Set Alpaca credentials for a customer."""
    data   = request.get_json(silent=True) or {}
    key    = data.get('alpaca_key', '').strip()
    secret = data.get('alpaca_secret', '').strip()
    if not key or not secret:
        return jsonify({'ok': False, 'error': 'Both alpaca_key and alpaca_secret required'}), 400
    try:
        auth.set_alpaca_credentials(customer_id, key, secret)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/customers/<customer_id>/deactivate', methods=['POST'])
@admin_required
def api_admin_deactivate_customer(customer_id):
    """Deactivate a customer account (soft delete)."""
    if customer_id == session.get('customer_id'):
        return jsonify({'ok': False, 'error': 'Cannot deactivate your own account'}), 400
    try:
        auth.deactivate_customer(customer_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/processes')
@admin_required
def api_admin_processes():
    """List monitored agent processes with PID, CPU, RAM, uptime."""
    import time as _time
    WATCHED = [
        ('trade_logic_agent',      'retail_trade_logic_agent.py'),
        ('news_agent',             'retail_news_agent.py'),
        ('market_sentiment_agent', 'retail_market_sentiment_agent.py'),
        ('sector_screener',        'retail_sector_screener.py'),
        ('portal',                 'retail_portal.py'),
    ]
    results = []
    try:
        import psutil
        for name, script in WATCHED:
            entry = {'name': name, 'running': False, 'pid': None, 'cpu_pct': None, 'ram_mb': None, 'uptime': None}
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'memory_info', 'create_time']):
                try:
                    cmdline = ' '.join(proc.info.get('cmdline') or [])
                    if script in cmdline:
                        entry['running']  = True
                        entry['pid']      = proc.info['pid']
                        entry['cpu_pct']  = round(proc.info['cpu_percent'] or 0, 1)
                        entry['ram_mb']   = round((proc.info['memory_info'].rss or 0) / 1024 / 1024, 1)
                        secs = int(_time.time() - (proc.info.get('create_time') or _time.time()))
                        h, rem = divmod(secs, 3600)
                        entry['uptime'] = f"{h}h {rem//60}m" if h else f"{rem//60}m"
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            results.append(entry)
    except ImportError:
        results = [{'name': n, 'running': False, 'pid': None, 'cpu_pct': None, 'ram_mb': None, 'uptime': None} for n, _ in WATCHED]

    return jsonify({'processes': results})




# ── MARKET ACTIVITY API (for monitor chart) ─────────────────────────────────
@app.route('/api/admin/market-activity')
@admin_required
def api_admin_market_activity():
    """Aggregate buy/sell activity across ALL customers for the monitor chart."""
    from datetime import datetime, timedelta
    import auth as _auth

    hours = int(request.args.get('hours', 24))
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    # Build hourly bins
    now = datetime.utcnow()
    bins = {}
    for i in range(hours):
        h = (now - timedelta(hours=hours - 1 - i))
        key = h.strftime('%Y-%m-%dT%H:00')
        bins[key] = {'buys': 0.0, 'sells': 0.0, 'buy_count': 0, 'sell_count': 0}

    # Aggregate across all customer DBs
    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    for cid in os.listdir(customers_dir):
        if cid == 'default':
            continue
        db_path = os.path.join(customers_dir, cid, 'signals.db')
        if not os.path.exists(db_path):
            continue
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            # Buys
            for r in conn.execute(
                "SELECT opened_at, entry_price * shares AS amt FROM positions WHERE opened_at >= ?",
                (cutoff,)
            ).fetchall():
                ts = (r['opened_at'] or '')[:13] + ':00'
                if ts in bins:
                    bins[ts]['buys'] += float(r['amt'] or 0)
                    bins[ts]['buy_count'] += 1
            # Sells
            for r in conn.execute(
                "SELECT closed_at, entry_price * shares AS amt FROM positions WHERE closed_at IS NOT NULL AND closed_at >= ?",
                (cutoff,)
            ).fetchall():
                ts = (r['closed_at'] or '')[:13] + ':00'
                if ts in bins:
                    bins[ts]['sells'] += float(r['amt'] or 0)
                    bins[ts]['sell_count'] += 1
            conn.close()
        except Exception as e:
            log.warning(f"market-activity scan for {cid[:8]}: {e}")

    # Session history from ring buffer
    session_bins = {k: 0 for k in bins}
    with _session_activity_lock:
        for ts, count in _session_hourly:
            # Map minute-level snapshots to hour bins
            hour_key = ts[:13] + ':00'
            if hour_key in session_bins:
                session_bins[hour_key] = max(session_bins[hour_key], count)
        # Current active count
        active_now = sum(1 for v in _session_activity.values()
                        if (datetime.utcnow() - v['last_activity']).total_seconds() < 900)

    hours_list = sorted(bins.keys())
    total_buys = sum(bins[h]['buys'] for h in hours_list)
    total_sells = sum(bins[h]['sells'] for h in hours_list)
    sessions_list = [session_bins.get(h, 0) for h in hours_list]
    peak = max(sessions_list) if sessions_list else 0

    return jsonify({
        'hours':    hours_list,
        'buys':     [round(bins[h]['buys'], 2) for h in hours_list],
        'sells':    [round(bins[h]['sells'], 2) for h in hours_list],
        'sessions': sessions_list,
        'summary': {
            'total_buys':    round(total_buys, 2),
            'total_sells':   round(total_sells, 2),
            'net_flow':      round(total_buys - total_sells, 2),
            'buy_count':     sum(bins[h]['buy_count'] for h in hours_list),
            'sell_count':    sum(bins[h]['sell_count'] for h in hours_list),
            'active_now':    active_now,
            'peak_sessions': max(peak, active_now),
        },
    })

@app.route('/api/admin/scheduler-history')
@admin_required
def api_admin_scheduler_history():
    """Return recent scheduler run history from scheduler_history.json."""
    history_file = os.path.join(_ROOT_DIR, 'logs', 'scheduler_history.json')
    try:
        if not os.path.exists(history_file):
            return jsonify({'history': [], 'note': 'No runs recorded yet'})
        with open(history_file) as f:
            history = json.load(f)
        return jsonify({'history': history[:50]})
    except Exception as e:
        return jsonify({'history': [], 'error': str(e)})


# ── START ──────────────────────────────────────────────────────────────────


# ── SESSION SNAPSHOT BACKGROUND THREAD ──────────────────────────────────────
def _session_snapshot_loop():
    """Record active session count every 60 seconds for the market activity chart."""
    import time as _t
    while True:
        _t.sleep(60)
        try:
            now = datetime.utcnow()
            ts = now.strftime('%Y-%m-%dT%H:%M')
            with _session_activity_lock:
                active = sum(1 for v in _session_activity.values()
                           if (now - v['last_activity']).total_seconds() < 900)
            _session_hourly.append((ts, active))
        except Exception:
            pass

_snap_thread = _threading.Thread(target=_session_snapshot_loop, daemon=True)
_snap_thread.start()

if __name__ == '__main__':
    auth.init_auth_db()
    auth.ensure_admin_account()
    auth.ensure_owner_customer()
    log.info(f"Synthos Portal starting on port {PORT}")
    log.info(f"Pi: {PI_ID} | Mode: {OPERATING_MODE}")
    log.info(f"Kill switch: {'ACTIVE' if kill_switch_active() else 'clear'}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
