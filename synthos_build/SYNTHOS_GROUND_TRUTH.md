# SYNTHOS — GROUND TRUTH EXTRACTION REPORT
## System Architect Analysis

**Generated:** 2026-03-25
**Source Documents:** SYSTEM_MANIFEST.md (v2.0), TOOL_DEPENDENCY_ARCHITECTURE.md (v1.0),
                     INSTALLER_STATE_MACHINE.md (v1.0), SYNTHOS_TECHNICAL_ARCHITECTURE.md (v1.1)
**Method:** Clean environment. No prior assumptions. Documents are sole source of truth.

---

## 1. SYSTEM OVERVIEW

Synthos is a distributed, offline-capable algorithmic trading assistant deployed on Raspberry Pi
hardware. It operates across three node types with explicit separation of concerns:

| Node | Hardware | Role | Customer-Facing |
|------|----------|------|-----------------|
| **retail_node** | Pi 2W | Trading agents, portal, local state | Yes |
| **monitor_node** | Pi 4B | Heartbeat receiver, observability console | No |
| **company_node** | Pi 4B | Operations agents, services, licensing | No |

**Governing principles (from TOOL_DEPENDENCY_ARCHITECTURE):**
- All config via `.env` only — no hardcoded values
- All tools idempotent and re-runnable
- Exit codes + structured logs mandatory — no silent failures
- Tools write to `logs/` and `data/` only — never to `user/`
- One tool class per file — no class-spanning tools

**Current version:** system_version 1.1 / manifest_version 2.0
**Audit status:** DEPLOY_READY (19 Python files syntax-clean, 25 DB methods verified)

---

## 2. ACTIVE FILE REGISTRY

### 2A. CORE SYSTEM — Retail Node

| File | TDA Class | Location | Purpose |
|------|-----------|----------|---------|
| `database.py` | Data | `${CORE_DIR}` | All tables, helpers, migrations — Layer 3 module |
| `boot_sequence.py` | Bootstrap | `${CORE_DIR}` | Boot coordinator; runs all startup steps in order |
| `watchdog.py` | Runtime | `${CORE_DIR}` | Crash monitor, auto-restart (3x), known-good snapshot, rollback |
| `health_check.py` | Maintenance | `${CORE_DIR}` | Post-reboot health verification; sole VERIFYING-state tool |
| `shutdown.py` | Maintenance | `${CORE_DIR}` | Graceful pre-maintenance shutdown; flushes writes |
| `cleanup.py` | Maintenance | `${CORE_DIR}` | Nightly database maintenance |
| `portal.py` | Runtime | `${CORE_DIR}` | Web portal — kill switch, approvals, settings, logs, live status |
| `install_retail.py` | Bootstrap | `${CORE_DIR}` | 7-step guided retail installer with web UI — canonical installer |
| `patch.py` | Repair | `${CORE_DIR}` | Non-volatile update system — safe file replacement with backup |
| `sync.py` | Maintenance | `${CORE_DIR}` | Dev sync utility — file updates from Claude/GitHub |
| `license_validator.py` | Security | `${CORE_DIR}` | License key validation — checked on every boot |
| `uninstall.py` | Repair | `${CORE_DIR}` | Full system removal; cleans legacy paths, unregisters cron |
| `synthos_heartbeat.py` | Runtime | `${CORE_DIR}` | Dead man switch heartbeat writer — POSTs to monitor server |

### 2B. AGENTS — Retail Node

| File | TDA Class | Location | Purpose |
|------|-----------|----------|---------|
| `agent1_trader.py` | Runtime | `${CORE_DIR}` | Trade execution; supervised/autonomous mode; kill switch; protective exit |
| `agent2_research.py` | Runtime | `${CORE_DIR}` | Disclosure fetching; signal scoring; WATCH re-evaluation |
| `agent3_sentiment.py` | Runtime | `${CORE_DIR}` | Sentiment scoring; cascade detection; urgent flag generation |

### 2C. CORE SYSTEM — Monitor Node

