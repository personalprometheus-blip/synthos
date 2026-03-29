# PROJECT STATUS — Synthos

**Last Updated:** 2026-03-29
**Current Phase:** Phase 5 — Deployment Pipeline (Phases 3 and 4 complete)
**Authority:** This document is the master cross-project tracker. For node-specific operational health, see each repo's STATUS.md.

---

## Repos

| Repo | Node | Role | Status |
|------|------|------|--------|
| [personalprometheus-blip/synthos](https://github.com/personalprometheus-blip/synthos) | retail_node (Pi 2W) | Trading agents, portal, signals.db | Active |
| [personalprometheus-blip/synthos-company](https://github.com/personalprometheus-blip/synthos-company) | company_node (Pi 4B / cloud) | Ops agents, licensing, backups, monitoring | Active |

---

## Phase Overview

| Phase | Name | Status |
|-------|------|--------|
| 1 | Core Trading System | ✅ Complete |
| 2 | Company Node + Validation Infrastructure | ✅ Complete |
| 3 | Normalization Sprint | ✅ Complete |
| 4 | Ground Truth Declaration | ✅ Complete |
| 5 | Deployment Pipeline | 🔴 Not Started |
| 6 | Live Trading Gate | 🔴 Not Started |

---

## Phase 1 — Core Trading System ✅ COMPLETE

- [x] agent1_trader.py (ExecutionAgent / Bolt) operational
- [x] agent2_research.py (ResearchAgent / Scout) operational
- [x] agent3_sentiment.py (SentimentAgent / Pulse) operational
- [x] signals.db schema stable (v1.2, 17+ tables)
- [x] Portal live (port 5001), validate_02 passing 22/22
- [x] Option B decision logic (MIRROR/WATCH/WATCH_ONLY)
- [x] Member weights, news_feed, 5yr price history
- [x] Interrogation listener (UDP peer corroboration)
- [x] Pending approvals queue (DB-backed)
- [x] validate_03b passing 44/44

---

## Phase 2 — Company Node + Validation Infrastructure ✅ COMPLETE

- [x] Company node agents deployed: blueprint, sentinel, vault, patches, librarian, fidget, scoop, timekeeper
- [x] patches.py bugs fixed (dry-run, timezone, continuous mode)
- [x] Heartbeat architecture resolved
- [x] Full architectural reconciliation (26 conflicts logged in CONFLICT_LEDGER.md)
- [x] Static validation report written
- [x] System validation report written
- [x] Repo reorganized to professional structure (CLAUDE.md, STATUS.md, README.md)
- [x] synthos-company initialized as separate git repo

---

## Phase 3 — Normalization Sprint 🟡 IN PROGRESS

**Goal:** Resolve all critical blockers identified in SYSTEM_VALIDATION_REPORT.md.

- [x] **Step 1 (CODE):** Migrate suggestions pipeline — vault.py, sentinel.py, librarian.py, watchdog.py → `db_helpers.post_suggestion()`
- [x] **Step 2 (CODE):** Migrate watchdog.py post_deploy_watch read → `db_helpers.get_active_deploy_watches()`
- [x] **Step 3 (CODE):** Fix `watchdog.py` hardcoded `COMPANY_DATA_DIR` → env var
- [x] **Step 4 (FILE MOVE):** Move strongbox.py to synthos-company/agents/
- [x] **Step 5 (DOC):** Document company.db schema — CL-012 RESOLVED. Canonical schema defined in docs/specs/DATABASE_SCHEMA_CANONICAL.md covering both signals.db (retail, v1.2, 12 tables) and company.db (company, v2.0, 13 tables). Stale schema in SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3/§3.3 replaced with references.
- [x] **Step 6 (HUMAN DECISION):** Declare license_validator.py status — FORMALLY DEFERRED (DEFERRED_FROM_CURRENT_BASELINE; removed from installer requirements; future work tracked in docs/milestones.md)

Secondary (required before Phase 4):
- [ ] Mark SUGGESTIONS_JSON_SPEC.md as SUPERSEDED
- [ ] Mark POST_DEPLOY_WATCH_SPEC.md as SUPERSEDED
- [ ] Update SYSTEM_MANIFEST.md (v1.2 env vars)
- [ ] Boot SMS alert (boot_sequence.py smtplib) — route through MONITOR_URL or document exception

---

## Phase 4 — Ground Truth Declaration ✅ COMPLETE

**Completed:** 2026-03-29

- [x] Schema extracted and canonicalized — `docs/specs/DATABASE_SCHEMA_CANONICAL.md`
- [x] Ground Truth synthesized — `docs/GROUND_TRUTH.md` (authoritative system definition)
- [x] All critical blockers resolved or formally deferred (CRITICAL_BLOCKERS_REMAIN: NO)
- [x] All normalization sprint steps complete
- [x] Ground Truth declared and committed

---

## Phase 5 — Deployment Pipeline 🔴 NOT STARTED

- [ ] Create update-staging git branch
- [ ] Document actual Friday push process
- [ ] First end-to-end deploy test in paper mode
- [ ] Verify post-deploy rollback trigger fires correctly
- [ ] Verify watchdog known-good snapshot and restore

---

## Phase 6 — Live Trading Gate 🔴 NOT STARTED

**This phase requires explicit human decision. No code change flips this.**

- [ ] Paper trading review — minimum 30-day clean run
- [ ] All validation checks passing
- [ ] Project lead approval documented
- [ ] TRADING_MODE=LIVE set by project lead only

### Pre-Release Security Hardening (gate condition for Phase 6)

These items must be completed before any live trading or adversarial deployment. They do not block normalization or deployment pipeline testing.

- [ ] Implement company boot-time integrity gate (`install_company.py` → `boot_company.py` or equivalent) — evaluates all §3 checks from `COMPANY_INTEGRITY_GATE_SPEC.md` before starting any agent
- [ ] Align installer required-key check with canonical company integrity-gate secret set (`ANTHROPIC_API_KEY`, `MONITOR_TOKEN` currently missing from installer)
- [ ] Add PRAGMA integrity_check to installer DB verification (currently checks existence only)
- [ ] Enforce `MONITOR_URL` and `PI_ID` presence at installer time
- [ ] Verify company startup trust path under normal and break-glass modes
- [ ] Implement retail boot-time license gate — FUTURE_RETAIL_ENTITLEMENT_WORK (deferred from current baseline; see docs/milestones.md)

**Reference:** `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`, `docs/validation/TRUST_GATE_ALIGNMENT_NOTE.md`

---

## Open Blockers (cross-project)

| ID | Repo | Severity | Description |
|----|------|----------|-------------|
| ~~SYS-B01~~ | synthos | ~~CRITICAL~~ | ~~license_validator.py missing~~ — DEFERRED_FROM_CURRENT_BASELINE |
| ~~SYS-B02~~ | synthos | ~~CRITICAL~~ | ~~No license gate in boot_sequence.py~~ — DEFERRED_FROM_CURRENT_BASELINE |
| CL-009 | synthos-company | HIGH | Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md |
| ~~CL-012~~ | synthos-company | ~~HIGH~~ | ~~company.db schema undocumented~~ — RESOLVED: docs/specs/DATABASE_SCHEMA_CANONICAL.md |
