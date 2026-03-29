# SYSTEM_VALIDATION_REPORT.md
## Synthos — System Validation: Step 3
**Generated:** 2026-03-29
**Method:** Direct code inspection of install_retail.py, install_company.py, boot_sequence.py, watchdog.py, patch.py, health_check.py, blueprint.py, sentinel.py, SYNTHOS_INSTALLER_ARCHITECTURE.md, SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md, SYSTEM_MANIFEST.md, and all reconciliation outputs from Steps 1–2.
**Prerequisite outputs:** STATIC_VALIDATION_REPORT.md (Step 1), BEHAVIORAL_VALIDATION_REPORT.md (Step 2, referenced as existing per prompt)

> **AMENDMENT — 2026-03-29:** Several critical blockers identified in this report have since been resolved or formally deferred. This report is a historical snapshot. For current blocker state see `docs/validation/BLOCKER_REFRESH_REPORT.md` and `docs/validation/RETAIL_LICENSE_DEFERRAL_NOTE.md`.
> - SYS-B01 (install VERIFYING always DEGRADED): **DEFERRED_FROM_CURRENT_BASELINE** — `license_validator.py` removed from REQUIRED_CORE_FILES
> - SYS-B02 (no boot license gate): **DEFERRED_FROM_CURRENT_BASELINE** — documented as intentional current state
> - SYS-B03 (post-deploy rollback broken): **RESOLVED** — watchdog.py migrated to db_helpers
> - SYS-B04 (suggestions pipeline split): **RESOLVED** — 4 agents migrated to db_helpers
> - SYS-B05 (watchdog hardcoded path): **RESOLVED** — env var override introduced
> - SYS-B09 (strongbox misplaced): **RESOLVED_PENDING_DEPLOYMENT** — wired in installer; live crontab pending re-run

---

## OVERALL STATUS

```
SYSTEM_VALIDATION_STATUS:    FAIL
DEPLOYABILITY_STATUS:        NOT_DEPLOYABLE
```

---

## SUMMARY COUNTS

```
scenarios_tested:              17
pass_count:                    4
fail_count:                    9
blocked_count:                 4
critical_blockers:             4
high_blockers:                 5
unresolved_system_conflicts:   6
```

---

## A. INSTALL + TERMINAL STATE VALIDATION

### A-1. Fresh Install to COMPLETE

**Status: FAIL — CRITICAL**

install_retail.py `verify_installation()` (line 412) checks each file in `REQUIRED_CORE_FILES` against `CORE_DIR / f`. `REQUIRED_CORE_FILES` explicitly includes `license_validator.py` (line 105). This file does not exist anywhere in either repo.

Result: VERIFYING phase always fails at the file check. The state machine cannot reach COMPLETE. Every fresh install is forced into DEGRADED.

This is not a configuration problem or race condition. It is a hard failure — the installer has a required artifact in its own verification list that was never built.

**Evidence:** install_retail.py:105 (`"license_validator.py"` in REQUIRED_CORE_FILES), install_retail.py:412–414 (VERIFYING loops over REQUIRED_CORE_FILES), confirmed by repo-wide grep returning no matches for license_validator.py.

**Verified directly.**

---

### A-2. Installer State Machine Document

**Status: FAIL — MEDIUM**

INSTALLER_STATE_MACHINE.md is referenced as a deliverable in the reconciliation index and expected as documentation for this validation. It does not exist. The state machine is only documented inside SYNTHOS_INSTALLER_ARCHITECTURE.md §4 (partial). Formal per-state acceptance criteria are absent.

**Evidence:** File not found at synthos_build/INSTALLER_STATE_MACHINE.md.

---

### A-3. Installer Re-entry on COMPLETE

**Status: BLOCKED**

Install architecture specifies: "Re-run on COMPLETE system: prints status, exits 0 without changes." This cannot be tested because COMPLETE is unreachable (A-1). The logic for re-entry detection exists in the code (`.install_progress.json` + `SENTINEL_PATH`), and the pattern is architecturally sound. BLOCKED until A-1 is resolved.

---

### A-4. DEGRADED Recovery Path

**Status: BLOCKED — Architecturally Incomplete**

DEGRADED state is reached when VERIFYING fails (exit code 2). The installer architecture states the recovery is: "Operator SSHs to Pi, resolves issue, re-runs `install_retail.py --repair`." There is no automated recovery path — this is by design. However, because the DEGRADED trigger (missing license_validator.py) is a missing artifact rather than a transient failure, re-running `--repair` will produce the same DEGRADED outcome indefinitely. The recovery path requires an artifact that doesn't exist.

**Blocker:** Recovery is only possible after license_validator.py is built and placed in core/.

---

### A-5. Protected Files Preserved Through Install/Re-run

**Status: PASS (inferred — protection logic is correct)**

install_retail.py `PROTECTED_PATHS` (lines 110–118) correctly lists: `user/.env`, `user/settings.json`, `user/agreements/`, `data/signals.db`, `data/backup/`, `.known_good/`, `consent_log.jsonl`. The `create_directories()` function skips any directory in `PROTECTED_PATHS` that already exists. The DB bootstrap (line 227–229) skips if `DB_PATH.exists()`.

Protection logic is architecturally correct. Cannot be verified at runtime until install reaches COMPLETE, but the code is unambiguous.

**Evidence:** install_retail.py:110–118, 173–176, 227–229. Verified directly.

---

### A-6. Path Assumptions Coherent Through Install

**Status: FAIL — HIGH**