| File | TDA Class | Location | Purpose |
|------|-----------|----------|---------|
| `synthos_monitor.py` | Observability | `${SYNTHOS_HOME}` | Flask monitor server — heartbeat receiver, console, daily reports, alerts |

### 2D. AGENTS — Company Node

| File | TDA Class | Location | Purpose |
|------|-----------|----------|---------|
| `patches.py` | Repair | `${SYNTHOS_HOME}/agents/` | Bug finder — log scanning, triage, morning report, post-deploy watch |
| `engineer.py` | Runtime | `${SYNTHOS_HOME}/agents/` | Code improvement agent — implements approved suggestions |
| `sentinel.py` | Observability | `${SYNTHOS_HOME}/agents/` | Customer health monitor — heartbeat liveness, silence alerts |
| `fidget.py` | Runtime | `${SYNTHOS_HOME}/agents/` | Cost efficiency monitor — token waste, spend alerts |
| `librarian.py` | Security | `${SYNTHOS_HOME}/agents/` | Security and dependency compliance |
| `scoop.py` | Runtime | `${SYNTHOS_HOME}/agents/` | Customer communication — alert delivery, retry queue |
| `vault.py` | Security | `${SYNTHOS_HOME}/agents/` | License compliance and customer status |
| `timekeeper.py` | Runtime | `${SYNTHOS_HOME}/agents/` | System resource scheduler — slot management, deadlock prevention |
| `strongbox.py` | Runtime | `${SYNTHOS_HOME}/agents/` | Backup Manager — encrypted R2 backups, 30-day retention, verify, restore orchestration |
| `db_helpers.py` | Data | `${SYNTHOS_HOME}/utils/` | Company Pi shared DB utilities — all agent writes go through here |
| `seed_backlog.py` | Bootstrap | `${SYNTHOS_HOME}/` | Seeds initial suggestion backlog for agent bootstrap |

### 2E. TOOLS — Operator Only (not deployed to any Pi)

| File | TDA Class | Location | Purpose |
|------|-----------|----------|---------|
| `generate_unlock_key.py` | Security | operator machine | HMAC-bound autonomous mode unlock key generation; logs consent |

### 2F. SHELL UTILITIES

| File | Status | Purpose |
|------|--------|---------|
| `first_run.sh` | **experimental** | One-time command registration after git clone — hardcodes `/home/pi/synthos` |
| `qpull.sh` | active | Quick git pull utility |
| `qpush.sh` | active | Quick git push utility |
| `setup_tunnel.sh` | active | Cloudflare tunnel setup |

### 2G. RUNTIME STATE FILES (not code — tracked as artifacts)

| File | Node | Purpose |
|------|------|---------|
| `user/.env` | retail | API keys, trading settings, operating mode — customer-owned, never overwritten |
| `data/signals.db` | retail | Full trade history, positions, signals — customer data |
| `.install_progress.json` | retail | Installer state machine checkpoint |
| `.install_complete` | retail | Terminal state sentinel |
| `.kill_switch` | retail | Portal-written; presence halts all agent activity |
| `.pending_approvals.json` | retail | Supervised mode trade queue |
| `.monitor_registry.json` | monitor | Pi registry; persists across monitor server restarts |
| `consent_log.jsonl` | operator machine | Append-only audit trail for unlock key generation |
| `.known_good/` | retail | Watchdog rollback snapshot directory |

### 2H. DOCUMENTATION

| File | Node | Status |
|------|------|--------|
| `api_security.md` | ops | active |
| `deadman_switch.md` | ops | active |
| `pi_maintenance.md` | ops | active |
| `synthos_framing_v1_1.md` | legal/ops | active — text reference only; master is `.docx` |
| `README.md` | repo | active |
| `user_guide.html` | customer | active — customer-facing |
| `synthos_tracker.html` | internal | active — internal tracking only |
| `synthos_shortcuts.html` | internal | active — internal reference only |
| `synthos_design_brief.md` | internal | active |
| `.gitignore` | repo | active |

### 2I. LEGAL / BUSINESS DOCUMENTS

