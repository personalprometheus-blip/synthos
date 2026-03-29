# COMPANY INTEGRITY GATE — SPECIFICATION

**Version:** 1.1
**Date:** 2026-03-29
**Status:** AUTHORITATIVE — applies to company_node and monitor_node
**Authority tier:** Tier 2 — Behavioral/Operational Authority

---

## IMPLEMENTATION STATUS OVERVIEW

This document has three layers. Read this summary before using any section as implementation guidance.

| Layer | Status |
|-------|--------|
| **A — Canonical model** | DEFINED — the architectural decision is made and authoritative |
| **B — Current enforcement** | PARTIAL — installer enforces a subset; no boot-time gate exists |
| **C — Deferred enforcement** | PENDING — full boot-time gate deferred to pre-release security hardening |

**Current state in one sentence:** The canonical model is defined and correct. The company installer enforces a partial subset of it. No company boot sequence exists yet to evaluate the full gate before runtime services start. This gap is known, accepted for the current phase, and tracked for pre-release security gating.

---

## 1. PURPOSE

This document defines the internal startup gate for company and monitor nodes.

The company integrity gate:
- Establishes a minimal, enforceable set of preconditions that must pass before runtime services start on company/internal nodes
- Replaces any prior requirement for company-side license validation
- Enables cold start, operator restore, and offline operation without dependency on external validation systems

This is **NOT**:
- A license validation system
- A security hardening spec
- A retail entitlement check
- A substitute for a future production security model

---

## 2. TRUST DOMAIN DEFINITION

### 2.1 Domains

| Domain | Classification | Who Lives Here |
|--------|--------------|---------------|
| company/internal | **Authority domain** | company_node, monitor_node, operator systems |
| retail/customer | **Validated domain** | retail_node (Pi 2W), customer Pis |

### 2.2 Explicit Rules

**R-1.** The company/internal domain is the authority domain. It issues licenses to and validates retail nodes. It is never validated by retail nodes.

**R-2.** Validation direction is one-way: company validates retail. Retail never validates company.

**R-3.** The company node must be startable without Vault being already operational. Vault is a company agent — it cannot be a prerequisite for the system that starts it.

**R-4.** The company node must be startable without any retail node being present or reachable.

**R-5.** The company node must be startable offline (no outbound network required).

**R-6.** No company-side boot or install step may depend on `license_validator.py` or any retail license system.

---

## 3. MINIMUM INTEGRITY GATE (CANONICAL MODEL)

The following checks define the integrity gate target. This is the canonical architectural model — the full set of checks that must pass before runtime services start when the boot-time gate is implemented.

**Current enforcement status is documented per-check below.** See §11 for the consolidated enforcement summary.

---

### 3.1 MODE CHECK

**Purpose:** Confirm this node is running as an internal/company node, not as a misconfigured retail node.

**Check:** `COMPANY_MODE` environment variable is present in `company.env` and equals `true` (case-insensitive).

**Fail condition:** `COMPANY_MODE` is absent, empty, or any value other than `true`.

**Rationale:** This is the canonical flag that distinguishes company-side behavior throughout the codebase. Its absence indicates either a misconfigured environment or a retail `.env` accidentally loaded in place of `company.env`.

**Current enforcement:** ENFORCED by installer (`install_company.py`) — presence and value both checked.

---

### 3.2 SECRET PRESENCE CHECK

**Purpose:** Confirm that required internal secrets are present and non-empty before any agent attempts to use them.

#### A. Canonical trust-anchor set (target)

The following keys must exist in `company.env` and be non-empty strings when full gate enforcement is implemented:

| Key | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Required by blueprint.py and other AI-assisted agents |
| `SENDGRID_API_KEY` | Required by scoop.py for alert delivery |
| `MONITOR_TOKEN` | Required for authenticated heartbeat reception |

#### B. Currently enforced by installer

`install_company.py` currently verifies the following keys as present in `company.env`:

| Key | Enforced |
|-----|---------|
| `COMPANY_MODE` | ✅ (also verifies value = `true`) |
| `SENDGRID_API_KEY` | ✅ |
| `SENDGRID_FROM` | ✅ |
| `OPERATOR_EMAIL` | ✅ |
| `KEY_SIGNING_SECRET` | ✅ |
| `DATABASE_PATH` | ✅ |

#### C. Alignment gap

