# STATIC_VALIDATION_REPORT.md
## Synthos — Static Validation: Step 1
**Generated:** 2026-03-29
**Method:** Direct repo inspection, grep analysis, file registry cross-reference
**Scope:** synthos_build/ (87 files) + synthos-company/ (46 non-log files)

---

## OVERALL STATUS

```
STATIC_VALIDATION_STATUS: FAIL
```

Multiple CRITICAL blockers prevent progression to runtime validation.
See BLOCKERS section for remediation path.

---

## SUMMARY COUNTS

| Category | Count |
|----------|-------|
| Total files inspected (synthos_build) | 87 |
| Total files inspected (synthos-company) | 46 non-log |
| Files in SYSTEM_MANIFEST as "active" | ~30 |
| Unclassified / undocumented files found | 17 |
| Ghost file references (never built) | 3 |
| Missing required artifacts | 4 |
| Functional hardcoded paths | 2 |
| Schema integrity gaps | 2 (CRITICAL), 1 (HIGH) |
| Node boundary violations | 3 |
| Tool classification gaps | 8 company agents |
| Direct email policy violations | 3 |

---

## VALIDATION RESULTS

---

### 1. FILE REGISTRY INTEGRITY

**Status: FAIL**

Files present on disk but absent from SYSTEM_MANIFEST v4.0 FILE_STATUS:

| File | Location | Issue |
|------|----------|-------|
| VERSION_MANIFEST.txt | synthos_build/ | Undocumented — purpose unclear |
| __init__.py | synthos_build/ | Unusual at repo root; undocumented |
| STATIC_VALIDATION_REPORT.md | synthos_build/ | This file (just created) |
| reconciliation_index.json | synthos_build/ | Reconciliation artifact; not in manifest |
| REPO_REALITY.md | synthos_build/ | Reconciliation artifact; not in manifest |
| DOCUMENT_AUTHORITY_STACK.md | synthos_build/ | Reconciliation artifact; not in manifest |
| CONFLICT_LEDGER.md | synthos_build/ | Reconciliation artifact; not in manifest |
| FILE_NORMALIZATION_PLAN.md | synthos_build/ | Reconciliation artifact; not in manifest |
| VALIDATION_MATRIX.md | synthos_build/ | Reconciliation artifact; not in manifest |
| GROUND_TRUTH_READINESS.md | synthos_build/ | Reconciliation artifact; not in manifest |
| REBASELINE_EXEC_SUMMARY.md | synthos_build/ | Reconciliation artifact; not in manifest |
| validate_02.py | synthos_build/ | Validation script; not in manifest |
| validate_03b.py | synthos_build/ | Validation script; not in manifest |
| validate_env.py | synthos_build/ | Validation script; not in manifest |
| *.pdf / *.docx / *.html | synthos_build/ (if present) | Non-code artifacts; check manifest |
| agents/fidget.py | synthos-company/agents/ | Present; confirm manifest entry |
| interrogation_listener.py | synthos_build/ | Check manifest entry |

**Ghost files (referenced in docs, never built):**

| File | Referenced By | Status |
|------|--------------|--------|
| heartbeat_receiver.py | HEARTBEAT_RESOLUTION.md | Deprecated — never built |
| install.py | SYNTHOS_GROUND_TRUTH.md | Deprecated — canonical is install_retail.py |
| post_deploy_watch.json | migrate_agents.py | Superseded by company.db.deploy_watches |

**VERDICT:** Registry is incomplete. 17 unclassified files. SYSTEM_MANIFEST must be updated before a clean ground truth can be declared.

---

### 2. NAMING CONSISTENCY

**Status: FAIL**

**Heartbeat file name collision (CRITICAL):**

| File | Problem |
|------|---------|
| heartbeat.py | Named "heartbeat" — actually writes heartbeat rows to signals.db (DB writer) |
| synthos_heartbeat.py | Named "heartbeat" — actually POSTs to monitor_node via HTTP (monitor client) |

Both files have "heartbeat" in the name. Their functions are entirely distinct. validate_03b.py checks for `heartbeat.py` by name. FILE_NORMALIZATION_PLAN.md proposes renaming both (RENAME NOW disposition), but this requires updating validate_03b.py, all imports, and SYSTEM_MANIFEST.