| File | Status |
|------|--------|
| `operating_agreement.docx` | active |
| `synthos_operating_agreement.docx` | active |
| `synthos_beta_agreements.docx` | active |
| `synthos_framing.docx` | active — master legal framing |
| `synthos_role_outlines.docx` | active |
| `legal_documents.md` | active — index |

---

## 3. IMPLIED / MISSING FILES

These files are explicitly referenced in the architecture documents but have NO entry
in SYSTEM_MANIFEST FILE_REGISTRY or FILE_STATUS. They are architectural gaps.

### 3A. HIGH CONFIDENCE — Referenced by name, clearly required

| File | Source Reference | Assessment |
|------|-----------------|------------|
| `suggestions.json` | TOOL_DEPENDENCY_ARCHITECTURE (Enforcement Model); SYNTHOS_TECHNICAL_ARCHITECTURE Part 11 | **Critical gap.** Central to company agent accountability model. Blueprint writes here. Patches reads here. Arch violation detection pipeline depends on it. Has no schema definition in any document. |
| `post_deploy_watch.json` | SYSTEM_MANIFEST §11 Rollback: "rollback_trigger condition met in post_deploy_watch.json" | **Functional gap.** Watchdog references this file for autonomous rollback decisions. No schema, no location, not registered. |

### 3B. MEDIUM CONFIDENCE — Implied by architecture, path-named in directory trees

| File | Source Reference | Assessment |
|------|-----------------|------------|
| `utils/api_client.py` | SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 directory tree | **Likely absorbed.** Arch doc shows it as a shared utility. Manifest does not list it. Either it was never built as a standalone module and its logic lives inside each agent, or it exists and is unregistered. Must be resolved before rebuild. |
| `utils/config.py` | SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 directory tree | Same status as `api_client.py`. Manifest makes no reference. |
| `utils/logging.py` | SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 directory tree (retail) and §3.2 (company) | Same status. TDA mandates a named logger per tool — this module may have been inlined. |
| `scheduler.py` (retail) | SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 directory tree and §2.4 | **Conflict.** Listed in the directory structure. §2.4 says its role is "minimal — just startup orchestration" and notes each agent is self-scheduled. This role is wholly covered by `boot_sequence.py` in the manifest. May be a ghost from an earlier design. |

### 3C. LOWER CONFIDENCE — Referenced in passing, status unclear

| File | Source Reference | Assessment |
|------|-----------------|------------|
| `digest_agent.py` | SYSTEM_MANIFEST §10 final_audit: "digest_agent.py and uninstall.py added to MIGRATION_GUIDE" | Mentioned alongside `uninstall.py` in a migration context. `uninstall.py` is active; `digest_agent.py` has no FILE_STATUS entry at all. Likely dead/renamed but requires confirmation. |
| `run_agent_locally.sh` | SYNTHOS_TECHNICAL_ARCHITECTURE §9.1 testing framework: `bash run_agent_locally.sh trader` | Developer test utility. Not registered in manifest. May be a development artifact not intended for deployment. |
| `schema.sql` | SYNTHOS_TECHNICAL_ARCHITECTURE §9.1: `sqlite3 company.db.staging < schema.sql` | Staging setup script for company Pi testing. Not registered. May be internal-only and intentionally excluded. |
| `user/settings.json` | SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 and §4.3 protected files list | Listed as a protected file in the tech arch (`/user/settings.json — customer preferences`). NOT listed in SYSTEM_MANIFEST protected files table, which only lists `.env`, `signals.db`, `backup/`, `consent_log.jsonl`, `.known_good/`. |

### 3D. COMPANY NODE SERVICES — Documented in arch, absent from manifest

The SYNTHOS_TECHNICAL_ARCHITECTURE §3.2 defines a `services/` directory with four files.
SYSTEM_MANIFEST has no `services/` entry for the company_node at all.

