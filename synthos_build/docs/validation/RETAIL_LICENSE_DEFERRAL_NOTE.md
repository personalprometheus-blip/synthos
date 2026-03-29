# RETAIL LICENSE DEFERRAL NOTE

**Date:** 2026-03-29
**Type:** Baseline scope decision record
**Decision:** Retail license validation formally deferred from current baseline
**Classification:** DEFERRED_FROM_CURRENT_BASELINE / FUTURE_RETAIL_ENTITLEMENT_WORK

---

## Decision Summary

Retail license validation (`license_validator.py`, boot-time entitlement gate, LICENSE_KEY verification) is formally deferred from the Synthos current-release baseline. This is a scoping decision, not a build decision.

- `license_validator.py` is not built and is not required for the current release
- Retail Pis operate without entitlement enforcement in the current baseline
- This is intentional and documented — it is not a defect in the current build
- The LICENSE_KEY is still collected during setup and stored in `.env` for future use
- Future implementation is tracked in `docs/milestones.md` (Retail Entitlement / License System)

---

## Files Changed

| File | Change |
|------|--------|
| `src/install_retail.py` | Removed `"license_validator.py"` from `REQUIRED_CORE_FILES`; added deferral comment. Removed `"LICENSE_KEY"` from installer verification `required_keys`; added deferral comment. |
| `CLAUDE.md` | Updated Critical Known Issue #1 from "MISSING — installer always fails" to DEFERRED_FROM_CURRENT_BASELINE status |
| `STATUS.md` | Step 6 marked complete (formally deferred). SYS-B01 and SYS-B02 struck and marked DEFERRED_FROM_CURRENT_BASELINE in blocker table. SYS-B05 also corrected to RESOLVED. |
| `PROJECT_STATUS.md` | Step 6 marked complete (formally deferred). Boot-time license gate item updated to FUTURE_RETAIL_ENTITLEMENT_WORK. SYS-B01 and SYS-B02 updated in cross-project blocker table. |
| `docs/architecture.md` | Trust Domain Model table: retail gate status updated from `Pending (SYS-B01)` to `DEFERRED_FROM_CURRENT_BASELINE` |
| `docs/milestones.md` | New section added: **Retail Entitlement / License System** with full future implementation task list |
| `docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md` | `license_validator.py` in file tree marked `DEFERRED_FROM_CURRENT_BASELINE`. `license_cache.json` marked deferred. Install flow step 5 updated. |
| `docs/specs/SYNTHOS_INSTALLER_ARCHITECTURE.md` | `license_validator.py` in file tree marked deferred. `license_cache.json` marked deferred. `LICENSE_KEY` env row updated. |
| `docs/specs/SYSTEM_MANIFEST.md` | `license_validator.py` in security group, FILE_STATUS, and REQUIRED_FILES sections marked deferred. `license_cache.json` marked deferred. Boot dependency diagram updated. |
| `docs/governance/SYNTHOS_OPERATIONS_SPEC.md` | §5.1 Vault section: added note that license validation post-trading behavior is future (DEFERRED_FROM_CURRENT_BASELINE) |
| `docs/governance/SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md` | §2 "LICENSE KEY" section: added deferral header block. Section now clearly marked as target design, not current implementation. |
| `docs/validation/BLOCKER_REFRESH_REPORT.md` | SYS-B01 and SYS-B02 updated to DEFERRED. Blocker table updated. CRITICAL_BLOCKERS_REMAIN changed to NO. |
| `docs/validation/STATIC_VALIDATION_REPORT.md` | Amendment note added at top listing all resolved/deferred blockers |
| `docs/validation/SYSTEM_VALIDATION_REPORT.md` | Amendment note added at top listing all resolved/deferred blockers |
| `docs/validation-testing.md` | System validation phase table updated. Blocker table updated with DEFERRED/RESOLVED status. Phase 3 note corrected. |

---

## What Requirement Language Was Removed

| Location | Removed claim |
|----------|---------------|
| `install_retail.py` REQUIRED_CORE_FILES | `"license_validator.py"` as a required present artifact |
| `install_retail.py` verify_installation() | `"LICENSE_KEY"` as a required env var for install verification |
| `STATUS.md` blockers | SYS-B01 and SYS-B02 as active CRITICAL blockers |
| `PROJECT_STATUS.md` blockers | SYS-B01 and SYS-B02 as active CRITICAL blockers |
| `BLOCKER_REFRESH_REPORT.md` | SYS-B01 and SYS-B02 as STILL_OPEN CRITICAL |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` | `license_validator.py` as an active present file "Key validation on every boot" |
| `SYSTEM_MANIFEST.md` | `license_validator.py` as a required security artifact; `license_cache.json` as a current runtime artifact |
| `SYSTEM_MANIFEST.md` boot dependency diagram | `license_validator.py` as SECURITY gate |
| `SYNTHOS_INSTALLER_ARCHITECTURE.md` | `license_validator.py` as required; LICENSE_KEY as "Format validated at install; Vault validates at boot" |

---

## What Was Reclassified as Deferred

All references to `license_validator.py` and the retail boot-time entitlement gate are now marked with one of:
- `DEFERRED_FROM_CURRENT_BASELINE` — not a current required artifact
- `FUTURE_RETAIL_ENTITLEMENT_WORK` — future implementation milestone

The LICENSE_KEY env var is still collected during setup. The key is in `.env`. This forward-compatibility is preserved. Deferral is of the *validation logic*, not of the *key collection*.

---

## What Future Tasks Were Added

`docs/milestones.md` — Retail Entitlement / License System section now tracks:
- Build `license_validator.py` with HMAC validation, Pi ID binding, online registry check
- Wire boot-time entitlement gate into boot_sequence.py
- Define behavior for invalid, revoked, and offline keys
- Cache model and grace period implementation
- Add LICENSE_KEY back to installer verification once validator is wired
- End-to-end validation of retail entitlement flow
- Post-trading hardening (Vault stricter mode, key rotation, rate limiting)

---

## What Was Intentionally NOT Implemented

- `license_validator.py` — not built. This is a deferral, not a build step.
- Boot-time license gate in `boot_sequence.py` — not wired. Deferral acknowledged.
- Any validation of the LICENSE_KEY value in `.env` — key is stored, not validated.
- Any changes to Vault's key management logic — Vault still manages key records; enforcement deferred.
- Any changes to the company integrity gate model — unaffected by this deferral.

---

## Trust Domain Consistency

This deferral affects only the retail/customer trust domain. The company/internal trust domain is unchanged:
- Company node uses the internal integrity gate model (`COMPANY_INTEGRITY_GATE_SPEC.md`) — unchanged
- Company boot has zero dependency on `license_validator.py` — unchanged and verified
- The trust domain split (company = authority, retail = validated) remains the canonical architecture — the current baseline simply operates with the retail validation side not yet implemented
