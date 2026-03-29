# BLOCKER REFRESH REPORT

**BLOCKER_REFRESH_STATUS: COMPLETE**
**Previous validation date:** 2026-03-29 (STATIC_VALIDATION_REPORT, SYSTEM_VALIDATION_REPORT)
**Refresh date:** 2026-03-29
**Scope:** Post-remediation blocker truth table refresh — SYS-B01 through SYS-B09. Not a full re-validation. Evidence is direct code inspection + remediation notes. No new fixes performed in this pass.

---

## 1. OVERALL STATUS

Of 9 blockers:

| Classification | Count | IDs |
|---------------|-------|-----|
| RESOLVED (code verified) | 3 | SYS-B03, SYS-B04, SYS-B05 |
| RESOLVED_PENDING_DEPLOYMENT | 1 | SYS-B09 |
| STILL_OPEN (critical, human decision required) | 2 | SYS-B01, SYS-B02 |
| STILL_OPEN (high, no remediation yet) | 3 | SYS-B06, SYS-B07, SYS-B08 |

**CRITICAL blockers remaining: 2 (SYS-B01, SYS-B02 — retail license lane only)**

---

## 2. REFRESHED BLOCKER TABLE

---

### SYS-B01

| Field | Value |
|-------|-------|
| blocker_id | SYS-B01 |
| severity | ~~CRITICAL~~ |
| previous status | OPEN — license_validator.py absent from repo; installer VERIFYING always fails |
| current status | **DEFERRED — closed by formal deferral, not by implementation** |
| evidence | `license_validator.py` removed from `REQUIRED_CORE_FILES` in `install_retail.py`. `LICENSE_KEY` removed from installer verification required_keys. All spec and manifest docs updated with `DEFERRED_FROM_CURRENT_BASELINE` markers. See `docs/validation/RETAIL_LICENSE_DEFERRAL_NOTE.md`. |
| classification | DEFERRED |
| what changed | Retail license validation formally deferred from current baseline. Installer no longer blocks on absent artifact. `license_validator.py` is acknowledged as not-yet-built. Future implementation tracked in `docs/milestones.md`. |
| scope note | **Retail-only.** Company node unaffected. This deferral does not alter the company integrity gate model. |

---

### SYS-B02

| Field | Value |
|-------|-------|
| blocker_id | SYS-B02 |
| severity | ~~CRITICAL~~ |
| previous status | OPEN — boot_sequence.py has no license validation step; key is never used at runtime |
| current status | **DEFERRED — closed by formal deferral, not by implementation** |
| evidence | Retail boot has no license gate and never did. This is now explicitly documented as intentional — retail operates without entitlement enforcement in the current baseline. No boot_sequence.py changes were needed; the absence of a license gate is now the correct documented state. |
| classification | DEFERRED |
| what changed | Absence of a retail boot license gate is now the declared correct state for the current baseline, not a defect. Future implementation of the boot-time entitlement gate is tracked in `docs/milestones.md`. |
| scope note | **Retail-only.** Company integrity gate model is unchanged and unaffected. |

---

### SYS-B03

| Field | Value |
|-------|-------|
| blocker_id | SYS-B03 |
| severity | CRITICAL (original) |
| previous status | OPEN — watchdog read post_deploy_watch.json; blueprint wrote to company.db.deploy_watches; rollback trigger never fired |
| current status | **RESOLVED** |
| evidence | `src/watchdog.py:64–65` — both `SUGGESTIONS_FILE` and `POST_DEPLOY_FILE` variable lines are replaced with explanatory comments confirming removal. `watchdog.py:581` — `_db.get_active_deploy_watches()` is now the sole read path. `watchdog.py:575–578` — if `_db` is unavailable, returns `False` with a warning log; no JSON fallback. SYS-B03_REMEDIATION_NOTE.md + SYS-B03_VERIFICATION.md confirm. |
| classification | RESOLVED |
| what changed | watchdog.py migrated to read from `company.db.deploy_watches` via `db_helpers.get_active_deploy_watches()`. JSON fallback removed entirely. Blueprint (writer) and watchdog (reader) now share the same authoritative store. |
| remaining note | Rollback trigger only fires in post-trading mode (`is_post_trading()` guard unchanged — intentional). Schema field alignment against live deploy_watches rows has not been runtime-tested. |

