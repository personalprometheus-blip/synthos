# TICKER_STATE Architecture

**Status:** APPROVED 2026-05-04 — design locked, implementation plan below
**Owner:** Operator + AI agents
**Supersedes:** Per-signal time-varying enrichment columns
**Companion docs:** [`SIGNAL_FUNNEL_REPORT_2026-05-04.md`](SIGNAL_FUNNEL_REPORT_2026-05-04.md) (the audit that surfaced the architectural problem)

---

## 1. The Architectural Mistake We're Correcting

The current schema treats **per-ticker time-varying data as columns on per-signal rows**. This is the source of every signal-funnel symptom we've documented:

```sql
-- TODAY (broken model): signals table holds the world's view of each ticker
CREATE TABLE signals (
  id, ticker, source, headline, tx_date,            -- ← STATIC: event facts
  sector,                                            -- ← STATIC: ticker metadata
  sentiment_score, sentiment_evaluated_at,           -- ← TIME-VARYING: ticker state
  screener_score, screener_evaluated_at,             -- ← TIME-VARYING: ticker state
  macro_regime_at_validation, market_state_at_...,   -- ← TIME-VARYING: global state
  validator_stamped_at,                              -- ← TIME-VARYING: validator state
  ...
);
```

Two signals for AAPL on different days each get their own copy of "what we currently know about AAPL." That copy goes stale unless someone explicitly re-stamps it. The status filter `WHERE status IN ('QUEUED','VALIDATED')` exists to gate these stamps — and that filter is exactly what gave us the 11% candidate-fill bug.

There is no concept in the schema of **"what is the current state of ticker X"** — only an N-times-replicated scattering of stale snapshots across all of X's signal rows.

## 2. The Clean Shape

Two tables, one role each:

```
┌─────────────────────────────────────────────────────────────┐
│ signals — DISCRETE EVENT LOG (static, append-only-ish)      │
├─────────────────────────────────────────────────────────────┤
│ id, ticker, source, source_tier, headline, politician,      │
│ tx_date, disc_date, amount_range, source_url, image_url,    │
│ confidence, staleness, corroborated, is_amended, is_spousal,│
│ status, created_at, expires_at, sector, company             │
│   (sector + company resolved at insert from lookups)        │
│                                                              │
│ STATIC at insert. Updates only on lifecycle transitions     │
│ (PENDING → QUEUED → ACTED_ON / EXPIRED).                    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ ticker_state — LIVE WORLDVIEW (per-ticker, mutable)         │
├─────────────────────────────────────────────────────────────┤
│ ticker (PK), is_active, first_seen_at, last_active_at,      │
│ price, price_at, vol_bucket, atr, spy_correlation,          │
│ news_score_4h, news_evaluated_at,                           │
│ sentiment_score, sentiment_evaluated_at, cascade_tier,      │
│ screener_score, screener_evaluated_at,                      │
│ momentum_score, momentum_evaluated_at,                      │
│ sector_score, sector_evaluated_at,                          │
│ insider_signal, insider_evaluated_at,                       │
│ volume_anomaly, volume_evaluated_at,                        │
│ updated_at                                                  │
│                                                              │
│ ONE row per ticker. Always reflects latest known state.     │
│ Read by validator + trader. Written by per-field owning     │
│ enrichment agents.                                          │
└─────────────────────────────────────────────────────────────┘
```

A signal event for AAPL stores **only** the event facts. Everything time-varying about AAPL lives in the AAPL row of `ticker_state`. When the sentiment agent updates AAPL, it updates **one row**, and every signal/decision that reads AAPL automatically sees the latest.

## 3. Field-Ownership Contract

Every field has exactly one owning agent and a derivation recipe. This is the contract that makes the schema extensible:

