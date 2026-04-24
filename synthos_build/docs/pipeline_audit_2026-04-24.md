# Trading Pipeline Data-Flow Audit — 2026-04-24

Complete producer → consumer trace of every signal / state / flag that
flows through the Synthos retail trading stack. Maps the enrichment
chain end-to-end and surfaces where sentiment-agent output feeds real
decisions vs. gets written and dropped.

Supersedes earlier verbal assessments that the sentiment agent's output
was "consumed by zero agents except cascade_detected." That was wrong —
sentiment score IS consumed, via a three-hop chain that breaks before
reaching the trader.

## The actual data flow (as of 2026-04-24)

```
[ Alpaca News API ]   [ SEC EDGAR ]   [ Congressional STOCK Act ]
         │                │                    │
         └────────────────┴────────────────────┘
                          │
                          ▼
              retail_news_agent.py   (22 gates)
                          │
                          ├──→ signals table (status=QUEUED)
                          │
                          ├──→ news_flags table     ─────┐
                          │    per-ticker score            │  (sustained
                          │    category (-1..+1)           │   per-ticker
                          │    fresh_until TTL             │   signal
                          │                                │   adjustments)
                          ▼                                │
              retail_interrogation_listener.py             │
                    (UDP port 5556)                        │
                          │                                │
                          └──→ signals.interrogation_status │
                                                           │
                                                           │
           ─── Enrichment loop, every 30 min ───           │
                          │                                │
                          ▼                                │
              retail_market_sentiment_agent.py  (27 gates) │
                          │                                │
                          ├──→ scan_log  (ticker="MARKET"  │
                          │    put_call_ratio, seller_dom  │
                          │    volume_vs_avg, tier,        │
                          │    event_summary,              │
                          │    cascade_detected BOOL)      │
                          │                                │
                          └──→ urgent_flags table ─────────┼─→
                               (only when                   │   (immediate
                               cascade_detected=TRUE)       │    pulse-exit
                                                           │    alerts)
                                                           │
                          ▼                                │
              retail_sector_screener.py                    │
                          │                                │
                          └──→ sector_screening + stamp   │
                               signals.screener_score      │
                                                           │
                          ▼                                │
              retail_macro_regime_agent.py  (5 gates)      │
                          │                                │
                          └──→ customer_settings           │
                               _MACRO_REGIME               │
                               _MACRO_REGIME_DETAIL        │
                                                           │
                          ▼                                │
              retail_market_state_agent.py  (4 gates)      │
                          │  reads: scan_log (latest)      │
                          │         system_log (news)      │
                          │         _MACRO_REGIME          │
                          │  weights: sentiment 0.40       │
                          │           news      0.25       │
                          │           macro     0.35       │
                          │                                │
                          └──→ customer_settings           │
                               _MARKET_STATE (label)       │
                               _MARKET_STATE_SCORE (-1..+1)│
                               _MARKET_STATE_DETAIL (json) │
                                                           │
                          ▼                                │
              retail_bias_detection_agent.py               │
                  (per customer, 6 gates)                  │
                          │                                │
                          └──→ customer_settings           │
                               _BIAS_SCAN_LAST (json)      │
                                                           │
                          ▼                                │
              retail_fault_detection_agent.py (7 gates)    │
                          │                                │
                          └──→ customer_settings           │
                               _FAULT_SCAN_LAST (json)     │
                                                           │
                          ▼                                │
              retail_validator_stack_agent.py (5 gates)    │
                  reads: _MARKET_STATE, _MARKET_STATE_SCORE│
                         _MACRO_REGIME, _BIAS_SCAN_LAST    │
                         _FAULT_SCAN_LAST                  │
                  emits: verdict ∈ {GO, CAUTION, NO_GO}    │
                         restrictions list                  │
                          │                                │
                          └──→ customer_settings           │
                               _VALIDATOR_VERDICT           │
                               _VALIDATOR_RESTRICTIONS     │
                               stamps signal row:          │
                                 market_state_at_validation│
                                 macro_regime_at_validation│
                                 validator_stamped_at      │
                                                           │
                          ▼                                │
                   promoter_validated_signals               │
                   (QUEUED → VALIDATED when                 │
                    all stamps present)                    │
                                                           │
                   ─── End enrichment ───                   │
                          │                                │
                          ▼                                │
           ┌──────────────────────────────────┐            │
           │ signals table (status=VALIDATED) │            │
           └──────────────────────────────────┘            │
                          │                                │
                          ▼                                │
           ┌──────────────────────────────────┐            │
           │ retail_trade_daemon.py           │            │
           │   30s cycle, Mon-Fri 9:30-16:00  │            │
           └──────────────────────────────────┘            │
                          │                                │
                          ▼                                │
        retail_trade_logic_agent.py  (13 gates)            │
                          │                                │
             ┌────────────┴───────────────┐                │
             │                            │                │
             ▼                            ▼                │
    ┌─── Position Management ─────────────┐                │
    │ Gate 10:                             │               │
    │   urgent_flags ◄────────────────────┼───── reads ────┘
    │     → PULSE_EXIT if match           │
    │   trail-stop ratchet                │
    │   late-day tighten (DISABLED)       │
    │   open-hour grace (15min)           │
    │   stop-loss trigger                 │
    └──────────────────────────────────────┘
                          │
             ▼ Signal Evaluation
    ┌──── Gate 1-14 chain ─────────────────────┐
    │ 1  System (drawdown / daily loss / API)  │
    │ 2  Benchmark SPY regime                   │
    │ 3  Regime (vol / trend / risk)            │
    │ 4  Eligibility:                           │
    │    - Ticker dedup                         │
    │    - Liquidity                            │
    │    - Spread                               │
    │    - Correlation                          │
    │    - Event Risk (reads earnings_cache)    │
    │    - Cooling off                          │
    │    - news_flags ◄─────────────────────────┼── reads news_flags
    │ 5  Signal Score (composite):              │
    │    - tier_score     * W_TIER              │
    │    - politician_wt  * W_POL               │
    │    - staleness      * W_STAL              │
    │    - interrogation  * W_INT               │
    │    - sentiment      * W_SENT              │ (per-signal field)
    │    - screener_adj                         │
    │    - news_flags_mod ◄─────────────────────┼── reads news_flags
    │ 5.5 News Veto (severe negative override)  │
    │ 6  Entry type + chase cap                 │
    │ 7  Sizing                                 │
    │ 8  Risk setup                             │
    │ 11 Portfolio Exposure                     │
    │ 13 Stress (SPY intraday > 5% drop)        │
    │ 14 Evaluation (Sharpe + drawdown kill)    │
    └───────────────────────────────────────────┘
                          │
                          ▼
          alpaca.submit_order("buy")  →  Alpaca API
                          │
                          ▼
          db.open_position(entry_price=candidate['price'])
                          │  ⚠ SLIPPAGE GAP: uses stale daily-close
                          │    price, ignores filled_avg_price from
                          │    Alpaca order response.
                          ▼
                    positions table
                          │
          ... position management loop ...
                          │
                          ▼
                   close_position()
                          │
                          ▼
                     outcomes table
                          │
                          ▼
             member_weight adjustment
             (congressional signals only)
```

