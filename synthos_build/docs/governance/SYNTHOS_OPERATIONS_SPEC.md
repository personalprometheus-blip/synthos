# SYNTHOS OPERATIONS SPECIFICATION
## System-Wide Operating Model

**Version:** 3.0
**Date:** March 2026
**Status:** Active — governs all agents
**Audience:** All agents + project lead
**Supersedes:** v2.0 (SYNTHOS_OPERATIONS_SPEC_1_.md)

---

## CHANGE LOG (v2.0 → v3.0)

| Change | Detail |
|--------|--------|
| Agent renaming | "Bolt's decision quality" → ExecutionAgent; all retail agent references updated |
| First-run assumption removed | Maturity gate criteria and agent references reframed for ongoing operation |
| Web access layer reference | Section 10 added — points to Addendum 2 |

---

## 1. PURPOSE

This document governs how Synthos operates as a system week to week. It covers the deployment pipeline, weekly cadence, morning report, the maturity gate between paper trading and live trading, Strongbox's backup responsibilities, and the web access layer.

Individual agents have their own workflow specs. This document sits above those — it defines the rhythm everything operates within.

---

## 2. HARDWARE REALITY

### 2.1 Current (Phase 1)

| Device | Role | Notes |
|--------|------|-------|
| Pi 4B | Company Pi — operations, agents, monitoring | Always-on, 24/7 |
| Pi 2W | Retail Pi simulation — dev + beta combined | Only device; dev and beta testing share this hardware in Phase 1 |

The Pi 2W is doing double duty in Phase 1. The deployment pipeline is partially theoretical until Phase 2 hardware arrives. Blueprint and Patches are aware of this constraint.

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
- SD card failure is a known reliability risk — mitigated by Strongbox's encrypted cloud backups
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

**Post-trading:** Beta Pis receive the push first. Customer Pis follow after a 24-hour validation window.

**Agent continuity on update:** When updated agent code is deployed, all three retail agents (ExecutionAgent, DisclosureResearchAgent, MarketSentimentAgent) read existing database state on their next run. No re-initialization occurs. Trade history, open positions, signal scores, and configuration are preserved. Updates change agent logic, not the state those agents operate on.

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

---

## 5. THE MATURITY GATE

### 5.1 What It Is

A single configuration flag that switches the entire system from pre-trading to post-trading mode. When flipped, Blueprint, Patches, Strongbox, Vault, and Watchdog all adjust their behavior.

**Strongbox** adjusts backup behavior: post-trading mode triggers increased backup frequency, stricter retention enforcement, and automated restore verification.

**Vault** adjusts security posture: post-trading mode will enforce stricter license validation (no grace period on cache expiry, immediate halt on revoked keys), tighter key enforcement behavior, and more aggressive audit logging. **Note:** Retail license validation is DEFERRED_FROM_CURRENT_BASELINE. This behavior activates once `license_validator.py` is implemented (FUTURE_RETAIL_ENTITLEMENT_WORK).

**Note on company startup:** Company node startup does not depend on retail-style license validation. The company node uses an internal integrity gate model (see `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`). Full boot-time enforcement of that gate is a pre-release security task.

```json
{
  "trading_mode": "pre-trading"
}
```

### 5.2 Who Flips It

The project lead, manually, when confidence is high. There is no automatic trigger. The system will paper trade indefinitely until this decision is made consciously.

Criteria for flipping (suggested, not exhaustive):
- System has run stably for multiple months of paper trading
- No CRITICAL regressions in the last 30 days
- Backup and rollback procedures have been tested and verified
- At least one beta customer has been running successfully
- The project lead is satisfied with ExecutionAgent's signal handling and trade quality across multiple market conditions

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

Patches already watches the whole system for problems — log patterns, crash data, agent health, suggestion queue depth. It is the right agent to synthesize system state into a daily briefing. Individual agents narrating their own work invites self-serving summaries.

### 6.2 Delivery

- **Email digest** via Scoop — delivered to the project lead every morning at 8am ET
- **Command portal** — same content available in the dashboard for reference

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
Backup status: OK / STALE (list any Pis with backup age >48h)
IP allowlist status: OK / MISMATCH (from Librarian audit)

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

## 7. STRONGBOX: ENCRYPTED CUSTOMER BACKUPS

*(Unchanged from v2.0. Strongbox is the canonical backup owner.)*

See SYNTHOS_OPERATIONS_SPEC_1_.md §7 for full detail on schedule, encryption model, failure handling, and restore process.

---

## 8. AGENT ACCOUNTABILITY IN OPERATIONS

All agents operate as managers, not task executors.

- **Patches** is accountable for knowing what is wrong before the project lead does
- **Blueprint** is accountable for the code it ships — not just "I ran the task"
- **Strongbox** is accountable for backup integrity — a missing or stale backup is Strongbox's failure
- **Fidget** is accountable for flagging cost anomalies before they become surprises
- **Sentinel** is accountable for knowing if any customer Pi goes silent
- **Scoop** is accountable for the morning report actually reaching the project lead
- **Vault** is accountable for key validity and for IP allowlist distribution to retail Pis
- **Librarian** is accountable for detecting any iptables configuration that deviates from the approved allowlist

When something fails, the question is not "what broke" but "which manager missed it and why."

---

## 9. WHAT CHANGES AS THE SYSTEM MATURES

| Trigger | What to Revisit |
|---------|-----------------|
| 3+ Pi 2Ws acquired | Deploy pipeline becomes fully real — update Section 3 |
| First beta customer onboarded | Beta validation window activates |
| Maturity gate flipped | Post-trading rules activate across all agents |
| Web portal live | End-user account provisioning activates (see Addendum 2) |
| 20+ customer Pis | Evaluate hybrid cloud model for reliability at scale |
| Fidget flags sustained cost increase | Re-evaluate token optimization priorities |
| Strongbox backup volume exceeds R2 free tier | Re-evaluate storage cost and retention policy |

---

## 10. WEB ACCESS LAYER

The web access layer governs how end users and company employees access the system through a domain-hosted login portal. It is fully specified in:

**SYNTHOS_ADDENDUM_2_WEB_ACCESS.md**

Key operational implications:

- End users do not connect directly to retail Pi IPs
- All user sessions are proxied through the web layer
- Pis communicate outbound only, to the approved IP list
- Two user classes exist: Company Employees and End Users (see Addendum 2 for provisioning model)
- Vault manages the user registry in coordination with the web access layer

---

**Document Version:** 3.0
**Status:** Active
**Owned by:** Project lead
**Supersedes:** SYNTHOS_OPERATIONS_SPEC_1_.md v2.0
**Next review:** When Phase 2 hardware acquired, or maturity gate is flipped
