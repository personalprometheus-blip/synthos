# Synthos Retail — Master Plan

Single-file tracker for every Phase, Round, and open item. Update the
check-marks as work lands.

**Legend**
- `[x]` done (merged to main)
- `[ ]` pending (scoped, not started)
- `[~]` deferred (with explicit trigger for when to revisit)
- `[-]` in progress

_Last updated: 2026-04-20 (end of Phase B re-run)_

---

## A. TRADER_RESTRUCTURE_PLAN — Phases 1–5

Original multi-phase plan that converted the trader from news-triggered
batch runs to a continuous window-driven daemon.

### A.1 — Phase 1: split trade_daemon from market_daemon
- [x] New `retail_trade_daemon.py` (continuous 30s cycle, market hours)
- [x] Market daemon retained only for enrichment (30min ticks)
- [x] Systemd units + timers for both (market 9:10, trade 9:25 Mon-Fri)
- [x] Watchdog integration + independent heartbeat
- [x] Trade daemon timer currently **disabled** pending Round 9 fixes

### A.2 — Phase 2: news_flags infrastructure
- [x] `news_flags` table + TTL + helpers in `retail_database.py`
- [x] `retail_news_agent` writes catalyst flags during enrichment
- [x] Fresh-flag reader for downstream gates
- [x] Circuit-breaker + cache fast-path fixes during bug hunt

### A.3 — Phase 3a: news integration at trader gates
- [x] Gate 4 EVENT_RISK reads news_flags (earnings/regulatory/litigation)
- [x] Gate 5 composite includes `news_flags_mod` (±0.2 clamp)
- [x] Gate 5.5 NEWS_VETO (severe-negative block)
- [x] Smoke-verified: ARCC/BIL/CADL/STT/CMND @ +0.600 catalyst

### A.4 — Phase 3b: window calculator + candidate generator
- [x] `trade_windows` table (macro + minor tiers, TTL, PK)
- [x] `retail_window_calculator.py` — enrichment + refresh modes
  (enrichment mode shipped; refresh mode stubbed for Phase 4+)
- [x] `retail_candidate_generator.py` — sector-driven WATCHING signals
- [x] Price poller extended to poll WATCHING tickers
- [x] Market-data `/v2/stocks/trades/latest` fallback for unheld tickers

### A.5 — Phase 3c: trader cutover
- [x] 3c.a — Gate 5 window_proximity_adj (up to +0.04 deep-band nudge)
- [x] 3c.b — Trader reads macro windows + live_prices → fires on in-band
- [x] 3c.b — v1 news-triggered entry path deleted (main loop + rotation)
- [x] 3c.b — `get_fresh_macro_windows()` helper added
- [x] 3c.c — Triple-check verified

### A.6 — Phase 4: ATR stops + sizing
- [x] 4.a — `atr` column on trade_windows + ATR fetcher
- [x] 4.b — Macro/minor bands scale with ATR (bias retained)
- [x] 4.c — Stop = anchor − 2.0 × ATR (bundled into 4.b)
- [x] 4.d — Trader reads ATR from window (single source of truth across window_calc + Gate 7 + Gate 8)

### A.7 — Phase 5: event calendar + cooling-off + daily rollup
- [x] 5.a — Gate 4 EVENT_RISK scheduled-event calendar
  - `earnings_cache` (Nasdaq bulk fetch, 7-day TTL)
  - `macro_events` (manual FOMC/CPI/NFP schedule table)
  - Gate 4 blocks entry within `EVENT_CALENDAR_WINDOW_DAYS` (default 2)
- [x] 5.b — Cooling-off after stop-out
  - `cooling_off` table + register/read helpers
  - `close_position()` auto-registers losses (24h default)
  - Gate 4 `4_COOLING_OFF` sub-gate
- [x] 5.c — `retail_daily_master.py` end-of-day Markdown rollup
  (opens/closes/P&L/candidates/cooling-off/upcoming earnings)
- [x] 5.d — Triple-check + smoke

---

