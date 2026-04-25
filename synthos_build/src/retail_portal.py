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
from flask import Flask, request, jsonify, render_template, render_template_string, redirect, session, flash
from dotenv import load_dotenv
import auth

_SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))   # src/
_ROOT_DIR            = os.path.dirname(_SCRIPT_DIR)                  # synthos_build/
sys.path.insert(0, _SCRIPT_DIR)
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

# Phase C / D6 — shared helpers (2026-04-20)
from retail_shared import kill_switch_active  # noqa: E402

PROJECT_DIR          = _SCRIPT_DIR                                   # keep for co-located script references
KILL_SWITCH_FILE     = os.path.join(_ROOT_DIR, '.kill_switch')
ENV_PATH             = os.path.join(_ROOT_DIR, 'user', '.env')
LOG_DIR              = os.path.join(_ROOT_DIR, 'logs')
ET                   = ZoneInfo("America/New_York")
PORT                 = int(os.environ.get('PORTAL_PORT', 5001))
PI_ID                = os.environ.get('PI_ID', 'synthos-pi')
AUTONOMOUS_UNLOCK_KEY = os.environ.get('AUTONOMOUS_UNLOCK_KEY', '')
OPERATING_MODE       = os.environ.get('OPERATING_MODE', 'MANAGED').upper()
ADMIN_TRADING_GATE   = os.environ.get('ADMIN_TRADING_GATE', 'ALL')
ADMIN_OPERATING_MODE = os.environ.get('ADMIN_OPERATING_MODE', 'ALL')

# ── SETTINGS UI LOCK ──────────────────────────────────────────────────────
# When true, the agent-configuration slide-out is hidden for ALL users
# (admin included), AND writes to /api/settings are refused for every key
# except `setup_complete` (which is just a "dismiss setup guide" flag).
# Used during the 2026-04 tier-calibration experiment: admin edits customer
# settings directly via DB/script so end-users can't drift the fleet config
# while we measure per-tier behavior.
# Flip to 'false' in .env when you want to re-open the slide-out.
SETTINGS_UI_LOCKED   = os.environ.get('SETTINGS_UI_LOCKED', 'true').lower() == 'true'
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

# ══════════════════════════════════════════════════════════════════════════
# EARLY-ACCESS TOS + NON-RESTRICTIVE SETUP GUIDE  (DORMANT)
# ─────────────────────────────────────────────────────────────────────────
# Everything guarded by EARLY_ACCESS_TOS_ENABLED.  When False:
#   - Server: no new routes are hit; helpers return no-op; render_template_string
#     gets ea_enabled=False; the dormant HTML blocks don't activate.
#   - Client: `window.EARLY_ACCESS_TOS_ENABLED === false`, the setup-overlay
#     and TOS-modal bootstraps are no-ops; legacy SETUP_COMPLETE auto-redirect
#     to the Setup Guide tab remains in place.
#
# When True:
#   - Real humans see a TOS modal on first login that supersedes /terms.
#   - Setup Guide tab no longer force-opens — a non-blocking "Getting
#     Started" overlay appears each login, dismissible with "OK" (session)
#     or "Don't show again" + "OK" (persistent).
#   - Fixture accounts (ACCOUNT_TYPE='fixture' in customer_settings) skip
#     both flows entirely.  That marker is set ONLY by the bootstrap script
#     — never by UI — so real beta-testers / early-adopters always get the
#     full flow.
#
# Design doc:  synthos_build/docs/early_access_tos_design.md
# TOS copy:    synthos_build/docs/tos_early_access.md
# ══════════════════════════════════════════════════════════════════════════
EARLY_ACCESS_TOS_ENABLED = False

# Bump this only when the TOS copy changes materially.  All users then
# re-accept via the modal.  Non-material changes (typos) must leave this
# alone, per §11 of the TOS.
EARLY_ACCESS_TOS_VERSION = "1.0"

# customer_settings value identifying a non-human fixture account (the
# paper accounts seeded by bootstrap_test_fleet.py).  Must only ever be
# written by the bootstrap script; the portal UI has no path to set it.
EA_FIXTURE_ACCOUNT_TYPE  = "fixture"
EA_ACCOUNT_TYPE_KEY      = "ACCOUNT_TYPE"
EA_TOS_ACCEPTED_KEY      = "EA_TOS_ACCEPTED_VERSION"
EA_TOS_ACCEPTED_AT_KEY   = "EA_TOS_ACCEPTED_AT"
EA_SETUP_HIDDEN_KEY      = "EA_SETUP_GUIDE_HIDDEN"


def _ea_load_tos_html() -> str:
    """Read the TOS markdown once at module load and convert it to
    minimal HTML for the modal body.  Kept deliberately simple — the
    TOS is under review and we don't want a markdown dependency here.

    Supports: # / ## / ### headings, paragraphs, **bold**, *italic*,
    unordered lists (- or *), and horizontal rules (---).  Anything
    fancier should be avoided in the TOS copy itself."""
    path = os.path.join(_ROOT_DIR, 'docs', 'tos_early_access.md')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
    except Exception as e:
        log.error(f"Could not read early-access TOS markdown: {e}")
        return "<p><em>(TOS copy unavailable — contact support.)</em></p>"

    import html as _html, re as _re
    out:   list[str] = []
    lines = raw.splitlines()
    i = 0
    in_list = False

    def _inline(s: str) -> str:
        s = _html.escape(s)
        s = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = _re.sub(r'(?<!\*)\*(?!\s)([^*]+?)\*(?!\*)', r'<em>\1</em>', s)
        return s

    def _flush_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            _flush_list()
            i += 1
            continue
        if line.strip() == '---':
            _flush_list()
            out.append('<hr style="border:0;border-top:1px solid rgba(255,255,255,0.08);margin:14px 0">')
            i += 1
            continue
        if line.startswith('### '):
            _flush_list()
            out.append(f'<h4 style="font-size:13px;font-weight:700;margin:14px 0 6px">{_inline(line[4:].strip())}</h4>')
            i += 1
            continue
        if line.startswith('## '):
            _flush_list()
            out.append(f'<h3 style="font-size:14px;font-weight:700;margin:18px 0 8px;color:var(--text)">{_inline(line[3:].strip())}</h3>')
            i += 1
            continue
        if line.startswith('# '):
            _flush_list()
            out.append(f'<h2 style="font-size:16px;font-weight:700;margin:4px 0 12px;color:var(--text)">{_inline(line[2:].strip())}</h2>')
            i += 1
            continue
        stripped = line.lstrip()
        if stripped.startswith('- ') or stripped.startswith('* '):
            if not in_list:
                out.append('<ul style="padding-left:18px;margin:6px 0 10px">')
                in_list = True
            out.append(f'<li style="margin:3px 0">{_inline(stripped[2:].strip())}</li>')
            i += 1
            continue
        # Paragraph — collect consecutive non-empty, non-structural lines.
        _flush_list()
        buf = [line]
        j = i + 1
        while j < len(lines) and lines[j].strip() and not (
            lines[j].startswith(('#', '-', '*'))
            or lines[j].strip() == '---'
        ):
            buf.append(lines[j].rstrip())
            j += 1
        out.append(f'<p style="margin:6px 0 10px;line-height:1.65">{_inline(" ".join(buf))}</p>')
        i = j

    _flush_list()
    return "\n".join(out)


# Rendered once at module load — cheap, and the TOS file is small.
_EA_TOS_HTML = _ea_load_tos_html()


def _ea_is_fixture(cdb) -> bool:
    """True if this customer is a non-human test fixture and should bypass
    the TOS modal + setup overlay.  Missing key → treated as a real user."""
    if not EARLY_ACCESS_TOS_ENABLED:
        return False
    try:
        return (cdb.get_setting(EA_ACCOUNT_TYPE_KEY) or "user").lower() \
               == EA_FIXTURE_ACCOUNT_TYPE
    except Exception:
        return False


def _ea_status(cdb) -> dict:
    """Shape the state the client needs to decide whether to show the
    modal / overlay.  Returns the dormant shape when the feature flag is
    off so the JS bootstrap has a consistent contract."""
    if not EARLY_ACCESS_TOS_ENABLED:
        return {
            "enabled":          False,
            "fixture":          False,
            "tos_needs_accept": False,
            "setup_hidden":     True,
            "tos_version":      EARLY_ACCESS_TOS_VERSION,
        }
    fixture     = _ea_is_fixture(cdb)
    accepted    = (cdb.get_setting(EA_TOS_ACCEPTED_KEY) or "") == EARLY_ACCESS_TOS_VERSION
    setup_hide  = (cdb.get_setting(EA_SETUP_HIDDEN_KEY) or "0") == "1"
    return {
        "enabled":          True,
        "fixture":          fixture,
        # Fixtures never see the modal; everyone else until they accept.
        "tos_needs_accept": (not fixture) and (not accepted),
        # Fixtures also skip the setup overlay; real users see it each
        # login until they tick "Don't show again".
        "setup_hidden":     fixture or setup_hide,
        "tos_version":      EARLY_ACCESS_TOS_VERSION,
    }
# ══════════════════════════════════════════════════════════════════════════

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

app = Flask(__name__, static_folder='../static', static_url_path='/static')
_env_secret = os.environ.get('PORTAL_SECRET_KEY', '')
if not _env_secret:
    log.warning("PORTAL_SECRET_KEY not set — generating ephemeral key (sessions won't survive restart)")
    _env_secret = secrets.token_hex(32)
app.secret_key = _env_secret

# ── SESSION COOKIE SECURITY ────────────────────────────────────────────────
# Secure=True: browser only sends cookie over HTTPS (Cloudflare Tunnel handles TLS).
# Set HTTPS_ONLY=false in .env only for local HTTP testing.
app.config['SESSION_COOKIE_SECURE']      = os.environ.get('HTTPS_ONLY', 'true').lower() != 'false'
app.config['SESSION_COOKIE_HTTPONLY']    = True          # JS cannot read session cookie
app.config['SESSION_COOKIE_SAMESITE']   = 'Strict'      # blocks cross-site request forgery
app.config['SESSION_COOKIE_NAME']        = 'synthos_s'  # non-default name
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# ── JINJA2 TEMPLATE AUTO-RELOAD ────────────────────────────────────────────
# Flask defaults TEMPLATES_AUTO_RELOAD=False unless debug mode is on. On local
# dev (HTTPS_ONLY=false — set by scripts/dev_portal.sh), enable reload so
# template edits land without a portal restart. Prod pi5 leaves this False —
# gunicorn workers get restarted explicitly on deploy anyway.
if os.environ.get('HTTPS_ONLY', 'true').lower() == 'false':
    app.config['TEMPLATES_AUTO_RELOAD'] = True

# ── SESSION ACTIVITY TRACKING ──────────────────────────────────────────────
import threading as _threading
from collections import deque as _deque

_session_activity = {}           # {customer_id: {last_activity: datetime, ip: str}}
_session_activity_lock = _threading.Lock()
# Server-side session revocation map. Set when a customer's credentials
# change (password reset, password update). Sessions whose `logged_in_at`
# timestamp is earlier than the revoked_at value get force-logged-out
# on next request. Cleared on portal restart (acceptable — portal restart
# already invalidates _session_activity tracking). Added 2026-04-25
# (security audit Phase 2.5 MED-1).
_session_revoked_at = {}         # {customer_id: datetime (UTC)}
_session_revoked_lock = _threading.Lock()


def _revoke_customer_sessions(customer_id: str, reason: str = ''):
    """Force-logout all of a customer's existing sessions on their next
    request. Idempotent — calling repeatedly is safe.

    Used after password reset or password change so the credentials no
    longer recognised by the security model can't continue to ride the
    8-hour cookie. The session itself stays valid client-side (cookie
    is still signed and parseable), but before_request sees the
    customer's revoked_at > session.logged_in_at and clears+redirects.
    """
    if not customer_id:
        return
    with _session_revoked_lock:
        _session_revoked_at[customer_id] = datetime.now(timezone.utc)
    log.info(f"[SESSIONS] Revoked all active sessions for {customer_id[:8]}… ({reason})")
# Non-admin inactivity auto-logout window. Set via CUSTOMER_SESSION_TIMEOUT_MINUTES
# env var (default 15). Set to 0 to disable the inactivity check entirely —
# sessions then last until the 8-hour cookie expiry regardless of idle time.
# Used during travel / extended remote work where tab-switching triggers
# false logouts.
_TIMEOUT_MINUTES = int(os.environ.get('CUSTOMER_SESSION_TIMEOUT_MINUTES', '15'))
_CUSTOMER_TIMEOUT = timedelta(minutes=_TIMEOUT_MINUTES)
_session_hourly = _deque(maxlen=1440)   # (iso_minute, active_count, [customer_names]) per-minute for 24h



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
        # Cloudflare Insights beacon (static.cloudflareinsights.com) is
        # auto-injected by Cloudflare when Web Analytics is enabled on the
        # hostname. If Web Analytics is turned off in the Cloudflare
        # dashboard, these two entries become no-ops.
        "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data: https://cdn.benzinga.com https://*.benzinga.com; "
        "connect-src 'self' https://cloudflareinsights.com; "
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
            return render_template('construction.html'), 200
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


def _notify_admin_new_customer(name, email):
    """Push NEW_CUSTOMER event to monitor scoop queue so admin gets a bell/toast."""
    monitor_url   = os.environ.get('MONITOR_URL', '').rstrip('/')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    if not monitor_url:
        log.debug("MONITOR_URL not set — new-customer notification skipped")
        return False
    try:
        import requests as _req
        payload = {
            "event_type":   "NEW_CUSTOMER",
            "priority":     1,
            "subject":      f"{name or 'New customer'} joined Synthos",
            "body":         f"{name or 'Someone'} ({email or 'no email'}) verified their email and was auto-approved.",
            "source_agent": "portal",
            "pi_id":        os.environ.get('PI_ID', 'synthos-retail'),
            "audience":     "internal",
            "payload":      "{}",
        }
        r = _req.post(
            f"{monitor_url}/api/enqueue",
            json=payload,
            headers={"X-Token": monitor_token, "Content-Type": "application/json"},
            timeout=5,
        )
        if r.status_code == 200:
            log.info(f"Admin notified: new customer {name} ({email})")
            return True
        log.warning(f"Admin notify returned {r.status_code}")
        return False
    except Exception as e:
        log.warning(f"Admin new-customer notify failed: {e}")
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
# _SHARED_CSS variable removed 2026-04-23 — contents moved to
# synthos_build/static/css/core.css. No remaining consumers after
# LANDING_HTML + LOGIN_HTML migrated to extend base.html.

# LANDING_HTML extracted 2026-04-23 → src/templates/landing.html
# Tier 2 migration on feat/portal-v2. Was a 386-line concatenation with
# _SHARED_CSS; now extends base.html and core.css handles the shared tokens.


# LOGIN_HTML extracted 2026-04-23 → src/templates/login.html
# Tier 2 migration on feat/portal-v2. Was a concatenation LOGIN_HTML = """...""" +
# _SHARED_CSS + """..."""; now extends base.html + core.css. Fixed missing --accent.


def _maintenance_msg():
    """Read .maintenance_message file if it exists. Returns message string or None."""
    try:
        f = _ROOT_DIR / '.maintenance_message'
        return f.read_text().strip() if f.exists() else None
    except Exception:
        return None

def is_authenticated():
    """Returns True if the current session has a valid customer_id."""
    return session.get('customer_id') is not None


def is_admin():
    """Returns True if the current session belongs to an admin account."""
    return session.get('role') == 'admin'


@app.before_request
def check_auth():
    # Static assets (fonts, images, etc.) must be publicly fetchable — the
    # login / signup pages themselves are public and need their fonts to
    # load before any session exists. Without this the @font-face URLs
    # get redirected to /login and fall back to system fonts.
    if request.path.startswith('/static/'):
        return
    # Routes that are always public — no session required
    public_routes = {'/', '/login', '/logout', '/signup', '/verify-email', '/forgot-password', '/sso', '/check-email', '/reset-password',
                     '/terms/view',
                     '/admin/construction-verify'}
    if request.path in public_routes:
        return
    # Token-based routes are public (the token IS the auth)
    if (request.path.startswith('/setup-account/')
            or request.path.startswith('/verify-email/')
            or request.path.startswith('/verify-email-change/')
            or request.path.startswith('/reset-password/')):
        return
    # Monitor-callable endpoints — bearer token handled inside the function
    if request.path in {'/api/logs-audit', '/api/get-keys', '/api/admin-override',
                        '/api/admin/alert'}:
        return
    # Stripe webhook — authenticated by Stripe signature, not session
    if request.path == '/webhook/stripe':
        return
    if not is_authenticated():
        return redirect('/login')

    # ── Server-side credential-rotation revocation check ──
    # If the customer's credentials were reset/changed since this session
    # was issued, force re-login. Set by _revoke_customer_sessions() after
    # password change / reset. Added 2026-04-25 (Phase 2.5 MED-1).
    _cid = session.get('customer_id')
    if _cid:
        revoked_at = _session_revoked_at.get(_cid)
        if revoked_at:
            sess_logged_in_at = session.get('logged_in_at')
            try:
                _sess_iso = datetime.fromisoformat(sess_logged_in_at) if sess_logged_in_at else None
            except Exception:
                _sess_iso = None
            if _sess_iso is None or _sess_iso < revoked_at:
                log.info(f"[SESSIONS] Force-logout {_cid[:8]}… "
                         f"(session_issued_at={sess_logged_in_at} < revoked_at={revoked_at.isoformat()})")
                session.clear()
                return redirect('/login')

    # ── Session activity tracking + non-admin auto-logout ──
    if _cid:
        _now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Auto-polling endpoints don't count as user activity
        _passive_paths = {'/api/status', '/api/approvals', '/api/agent-pulse',
                          '/api/portfolio-history', '/api/customer-settings'}
        _is_active_request = request.path not in _passive_paths

        with _session_activity_lock:
            _prev = _session_activity.get(_cid)
            # Non-admin: auto-logout after the configured idle window.
            # Skip entirely when _TIMEOUT_MINUTES == 0 (explicitly disabled).
            if (_TIMEOUT_MINUTES > 0
                    and _prev and session.get('role') != 'admin'):
                _elapsed = (_now - _prev['last_activity']).total_seconds()
                if _elapsed > _CUSTOMER_TIMEOUT.total_seconds():
                    _session_activity.pop(_cid, None)
                    session.clear()
                    return redirect('/login')
            # Only update activity timestamp on real user interaction
            if _is_active_request:
                _session_activity[_cid] = {
                    'last_activity': _now,
                    'ip': request.remote_addr or '',
                }
            elif _cid not in _session_activity:
                # First request — initialize even if passive
                _session_activity[_cid] = {
                    'last_activity': _now,
                    'ip': request.remote_addr or '',
                }


# ── SIGNUP ────────────────────────────────────────────────────────────────────

# _SIGNUP_PAGE_HTML extracted 2026-04-23 → src/templates/signup.html
# Tier 2 migration on feat/portal-v2 (extends base.html, uses static/js/pw-toggle.js).



