# SYNTHOS — SYSTEM MANIFEST
## Production-Grade System Definition

**Document Version:** 2.0
**Supersedes:** VERSION_MANIFEST.txt v1.1
**Last Updated:** 2026-03-24
**Audience:** Engineers, AI agents, automated deployment systems
**Status:** Active

---

## SUMMARY OF IMPROVEMENTS OVER VERSION_MANIFEST.txt

| Issue in v1.1 | Resolution in v2.0 |
|---|---|
| Hardcoded `/home/pi/synthos/` paths throughout | Replaced with `SYNTHOS_HOME` variable; all paths derived |
| No multi-user or multi-node model | NODE_DEFINITIONS section; roles explicit per node type |
| Files listed in flat prose | FILE_REGISTRY structured by role; FILE_LOCATIONS maps every file to a path variable |
| `.env` keys listed in version history only | ENV_SCHEMA section; all keys declared with type, default, and node applicability |
| No tool classification | Each file assigned a Tool Dependency Architecture class |
| Upgrade instructions are prose | UPGRADE_RULES section; protected files explicitly listed |
| No file status tracking | FILE_STATUS section; every file marked active / deprecated / experimental |
| Audit results buried at end | Surfaced into FILE_STATUS and VERSION_HISTORY |
| `first_run.sh` hardcodes path | Flagged in EXECUTION_CONTEXT; documented as pre-parameterization bootstrap |

---

## 1. SYSTEM_METADATA

```yaml
system_name:      Synthos
system_version:   1.1
manifest_version: 2.1
last_updated:     2026-03-27
description: >
  Synthos is a distributed, offline-capable algorithmic trading assistant
  deployed on Raspberry Pi hardware. It operates across two node types:
  retail nodes (customer-facing trading agents) and monitor nodes
  (company-operated observability and operations infrastructure).
  The system is fully self-contained after installation and requires no
  persistent connection to company infrastructure.
status:           deploy_ready
audit_status:     passed
audit_notes:
  - All 19 Python files syntax clean
  - All 25 DB methods verified present in DB class
  - Database self-test all 8 assertions pass
  - Portal settings round-trip verified
  - Pending approvals TTL logic verified
  - 9 previously undocumented env keys resolved
  - 15 previously missing install.py env keys resolved
```

---

## 2. SYSTEM_PATHS

All paths are derived from `SYNTHOS_HOME`. No tool, script, or document may hardcode an absolute path.

```yaml
variables:
  SYNTHOS_HOME:   "<resolved at runtime — root of synthos installation>"
  CORE_DIR:       "${SYNTHOS_HOME}/core"
  USER_DIR:       "${SYNTHOS_HOME}/user"
  DATA_DIR:       "${SYNTHOS_HOME}/data"
  LOG_DIR:        "${SYNTHOS_HOME}/logs"
  BACKUP_DIR:     "${SYNTHOS_HOME}/data/backup"
  SNAPSHOT_DIR:   "${SYNTHOS_HOME}/.known_good"
  CRASH_DIR:      "${SYNTHOS_HOME}/logs/crash_reports"
  AGENT_DIR:      "${SYNTHOS_HOME}/core"
  SENTINEL_DIR:   "${SYNTHOS_HOME}"

runtime_files:
  env_file:               "${SYNTHOS_HOME}/user/.env"
  signals_db:             "${DATA_DIR}/signals.db"
  install_progress:       "${SYNTHOS_HOME}/.install_progress.json"
  install_complete:       "${SYNTHOS_HOME}/.install_complete"
  kill_switch:            "${SYNTHOS_HOME}/.kill_switch"
  pending_approvals:      "${SYNTHOS_HOME}/.pending_approvals.json"
  monitor_registry:       "${SYNTHOS_HOME}/.monitor_registry.json"
  consent_log:            "${SYNTHOS_HOME}/consent_log.jsonl"

  # Architectural stabilization additions — 2026-03-27
  suggestions_file:       "${SYNTHOS_HOME}/data/suggestions.json"
  suggestions_archive:    "${SYNTHOS_HOME}/data/suggestions_archive.json"
  post_deploy_watch:      "${SYNTHOS_HOME}/data/post_deploy_watch.json"
  company_services_dir:   "${SYNTHOS_HOME}/services"
  company_utils_dir:      "${SYNTHOS_HOME}/utils"
  company_config_dir:     "${SYNTHOS_HOME}/config"
  blueprint_staging_dir:  "${SYNTHOS_HOME}/.blueprint_staging"

node_specific:
  retail_node:
    home_default:    "/home/${SYNTHOS_USER}/synthos"
  monitor_node:
    home_default:    "/home/${SYNTHOS_USER}/synthos-monitor"
  company_node:
    home_default:    "/home/${SYNTHOS_USER}/synthos-company"
```

**Resolution order for `SYNTHOS_HOME`:**
1. `SYNTHOS_HOME` environment variable if set
2. Parent directory of the executing script
3. Deployment-provided override in `.env`

---

## 3. EXECUTION_CONTEXT

