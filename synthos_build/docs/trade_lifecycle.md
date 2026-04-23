# Trade Lifecycle — Regulatory Audit Reference

One-page walkthrough of how a signal becomes a trade in the Synthos
retail stack. Written so a compliance reader can trace any executed
order back to the data and decisions that produced it.

All timestamps are UTC.
All decisions leave an audit row in `signal_decisions` or
`pending_approvals` or `system_log`.
No order is submitted to a broker outside US regular market hours
(13:30-20:00 UTC, weekdays).

---

## 1. Signal capture — news agent (`retail_news_agent.py`)

**Input sources.** Alpaca News API for headlines; SEC EDGAR for insider
transactions; Alpaca bars API for price context. No LLM in the
classification path — every gate is deterministic keyword/phrase
matching or simple arithmetic (see agent docstring and gate
definitions).

**Gates.** 22 sequential checks covering topic classification, entity
resolution, sentiment, novelty, confirmation, timing, crowding, and
output controls. Each gate's input values and pass/fail result are
written to the `NewsDecisionLog` object that commits one
`system_log` row per article with the full gate trace.

**Output.** An article that passes all gates is written as a `signals`
row with `status='QUEUED'` and an `interrogation_status` field set by
the subsequent peer-corroboration step (see §2).

**Rate-limit posture.** Incremental fetch via `fetch_cursors`; each
source is pulled against its own cursor so missed cycles don't cause
duplicate re-processing. External calls go through `fetch_with_retry`
which has a 3-consecutive-failure circuit breaker and tuple
(5s connect, 10s read) timeout.

---

## 2. Peer corroboration — interrogation listener (`retail_interrogation_listener.py`)

**Mechanism.** News agent broadcasts a UDP packet on port 5556 with the
signal ID and ticker. Peer Synthos nodes on the local network receive,
run deterministic sanity checks (ticker format, duplicate-in-6h check,
rate limit, price summary plausibility), and ACK on port 5557 if the
signal passes. No external API calls in this step.

**Outcomes.** `interrogation_status` lands as one of:
- `VALIDATED` — peer ACK received
- `UNVALIDATED` — broadcast sent, no peer response (degraded state)
- `SKIPPED` — news agent routed the signal to WATCH (not a trade
  candidate), so no interrogation was attempted
- `CORROBORATED` — reserved for future multi-peer consensus

**Degraded-state detection.** The listener heartbeats to the owner
customer DB every 60 seconds. Fault detection flags
`NO_HEARTBEAT_INTERROGATION_LISTENER` if the heartbeat goes stale; the
watchdog auto-restarts the listener process if it dies between boots.
If the pipeline accumulates UNVALIDATED signals, the watchdog's
pipeline-stall alert names `interrogation_listener` as the bottleneck.

---

## 3. Validation chain — enrichment pipeline (`retail_market_daemon.py`)

**Stages** (run in order every 30 min during market hours, once per
off-hours cron firing):

| Agent | What it stamps on the signal |
|-------|------------------------------|
| `retail_market_sentiment_agent` | `sentiment_score` + `sentiment_evaluated_at` |
| `retail_sector_screener` | `screener_evaluated_at` (for candidate tickers) |
| `retail_macro_regime_agent` | `macro_regime_at_validation` |
| `retail_market_state_agent` | `market_state_at_validation` |
| `retail_bias_detection_agent` | per-customer bias scan to `_BIAS_SCAN_LAST` |
| `retail_fault_detection_agent` | system health scan to `_FAULT_SCAN_LAST` |
| `retail_validator_stack_agent` | `validator_stamped_at` + per-customer verdict |

**Promoter (last link).** `DB.promote_validated_signals()` transitions
any QUEUED signal with a complete stamp set AND a promotable
`interrogation_status` (VALIDATED / CORROBORATED / SKIPPED — not
UNVALIDATED) to status=`VALIDATED`. Per-signal PROMOTED rows and
STUCK rows (with reason: which stamp is missing, or which
non-promotable value is present) are written to `signal_decisions`.

**Cap.** `DB.get_validated_signals` applies tier-weighted quotas
(60/25/10/5 across source_tier 1-4 = 100 total). Higher-conviction
signals preserved; low-tier flood can't crowd them out. The cap is a
read-time filter — all validated rows remain in the DB for audit.

**Expiry.** QUEUED rows older than 72h and VALIDATED rows older than
12h transition to `EXPIRED` on the next trader run. The expiry query
runs inside the trader before entry evaluation.

---

## 4. Decision — trade logic agent (`retail_trade_logic_agent.py`)