| File | Port | Assessment |
|------|------|------------|
| `command_interface.py` | 5002 | **Unregistered.** Fully specified in arch (7-page dashboard). Not in manifest FILE_REGISTRY or FILE_STATUS. |
| `installer_service.py` | 5003 | **Unregistered.** Fully specified in arch (Cloudflare-exposed). Not in manifest. |
| `heartbeat_receiver.py` | 5004 | **RESOLVED 2026-03-27.** Deprecated — never built. Authoritative heartbeat receiver is `synthos_monitor.py` on monitor_node:5000. See `HEARTBEAT_RESOLUTION.md`. Registered as `deprecated` in SYSTEM_MANIFEST FILE_STATUS. |
| `config_manager.py` | — | **Unregistered.** Named in arch directory tree with no description. No manifest entry. |

### 3E. COMPANY NODE UTILITIES — Documented in arch, absent from manifest

| File | Assessment |
|------|------------|
| `utils/scheduler_core.py` | Implements Request/Grant logic for Timekeeper. Not in manifest. |
| `utils/db_guardian.py` | Implements lock management for company.db. Referenced by name with code samples. Not in manifest. |
| `utils/api_client.py` (company) | Company-node API client. Not in manifest. |
| `utils/logging.py` (company) | Company-node shared logging. Not in manifest. |

### 3F. COMPANY NODE CONFIG FILES — Documented in arch, absent from manifest

| File | Assessment |
|------|------------|
| `config/agent_policies.json` | Who runs when. Not in manifest. |
| `config/market_calendar.json` | Trading hours reference. Not in manifest. |
| `config/priorities.json` | Task urgency ranking. Not in manifest. |

---

## 4. DEFUNCT / REDUNDANT FILES

| File | Status | Evidence | Disposition |
|------|--------|----------|-------------|
| `deadman_apps_script.gs` | **Deprecated** | SYSTEM_MANIFEST FILE_STATUS explicitly marks deprecated. Replaced by monitor server architecture in v1.1. | Archive — do not deploy |
| `first_run.sh` | **Experimental / flagged** | Hardcodes `/home/pi/synthos` — violates SYNTHOS_HOME resolution rule. SYSTEM_MANIFEST flags as "pre-parameterization bootstrap artifact; scheduled for refactor." | Refactor or replace before production |
| `digest_agent.py` | **Likely dead** | Appears only in a migration note in VERSION_HISTORY alongside `uninstall.py`. No FILE_STATUS entry. No FILE_REGISTRY entry. | Verify: if exists on disk, audit and formally register or deprecate |
| `scheduler.py` (retail) | **Likely ghost** | Appears in SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 directory tree only. §2.4 says role is "minimal." Manifest does not register it. `boot_sequence.py` covers its described function entirely. | Verify: if exists on disk, determine if absorbed into boot_sequence.py |

---

## 5. ARCHITECTURAL INCONSISTENCIES

### INC-001 — HEARTBEAT RECEIVER: Node Conflict — **RESOLVED 2026-03-27**
**Description:** Two documents assigned the heartbeat receiver to different nodes with different ports.

**Resolution:** monitor_node is the authoritative heartbeat receiver (`synthos_monitor.py`, port 5000). `heartbeat_receiver.py` (port 5004) on company_node was never built and is formally deprecated. `HEARTBEAT_URL` env var is deprecated; canonical env var is `MONITOR_URL`. All affected documents corrected. See `HEARTBEAT_RESOLUTION.md` for full decision record.

---

### INC-002 — COMPANY NODE ENTIRELY ABSENT FROM MANIFEST — **RESOLVED 2026-03-27**
**Description:** company_node `services/`, `utils/`, and `config/` directories were not registered in FILE_REGISTRY or FILE_STATUS.

**Resolution:** MANIFEST_PATCH.md applied to SYSTEM_MANIFEST.md — all company_node services, utilities, config files, and runtime state artifacts registered. `heartbeat_receiver.py` registered as deprecated.

---

### INC-003 — SUGGESTIONS.JSON HAS NO SCHEMA OR LOCATION — **RESOLVED 2026-03-27**
**Description:** `suggestions.json` had no path, schema, FILE_REGISTRY entry, or FILE_STATUS entry.

**Resolution:** `SUGGESTIONS_JSON_SPEC.md` written — full lifecycle model, authority model, mutation rules, validation rules, and schema. Path defined: `${SYNTHOS_HOME}/data/suggestions.json`. Registered in FILE_REGISTRY, FILE_LOCATIONS, FILE_STATUS, and SYSTEM_PATHS via MANIFEST_PATCH.

