# SYNTHOS OPERATIONS SPEC — ADDENDUM 1
## Design Decisions & Architecture Updates

**Version:** 1.0
**Date:** March 2026
**Appends:** SYNTHOS_OPERATIONS_SPEC.md v1.0
**Status:** Active — applies to all agents and installers

---

## 1. INSTALLATION PATH — DYNAMIC BASE, NOT HARDCODED

**Problem:** All original code assumed `/home/pi/synthos/`. If a customer creates
a different username (e.g., `/home/alice/synthos/`), the installation breaks.

**Rule:** No agent, installer, or script may hardcode `/home/pi/` anywhere.
All path resolution must be dynamic, derived from the script's own location at runtime.

**Pattern (Python):**
```python
# Correct — resolves from wherever the script actually lives
BASE_DIR = Path(__file__).resolve().parent.parent

# Wrong — breaks on non-default usernames
BASE_DIR = Path("/home/pi/synthos")
```

**Pattern (Bash):**
```bash
# Correct
SYNTHOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Wrong
SYNTHOS_DIR="/home/pi/synthos"
```

**Applied to:** All agents, installer scripts, cron entries, watchdog, vault,
sentinel, heartbeat, and any future scripts.

**Cron entries** must also be dynamic. The installer generates cron lines using
the actual resolved path at install time, not a hardcoded path.

---

## 2. LICENSE KEY — REQUIRED FOR ALL RETAIL PI ACTIVATIONS

Every retail Pi must present a valid license key issued by Vault before any
agent will run. This is non-negotiable and applies to all customers.

### 2.1 What Requires a Key

- All three trading agents (Bolt, Scout, Pulse)
- The retail portal
- Heartbeat (prevents unauthorized Pis phoning home)

### 2.2 What Does NOT Require a Key

- Company Pi (Pi 4B) — all company agents run without key validation
- Dev Pi during development — key validation disabled via `DEV_MODE=true` in .env
- Offline operation — once a key is validated and cached, the Pi can trade
  offline for up to 30 days before requiring re-validation

### 2.3 Validation Flow

```
Boot sequence:
  1. Read LICENSE_KEY from user/.env
  2. Check local cache (data/license_cache.json) — valid if <30 days old
  3. If cache valid → proceed, log "License OK (cached)"
  4. If cache stale → attempt online validation via Vault HTTP endpoint
  5. If online validation succeeds → update cache, proceed
  6. If online validation fails AND cache exists → proceed with warning
  7. If no cache AND no network → HALT with clear error message
  8. If key revoked → HALT immediately, clear cache

Agents check license on startup:
  - If license invalid → log error, exit cleanly (not crash)
  - Portal shows "License invalid — contact support" on landing page
```

### 2.4 Key Security — Anti-Spoofing Model

**Threat:** Someone generates a fake key that passes format validation.

**Defense layers:**

**Layer 1 — HMAC signature**
Keys are signed with `KEY_SIGNING_SECRET` (held only by Vault, never in
any public file). A fake key cannot pass signature verification without
knowing the secret.

```
Key format: synthos-<pi_id>-<timestamp>-<hmac_signature>
HMAC input: sha256(KEY_SIGNING_SECRET + pi_id + timestamp)
Signature:  first 16 hex chars of HMAC output
```

**Layer 2 — Pi ID binding**
Each key is bound to a specific `pi_id`. A key for `retail-pi-01` will fail
validation if used on `retail-pi-02`. The Pi reads its own `PI_ID` from `.env`
and the validator confirms it matches the key.

**Layer 3 — Online registry check**
When online, the Pi queries Vault's validation endpoint. Vault checks:
- Key exists in the `keys` table
- Status is `ACTIVE` (not REVOKED or SUPERSEDED)
- `pi_id` in the key record matches the requesting Pi's `PI_ID`
- Key has not expired

**Layer 4 — Timestamp replay protection**
Keys include an issuance timestamp. Vault rejects keys with timestamps
more than 1 year old unless they have been explicitly renewed. This prevents
someone from capturing a valid key and reusing it indefinitely after it
should have been rotated.

**Layer 5 — Rate limiting on validation endpoint**
Vault's HTTP validation endpoint enforces:
- Max 10 validation attempts per `pi_id` per hour
- Lockout after 20 failed attempts (alerts Patches)
- All validation attempts logged to `audit_trail` table

**Key rotation:** Vault can rotate a key by issuing a new one. The old key
is marked SUPERSEDED. The Pi gets a new key at next online check if the
old one is still valid — transparent to the customer.

**What this prevents:**
- Randomly guessing a valid key string — HMAC prevents this
- Copying a key from one Pi to another — pi_id binding prevents this
- Reusing an old key after account cancellation — registry status prevents this
- Generating keys offline using the format alone — secret prevents this

---

## 3. COMPANY PI — FAST RESTORE, NO KEY REQUIRED

The Company Pi must be restorable from backup and operational within minutes.
No license key, no activation step, no external dependency on Vault to start up.

### 3.1 Restore Sequence

```
1. Flash Pi OS Lite to new microSD
2. Copy backup archive to Pi (USB or network)
3. Run: bash restore.sh
4. restore.sh:
   a. Extracts archive to synthos-company/
   b. Restores company.db from backup
   c. Restores .env from encrypted backup (project lead provides key)
   d. Sets correct permissions
   e. Starts all company agents via boot_sequence
5. All agents running within 5 minutes
```

