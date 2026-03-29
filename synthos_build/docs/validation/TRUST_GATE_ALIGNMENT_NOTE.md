# TRUST GATE ALIGNMENT NOTE

**Date:** 2026-03-29
**Type:** Documentation correction record
**Scope:** Company integrity gate spec vs implementation reality

---

## 1. What Was Corrected

`docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` v1.0 described the company integrity gate as if it were fully implemented. A cross-check against the actual codebase found it was not. The following specific corrections were made in v1.1:

| Section | v1.0 (incorrect) | v1.1 (corrected) |
|---------|-----------------|-----------------|
| §3.2 Secrets | Listed ANTHROPIC_API_KEY, SENDGRID_API_KEY, MONITOR_TOKEN as enforced | Split into canonical target vs currently enforced vs alignment gap. Installer only enforces SENDGRID_API_KEY from that set. |
| §3.5 Config sanity | Implied MONITOR_URL and PI_ID were enforced | Explicitly noted these are not currently enforced by installer |
| §7.2 Boot sequence | Described a boot sequence as if it existed | Rewritten: no company boot sequence exists; boot-time gate is a future implementation |
| §11 (new) | Did not exist | Added enforcement summary table showing per-check current state |

Supporting documents updated for consistency:
- `STATUS.md` — added company integrity gate status note
- `PROJECT_STATUS.md` — added pre-release security hardening checklist under Phase 6
- `docs/validation-testing.md` — added company integrity gate scope note
- `docs/governance/SYNTHOS_OPERATIONS_SPEC.md` — added trust domain note under §5.1
- `docs/governance/SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md` — added trust domain split and enforcement status to §3.2
- `docs/architecture.md` — added Trust Domain Model section

---

## 2. What Remains Intentionally Deferred

The following are known gaps, accepted for the current phase:

| Gap | Deferral Reason |
|-----|----------------|
| Company boot-time integrity gate | No company boot sequence exists. Implementing one is a pre-release security task, not a normalization sprint task. |
| ANTHROPIC_API_KEY and MONITOR_TOKEN in installer required-key check | Minor installer alignment gap. Does not block current operations. |
| PRAGMA integrity_check in installer | Installer checks DB existence; full integrity check deferred to boot-time gate. |
| MONITOR_URL and PI_ID config enforcement at installer | Not blocking. Collected by installer but not asserted as required keys in company.env. |
| Full §6 security hardening items | All explicitly tagged `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE`. |

None of these gaps block system stabilization, normalization sprint completion, or deployment testing.

---

## 3. Why the Company Integrity Gate Architecture Remains Valid

The canonical model in `COMPANY_INTEGRITY_GATE_SPEC.md` was not weakened by this correction. The correction distinguished between:

- **what the architecture requires** (unchanged — the canonical model is still the target)
- **what is currently enforced** (partial — installer-level only)
- **what is deferred** (boot-time enforcement — explicitly tracked)

The architectural decisions remain sound:
- Company node uses an internal integrity gate, not retail-style license validation — **correct**
- The gate has zero dependency on `license_validator.py` — **correct and enforced**
- The installer does not collect LICENSE_KEY or call license_validator.py — **verified true**
- COMPANY_MODE=true is enforced at install time — **verified true**
- The trust domain split (company = authority, retail = validated) — **correct and consistent across all docs**

The gap is purely at the enforcement layer (no boot sequence), not at the architectural layer.

---

## 4. Where the Deferred Implementation Is Tracked

| Tracker | Location |
|---------|----------|
| Master project tracker | `PROJECT_STATUS.md` → Phase 6 → Pre-Release Security Hardening |
| Company node status | `synthos-company/STATUS.md` |
| Spec enforcement summary | `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` §11 |
| Validation scope | `docs/validation-testing.md` → Company Integrity Gate section |

The company boot-time integrity gate is a **release-gate security task**. It must be implemented and validated before any live trading or adversarial deployment. It does not block the normalization sprint, ground truth declaration, or deployment pipeline testing.