---

### INC-004 — RETAIL UTILS/ DIRECTORY: GHOST OR MISSING (MEDIUM)
**Description:** SYNTHOS_TECHNICAL_ARCHITECTURE §2.2 shows a `utils/` subdirectory under `core/` with three files: `api_client.py`, `config.py`, `logging.py`. SYSTEM_MANIFEST does not list these files anywhere — not in FILE_REGISTRY, FILE_LOCATIONS, or FILE_STATUS.

Two interpretations:
- (A) These were an early design artifact absorbed into individual agents. The manifest reflects the actual built state.
- (B) These exist but were never registered — a documentation gap from an incomplete manifest migration.

**Impact:** If (B), the Tool Agent's job of auditing `utils/` for outdated code references files that may not exist. If (A), the arch doc contains a misleading directory structure that will confuse all future builders.

**Recommendation:** Verify disk state. If files do not exist, strike the `utils/` subtree from the retail arch doc and document the inline approach. If they do exist, register them.

---

### INC-005 — POST_DEPLOY_WATCH.JSON: REFERENCED BUT UNDEFINED — **RESOLVED 2026-03-27**
**Description:** `post_deploy_watch.json` was referenced in SYSTEM_MANIFEST rollback rules but had no path, schema, or registry entries.

**Resolution:** `POST_DEPLOY_WATCH_SPEC.md` written — full lifecycle model, authority model (Blueprint creates, Patches updates, Watchdog evaluates), mutation rules, validation rules, and schema. Path defined: `${SYNTHOS_HOME}/data/post_deploy_watch.json`. Registered in SYSTEM_PATHS, FILE_REGISTRY, FILE_LOCATIONS, and FILE_STATUS via MANIFEST_PATCH.

---

### INC-006 — USER/SETTINGS.JSON: PROTECTED FILE NOT IN MANIFEST PROTECTION LIST — **RESOLVED 2026-03-27**
**Description:** `user/settings.json` was not listed in SYSTEM_MANIFEST UPGRADE_RULES protected files table.

**Resolution:** Added via MANIFEST_PATCH §PATCH 5: `${USER_DIR}/settings.json — Portal preferences; customer-owned; must never be overwritten by updates`.

---

### INC-007 — TDA LOG PATH HARDCODES /HOME/PI (LOW)
**Description:** TOOL_DEPENDENCY_ARCHITECTURE Logging section states:
`Log file: /home/pi/synthos/logs/<tool_name>.log`
This contradicts the core SYSTEM_MANIFEST principle that no tool may hardcode an absolute path, and the resolution rule for `SYNTHOS_HOME`.

**Impact:** Minor — this is documentation, not code. But it will produce incorrect implementations if AI agents or engineers follow the TDA template literally.

**Recommendation:** Update TDA logging section to reference `${LOG_DIR}/<tool_name>.log`.

---

### INC-008 — INSTALLER STATE MACHINE USES HARDCODED PATHS (LOW)
**Description:** INSTALLER_STATE_MACHINE.md detection criteria use hardcoded paths throughout:
- `UNINITIALIZED`: `/home/pi/synthos/user/.env`
- `COMPLETE` terminal state: `/home/pi/synthos/.install_complete`

This again contradicts the SYNTHOS_HOME parameterization rule.

**Impact:** If `install.py` implements detection logic literally from this document, it breaks on any non-default installation path.

**Recommendation:** Update INSTALLER_STATE_MACHINE detection criteria to use `${SYNTHOS_HOME}` variable references, consistent with SYSTEM_MANIFEST §2.

---

