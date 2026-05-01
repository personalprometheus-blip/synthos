# PROJECT STATUS — Synthos

> **⚠ Historical snapshot (frozen 2026-04-05).** The phase tracker
> below is preserved for audit trail. Current operational truth
> lives in `data/system_architecture.json` (v3.23) and the live
> JSON dashboard at `data/project_status.json`. Retail Pi 5 is
> deployed on NVMe, Phase C refactor + pipeline-audit Gaps 1-3 +
> pre-launch security audit (2026-04-24/25) + 11-agent audit pass
> (2026-04-28 → 2026-05-01) all landed. The stack is in supervised
> Phase 8 paper mode; the entire customer-facing dashboard / news /
> intel pages were overhauled (Phases 5–7L, ~20 commits). For live
> data: `docs/pipeline_audit_2026-04-24.md`, `docs/security_review.md`,
> `docs/trade_lifecycle.md`, `docs/backlog.md`.
>
> **Live phase pointer:** `data/project_status.json` (v2.4,
> 2026-05-01). 11 phases tracked there — phases 1-7L complete,
> phase 8 (Paper Trading Review) in flight.
>
> **2026-04-27 — Trader-visibility audit:** verified Gate 5 actually
> consumes every screener input that was wired in. Re-weighted
> combined_score 40/40/0/20 → 30/30/30/10 so momentum_score finally
> contributes to candidate ranking. ret_3m raw 3-month return now
> persisted + surfaced on screener page + planning drawer. Gate 5
> decision_log emits a single consolidated entry with every
> screener field considered (visibility-only, zero behavior change).
> Documented intentional sentiment dual-write
> (sector_screening.sentiment_score per-ticker vs
> signals.sentiment_score per-signal — same detect_cascade
> computation, two cardinalities). Same day earlier: MRVL trail-stop
> display bug, settlement-lag race in Gate 0, rotation-at-loss
> reversed (winners-only), BIL excluded from Gate 10, CBOE caching +
> None-safe formatting fix (had been pinning every screener-sentiment
> fulfilment to 0.5 since CBOE Cloudflare block), customer-activity
> report engine on cmd portal /customer-activity, P&L report polish.

---

**Last Updated:** 2026-04-05
**Current Phase (at freeze):** Phase 5 complete — Pi 5 retail build pending before Phase 6
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

**Reference:** `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`, `docs/validation/TRUST_GATE_ALIGNMENT_NOTE.md`

---

## Open Blockers (cross-project)

| ID | Repo | Severity | Description |
|----|------|----------|-------------|
| ~~SYS-B01~~ | synthos | ~~CRITICAL~~ | ~~license_validator.py missing~~ — DEFERRED_FROM_CURRENT_BASELINE |
| ~~SYS-B02~~ | synthos | ~~CRITICAL~~ | ~~No license gate in boot_sequence.py~~ — DEFERRED_FROM_CURRENT_BASELINE |
| ~~CL-009~~ | synthos-company | ~~HIGH~~ | ~~Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md~~ — RESOLVED 2026-03-30 |
| ~~CL-012~~ | synthos-company | ~~HIGH~~ | ~~company.db schema undocumented~~ — RESOLVED: docs/specs/DATABASE_SCHEMA_CANONICAL.md |
| ~~MEDIUM-C~~ | synthos | ~~MEDIUM~~ | ~~`sync_to_github` operates on wrong dir, portal uploads never reach GitHub~~ — RESOLVED 2026-04-25 (Phase 7L commit `fe5b3a6`) |

---

## Phase 7+ — Customer Dashboard UX Overhaul ✅ COMPLETE (2026-04-25)

> **One-day work stream.** ~20 commits on main from `2b38efb` through
> `9cd27cf`. Touched portal template, retail_portal.py,
> retail_database.py, retail_news_agent.py, retail_trade_logic_agent.py.
> Net code change: ~2,700 lines, with 4 dead-code paths removed and 8
> punch-list cleanups landed. Live and verified on pi5.

### Sub-phase summary

