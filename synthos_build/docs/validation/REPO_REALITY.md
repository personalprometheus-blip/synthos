# REPO_REALITY.md
## Synthos — Repository Reality Extraction
**Generated:** 2026-03-29
**Method:** Direct repo inspection, grep analysis, DB introspection
**Scope:** /home/pi/synthos/synthos_build/ + /home/pi/synthos-company/
**Rule:** Repo state overrides doc claims where they conflict. All inferences labeled [INFERRED].

---

## 1. FILE INVENTORY BY NODE/DOMAIN

### 1A — retail_node (synthos_build/)

| File | Status | Notes |
|------|--------|-------|
| agent1_trader.py | confirmed active | v1.2 logic (Option B, 5yr price history, member weights) |
| agent2_research.py | confirmed active | v1.2 logic (member weights, interrogation, news_feed) |
| agent3_sentiment.py | confirmed active | v1.1 logic — untouched in v1.2 per MASTER_STATUS |
| database.py | confirmed active | v1.2 schema (adds member_weights, news_feed, handshakes, pending_approvals, scan_log, outcomes) |
| portal.py | confirmed active | v1.2 (adds /news page, /api/news-feed) |
| heartbeat.py | confirmed active | writes heartbeat to local DB; imported by agents |
| synthos_heartbeat.py | confirmed active | POSTs session-end heartbeat to monitor_node |
| boot_sequence.py | confirmed active | 10-step startup; includes seed_backlog step |
| watchdog.py | confirmed active | crashes to suggestions.json via HARDCODED path (see conflicts) |
| health_check.py | confirmed active | |
| cleanup.py | confirmed active | nightly DB maintenance |
| shutdown.py | confirmed active | pre-maintenance graceful shutdown |
| patch.py | confirmed active | safe file replacement with .known_good/ backup |
| portal.py | confirmed active | port 5001 |
| interrogation_listener.py | confirmed active | UDP peer corroboration listener; started by boot_sequence.py |
| install_retail.py | confirmed active | canonical installer |
| uninstall.py | confirmed active | contains legacy "quorum" path references |
| sync.py | confirmed active | dev sync utility |
| seed_backlog.py | confirmed active | seeds suggestions.json on company node (misplaced — see conflicts) |
| validate_02.py | confirmed active | Phase 02 validation script |
| validate_03b.py | confirmed active | Phase 03b validation script |
| validate_env.py | confirmed active | env key validation |
| synthos_test.py | likely active but undocumented | not in any spec doc |
| strongbox.py | MISPLACED — confirmed in wrong location | belongs on company_node; present in retail build |
| first_run.sh | confirmed active | HARDCODED /home/pi/synthos (T-10, open) |
| console_cmd.sh | likely active but undocumented | |
| portal_cmd.sh | likely active but undocumented | |
| qpull.sh | likely active but undocumented | dev helper |
| qpush.sh | likely active but undocumented | dev helper |
| restore.sh | confirmed active | per MASTER_STATUS |
| synthos_monitor.py | confirmed present | just added to repo (was in synthos-company/ only); monitor_node file |
| patch.py | confirmed active | |
| license_validator.py | MISSING — referenced extensively | boot_sequence, arch docs, ops spec cite it as SECURITY gate; NOT FOUND anywhere |

### 1B — company_node (synthos-company/)

| File | Status | Notes |
|------|--------|-------|
| agents/patches.py | confirmed active | running --mode continuous |
| agents/blueprint.py | confirmed active | loads .env (not company.env — inconsistency) |
| agents/sentinel.py | confirmed active | Flask server on port 5004 |
| agents/fidget.py | confirmed active | |
| agents/librarian.py | confirmed active | |
| agents/scoop.py | confirmed active | |
| agents/vault.py | confirmed active | |
| agents/timekeeper.py | confirmed active | |
| utils/db_helpers.py | confirmed active | shared DB layer for all company agents |
| utils/synthos_paths.py | confirmed active | canonical path resolution for company node |
| install_company.py | confirmed active | |
| synthos_monitor.py | confirmed active | runs on port 5000 (monitor_node role) |
| migrate_agents.py | likely active but undocumented | migration tool (JSON→DB); no spec doc |
| generate_unlock_key.py | confirmed active | |
| setup_tunnel.sh | confirmed active | Cloudflare tunnel setup |
| installers/common/env_writer.py | confirmed active | writes COMMAND_PORT=5002, INSTALLER_PORT=5003, HEARTBEAT_PORT=5004 |
| installers/common/preflight.py | confirmed active | |
| installers/common/progress.py | confirmed active | |
| config/agent_policies.json | confirmed active | |
| config/allowed_ips.json | confirmed active | inbound allowlist for company Pi |
| config/market_calendar.json | confirmed active | |
| config/priorities.json | MISSING — referenced in arch doc | in TECHNICAL_ARCHITECTURE.md company directory tree; not on disk |
| synthos_agent_policy.yaml | likely active but undocumented | not in any spec doc |
| company.env | confirmed active | loaded by patches.py, sentinel.py, vault.py etc. |
| data/company.db | confirmed active | 172KB, active WAL (4.1MB) |
| data/suggestions.json | NOT PRESENT as file | company agents write to company.db.suggestions table via db_helpers; no file |