## Gaps identified

### 🔴 Gap 1 — Validator verdict ignored by trader

**Producer:** `retail_validator_stack_agent.py` Gate 5 synthesizes a verdict
`GO | CAUTION | NO_GO` and a `restrictions` list (e.g.
`DEGRADED_NO_MARKET_STATE`, `BIAS_SECTOR_CONCENTRATION`). Writes to
`customer_settings` as `_VALIDATOR_VERDICT` and `_VALIDATOR_RESTRICTIONS`.

**Consumer:** None. Trader doesn't read either key.

**Impact:** Validator can say "system is degraded, don't trust decisions
right now" and the trader will keep opening positions. Defeats the
purpose of the 5-gate validation chain.

**Fix:** add a check to `gate1_system` (or new Gate 1.5) that consults
`_VALIDATOR_VERDICT`. If `NO_GO` → halt new entries (existing positions
unaffected); if `CAUTION` → log warning and proceed.

### 🔴 Gap 2 — Market-state score missing from Gate 5 composite

**Producer:** `market_state_agent` synthesizes sentiment (40%) + news
(25%) + macro (35%) into a `_MARKET_STATE_SCORE` (−1 to +1).

**Consumer:** Validator (for verdict only). NOT the trader's Gate 5
composite.

**Impact:** Gate 5 composite score combines 5 per-signal inputs
(tier / politician / staleness / interrogation / sentiment) but NO
market-wide regime input. A signal gets the same composite during a
high-fear broad selloff as during a calm bull day.

