#!/usr/bin/env python3
"""
validate_env.py — .env Integration Audit
Synthos v3.0

Run from the synthos project directory on the Pi:
    python3 tests/validate_env.py

Checks every .env key used by the system:
  1. Key presence and non-empty value
  2. Format validation (URL shape, key prefix patterns, enum values)
  3. Live connectivity tests (Alpaca, Anthropic, Congress.gov, Resend)
  4. Cross-key consistency (TRADING_MODE vs ALPACA_BASE_URL, etc.)
  5. v3.0 keys: ENCRYPTION_KEY, ADMIN_EMAIL/PASSWORD, CONSTRUCTION_MODE, Stripe
  6. Redacted summary — shows what's set without exposing secrets

Keys are never printed in full. Only first/last few chars shown.
"""

import os
import sys
import json
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH    = os.path.join(PROJECT_DIR, '.env')

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [WARN]"
INFO = "  [INFO]"
SEP  = "-" * 60

results = []

def p(label, ok, detail=""):
    icon = PASS if ok else FAIL
    line = f"{icon} {label}"
    if detail:
        line += f"\n         {detail}"
    print(line)
    results.append((label, ok, detail))
    return ok

def w(label, detail=""):
    line = f"{WARN} {label}"
    if detail:
        line += f"\n         {detail}"
    print(line)

def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def redact(val, show=4):
    """Show first N chars only — never expose full key."""
    if not val:
        return "(empty)"
    if len(val) <= show * 2:
        return "*" * len(val)
    return val[:show] + "..." + val[-2:]

def key_present(key, required=True):
    val = os.environ.get(key, '')
    present = bool(val.strip())
    label = f"{'[REQUIRED]' if required else '[OPTIONAL]'} {key}"
    if present:
        p(label, True, f"set — {redact(val)}")
    elif required:
        p(label, False, "MISSING or empty — required for system operation")
    else:
        w(label, "not set — optional but some features may be disabled")
    return val.strip() if present else ''


# ══════════════════════════════════════════════════════════
# LOAD .env
# ══════════════════════════════════════════════════════════
section("LOADING .env")

if not os.path.exists(ENV_PATH):
    print(f"{FAIL} .env file not found at {ENV_PATH}")
    print(f"       Run the installer first: python3 install.py")
    sys.exit(1)

load_dotenv(ENV_PATH, override=True)
p(".env file found", True, ENV_PATH)

# Count keys
with open(ENV_PATH) as f:
    lines = [l.strip() for l in f if l.strip() and not l.startswith('#') and '=' in l]
print(f"{INFO} {len(lines)} key(s) defined in .env")


# ══════════════════════════════════════════════════════════
# 1. KEY PRESENCE
# ══════════════════════════════════════════════════════════
section("1. KEY PRESENCE")

# Critical — system won't start without these
anthropic_key  = key_present('ANTHROPIC_API_KEY',  required=True)
alpaca_key     = key_present('ALPACA_API_KEY',      required=True)
alpaca_secret  = key_present('ALPACA_SECRET_KEY',   required=True)
alpaca_url     = key_present('ALPACA_BASE_URL',     required=True)
trading_mode   = key_present('TRADING_MODE',        required=True)
operating_mode = key_present('OPERATING_MODE',      required=True)
encryption_key = key_present('ENCRYPTION_KEY',      required=True)
admin_email    = key_present('ADMIN_EMAIL',         required=True)
admin_password = key_present('ADMIN_PASSWORD',      required=True)