```yaml
os:
  family:        Linux
  tested_on:     Raspberry Pi OS Lite (Debian-based)
  architecture:  aarch64 | armv7l | armv6l
  non_pi_support: permitted with operator confirmation at install

python:
  minimum_version: "3.9"
  interpreter:     python3
  package_manager: pip (with --break-system-packages on Pi OS)

user_model:
  type:          dynamic
  note: >
    No username is hardcoded. SYNTHOS_HOME is resolved at runtime.
    first_run.sh is the sole exception — it contains a build-time default
    path for initial command registration. This is a known pre-parameterization
    bootstrap artifact and is flagged as experimental in FILE_STATUS.
  sudo_required: install phase only (for /usr/local/bin registration)

deployment_type:
  retail_node:   single-user, single-node, offline-capable
  monitor_node:  single-node, always-on, company-operated
  company_node:  single-node, always-on, company-operated

network:
  required_at_install:    true
  required_at_runtime:    false (offline-capable after install)
  optional_at_runtime:    heartbeat POST, GitHub sync, API calls
```

---

## 4. NODE_DEFINITIONS

### retail_node

```yaml
role:          Customer trading Pi
hardware:      Raspberry Pi 2W (recommended)
purpose:       Run trading agents, serve portal, maintain local state
autonomy:      Fully standalone after installation
connection_to_monitor: optional (heartbeat POST)

required_files:
  agents:
    - agent1_trader.py
    - agent2_research.py
    - agent3_sentiment.py
  system:
    - database.py
    - boot_sequence.py
    - watchdog.py
    - health_check.py
    - shutdown.py
    - cleanup.py
    - synthos_heartbeat.py
    - portal.py
    - patch.py
    - install.py
    - sync.py
  security:
    - license_validator.py
  runtime_state:
    - user/.env
    - data/signals.db

ports:
  portal: 5001 (configurable via PORTAL_PORT)

cron_entries:
  - "@reboot sleep 60 && python3 ${CORE_DIR}/boot_sequence.py >> ${LOG_DIR}/boot.log 2>&1"
  - "@reboot sleep 90 && python3 ${CORE_DIR}/watchdog.py &"
  - "@reboot sleep 90 && python3 ${CORE_DIR}/portal.py &"
  - "55 3 * * 6  python3 ${CORE_DIR}/shutdown.py"
  - "0 4 * * 6   sudo reboot"
```

### monitor_node

```yaml
role:          Company observability server
hardware:      Raspberry Pi 4B (recommended)
purpose:       Receive heartbeats, serve monitor console, send alerts, generate reports
autonomy:      Requires network; not deployed to customers

required_files:
  services:
    - synthos_monitor.py
  runtime_state:
    - user/.env
    - .monitor_registry.json

ports:
  heartbeat_receiver: 5000 (configurable via PORT env var; this is the AUTHORITATIVE heartbeat port)

note_on_company_node_port_5004: >
  SYNTHOS_TECHNICAL_ARCHITECTURE §3.2 references a heartbeat_receiver.py on company_node at port 5004.
  This is DEPRECATED. It was a design artifact that was never built. The authoritative heartbeat
  receiver is synthos_monitor.py on monitor_node at port 5000. See HEARTBEAT_RESOLUTION.md.
  The MONITOR_URL env var on retail Pis points to monitor_node:5000 exclusively.
```

### company_node

```yaml
role:          Company operations Pi
hardware:      Raspberry Pi 4B
purpose:       Run company agents (Patches, Blueprint, Sentinel, Fidget, etc.)
autonomy:      Internal only; not customer-facing

required_files:
  agents:
    - patches.py
    - blueprint.py (engineer.py)
    - sentinel.py
    - fidget.py
    - librarian.py
    - scoop.py
    - vault.py
    - timekeeper.py
  data:
    - db_helpers.py
  operator_tools:
    - generate_unlock_key.py
    - seed_backlog.py
```

---

## 5. FILE_REGISTRY

### Agents (retail_node)

| File | Description | Tool Class |
|---|---|---|
| `agent1_trader.py` | Trader — trade execution, supervised/autonomous mode, kill switch, protective exit | Runtime |
| `agent2_research.py` | Daily — disclosure fetching, signal scoring, WATCH re-evaluation | Runtime |
| `agent3_sentiment.py` | Pulse — sentiment scoring, cascade detection, urgent flag generation | Runtime |

### System Tools (retail_node)

| File | Description | Tool Class |
|---|---|---|
| `database.py` | Core database — all tables, helpers, migrations | Data |
| `boot_sequence.py` | Boot coordinator — runs all startup steps in order | Bootstrap |
| `watchdog.py` | Crash monitor, auto-restart (3 attempts), known-good snapshot, rollback | Runtime |
| `health_check.py` | Post-reboot health verification — DB integrity, tables, Alpaca, position reconciliation | Maintenance |
| `shutdown.py` | Graceful pre-maintenance shutdown — flush writes, mark interrupted ops | Maintenance |
| `cleanup.py` | Nightly database maintenance | Maintenance |
| `synthos_heartbeat.py` | Dead man switch heartbeat writer — POSTs to monitor server | Runtime |
| `portal.py` | Web portal — kill switch, trade approvals, settings, log viewer, live status | Runtime |
| `patch.py` | Non-volatile update system — safe file replacement with backup | Repair |
| `install.py` | Guided installer with web UI — 7-step setup wizard | Bootstrap |
| `sync.py` | Dev sync utility — file updates from Claude/GitHub | Maintenance |
| `license_validator.py` | License key validation — checked on every boot before agents start | Security |
| `health_check.py` | Invoked by installer VERIFYING state and by boot_sequence.py | Maintenance |
| `uninstall.py` | Full system removal — cleans legacy paths, unregisters cron | Repair |

### Company Pi Agents