## B. Audit Rounds — post-Phase-5 cleanup

Spawned when we paused trading after observing the AAPL runaway.

### B.1 — Round 1: 5 CRITICAL bugs (the runaway)
- [x] 1.1 — `import time` missing in sector_screener (NameError risk)
- [x] 1.2 — `timezone` missing in trade_logic_agent (2 sites)
- [x] 1.3 — Out-of-scope `trade_events` in `_rotate_positions`
- [x] 1.4 — Candidate-gen `tx_date` dedup broken (AAPL runaway root cause 1)
- [x] 1.5 — Gate 4 ticker dedup missing (runaway root cause 2)

### B.2 — Round 2: re-enable with confidence
- [x] Systemd trade-daemon service file present on disk
- [x] Candidate-gen dedup verified (emitted=0 updated=7 on re-run)
- [x] Gate 4 ticker dedup firing (AAPL blocked in smoke test)
- [ ] Re-enable `synthos-trade-daemon.timer` (held pending Round 9 / user approval)
- [ ] Decide: close stuck AAPL × 3 paper positions, or let stops trigger on next market day

### B.3 — Round 3: quality pass
- [x] `retail_window_calculator` utcnow() → now(timezone.utc)
- [x] `retail_boot_sequence` _ok locals tidied (no dead summary)
- [x] `retail_event_calendar` docstring update (TODO → Phase 5.a done)
- [x] `retail_window_calculator.get_active_customers` delegates to canonical
- [x] `system_architecture.json` v3.4 → v3.5 (new agents + DBs + milestones)

### B.4 — Round 4: window anchor + cleanup
- [x] Macro window pin (`INSERT OR IGNORE`); minor still upserts
- [x] `.known_good/` deleted (832K of stale pre-refactor snapshot)
- [x] Portal `customer_count` template-var placeholder fixed
- [x] Updated project-memory note (window anchoring resolved)

### B.5 — Round 5: BIL alert + tradable filter
- [x] `check_bil_concentration()` helper + threshold env var
- [x] Trader Gate 0 `0_BIL_CONCENTRATION` sub-gate + daily notification
- [x] `retail_tradable_cache.py` — Alpaca `/v2/assets` cache
- [x] Candidate generator filters un-tradable tickers before emission
- [x] Market daemon premarket prep calls `run_tradable_refresh()` once/day

### B.6 — Round 6: Phase B small follow-ups
- [x] Composite index `signals(ticker, tx_date, status)` (news-agent dedup path)
- [x] `api_calls` date queries rewritten to use range (not `date(timestamp)`)
- [x] Same-day guard on `refresh_earnings_calendar()` (~12× HTTP reduction)
- [x] Three `if True:` trader placeholders deleted (+ safe scripted dedent)
- [x] `log_api_call` bare-except → `log.debug(f"suppressed exception: {_e}")`
- [x] Stamp-writer ticker context (verified already clean — false positive)

### B.7 — Round 7: Phase B medium follow-ups
- [x] `entry_signal_score`: writers pass `round(float, 4)` instead of `f"{x:.4f}"`
      + one-shot `CAST AS REAL` migration for existing TEXT rows
- [x] `close_position()` transaction-wrapped (portfolio read inside)
- [x] Sentiment `fetch_with_retry` tuple timeout + status code in logs
- [x] Gate 11 equity source alignment with Gate 7 (`_ALPACA_EQUITY` + cash fallback)
- [x] Gate 1 fail-CRITICAL on missing data (**verified false positive** — already does)
- [x] `daily_master` ET-day binning via UTC bounds helper

### B.8 — Round 8: closeout
- [x] Validator `gate3_market_state`: missing `_MARKET_STATE_UPDATED` = STALE
- [x] `docs/audit/AUDIT_2026-04-20-deferred.md` (6 items, each with trigger)

### B.9 — Round 9 (pending): Phase B re-run follow-ups

All are sibling sites I missed when fixing earlier rounds, or small
parallel fixes. Same patterns as Rounds 6-7. One patch branch, low risk.