### INC-009 — TOOL_DEPENDENCY_ARCHITECTURE MISSING COMPANY AGENT CLASSIFICATIONS (MEDIUM)
**Description:** TOOL_DEPENDENCY_ARCHITECTURE classifies tools in its examples using only retail-node files. Company-node agents (`patches.py`, `engineer.py`, `sentinel.py`, etc.) each have behaviors that need TDA classification:
- `patches.py` is classified as Repair in the manifest but its company behavior (log analysis, morning reports) is more Maintenance/Observability.
- `sentinel.py` is listed as Runtime in manifest but its described behavior (monitor without modifying) fits Observability.
- `timekeeper.py` is Runtime in manifest; its scheduling role may conflict with the "one class per file" rule if it both schedules (Bootstrap-adjacent) and monitors (Observability-adjacent).

**Impact:** Without explicit TDA classification for company agents, the enforcement model (Blueprint + Patches scanning for conformance) has no authoritative class assignments to validate against.

**Recommendation:** Add company-node agent classifications to TDA with explicit rationale for any non-obvious assignments.

---

## 6. RECOMMENDED CLEAN STATE

What the system SHOULD look like after cleanup and reconciliation.

### 6A. MANIFEST CHANGES REQUIRED

**Add to FILE_REGISTRY (company_node):**
```
services/
  command_interface.py   — Runtime, port 5002
  installer_service.py   — Bootstrap, port 5003
  heartbeat_receiver.py  — Runtime, port 5004
  config_manager.py      — Runtime

utils/
  scheduler_core.py      — Data (imported by timekeeper.py only)
  db_guardian.py         — Data (imported by all company agents)
  api_client.py          — Data (if exists)
  logging.py             — Data (if exists)

config/
  agent_policies.json    — runtime config artifact
  market_calendar.json   — runtime config artifact
  priorities.json        — runtime config artifact

data/
  suggestions.json       — accountability + enforcement pipeline
  post_deploy_watch.json — watchdog rollback trigger config
```

**Add to FILE_STATUS:**
All of the above files, plus `suggestions.json` and `post_deploy_watch.json`.

**Add to SYSTEM_PATHS:**
```yaml
SUGGESTIONS_FILE:       "${SYNTHOS_HOME}/suggestions.json"
POST_DEPLOY_WATCH:      "${SYNTHOS_HOME}/post_deploy_watch.json"
COMPANY_SERVICES_DIR:   "${SYNTHOS_HOME}/services"
COMPANY_UTILS_DIR:      "${SYNTHOS_HOME}/utils"
COMPANY_CONFIG_DIR:     "${SYNTHOS_HOME}/config"
```

**Add to UPGRADE_RULES protected files:**
```
${USER_DIR}/settings.json   — Portal preferences; customer-owned
```

**INC-001 (heartbeat receiver node conflict): RESOLVED 2026-03-27**
monitor_node is the authoritative heartbeat receiver. heartbeat_receiver.py (port 5004) deprecated.
See HEARTBEAT_RESOLUTION.md.

### 6B. DOCUMENT CORRECTIONS REQUIRED

| Document | Fix |
|----------|-----|
| TOOL_DEPENDENCY_ARCHITECTURE | Replace `/home/pi/synthos/logs/` with `${LOG_DIR}/` in Logging section |
| INSTALLER_STATE_MACHINE | Replace all `/home/pi/synthos/` with `${SYNTHOS_HOME}/` in detection criteria |
| SYNTHOS_TECHNICAL_ARCHITECTURE | Reconcile §2.2 `utils/` retail directory tree against manifest (strike or register) |
| SYNTHOS_TECHNICAL_ARCHITECTURE | Reconcile §2.4 retail `scheduler.py` against `boot_sequence.py` — decide which is canonical |

### 6C. FILES TO CREATE

| File | Priority | Description |
|------|----------|-------------|
| ~~`SUGGESTIONS_SCHEMA.md`~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — Superseded by `SUGGESTIONS_JSON_SPEC.md` (full spec, not just schema) |
| `suggestions.json` | HIGH | Initialize as empty array `[]` on company_node — see NEXT_BUILD_SEQUENCE.md Step 4 |
| ~~`post_deploy_watch.json`~~ | ~~HIGH~~ | **RESOLVED 2026-03-27** — `POST_DEPLOY_WATCH_SPEC.md` written; file created at first Blueprint deployment |
| `user/settings.json` | MEDIUM | Define schema; currently referenced as protected but has no formal definition |