**Agent informal alias fragmentation (HIGH):**

Three naming systems coexist with no declared canonical:
- File-based: agent1_trader.py, agent2_research.py, agent3_sentiment.py
- Informal: Bolt, Scout, Pulse
- Formal: ExecutionAgent, DisclosureResearchAgent, MarketSentimentAgent

Operational docs (SYSTEM_MANIFEST v4.0) use formal names. GROUND_TRUTH uses informal. Source headers use all three. This causes unreliable cross-doc searching.

**Document naming inconsistency (LOW):**

| File | Issue |
|------|-------|
| synthos_design_brief.md | Lowercase — inconsistent with ALL_CAPS_UNDERSCORES convention |

**ENV variable naming inconsistency (MEDIUM):**

| Variable | Status | Issue |
|----------|--------|-------|
| HEARTBEAT_URL | deprecated | Removed from architecture; still referenced in some docs |
| HEARTBEAT_TOKEN | deprecated | Same |
| HEARTBEAT_PORT | in company.env | This is actually SENTINEL port 5004, not a heartbeat receiver port |

**VERDICT:** Two RENAME NOW items outstanding. Heartbeat naming is active source of confusion. Alias fragmentation documented; decision to adopt Option A (file-based canonical) recommended but not yet formally adopted.

---

### 3. PATH INTEGRITY

**Status: FAIL**

**Functional hardcoded paths:**

| File | Line | Hardcoded Path | Severity | Conflict |
|------|------|---------------|----------|----------|
| watchdog.py | 64 | `COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")` | CRITICAL | CL-005 |
| first_run.sh | 10 | hardcoded /home/pi path | MEDIUM | T-10 |

The watchdog.py hardcode is functional — it determines where the company DB is located. This violates SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md §1 (no hardcoded /home/pi/ paths). It is auto-detected as a CRITICAL failure by V-C02. Fix: introduce `COMPANY_DATA_DIR` env var.

**Comments/docstrings with /home/pi (acceptable, excluded from count):**
- Multiple .py files contain /home/pi in comments, example paths, or docstrings — these are informational and not policy violations.

**Company agent path resolution (PASS):**
- patches.py, blueprint.py, sentinel.py, vault.py, librarian.py all use `_HERE = Path(__file__).resolve().parent` or import from synthos_paths.py
- No functional /home/pi hardcodes confirmed in these agents

**Retail agent path resolution (PASS):**
- agent1_trader.py, agent2_research.py, agent3_sentiment.py: no functional /home/pi hardcodes confirmed

**VERDICT:** 1 CRITICAL path violation (watchdog.py:64), 1 MEDIUM (first_run.sh:10). CL-005 must be resolved before ground truth.

---

### 4. REQUIRED ARTIFACT PRESENCE

**Status: FAIL**

| Artifact | Status | Severity | Conflict |
|----------|--------|----------|---------|
| license_validator.py | MISSING — not present anywhere in either repo | CRITICAL | CL-001 |
| config/priorities.json | MISSING — referenced in TECHNICAL_ARCH; not found | MEDIUM | CL-018 |
| allowed_outbound_ips.json | MISSING — referenced in SYSTEM_MANIFEST + TECHNICAL_ARCH | MEDIUM | CL-020 |
| update-staging git branch | MISSING — only `main` exists | HIGH | CL-011 |

**license_validator.py detail:**
- Referenced as a required security gate in: boot_sequence.py, SYNTHOS_TECHNICAL_ARCHITECTURE.md, SYNTHOS_OPERATIONS_SPEC.md
- `grep` of boot_sequence.py confirms it is NOT called anywhere in boot (no invocation)
- Either: (a) boot gracefully skips the missing step, or (b) boot silently fails at the license check step
- Status is UNKNOWN — requires human confirmation (CL-001)
- This is the only required artifact with security implications. Cannot declare a canonical security model while this is unresolved.

**VERDICT:** 1 CRITICAL missing artifact (license_validator.py, human decision required), 2 MEDIUM missing config files, 1 HIGH missing branch. Cannot declare ground truth while CL-001 is unresolved.

---

### 5. SCHEMA INTEGRITY

**Status: FAIL**

**Suggestions pipeline dual-store (CRITICAL — CL-002):**