@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    """Send password reset email with time-limited link."""
    submitted = False
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if email:
            log.info(f"Password reset requested for: {email}")
            try:
                token = auth.create_password_reset_token(email)
                if token:
                    # Send reset email via Resend
                    import requests as _req
                    resend_key = os.environ.get('RESEND_API_KEY', '')
                    portal_domain = os.environ.get('PORTAL_DOMAIN', 'portal.synth-cloud.com')
                    reset_url = f"https://{portal_domain}/reset-password/{token}"
                    if resend_key:
                        _req.post("https://api.resend.com/emails", json={
                            "from": f"Synthos <{os.environ.get('ALERT_FROM', 'alerts@synth-cloud.com')}>",
                            "to": [email],
                            "subject": "Synthos — Password Reset",
                            "html": f"<p>You requested a password reset for your Synthos account.</p>"
                                    f"<p><a href=\"{reset_url}\" style=\"display:inline-block;padding:12px 24px;background:#00f5d4;color:#000;text-decoration:none;border-radius:8px;font-weight:bold\">Reset Password</a></p>"
                                    f"<p style=\"color:#888;font-size:12px\">This link expires in 30 minutes. If you did not request this, ignore this email.</p>",
                        }, headers={"Authorization": f"Bearer {resend_key}"}, timeout=10)
                        log.info(f"Password reset email sent to {email}")
                    else:
                        log.warning("RESEND_API_KEY not set — cannot send reset email")
            except Exception as e:
                log.error(f"Password reset error: {e}")
            submitted = True  # Always show success (don't reveal if email exists)

    return render_template('forgot_password.html', submitted=submitted)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password_page(token):
    """Receive the password-reset link. Validates the token, lets the
    customer set a new password.

    Added 2026-04-25 (security audit Phase 2.5 HIGH-1 fix). Prior to this,
    forgot_password_page() generated reset URLs but the corresponding
    handler did not exist — clicking the email link returned 404.

    GET — validates token, renders the new-password form
    POST — sets the new password (min 12 chars), revokes all existing
           sessions for this customer (so a stolen/leaked session
           cannot continue), redirects to /login?reset=1.

    Security properties:
      • Token is 256-bit URL-safe random (auth.create_password_reset_token)
      • 30-min TTL enforced by auth.verify_reset_token / auth.reset_password
      • Single-use: auth.reset_password clears token+expires on success
      • Re-render hides whether the customer's email is registered
        (we already validated the token; if it's bad we say so)
    """
    # Validate token before rendering the form so a bad/expired token
    # gets a clear error page instead of a working form that errors at submit.
    customer_row = auth.verify_reset_token(token)
    if not customer_row:
        log.info(f"[RESET] invalid or expired reset token {token[:8]}…")
        return render_template(
            'reset_password.html',
            error="This reset link is invalid or has expired. Request a new one from /forgot-password."
        ), 400

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')
        if len(password) < 12:
            return render_template('reset_password.html',
                                   error="Password must be at least 12 characters.")
        if password != confirm:
            return render_template('reset_password.html',
                                   error="Passwords do not match.")
        try:
            ok = auth.reset_password(token, password)
        except Exception as e:
            log.error(f"[RESET] password update failed: {e}")
            ok = False
        if not ok:
            # Race: token expired between GET form-render and POST. Tell
            # the customer plainly and direct them back to forgot-password.
            return render_template(
                'reset_password.html',
                error="This reset link expired during submission. "
                      "Request a new one from /forgot-password."
            ), 400

        # MED-1: revoke all of this customer's existing sessions. Any
        # other browser/device still riding an 8h cookie will be force-
        # logged-out on next request.
        _revoke_customer_sessions(customer_row['id'], reason='password_reset')
        log.info(f"[RESET] password reset succeeded for customer {customer_row['id']}")
        return redirect('/login?reset=1')

    return render_template('reset_password.html', error=None)


@app.route('/signup', methods=['GET', 'POST'])
def signup_page():
    """Public signup page — validates access code, stores pending signup for admin approval."""
    error   = None
    success = False

    if request.method == 'POST':
        code         = request.form.get('access_code', '').strip()
        name         = request.form.get('name', '').strip()
        email        = request.form.get('email', '').strip()
        phone        = request.form.get('phone', '').strip()
        state        = request.form.get('state', '').strip().upper()
        zip_code     = request.form.get('zip_code', '').strip()
        password     = request.form.get('password', '')
        confirm      = request.form.get('confirm_password', '')
        tos_accepted = request.form.get('tos_accepted') == 'yes'

        # Basic email format check — not RFC-perfect but rejects obvious garbage
        # (no @, no domain, whitespace). Final validation is the user actually
        # receiving the verification email.
        import re as _re
        EMAIL_RE = _re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

        if not auth.verify_signup_access_code(code):
            error = "Invalid access code. Contact the operator for an invite code."
        elif not name or not email:
            error = "Name and email are required."
        elif not EMAIL_RE.match(email):
            error = "Please enter a valid email address."
        elif state != 'GA':
            error = "Synthos is currently available to Georgia residents only. More states coming soon."
        elif not zip_code or len(zip_code) != 5 or not zip_code.isdigit():
            error = "Please enter a valid 5-digit zip code."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif not tos_accepted:
            error = "You must accept the Terms of Service to continue."
        else:
            try:
                # Capture audit trail for the ToS acceptance event. These are
                # stored on the pending_signups row and carried into the customer
                # row on approval, so the "I agreed" timestamp always reflects
                # when the user checked the box (not when an admin approved).
                tos_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
                tos_ua = request.headers.get('User-Agent', '')[:500]
                _sid = auth.create_pending_signup(
                    name, email, phone, password,
                    state=state, zip_code=zip_code,
                    tos_accepted=True,
                    tos_version=TOS_CURRENT_VERSION,
                    tos_ip=tos_ip,
                    tos_user_agent=tos_ua,
                )
                # Send email verification link
                try:
                    _vtoken = auth.generate_signup_verify_token(_sid)
                    _send_verification_email(email, name, _vtoken)
                except Exception as _ve:
                    log.warning(f"Verification email failed: {_ve}")
                success = True
                log.info(f"Signup submitted: {email} (ToS v{TOS_CURRENT_VERSION} accepted, verification email sent)")
            except ValueError as e:
                error = str(e)
            except Exception as e:
                log.error(f"Signup error: {e}")
                error = "An unexpected error occurred. Please try again."

    return render_template('signup.html', error=error, success=success)


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
        cdb = get_customer_db(result['customer_id'])
        cdb.set_setting('NEW_CUSTOMER', 'true')
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
    """Fetch notifications for the current customer.

    Query params:
      widget_only=1  — dashboard widget / bell dropdown view; hides routine
                       'daily' / 'system' status pings.
      category=X     — explicit single-category filter (overrides widget_only).
      offset=N       — pagination (used by /notifications full-page view).
    """
    db = _customer_db()
    unread_only = request.args.get('unread_only') == '1'
    widget_only = request.args.get('widget_only') == '1'
    category = request.args.get('category')
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = max(int(request.args.get('offset', 0)), 0)
    notifs = db.get_notifications(
        limit=limit, unread_only=unread_only, category=category,
        widget_only=widget_only, offset=offset,
    )
    total = db.count_notifications(
        unread_only=unread_only, category=category, widget_only=widget_only,
    )
    return jsonify({"notifications": notifs, "total": total,
                    "offset": offset, "limit": limit})


@app.route('/api/notifications/unread-count', methods=['GET'])
@login_required
def api_notifications_unread_count():
    """Lightweight unread count for badge polling.
    widget_only=1 restricts to the categories the bell actually displays."""
    db = _customer_db()
    widget_only = request.args.get('widget_only') == '1'
    count = db.get_unread_count(widget_only=widget_only)
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


@app.route('/notifications', methods=['GET'])
@login_required
def notifications_page():
    """Full-page notification archive. Shows all categories (including the
    routine 'daily' / 'system' pings filtered out of the dashboard widget and
    bell dropdown). Supports category filter, unread-only toggle, pagination."""
    return render_template('notifications.html')


# NOTIFICATIONS_PAGE_HTML extracted 2026-04-23 → src/templates/notifications.html
# Tier 2 migration on feat/portal-v2. Full page + JS kept inline in the template;
# candidate for JS extraction later.