**Fix:** add `market_state_score` as a 6th weighted input to the Gate 5
composite with its own configurable weight (default ~0.10). When market
state is deteriorating, all composite scores tilt slightly down,
reducing entry likelihood without hard-blocking.

### 🔴 Gap 3 — Slippage not recorded (re-flag from earlier today)

Trader records `candidate['price']` (stale daily close) as `entry_price`
instead of `order['filled_avg_price']` from the actual Alpaca fill.
Every downstream P&L calc is measured against a fictional entry.

**Fix:** ~10 lines. Poll order status until filled, use real fill price.

### 🟡 Gap 4 — `urgent_flags` vs `news_flags` — NOT a unification candidate

Earlier analysis suggested unifying these. On closer look they serve
DIFFERENT purposes at different severity tiers:

- `urgent_flags` → cascade alerts, trigger **Gate 10 PULSE_EXIT** (force
  exit existing positions). Written only when sentiment's
  `cascade_detected=True`. Small, binary.
- `news_flags` → per-ticker scoring adjustments, feed **Gate 5 composite
  modifier**. Written by news agent for every flagged event. Score from
  −1 to +1 with TTL and category.

Different schemas, different TTLs, different consumers, different
severity. Keep separate. Update earlier recommendation to unify.

### 🟡 Gap 5 — Sentiment agent's 27 gates — most outputs dropped

Of the 27 gates, only the composite `cascade_detected` boolean and the
composite market score flow forward:

- `cascade_detected` → urgent_flags → Gate 10 PULSE_EXIT
- Composite score → scan_log MARKET row → market_state_agent (weight 0.40)

Gates 5 (breadth), 7 (volatility), 8 (options), 9 (safe-haven),
10 (credit), 11 (sector rotation), 13 (news), 15 (divergence),
21 (divergence warnings), 23 (risk discounts), 24 (persistence),
25 (evaluation), 26 (output) — each emits rich state labels that are
logged to scan_log but never read by anyone downstream.

**Implication:** the sentiment agent's surface area is much wider than
its consumed output. "Resurrecting properly" can mean (a) extract just
the composite + cascade path and delete the rest, or (b) wire the rich
per-dimension outputs into something that uses them. Option (a) is
smaller; option (b) bigger but reveals more of sentiment's design intent.

## What IS working end-to-end

- **Signal capture → validation → trader** pipeline: solid
- **news_flags → Gate 4 EVENT_RISK + Gate 5 composite modifier**: wired and active
- **urgent_flags → Gate 10 PULSE_EXIT** for cascade force-exits: wired
- **Member weights → news agent confidence adjustment**: feedback loop alive
- **Validator → signal stamps (market_state_at_validation, etc.)**: signal carries the context
- **Graceful degradation**: missing upstream data produces CAUTION (never NO_GO unless all sources are down)

## Recommended wiring for Option A ("Resurrection Lite")

1. **Close Gap 1:** trader reads `_VALIDATOR_VERDICT`. NO_GO → halt new
   entries in Gate 1 chain. CAUTION → log + proceed.
2. **Close Gap 2:** add `market_state_score` as 6th input in Gate 5
   composite scoring. Default weight 0.10 (env-configurable).
3. **Close Gap 3:** slippage fix — use `filled_avg_price` as entry_price.
4. **Document Gap 4:** add comment block explaining urgent_flags vs
   news_flags separation rationale, to prevent future "let's unify"
   questions.
5. **Log Gap 5:** separate backlog entry to decide sentiment agent's
   long-term scope (strip vs expand). Don't touch pre-travel.