**Gates.** 14 sequential checks per customer, per run:
- System: kill switch, API health, drawdown, daily-loss limits
- Benchmark: SPY regime (trend, volatility, drawdown)
- Regime: benchmark-relative risk posture
- Position review: per-open-position exit triggers (stop loss,
  trailing ratchet, late-day tighten, open-hour grace window,
  protective exit)
- Signal evaluation: liquidity, spread, score, entry pattern,
  anchor-proximity chase caps, sizing, risk setup, portfolio-level
  exposure, stress, evaluation-loop kill conditions

Each gate writes to `TradeDecisionLog` which commits a structured
`TRADE_DECISION` row to `system_log` per signal and a `scan_log` row
per ticker.

**Gate 6 — entry pattern classification + anchor-proximity caps**
*(added 2026-04-23)*. Each candidate signal is classified into one of
four entry types, each tied to a historical anchor computed from the
Alpaca daily bars already loaded for that ticker:

| Entry type | Anchor | Default max chase above anchor |
|------------|--------|-------------------------------|
| MOMENTUM | 20-day close MA (`MA20`) | `MAX_MOMENTUM_CHASE_PCT` (2%) |
| BREAKOUT | N-day rolling high (`HIGH_20D` default) | `MAX_BREAKOUT_CHASE_PCT` (1.5%) |
| MEAN_REVERSION | 20-day rolling mean (`MEAN20`) | `MAX_MEANREV_CHASE_PCT` (1%) — belt-and-suspenders; z-score already gates |
| PULLBACK | Recent 10-day high (`HIGH_10D`) | no cap — retrace gate already enforces anti-chase |

A signal that classifies as an entry type but whose current price
exceeds `anchor × (1 + cap)` is rejected to WATCH with reason
`"blocked by chase cap (price extended from anchor)"` — distinct from
`"no entry condition met"`. The anchor type, anchor price, and chase
percent are stamped into the Gate 6 log inputs and into the approval
email `reasoning` field. Each cap can be widened or disabled (set to
999) via env var without a code change. Motivation: pre-change audit
found every entry was priced at `current_price` regardless of how far
above its anchor, producing systematic peak-buying on momentum and
breakout paths.

**Order types handled.**
- Market / notional BUY — overnight-queued if off-hours (see §5)
- Market SELL — same overnight gate
- Trailing stop (SELL, conditional) — submitted to Alpaca directly,
  triggers server-side at the stop price
- Close position (market sell via Alpaca position endpoint) — same
  overnight gate

**Per-trader runtime envelope.** 180s soft wall-clock budget enforced
by a background watchdog thread that logs current phase every 15s.
240s hard kill by the dispatch pool as safety net.

---

## 5. Overnight queue + pre-open re-evaluation

**Gate location.** `AlpacaClient.submit_order`, `_submit_notional`,
`close_position` all check `is_market_hours_utc_now()` at entry. If
the market is closed, the order is NOT submitted to Alpaca — instead
a `pending_approvals` row is written with:
- `status = 'QUEUED_FOR_OPEN'`
- `queue_origin = 'overnight'`
- `reasoning` field captures side/ticker/qty or notional/order_type
- Signal ID has a deterministic shape
  `overnight_<side>_<ticker>_<ts>_<rand6>`

**Re-evaluation.** `run_pre_open_reeval()` runs as the first phase of
the market-open pipeline. Per customer:
- Walks every QUEUED_FOR_OPEN row
- Marks `reevaluated_at`
- Row older than `PRE_OPEN_REEVAL_MAX_AGE_HOURS` (default 18h) →
  `status='CANCELLED_PROTECTIVE'`, `cancelled_reason` captures the
  exact age and rationale, `decided_by='pre_open_reeval'`
- Otherwise → `status='APPROVED'` with a decision note capturing the
  age at re-eval time

**Execution.** Existing managed-mode executor picks up APPROVED rows
during the trader dispatch that follows re-eval. Executor submits the
order to Alpaca during market hours, marks the row `EXECUTED`, and
preserves the row forever (audit trail never deletes).

**Cancellation visibility.** Cancelled rows surface on the portal
trade-history overlay with a `WARN_RED` badge, the
"CANCELLED (protective)" label, and the `cancelled_reason` text — so
the user sees what would have traded and why it didn't.

---

## 6. Execution — managed-mode executor (trader, same file)

- Reads `pending_approvals` rows where `status='APPROVED'`
- For each: calls `alpaca.submit_order(ticker, qty, 'buy')` (now inside
  market hours, so the overnight gate no-ops)
- On successful submission:
  - Writes a `positions` row with `status='OPEN'`
  - Submits a trailing-stop SELL as follow-up protection
  - Marks the approval row `EXECUTED`
  - Writes a `trade` notification for the user