| File | Description | Tool Class |
|---|---|---|
| `patches.py` | Bug Finder — log scanning, triage, morning report, post-deploy watch | Runtime |
| `engineer.py` (blueprint) | Code improvement agent — implements approved suggestions | Runtime |
| `sentinel.py` | Customer health monitor — heartbeat liveness, silence alerts | Runtime |
| `fidget.py` | Cost efficiency monitor — token waste, spend alerts | Runtime |
| `librarian.py` | Security and dependency compliance | Runtime |
| `scoop.py` | Customer communication — alert delivery, retry queue | Runtime |
| `vault.py` | License compliance and customer status | Runtime |
| `timekeeper.py` | System resource scheduler — slot management, deadlock prevention | Runtime |
| `db_helpers.py` | Company Pi shared DB utilities — all agent writes go through here | Data |

### Runtime State Files (company_node)

| File | Description | Tool Class | Node |
|---|---|---|---|
| `data/suggestions.json` | Central improvement and enforcement tracking — all suggestion lifecycle states | Data (runtime state artifact) | company_node |
| `data/suggestions_archive.json` | Completed/superseded suggestions older than 90 days — archived by Patches weekly | Data (runtime state artifact) | company_node |
| `data/post_deploy_watch.json` | Post-deployment monitoring record — governs rollback eligibility; read by retail Watchdog | Data (runtime state artifact) | company_node |

### Services (company_node)

| File | Description | Tool Class | Port |
|---|---|---|---|
| `services/command_interface.py` | Command portal Flask app — project lead dashboard, pending changes, approval UI | Runtime | 5002 |
| `services/installer_service.py` | Installer delivery Flask app — Cloudflare-exposed; serves install scripts to customers | Bootstrap | 5003 |
| `services/config_manager.py` | Configuration management service — runtime config reads/writes for company agents | Runtime | — |

**Note on heartbeat_receiver.py (port 5004):** This file is listed in SYNTHOS_TECHNICAL_ARCHITECTURE §3.2 but is DEPRECATED as a company_node service. The authoritative heartbeat receiver is `synthos_monitor.py` on the monitor_node at port 5000. See HEARTBEAT_RESOLUTION.md for full decision record.

### Utilities (company_node)

| File | Description | Tool Class |
|---|---|---|
| `utils/scheduler_core.py` | Request/Grant logic for Timekeeper — imported by timekeeper.py only | Data |
| `utils/db_guardian.py` | Lock management and conflict detection for company.db — imported by all company agents | Data |
| `utils/api_client.py` | Anthropic, GitHub, SendGrid API client — shared across company agents | Data |
| `utils/logging.py` | Structured logging factory — shared across company agents | Data |

### Config Files (company_node)

| File | Description | Class |
|---|---|---|
| `config/agent_policies.json` | Who runs when — scheduling and priority rules per agent | runtime config |
| `config/market_calendar.json` | Trading hours and market session definitions | runtime config |
| `config/priorities.json` | Task urgency ranking for Timekeeper slot assignment | runtime config |

### Staging Workspace (company_node)

| Path | Description | Class |
|---|---|---|
| `.blueprint_staging/` | Blueprint's exclusive workspace — never committed to git; cleaned at each run start | Repair |

### Operator Tools

| File | Description | Tool Class |
|---|---|---|
| `generate_unlock_key.py` | Generates HMAC-bound autonomous mode unlock keys — operator hardware only | Security |
| `seed_backlog.py` | Seeds initial suggestion backlog for agent bootstrap | Bootstrap |
| `first_run.sh` | One-time command registration after git clone | Bootstrap |
| `qpull.sh` | Quick git pull utility | Maintenance |
| `qpush.sh` | Quick git push utility | Maintenance |
| `setup_tunnel.sh` | Cloudflare tunnel setup | Bootstrap |

### Services (monitor_node)

| File | Description | Tool Class |
|---|---|---|
| `synthos_monitor.py` | Flask monitor server — heartbeat receiver, console, daily reports, SendGrid alerts, state persistence | Observability |

### Documentation