### 1C — operator_only

| File | Status | Notes |
|------|--------|-------|
| install_retail.py | confirmed active | operator runs to install retail Pi |
| install_company.py | confirmed active | operator runs to install company Pi |
| first_run.sh | confirmed active | post-install first run helper |
| restore.sh | confirmed active | disaster recovery |
| generate_unlock_key.py | confirmed active | operator issues autonomous mode keys |

### 1D — docs/legal/specs (synthos_build/)

| File | Modified | Status | Notes |
|------|----------|--------|-------|
| SYNTHOS_GROUND_TRUTH.md | Mar 27 14:16 | historical authority — partially stale | system_version 1.1; v1.2 changes not reflected |
| SYSTEM_MANIFEST.md | Mar 29 00:37 | current canonical manifest (v4.0) | agent renaming applied; v1.2 schema not reflected |
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | Mar 29 00:37 | current structural doc (v3.0) | DB schema SEVERELY outdated; sentinel port conflict; v1.2 features absent |
| SYNTHOS_OPERATIONS_SPEC.md | Mar 29 00:37 | current ops doc (v3.0) | generally accurate; update-staging branch doesn't exist |
| BLUEPRINT_SAFETY_CONTRACT.md | Mar 27 09:32 | confirmed active authority | |
| SUGGESTIONS_JSON_SPEC.md | Mar 27 09:24 | partially superseded | spec describes JSON file; actual impl uses company.db.suggestions table |
| POST_DEPLOY_WATCH_SPEC.md | Mar 27 09:27 | partially superseded | spec describes JSON; actual impl uses company.db.deploy_watches table |
| MANIFEST_PATCH.md | Mar 27 09:30 | historical — applied | patches described as applied in GROUND_TRUTH; doc retained for audit |
| HEARTBEAT_RESOLUTION.md | Mar 27 14:09 | confirmed active decision record | correctly documents deprecation; sentinel port 5004 reuse not noted |
| SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md | Mar 27 14:13 | confirmed active | |
| NEXT_BUILD_SEQUENCE.md | Mar 27 14:10 | partially stale | Steps 4-5 marked complete; Step 3 cross-doc consistency NOT complete |
| SYNTHOS_ADDENDUM_2_WEB_ACCESS.md | Mar 29 00:37 | speculative — not implemented | cloud portal layer; zero implementation; registered in manifest as active |
| TOOL_DEPENDENCY_ARCHITECTURE.md | Mar 27 12:23 | active but has gaps | missing company agent classifications (INC-009 open); hardcoded /home/pi/ (INC-007 open) |
| SYNTHOS_INSTALLER_ARCHITECTURE.md | Mar 27 08:20 | active but has gaps | hardcoded paths (INC-008 open) |
| synthos_design_brief.md | Mar 27 08:20 | historical context | pre-v1.0 origin doc |
| AGENT_ENHANCEMENT_PLAN.md | Mar 29 00:37 | planning doc | v3.1 future features |
| AUDITINGAGENT_SPECIFICATION_v1.md | Mar 29 00:37 | specification — not yet implemented | |
| EXECUTIONAGENT_SPECIFICATION_v2.md | Mar 29 00:37 | specification — not yet implemented | |
| RESEARCHAGENTS_SPECIFICATION_v2.md | Mar 29 00:37 | specification — not yet implemented | |
| SYNTHOS_MASTER_STATUS.md | Mar 28 10:35 | current project tracker | version mismatch with manifest |
| SYNTHOS_TODO_COMBINED.md | Mar 28 12:52 | current todo list | |
| BLUEPRINT_WORKFLOW_SPEC_ADDENDUM_1.md | Mar 27 08:20 | active | |
| INSTALLER_STATE_MACHINE.md | Mar 27 08:20 | active with open issues | |