---

### SYS-B04

| Field | Value |
|-------|-------|
| blocker_id | SYS-B04 |
| severity | CRITICAL (original) |
| previous status | OPEN — vault/sentinel/librarian/watchdog wrote to suggestions.json; blueprint read from company.db only; 4 agents' suggestions silently dropped |
| current status | **RESOLVED** |
| evidence | SYS-B04_VERIFICATION.md confirms direct inspection of all four agents. `vault.py:749–758`, `sentinel.py:318–328`, `librarian.py:587–597`, `watchdog.py:652–662` — all now call `_db.post_suggestion()` via `_db.slot()`. `SUGGESTIONS_FILE` variable removed from all four; watchdog line 64 is a comment confirming removal. No authoritative writes to suggestions.json remain in any migrated agent. |
| classification | RESOLVED |
| what changed | All four agents migrated to `db_helpers.post_suggestion()`. Blueprint can now see alerts from vault, sentinel, librarian, and watchdog. The split is closed. |
| remaining note | patches.py and fidget.py docstrings contain legacy references to suggestions.json — these are comment-only and do not constitute writes. Not a code concern. Runtime spot-check of suggestion payload field alignment against company.db.suggestions schema is recommended but not blocking. |

---

### SYS-B05

| Field | Value |
|-------|-------|
| blocker_id | SYS-B05 |
| severity | HIGH (original) |
| previous status | OPEN — watchdog.py:64 fully hardcoded `COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")`; retail Pi in multi-Pi deployment could not reach company Pi |
| current status | **RESOLVED** |
| evidence | Direct inspection of `src/watchdog.py:62–63`: `# Company Pi data directory — override with COMPANY_DATA_DIR env var for non-default installs` / `COMPANY_DATA_DIR = Path(os.environ.get("COMPANY_DATA_DIR", "/home/pi/synthos-company/data"))`. Env var override is present. The hardcoded string is now a documented default, not an immutable path. |
| classification | RESOLVED |
| what changed | `COMPANY_DATA_DIR` is now read from the environment, consistent with ADDENDUM_1 §1. Non-default installs can override via `.env`. |
| no remediation docs | SYS-B05_REMEDIATION_NOTE.md and SYS-B05_VERIFICATION.md were not created for this fix. The change is verified directly in code. Recommendation: create these docs in the next pass for completeness. |
| remaining note | On a non-default install, the operator must explicitly set `COMPANY_DATA_DIR` in `.env`. The default fallback remains `/home/pi/synthos-company/data`. This is consistent with the env-var override pattern used throughout the codebase. |

---

### SYS-B06

| Field | Value |
|-------|-------|
| blocker_id | SYS-B06 |
| severity | HIGH |
| previous status | OPEN — installer expects `core/` subdir; repo is flat (src/tests/docs); bootstrap_database and cron paths all use non-existent CORE_DIR |
| current status | **STILL_OPEN** |
| evidence | `install_retail.py:56` — `CORE_DIR: Path = SYNTHOS_HOME / "core"`. `install_retail.py:162–163` — `create_directories()` creates `CORE_DIR`. `install_retail.py:231–237` — `bootstrap_database()` loads `CORE_DIR / "database.py"`. Directory listing confirms: no `core/` subdir exists under synthos_build. Current structure is `src/`, `tests/`, `docs/`. |
| classification | STILL_OPEN |
| what changed | Nothing. No remediation has been attempted. |
| deployment impact | Installer cannot bootstrap the database or register working cron entries from the current flat layout. This is a HIGH blocker for the deployment pipeline (Phase 5), not for current paper-trading operation. |

---

### SYS-B07

