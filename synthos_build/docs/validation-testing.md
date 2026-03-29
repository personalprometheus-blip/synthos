# VALIDATION TESTING

**Last Updated:** 2026-03-29
**Validation Program:** Three-step (Static → Behavioral → System)

---

## Step 1 — Static Validation
**Report:** docs/validation/STATIC_VALIDATION_REPORT.md
**Status:** FAIL (multiple critical conflicts)

| Area | Status |
|------|--------|
| File registry integrity | FAIL — 17 unclassified files |
| Naming consistency | FAIL — heartbeat file collision, alias fragmentation |
| Path integrity | FAIL — watchdog.py:64 hardcoded path (CRITICAL) |
| Required artifact presence | FAIL — license_validator.py missing |
| Schema integrity | FAIL — suggestions split, deploy_watch split |
| Node boundary integrity | FAIL — strongbox.py misplaced |
| Tool classification integrity | FAIL — 9 company agents not in TDA |
| Document consistency | FAIL — TECHNICAL_ARCH schema out of date |

---

## Step 2 — Behavioral Validation
**Reports:** tests/validate_02.py, tests/validate_03b.py
**Status:** PASS (within scope of these two validators)

| Validator | Scope | Result |
|-----------|-------|--------|
| validate_02.py | Portal surface (4 tabs, cross-surface coherence) | 22/22 PASS |
| validate_03b.py | Approval queue (schema, lifecycle, code paths) | 44/44 PASS |

---

## Step 3 — System Validation
**Report:** docs/validation/SYSTEM_VALIDATION_REPORT.md
**Status:** FAIL / NOT_DEPLOYABLE

| Phase | Status | Critical Findings |
|-------|--------|------------------|
| A — Install | FAIL | Installer cannot reach COMPLETE (license_validator.py missing in REQUIRED_CORE_FILES) |
| B — Boot | FAIL | No license gate at boot; health check always non-fatal; monitor leaks to retail node |
| C — Node Topology | CONDITIONAL | Retail self-contained; watchdog alert path broken in multi-Pi |
| D — License | FAIL | License validation entirely absent from runtime |
| E — Update/Rollback | FAIL | Post-deploy rollback trigger broken (CL-004) |
| F — Runtime | FAIL | Suggestions pipeline split; hidden deps on missing services |

**Scenarios tested:** 17
**PASS:** 4 | **FAIL:** 9 | **BLOCKED:** 4

---

## Critical Blockers (must resolve before re-running system validation)

| ID | Issue | Fix Required |
|----|-------|-------------|
| SYS-B01 | license_validator.py missing | Human decision + build or remove from requirements |
| SYS-B02 | No boot license gate | Implement after SYS-B01 resolved |
| SYS-B03 | Post-deploy rollback broken | Migrate watchdog.py to DB read |
| SYS-B04 | Suggestions pipeline split | Migrate 4 agents to db_helpers.post_suggestion() |
| SYS-B05 | watchdog.py hardcoded path | Introduce COMPANY_DATA_DIR env var |

---

## Validation Run Order (when re-running after normalization)

```
Phase 1 (run now, no code changes needed):
  V-A02, V-A03    — heartbeat refs clean
  V-B05           — all manifest-active files exist
  V-C02           — watchdog hardcoded path (will FAIL until SYS-B05 fixed)
  V-F01, V-F02    — suggestions/watch store consistency (will FAIL until SYS-B03/B04 fixed)
  V-E03           — Scoop is only email sender

Phase 2 (after normalization sprint complete):
  Full VALIDATION_MATRIX.md — all categories A through J

Phase 3 (human confirmation required):
  V-G02, V-G04    — deployment + PAPER mode confirmation
  SYS-B01 decision — license_validator status
```

Full validation matrix: docs/validation/VALIDATION_MATRIX.md

---

## Company Integrity Gate — Validation Scope Note

The company integrity gate architecture is defined in `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`.

**What is IN SCOPE for the current normalization sprint validation pass:**
- Confirming the company license-dependency split (company node does not call `license_validator.py`)
- Confirming installer does not collect `LICENSE_KEY` or `OPERATING_MODE`
- Confirming `COMPANY_MODE=true` is set and checked by installer

**What is OUT OF SCOPE for the current normalization sprint:**
- Full company integrity-gate enforcement implementation
- Boot-time gate evaluation (no company boot sequence exists yet)
- Alignment of installer secret checks with canonical gate spec (§3.2 gap)
- PRAGMA integrity_check enforcement
- `MONITOR_URL` / `PI_ID` config sanity enforcement

Full company integrity-gate enforcement must be validated as part of the **pre-release security gate** (Phase 6 / PROJECT_STATUS.md). Current validation only confirms the license-dependency split, not full enforcement implementation.

---

## Test File Locations

| File | Location | Purpose |
|------|----------|---------|
| validate_02.py | tests/ | Portal surface validation (22 checks) |
| validate_03b.py | tests/ | Approval queue validation (44 checks) |
| validate_env.py | tests/ | Environment variable presence check |
| synthos_test.py | tests/ | General system tests |
