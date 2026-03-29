# PRODUCT SPEC

**Last Updated:** 2026-03-29
**Full design brief:** docs/specs/synthos_design_brief.md
**System manifest:** docs/specs/SYSTEM_MANIFEST.md

---

## What Synthos Does

Synthos monitors U.S. Congressional trading disclosures (via Congress.gov API) and uses multi-agent analysis to generate, score, and — in paper mode — execute trades via Alpaca. A company node manages the system, approves changes, and routes alerts. All trading is supervised and currently in paper mode only.

---

## Core Trading Pipeline (retail_node)

```
agent2_research.py (Scout)
  └─ Fetches congressional disclosures daily
  └─ Scores members by historical win rate (member_weights)
  └─ Writes signals to signals.db

agent3_sentiment.py (Pulse)
  └─ Runs market sentiment scan
  └─ Writes sentiment scores to scan_log
  └─ Issues pulse_warning if sentiment is bearish

agent1_trader.py (Bolt)
  └─ Reads scored signals from signals.db
  └─ Applies Option B logic:
       MIRROR  → adjusted_score ≥ threshold, no pulse_warning
       WATCH   → adjusted_score ≥ threshold, pulse_warning present
       WATCH_ONLY → below threshold
  └─ Submits MIRROR signals to pending_approvals queue
  └─ Awaits project lead approval via portal
  └─ Executes approved trades via Alpaca (PAPER only)
```

---

## Signal Scoring

| Factor | Description |
|--------|-------------|
| base_score | Raw signal score from congressional disclosure data |
| member_weight | Per-member multiplier (0.5–1.5) based on historical win/loss rate |
| adjusted_score | base_score × member_weight |
| pulse_warning | Set by agent3 when sentiment is bearish |
| interrogation | Peer corroboration via UDP — marks signal VALIDATED or UNVALIDATED |

---

## Approval Workflow

1. Bolt scores signal → writes to `pending_approvals` table if meets threshold
2. Project lead reviews in Portal (port 5001)
3. Approved → Bolt executes via Alpaca paper API
4. Rejected → signal archived, no execution
5. All outcomes tracked in `outcomes` and `ledger` tables

---

## Company Node Functions (synthos-company/)

| Agent | Function |
|-------|---------|
| blueprint.py | Implements approved code suggestions via staging workflow |
| sentinel.py | Monitors retail Pi heartbeats (silence = alert) |
| vault.py | License key generation and management |
| patches.py | Continuous code audit — finds bugs, posts suggestions |
| librarian.py | Documentation management |
| fidget.py | Processes user feedback |
| scoop.py | **Only** outbound email sender — delivers alerts to project lead |
| timekeeper.py | DB access slot management |
| strongbox.py | Automated backups — CURRENTLY MISPLACED, not running |

---

## Portal Features (port 5001)

| Tab | Function |
|-----|---------|
| Dashboard | Live portfolio value, position count, recent signals |
| Signals | Signal queue, status, approval actions |
| Portfolio | Open positions, P&L |
| System | Agent health, recent logs, heartbeat status |

---

## Trading Constraints (non-negotiable)

| Rule | Value |
|------|-------|
| Trading mode | PAPER only until explicit project lead approval |
| Position sizing | Defined by signal score + member weight |
| Market | U.S. equities via Alpaca |
| Data source | Congress.gov (QUORUM) API |
| Execution gate | Pending approval queue — no autonomous execution |

---

## Planned Features (not yet implemented)

These are in spec docs only. None are live. Do not reference as active.

| Feature | Spec Document |
|---------|-------------|
| Web access layer | docs/specs/SYNTHOS_ADDENDUM_2_WEB_ACCESS.md |
| Enhanced execution agent (v2) | docs/specs/EXECUTIONAGENT_SPECIFICATION_v2.md |
| Enhanced research agents (v2) | docs/specs/RESEARCHAGENTS_SPECIFICATION_v2.md |
| Auditing agent | docs/specs/AUDITINGAGENT_SPECIFICATION_v1.md |
| Multi-customer Pi scaling | Phase 2 hardware not yet acquired |
| Interrogation peer ACK (second Pi) | Deferred — second retail Pi not deployed |