@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_authenticated():
        return redirect('/admin' if is_admin() else '/')

    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
        # Strip X-Forwarded-For chains down to the first hop (the real client
        # IP per Cloudflare's docs); guards against spoofed long chains.
        ip = ip.split(',')[0].strip()
        ua = (request.headers.get('User-Agent') or '')[:255]

        # ── In-memory per-IP burst limiter (cheap, first gate) ───────
        if _rate_limited(_login_attempts, ip, _LOGIN_MAX, _LOGIN_WINDOW):
            log.warning("Login rate limit hit (in-mem) for IP %s", ip)
            return render_template('login.html',
                error="Too many login attempts — please wait a few minutes.",
                maintenance_msg=_maintenance_msg())

        # ── Persistent per-IP lockout (defeats restart-amnesia) ──────
        ip_locked, ip_retry = auth.is_ip_locked(ip)
        if ip_locked:
            log.warning(f"Login IP-lockout active for {ip} (retry in {ip_retry}s)")
            mins = max(1, (ip_retry or 60) // 60)
            return render_template('login.html',
                error=f"Too many failed login attempts from your network. "
                      f"Try again in about {mins} minute{'s' if mins != 1 else ''}.",
                maintenance_msg=_maintenance_msg())

        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        # ── Persistent per-account lockout (defeats IP rotation) ──────
        # Checked before password verification so the attacker doesn't get a
        # pw-correct/incorrect oracle inside the lockout window.
        if email:
            acct_locked, acct_retry = auth.is_account_locked(email)
            if acct_locked:
                log.warning(f"Login account-lockout active for {email!r} (retry in {acct_retry}s)")
                # Record the failure so the lockout window slides forward
                # if the attacker keeps trying.
                auth.record_login_attempt(email, ip, ua, success=False)
                mins = max(1, (acct_retry or 900) // 60)
                return render_template('login.html',
                    error=f"This account is temporarily locked after too many failed attempts. "
                          f"Try again in about {mins} minute{'s' if mins != 1 else ''}, or "
                          f"reset your password using the 'Forgot password?' link below.",
                    maintenance_msg=_maintenance_msg())

        # ── Primary: account-based auth via auth.db ──
        if email:
            try:
                customer = auth.get_customer_by_email(email)
                if customer and auth.verify_password(password, customer['password_hash']):
                    # ── Access gate: subscription + email verification ──────────
                    allowed, reason = auth.is_access_allowed(customer['id'], customer['role'])
                    if not allowed:
                        # Even denied logins are recorded so abuse of valid
                        # credentials against a deactivated account is visible.
                        auth.record_login_attempt(email, ip, ua, success=False)
                        if reason == 'unverified':
                            # Account created but setup link not yet completed
                            return redirect('/check-email?reason=unverified')
                        elif reason in ('past_due', 'inactive', 'cancelled'):
                            return redirect('/subscribe?reason=' + reason)
                        else:
                            log.warning(f"Login denied: {customer['id']} reason={reason}")
                            return render_template('login.html', error="Account access denied. Contact support.", maintenance_msg=_maintenance_msg())

                    session.clear()
                    session['customer_id']  = customer['id']
                    session['role']         = customer['role']
                    session['display_name'] = auth.get_display_name(customer)
                    session['access_reason']= reason   # 'active'|'trialing'|'grace_period'|'admin'
                    session['tos_version']  = customer['tos_version']
                    # Issued-at marker for credential-rotation revocation
                    # (Phase 2.5 MED-1). before_request compares this to
                    # _session_revoked_at[customer_id] to force re-login
                    # after password reset/change.
                    session['logged_in_at'] = datetime.now(timezone.utc).isoformat()
                    session.permanent       = True
                    auth.record_login(customer['id'])
                    auth.record_login_attempt(email, ip, ua, success=True)
                    log.info(f"Login: {customer['id']} (role={customer['role']} access={reason})")
                    return redirect('/')
            except Exception as e:
                log.error(f"Auth error during login: {e}")

        # Failed login (wrong password, missing customer, or exception above).
        # Record so per-account + per-IP lockouts work.
        auth.record_login_attempt(email, ip, ua, success=False)
        return render_template('login.html', error="Incorrect email or password", maintenance_msg=_maintenance_msg())

    return render_template('login.html', error=None, maintenance_msg=_maintenance_msg())


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── TERMS OF SERVICE ──────────────────────────────────────────────────────
#
# Template: src/templates/terms.html
# Extracted from inline _TERMS_HTML on 2026-04-22
# (patch/2026-04-22-portal-template-extraction). First page in the portal
# template-extraction initiative — pattern for subsequent pages:
#   1. Move HTML string → src/templates/<page>.html
#   2. Replace render_template_string(_X_HTML, …) → render_template('<page>.html', …)
#   3. Delete the inline constant
# Jinja2 syntax already used by render_template_string — no conversion.


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


@app.route('/terms/view', methods=['GET'])
def terms_view_public():
    """Public read-only ToS page — linked from the signup form so prospective
    users can read the terms before agreeing. No Accept button rendered;
    template hides the form when `read_only=True`."""
    return render_template('terms.html', version=TOS_CURRENT_VERSION,
                           error=None, read_only=True)


@app.route('/terms', methods=['GET'])
@authenticated_only
def terms_get():
    """Show Terms of Service page. Accessible to authenticated users regardless of ToS status."""
    # If already accepted current version, skip straight through
    if session.get('tos_version') == TOS_CURRENT_VERSION:
        return redirect('/')
    return render_template('terms.html', version=TOS_CURRENT_VERSION, error=None)


@app.route('/terms', methods=['POST'])
@authenticated_only
def terms_post():
    """Record ToS acceptance, write agreement file, update session."""
    if request.form.get('accepted') != 'yes':
        return render_template(
            'terms.html', version=TOS_CURRENT_VERSION,
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


# _CONSTRUCTION_PAGE_HTML extracted 2026-04-23 → src/templates/construction.html
# (patch/2026-04-22-portal-template-extraction). No Jinja variables used;
# static coming-soon page gating the customer acquisition pipeline.


# _CHECK_EMAIL_HTML extracted 2026-04-23 → src/templates/check_email.html
# Post-signup holding page shown after account creation. No Jinja vars.


# _SETUP_ACCOUNT_HTML extracted 2026-04-23 → src/templates/setup_account.html
# Password-setup form used by /setup-account/<token>. Renders with error kwarg.


# _SUBSCRIBE_HTML extracted 2026-04-23 → src/templates/subscribe.html
# Subscription-gate page for /subscribe. Renders with reason kwarg
# (past_due | cancelled | default).


# _CONSTRUCTION_VERIFY_HTML extracted 2026-04-23 → src/templates/construction_verify.html
# Admin OTP gate for construction-mode routes. Renders with error + sent kwargs.


@app.route('/check-email')
def check_email_page():
    """Holding page shown after account creation — instructs customer to check inbox."""
    return render_template('check_email.html'), 200


@app.route('/setup-account/<token>', methods=['GET', 'POST'])
def setup_account(token):
    """
    Password setup page. Token is single-use, 48h expiry.
    GET  — shows password setup form
    POST — validates passwords, calls auth.activate_account(), redirects to login
    """
    customer = auth.consume_verify_token(token)
    if not customer:
        return render_template('setup_account.html',
                                      error="This setup link is invalid or has expired. "
                                            "Contact support for a new link."), 400

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')

        if len(password) < 12:
            return render_template('setup_account.html',
                                          error="Password must be at least 12 characters.")
        if password != confirm:
            return render_template('setup_account.html',
                                          error="Passwords do not match.")

        try:
            auth.activate_account(customer['id'], password)
            log.info(f"Account activated via setup link: {customer['id']}")
            return redirect('/login?activated=1')
        except Exception as e:
            log.error(f"Account activation failed: {e}")
            return render_template('setup_account.html',
                                          error="Activation failed — please try again or contact support.")

    return render_template('setup_account.html', error=None)


# Email verification result pages
# _VERIFY_SUCCESS_HTML + _VERIFY_ERROR_HTML extracted 2026-04-23 →
# src/templates/verify_success.html + src/templates/verify_error.html
# Error template upgraded from .replace('ERROR_MSG', ...) hack to proper
# Jinja {{ error_msg }} variable; callsites updated to pass error_msg kwarg.


@app.route('/verify-email/<token>')
def verify_email(token):
    """Public route — verifies signup email from the link sent via Resend."""
    try:
        import auth as _auth
        result = _auth.verify_signup_email(token)
        if result.get("customer_id") and not result.get("already"):
            try:
                _send_approval_email(result.get("email", ""), result.get("name", ""))
            except Exception as _ae:
                log.warning(f"Approval email after verify failed: {_ae}")
            try:
                _notify_admin_new_customer(result.get("name", ""), result.get("email", ""))
            except Exception as _ne:
                log.warning(f"Admin notification after verify failed: {_ne}")
        return render_template('verify_success.html')
    except ValueError as e:
        # Not a signup verification token — try setup-account redirect (legacy)
        return redirect(f'/setup-account/{token}')
    except Exception as e:
        return render_template('verify_error.html',
                               error_msg="An unexpected error occurred."), 500


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
        return render_template('construction.html'), 200
    return render_template('subscribe.html', reason=reason)


def _verify_stripe_signature(payload, sig_header):
    """Verify Stripe-Signature header using HMAC-SHA256.
    Returns None on success, or a Flask (response, status_code) tuple on failure.
    """
    import hmac as _hmac
    import hashlib
    import time as _time

    if not STRIPE_WEBHOOK_SECRET:
        # Return 503 (Service Unavailable) instead of 500 — this is a
        # configuration state, not a server fault. Stripe retries on 5xx
        # but treats 503 + a Retry-After hint correctly. The 401/403
        # alternatives would tell an attacker the endpoint exists but
        # is misconfigured; 503 is more honest about the actual state.
        log.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET not set — rejecting")
        return jsonify({"error": "webhook handler not configured"}), 503

    try:
        parts     = {k: v for k, v in (p.split('=', 1) for p in sig_header.split(',')
                                        if '=' in p)}
        timestamp = parts.get('t', '')
        v1_sigs   = [v for k, v in parts.items() if k == 'v1']

        if not timestamp or not v1_sigs:
            log.warning("Stripe webhook: malformed Stripe-Signature header")
            return jsonify({"error": "invalid signature header"}), 400

        try:
            ts_age = abs(_time.time() - int(timestamp))
            if ts_age > 300:
                log.warning(f"Stripe webhook: timestamp too old ({ts_age:.0f}s) — replay?")
                return jsonify({"error": "timestamp too old"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "invalid timestamp"}), 400

        signed_payload = f"{timestamp}.".encode() + payload
        expected_sig = _hmac.new(
            STRIPE_WEBHOOK_SECRET.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        if not any(_hmac.compare_digest(expected_sig, sig) for sig in v1_sigs):
            log.warning("Stripe webhook: signature mismatch — rejecting")
            return jsonify({"error": "signature verification failed"}), 400

    except Exception as e:
        log.error(f"Stripe webhook signature check error: {e}")
        return jsonify({"error": "signature check failed"}), 400

    return None  # success


def _handle_checkout_completed(obj):
    """Handle checkout.session.completed Stripe event.
    Returns a Flask (response, status_code) tuple.
    """
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
            log.warning(f"checkout.session.completed duplicate: {e}")
            row = auth.get_customer_by_email(customer_email)
            if row:
                customer_id  = row['id']
                display_name = auth.get_display_name(row) or ''
            else:
                return jsonify({"error": str(e)}), 409

    token      = auth.generate_verify_token(customer_id)
    base       = PORTAL_BASE_URL or f"http://localhost:{PORT}"
    setup_link = f"{base}/setup-account/{token}"

    email_ok = _send_setup_email(customer_email, setup_link, display_name)
    if not email_ok:
        log.warning(
            f"Setup email failed for {customer_id} — token={token[:8]}… "
            f"link={setup_link}"
        )

    if stripe_sub_id:
        auth.update_subscription(
            customer_id=customer_id,
            stripe_customer_id=stripe_customer_id,
            subscription_id=stripe_sub_id,
            status='inactive',
        )

    return jsonify({"ok": True, "customer_id": customer_id}), 200


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
    payload    = request.data
    sig_header = request.headers.get('Stripe-Signature', '')

    err = _verify_stripe_signature(payload, sig_header)
    if err:
        return err

    try:
        event      = json.loads(payload)
        event_type = event.get('type', '')
        obj        = event.get('data', {}).get('object', {})
    except Exception as e:
        log.error(f"Stripe webhook: could not parse JSON body: {e}")
        return jsonify({"error": "invalid JSON"}), 400

    log.info(f"Stripe webhook: {event_type} id={event.get('id','?')[:20]}")

    if event_type == 'checkout.session.completed':
        return _handle_checkout_completed(obj)

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

    return render_template('construction_verify.html', error=error, sent=sent)


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
    # See same field at /login for rationale (Phase 2.5 MED-1)
    session['logged_in_at'] = datetime.now(timezone.utc).isoformat()
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

# kill_switch_active: imported from retail_shared above

def _read_agent_running():
    """Check if scheduler has an agent session running (.agent_running file)."""
    import json as _json
    status_file = os.path.join(_ROOT_DIR, '.agent_running')
    if not os.path.exists(status_file):
        return None
    try:
        data = _json.loads(open(status_file).read())
        agent = data.get('agent', 'unknown')
        started = data.get('started', '')
        from datetime import datetime
        age = 0
        if started:
            try:
                st = datetime.fromisoformat(started)
                age = int((datetime.now(st.tzinfo) - st).total_seconds())
            except Exception:
                pass
        if age > 900:  # stale — agent probably crashed
            try:
                os.remove(status_file)
            except Exception:
                pass
            return None
        return {'agent': agent, 'age_secs': age, 'session': data.get('session', '')}
    except Exception:
        return None


def get_wave_status():
    """Return wave animation state — checks override first, then agent running status."""
    import json as _json
    # Check admin wave override (from command portal)
    override_file = os.path.join(_ROOT_DIR, '.wave_override')
    if os.path.exists(override_file):
        try:
            data = _json.loads(open(override_file).read())
            if data.get('override'):
                return {
                    'agent': 'Override',
                    'age_secs': 0,
                    'color': data.get('color', 'teal'),
                    'amplitude': data.get('amplitude', 30),
                    'speed': data.get('speed'),
                    'frequency': data.get('frequency'),
                    'direction': data.get('direction'),
                    'is_override': True,
                }
        except Exception:
            pass
    # Fall back to scheduler status
    running = _read_agent_running()
    if running:
        return running
    return None


# ── AGENT STATUS LEXICON (view-layer only; never touches agent code) ─────────
# Maps raw agent identifiers to user-facing Synthos-persona lines.
# Sticky rotation holds a chosen line for `sticky_seconds` so the UI
# doesn't animate on every poll. Lexicon hot-reloads on mtime change.
_LEXICON_FILE = os.path.join(_ROOT_DIR, 'config', 'agent_status_lexicon.json')
_lexicon_cache = {'mtime': 0.0, 'data': None}
_lexicon_pick_cache = {}  # {key: (action, aside, picked_at_epoch)}

_LEXICON_FALLBACK = {
    'persona': 'Synthos',
    'sticky_seconds': 25,
    'agents': {},
    'aliases': {},
    'idle':     {'event_label': 'idle',     'lines': [['on watch', 'the tape is quiet']]},
    'fallback': {'event_label': 'activity', 'lines': [['on the grid', 'doing the work']]},
}


def _load_lexicon():
    """Return the parsed lexicon, cached and hot-reloaded on mtime change."""
    import json as _json
    try:
        mtime = os.path.getmtime(_LEXICON_FILE)
        if _lexicon_cache['data'] is not None and _lexicon_cache['mtime'] == mtime:
            return _lexicon_cache['data']
        with open(_LEXICON_FILE, 'r') as fh:
            data = _json.load(fh)
        _lexicon_cache['mtime'] = mtime
        _lexicon_cache['data'] = data
        return data
    except Exception as exc:
        log.warning(f"agent_status_lexicon unreadable — using builtin fallback: {exc}")
        return _LEXICON_FALLBACK


def _resolve_agent_key(raw_name, lex):
    """Resolve an agent identifier to its canonical key in lex['agents'], or None."""
    if not raw_name:
        return None
    canonical = lex.get('aliases', {}).get(raw_name, raw_name)
    if canonical in lex.get('agents', {}):
        return canonical
    return None


def interpret_agent_status(raw_agent):
    """
    View-layer mapping: raw agent name -> {persona, action, aside}.
    Accepts None (idle), a dict from get_wave_status(), or a bare string.
    Sticky rotation prevents per-poll churn.
    """
    import random
    import time as _time

    lex = _load_lexicon()
    persona = lex.get('persona', 'Synthos')
    sticky = int(lex.get('sticky_seconds', 25) or 25)

    if raw_agent is None or (isinstance(raw_agent, dict) and not raw_agent.get('agent')):
        key = '__idle__'
        bucket = lex.get('idle') or _LEXICON_FALLBACK['idle']
    else:
        raw_name = raw_agent.get('agent') if isinstance(raw_agent, dict) else raw_agent
        canonical = _resolve_agent_key(raw_name, lex)
        if canonical is None:
            key = '__fallback__'
            bucket = lex.get('fallback') or _LEXICON_FALLBACK['fallback']
        else:
            key = canonical
            bucket = lex['agents'][canonical]

    pool = bucket.get('lines') or [['on the grid', 'doing the work']]
    now = _time.time()
    cached = _lexicon_pick_cache.get(key)
    if cached and (now - cached[2]) < sticky:
        action, aside = cached[0], cached[1]
    else:
        choice = random.choice(pool)
        action = choice[0] if len(choice) > 0 else ''
        aside  = choice[1] if len(choice) > 1 else ''
        _lexicon_pick_cache[key] = (action, aside, now)

    return {
        'persona': persona,
        'action':  action,
        'aside':   aside,
    }


def interpret_event_label(raw_agent):
    """Return the short event label for an agent (stable, not rotated)."""
    lex = _load_lexicon()
    if not raw_agent:
        return (lex.get('idle') or _LEXICON_FALLBACK['idle']).get('event_label', 'activity')
    canonical = _resolve_agent_key(raw_agent, lex)
    if canonical is None:
        return (lex.get('fallback') or _LEXICON_FALLBACK['fallback']).get('event_label', 'activity')
    return lex['agents'][canonical].get('event_label', 'activity')


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
                # live_prices timestamps are UTC (written via db.now()).
                age = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(latest.replace('Z','+00:00').split('+')[0])).total_seconds()
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


def _enrich_single_db_position(p, alpaca_pos_map):
    """Enrich a single DB position dict with Alpaca live data and signal metadata.
    Returns the enriched position dict.

    Qty-drift defense (added 2026-04-25): when Alpaca reports a different
    qty than the DB row holds, the portal trusts Alpaca for the displayed
    shares + market_value (Alpaca is truth — that's the actual broker
    position). This closes the customer-display gap immediately, even if
    the trader's reconciliation hasn't run yet. The DB row itself is NOT
    written to here; the trader's _reconcile_position_qty handles the
    write on its next cycle.
    """
    ticker = p.get('ticker', '')
    ap = alpaca_pos_map.get(ticker, {})
    entry = float(p.get('entry_price', 0) or 0)
    db_shares = float(p.get('shares', 0) or 0)
    # Defensive qty-drift handling: prefer Alpaca's qty when it reports a
    # different value than DB. Tolerance of 1e-6 handles fractional rounding.
    if ap:
        try:
            a_qty = float(ap.get('qty', 0) or 0)
        except (TypeError, ValueError):
            a_qty = db_shares
    else:
        a_qty = db_shares
    if ap and a_qty > 0 and abs(db_shares - a_qty) > 1e-6:
        log.warning(f"[QTY_DRIFT] {ticker}: DB={db_shares:.6f} Alpaca={a_qty:.6f} "
                    f"({a_qty - db_shares:+.6f}) — using Alpaca qty for display")
        shares = a_qty
    else:
        shares = db_shares
    cost = round(entry * shares, 2)
    if ap:
        cur_price   = float(ap.get('current_price', entry) or entry)
        avg_entry   = float(ap.get('avg_entry_price', entry) or entry)
        # Prefer Alpaca's market_value when present (broker truth) — the
        # multiply path is a fallback for the rare case Alpaca returns the
        # field as 0/missing.
        a_mkt_value = float(ap.get('market_value', 0) or 0)
        mkt_value   = round(a_mkt_value, 2) if a_mkt_value > 0 else (
                      round(cur_price * shares, 2) if cur_price else cost)
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

    # Signal metadata lookup
    _sig_headline = _sig_source = _sig_confidence = _sig_image_url = _sig_source_url = None
    _sig_id = p.get('signal_id')
    if _sig_id:
        # R10-5 — was leaking the sqlite3 connection on any exception
        # between connect() and the trailing close(). Context manager
        # commits / closes cleanly even if .execute() raises.
        try:
            import sqlite3 as _sql
            _shared_path = _shared_db().path
            with _sql.connect(_shared_path, timeout=5) as _sc:
                _sc.row_factory = _sql.Row
                _sig = _sc.execute(
                    "SELECT headline, source, confidence, image_url, source_url FROM signals WHERE id=?",
                    (_sig_id,)
                ).fetchone()
                if _sig:
                    _sig_headline   = _sig['headline']
                    _sig_source     = _sig['source']
                    _sig_confidence = _sig['confidence']
                    _sig_image_url  = _sig['image_url'] if 'image_url' in _sig.keys() else None
                    _sig_source_url = _sig['source_url'] if 'source_url' in _sig.keys() else None
        except Exception as _e:
            log.warning(f"Signal lookup failed for id={_sig_id}: {_e}")

    return {
        **p,
        'shares':            shares,          # may differ from p['shares'] when qty-drift triggered above
        'current_price':     round(cur_price, 4),
        'market_value':      round(mkt_value, 2),
        'unrealized_pl':     round(unreal_pl, 2),
        'unrealized_plpc':   round(unreal_plpc * 100, 2),
        'day_pl':            round(day_pl, 2),
        'day_plpc':          round(day_plpc * 100, 2),
        'avg_entry_price':   round(avg_entry, 4),
        'cost_basis':        round(cost, 2),
        'is_orphan':         False,
        # Phase 7L (2026-04-25): fall back to entry_thesis (headline
        # copied to the positions row at open time) when the signal_id
        # lookup didn't resolve. Owner customer typically has both;
        # non-owners only have entry_thesis.
        'signal_headline':   _sig_headline or p.get('entry_thesis'),
        'signal_source':     _sig_source,
        'signal_confidence': _sig_confidence,
        'signal_image_url':  _sig_image_url,
        'signal_source_url': _sig_source_url,
    }


def _extract_orphan_positions(alpaca_pos_map, db_tickers):
    """Build orphan position dicts for Alpaca positions not tracked in DB.
    Returns list of orphan position dicts.
    """
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
    return orphans


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
        db_tickers.add(p.get('ticker', ''))
        enriched.append(_enrich_single_db_position(p, alpaca_pos_map))
    orphans = _extract_orphan_positions(alpaca_pos_map, db_tickers)
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


def _status_locked_mode(db, agent_running_info):
    """Fast-path status response when agent is actively running (no live
    Alpaca enrichment). Returns the status dict.

    During market hours an agent has the `.agent_running` lock most of
    the time (sentiment+news every 30 min, trader every 30 s). Without
    care this path used to hard-code `day_pl: 0.0`, which produced the
    UX bug "today's gain/loss only shows after close." Fix (2026-04-24):
    read live_prices (price_poller keeps it fresh every 60 s
    independent of the agent lock) and compute day_pl locally per
    position. Falls back to 0 if live_prices is missing the ticker or
    has no prev_close.
    """
    portfolio = db.get_portfolio()
    positions = db.get_open_positions()
    last_hb   = db.get_last_heartbeat()

    # Pull a snapshot of live_prices once. price_poller writes here every
    # 60 s during 8 AM–6 PM ET on weekdays; pre-market / weekend / off-hours
    # values may be stale but that's the same behaviour the non-locked path
    # gets, so behaviour matches.
    live_price_map = {}
    try:
        shared = _shared_db()
        with shared.conn() as c:
            for r in c.execute(
                "SELECT ticker, price, prev_close, day_change, day_change_pct "
                "FROM live_prices"
            ).fetchall():
                live_price_map[r['ticker']] = {
                    'price':          float(r['price'] or 0),
                    'prev_close':     float(r['prev_close'] or 0),
                    'day_change':     float(r['day_change'] or 0),
                    'day_change_pct': float(r['day_change_pct'] or 0),
                }
    except Exception as e:
        log.debug(f"locked-mode live_prices read failed (showing 0 day_pl): {e}")

    basic_positions = []
    for p in positions:
        entry  = float(p.get('entry_price', 0) or 0)
        shares = float(p.get('shares', 0) or 0)
        ticker = (p.get('ticker') or '').upper()
        lp = live_price_map.get(ticker, {})

        # Prefer live price; fall back to last DB-recorded current_price; entry as last resort
        cur = float(lp.get('price') or p.get('current_price') or 0) or entry

        # Compute today's P&L from prev_close locally — robust to any
        # qty drift between our DB and Alpaca's positions endpoint.
        prev_close = lp.get('prev_close', 0) or 0
        if prev_close and cur and shares:
            day_pl   = round((cur - prev_close) * shares, 2)
            day_plpc = round((cur - prev_close) / prev_close * 100, 2)
        else:
            day_pl, day_plpc = 0.0, 0.0

        basic_positions.append({
            **p,
            'current_price':    round(cur, 4),
            'market_value':     round(cur * shares, 2),
            'unrealized_pl':    round((cur - entry) * shares, 2),
            'unrealized_plpc':  round(((cur - entry) / entry * 100) if entry else 0, 2),
            'day_pl':           day_pl,
            'day_plpc':         day_plpc,
            'avg_entry_price':  round(entry, 4),
            'cost_basis':       round(entry * shares, 2),
            'is_orphan':        False,
        })
    mkt_total = sum(p['market_value'] for p in basic_positions)
    return {
        "portfolio_value":    round(portfolio['cash'] + mkt_total, 2),
        "cash":               round(portfolio['cash'], 2),
        "realized_gains":     round(portfolio.get('realized_gains', 0), 2),
        "open_positions":     len(basic_positions),
        "orphan_count":       0,
        "positions":          basic_positions,
        "urgent_flags":       0,
        "critical_flags":     0,
        "flags_detail":       [],
        "last_heartbeat":     last_hb['timestamp'] if last_hb else "Never",
        "kill_switch":        db.get_setting('KILL_SWITCH') == '1' or kill_switch_active(),
        "operating_mode":     db.get_setting('OPERATING_MODE') or OPERATING_MODE,
        "trading_mode":       os.environ.get('TRADING_MODE', 'PAPER'),
        "max_trade_usd":      float(os.environ.get('MAX_TRADE_USD', '0')),
        "pi_id":              PI_ID,
        "agent_running":      agent_running_info['agent'],
        "agent_running_secs": agent_running_info.get('age_secs', 0),
        # R10-4 — normal path (get_system_status) returns user_warnings;
        # omitting it here breaks any JS reading data.user_warnings.length
        # when the agent happens to be running at fetch time.
        "user_warnings":      [],
        "admin_overrides": {
            "trading_gate":   os.environ.get('ADMIN_TRADING_GATE', 'ALL'),
            "operating_mode": os.environ.get('ADMIN_OPERATING_MODE', 'ALL'),
        },
    }


def _compute_user_warnings(enriched, db):
    """Compute user-facing warnings about cap utilization and cash starvation.
    Returns list of warning dicts.
    """
    warnings = []
    _auto_n = sum(1 for p in enriched if (p.get('managed_by') or 'bot') == 'bot')
    _user_n = sum(1 for p in enriched if (p.get('managed_by') or 'bot') == 'user')
    if (_auto_n + _user_n) >= 1 and _auto_n < max(2, int(AUTO_USER_POSITION_CAP * 0.4)):
        warnings.append({
            'type': 'cap_underutilized',
            'severity': 'info',
            'message': (f"Bot is only managing {_auto_n}/{AUTO_USER_POSITION_CAP} positions. "
                        f"Promote USER positions to AUTO below if you want more bot coverage."),
        })
    try:
        with db.conn() as _c:
            _skip_row = _c.execute(
                "SELECT COUNT(*) AS n FROM signal_decisions "
                "WHERE action = 'SKIP_INSUFFICIENT_CASH_AFTER_MANUAL' "
                "AND ts >= datetime('now', '-7 days')"
            ).fetchone()
            _skip_count = int(_skip_row['n']) if _skip_row else 0
    except Exception:
        _skip_count = 0
    if _skip_count >= 2:
        warnings.append({
            'type': 'cash_starved',
            'severity': 'warn',
            'message': (f"Bot skipped {_skip_count} signals in the last 7 days because "
                        f"there wasn't enough cash after your manual positions. "
                        f"Closing a USER position or adding funds would restore bot coverage."),
        })
    return warnings


def get_system_status():
    """Read live status from database, enriched with Alpaca real-time prices."""
    import time as _time

    _agent_running = _read_agent_running()
    if _agent_running:
        log.debug(f"Agent running: {_agent_running['agent']} — using DB-only data (no Alpaca enrichment)")
        try:
            db = _customer_db()
            return _status_locked_mode(db, _agent_running)
        except Exception as e:
            log.warning(f"Lock-mode status read failed: {e}")

    for attempt in range(3):
        try:
            db = _customer_db()
            portfolio  = db.get_portfolio()
            positions  = db.get_open_positions()
            last_hb    = db.get_last_heartbeat()
            flags      = db.get_urgent_flags()

            alpaca_map           = _fetch_alpaca_positions()
            enriched, orphans    = _enrich_positions(positions, alpaca_map)
            all_positions        = enriched + orphans

            try:
                with db.conn() as _c:
                    _sticky_map = {
                        r['ticker']: r['sticky']
                        for r in _c.execute(
                            "SELECT ticker, sticky FROM position_preferences"
                        ).fetchall()
                    }
            except Exception:
                _sticky_map = {}
            for _p in all_positions:
                _p['sticky'] = _sticky_map.get(_p.get('ticker'))

            _user_warnings = _compute_user_warnings(enriched, db)

            enriched_flags = _enrich_flags([dict(f) for f in flags], db.path)
            critical_count = sum(1 for f in enriched_flags if f['tier'] == 1)

            market_value_total = sum(p['market_value'] for p in all_positions)
            total = round(portfolio['cash'] + market_value_total, 2)

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
                "user_warnings":    _user_warnings,
                "admin_overrides": {
                    "trading_gate":   os.environ.get('ADMIN_TRADING_GATE', 'ALL'),
                    "operating_mode": os.environ.get('ADMIN_OPERATING_MODE', 'ALL'),
                },
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
                "critical_flags":   0,
                "flags_detail":     [],
                "user_warnings":    [],
                "last_heartbeat":   "Unavailable",
                "kill_switch":      kill_switch_active(),
                "operating_mode":   OPERATING_MODE,
                "trading_mode":     os.environ.get('TRADING_MODE', 'PAPER'),
                "max_trade_usd":    float(os.environ.get('MAX_TRADE_USD', '0')),
                "pi_id":            PI_ID,
                "agent_running":    None,
                "admin_overrides": {
                    "trading_gate":   os.environ.get('ADMIN_TRADING_GATE', 'ALL'),
                    "operating_mode": os.environ.get('ADMIN_OPERATING_MODE', 'ALL'),
                },
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

# PORTAL_HTML extracted 2026-04-23 → src/templates/portal.html
# Tier 4 / final inline-HTML extraction on feat/portal-v2. Kept as full
# standalone document (not base.html-extending) because of 2 inline body
# <style> blocks and 71 non-Jinja {placeholder} JS tokens. Future-work:
# migrate to extend base.html once body-level CSS is consolidated.

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
        return render_template('landing.html')

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

    return render_template(
        'portal.html',
        status=skeleton_status,
        approvals=[],
        pending_count=0,
        operating_mode=operating_mode,
        pi_id=PI_ID,
        settings=settings,
        async_load=True,
        grace_warning=grace_warning,
        settings_ui_locked=SETTINGS_UI_LOCKED,
        # Early-access TOS + setup overlay wiring.  When the feature
        # flag is off (the default) these render as the inert shape
        # and nothing activates on the client side.
        ea_enabled=EARLY_ACCESS_TOS_ENABLED,
        ea_tos_html=_EA_TOS_HTML,
    )


@app.route('/api/set-mode', methods=['POST'])
@login_required
def api_set_mode():
    """Toggle between MANAGED (approve all trades) and AUTOMATIC (bot executes) per customer."""
    # Check admin override
    admin_mode = os.environ.get('ADMIN_OPERATING_MODE', 'ALL')
    if admin_mode != 'ALL':
        return jsonify({"ok": False, "locked": True, "forced_mode": admin_mode,
                        "error": "Operating mode is locked by administrator"}), 403
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', '').upper()
    if mode not in ('MANAGED', 'AUTOMATIC'):
        return jsonify({"ok": False, "error": "mode must be MANAGED or AUTOMATIC"}), 400
    customer_id = session.get('customer_id', 'admin')
    try:
        auth.set_operating_mode(customer_id, mode)
        _customer_db().set_setting('OPERATING_MODE', mode)
        log.info(f"Operating mode set to {mode} for customer {customer_id}")
        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        log.error(f"set-mode error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/admin/alert', methods=['POST'])
def api_admin_alert():
    """Tier-2 destination in the escalating-alert chain used by external
    watchdogs (pi2w_monitor). Writes a notification to the OWNER's
    per-customer DB with category='admin'. Authenticated via MONITOR_TOKEN
    bearer (same token scheme as /api/logs-audit).

    Payload (JSON):
        {
            "subject":  "short title",           # required
            "body":     "longer detail text",    # optional
            "priority": "normal|high|critical"   # optional, default 'normal'
        }

    Response: {"ok": true, "notification_id": <int>}
    """
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    auth_header   = request.headers.get('Authorization', '')
    x_token       = request.headers.get('X-Token', '')
    token_ok = bool(monitor_token and
                    (auth_header == f'Bearer {monitor_token}' or x_token == monitor_token))
    if not token_ok:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    subject  = (data.get('subject')  or '').strip()
    body     = (data.get('body')     or '').strip()
    priority = (data.get('priority') or 'normal').strip().lower()
    if not subject:
        return jsonify({'ok': False, 'error': 'subject required'}), 400

    # Write into OWNER customer's notifications table (operator = owner).
    owner_id = os.environ.get('OWNER_CUSTOMER_ID', '')
    if not owner_id:
        log.error("api_admin_alert: OWNER_CUSTOMER_ID env missing — cannot route")
        return jsonify({'ok': False, 'error': 'owner not configured'}), 500
    try:
        from retail_database import get_customer_db
        odb = get_customer_db(owner_id)
        nid = odb.add_notification(
            category='admin',
            title=subject[:120],
            body=body[:2000],
            meta={'source': 'external_watchdog', 'priority': priority},
        )
        log.info(f"[ADMIN_ALERT] wrote notification {nid}: {subject[:60]}")
        return jsonify({'ok': True, 'notification_id': nid})
    except Exception as e:
        log.error(f"api_admin_alert write failed: {e}")
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


@app.route('/api/admin-override', methods=['GET', 'POST'])
def api_admin_override():
    """Receive admin override push from monitor (POST) or return current
    state (GET).

    Auth model (security audit 2026-04-24, fix for CRITICAL-1):
      GET  → readable by any authenticated session OR by MONITOR_TOKEN.
             The override state is informational; legitimate customer
             UI may want to display "Trading Gate: PAPER (admin override)".
      POST → mutates global .env (TRADING_MODE, OPERATING_MODE, and
             forces every customer's mode). Restricted to MONITOR_TOKEN
             OR an authenticated admin session. Customer sessions alone
             are NOT sufficient — fixes the prior bug where any logged-in
             customer could flip the system to LIVE trading.
    """
    if request.method == 'GET':
        token = request.headers.get('X-Token', '')
        if token != MONITOR_TOKEN and not is_authenticated():
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({
            "trading_gate":   os.environ.get('ADMIN_TRADING_GATE', 'ALL'),
            "operating_mode": os.environ.get('ADMIN_OPERATING_MODE', 'ALL'),
        })

    # POST — verify token (monitor sends X-Token header) OR authenticated admin.
    # NOT customer-authenticated alone — prior buggy check allowed any customer
    # to mutate .env and force trading-mode flips.
    token = request.headers.get('X-Token', '')
    if token != MONITOR_TOKEN and not (is_authenticated() and is_admin()):
        log.warning(f"[ADMIN_OVERRIDE] POST denied — token_ok={token == MONITOR_TOKEN}, "
                    f"authed={is_authenticated()}, admin={is_admin()}, "
                    f"sid={(session.get('customer_id') or '')[:8]}")
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    trading_gate   = data.get('trading_gate', 'ALL').upper()
    operating_mode = data.get('operating_mode', 'ALL').upper()

    if trading_gate not in ('PAPER', 'LIVE', 'ALL'):
        return jsonify({"ok": False, "error": "trading_gate must be PAPER, LIVE, or ALL"}), 400
    if operating_mode not in ('MANAGED', 'AUTOMATIC', 'ALL'):
        return jsonify({"ok": False, "error": "operating_mode must be MANAGED, AUTOMATIC, or ALL"}), 400

    update_env('ADMIN_TRADING_GATE', trading_gate)
    update_env('ADMIN_OPERATING_MODE', operating_mode)
    os.environ['ADMIN_TRADING_GATE'] = trading_gate
    os.environ['ADMIN_OPERATING_MODE'] = operating_mode

    # If operating mode is forced, update all customers in auth.db + customer_settings
    if operating_mode != 'ALL':
        try:
            for cust in auth.list_customers():
                auth.set_operating_mode(cust['id'], operating_mode)
            log.info(f"Admin override: forced all customers to {operating_mode}")
        except Exception as e:
            log.warning(f"admin-override: could not update all customers: {e}")

    # If trading gate is forced, update .env TRADING_MODE
    if trading_gate != 'ALL':
        update_env('TRADING_MODE', trading_gate)
        os.environ['TRADING_MODE'] = trading_gate

    log.info(f"Admin override received: trading_gate={trading_gate} operating_mode={operating_mode}")
    return jsonify({"ok": True})


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
    update_env('OPERATING_MODE', 'AUTOMATIC')
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
    """Update API keys.

    Auth model (security audit 2026-04-24, fix for CRITICAL-2):
      Per-customer Alpaca credentials → encrypted to that customer's
        auth.db row. Writable by the customer themselves OR by a
        MONITOR_TOKEN bearer.
      Global .env keys (LIVE_TRADING_ENABLED, ANTHROPIC_API_KEY,
        MONITOR_TOKEN, RESEND_API_KEY, etc.) → require MONITOR_TOKEN
        OR an authenticated admin. Customer sessions alone are NOT
        sufficient. Prior bug let any customer rewrite global secrets
        and flip live trading.
    """
    auth_header   = request.headers.get('Authorization', '')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    token_ok = bool(monitor_token and auth_header == f'Bearer {monitor_token}')
    if not token_ok and not session.get('customer_id'):
        return jsonify({'ok': False, 'updated': [], 'errors': ['Not authenticated']}), 401
    data = request.get_json(silent=True) or {}

    # Alpaca credentials go to auth.db (encrypted), not .env. These are
    # legitimately customer-writable (each customer manages their own
    # broker credentials).
    ALPACA_KEYS = {'ALPACA_API_KEY', 'ALPACA_SECRET_KEY'}

    # Whitelist of keys that write to .env. Admin-only or token-only —
    # see top-of-function auth model. Includes LIVE_TRADING_ENABLED,
    # MONITOR_TOKEN, ANTHROPIC_API_KEY, RESEND_API_KEY, the Stripe keys,
    # and the trading-mode levers — all of which affect every customer
    # and the operator's billing surface.
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
        'ADMIN_TRADING_GATE',
        'ADMIN_OPERATING_MODE',
        'STRIPE_SECRET_KEY',
        'STRIPE_WEBHOOK_SECRET',
        'STRIPE_PRICE_ID',
        'STRIPE_EARLY_ADOPTER_PRICE_ID',
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

    # ── ADMIN-ONLY GATE for .env writes ───────────────────────────────
    # Any remaining keys after the Alpaca-credentials pop above will go
    # to global .env. Restrict to monitor-token bearers OR admin sessions.
    # Customer sessions are blocked from .env writes — fixes CRITICAL-2
    # from 2026-04-24 security audit. We separate this from the Alpaca
    # path so a customer with no admin role can still update their own
    # broker credentials.
    env_write_allowed = token_ok or (is_authenticated() and is_admin())
    env_keys_attempted = [k for k in data.keys() if k in ALLOWED_KEYS]
    if env_keys_attempted and not env_write_allowed:
        log.warning(
            f"[KEYS] Customer {(session.get('customer_id') or '?')[:8]} attempted "
            f"to write {len(env_keys_attempted)} global .env key(s): "
            f"{env_keys_attempted} — denied (admin only)"
        )
        for k in env_keys_attempted:
            errors.append(f"{k}: admin only")
            data.pop(k, None)

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
    # Min length 12 to match setup_account flow. Standardized 2026-04-25
    # (security audit Phase 2.5 MED-4 — was 8). OWASP 2023 recommends
    # ≥12 for non-MFA accounts.
    if len(new_pw) < 12:
        return jsonify({'ok': False, 'error': 'New password must be at least 12 characters'})
    try:
        auth.update_password(customer_id, current_pw, new_pw)
        # MED-1: invalidate all OTHER active sessions for this customer.
        # The current session is preserved by re-stamping logged_in_at
        # AFTER the revocation timestamp — see comment below.
        _revoke_customer_sessions(customer_id, reason='password_change')
        # Re-stamp the active session's logged_in_at so this same session
        # survives the revocation check (the customer doesn't want to be
        # logged out of the tab they just used to change their password).
        # Other tabs/devices have older logged_in_at values, so they get
        # force-logged-out on their next request.
        session['logged_in_at'] = datetime.now(timezone.utc).isoformat()
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'ok': False, 'error': 'Server error'})


@app.route('/api/account/change-email', methods=['POST'])
@login_required
def api_change_email():
    """Initiate email change — TWO-STEP secure flow (Phase 2.5 MED-2 + MED-3,
    rewritten 2026-04-25).

    Step 1 (this endpoint): verify password, validate new email, create a
    pending_email_changes row with a 1h-TTL verification token. Send:
      • verification email to NEW address (must be clicked to commit change)
      • alert email to OLD address ("if this wasn't you, ignore the
        verification — your account email has not been changed")

    Step 2 (the /verify-email-change/<token> route): clicking the link
    in the new-address email actually commits the email change to
    customers.email_hash + email_enc.

    The customer's email field stays as the OLD address until step 2
    is completed. If they never click, the change times out at 1h and
    the row gets cleaned up.
    """
    data        = request.get_json(silent=True) or {}
    current_pw  = data.get('current_password', '').strip()
    new_email   = data.get('new_email', '').strip()
    customer_id = session.get('customer_id', '')
    if not current_pw or not new_email:
        return jsonify({'ok': False, 'error': 'All fields are required'})
    if '@' not in new_email or '.' not in new_email.split('@')[-1]:
        return jsonify({'ok': False, 'error': 'Invalid email address'})
    try:
        result = auth.initiate_email_change(customer_id, current_pw, new_email)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    except Exception as e:
        log.error(f"initiate_email_change failed: {e}")
        return jsonify({'ok': False, 'error': 'Server error'})

    token       = result['token']
    old_email   = result['old_email']
    new_addr    = result['new_email']
    portal_dom  = os.environ.get('PORTAL_DOMAIN', 'portal.synth-cloud.com')
    verify_url  = f"https://{portal_dom}/verify-email-change/{token}"
    resend_key  = os.environ.get('RESEND_API_KEY', '')
    alert_from  = os.environ.get('ALERT_FROM', 'alerts@synth-cloud.com')

    if not resend_key:
        log.warning("RESEND_API_KEY not set — email-change emails NOT sent. "
                    f"Verification link for testing: {verify_url}")
        # Without Resend we still confirm the request was accepted; the
        # operator running locally can pull the token from the log.
        return jsonify({'ok': True,
                        'message': 'Request accepted. Check your new email '
                                   'inbox for the verification link.',
                        'email_sent': False})

    import requests as _req
    sent_new = sent_old = False

    # ── Verification email to NEW address (MED-3) ────────────────────
    try:
        _req.post("https://api.resend.com/emails", json={
            "from":    f"Synthos <{alert_from}>",
            "to":      [new_addr],
            "subject": "Synthos — Confirm your new email address",
            "html": (
                f"<p>You requested to change your Synthos account email to this address.</p>"
                f"<p><a href=\"{verify_url}\" style=\"display:inline-block;padding:12px 24px;"
                f"background:#00f5d4;color:#000;text-decoration:none;border-radius:8px;"
                f"font-weight:bold\">Confirm New Email</a></p>"
                f"<p style=\"color:#888;font-size:12px\">This link expires in 1 hour. "
                f"If you did not request this, ignore this email and your account email will "
                f"remain unchanged.</p>"
            ),
        }, headers={"Authorization": f"Bearer {resend_key}"}, timeout=10)
        sent_new = True
    except Exception as e:
        log.warning(f"Email-change verify mail to NEW address failed: {e}")

    # ── Alert email to OLD address (MED-2) ───────────────────────────
    try:
        _req.post("https://api.resend.com/emails", json={
            "from":    f"Synthos <{alert_from}>",
            "to":      [old_email],
            "subject": "Synthos — Email change request on your account",
            "html": (
                f"<p>An email-change request was just submitted on your Synthos account.</p>"
                f"<p>Requested new address: <code>{new_addr}</code></p>"
                f"<p><b>If this was you</b>, check your new inbox and click the verification "
                f"link there. Your account email stays at this address until you do.</p>"
                f"<p><b>If this was NOT you</b>, your account is fine — no email change has "
                f"happened yet, and the verification link will expire in 1 hour. We strongly "
                f"recommend you sign in and change your password immediately, since the request "
                f"required a valid password to submit.</p>"
                f"<p style=\"color:#888;font-size:12px\">This is an automated security notice "
                f"from Synthos. The pending request will time out automatically if not confirmed.</p>"
            ),
        }, headers={"Authorization": f"Bearer {resend_key}"}, timeout=10)
        sent_old = True
    except Exception as e:
        log.warning(f"Email-change alert mail to OLD address failed: {e}")

    log.info(f"[EMAIL_CHANGE] customer {customer_id[:8]}… initiated change "
             f"(verify mail to NEW: {sent_new}, alert mail to OLD: {sent_old})")
    return jsonify({
        'ok': True,
        'message': 'Verification email sent. Check your new email inbox '
                   'and click the link to complete the change. Your '
                   'current account email has also been notified.',
        'email_sent': sent_new and sent_old,
    })


@app.route('/verify-email-change/<token>')
def verify_email_change(token):
    """Step 2 of email-change flow. Visiting this URL (typically from
    the verification email) confirms the new address and commits the
    change to customers.email_hash + email_enc.

    Logged in or not — the token is the auth. Customer can be on a
    different device than the one that initiated.

    Side effects:
      • customers.email_hash + email_enc updated to new address
      • pending_email_changes.consumed_at set
      • If currently logged in as the affected customer, session
        keeps working but the displayed email reflects the new value
        on next page load
    """
    try:
        customer_id = auth.confirm_email_change(token)
    except ValueError as e:
        return render_template('verify_error.html',
                               error_msg=str(e)), 400
    except Exception as e:
        log.error(f"confirm_email_change failed: {e}")
        return render_template('verify_error.html',
                               error_msg="An unexpected error occurred."), 500

    log.info(f"[EMAIL_CHANGE] customer {customer_id} email change confirmed")
    return render_template('verify_success.html')

# ══════════════════════════════════════════════════════════════════════════
# EARLY-ACCESS TOS + SETUP OVERLAY — API ENDPOINTS (DORMANT)
# ─────────────────────────────────────────────────────────────────────────
# All three endpoints short-circuit to 404/no-op when the feature flag is
# off, so a stray client-side call can't accidentally touch state.
# ══════════════════════════════════════════════════════════════════════════
@app.route('/api/ea/status')
@authenticated_only  # deliberately NOT @login_required — modal renders
                     # over dashboard, but server must not insist on
                     # legacy tos_version being current when the new
                     # modal is the supersession mechanism.
def api_ea_status():
    """Read-only state for the dashboard bootstrap. Safe when disabled."""
    if not EARLY_ACCESS_TOS_ENABLED:
        return jsonify(_ea_status(None)), 200
    try:
        cdb = _customer_db()
    except Exception as e:
        log.error(f"ea/status: db error: {e}")
        return jsonify({"enabled": True, "error": "db"}), 500
    return jsonify(_ea_status(cdb)), 200


@app.route('/api/ea/accept-tos', methods=['POST'])
@authenticated_only
def api_ea_accept_tos():
    """Record acceptance of the current early-access TOS. Idempotent."""
    if not EARLY_ACCESS_TOS_ENABLED:
        return jsonify({"ok": False, "error": "disabled"}), 404
    try:
        cdb = _customer_db()
        if _ea_is_fixture(cdb):
            # Fixtures never reach the modal in normal flow; accept this
            # path defensively so a replay doesn't 500.
            return jsonify({"ok": True, "fixture": True}), 200
        from datetime import datetime, timezone
        cdb.set_setting(EA_TOS_ACCEPTED_KEY,    EARLY_ACCESS_TOS_VERSION)
        cdb.set_setting(EA_TOS_ACCEPTED_AT_KEY, datetime.now(timezone.utc).isoformat())
        # Mirror into the session so the legacy login_required gate, which
        # checks session['tos_version'] vs TOS_CURRENT_VERSION, treats the
        # new acceptance as sufficient — i.e. this TOS supersedes /terms.
        session['tos_version'] = TOS_CURRENT_VERSION
        try:
            cdb.log_event(
                'EA_TOS_ACCEPTED',
                agent='portal',
                details=f"version={EARLY_ACCESS_TOS_VERSION}",
            )
        except Exception:
            pass
        return jsonify({"ok": True, "version": EARLY_ACCESS_TOS_VERSION}), 200
    except Exception as e:
        log.error(f"ea/accept-tos: {e}")
        return jsonify({"ok": False, "error": "save_failed"}), 500


@app.route('/api/ea/hide-setup', methods=['POST'])
@authenticated_only
def api_ea_hide_setup():
    """Persistently dismiss the non-restrictive setup overlay. The 'OK'
    button on the overlay does NOT call this — it only closes the overlay
    client-side for the current session. Only the 'Don't show again'
    checkbox + OK combination triggers this write."""
    if not EARLY_ACCESS_TOS_ENABLED:
        return jsonify({"ok": False, "error": "disabled"}), 404
    try:
        cdb = _customer_db()
        cdb.set_setting(EA_SETUP_HIDDEN_KEY, "1")
        return jsonify({"ok": True}), 200
    except Exception as e:
        log.error(f"ea/hide-setup: {e}")
        return jsonify({"ok": False, "error": "save_failed"}), 500
# ══════════════════════════════════════════════════════════════════════════


@app.route('/api/settings', methods=['POST'])
@login_required
def api_settings():
    """Save trading settings to per-customer DB (customer_settings table).
    Falls through to global .env only for system-level keys."""
    data = request.get_json(silent=True) or {}

    # ── SETTINGS UI LOCK ──────────────────────────────────────────────
    # During the tier-calibration experiment the slide-out is hidden for
    # all users. Writes are rejected here too so a stale tab or a hand-
    # crafted request can't bypass the UI lock. `setup_complete` is the
    # only key we allow — it's just a "dismiss setup guide" flag that
    # doesn't touch trading parameters.
    if SETTINGS_UI_LOCKED:
        allowed = {'setup_complete'}
        illegal = [k for k in data.keys() if k not in allowed]
        if illegal:
            log.info(f"[SETTINGS_LOCK] Rejected write from {session.get('customer_id','?')[:8]}: "
                     f"keys={illegal}")
            return jsonify({
                'ok': False,
                'locked': True,
                'error': 'Trading settings are locked by administrator during calibration.',
            }), 403

    # Per-customer trading params → customer_settings table.
    # Note: max_positions is deliberately NOT customer-settable; it's
    # determined by portfolio equity tier (see PORTFOLIO_TIERS in the
    # trade logic agent). Any incoming max_positions is dropped silently.
    customer_keys = {
        'min_confidence':     'MIN_CONFIDENCE',
        'max_position_pct':   'MAX_POSITION_PCT',
        'max_trade_usd':      'MAX_TRADE_USD',
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
        'notification_preference': 'NOTIFICATION_PREFERENCE',
    }
    try:
        db = _customer_db()
        written = []
        # Strip admin-locked fields before saving
        if os.environ.get('ADMIN_TRADING_GATE', 'ALL') != 'ALL' and 'trading_mode' in data:
            del data['trading_mode']
        if os.environ.get('ADMIN_OPERATING_MODE', 'ALL') != 'ALL' and 'operating_mode' in data:
            del data['operating_mode']
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




@app.route('/api/portfolio-info')
@login_required
def api_portfolio_info():
    """Portfolio info with notifications for the portfolio modal."""
    try:
        db = _customer_db()
        cid = session.get('customer_id', '')

        # Last equity update timestamp
        last_eq_ts = None
        with db.conn() as c:
            row = c.execute("SELECT updated_at FROM customer_settings WHERE key='_ALPACA_EQUITY'").fetchone()
            if row:
                last_eq_ts = row['updated_at'] if hasattr(row, 'keys') else row[0]

        # Last heartbeat
        hb = db.get_last_heartbeat()
        last_hb = hb['timestamp'] if hb else None

        # Account created_at from auth
        account_created = None
        try:
            with auth._auth_conn() as c:
                arow = c.execute("SELECT created_at FROM customers WHERE id=?", (cid,)).fetchone()
                if arow:
                    account_created = arow['created_at'] if hasattr(arow, 'keys') else arow[0]
        except Exception:
            pass

        # Current equity
        equity_str = db.get_setting('_ALPACA_EQUITY')
        equity = float(equity_str) if equity_str else 0.0

        # NEW_CUSTOMER flag
        nc = db.get_setting('NEW_CUSTOMER')
        new_customer = nc != 'false'

        # Has Alpaca keys?
        has_keys = False
        try:
            ak, sk = auth.get_alpaca_credentials(cid)
            has_keys = bool(ak and sk)
        except Exception:
            pass

        # Build notifications
        notifications = []

        if has_keys and equity < 1.0 and new_customer:
            # Check if account is old enough (2+ market days)
            stale_paper = False
            if account_created:
                try:
                    from datetime import datetime, timezone
                    created_dt = datetime.fromisoformat(account_created.replace('Z', '+00:00')) if 'T' in account_created else datetime.strptime(account_created, '%Y-%m-%d %H:%M:%S')
                    now = datetime.now(timezone.utc) if created_dt.tzinfo else datetime.now(timezone.utc).replace(tzinfo=None)
                    age_hours = (now - created_dt).total_seconds() / 3600
                    # 2 market days ~ 48+ hours (conservative: use 36 hours to account for weekends)
                    if age_hours >= 36:
                        stale_paper = True
                except Exception:
                    stale_paper = True  # If we can't parse date, err on side of showing warning

            if stale_paper:
                notifications.append({
                    "level": "warning",
                    "title": "Paper Account Not Funded",
                    "message": "Your Alpaca paper account still shows $0 after 2+ market days. "
                               "This is a known issue with new paper accounts that didn\u0027t initialize properly. "
                               "You need to create a new paper account on alpaca.markets and generate new API keys, "
                               "then update them in Settings \u2192 Alpaca Keys."
                })
            else:
                notifications.append({
                    "level": "info",
                    "title": "Waiting for Paper Funding",
                    "message": "Your Alpaca paper account is being set up. Initial $100,000 funding "
                               "typically appears within 1\u20132 market days. If it hasn\u0027t arrived after 2 days, "
                               "you may need to create a new paper account."
                })

        elif has_keys and equity < 1.0 and not new_customer:
            # Was funded before but now shows $0 — different issue
            notifications.append({
                "level": "error",
                "title": "Account Equity at $0",
                "message": "Your account previously had equity but is now showing $0. "
                           "This may indicate an API issue. Check your Alpaca dashboard or contact support."
            })

        elif not has_keys:
            notifications.append({
                "level": "info",
                "title": "Connect Your Alpaca Account",
                "message": "To start trading, connect your Alpaca paper trading account in the setup tutorial. "
                           "Go to Settings \u2192 Alpaca Keys to enter your API credentials."
            })

        # Kill switch warning
        ks = db.get_setting('KILL_SWITCH')
        if ks == '1':
            notifications.append({
                "level": "error",
                "title": "Trading Halted",
                "message": "The kill switch is active. All trading has been paused. "
                           "Go to Settings to re-enable trading."
            })

        return jsonify({
            "last_equity_update": last_eq_ts,
            "last_heartbeat": last_hb,
            "equity": equity,
            "has_keys": has_keys,
            "new_customer": new_customer,
            "account_created": account_created,
            "notifications": notifications,
        })
    except Exception as e:
        log.error(f"portfolio-info error: {e}")
        return jsonify({"error": str(e), "notifications": []}), 500


@app.route('/api/alpaca-funding-status')
@login_required
def api_alpaca_funding_status():
    """Check whether the customer's Alpaca paper account is funded."""
    try:
        cid = session.get('customer_id')
        ak, sk = auth.get_alpaca_credentials(cid)
        if not ak or not sk:
            return jsonify({"has_keys": False, "equity": 0, "funded": False, "status": "no_keys"})
        import requests as _req
        try:
            resp = _req.get(
                'https://paper-api.alpaca.markets/v2/account',
                headers={'APCA-API-KEY-ID': ak, 'APCA-API-SECRET-KEY': sk},
                timeout=8
            )
            resp.raise_for_status()
            acct = resp.json()
            equity = float(acct.get('equity', 0))
            funded = equity >= 1.0
            # Check NEW_CUSTOMER flag from DB
            db = _customer_db()
            nc = db.get_setting('NEW_CUSTOMER')
            new_customer = nc != 'false'  # true if not set or 'true'
            return jsonify({"has_keys": True, "equity": equity, "funded": funded, "new_customer": new_customer, "status": "ok"})
        except Exception:
            return jsonify({"has_keys": True, "equity": 0, "funded": False, "status": "api_error"})
    except Exception as e:
        log.error(f"alpaca-funding-status error: {e}")
        return jsonify({"has_keys": False, "equity": 0, "funded": False, "status": "error"})


@app.route('/api/customer-settings')
@login_required
def api_customer_settings():
    """Read per-customer settings with global .env fallback."""
    try:
        db = _customer_db()
        customer = db.get_all_settings()

        # Global defaults from .env
        global_defaults = {
            'OPERATING_MODE':     os.environ.get('OPERATING_MODE', 'MANAGED'),
            'MIN_CONFIDENCE':     os.environ.get('MIN_CONFIDENCE', 'LOW'),
            'MAX_POSITION_PCT':   os.environ.get('MAX_POSITION_PCT', '0.10'),
            'MAX_TRADE_USD':      os.environ.get('MAX_TRADE_USD', '1000'),
            # MAX_POSITIONS intentionally omitted — tier-based via PORTFOLIO_TIERS.
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

        # Portfolio tier — source of truth for Max Positions cap (not customer-settable).
        # Mirror of PORTFOLIO_TIERS in retail_trade_logic_agent.py.
        try:
            portfolio = db.get_portfolio()
            positions = db.get_open_positions()
            equity = float(portfolio.get('cash', 0))
            for p in positions:
                equity += float(p.get('current_price') or p.get('entry_price') or 0) * float(p.get('shares') or 0)
            # Tier thresholds — keep in sync with retail_trade_logic_agent.PORTFOLIO_TIERS
            _tiers = [
                (50000,  'Mature',  12),
                (20000,  'Scaled',  10),
                (5000,   'Growth',   8),
                (1000,   'Early',    5),
                (0,      'Seed',     3),
            ]
            tier_label, tier_max = 'Seed', 3
            for threshold, label, cap in _tiers:
                if equity >= threshold:
                    tier_label, tier_max = label, cap
                    break
            merged['PORTFOLIO_TIER'] = tier_label
            merged['PORTFOLIO_TIER_MAX_POSITIONS'] = tier_max
            merged['PORTFOLIO_EQUITY'] = round(equity, 2)
        except Exception:
            merged['PORTFOLIO_TIER'] = 'Seed'
            merged['PORTFOLIO_TIER_MAX_POSITIONS'] = 3

        return jsonify(merged)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/status')
@login_required
def api_status():
    return jsonify(get_system_status())


@app.route('/api/dashboard-data')
@login_required
def api_dashboard_data():
    """Single-shot data feed for the new dashboard cards (added 2026-04-25):
      • Market Regime Strip — macro regime + market-state score + validator
        verdict + last decision time + SPY day %
      • Today's Flow Strip — entries / exits / decisions / day P&L / vs SPY
      • Idle-reason card (conditional) — only populated when bot has been
        idle ≥30 min during market hours; null otherwise

    Designed to be polled on the same cadence as /api/status (~30 s). All
    queries are read-only and degrade gracefully — missing settings or
    empty tables return defaults so the frontend never crashes.
    """
    db     = _customer_db()
    shared = _shared_db()
    today_iso = datetime.now(ET).strftime('%Y-%m-%d')

    # ── Regime data ──────────────────────────────────────────────
    macro_regime       = shared.get_setting('_MACRO_REGIME')       or 'NORMAL'
    market_state       = shared.get_setting('_MARKET_STATE')       or 'OPEN'
    try:
        market_state_score = float(shared.get_setting('_MARKET_STATE_SCORE') or 0)
    except (TypeError, ValueError):
        market_state_score = 0.0
    validator_verdict  = (db.get_setting('_VALIDATOR_VERDICT')     or 'GO').upper()

    # SPY day % — pull from live_prices if SPY is being polled, else
    # leave at None and let the frontend show '—'. Avoids an extra
    # Alpaca call on every dashboard refresh.
    spy_pct = None
    try:
        with shared.conn() as c:
            row = c.execute(
                "SELECT day_change_pct FROM live_prices WHERE ticker = 'SPY'"
            ).fetchone()
            if row and row['day_change_pct'] is not None:
                spy_pct = round(float(row['day_change_pct']), 2)
    except Exception:
        pass

    # Last decision time — most recent TRADE_DECISION row in system_log
    last_decision_at = None
    try:
        with db.conn() as c:
            r = c.execute(
                "SELECT timestamp FROM system_log WHERE event = 'TRADE_DECISION' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if r:
                last_decision_at = r['timestamp']
    except Exception:
        pass

    # ── Today's flow ─────────────────────────────────────────────
    today_entries = today_exits = today_decisions = 0
    try:
        with db.conn() as c:
            today_entries = c.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE date(opened_at) = ?",
                (today_iso,)
            ).fetchone()['n'] or 0
            today_exits = c.execute(
                "SELECT COUNT(*) AS n FROM outcomes WHERE date(created_at) = ?",
                (today_iso,)
            ).fetchone()['n'] or 0
            today_decisions = c.execute(
                "SELECT COUNT(*) AS n FROM system_log "
                "WHERE event = 'TRADE_DECISION' AND date(timestamp) = ?",
                (today_iso,)
            ).fetchone()['n'] or 0
    except Exception as e:
        log.debug(f"dashboard-data today queries failed: {e}")

    # Day P&L — already computed by the same path as /api/status. Re-derive
    # here so the frontend doesn't have to combine two endpoints.
    day_pl = None
    try:
        st = get_system_status()
        positions = st.get('positions', [])
        day_pl = round(sum(float(p.get('day_pl', 0) or 0) for p in positions), 2)
    except Exception:
        pass

    # vs SPY (today) — operator's day return % minus SPY's day %.
    # Operator return = day_pl / (portfolio_value - day_pl). Computed
    # client-side too in the existing UI; re-derived here for the strip.
    vs_spy = None
    try:
        if day_pl is not None and spy_pct is not None and st:
            port_val = float(st.get('portfolio_value', 0) or 0)
            if port_val and (port_val - day_pl) > 0:
                day_pl_pct = (day_pl / (port_val - day_pl)) * 100
                vs_spy = round(day_pl_pct - spy_pct, 2)
    except Exception:
        pass

    # ── Idle-reason card ─────────────────────────────────────────
    # Only populated when bot has been idle ≥30 min during market hours.
    # Outside market hours: null (the card stays hidden client-side).
    idle = None
    try:
        IDLE_THRESHOLD_MIN = int(os.environ.get('DASHBOARD_IDLE_THRESHOLD_MIN', '30'))
        if is_market_hours_utc_now():
            # Find the most recent ENTRY/EXIT in system_log
            with db.conn() as c:
                r = c.execute(
                    "SELECT MAX(timestamp) AS ts FROM system_log "
                    "WHERE event IN ('TRADE_OPENED','TRADE_CLOSED','POSITION_OPENED','POSITION_CLOSED')"
                ).fetchone()
                last_action_at = r['ts'] if r else None
            idle_minutes = None
            if last_action_at:
                try:
                    last_dt = datetime.fromisoformat(
                        last_action_at.replace('Z','').replace(' ', 'T')[:19]
                    )
                    idle_minutes = (datetime.now() - last_dt).total_seconds() / 60.0
                except Exception:
                    idle_minutes = None
            if idle_minutes is None or idle_minutes >= IDLE_THRESHOLD_MIN:
                # Compute slot utilization
                portfolio = db.get_portfolio()
                equity = float(st.get('portfolio_value', 0) or 0) if st else float(portfolio.get('cash', 0) or 0)
                # Tier max_positions lookup (same constants as trade_logic_agent)
                _tiers = [
                    (0,      3),  (1000,   5),  (5000,   8),
                    (20000, 10),  (50000, 12),
                ]
                max_positions = next(
                    (mp for thr, mp in reversed(_tiers) if equity >= thr),
                    3
                )
                open_count = (st.get('open_positions', 0) if st else 0)
                # Validated-pool count
                validated = 0
                try:
                    with shared.conn() as c:
                        validated = c.execute(
                            "SELECT COUNT(*) AS n FROM signals WHERE status = 'VALIDATED'"
                        ).fetchone()['n'] or 0
                except Exception:
                    pass
                # Cooling-off list (active only)
                cooling = []
                try:
                    with db.conn() as c:
                        rows = c.execute(
                            "SELECT ticker, cool_until, reason FROM cooling_off "
                            "WHERE cool_until > ? ORDER BY cool_until ASC LIMIT 10",
                            (datetime.now(timezone.utc).isoformat(),)
                        ).fetchall()
                        cooling = [
                            {'ticker': r['ticker'], 'until': r['cool_until'], 'reason': r['reason']}
                            for r in rows
                        ]
                except Exception:
                    pass
                idle = {
                    'idle_minutes':    round(idle_minutes, 1) if idle_minutes is not None else None,
                    'validator':       validator_verdict,
                    'macro_regime':    macro_regime,
                    'open_positions':  open_count,
                    'max_positions':   max_positions,
                    'cash':            round(float(st.get('cash', 0) or 0), 2) if st else 0,
                    'validated_count': validated,
                    'cooling_off':     cooling,
                }
    except Exception as e:
        log.debug(f"dashboard-data idle computation failed: {e}")

    return jsonify({
        'regime': {
            'macro':              macro_regime,
            'market_state':       market_state,
            'market_state_score': market_state_score,
            'validator':          validator_verdict,
            'spy_pct':            spy_pct,
            'last_decision_at':   last_decision_at,
        },
        'today': {
            'entries':   today_entries,
            'exits':     today_exits,
            'decisions': today_decisions,
            'day_pl':    day_pl,
            'vs_spy':    vs_spy,
        },
        'idle': idle,
    })


@app.route('/api/sparklines')
@login_required
def api_sparklines():
    """Returns 12-trading-hours of 15-min bar closes per ticker, RTH-only.
    48 closes per ticker (12h × 4 bars/h). Last point is overlaid with the
    live price from live_prices for sub-15-min freshness.

    Backend: persistent SQLite cache table sparkline_bars(ticker, bar_ts,
    close). Top-up from Alpaca when stale (<5 min old serves from cache,
    older fetches missing range only). Designed to handle ticker churn
    cleanly: dropped tickers age out via 7-day prune; new tickers do one
    cold fetch then live on top-ups.

    Added 2026-04-25 — see docs/security_review.md for caching/eviction
    discussion.

    Query params:
      tickers — comma-separated, max 50

    Response:
      { "AMD": [347.10, 347.34, ..., 347.81], "AMZN": [...], ... }
      Closing prices in chronological order. May be shorter than 48 if
      not enough RTH history exists yet (e.g., new ticker, weekend).
    """
    import requests as _req
    from datetime import timezone as _tz

    tickers_str = request.args.get('tickers', '')
    tickers = [t.strip().upper() for t in tickers_str.split(',') if t.strip()]
    tickers = list(dict.fromkeys(tickers))  # de-dupe preserving order
    if not tickers:
        return jsonify({})
    if len(tickers) > 50:
        return jsonify({'error': 'too many tickers (max 50)'}), 400

    shared = _shared_db()

    # ── Lazy-create the cache table on first call ────────────────
    try:
        with shared.conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS sparkline_bars (
                    ticker  TEXT NOT NULL,
                    bar_ts  TEXT NOT NULL,
                    close   REAL NOT NULL,
                    PRIMARY KEY (ticker, bar_ts)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_spark_ts ON sparkline_bars(bar_ts)")
    except Exception as e:
        log.warning(f"sparkline_bars table init failed: {e}")
        return jsonify({}), 500

    alpaca_data_url = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')
    # Use OWNER's Alpaca creds for the bar fetch — sparkline data is shared
    # market data (not customer-specific), so non-Alpaca-credentialed
    # customers should still see sparklines on the dashboard.
    alpaca_key    = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    if not alpaca_key:
        # No owner creds — best-effort: return what's in cache
        log.debug("sparklines: no owner Alpaca creds, serving from cache only")

    # RTH check helper — Alpaca bars are timestamped UTC ISO strings.
    # Convert to ET and check 9:30 <= time < 16:00 on weekday.
    try:
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI('America/New_York')
    except Exception:
        _ET = ET

    def _is_rth(iso_ts):
        try:
            dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            et = dt.astimezone(_ET)
            if et.weekday() > 4:  # weekend
                return False
            mins = et.hour * 60 + et.minute
            # 9:30 = 570, 16:00 = 960
            return 570 <= mins < 960
        except Exception:
            return False

    # ── Fetch + cache helper ─────────────────────────────────────
    def _topup_from_alpaca(ticker, since_iso=None):
        """Pull bars from Alpaca and INSERT into sparkline_bars. since_iso
        cuts the start time so we only fetch missing range. Returns count
        of new rows inserted (0 if no creds or API error)."""
        if not alpaca_key:
            return 0
        # Window: 36 calendar hours covers ~12 RTH hours plus weekend buffer.
        # If since_iso provided, start from there but cap at 36h ago.
        end_dt   = datetime.now(timezone.utc).replace(microsecond=0)
        start_dt = end_dt - timedelta(hours=36)
        if since_iso:
            try:
                hint = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
                if hint.tzinfo is None:
                    hint = hint.replace(tzinfo=timezone.utc)
                # Top-up: start a bit BEFORE since_iso to handle clock skew
                start_dt = max(start_dt, hint - timedelta(minutes=15))
            except Exception:
                pass
        try:
            r = _req.get(
                f"{alpaca_data_url}/v2/stocks/bars",
                headers={'APCA-API-KEY-ID': alpaca_key, 'APCA-API-SECRET-KEY': alpaca_secret},
                params={
                    'symbols':   ticker,
                    'timeframe': '15Min',
                    'start':     start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'end':       end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'limit':     200,
                    'feed':      'iex',
                },
                timeout=8,
            )
            if r.status_code != 200:
                log.debug(f"sparklines fetch {ticker}: {r.status_code}")
                return 0
            bars = (r.json() or {}).get('bars', {}).get(ticker, []) or []
            if not bars:
                return 0
            rows = [
                (ticker, b['t'], float(b['c']))
                for b in bars
                if 't' in b and 'c' in b
            ]
            with shared.conn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO sparkline_bars (ticker, bar_ts, close) "
                    "VALUES (?, ?, ?)",
                    rows
                )
                # Prune anything older than 7 days at the same time — bounded growth.
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
                c.execute("DELETE FROM sparkline_bars WHERE bar_ts < ?", (cutoff,))
            return len(rows)
        except Exception as e:
            log.debug(f"sparklines fetch {ticker} error: {e}")
            return 0

    # ── Per-ticker logic ─────────────────────────────────────────
    NEEDED_BARS  = 48                 # 12 trading hours × 4 bars/hour
    FRESH_WINDOW = timedelta(minutes=15)
    FETCH_AFTER  = timedelta(minutes=5)
    now_utc      = datetime.now(timezone.utc)
    cutoff_db    = (now_utc - timedelta(hours=36)).strftime('%Y-%m-%dT%H:%M:%SZ')

    result = {}
    for ticker in tickers:
        # Read existing cached bars
        try:
            with shared.conn() as c:
                rows = c.execute(
                    "SELECT bar_ts, close FROM sparkline_bars "
                    "WHERE ticker = ? AND bar_ts >= ? "
                    "ORDER BY bar_ts ASC",
                    (ticker, cutoff_db)
                ).fetchall()
        except Exception:
            rows = []

        # Decide whether to top up from Alpaca
        latest_ts = rows[-1]['bar_ts'] if rows else None
        need_fetch = False
        if not rows:
            need_fetch = True   # cold cache
        else:
            try:
                latest_dt = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=timezone.utc)
                age = now_utc - latest_dt
                # Only refetch if (a) latest bar is at least 5 min old AND
                # (b) more than 15 min has passed since we'd expect a new
                # bar. Avoids hammering Alpaca on rapid dashboard refreshes.
                if age > FRESH_WINDOW + FETCH_AFTER:
                    need_fetch = True
            except Exception:
                need_fetch = True

        if need_fetch:
            _topup_from_alpaca(ticker, since_iso=latest_ts)
            try:
                with shared.conn() as c:
                    rows = c.execute(
                        "SELECT bar_ts, close FROM sparkline_bars "
                        "WHERE ticker = ? AND bar_ts >= ? "
                        "ORDER BY bar_ts ASC",
                        (ticker, cutoff_db)
                    ).fetchall()
            except Exception:
                pass

        # Filter to RTH and take last NEEDED_BARS
        rth = [(r['bar_ts'], float(r['close'])) for r in rows if _is_rth(r['bar_ts'])]
        rth = rth[-NEEDED_BARS:]

        # Overlay live tail from live_prices (sub-15-min freshness)
        try:
            with shared.conn() as c:
                lp = c.execute(
                    "SELECT price, updated_at FROM live_prices WHERE ticker = ?",
                    (ticker,)
                ).fetchone()
            if lp and lp['price']:
                live_price = float(lp['price'])
                if rth:
                    rth[-1] = (rth[-1][0], live_price)
                else:
                    rth = [(now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), live_price)]
        except Exception:
            pass

        result[ticker] = [round(close, 4) for _, close in rth]

    return jsonify(result)


@app.route('/api/approvals')
@login_required
def api_approvals():
    data = load_pending_approvals()
    log.info(f'[DEBUG] api_approvals returning {len(data)} items, customer_id={session.get("customer_id", "MISSING")}')
    return jsonify(data)


@app.route('/api/pending')
@login_required
def api_pending():
    """Dashboard pending-decisions card data source.

    Returns the subset of pending_approvals rows that represent
    'decisions the system is about to make / made and is re-checking':

      - PENDING_APPROVAL   — supervised mode: user needs to approve/reject
      - QUEUED_FOR_OPEN    — automatic mode: waiting for pre-open re-eval
      - APPROVED           — re-eval passed or user approved, awaiting exec

    Also returns recent CANCELLED_PROTECTIVE rows in a separate field so
    the card can show "we killed these before they ran" with the reason.

    Shape:
      {
        "active":     [row, ...],   // PENDING_APPROVAL + QUEUED_FOR_OPEN + APPROVED
        "cancelled":  [row, ...],   // CANCELLED_PROTECTIVE in last 14d
        "operating_mode": "AUTOMATIC" | "MANAGED" | "SUPERVISED"
      }
    """
    try:
        db   = _customer_db()
        mode = (db.get_setting('OPERATING_MODE') or 'AUTOMATIC').upper()
        all_rows = db.get_pending_approvals()
        active = [r for r in all_rows
                  if r.get('status') in ('PENDING_APPROVAL',
                                         'QUEUED_FOR_OPEN',
                                         'APPROVED')]
        cancelled = db.get_cancelled_protective(since_days=14)
        return jsonify({
            "active":         active,
            "cancelled":      cancelled,
            "operating_mode": mode,
        })
    except Exception as e:
        log.error(f"/api/pending error: {e}")
        return jsonify({"active": [], "cancelled": [],
                        "operating_mode": "AUTOMATIC",
                        "error": str(e)}), 500


# ── AUTO / USER POSITION MANAGEMENT ──────────────────────────────────────────
# v1 hard cap — future iterations can raise this based on measured dispatch
# time vs. customer count. Tooltip in the UI notes this is expandable.
AUTO_USER_POSITION_CAP = 12


def _count_auto_user_slots(positions: list) -> dict:
    """Summarize AUTO / USER slot usage for a position list.
    Excludes orphans (they get adopted as 'user' on next trader cycle)."""
    auto = 0
    user = 0
    for p in positions or []:
        if p.get('is_orphan'):
            continue
        mb = (p.get('managed_by') or 'bot').lower()
        if mb == 'user':
            user += 1
        else:
            auto += 1
    return {
        'auto': auto,
        'user': user,
        'capacity': AUTO_USER_POSITION_CAP,
        'can_promote': auto < AUTO_USER_POSITION_CAP,
    }


@app.route('/api/positions/<pos_id>/managed-by', methods=['POST'])
@login_required
def api_set_managed_by(pos_id: str):
    """Flip a position's AUTO/USER tag. Body: {managed_by: 'bot'|'user'}.

    Rejects promotion to 'bot' when the auto cap is already full — UI
    should disable the toggle in that case, but enforce server-side too.
    Sticky USER preference overrides: if sticky=user is set for the
    ticker, refuse promotion to 'bot' until sticky is cleared first.
    """
    try:
        data = request.get_json(silent=True) or {}
        target = (data.get('managed_by') or '').lower().strip()
        if target not in ('bot', 'user'):
            return jsonify({'ok': False, 'error': "managed_by must be 'bot' or 'user'"}), 400

        db = _customer_db()
        with db.conn() as c:
            row = c.execute(
                "SELECT id, ticker, managed_by FROM positions WHERE id=? AND status='OPEN'",
                (pos_id,),
            ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'position not found or already closed'}), 404

        current = (row['managed_by'] or 'bot').lower()
        if current == target:
            return jsonify({'ok': True, 'noop': True, 'managed_by': current})

        # Guard: promoting USER → AUTO hits server-side cap + sticky rules
        if target == 'bot':
            sticky = db.get_ticker_sticky(row['ticker'])
            if sticky == 'user':
                return jsonify({
                    'ok': False,
                    'error': f"ticker {row['ticker']} is marked sticky USER — clear the sticky lock before promoting",
                }), 409
            # Count auto (excluding this position, since we're about to flip it)
            all_pos = db.get_open_positions()
            auto_count = sum(1 for p in all_pos
                             if p['id'] != pos_id and (p.get('managed_by') or 'bot') == 'bot')
            if auto_count >= AUTO_USER_POSITION_CAP:
                return jsonify({
                    'ok': False,
                    'error': f'AUTO cap reached ({auto_count}/{AUTO_USER_POSITION_CAP}) — close an auto position before promoting',
                }), 409

        db.set_position_managed_by(pos_id, target)
        db.log_event('POSITION_MANAGED_BY_CHANGED', agent='Portal',
                     details=f"{row['ticker']} {current}->{target} (pos_id={pos_id})")
        return jsonify({'ok': True, 'managed_by': target, 'ticker': row['ticker']})
    except Exception as e:
        log.error(f"/api/positions/{pos_id}/managed-by failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/auto-slots')
@login_required
def api_auto_slots():
    """Slot usage summary for the Open Positions card header + promotion UX."""
    try:
        db = _customer_db()
        positions = db.get_open_positions()
        return jsonify(_count_auto_user_slots(positions))
    except Exception as e:
        return jsonify({'auto': 0, 'user': 0, 'capacity': AUTO_USER_POSITION_CAP,
                        'can_promote': False, 'error': str(e)}), 500


# ── HALT AGENT (kill switch v2) ──────────────────────────────────────────────

@app.route('/api/halt-status')
@login_required
def api_halt_status():
    """Return halt state for the current customer + the admin halt (if any).

    Shape: {
      customer_halt: {active, reason, set_by, set_at, expected_return} | null,
      admin_halt:    {active, reason, set_by, set_at, expected_return} | null,
      active_source: 'admin' | 'customer' | null,
    }

    'active_source' is a convenience for the banner — the UI can render based
    on whichever is active, with admin taking precedence if both are set.
    """
    try:
        cust = _customer_db().get_halt()
    except Exception:
        cust = {'active': False, 'reason': None, 'set_by': None,
                'set_at': None, 'expected_return': None}
    try:
        admin = _shared_db().get_halt()
    except Exception:
        admin = {'active': False, 'reason': None, 'set_by': None,
                 'set_at': None, 'expected_return': None}

    active_source = 'admin' if admin.get('active') else ('customer' if cust.get('active') else None)

    return jsonify({
        'customer_halt': cust,
        'admin_halt':    admin,
        'active_source': active_source,
    })


@app.route('/api/admin/halt-agent', methods=['POST'])
def api_admin_halt_agent():
    """Activate or deactivate the ADMIN halt (applies to every customer).

    Token-authenticated (SECRET_TOKEN / MONITOR_TOKEN) — called from the
    command portal's monitor page on pi4b, not browser-UI on pi5.

    Body: {active: bool, reason?: str, expected_return?: str}

    Writes the system_halt singleton row in the MASTER customer's
    signals.db — the shared DB that every trader subprocess reads via
    _shared_db(). Takes effect on the NEXT trader invocation per
    customer; no existing subprocesses are interrupted.
    """
    # Token auth — accept either X-Token header or Authorization: Bearer
    auth_ok = False
    token = request.headers.get('X-Token', '')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
    expected = os.environ.get('MONITOR_TOKEN', '') or os.environ.get('SECRET_TOKEN', '')
    if expected and token and token == expected:
        auth_ok = True
    if not auth_ok:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    try:
        data = request.get_json(silent=True) or {}
        active = bool(data.get('active'))
        reason = (data.get('reason') or '').strip()[:300] or None
        expected_return = (data.get('expected_return') or '').strip()[:80] or None
        set_by = f"admin:{data.get('admin_id') or 'monitor'}"

        master = _shared_db()
        master.set_halt(active=active, reason=reason, set_by=set_by,
                        expected_return=expected_return)
        master.log_event(
            'HALT_ACTIVATED' if active else 'HALT_DEACTIVATED',
            agent='Admin Portal',
            details=f"src=admin set_by={set_by} reason={reason or '(none given)'} "
                    f"expected_return={expected_return or '(none)'}",
        )
        return jsonify({'ok': True, 'active': active, 'reason': reason,
                        'expected_return': expected_return})
    except Exception as e:
        log.error(f"/api/admin/halt-agent failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/halt-agent', methods=['POST'])
@login_required
def api_halt_agent():
    """Activate or deactivate the CUSTOMER halt for the logged-in customer.

    Body: {active: bool, reason?: str}

    Customer cannot clear admin halt — only the admin portal can. If admin
    halt is active, the endpoint still accepts customer halt changes (so
    preferences are recorded), but the banner will keep showing admin
    precedence until admin clears their halt.
    """
    try:
        data = request.get_json(silent=True) or {}
        active = bool(data.get('active'))
        reason = (data.get('reason') or '').strip()[:300] or None

        customer_id = session.get('customer_id') or 'unknown'
        set_by = f'customer:{customer_id[:12]}'

        db = _customer_db()
        db.set_halt(active=active, reason=reason, set_by=set_by)
        db.log_event(
            'HALT_ACTIVATED' if active else 'HALT_DEACTIVATED',
            agent='Portal',
            details=f"src=customer set_by={set_by} reason={reason or '(none given)'}",
        )
        return jsonify({'ok': True, 'active': active, 'reason': reason})
    except Exception as e:
        log.error(f"/api/halt-agent failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ticker-preferences/<ticker>', methods=['POST'])
@login_required
def api_set_ticker_preference(ticker: str):
    """Set or clear the sticky preference for a ticker.

    Body: {sticky: 'user' | null}

    'user' → bot never takes this ticker, even if a signal fires.
             Any currently-OPEN bot position on the ticker is NOT
             auto-flipped; user decides whether to close/hand it off
             via the per-position AUTO/USER toggle.
    null   → clear the sticky; ticker becomes eligible for the bot
             again (subject to per-position tagging of any open row).
    """
    try:
        data = request.get_json(silent=True) or {}
        sticky = data.get('sticky')  # None or 'user' (explicit 'bot' reserved for future)
        if sticky not in (None, 'user'):
            return jsonify({'ok': False, 'error': "sticky must be 'user' or null"}), 400
        ticker = (ticker or '').strip().upper()
        if not ticker or len(ticker) > 10:
            return jsonify({'ok': False, 'error': 'invalid ticker'}), 400

        db = _customer_db()
        db.set_ticker_sticky(ticker, sticky, set_by='user')
        db.log_event('TICKER_PREFERENCE_SET', agent='Portal',
                     details=f"{ticker} sticky={sticky}")
        return jsonify({'ok': True, 'ticker': ticker, 'sticky': sticky})
    except Exception as e:
        log.error(f"/api/ticker-preferences/{ticker} failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/agent-pulse')
@login_required
def api_agent_pulse():
    """Real-time agent status for the dashboard wave card."""
    try:
        db = _customer_db()
        shared = _shared_db()
        lock = get_wave_status()

        # Signal queue from shared DB (all customers see same intel).
        # `queued` reported is total in-flight (QUEUED awaiting validation +
        # VALIDATED awaiting trader action) — matches user expectation of
        # "signals currently in the pipeline."
        with shared.conn() as c:
            queued = c.execute("SELECT COUNT(*) FROM signals WHERE status IN ('QUEUED','VALIDATED')").fetchone()[0]
            watching = c.execute("SELECT COUNT(*) FROM signals WHERE status IN ('QUEUED','VALIDATED','WATCHING')").fetchone()[0]

        # Last 8 agent events from customer DB
        with db.conn() as c:
            events = [dict(r) for r in c.execute(
                "SELECT event, agent, details, timestamp FROM system_log "
                "WHERE event IN ('AGENT_START','AGENT_COMPLETE','TRADE_DECISION') "
                "ORDER BY timestamp DESC LIMIT 8"
            ).fetchall()]

        # Count today's decisions
        with db.conn() as c:
            from datetime import datetime
            today = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d')
            decisions = c.execute(
                "SELECT COUNT(*) FROM system_log WHERE event='TRADE_DECISION' AND timestamp >= ?",
                (today,)).fetchone()[0]

        # Agent color mapping
        agent_colors = {
            'Trade Logic': 'teal', 'retail_trade_logic_agent.py': 'teal',
            'News': 'purple', 'retail_news_agent.py': 'purple',
            'The Pulse': 'amber', 'Market Sentiment': 'amber', 'retail_market_sentiment_agent.py': 'amber',
            'Screener': 'pink', 'Sector Screener': 'pink', 'retail_sector_screener.py': 'pink',
        }

        # View-layer translation: hide raw agent names from customer UI.
        # Everything the customer sees is framed as "Synthos is {action} · {aside}".
        status = interpret_agent_status(lock)

        running = None
        if lock:
            agent_name = lock['agent']
            running = {
                'persona':   status['persona'],
                'action':    status['action'],
                'aside':     status['aside'],
                'age_secs':  lock.get('age_secs', 0),
                'color':     lock.get('color') or agent_colors.get(agent_name, 'teal'),
                'amplitude': lock.get('amplitude'),
                'speed':     lock.get('speed'),
                'frequency': lock.get('frequency'),
                'direction': lock.get('direction'),
            }

        # Market regime from shared DB (Pulse is a shared agent)
        regime = 'unknown'
        try:
            import sqlite3 as _sql
            _shared_path = _shared_db().path
            _rc = _sql.connect(_shared_path, timeout=5)
            _rc.row_factory = _sql.Row
            _regime_row = _rc.execute(
                "SELECT details FROM system_log WHERE agent='The Pulse' AND event='AGENT_COMPLETE' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if _regime_row:
                _det = _regime_row['details'] or ''
                if 'regime=' in _det:
                    regime = _det.split('regime=')[1].split(' ')[0].split(',')[0]
            _rc.close()
        except Exception:
            pass

        # Translate events to user-facing event labels (hide raw agent names).
        translated_events = []
        for ev in events[:6]:
            translated_events.append({
                'event':      ev.get('event'),
                'timestamp':  ev.get('timestamp'),
                'event_label': interpret_event_label(ev.get('agent')),
                'details':    ev.get('details'),
            })

        # Idle status (shown when nothing is actively running).
        idle_status = None if running else {
            'persona': status['persona'],
            'action':  status['action'],
            'aside':   status['aside'],
        }

        return jsonify({
            'running': running,
            'idle_status': idle_status,
            'queued_signals': queued,
            'watching': watching,
            'decisions_today': decisions,
            'regime': regime,
            'events': translated_events,
        })
    except Exception as e:
        return jsonify({'running': None, 'idle_status': None, 'queued_signals': 0, 'watching': 0,
                        'decisions_today': 0, 'regime': 'unknown', 'events': [], 'error': str(e)})


@app.route('/api/portfolio-history')
@login_required
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
@login_required
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

            # Frozen thesis lookup (added 2026-04-25 for Phase 7c history
            # drawer): if signal_id is present, fetch the headline /
            # source from the shared signals table so the drawer can show
            # "why we bought this" without exposing system internals.
            # Most non-owner customers have signal_id=NULL — graceful
            # fallback yields headline=None.
            sig_headline = sig_source = sig_source_url = None
            _sig_id = t.get('signal_id')
            if _sig_id:
                try:
                    import sqlite3 as _sql
                    with _sql.connect(_shared_db().path, timeout=5) as _sc:
                        _sc.row_factory = _sql.Row
                        _row = _sc.execute(
                            "SELECT headline, source, source_url FROM signals WHERE id=?",
                            (_sig_id,)
                        ).fetchone()
                        if _row:
                            sig_headline   = _row['headline']
                            sig_source     = _row['source']
                            sig_source_url = _row['source_url'] if 'source_url' in _row.keys() else None
                except Exception:
                    pass

            rows.append({
                'id':              t.get('id'),
                'ticker':          t.get('ticker', '--'),
                'company':         t.get('company') if t.get('company') and t.get('company') != t.get('ticker') else None,
                'sector':          t.get('sector'),
                'side':            'LONG',
                'entry':           round(t.get('entry_price') or 0, 2),
                'exit':            round(t.get('current_price') or 0, 2),
                'shares':          float(t.get('shares') or 0),
                'hold':            hold_label,
                'hold_hours':      round(hrs, 1),
                'pnl':             round(pnl, 2),
                'ret_pct':         ret_pct,
                'opened_at':       (t.get('opened_at') or '')[:16].replace('T', ' '),
                'closed_at':       (t.get('closed_at') or '')[:16].replace('T', ' '),
                'exit_reason':     t.get('exit_reason') or '--',
                'entry_pattern':   t.get('entry_pattern'),  # NULL for pre-Phase-5 trades
                'managed_by':      t.get('managed_by') or 'bot',
                # signal_headline comes from the signals table via signal_id
                # lookup (only resolves for owner customer; ~22% coverage).
                # entry_thesis is the same headline copied to the position
                # row at open time — works for ALL customers (Phase 7L).
                # Frontend uses signal_headline first, falls back to
                # entry_thesis when signal_id was NULL.
                'signal_headline': sig_headline or t.get('entry_thesis'),
                'signal_source':   sig_source,
                'signal_source_url': sig_source_url,
                'entry_thesis':    t.get('entry_thesis'),
            })

        total_trades  = wins + losses
        win_rate      = round(wins / total_trades * 100, 1) if total_trades else 0.0
        avg_hold_hrs  = round(sum(hold_hours_list) / len(hold_hours_list), 1) if hold_hours_list else 0.0
        avg_hold_lbl  = (f"{int(avg_hold_hrs)}h" if avg_hold_hrs < 48 else f"{round(avg_hold_hrs/24,1)}d") if avg_hold_hrs else '--'
        month_start   = port.get('month_start') or port.get('cash') or 1
        total_ret_pct = round(total_pnl / month_start * 100, 2) if month_start else 0.0

        # Best and worst trades
        best_trade = max(rows, key=lambda x: x['pnl']) if rows else None
        worst_trade = min(rows, key=lambda x: x['pnl']) if rows else None

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
            'best_trade':      best_trade,
            'worst_trade':     worst_trade,
        })
    except Exception as e:
        return jsonify({'total_pnl': 0, 'win_rate': 0, 'total_trades': 0,
                        'trades': [], 'error': str(e)})


@app.route('/api/watchlist')
@login_required
def api_watchlist():
    """Signals from shared intelligence — all customers see the same
    market data.

    Phase 7k (2026-04-25): repointed from get_watching_signals() →
    get_signals_by_status(). The old function read news_feed (the raw
    article inbox) which has 1100+ MACRO sentinels and articles with
    unresolved tickers, displayed on the Watchlist page as "MACR" and
    "?" — making the page look like dirty data. The signals table
    itself is clean (zero MACRO, zero null tickers across 1800+ rows);
    we just weren't reading from it. This change surfaces the actual
    bot watchlist (status WATCHING / QUEUED / VALIDATED).
    """
    try:
        signals = _shared_db().get_signals_by_status(
            ['WATCHING', 'QUEUED', 'VALIDATED'], limit=50
        )
        return jsonify({'signals': signals, 'count': len(signals)})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})


@app.route('/api/planning')
@login_required
def api_planning():
    """Planning panel — real signal queue first, watchlist fallback when empty.

    Phase 7L (2026-04-25): fallback now reads the curated signals
    table via get_signals_by_status(['WATCHING']) instead of news_feed
    via get_watching_signals(). Same wiring fix as 7k applied to the
    Planning card's empty-state fallback so MACR/? sentinels don't
    leak through when the queue happens to be empty.
    """
    try:
        queued = _shared_db().get_queued_signals()
        if queued:
            return jsonify({'signals': queued, 'mode': 'queue', 'count': len(queued)})
        intel = _shared_db().get_signals_by_status(['WATCHING'], limit=8)
        return jsonify({'signals': intel, 'mode': 'intel', 'count': len(intel)})
    except Exception as e:
        return jsonify({'signals': [], 'mode': 'intel', 'count': 0, 'error': str(e)})


@app.route('/api/ticker-news')
@login_required
def api_ticker_news():
    """Recent news_feed articles for a single ticker — powers the
    'Recent news' section of the planning drawer (Phase 7g, 2026-04-25;
    tightened in 7h to fresh-only top-10).

    The news pipeline already filters at ingestion time: tier-4 opinion
    sources are excluded at gate 3, and articles below MIN_CREDIBILITY /
    MIN_RELEVANCE never enter news_feed. So we render raw_headline as-is
    — no client-side declickbait pass needed.

    Query params:
      ticker      — required, normalized to uppercase
      limit       — default 10, max 20
      since_days  — default 7, max 30. Only return articles whose
                    created_at falls within the last N days. With 16k+
                    rows in news_feed, "every article ever" was too many;
                    a freshness window is what users actually want.

    Returns rows shaped for the drawer:
      {timestamp, headline, source, source_url, image_url, sentiment_score}
    The metadata JSON blob stored on each row is parsed server-side.
    """
    ticker = (request.args.get('ticker') or '').strip().upper()
    if not ticker or not ticker.replace('.', '').replace('-', '').isalnum() or len(ticker) > 8:
        return jsonify({'articles': [], 'error': 'invalid ticker'}), 400
    try:
        limit = max(1, min(20, int(request.args.get('limit') or 10)))
    except (TypeError, ValueError):
        limit = 10
    try:
        since_days = max(1, min(30, int(request.args.get('since_days') or 7)))
    except (TypeError, ValueError):
        since_days = 7

    try:
        import sqlite3 as _sql
        articles = []
        # news_feed lives in the shared (owner) DB — written by the news
        # agent and accessible to all customers. Use _shared_db's path.
        with _sql.connect(_shared_db().path, timeout=5) as c:
            c.row_factory = _sql.Row
            rows = c.execute(
                "SELECT id, timestamp, raw_headline, sentiment_score, source, "
                "metadata, created_at "
                "FROM news_feed WHERE UPPER(ticker)=? "
                "AND created_at >= datetime('now', ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (ticker, f'-{since_days} days', limit)
            ).fetchall()
        for r in rows:
            md = {}
            try:
                md = json.loads(r['metadata']) if r['metadata'] else {}
            except (ValueError, TypeError):
                md = {}
            articles.append({
                'id':              r['id'],
                'timestamp':       r['created_at'] or r['timestamp'],
                'headline':        r['raw_headline'] or '',
                'source':          md.get('source') or r['source'] or 'News',
                'source_url':      md.get('link'),
                'image_url':       md.get('image_url'),
                'sentiment_score': r['sentiment_score'],
            })
        return jsonify({
            'ticker': ticker, 'articles': articles, 'count': len(articles),
            'since_days': since_days, 'limit': limit,
        })
    except Exception as e:
        log.warning(f"/api/ticker-news error: {e}")
        return jsonify({'ticker': ticker, 'articles': [], 'error': str(e)})


# ── /api/ticker-context support: in-process cache + sector→ETF map ──
# Cache is an LRU-ish dict keyed by ticker, valid for 60 seconds. No
# eviction beyond TTL because the cache is tiny in practice (one entry
# per drawer-opened ticker). Cleared on portal restart, which is fine.
_TICKER_CTX_CACHE = {}      # {ticker: (timestamp_epoch, payload)}
_TICKER_CTX_TTL_SEC = 60

# Subset of SECTOR_ETF_MAP from agents/news/keywords.py — kept in sync
# manually because it's used cross-module in only this one spot. Sector
# names are normalized lowercase before lookup.
_SECTOR_ETF = {
    "technology":             "XLK",
    "defense":                "XLI",
    "healthcare":             "XLV",
    "energy":                 "XLE",
    "financials":             "XLF",
    "materials":              "XLB",
    "industrials":            "XLI",
    "consumer staples":       "XLP",
    "consumer discretionary": "XLY",
    "real estate":            "XLRE",
    "utilities":              "XLU",
    "communication":          "XLC",
}


@app.route('/api/ticker-context')
@login_required
def api_ticker_context():
    """Live snapshot for a ticker — powers the planning drawer's "Live
    snapshot" section and the approval drawer's live current price /
    distance-from-buy-zone (Phase 7h, 2026-04-25).

    Single Alpaca call returns 14 daily bars; we compute:
      • current_price  — latest minute close (most recent bar)
      • today_high / today_low / today_pct  — today's daily bar
      • adr_pct        — 14-day average true daily range as % of close
      • sector_etf     — symbol mapped from passed-in ?sector=
      • sector_etf_pct — today's % change for that ETF (one extra call,
                         memoized 60s)

    Cached for 60s per ticker. ADR is shared market data so we use the
    owner's ALPACA_API_KEY (same pattern as /api/sparklines), not the
    customer's — see comment on Phase 3 sparklines fix.
    """
    ticker = (request.args.get('ticker') or '').strip().upper()
    sector = (request.args.get('sector') or '').strip().lower()
    if not ticker or not ticker.replace('.', '').replace('-', '').isalnum() or len(ticker) > 8:
        return jsonify({'error': 'invalid ticker'}), 400

    import time as _time
    cache_key = f"{ticker}|{sector}"
    now = _time.time()
    cached = _TICKER_CTX_CACHE.get(cache_key)
    if cached and (now - cached[0] < _TICKER_CTX_TTL_SEC):
        return jsonify(cached[1])

    alpaca_key    = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    alpaca_data_url = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets').rstrip('/')
    if not (alpaca_key and alpaca_secret):
        return jsonify({'ticker': ticker, 'error': 'alpaca creds unavailable'}), 503

    payload = {'ticker': ticker, 'sector': sector or None}

    try:
        from datetime import datetime, timezone, timedelta
        import requests as _req
        # 14 trading days ≈ 22 calendar days back to be safe
        end_dt   = datetime.now(timezone.utc) - timedelta(minutes=15)  # SIP delay
        start_dt = end_dt - timedelta(days=22)

        # Fetch daily bars for the ticker (+ optional sector ETF in same call).
        symbols = [ticker]
        sector_etf = _SECTOR_ETF.get(sector) if sector else None
        if sector_etf and sector_etf != ticker:
            symbols.append(sector_etf)

        r = _req.get(
            f"{alpaca_data_url}/v2/stocks/bars",
            headers={'APCA-API-KEY-ID': alpaca_key, 'APCA-API-SECRET-KEY': alpaca_secret},
            params={
                'symbols':   ','.join(symbols),
                'timeframe': '1Day',
                'start':     start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'end':       end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'limit':     30,
                'feed':      'iex',
            },
            timeout=8,
        )
        if r.status_code != 200:
            log.debug(f"ticker-context fetch {ticker}: {r.status_code}")
            return jsonify({'ticker': ticker, 'error': f'data fetch failed (HTTP {r.status_code})'}), 502

        bars_by_sym = (r.json() or {}).get('bars', {}) or {}

        # Primary ticker
        ticker_bars = bars_by_sym.get(ticker, []) or []
        if not ticker_bars:
            payload['error'] = 'no bars for ticker'
        else:
            # Today's bar = newest. ADR = mean((h-l)/c) over the prior 14 bars.
            today = ticker_bars[-1]
            recent = ticker_bars[-15:-1] if len(ticker_bars) >= 15 else ticker_bars[:-1]
            adr_vals = []
            for b in recent:
                try:
                    h, l, c = float(b.get('h', 0)), float(b.get('l', 0)), float(b.get('c', 0))
                    if c > 0:
                        adr_vals.append((h - l) / c)
                except (TypeError, ValueError):
                    continue
            adr_pct = (sum(adr_vals) / len(adr_vals) * 100) if adr_vals else None
            try:
                t_open  = float(today.get('o', 0)) or None
                t_high  = float(today.get('h', 0)) or None
                t_low   = float(today.get('l', 0)) or None
                t_close = float(today.get('c', 0)) or None
                # Day % change uses the prior-day close as denominator
                # rather than today's open — matches how Alpaca portfolio
                # P&L is computed and what most retail platforms display.
                prev_close = float(ticker_bars[-2].get('c', 0)) if len(ticker_bars) >= 2 else None
                day_pct = ((t_close - prev_close) / prev_close * 100) if (t_close and prev_close) else None
                payload['current_price'] = round(t_close, 2) if t_close else None
                payload['today_open']    = round(t_open, 2)  if t_open  else None
                payload['today_high']    = round(t_high, 2)  if t_high  else None
                payload['today_low']     = round(t_low, 2)   if t_low   else None
                payload['today_pct']     = round(day_pct, 2) if day_pct is not None else None
                payload['adr_pct']       = round(adr_pct, 2) if adr_pct is not None else None
            except (TypeError, ValueError) as e:
                log.debug(f"ticker-context parse {ticker}: {e}")

        # Sector ETF context
        if sector_etf:
            etf_bars = bars_by_sym.get(sector_etf, []) or []
            if len(etf_bars) >= 2:
                try:
                    etf_close = float(etf_bars[-1].get('c', 0))
                    etf_prev  = float(etf_bars[-2].get('c', 0))
                    if etf_prev > 0:
                        etf_pct = (etf_close - etf_prev) / etf_prev * 100
                        payload['sector_etf']     = sector_etf
                        payload['sector_etf_pct'] = round(etf_pct, 2)
                except (TypeError, ValueError):
                    pass

        _TICKER_CTX_CACHE[cache_key] = (now, payload)
        return jsonify(payload)

    except Exception as e:
        log.warning(f"/api/ticker-context error: {e}")
        return jsonify({'ticker': ticker, 'error': str(e)}), 500


@app.route('/api/screening')
@login_required
def api_screening():
    """Latest sector screening — shared across all customers.
    Query ?sector=<name> to restrict to one sector; default is all sectors
    in the most recent screener run."""
    try:
        sector = request.args.get('sector') or None
        candidates = _shared_db().get_latest_screening_run(sector=sector)
        return jsonify({'candidates': candidates, 'sector': sector})
    except Exception as e:
        return jsonify({'candidates': [], 'error': str(e)})


@app.route('/api/screening/sectors')
@login_required
def api_screening_sectors():
    """Per-sector summary — sector / ETF / 5yr return / top ticker / top score.
    Sorted so the strongest sector (by best candidate combined_score) is
    first — used as the dashboard widget's default selection."""
    try:
        summary = _shared_db().get_sector_screening_summary()
        return jsonify({'sectors': summary})
    except Exception as e:
        return jsonify({'sectors': [], 'error': str(e)})


@app.route('/api/market-indices')
@login_required
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

    now_utc  = datetime.now(timezone.utc).replace(tzinfo=None)
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
@login_required
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
<style>
@font-face{font-family:'Inter';font-style:normal;font-weight:100 900;font-display:swap;src:url('/static/fonts/Inter.woff2') format('woff2')}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:100 900;font-display:swap;src:url('/static/fonts/JetBrainsMono.woff2') format('woff2')}
</style>"""
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
@login_required
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
                'trading_mode': c.get('trading_mode', 'PAPER'),
                'operating_mode': c.get('operating_mode', 'MANAGED'),
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
    if status not in ('open', 'in_progress', 'resolved', 'closed', 'archived'):
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
    # Parse the `[YYYY-MM-DD HH:MM:SS]` prefix that every agent's logger emits
    # so first_seen/last_seen reflect when the line was WRITTEN, not when the
    # /api/logs-audit endpoint happened to run. Without this, the monitor UI
    # shows "N seconds ago" for errors that are actually days old.
    TS_RE = _re.compile(r'^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})')

    def _parse_log_ts(line: str) -> 'str | None':
        m = TS_RE.match(line)
        if not m:
            return None
        try:
            naive = _dt.strptime(m.group(1).replace('T', ' '), '%Y-%m-%d %H:%M:%S')
            # Agent loggers emit local-time (ET) without a zone — tag as ET, convert to UTC ISO
            return naive.replace(tzinfo=ET).astimezone(_tz.utc).isoformat()
        except Exception:
            return None

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
                        # Use the log line's own timestamp when present;
                        # fall back to scan-time only when absent.
                        line_ts = _parse_log_ts(line) or now_iso
                        if key in seen:
                            seen[key]['hit_count'] += 1
                            # last_seen tracks the MOST RECENT occurrence of the
                            # same line — keep the later of existing vs. this line.
                            if line_ts > seen[key]['last_seen']:
                                seen[key]['last_seen'] = line_ts
                            if line_ts < seen[key]['first_seen']:
                                seen[key]['first_seen'] = line_ts
                        else:
                            entry = {
                                'id':          len(issues) + 1,
                                'source_file': fname,
                                'severity':    sev,
                                'context':     ctx,
                                'hit_count':   1,
                                'first_seen':  line_ts,
                                'last_seen':   line_ts,
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
@admin_required
def api_audit():
    """Latest audit result from agent4_audit.py.

    Admin-only — leaks system-level audit findings (file paths, log lines,
    severity tags) which are infrastructure recon for an attacker. Was
    previously @login_required, fixed 2026-04-24 (Phase 4 of security audit).
    """
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
@admin_required
def api_update():
    """
    Pull latest from GitHub and restart portal.
    Runs qpull.sh if available, otherwise git pull directly.

    Admin-only. The customer-facing UI trigger was removed (item 10
    cleanup) so this endpoint is reachable only via direct API call
    by admin sessions. Still useful for admin CLI / curl workflow.
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
    return render_template('file_manager.html',
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
    Copy uploaded files from staging into PROJECT_DIR, stage them,
    commit, and push to GitHub. Uses GITHUB_TOKEN from .env if
    available. Returns dict with ok, message.

    Phase 7L (2026-04-25) wrong-dir bug fix (MEDIUM-C from the
    file-upload audit): files are uploaded to ~/synthos/upload_staging/
    via /api/files/upload but were never copied into PROJECT_DIR
    (~/synthos/synthos_build/). The previous version ran `git add`
    against PROJECT_DIR with bare basenames — basenames that didn't
    exist there — so git silently no-op'd, the diff was empty, and
    the function returned "Already up to date" while the upload sat
    in staging. Operators thought files had been pushed when they
    hadn't. Fix: explicit copy STAGING_DIR/<f> → PROJECT_DIR/<f>
    before staging.
    """
    import subprocess
    import shutil

    github_token = os.environ.get('GITHUB_TOKEN', '')
    STAGING_DIR  = os.path.join(os.path.dirname(PROJECT_DIR), 'upload_staging')

    try:
        # Check git is available and we're in a repo
        check = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=5
        )
        if check.returncode != 0:
            return {'ok': False, 'message': 'Not a git repository'}

        # Copy from staging into the live project dir. Each file's
        # absence in staging is logged but doesn't abort the rest of
        # the batch. Once copied, the source staging copy is removed
        # so the staging dir doesn't accumulate stale uploads.
        copied = []
        copy_errors = []
        for fname in files:
            src = os.path.join(STAGING_DIR, fname)
            dst = os.path.join(PROJECT_DIR, fname)
            if not os.path.isfile(src):
                copy_errors.append(f'{fname}: not in staging')
                log.warning(f"sync_to_github: source missing in staging: {src}")
                continue
            try:
                shutil.copy2(src, dst)
                copied.append(fname)
                log.info(f"sync_to_github: copied {fname} staging → project")
                try:
                    os.unlink(src)
                except OSError:
                    pass  # remove failure is non-fatal
            except OSError as e:
                copy_errors.append(f'{fname}: copy failed ({e})')
                log.warning(f"sync_to_github: copy failed for {fname}: {e}")

        if not copied:
            return {
                'ok': False,
                'message': 'No files copied to project dir: ' + '; '.join(copy_errors[:3])
            }

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

        # Stage the copied files (now they actually exist in PROJECT_DIR)
        subprocess.run(
            ['git', 'add', '-f'] + copied,
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=10
        )

        # Check if anything to commit
        status = subprocess.run(
            ['git', 'diff', '--cached', '--stat'],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=5
        )
        if not status.stdout.strip():
            return {'ok': True, 'message': 'Already up to date on GitHub (no diff after copy)'}

        # Commit — use the actually-copied filenames, not the requested
        # set, since the staging-missing case may have dropped some.
        msg = f"Portal upload: {', '.join(copied)}"
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
            log.info(f"GitHub sync OK: {copied}")
            tail = ' (' + '; '.join(copy_errors[:2]) + ')' if copy_errors else ''
            return {'ok': True, 'message': f"Pushed to GitHub: {', '.join(copied)}" + tail}
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


# FILE_MANAGER_HTML extracted 2026-04-23 → src/templates/file_manager.html
# Tier 3 migration on feat/portal-v2.


# ── NEWS FEED ──────────────────────────────────────────────────────────────

def get_news_feed_data(limit=100):
    """Fetch recent news feed entries from the shared intelligence database."""
    try:
        return _shared_db().get_news_feed(limit=limit)
    except Exception as e:
        log.warning(f"get_news_feed_data error: {e}")
        return []


# NEWS_FEED_HTML extracted 2026-04-23 → src/templates/news_feed.html
# Tier 2 migration on feat/portal-v2. Light-themed; variables live in template.


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

    return render_template('news_feed.html', count=len(rows),
                           updated=now_str, table_content=table_content)


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

# ADMIN_HTML removed 2026-04-23 — 817-line dead-code constant. No @app.route
# used it; old /admin route was removed somewhere along the way but the
# constant got orphaned. Discovered during Tier 3 extraction on feat/portal-v2.


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
        from retail_database import get_customer_db
        cdb = get_customer_db(customer_id)
        cdb.set_setting('NEW_CUSTOMER', 'true')
        log.info(f"Admin created customer {customer_id} ({email}) — auto-activated, NEW_CUSTOMER=true")
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


@app.route('/api/admin/customers/<customer_id>/trading-mode', methods=['POST'])
@admin_required
def api_admin_set_trading_mode(customer_id):
    """Toggle trading mode between PAPER and LIVE for a customer."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', '').strip().upper()
    if mode not in ('PAPER', 'LIVE'):
        return jsonify({'ok': False, 'error': 'mode must be PAPER or LIVE'}), 400
    try:
        import auth as _auth
        _auth.set_trading_mode(customer_id, mode)
        return jsonify({'ok': True, 'mode': mode})
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
def _get_customer_trading_modes():
    """Count customers by trading mode from their customer_settings."""
    import sqlite3 as _sql
    counts = {'PAPER': 0, 'LIVE': 0, 'total': 0}
    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    for cid in os.listdir(customers_dir):
        if cid == 'default':
            continue
        db_path = os.path.join(customers_dir, cid, 'signals.db')
        if not os.path.exists(db_path):
            continue
        try:
            conn = _sql.connect(db_path, timeout=5)
            row = conn.execute("SELECT value FROM customer_settings WHERE key='TRADING_MODE'").fetchone()
            mode = (row[0] if row else 'PAPER').upper()
            conn.close()
            counts[mode] = counts.get(mode, 0) + 1
            counts['total'] += 1
        except Exception:
            pass
    return counts


def _parse_db_dt_to_et(ts_str):
    """Parse a DB timestamp (naive UTC or ISO+tz) and return tz-aware ET datetime, or None."""
    from zoneinfo import ZoneInfo
    if not ts_str:
        return None
    _et = ZoneInfo("America/New_York")
    s = str(ts_str).strip()
    try:
        if '+' in s or s.endswith('Z'):
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            return dt.astimezone(_et)
        dt = datetime.fromisoformat(s.replace(' ', 'T')[:19])
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_et)
    except Exception:
        return None


