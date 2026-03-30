# PROJECT STATUS

> **REPO IDENTITY:** `personalprometheus-blip/synthos` — local: `/home/pi/synthos/synthos_build/`
> **This repo owns:** retail_node (Pi 2W) — trading agents, portal, signals.db
> **Also owns:** master project tracker (PROJECT_STATUS.md) for all phases/cross-repo blockers
> **Companion:** `synthos-company` owns company_node (Pi 4B) agents — do NOT put company code here
> **Separate:** `Sentinel` repo is unrelated to Synthos

**Last Updated:** 2026-03-30
**Current Phase:** Phase 5 — Deployment Pipeline
**Overall Progress:** 4 of 6 phases complete

---

## ✅ Completed

### Phase 1 — Core Trading System (retail_node)
- Three trading agents operational: agent1_trader.py, agent2_research.py, agent3_sentiment.py
- Portal live on port 5001
- signals.db schema stable (17+ tables, v1.2)
- Option B decision logic implemented (MIRROR/WATCH/WATCH_ONLY)
- Member weights, news_feed, 5yr price history, interrogation, pending_approvals all live
- Approval queue: validate_03b passing 44/44

### Phase 2 — Company Node + Validation Infrastructure
- Company node agents operational: blueprint, sentinel, vault, patches, librarian, fidget, scoop, timekeeper
- validate_02.py passing 22/22 (portal surface)
- validate_03b.py passing 44/44 (approval queue)
- patches.py bugs fixed (dry-run, timezone, continuous mode)
- Heartbeat architecture resolved (HEARTBEAT_RESOLUTION closed)
- Full architectural reconciliation complete (26 conflicts identified and logged)
- Static validation complete (STATIC_VALIDATION_REPORT.md)
- System validation complete (SYSTEM_VALIDATION_REPORT.md)
- Repo reorganized to professional structure

### Phase 3 — Normalization Sprint
- Suggestions pipeline migrated to db_helpers.post_suggestion() across all agents
- watchdog.py post_deploy_watch migrated to db_helpers.get_active_deploy_watches()
- watchdog.py COMPANY_DATA_DIR hardcode fixed to env var
- strongbox.py moved to synthos-company/agents/
- company.db schema canonicalized — docs/specs/DATABASE_SCHEMA_CANONICAL.md (CL-012 RESOLVED)
- license_validator.py formally deferred — DEFERRED_FROM_CURRENT_BASELINE
- All secondary doc tasks complete (SUGGESTIONS_JSON_SPEC, POST_DEPLOY_WATCH_SPEC, SYSTEM_MANIFEST)

### Phase 4 — Ground Truth Declaration
- Schema extracted and canonicalized — docs/specs/DATABASE_SCHEMA_CANONICAL.md
- Ground Truth synthesized — docs/GROUND_TRUTH.md
- All critical blockers resolved or formally deferred (CRITICAL_BLOCKERS_REMAIN: NO)
- Ground Truth declared and committed

---

## 🟡 In Progress

### Phase 5 — Deployment Pipeline
- [x] Create update-staging branch
- [ ] Document actual Friday push process
- [ ] First end-to-end deploy test in paper mode
- [ ] Verify post-deploy rollback trigger fires correctly
- [ ] Verify watchdog known-good snapshot and restore

## 🔴 Not Started

### Phase 6 — Live Trading Gate
- Paper trading review complete
- Project lead approval obtained
- TRADING_MODE flip to LIVE (explicit human action only)

---

## Current Milestone: Deployment Pipeline (Phase 5)
**Goal:** Build and validate the end-to-end deploy pipeline in paper mode
**Status:** Not started — stragglers from Phase 3/4 cleared; ready to begin

## Blockers
| ID | Severity | Description |
|----|----------|-------------|
| ~~SYS-B01~~ | ~~CRITICAL~~ | ~~`license_validator.py` missing~~ — DEFERRED_FROM_CURRENT_BASELINE |
| ~~SYS-B02~~ | ~~CRITICAL~~ | ~~No boot-time license gate~~ — DEFERRED_FROM_CURRENT_BASELINE |
| ~~SYS-B03~~ | ~~CRITICAL~~ | ~~Post-deploy rollback broken~~ — RESOLVED |
| ~~SYS-B04~~ | ~~CRITICAL~~ | ~~Suggestions pipeline split~~ — RESOLVED |
| ~~SYS-B05~~ | ~~HIGH~~ | ~~`watchdog.py` hardcoded `COMPANY_DATA_DIR`~~ — RESOLVED |
| ~~SYS-B06~~ | ~~HIGH~~ | ~~Installer core/ vs flat layout mismatch~~ — RESOLVED 2026-03-30 |
| ~~SYS-B07~~ | ~~HIGH~~ | ~~`update-staging` branch absent~~ — RESOLVED 2026-03-30 |

Full blocker detail: docs/validation/SYSTEM_VALIDATION_REPORT.md

## Company Integrity Gate
- Architecture defined: `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`
- Installer enforces a partial subset (MODE check, some secrets, file presence)
- **Full enforcement is not implemented:** no company boot sequence exists to evaluate the gate before runtime services start
- Boot-time company integrity gate is tracked as a pre-release security gate task (Phase 6 / PROJECT_STATUS.md)
- This gap does not block the normalization sprint

---

## Backup Model

- **Policy:** Monthly full baseline snapshot + nightly incremental chain (local-only)
- **Retention:** 6-month full baseline; incremental chain deleted on each new baseline
- **Status:** Policy defined (`docs/specs/BACKUP_STRATEGY_INITIAL.md`); implementation pending
- **Deferred:** Networked / off-device backup, cloud, encryption — future evaluation only

---

## Notes for AI Agents
- This is a paper-trading-only system. TRADING_MODE must remain PAPER.
- patches.py was killed for this work session — restart at end: `nohup python3 /home/pi/synthos-company/agents/patches.py --mode continuous >> logs/bug_finder.log 2>&1 &`
- company_node repo is at `/home/pi/synthos-company/` — separate from this retail repo
- Do NOT rename files or refactor architecture during the normalization sprint
- See CLAUDE.md for full session context
