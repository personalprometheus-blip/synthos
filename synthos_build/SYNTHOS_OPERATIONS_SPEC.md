# SYNTHOS OPERATIONS SPECIFICATION
## System-Wide Operating Model

**Version:** 1.0
**Date:** March 2026
**Status:** Active — governs all agents
**Audience:** All agents + project lead

---

## 1. PURPOSE

This document governs how Synthos operates as a system week to week. It covers the deployment pipeline, weekly cadence, morning report, the maturity gate between paper trading and live trading, and Vault's backup responsibilities.

Individual agents have their own workflow specs. This document sits above those — it defines the rhythm everything operates within.

---

## 2. HARDWARE REALITY

### 2.1 Current (Phase 1)

| Device | Role | Notes |
|--------|------|-------|
| Pi 4B | Company Pi — operations, agents, monitoring | Always-on, 24/7 |
| Pi 2W | Retail Pi simulation — dev + beta combined | Only device; can't fully separate dev from beta yet |

The Pi 2W is doing double duty in Phase 1. This means the deployment pipeline is partially theoretical until Phase 2 hardware arrives. Dev and beta testing happen on the same device. Blueprint and Patches are aware of this constraint.

### 2.2 Expanded (Phase 2)

| Device | Role |
|--------|------|
| Pi 4B | Company Pi (unchanged) |
| Pi 2W (dedicated dev) | Development and sandbox testing |
| Pi 2W x1-2 (beta) | Beta tester / founder customer Pis |
| Pi 2W x N (production) | Paying customer Pis |

Phase 2 begins when additional hardware is acquired. The deployment pipeline becomes fully real at that point.

### 2.3 Future Considerations

- Physical Pi hardware is a feature for early customers — tangible, theirs, they control it
- SD card failure is a known reliability risk — mitigated by Vault's encrypted cloud backups
- At approximately 20–30 customers, logistics of managing individual devices may warrant a hybrid cloud model
- No action required now — Fidget will flag when cost/complexity warrants the conversation

---

## 3. THE DEPLOYMENT PIPELINE

```
Company Pi 4B (Blueprint builds here)
Dev Pi 2W (sandbox testing)
         │
         ▼ Thursday EOD
    update-staging branch
         │
         ▼ Friday after market close (project lead approves)
         main
         │
         ├──▶ Beta Pi 2Ws (first target)
         │         │
         │         ▼ 24h validation (post-trading only)
         └──▶ Customer Pi 2Ws
```

**Pre-trading:** Beta and customer Pis may receive the same Friday push simultaneously. Moving fast is appropriate.

**Post-trading:** Beta Pis receive the push first. Customer Pis follow after a 24-hour validation window. Catching a regression on beta before it hits paying customers is worth the extra day.

---

## 4. THE WEEKLY CADENCE

### Monday–Thursday: Build Window

- Blueprint processes approved suggestions in the sandbox branch
- Patches monitors the sandbox, reviews implementations, flags concerns
- Librarian checks any new dependencies Blueprint flags
- Fidget tracks token usage from the week's Claude API calls
- Timekeeper coordinates database access across all agents
- No production changes. No live DB access for Blueprint or Patches.

### Friday: Push Day

**After market close (4pm ET):**

1. Patches delivers the weekly audit to the morning report (see Section 6)
2. Project lead reviews the Pending Changes package in the command portal
3. Project lead approves or rejects each change
4. Approved changes merge to `main`
5. Pi cron jobs pull the update
6. Watchdog activates heightened monitoring (48 hours)
7. Blueprint updates `suggestions.json` with `status: implemented`

**If the project lead rejects a change:** It returns to `update-staging` with rejection notes. Blueprint addresses the concern in the following week's build window.

### Saturday–Sunday: Correction Window

- Watchdog monitors all Pis continuously
- Patches watches for regressions, crash patterns, unexpected behavior
- Blueprint is on standby for event-triggered hot-fixes
- **Sunday morning deadline:** Any regression that cannot be resolved by Sunday morning triggers a full rollback of Friday's changes. Monday starts on the previous known-good state.
- If the weekend is clean: Blueprint begins triage on next week's approved suggestion queue

---

## 5. THE MATURITY GATE

### 5.1 What It Is

A single configuration flag that switches the entire system from pre-trading to post-trading mode. When flipped, Blueprint, Patches, Vault, and Watchdog all adjust their behavior.

```json
{
  "trading_mode": "pre-trading"  // or "post-trading"
}
```

### 5.2 Who Flips It

The project lead, manually, when confidence is high. There is no automatic trigger. The system will paper trade indefinitely until this decision is made consciously.

Criteria for flipping (suggested, not exhaustive):
- System has run stably for multiple months of paper trading
- No CRITICAL regressions in the last 30 days
- Backup and rollback procedures have been tested and verified
- At least one beta customer has been running successfully
- The project lead is satisfied with Bolt's decision quality

### 5.3 What Changes When It Flips

| Area | Pre-Trading | Post-Trading |
|------|-------------|--------------|
| Blueprint weekly cap (retail Pi) | 5 suggestions | 3 suggestions |
| Retail Pi change review | Standard | Requires Patches sign-off |
| Beta → customer pipeline | Simultaneous push OK | 24h beta validation required |
| Rollback authority | Project lead only | Patches can trigger auto-rollback |
| Regression tolerance | Inconvenience | Trust and money at stake |
| Morning report urgency | Informational | Actionable — project lead must respond to HIGH/CRITICAL |

---

## 6. MORNING REPORT

### 6.1 Ownership

**Patches writes it. Scoop delivers it.**

Patches already watches the whole system for problems — log patterns, crash data, agent health, suggestion queue depth. It is the right agent to synthesize the system state into a daily briefing. Individual agents narrating their own work invites self-serving summaries.