- `ANTHROPIC_API_KEY` and `MONITOR_TOKEN` are in the canonical target but **not currently in the installer's required-key check**
- `SENDGRID_FROM`, `OPERATOR_EMAIL`, `KEY_SIGNING_SECRET`, `DATABASE_PATH` are enforced by the installer but not yet formally in the canonical spec
- Full alignment between the canonical set and installer checks is deferred to pre-release security hardening

**Note on presence-only checking:** This check confirms **presence only**. Secret format validation and revocation checking are **DEFERRED** (see §6). A syntactically present but semantically invalid key passes this check.

---

### 3.3 CRITICAL FILE CHECK

**Purpose:** Confirm the minimum set of files required for company node operation are present before attempting to start any service.

**Canonical required files** (relative to `SYNTHOS_HOME`):

| File | Required By |
|------|------------|
| `agents/blueprint.py` | Core deployment agent |
| `agents/sentinel.py` | Heartbeat monitor (port 5004) |
| `agents/scoop.py` | Alert delivery |
| `agents/patches.py` | Continuous audit |
| `utils/db_helpers.py` | Shared DB utility — required by all agents |
| `data/company.db` | Primary data store (may be empty schema, must exist) |
| `company.env` | Environment config |

**Fail condition:** Any listed file or directory is absent.

**Note:** `license_validator.py` is explicitly **NOT** in this list. It must never be added to company node required files.

**Current enforcement:** ENFORCED by installer. The installer checks `blueprint.py`, `sentinel.py`, `scoop.py`, `patches.py`, `fidget.py`, `librarian.py`, `vault.py`, `timekeeper.py` (superset of canonical minimum) and `utils/db_helpers.py`. Consistent with this spec.

---

### 3.4 DATABASE INTEGRITY CHECK

**Purpose:** Confirm the company database is structurally sound before any agent attempts to read or write.

**Check:** `company.db` must:
1. Exist at `data/company.db` relative to `SYNTHOS_HOME`
2. Pass SQLite `PRAGMA integrity_check` (returns `"ok"`)

**Cold-start exception:** If `company.db` does not exist AND the node is in a first-run state (`.install_complete` sentinel is absent), the gate passes this check and defers DB creation to the installer. This is the only condition under which an absent DB does not fail the gate.

**Fail condition:** DB exists but `PRAGMA integrity_check` returns any value other than `"ok"`.

**Current enforcement:** Installer checks DB existence. PRAGMA integrity_check is **not currently run by the installer**. Full DB integrity check is deferred to boot-time gate implementation.

---

### 3.5 CONFIGURATION SANITY CHECK

**Purpose:** Confirm that required configuration values are present and structurally valid (not semantically validated).

#### A. Intended config sanity scope (target)

| Config Value | Rule |
|-------------|------|
| `COMMAND_PORT` | Present and parseable as integer |
| `INSTALLER_PORT` | Present and parseable as integer |
| `MONITOR_URL` | Present and non-empty string |
| `PI_ID` | Present and non-empty string |

**Note:** URL reachability and port availability are **NOT** checked here. This check confirms configuration is structurally present, not operationally valid.

#### B. Currently enforced by installer

The installer collects `command_port` and `installer_port` as part of setup. It does **not** currently enforce `MONITOR_URL` or `PI_ID` as required present values in `company.env`.

#### C. Enforcement gap

- `MONITOR_URL` and `PI_ID` presence verification is **not currently enforced** by the installer
- Full config sanity check against the canonical set is deferred to boot-time gate implementation

---

### 3.6 DEPENDENCY RULE

The company integrity gate has **zero dependency** on:

- `license_validator.py`
- Any retail license system or entitlement service
- Any external validation API
- Any retail node being reachable
- Vault being pre-operational (Vault is a company agent, not a prerequisite)
- Internet connectivity

Violation of this rule invalidates the gate design.

---

## 4. FAIL BEHAVIOR

### 4.1 Gate Failure Response

If any check in §3 fails, the system **must not start runtime services**. This is a hard stop.

"Runtime services" means: starting agents (blueprint, sentinel, scoop, etc.), initializing scheduled tasks, or accepting inbound connections.

### 4.2 Failure Must Be

**Explicit:** The failing check must be named in the log output. "Integrity gate failed" without a specific check name is not acceptable.

**Logged:** Failure is written to `logs/integrity_gate.log` with:
- Timestamp
- Check name that failed
- Specific failure reason (e.g., "COMPANY_MODE not set", "company.db integrity check returned: malformed")

