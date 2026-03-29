# SYNTHOS ADDENDUM 2: WEB ACCESS LAYER
## Domain Portal, User Management, and Pi Session Tunneling

**Document Version:** 1.0
**Date:** March 2026
**Status:** Active
**Audience:** Engineers, project lead
**Companion documents:**
  - SYNTHOS_TECHNICAL_ARCHITECTURE.md v3.0
  - SYSTEM_MANIFEST.md v4.0
  - SYNTHOS_OPERATIONS_SPEC.md v3.0

---

## PURPOSE

This addendum specifies how end users and company employees access Synthos through a web-hosted portal. It covers the full stack: domain hosting, login authentication, session routing to customer Pi portals, user provisioning, and the separation between employee and end-user access.

**Core constraint:** Retail Pis are never directly reachable from the public internet. Every user interaction with a Pi travels through this web layer.

---

## PART 1: ARCHITECTURE OVERVIEW

### 1.1 What the Web Layer Does

```
User opens browser → your domain (e.g. app.synthos.com)
                          │
                          ▼
              Web server: login + auth
                          │
                      ┌───┴───┐
                      │       │
               Employee    End User
               access      access
                  │            │
                  ▼            ▼
           Admin dashboard  Pi portal proxy
           (Company Pi        (customer's retail
            command           Pi portal, tunneled
            interface)        through web layer)
```

### 1.2 What the Web Layer Does NOT Do

- It does not run trading agents
- It does not store customer trading data
- It does not replace the retail Pi portal — it proxies to it
- It does not provide direct SSH or raw Pi access to end users

### 1.3 Network Position

```
Internet ──→ your domain (HTTPS) ──→ Web server
                                          │
                                          ├──→ [Employee] Company Pi (5002) via internal tunnel
                                          │
                                          └──→ [End User] Retail Pi portal (5001) via secure tunnel
                                                      │
                                                      Pi is NOT reachable except through this path
```

---

## PART 2: HOSTING AND INFRASTRUCTURE

### 2.1 Recommended Stack

The web layer is a lightweight server application. It does not need to be large — its job is routing and authentication, not computation.

| Component | Recommended | Notes |
|-----------|-------------|-------|
| Web server | Flask (Python) or FastAPI | Consistent with existing codebase |
| Hosting | VPS (DigitalOcean, Linode, Hetzner) or Cloudflare Workers | Low monthly cost, always-on |
| Domain | Your domain (e.g. synthos.com, app.synthos.com) | HTTPS required |
| TLS | Cloudflare or Let's Encrypt | Terminate at web server |
| Session management | Server-side sessions (Flask-Login or equivalent) | JWTs acceptable for stateless variant |
| Database (users) | SQLite (small scale) → Postgres (20+ users) | Separate from company.db |

### 2.2 Domain Structure

```
synthos.com             → Marketing / landing page (optional)
app.synthos.com         → Web access layer (login portal)
install.synthos.com     → Installer delivery (Cloudflare tunnel, existing)
```

All three can point to the same server or be split. The web access layer (`app.`) is the scope of this document.

### 2.3 Pi Tunnel

The web server reaches retail Pi portals through a secure tunnel. Two options:

**Option A — Cloudflare Tunnel (preferred)**
- Each retail Pi runs a `cloudflared` tunnel that exposes its portal (port 5001) to a named Cloudflare URL
- The web server knows each Pi's tunnel URL and proxies authenticated user sessions to it
- The Pi's local port is never exposed to the raw internet — only through the tunnel

**Option B — Reverse SSH Tunnel**
- Each retail Pi maintains an outbound SSH tunnel to the web server
- The web server forwards authenticated sessions through the tunnel
- More complex to maintain; use if Cloudflare is not available

The web server stores each Pi's tunnel address in the user database, associated with that customer's account.

---

## PART 3: USER MODEL

### 3.1 Two User Classes

The system distinguishes between two types of users. These classes have different access levels, are provisioned differently, and are stored separately.

