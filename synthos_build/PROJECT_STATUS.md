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
  - ✅ MONITOR_URL and MONITOR_TOKEN pre-populated in `env_writer.py` installer template (2026-04-06)
  - Retail Pi setup pending — `MONITOR_URL=http://192.168.203.10:5000`, token pre-filled
- [ ] Verify company startup trust path under normal and break-glass modes
- [ ] Implement retail boot-time license gate — FUTURE_RETAIL_ENTITLEMENT_WORK (deferred from current baseline; see docs/milestones.md)
- [ ] **Backup pipeline hardening — encrypt-on-source + 3-stream split + schedule retail_backup** — DEFERRED (2026-04-18). Plan captured in `synthos-company/documentation/specs/BACKUP_ENCRYPT_AND_SPLIT_PLAN.md`. Closes: plaintext LAN transit of retail backups, co-mingled customer PII / operator config, missing retail_backup cron. Recovery-critical — execute only when current phase stabilizes.
- [ ] **Installer restore UI — post-install "restore from file or R2" page** — DEFERRED (2026-04-18). Companion to backup hardening; tracked in same plan doc (`BACKUP_ENCRYPT_AND_SPLIT_PLAN.md` → Companion task). Depends on new R2 key layout; build after encrypt/split work.

**Reference:** `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`, `docs/validation/TRUST_GATE_ALIGNMENT_NOTE.md`, `synthos-company/documentation/specs/BACKUP_ENCRYPT_AND_SPLIT_PLAN.md`

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

**4. Pi 2W role reassigned — now pi2w_monitor_node.**
Previously retired (old IP 10.0.0.121, old role). Now recommissioned as the dedicated
heartbeat monitor node. Reflashed 2026-04-06. See Addendum below for full setup details.

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

---

## Addendum — pi2w_monitor_node Setup (2026-04-06)

### Node commissioned

| Property | Value |
|---|---|
| Designation | `pi2w_monitor_node` |
| Hardware | Raspberry Pi Zero 2W |
| Hostname | `pi0-2Wmonitor` |
| OS | Debian GNU/Linux 13 (trixie), aarch64 |
| SSH user | `pi-02w` |
| SSH alias | `ssh pi2w_monitor_node` (Mac `~/.ssh/config`) |
| WiFi IP | `192.168.203.10` (DHCP, Akamai network) |
| Network scope | LAN only — no Cloudflare tunnel |
| Service | `synthos_monitor.py` — port 5000 — **not yet installed as systemd service** |

### What was completed 2026-04-06

- Reflashed SD card with new credentials (hostname `pi0-2Wmonitor`, user `pi-02w`)
- Connected via USB ethernet adapter → USB hub → Pi 2W OTG port, tunnelled through pi4b
- Resolved SSH host key warning from reflash
- Installed authorized SSH keys: pi4b (`pi@pi4b`) + Mac (`personal_prometheus@icloud.com`)
- Configured WiFi profiles: `SantaMcGuire` and `Akamai` (both autoconnect)
- Created `~/synthos/.env` with keys from pi4b vault (chmod 600)
- Added `pi2w_monitor_node` SSH alias to Mac `~/.ssh/config`
- Updated `user/.env` (retail template) with correct `MONITOR_URL` and `MONITOR_TOKEN`
- Updated `env_writer.py` installer template with pre-filled monitor node values and comments
- Updated `MEMORY.md` with node naming convention and network switch future planning note

### .env on pi2w_monitor_node (`~/synthos/.env`)

```
PORT=5000
SECRET_TOKEN=synthos-default-token        # must match MONITOR_TOKEN on retail Pis
RESEND_API_KEY=re_NwsJo4Yh_...            # from pi4b vault
ALERT_FROM=Synth_Alerts@synth-cloud.com
ALERT_TO=personal_prometheus@icloud.com
COMPANY_URL=http://192.168.206.172:5010   # pi4b company server
```

### Retail Pi integration — pending

When retail Pi is set up, ensure its `.env` contains:

```
MONITOR_URL=http://192.168.203.10:5000   # pi2w_monitor_node WiFi IP (DHCP — update on switch install)
MONITOR_TOKEN=synthos-default-token      # must match SECRET_TOKEN above
```

Both values are now pre-filled in `installers/common/env_writer.py` and `user/.env`.

### Remaining tasks before monitor is fully operational

- [ ] Deploy `synthos_monitor.py` to `~/synthos/` on pi2w_monitor_node
- [ ] Install as systemd service (`synthos-monitor.service`)
- [ ] Verify retail Pi heartbeat POSTs reach `http://192.168.203.10:5000/heartbeat`
- [ ] **IP finalization** — when ethernet switch is installed, assign static IPs and update
      `MONITOR_URL` on all retail Pis and `COMPANY_URL` on pi2w_monitor_node
      (see MEMORY.md — Future Planning Notes for full checklist)

### Node naming convention (established 2026-04-06)

All physical Pi nodes are named `<model>_<role>`. When Patrick references a node by model
shorthand ("the 2W", "the 4B", "the 5"), map to full designation.

| Designation | Hardware | Status |
|---|---|---|
| `pi4b` | Raspberry Pi 4B | ✅ Live — company server |
| `pi2w_monitor_node` | Raspberry Pi Zero 2W | ✅ Live — monitor node |
| `pi5` (TBD) | Raspberry Pi 5 | 🔲 Pending delivery — retail node |