- On failure: logs ERROR; approval row stays APPROVED for retry at
  next dispatch

---

## 7. Post-execution lifecycle

**Position management** runs on every trader dispatch (Gate 10):
- Trailing stop ratchet (move stop up as price rises)
- Late-day stop tightening (reduce gap risk into close) — disabled in
  prod via `LATE_DAY_TIGHTEN_PCT=0` after 2026-04-22 audit showed
  late-day tightening caused disproportionate closing-hour loss exits
- **Open-hour stop-loss grace** *(added 2026-04-23)* — stop-loss
  triggering is suppressed for the first `STOP_LOSS_OPEN_GRACE_MINUTES`
  (default 15) after 09:30 ET. Would-be triggers are recorded as
  `pos_log.note` entries for audit (`"Stop-loss suppressed (15-min
  open grace)..."`) but do not fire a SELL. Pulse exits (CASCADE
  signals, severe news veto) are unaffected and still fire
  immediately. Motivation: audit found every opening-hour stop-loss
  over 7 days fired at 09:32 ET due to overnight-ratcheted stops
  sitting inside the market-open gap band. Set
  `STOP_LOSS_OPEN_GRACE_MINUTES=0` to restore prior behavior.
- Protective exit (Pulse urgent flag, benchmark-relative stop
  adjustment)
- Stop-loss fire (submits market SELL — overnight-gated if off-hours)
- Take-profit tiers, holding-period expiry, close-session mode

**Post-close reconciliation** sweeps open positions, compares to
Alpaca's authoritative position list, auto-adopts orphans and closes
ghosts. Differences are logged.

**Outcome tracking.** When a position closes, an `outcomes` row is
written with the full PnL, hold period, entry/exit reasons, and a
backref to the originating signal. Member-weight updates flow from
here if the signal came from a congress member.

**Portal history rendering** *(2026-04-23)*. The dashboard History
panel decouples the outcome-classification badge from the dollar-P&L
color:
- Badge word (`PROTECTIVE` / `WIN` / `LOSS` / `EVEN`) + row icon
  color reflect the *exit reason* — a stop-loss / trailing / safety
  exit always shows amber "PROTECTIVE" regardless of whether the
  trade closed at a profit.
- Dollar amount color is strictly sign-based (teal = gain, pink =
  loss, grey = flat).

So a protective stop-out that closed at +$5.20 shows an amber
"PROTECTIVE" badge next to a teal "+$5.20" — preserving both how
the trade closed and whether it made money. No backend change;
rendering split in `portal.html` `classify()` vs. `pnlColor()`.

---

## Regulatory angle — why this design defends well

1. **Every trade has a pre-commit audit entry.** Even AUTOMATIC-mode
   trades go through `pending_approvals → APPROVED → EXECUTED`
   rather than direct execution; the approval row exists for 30+
   seconds before execution and is preserved afterward.

2. **Re-evaluation creates a second data point.** The system
   self-checks between decision time and execution time rather than
   executing blindly against stale state.

3. **Cancellations are first-class, logged, and visible to the user.**
   `CANCELLED_PROTECTIVE` isn't a silent drop — it has a reason field,
   a UI surface, and an entry in the decision chain.

4. **Customer authorization is explicit per-trade for managed mode.**
   Supervised users see the system's intent AND a second data point
   (the re-eval) before any execution.

5. **All broker submissions happen in regulated-session liquid hours.**
   The overnight queue gate at the Alpaca client boundary is
   enforced in one place; no code path bypasses it.

6. **No LLM in any decision path.** Every classification gate is a
   keyword/phrase/arithmetic check that can be replayed against
   identical inputs to produce identical outputs — the agent's
   decision log captures all inputs and results.

---

## Where to look

| What you want | Where |
|---------------|-------|
| Full decision trace for a signal | `signal_decisions` rows filtered by `signal_id` |
| Full gate results for a trade | `system_log` WHERE `event='TRADE_DECISION'` + `scan_log` |
| Approval/execution history | `pending_approvals` table (never deleted) |
| Cancelled-protective record | `pending_approvals` WHERE `status='CANCELLED_PROTECTIVE'` |
| Agent liveness / heartbeats | `system_log` WHERE `event='HEARTBEAT'` |
| Fault scan snapshots | `customer_settings` key `_FAULT_SCAN_LAST` (JSON) |
| Bias scan snapshots | `customer_settings` key `_BIAS_SCAN_LAST` (JSON) |
| Validator verdict detail | `customer_settings` key `_VALIDATOR_DETAIL` (JSON) |
| Closed-trade outcomes + PnL | `outcomes` table |
| News article classification | `news_feed` table + `NEWS_CLASSIFIED` in `system_log` |