| Class | Who | Access | Provisioned By |
|-------|-----|--------|---------------|
| **Company Employee** | Project lead, engineers, support staff | Admin dashboard + all company Pi functions | Project lead manually |
| **End User** | Paying customers | Their own Pi portal only | Automated on license issuance |

These classes must never be conflated. A bug that grants an end user access to another customer's Pi or to the company admin interface is a critical security failure.

### 3.2 Company Employee Access

**What they can access:**
- Command Interface (Company Pi port 5002) — full operator view
- All customer Pi portals (read-only monitoring view, clearly marked)
- User management console (provision, disable, reset passwords)
- System logs and morning report

**What they cannot access:**
- Customer `.env` files (API keys are never displayed)
- Customer trading decisions or portfolio values without explicit authorization
- The ability to execute trades on a customer's behalf

**Provisioning:**
- Project lead creates employee accounts manually in the admin console
- Each employee account requires a strong password + MFA (required, not optional)
- Employee accounts are tied to a named individual — no shared accounts
- Access levels within the employee class are role-based (see §3.4)

### 3.3 End User Access

**What they can access:**
- Their own Pi portal, proxied through the web layer
- The same portal pages they would see on local network: dashboard, signals queue, trade history, settings, kill switch, system status

**What they cannot access:**
- Any other customer's portal
- The company admin interface
- Company Pi services on any port
- Raw Pi filesystem or SSH

**Provisioning:**
- When Vault issues a license key, it also creates an end-user account in the web layer's user database
- The account is linked to the customer's `pi_id`
- A welcome email (via Scoop) delivers login credentials
- End users set their own password on first login
- MFA is optional for end users (strongly encouraged, not enforced)

### 3.4 Role-Based Access Within Employee Class

| Role | Access |
|------|--------|
| **Admin** | Full access — all functions, user management, key generation |
| **Support** | Read-only access to customer Pi portals; can view bug reports; cannot modify |
| **Engineer** | Access to system logs, Blueprint/Patches output; cannot access customer portals |

Default for new employee accounts: **Support**. Admin must be explicitly granted by project lead.

---

## PART 4: AUTHENTICATION MODEL

### 4.1 Login Flow (End User)

```
1. User navigates to app.synthos.com
2. Login form: email + password
3. Web server validates credentials against user database
4. If valid: create authenticated session
5. Session lookup: retrieve pi_id linked to this account
6. Session lookup: retrieve Pi tunnel URL for this pi_id
7. Proxy user's subsequent requests to their Pi portal via tunnel
8. User sees their Pi portal, rendered through the web layer
```

### 4.2 Login Flow (Company Employee)

```
1. Employee navigates to app.synthos.com/admin
2. Login form: email + password + MFA token (required)
3. Web server validates credentials + MFA
4. If valid: create authenticated admin session
5. Employee sees admin dashboard with access to company Pi functions
```

### 4.3 Session Security

- Sessions expire after 8 hours of inactivity (configurable)
- Session tokens are server-side; never expose raw session IDs in URLs
- All session traffic is HTTPS only — no fallback to HTTP
- Failed login attempts are rate-limited: 5 attempts, then 15-minute lockout
- Lockout events are logged and surfaced in the morning report

### 4.4 Password Policy

- Minimum 12 characters
- Required for all accounts: employee and end user
- Passwords are hashed (bcrypt) — never stored in plaintext
- Password reset via email link (Scoop delivers the reset email)
- Password reset links expire in 30 minutes

### 4.5 MFA

- **Company Employees:** Required. TOTP (Google Authenticator / Authy compatible)
- **End Users:** Optional. TOTP. Encouraged but not enforced at launch

---

## PART 5: USER DATABASE SCHEMA

The web layer maintains its own user database, separate from `company.db`. This database lives on the web server, not on any Pi.

