# DEPLOY_NOTES — `patch/2026-05-03-auto-user-tagging`

**Target merge date:** 2026-05-03
**Purpose:** AUTO / USER per-position management + bulk prefetch extension

This file lives on `patch/2026-05-03-auto-user-tagging` only. Must be deleted
(or rolled into the merge commit) before merging to `main`.

---

## Scope

Adds AUTO / USER tagging so the bot can coexist with user-initiated positions
in a shared Alpaca account. Trader skips USER positions for buy/sell/stop.
User can toggle per-position or set a sticky "never auto this ticker" preference.

Hard cap of 12 AUTO positions per customer stays in v1, tooltip notes it's
future-expandable based on session-time measurements.

## Files landing on main at merge

### Schema (customer signals.db)
- `synthos_build/src/retail_database.py`
  - `positions.managed_by` column added (default `'bot'`; app-side enforces `bot|user`)
  - New `position_preferences` table: `(ticker PK, sticky, set_by, set_at)`
  - Migration runs per-customer-DB on next agent open (`IF NOT EXISTS` + ALTER with duplicate-column safeguard)

### Trader (runtime behavior change)
- `synthos_build/agents/retail_trade_logic_agent.py`
  - Skips buy/sell/stop on rows where `managed_by='user'`
  - Checks `position_preferences.sticky='user'` before acting on incoming signals for that ticker
  - New Alpaca-discovered positions tag `'bot'` only if bot recently enqueued the signal; else `'user'`

### Sizing
- Position sizing uses total_equity for math (Model B), caps at available_cash (Model C guard)
- New SKIP reason `INSUFFICIENT_CASH_AFTER_MANUAL` logged to `signal_decisions`

### Bulk prefetch extension
- `synthos_build/agents/retail_price_poller.py`
  - `_get_held_tickers()` extended to union OPEN positions + VALIDATED signals
  - Trader reads prices from `live_prices` table (already exists on master DB)

### UI
- `synthos_build/src/retail_portal.py` — Open Positions card:
  - Header counter: `X/12 auto · Y user` with tooltip
  - Per-row AUTO/USER toggle button (locks + grays out when cap full)
  - Per-ticker sticky-USER lock icon + dialog
  - Cap-underutilized soft warning (<40% of cap)
  - Cash-starved warning banner (N `INSUFFICIENT_CASH_AFTER_MANUAL` skips/week)
- New API endpoints:
  - `POST /api/positions/<id>/managed-by` — flip AUTO/USER
  - `POST /api/ticker-preferences` — set/clear sticky
  - `GET /api/auto-slots` — returns `{used, capacity, can_promote}` for UI

### Meta
- `DEPLOY_NOTES.md` (this file) — **DELETE before merging**

## Not in this patch

- Sticky-BOT preference (Q2 deferred)
- Raising cap above 12 — waiting for Friday's dispatch-time measurements
- Promotion queue (simplified to disabled-toggle UX; no queue table needed)
- Email notifications for slot-free events (in-app visual feedback only)

## Phased commit plan

Each phase is an independent commit. Safe to stop partway; main is fine without
later phases (feature is additive).

| Phase | Content | Invisible to user? |
|---|---|---|
| 1 | DB schema migration only | Yes |
| 2 | Trader skip logic for USER rows | Invisible (all default `'bot'` today) |
| 3 | Sizing Model B + C | Behavior change (better) |
| 4 | Prefetch extension to cover validated signals | Invisible perf improvement |
| 5 | UI counter + toggle + endpoint | User-visible |
| 6 | Sticky UI + endpoint | User-visible |
| 7 | Warning banners | User-visible |
| 8 | Docs update | - |

## Pre-merge checklist

- [ ] All 5 news baseline cycles still capture IDENTICAL (no regression — run capture_baseline.py for each)
- [ ] Daily health aggregator shows no new ERROR/CAUTION patterns in the 3 days prior to merge
- [ ] c8_readiness.py still reports same condition states
- [ ] Schema migration applied cleanly on a test customer DB copy (verify `PRAGMA table_info(positions)` shows managed_by)
- [ ] UI QA: toggle works end-to-end on a test customer
- [ ] Measurement: dispatch-cycle time on pi5 with live traffic should not regress (bulk prefetch should improve it)
- [ ] DEPLOY_NOTES.md deleted
- [ ] `/system-architecture` portal page updated

## Post-merge verification

- [ ] Pi 5 pulled successfully
- [ ] `synthos-portal.service` restarted, `is-active`, no startup errors
- [ ] Schema columns present on each customer's signals.db (`PRAGMA table_info(positions)` per customer)
- [ ] Tail `scheduler.log` 15 min — no new trader exceptions
- [ ] `live_prices` table continues to be updated every 60s
- [ ] Hit `/api/auto-slots` for a test customer — returns sensible numbers

## Rollback

- `git revert <merge commit>` → push → pull on pi5 → restart portal service
- Schema changes are additive (ADD COLUMN, IF NOT EXISTS CREATE TABLE). Rollback leaves orphan columns but doesn't break older code.
- Sticky tags + USER-tagged rows: no harm if trader's old code ignores them (existing `positions` reads don't reference the column).
