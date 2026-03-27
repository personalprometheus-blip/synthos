# SYNTHOS TECHNICAL ARCHITECTURE
## Installer System Architecture

**Document Version:** 1.0
**Date:** March 2026
**Audience:** Engineers, AI agents
**Scope:** Retail Pi installer — initial deployment, repair, dependency validation, update preparation

---

## Design Alignment

| Decision | Synthos Principle |
|---|---|
| State written to `.install_progress.json` before transitions | Idempotent and re-runnable |
| Re-entry reads current state, resumes from exact position | Automatic recovery from partial failure |
| `user/` directory is never written or modified by installer | User data must never be modified or lost |
| All states detectable via filesystem and process checks | All states externally detectable |
| Dependency installation is verified, not assumed | No assumption of clean system state |
| Terminal state defined by explicit validation criteria | Deterministic and reproducible behavior |
| No network dependency after initial package fetch | Fully self-contained, offline-capable operation |

---

## State Model

### UNINITIALIZED
System has no installer state. No progress file. No `.env`. No directory structure.

**Detection:**
- `.install_progress.json` absent
- `/home/pi/synthos/user/.env` absent
- `/home/pi/synthos/data/signals.db` absent

---

### COLLECTING
User configuration is being gathered. Progress file exists but is incomplete.

**Detection:**
- `.install_progress.json` present
- `disclaimer_accepted` key absent or `false`
- `.env` absent

---

### VALIDATING
Configuration collected. API connections being tested before write.

**Detection:**
- `.install_progress.json` present
- `disclaimer_accepted == true`
- At least one `test_results` entry present
- `.env` absent

---

### INSTALLING
Environment is being written. Packages being installed. Directory structure being created.

**Detection:**
- `.env` absent or incomplete (missing required keys)
- `.install_progress.json` present
- `install_started == true` in runtime state
- Package installation process running (pip subprocess active)

---

### VERIFYING
Installation complete. Running `health_check.py` to confirm system integrity.

**Detection:**
- `.env` present and complete
- All required packages importable
- Cron entries written
- `health_check.py` process running
- `INSTALL_COMPLETE` sentinel absent

---

### DEGRADED
Verification failed. System is partially installed. One or more checks did not pass.

**Detection:**
- `.env` present
- `health_check.py` exited non-zero
- One or more of: DB integrity failed, required tables absent, Alpaca unreachable
- `INSTALL_COMPLETE` sentinel absent

---

### COMPLETE
All verification passed. System is fully operational.

**Detection:**
- `.env` present and all required keys populated
- All required packages importable
- All required DB tables present
- `health_check.py` last exit code `0`
- `INSTALL_COMPLETE` sentinel present with valid timestamp
- Cron schedule active (`crontab -l` returns expected entries)

---

## State Transitions

```
UNINITIALIZED   → COLLECTING   : .install_progress.json created on installer launch
COLLECTING      → VALIDATING   : disclaimer_accepted == true AND all required fields populated
VALIDATING      → INSTALLING   : all required API tests return ok == true
VALIDATING      → COLLECTING   : any required API test returns ok == false
INSTALLING      → VERIFYING    : .env written, all packages installed, cron written, no subprocess errors
INSTALLING      → DEGRADED     : any install subprocess exits non-zero OR timeout exceeded (900s)
VERIFYING       → COMPLETE     : health_check.py exits 0, all checks passed, INSTALL_COMPLETE written
VERIFYING       → DEGRADED     : health_check.py exits non-zero OR any check returns false
DEGRADED        → INSTALLING   : operator re-runs installer; progress file intact, .env absent or incomplete
DEGRADED        → VERIFYING    : operator re-runs installer; .env intact, repair mode targets failed checks only
COMPLETE        → VERIFYING    : installer re-run on COMPLETE system (update or repair path)
```

---

## Execution Model

**State evaluation on entry:**
1. Read `.install_progress.json` if present
2. Check `.env` presence and completeness
3. Check `INSTALL_COMPLETE` sentinel
4. Run `health_check.py` in read-only probe mode (no writes)
5. Resolve current state from detection criteria above

**Transition execution:**
- Each transition writes state to `.install_progress.json` before performing the action
- No action begins without the prior state being durably recorded
- Each state's completion condition is verified before the next state is entered

**Re-entry behavior:**
- Installer always evaluates current state before acting
- If state is COMPLETE, installer prompts: repair or update path
- If state is DEGRADED, installer resumes from last successful checkpoint
- If state is INSTALLING and progress file is intact, installer skips completed steps

---

## Idempotency Model

| Re-run State | Behavior |
|---|---|
| UNINITIALIZED | Full fresh install |
| COLLECTING | Loads saved config, resumes form at last incomplete step |
| VALIDATING | Re-runs connection tests, does not clear collected config |
| INSTALLING | Skips already-written `.env` keys, re-runs only failed package installs |
| VERIFYING | Re-runs `health_check.py`, does not re-write `.env` or packages |
| DEGRADED | Targets repair at failed checks only; intact components untouched |
| COMPLETE | Enters update-prep or repair path; never re-initializes a passing system |

No installer run modifies `user/` contents under any condition.

---

## Dependency Interaction

**Verification:**
- Each required package is import-tested after pip install, not assumed installed
- Packages listed in `REQUIRED_PACKAGES` are verified individually
- Python version confirmed `>= 3.9` before any installation begins

**Repair:**
- If a package import fails post-install, that package is re-attempted once
- If re-attempt fails, state transitions to DEGRADED with specific package logged
- Dependency failures are recorded in `.install_progress.json` under `failed_deps`

**Transition guards:**
- INSTALLING → VERIFYING is blocked if any entry in `failed_deps` is non-empty
- VERIFYING → COMPLETE is blocked if `health_check.py` reports any required table absent

---

## Failure & Recovery Model

**Partial install:**
- `.install_progress.json` records last completed step
- Re-run reads checkpoint, skips completed steps
- `.env` is written atomically; partial `.env` is detected by key completeness check

**Missing dependencies:**
- Logged to `failed_deps` in progress file
- Installer retries once per missing package
- After retry failure: state → DEGRADED, specific deps reported to operator

**Corrupted system state:**
- DB integrity failure detected by `health_check.py` (`PRAGMA integrity_check`)
- If DB is corrupt and `data/backup/` contains valid backup: backup is restored, VERIFYING re-entered
- If no valid backup exists: state → DEGRADED, operator notified, data directory preserved unchanged
- `.env` corruption (unparseable): installer reports specific parse error, does not overwrite — requires operator resolution

**Timeout:**
- Install subprocess timeout: 900 seconds
- On timeout: state → DEGRADED, partial work preserved, timeout logged to install log

---

## Terminal State Definition

A system is **COMPLETE** when all of the following are simultaneously true:

1. `/home/pi/synthos/user/.env` is present and all required keys are non-empty
2. All packages in `REQUIRED_PACKAGES` are importable without error
3. `/home/pi/synthos/data/signals.db` passes `PRAGMA integrity_check`
4. All tables in `REQUIRED_TABLES` are present in `signals.db`
5. Alpaca API responds `200` with valid account data using keys from `.env`
6. Cron schedule entries for agents and `health_check.py` are present in `crontab -l`
7. `health_check.py` last exit code is `0`
8. `INSTALL_COMPLETE` sentinel file is present at `/home/pi/synthos/.install_complete` with a valid ISO timestamp

A system that meets conditions 1–7 but lacks condition 8 is in VERIFYING, not COMPLETE.

---

**Version:** 1.0
**Last Updated:** March 2026
