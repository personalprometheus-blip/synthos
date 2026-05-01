# Synthos TODO

> Shared todo list — editable in Obsidian, git-tracked, used by Claude for context.
> **Checkbox syntax**: `- [ ]` pending, `- [x]` done. Feel free to reorder, add, or delete.
> Last sync: 2026-05-01 (post-scoop-rebuild + EDGAR enable + branch sweep + server cleanup)

---

## 🔥 Active this week

- [ ] **Pre-trip SD mitigations** (before user is away 3 weeks):
	- [ ] Snapshot pi4b `.env` + systemd units to safe location
	- [ ] Take full SD image → R2 (`dd | gzip | rclone`)
	- [ ] Verify pi5 has independent R2 backup path (not dependent on pi4b strongbox)
	- [ ] Confirm `daily_master` email actually sends (visibility signal during absence) — `retail_daily_master.py` confirmed present at `synthos_build/src/`; logs dir exists on pi5; only the email-deliverability needs a single-shot test
- [ ] **SD card swap on pi4b** — waiting on powered USB hub delivery. Attempt this weekend if hardware arrives.
- [ ] **Watch attribution patch behavior** — TICKER_REMAP + TICKER_REJECT enforcement now ON (2026-04-25). Sample real-world enforced rejects weekly to make sure no false positives are blocking legitimate signals.
- [ ] **Verify stop-loss behavior 15:00-16:00 ET window** — `LATE_DAY_TIGHTEN_PCT=0.0` deployed 2026-04-21 (4+ trading days now). Pull `exit_performance` to confirm the 15:00-16:00 bucket no longer shows the breakeven-noise pattern.

## 🟡 Auditor findings — kept visible by design

After 2026-04-25 triage sweep (50 → 4 open). Deferred items remaining:

- [ ] **#270 Stripe webhook secret unset** (HIGH, x3 hits) — Phase 8 task. Either wire up `STRIPE_WEBHOOK_SECRET` or firewall the endpoint when not in use.
- [ ] **#249 / #255 Trader 240s timeout** (HIGH, x1 each) — customers `f313a3d9` (2026-04-22) and `80419c9e` (2026-04-24). Once each, days apart. Monitor for recurrence; investigate if a pattern emerges.
- [ ] **#256 NEGATIVE_CASH owner $-0.01** (CRITICAL, x1) — paper-trade rounding artifact on owner customer (30eff008). Will self-clear on next portfolio reconcile/deposit. No action.

## 🟢 2026-05-01 afternoon session — completed

