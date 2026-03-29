#!/usr/bin/env python3
"""
validate_env.py — .env Integration Audit
Synthos

Run from the synthos project directory on the Pi:
    python3 validate_env.py

Checks every .env key used by the system:
  1. Key presence and non-empty value
  2. Format validation (URL shape, key prefix patterns, enum values)
  3. Live connectivity tests (Alpaca, Anthropic, Congress.gov, SendGrid)
  4. Cross-key consistency (TRADING_MODE vs ALPACA_BASE_URL, etc.)
  5. Redacted summary — shows what's set without exposing secrets

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
anthropic_key  = key_present('ANTHROPIC_API_KEY', required=True)
alpaca_key     = key_present('ALPACA_API_KEY',     required=True)
alpaca_secret  = key_present('ALPACA_SECRET_KEY',  required=True)
alpaca_url     = key_present('ALPACA_BASE_URL',    required=True)
trading_mode   = key_present('TRADING_MODE',       required=True)
operating_mode = key_present('OPERATING_MODE',     required=True)

print()
# Important — degraded without these
congress_key   = key_present('CONGRESS_API_KEY',   required=False)
sendgrid_key   = key_present('SENDGRID_API_KEY',   required=False)
alert_from     = key_present('ALERT_FROM',         required=False)
alert_to       = key_present('ALERT_TO',           required=False)
user_email     = key_present('USER_EMAIL',         required=False)
monitor_url    = key_present('MONITOR_URL',        required=False)
monitor_token  = key_present('MONITOR_TOKEN',      required=False)
pi_id          = key_present('PI_ID',              required=False)
portal_pass    = key_present('PORTAL_PASSWORD',    required=False)


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

# OPERATING_MODE enum
if operating_mode:
    ok = operating_mode.upper() in ('SUPERVISED', 'AUTONOMOUS')
    p("OPERATING_MODE value",
      ok,
      f"'{operating_mode}' — {'valid' if ok else 'must be SUPERVISED or AUTONOMOUS'}")

# SendGrid from-address format
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

# ── Congress.gov ───────────────────────────────────────────────────────────
if _req and congress_key:
    print(f"\n  Congress.gov:")
    try:
        r = _req.get(
            'https://api.congress.gov/v3/bill',
            params={'api_key': congress_key, 'limit': 1, 'format': 'json'},
            timeout=10,
        )
        if r.status_code == 200:
            p("Congress.gov API key", True, "Valid — API responding")
        elif r.status_code == 403:
            p("Congress.gov API key", False,
              "403 — key invalid or not yet activated (can take 24h after signup)")
        elif r.status_code == 429:
            p("Congress.gov API key", True,
              "Rate limited — key is valid")
        else:
            p("Congress.gov API key", False,
              f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        p("Congress.gov API key", False, f"Connection failed: {e}")
else:
    w("Congress.gov test skipped", "CONGRESS_API_KEY not set")

# ── SendGrid ───────────────────────────────────────────────────────────────
if _req and sendgrid_key and alert_from and alert_to:
    print(f"\n  SendGrid:")
    try:
        # Just validate the key via /v3/user/profile — no email sent
        r = _req.get(
            'https://api.sendgrid.com/v3/user/profile',
            headers={'Authorization': f'Bearer {sendgrid_key}'},
            timeout=10,
        )
        if r.status_code == 200:
            data     = r.json()
            username = data.get('username', '?')
            p("SendGrid API key", True,
              f"Valid — account: {username}")
        elif r.status_code == 401:
            p("SendGrid API key", False,
              "401 Unauthorized — key invalid or revoked")
        elif r.status_code == 403:
            p("SendGrid API key", False,
              "403 Forbidden — key may have restricted scopes (needs mail.send)")
        else:
            p("SendGrid API key", False,
              f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        p("SendGrid API key", False, f"Connection failed: {e}")
elif sendgrid_key and not (alert_from and alert_to):
    w("SendGrid alert config incomplete",
      "SENDGRID_API_KEY set but ALERT_FROM and/or ALERT_TO missing — "
      "protective exit emails will not send")
else:
    w("SendGrid test skipped",
      "SENDGRID_API_KEY not set — protective exit emails disabled")

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