Agents that write to `suggestions.json` directly (legacy path):
- vault.py — writes via SUGGESTIONS_FILE / json.dump
- sentinel.py — writes via SUGGESTIONS_FILE / json.dump
- librarian.py — writes via SUGGESTIONS_FILE / json.dump
- watchdog.py — writes via SUGGESTIONS_FILE / json.dump

Agents that write via `db_helpers.post_suggestion()` (canonical path):
- patches.py
- blueprint.py
- fidget.py

The two stores are **not synchronized**. blueprint.py reads suggestions exclusively from company.db.suggestions. Suggestions from vault, sentinel, librarian, and watchdog are written to the JSON file and **never reach blueprint**. Critical alerts from these four agents are silently dropped from the approval pipeline.

**Post-deploy watch dual-store (CRITICAL — CL-004):**

- blueprint.py writes deploy watches via `db_helpers.post_deploy_watch()` → company.db.deploy_watches
- watchdog.py reads post_deploy_watch.json via `POST_DEPLOY_FILE` env var (JSON path)

These stores are not synchronized. Watchdog rollback trigger is **silently broken** — it can never see watches posted by blueprint.

**Retail DB schema drift (CRITICAL — CL-003):**

SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3 documents 6 retail DB tables from v1.1. Actual signals.db (confirmed via PRAGMA table_info) has 17+ tables including v1.2 additions: member_weights, news_feed, interrogation log, pending_approvals, outcomes, ledger, system_log, scan_log, and others absent from the architecture doc. The architecture doc is wrong on day one of any new ground truth if not updated.

**Company DB schema (HIGH — CL-012):**

company.db has 14 tables. No architecture document currently captures this schema. V-D02 and V-A05 cannot pass until documented.

**VERDICT:** 2 CRITICAL pipeline splits (data is being lost NOW), 1 CRITICAL doc drift (architecture wrong), 1 HIGH undocumented schema. Steps 1 and 2 of normalization sprint are required before next deployment.

---

### 6. NODE BOUNDARY INTEGRITY

**Status: FAIL**

Files in the wrong repo/node:

| File | Current Location | Should Be | Severity | Conflict |
|------|-----------------|-----------|----------|---------|
| strongbox.py | synthos_build/ (retail) | synthos-company/agents/ | HIGH | CL-007 |
| seed_backlog.py | synthos_build/ (retail) | synthos-company/ | MEDIUM | CL-015 |
| synthos_monitor.py (copy) | synthos_build/ | Remove copy; canonical is synthos-company/synthos_monitor.py | MEDIUM | — |
| data/company.db | synthos_build/data/ | synthos-company/data/ only | MEDIUM | CL-014 |

**strongbox.py detail:**
strongbox.py is a company_node backup agent. It is not present in synthos-company/agents/. If it is not running on the company Pi, no backups are being made. This is a data safety gap, not just a structural concern.

**synthos_monitor.py duplication:**
A copy was added to synthos_build/ to satisfy validate_03b.py FILE PRESENCE check. This is technically correct for the validation but architecturally incorrect — the retail build should not contain the monitor server. The better fix is to update validate_03b.py to reflect that synthos_monitor.py belongs to the company/monitor node. Alternatively, the copy is acceptable if documented as intentional in SYSTEM_MANIFEST.

**VERDICT:** 4 node boundary violations. strongbox.py misplacement is a data safety concern (no backups). Must be resolved before ground truth.

---

### 7. TOOL CLASSIFICATION INTEGRITY

**Status: FAIL**

**TOOL_DEPENDENCY_ARCHITECTURE.md (TDA) gap (HIGH — CL-009):**

TDA classifies retail agents and some infrastructure. It does NOT classify the following company agents:
- patches.py
- blueprint.py
- sentinel.py
- fidget.py
- librarian.py
- scoop.py
- vault.py
- timekeeper.py
- strongbox.py

V-E01 requires all company agents be classified in TDA. This is currently a documentation gap, not a code problem.

**Direct email/communication policy violations (HIGH):**

BLUEPRINT_SAFETY_CONTRACT.md and SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md §4 designate scoop.py as the ONLY agent with outbound email/comms capability. The following violations were found:

