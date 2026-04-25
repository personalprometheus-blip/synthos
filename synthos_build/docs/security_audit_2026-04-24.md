# Pre-Launch Security Audit — Retail Portal (pi5)

**Date:** 2026-04-24
**Scope:** `synthos_build/src/retail_portal.py` (102 routes), `auth.py`,
session/cookie config, host hardening on pi5. Out of scope: pi4b
command portal, network-layer pen-testing, exploit development against
upstream services (Cloudflare, Alpaca).
**Method:** static analysis + live test-customer probe (35 endpoints
hit with real HTTP after creating a sandbox customer in `auth.db`,
test row deleted on completion).

---

## Executive summary

Two **CRITICAL** customer→admin privilege escalations existed and are
now closed. One **HIGH** information-disclosure route is closed. One
**HIGH** architectural finding (LAN-side portal exposure) requires a
DNS/tunnel reconfig and is documented for follow-up. Three **MEDIUM**
findings are noted — one fixed, two have remediation paths and were
left for operator decision. Foundational primitives (password hashing,
session cookies, encryption-at-rest, IDOR-resistant DB resolution) are
all sound.

**Bottom line:** the portal is safe to onboard customers to **after
the four landed fixes above**, with the LAN-bind item explicitly
acknowledged as a deferred architectural improvement. There are no
remaining customer→admin paths.

---

## Findings

### 🔴 CRITICAL-1 — `/api/admin-override` POST allows trading-mode hijack ✅ FIXED

**File:** `src/retail_portal.py:2607` (pre-fix), commit `b3c9824`.

**Problem.** Auth gate was
```python
if token != MONITOR_TOKEN and not is_authenticated():
    return 401
```
A logged-in customer satisfied the second clause. The endpoint then
wrote to `.env` and forced every customer's mode:
- `ADMIN_TRADING_GATE` (PAPER ↔ LIVE)
- `ADMIN_OPERATING_MODE` (MANAGED ↔ AUTOMATIC)
- `TRADING_MODE` (separate gate, also flipped)

**Impact.** Any authenticated customer could flip the entire system
from paper to live trading.

**Fix.** Split GET (read state — fine for customer UI) from POST
(write state — admin/token only):
```python
if token != MONITOR_TOKEN and not (is_authenticated() and is_admin()):
    return 401
```
Plus a warning log of the attempt for audit trail.

**Verified.** Phase-4 probe confirms 401 returned to customer (was
previously 200). Admin still works.

---

### 🔴 CRITICAL-2 — `/api/keys` POST allows global `.env` mutation ✅ FIXED

**File:** `src/retail_portal.py:2754` (pre-fix), commit `b3c9824`.

**Problem.** Same OR pattern. Whitelist included `LIVE_TRADING_ENABLED`,
`ANTHROPIC_API_KEY`, `MONITOR_TOKEN`, `RESEND_API_KEY`, all Stripe keys,
and the full set of trading-mode levers — all written to global `.env`.