```sql
-- User accounts (both classes)
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    user_class TEXT NOT NULL,          -- 'employee' | 'end_user'
    role TEXT,                         -- 'admin' | 'support' | 'engineer' (employee only)
    pi_id TEXT,                        -- linked retail Pi (end_user only)
    pi_tunnel_url TEXT,                -- tunnel address for this Pi (end_user only)
    mfa_secret TEXT,                   -- TOTP secret (NULL if MFA not enabled)
    mfa_enabled BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'active',      -- 'active' | 'suspended' | 'pending_setup'
    created_at DATETIME,
    last_login DATETIME,
    created_by TEXT                    -- email of admin who created account
);

-- Login attempts (rate limiting + audit)
CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY,
    email TEXT,
    attempt_time DATETIME,
    success BOOLEAN,
    ip_address TEXT,
    failure_reason TEXT                -- 'wrong_password' | 'mfa_failed' | 'account_suspended'
);

-- Active sessions
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,               -- UUID
    user_id INTEGER,
    created_at DATETIME,
    last_active DATETIME,
    expires_at DATETIME,
    ip_address TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- Password reset tokens
CREATE TABLE password_resets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    token_hash TEXT UNIQUE,
    created_at DATETIME,
    expires_at DATETIME,
    used BOOLEAN DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- Audit log
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    action TEXT,                       -- 'login' | 'logout' | 'password_reset' | 'settings_change' | 'trade_approved'
    timestamp DATETIME,
    ip_address TEXT,
    detail TEXT                        -- JSON string with action-specific context
);
```

---

## PART 6: END USER PROVISIONING FLOW

This is the automated path that runs when Vault issues a license key to a new customer.

```
1. Vault issues license key for new customer Pi
   └── Includes: pi_id, customer_email, customer_name

2. Vault calls web server provisioning endpoint:
   POST /api/provision_user
   {
     "pi_id": "retail-pi-07",
     "email": "customer@example.com",
     "name": "Customer Name",
     "pi_tunnel_url": "https://customer-pi.cfargotunnel.com"
   }

3. Web server:
   a. Creates user record (status: 'pending_setup')
   b. Generates temporary password (random, 16 chars)
   c. Writes to users table
   d. Returns success to Vault

4. Vault writes provisioning_complete to company.db customer record

5. Scoop sends welcome email to customer:
   Subject: "Your Synthos account is ready"
   Body:
     - Login URL: app.synthos.com
     - Temporary password (one-time use)
     - Instructions: set your password on first login
     - Link to user guide

6. Customer visits app.synthos.com, logs in with temp password
7. Forced password change on first login (status: 'pending_setup' → 'active')
8. Customer sees their Pi portal
```

---

## PART 7: EMPLOYEE PROVISIONING FLOW

Employee accounts are created manually by the project lead through the admin console. There is no automated path.

```
1. Project lead logs into admin console (app.synthos.com/admin)
2. Navigates to User Management → Create Employee
3. Inputs: name, email, role (Support / Engineer / Admin)
4. System creates account (status: 'pending_setup')
5. Scoop sends welcome email with temporary password
6. Employee logs in, sets password, enrolls MFA (required before access granted)
7. Account status: 'pending_setup' → 'active' on MFA enrollment
```

**Revoking access:**
```
1. Project lead navigates to User Management → [employee name]
2. Clicks Suspend
3. All active sessions are immediately invalidated
4. Employee cannot log in until status is restored
5. Audit log entry written
```

---

## PART 8: SESSION PROXYING TO PI PORTAL

When an authenticated end-user session is active, the web server acts as a reverse proxy to that user's Pi portal.

### 8.1 How It Works

```python
# Simplified proxy logic (Flask example)

@app.route('/portal/<path:endpoint>', methods=['GET', 'POST'])
@login_required
def proxy_to_pi(endpoint):
    user = current_user
    if user.user_class != 'end_user':
        abort(403)

    pi_url = user.pi_tunnel_url
    if not pi_url:
        return "Pi not configured", 503

    # Forward request to Pi portal
    response = requests.request(
        method=request.method,
        url=f"{pi_url}/{endpoint}",
        headers={k: v for k, v in request.headers if k != 'Host'},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False,
        timeout=10
    )

    return Response(
        response.content,
        status=response.status_code,
        headers=dict(response.headers)
    )
```

