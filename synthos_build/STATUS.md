# PROJECT STATUS

> **⚠ This file is a historical Phase-1-through-5 snapshot (frozen
> 2026-04-05).** It captures the pre-Pi-5-deployment era and the
> blockers that existed then. Everything below is preserved for
> audit trail; none of it is the live state of the system.
>
> **Current state (2026-04-27):** The retail Pi 5 is deployed and
> running on NVMe storage; the trading stack is in Phase 6 paper mode
> with Phase C refactor, full sentiment-chain wiring audit, and
> pre-launch security audit complete. The single source of truth for
> running agents, services, cron schedules, data flow, and known
> issues is:
>
> - `data/system_architecture.json` (v3.13, living document)
> - `docs/pipeline_audit_2026-04-24.md` (producer→consumer trace)
> - `docs/trade_lifecycle.md` (per-trade decision path)
> - `docs/backlog.md` (deferred work with entry conditions)
> - `docs/security_review.md` (pre-launch security roadmap)
>
> **Recent landmark changes not reflected below:**
> - **Phase G pill-usage telemetry (2026-04-27)**: closes the loop
>   on Phases E+F. New `pill_interactions` table on shared
>   user/signals.db captures every drawer/screener pill click
>   (customer_id, pill_type, label, ticker, drawer_kind, page,
>   created_at). 30-day retention sweep. POST `/api/pill-interaction`
>   (customer-side, best-effort) + GET `/api/admin/pill-usage`
>   (Bearer-token, returns 4 rollups). Single document-level click
>   listener with event delegation catches every `[data-pill]` click
>   — keepalive:true + capture-phase so fast navs / stopPropagation
>   don't lose events. Cmd portal `/pill-usage` page renders
>   1d/7d/30d/90d window switcher with by-pill / by-drawer /
>   by-customer rollups so we can prune the unused pill categories
>   based on actual usage instead of guessing.
> - **Portal UX pass — Phases E+F (2026-04-27)**: pills layer landed
>   on every drawer + the screener. Phase E adds a drawer-pills bar
>   at the top of all four detail drawers (Position/History/Approval/
>   Planning) with three pill categories: pipeline stage (HOLDING /
>   QUEUED / WATCHING / VALIDATED / TRACKED / CLOSED — one per drawer
>   kind), multi-signal corroboration (ALL BULLISH / ALL BEARISH /
>   DIVERGENT — fires when news+sent+momentum scores all present),
>   and freshness (FRESH / AGING / STALE — off existing staleness
>   field). Pure visibility, no API/schema changes. Phase F adds
>   gold `.spill` hero pills inline with screener column headers
>   tagging the row that wins each metric (TOP NEWS / TOP SENT /
>   TOP MOM / RANK #1). Telemetry on pill clicks (Phase G) is the
>   next planned step so we can prune the catalogue based on actual
>   usage rather than guessing.
> - **Portal UX pass — Phases A+B (2026-04-27)**: addressed long-standing
>   ticker-identity legibility complaints. Phase A: company name now
>   visible on all six list-view ticker surfaces (dashboard, history,
>   planning queue+intel, approval queue, intel charms) — drawer
>   headers already had it since Phase 7L. Phase B: per-ticker
>   company logos overlay every initials block. New ticker_logos
>   table caches PNG bytes in shared user/signals.db (180KB for 146
>   seed tickers), `/api/ticker-logo/<ticker>` serves with 7-day
>   immutable Cache-Control, mountTickerLogos() lazy-loads via
>   onload/onerror with naturally-graceful 404→initials fallback.
>   Provider pivot mid-build: original plan was Clearbit but
>   logo.clearbit.com was sunset post-HubSpot acquisition (DNS dead);
>   switched to Google's s2/favicons endpoint. tools/populate_logos.py
>   is idempotent + provider-agnostic — re-runnable with
>   --refresh-failed if we swap providers later.
> - **Trader-visibility audit (2026-04-27)**: verified Gate 5 actually
>   consumes every screener input wired into the chain. Three landings:
>   (1) sector_screener.combined_score re-weighted 40/40/0/20 → 30/30/30/10
>   so momentum is included in candidate ranking; (2) ret_3m raw 3-month
>   return now persisted on sector_screening + surfaced on portal screener
>   page + planning drawer (lazy ALTER + calc_momentum_score returning
>   tuple + write_screening_run threading it through); (3) trader
>   gate5_signal_score emits a single consolidated decision_log entry per
>   evaluation containing sector, combined, news, sentiment, momentum,
>   ret_3m, congressional_flag, screener_adj, screener_stamp_adj —
>   after-the-fact audit can verify exactly what the trader saw at
>   decision time. Plus: intentional sentiment dual-write
>   (sector_screening.sentiment_score per-ticker for display vs
>   signals.sentiment_score per-signal for Gate 5) documented inline at
>   the call site. Earlier same day: MRVL trail-stop -491% display bug
>   fix (drop *100 in render path), settlement-lag race in Gate 0 orphan
>   adoption (5-min recently-closed window guard), rotation-at-loss
>   reversed (winners-only per user directive), BIL excluded from Gate 10
>   (sync_bil_reserve owns its lifecycle), CBOE put_call_ratio caching +
>   None-safe formatting (had been pinning every screener-sentiment
>   fulfilment to 0.5 since CBOE Cloudflare block), customer-activity
>   report engine on cmd portal /customer-activity, P&L report polish
>   (total-from-zero, sign rendering, long-term, cross-account).
> - pi5 16GB deployed to NVMe (2026-04-18)
> - pi2w_monitor re-enabled as external watchdog (2026-04-24)
> - Phase C (D1-D6) refactor: trader `run()` 897→15 lines, portal
>   14,294→5,931 lines, `retail_shared.py` consolidation
> - Pipeline audit Gaps 1-3 wired (2026-04-24): validator verdict
>   consumed by Gate 1, `_MARKET_STATE_SCORE` in Gate 5 composite,
>   real fill price via `_resolve_fill_price`
> - Pre-launch security audit (2026-04-24/25): closed 2 CRITICAL +
>   2 HIGH customer→admin escalation paths; added /reset-password
>   handler (forgot-password was broken); persistent login rate
>   limiter (login_attempts table); two-step verify-new-email flow
>   (pending_email_changes table); server-side credential-rotation
>   revocation; Phase 4.5 file-upload audit 21/21 customer-side pass
> - Attribution patch enforcement (2026-04-25): TICKER_REMAP +
>   TICKER_REJECT flipped to True after 4.5-day shadow review
> - Bonus bugfix (2026-04-25): auth.py state/zip_code migration
>   entries had been silently broken since 2026-04-13
> - **Customer Dashboard UX overhaul (2026-04-25, Phases 5–7L,
>   ~20 commits):** Sparklines next to every ticker (`/api/sparklines`
>   + `sparkline_bars` cache, RTH-only 12h window), four specialized
>   slide-out drawers replacing inline expansion + the polymorphic
>   "openLogicModal" reuse hack — Position drawer (company name +
>   sparkline + Trade Info + Signal Trust), History drawer (outcome
>   strip + entry→exit arc + frozen thesis + exit reason), Approval
>   drawer (Signal Trust + hero headline + buy zone/stop/target +
>   sizing + memo), Planning drawer (watchlist deep-dive with Live
>   snapshot + sizing calculator + 10 most-recent freshness-filtered
>   ticker-scoped news articles). Lock chip restyled as circular
>   header-bell-style icon. Bot Active dot on Agent Mode card
>   (green/red/amber). History WIN/LOSS reclassified by realized P&L
>   sign (was: PROTECTIVE-on-stop regardless of outcome). Signal Trust
>   widget (5-bar meter + score + bucket label) replaces the leaked
>   "Synthos Score / Market Alignment / Sentiment Score" trio
>   everywhere. News page: dead Breaking/US/Global filters retired
>   (100% of articles are tagged Markets), replaced with Tracked-
>   tickers filter + Hide-low-quality toggle (URL-primary dedup +
>   tightened 0.55 Jaccard + opinion-verb / retread / quote-bait
>   patterns). Intel page → "Bot Watchlist" rename, drops internal-
>   score leak, click routes to openPlanningDrawer. Watchlist wiring
>   fix: /api/watchlist now reads signals table (was reading news_feed
>   which had MACR/? sentinels). New endpoints: `/api/ticker-news`,
>   `/api/ticker-context` (live price + ADR + today's range + sector
>   ETF %). New schema: `positions.entry_pattern` (Gate-6 type),
>   `positions.entry_thesis` (frozen headline for non-owner customers
>   where signal_id resolves NULL), `pending_approvals.entry_pattern`.
>   `computeSignalTrust()` unified across 4 widgets so the same trade
>   shows the same score everywhere. sync_to_github wrong-dir bug
>   fixed (MEDIUM-C from file-upload audit — uploads were silently
>   never reaching GitHub). News-agent dedup tightened.
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