**Impact.** Any authenticated customer could:
- Flip `LIVE_TRADING_ENABLED=true`
- Replace `ANTHROPIC_API_KEY` (cost-shifting attack: bill operator OR
  exfiltrate prompts to attacker's key)
- Rotate `MONITOR_TOKEN` (kill cross-node auth)
- Replace `RESEND_API_KEY` (intercept all outgoing email)
- Rotate Stripe keys (intercept payment events)

**Fix.** Two-tier auth: top-level gate kept (allows customer to write
their own Alpaca creds — those go to per-customer encrypted storage,
not `.env`). Added second admin-only gate before the `.env` write loop:
```python
env_write_allowed = token_ok or (is_authenticated() and is_admin())
if env_keys_attempted and not env_write_allowed:
    for k in env_keys_attempted:
        errors.append(f"{k}: admin only")
        data.pop(k, None)
```
Customer attempts log a WARNING with the customer-id-prefix and the
list of attempted keys.

**Verified.** Probe sent 3 attacks (LIVE_TRADING_ENABLED, MONITOR_TOKEN,
ANTHROPIC_API_KEY) — all rejected per-key with "admin only" in the
response body. No `.env` mutation occurred.

---

### 🔴 HIGH-3 — `/api/audit` discloses system audit data ✅ FIXED

**File:** `src/retail_portal.py:5003`, commit `737df51`.

**Problem.** Endpoint was `@login_required` but returns the
`.audit_latest.json` blob — system-level audit findings including
file paths, log lines, and severity-tagged issues. Useful infrastructure
recon for an attacker.

**Fix.** Changed decorator to `@admin_required`. Added docstring noting
the prior misclassification.

**Verified.** Probe returns 403 to customer.

---

### 🟠 HIGH-4 — Retail portal binds `0.0.0.0:5001` (LAN exposure)

**Status.** Documented, not fixed. Architectural change required.

**Problem.** Gunicorn binds to all interfaces. pi4b's cloudflared has
`portal.synth-cloud.com → http://10.0.0.11:5001`, so the LAN binding
is intentional today. But it means:
- Anyone on the same LAN/Wi-Fi as pi5 can hit the portal directly over
  plain HTTP, bypassing Cloudflare's TLS, CSP, WAF, and rate limiting.
- `Secure`-cookie + SameSite=Strict prevent authenticated session use,
  but unauthenticated routes (`/login`, `/signup`, `/sso`,
  `/webhook/stripe`, `/api/admin/alert`, `/api/logs-audit`,
  `/api/admin-override` GET) are reachable.
- An attacker on the LAN sees the service exists, can probe for
  vulnerabilities without going through Cloudflare's protections,
  and can brute-force the in-memory rate limiter (which resets on
  portal restart).

**Recommended fix.** Move `portal.synth-cloud.com` to pi5's own
cloudflared tunnel (which already serves `app.synth-cloud.com` via
localhost). After that, gunicorn can bind `127.0.0.1:5001` safely:

1. Cloudflare DNS console: change CNAME for `portal.synth-cloud.com`
   from pi4b's tunnel to pi5's tunnel.
2. pi5 `/etc/cloudflared/config.yml`:
   ```
   - hostname: portal.synth-cloud.com
     service: http://localhost:5001
   ```
3. Remove the entry from pi4b's cloudflared config.
4. Restart cloudflared on both nodes.
5. Change gunicorn `--bind` from `0.0.0.0:5001` to `127.0.0.1:5001`.
6. `sudo systemctl daemon-reload && sudo systemctl restart synthos-portal`.

I attempted step 5 alone first and broke the public site for ~5 min
(commit `e94aaea` then revert `4a010a0`). DO NOT attempt the bind
change without doing the DNS/tunnel reconfig first.

**Risk while deferred.** Operator's home LAN is the immediate threat
surface. Acceptable if you trust everyone on the home Wi-Fi and don't
have IoT devices on the same VLAN. NOT acceptable post-launch when
customers are real and the system handles real money.

---

### 🟡 MEDIUM-5 — `/webhook/stripe` returned HTTP 500 when secret unset ✅ FIXED

**File:** `src/retail_portal.py:1340`, commit `737df51`.

**Problem.** Returned 500 ("webhook secret not configured") when
`STRIPE_WEBHOOK_SECRET` is unset. 5xx triggers Stripe retry storms
and obscures the actual cause.

**Fix.** Return 503 ("webhook handler not configured") — semantically
correct for "service exists but is misconfigured." No effect today
(Stripe not yet wired); matters when Stripe IS wired.

---

### 🟡 MEDIUM-6 — `pi516gb` has `NOPASSWD: ALL` sudo

**Status.** Operator decision required.

**Problem.** Phase-5 sweep showed:
```
User pi516gb may run the following commands on SentinelRetail:
    (ALL : ALL) ALL
    (ALL) NOPASSWD: ALL
```
Common pattern for single-user-host Pi setups. If the gunicorn worker
or any pi-user-context process is ever RCE'd, the attacker gets root
with no friction.

**Recommended remediation.**
- Convert to password-required sudo for general use.
- Optionally narrow systemd-restart commands to NOPASSWD via
  `/etc/sudoers.d/synthos-deploy`:
  ```
  pi516gb ALL=(root) NOPASSWD: /bin/systemctl restart synthos-portal.service, /bin/systemctl restart synthos-watchdog.service, /bin/systemctl reload synthos-portal.service
  ```
- Other privilege-required ops use sudo with password.

**Risk while deferred.** Internal threat only. Requires another bug
to chain to first. Acceptable on an isolated home network; revisit
post-launch.

---

### 🟡 MEDIUM-7 — Login rate limit is in-memory + per-IP only

**Status.** Defensive improvement, not yet planned.

**Problem.** `_login_attempts` is a `defaultdict(list)` keyed by IP.
Resets on portal restart. No per-account lockout — a brute-force
attacker can rotate through IPs (botnet, residential proxy) and hit
the same email indefinitely.

**Recommended remediation.**
- Persist rate-limit counters in a `login_attempts` table in `auth.db`.
- Add per-account lockout (e.g., 10 failures on the same `email_hash`
  → 15-min cooldown regardless of IP).
- Capture the User-Agent + IP for triage.

**Effort.** ~80 lines + 1 schema migration. Could be bundled with the
operational-hardening backlog item §3.

---

### 🟢 LOW-8 — `data/customers/` directory is mode 775

**Problem.** Per-customer signals.dbs live under `data/customers/<uuid>/`.
The parent directory was group/world-readable. Mitigated because
`auth.db` itself (the password store) is 0600.

**Recommended fix.**
```bash
sudo chmod 700 ~/synthos/synthos_build/data/customers
```

---

### 🟢 LOW-9 — `system_architecture.json` is world-readable

**Problem.** Mode `0664`. Leaks the topology + agent map if the file
becomes accessible some other way (e.g., a future log-bundle export
endpoint).

**Recommended fix.**
```bash
chmod 0600 ~/synthos/synthos_build/data/system_architecture.json
```

---

### 🟢 LOW-10 — CSP `script-src 'unsafe-inline'`

**Problem.** Cloudflare's CSP allows inline scripts. Common because
many Flask templates use inline `<script>` tags. Tightening would
require nonce-based CSP and template work.

**Risk.** Reduced XSS protection. Today, no user-controlled input
is rendered in a way that could inject script (verified during
Phase 4 — XSS probe `?<script>alert(1)</script>` returned 403 cleanly).

**Recommended remediation.** Migrate to nonce-based CSP when the
portal templates are next refactored. Not an emergency.

---

## What's working well

| Area | What's solid |
|---|---|
| Password hashing | PBKDF2-HMAC-SHA256, 480k iterations, fresh salt per-password. Meets OWASP 2023. |
| Session cookies | `Secure`, `HttpOnly`, `SameSite=Strict`, non-default name (`synthos_s`), 8h TTL, 15-min idle auto-logout for non-admin. |
| Encryption at rest | Fernet on email, name, phone, Alpaca creds in `auth.db`. Per-customer Alpaca creds isolated. |
| Email lookup | HMAC-SHA256 (keyed by `ENCRYPTION_KEY`) — no plaintext email queryable. |
| Customer DB resolution | `_customer_db()` reads `customer_id` from server session, not user input. **No IDOR via direct customer-id substitution** (verified by probe). |
| Customer settings | `/api/settings` uses explicit allowlist — `role` is NOT writable, no privilege escalation via settings. |
| Pre-login route gating | `before_request` whitelist of public routes; everything else gated. Token-only routes (`/api/logs-audit`, `/api/admin/halt-agent`, `/api/admin/alert`) have no `or is_authenticated()` escape. |
| File permissions | `auth.db` and `.env` both 0600, owner pi516gb. |
| Process argv | No secrets in `ps -ef` output (verified). |
| Log files | No secret leakage in last 1000 lines of every active log (verified pattern scan). |
| Cloudflare-side headers | HSTS (1 yr), X-Frame-Options: DENY, X-Content-Type-Options: nosniff, frame-ancestors: 'none'. |
| `/api/get-keys` | Returns obscured (4 chars + `…`) key previews — no full secrets ever returned to client. |
| Stripe webhook signature | When configured, validates HMAC-SHA256 + timestamp window (300s replay protection). |
| Encryption-key rotation | Tool exists at `tools/rotate_encryption_key.py` with self-test, ready for operator use (built earlier this session). |

---

## Probe summary (Phase 4)

35 probes executed against a live test customer with role=`customer`:

| Result | Count | Notes |
|---|---|---|
| ✓ Pass | 35/35 | After deploying the 3 fixes above |
| Critical reproductions | 4 | All correctly blocked (admin-override 401, keys × 3 with admin-only error) |
| Admin endpoint probes | 15 | All 403 |
| IDOR attempts | 3 | All correctly resolved (no cross-customer data leak) |
| Role-mutation attempts | 2 | Whitelist correctly dropped extra fields |
| SQLi probe | 1 | Path-routed to 404, no DB error |
| XSS recon | 1 | Reflected param echoed safely (403 returned) |
| Stripe webhook | 1 | 503 with clear message |

Test customer (`sec-audit-*@synthos-audit.local`) created in pi5's
auth.db, exercised, and deleted at end of each run. One residual was
found and cleaned in Phase 7 (run 2 errored out before reaching its
own cleanup). Final `auth.db` row count: 13 — 12 real customers + 1
admin (operator), 0 audit residuals.

---

## Recommended next steps

1. **Pre-launch:** complete the LAN-bind architectural fix
   (HIGH-4 above). 30 minutes including DNS propagation.
2. **Pre-launch:** narrow `pi516gb` sudo (MEDIUM-6).
3. **Pre-launch:** chmod `data/customers/` to 700 (LOW-8).
4. **Operator-decision:** persist the login rate limiter (MEDIUM-7).
   Required before any meaningful number of public users.
5. **Post-travel:** consider nonce-based CSP for the portal templates
   (LOW-10).
6. **Operationally:** the encryption-key-rotation tool (built earlier
   this session) is ready. Run a dry-run within 90 days to keep the
   key fresh, even if the operator-side never leaks.

---

## Out of scope for this audit (consider next round)

- pi4b command portal route audit (102 routes on pi5 was today's budget)
- Cross-node SSH trust between pi4b and pi5 (root-equivalent SSH keys
  are stored in `~/.ssh/` — should be reviewed for rotation policy)
- Cloudflare tunnel configuration (audit logs, access policies)
- Stripe webhook end-to-end (deferred until Stripe is actually wired)
- Network-layer pen-testing (Cloudflare-side DDoS/bot, OS-layer hardening)

---

## Audit artifacts

- Probe script: `/tmp/sec_audit_phase4.py` (NOT in repo — single-use
  tool with hardcoded localhost URL; recreate from this doc if needed
  for re-audit)
- Findings report: this file
- Commits:
  - `b3c9824` security: close 2 customer→admin privilege escalations
  - `737df51` security: fix Phase-4 audit findings — /api/audit + stripe-webhook 500
  - `e94aaea` security: bind retail portal to 127.0.0.1 (REVERTED in next)
  - `4a010a0` revert(security): undo 127.0.0.1 bind — broke portal.synth-cloud.com