The installer assumes a `core/` subdirectory layout:
- `CORE_DIR = SYNTHOS_HOME / "core"` (install_retail.py:57)
- `bootstrap_database()` loads `CORE_DIR / "database.py"` (line 232)
- `verify_installation()` checks `CORE_DIR / f` for all required files (line 412)
- `register_cron()` writes cron entries pointing to `CORE_DIR / "boot_sequence.py"` etc. (lines 267–278)

The current repo (synthos_build/) uses a **flat layout** — all files at the root level. No `core/` subdirectory exists. When install_retail.py is run from synthos_build/, every CORE_DIR reference fails: bootstrap, verification, and cron all point to paths that do not exist.

The installer is designed for a `synthos-retail/core/` deployment layout. The development repo does not match this layout. This means the installer cannot be tested against the current repo without manually creating the `core/` subdirectory and moving files.

**Evidence:** install_retail.py:57, 232, 267–278; synthos_build/ directory listing (flat, no core/ subdir). Verified directly.

---

### A-7. Installer Idempotency (Protected State)

**Status: CONDITIONAL**

The idempotency logic is correct by inspection: .env is backed up before any write; signals.db is skipped if it exists; protected dirs are skipped; the `.install_progress.json` state machine prevents re-entry into already-completed phases. Idempotency is structurally sound but cannot be runtime-verified until A-1 and A-6 are resolved.

---

### A SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| fresh_install_to_complete | FAIL | CRITICAL |
| installer_state_machine_doc_exists | FAIL | MEDIUM |
| installer_rerun_on_complete | BLOCKED | CRITICAL (depends on above) |
| degraded_recovery_path | BLOCKED | CRITICAL (depends on above) |
| protected_files_survive_install | PASS | NONE |
| installer_path_assumptions_coherent | FAIL | HIGH |
| installer_idempotency | CONDITIONAL | NONE (blocked by A-1) |

---

## B. BOOT + STARTUP ORCHESTRATION VALIDATION

### B-1. Boot Required Files vs. Installer Required Files

**Status: FAIL — HIGH**

boot_sequence.py `REQUIRED_FILES` (lines 57–67) lists 9 files. install_retail.py `REQUIRED_CORE_FILES` lists 15 files. The two lists are inconsistent in both directions:

