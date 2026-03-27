# SYSTEM_MANIFEST PATCH
## Direct-Paste Additions for SYSTEM_MANIFEST.md v2.0

**Version:** 1.0
**Date:** 2026-03-27
**Purpose:** Registers all artifacts defined in architectural stabilization phase.
**Action:** Insert each section below into the indicated location in SYSTEM_MANIFEST.md.

---

## PATCH 1 — SYSTEM_PATHS additions

**Insert into:** `## 2. SYSTEM_PATHS` → `runtime_files:` block

```yaml
# Architectural stabilization additions — 2026-03-27
suggestions_file:       "${SYNTHOS_HOME}/data/suggestions.json"
suggestions_archive:    "${SYNTHOS_HOME}/data/suggestions_archive.json"
post_deploy_watch:      "${SYNTHOS_HOME}/data/post_deploy_watch.json"
company_services_dir:   "${SYNTHOS_HOME}/services"
company_utils_dir:      "${SYNTHOS_HOME}/utils"
company_config_dir:     "${SYNTHOS_HOME}/config"
blueprint_staging_dir:  "${SYNTHOS_HOME}/.blueprint_staging"
```

---

## PATCH 2 — FILE_REGISTRY additions

**Insert into:** `## 5. FILE_REGISTRY` → new subsection after "Company Pi Agents"

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

---

## PATCH 3 — FILE_LOCATIONS additions

**Insert into:** `## 6. FILE_LOCATIONS` → `company_node:` block

```yaml
company_node:
  # Existing agent entries (unchanged)
  ...

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
```

---

## PATCH 4 — FILE_STATUS additions

**Insert into:** `## FILE_STATUS` section (or create it if absent)

```yaml
# Runtime state artifacts
suggestions.json:
  status:    active
  node:      company_node
  class:     runtime_state
  generated: yes (initialized on first boot by seed_backlog.py or as empty array)
  protected: no (mutable per SUGGESTIONS_JSON_SPEC.md authority model)
  schema:    SUGGESTIONS_JSON_SPEC.md

suggestions_archive.json:
  status:    active
  node:      company_node
  class:     runtime_state
  generated: yes (created by Patches on first archive operation)
  protected: no (append-only additions from main file; no deletions)
  schema:    SUGGESTIONS_JSON_SPEC.md (same structure)

post_deploy_watch.json:
  status:    active
  node:      company_node
  class:     runtime_state
  generated: yes (created by Blueprint at each deployment)
  protected: no (mutable per POST_DEPLOY_WATCH_SPEC.md authority model)
  schema:    POST_DEPLOY_WATCH_SPEC.md

# Services
services/command_interface.py:
  status:  active
  node:    company_node
  class:   Runtime
  port:    5002

services/installer_service.py:
  status:  active
  node:    company_node
  class:   Bootstrap
  port:    5003

services/heartbeat_receiver.py:
  status:  deprecated
  node:    company_node (was proposed, never built)
  class:   —
  note:    Superseded by synthos_monitor.py on monitor_node (port 5000). See HEARTBEAT_RESOLUTION.md.

services/config_manager.py:
  status:  active
  node:    company_node
  class:   Runtime

# Utilities
utils/scheduler_core.py:
  status:  active
  node:    company_node
  class:   Data

utils/db_guardian.py:
  status:  active
  node:    company_node
  class:   Data

# Config
config/agent_policies.json:
  status:  active
  node:    company_node
  class:   runtime_config

config/market_calendar.json:
  status:  active
  node:    company_node
  class:   runtime_config

config/priorities.json:
  status:  active
  node:    company_node
  class:   runtime_config

# Blueprint workspace
.blueprint_staging/:
  status:  active
  node:    company_node
  class:   Repair (ephemeral workspace)
  note:    Never committed to git. Contents are ephemeral staging artifacts only.
```

---

## PATCH 5 — UPGRADE_RULES protected files addition

**Insert into:** `UPGRADE_RULES` → protected files table

```
${USER_DIR}/settings.json    — Portal preferences; customer-owned; must never be overwritten by updates
```

---

## PATCH 6 — NODE_DEFINITIONS heartbeat correction

**Find in:** `## 4. NODE_DEFINITIONS` → `monitor_node:` block

**Current (incorrect):**
```yaml
ports:
  heartbeat_receiver: 5000 (configurable via PORT)
```

**Replace with:**
```yaml
ports:
  heartbeat_receiver: 5000 (configurable via PORT env var; this is the AUTHORITATIVE heartbeat port)

note_on_company_node_port_5004: >
  SYNTHOS_TECHNICAL_ARCHITECTURE §3.2 references a heartbeat_receiver.py on company_node at port 5004.
  This is DEPRECATED. It was a design artifact that was never built. The authoritative heartbeat
  receiver is synthos_monitor.py on monitor_node at port 5000. See HEARTBEAT_RESOLUTION.md.
  The MONITOR_URL env var on retail Pis points to monitor_node:5000 exclusively.
```

---

## PATCH 7 — DOCUMENTATION registry addition

**Insert into:** `## 5. FILE_REGISTRY` → Documentation table

| File | Description | Node |
|---|---|---|
| `SUGGESTIONS_JSON_SPEC.md` | Full specification for suggestions.json — lifecycle, authority, schema | engineering |
| `POST_DEPLOY_WATCH_SPEC.md` | Full specification for post_deploy_watch.json — lifecycle, authority, schema | engineering |
| `BLUEPRINT_SAFETY_CONTRACT.md` | Blueprint's non-negotiable deployment safety rules | engineering |
| `HEARTBEAT_RESOLUTION.md` | Decision record resolving monitor_node vs company_node heartbeat conflict | engineering |
| `NEXT_BUILD_SEQUENCE.md` | Ordered build sequence for current phase | engineering |
| `MANIFEST_PATCH.md` | This document — paste-in additions for SYSTEM_MANIFEST.md | engineering |

---

**Patch Version:** 1.0
**Apply to:** SYSTEM_MANIFEST.md v2.0
**Status:** Ready to apply