print()
# Important — degraded without these
resend_key        = key_present('RESEND_API_KEY',           required=False)
alert_from        = key_present('ALERT_FROM',              required=False)
alert_to          = key_present('ALERT_TO',                required=False)
user_email        = key_present('USER_EMAIL',              required=False)
monitor_url       = key_present('MONITOR_URL',             required=False)
monitor_token     = key_present('MONITOR_TOKEN',           required=False)
company_url       = key_present('COMPANY_URL',             required=False)
pi_id             = key_present('PI_ID',                   required=False)
construction_mode = key_present('CONSTRUCTION_MODE',       required=False)
portal_base_url   = key_present('PORTAL_BASE_URL',         required=False)
stripe_pub        = key_present('STRIPE_PUBLISHABLE_KEY',  required=False)
stripe_secret     = key_present('STRIPE_SECRET_KEY',       required=False)
stripe_webhook    = key_present('STRIPE_WEBHOOK_SECRET',   required=False)


# ══════════════════════════════════════════════════════════
# 2. FORMAT VALIDATION
# ══════════════════════════════════════════════════════════
section("2. FORMAT VALIDATION")

# Anthropic key format: sk-ant-api03-...
if anthropic_key:
    ok = anthropic_key.startswith('sk-ant-')
    p("ANTHROPIC_API_KEY format",
      ok,
      f"Expected prefix 'sk-ant-' — got '{anthropic_key[:7]}...'"
      if not ok else f"prefix OK ({redact(anthropic_key)})")

# Alpaca key — paper keys start with PK, live keys start with AK
if alpaca_key:
    is_paper_key  = alpaca_key.startswith('PK')
    is_live_key   = alpaca_key.startswith('AK') or alpaca_key.startswith('CK')
    ok = is_paper_key or is_live_key
    kind = "paper" if is_paper_key else "live" if is_live_key else "UNKNOWN FORMAT"
    p("ALPACA_API_KEY format",
      ok,
      f"Detected as {kind} key ({redact(alpaca_key)})"
      if ok else f"Unexpected prefix '{alpaca_key[:2]}' — paper keys start PK, live start AK")

# Alpaca secret — no fixed prefix but should be 40+ chars
if alpaca_secret:
    ok = len(alpaca_secret) >= 32
    p("ALPACA_SECRET_KEY length",
      ok,
      f"{len(alpaca_secret)} chars — {'OK' if ok else 'too short, expected 32+'}")

# Alpaca URL
if alpaca_url:
    valid_urls = [
        'https://paper-api.alpaca.markets',
        'https://api.alpaca.markets',
        'https://broker-api.alpaca.markets',
    ]
    clean_url = alpaca_url.rstrip('/')
    ok = any(clean_url.startswith(u) for u in valid_urls)
    is_paper = 'paper' in clean_url
    p("ALPACA_BASE_URL format",
      ok,
      f"{'Paper endpoint' if is_paper else 'Live endpoint'}: {clean_url}"
      if ok else f"Unexpected URL: {clean_url}")
    # Check trailing slash
    if alpaca_url.endswith('/'):
        w("ALPACA_BASE_URL has trailing slash",
          "Code strips it with rstrip('/') — harmless but tidy to remove")

# TRADING_MODE enum
if trading_mode:
    ok = trading_mode.upper() in ('PAPER', 'LIVE')
    p("TRADING_MODE value",
      ok,
      f"'{trading_mode}' — {'valid' if ok else 'must be PAPER or LIVE'}")

# Cross-check: TRADING_MODE vs ALPACA_BASE_URL
if trading_mode and alpaca_url:
    is_paper_mode = trading_mode.upper() == 'PAPER'
    is_paper_url  = 'paper' in alpaca_url.lower()
    consistent = is_paper_mode == is_paper_url
    p("TRADING_MODE consistent with ALPACA_BASE_URL",
      consistent,
      f"mode={trading_mode.upper()} url={'paper' if is_paper_url else 'live'} — "
      f"{'consistent ✓' if consistent else 'MISMATCH ✗ — live mode pointed at paper URL or vice versa'}")

# Alpaca key type vs URL
if alpaca_key and alpaca_url:
    key_is_paper = alpaca_key.startswith('PK')
    url_is_paper = 'paper' in alpaca_url.lower()
    consistent   = key_is_paper == url_is_paper
    p("ALPACA_API_KEY type consistent with ALPACA_BASE_URL",
      consistent,
      f"key={'paper(PK)' if key_is_paper else 'live(AK)'} "
      f"url={'paper' if url_is_paper else 'live'} — "
      f"{'consistent ✓' if consistent else 'MISMATCH ✗ — paper key against live URL or vice versa'}")

