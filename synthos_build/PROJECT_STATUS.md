# PROJECT STATUS — Synthos

**Last Updated:** 2026-04-05
**Current Phase:** Phase 5 complete — Pi 5 retail build pending before Phase 6
**Authority:** This document is the master cross-project tracker. For node-specific operational health, see each repo's STATUS.md.

---

## Repos

| Repo | Node | Role | Status |
|------|------|------|--------|
| [personalprometheus-blip/synthos](https://github.com/personalprometheus-blip/synthos) | retail_node (Pi 5, incoming) | Trading agents, portal, signals.db, ingestion pipeline | Hardware pending |
| [personalprometheus-blip/synthos-company](https://github.com/personalprometheus-blip/synthos-company) | company_node (Pi 4B) | Ops agents, company_server API, backups, monitoring | Active |
| ~~personalprometheus-blip/synthos-process~~ | ~~process_node~~ | ~~News/signal ingestion~~ | CANCELLED — merged into retail_node |

---

## Phase Overview

| Phase | Name | Status |
|-------|------|--------|
| 1 | Core Trading System | ✅ Complete |
| 2 | Company Node + Validation Infrastructure | ✅ Complete |
| 3 | Normalization Sprint | ✅ Complete |
| 4 | Ground Truth Declaration | ✅ Complete |
| 5 | Deployment Pipeline | ✅ Complete |
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

- [x] Company node agents deployed: scoop, strongbox, company_server (planned: company_sentinel, company_auditor, company_vault, company_archivist, company_keepalive)
- [x] company_auditor.py bugs fixed (dry-run, timezone, continuous mode)
- [x] Heartbeat architecture resolved
- [x] Full architectural reconciliation (26 conflicts logged in CONFLICT_LEDGER.md)
- [x] Static validation report written
- [x] System validation report written
- [x] Repo reorganized to professional structure (CLAUDE.md, STATUS.md, README.md)
- [x] synthos-company initialized as separate git repo

---

## Phase 3 — Normalization Sprint ✅ COMPLETE

**Goal:** Resolve all critical blockers identified in SYSTEM_VALIDATION_REPORT.md.

- [x] **Step 1 (CODE):** Migrate suggestions pipeline — company_vault.py, company_sentinel.py, company_archivist.py, retail_watchdog.py → `db_helpers.post_suggestion()`
- [x] **Step 2 (CODE):** Migrate watchdog.py post_deploy_watch read → `db_helpers.get_active_deploy_watches()`
- [x] **Step 3 (CODE):** Fix `watchdog.py` hardcoded `COMPANY_DATA_DIR` → env var
- [x] **Step 4 (FILE MOVE):** Move strongbox.py to synthos-company/agents/
- [x] **Step 5 (DOC):** Document company.db schema — CL-012 RESOLVED. Canonical schema defined in docs/specs/DATABASE_SCHEMA_CANONICAL.md covering both signals.db (retail, v1.2, 12 tables) and company.db (company, v2.0, 13 tables). Stale schema in SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3/§3.3 replaced with references.
- [x] **Step 6 (HUMAN DECISION):** Declare license_validator.py status — FORMALLY DEFERRED (DEFERRED_FROM_CURRENT_BASELINE; removed from installer requirements; future work tracked in docs/milestones.md)

Secondary (required before Phase 4):
- [x] Mark SUGGESTIONS_JSON_SPEC.md as SUPERSEDED
- [x] Mark POST_DEPLOY_WATCH_SPEC.md as SUPERSEDED
- [x] Update SYSTEM_MANIFEST.md — CORE_DIR core→src, install.py→install_retail.py, remove cleanup.py
- [x] Boot SMS alert — documented as formal architectural exception in boot_sequence.py (pre-agent context; scoop.py not yet running at boot time)

---

## Phase 4 — Ground Truth Declaration ✅ COMPLETE

**Completed:** 2026-03-29

- [x] Schema extracted and canonicalized — `docs/specs/DATABASE_SCHEMA_CANONICAL.md`
- [x] Ground Truth synthesized — `docs/GROUND_TRUTH.md` (authoritative system definition)
- [x] All critical blockers resolved or formally deferred (CRITICAL_BLOCKERS_REMAIN: NO)
- [x] All normalization sprint steps complete
- [x] Ground Truth declared and committed

---

## Phase 5 — Deployment Pipeline ✅ COMPLETE

- [x] Create update-staging git branch
- [x] Document actual Friday push process — `docs/governance/FRIDAY_PUSH_RUNBOOK.md`
- [x] First end-to-end deploy test in paper mode
- [x] Verify post-deploy rollback trigger fires correctly
- [x] Verify watchdog known-good snapshot and restore

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
| ~~CL-009~~ | synthos-company | ~~HIGH~~ | ~~Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md~~ — RESOLVED 2026-03-30 |
| ~~CL-012~~ | synthos-company | ~~HIGH~~ | ~~company.db schema undocumented~~ — RESOLVED: docs/specs/DATABASE_SCHEMA_CANONICAL.md |

---

## Addendum — v3 Portal Architecture (2026-04-05)

### Decisions locked

**1. Single portal model.**
All web-facing access routes through the Pi 5 retail portal (`app.synth-cloud.com`, port 5001).
Customers log in and see their own data. Patrick logs in as `role='admin'` and sees his trading
dashboard plus a Company Admin link. There is no separate admin subdomain.

**2. company_server.py is internal API only.**
The Pi 4B runs `company_server.py` on port 5010 as a private backend. The Pi 5 retail portal
calls it over the local network to serve admin data. No public domain points to it.
`admin.synth-cloud.com` DNS and Cloudflare Access app have been removed.

**3. login_server/ retired.**
The node-picker SSO model was the wrong design. Customers do not have individual Pi nodes.
`synthos-login.service` is stopped and disabled. `login_server/` code remains in repo for
reference but is not active. `portal.synth-cloud.com` redirects to `app.synth-cloud.com`.

**4. Pi 2W fully retired.**
Removed from all architecture. No further SSH or configuration work on 10.0.0.121.

### Final domain map

| Domain | Destination | Auth | Notes |
|--------|-------------|------|-------|
| `app.synth-cloud.com` | Pi 5 port 5001 | Portal login (auth.py) | Primary portal for all users |
| `portal.synth-cloud.com` | redirect → app | none | Convenience redirect |
| `ssh.synth-cloud.com` | Pi 4B port 22 | Cloudflare Access (iCloud OTP) | Admin SSH |
| `ssh2.synth-cloud.com` | Pi 5 port 22 | Cloudflare Access (iCloud OTP) | Retail SSH |
| ~~`admin.synth-cloud.com`~~ | removed | — | Was Pi 4B :5010 — retired |

### Portal flow

```
portal.synth-cloud.com ──redirect──▶ app.synth-cloud.com (Pi 5 :5001)
                                              │
                                   ┌──────────┴───────────┐
                              customer login           admin login
                              → trading dashboard      → trading dashboard
                                                        + [Company Admin →]
                                                              │
                                                    Pi 4B :5010 API
                                                    (local network only)
```

---

## Validation Plan — Pi 5 Retail Build

To be executed when Pi 5 arrives. All items must pass before Phase 6 consideration.

### Infrastructure
- [ ] Pi 5 on network, SSH accessible via `ssh2.synth-cloud.com`
- [ ] Cloudflare retail-pi tunnel config updated for Pi 5 MAC/IP
- [ ] `app.synth-cloud.com` routes to Pi 5 port 5001 and returns HTTP 200

### Portal & Auth
- [ ] `retail_portal.py` starts cleanly on Pi 5
- [ ] `auth.db` created with correct schema (init_auth_db + migrate_auth_db)
- [ ] Admin account created from `.env` on first start (ensure_admin_account)
- [ ] Owner account created from `.env` on first start (ensure_owner_customer)
- [ ] Patrick can log in at `app.synth-cloud.com` with `personal_prometheus@icloud.com`
- [ ] Admin role confirmed — Company Admin link visible to Patrick, absent for test customer account
- [ ] Test customer account can log in and sees only their own data

### Company Admin Link
- [ ] Company Admin link in retail portal points to Pi 4B `company_server.py` API
- [ ] Admin section in portal renders company queue, agent status, and logs correctly
- [ ] Non-admin users receive 403 if they attempt to access admin routes directly

### Trading System
- [ ] All three trading agents start and post signals to signals.db
- [ ] Portal dashboard displays live signal data
- [ ] validate_02.py passes (portal surface)
- [ ] validate_03b.py passes (approval queue)
- [ ] Watchdog snapshot and rollback verified in paper mode
- [ ] Friday push runbook tested end-to-end on Pi 5

### Company ↔ Retail Integration
- [ ] Retail agents can reach Pi 4B `company_server.py` at local network address
- [ ] Heartbeat from Pi 5 received by company_sentinel on Pi 4B
- [ ] Scoop queue drains correctly — alerts delivered via Resend

---

## Document Consolidation Plan

The following documents contain stale references to Pi 2W, process_node, or the old
node-picker portal model. They must be updated before Phase 6 or first customer onboarding.

### Priority 1 — Update before Pi 5 build starts
| Document | Location | Stale content |
|----------|----------|---------------|
| CLAUDE.md | synthos/synthos_build/ | References Pi 2W, old phase, process_node |
| CLAUDE.md | synthos-company/ | References Pi 2W, process_node, old phase |
| GROUND_TRUTH.md | synthos-company/docs/ | May reference Pi 2W retail node |
| SYSTEM_MANIFEST.md | synthos-company/docs/ | Node architecture section |

### Priority 2 — Update during Pi 5 build
| Document | Location | Stale content |
|----------|----------|---------------|
| DATABASE_SCHEMA_CANONICAL.md | synthos-company/docs/specs/ | Verify schema still matches deployed DBs |
| TOOL_DEPENDENCY_ARCHITECTURE.md | synthos-company/docs/ | login_server agents should be marked retired |
| FRIDAY_PUSH_RUNBOOK.md | synthos/docs/governance/ | Update for Pi 5 deploy target |

### Priority 3 — Archive before Phase 6
| Document | Location | Action |
|----------|----------|--------|
| login_server/ | synthos-company/ | Move to documentation/archive/ |
| SYNTHOS_TODO_COMBINED.md | if present | Reconcile against current phase plan |
| Any docs referencing `synthos-process` repo | both repos | Mark CANCELLED or remove |