### 3.2 No Key Dependency

Company agents do NOT call Vault's license endpoint. They read a local
`COMPANY_MODE=true` flag from `.env`. When this flag is set:
- License checks are bypassed entirely
- All agents start without key validation
- Vault still manages keys for customers — it just doesn't validate its own Pi

### 3.3 Backup Schedule

Vault (and eventually Strongbox) backs up company.db daily at 1am ET.
The backup includes the full database, encrypted with the project lead's key.
A restore takes the backup, decrypts it, and drops it into the data/ directory.

---

## 4. ENCRYPTED COMMUNICATIONS — PI ↔ COMPANY

**Principle:** All traffic between retail Pis and the Company Pi is encrypted.
No plaintext customer data, portfolio values, or heartbeat data over the wire.

### 4.1 Transport Security

All HTTP traffic between Pi and Company Pi uses HTTPS via the Cloudflare tunnel.
The tunnel provides TLS 1.3. No additional application-layer encryption is
needed for transport — the tunnel handles it.

For direct Pi-to-Pi communication (if implemented in future), use mutual TLS
with certificates issued by Vault.

### 4.2 IP Isolation During Testing

During development and testing, only known IP addresses may communicate with
the monitor_node's heartbeat receiver (synthos_monitor.py, port 5000) and company Pi validation endpoints.

**Allowed IPs list** (stored in `config/allowed_ips.json`):
```json
{
  "allowed_ips": [
    "YOUR_HOME_IP",
    "YOUR_DEV_PI_IP"
  ],
  "mode": "testing",
  "updated_at": "2026-03-24"
}
```

Sentinel enforces this list — heartbeat POSTs from unknown IPs return 403
and log the attempt. Patches is alerted on repeated unknown IP attempts.

**SSH note:** IP allowlisting will block SSH from unexpected IPs. Before
enabling, configure SSH keys and test access from all expected locations.
This is deferred until the IP list is stable and SSH access is confirmed.

### 4.3 Payload Signing

Heartbeat payloads are signed with `MONITOR_TOKEN` (HMAC). Sentinel validates
the signature before writing to the database. Unsigned or tampered heartbeats
are rejected with 401 and logged.

---

## 5. AGENT 12 — STRONGBOX > BACKUP MANAGER

**Pre-approved.** Agent 12 is formally part of the roster.

| Field | Value |
|-------|-------|
| Number | 12 |
| Alias | **Strongbox** |
| Functional Role | Backup Manager |
| Location | Company Pi |
| Replaces | Vault's backup responsibilities |
| Status | Pre-approved, pending implementation |

**Handoff trigger:** When Vault's backup management competes with its core
compliance work for time and attention, Strongbox takes over all backup
operations. Vault retains key management and compliance tracking.

**Strongbox responsibilities:**
- Daily encrypted backups of all retail Pi data to Cloudflare R2
- Backup integrity verification (download and spot-check)
- Restore orchestration (project lead initiates, Strongbox executes)
- Retention management (30-day rolling window)
- Backup health reporting in morning digest

**Strongbox does NOT:**
- Generate or revoke license keys (Vault owns this)
- Send emails (Scoop owns this)
- Make compliance decisions (Vault owns this)

---

## 6. SCOOP — SINGLE DELIVERY CHANNEL FOR ALL ALERTS

Scoop is responsible for ALL outbound communication. No other agent sends
email, SMS, or any external notification directly.

**Internal company alerts** (previously sent via SMTP in some agents):
→ Written to `scoop_trigger.json` by the originating agent
→ Scoop delivers to project lead email

**Customer-facing alerts** (heartbeat confirmations, trade notifications):
→ Written to `scoop_trigger.json` with `audience: "customer"`
→ Scoop reads customer email from `customers` table
→ Scoop delivers via SendGrid

**Alert types Scoop handles:**
- Pi silence alerts (from Sentinel)
- Agent crash/halt alerts (from Watchdog via suggestions.json)
- Key generation confirmations (from Vault)
- Backup failure alerts (from Vault / Strongbox)
- Morning report delivery (from Patches)
- Trade execution notifications (from Bolt, when customer alerts enabled)
- Compliance warnings (from Vault)
- CVE notifications (from Librarian)

**What this means for other agents:**
No agent should import `smtplib`, `sendgrid`, or any mail library.
The pattern is always: write to `scoop_trigger.json`, Scoop handles delivery.

---

## 7. TRADING TRIO — SMTP REMOVAL

Bolt (agent1_trader.py) contained direct SMTP email calls for alert notifications.
These have been removed in the updated version. Bolt writes trade events to
the local database. Scoop reads them via the heartbeat/report mechanism and
sends customer notifications if `mail_alerts_enabled` is true for that customer.

**Removed from all trading agents:**
- `smtplib` imports
- `MIMEText`, `MIMEMultipart` imports
- `GMAIL_USER`, `GMAIL_APP_PASSWORD` env var references
- Direct SMTP calls

---

**Addendum Version:** 1.0
**Status:** Active
**Applies to:** All agents, all environments