| File | Line | Violation | Severity |
|------|------|-----------|----------|
| boot_sequence.py | 32, 125 | `import smtplib` + direct use | HIGH |
| health_check.py | 111-134 | Direct SendGrid send (fallback path) | HIGH |
| agent1_trader.py | 260-275 | Direct SendGrid fallback send | MEDIUM |

**boot_sequence.py** uses smtplib directly — not a fallback, a direct call. This is the most clear policy violation. Whether boot is considered inside the safety contract boundary is a judgment call, but ADDENDUM_1 §4 says "no agent other than scoop.py may send outbound email."

**health_check.py** has a fallback SendGrid path that fires when scoop.py is unavailable. Intent is defensive, but the policy does not include exceptions.

**agent1_trader.py** has a SendGrid fallback that fires when scoop fails. Same issue.

**Legacy artifact references (LOW):**

| Reference | File | Line | Issue |
|-----------|------|------|-------|
| .improvement_backlog.json | portal.py | 2112 | Legacy artifact reference |
| .pending_approvals.json | patch.py, sync.py | — | Listed as protected file; superseded by DB |

**VERDICT:** TDA is incomplete for all 9 company agents. 3 direct email policy violations (2 HIGH). Must be addressed — at minimum, decision on whether boot and health_check are exempt from §4 must be documented.

---

### 8. DOCUMENT CONSISTENCY

**Status: FAIL**

**Architecture doc vs reality drift (CRITICAL — CL-003):**
SYNTHOS_TECHNICAL_ARCHITECTURE.md DB schema section (§2.3) describes v1.1 schema (6 tables). Actual schema has 17+ tables. The following v1.2 tables are absent from the architecture doc: member_weights, news_feed, scan_log, interrogation (or equivalent), pending_approvals, outcomes, ledger, system_log, and others. This document must be updated before ground truth.

**SYSTEM_MANIFEST v4.0 gaps (HIGH):**
- install.py: may still appear as active (should be deprecated)
- license_validator.py: not declared as missing/deferred
- v1.2 env vars absent: COMPANY_SUBSCRIPTION, MIN_SIGNAL_THRESHOLD, ALPACA_DATA_URL
- ADDENDUM_2 (web access): not marked as speculative

**Suggestions/deploy_watch specs (HIGH):**
- SUGGESTIONS_JSON_SPEC.md: not marked as superseded
- POST_DEPLOY_WATCH_SPEC.md: not marked as superseded

Both documents describe the JSON-based stores as authoritative. This is incorrect — company.db is the intended canonical. Marking these superseded is required by V-A06 and V-A07.

**GROUND_TRUTH currency (HIGH):**
SYNTHOS_GROUND_TRUTH.md is at v1.1 baseline (2026-03-27). It does not reflect v1.2 features (member_weights, news_feed, interrogation, Option B, 5yr price history, pending_approvals). V-A09 requires either updating to v1.2 or explicitly marking as v1.1 baseline (not current). Currently, it reads as if current, which is misleading.

**Speculative features registered as active (MEDIUM):**
SYNTHOS_ADDENDUM_2_WEB_ACCESS.md: describes a full web access layer that has zero implementation in either repo. It should not appear as an active feature in any Tier 1-4 document.

**VERDICT:** Architecture doc is wrong for v1.2 schema. Two specs describe superseded implementations. Ground truth is stale. Minimum document update set is 5 documents (see REBASELINE_EXEC_SUMMARY.md §7).

---

## BLOCKERS

The following items prevent progression to runtime validation or ground truth declaration.

### CRITICAL BLOCKERS (must fix before next deployment)

| ID | Issue | Immediate Risk | Fix |
|----|-------|---------------|-----|
| CL-002 | Suggestions pipeline split — vault/sentinel/librarian/watchdog write to JSON; blueprint reads from DB only | Critical suggestions from 4 agents are silently dropped | Migrate all 4 agents to db_helpers.post_suggestion() |
| CL-004 | Post-deploy watch split — watchdog reads JSON; blueprint writes to DB | Watchdog rollback is silently broken | Migrate watchdog.py to db_helpers.get_active_deploy_watches() |
| CL-005 | watchdog.py:64 hardcodes COMPANY_DATA_DIR=/home/pi/synthos-company/data | Breaks on any non-default install path; policy violation | Introduce COMPANY_DATA_DIR env var |

