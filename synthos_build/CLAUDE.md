# CLAUDE.md — Project Context for AI Agents

## Project Name
Synthos

## What This Project Does
A distributed algorithmic trading assistant running on Raspberry Pi hardware. Monitors News Sources, sector momentum, SEC EDGAR trading disclosures, runs multi-agent signal analysis, and executes paper trades via Alpaca. Currently in supervised paper-trading mode only (TRADING_MODE=PAPER).

## Current Phase
Phase 3 — System Validation + Normalization Sprint

## Node Architecture
- **retail_node** (Pi 5, deployed 2026-04-18, NVMe boot): trading agents, retail_portal, customer signals.db, ingestion pipeline (news/sentiment/screener absorbed from cancelled process_node), MQTT broker, distributed-trader server — lives in this repo (synthos_build/)
- **company_node** (Pi 4B): operational agents (scoop, strongbox, company_sentinel, company_auditor, company_vault, company_archivist, company_keepalive) + synthos_monitor.py (dashboard + queue API + heartbeat receiver, port 5050) + company_mqtt_listener.py (subscribes to pi5 broker) — lives in synthos-company/
- **pi2w_monitor** (Pi Zero 2W): external fallback heartbeat receiver, currently disabled — planned for MQTT subscriber upgrade (see project_pi4b_cleanup_followups memory)
- ~~**process_node** (Pi 3)~~: cancelled 2026-04-05 — news/signal ingestion absorbed into retail_node

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

## Architecture Doc Maintenance — CRITICAL

`synthos_build/data/system_architecture.json` is the single source of
truth for what this system is made of. The interactive system map at
`/system-architecture` on the command portal (pi4b `synthos_monitor`)
fetches it from GitHub at runtime and renders it. **If the JSON drifts
from the code, the portal lies.** Keep it in sync in the same commit
that changes the system.

| Code change | JSON section to update |
|---|---|
| Add/rename/remove an agent file | `nodes[].agents[]` for that node |
| Add a `.db` or substantially change a schema | `nodes[].databases[]` |
| Add/remove an external service (Alpaca endpoint, broker, etc.) | `nodes[].services[]` |
| Add/rename/remove a trader gate (`def gate*` in `retail_trade_logic_agent.py`) | `trader_gates.gates[]` |
| Add/change an operating mode (DISPATCH_MODE / OPERATING_MODE / TRADING_MODE) | `operating_modes.modes[]` |
| Add/remove a `register_telemetry()` call in any agent | `telemetry_agents.long_running[]` or `.one_shot[]` |
| Ship a new tier of the distributed-trader migration | `distributed_trader_tiers.tiers[]` + update `current_state` |
| Change market-hours boundaries or which agents run in a session | `market_hours.<session>` |
| Add/change a cron entry or systemd timer | `data_flow.daily_timeline` AND `synthos-company/templates/system_map.html M.timeline[]` |

**Always when editing the JSON:**
- Bump `meta.last_updated` (ISO timestamp) and `meta.version` (semver: minor for additions, patch for fixes)
- Validate JSON parses before commit:
  `python3 -c "import json; json.load(open('synthos_build/data/system_architecture.json'))"`
- A broken JSON breaks the entire portal page — never push unvalidated

**Completeness check before bumping the version:**

Use the diff to verify every code change in this commit has a JSON
counterpart. Common pattern: forget to register a newly-shipped tier or
agent, then a follow-up "v3.X+1" bump lands the missing pieces minutes
later. **Same-day double-bumps mean the first one missed something —
catch it the first time.**

Quick one-liner to surface what you might be missing:
```sh
# Newly-added .py files in the staged commit (these need agent entries)
git diff --cached --diff-filter=A --name-only -- 'synthos_build/**/*.py'

# Files renamed or removed (these need an arch.json update too)
git diff --cached --diff-filter=DR --name-only -- 'synthos_build/**/*.py'
```
For each result, confirm there's a matching change in the JSON — agents/
DBs/services arrays, telemetry_agents, trader_gates, or
distributed_trader_tiers as appropriate. If the table above doesn't
cover your change, add a row to it as part of the same commit.

**Visual layout coordinates are in the TEMPLATE, not the JSON.** The
positioning of nodes / agents / DBs on the topology map (`x`, `y`, `w`,
`h` props) lives in `synthos-company/templates/system_map.html` under
the `M.nodes[]` / `M.agents[]` / `M.dbs[]` constants. The JSON is the
inventory; the template is the arrangement. Adding to JSON without
adding template coordinates means the agent shows nowhere on the map.

**Pipeline & gate cards are EMBEDDED in the template** (`PIPELINE`
constant in `system_map.html`). Adding a new pipeline-level agent
(like the Tier 5 dispatcher / trader_server) requires editing the
template's `PIPELINE` constant AND the JSON's `trader_gates` section.
The cutover runbook at `synthos_build/docs/CUTOVER_RUNBOOK.md` is the
operational doc that pulls all of this together for migrations.

## How To Update Progress
When a task is complete:
1. Check it off in PROJECT_STATUS.md (master tracker, this repo)
2. Update Current Phase and Last Updated in STATUS.md (this node)
3. Update synthos-company/STATUS.md if company node work was involved
4. Commit: `git commit -m "progress: [what was completed]"`