| File | Description | Node |
|---|---|---|
| `deadman_switch.md` | Dead man switch setup — monitor server architecture, Cloudflare tunnel options | ops |
| `api_security.md` | API key security guide — all .env keys, operating mode model, kill switch, git ignore | ops |
| `pi_maintenance.md` | Pi maintenance reference — crontab, boot sequence, portal, directory structure | ops |
| `synthos_framing_v1_1.md` | Legal framing document v1.1 text | legal |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` | Full system architecture | engineering |
| `TOOL_DEPENDENCY_ARCHITECTURE.md` | Tool classification, execution contract, standard interface | engineering |
| `SYNTHOS_OPERATIONS_SPEC.md` | Weekly cadence, deployment pipeline, maturity gates | engineering |
| `AGENT_ROSTER.md` | Company Pi agent roles and accountability model | engineering |
| `SUGGESTIONS_SCHEMA.md` | Suggestions.json schema definition | engineering |
| `COMMAND_PORTAL_SPEC.md` | Command portal specification | engineering |
| `SUGGESTIONS_JSON_SPEC.md` | Full specification for suggestions.json — lifecycle, authority, schema | engineering |
| `POST_DEPLOY_WATCH_SPEC.md` | Full specification for post_deploy_watch.json — lifecycle, authority, schema | engineering |
| `BLUEPRINT_SAFETY_CONTRACT.md` | Blueprint's non-negotiable deployment safety rules | engineering |
| `HEARTBEAT_RESOLUTION.md` | Decision record resolving monitor_node vs company_node heartbeat conflict | engineering |
| `NEXT_BUILD_SEQUENCE.md` | Ordered build sequence for current phase | engineering |
| `MANIFEST_PATCH.md` | Paste-in additions for SYSTEM_MANIFEST.md — applied 2026-03-27 | engineering |

### Legal / Business

| File | Description |
|---|---|
| `operating_agreement.docx` | LLC operating agreement |
| `synthos_operating_agreement.docx` | Synthos-specific operating agreement |
| `synthos_beta_agreements.docx` | Beta tester agreements |
| `synthos_framing.docx` | Legal framing master document |
| `synthos_role_outlines.docx` | Role definitions |
| `legal_documents.md` | Legal document index |

---

## 6. FILE_LOCATIONS

All locations expressed as path variable references. Absolute paths are resolved at runtime from `SYNTHOS_HOME`.

```yaml
retail_node:
  ${CORE_DIR}/agent1_trader.py
  ${CORE_DIR}/agent2_research.py
  ${CORE_DIR}/agent3_sentiment.py
  ${CORE_DIR}/database.py
  ${CORE_DIR}/boot_sequence.py
  ${CORE_DIR}/watchdog.py
  ${CORE_DIR}/health_check.py
  ${CORE_DIR}/shutdown.py
  ${CORE_DIR}/cleanup.py
  ${CORE_DIR}/synthos_heartbeat.py
  ${CORE_DIR}/portal.py
  ${CORE_DIR}/patch.py
  ${CORE_DIR}/install.py
  ${CORE_DIR}/sync.py
  ${CORE_DIR}/license_validator.py
  ${CORE_DIR}/uninstall.py
  ${USER_DIR}/.env
  ${DATA_DIR}/signals.db
  ${DATA_DIR}/backup/
  ${LOG_DIR}/
  ${SNAPSHOT_DIR}/

monitor_node:
  ${SYNTHOS_HOME}/synthos_monitor.py
  ${USER_DIR}/.env
  ${SYNTHOS_HOME}/.monitor_registry.json

company_node:
  ${SYNTHOS_HOME}/agents/patches.py
  ${SYNTHOS_HOME}/agents/engineer.py
  ${SYNTHOS_HOME}/agents/sentinel.py
  ${SYNTHOS_HOME}/agents/fidget.py
  ${SYNTHOS_HOME}/agents/librarian.py
  ${SYNTHOS_HOME}/agents/scoop.py
  ${SYNTHOS_HOME}/agents/vault.py
  ${SYNTHOS_HOME}/agents/timekeeper.py
  ${SYNTHOS_HOME}/utils/db_helpers.py
  ${SYNTHOS_HOME}/data/company.db

  # Runtime state artifacts — added 2026-03-27
  ${SYNTHOS_HOME}/data/suggestions.json
  ${SYNTHOS_HOME}/data/suggestions_archive.json
  ${SYNTHOS_HOME}/data/post_deploy_watch.json

  # Services
  ${SYNTHOS_HOME}/services/command_interface.py
  ${SYNTHOS_HOME}/services/installer_service.py
  ${SYNTHOS_HOME}/services/config_manager.py

  # Utilities
  ${SYNTHOS_HOME}/utils/scheduler_core.py
  ${SYNTHOS_HOME}/utils/db_guardian.py
  ${SYNTHOS_HOME}/utils/api_client.py
  ${SYNTHOS_HOME}/utils/logging.py

  # Config
  ${SYNTHOS_HOME}/config/agent_policies.json
  ${SYNTHOS_HOME}/config/market_calendar.json
  ${SYNTHOS_HOME}/config/priorities.json

  # Blueprint workspace
  ${SYNTHOS_HOME}/.blueprint_staging/

operator_only:
  generate_unlock_key.py    # NOT deployed to any Pi
  consent_log.jsonl         # operator machine only
```

---

## 7. DEPENDENCY_GRAPH

```
boot_sequence.py
  ├── license_validator.py      (SECURITY gate — blocks all agents if fails)
  ├── health_check.py           (MAINTENANCE — must pass before agents start)
  │     └── database.py         (DATA — DB integrity, table verification)
  ├── watchdog.py               (RUNTIME — started in background)
  ├── portal.py                 (RUNTIME — started in background)
  └── [agent1, agent2, agent3]  (RUNTIME — started via cron after boot)

agent1_trader.py
  ├── database.py               (all trade writes)
  ├── synthos_heartbeat.py      (session-end heartbeat POST)
  └── license_validator.py      (periodic re-check)

agent2_research.py
  ├── database.py               (signal upserts)
  └── synthos_heartbeat.py      (session-end heartbeat POST)

agent3_sentiment.py
  ├── database.py               (urgent flag writes)
  └── synthos_heartbeat.py      (session-end heartbeat POST)

portal.py
  └── database.py               (read positions, signals, portfolio)

watchdog.py
  ├── database.py               (crash log writes)
  └── [spawns agent subprocesses]

install.py
  ├── health_check.py           (VERIFYING state trigger)
  └── database.py               (schema bootstrap)

patches.py (company_node)
  └── db_helpers.py             (all writes via slot system)

synthos_monitor.py
  └── (no internal imports — standalone Flask service)