- [ ] R9-1 `reduce_position()` → single-transaction (mirror R7.2)
- [ ] R9-2 `update_portfolio()` → read+write inside one transaction
- [ ] R9-3 `get_api_call_history()` → replace `date(timestamp) = ?` with range (mirror R6.2)
- [ ] R9-4 `cooling_off` composite index `(ticker, cool_until)`
- [ ] R9-5 `price_poller`: wrap upsert loop in `BEGIN/COMMIT`; move `DELETE stale` before inserts
- [ ] R9-6 `tradable_cache.refresh()`: pagination check on `/v2/assets` + transaction wrap
- [ ] R9-7 `event_calendar` earnings upsert: transaction wrap
- [ ] R9-8 `retail_market_daemon`: check `r.ok` / `.status_code` in `run_exit_backfill` + `_send_sms` / `_send_email`
- [ ] R9-9 `get_atr` / `get_volume_avg`: log reason on None/0 return (observability)
- [ ] R9-10 Unsafe dict subscripts in trader (`signal['ticker']`, `portfolio['cash']`) → `.get()` with defaults
- [ ] R9-11 Residual `except Exception: pass` in housekeeping helpers (`cleanup_api_calls`, `get_halt`, etc.)

After Round 9 ships and smoke-verifies:
- [ ] Re-enable `synthos-trade-daemon.timer` on pi5
- [ ] Decide on stuck AAPL × 3 paper positions

---

## C. Deferred items (with triggers)

Documented fully in `docs/audit/AUDIT_2026-04-20-deferred.md`. Marked
`[~]` here for at-a-glance tracking.

- [~] **D1** — Trader `run()` complexity refactor (radon 184)
  _Trigger: next non-trivial trader change; or when we're afraid to touch the file._
- [~] **D2** — Portal complexity hotspots (`api_admin_market_activity` F=67, `_enrich_positions` F=45, etc.)
  _Trigger: next admin-view addition._
- [~] **D3** — `decimal.Decimal` money math replacement
  _Trigger: going live with real capital._
- [~] **D4** — Blanket `datetime.utcnow()` → `datetime.now(timezone.utc)` (~27 sites)
  _Trigger: Python 3.12 upgrade; deprecation becomes runtime warning._
- [~] **D5** — Portal template var / unused-ID hand audit (32 warnings)
  _Trigger: next portal UI sprint._
- [~] **D6** — Duplicate helper consolidation (`now_et`, `is_market_hours`, `kill_switch_active`, etc.)
  _Trigger: when a caller needs to diverge from the canonical._

---

## D. Operational items (post-audit)

Carried forward from pre-audit todos + new items from audit aftermath.

- [ ] Close / let-expire the 3 stuck AAPL paper positions on pi5
- [ ] Re-enable trade-daemon systemd timer after Round 9
- [ ] Finalize Phase 6 scope (not yet planned — candidate: multi-week paper backtest harness, as user mentioned)

---

## E. Next-up work beyond Round 9

### E.1 — Multi-week paper backtest (user-mentioned, not scoped)
Currently no scaffolding. What it'd need:
- Historical bar replay harness
- Per-day synthetic signal injection
- `retail_dry_run.py` already a partial starting point
- Output: per-day `daily_master.md` files for a rolling window

### E.2 — Move to live trading (long-term goal)
Guard rails before this can happen:
- D3 (decimal money math) must land first
- All deferred items reviewed
- Paper backtest results accepted
- Admin override re-reviewed

---

## F. How to use this doc

1. When a task lands, flip `[ ]` → `[x]` and move on.
2. When a task is explicitly deferred, flip to `[~]` and add the trigger.
3. When we spawn a new round / phase, add a new subsection under B or A.
4. Update `_Last updated_` stamp at the top on material changes.
5. This file is the canonical reference. The three audit docs
   (`AUDIT_2026-04-20.md`, `-phase-b.md`, `-phase-b-rerun.md`,
   `-deferred.md`) are the detailed source material.