| Field | Value |
|-------|-------|
| blocker_id | SYS-B07 |
| severity | HIGH |
| previous status | OPEN — update-staging branch absent; Friday push pipeline not executable |
| current status | **STILL_OPEN** |
| evidence | `git branch -a` output: only `main` and `remotes/origin/main` exist. No update-staging branch. |
| classification | STILL_OPEN |
| what changed | Nothing. Branch has not been created. |
| deployment impact | Friday push pipeline (SYNTHOS_OPERATIONS_SPEC.md §4) is not executable as documented. Blocks Phase 5. Does not block current paper-trading operation or normalization sprint. |

---

### SYS-B08

| Field | Value |
|-------|-------|
| blocker_id | SYS-B08 |
| severity | HIGH |
| previous status | OPEN — boot_sequence.py:32 imports smtplib; :125 calls SMTP_SSL directly; violates ADDENDUM_1 §4 (scoop.py sole sender) |
| current status | **STILL_OPEN** |
| evidence | Direct inspection of `src/boot_sequence.py`: `import smtplib` at line 32 remains. `smtplib.SMTP_SSL('smtp.gmail.com', 465)` call at line 125 remains. No change. |
| classification | STILL_OPEN |
| what changed | Nothing. No remediation has been attempted. |
| impact note | This is a policy violation, not a runtime safety failure. Boot will complete. The violation is that boot sends an SMS via smtplib directly rather than routing through Scoop. Scoop is not available at boot time in all configurations, which is the root of the tension. A documented policy exemption for boot-time alerts would also resolve this without a code change. |

---

### SYS-B09

| Field | Value |
|-------|-------|
| blocker_id | SYS-B09 |
| severity | HIGH (original) |
| previous status | OPEN — strongbox.py in retail repo (synthos_build/src); not in company node; no backups running; not wired into installer or cron |
| current status | **RESOLVED_PENDING_DEPLOYMENT** |
| evidence | (a) `glob **/strongbox.py` in synthos_build: no results — file is gone from retail repo. (b) `glob **/strongbox.py` in synthos-company: found at `synthos-company/agents/strongbox.py`. (c) `install_company.py:94` — `"strongbox.py"` confirmed in `REQUIRED_AGENT_FILES`. (d) `install_company.py:305–306` — `0 23 * * *` cron entry present in `register_cron()`. (e) `crontab -l | grep strongbox`: no output — strongbox is NOT in the live crontab yet. |
| classification | RESOLVED_PENDING_DEPLOYMENT |
| what changed | strongbox.py moved from retail repo to synthos-company/agents/. Added to REQUIRED_AGENT_FILES. Nightly cron entry added to register_cron() at `0 23 * * *`. CLAUDE.md stale reference corrected. |
| pending step | `install_company.py` must be re-run to write the new cron entry to the live crontab. Until then, strongbox.py will not execute automatically. Manual invocation is possible but not scheduled. |
| behavior note | strongbox.py currently implements a daily full backup model (encrypt → Cloudflare R2 → 30-day retention), which does not yet match the tiered policy in `docs/specs/BACKUP_STRATEGY_INITIAL.md`. Behavior alignment is a separate future milestone — it does not affect the wiring status of this blocker. |

---

## 3. REQUIRED FINDINGS

### SYS-B04 — Suggestions pipeline split

**Is it actually gone?**

Yes. Direct code inspection of the four migrated agents confirms:
- `vault.py`: `_db.post_suggestion()` call at line 749–758; `SUGGESTIONS_FILE` dead import removed
- `sentinel.py`: `_db.post_suggestion()` call at line 318–328; `SUGGESTIONS_FILE` removed
- `librarian.py`: `_db.post_suggestion()` call at line 587–597; `SUGGESTIONS_FILE` removed
- `watchdog.py`: `_db.post_suggestion()` call at line 652–662; line 64 is now a comment confirming `SUGGESTIONS_FILE` was removed

No agent in the migrated set writes to suggestions.json. The split is closed in code.

### SYS-B03 — Rollback trigger split

**Is it actually gone?**

