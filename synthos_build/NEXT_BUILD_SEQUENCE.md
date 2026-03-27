# NEXT BUILD SEQUENCE
## Ordered Implementation Plan — Post Architectural Stabilization Phase

**Document Version:** 1.0
**Date:** 2026-03-27
**Status:** Active
**Follows:** Architectural stabilization and pipeline unblocking phase
**Audience:** Blueprint, Patches, Project Lead

---

## Phase Completion Criteria

This phase (architectural stabilization) is complete when ALL of the following are true:

1. SUGGESTIONS_JSON_SPEC.md — written and present in `synthos_build/`
2. POST_DEPLOY_WATCH_SPEC.md — written and present in `synthos_build/`
3. MANIFEST_PATCH.md — written and present in `synthos_build/`
4. BLUEPRINT_SAFETY_CONTRACT.md — written and present in `synthos_build/`
5. HEARTBEAT_RESOLUTION.md — written and present in `synthos_build/`
6. NEXT_BUILD_SEQUENCE.md — written and present in `synthos_build/`
7. MANIFEST_PATCH.md has been applied to SYSTEM_MANIFEST.md
8. HEARTBEAT_RESOLUTION.md corrections have been applied to SYNTHOS_TECHNICAL_ARCHITECTURE.md
9. All six documents are internally consistent (no contradictions with SYSTEM_MANIFEST or SYNTHOS_GROUND_TRUTH)

Items 1–6 are complete as of 2026-03-27. Items 7–9 are the immediate next steps.

---

## STEP 1 — Apply MANIFEST_PATCH.md to SYSTEM_MANIFEST.md

**Status:** Ready to execute immediately
**Owner:** Project Lead (manual paste-in) or Blueprint (if a suggestion is queued)
**Blocked by:** Nothing

**Actions:**
1. Open SYSTEM_MANIFEST.md
2. Apply each of the 7 patches defined in MANIFEST_PATCH.md in order (PATCH 1 through PATCH 7)
3. Each patch specifies the exact insertion point — follow the instructions verbatim
4. Save SYSTEM_MANIFEST.md

**Validation:** After applying, confirm:
- `suggestions.json` and `post_deploy_watch.json` appear in `## 2. SYSTEM_PATHS`
- `heartbeat_receiver.py` is marked `deprecated` in FILE_STATUS
- `HEARTBEAT_RESOLUTION.md`, `NEXT_BUILD_SEQUENCE.md`, and all new spec docs are in the documentation registry
- `monitor_node:` ports block includes the `note_on_company_node_port_5004` deprecation note

---

## STEP 2 — Apply Heartbeat Corrections to SYNTHOS_TECHNICAL_ARCHITECTURE.md

**Status:** Ready to execute immediately (parallel with Step 1)
**Owner:** Project Lead (manual edit) or Blueprint (if a suggestion is queued)
**Blocked by:** Nothing

**Actions:** Apply each correction defined in HEARTBEAT_RESOLUTION.md §5, Document 1:

1. Remove `• Heartbeat Receiver (5004)` from the company Pi architecture diagram
2. Update §2.7 — replace `HEARTBEAT_URL` with `MONITOR_URL`, update the POST target from "Company Pi" to "monitor_node"
3. In the company Pi file tree (§3.2): replace `heartbeat_receiver.py # Flask app (port 5004)` with the deprecation comment
4. Remove or replace the "Service: Heartbeat Receiver (port 5004)" service block
5. In the retail Pi `.env` example: replace `HEARTBEAT_URL` / `HEARTBEAT_TOKEN` with `MONITOR_URL` / `MONITOR_TOKEN`
6. In any system `.env` example: remove `HEARTBEAT_PORT=5004`

**Also apply:** SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md correction:
- Find: "the Company Pi's heartbeat receiver and validation endpoints"
- Replace: "the monitor_node's heartbeat receiver (synthos_monitor.py, port 5000) and company Pi validation endpoints"

**Validation:** After applying, grep SYNTHOS_TECHNICAL_ARCHITECTURE.md for `HEARTBEAT_URL` and `port 5004` — both should return zero results.

---

## STEP 3 — Validate Cross-Document Consistency

**Status:** Blocked until Steps 1 and 2 are complete
**Owner:** Patches (automated review) or Project Lead
**Blocked by:** Steps 1 and 2

**Actions:**
1. Confirm SYSTEM_MANIFEST.md, SYNTHOS_GROUND_TRUTH.md, and SYNTHOS_TECHNICAL_ARCHITECTURE.md agree on:
   - Heartbeat architecture (monitor_node, port 5000, MONITOR_URL)
   - File registry entries for suggestions.json and post_deploy_watch.json
   - Blueprint staging directory path (`.blueprint_staging/`)
2. Confirm no document still references `heartbeat_receiver.py` as an active (non-deprecated) file
3. Confirm SUGGESTIONS_JSON_SPEC.md and POST_DEPLOY_WATCH_SPEC.md are consistent with MANIFEST_PATCH.md schema descriptions

**This step has no code deliverables — it is a review gate only.**

---

## STEP 4 — Initialize Runtime State Artifacts

**Status:** Blocked until Step 3 is complete
**Owner:** Blueprint (via approved suggestions) or Project Lead (manual)
**Blocked by:** Step 3

**Actions:**
1. Create `${SYNTHOS_HOME}/data/suggestions.json` as an empty array: `[]`
   - This is the initial state; it will be populated by `seed_backlog.py` on company install
   - Schema: SUGGESTIONS_JSON_SPEC.md
2. Confirm `${SYNTHOS_HOME}/data/` exists on the company Pi; create if missing
3. `post_deploy_watch.json` does NOT need to be pre-created — Blueprint creates it at first deployment