### 1E — runtime artifacts / config templates

| Artifact | Location | Status |
|----------|----------|--------|
| signals.db | synthos_build/signals.db (runtime, not in data/) | confirmed; tables created at first run |
| data/signals.db | synthos_build/data/signals.db | EMPTY (no tables) — stale file or wrong path |
| data/company.db | synthos_build/data/company.db | WRONG LOCATION — company DB file in retail build (4KB, stale) |
| data/suggestions.json | synthos_build/data/suggestions.json | 15 seed entries; this is the SEED FILE, not the live pipeline |
| company.db | synthos-company/data/company.db | confirmed active — 172KB, WAL active |
| .patches_state.json | synthos-company/data/ | confirmed runtime state |
| patches_latest.json | synthos-company/data/ | confirmed runtime state |
| timekeeper_status.json | synthos-company/data/ | confirmed runtime state |

---

## 2. NAMING INCONSISTENCIES

### 2A — Agent Name Fragmentation (3 incompatible naming systems)

| Role | File Name | Informal Alias | Formal Name (v4.0) |
|------|-----------|----------------|---------------------|
| Trading execution | agent1_trader.py | Bolt | ExecutionAgent |
| Disclosure research | agent2_research.py | Scout | DisclosureResearchAgent |
| Sentiment | agent3_sentiment.py | Pulse | MarketSentimentAgent |
| Bug finder (company) | patches.py | Patches | Patches |
| Code implementer (company) | blueprint.py | Blueprint | Blueprint |

**Documents using informal aliases:** MASTER_STATUS, GROUND_TRUTH (partial), source file docstrings
**Documents using formal names:** SYSTEM_MANIFEST v4.0, TECHNICAL_ARCH v3.0, OPERATIONS_SPEC v3.0
**Documents mixed:** GROUND_TRUTH uses both
**Validators:** use file-based names (agent1_trader, etc.) — correct

### 2B — Version Number Fragmentation

| Document | Version Claimed |
|----------|----------------|
| SYNTHOS_GROUND_TRUTH.md | system_version: 1.1, manifest_version: 2.0 |
| SYNTHOS_MASTER_STATUS.md | System version: 1.2 |
| SYSTEM_MANIFEST.md | System version: 3.0, Manifest version: 4.0 |
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | Document version: 3.0 |
| SYNTHOS_OPERATIONS_SPEC.md | Document version: 3.0 |

No single canonical version number. "1.2" and "3.0" are tracking different things (feature version vs doc version) but this is not stated anywhere.

### 2C — File Naming Inconsistencies

| Observed | Issue |
|----------|-------|
| heartbeat.py + synthos_heartbeat.py | Two files, similar names, different roles — confusing |
| allowed_ips.json (company inbound) vs allowed_outbound_ips.json (retail outbound) | Different files, similar names, opposite directions |
| company.db (company node DB) vs signals.db (retail DB) | OK naming but company.db appears in retail build |
| suggestions.json (seed file in repo) vs suggestions table (live company DB) | Same logical name, different implementations |
| install_retail.py (exists) vs install.py (referenced in SYSTEM_MANIFEST v4.0 file registry) | manifest still lists install.py as active |

### 2D — Deprecated Legacy References Still Present

| Reference | Location | Status |
|-----------|----------|--------|
| /home/pi/quorum | uninstall.py:40 | legacy project name |
| .improvement_backlog.json | agent1_trader.py:2112,2119 | legacy artifact referenced in code |
| .audit_latest.json | agent1_trader.py:2119 | legacy artifact referenced in code |
| install.py | SYSTEM_MANIFEST v4.0 file registry | deprecated; canonical is install_retail.py |

---

## 3. PATH INCONSISTENCIES

### 3A — Hardcoded /home/pi/ violations

| File | Line(s) | Violation |
|------|---------|-----------|
| first_run.sh | 10 | SYNTHOS_DIR="/home/pi/synthos" — T-10 open |
| watchdog.py | 64 | COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data") — CRITICAL |
| watchdog.py | 25,52 | docstring/comment only (acceptable) |
| uninstall.py | 39-46 | hardcoded target paths (functionally correct but brittle) |
| strongbox.py | 504 | restore instructions in help text only |
| portal.py | 10,13 | cron example comments only (acceptable) |
| boot_sequence.py | 19 | cron example comment only (acceptable) |
| health_check.py | 6 | cron example comment only (acceptable) |
| shutdown.py | 6 | cron example comment only (acceptable) |
| sentinel.py | 39 | cron example comment only (acceptable) |
| seed_backlog.py | 182 | string in description text only (acceptable) |

