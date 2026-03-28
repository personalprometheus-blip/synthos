# SYNTHOS — SYSTEM MANIFEST

**Document Version:** 4.0
**Supersedes:** SYSTEM_MANIFEST_1_.md v3.0
**Last Updated:** 2026-03-28
**Audience:** Engineers, AI agents, automated deployment systems
**Status:** Active

---

## CHANGE LOG (v3.0 → v4.0)

| Change | Detail |
|--------|--------|
| Agent renaming | Bolt → ExecutionAgent, Scout → DisclosureResearchAgent, Pulse → MarketSentimentAgent |
| FILE_REGISTRY updated | All three retail agent entries updated with new descriptive names |
| DEPENDENCY_GRAPH updated | Agent references use new names throughout |
| NODE_DEFINITIONS updated | `required_files` comments updated |
| Documentation registry updated | Addendum 2 added to doc list |

---

## 1. SYSTEM_METADATA

```yaml
system_name:      Synthos
system_version:   3.0
manifest_version: 4.0
last_updated:     2026-03-28
description: >
  Synthos is a distributed, offline-capable algorithmic trading assistant
  deployed on Raspberry Pi hardware. It operates across two node types:
  retail nodes (customer-facing trading agents) and company nodes
  (company-operated operations and infrastructure).
  End-user access is delivered through a web portal layer that tunnels
  sessions to retail Pi portals — Pis are never directly internet-accessible.
  The system is fully self-contained after installation and requires no
  persistent connection to company infrastructure.
status:           deploy_ready
audit_status:     passed
audit_notes:
  - v3.0: Addendum 1 fully absorbed; INSTALLER_STATE_MACHINE.md retired
  - v3.0: Strongbox added as canonical backup agent
  - v3.0: scoop_trigger.json retired; scoop_trigger table is authoritative
  - v3.0: Heartbeat model updated to session-end / agent-driven
  - v4.0: Retail agent nicknames retired; descriptive names adopted
  - v4.0: Agent integration contract added (see SYNTHOS_TECHNICAL_ARCHITECTURE §2.5)
  - v4.0: Addendum 2 (web access layer) added to documentation registry
```

---

## 2. SYSTEM_PATHS

All paths are derived from `SYNTHOS_HOME`. **No tool, script, or document may hardcode an absolute path.**

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
  SENTINEL_DIR:   "${SYNTHOS_HOME}"
  CONFIG_DIR:     "${SYNTHOS_HOME}/config"

runtime_files:
  env_file:               "${USER_DIR}/.env"
  signals_db:             "${DATA_DIR}/signals.db"
  license_cache:          "${DATA_DIR}/license_cache.json"
  install_progress:       "${SYNTHOS_HOME}/.install_progress.json"
  install_complete:       "${SYNTHOS_HOME}/.install_complete"
  kill_switch:            "${SYNTHOS_HOME}/.kill_switch"
  pending_approvals:      "${SYNTHOS_HOME}/.pending_approvals.json"
  consent_log:            "${SYNTHOS_HOME}/consent_log.jsonl"
  allowed_outbound_ips:   "${CONFIG_DIR}/allowed_outbound_ips.json"

node_specific:
  retail_node:
    home_default:    "/home/${SYNTHOS_USER}/synthos"
  company_node:
    home_default:    "/home/${SYNTHOS_USER}/synthos-company"
```

**Resolution order for `SYNTHOS_HOME`:**
1. `SYNTHOS_HOME` environment variable if set
2. Parent directory of the executing script
3. Deployment-provided override in `.env`

### Path Resolution Rules (Enforcement)

**Python pattern — correct:**
```python
BASE_DIR = Path(__file__).resolve().parent.parent
```

**Python pattern — forbidden:**
```python
BASE_DIR = Path("/home/pi/synthos")  # VIOLATION: hardcoded path
```

**Bash pattern — correct:**
```bash
SYNTHOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
```

**Bash pattern — forbidden:**
```bash
SYNTHOS_DIR="/home/pi/synthos"  # VIOLATION: hardcoded path
```

`first_run.sh` is the sole known exception (build-time bootstrap artifact, flagged `experimental`, scheduled for refactor).

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
    No username is hardcoded anywhere except first_run.sh (see §2).
    SYNTHOS_HOME is resolved at runtime from script location.
  sudo_required: install phase only

deployment_type:
  retail_node:   single-user, single-node, offline-capable
  company_node:  single-node, always-on, company-operated

network:
  required_at_install:    true
  required_at_runtime:    false (offline-capable after install)
  optional_at_runtime:    heartbeat POST, API calls
  outbound_restriction:   enforced at OS level via iptables (retail node)
                          approved IPs defined in allowed_outbound_ips.json
```