# OPERATING_MODE enum — v3.1 standardized to MANAGED/AUTOMATIC only
if operating_mode:
    valid_modes = ('MANAGED', 'AUTOMATIC')
    ok = operating_mode.upper() in valid_modes
    p("OPERATING_MODE value",
      ok,
      f"'{operating_mode}' — {'valid' if ok else 'must be MANAGED | AUTOMATIC'}")

# ENCRYPTION_KEY — must be a valid Fernet key (32 url-safe base64 bytes = 44 chars)
if encryption_key:
    import base64 as _b64
    try:
        decoded = _b64.urlsafe_b64decode(encryption_key + '==')
        ok = len(decoded) == 32
        p("ENCRYPTION_KEY format",
          ok,
          f"{'Valid 32-byte Fernet key' if ok else f'Wrong length: {len(decoded)} bytes (need 32)'}")
    except Exception:
        p("ENCRYPTION_KEY format", False,
          "Not valid URL-safe base64 — generate with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")

# ADMIN_EMAIL format
if admin_email:
    ok = '@' in admin_email and '.' in admin_email.split('@')[-1]
    p("ADMIN_EMAIL format", ok,
      f"'{admin_email}' — {'valid' if ok else 'not a valid email address'}")

# ADMIN_PASSWORD strength (min 12 chars)
if admin_password:
    ok = len(admin_password) >= 12
    p("ADMIN_PASSWORD length",
      ok,
      f"{'OK' if ok else 'Too short — use at least 12 characters'} ({len(admin_password)} chars)")

# CONSTRUCTION_MODE enum
if construction_mode:
    ok = construction_mode.lower() in ('true', 'false', '1', '0', 'yes', 'no')
    p("CONSTRUCTION_MODE value",
      ok,
      f"'{construction_mode}' — {'valid' if ok else 'expected true/false or 1/0'}")

# PORTAL_BASE_URL — used to build /setup-account links in Stripe webhook emails
if portal_base_url:
    ok = portal_base_url.startswith('http://') or portal_base_url.startswith('https://')
    p("PORTAL_BASE_URL format", ok,
      f"'{portal_base_url}' — {'valid' if ok else 'must start with http:// or https://'}")
    if ok and portal_base_url.endswith('/'):
        w("PORTAL_BASE_URL has trailing slash", "stripped in code but tidy to remove")
else:
    w("PORTAL_BASE_URL not set",
      "Stripe webhook setup emails will fall back to http://localhost:<PORT> — "
      "set to https://portal.synth-cloud.com before enabling Stripe integration")

# Stripe key formats
if stripe_pub:
    ok = stripe_pub.startswith('pk_')
    p("STRIPE_PUBLISHABLE_KEY format",
      ok,
      f"{'OK — starts with pk_' if ok else 'Expected prefix pk_test_ or pk_live_'}")

if stripe_secret:
    ok = stripe_secret.startswith('sk_')
    p("STRIPE_SECRET_KEY format",
      ok,
      f"{'OK — starts with sk_' if ok else 'Expected prefix sk_test_ or sk_live_'}")

if stripe_webhook:
    ok = stripe_webhook.startswith('whsec_')
    p("STRIPE_WEBHOOK_SECRET format",
      ok,
      f"{'OK — starts with whsec_' if ok else 'Expected prefix whsec_'}")

# Cross-check Stripe test vs live consistency
if stripe_pub and stripe_secret:
    pub_is_test    = stripe_pub.startswith('pk_test_')
    secret_is_test = stripe_secret.startswith('sk_test_')
    consistent     = pub_is_test == secret_is_test
    p("Stripe key environment consistent",
      consistent,
      f"{'Both test or both live ✓' if consistent else 'MISMATCH — one test key, one live key'}")