**Local:** No outbound network call is made on failure. No alert is sent to Scoop (Scoop may not be startable). The failure record exists on disk only.

### 4.3 Partial Startup

Partial startup — where some services start despite a gate failure — is **disallowed**. The gate is all-or-nothing. No agent starts until all checks pass.

### 4.4 Failure Is Not Fatal to the Pi

Gate failure halts the Synthos runtime startup. It does not crash the Pi or prevent the operator from SSHing in to diagnose. The failure is written to log and the process exits with a non-zero code.

**Implementation note:** §4.1–4.4 describe the target behavior. This behavior cannot be enforced until a company boot sequence is implemented. See §7.2 and §11.

---

## 5. BREAK-GLASS / RECOVERY MODE

### 5.1 Trigger Condition

Break-glass mode is operator-initiated only. It is not triggered automatically by any system condition.

Legitimate uses:
- First-time install (installer running, no `company.db` yet)
- Restore from Strongbox backup (DB being rebuilt)
- Operator debugging after a configuration error

### 5.2 Activation

Break-glass is activated by running `install_company.py` directly (or `install_company.py --repair`). This is the only sanctioned bypass of the integrity gate.

Running the installer is the recovery path. There is no separate "override flag" or environment variable that disables the gate outside of the installer context.

### 5.3 Constraints

- Local-only: no network connectivity required
- All break-glass activity is logged to `logs/install.log`
- Break-glass mode ends when the installer exits — it does not persist
- Break-glass is not available via any API, portal endpoint, or agent action

### 5.4 Scope Limit

Break-glass exists to repair and bootstrap. It does not become a normal runtime path. Any operator action that would require bypassing the gate during normal operation indicates a gate misconfiguration that should be fixed, not bypassed.

---

## 6. EXPLICIT NON-GOALS (DEFERRED)

The following items are **not part of this specification** and are deferred to a future pre-release security phase.

| Item | Deferral Status |
|------|----------------|
| Cryptographic identity enforcement | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Remote attestation | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Signed artifact enforcement | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Secret format validation (beyond presence) | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Secret revocation checking | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Anti-spoofing guarantees for COMPANY_MODE flag | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Intrusion detection | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Mutual TLS between nodes | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |
| Full security hardening | `DEFERRED_TO_PRE_RELEASE_SECURITY_PHASE` |

These items do not block system stabilization, validation, or deployment testing. They must be addressed before any adversarial or production-grade deployment.

---

## 7. RELATION TO INSTALLER + BOOT

### 7.1 Installer

`install_company.py` must:
- Collect and write only the secrets listed in §3.2
- Set `COMPANY_MODE=true` in `company.env`
- Bootstrap `company.db` schema via `db_helpers.py`
- Write `.install_complete` sentinel on success
- Never collect or validate `LICENSE_KEY`, `OPERATING_MODE`, or `AUTONOMOUS_UNLOCK_KEY`
- Never reference or call `license_validator.py`

**Current status:** installer does NOT collect LICENSE_KEY or call license_validator.py ✅. Full alignment with §3.2 canonical secret set is pending (see §3.2-C).

### 7.2 Boot Sequence

**A company boot sequence that evaluates the integrity gate does not currently exist.**

This is a known implementation gap. Current enforcement is installer/setup-level only. The full boot-time integrity gate — which must evaluate all §3 checks before starting any agent — is a required future implementation deferred to pre-release security hardening.

**Target behavior (when implemented):**
```
company boot starts
  → evaluate integrity gate (§3.1 through §3.5)
  → if any check fails → log failure, exit (§4)
  → if all checks pass → start runtime services
```

**Current behavior:**
```
company agents start manually or via cron
  → no pre-start integrity gate evaluation
  → installer enforced a partial gate check at setup time only
```

This gap means the fail behavior in §4 is currently aspirational. It becomes enforceable when the boot sequence is implemented.

**Tracking:** Implementing the company boot-time integrity gate is a release-gate security task. See docs/validation/TRUST_GATE_ALIGNMENT_NOTE.md and PROJECT_STATUS.md Phase 6 for tracking.

### 7.3 Install → Boot Handoff

```
install_company.py (setup-time):
  → partial gate checks (§3.1 MODE, §3.2 partial secrets, §3.3 files, §3.4 DB existence)
  → writes .install_complete sentinel

company boot (runtime, NOT YET IMPLEMENTED):
  → full gate evaluation (§3.1 through §3.5)
  → starts runtime services only on full pass
```