```

---

## 8. ENV_SCHEMA

Keys marked `[R]` = required. Keys marked `[O]` = optional. Column `Node` identifies where the key is consumed.

### Core API Keys

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | string | — | retail | `[R]` Claude API — reasoning engine |
| `ALPACA_API_KEY` | string | — | retail | `[R]` Alpaca trading key |
| `ALPACA_SECRET_KEY` | string | — | retail | `[R]` Alpaca trading secret |
| `ALPACA_BASE_URL` | string | `https://paper-api.alpaca.markets` | retail | `[R]` Paper: paper-api.alpaca.markets / Live: api.alpaca.markets |
| `TRADING_MODE` | enum | `PAPER` | retail | `[R]` `PAPER` or `LIVE` — both must match for live trades |
| `CONGRESS_API_KEY` | string | — | retail | `[R]` Congress.gov API (free — api.congress.gov) |

### Monitor / Dead Man Switch

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `MONITOR_URL` | string | — | retail | `[O]` e.g. `http://your-monitor-pi:5000` |
| `MONITOR_TOKEN` | string | `changeme` | retail + monitor | `[O]` Must match `SECRET_TOKEN` on monitor node |
| `PI_ID` | string | `synthos-pi` | retail | `[O]` Unique node identifier e.g. `synthos-pi-1` |
| `PI_LABEL` | string | — | retail | `[O]` Display name in monitor console |
| `PI_EMAIL` | string | — | retail | `[O]` Alert email shown in monitor console |

### Operating Mode

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `OPERATING_MODE` | enum | `SUPERVISED` | retail | `[R]` `SUPERVISED` or `AUTONOMOUS` |
| `AUTONOMOUS_UNLOCK_KEY` | string | — | retail | `[R if AUTONOMOUS]` HMAC key issued after onboarding call |

### Protective Exit / Alerts

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `SENDGRID_API_KEY` | string | — | retail + monitor | `[O]` SendGrid API key for email alerts |
| `ALERT_FROM` | string | — | retail + monitor | `[O]` Verified sender email |
| `USER_EMAIL` | string | — | retail | `[O]` Recipient for trade and protective exit emails |

### Portal

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `PORTAL_PORT` | integer | `5001` | retail | `[O]` Web portal port |
| `PORTAL_PASSWORD` | string | — | retail | `[O]` Leave blank for open access on local network |
| `PORTAL_SECRET_KEY` | string | — | retail | `[R]` Random hex — generated by installer, never share |

### Portal Advanced Settings (written by `/api/settings`)

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `CLOSE_SESSION_MODE` | enum | `conservative` | retail | `conservative` or `normal` (3:30pm behavior) |
| `MAX_POSITION_PCT` | decimal | `0.10` | retail | Max % of tradeable capital per position (form: integer %, env: decimal) |
| `MAX_SECTOR_PCT` | integer | `25` | retail | Max % in one sector before penalty |
| `MAX_STALENESS` | enum | — | retail | `Fresh` / `Aging` / `Stale` / `Expired` |
| `MIN_CONFIDENCE` | enum | — | retail | `HIGH` / `MEDIUM` / `LOW` |
| `RSS_FEEDS_JSON` | json | — | retail | JSON array of `[name, url, tier]` — custom RSS feeds |
| `SPOUSAL_WEIGHT` | enum | — | retail | `reduced` / `skip` / `equal` |

### SMS Alerts

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `GMAIL_USER` | string | — | retail | `[O]` Gmail address for crash alerts |
| `GMAIL_APP_PASSWORD` | string | — | retail | `[O]` Gmail app password (not account password) |
| `ALERT_PHONE` | string | — | retail | `[O]` 10-digit phone number |
| `CARRIER_GATEWAY` | string | `tmomail.net` | retail | `[O]` e.g. `tmomail.net`, `txt.att.net`, `vtext.com` |

### System Identity

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `STARTING_CAPITAL` | integer | `100` | retail | `[R]` Starting capital in dollars |
| `OWNER_NAME` | string | — | retail | `[R]` Customer name |
| `OWNER_EMAIL` | string | — | retail | `[R]` Customer email |
| `SUPPORT_EMAIL` | string | `synthos.signal@gmail.com` | retail | `[O]` Support contact |
| `GITHUB_TOKEN` | string | — | retail | `[O]` Fine-grained read-only token, 1-year expiry, for sync.py |

### Monitor Node

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `SECRET_TOKEN` | string | — | monitor | `[R]` Must match `MONITOR_TOKEN` on all retail nodes |
| `PORT` | integer | `5000` | monitor | `[O]` Monitor server port |
| `OPERATOR_EMAIL` | string | — | monitor | `[O]` Where triage/digest emails go |

### Operator Tools (not deployed to Pi)

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `SYNTHOS_KEY_SEED` | string | — | operator machine | `[R]` Secret HMAC seed for `generate_unlock_key.py` — never on a Pi |

---

## 9. LIFECYCLE

