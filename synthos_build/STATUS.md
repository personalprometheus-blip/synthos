# PROJECT STATUS

> **⚠ This file is a historical Phase-1-through-5 snapshot (frozen
> 2026-04-05).** It captures the pre-Pi-5-deployment era and the
> blockers that existed then. Everything below is preserved for
> audit trail; none of it is the live state of the system.
>
> **Current state (2026-04-24):** The retail Pi 5 is deployed and
> running on NVMe storage; the trading stack is in Phase 6 paper mode
> with Phase C refactor and a full sentiment-chain wiring audit
> already landed. The single source of truth for running agents,
> services, cron schedules, data flow, and known issues is:
>
> - `data/system_architecture.json` (v3.12, living document)
> - `docs/pipeline_audit_2026-04-24.md` (producer→consumer trace)
> - `docs/trade_lifecycle.md` (per-trade decision path)
> - `docs/backlog.md` (deferred work with entry conditions)
>
> **Recent landmark changes not reflected below:**
> - pi5 16GB deployed to NVMe (2026-04-18)
> - pi2w_monitor re-enabled as external watchdog (2026-04-24)
> - Phase C (D1-D6) refactor: trader `run()` 897→15 lines, portal
>   14,294→5,931 lines, `retail_shared.py` consolidation
> - Pipeline audit Gaps 1-3 wired (2026-04-24): validator verdict
>   consumed by Gate 1, `_MARKET_STATE_SCORE` in Gate 5 composite,
>   real fill price via `_resolve_fill_price`
>
> **REPO IDENTITY:** `personalprometheus-blip/synthos` — local: `/home/pi/synthos/synthos_build/`
> **This repo owns:** retail_node (Pi 5) — trading agents, portal, signals.db, ingestion pipeline
> **Also owns:** master project tracker (PROJECT_STATUS.md) for all phases/cross-repo blockers
> **Companion:** `synthos-company` owns company_node (Pi 4B) agents — do NOT put company code here
> **Separate:** `Sentinel` repo is a display side project — unrelated to Synthos operation

---

## HISTORICAL SNAPSHOT (frozen 2026-04-05) — preserved for audit trail

**Last Updated:** 2026-04-05
**Current Phase:** Phase 5 complete — retail Pi 5 build is next
**Overall Progress:** 5 of 6 phases complete

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
- Company node agents operational: scoop, strongbox, company_server (planned: company_sentinel, company_auditor, company_vault, company_archivist, company_keepalive)
- validate_02.py passing 22/22 (portal surface)
- validate_03b.py passing 44/44 (approval queue)
- company_auditor.py bugs fixed (dry-run, timezone, continuous mode)
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

## ✅ Completed (continued)

### Phase 5 — Deployment Pipeline
- [x] Create update-staging branch
- [x] Document actual Friday push process
- [x] First end-to-end deploy test in paper mode
- [x] Verify post-deploy rollback trigger fires correctly
- [x] Verify watchdog known-good snapshot and restore

## 🔴 Not Started

### Phase 6 — Live Trading Gate
- Paper trading review complete
- Project lead approval obtained
- TRADING_MODE flip to LIVE (explicit human action only)

---

## Current Milestone: Pi 5 Retail Build
**Goal:** Deploy retail portal and all trading agents on the incoming Pi 5
**Status:** Blocked on hardware — Pi 5 on order

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

## Deferred — Revisit When First Paying Customer Goes Live
- **Heartbeat fallback service**: Company Pi and Monitor Pi currently have no external watchdog.
  If either goes down, no alert can be sent (Scoop is on Company Pi; Monitor can't self-report).
  Current plan: Google Apps Script dead man's switch (POST timestamp on schedule; Gmail alert if stale).
  When paying customers are live, evaluate purpose-built services (Healthchecks.io, Cronitor) for
  SMS alerts, status page, and multi-channel notification. For now, Google approach is sufficient.

---

## Notes for AI Agents
- This is a paper-trading-only system. TRADING_MODE must remain PAPER.
- Pi 2W (10.0.0.121) is retired — do not SSH to it or reference it in planning
- company_node repo is at `/home/pi/synthos-company/` — separate from this retail repo
- See CLAUDE.md for full session context