| field | owning agent | trigger | recipe (for rebuild) |
|---|---|---|---|
| `sector`, `company` | `upsert_signal` helper (`_resolve_ticker_meta`) | first-seen of ticker | `ticker_sectors` ∪ `sector_screening` ∪ `tradable_assets` lookup |
| `price`, `price_at` | `retail_price_poller` | per tick (~60s) | `live_prices` last entry |
| `vol_bucket`, `atr`, `spy_correlation` | `retail_price_poller` (or sentiment_agent) | per cycle | rolling N-bar stats from `live_prices` |
| `news_score_4h`, `news_evaluated_at` | `retail_news_agent` | per news_feed insert | aggregate of news_feed last 4h for ticker |
| `sentiment_score`, `sentiment_evaluated_at`, `cascade_tier` | `retail_market_sentiment_agent` | every cycle (5-10min) OR per news event | 27-gate output + cascade detector |
| `screener_score`, `screener_evaluated_at` | `retail_sector_screener` | per screener run (twice daily + on-demand) | latest `sector_screening` row for ticker |
| `momentum_score`, `momentum_evaluated_at` | `retail_sector_screener` | same as screener_score | momentum component of sector_screening |
| `sector_score`, `sector_evaluated_at` | `retail_sector_screener` | per cycle | sector ETF rotation score for ticker's sector |
| `insider_signal`, `insider_evaluated_at` | future EDGAR daemon (or part of sentiment_agent today) | per Form 4 ingest | EDGAR last-30d net for ticker |
| `volume_anomaly`, `volume_evaluated_at` | `retail_market_sentiment_agent` (Phase 2 today) | per cycle | Finviz volume vs 20d avg |

### Adding new depth in the future

Want momentum z-score, dealer gamma, options IV rank, analyst consensus, social sentiment? All follow the same contract:

1. Add column to `ticker_state`
2. Identify (or build) the owning agent
3. Define the recipe
4. Agent writes the field on its trigger schedule

No architectural rework. The schema scales.

## 4. Row Lifecycle

```
APPEAR
  Trigger: first time we encounter the ticker
    - signal arrives for ticker T (most common)
    - position opened for T (rare — usually after a signal)
    - prefill of watchlist on system startup
    - manual operator add to watchlist
  Action: INSERT bare row, set is_active=true, first_seen_at=now,
          resolve sector + company synchronously.
  After: agents discover ticker exists; their owned fields fill in
         on next cycle/event.

LIVE
  All updates are field-scoped (UPDATE ticker_state SET <field>=?,
  <field>_evaluated_at=? WHERE ticker=?). No row replacement.
  last_active_at refreshed on any event referencing this ticker.

COLD
  Trigger: no event for ticker in N days (default 90).
  Action: set is_active=false. Row stays in table, agents skip
          on next pass to keep cycle work bounded. Reads still work.

ARCHIVE
  Trigger: cold for M additional days (default 90 → 180 total).
  Action: move row to ticker_state_archive, then DELETE from
          ticker_state. Reborn (back to APPEAR) on first new event.
```

We never lose data. The hot/cold/archive cycle is automatic based on activity.

## 5. Position Snapshot (audit + analysis)

When a position opens, the trader's gate 5 reads `ticker_state` and decides BUY. The position row captures a snapshot of `ticker_state` at decision time:

```sql
ALTER TABLE positions ADD COLUMN entry_state_snapshot TEXT;
-- JSON blob: { "ticker_state": {...full row...}, "captured_at": "..." }
```

Three reasons this matters:

1. **Audit trail** — "why did the bot buy AAPL on May 5?" answerable forever
2. **Post-trade analysis** — compare entry snapshot vs exit conditions; identify which fields predict winners
3. **Recovery from earlier audit gap** — `positions.signal_id = NULL` for every row today; we lost the link from trade back to triggering signal. Snapshot fixes that and adds richer context.

Optionally: `exit_state_snapshot` captured on close, for the full lifecycle.

## 6. Source-of-Truth Hierarchy

```
RAW EVENT LOG (append-only-ish, source of truth)
  news_feed, sector_screening, live_prices, EDGAR rows,
  macro_events, tradable_assets, signals
    ↓ owning agents read + compute
TICKER_STATE (computed view, source of truth for "current state")
  Persistent. Per-ticker. Updated in place.
    ↓ downstream consumers read
TRADER, VALIDATOR, DASHBOARD, FUTURE TOOLS
  Read latest. Don't recompute.
```

`ticker_state` is the **source of truth for current ticker state**. Underlying tables are the source of truth for **what events occurred**. A rebuild path exists (Section 8) so ticker_state can always be recomputed from raw events when needed.

## 7. Update Triggers