```yaml
install:
  trigger:       Operator runs install.py
  tool_classes:  Bootstrap, Security, Data
  sequence:
    1. PREFLIGHT    — apt layer validation (python3, pip, git, sqlite3, cron)
    2. COLLECTING   — 7-step web UI wizard (personal, api-keys, alerts, capital, disclaimer, review, install)
    3. VALIDATING   — live API connection tests (Anthropic, Alpaca, Congress.gov)
    4. INSTALLING   — .env write, pip installs, directory creation, cron registration
    5. VERIFYING    — health_check.py; all checks must pass
    6. COMPLETE     — .install_complete sentinel written
  protected_files:
    - user/.env             # never overwritten
    - data/signals.db       # never touched
    - user/agreements/      # immutable

boot:
  trigger:       @reboot cron fires boot_sequence.py (60s delay)
  tool_classes:  Bootstrap, Maintenance, Security, Data
  sequence:
    1. Wait for network
    2. Verify project integrity (.env, required files, DB)
    3. license_validator.py     — hard stop if fails
    4. health_check.py          — hard stop if critical checks fail
    5. watchdog.py              — started in background (90s delay)
    6. Write boot heartbeat
    7. portal.py                — started in background (90s delay)
    8. Agents started via individual cron schedule

runtime:
  trigger:       Individual cron entries or boot_sequence.py subprocess
  tool_classes:  Runtime, Security, Observability, Data
  scheduled_tools:
    - agent1_trader.py          — market hours schedule
    - agent2_research.py        — market hours schedule
    - agent3_sentiment.py       — market hours schedule
    - synthos_heartbeat.py      — called by agents at session end
    - portal.py                 — continuous (@reboot)
    - watchdog.py               — continuous (@reboot)

maintenance:
  trigger:       Scheduled cron
  tool_classes:  Maintenance, Data
  scheduled_tools:
    - cleanup.py                — nightly
    - health_check.py           — @reboot + manual
    - shutdown.py               — Saturday 3:55 AM ET
    - sync.py                   — operator-triggered

repair:
  trigger:       Watchdog escalation or operator invocation
  tool_classes:  Repair, Maintenance, Security, Data
  tools:
    - patch.py                  — safe file replacement with backup
    - patches.py                — company Pi bug detection and triage
    - uninstall.py              — full removal

shutdown:
  trigger:       Cron: Saturday 3:55 AM ET (before 4:00 AM maintenance reboot)
  sequence:
    1. shutdown.py — log event, DB integrity check, mark INTERRUPTED ops, flush writes
    2. sudo reboot  — cron at 4:00 AM
```

---

## 10. VERSION_HISTORY