### CRITICAL BLOCKERS (require human confirmation)

| ID | Issue | Required Action |
|----|-------|----------------|
| CL-001 | license_validator.py missing — boot security model unknown | Project lead must declare: (a) deferred to later release — strike from current arch, or (b) required — build immediately |
| CL-003 | Retail DB schema not in architecture doc | Update TECHNICAL_ARCH §2.3 with PRAGMA table_info output (doc change only, no code) |

### HIGH BLOCKERS (required before ground truth, not before deployment)

| ID | Issue | Fix |
|----|-------|-----|
| CL-007 | strongbox.py in wrong repo (no backups running) | Move to synthos-company/agents/; verify running |
| CL-009 | Company agents not classified in TDA | Update TOOL_DEPENDENCY_ARCHITECTURE.md |
| CL-012 | Company DB schema undocumented | Document company.db 14-table schema |
| — | boot_sequence.py direct smtplib use | Decision: exempt boot from §4, or route through scoop |
| — | health_check.py direct SendGrid fallback | Decision: exempt or refactor |
| — | SUGGESTIONS_JSON_SPEC.md not marked superseded | Add SUPERSEDED header |
| — | POST_DEPLOY_WATCH_SPEC.md not marked superseded | Add SUPERSEDED header |

---

## PASS ITEMS

The following validation areas are confirmed clean:

| Check | Result | Evidence |
|-------|--------|----------|
| Heartbeat architecture resolution | PASS | HEARTBEAT_RESOLUTION.md properly closed; no active HEARTBEAT_URL refs in code |
| Portal API surface (validate_02) | PASS | 22/22 checks passing |
| Approval queue (validate_03b) | PASS | 44/44 checks passing |
| Scoop sole email sender (excluding boot/health_check violations) | CONDITIONAL | scoop.py is the only agent with smtplib/sendgrid in primary path; violations are in infrastructure files |
| Company agent path resolution (patches/blueprint/sentinel/vault/librarian) | PASS | All use _HERE or synthos_paths.py |
| Retail agent path resolution | PASS | No functional /home/pi hardcodes |
| Blueprint safety contract staging dir | PASS | .blueprint_staging/ created by blueprint.py; no evidence of violation |
| Option B logic in agents | PASS | agent1/2/3 all implement consistent MIRROR/WATCH/WATCH_ONLY rules |
| Approval pipeline (pending_approvals) | PASS | Schema correct, lifecycle complete |
| Interrogation listener (UDP peer corroboration) | PASS | Present; VALIDATED/UNVALIDATED flags implemented |

---

## FINAL VERDICT

```
STATIC_VALIDATION_READY_FOR_NEXT_STEP: NO

Blocking conditions:
  [X] Unclassified files: 17 files absent from SYSTEM_MANIFEST
  [X] Critical pipeline split: suggestions JSON vs DB (data being lost NOW)
  [X] Critical pipeline split: post_deploy_watch JSON vs DB (rollback broken)
  [X] Critical path violation: watchdog.py:64 hardcoded /home/pi path
  [X] Missing security artifact: license_validator.py (human decision required)
  [X] Schema documentation gap: TECHNICAL_ARCH does not reflect v1.2 DB (17+ tables)
  [X] Node boundary violation: strongbox.py misplaced (no backups running)
  [X] Direct email policy violations: boot_sequence.py, health_check.py, agent1_trader.py

Minimum to unblock:
  Step 1: Migrate vault.py, sentinel.py, librarian.py, watchdog.py → db_helpers.post_suggestion()
  Step 2: Migrate watchdog.py → db_helpers.get_active_deploy_watches()
  Step 3: Fix watchdog.py COMPANY_DATA_DIR env var (remove hardcode)
  Step 4: Move strongbox.py to synthos-company/agents/
  Step 5: Update TECHNICAL_ARCH §2.3 DB schema to v1.2 reality
  Step 6: Human decision on license_validator.py status

See REBASELINE_EXEC_SUMMARY.md §5 for full normalization sprint plan.
See VALIDATION_MATRIX.md for per-item verification commands.
See CONFLICT_LEDGER.md for full conflict detail.
```