Each agent decides its own update model. The TABLE is just a write target. Three patterns coexist:

- **Cron-paced** — sentiment_agent today (every 5-10 min). Works fine. Writes ticker_state on each cycle.
- **Event-driven** — news_agent on per-article ingest. Writes `news_score_4h` for the affected ticker.
- **On-demand** — trader gate 5 detects stale field, requests synchronous refresh. Lower-priority queue path.

The architecture supports all three. Agent owners pick what makes sense for their data's volatility + cost.

## 8. Rebuild Path

A standalone tool (`synthos_build/src/ticker_state_rebuild.py`):

```
python3 ticker_state_rebuild.py --ticker AAPL --field sentiment_score
  → Reads news_feed for AAPL in the relevant window
  → Re-runs sentiment_agent's recipe over those events
  → Writes the result to ticker_state.sentiment_score
  → Logs the rebuild in ticker_state_rebuild_log

python3 ticker_state_rebuild.py --all
  → Walk every ticker, every field, run each recipe
  → Resource-heavy; intended for recipe migrations + DB recovery
```

Rebuild is a **power tool**, not a normal path. Triggers:

- We change a formula (e.g. new sentiment scoring) and want to backfill
- DB corruption — recover from raw event tables
- Migration validation — compare freshly-rebuilt values vs live values to find drift bugs

## 9. Schemas (full DDL)

### ticker_state

```sql
CREATE TABLE ticker_state (
  ticker TEXT PRIMARY KEY,

  -- Lifecycle
  is_active INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_active_at TEXT NOT NULL,

  -- Static metadata (resolved at first-seen, can be refreshed)
  -- These also live on signals.sector / signals.company for the per-event
  -- record, but ticker_state is canonical for "current sector for AAPL."
  sector TEXT,
  company TEXT,
  exchange TEXT,

  -- Price layer (highest update frequency)
  price REAL,
  price_at TEXT,
  vol_bucket TEXT,        -- LOW / MED / HIGH
  atr REAL,
  spy_correlation REAL,

  -- News layer (event-driven)
  news_score_4h REAL,     -- aggregate sentiment of last 4h news for ticker
  news_evaluated_at TEXT,

  -- Sentiment layer (cron-paced today; potentially event-driven later)
  sentiment_score REAL,
  sentiment_evaluated_at TEXT,
  cascade_tier INTEGER,   -- 1-4 from cascade detector

  -- Screener layer (twice-daily + on-demand)
  screener_score REAL,
  screener_evaluated_at TEXT,
  momentum_score REAL,
  momentum_evaluated_at TEXT,
  sector_score REAL,
  sector_evaluated_at TEXT,

  -- Insider layer
  insider_signal REAL,    -- normalized 0..1 net 30-day insider activity
  insider_evaluated_at TEXT,

  -- Volume layer
  volume_anomaly REAL,    -- ratio vs 20d avg
  volume_evaluated_at TEXT,

  -- Bookkeeping
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_ticker_state_active ON ticker_state(is_active, last_active_at);
CREATE INDEX idx_ticker_state_updated ON ticker_state(updated_at);
```

### ticker_state_archive

Same schema as `ticker_state` plus `archived_at TEXT NOT NULL`. Cold rows that have aged past the archive threshold land here. Agents skip these.

### ticker_state_rebuild_log

```sql
CREATE TABLE ticker_state_rebuild_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT,                     -- NULL if --all
  field TEXT,                      -- NULL if all fields
  triggered_by TEXT NOT NULL,      -- 'recipe_change' | 'corruption_recovery' | 'migration' | 'manual'
  rows_affected INTEGER,
  duration_ms INTEGER,
  notes TEXT,
  ran_at TEXT NOT NULL
);
```

### positions schema additions

```sql
ALTER TABLE positions ADD COLUMN entry_state_snapshot TEXT;
ALTER TABLE positions ADD COLUMN exit_state_snapshot TEXT;
-- Both: JSON blob of ticker_state row at the moment of buy/sell decision.
-- Nullable for backward compat.
```

Also: backfill `positions.signal_id` (currently always NULL) is a **separate** bug-fix tracked outside this spec but should land alongside Phase 2.

## 10. Migration Phases

