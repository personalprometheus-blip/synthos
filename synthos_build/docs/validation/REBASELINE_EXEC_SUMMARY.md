# REBASELINE_EXEC_SUMMARY.md
## Synthos — Executive Reconciliation Summary
**Generated:** 2026-03-29
**Audience:** Project lead
**Method:** Direct repo inspection + document comparison. All claims are evidence-backed.

---

## 1. WHAT SYNTHOS CURRENTLY IS

A distributed algorithmic trading assistant running on three Raspberry Pi nodes:
- **retail_node** (Pi 2W): three trading agents (agent1/2/3), portal, local SQLite DB (signals.db). Currently in paper trading mode, supervised.
- **company_node** (Pi 4B): nine operational agents (patches, blueprint, sentinel, fidget, librarian, scoop, vault, timekeeper + strongbox misplaced in retail repo). Manages licensing, monitoring, backups, deployment.
- **monitor_node** (same Pi 4B, different process): synthos_monitor.py on port 5000. Receives heartbeats from all retail Pis.

The system is **functional** — agents are running, portal is up, validation passes at the structural level. However, it is in a state of **active architectural drift** between v1.1 (what the ground truth documents say) and v1.2 (what the code actually does).

---

## 2. WHERE THE ARCHITECTURE IS COHERENT

| Domain | Status |
|--------|--------|
| Retail agent logic (v1.2) | Coherent and aligned — agent1/2/3 implement consistent Option B, member weights, interrogation |
| Approval queue (pending_approvals) | Coherent — validate_03b passes 44/44; DB schema correct |
| Portal | Coherent — API endpoints match DB methods; validate_02 passes 22/22 |
| Company agent path resolution | Coherent — patches, sentinel, vault, librarian use synthos_paths or _HERE pattern |
| Blueprint safety contract | Coherent — contract exists; staging directory defined; no evidence it's been violated |
| Heartbeat architecture | Coherent — HEARTBEAT_RESOLUTION formally resolved the conflict; monitor_node is authoritative |
| Scoop (comms) | Coherent — only outbound email agent; no other agent imports smtplib/sendgrid |

---

## 3. WHERE IT IS DRIFTING

### Drift Type 1: Code ahead of documentation
v1.2 added member_weights, news_feed, interrogation, Option B, 5yr price history, pending_approvals. These features exist in running code but are absent from SYNTHOS_TECHNICAL_ARCHITECTURE.md (DB schema section), SYNTHOS_GROUND_TRUTH.md, and SYSTEM_MANIFEST.md ENV_SCHEMA.

### Drift Type 2: Partial migration (most dangerous)
The suggestion pipeline and post_deploy_watch pipeline are mid-migration from JSON files to company.db tables. Multiple agents still write to the JSON file directly. The DB implementation is newer. The two stores are NOT synchronized. Blueprint reads from the DB. Vault/Sentinel/Librarian/Watchdog write to JSON. This means critical alerts from these agents may never reach Blueprint.

### Drift Type 3: Misplaced files
strongbox.py lives in synthos_build/ (retail repo) but is a company_node agent. It is not in synthos-company/agents/. If it's not running on the company Pi, no backups are being made.

### Drift Type 4: Missing file with security implications
license_validator.py is cited as a required security gate at boot. It does not exist anywhere. Either boot_sequence.py handles this gracefully (license check is skipped), or boot fails silently at that step. This is unresolved.

### Drift Type 5: Operational process vs repo reality
The deployment pipeline assumes an `update-staging` git branch. Only `main` exists. The pipeline as written in OPERATIONS_SPEC v3.0 is not executable in the current repo state.

---

## 4. WHAT BLOCKS CLEAN VALIDATION

Listed by priority:

| Blocker | Why It Blocks | Conflict Ref |
|---------|--------------|-------------|
| suggestions.json vs DB split | Cannot validate suggestion pipeline integrity — two stores, neither complete | CL-002 |
| post_deploy_watch.json vs DB split | Cannot validate watchdog rollback trigger | CL-004 |
| DB schema not in architecture doc | Cannot validate architecture coherence (V-D01) | CL-003 |
| license_validator.py missing | Cannot confirm boot security model | CL-001 |
| watchdog.py hardcoded path | Path integrity check fails automatically | CL-005 |
| company DB schema undocumented | Cannot validate company node architecture integrity | CL-012 |

---

## 5. WHAT SHOULD BE NORMALIZED FIRST

In priority order — do these before anything else:

**Step 1 (code fix, no doc required): Migrate suggestions pipeline — 4 agents**
vault.py, sentinel.py, librarian.py, watchdog.py must be updated to write via `db_helpers.post_suggestion()` instead of directly to suggestions.json. This unblocks the most dangerous split (CL-002).

**Step 2 (code fix): Migrate watchdog.py post_deploy_watch to DB**
Replace JSON read with `db_helpers.get_active_deploy_watches()`. This unblocks watchdog rollback (CL-004) and is required before the next deployment.