### 6.2 Delivery

- **Email digest** via Scoop — delivered to the project lead every morning at 8am ET
- **Command portal** — same content available in the dashboard for reference

The email is the primary channel. It is designed to be readable in under 3 minutes, on a phone, before the market opens.

### 6.3 Report Structure

```
SYNTHOS MORNING REPORT — [Date]
Trading mode: pre-trading / post-trading

━━━ CRITICAL (requires your attention today) ━━━
[Any CRITICAL items — if none, this section is omitted]

━━━ THIS WEEK'S BUILD (Mon–Thu progress) ━━━
Blueprint: [N] suggestions in progress, [N] staged, [N] blocked
Patches: [summary of what was reviewed, any concerns flagged]
Ready for Friday push: YES / NO / PARTIAL

━━━ SYSTEM HEALTH ━━━
All Pis online: YES / NO (list any silent Pis)
Agent errors this week: [count and summary]
Token spend vs. last week: [+/- %]

━━━ MANAGER NOTES ━━━
[Each manager with something worth surfacing gets 1–2 sentences]
[Managers with nothing to report are omitted — no "all clear" noise]

━━━ UPCOMING ━━━
Friday push: [list of changes queued]
Weekend risk: LOW / MEDIUM / HIGH
```

### 6.4 What Patches Does Not Do

- Pad the report to make things look active
- Report "no issues" as a positive signal — silence is just silence
- Summarize things that don't require the project lead's attention
- Editorialize beyond what the data shows

---

## 7. VAULT: ENCRYPTED CUSTOMER BACKUPS

### 7.1 Responsibility

Vault owns customer backup integrity. This is a natural extension of its existing role — it already manages customer compliance, key validity, and data integrity. Encrypted backups of customer Pi data fit that mandate.

When Vault's backup responsibilities grow complex enough to warrant a dedicated agent, the project lead will initiate that handoff. The trigger is when backup management starts competing with Vault's core compliance work for time and attention.

### 7.2 What Gets Backed Up

| Data | Backup? | Notes |
|------|---------|-------|
| `signals.db` | ✅ Yes | Trading history, positions, signals |
| `/user/.env` | ✅ Yes (encrypted) | API keys, customer settings — critical for restore |
| `/user/settings.json` | ✅ Yes | Portal preferences |
| `/user/agreements/` | ❌ No | Static legal docs, not customer-specific |
| `logs/` | ✅ Yes (compressed) | Useful for debugging post-failure |
| `data/backup/` | ❌ No | Already a backup — don't backup the backup |

### 7.3 Encryption

Customer `.env` files contain API keys and trading credentials. These must be encrypted before leaving the Pi.

- Encryption key is held by the project lead, not stored on the Pi or in the backup
- Vault uses the key to encrypt before upload, project lead uses it to decrypt for restore
- If a customer Pi needs to be restored, the project lead performs the decryption step — Vault cannot self-serve a restore

### 7.4 Schedule and Storage

- **Frequency:** Daily backup, triggered at 2am ET (off-hours, low system load)
- **Storage:** Cloud object storage (Cloudflare R2 recommended — no egress fees, S3-compatible API, already in the Synthos infrastructure stack)
- **Retention:** 30 days of daily backups per customer Pi
- **Naming:** `backup/{pi_id}/{date}/synthos_backup_{pi_id}_{date}.tar.gz.enc`

### 7.5 Failure Handling

| Failure | Vault Does |
|---------|------------|
| Storage provider down | Queue backup, retry every 30 min for 4 hours, alert Scoop if still failing |
| Pi unreachable | Skip, log, include in morning report |
| Encryption fails | Halt backup entirely, alert command portal — never upload unencrypted data |
| Backup older than 48 hours | Flag in morning report as CRITICAL |

### 7.6 Restore Process

When a customer Pi fails and needs restore:

1. Project lead identifies Pi ID and requests restore
2. Vault retrieves encrypted backup from cloud storage
3. Project lead decrypts the backup (Vault cannot do this — key stays with project lead)
4. Fresh Pi is flashed with Pi OS Lite
5. Synthos is installed via the standard installer
6. Backup is extracted to `/home/pi/synthos/`
7. Pi boots, agents resume from last known state

---

## 8. AGENT ACCOUNTABILITY IN OPERATIONS

All agents operate as managers, not task executors. This applies to operations as much as implementation.

- **Patches** is accountable for knowing what is wrong before the project lead does
- **Blueprint** is accountable for the code it ships — not just "I ran the task"
- **Vault** is accountable for backup integrity — a missing backup is Vault's failure, not an infrastructure problem
- **Fidget** is accountable for flagging cost anomalies before they become surprises
- **Sentinel** is accountable for knowing if any customer Pi goes silent
- **Scoop** is accountable for the morning report actually reaching the project lead

When something fails, the question is not "what broke" but "which manager missed it and why."

---

## 9. WHAT CHANGES AS THE SYSTEM MATURES

This spec is written for Phase 1 reality. As the system grows, these areas will need revisiting:

| Trigger | What to Revisit |
|---------|-----------------|
| 3+ Pi 2Ws acquired | Deploy pipeline becomes fully real — update Section 3 |
| First beta customer onboarded | Beta validation window activates |
| Maturity gate flipped | Post-trading rules activate across all agents |
| Vault backup work competes with compliance | Spin off dedicated Backup agent |
| 20+ customer Pis | Evaluate hybrid cloud model for reliability at scale |
| Fidget flags sustained cost increase | Re-evaluate token optimization priorities |

---

**Document Version:** 1.0
**Status:** Active
**Owned by:** Project lead
**Next review:** When Phase 2 hardware acquired, or maturity gate is flipped
