# Synthos

A distributed algorithmic trading assistant running on Raspberry Pi hardware. Monitors U.S. Congressional trading disclosures, scores signals using multi-agent analysis (research + sentiment), and executes paper trades via Alpaca. Currently in supervised paper-trading mode only.

## Status
See [STATUS.md](./STATUS.md) for current progress.
See [docs/validation/SYSTEM_VALIDATION_REPORT.md](./docs/validation/SYSTEM_VALIDATION_REPORT.md) for known blockers.

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
| company_node | Pi 4B | Operational agents (blueprint, sentinel, patches, vault, etc.) |
| monitor_node | Pi 4B (same) | Heartbeat receiver, alert routing (port 5000) |

## Current Phase
**Phase 3 — Normalization Sprint**
Resolving 4 critical system blockers before any deployment claim. See STATUS.md.

## Important
- Paper trading only. TRADING_MODE=PAPER enforced.
- PAPER→LIVE requires explicit project lead approval.
- AI agents: read CLAUDE.md first, then STATUS.md.
