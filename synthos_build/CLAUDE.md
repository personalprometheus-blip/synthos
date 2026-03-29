# CLAUDE.md — Project Context for AI Agents

## Project Name
Synthos

## What This Project Does
A distributed algorithmic trading assistant running on Raspberry Pi hardware that monitors U.S. Congressional trading disclosures, scores signals using multi-agent analysis, and executes paper trades via Alpaca. Currently in supervised paper-trading mode only.

## Current Phase
Phase 3 — System Validation + Normalization Sprint

## Node Architecture
- **retail_node** (Pi 2W): trading agents, portal, local signals.db — lives in this repo (synthos_build/)
- **company_node** (Pi 4B): operational agents (blueprint, sentinel, patches, vault, etc.) — lives in synthos-company/
- **monitor_node** (same Pi 4B): synthos_monitor.py on port 5000, receives heartbeats

## Where To Find Things
- **Project status** → STATUS.md (start here every session)
- **Blockers and conflicts** → docs/validation/CONFLICT_LEDGER.md
- **What must be fixed before deployment** → docs/validation/SYSTEM_VALIDATION_REPORT.md
- **Milestone plan** → docs/milestones.md
- **Feature specs** → docs/specs/
- **Governance / safety rules** → docs/governance/BLUEPRINT_SAFETY_CONTRACT.md
- **Operations spec** → docs/governance/SYNTHOS_OPERATIONS_SPEC.md + ADDENDUM_1
- **All validation reports** → docs/validation/
- **Source code** → src/
- **Tests** → tests/

## Critical Known Issues (read before touching any code)
1. `license_validator.py` is MISSING — installer always fails VERIFYING; no license gate at boot
2. Suggestions pipeline is SPLIT — vault/sentinel/librarian/watchdog write to JSON; blueprint reads DB only
3. Post-deploy rollback trigger is BROKEN — watchdog reads JSON; blueprint writes to DB
4. `watchdog.py` hardcodes `COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")` — breaks multi-Pi
5. `strongbox.py` is in wrong repo — no backups running on company node
See docs/validation/SYSTEM_VALIDATION_REPORT.md for full blocker list.

## Conventions
- All progress updates go in STATUS.md
- Completed tasks get checked off in docs/milestones.md
- Never delete files — move deprecated work to /archive
- Source code lives in src/, tests in tests/, all docs in docs/
- TRADING_MODE must remain PAPER — PAPER→LIVE requires explicit project lead action

## How To Update Progress
When a task is complete:
1. Check it off in docs/milestones.md
2. Update Current Phase and Last Updated in STATUS.md
3. Commit: `git commit -m "progress: [what was completed]"`
