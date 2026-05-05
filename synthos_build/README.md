# Synthos

A distributed algorithmic trading assistant running on Raspberry Pi hardware. Monitors News Sources, sector momentum, SEC EDGAR trading disclosures, runs multi-agent signal analysis, and executes paper trades via Alpaca. Currently in supervised paper-trading mode only (TRADING_MODE=PAPER).

> **Current state (2026-05-05).** The sections below are a historical
> snapshot from the Phase 3 pre-deployment era (April 5, 2026). For
> the live operational map — node roles, running agents, cron/timer
> schedules, data flow — see `data/system_architecture.json` (v3.30).
> For a producer→consumer trace of the trading pipeline, see
> `docs/pipeline_audit_2026-04-24.md`. The retail node is a Pi 5 16GB
> (NVMe storage), the company node is a Pi 4B, and Phase 8 (paper
> trading review) is in progress — `TRADING_MODE=PAPER`.
>
> **Security posture:** pre-launch security audit + Phase 2.5
> token-flow audit + Phase 4.5 file-upload audit complete. All
> CRITICAL + HIGH customer→admin escalation paths closed. See
> `docs/security_review.md` for the living roadmap.

## Status
See [STATUS.md](./STATUS.md) for the historical Phase 1-5 record.
See `synthos-company/documentation/validation/SYSTEM_VALIDATION_REPORT.md` (companion repo) for historical blockers.
Operational truth lives in `data/system_architecture.json`.

## Structure

```
/src         Source code — all retail node agents and runtime scripts
/agents      Standalone agents (news pipeline, ticker identity, dispatcher, trader server)
/tests       Validation scripts + EDGAR fixtures
/docs        Flat docs: backlog, trade_lifecycle, pipeline audit, security review, EDGAR,
             dispatch mode guide, cutover runbook, and operational runbooks
/config      Systemd units, Cloudflare tunnel, Mosquitto broker config
/data        system_architecture.json (live system map), project_status.json, activists.json
/ops         Crontabs, systemd service files per node (pi4b / pi5)
/tools       Admin + ops tooling (audits, migrations, cleanup scripts)
/static      CSS, fonts, JS assets for the portal
```

## Node Architecture
| Node | Hardware | Role |
|------|----------|------|
| retail_node | Pi 5 16GB (NVMe) | Trading agents, portal, signals.db, ingestion pipeline — this repo |
| company_node | Pi 4B | Operational agents (scoop, strongbox, company_sentinel, company_auditor, company_vault, company_archivist, company_fidget, company_librarian) — synthos_monitor.py serves the dashboard + queue API on :5050 |
| pi2w_monitor | Pi Zero 2W | External watchdog — pings pi4b + pi5, escalates alerts on silence |
| pi2w_sentinel | Pi Zero 2W | Display node (DISABLED) — pending power-source relocation |

## Current Phase
**Phase 8 — Paper Trading Review**
30-day supervised paper-trading observation window. See `data/project_status.json` for live phase status.

## Important
- Paper trading only. TRADING_MODE=PAPER enforced.
- PAPER→LIVE requires explicit project lead approval.
- AI agents: read CLAUDE.md first, then STATUS.md.