---

## 4. NODE_DEFINITIONS

### retail_node

```yaml
role:          Customer trading Pi
hardware:      Raspberry Pi 2W (recommended)
purpose:       Run trading agents, serve portal, maintain local state
autonomy:      Fully standalone after installation
connection_to_company: optional (session-end heartbeat POST)
network_posture: outbound-only to approved IPs; no inbound from public internet

required_files:
  agents:
    - agent1_trader.py       # ExecutionAgent
    - agent2_research.py     # DisclosureResearchAgent
    - agent3_sentiment.py    # MarketSentimentAgent
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
    - uninstall.py
  security:
    - license_validator.py
  config:
    - allowed_outbound_ips.json
  runtime_state:
    - user/.env
    - data/signals.db
    - data/license_cache.json

ports:
  portal: 5001 (configurable via PORTAL_PORT)
  note: >
    Portal is accessible locally and via web portal proxy.
    No inbound port is exposed directly to public internet.

cron_entries:
  - "@reboot sleep 60 && python3 ${CORE_DIR}/boot_sequence.py >> ${LOG_DIR}/boot.log 2>&1"
  - "@reboot sleep 90 && python3 ${CORE_DIR}/watchdog.py &"
  - "@reboot sleep 90 && python3 ${CORE_DIR}/portal.py &"
  - "55 3 * * 6  python3 ${CORE_DIR}/shutdown.py"
  - "0 4 * * 6   sudo reboot"

note: >
  Cron paths are generated dynamically at install time from the resolved
  SYNTHOS_HOME. The above are templates; actual entries use real paths.
```

### company_node

```yaml
role:          Company operations Pi
hardware:      Raspberry Pi 4B
purpose:       Run company agents; manage keys, backups, monitoring, comms
autonomy:      Internal only; not customer-facing
license_required: false (COMPANY_MODE=true bypasses validation)

required_files:
  agents:
    - patches.py
    - blueprint.py
    - sentinel.py
    - fidget.py
    - librarian.py
    - scoop.py
    - vault.py
    - timekeeper.py
    - strongbox.py
  data:
    - db_helpers.py
  operator_tools:
    - generate_unlock_key.py
    - seed_backlog.py
  config:
    - allowed_ips.json         # inbound IP allowlist for company Pi services
```

---

## 5. FILE_REGISTRY

### Agents (retail_node)

| File | Agent Name | Function | Tool Class |
|---|---|---|---|
| `agent1_trader.py` | ExecutionAgent | Trade execution — supervised/autonomous mode, kill switch, protective exit, state-aware | Runtime |
| `agent2_research.py` | DisclosureResearchAgent | Disclosure fetching, deduplication, signal scoring, WATCH re-evaluation | Runtime |
| `agent3_sentiment.py` | MarketSentimentAgent | Sentiment scoring, cascade detection, urgent flag generation | Runtime |

> **Naming note:** Nicknames Bolt, Scout, and Pulse are retired as of v4.0. File names are unchanged and remain the canonical reference for scheduling, watchdog registration, and imports. Agent names above are display identifiers used in logs, portal, and documentation.

### System Tools (retail_node)

| File | Description | Tool Class |
|---|---|---|
| `database.py` | Core database — all tables, helpers, migrations | Data |
| `boot_sequence.py` | Boot coordinator — runs all startup steps in order | Bootstrap |
| `watchdog.py` | Crash monitor, auto-restart (3 attempts), known-good snapshot, rollback | Runtime |
| `health_check.py` | Health verification — DB integrity, tables, Alpaca, position reconciliation | Maintenance |
| `shutdown.py` | Graceful pre-maintenance shutdown — flush writes, mark interrupted ops | Maintenance |
| `cleanup.py` | Nightly database maintenance | Maintenance |
| `synthos_heartbeat.py` | Session-end heartbeat writer — POSTs to Company Pi at agent session completion | Runtime |
| `portal.py` | Web portal — kill switch, trade approvals, settings, log viewer, live status | Runtime |
| `patch.py` | Non-volatile update system — safe file replacement with backup | Repair |
| `install.py` | Guided installer with web UI — 7-step setup wizard | Bootstrap |
| `sync.py` | Dev sync utility — file updates from GitHub | Maintenance |
| `license_validator.py` | License key validation — checked on every boot before agents start | Security |
| `uninstall.py` | Full system removal — cleans legacy paths, unregisters cron | Repair |

