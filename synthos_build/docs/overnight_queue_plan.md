# Overnight Queue + Pre-Open Re-Evaluation — Build Plan

**Status**: Planned
**Scheduled**: Today after market close (20:00 UTC / 16:00 ET)
**Est. total**: 5–6 hours focused work

## Why

Currently the system can submit orders outside market hours (seen at 04:01 ET
today when Patrick's BIL + XOM got pending sells, and 08:05 ET when AMD's
stop-loss fired a sell order that sat in Alpaca `new` state until market open).

Problems with executing outside 13:30–20:00 UTC:
- Market orders submitted pre/post-market sit in Alpaca as `new` until open
  and fill at open price anyway — zero information/timing advantage
- DB writes the position as CLOSED with an assumed fill price; when Alpaca
  actually fills later at a different price, DB P&L ≠ reality
- Sentiment "cascade detected" path can trigger bulk liquidations on
  thin-volume overnight price noise that often reverses at open
- Any bug in "price dipped then recovered" territory (today's AMD case)

Desired model:
- **One** hourly daemon run, 24/7, market-aware internally
- Inside market hours: execute as today
- Outside market hours: queue decisions as `QUEUED_FOR_OPEN`, no Alpaca submission
- Pre-open: re-evaluate queued decisions; cancel with a visible flag if the
  trade is no longer good/neutral

## Phase 1 — Foundation (45 min)

### 1.1 Icon library — `synthos_build/src/icons.py`
- Inventory existing inline SVG icons in `retail_portal.py` (bell, portrait,
  any others)
- Create Python module returning SVG strings keyed by name
- `icons.WARN_RED`, `icons.WARN_AMBER`, `icons.BELL`, `icons.PORTRAIT`, ...
- All stroke-based wire style, uniform `stroke-width`, uniform `viewBox`
- Portal HTML switches from inline SVG to icon-library references

### 1.2 ⚠️ stylization — red + amber variants
- `icons.WARN_RED` — cancelled-protective, critical alerts
- `icons.WARN_AMBER` — warnings / attention-needed
- Same wire language as bell / portrait icons

### 1.3 DB schema extension — `retail_database.py`
New columns on `pending_approvals`:
- `queue_origin` TEXT: `'market'` | `'overnight'`
- `reevaluated_at` TEXT NULL
- `cancelled_reason` TEXT NULL

New status values:
- `QUEUED_FOR_OPEN` — overnight decision waiting for next market open
- `CANCELLED_PROTECTIVE` — pre-open re-eval killed it

Migration via idempotent ALTER in `MIGRATIONS` list.

## Phase 2 — Core logic (2 hours)

### 2.1 Market-hours gate — `retail_trade_logic_agent.py`
- Add `_is_market_hours()` → True only during 13:30–20:00 UTC weekdays, minus
  US market holidays
- Wrap every `alpaca.submit_order()`: if not market hours → write to
  `pending_approvals` with `queue_origin='overnight'` + `status='QUEUED_FOR_OPEN'`
  instead of submitting

### 2.2 Overnight queue path
- AUTOMATIC + queued overnight → starts `QUEUED_FOR_OPEN`, will auto-execute
  after pre-open re-eval passes
- MANAGED / SUPERVISED + queued overnight → `PENDING_APPROVAL` (unchanged) +
  `queue_origin='overnight'` metadata

### 2.3 Pre-open re-evaluation — new function, first step of market-open pipeline
- Fetch all `QUEUED_FOR_OPEN` + user-approved MANAGED rows
- Re-run the original decision's key checks (entry gate or exit gate)
- Still good/neutral → leave ready for execution
- No longer valid → `status='CANCELLED_PROTECTIVE'` + `cancelled_reason`
- Log each cancellation to `system_log` for audit

### 2.4 Execution step
- Market-open pipeline executes remaining `QUEUED_FOR_OPEN` / approved-MANAGED
  rows in one batch

## Phase 3 — UI surfacing (1.5 hours)

### 3.1 `/api/pending` endpoint
Returns customer's queue for dashboard card.
Fields: ticker, side, shares, price, reasoning, queue_origin, status,
cancelled_reason, queued_at.

### 3.2 Pending dashboard card
- Shows `QUEUED_FOR_OPEN` + `PENDING_APPROVAL` entries
- AUTOMATIC users: **read-only preview**, low-emphasis styling
- MANAGED / SUPERVISED: approve / reject action buttons
- Empty state: "No pending decisions — check back after the next overnight cycle"

### 3.3 Cancelled-protective overlay in trade history
- Render `CANCELLED_PROTECTIVE` rows in place of the trade that would have been
- `icons.WARN_RED` badge + "CANCELLED (protective)" label
- Shows `cancelled_reason`
- Muted / struck-through styling on trade details

## Phase 4 — Integration (45 min)

### 4.1 Collapse cron entries
Remove:
- `5 0-8,16-23 * * 1-5 retail_scheduler.py --session overnight`
- `21 * * * 1-5 retail_heartbeat.py --session trade`

Replace with:
- `5 * * * * retail_market_daemon.py` (hourly, 24/7, market-aware internally)

### 4.2 `retail_market_daemon.py` updates
- If `is_market_hours()` → continuous dispatch as today
- Else → one-shot full pipeline + exit
- Pre-open re-eval runs as first phase of the market-open cycle

### 4.3 Deploy + verify
- Dry-run with `--dry-run` flag to avoid accidental executions during test
- Confirm pending queue populates outside market hours
- Confirm pre-open re-eval cancels a stale decision
- Confirm UI renders on both auto and manual customer accounts

## Phase 5 — Regulatory audit doc (30 min)

### 5.1 `synthos_build/docs/trade_lifecycle.md`
One-page walkthrough: signal → overnight queue → re-eval → execution → history.

Emphasizes:
- No executions outside market hours
- Every trade has a pre-commit log entry
- Cancellations are first-class, logged, visible to the user
- Consistent flow for both AUTOMATIC and MANAGED customers

## Regulatory angle

Why this design defends well:

1. **Every trade has a 30-minute prior audit entry** — no surprise executions
2. **Re-evaluation creates a second data point** — system self-checks, not blind
3. **Cancellation is logged + visible** — shows risk-aware behavior
4. **Customer authorization is explicit per trade for manual users** — they see
   intent AND the system double-checks
5. **All executions happen in liquid hours** — best-execution defense

## Dependencies / sequencing

- Phase 1 (schema + icons) must land before Phase 2/3 reference them
- Phases 2 and 3 can run in parallel once schema is in
- Phase 4 (cron + daemon changes) must be last — integration step
- Weekend is ideal for shake-out since market is closed and queue behavior
  is the dominant path