**Critical:** watchdog.py:64 is a functional hardcoded path, not a comment. If deployed with a different username, suggestions.json writes silently fail.

### 3B — Path Architecture Contradiction

| Claim | Location | Reality |
|-------|----------|---------|
| Files live at ${SYNTHOS_HOME}/core/ | TECHNICAL_ARCH v3.0, SYSTEM_MANIFEST v4.0 | Repo has files at synthos_build/ (flat, no core/ subdir) |
| signals.db at ${SYNTHOS_HOME}/data/signals.db | arch docs | Runtime DB is at PROJECT_DIR/signals.db (flat); data/signals.db is empty |
| utils/ directory on retail Pi | arch docs, INC-004 | NO utils/ in synthos_build/ |

**Note [INFERRED]:** The `core/` subdirectory is the DEPLOYED layout. During development, files run flat from `synthos_build/`. This is an intentional distinction but not documented explicitly.

### 3C — Company Agent Path Patterns

| Agent | Path Pattern | Method |
|-------|-------------|--------|
| patches.py | `_HERE.parent` (agents/ → synthos-company/) | dynamic ✓ |
| blueprint.py | `_HERE.parent` | dynamic ✓ |
| sentinel.py | imports from `synthos_paths` | dynamic ✓ |
| vault.py | imports from `synthos_paths` | dynamic ✓ |
| librarian.py | imports from `synthos_paths` | dynamic ✓ |
| watchdog.py (retail) | PATH OF CONCERN: hardcoded company path | **violation** |

---

## 4. KEY MISSING FILES

| File | Referenced By | Severity | Notes |
|------|--------------|----------|-------|
| license_validator.py | boot_sequence.py, TECHNICAL_ARCH, OPERATIONS_SPEC, GROUND_TRUTH | CRITICAL | Security gate; called at every boot; not found anywhere |
| config/priorities.json | TECHNICAL_ARCH company dir tree | LOW | config/market_calendar.json exists but priorities.json does not |
| update-staging git branch | OPERATIONS_SPEC deployment pipeline | MEDIUM | Only `main` branch exists |
| allowed_outbound_ips.json | SYSTEM_MANIFEST v4.0, TECHNICAL_ARCH v3.0 | MEDIUM | retail Pi outbound allowlist; referenced but no file |
| .known_good/ directory | patch.py, watchdog.py | MEDIUM | snapshot directory for rollback; not in repo (runtime-created) |

---

## 5. KEY GHOST FILES (referenced but never existed or deprecated)

| File | Status | Evidence |
|------|--------|---------|
| heartbeat_receiver.py | deprecated — never built | HEARTBEAT_RESOLUTION.md |
| install.py | deprecated — never committed | GROUND_TRUTH §FILE_STATUS |
| post_deploy_watch.json | superseded by DB | migrate_agents.py migrates away from it |

---

## 6. MISPLACED FILES

| File | Current Location | Should Be In | Evidence |
|------|-----------------|-------------|---------|
| strongbox.py | synthos_build/ | synthos-company/agents/ | MASTER_STATUS, TECHNICAL_ARCH: company_node agent |
| synthos_monitor.py | synthos_build/ AND synthos-company/ | synthos-company/ is authoritative | monitor_node file; just added to repo as copy |
| data/company.db | synthos_build/data/ | synthos-company/data/ | retail DB is signals.db, not company.db |

---

## 7. TWO-IMPLEMENTATION ARTIFACTS

| Artifact | Old Implementation | New Implementation | Migration Status |
|----------|-------------------|-------------------|-----------------|
| suggestions pipeline | suggestions.json file | company.db.suggestions table | MIGRATED — db_helpers has post_suggestion(); some older agents (vault, sentinel, librarian, watchdog) still write to JSON file directly |
| post_deploy_watch | post_deploy_watch.json file | company.db.deploy_watches table | MIGRATED — db_helpers has post_deploy_watch(); patches.py uses DB; watchdog.py still reads JSON |

**This is the most dangerous divergence.** Multiple agents writing to suggestions.json directly will conflict with agents writing via db_helpers. The JSON and DB are NOT synchronized.