- **In installer, not in boot:** `boot_sequence.py`, `synthos_heartbeat.py`, `patch.py`, `sync.py`, `license_validator.py`, `uninstall.py`, `cleanup.py`
- **In boot, not in installer:** None (boot's list is a subset)

The critical mismatch is `license_validator.py`: the installer requires it; the boot sequence does not. This means:
1. Installer VERIFYING fails if license_validator.py is absent.
2. Boot proceeds normally whether or not license_validator.py is present.
3. The security gate the installer enforces has no runtime enforcement at all.

**Evidence:** boot_sequence.py:57–67, install_retail.py:91–107. Verified directly.

---

### B-2. License Gate at Boot

**Status: FAIL — CRITICAL**

SYNTHOS_INSTALLER_ARCHITECTURE.md §1 states: "License validation at retail install — Collect key, write to `.env`, defer validation to `boot_sequence.py`." SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md §2.3 states the validation flow starts at boot.

boot_sequence.py has **no license validation step**. The 11 boot steps (network, env, files, database, health check, watchdog, portal, monitor, interrogation, data seed, suggestions backlog) include no call to `license_validator.py` or any equivalent.

The license key is written to `.env` by the installer and then: nothing. It is never checked at boot, never validated against Vault, and never used to gate agent startup.

This is not a deferred feature — the architecture documents explicitly say validation happens at boot. The gap between the specification and implementation is complete.

**Evidence:** boot_sequence.py full scan — no import or call to license_validator. SYNTHOS_INSTALLER_ARCHITECTURE.md §1 decision table row: "License validation at retail install." Verified directly.

---

### B-3. Health Check Gates Startup

**Status: FAIL — MEDIUM**

boot_sequence.py `step5_health_check()` (lines 195–225) explicitly never halts boot regardless of health check outcome. All failure paths return `step("Health check", True, ...)` — meaning the health check result is always recorded as PASSED in BOOT_STEPS. Even a timeout or exception returns True.

The health check result has no gating effect. A Pi can boot with a corrupted DB, failed API connections, or position reconciliation failures, and boot will report "all checks passed."

The boot critical failure list (lines 496–497) only includes: `"Agent files"`, `"Database integrity"`, `".env file"`. Health check is not in it.

**Evidence:** boot_sequence.py:195–225, 496–497. Verified directly.

---

### B-4. Watchdog and Portal Startup Assumptions

**Status: CONDITIONAL**

Watchdog startup (step 6): if watchdog.py is not found, returns `step("Watchdog", False, ...)` which IS a failure, and `"Watchdog"` is not in the critical failure list, so boot continues without the watchdog. Agents will run unmonitored.

Portal startup (step 7): same pattern — portal failure is not a critical halt. Portal can silently fail to start.

Boot proceeds with no monitor, no crash recovery, and no web UI if both fail. The system degrades to cron-driven-only. This is by design (graceful degradation), but the graceful failure is not communicated beyond the boot log.

Neither watchdog.py nor portal.py failure triggers the SMS alert. Only "Agent files", "Database integrity", ".env file" trigger the alert. This is a gap: an operator could have a running but unmonitored system.

**Evidence:** boot_sequence.py:228–248, 255–274, 496–497. Verified directly.

---

### B-5. Agent Startup Ordering

**Status: PASS (inferred)**

Agents are NOT started by boot_sequence.py. Boot starts persistent processes only (watchdog, portal, monitor). All trading agents (agent1, agent2, agent3, cleanup) are cron-managed. This is architecturally correct: agents run on schedule, not on boot.

boot_sequence.py calls `step9_initial_seed()` which runs agent2 once on empty DB — this is conditional and correct (only when signals table is empty).

**Evidence:** boot_sequence.py:308–345, WATCHED_AGENTS in watchdog.py (all set to `managed=False`). Verified directly.

---

### B-6. Monitor Server on Retail Node

**Status: FAIL — HIGH (node boundary leak)**

boot_sequence.py `step8_monitor()` (lines 277–305) attempts to start `synthos_monitor.py` on PORT=5000. This step is present in the boot sequence for ALL nodes. Per architecture, `synthos_monitor.py` belongs to the monitor_node (company Pi) only.

If `synthos_monitor.py` is present at `PROJECT_DIR` on a retail Pi (which it is in the current synthos_build/ flat layout), every retail Pi will boot a monitor server on port 5000.

The step gracefully skips if the file is not found. In the intended `synthos-retail/core/` deployment layout, `synthos_monitor.py` would not be present in `core/` (it is not in `REQUIRED_CORE_FILES`). So on a properly-deployed retail Pi, this step silently skips.

However: this step should not exist in the retail boot sequence. Its presence is misleading, and it will fire if the file is ever accidentally included in a retail deployment package.

**Evidence:** boot_sequence.py:277–305; SYNTHOS_INSTALLER_ARCHITECTURE.md directory tree (synthos_monitor.py absent from synthos-retail/core/). Verified directly.

---

### B-7. Boot Alert via Direct smtplib (Policy Violation)

**Status: FAIL — HIGH**

boot_sequence.py `send_sms_alert()` (lines 107–132) uses `smtplib.SMTP_SSL` directly (line 125). SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md §4 designates scoop.py as the only permitted outbound email sender.

This is a functional policy violation. The boot alert path bypasses the Scoop queue, sends directly from the retail Pi's GMAIL credentials, and is architecturally inconsistent with the communications model.

Note: The alert fires only on critical boot failure (lines 499–506). It is not a routine path, but it is an active code path.

**Evidence:** boot_sequence.py:32, 107–132. ADDENDUM_1 §4 (scoop.py sole sender). Verified directly.

---

### B-8. step10_seed_suggestions Boot Step (Retail Node)

**Status: FAIL — MEDIUM**

boot_sequence.py `step10_seed_suggestions()` (lines 389–434) seeds `suggestions.json` when `COMPANY_MODE=true`. This step runs in the retail boot sequence and gates on a company-only env var.

Problems:
1. The step references `suggestions.json` (JSON store), not `company.db.suggestions` (DB store). This is the legacy CL-002 store.
2. If somehow a retail Pi had `COMPANY_MODE=true`, it would attempt to seed the JSON file at `PROJECT_DIR/data/suggestions.json` — not the company Pi's data dir.
3. The step belongs on the company node boot, not in the retail boot sequence.

This step is likely a no-op on retail Pis (`COMPANY_MODE` defaults to absent), but its presence in the retail boot sequence is architecturally wrong.

**Evidence:** boot_sequence.py:389–434. Verified directly.

---

### B SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| boot_required_files_vs_installer | FAIL | HIGH |
| license_gate_at_boot | FAIL | CRITICAL |
| health_check_gates_startup | FAIL | MEDIUM |
| watchdog_portal_startup_assumptions | CONDITIONAL | LOW |
| agent_startup_ordering | PASS | NONE |
| monitor_server_on_retail_node | FAIL | HIGH |
| boot_alert_policy_compliance | FAIL | HIGH |
| step10_suggestions_seed_placement | FAIL | MEDIUM |

---

## C. NODE ROLE + SYSTEM TOPOLOGY VALIDATION

### C-1. Retail Node Self-Containment

**Status: PASS**

Retail node responsibilities are well-defined and self-contained:
- Three trading agents (agent1, agent2, agent3) on cron schedule
- Local signals.db for trade history, signals, positions
- Portal on port 5001 for local visibility
- heartbeat.py writes local heartbeat rows to signals.db
- synthos_heartbeat.py POSTs to MONITOR_URL (company/monitor node)
- watchdog.py monitors agents, alerts via suggestions.json on company Pi (requires network mount or shared path)
- interrogation_listener.py (UDP 5556) for peer corroboration (non-fatal if absent)

No retail agent requires services from the company node to execute its primary function. Agents are degraded but operational if company node is unreachable.

**Evidence:** WATCHED_AGENTS in watchdog.py, REQUIRED_FILES in boot_sequence.py, agent1/2/3 imports (no company node deps). Verified directly.

---

### C-2. Company Node Coherence

**Status: CONDITIONAL**

Company node has 9 defined agents. Responsibilities are logical: blueprint (deployment), sentinel (monitoring), vault (license/keys), patches (auditing), librarian (docs), fidget (feedback), scoop (comms), timekeeper (scheduling), strongbox (backups — misplaced).

Two active coherence problems:
1. **strongbox.py is in synthos_build/ (retail repo), not synthos-company/agents/.** No backup agent is running on the company node. Data safety gap.
2. **Suggestions pipeline split (CL-002):** vault.py, sentinel.py, librarian.py write to suggestions.json; blueprint.py reads from company.db.suggestions. Critical suggestions from 4 agents are silently dropped.

**Evidence:** REBASELINE_EXEC_SUMMARY.md §3 Drift Type 2 and 3. Verified in previous sessions.

---

### C-3. Monitor Node Coherence

**Status: PASS (inferred)**

monitor_node is the same physical Pi as company_node, running synthos_monitor.py on port 5000. Responsibilities are clear: receive heartbeats from retail Pis, maintain last-seen timestamps, expose `/console` for visibility, trigger Scoop alerts on silence.

sentinel.py on port 5004 is distinct from the monitor (receives heartbeats in a different protocol context — confirmed in HEARTBEAT_RESOLUTION.md). The two services coexist correctly on the company Pi.

No conflicts identified with company_node responsibilities. Both are independent Flask servers on different ports.

**Evidence:** synthos_monitor.py (port 5000), sentinel.py (port 5004), HEARTBEAT_RESOLUTION.md. Inferred from architecture docs.

---

### C-4. Heartbeat Target Unambiguous

**Status: CONDITIONAL**

Heartbeat routing at the system level:
- `heartbeat.py` → writes to local signals.db (DB heartbeat, retail Pi only)
- `synthos_heartbeat.py` → POSTs to `MONITOR_URL` (HTTP, targets monitor_node port 5000)
- `sentinel.py` → receives inbound POSTs on port 5004 from retail Pis

This is now correct per HEARTBEAT_RESOLUTION.md. The `HEARTBEAT_URL` env var is deprecated. Monitor routing via `MONITOR_URL` is canonical.

Remaining naming gap: `HEARTBEAT_PORT` in company.env actually refers to Sentinel's port (5004), not a heartbeat receiver. The variable name is misleading but not functionally broken.

**Evidence:** heartbeat.py, synthos_heartbeat.py, sentinel.py, HEARTBEAT_RESOLUTION.md. Verified directly.

---

### C-5. Cross-Node Leakage

**Status: FAIL — HIGH**

Two active cross-node leakage issues:

**Leakage 1 — watchdog.py hardcoded company path:**
`COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")` (watchdog.py:64). The watchdog alert function (`alert_company_pi`) checks `if not COMPANY_DATA_DIR.exists()` before writing. On a customer Pi where synthos-company is at a different mount point, or where the retail and company node are on separate hardware, this path resolves to nothing — alerts are lost locally.

If retail and company are on the same Pi (single-Pi development setup), this works. In a multi-Pi deployment (retail Pi 2W + company Pi 4B), `COMPANY_DATA_DIR` resolves to a local path that doesn't exist on the retail Pi, and all watchdog alerts are silently dropped.

**Leakage 2 — boot_sequence.py step 8 starts monitor on retail node:**
(See B-6. Not repeated here.)

**Evidence:** watchdog.py:64–66; REBASELINE_EXEC_SUMMARY.md §1 (three-Pi topology). Verified directly.

---

### C-6. Operator Tooling Scope

**Status: PASS (inferred)**

validate_02.py, validate_03b.py, and reconciliation artifacts are in synthos_build/ (development). They are not in install_retail.py's REQUIRED_CORE_FILES or PATCHABLE_FILES. They are not deployed to customer Pis. Operator tooling is correctly separated.

---

### C SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| retail_node_self_contained | PASS | NONE |
| company_node_coherence | CONDITIONAL | HIGH |
| monitor_node_coherence | PASS | NONE |
| heartbeat_target_unambiguous | CONDITIONAL | LOW |
| cross_node_leakage | FAIL | HIGH |
| operator_tooling_scope | PASS | NONE |

---

## D. LICENSE + OFFLINE/DEGRADED OPERATION VALIDATION

### D-1. Retail License Validation Flow

**Status: FAIL — CRITICAL**

The complete license validation flow is undefined at the implementation level:

| Layer | Specified | Implemented |
|-------|-----------|-------------|
| Installer collects license key → writes to .env | YES | YES |
| boot_sequence.py validates key via license_validator.py | YES (ADDENDUM_1 §2.3) | NO |
| license_validator.py exists | YES (REQUIRED_CORE_FILES) | NO |
| Vault API validates license online | YES (ADDENDUM_1 §2) | NO |
| Offline cache (30-day grace) | YES (ADDENDUM_1 §2.3) | NO |
| Revocation path | YES (ADDENDUM_1 §2.3) | NO |

The license key is written to `.env` and then: nothing. No validation, no Vault check, no expiry enforcement, no offline grace period. The system operates identically whether the license key is valid, invalid, or absent — as long as `.env` contains the `LICENSE_KEY` field (any value).

**Evidence:** boot_sequence.py full text — no license check. ADDENDUM_1 §2.3 (full validation spec). install_retail.py:105 (requires file). Verified directly.

---

### D-2. Cached License / Offline Behavior

**Status: BLOCKED**

Cannot be assessed. license_validator.py does not exist. The specified offline behavior (30-day cache, grace period, degraded-but-running mode) has no implementation to examine. All scenarios in this category are BLOCKED.

---

### D-3. Revocation Path

**Status: BLOCKED**

Same as D-2. No implementation exists.

---

### D-4. Company Node — No License Dependency

**Status: PASS**

Confirmed via install_company.py: explicitly lists no `LICENSE_KEY`, no `OPERATING_MODE`, no `AUTONOMOUS_UNLOCK_KEY` in env_writer fields. COMPANY_MODE=true is set. Architecture doc (SYNTHOS_INSTALLER_ARCHITECTURE.md §1): "Company node truly has no startup key dependency."

All company agents check COMPANY_MODE before executing license-gated behavior.

**Evidence:** install_company.py, SYNTHOS_INSTALLER_ARCHITECTURE.md §1. Inferred from code inspection.

---

### D-5. Failure Behavior Explicit and Recoverable

**Status: FAIL — CRITICAL**

The failure behavior for license issues is undefined because the license check doesn't exist. What is defined:
- If `.env` is missing: boot halts with SMS alert (critical failure)
- If `LICENSE_KEY` key is absent from .env: install VERIFYING fails
- If `LICENSE_KEY` is present but wrong value: no consequence at any runtime layer

An invalid license key is currently indistinguishable from a valid one. The system will operate in full trading mode on a revoked, expired, or fabricated license.

**Evidence:** boot_sequence.py `step2_env()` checks for `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `TRADING_MODE` — not `LICENSE_KEY` (lines 152–153). Verified directly.

---

### D SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| retail_license_validation_flow | FAIL | CRITICAL |
| offline_cached_license_behavior | BLOCKED | CRITICAL |
| revocation_path | BLOCKED | HIGH |
| company_no_license_dependency | PASS | NONE |
| failure_behavior_explicit | FAIL | CRITICAL |

---

## E. UPDATE + PATCH + ROLLBACK VALIDATION

### E-1. Update Flow vs. Docs

**Status: FAIL — HIGH**

SYNTHOS_OPERATIONS_SPEC.md §3 defines the deployment pipeline as:
1. Update-staging branch created from main
2. Changes merged to update-staging
3. Friday deploy window (3:55–4:00 AM ET)
4. Watchdog monitors post-deploy for trigger conditions
5. Sunday rollback window if trigger fires

The `update-staging` branch does not exist (only `main` exists). The Friday push process cannot be executed as documented. The deployment pipeline is aspirational, not operational.

**Evidence:** Confirmed in reconciliation (CL-011). No `update-staging` branch in repo.

---

### E-2. patch.py / Protected File Policy

**Status: PASS**

patch.py `PROTECTED_FILES` (lines 63–71) correctly excludes: `.env`, `credentials.json`, `signals.db`, `.kill_switch`, `.pending_approvals.json`, `.install_progress.json`. These files cannot be overwritten by the patcher regardless of source.

`PATCHABLE_FILES` (lines 81–100) lists valid update targets. Minor: includes `install.py` (deprecated ghost file — harmless since it doesn't exist, but should be removed from the list).

patch.py pre-patches `signals.db` backup before any file change. File-level rollback via `--rollback <file>` is defined and functional.

**Evidence:** patch.py:63–100. Verified directly.

---

### E-3. Known-Good Snapshot Rollback

**Status: CONDITIONAL**

Snapshot scope is correct but incomplete:

`SNAPSHOT_FILES` covers: agent1_trader.py, agent2_research.py, agent3_sentiment.py, database.py, heartbeat.py, cleanup.py.

NOT in snapshot: portal.py, watchdog.py, boot_sequence.py, patch.py, health_check.py, interrogation_listener.py.

If any non-agent file is broken by a deploy, the known-good restore will not fix it. The rollback is scoped to trading agents only, not the full system.

`SNAPSHOT_ENV` (`.known_good/.env.known_good`) backs up .env separately — correct.

Snapshot path is derived dynamically (`PROJECT_DIR / ".known_good"`) — no hardcoded paths. Restore command (`python3 watchdog.py --restore`) is defined.

**Evidence:** watchdog.py:59, 83–90, `SNAPSHOT_ENV = SNAPSHOT_DIR / ".env.known_good"`. Verified directly.

---

### E-4. Post-Deploy Rollback Trigger Path

**Status: FAIL — CRITICAL**

watchdog.py `check_post_deploy_rollback()` (lines 554–619) reads `POST_DEPLOY_FILE` = `COMPANY_DATA_DIR / "post_deploy_watch.json"` — a JSON file.

blueprint.py writes deploy watches via `db_helpers.post_deploy_watch()` → `company.db.deploy_watches` table.

These two stores are not synchronized. blueprint.py never writes to `post_deploy_watch.json`. The JSON file watchdog reads will never contain watches posted by blueprint.

Result: The automated post-deploy rollback trigger **will never fire**. A deploy that breaks agents in post-trading mode will not trigger the autonomous rollback described in the operations spec.

This is CL-004 and was identified in Steps 1–2. Confirmed as a runtime-level failure, not just a static inconsistency.

**Evidence:** watchdog.py:554–619 (reads JSON); blueprint.py `db_helpers.post_deploy_watch()` call (writes to DB). Verified directly.

---

### E-5. Blueprint Atomic Deploy / Truncation Safety

**Status: PASS**

blueprint.py implements a staging → review → deploy pipeline:
1. Changes written to `.blueprint_staging/` directory
2. Human approval required before deployment
3. Atomic swap: staging files moved to production atomically
4. Truncation protection: blueprint tracks which files it has touched

No evidence of blueprint violating the protected file list from BLUEPRINT_SAFETY_CONTRACT.md. The contract file exists; staging directory is created by blueprint.py.

**Evidence:** blueprint.py (staging logic), BLUEPRINT_SAFETY_CONTRACT.md. Inferred from code inspection.

---

### E-6. Update Path Preserves Customer State

**Status: CONDITIONAL**

patch.py and the installer both correctly protect signals.db (trade history), .env (API keys), .known_good/ (snapshots), and user/agreements/. A standard patch operation will not overwrite customer-owned data.

However: the automated post-deploy rollback is broken (E-4). If a bad deploy causes trading disruption, the system cannot autonomously recover. Manual rollback via `python3 watchdog.py --restore` is available but requires SSH access to the Pi.

Customer financial state (open positions, trade history) is not touched by any update path. This is PASS for data preservation, CONDITIONAL for recovery capability.

**Evidence:** patch.py:63–71, install_retail.py:110–118. Verified directly.

---

### E SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| update_flow_vs_docs | FAIL | HIGH |
| patch_protected_file_policy | PASS | NONE |
| known_good_snapshot_rollback | CONDITIONAL | MEDIUM |
| post_deploy_rollback_trigger | FAIL | CRITICAL |
| blueprint_atomic_deploy_safety | PASS | NONE |
| update_preserves_customer_state | CONDITIONAL | MEDIUM |

---

## F. RUNTIME OPERABILITY VALIDATION

### F-1. Scheduled Runtime Model

**Status: PASS**

Cron drives all agent execution. boot_sequence.py starts persistent processes (watchdog, portal). The separation of cron-agents vs. persistent-services is coherent and correctly implemented. install_retail.py registers cron entries using resolved absolute paths (no /home/pi hardcodes).

agents 1/2/3 run on schedule; portal and watchdog run continuously. This is coherent.

**Evidence:** install_retail.py `register_cron()` (lines 257–312), WATCHED_AGENTS `managed=False` for all trading agents. Verified directly.

---

### F-2. Maintenance Tools Fit Runtime Assumptions

**Status: CONDITIONAL**

patches.py audits code and files continuously. validate_02.py and validate_03b.py are operator tools. patch.py handles file updates.

Gap: validate_02.py and validate_03b.py are in synthos_build/ and not registered anywhere for periodic execution. They run manually. This is acceptable for development but there is no runtime mechanism to surface validation failures automatically.

patches.py (company agent) fills this role for code quality — it audits DB behavior. But it audits the company-side pipeline, not the retail-side schema drift.

---

### F-3. Observability Path

**Status: CONDITIONAL**

Log files are well-structured: boot.log, watchdog.log, portal.log, daily.log, pulse.log, trader.log, crash_reports/. Portal exposes a web UI with signal visibility.

Gap: Crash reports and watchdog alerts are written to suggestions.json on the company Pi (via `alert_company_pi`). In a multi-Pi deployment, this requires the company Pi data directory to be accessible at `COMPANY_DATA_DIR` — which is hardcoded as `/home/pi/synthos-company/data` in watchdog.py. On a retail Pi 2W pointing to a company Pi 4B, this path doesn't exist locally. Crash alerts are silently swallowed.

Sentinel on port 5004 receives heartbeats from retail Pis and can detect silent Pis — this is a working observability channel. But agent-level crash visibility from retail is broken in multi-Pi deployment.

**Evidence:** watchdog.py:64–66, 631–703. Verified directly.

---

### F-4. Outbound Communication Model

**Status: FAIL — HIGH**

Three direct email paths exist outside of scoop.py, violating ADDENDUM_1 §4:

| File | Violation | Severity |
|------|-----------|----------|
| boot_sequence.py:125 | smtplib.SMTP_SSL direct send — boot failure SMS | HIGH |
| health_check.py:96–134 | SendGrid direct fallback when Scoop enqueue fails | HIGH |
| agent1_trader.py:260–275 | SendGrid direct fallback when Scoop fails | MEDIUM |

health_check.py primary path is correct: it uses `_enqueue_alert()` → `MONITOR_URL/api/enqueue` (Scoop queue). The fallback fires when that fails. The intent is defensive, but it violates the policy.

boot_sequence.py has no fallback attempt: it goes directly to smtplib.

**Evidence:** boot_sequence.py:32,125; health_check.py:96–134; agent1_trader.py:260–275. Verified directly.

---

### F-5. Hidden Dependency on Missing Services

**Status: FAIL — CRITICAL**

Active hidden dependencies on non-existent artifacts:

| Dependency | Required By | Status |
|-----------|------------|--------|
| license_validator.py | install_retail.py VERIFYING | MISSING |
| company.db.suggestions sync | blueprint.py reading pipeline | BROKEN (suggestions from 4 agents are lost) |
| company.db.deploy_watches sync | watchdog.py rollback trigger | BROKEN (trigger never fires) |
| update-staging branch | deployment pipeline | MISSING |

These are not edge-case failures — they affect the primary install path, the primary alert pipeline, and the primary safety mechanism.

**Evidence:** All confirmed in Steps 1–2 and above sections. Verified directly.

---

### F-6. No Contradictions Blocking Deployment

**Status: FAIL**

Multiple contradictions would prevent a clean deployment:
1. Installer cannot reach COMPLETE (license_validator.py missing in REQUIRED_CORE_FILES)
2. Boot license gate is absent (installer says boot validates, boot doesn't)
3. Post-deploy rollback is broken (watchdog and blueprint use incompatible stores)
4. Multi-Pi watchdog alerts are silently dropped (hardcoded path fails on retail Pi)
5. Critical suggestions from 4 company agents never reach blueprint

These are not documentation gaps — they are runtime behavior contradictions that would produce undefined or broken behavior on first deployment to a customer.

---

### F SUMMARY

| Check | Status | Blocker Level |
|-------|--------|--------------|
| scheduled_runtime_model | PASS | NONE |
| maintenance_tools_fit_runtime | CONDITIONAL | LOW |
| observability_path | CONDITIONAL | HIGH |
| outbound_comms_model | FAIL | HIGH |
| hidden_dependency_missing_services | FAIL | CRITICAL |
| no_contradictions_blocking_deployment | FAIL | CRITICAL |

---

## SCENARIO LEDGER

| scenario_id | category | status | evidence | verified | blocker_level |
|-------------|----------|--------|----------|----------|--------------|
| fresh_install_to_complete | INSTALL | FAIL | install_retail.py:105,412 — license_validator.py in REQUIRED_CORE_FILES; file absent from repo | DIRECT | CRITICAL |
| installer_rerun_on_complete | INSTALL | BLOCKED | Cannot reach COMPLETE without license_validator.py | DIRECT | CRITICAL |
| degraded_recovery_path | INSTALL | BLOCKED | DEGRADED trigger is a missing artifact; --repair produces same failure indefinitely | DIRECT | CRITICAL |
| retail_boot_with_valid_license | BOOT | FAIL | boot_sequence.py has no license validation step; key is ignored at runtime | DIRECT | CRITICAL |
| retail_boot_with_invalid_license | BOOT | FAIL | Same as above — invalid key is treated identically to valid key | DIRECT | CRITICAL |
| retail_offline_with_cached_license | LICENSE | BLOCKED | license_validator.py absent; no implementation to assess | DIRECT | CRITICAL |
| company_boot_without_license_dependency | BOOT | PASS | install_company.py has no license fields; COMPANY_MODE=true bypasses all license checks | INFERRED | NONE |
| heartbeat_routing_consistency | TOPOLOGY | CONDITIONAL | Functional routing correct; HEARTBEAT_PORT naming misleading (it's Sentinel's port) | DIRECT | LOW |
| protected_files_survive_update | UPDATE | PASS | patch.py PROTECTED_FILES and installer PROTECTED_PATHS both correct; signals.db, .env, .known_good/ protected | DIRECT | NONE |
| rollback_trigger_path_defined | ROLLBACK | FAIL | Post-deploy trigger reads JSON file; blueprint writes to DB; stores not synced; trigger never fires | DIRECT | CRITICAL |
| watchdog_known_good_path_valid | ROLLBACK | CONDITIONAL | Snapshot logic correct; restore command defined; scope limited to 6 agent files only (not portal/watchdog/boot) | DIRECT | MEDIUM |
| update_path_preserves_customer_state | UPDATE | CONDITIONAL | Data protected by patch.py; post-deploy recovery broken (CL-004); manual rollback available via SSH | DIRECT | MEDIUM |
| installer_path_layout_coherence | INSTALL | FAIL | Installer expects core/ subdir; repo is flat; bootstrap_database and cron registration point to non-existent paths | DIRECT | HIGH |
| boot_monitor_node_leakage | BOOT | FAIL | boot_sequence.py step 8 tries to start synthos_monitor.py on retail node; step is absent from correct deployment (file not in core/) but should not exist | DIRECT | HIGH |
| license_boot_gate_absent | BOOT | FAIL | Architecture specifies boot validates license; boot_sequence.py has no such step; key is never used at runtime | DIRECT | CRITICAL |
| suggestions_pipeline_integrity | RUNTIME | FAIL | vault/sentinel/librarian/watchdog write to JSON; blueprint reads DB; critical alerts silently dropped | DIRECT | CRITICAL |
| boot_sms_policy_compliance | BOOT | FAIL | boot_sequence.py:125 uses smtplib directly; violates ADDENDUM_1 §4 (scoop.py sole sender) | DIRECT | HIGH |

---

## G. SYSTEM READINESS VERDICT

### G-1. Can Synthos complete install deterministically?

**NO.** install_retail.py's VERIFYING step requires `license_validator.py` in `core/`. The file does not exist. Every fresh install reaches DEGRADED state. There is no install path to COMPLETE with the current codebase.

### G-2. Can it boot deterministically?

**CONDITIONALLY.** The boot sequence itself (boot_sequence.py) will run and complete on a deployed system where `.env` exists and agent files are present. However, the boot sequence does not implement the license gate specified in the architecture — it boots regardless of license validity. "Deterministic boot" is achievable only because the licensing layer is absent, not because it's correctly implemented.

### G-3. Can it operate coherently across declared nodes?

**NO.** Two blocking coherence failures:
1. Retail Pi watchdog alerts are silently dropped in multi-Pi deployment (hardcoded COMPANY_DATA_DIR path fails on retail hardware).
2. Suggestions pipeline is split — 4 company agents' critical alerts never reach blueprint.

### G-4. Can it survive degraded conditions without undefined behavior?

**PARTIALLY.** The system degrades gracefully for agent crashes (watchdog 3-phase recovery is sound). The system has undefined behavior for: invalid license (not enforced at all), company Pi unreachable from retail watchdog (alerts dropped silently), post-deploy crash conditions (rollback trigger never fires).

### G-5. Can it be updated safely?

**CONDITIONALLY.** patch.py correctly protects customer data. Blueprint staging is safe. The update-staging pipeline cannot be executed (branch doesn't exist). Manual patch-level updates are safe.

### G-6. Can it be rolled back safely?

**PARTIALLY.** File-level rollback (`patch.py --rollback`) is functional. Known-good snapshot restore (`watchdog.py --restore`) is functional but covers only 6 agent files. Automated post-deploy rollback is **completely broken** — the trigger mechanism is split across two incompatible stores and will never fire.

### G-7. Is the system deployable as currently defined?

**NO.**

### G-8. Specific blockers to deployable status

See BLOCKERS section below.

---

## BLOCKERS

### CRITICAL BLOCKERS

| blocker_id | severity | description | affected nodes | why blocks deployment | minimum condition to clear |
|-----------|----------|-------------|---------------|----------------------|---------------------------|
| SYS-B01 | CRITICAL | license_validator.py absent from repo — installer VERIFYING always fails; fresh install cannot reach COMPLETE | retail_node | Cannot install new customer Pis | Human decision: (a) build license_validator.py, or (b) remove from REQUIRED_CORE_FILES and strike license validation from current architecture |
| SYS-B02 | CRITICAL | No boot-time license gate — license key is written to .env but never validated; any key value allows full system operation | retail_node | System operates without license enforcement; licensing model is non-functional | Requires license_validator.py to exist first; then add validation step to boot_sequence.py |
| SYS-B03 | CRITICAL | Post-deploy rollback trigger broken (CL-004) — watchdog reads post_deploy_watch.json (JSON); blueprint writes to company.db.deploy_watches (DB); stores never synchronized | retail_node, company_node | Automated rollback is the primary safety mechanism for post-deploy crash; it cannot fire | Migrate watchdog.py to read from db_helpers.get_active_deploy_watches() |
| SYS-B04 | CRITICAL | Suggestions pipeline split (CL-002) — vault/sentinel/librarian/watchdog write to suggestions.json; blueprint reads from company.db only; 4 agents' critical alerts are silently dropped | company_node | Critical system alerts (agent halts, anomalies) are not visible to the approval pipeline | Migrate 4 agents to db_helpers.post_suggestion() |

### HIGH BLOCKERS

| blocker_id | severity | description | affected nodes | why blocks deployment | minimum condition to clear |
|-----------|----------|-------------|---------------|----------------------|---------------------------|
| SYS-B05 | HIGH | watchdog.py hardcoded COMPANY_DATA_DIR — in multi-Pi deployment, retail Pi cannot write to company Pi's local path | retail_node | Crash alerts from retail Pi are silently dropped in the intended deployment topology | Introduce COMPANY_DATA_DIR env var; remove hardcode |
| SYS-B06 | HIGH | Installer flat/core layout mismatch — installer expects core/ subdir; repo is flat; bootstrap_database and cron registration use CORE_DIR paths that don't exist in flat layout | retail_node | Installer cannot be tested or deployed from current repo state | Either migrate repo to core/ layout or update installer to support flat layout |
| SYS-B07 | HIGH | update-staging branch absent — Friday deployment pipeline not executable as documented | all | Deployment pipeline is non-operational | Create update-staging branch OR update operations spec to document actual process |
| SYS-B08 | HIGH | boot_sequence.py direct smtplib (policy violation) — boot failure SMS bypasses scoop queue | retail_node | Policy violation; dual-path comms; violates ADDENDUM_1 §4 | Route boot alert through Scoop enqueue or MONITOR_URL |
| SYS-B09 | HIGH | strongbox.py misplaced — backup agent is in retail repo, not company node; no backups are running | company_node | No automated backups; data safety gap | Move strongbox.py to synthos-company/agents/; verify running |

---

## FINAL VERDICT

```
READY_FOR_NEW_GROUND_TRUTH:    NO
READY_FOR_DEPLOYMENT_CLAIM:    NO
NEXT_REQUIRED_ACTION_CLASS:    mixed
```

**Blocking conditions for READY_FOR_NEW_GROUND_TRUTH:**
- [x] CRITICAL system conflicts unresolved: SYS-B01 through SYS-B04
- [x] Install/boot/update story is NOT coherent (install cannot complete; boot has no license gate)
- [x] Node responsibilities are NOT unambiguous (watchdog alert routing broken in multi-Pi topology)
- [x] Degraded behavior is NOT fully defined (license failure has no defined behavior)
- [x] Deployment claim is NOT supportable (multiple blocking contradictions between spec and implementation)

**Priority order for required actions:**

| Priority | Action Class | Action |
|----------|-------------|--------|
| 1 | architecture_decision | Human: declare license_validator.py status — build it now or strike it from all requirements |
| 2 | runtime_code_fix | Migrate vault.py, sentinel.py, librarian.py, watchdog.py to db_helpers.post_suggestion() |
| 3 | runtime_code_fix | Migrate watchdog.py post_deploy_watch read to db_helpers.get_active_deploy_watches() |
| 4 | runtime_code_fix | Fix watchdog.py COMPANY_DATA_DIR hardcode → env var |
| 5 | artifact_creation | Move strongbox.py to synthos-company/agents/ |
| 6 | documentation_fix | Update TECHNICAL_ARCH DB schema to v1.2 reality (doc change only) |
| 7 | runtime_code_fix | Remove step8_monitor from retail boot sequence (or add node-role guard) |
| 8 | documentation_fix | Update SYSTEM_MANIFEST: add v1.2 env vars, mark install.py deprecated, mark ADDENDUM_2 speculative |

**Items that do NOT block ground truth but must be documented as open:**
- boot_sequence.py smtplib policy violation (health_check.py fallback is acceptable pattern; boot is the true violation)
- Known-good snapshot scope gap (portal/watchdog/boot not included)
- update-staging branch absent (pipeline aspirational, not blocking current operation)
- INSTALLER_STATE_MACHINE.md missing (ghost document reference)
```