---

## 8. RELATION TO RETAIL VALIDATION

| Property | retail_node | company_node |
|----------|------------|-------------|
| Boot gate | License validation (license_validator.py) | Integrity gate (this spec) |
| Gate checks | License key presence + Vault validation | COMPANY_MODE + secrets + files + DB + config |
| External dependency | Vault API (online), cached license (offline) | None |
| Fail behavior | Defined by license validation flow | Defined by §4 of this spec |
| Shared enforcement path | None | None |
| Boot-time gate implemented | Pending (license_validator.py missing) | Pending (no company boot sequence) |

`license_validator.py` is scoped to `retail_node` only. It must not be imported, called, or required by any company-side code path unless a future explicit architectural decision creates a shared or split model. That decision is not made here.

---

## 9. CURRENT LIMITATIONS

This minimal model has known limitations that are acceptable for the current phase:

**L-1.** The gate relies on environment variables and local file presence. A malicious actor with local filesystem access could satisfy all checks without a legitimate deployment.

**L-2.** `COMPANY_MODE=true` is a plain environment variable. It does not cryptographically prove node identity.

**L-3.** Secret presence (§3.2) does not verify secrets are valid. An agent may start and fail at runtime due to a bad API key that passed the presence check.

**L-4.** The gate does not detect if company agents have been tampered with. File presence (§3.3) is not file integrity.

**L-5.** The gate assumes a controlled deployment environment where the operator is trusted and local access is controlled.

**L-6.** No boot-time gate currently exists. Current enforcement is install-time only.

These limitations are accepted. Remediating L-1 through L-5 is the responsibility of the pre-release security phase (§6). L-6 is tracked as a required implementation task (§7.2).

---

## 10. READINESS STATEMENT

### This integrity gate architecture IS sufficient for:

- System stabilization (clears company-side license dependency)
- Validation consistency (company node boot path is defined and inspectable)
- Deployment testing (partial gate checks confirm minimum operational readiness at install time)
- Blocker remediation (SYS-B01 and SYS-B02 resolution — removes licensing from company boot path)

### This integrity gate architecture is NOT sufficient for:

- Final production-grade security
- Adversarial environments
- Multi-tenant or hosted deployments
- Any context where local filesystem access is not controlled by the operator

### Conflict resolution

This spec supersedes any prior requirement for `license_validator.py` on company/internal nodes. Any document that references license validation as a company-side boot requirement (including prior versions of SYNTHOS_TECHNICAL_ARCHITECTURE.md or SYSTEM_MANIFEST.md) should be updated to reflect this separation.

The retail license model remains unchanged and is out of scope for this document.

---

## 11. ENFORCEMENT SUMMARY (CURRENT STATE)

| Gate Check | Canonical Target | Installer (setup-time) | Boot-time |
|-----------|-----------------|----------------------|-----------|
| §3.1 MODE CHECK | COMPANY_MODE=true | ✅ Enforced | ⏳ Not yet — no boot sequence |
| §3.2 SECRETS — SENDGRID_API_KEY | Required | ✅ Enforced | ⏳ Not yet |
| §3.2 SECRETS — ANTHROPIC_API_KEY | Required | ❌ Not currently checked | ⏳ Not yet |
| §3.2 SECRETS — MONITOR_TOKEN | Required | ❌ Not currently checked | ⏳ Not yet |
| §3.3 CRITICAL FILES | agents + db_helpers + company.db | ✅ Enforced (superset) | ⏳ Not yet |
| §3.4 DB INTEGRITY (PRAGMA) | Required | ❌ Existence only, no PRAGMA | ⏳ Not yet |
| §3.5 CONFIG — COMMAND_PORT | Required | ✅ Collected | ⏳ Not yet |
| §3.5 CONFIG — INSTALLER_PORT | Required | ✅ Collected | ⏳ Not yet |
| §3.5 CONFIG — MONITOR_URL | Required | ❌ Not enforced | ⏳ Not yet |
| §3.5 CONFIG — PI_ID | Required | ❌ Not enforced | ⏳ Not yet |

**Legend:** ✅ = currently enforced | ❌ = gap vs canonical target | ⏳ = awaiting boot sequence implementation

All boot-time enforcement is deferred to pre-release security hardening. See §7.2 for tracking.
