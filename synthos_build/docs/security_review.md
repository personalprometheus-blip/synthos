# Synthos Retail — Security Review

**Living document.** Captures what's been audited, what's deferred,
what needs deeper testing in the future, and the gates before each
launch milestone.

The historical findings record from the 2026-04-24 audit lives at
[`security_audit_2026-04-24.md`](./security_audit_2026-04-24.md). This
file is the **forward-looking** plan; that file is the **point-in-time**
record.

---

## Current security posture (2026-04-24)

| Area | Status |
|---|---|
| Customer→admin privilege escalation paths | **Closed** (2 CRITICAL, 1 HIGH found + fixed) |
| Authenticated session security | **Strong** — Secure/HttpOnly/SameSite=Strict cookies, 8h TTL, 15-min idle auto-logout |
| Password storage | **Strong** — PBKDF2-HMAC-SHA256 480k iterations |
| PII at rest | **Strong** — Fernet encryption on email/name/phone/Alpaca creds |
| Cross-customer isolation | **Strong** — DB resolution via session-only customer_id |
| Cloudflare-side hardening | **Solid** — HSTS, CSP, X-Frame, frame-ancestors all configured |
| File permissions on secrets | **Solid** — 0600 on auth.db and .env |
| LAN-side portal exposure | **Architectural finding** — pi5:5001 reachable on LAN by design (pi4b's tunnel routes to 10.0.0.11) |
| Token-based flow security | **Not yet deeply audited** (see "Future testing" below) |
| File upload validation | **Reachability tested only**, not deeply probed |
| systemd / SSH / firewall hardening | **Not yet audited** |
| WAF / Cloudflare access policies | **Not yet audited** (requires dashboard access) |

---

## Pre-customer-launch checklist (gates before onboarding paying customers)

Each item is a hard requirement. Customer onboarding should not begin
until every box is checked.

### Architectural

- [ ] **HIGH-4: LAN-bind fix.** Move `portal.synth-cloud.com` from
  pi4b's cloudflared tunnel to pi5's own tunnel; rebind gunicorn to
  `127.0.0.1:5001`. Without this, anyone on the LAN can hit the portal
  directly bypassing Cloudflare's protections. **Procedure:**
  1. Cloudflare DNS console: change CNAME for `portal.synth-cloud.com`
     from pi4b's tunnel ID to pi5's tunnel ID.
  2. pi5 `/etc/cloudflared/config.yml`: add
     ```
     - hostname: portal.synth-cloud.com
       service: http://localhost:5001
     ```
  3. pi4b `/etc/cloudflared/config.yml`: remove the
     `portal.synth-cloud.com` entry.
  4. Restart cloudflared on both nodes.
  5. Edit `synthos_build/ops/systemd/pi5/synthos-portal.service`:
     `--bind 0.0.0.0:5001` → `--bind 127.0.0.1:5001`.
  6. `sudo cp` the unit file, `sudo systemctl daemon-reload`, restart.
  7. Verify `portal.synth-cloud.com` returns 200 publicly + LAN curl
     to `10.0.0.11:5001` is refused.

  **Do NOT do step 5 alone first** — broke the portal for ~5 min today
  (commit `e94aaea` → reverted in `4a010a0`).

- [ ] **MEDIUM-7: Persist login rate limiter.** Convert in-memory
  `_login_attempts` defaultdict to a `login_attempts` table in
  `auth.db`. Add per-account lockout (10 failures on same `email_hash`
  → 15-min cooldown regardless of IP). Capture User-Agent + IP for
  triage. ~80 lines + 1 schema migration.

- [ ] **MEDIUM-6: Narrow `pi516gb` sudo.** Replace `(ALL) NOPASSWD: ALL`
  with a `/etc/sudoers.d/synthos-deploy` file scoped to the specific
  systemctl restart commands needed for deploys. Operator decision:
  password-required for everything else, OR keep NOPASSWD for the
  short list of deploy commands only.

### Hardening

- [ ] **LOW-8: `chmod 700 ~/synthos/synthos_build/data/customers`** —
  per-customer DBs live there, parent dir was 775.

- [ ] **LOW-9: `chmod 0600 ~/synthos/synthos_build/data/system_architecture.json`** —
  topology info, currently 664.

### Token-flow audit (not yet performed)

- [ ] Audit `/forgot-password` flow:
  - Token TTL bounded?
  - Replay protection?
  - Rate-limited?
  - Email-enumeration resistant? (response identical for known/unknown email)
- [ ] Audit `/verify-email/<token>`:
  - Token expiration bounded?
  - One-time use?
  - Tied to specific account ID?
- [ ] Audit `/setup-account/<token>`:
  - Same questions as above
  - Can the token be guessed / brute-forced?
- [ ] Audit `/reset-password/<token>`:
  - All of the above
  - Does setting new password invalidate other active sessions?
- [ ] Audit `/sso` flow:
  - SSO_SECRET storage, rotation
  - URLSafeTimedSerializer configuration (TTL = 900s — verified, but
    audit replay window)
  - Token replay protection
  - What happens if the SSO key leaks?
- [ ] Password change flow (`/api/account/change-password`):
  - Requires current password? (verify yes)
  - Rate-limited?
  - Invalidates other sessions on success?
- [ ] Email change flow (`/api/account/change-email`):
  - Requires password?
  - Sends verification to old + new addresses?
  - Allows enumeration of registered emails?

### File-upload security (Phase 4.5 deferred)

- [ ] Path-traversal probe (`../../etc/passwd` filename)
- [ ] Content-type bypass (image with executable payload)
- [ ] Size limits enforced (DoS via massive POST)
- [ ] MIME sniffing handled
- [ ] Storage location isolated per-customer
- [ ] Filename sanitization

### Customer→customer info leak

- [ ] Can customer A see customer B's email/name via shared support
  thread, beta-test response, broadcast notification mention?
- [ ] Are pending-signup row counts visible to customers (timing-attack
  enumeration)?

---

## Pre-LIVE-trading checklist (additional gates before flipping TRADING_MODE=LIVE)

Live money on the line — these gates apply on top of customer-launch
gates.

- [ ] **Encryption-key rotation drilled** — run the rotation tool
  end-to-end against a copy of `auth.db` then live (already built;
  exercise once before launch).
- [ ] **Backup restore drilled** — pull a sample R2-encrypted backup,
  decrypt, verify signals.db / auth.db round-trip. Confirm 30-day
  retention is firing.
- [ ] **Operator 2FA on Cloudflare Zero Trust + GitHub** — both
  accounts have hardware-key 2FA, not TOTP-only.
- [ ] **Monitor token rotation procedure documented + drilled.**
- [ ] **Alpaca live keys stored in auth.db only, not .env.** Confirm
  no live keys exist in any `.env` file across pi4b/pi5/pi2w.
- [ ] **Cloudflare Access policy** restricts `command.synth-cloud.com`
  to specific authenticated identities (not public).
- [ ] **WAF rules reviewed** — bot-fight mode, custom rules for
  Stripe webhook IP allowlist, rate limiting at edge.
- [ ] **Incident response runbook** exists + operator has rehearsed
  steps for: account compromise, key leak, R2 bucket compromise,
  pi5 host compromise, dependency-supply-chain compromise.

---

## Future-testing scope (next audit pass)

The 2026-04-24 audit was an "engineer-in-a-day" pre-launch sweep.
Roughly 70% coverage of what a serious 1-day pen test would do. The
30% gap is captured in the pre-customer-launch checklist above; this
section is what to add for a **deeper, ongoing security program**.

### Active testing
- **Authenticated fuzzer pass** — Burp/ZAP-style hammering of every
  authenticated endpoint with malformed payloads, oversized inputs,
  unicode tricks, ID manipulation. Catches what spot-check can't.
- **Authentication flow chaining** — set up scripted attack sequences
  (e.g., "register → verify → reset password → take over admin").
- **Concurrent-request race condition tests** — fire N parallel writes
  at `/api/settings`, `/api/account/change-email`, etc.
- **DAST scanner against the public hostname** — Cloudflare-side
  protections in play; tests the real perimeter.

### Static testing
- **`bandit` security linter** for Python — automated scan for common
  Python security bugs. Run weekly in CI once CI exists.
- **Dependency vulnerability scan** — `pip-audit` or Snyk against
  the requirements set. Already partially covered by `company_librarian`
  but worth a dedicated scan.
- **Secret-scanning pre-commit hook** — git-secrets or
  `truffleHog` to catch accidental commits of API keys, passwords.

### Monitoring + audit trail
- **Audit log table** — `security_events` table that records: every
  login (success + fail), password change, email change, role change,
  admin override, settings write, key rotation, file upload. Operator
  can review weekly.
- **Anomaly detection** — alert on: new IP for an existing account,
  login from a new geolocation, password change without recent
  successful login, multiple failed logins followed by success
  (account-takeover signal), bulk download of customer data, settings
  rewritten outside normal hours.
- **Forensic readiness** — can we reconstruct "what did customer X do
  in the last 24 hours" from logs? Today: probably yes for trades, no
  for settings/profile changes. Audit log table closes the gap.

### Compliance angle (post-paying-customers)
- **GDPR/CCPA data deletion workflow** — already in backlog as
  CUSTOMER-DATA-DELETION. Pull current ages of customer data, define
  retention SLOs, write deletion runbook + automation.
- **SOC 2 Lite readiness gap analysis** — ~6 hours of work to map
  current controls to SOC 2 trust services criteria. Tells you what
  to invest in vs. what's already covered.
- **PII inventory** — formal list of every place customer PII (name,
  email, phone, address, broker creds) is stored. Today informally:
  `auth.db` (encrypted) + R2 backups (encrypted) + maybe email logs.
  Validate.

### Threat-model artifacts
- **Architecture data-flow diagram** with trust boundaries marked
  (already partially captured in `pipeline_audit_2026-04-24.md` and
  the System Architecture page).
- **Top-10 threat scenarios** with likelihood × impact rated:
  account takeover via email-reset compromise, Alpaca key exfil
  via portal RCE, R2 bucket compromise, monitor-token leak, etc.
- **Per-scenario mitigations + residual risk** — what's already in
  place, what's not, what's accepted risk.

### Hardening surfaces not yet touched
- **systemd unit hardening** — add `NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `ReadWritePaths=...` to portal/trader/agent services. Reduces
  blast radius of any code-execution bug.
- **SSH config audit** — confirm `PermitRootLogin no`, key-only auth,
  `MaxAuthTries` tuned, no password auth, host-based auth for
  cross-Pi calls if applicable.
- **Firewall** — pi5 should have `ufw` with default-deny inbound,
  allow only TCP/22 from operator IP and TCP/5001 from cloudflared
  loopback (after the LAN-bind fix above is done).
- **Kernel + package patch cadence** — automated `unattended-upgrades`
  for security patches. Today: relies on operator running `apt upgrade`
  manually.

---

## Operational practices (in place today vs. recommended)

| Practice | Today | Recommended |
|---|---|---|
| Password storage | PBKDF2 480k + salt | ✅ As-is |
| Session lifetime | 8h max, 15min idle | ✅ As-is |
| TLS termination | Cloudflare tunnel | ✅ As-is |
| Backup encryption | Strongbox AES-256 → R2 | ✅ As-is |
| Backup retention | 30 days in R2 | ✅ As-is |
| Encryption-key rotation cadence | Tool exists, never run | Run dry-run quarterly + after any suspected leak |
| Monitor-token rotation cadence | No procedure | Define + drill, see backlog OPERATIONAL-HARDENING §3 |
| Incident-response runbook | None | Pre-LIVE gate above |
| Penetration test cadence | None | Annual + before major releases |
| Static security scan | None | Weekly `bandit` + `pip-audit` in CI |
| Supply-chain audit | Manual via librarian | + Dependabot or equivalent automated alerting |
| 2FA on cloud accounts | Unverified by this audit | Confirm + document |
| Hardware-key-only Cloudflare access | Unverified by this audit | Confirm + document |

---

## Audit history

| Date | Scope | Findings | Report |
|---|---|---|---|
| 2026-04-24 | Retail portal route audit + live test-customer probe + host hardening sweep | 2 CRITICAL, 2 HIGH, 3 MEDIUM, 3 LOW. CRITICALs and HIGH-3 fixed during audit. HIGH-4 architectural; deferred. | [security_audit_2026-04-24.md](./security_audit_2026-04-24.md) |
| _next_ | Token-flow + file-upload + systemd/SSH/firewall (Phases 2.5/4.5/5.5) | TBD | TBD |

---

## How to use this document

- **Pre-customer-launch:** check off every box in "Pre-customer-launch
  checklist" above. Re-run a probe similar to Phase 4 to confirm no
  regressions before flipping the lights on.
- **Pre-LIVE-trading:** stack the additional gates from "Pre-LIVE-trading
  checklist" on top.
- **When something breaks:** consult the audit-history reports to see
  if the area was previously cleared, and what the test methodology was.
- **When adding a new endpoint:** look at the pattern in
  `security_audit_2026-04-24.md` "What's working well" — add
  `@login_required` or `@admin_required` decorator, never rely on
  body-only `if not is_admin()` checks alone (defense in depth), use
  `_customer_db()` from session not user input.

---

_Document maintained alongside code; update on every security-relevant
change. Living doc — superseded entries should be struck through, not
deleted, so the historical record stays intact._