### 6D. FILES TO RESOLVE (verify disk state before acting)

| File | Action Required |
|------|----------------|
| `digest_agent.py` | Verify existence. If found: audit purpose, register or formally deprecate with FILE_STATUS entry. |
| `scheduler.py` (retail) | Verify existence. If found: confirm whether it duplicates `boot_sequence.py`. If duplicate, deprecate. |
| `utils/api_client.py` (retail) | Verify existence. If found: register. If absent: remove from arch doc directory tree. |
| `utils/config.py` (retail) | Same as above. |
| `utils/logging.py` (retail) | Same as above. |
| `first_run.sh` | Refactor to read `SYNTHOS_HOME` from environment before promoting from experimental to active. |

### 6E. TDA CLASSIFICATION CLEAN STATE

Company agents need explicit classification. Proposed:

| File | Proposed Class | Rationale |
|------|---------------|-----------|
| `patches.py` | Repair | Primary function is failure detection and triage |
| `engineer.py` | Repair | Primary function is implementing fixes |
| `sentinel.py` | Observability | Monitors without modifying — read-only by design |
| `fidget.py` | Observability | Cost monitoring — read-only by design |
| `librarian.py` | Security | Dependency compliance = security gating function |
| `scoop.py` | Runtime | Continuous delivery; handles retry queues |
| `vault.py` | Security | License validation = security gating function |
| `timekeeper.py` | Runtime | Active scheduler — modifies state (grants, queues) |
| `db_helpers.py` | Data | Layer 3 module — imported only, never invoked directly |
| `seed_backlog.py` | Bootstrap | Run once at company_node first boot |
| `scheduler_core.py` | Data | Library — imported by timekeeper only |
| `db_guardian.py` | Data | Library — imported by all company agents |

---

## SUMMARY SCORECARD

| Category | Count | Notes |
|----------|-------|-------|
| Active files (confirmed) | 43 | Retail: 16 core + 3 agents; Monitor: 1; Company: 10 agents + 1 data; Operator: 1; Shell: 4; Docs/Legal: ~8 |
| Active runtime state artifacts | 9 | `.env`, `signals.db`, sentinels, registries, etc. |
| Implied / missing files (critical) | 0 | RESOLVED — `suggestions.json` and `post_deploy_watch.json` now fully specified and registered |
| Implied / missing files (company services) | 0 | RESOLVED — Entire `services/` directory registered via MANIFEST_PATCH |
| Implied / missing files (company utils/config) | 0 | RESOLVED — All utility and config files registered via MANIFEST_PATCH |
| Implied / missing files (retail utils) | 3 | Status ambiguous — verify disk (INC-004 still open) |
| Defunct / flagged for cleanup | 4 | `deadman_apps_script.gs`, `first_run.sh` (experimental), `digest_agent.py` (likely dead), retail `scheduler.py` (ghost) |
| Architectural inconsistencies | 9 | INC-001 through INC-009 |
| High-severity inconsistencies resolved | 3 | INC-001 (heartbeat), INC-002 (company node manifest), INC-003 (suggestions.json) — all resolved 2026-03-27 |
| Medium-severity inconsistencies resolved | 2 | INC-005 (post_deploy_watch.json), INC-006 (settings.json protection) — resolved 2026-03-27 |
| Remaining open inconsistencies | 4 | INC-004 (retail utils/ ambiguous), INC-007 (TDA log path), INC-008, INC-009 |

---

**Status — updated 2026-03-27:** Architectural stabilization phase complete.
  All HIGH-severity inconsistencies (INC-001, INC-002, INC-003) resolved.
  All MEDIUM-severity inconsistencies related to runtime state artifacts (INC-005, INC-006) resolved.
  Company_node documentation is now complete. Retail_node remains coherent.
  Remaining open: INC-004 (retail utils/ — verify disk), INC-007 (TDA log path), INC-008, INC-009.

**Next step:** Begin build sequence per NEXT_BUILD_SEQUENCE.md.
              Immediate: apply Step 3 cross-document consistency validation, then initialize
              `suggestions.json` on company_node and queue utility module build suggestions.