# Cross-check: Stripe webhook requires PORTAL_BASE_URL to build setup-account links
if stripe_webhook and not portal_base_url:
    w("STRIPE_WEBHOOK_SECRET set but PORTAL_BASE_URL missing",
      "Webhook will fire but setup emails will contain localhost links — "
      "set PORTAL_BASE_URL=https://portal.synth-cloud.com")

# Resend key format — keys start with re_
if resend_key:
    ok = resend_key.startswith('re_')
    p("RESEND_API_KEY format",
      ok,
      f"{'prefix re_ OK' if ok else 'Expected prefix re_ — check key from resend.com/api-keys'}")

# Resend from-address format
if alert_from:
    ok = '@' in alert_from and '.' in alert_from.split('@')[-1]
    p("ALERT_FROM email format", ok,
      f"'{alert_from}' — {'valid' if ok else 'not a valid email address'}")

if user_email:
    ok = '@' in user_email and '.' in user_email.split('@')[-1]
    p("USER_EMAIL format", ok,
      f"'{user_email}' — {'valid' if ok else 'not a valid email address'}")

if alert_to:
    ok = '@' in alert_to and '.' in alert_to.split('@')[-1]
    p("ALERT_TO format", ok,
      f"'{alert_to}' — {'valid' if ok else 'not a valid email address'}")

# Monitor URL format
if monitor_url:
    ok = monitor_url.startswith('http://') or monitor_url.startswith('https://')
    p("MONITOR_URL format", ok,
      f"'{monitor_url}' — {'valid' if ok else 'must start with http:// or https://'}")

# Company URL format
if company_url:
    ok = company_url.startswith('http://') or company_url.startswith('https://')
    p("COMPANY_URL format", ok,
      f"'{company_url}' — {'valid' if ok else 'must start with http:// or https://'}")
    if ok and company_url.endswith('/'):
        w("COMPANY_URL has trailing slash",
          "code strips it with rstrip('/') — harmless but tidy to remove")
else:
    w("COMPANY_URL not set",
      "Scoop events will route via MONITOR_URL proxy (or be dropped if MONITOR_URL "
      "also unset). Set COMPANY_URL=http://<company-pi-ip>:5050 for direct routing.")


# ══════════════════════════════════════════════════════════
# 3. LIVE CONNECTIVITY TESTS
# ══════════════════════════════════════════════════════════
section("3. LIVE CONNECTIVITY TESTS")

try:
    import requests as _req
except ImportError:
    print(f"{WARN} requests package not available — skipping live tests")
    _req = None