- [x] **Scoop notification pipeline rebuilt** — diagnosed 3 structural bugs (schema mismatch between strongbox writer + scoop drain; one-shot drain at startup; dead-end portal-dispatch auth). Built `agents/_shared_scoop.py` as single enqueue source of truth. Refactored strongbox + sentinel writers to use it. Removed dead `vault._trigger_scoop`. Made scoop's drain re-pollable + dual-schema (accepts both old `delivered:false` + new `status:pending`). Triple-check passed: 3 emails delivered to `personal_prometheus@icloud.com` end-to-end. Scoop is back in business after 11 silent days.
- [x] **EDGAR ingestion enabled on pi5** — `SEC_EDGAR_UA_NAME` + `SEC_EDGAR_UA_EMAIL` set; `EDGAR_ALL_ENABLED=true`; live Form 4 fetch verified (1 actionable signal in 10 filings). `data/activists.json` seeded with 12 activists (Pershing, Icahn, ValueAct, Starboard, Third Point, Trian, JANA, Elliott, Engine, Engaged, Cevian, Land & Buildings). 11 entries marked `PENDING_OPERATOR_VERIFICATION` for spot-check at edgar.sec.gov (failure mode is silent miss, not false positive).
- [x] **Stale branch + server cleanup** — synthos 41 → 3 branches, synthos-company 10 → 1, origin remotes also swept. `TRADER_RESTRUCTURE_PLAN.md` archived under `documentation/archive/specs/` before its source branch deletion. Server cleanup: removed pi4b synthos-login.service / `/home/pi/synthos_build/` / `/home/pi/synthos-process/`; removed pi5 synthos-portal.service.bak; archived `synthos-company/login_server/` → `documentation/archive/login_server/`.
- [x] **System-architecture pipeline page deferred-data-sources panel** — added new `deferred` gate flag (purple ⏸, distinct from amber ⚠ scaffolding) for News G12/G14/G15 + Sentiment G5/G7/G8/G10/G16. New 'Deferred Data Sources' panel below pipeline grid summarizes paid-tier gaps (StockTwits / X / Reddit social, NYSE TIQ A/D, options chain VVIX, ICE BofA credit, FMP /etf-holdings) and formally-deferred future phases (license validator SYS-B01/B02, Patch D-full, Phase 7e bucket, Congressional weighting).
- [x] **System-architecture pipeline page accuracy fix** — re-read agent code; removed scaffold flags from all 6 bias gates (sample-size guards aren't stubs), macro G4 (sector rotation, fully implemented), sentiment G15 (5-pattern divergence detector), screener G1 (hand-curated holdings work fine). Macro gate names corrected (page had wrong order — G1 was actually VIX not Yield curve, etc.).

## 🔮 Queued for after market close (16:00 ET, 2026-05-01)

- [ ] **Portal-dispatch service-token auth** — add X-Service-Token check to `synthos_build/src/retail_portal.py`'s `/api/notifications/send` + `/api/notifications/broadcast`. Then set `PORTAL_TOKEN=$MONITOR_TOKEN` in pi4b's `company.env`, restart scoop, in-app notifications light up. ~30 min.
- [ ] **Customer account-deletion feature** — settings page; confirm dialog + reason capture; design first (hard delete vs deactivate, Stripe cancel, retention period, open positions handling, GDPR). User-requested earlier this session.
- [ ] **News-agent duplicate flag writes fix** (TODO line 85) — verified still pending; no dedup pattern detected in `retail_news_agent.py`. Article processed via signal + display paths still writes duplicate flag rows.
- [ ] **`TICKER_ALIASES` grow** (TODO line 86) — review recent `[TICKER_REJECT]` log lines, add observed mega-caps.
- [ ] **`get_watching_signals()` removal** — legacy function, only test code references it. Update test, delete function. ~15 min.
- [ ] **`check_email.html` theming** — `📬` emoji + dark-card vs SYNTHOS visual language mismatch.

## 🟢 11-Agent Audit Pass — completed 2026-04-28 → 2026-05-01

Multi-week audit of every agent + the validator stack. All deploys live on pi5.

- [x] **Agent 1 Trader** — 13-gate spine documented; G13 stress overrides activated; G5.5 news veto wired; VIX integration via `_vix_from_macro_regime` (commit `32a7d19`); per-customer dedup keys with `_<short_id>` suffix.
- [x] **Agent 2 News** — 22-gate spine; risk discounts (G18) now read VIX/macro from signals.db instead of recomputing; admin_alert dedup via `retail_shared.emit_admin_alert()`.
- [x] **Agent 3 Sentiment** — Stage 4 G24 temporal persistence (must persist 2 cycles) added; ATR fallback when FRED VIX is stale.
- [x] **Agent 5 Sector Screener** — admin_alert dedup unified; `_short_id` keying; visual styling on `/system-architecture` pipeline page made consistent with other agent cards (2026-05-01).
- [x] **Agent 6 Fault Detection** — 8-gate spine confirmed (was previously documented as 4 in arch JSON); per-customer Gate 5 added so customer-A's Alpaca 401 no longer leaks into customer-B's validator (Round 9 hot-fix `7d1dd04`, `_finding_applies_to_customer()` filter).
- [x] **Agent 7 Bias Detection** — `INFORMATIONAL_BIAS_CODE_PREFIXES` tuple + `_is_informational_bias_code()` to handle per-customer code suffixes.
- [x] **Agent 8 Macro Regime** — VIX migrated to FRED VIXCLS; yield curve uses DGS10 − DGS3MO from FRED; macro_regime_detail JSON persisted with stale-window guard.
- [x] **Agent 9 Market State** — 4-gate spine (was previously documented as 5); macro stale-hours guard before synthesis.
- [x] **Agent 10 Validator Stack** — per-customer suffix on VALIDATOR_NO_GO codes; bias informational-prefix filtering; cross-customer fault-leakage closed.
- [x] **System Map (`/system-architecture`)** — pipeline page now matches reality: gate counts corrected (Fault 4→8, Market State 5→4, Trader 14→13), foreign data sources annotated (FRED / Alpaca / Yahoo / News APIs), scaffolding gates flagged in amber, click-to-detail wired on every gate + agent header, Sector Screener restyled into the ingest column.

## 🆕 Active follow-ups from 2026-04-25 dashboard sprint

- [ ] **Monday 09:25 ET — verify Phase 7 work fires correctly** on first live trader run:
	- [ ] history WIN/LOSS reclassification on first trail-stop win
	- [ ] `entry_pattern` populates on next-opened position (Phase 5 lazy ALTER triggers)
	- [ ] `entry_thesis` populates on next-opened position (Phase 7L)
	- [ ] Planning drawer Live Snapshot loads ADR + sector ETF on real position
	- [x] News tracker chip appears on ticker-attached articles after dashboard polls run — verified 2026-04-28 (`trackerChip()` at portal.html:6413, wired into News page line 7597, positions/planning panels lines 6618/6656, watchlist line 7555). Per-article chip in planning drawer's "Recent news" section deliberately not added — drawer is ticker-specific, chip would be redundant on every card.
- [ ] **Phase 7e deferred bucket** (~3-5h total when scheduled):
	- [ ] Continuous trade-arc chart on History drawer (replaces 2-dot arc; needs Alpaca daily-bar fetch on drawer-open)
	- [ ] User memos surviving close into `closed_positions` (schema add `user_memo TEXT`)
	- [ ] Volatility-anchored "Suggested Levels" on Planning drawer (ATR-based bands, replaces rejected generic-percentage version) — Option B, approved
	- [ ] Feedback button ("bot got this wrong") with three flag types — discussed, queue-style backlog table
- [ ] **Pattern-calibration line on Approval drawer** — needs ~30d post-Phase-5 trade data before stats are meaningful (calendar item, eligible ~2026-05-25)

### Documented but not-fixed (residue from Phase 7L recap, intentional)

- `drawer-ticker` element ID is misnamed (holds company name) — comment in code, rename has search/replace risk
- `get_watching_signals()` legacy function still defined — no production callers after 7k/7L; tests still call it for backwards-compat smoke. Removable after the test is updated.

## 📱 Retail Portal

### Template extraction — VERIFIED ESSENTIALLY DONE 2026-04-25

✅ All public/customer-facing pages extracted: `/terms`, `/login`, `/logout`, `/subscribe`, `/check-email`, `/activate/<token>` (→ setup_account / verify_success / verify_error), `/notifications`, `/news`, `/screening`, `/performance`, `/settings`, `/dashboard` (`/`). 18 templates in `src/templates/`. Zero top-level `*_HTML` constants in retail_portal.py.

- [ ] **`/logs` admin page** — only remaining inline HTML in retail_portal.py (~100 lines in `logs_page()` at line 5441). Admin-only, low priority. Extract when convenient.

### Small portal issues to fix (verified 2026-04-25)

- [ ] **`check_email.html` theming** — verified `📬` emoji still present (line: `<div class="icon">📬</div>`). Page theme also still generic dark-card vs newer SYNTHOS visual language. Not yet addressed.
- [ ] **Portal shared-CSS consolidation** — verified `src/static/css/` directory does NOT exist. `@font-face` rules and `.pw-eye` block still copy-pasted. Defer until next DRY pass.
- [ ] **Password-toggle eye icons bug** — verified `.pw-eye` / `.pw-wrap` CSS not present in any template currently. The original templates that had the bug may have been replaced during extraction. Re-test on `signup.html` and `setup_account.html` post-extraction; could already be moot.

### Mobile wave experience (spec from 2026-04-21 evening)

- [ ] No patch branch yet. All sub-items still pending:
	- [ ] Cut patch branch `patch/YYYY-MM-DD-mobile-wave` when ready to build
	- [ ] Client-side viewport detection (≤768px)
	- [ ] Fullscreen wave view on mobile login → Exit Wave pill (bottom-center, auto-hide after 3-4s of no touch)
	- [ ] Desktop: condensed wave card + triangle toggle + Enter Wave pill
	- [ ] PWA manifest + icons + iOS standalone meta tags
	- [ ] Smooth exit transition (crossfade or slide-up)
	- [ ] Landscape: naive stretch first, revisit after testing

## 🖥️ Command Portal

✅ ROI-deferred 2026-04-25. `synthos_monitor.py` is 9,194 lines with ~1,952 lines of inline HTML (21%, 7 constants). Only used by admin (Patrick), no observed bugs. Not extracting unless specific edit-pain emerges.

- [ ] *(reactivate when admin-portal editing actually hurts)*
- [x] **Sector Screener visual styling on system architecture page** — fixed 2026-05-01. Now renders through the same `agentCard` path as other ingest agents with a small "PRE-COMPUTED · 06:45 ET" pill above it. Click-to-detail also wired.

## 🧠 Trading Logic

### Attribution patch followups (from 2026-04-21 deploy)

- [x] **2026-04-28 enforcement review** — VERIFIED done early on 2026-04-25: `TICKER_REMAP_ENFORCE=True`, `TICKER_REJECT_ENFORCE=True` flipped after 4.5-day shadow review.
- [ ] **Fix duplicate flag writes** — verified still pending; no dedup pattern detected in `retail_news_agent.py`. Article processed via signal + display paths still writes duplicate flag rows.
- [ ] Grow `TICKER_ALIASES` dict based on observed `[TICKER_REJECT]` logs (currently ~30 mega-caps).

### Late-day tightening follow-up

- [ ] **Re-evaluate in 2-4 weeks** — once `exit_performance` has 20+ exits for the optimizer. Decide whether to keep `LATE_DAY_TIGHTEN_PCT=0.0` or move to Option 3 (conditional: only tighten if `gain_pct > 0.01`). Calendar: ~2026-05-05.
- [x] **`LATE_DAY_TIGHTEN_PCT=0.0` resolved-items entry to `system_architecture.json`** — VERIFIED present in arch.json under "Late-day stop tightening disabled" entry.

### AUTO/USER per-position management

- [x] **Feature merged & shipped to main** — VERIFIED 2026-04-25. Multiple commits on main reference the feature. `managed_by` column populated, `sticky=user` mechanism live, lock chip + ticker-preferences UI in production. Branch `patch/2026-05-03-auto-user-tagging` could be cleaned up.

### Other trading-logic items (design decisions / future)

- [ ] Separate premarket from overnight cycle (future refactor — noted as TODO in code)
- [ ] News-triggered sentiment re-run (Q1 from earlier design — speculative)
- [ ] **Congressional signal → combined_score** — design decision still open. `congressional_flag` displayed in sector_screening but not weighted into `combined_score`. Should it modify the score?

### SYSTEM_MANIFEST.md rewrite-or-retire decision

- [ ] **Decide fate of `synthos-company/documentation/specs/SYSTEM_MANIFEST.md`** — stamped OUTDATED on 2026-04-23. Two paths:
	1. **Rewrite as v6.0** reflecting actual architecture — 2-3h focused work
	2. **Retire** and redistribute unique content (ENV_SCHEMA, UPGRADE_RULES, DEPENDENCY_GRAPH, SYSTEM_PATHS) into smaller focused docs
- 136 cross-references exist; either path needs a redirect/update sweep.

### Operational stale cleanup (verified 2026-04-25)

| Item | Verified state | Action |
|---|---|---|
| pi4b: `company_scoop.py` decision | Service unit no longer exists on pi4b (`Unit synthos-scoop.service could not be found`). `.py` file may still exist; need to decide retire-and-delete vs revive-as-systemd | retire decision |
| pi4b: `synthos-login.service` | **Still present** (`/etc/systemd/system/synthos-login.service`, disabled+inactive). Pending removal. | remove |
| pi4b: `/home/pi/synthos_build/` | **Still present** (empty skeleton from 2026-04-08) | remove |
| pi4b: `/home/pi/synthos-process/` | **Still present** (10 entries from cancelled process_node) | remove |
| pi5: `synthos-portal.service.bak.20260418_081448` | **Still present** in `/etc/systemd/system/` | remove |
| pi4b: `synthos-company/login_server/` directory | **Still present** | move to documentation/archive/ |

### Stale branch cleanup (newly surfaced 2026-04-25)

- [ ] **4 stale `claude/*` branches** + **multiple stale `patch/2026-04-19-*` branches** still hanging around. Last cleanup was 2026-04-21 (cleaned 9 + 4 worktrees); a fresh sweep would clear another batch.

## 🏗️ Infrastructure

- [ ] **pi4b USB SSD install** (hardware on hand, blocker: powered USB hub delivery)
- [ ] **Full disaster recovery drill** — restore from R2 + verify trading continuity
- [ ] **pi5-expansion prep**: `ASSIGNED_NODE` customer setting for multi-node routing — verified NOT implemented (no references in codebase)
- [ ] **pi5-expansion decision**: enrichment master pattern vs PostgreSQL-first (Phase 8)

## 🧱 Architectural cleanup (deferred)

- [ ] **Patch D-full — split shared/customer DB schema**

  **Status:** D-rows landed 2026-04-27 (deleted ~58k orphaned rows from
  customer DBs). Tables themselves still get created on every customer
  DB open via `CREATE TABLE IF NOT EXISTS` in the canonical SCHEMA, so
  every customer DB still has 19 always-empty shared tables.

  **What D-full does:**
  Split `retail_database.SCHEMA` into `_SCHEMA_BASE` (tables present in
  both contexts — telemetry: `scan_log`, `system_log`, `api_calls`, plus
  customer-specific tables) and `_SCHEMA_SHARED_ONLY` (the 19 tables
  cleaned in D-rows: `signals`, `news_feed`, `news_flags`, etc.).
  Same split for the `MIGRATIONS` list. Add `is_customer: bool = False`
  to `DB.__init__`; route `get_customer_db()` with `is_customer=True`
  and `get_shared_db()` with the default. Conditionally apply the
  shared-only schema. One-time DROP migration for is_customer=True
  removes the legacy empty tables on existing customer DBs.

  **Why deferred (not done at the same time as D-rows):**
  Touches `DB.__init__` which runs on every customer-DB open (every
  portal API call, every trader cycle, every fault-detection scan).
  A misclassified table — categorizing something shared that turns out
  to need per-customer rows, or vice versa — silently breaks the wrong
  half of the system. The benefit of D-full is mostly cosmetic
  (cleaner DB browser, slightly less disk per customer) so it doesn't
  justify deploying the change late at night with a trader run firing
  09:25 ET the next morning. Land it on a Sunday with attention.

  **Entry conditions:**
  1. ≥7 days post-Patch-A with the `news_dedup_scanner` showing 0
     hard-dups every hour. Confirms no missed code path is still
     writing to any customer-DB shared table — if it were, the data
     would be silently dropped on D-rows cleanup runs.
  2. Tier readout / system-health-daily aggregation confirmed running
     against `user/signals.db` for ≥1 week.
  3. Test-customer creation flow exercised post-Patch-A — confirms
     `get_customer_db(<new_uuid>)` produces a working DB without
     touching shared tables. Today this is enforced by `_db()` routing,
     but D-full would enforce it at the schema level.

  **Scope:**
  - `synthos_build/src/retail_database.py` — split SCHEMA + MIGRATIONS,
    add `is_customer` flag (~80 lines net)
  - `get_customer_db()` passes `is_customer=True`
  - One-time DROP migration: `_drop_legacy_shared_tables_if_customer()`
    fires once per customer DB, idempotent
  - Smoke test: test-customer signup, trader dispatch on existing
    customer, news_agent on shared DB — all work

  **Risk:**
  - HIGH if rolled out without watching: trader's `_db()` call resolves
    via per-customer DB; if its schema is missing a table the trader
    expected, exception cascade.
  - MEDIUM with smoke + staged rollout: the schema split is mechanical
    but classification errors are subtle.

  **Mitigation if pursued:**
  - Land in a worktree first, dry-run against a copy of pi5's data dir
  - Run trader + news_agent + portal against the new schema split in dry
    mode for ≥1 hour before enabling in production
  - Keep the un-split SCHEMA constant under a feature flag for fast
    revert

## 📦 Deferred / Next Phase

- [ ] Backup pipeline hardening — encrypt-on-source + 3-stream split + retail_backup schedule. Plan: `synthos-company/documentation/specs/BACKUP_ENCRYPT_AND_SPLIT_PLAN.md`
- [ ] Installer restore UI — post-install "restore from file or R2" page. Companion to backup hardening.
- [ ] Phase 6 gate conditions — trading mode, boot-time integrity, retail license gate (see `PROJECT_STATUS.md`)
- [ ] C8 news-agent gate-pipeline refactor — baseline harness is on `patch/2026-04-24`, actual refactor is future work

## ✅ Recently completed (last 7 days)

### 2026-04-25 — Retail logs-audit triage (200+ → 27, then ~5-10 after 72h aging)

Separate from the company auditor.db cleanup below — `/api/logs-audit`
on the retail portal is a LIVE re-scan of pi5's log files on every
page load, with no persistent resolution state. Two structural fixes
+ five rounds of IGNORE-pattern expansion brought it down from 200+
findings to 27.

- [x] **72h age filter** (`d6203dd`) — only surface log lines whose
  parsed timestamp falls within the last 72 hours. Prevents stale
  one-off lines from accumulating forever; real recurring bugs
  reappear within a single trading day.
- [x] **IGNORE pattern expansion** across multiple commits
  (`d6203dd`, `d8db2c8`, `de07d94`, `b4d9d86`):
  - Yahoo / circuit-breaker retry warnings
  - `WARNING price_poller: Market-data fallback returned 400`
    (after-hours SIP/IEX restriction noise — was 162 hits/scan)
  - `WARNING price_poller: Alpaca <CID> fetch failed` /
    `Market-data fallback fetch failed` (paper-API SSL + timeout
    flakiness)
  - `[KEYS] Customer X attempted to write` (auth gate blocking;
    security accounting working as designed)
  - `[ADMIN_OVERRIDE] POST denied` (same family)
  - `WARNING watchdog: Interrogation not running` (watchdog auto-
    restarting the listener — recovery action working)
  - `[HB] POST failed: ... Max retries exceeded` (heartbeat retries
    on its next tick; company_sentinel handles sustained silence)
- [x] **Net effect** — retail audit went from "200 hits, mostly
  noise" to "27 hits, mostly historical from before today's fixes
  deployed (boot.log + market_daemon.log)". Those 17 historical
  entries naturally age out within 72h leaving ~10 real items
  (Stripe webhook secret + trader timeouts × 2 + a few outliers).

### 2026-04-25 — Auditor triage sweep (50 → 4 open findings)

- [x] **`run_window_calculator` NameError on every overnight cycle** (`183bb68`) — agent retired in Phase C but two call sites in retail_market_daemon.py weren't cleaned up. Removed both. Auditor #261/#262/#282/#283 resolved.
- [x] **synthos-watchdog 'inactive' false-positive at boot** (`183bb68`) — `_check_systemd_service` now polls `is-active` for up to 15s, treating 'activating'/'inactive' as transient. Boot-race between synthos-boot-sequence.service and synthos-watchdog.service no longer logs ERROR. Auditor #260/#281/#258/#279 resolved.
- [x] **Test-customer debris on auditor** (`183bb68`) — `customer_health_check.py` now cross-references on-disk customer dirs against `auth.db` and silently skips dirs whose cid isn't an active customer. Smoke-test customers no longer generate MISSING_DB / NO_SETTINGS findings every audit run. 23 finding rows resolved.
- [x] **pi2w_monitor unreachable findings stuck** (`018bdb4` synthos-company) — auditor's existing `disabled:True` skip logic prevented NEW findings but left historical ones unresolved. Now also auto-resolves any `<node>::unreachable` row when iterating a disabled node. Idempotent. Auditor #243 resolved.
- [x] **Bulk-resolved 28 stale findings via direct SQL** to auditor.db (test-debris × 23 + pi2w_disabled × 1 + transient-from-portal-restart × 4).
- [x] **Bulk-resolved 18 post-deploy findings** (4 window_calc + 2 watchdog + 2 service_down + 10 price_poller WARNING noise — circuit breaker mitigated, warnings are diagnostic only).
- Result: 50 → 4 open findings; remaining are real items kept visible by design (Stripe webhook secret, trader timeouts × 2, NEGATIVE_CASH penny — all annotated in the new "Auditor findings — kept visible by design" section above).

### 2026-04-25 — Customer Dashboard UX Overhaul (Phases 5–7L, ~20 commits)

- [x] **Phase 5** (`2b38efb`) — `entry_pattern` column on positions, row badge + trail-stop% + days held + sector
- [x] **Phase 6** (`8347f06`) — kill Regime Strip, planning card upgrade with buy zone/stop/target/thesis
- [x] **Phase 7a** (`2f43b0a`) — lock chip, Bot Active dot, history WIN/LOSS reclass, Signal Trust widget
- [x] **Phase 7b** (`f94b2c7`) — circular lock icon, drawer header = company name + sparkline
- [x] **Phase 7c** (`3f4b64d`) — new History drawer (replaces openLogicModal hack)
- [x] **Phase 7d** (`84d7a1d`) — new Approval drawer (replaces inline expansion)
- [x] **Phase 7f** (`6a981ad`) — cleanup: openLogicModal removed, Cost Basis dedup, ESC-to-close on all drawers
- [x] **Phase 7g** (`2889b32`) — Planning drawer (watchlist deep-dive); `/api/ticker-news` endpoint
- [x] **Phase 7h** (`d7280b8`) — fresh-only ticker-news; new `/api/ticker-context` (live price + ADR + sector ETF)
- [x] **Phase 7i+j** (`e7998e9`, `bbc6feb`) — News page redesign (Tracked + Hide-low-quality + ticker chips); Intel → Bot Watchlist
- [x] **Phase 7i fixup** (`4f214c0`) — News filters re-render on tracker-update; expanded clickbait regex (10% flag rate on real data)
- [x] **Phase 7k** (`b5963eb`) — Watchlist wiring fix: `/api/watchlist` reads signals table not news_feed
- [x] **Phase 7L** (`a7a7176`, `fe5b3a6`, `3181fe0`, `3b7ab14`, `9cd27cf`) — punch-list cleanup: openSigModal removed, /api/planning fallback fixed, sizing reads window cache, sync_to_github wrong-dir fix (MEDIUM-C audit), news dedup tightened, positions.entry_thesis column, computeSignalTrust unified
- [x] Status sync (`f2c625f` synthos / `a47238a` synthos-company) — all 6 status files refreshed for the dashboard overhaul

### 2026-04-24/25 — Pre-launch security audit + attribution enforcement

- [x] CRITICAL-1 closed: /api/admin-override admin gate
- [x] CRITICAL-2 closed: /api/keys admin gate
- [x] HIGH-1 closed: /reset-password handler added (forgot-password was broken)
- [x] HIGH-3 closed: /api/audit promoted to @admin_required
- [x] Persistent login rate limiter (login_attempts table) — 10 fails/15min per account, 20/5min per IP
- [x] Two-step verify-new-email flow (pending_email_changes table)
- [x] Password min-length standardized 8→12 (OWASP 2023)
- [x] auth.py state/zip_code migration bug (silently broken since 2026-04-13)
- [x] Phase 4.5 file-upload audit: 21/21 customer-side probes pass
- [x] **Attribution patch enforcement: TICKER_REMAP + TICKER_REJECT flipped True after 4.5-day shadow** — VERIFIED in news agent

### 2026-04-25 — Items previously listed as pending but verified DONE

- [x] **Retail portal template extraction** — verified essentially complete (12 of 12 listed pages now in `src/templates/`; only `/logs` admin page remains)
- [x] **AUTO/USER per-position management feature** — verified merged to main (managed_by + sticky=user + lock chip all in production)
- [x] **`LATE_DAY_TIGHTEN_PCT=0.0` to architecture.json** — verified present in arch.json
- [x] **2026-04-28 enforcement review** — verified completed early (2026-04-25)
- [x] **company_scoop.py service unit** — verified removed from pi4b (`Unit synthos-scoop.service could not be found`); `.py` retire-vs-revive decision still open

### 2026-04-21 (week of)

- [x] News attribution patch (Fix A enforced, Fix C shadow, company populate, flag table)
- [x] `LATE_DAY_TIGHTEN_PCT=0.0` disabled on pi5
- [x] Enrichment pipeline reconstruction (timer 09:10→09:15, overnight cycle completion, standalone price_poller timer, pre-market self-check)
- [x] Candidate-generator filter `combined_score` → `momentum_score` (threshold 0.45)
- [x] `_MARKET_STATE_UPDATED` key-name fix (validator degradation)
- [x] Second sector_screener pass at market close (2×/day)
- [x] Cleaned 9 stale `claude/*` branches + 4 orphan worktrees

---

## 🔗 Reference docs

- `PROJECT_STATUS.md` — phased roadmap, gate conditions
- `synthos_build/data/system_architecture.json` — live system map (v3.24)
- `synthos_build/data/project_status.json` — live JSON dashboard (v2.5)
- `docs/` — spec archive

## Workflow conventions

- **Patch branches**: `patch/YYYY-MM-DD-short-name` off main. Never commit to main directly.
- **Always**: `py_compile` check → dry-run if possible → click-test on pi5 before merging to main.
- **Deploy chain**: edit on Mac → commit → push to GitHub → `git pull` on Pi → restart service → verify.
- **Never**: push during market hours (09:30-16:00 ET) unless the change is trading-critical.