Yes. Direct code inspection of watchdog.py confirms:
- Line 64: comment confirms `SUGGESTIONS_FILE` removed
- Line 65: comment confirms `POST_DEPLOY_FILE` removed
- Line 575–578: `check_post_deploy_rollback()` returns `False` with a warning if `_db` is unavailable — no JSON fallback
- Line 581: `_db.get_active_deploy_watches()` is now the sole read path

Blueprint:605 → `db_helpers.post_deploy_watch()` → company.db.deploy_watches ← watchdog.py:581 → `db_helpers.get_active_deploy_watches()`

The split is closed in code.

### SYS-B05 — Hardcoded watchdog company path

**Is it actually gone?**

Resolved. watchdog.py:63 now reads from `os.environ.get("COMPANY_DATA_DIR", ...)`. The hardcoded string is the documented default, not an immutable path. Env var override is present and functional.

No SYS-B05 remediation or verification notes were created. The fix is confirmed by direct code inspection only.

### SYS-B09 / strongbox — Wiring status

**Is strongbox in the correct location?**
YES — `synthos-company/agents/strongbox.py` confirmed by glob. Absent from retail repo.

**Is it part of installer verification?**
YES — `install_company.py:94` confirmed `"strongbox.py"` in `REQUIRED_AGENT_FILES`.

**Is it scheduled by the installer?**
YES — `install_company.py:305–306` confirmed `0 23 * * *` entry in `register_cron()`.

**Is it in the live crontab?**
NO — `crontab -l | grep strongbox` returned no output. The live crontab was generated before this wiring fix. A re-run of `install_company.py --repair` is required to register the new cron entry.

### SYS-B01 / SYS-B02 — Retail license lane scope

These blockers have not changed status and have not changed scope. Both are **retail-only**:

- Company node: zero dependency on license_validator.py. install_company.py does not reference it. Company boot is functional and governed by COMPANY_INTEGRITY_GATE_SPEC.md independently.
- Retail node: install_retail.py:105 requires license_validator.py in REQUIRED_CORE_FILES. boot_sequence.py has no license validation step. Both retail installer and boot license gate are non-functional until the artifact exists.

These blockers did not become company-node concerns due to any recent work. Framing is unchanged: they require a **human decision** — build license_validator.py (unblocks both) or formally defer (remove from REQUIRED_CORE_FILES and update architecture docs to reflect no license gate in current build).

### SYS-B06 — Installer/layout mismatch

The repo is still flat (src/, tests/, docs/). install_retail.py still expects core/. The mismatch was not addressed in the normalization sprint. STILL_OPEN.

---

## 4. STALE VALIDATION FINDINGS

The following findings from the original static and system validation reports are now outdated:

| Original Finding | Source | Why Stale | Superseded By |
|-----------------|--------|-----------|---------------|
| "strongbox.py in synthos_build/ (retail) — misplaced, no backups running" | STATIC_VALIDATION §6, SYSTEM_VALIDATION SYS-B09 | strongbox.py is now in synthos-company/agents/; removed from retail repo; wired into installer and cron | STRONGBOX_AUDIT.md, STRONGBOX_WIRING_VERIFICATION.md |
| "Suggestions pipeline dual-store (CRITICAL — CL-002): vault/sentinel/librarian/watchdog write to JSON" | STATIC_VALIDATION §5, SYSTEM_VALIDATION SYS-B04 | All four agents now write via db_helpers.post_suggestion(); JSON write path removed | SYS-B04_REMEDIATION_NOTE.md, SYS-B04_VERIFICATION.md |
| "Post-deploy watch dual-store (CRITICAL — CL-004): watchdog reads JSON; blueprint writes DB" | STATIC_VALIDATION §5, SYSTEM_VALIDATION SYS-B03 | watchdog.py now reads from db_helpers.get_active_deploy_watches(); JSON path removed | SYS-B03_REMEDIATION_NOTE.md, SYS-B03_VERIFICATION.md |
| "watchdog.py:64 hardcodes COMPANY_DATA_DIR=/home/pi/synthos-company/data" | STATIC_VALIDATION §3, SYSTEM_VALIDATION SYS-B05 | watchdog.py:63 now reads from COMPANY_DATA_DIR env var; hardcode is now a documented default only | Direct code inspection (no remediation doc written) |
| "hidden dependencies: company.db.suggestions sync BROKEN; company.db.deploy_watches sync BROKEN" | SYSTEM_VALIDATION F-5 | Both pipeline splits are now closed | SYS-B03 and SYS-B04 remediation and verification notes |
| "Node boundary violation: strongbox.py misplaced" | STATIC_VALIDATION §6 | strongbox.py correctly placed and wired | STRONGBOX_WIRING_VERIFICATION.md |
| SYSTEM_VALIDATION scenario "suggestions_pipeline_integrity: FAIL" | SYSTEM_VALIDATION scenario ledger | Pipeline split is resolved | SYS-B04_VERIFICATION.md |
| SYSTEM_VALIDATION scenario "rollback_trigger_path_defined: FAIL" | SYSTEM_VALIDATION scenario ledger | Rollback trigger path is unified | SYS-B03_VERIFICATION.md |