```yaml
v1.0:
  date:  2026-03-21
  label: Initial release
  changes:
    - Full system complete
    - Watchdog, crash monitoring, auto-restart
    - All three agents functional on Mac and Pi
    - Database self-tests passing
    - Alpaca paper trading connected
    - Congress.gov API connected
    - Claude API connected

v1.1:
  date:  2026-03-22
  label: Framing audit — all critical items addressed
  sessions: 1–5

  session_1:
    heartbeat.py:
      - Replaced Google Sheets / Apps Script dead man switch
      - Now POSTs to Synthos Monitor server (MONITOR_URL in .env)
      - New .env keys: MONITOR_URL, MONITOR_TOKEN, PI_ID, PI_LABEL, PI_EMAIL
      - Falls back gracefully if monitor unreachable
    agent1_trader.py:
      - SUPERVISED MODE (framing C2/C3) — now default
        Claude queues proposals to .pending_approvals.json
        Portal reads and presents trades for user approval
        Approved trades execute on next session run
      - AUTONOMOUS MODE gate (framing C4)
        OPERATING_MODE=AUTONOMOUS requires AUTONOMOUS_UNLOCK_KEY
        Agent refuses to run in autonomous mode without key
      - KILL SWITCH check (framing C1/C5)
        Reads .kill_switch at session start
        If present: halts all activity, logs, exits cleanly
        Portal writes this file; user may also touch it manually
      - LAYER 1 PROTECTIVE EXIT with email (framing M1)
        Urgent flags from The Pulse trigger immediate position close
        SendGrid email sent to user with full reasoning
      - New .env keys: OPERATING_MODE, AUTONOMOUS_UNLOCK_KEY,
                       SENDGRID_API_KEY, ALERT_FROM, USER_EMAIL
    synthos_monitor.py:
      - /report POST endpoint — receives daily performance reports
      - /api/reports GET — returns latest report per Pi
      - Reports include: portfolio value, realized P&L, trades, wins/losses
    portal.py:
      - NEW FILE — web portal on port 5001
      - Basic auth via PORTAL_PASSWORD in .env
      - Kill switch button (framing C1/C5)
      - Supervised mode trade approval queue (framing C2/C3)
      - Autonomous mode unlock gate (framing C4)
      - Advanced settings panel: position sizing, confidence threshold,
        staleness cutoff, close session mode, spousal weight
      - /api/settings POST — saves to .env
      - Live status: portfolio, positions, P&L, urgent flags, heartbeat
      - Auto-refreshes every 60s
    install.py:
      - page_capital() added — step 5 of 6 (framing m6)
      - page_disclaimer() added — step 6 of 6, required acceptance (framing m1)
      - Alerts page updated to monitor server fields
      - Steps renumbered: 6 total
    boot_sequence.py:
      - portal.py added to required files list
      - step7_portal() added — starts portal in background, logs URL
      - Steps renumbered 1–7
    health_check.py:
      - Heartbeat now calls write_heartbeat() from heartbeat.py
    deadman_switch.md:
      - Complete rewrite for monitor server architecture
      - Google Sheets / Apps Script instructions removed
      - Cloudflare Tunnel and port forwarding options documented
    api_security.md:
      - Complete rewrite — all v1.1 .env keys documented
    pi_maintenance.md:
      - Complete rewrite for v1.1

  session_2:
    agent1_trader.py:
      - Portal settings wired in: MIN_CONFIDENCE, MAX_STALENESS, SPOUSAL_WEIGHT,
        MAX_SECTOR_CAP_PCT, CLOSE_SESSION_MODE all read from env, filter signals
      - MAX_POSITION_PCT dynamic from env (default 0.10)
      - Pending approvals TTL: PENDING_APPROVAL entries expire after 48h
      - P&L logged on profit-taking exits
      - Heartbeat POST to monitor after every session
      - Daily report POST to /report on close session
    agent2_research.py:
      - Heartbeat POST to monitor at session completion
    agent3_sentiment.py:
      - Heartbeat POST to monitor at session completion
    portal.py:
      - Basic auth enforced via PORTAL_PASSWORD
      - Settings panel loads live values from .env on page render
      - RSS feed manager added to settings panel
      - MAX_POSITION_PCT round-trip bug fixed (form=integer%, env=decimal)
      - All settings wired end-to-end: portal → .env → agent behavior
    install.py:
      - Step counters corrected throughout
      - Google API packages removed from REQUIRED_PACKAGES
      - flask and sendgrid added to REQUIRED_PACKAGES
      - google_sheet_id hidden input removed
      - Review page shows monitor server URL instead of sheet ID
      - generate_deadman_script() dead function removed (180 lines)
      - Apps Script generation replaced with monitor server status note
    database.py:
      - _run_migrations() added — safe ALTER TABLE on every startup
      - Handles v1.0→v1.1 column additions without breaking existing DBs

  session_3:
    generate_unlock_key.py:
      - NEW FILE — operator use only
      - Generates account-bound HMAC keys
      - Logs consent record to consent_log.jsonl (append-only audit trail)
      - Records: customer name/email, Alpaca key prefix, topics covered,
        recording consent, operator name, call duration
      - Outputs ready-to-send email with unlock key
      - Framing section 4.2 / C4 complete end-to-end
    portal.py:
      - Login page: proper HTML form replaces browser basic auth dialog
      - Logout button in header
      - /logs endpoint: tabbed log viewer for all agent logs, no SSH needed
      - Portal process now monitored by watchdog (auto-restart on crash)
      - PORTAL_SECRET_KEY wired in for session persistence
    synthos_monitor.py:
      - State persistence: save_registry() / load_registry() added
      - Pi list survives monitor server restarts
      - Registry saved to .monitor_registry.json after every heartbeat + delete
    install.py:
      - PORTAL_PASSWORD and PORTAL_SECRET_KEY added to .env template
      - secrets import added for token generation
    api_security.md:
      - PORTAL_SECRET_KEY documented

  session_4:
    agent2_research.py:
      - Bug fixed: duplicate sector= kwarg in upsert_signal removed
      - WATCH signal re-evaluation implemented
        needs_reeval flag set on WATCH decision
        Step 4 re-runs Claude on up to 10 flagged signals per run
    health_check.py:
      - Bug fixed: total variable undefined after previous session edit
    README.md:
      - NEW FILE — GitHub README with quick start, architecture overview,
        agent table, exit hierarchy, requirements, files table, legal section
    .gitignore:
      - NEW FILE — secrets, runtime state, logs, Python cache, OS files
    user_guide.html:
      - NEW FILE — print-ready HTML user guide (9 sections)
        Sections: What Synthos Is, How It Works, Installation, Portal,
        Operating Modes, Capital, Maintenance, Troubleshooting, Disclaimers
        Architecture flow diagram, exit hierarchy ladder, full tables
    synthos_framing_v1_1.md:
      - NEW FILE — text version of framing doc with two fixes
        [LLC NAME] → Synthos Resurgens LLC
        "continuously" → "regularly" in Agent 2 table row
        NOTE: Apply changes to synthos_framing.docx manually

  framing_audit_status:
    critical_complete:
      C1_C5: Kill switch built and wired
      C2_C3: Supervised mode built, portal approval queue
      C4:    Autonomous unlock built, key generation tool built
      M1:    Protective exit email via SendGrid
    minor_complete:
      m1: Disclaimer acknowledgment at install
      m2: "regularly monitors" fix applied
      m6: Capital configuration at install
      m7: "[LLC NAME]" fix applied
    remaining_non_code:
      M2: Attorney review of profit flow model
      M3: Stripe/licensing — v1.1 commercial build
      T15: Signup automation — v1.1 commercial build

  final_audit:
    python_files_syntax_clean: 19
    db_methods_verified: 25
    db_self_test_assertions: 8 (all pass)
    bugs_fixed:
      - 9 env keys undocumented in api_security.md
      - 15 env keys missing from install.py .env template
        (portal settings, SMS keys, OPERATOR_EMAIL, SECRET_TOKEN)
      - deadman_apps_script.gs archived from outputs
      - digest_agent.py and uninstall.py added to MIGRATION_GUIDE
    acceptable_non_issues:
      - sys.exit() in top-level except blocks — intentional fatal handlers
      - quorum references in uninstall.py — correct, cleans legacy paths
    status: DEPLOY_READY
```

---

## 11. UPGRADE_RULES

### Safe Update Procedure

```
1. Check this manifest — identify which files changed in the new version
2. Run: python3 ${CORE_DIR}/patch.py --status
3. Copy new files to ${CORE_DIR}/ (no version suffixes in filenames)
4. Add any new .env keys listed in ENV_SCHEMA for the target version
5. Run: python3 ${CORE_DIR}/patch.py --dir /path/to/update/folder/
6. Verify: python3 ${CORE_DIR}/health_check.py
7. Update SYSTEM_MANIFEST system_version and last_updated
```