**Step 3 (code fix): Fix watchdog.py hardcoded path (CL-005)**
Replace `Path("/home/pi/synthos-company/data")` with an env var `COMPANY_DATA_DIR`.

**Step 4 (file move): Move strongbox.py to synthos-company/agents/**
Verify it's not imported by any retail code. Remove from synthos_build/.

**Step 5 (doc update): Update TECHNICAL_ARCH DB schema to match reality**
Source: `database.py _create_tables()`. This is a doc change only — no code touched.

**Step 6 (human decision): Declare license_validator.py status**
Project lead confirms: (a) license validation is deferred to a later release and should be struck from current architecture, OR (b) it needs to be built immediately as it's a blocking security gap.

---

## 6. WHAT SHOULD BE TESTED IN CLAUDE CODE NEXT

In execution order:

```
1. Run Phase 1 validation checks from VALIDATION_MATRIX.md:
   V-A02, V-A03 (heartbeat refs)
   V-C02 (watchdog hardcoded path — will FAIL until Step 3 above is done)
   V-F01, V-F02 (suggestions/watch store — will FAIL until Steps 1-2 done)
   V-E03 (Scoop is sole email sender — should PASS)

2. After Steps 1-4 (code fixes):
   Re-run validate_02.py (should still pass 22/22)
   Re-run validate_03b.py (should still pass 44/44)
   Run V-F01 check (should now PASS)
   Run V-F02 check (should now PASS)

3. After Step 5 (doc update):
   Run V-D01 through V-D07 (schema checks)

4. After Step 6 (license decision):
   Update V-B01 check accordingly
   If license_validator is to be built — add to validate_03b.py FILE PRESENCE section
```

---

## 7. MINIMUM DOCUMENT UPDATE SET

These documents require updates before a new ground truth can be declared. No others need to change for the baseline to be valid.

| Document | Required Change | Priority |
|----------|----------------|----------|
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | Update §2.3 retail DB schema to v1.2 reality (add 10 tables, fix column lists); update §sentinel section to note port 5004 is Sentinel, not heartbeat | CRITICAL |
| SYNTHOS_GROUND_TRUTH.md | Update to v1.2: reflect DB schema, agent features, corrected file registry (strongbox, seed_backlog location), add CL open items | HIGH |
| SYSTEM_MANIFEST.md | Mark install.py as deprecated; add v1.2 env vars (COMPANY_SUBSCRIPTION, MIN_SIGNAL_THRESHOLD, ALPACA_DATA_URL); clarify ADDENDUM_2 as speculative | HIGH |
| SUGGESTIONS_JSON_SPEC.md | Add "SUPERSEDED" header; document migration to DB | HIGH |
| POST_DEPLOY_WATCH_SPEC.md | Add "SUPERSEDED" header; document migration to DB | HIGH |

These documents do NOT need to change for the baseline:
- BLUEPRINT_SAFETY_CONTRACT.md (stable)
- HEARTBEAT_RESOLUTION.md (historical record)
- OPERATIONS_SPEC v3.0 (largely accurate; update-staging branch gap is LOW until pipeline is activated)

---

## 8. IS THE PROJECT CONVERGING OR EXPANDING?

**Honest assessment: Expanding faster than it is converging.**

Evidence for expansion:
- Four new specification documents (EXECUTIONAGENT_SPECIFICATION_v2, RESEARCHAGENTS_SPECIFICATION_v2, AUDITINGAGENT_SPECIFICATION_v1, AGENT_ENHANCEMENT_PLAN) added 2026-03-29 — all for features not yet implemented
- SYNTHOS_ADDENDUM_2_WEB_ACCESS registered as active despite having zero implementation
- v1.2 features added without corresponding architecture doc updates

Evidence for convergence:
- validate_02 and validate_03b passing
- patches.py bugs fixed
- HEARTBEAT_RESOLUTION properly closed a multi-doc inconsistency
- MANIFEST_PATCH applied

**Net verdict:** The codebase is converging (validation passing, agents running, pipeline functional). The documentation is diverging (docs don't reflect v1.2, speculative features registered as active, two pipeline stores active simultaneously). The dangerous gap is not in the code — it is in the suggestion/deploy_watch split, which is a code problem that was started but not completed.

**The project is NOT ready for a new ground truth declaration.** It is ready for a focused 2-day normalization sprint (Steps 1-6 above) after which a clean ground truth can be written.

---

## CONFLICT COUNT BY SEVERITY

| Severity | Count |
|----------|-------|
| CRITICAL | 5 |
| HIGH | 7 |
| MEDIUM | 8 |
| LOW | 6 |
| **Total** | **26** |

## VALIDATION ITEMS BY AUTO-VERIFIABLE STATUS

| Status | Count |
|--------|-------|
| Claude Code can verify directly | 38 |
| Requires human confirmation | 6 |
| **Total** | **44** |