### Company Pi Agents

| File | Agent Name | Description | Tool Class |
|---|---|---|---|
| `patches.py` | Patches | Bug detection, log triage, morning report, post-deploy watch | Runtime |
| `blueprint.py` | Blueprint | Code improvement, approved suggestion implementation | Runtime |
| `sentinel.py` | Sentinel | Customer health monitor — heartbeat liveness, silence alerts | Runtime |
| `fidget.py` | Fidget | Cost efficiency monitor — token waste, spend alerts | Runtime |
| `librarian.py` | Librarian | Security and dependency compliance — audits iptables vs IP allowlist | Runtime |
| `scoop.py` | Scoop | All outbound communications — sole delivery channel | Runtime |
| `vault.py` | Vault | License key management, compliance, IP allowlist distribution | Runtime |
| `timekeeper.py` | Timekeeper | Resource scheduler — slot management, deadlock prevention | Runtime |
| `strongbox.py` | Strongbox | Encrypted backup management — creation, encryption, retention, restore | Runtime |
| `db_helpers.py` | — | Company Pi shared DB utilities — all agent writes go through here | Data |

### Operator Tools

| File | Description | Tool Class |
|---|---|---|
| `generate_unlock_key.py` | Generates HMAC-bound autonomous mode unlock keys — operator hardware only | Security |
| `seed_backlog.py` | Seeds initial suggestion backlog for agent bootstrap | Bootstrap |
| `first_run.sh` | One-time command registration after git clone — contains hardcoded path (see §2) | Bootstrap |
| `qpull.sh` | Quick git pull utility | Maintenance |
| `qpush.sh` | Quick git push utility | Maintenance |
| `setup_tunnel.sh` | Cloudflare tunnel setup | Bootstrap |

### Documentation

| File | Description | Node |
|---|---|---|
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` | System behavior, agents, integration contract, network security | engineering |
| `SYSTEM_MANIFEST.md` | Environment, paths, runtime artifacts, file registry, ENV schema | engineering |
| `SYNTHOS_OPERATIONS_SPEC.md` | Weekly cadence, deployment pipeline, maturity gates, backup governance | engineering |
| `SYNTHOS_ADDENDUM_2_WEB_ACCESS.md` | Web portal layer, user management, employee/end-user separation | engineering |
| `TOOL_DEPENDENCY_ARCHITECTURE.md` | Tool classification, execution contract, standard interface | engineering |
| `api_security.md` | API key security guide — all .env keys, operating mode model, kill switch | ops |
| `deadman_switch.md` | Monitor server architecture, Cloudflare tunnel options | ops |
| `pi_maintenance.md` | Pi maintenance reference — crontab, boot sequence, portal, directory structure | ops |

---

## 6. FILE_LOCATIONS

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
  ${DATA_DIR}/license_cache.json
  ${DATA_DIR}/backup/
  ${LOG_DIR}/
  ${SNAPSHOT_DIR}/
  ${CONFIG_DIR}/allowed_outbound_ips.json

company_node:
  ${SYNTHOS_HOME}/agents/patches.py
  ${SYNTHOS_HOME}/agents/blueprint.py
  ${SYNTHOS_HOME}/agents/sentinel.py
  ${SYNTHOS_HOME}/agents/fidget.py
  ${SYNTHOS_HOME}/agents/librarian.py
  ${SYNTHOS_HOME}/agents/scoop.py
  ${SYNTHOS_HOME}/agents/vault.py
  ${SYNTHOS_HOME}/agents/timekeeper.py
  ${SYNTHOS_HOME}/agents/strongbox.py
  ${SYNTHOS_HOME}/utils/db_helpers.py
  ${SYNTHOS_HOME}/data/company.db
  ${SYNTHOS_HOME}/config/allowed_ips.json

operator_only:
  generate_unlock_key.py    # NOT deployed to any Pi
  consent_log.jsonl         # operator machine only
```

---

## 7. DEPENDENCY_GRAPH