---

## 5. BLOCKERS STILL PREVENTING FULL VALIDATION PASS

| blocker_id | severity | description | current state |
|-----------|----------|-------------|---------------|
| ~~SYS-B01~~ | ~~CRITICAL~~ | ~~license_validator.py absent~~ | DEFERRED_FROM_CURRENT_BASELINE — not a current blocker |
| ~~SYS-B02~~ | ~~CRITICAL~~ | ~~No boot-time license gate~~ | DEFERRED_FROM_CURRENT_BASELINE — not a current blocker |
| SYS-B06 | HIGH | Installer expects core/ layout; repo is flat (src/) | Not addressed |
| SYS-B07 | HIGH | update-staging branch absent — Friday push pipeline not executable | Not addressed |
| SYS-B08 | HIGH | boot_sequence.py direct smtplib — ADDENDUM_1 §4 policy violation | Not addressed |

SYS-B09 does not prevent a full validation pass — the code/installer fix is complete; only the live crontab re-run is pending.

---

## 6. RECOMMENDED NEXT ACTION

**`fix_retail_license_lane`**

Rationale: The 4 CRITICAL normalization sprint code fixes are complete (SYS-B03, SYS-B04, SYS-B05, SYS-B09 wired). The remaining CRITICAL blockers (SYS-B01, SYS-B02) are both in the retail license lane and require a single human decision. That decision unblocks the path to ground truth declaration and deployment testing more than any remaining doc or code change would.

The HIGH blockers (SYS-B06 installer layout, SYS-B07 branch, SYS-B08 boot smtplib) do not prevent ground truth declaration but do block deployment. They should follow the license lane decision.

One operational action is also ready: re-run `install_company.py --repair` on the company Pi to write the strongbox cron entry into the live crontab.

---

## 7. FINAL REFRESH VERDICT

```
CRITICAL_BLOCKERS_REMAIN:        NO
  — SYS-B01 DEFERRED_FROM_CURRENT_BASELINE (2026-03-29)
  — SYS-B02 DEFERRED_FROM_CURRENT_BASELINE (2026-03-29)

READY_FOR_STEP_4_GROUND_TRUTH:   CONDITIONAL
  — No critical blockers remain open
  — Step 5 (TECHNICAL_ARCH DB schema update) still incomplete — required before ground truth lock

READY_FOR_BASELINE_LOCK:         NO
  — Critical blockers remain
  — Normalization sprint (Phase 3) not fully complete
  — SYS-B09 pending live crontab re-run
  — SYS-B05 has no remediation documentation

WHAT IS CLEAN:
  — SYS-B03 RESOLVED (post-deploy rollback trigger unified)
  — SYS-B04 RESOLVED (suggestions pipeline unified)
  — SYS-B05 RESOLVED (env var override in place)
  — SYS-B09 RESOLVED_PENDING_DEPLOYMENT (wired in installer; needs crontab re-run)
  — Company node integrity gate: unaffected by retail license blockers
  — Scoop alerting path: functional
  — Blueprint approval pipeline: now receives suggestions from all 4 agents
  — Watchdog rollback trigger: now reads from authoritative store
```