### Protected Files — Never Overwrite

| File | Reason |
|---|---|
| `${USER_DIR}/.env` | API keys, trading settings, operating mode — customer-owned |
| `${DATA_DIR}/signals.db` | Full trade history, positions, signals — customer data |
| `${DATA_DIR}/backup/` | Backup copies of signals.db |
| `consent_log.jsonl` | Append-only audit trail — operator machine only |
| `${SYNTHOS_HOME}/.known_good/` | Watchdog rollback snapshot |
| `${USER_DIR}/settings.json` | Portal preferences; customer-owned; must never be overwritten by updates |

### Database Migrations

- `database.py` runs `_run_migrations()` on every startup
- Migrations are `ALTER TABLE IF NOT EXISTS` — safe to run on any DB version
- No destructive migrations are permitted without explicit operator approval
- Migration history is logged to `system_log` table

### Rollback

- `watchdog.py` maintains a `.known_good` snapshot (updated after each successful agent run, max once per 7 days)
- Pre-trading: watchdog halts and alerts; project lead decides on rollback
- Post-trading: watchdog may trigger autonomous rollback if `rollback_trigger` condition met in `post_deploy_watch.json`

---

## 12. FILE_STATUS

| File | Status | Notes |
|---|---|---|
| `agent1_trader.py` | active | |
| `agent2_research.py` | active | |
| `agent3_sentiment.py` | active | |
| `database.py` | active | |
| `boot_sequence.py` | active | |
| `watchdog.py` | active | v2.0 |
| `health_check.py` | active | |
| `shutdown.py` | active | |
| `cleanup.py` | active | |
| `synthos_heartbeat.py` | active | |
| `portal.py` | active | |
| `patch.py` | active | |
| `install.py` | active | |
| `sync.py` | active | |
| `license_validator.py` | active | |
| `uninstall.py` | active | |
| `generate_unlock_key.py` | active | operator machine only — not deployed to Pi |
| `synthos_monitor.py` | active | monitor_node only |
| `patches.py` | active | company_node only |
| `engineer.py` | active | company_node only |
| `sentinel.py` | active | company_node only |
| `fidget.py` | active | company_node only |
| `librarian.py` | active | company_node only |
| `scoop.py` | active | company_node only |
| `vault.py` | active | company_node only |
| `timekeeper.py` | active | company_node only |
| `db_helpers.py` | active | company_node only |
| `seed_backlog.py` | active | company_node only |
| `first_run.sh` | experimental | hardcodes `/home/pi/synthos` — pre-parameterization bootstrap artifact; scheduled for refactor to use `SYNTHOS_HOME` |
| `qpull.sh` | active | |
| `qpush.sh` | active | |
| `setup_tunnel.sh` | active | |
| `synthos_framing_v1_1.md` | active | text reference only — master is synthos_framing.docx |
| `api_security.md` | active | |
| `deadman_switch.md` | active | |
| `pi_maintenance.md` | active | |
| `synthos_tracker.html` | active | internal tracking only |
| `synthos_shortcuts.html` | active | internal reference only |
| `user_guide.html` | active | customer-facing |
| `synthos_design_brief.md` | active | internal reference |
| `deadman_apps_script.gs` | deprecated | archived — replaced by monitor server architecture in v1.1 |
| `data/suggestions.json` | active | company_node — runtime state; schema: SUGGESTIONS_JSON_SPEC.md |
| `data/suggestions_archive.json` | active | company_node — runtime state; created by Patches on first archive |
| `data/post_deploy_watch.json` | active | company_node — runtime state; schema: POST_DEPLOY_WATCH_SPEC.md |
| `services/command_interface.py` | active | company_node — port 5002 |
| `services/installer_service.py` | active | company_node — port 5003 |
| `services/heartbeat_receiver.py` | deprecated | company_node (was proposed, never built) — superseded by synthos_monitor.py on monitor_node; see HEARTBEAT_RESOLUTION.md |
| `services/config_manager.py` | active | company_node |
| `utils/scheduler_core.py` | active | company_node |
| `utils/db_guardian.py` | active | company_node |
| `utils/api_client.py` | active | company_node |
| `utils/logging.py` | active | company_node |
| `config/agent_policies.json` | active | company_node — runtime config |
| `config/market_calendar.json` | active | company_node — runtime config |
| `config/priorities.json` | active | company_node — runtime config |
| `.blueprint_staging/` | active | company_node — ephemeral workspace; never committed to git |
| `SUGGESTIONS_JSON_SPEC.md` | active | engineering reference |
| `POST_DEPLOY_WATCH_SPEC.md` | active | engineering reference |
| `BLUEPRINT_SAFETY_CONTRACT.md` | active | engineering reference |
| `MANIFEST_PATCH.md` | active | engineering reference — applied 2026-03-27 |

---

**Version:** 2.1
**Last Updated:** 2026-03-27
**Supersedes:** VERSION_MANIFEST.txt v1.1
**Patch Applied:** MANIFEST_PATCH.md v1.0 — architectural stabilization additions (SYSTEM_PATHS, FILE_REGISTRY, FILE_LOCATIONS, FILE_STATUS, UPGRADE_RULES, NODE_DEFINITIONS)