```
boot_sequence.py
  ├── license_validator.py       (SECURITY gate)
  ├── health_check.py            (MAINTENANCE — must pass before agents start)
  │     └── database.py
  ├── watchdog.py                (RUNTIME — started in background, 90s delay)
  ├── portal.py                  (RUNTIME — started in background, 90s delay)
  └── [agent1, agent2, agent3]   (RUNTIME — started via individual cron entries)

agent1_trader.py  (ExecutionAgent)
  ├── database.py                (signal reads, trade writes, agent_status writes)
  ├── synthos_heartbeat.py       (session-end POST to Company Pi)
  └── license_validator.py       (periodic re-check)

agent2_research.py  (DisclosureResearchAgent)
  ├── database.py                (signal upserts, config reads for last timestamp)
  └── synthos_heartbeat.py       (session-end POST to Company Pi)

agent3_sentiment.py  (MarketSentimentAgent)
  ├── database.py                (position reads, urgent flag writes)
  └── synthos_heartbeat.py       (session-end POST to Company Pi)

portal.py
  └── database.py                (read positions, signals, portfolio, agent_status)

watchdog.py
  ├── database.py                (crash log writes)
  └── [spawns agent subprocesses on restart]

install.py
  ├── health_check.py            (VERIFYING state)
  └── database.py                (schema bootstrap)

patches.py / blueprint.py / sentinel.py / fidget.py /
librarian.py / scoop.py / vault.py / timekeeper.py / strongbox.py
  └── db_helpers.py              (all company.db writes)

librarian.py
  └── config/allowed_outbound_ips.json   (audits retail Pi iptables compliance)

vault.py
  └── config/allowed_outbound_ips.json   (distributes updates to retail Pis)
```

---

## 8. ENV_SCHEMA

*(Unchanged from v3.0. Full schema in SYSTEM_MANIFEST_1_.md §8.)*

Added keys:

| Key | Type | Default | Node | Notes |
|---|---|---|---|---|
| `ALLOWED_IPS_VERSION` | string | — | retail | `[O]` Version tag of deployed allowed_outbound_ips.json — used by Librarian to detect stale rulesets |

---

## 9. FILE_STATUS

*(Unchanged from v3.0 except:)*

| File | Status | Notes |
|---|---|---|
| `agent1_trader.py` | Active | Now: ExecutionAgent |
| `agent2_research.py` | Active | Now: DisclosureResearchAgent |
| `agent3_sentiment.py` | Active | Now: MarketSentimentAgent |
| `SYNTHOS_ADDENDUM_2_WEB_ACCESS.md` | New | Web portal layer specification |
| `SYNTHOS_TECHNICAL_ARCHITECTURE_1_.md` | Superseded | Replaced by v3.0 |
| `SYSTEM_MANIFEST_1_.md` | Superseded | Replaced by v4.0 |

---

## 10. VERSION_HISTORY

```yaml
v4.0:
  date:  2026-03-28
  label: Agent renaming + web access layer + IP allowlist formalization
  changes:
    - Retail agent nicknames retired (Bolt, Scout, Pulse)
    - ExecutionAgent, DisclosureResearchAgent, MarketSentimentAgent adopted
    - File names unchanged; display names updated in registry and dependency graph
    - allowed_outbound_ips.json added as required retail node config file
    - CONFIG_DIR path variable added
    - ALLOWED_IPS_VERSION env key added
    - Librarian and Vault responsibilities updated re: IP allowlist
    - SYNTHOS_ADDENDUM_2_WEB_ACCESS.md registered in documentation table
    - NODE_DEFINITIONS: network posture documented for retail_node
```

---

## 11. UPGRADE_RULES

### Safe Update Procedure

```
1. Check this manifest — identify which files changed in the new version
2. Run: python3 ${CORE_DIR}/patch.py --status
3. Copy new files to ${CORE_DIR}/ (no version suffixes in filenames)
4. Add any new .env keys listed in ENV_SCHEMA for the target version
5. Run: python3 ${CORE_DIR}/health_check.py
6. If health check passes: reboot
7. Monitor watchdog log for 30 minutes
```

### Adding a New Agent

```
1. Agent implements SynthosAgent contract (see SYNTHOS_TECHNICAL_ARCHITECTURE §2.5)
2. File placed at ${CORE_DIR}/agent_new.py
3. Cron entry added by installer or manually (follow cron_entries template in §4)
4. watchdog.py process list updated with agent file name
5. boot_sequence.py health check list updated with agent file name
6. FILE_REGISTRY in this manifest updated
7. DEPENDENCY_GRAPH updated
8. No other files require modification
```

### Replacing an Existing Agent

```
1. New agent file implements SynthosAgent contract
2. File replaces existing file at same path (e.g., ${CORE_DIR}/agent1_trader.py)
3. No cron changes needed
4. No watchdog changes needed
5. No portal changes needed
6. On next scheduled run: new logic executes, existing DB state intact
```

---

## END OF DOCUMENT

**Version:** 4.0
**Last Updated:** March 28, 2026