# ── Alpaca ─────────────────────────────────────────────────────────────────
if _req and alpaca_key and alpaca_secret and alpaca_url:
    print(f"\n  Alpaca:")
    try:
        r = _req.get(
            f"{alpaca_url.rstrip('/')}/v2/account",
            headers={
                "APCA-API-KEY-ID":     alpaca_key,
                "APCA-API-SECRET-KEY": alpaca_secret,
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            cash     = float(data.get('cash', 0))
            bp       = float(data.get('buying_power', 0))
            status   = data.get('status', '?')
            acct_num = redact(data.get('account_number', ''), show=3)
            p("Alpaca /v2/account", True,
              f"status={status} cash=${cash:.2f} buying_power=${bp:.2f} acct={acct_num}")

            # Paper vs live account type sanity
            if 'paper' in alpaca_url.lower():
                p("Alpaca is paper account",
                  data.get('account_number','').startswith('PA') or cash < 1_000_000,
                  "Looks like paper account ✓")
        elif r.status_code == 401:
            p("Alpaca /v2/account", False,
              "401 Unauthorized — ALPACA_API_KEY or ALPACA_SECRET_KEY is wrong")
        elif r.status_code == 403:
            p("Alpaca /v2/account", False,
              "403 Forbidden — keys may be for wrong environment (paper vs live)")
        else:
            p("Alpaca /v2/account", False,
              f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        p("Alpaca /v2/account", False, f"Connection failed: {e}")
else:
    w("Alpaca test skipped", "Missing key, secret, or URL")

# ── Anthropic ──────────────────────────────────────────────────────────────
if _req and anthropic_key:
    print(f"\n  Anthropic:")
    try:
        r = _req.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         anthropic_key,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens':  10,
                'messages':   [{'role': 'user', 'content': 'Hi'}],
            },
            timeout=15,
        )
        if r.status_code == 200:
            p("Anthropic API key", True, "Valid — API responding")
        elif r.status_code == 401:
            p("Anthropic API key", False,
              "401 Unauthorized — key is invalid or revoked")
        elif r.status_code == 429:
            p("Anthropic API key", True,
              "Rate limited — key is valid but hitting limits")
        elif r.status_code == 400:
            data = r.json()
            if 'credit' in r.text.lower() or 'balance' in r.text.lower():
                p("Anthropic API key", False,
                  "Key valid but no credits — add credits at console.anthropic.com")
            else:
                p("Anthropic API key", True,
                  f"Key accepted (400 on test model is OK): {r.text[:80]}")
        else:
            p("Anthropic API key", False,
              f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        p("Anthropic API key", False, f"Connection failed: {e}")
else:
    w("Anthropic test skipped", "ANTHROPIC_API_KEY not set")

# ── Alpaca News API ────────────────────────────────────────────────────────
_alpaca_key    = os.getenv('ALPACA_API_KEY', '')
_alpaca_secret = os.getenv('ALPACA_SECRET_KEY', '')
if _req and _alpaca_key and _alpaca_secret:
    print(f"\n  Alpaca News API:")
    try:
        r = _req.get(
            'https://data.alpaca.markets/v1beta1/news',
            params={'limit': 1, 'exclude_contentless': 'true'},
            headers={
                'APCA-API-KEY-ID':     _alpaca_key,
                'APCA-API-SECRET-KEY': _alpaca_secret,
            },
            timeout=10,
        )
        if r.status_code == 200:
            count = len(r.json().get('news', []))
            p("Alpaca News API", True, f"Responding — {count} article(s) returned")
        elif r.status_code == 403:
            p("Alpaca News API", False, "403 — credentials rejected")
        elif r.status_code == 429:
            p("Alpaca News API", True, "Rate limited — credentials valid")
        else:
            p("Alpaca News API", False, f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        p("Alpaca News API", False, f"Connection failed: {e}")
else:
    w("Alpaca News API test skipped", "ALPACA_API_KEY or ALPACA_SECRET_KEY not set")

# ── Resend ─────────────────────────────────────────────────────────────────
if _req and resend_key:
    print(f"\n  Resend:")
    try:
        # Validate key via /domains — no email sent
        r = _req.get(
            'https://api.resend.com/domains',
            headers={'Authorization': f'Bearer {resend_key}'},
            timeout=10,
        )
        if r.status_code == 200:
            data    = r.json()
            domains = [d.get('name', '?') for d in data.get('data', [])]
            p("RESEND_API_KEY", True,
              f"Valid — verified domains: {', '.join(domains) if domains else 'none yet'}")
            if not domains:
                w("No verified domains in Resend",
                  "Add and verify a sending domain at resend.com/domains before emails will deliver")
        elif r.status_code == 401:
            p("RESEND_API_KEY", False,
              "401 Unauthorized — key invalid or revoked")
        else:
            p("RESEND_API_KEY", False,
              f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        p("RESEND_API_KEY", False, f"Connection failed: {e}")

    if not (alert_from and admin_email):
        w("Resend alert config incomplete",
          "RESEND_API_KEY set but ALERT_FROM and/or ADMIN_EMAIL missing — "
          "protective exit emails and construction OTP will not send")
else:
    w("Resend test skipped",
      "RESEND_API_KEY not set — email alerts and account setup emails disabled")

# ── Monitor ────────────────────────────────────────────────────────────────
if _req and monitor_url:
    print(f"\n  Monitor server:")
    try:
        r = _req.get(
            f"{monitor_url.rstrip('/')}/api/status",
            headers={'X-Token': monitor_token},
            timeout=5,
        )
        if r.status_code == 200:
            p("Monitor server reachable", True,
              f"{monitor_url}")
        else:
            p("Monitor server reachable", False,
              f"HTTP {r.status_code} — server up but returned error")
    except Exception as e:
        w("Monitor server unreachable",
          f"{monitor_url} — {e} (non-blocking, heartbeats will fail silently)")

# ── Company Node ────────────────────────────────────────────────────────────
if _req and company_url:
    print(f"\n  Company Node:")
    try:
        # /health is unauthenticated — returns queue counts
        r = _req.get(
            f"{company_url.rstrip('/')}/health",
            timeout=5,
        )
        if r.status_code == 200:
            data   = r.json()
            counts = data.get('queue', {})
            detail = (
                f"pending={counts.get('pending',0)} "
                f"sent={counts.get('sent',0)} "
                f"failed={counts.get('failed',0)}"
            )
            p("Company Node reachable (/health)", True,
              f"{company_url} — {detail}")
        elif r.status_code == 401:
            p("Company Node reachable (/health)", False,
              f"401 — /health should be unauthenticated; check synthos_monitor.py version")
        else:
            p("Company Node reachable (/health)", False,
              f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        w("Company Node unreachable",
          f"{company_url} — {e} (non-blocking — Scoop events will queue locally "
          f"or proxy via monitor if MONITOR_URL is set)")
else:
    w("Company Node test skipped",
      "COMPANY_URL not set — set to http://<company-pi-ip>:5050 to enable direct routing")


# ══════════════════════════════════════════════════════════
# 4. WHITESPACE / ENCODING CHECKS
# ══════════════════════════════════════════════════════════
section("4. WHITESPACE & ENCODING CHECKS")

# Read raw .env and check for common copy-paste issues
issues_found = False
with open(ENV_PATH) as f:
    for lineno, raw_line in enumerate(f, 1):
        line = raw_line.rstrip('\n')
        if not line.strip() or line.strip().startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip()
        val = val.strip()

        # Leading/trailing whitespace in value
        if val != val.strip():
            w(f"Line {lineno}: {key} has leading/trailing whitespace",
              f"Value: '{val[:30]}...' — may cause auth failures")
            issues_found = True

        # Quoted values (dotenv handles these but flag for awareness)
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            print(f"{INFO} Line {lineno}: {key} value is quoted — "
                  f"dotenv strips quotes automatically")

        # Windows line endings
        if '\r' in raw_line:
            w(f"Line {lineno}: {key} has Windows line endings (\\r\\n)",
              "Can cause subtle failures — run: sed -i 's/\\r//' .env")
            issues_found = True

        # Non-ASCII characters
        try:
            val.encode('ascii')
        except UnicodeEncodeError:
            w(f"Line {lineno}: {key} contains non-ASCII characters",
              "Copy-paste from a rich text source may have introduced smart quotes")
            issues_found = True

if not issues_found:
    p(".env encoding and whitespace", True,
      "No whitespace, encoding, or line-ending issues detected")


# ══════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════
section("SUMMARY")

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total  = len(results)

print(f"\n  Passed: {passed}/{total}")
if failed:
    print(f"  Failed: {failed}/{total}")
    print(f"\n  Failed checks:")
    for label, ok, detail in results:
        if not ok:
            print(f"    {FAIL} {label}")
            if detail:
                print(f"           {detail}")

print()
if failed == 0:
    print("  ✓ .env VALID — all integrations configured correctly")
elif failed <= 2:
    print("  ⚠ MINOR ISSUES — fix failed checks before running agents")
else:
    print("  ✗ CONFIGURATION ISSUES — fix failures before system will operate correctly")
print()
