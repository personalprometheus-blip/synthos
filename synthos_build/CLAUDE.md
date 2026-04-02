# CLAUDE.md — Project Context for AI Agents

## Project Name
Synthos

## What This Project Does
A distributed algorithmic trading assistant running on Raspberry Pi hardware that monitors U.S. Congressional trading disclosures, scores signals using multi-agent analysis, and executes paper trades via Alpaca. Currently in supervised paper-trading mode only.

## Current Phase
Phase 3 — System Validation + Normalization Sprint

## Node Architecture
- **retail_node** (Pi 2W): trading agents, portal, local signals.db — lives in this repo (synthos_build/)
- **process_node** (Pi 3): news/signal ingestion pipeline, article enrichment, Redis-based distribution — repo TBD; hardware in hand, SD card arriving ~2026-03-31
- **company_node** (Pi 4B): operational agents (scoop, strongbox, company_server — planned: company_sentinel, company_auditor, company_vault, company_archivist, company_keepalive) — lives in synthos-company/
- **monitor_node** (same Pi 4B): synthos_monitor.py on port 5000, receives heartbeats

## Where To Find Things
- **Master project status** → PROJECT_STATUS.md (phases, cross-repo blockers, overall progress)
- **This node's status** → STATUS.md (retail node operational health)
- **Source code** → src/
- **Tests** → tests/
- **All governance, specs, validation, and planning docs** → synthos-company/documentation/

## Critical Known Issues (read before touching any code)
1. Retail license validation — DEFERRED_FROM_CURRENT_BASELINE (SYS-B01/B02 formally closed by deferral). `license_validator.py` is not built; removed from installer requirements; boot has no entitlement gate. This is intentional and documented. Future work tracked in synthos-company/documentation/milestones.md.
2. Suggestions pipeline — RESOLVED (Steps 1-3): company_vault/company_sentinel/company_archivist/retail_watchdog now write via db_helpers
3. Post-deploy rollback — RESOLVED (Step 2): watchdog now reads from db_helpers
4. `watchdog.py` COMPANY_DATA_DIR — RESOLVED (Step 3): now reads from env var
5. `strongbox.py` — RESOLVED (Step 4): moved to synthos-company/agents/
See synthos-company/documentation/validation/SYSTEM_VALIDATION_REPORT.md for full blocker list.

## Conventions
- Never delete files — move deprecated work to synthos-company/documentation/archive/
- Source code lives in src/, tests in tests/
- TRADING_MODE must remain PAPER — PAPER→LIVE requires explicit project lead action

## How To Update Progress
When a task is complete:
1. Check it off in PROJECT_STATUS.md (master tracker, this repo)
2. Update Current Phase and Last Updated in STATUS.md (this node)
3. Update synthos-company/STATUS.md if company node work was involved
4. Commit: `git commit -m "progress: [what was completed]"`
