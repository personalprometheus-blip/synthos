# Synthos

A distributed algorithmic trading assistant running on Raspberry Pi hardware. Monitors U.S. Congressional trading disclosures, scores signals using multi-agent analysis (research + sentiment), and executes paper trades via Alpaca. Currently in supervised paper-trading mode only.

> **Current state (2026-04-25).** The sections below are a historical
> snapshot from the Phase 3 pre-deployment era (April 5, 2026). For
> the live operational map — node roles, running agents, cron/timer
> schedules, data flow — see `data/system_architecture.json` (v3.13).
> For a producer→consumer trace of the trading pipeline, see
> `docs/pipeline_audit_2026-04-24.md`. The retail node is a Pi 5 16GB
> (NVMe storage), the company node is a Pi 4B, and Phase 6 (live
> trading) has not flipped — still `TRADING_MODE=PAPER`.
>
> **Security posture:** pre-launch security audit + Phase 2.5
> token-flow audit + Phase 4.5 file-upload audit complete. All
> CRITICAL + HIGH customer→admin escalation paths closed. See
> `docs/security_review.md` for the living roadmap.

## Status
See [STATUS.md](./STATUS.md) for the historical Phase 1-5 record.
See [docs/validation/SYSTEM_VALIDATION_REPORT.md](./docs/validation/SYSTEM_VALIDATION_REPORT.md) for historical blockers.
Operational truth lives in `data/system_architecture.json`.

## Structure

```
/src         Source code — all retail node agents and runtime scripts
/tests       Validation scripts (validate_02, validate_03b, validate_env)
/docs
  /specs       Architecture, installer, agent specifications
  /governance  Operations spec, safety contract, pipeline rules
  /validation  Conflict ledger, validation reports, ground truth
  /reference   PDFs, research docs, addenda
/archive     Historical records and superseded documents
```

## Node Architecture
| Node | Hardware | Role |
|------|----------|------|
| retail_node | Pi 2W | Trading agents, portal, local DB — this repo |
| company_node | Pi 4B | Operational agents (scoop, strongbox, company_server — planned: company_sentinel, company_auditor, company_vault, company_archivist, company_keepalive) |
| monitor_node | Pi 4B (same) | Heartbeat receiver, alert routing (port 5000) |

## Current Phase
**Phase 3 — Normalization Sprint**
Resolving 4 critical system blockers before any deployment claim. See STATUS.md.

## Important
- Paper trading only. TRADING_MODE=PAPER enforced.
- PAPER→LIVE requires explicit project lead approval.
- AI agents: read CLAUDE.md first, then STATUS.md.