def _session_bin_index(dt_et, session_start, session_end, n_bins, bin_minutes):
    """Return the bin index [0..n_bins) for a session-day timestamp, or None if outside window."""
    if dt_et is None:
        return None
    if dt_et < session_start or dt_et >= session_end:
        return None
    delta = int((dt_et - session_start).total_seconds() // 60)
    idx = delta // bin_minutes
    return idx if 0 <= idx < n_bins else None


def _resolve_trading_session_date(date_param, now):
    """Resolve ?date= param to a trading session date (ET date object).
    Returns (session_date, error_response) — error_response is a Flask
    (response, status) tuple or None on success.
    """
    from datetime import time as _time, timedelta
    _market_start_t = _time(9, 30)
    if date_param:
        try:
            session_date = datetime.strptime(date_param, '%Y-%m-%d').date()
            if session_date > now.date() or (now.date() - session_date).days > 366:
                raise ValueError('out of range')
        except ValueError:
            return None, (jsonify({'error': f'invalid or out-of-range date {date_param!r}'}), 400)
    else:
        session_date = now.date()
        if now.weekday() >= 5 or (now.weekday() < 5 and now.time() < _market_start_t):
            session_date = session_date - timedelta(days=1)
            while session_date.weekday() >= 5:
                session_date = session_date - timedelta(days=1)
    return session_date, None


def _build_market_bins(session_start, session_end, bin_minutes=10):
    """Build bin metadata for a trading session.
    Returns (n_bins, bin_labels, bin_starts).
    """
    from datetime import timedelta
    session_minutes = int((session_end - session_start).total_seconds() // 60)
    n_bins = session_minutes // bin_minutes
    labels = []
    starts = []
    for i in range(n_bins):
        t = session_start + timedelta(minutes=i * bin_minutes)
        labels.append(t.strftime('%H:%M'))
        starts.append(t.isoformat())
    return n_bins, labels, starts


def _aggregate_customer_market_activity(n_bins, session_start, session_end,
                                          session_start_cutoff, session_end_cutoff,
                                          customer_names):
    """Aggregate buy/sell activity across all customers for a trading session.
    Returns (customers_data, total_buys_bins, total_sells_bins, buy_count, sell_count).
    """
    BIN_MINUTES = 10
    customers_data   = {}
    total_buys_bins  = [0.0] * n_bins
    total_sells_bins = [0.0] * n_bins
    total_buy_count  = 0
    total_sell_count = 0

    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    for cid in os.listdir(customers_dir):
        if cid == 'default':
            continue
        db_path = os.path.join(customers_dir, cid, 'signals.db')
        if not os.path.exists(db_path):
            continue
        # R10-5 — wrap in contextlib.closing so conn.close() runs on every
        # path including mid-loop exceptions (SQL errors, locked DB, etc.).
        # The original `conn.close()` was inside the try block, so any
        # exception from .execute() left the connection open until GC.
        try:
            import sqlite3
            from contextlib import closing
            cust_buys  = [0.0] * n_bins
            cust_sells = [0.0] * n_bins
            has_activity = False

            with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row

                for r in conn.execute(
                    "SELECT opened_at, entry_price * shares AS amt FROM positions "
                    "WHERE opened_at IS NOT NULL AND opened_at >= ? AND opened_at < ?",
                    (session_start_cutoff, session_end_cutoff)
                ).fetchall():
                    idx = _session_bin_index(
                        _parse_db_dt_to_et(r['opened_at']),
                        session_start, session_end, n_bins, BIN_MINUTES,
                    )
                    if idx is None:
                        continue
                    amt = float(r['amt'] or 0)
                    cust_buys[idx]        += amt
                    total_buys_bins[idx]  += amt
                    total_buy_count       += 1
                    has_activity = True

                for r in conn.execute(
                    "SELECT closed_at, entry_price * shares AS amt FROM positions "
                    "WHERE closed_at IS NOT NULL AND closed_at >= ? AND closed_at < ?",
                    (session_start_cutoff, session_end_cutoff)
                ).fetchall():
                    idx = _session_bin_index(
                        _parse_db_dt_to_et(r['closed_at']),
                        session_start, session_end, n_bins, BIN_MINUTES,
                    )
                    if idx is None:
                        continue
                    amt = float(r['amt'] or 0)
                    cust_sells[idx]        += amt
                    total_sells_bins[idx]  += amt
                    total_sell_count       += 1
                    has_activity = True

            if has_activity:
                customers_data[cid] = {
                    'name':  customer_names.get(cid, cid[:8]),
                    'buys':  [round(v, 2) for v in cust_buys],
                    'sells': [round(v, 2) for v in cust_sells],
                }
        except Exception as e:
            log.warning(f"market-activity scan for {cid[:8]}: {e}")

    return customers_data, total_buys_bins, total_sells_bins, total_buy_count, total_sell_count


def _aggregate_user_sessions_historical(session_date):
    """Aggregate user sessions for a historical ET calendar day from session_history.
    Returns (hour_keys, session_bins, session_names).
    """
    from datetime import timedelta, time as _time
    from zoneinfo import ZoneInfo
    import json as _json_local
    _et = ZoneInfo("America/New_York")
    session_day_start_et = datetime.combine(session_date, _time(0, 0), tzinfo=_et)
    hour_keys = [
        (session_day_start_et + timedelta(hours=i)).strftime('%Y-%m-%dT%H:00')
        for i in range(24)
    ]
    day_start_utc = session_day_start_et.astimezone(ZoneInfo("UTC"))
    day_end_utc   = day_start_utc + timedelta(hours=24)
    u_lo = day_start_utc.strftime('%Y-%m-%dT%H:%M')
    u_hi = day_end_utc.strftime('%Y-%m-%dT%H:%M')
    session_bins  = {k: 0     for k in hour_keys}
    session_names = {k: set() for k in hour_keys}
    try:
        shared = _shared_db()
        with shared.conn() as c:
            rows = c.execute(
                "SELECT ts, count, names FROM session_history "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (u_lo, u_hi),
            ).fetchall()
        for r in rows:
            try:
                utc_dt = datetime.fromisoformat(r['ts']).replace(tzinfo=ZoneInfo("UTC"))
            except Exception:
                continue
            et_dt = utc_dt.astimezone(_et)
            hkey = et_dt.strftime('%Y-%m-%dT%H:00')
            if hkey not in session_bins:
                continue
            session_bins[hkey] = max(session_bins[hkey], int(r['count'] or 0))
            try:
                for n in (_json_local.loads(r['names']) if r['names'] else []):
                    session_names[hkey].add(n)
            except Exception:
                pass
    except Exception as e:
        log.debug(f"session_history historical fetch failed: {e}")
    return hour_keys, session_bins, session_names


@app.route('/api/admin/market-activity')
@admin_required
def api_admin_market_activity():
    """Aggregate buy/sell activity + user sessions for the monitor.

    Response is split into two visualizations:

      market_activity — the most recent trading session (9:30-16:00 ET),
        bucketed into 10-min bins (39 bins). Buys and sells in dollars.
        Net is buys minus sells. Per-customer breakdown included when
        multiple customers traded.

      user_sessions — rolling 24h window of distinct-user counts,
        bucketed hourly (24 bins). Used for the separate user-count
        chart the monitor now renders beside the market chart.

    The ?hours= query param still controls the user_sessions window
    (legacy clients can keep passing hours=24). Market activity is
    always the most-recent session — not configurable because the
    monitor chart is always "how did today go", not a rolling window.
    """
    from datetime import datetime, timedelta, time as _time
    from zoneinfo import ZoneInfo
    import auth as _auth

    hours = int(request.args.get('hours', 24))
    _et = ZoneInfo("America/New_York")
    now = datetime.now(_et)
    date_param = (request.args.get('date') or '').strip()

    # ── Session date ────────────────────────────────────────────────────
    session_date, err = _resolve_trading_session_date(date_param, now)
    if err:
        return err

    session_start = datetime.combine(session_date, _time(9, 30), tzinfo=_et)
    session_end   = datetime.combine(session_date, _time(16, 0), tzinfo=_et)

    # ── Navigation metadata ─────────────────────────────────────────────
    def _shift_trading_day(d, delta):
        step = 1 if delta > 0 else -1
        remaining = abs(delta)
        while remaining > 0:
            d = d + timedelta(days=step)
            if d.weekday() < 5:
                remaining -= 1
        return d

    _market_start_t = _time(9, 30)
    prev_session_date = _shift_trading_day(session_date, -1)
    _today_session = now.date()
    if now.weekday() >= 5 or (now.weekday() < 5 and now.time() < _market_start_t):
        _today_session = _shift_trading_day(_today_session, -1)
        while _today_session.weekday() >= 5:
            _today_session = _today_session - timedelta(days=1)
    next_session_date = _shift_trading_day(session_date, 1) if session_date < _today_session else None

    # ── Bins ────────────────────────────────────────────────────────────
    BIN_MINUTES = 10
    n_bins, market_bin_labels, market_bin_starts = _build_market_bins(session_start, session_end, BIN_MINUTES)

    # ── Customer name lookup ────────────────────────────────────────────
    customer_names = {}
    try:
        with _auth._auth_conn() as c:
            for row in c.execute("SELECT id, display_name_enc FROM customers").fetchall():
                try:
                    customer_names[row['id']] = _auth.decrypt_field(row['display_name_enc']) or row['id'][:8]
                except Exception:
                    customer_names[row['id']] = row['id'][:8]
    except Exception:
        pass

    # ── Market activity ─────────────────────────────────────────────────
    session_start_utc = session_start.astimezone(ZoneInfo("UTC"))
    session_end_utc   = session_end.astimezone(ZoneInfo("UTC"))
    _session_start_cutoff = (session_start_utc - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
    _session_end_cutoff   = (session_end_utc + timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')

    customers_data, total_buys_bins, total_sells_bins, total_buy_count, total_sell_count = \
        _aggregate_customer_market_activity(
            n_bins, session_start, session_end,
            _session_start_cutoff, _session_end_cutoff, customer_names,
        )

    total_buys_bins_r  = [round(v, 2) for v in total_buys_bins]
    total_sells_bins_r = [round(v, 2) for v in total_sells_bins]
    total_net_bins     = [round(b - s, 2) for b, s in zip(total_buys_bins, total_sells_bins)]

    # ── User sessions ────────────────────────────────────────────────────
    if date_param:
        hour_keys, session_bins, session_names = _aggregate_user_sessions_historical(session_date)
        active_now = 0
        active_customers = []
    else:
        hour_keys = [
            (now - timedelta(hours=hours - 1 - i)).strftime('%Y-%m-%dT%H:00')
            for i in range(hours)
        ]
        session_bins  = {k: 0     for k in hour_keys}
        session_names = {k: set() for k in hour_keys}
        with _session_activity_lock:
            for entry in _session_hourly:
                ts    = entry[0]
                count = entry[1]
                names = entry[2] if len(entry) > 2 else []
                hkey = ts[:13] + ':00'
                if hkey in session_bins:
                    session_bins[hkey] = max(session_bins[hkey], count)
                    for n in names:
                        session_names[hkey].add(n)
            active_now = 0
            active_customers = []
            for cid, v in _session_activity.items():
                idle = (datetime.now(timezone.utc).replace(tzinfo=None) - v['last_activity']).total_seconds()
                if idle < 900:
                    active_now += 1
                    try:
                        name = auth.get_display_name_by_id(cid) if hasattr(auth, 'get_display_name_by_id') else cid[:8]
                    except Exception:
                        name = cid[:8]
                    active_customers.append({'name': name, 'idle_secs': int(idle)})

    hours_list    = sorted(session_bins.keys())
    sessions_list = [session_bins[h] for h in hours_list]
    peak          = max(sessions_list) if sessions_list else 0
    total_buys_all  = sum(total_buys_bins)
    total_sells_all = sum(total_sells_bins)

    return jsonify({
        'market_activity': {
            'bins':               market_bin_labels,
            'bin_starts':         market_bin_starts,
            'buys':               total_buys_bins_r,
            'sells':              total_sells_bins_r,
            'net':                total_net_bins,
            'customers':          customers_data,
            'session_date':       session_date.isoformat(),
            'session_open_iso':   session_start.isoformat(),
            'session_close_iso':  session_end.isoformat(),
            'prev_session_date':  prev_session_date.isoformat(),
            'next_session_date':  (next_session_date.isoformat() if next_session_date else None),
            'is_current_session': session_date == _today_session,
        },
        'user_sessions': {
            'hours':            hours_list,
            'counts':           sessions_list,
            'names':            {h: sorted(session_names.get(h, set()))
                                 for h in hours_list if session_names.get(h)},
            'active_now':       active_now,
            'active_customers': active_customers,
            'peak':             max(peak, active_now),
            'session_date':     session_date.isoformat() if date_param else None,
            'mode':             'historical' if date_param else 'rolling_24h',
        },
        'summary': {
            'total_buys':       round(total_buys_all, 2),
            'total_sells':      round(total_sells_all, 2),
            'net_flow':         round(total_buys_all - total_sells_all, 2),
            'buy_count':        total_buy_count,
            'sell_count':       total_sell_count,
            'active_now':       active_now,
            'active_customers': active_customers,
            'peak_sessions':    max(peak, active_now),
        },
        'trading_modes': _get_customer_trading_modes(),
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


@app.route('/api/admin/api-usage')
@admin_required
def api_admin_api_usage():
    """API call tracking — surfaces proximity to Alpaca's 200/min rate limit
    (primary gauge) plus daily totals and history (secondary)."""
    try:
        db = _shared_db()
        today       = db.get_api_call_counts()
        history     = db.get_api_call_history(days=5)
        current_rate = db.get_api_call_rate(window_seconds=60)
        peak_rate   = db.get_api_call_peak_rate(window_seconds=60, lookback_hours=24)
        return jsonify({
            'today':        today,
            'history':      history,
            'current_rate': current_rate,     # calls in last 60s
            'peak_rate':    peak_rate,        # worst 60s window in last 24h
            'rate_limit':   200,              # Alpaca free/paper tier req/min
        })
    except Exception as e:
        return jsonify({'today': {'total': 0}, 'history': [],
                        'current_rate': 0, 'peak_rate': 0, 'rate_limit': 200,
                        'error': str(e)})


@app.route('/api/admin/alerts', methods=['GET'])
@admin_required
def api_admin_alerts():
    """List admin alerts (validator / fault / bias). Default: unresolved only."""
    try:
        db = _shared_db()
        unresolved_only = request.args.get('unresolved_only', '1') == '1'
        severity = request.args.get('severity') or None
        limit = min(int(request.args.get('limit', 100)), 500)
        alerts = db.get_admin_alerts(limit=limit, unresolved_only=unresolved_only,
                                      severity=severity)
        unresolved_count = db.count_admin_alerts(unresolved_only=True)
        return jsonify({'alerts': alerts, 'unresolved_count': unresolved_count})
    except Exception as e:
        return jsonify({'alerts': [], 'unresolved_count': 0, 'error': str(e)})


@app.route('/api/admin/alerts/resolve', methods=['POST'])
@admin_required
def api_admin_alerts_resolve():
    """Mark an admin alert as resolved."""
    data = request.get_json(silent=True) or {}
    alert_id = data.get('id')
    if not alert_id:
        return jsonify({'ok': False, 'error': 'id required'}), 400
    try:
        db = _shared_db()
        db.resolve_admin_alert(int(alert_id))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── START ──────────────────────────────────────────────────────────────────


# ── SESSION SNAPSHOT BACKGROUND THREAD ──────────────────────────────────────
def _session_snapshot_loop():
    """Record active session count + names every 60 seconds. Persists to shared DB."""
    import time as _t
    import json as _json

    # Load persisted history on startup
    try:
        shared = _shared_db()
        with shared.conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS session_history (
                ts TEXT PRIMARY KEY, count INTEGER, names TEXT)""")
            rows = c.execute(
                "SELECT ts, count, names FROM session_history ORDER BY ts DESC LIMIT 1440"
            ).fetchall()
            for r in reversed(rows):
                try:
                    names = _json.loads(r['names']) if r['names'] else []
                except Exception:
                    names = []
                _session_hourly.append((r['ts'], r['count'], names))
        if rows:
            log.info(f"Loaded {len(rows)} session history snapshots from DB")
    except Exception as e:
        log.debug(f"Session history load: {e}")

    while True:
        _t.sleep(60)
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            ts = now.strftime('%Y-%m-%dT%H:%M')
            with _session_activity_lock:
                active_names = []
                for cid, v in _session_activity.items():
                    if (now - v['last_activity']).total_seconds() < 900:
                        try:
                            name = auth.get_display_name_by_id(cid)
                        except Exception:
                            name = cid[:8]
                        active_names.append(name)
            _session_hourly.append((ts, len(active_names), active_names))

            # Persist to shared DB
            try:
                shared = _shared_db()
                with shared.conn() as c:
                    c.execute("""CREATE TABLE IF NOT EXISTS session_history (
                        ts TEXT PRIMARY KEY, count INTEGER, names TEXT)""")
                    c.execute(
                        "INSERT OR REPLACE INTO session_history (ts, count, names) VALUES (?, ?, ?)",
                        (ts, len(active_names), _json.dumps(active_names)))
                    # Keep only last 1440 rows (24h at 1/min)
                    c.execute(
                        "DELETE FROM session_history WHERE ts NOT IN "
                        "(SELECT ts FROM session_history ORDER BY ts DESC LIMIT 1440)")
            except Exception:
                pass
        except Exception:
            pass

_snap_thread = _threading.Thread(target=_session_snapshot_loop, daemon=True)
_snap_thread.start()

# ── Module-level init (runs on import — required for gunicorn) ────────────
auth.init_auth_db()
auth.ensure_admin_account()
auth.ensure_owner_customer()
log.info(f"Synthos Portal initialized — port {PORT} | Pi: {PI_ID}")

if __name__ == '__main__':
    # Dev-only fallback (production uses gunicorn)
    log.info(f"Starting Flask dev server on port {PORT}")
    log.info(f"Kill switch: {'ACTIVE' if kill_switch_active() else 'clear'}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