Each phase is independently shippable, leaves the system in a working state, and can be paused or rolled back. **Order matters.**

### Phase 1 — Foundation (1 PR)

Goal: tables exist, helpers exist, baseline data populated. Nothing yet reads or writes them.

**Deliverables:**
- `ticker_state`, `ticker_state_archive`, `ticker_state_rebuild_log` tables created (idempotent CREATE TABLE IF NOT EXISTS in `retail_database.py`'s schema init)
- `entry_state_snapshot`, `exit_state_snapshot` columns added to `positions` (nullable)
- New methods on `DB` class:
  - `upsert_ticker_state(ticker, **fields)` — UPSERT pattern, sets `updated_at`, sets per-field `*_evaluated_at` automatically when a field is updated
  - `get_ticker_state(ticker)` → dict or None
  - `mark_ticker_active(ticker)` — refresh `last_active_at`, ensure `is_active=true`
  - `archive_cold_tickers(cold_days=90, archive_days=180)` — lifecycle housekeeping
- One-shot backfill script `src/ticker_state_backfill.py`:
  - Walk `signals` table, build initial `ticker_state` rows from existing data
  - Use the latest non-null value per ticker for each field
  - Mark all tickers `is_active=true`; cold pass happens later
- Tests: insert/read/upsert smoke tests

**Done when:**
- `ticker_state` exists in shared DB
- Backfill populated rows for every ticker that has had a signal in the last 60 days
- No reader or writer in production has changed yet
- Tests pass

### Phase 2 — Writers Migrate (1 PR per agent, parallel)

Goal: enrichment agents write to `ticker_state`. Old paths still work as deprecation shims.

**Deliverables (per agent):**

A. `retail_market_sentiment_agent.py`:
- `stamp_signals_sentiment(ticker, score)` becomes `upsert_ticker_state(ticker, sentiment_score=score)` + DEPRECATED shim that ALSO writes the old way (for safety during migration)
- Same for screener stamps in this agent

B. `retail_sector_screener.py`:
- Writes `screener_score`, `momentum_score`, `sector_score` to ticker_state
- Old `stamp_signals_screener` becomes deprecation shim

C. `retail_news_agent.py`:
- Writes `news_score_4h` to ticker_state per ticker as articles arrive
- Refreshes `news_evaluated_at`

D. `retail_price_poller.py`:
- Writes `price`, `price_at`, `vol_bucket` (and `atr`, `spy_correlation` if it computes them) to ticker_state on each tick

**Done when (each agent):**
- Agent's writes land in ticker_state
- Old path still writes too (deprecation shim active)
- Telemetry confirms both paths writing same values within tolerance
- Agent tests pass

### Phase 3 — Readers Migrate (1 PR per consumer)

Goal: consumers read from `ticker_state`. Source of truth shifts. Old columns become read-only shadows.

**Deliverables:**

A. Trader gate 5 (`retail_trade_logic_agent.py`):
- Reads `ticker_state.sentiment_score`, `screener_score`, `momentum_score`, etc., instead of `signal['sentiment_score']`
- ~6-10 read sites change
- Includes the freshness check: if `*_evaluated_at` > 30 min old, log a `STALE_FIELD` note

B. Validator (`retail_validator_stack_agent.py`):
- Reads ticker-level state for unknown-sector check (instead of bias-detection over signal rows)
- The `BLOCK_SECTOR_UNKNOWN` restriction logic operates on ticker_state.sector NULL count, not signal-row count

C. Position open (`retail_trade_logic_agent.py`):
- At trade open, capture `entry_state_snapshot` from ticker_state
- At trade close, capture `exit_state_snapshot`
- Backfill `positions.signal_id` from the source signal (separate fix, lands here)

D. Dashboard / portal:
- Optional: surface ticker_state on the operator UI for live debugging

**Done when:**
- Trader produces same scores reading from ticker_state as it did from signal columns (within tolerance, accounting for fresher data)
- Validator's BLOCK_SECTOR_UNKNOWN now keys on ticker_state, not signal rows
- New positions get entry_state_snapshot populated
- Old path still receives writes (Phase 2 shims still active)

### Phase 4 — Deprecate Old Columns

Goal: instrument confirms readers no longer touch the old per-signal columns. Then we remove them.

**Deliverables:**
- Add read-counter telemetry on `signals.sentiment_score` etc. for one trading week
- Confirm zero reads from production code paths
- Remove deprecation shims from Phase 2 (writers no longer write the old columns)
- Remove the columns from signals (or set NULLABLE and stop populating)

**Done when:**
- Telemetry shows zero reads for one full week
- Old columns either dropped or nulled-and-frozen
- Schema is clean

### Phase 5 — Optional Event-Driven Layer (MQTT)

Goal: real-time sentiment fan-out + reduced staleness for high-frequency consumers.

**Deliverables:**
- Sentiment agent (and others) publishes to `process/sentiment/<TICKER>` (and parallel topics) on every ticker_state update
- Retained messages on the broker so late subscribers get latest immediately
- Dashboard subscribes for live updates
- Trader OPTIONALLY subscribes for sub-cycle freshness (currently 30s cycle is fine without)

**Done when:**
- MQTT topics carrying ticker state changes
- One subscriber (dashboard) consuming
- Trader can subscribe if/when needed but doesn't have to

This phase is **NOT a precondition** for the rest. The DB-as-source-of-truth model works without MQTT. MQTT is upgrade path for real-time use cases.

## 11. Test Strategy

| phase | what we test | how |
|---|---|---|
| 1 | Schema correct, helpers work | Unit tests on `upsert_ticker_state`, `get_ticker_state`. Backfill produces expected rows. |
| 2 | Writers' values correct | For each agent, run a cycle, compare ticker_state value vs old per-signal stamp. Should match within tolerance. |
| 3 | Readers consume latest | Trader gate 5 produces same `entry_signal_score` as before for the same input data. Compare in shadow mode (run both, log diff). |
| 4 | No regressions, old columns inert | Telemetry counters confirm zero reads. Production scores unchanged. |
| 5 | MQTT messages flow correctly | Publish sentinel test value, confirm subscriber receives it. Latency < 1s. |

## 12. Rollback Plan

Each phase is rollback-safe because:

- **Phase 1**: tables added, no behavior change. Drop tables to revert.
- **Phase 2**: writers dual-write (new + old). Disable new path via env flag → old behavior restored.
- **Phase 3**: readers prefer ticker_state, fall back to signals.* if NULL. Toggle preference via env flag.
- **Phase 4**: just don't drop columns yet; revert is not removing them.
- **Phase 5**: MQTT is additive; remove publishes to revert.

All envs include a `TICKER_STATE_ENABLE_READS` and `TICKER_STATE_ENABLE_WRITES` flag during Phases 2-3 for kill-switch capability.

## 13. Open Questions To Resolve Before Phase 1 PR

1. **Per-customer vs shared**: ticker_state lives in shared DB (same place sentiment writes go today). Confirmed.
2. **Snapshot blob vs structured columns** on positions: JSON blob is more flexible (forward-compat with new ticker_state fields). Confirmed.
3. **Cold/archive thresholds**: 90 days cold, 180 days archive, defaults overridable via env. Confirmed.
4. **Naming of `_evaluated_at` columns**: keep per-field. Verbose but clear. Confirmed.
5. **Migration of `tradable_assets` for sector lookup**: bulk import (yfinance + Alpaca `/v2/assets`) is **separate** from this spec but Phase 1 should NOT block on it. The `_resolve_ticker_meta` lookup chain handles missing entries gracefully (returns None).

## 14. What's NOT in scope for this spec

- The bulk sector import script (separate work item, runs anytime)
- The candidate signal sentiment-stamp filter fix (`_STAMPABLE_STATUSES`) — kill it during Phase 2 once `upsert_ticker_state` replaces `stamp_signals_sentiment`
- The profit-taking control-flow fix (already shipped 2026-05-04)
- The v2 customer's stacked validator restrictions (separate investigation)
- Daemon/MQTT redesign of sentiment agent (Phase 5 only)

## 15. Sequencing Decision

**Phase 1 starts now.** It has no dependencies and no production impact (tables exist but nothing reads/writes them yet). We can ship Phase 1 in a single PR tonight or tomorrow, and it sets up everything else.

Phases 2-5 sequence after observing Phase 1 in production for a trading day.