| Sub-phase | Commit | What landed |
|---|---|---|
| **5** | `2b38efb` | `entry_pattern` column on positions; row badge (MOMO/BRKO/MEAN/PULL/RSRV) + trail-stop% + days held + sector |
| **6** | `8347f06` | Killed Regime Strip (data redundant with Agent Status); planning card upgrade with buy zone / stop / target / thesis line |
| **7a** | `2f43b0a` | Lock chip themed; Bot Active dot on Agent Mode card; history WIN/LOSS reclassified by realized P&L sign; Signal Trust widget replacing internal-score leak |
| **7b** | `f94b2c7` | Lock = circular header-bell-style icon; drawer header = company name + inline sparkline |
| **7c** | `3f4b64d` | New History drawer (outcome strip / entry→exit arc / entry block with frozen thesis / exit block with reason) |
| **7d** | `84d7a1d` | New Approval drawer (Signal Trust + hero headline + buy zone/stop/target + sizing + memo) |
| **7f** | `6a981ad` | Cleanup: removed `openLogicModal` (~60 lines), Cost Basis dedup, ESC-to-close on all drawers |
| **7g** | `2889b32` | Planning drawer (watchlist deep-dive); `/api/ticker-news` endpoint |
| **7h** | `d7280b8` | Tightened ticker-news (fresh-only top-10); new `/api/ticker-context` (live price + ADR + today's range + sector ETF %) |
| **7i+j** | `e7998e9` | News page redesign (Tracked filter + Hide-low-quality toggle + ticker chips); Intel → "Bot Watchlist" rename + drop score leak + route to openPlanningDrawer |
| **7k** | `b5963eb` | Watchlist wiring fix: `/api/watchlist` now reads signals table (was reading news_feed which had MACR/? sentinels) |
| **7L** | `a7a7176`, `fe5b3a6`, `3181fe0`, `3b7ab14`, `9cd27cf` | Punch-list cleanup: openSigModal removed, /api/planning fallback fixed, sizing reads window cache, sync_to_github wrong-dir fix (MEDIUM-C), news dedup tightened, positions.entry_thesis column, computeSignalTrust unified across 4 widgets |

### New schema columns (lazy ALTER, idempotent)

| Table | Column | Purpose |
|---|---|---|
| `positions` | `entry_pattern TEXT` | Gate-6 type (MOMENTUM/BREAKOUT/MEAN_REVERSION/PULLBACK/RESERVE) for the dashboard pattern badge |
| `positions` | `entry_thesis TEXT` | Frozen news headline at entry — works for non-owner customers where `signal_id` resolves NULL |
| `pending_approvals` | `entry_pattern TEXT` | Plumbed through queue→approve→execute so the badge survives the round trip |

### New endpoints

| Endpoint | Returns |
|---|---|
| `/api/ticker-news?ticker=&limit=&since_days=` | Last 10 articles for ticker, freshness-windowed (default 7d). Reads `news_feed` (post-curation; pipeline already filters Tier-4 opinion at gate 3). |
| `/api/ticker-context?ticker=&sector=` | Live current_price, today_open/high/low/pct, 14-day adr_pct, sector_etf + sector_etf_pct. One Alpaca daily-bars call per ticker; cached server-side 60s. |
| `/api/watchlist` (rewired) | Now reads `signals` table via `get_signals_by_status(['WATCHING','QUEUED','VALIDATED'])` — produces ~50 clean curated signals instead of ~30 raw news_feed rows. |
| `/api/planning` (fallback rewired) | Empty-queue fallback now also reads signals table (was news_feed). |

### Visual / UX wins

- **Four specialized slide-out drawers** (Position / History / Approval / Planning) — each with its own DOM, content tuned to context, ESC-to-close, sparkline in header, no DOM reuse hacks.
- **Signal Trust widget** unified — same 5-bar meter / score / bucket-label across sig-modal, position drawer, approval drawer, planning drawer, watchlist cards. Single `computeSignalTrust(obj)` derivation so a trade can't read 75 in one place and 81 in another.
- **Tracker chips** on news + watchlist cards: `📊 IN PORTFOLIO` (teal) when ticker is held, `👁 WATCHING` (cyan) when on bot's watchlist.
- **News-page filters** rebuilt from "category" (100% Markets, useless) → task-oriented (All, Tracked-only, Hide low-quality toggle).
- **"Why we bought"** in History drawer now works for ALL customers (not just owner) thanks to `entry_thesis` snapshot at open time.
- **News dedup** tightened: URL-primary check + 0.55 Jaccard backup + opinion-verb / retread / quote-bait pattern matching catches the celebrity-opinion residue that was sneaking through (Cuban Slams, O'Leary Reveals, Scaramucci's Biggest Mistake).
- **`sync_to_github` MEDIUM-C bug** from the file-upload audit fixed — portal uploads now actually reach GitHub (were silently no-oping in `git add` against the wrong directory).

### Deferred from this work stream

| Item | Why deferred |
|---|---|
| Continuous trade-arc chart on History drawer | Two-dot arc good enough; full chart needs Alpaca daily-bar fetch per drawer-open |
| User memos surviving close into `closed_positions` | Schema add on `positions.user_memo`; not yet a stated need |
| Pattern calibration line on Approval drawer ("bot's last 9 MOMOs: 6W/3L, +2.4%") | Needs ~30d of post-Phase-5 trade data before stats are meaningful |
| Volatility-anchored "Suggested Levels" on Planning drawer | Approved approach (B), not built yet — replaces the rejected generic-percentage version with ATR-based bands |
| User feedback button ("bot got this wrong") | Three flag types, queue-style backlog table — discussed, not built |
| LLM article crawl | Pushed back: cost + latency + dependency outweigh marginal value over current regex filter |



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