**Note:** Do not pre-populate `suggestions.json` with test data or placeholder entries. The seed file (`seed_backlog.py`) is responsible for initial population on install.

---

## STEP 5 — Wire seed_backlog.py into Company Installer (TODO T-08)

**Status:** Blocked until Step 4 is complete
**Owner:** Blueprint (engineer.py) via approved suggestion
**Blocked by:** Step 4

**What this means:**
`install_company.py` must run `seed_backlog.py` automatically as a post-install step, so that `suggestions.json` is populated with the initial backlog on first company Pi install. Currently, the operator must run it manually.

**Exact change:** In `install_company.py`, add a step after the data directory creation:
```
Step N: Run seed_backlog.py to initialize suggestions.json
        Command: python3 ${SYNTHOS_HOME}/seed_backlog.py
        On failure: log warning, do not abort install (operator can run manually)
```

**This requires a suggestion in suggestions.json before Blueprint can implement it.** Patches should queue this as a suggestion on the company Pi's next run cycle.

---

## STEP 6 — Build Shared Utilities

**Status:** Blocked until Step 3 is complete (parallel with Step 5)
**Owner:** Blueprint (engineer.py) via approved suggestions
**Blocked by:** Step 3

The following shared utility modules are defined in MANIFEST_PATCH.md but do not yet exist as files. They are prerequisites for company agent implementations.

| File | Purpose | Depends on |
|---|---|---|
| `utils/scheduler_core.py` | Request/Grant logic for Timekeeper | Nothing |
| `utils/db_guardian.py` | Lock management and conflict detection for company.db | Nothing |
| `utils/api_client.py` | Anthropic, GitHub, SendGrid API client | Nothing |
| `utils/logging.py` | Structured logging factory | Nothing |

**Build order within this step:** All four are independent; they may be implemented in parallel suggestions or in any order.

**Each utility requires:** An approved suggestion in `suggestions.json` with `target_files` pointing to the utility file, following the BLUEPRINT_SAFETY_CONTRACT.md deployment sequence.

---

## STEP 7 — Activate IP Allowlisting (TODO T-15 / T-16)

**Status:** Deferred — not a current blocker
**Owner:** Project Lead decision + Sentinel implementation
**Blocked by:** Step 3 (minimum), plus stable IP inventory

**Condition to unblock:** Operator confirms all expected IP addresses from which SSH access will occur. Once the IP list is stable, Sentinel can activate enforcement from `config/allowed_ips.json`.

**Do not activate prematurely.** Activating with an incomplete IP list will lock out the operator.

---

## WHAT MUST NOT BE TOUCHED

The following items are explicitly out of scope until further notice:

1. **Production code on live retail Pis** — No changes to core/ agents on any deployed Pi until the company Pi pipeline (suggestions.json → Blueprint) is operational end-to-end.

2. **`agent1_trader.py` (live trading logic)** — T-01, T-02, T-03 are resolved. Do not re-open. T-04 (Gmail SMTP) remains deferred until credentials are configured — do not touch the trader to enable it.

3. **`company.db` schema** — The company database schema is defined in SYNTHOS_TECHNICAL_ARCHITECTURE.md. Do not alter the schema outside of a formal migration process.

4. **PAPER_TRADE flag** — This flag must NOT be flipped to live trading autonomously. The project lead makes this call manually and only when confidence is established. No agent may change this.

5. **install_retail.py / install_company.py** — Do not modify the installers until utility modules (Step 6) are built and validated. Exception: Step 5 (seed_backlog.py wiring) is explicitly approved.

6. **Cloudflare tunnel configuration** — T-09 (named tunnel) is LOW priority. Do not migrate the tunnel during active development.

7. **SYNTHOS_GROUND_TRUTH.md** — This is the canonical source of truth. Do not modify it without explicit Project Lead instruction. If it conflicts with another document, the other document is wrong.

---

## IMMEDIATE START CHECKLIST

These items can begin right now without waiting for anything:

```
[ ] Apply MANIFEST_PATCH.md → SYSTEM_MANIFEST.md        (Step 1)
[ ] Apply HEARTBEAT_RESOLUTION.md → SYNTHOS_TECHNICAL_ARCHITECTURE.md  (Step 2)
[ ] Apply heartbeat correction → SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md (Step 2)
```

These items are blocked until the above are complete and validated:

```
[ ] Cross-document consistency review          (Step 3)
[ ] Initialize suggestions.json as empty array (Step 4)
[ ] Queue suggestion for seed_backlog.py wiring (Step 5)
[ ] Queue suggestions for utility modules       (Step 6)
```

---

## EXACT CONDITION FOR PHASE COMPLETION

This architectural stabilization phase is **complete** when:

1. All six specification documents exist in `synthos_build/`
2. SYSTEM_MANIFEST.md has been patched (all 7 patches from MANIFEST_PATCH.md applied)
3. SYNTHOS_TECHNICAL_ARCHITECTURE.md has zero references to `HEARTBEAT_URL` or `heartbeat_receiver.py` as active (non-deprecated) artifacts
4. A cross-document consistency check finds no contradictions between SYSTEM_MANIFEST.md, SYNTHOS_GROUND_TRUTH.md, SYNTHOS_TECHNICAL_ARCHITECTURE.md, and the new spec documents
5. `suggestions.json` exists on the company Pi and is initialized (empty array or seeded)

When all five conditions are met, the pipeline is unblocked and Blueprint can begin operating on the suggestions backlog.

---

**Document Version:** 1.0
**Status:** Active — governs current phase
**Next review:** After Step 3 (cross-document consistency gate)
