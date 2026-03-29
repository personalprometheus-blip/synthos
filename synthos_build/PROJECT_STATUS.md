# PROJECT STATUS — Synthos

**Last Updated:** 2026-03-29
**Current Phase:** Phase 3 — Normalization Sprint
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
| 3 | Normalization Sprint | 🟡 In Progress |
| 4 | Ground Truth Declaration | 🔴 Not Started |
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
- [ ] **Step 5 (DOC):** Document company.db schema (PRAGMA table_info) — CL-012
- [ ] **Step 6 (HUMAN DECISION):** Declare license_validator.py status — (a) build now or (b) formally defer

Secondary (required before Phase 4):
- [ ] Mark SUGGESTIONS_JSON_SPEC.md as SUPERSEDED
- [ ] Mark POST_DEPLOY_WATCH_SPEC.md as SUPERSEDED
- [ ] Update SYSTEM_MANIFEST.md (v1.2 env vars)
- [ ] Boot SMS alert (boot_sequence.py smtplib) — route through MONITOR_URL or document exception

---

## Phase 4 — Ground Truth Declaration 🔴 NOT STARTED

**Gate:** All Phase 3 steps complete.

- [ ] Run PRAGMA table_info on signals.db and company.db — extract live schema
- [ ] Update docs/validation/SYNTHOS_GROUND_TRUTH.md to v1.2
- [ ] Confirm all active processes match manifest
- [ ] Confirm suggestions store state on company Pi
- [ ] Declare new ground truth — commit and tag

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

---

## Open Blockers (cross-project)

| ID | Repo | Severity | Description |
|----|------|----------|-------------|
| SYS-B01 | synthos | CRITICAL | license_validator.py missing — installer always fails VERIFYING phase |
| SYS-B02 | synthos | CRITICAL | No license gate in boot_sequence.py |
| CL-009 | synthos-company | HIGH | Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md |
| CL-012 | synthos-company | HIGH | company.db schema undocumented |