### 8.2 Pi Availability

If the Pi portal is unreachable when a user tries to connect:
- Web server returns a "Your Pi is offline" page with last-known status
- No error is surfaced that reveals internal infrastructure details
- Patches is alerted via company Pi if the Pi has been unreachable for > 15 minutes during market hours

### 8.3 What the User Sees

From the user's perspective, they are using a normal web application at your domain. They do not see:
- The Pi's local IP address
- The tunnel URL
- Any Pi-specific infrastructure details
- The fact that the web layer is proxying their session

---

## PART 9: SECURITY BOUNDARIES

### 9.1 Isolation Between End Users

Each end-user session is strictly scoped to their own `pi_id`. The proxy layer enforces this at the session level — an authenticated user cannot request content from another user's Pi by manipulating URLs. The `pi_tunnel_url` is resolved from the server-side session, not from any user-supplied parameter.

### 9.2 Isolation Between Classes

End users have no route to admin endpoints. The `/admin` path is protected by a separate middleware check that verifies `user_class == 'employee'` before any admin function is reachable. A valid end-user session cannot access `/admin` regardless of URL manipulation.

### 9.3 Pi Inbound Restriction

Retail Pis accept inbound connections from:
1. The Cloudflare tunnel (their own tunnel, not others)
2. The company Pi heartbeat receiver

They do not accept inbound connections from arbitrary IPs on port 5001. The iptables rules on the Pi (see SYNTHOS_TECHNICAL_ARCHITECTURE §4.4) enforce outbound-only access. Inbound connections to port 5001 are not relevant from the public internet because the Pi's local port is only accessible via its own tunnel.

### 9.4 Credential Security

- Web layer never transmits or stores customer Alpaca API keys — those stay on the Pi in `.env`
- Web layer never decrypts or displays the contents of Pi `.env` files
- The only credentials the web layer holds: web portal login credentials (hashed)

---

## PART 10: VAULT INTEGRATION

Vault is responsible for:
1. Triggering end-user account provisioning when a license key is issued
2. Distributing Pi tunnel URLs to the web layer when a new Pi is registered
3. Updating the web layer when a customer's Pi is archived or license is revoked
4. Suspending the end-user account when license status changes to REVOKED

This communication happens via a provisioning API on the web server. Vault holds the API key for this endpoint. The endpoint is not publicly accessible — it is only reachable from the Company Pi's IP.

---

## PART 11: OPERATIONAL NOTES

### 11.1 Web Server Maintenance

- Web server updates do not affect Pi operation
- Pi agents run independently and do not know or care whether the web layer is up
- If the web layer goes down, end users lose web access but their Pi continues trading
- Web layer downtime should be treated as a service issue, not a trading system failure

### 11.2 Adding a New Pi to an Existing Account

If a customer replaces their Pi hardware:
1. Vault issues a new license key for the new pi_id
2. Old pi_id is archived
3. Vault updates the user's `pi_id` and `pi_tunnel_url` in the web layer
4. No action required from the end user — their login continues to work, pointing to the new Pi

### 11.3 Monitoring

- Login failures and lockouts surface in the morning report (Patches via Sentinel)
- Session anomalies (multiple simultaneous sessions from different IPs) are flagged
- Web server health is monitored by Sentinel with the same liveness check as Pi heartbeats

---

## END OF DOCUMENT

**Version:** 1.0
**Last Updated:** March 28, 2026
**Companion documents:** SYNTHOS_TECHNICAL_ARCHITECTURE.md v3.0, SYSTEM_MANIFEST.md v4.0, SYNTHOS_OPERATIONS_SPEC.md v3.0
