# System Audit — Phase B (manual subsystem review)

Continuation of `AUDIT_2026-04-20.md`. Five parallel reviewer agents read
the trader, market daemon, database layer, Phase 4/5 modules, and
enrichment pipeline. **Total raw findings: ~139.** After verification
against the actual code, the list below contains only the ones that
survived scrutiny. False positives explicitly called out in §6.

Severity legend: **CRIT** = active or imminent bug · **HIGH** = latent
correctness risk · **MED** = quality / observability · **LOW** = cosmetic.

---

## 1. Trader — `retail_trade_logic_agent.py`

### HIGH
- **Gate 11 uses `cash` instead of total equity** for sizing checks
  (~line 2105). Gate 7 already sizes off `_ALPACA_EQUITY` setting. This
  means sizing inputs across two gates disagree when USER-managed
  positions hold most of the cash — Gate 7 sees full equity, Gate 11
  only cash. Fix: read the same `_ALPACA_EQUITY` setting Gate 7 uses.
- **Stale `positions` list inside signal-evaluation loop** (~line 3314
  and throughout the for-loop). `positions`, `portfolio`, `tradeable`
  cached at loop top, but rotation + earlier buys mutate state. Later
  signals in the same run evaluate against stale deployment %. Fix:
  refetch per signal, or document that the staleness is intentional
  for perf.
- **Three dead `if True:` placeholders** at lines 3220, 3249, 3326.
  These are the same ones vulture flagged as "redundant if-condition"
  in Phase A (deferred). Context says "Gate 0 already verified" —
  leftover refactor scaffolding. Fix: delete the branches (keep bodies
  at outer indent).

### MED
- **Uninitialized `screening_adj` / `screening_info` logging**
  (Gate 5, around line 1790). Referenced in the `decision_log.gate(...)`
  call outside the `if scr_score is not None` block that defines them.
  When `scr_score` is None, KeyError. Fix: initialize at function top.
- **Silent exception swallows in Alpaca HTTP logging** (API call log
  writes) around lines 960/1006/1027/1094/1190. Bare `except Exception:
  pass`. Fix: demote to `log.debug()` at minimum — DB connectivity
  issues currently invisible.
- **Drawdown divisor could hit `0` semantics** (~line 1279). Guard
  `if peak > 0 else 0` technically avoids ZeroDivisionError, but
  `peak = portfolio.get('peak_equity') or portfolio.get('cash', 0)`
  can give `peak=0` if both missing. Means drawdown silently reports
  0. Fix: fallback to equity, not cash; never allow 0.
- **Dict access assumes schema** at lines 2060 (`['is_overnight']`),
  2600 (`h['pnl_pct']`), 3220 (`minor['entry_low']`) etc. Should use
  `.get(key, default)` with sensible defaults.

### LOW
- **Stale import inside function** at ~line 2274
  (`from datetime import timezone as _tz`). Redundant — now that
  `timezone` is imported module-level in Round 1. Cleanup.
- **Docstring says "cron-scheduled"** in places where systemd now
  drives it. Doc drift.

---

## 2. Market daemon — `retail_market_daemon.py`

### HIGH
- **`run_trade_all_customers()` return value not used**. The dispatcher
  returns `(ok, fail)` but the idle loop calls it and drops the result.
  Silent when every trader fails. Fix: log `fail > 0` and maybe alert.
- **DB connections opened in hot loops without close** —
  `run_pre_open_reeval`, `run_exit_backfill`, `run_trail_optimizer`
  all do `db = get_customer_db(cid)` per customer with no explicit
  close. `get_customer_db()` caches the instance so this is a
  file-descriptor / WAL issue rather than a leak-per-iteration, but
  worth confirming the cache's eviction semantics.
- **Duplicate-notification race in `check_approval_notifications()`**
  (~line 1154). The `abs(t.minute - check_m) <= 2` match window can
  fire twice inside the same minute if the idle loop ticks twice.
  Currently no "already-notified" guard. Fix: track last-fire timestamp
  or dedup on approval id.
- **Hardcoded owner UUID fallback** (~line 514/536). If `OWNER_CUSTOMER_ID`
  env is unset, we fall back to a hardcoded UUID. If someone deployed a
  fresh instance with a different owner, the whole stack silently
  writes to the wrong DB. Fix: fail fast at startup if env unset.

### MED
- **Bare `except: pass` in pidlock release** (~line 186) — stale pidfile
  failures invisible. `log.debug()` minimum.
- **`r.json()` calls without try/except** in approval-notify HTTP paths
  — malformed response = uncaught JSONDecodeError.
- **stderr captured with `[-200:]` slice** — shows the tail of an error,
  missing the traceback prefix that usually has the real cause. Change
  to `[:400]`.
- **Recurring subprocess dispatch** — `kill()` then `wait(timeout=5)`
  but no `SIGKILL` fallback. If the child blocks SIGTERM, pool slot
  stays occupied until the next watchdog cycle.

### LOW
- **Stale `cron` mentions in docstring** (module header says cron-based;
  it's systemd now).

---

## 3. Database — `retail_database.py`

### CRIT
- **Missing composite index on `signals(ticker, tx_date, status)`**.
  Verified — only `idx_signals_status` + `idx_signals_ticker` exist as
  separate single-column indexes. News agent's dedup runs this filter on
  every incoming article, can't cover. Fix: add migration.
- **`entry_signal_score` declared `TEXT`** (lines 700, 703). Values
  stored as `f"{x:.4f}"`. This means:
  - SQL `ORDER BY entry_signal_score` sorts lexicographically
    (`"0.7890" > "0.1000"` but `"0.7890" < "0.8100"` correctly; not
    all edge cases break, but it's fragile)
  - Comparisons against numeric constants rely on SQLite's type
    affinity; some paths will coerce, some won't
  - Gate 5 composite reads `entry_signal_score` via `float(...)` so
    it's OK at read time
  Fix: migrate to `REAL`, update writers to store as number.

### HIGH
- **Transaction race in `close_position()` + `update_portfolio()`**.
  Read-modify-write across separate `with self.conn() as c:` blocks.
  Two concurrent `close_position()` calls for the same customer (e.g.
  rotation firing simultaneously with manual close) can lose one
  write. WAL mode helps but doesn't prevent it. Fix: wrap the read
  and updates in a single transaction or add a rowversion check.
- **Missing index usable for `api_calls` date scans**. `idx_api_calls_ts`
  covers `timestamp` but dashboard queries use `date(timestamp) = ?`,
  which can't use the index (function on column). Fix: rewrite queries
  as range: `timestamp >= 'YYYY-MM-DD' AND timestamp < 'YYYY-MM-DD+1'`.
- **Silent cooling-off registration failure** in `close_position()`
  (~line 1696). `except Exception as _e: log.debug(...)`. If cooling_off
  write fails, trader will re-enter the losing ticker immediately next
  cycle — silently defeats the whole point. Fix: log at WARNING; maybe
  queue retry.
- **`busy_timeout` inconsistency** — 30 s in `conn()`, 15 s in
  `_init_schema()` / `_run_migrations()`. Migrations under contention
  could time out earlier than normal queries. Fix: standardize (15 s
  is fine; bump migrations if they need more).

### MED
- **Float money math** — `proceeds = round(exit_price * shares, 2)`.
  Enough rounding for typical trades, but accumulates across months.
  Fix: move cash math to `decimal.Decimal`.
- **`stamp_signals_screener()` uses f-string placeholder construction**
  (~line 2006). Placeholders are bound, so no injection — but the
  pattern is error-prone for future modifiers. Add a comment warning.
- **`PRAGMA integrity_check()` reads first row only** (~line 3614).
  Integrity errors return multiple rows; checking only the first
  misses additional corruption. Fix: scan all rows, count non-OK.

### LOW
- **`_STAMPABLE_STATUSES` leading underscore on a const used in 6+
  public methods.** Rename to `STAMPABLE_STATUSES`.
- **`STARTING_CAPITAL` env var has no bounds**. Accepts 0 / negative.

---

## 4. Phase 4/5 modules

### HIGH
- **`retail_price_poller` doesn't dedupe tickers across customers**
  (~lines 178-201). Stage 1 iterates customer-by-customer through
  `/v2/positions`. Two customers holding AAPL → AAPL's position row
  fetched twice in one poll cycle. Harmless but wasteful and accelerates
  rate-limit risk. Fix: aggregate customer creds first, fetch each
  ticker once.
- **`retail_event_calendar.refresh_earnings_calendar()` has no
  same-day guard**. Market daemon's enrichment tick calls this every
  30 min — currently re-fetches all 10 business days from Nasdaq each
  call. Fix: check "cache fetched_at within last N hours" and skip.
- **Window calculator multi-write atomicity** — macro and minor rows
  written in separate calls per signal × customer. If the minor write
  fails, macro is pinned (4a) without its refire zone. Fix: wrap in
  a single `write_trade_windows_pair()` helper or explicit transaction.
- **`retail_daily_master.render_report` queries use naive date
  comparison** (`opened_at >= '2026-04-20'`). If DB stores UTC iso,
  the `2026-04-20` bin captures 20:00 ET on the 19th onward as a
  Monday — mis-bins the last four hours of Sunday into Monday's
  report. Fix: compute ET-day boundaries as UTC iso strings.

### MED
- **`retail_price_poller` stage 2 missing status-code log** on non-200.
  Same issue flagged in Round 5 for `_fetch_nasdaq_day`. Fix: log `.status_code`.
- **`retail_event_calendar._fetch_nasdaq_day` bare except**.
  Returns [] on any error without logging. A 401 / 503 looks like
  "no earnings today" — worth at least `log.debug`.
- **`retail_daily_master` per-customer exception skip** — if one
  customer's DB throws, that customer's rows silently missing from
  the report. Summary still shows "N customers." Fix: track
  `successful_customers` / `failed_customers`; surface in header.
- **`retail_daily_master.generate` not atomic write** — if
  `write_text()` interrupted, partial file left. Fix: tempfile +
  `os.replace()`.
- **`retail_window_calculator` `run_refresh_pass` is a stub** — still
  prints "refresh mode is a stub" and returns. Either finish it
  (Phase 4+), delete the argparse option, or raise NotImplementedError.

### LOW
- **`retail_tradable_cache._ensure_table` duplicated** with the main
  MIGRATIONS entry added in Round 5. The module-local version is
  now redundant (schema guaranteed by main migrations). Cleanup.

---

## 5. Enrichment pipeline

### HIGH
- **`retail_market_sentiment_agent.fetch_with_retry` lacks 429/5xx
  retry** (~line 90). News agent has it; sentiment doesn't. Under
  rate limit, sentiment hard-fails instead of backing off. Fix: mirror
  news_agent's pattern.
- **Validator stamps staleness check** (`retail_validator_stack_agent.py`
  ~line 454). If `_MARKET_STATE_UPDATED` setting is missing, code
  treats "age=None" as "fresh." Should be the opposite — missing
  timestamp = STALE. Fix: explicit check for None, treat as stale.
- **Cross-agent stamp contract is implicit**. News stamps
  `interrogation_status`, Market-State stamps `market_state_at_validation`,
  etc. If any stamp-writer fails silently, signal stalls at QUEUED
  forever and nothing surfaces it (watchdog only catches "no
  promotion for N minutes"). Fix: stamp writers should log the
  ticker + stamp name on failure.
- **`gate1_system` paths return "UNAVAILABLE" but proceed**
  (news + sentiment agents). When upstream data is unavailable,
  downstream gates score against empty dicts. Fix: short-circuit the
  whole agent run with `result='FAIL' severity='CRITICAL'` when
  gate1 has no data.

### MED
- **Sentiment agent `stamp_signals_sentiment` error log missing
  ticker** (~line 2663). `log.warning(f"stamp failed: {_e}")` —
  which ticker? Fix: add `{ticker}={score}` context.
- **`retail_sector_screener._fetch_bars_alpaca` bare except on api_call
  log** (~line 348). `except Exception: pass` inside the fallback.
  `log.debug(...)` minimum.
- **`retail_macro_regime_agent` `_fetch_alpaca_bars` uses scalar
  timeout** (~line 147). Other agents use `(5, REQUEST_TIMEOUT)`
  tuple. Slow-DNS scenario eats the full timeout budget. Fix:
  normalize.
- **Circuit breakers never log "still open"** across multiple
  fallback fetches. Hard to trace whether Alpaca recovered partway.
  Fix: `log.info("[CIRCUIT] Alpaca still tripped, using Yahoo")`
  per fallback.

### LOW
- Plenty of honest `TODO: DATA_DEPENDENCY` markers (VIX, credit
  spreads, social feeds, etc.) that describe real missing feeds.
  Behavior is correct (gates degrade to fallbacks). Don't delete them
  — they're documentation of what's paid-feed-blocked.

---

## 6. False positives / verified not-a-bug

Flagged by reviewers but rejected after source verification:

- **"`candidate_generator` metric inversion"** — agent claimed
  `updated` was counting duplicates as "updated" incorrectly. Read
  the code: `updated += 1` fires when `new_id is None`, which
  corresponds to the `db.add_candidate_signal()` on-conflict UPDATE
  path. That's exactly what "updated" means. Label is correct.
- **"`tradable_cache` uses wrong field name `a.get('class')`"** —
  agent suggested this should be `asset_class`. Verified against
  Alpaca's `/v2/assets` response format: the field is literally
  `class`. Code is correct; `asset_class` is the request parameter,
  not the response key.
- **"Context manager doesn't commit"** — several flags on DB writes
  claiming commits were missing. SQLite connection objects used via
  `with self.conn() as c:` commit on exit of the context manager
  (see `conn()` definition — it commits unless an exception set
  rollback). All flagged sites are fine.
- **"f-string SQL injection risk"** in database.py — every flagged
  site had bound parameters for the actual data; only column names
  or placeholder patterns were f-string'd, which is safe.
- **Many "unused HTML ID"** warnings from portal_lint — these are
  mostly selector-based CSS references or pre-rendered slots that
  JS populates. Need hand-review but most aren't dead.

---

## 7. Recommended fix order

**Round 6 (small + high value):**
1. Add composite index `signals(ticker, tx_date, status)` — 1-line
   migration, measurable hot-path speedup.
2. Fix `api_calls` date-scan query to use range (no index needed).
3. Add same-day guard to `refresh_earnings_calendar()` — cuts
   Nasdaq HTTP calls ~12×.
4. Delete the three `if True:` trader placeholders.
5. Add `log.debug()` to the bare-except patterns in news_agent,
   sentiment_agent, macro_agent, price_poller (scripted pass).
6. Stamp-writer failure logging — include ticker + stamp name.

**Round 7 (medium, needs care):**
7. `entry_signal_score` TEXT → REAL migration. Must cope with
   existing stringified data across all per-customer DBs.
8. Transaction-wrap `close_position()` + `update_portfolio()`.
9. Sentiment agent 429/5xx retry.
10. Gate 11 equity source alignment with Gate 7.
11. Gate 1 in news/sentiment should fail CRITICAL, not proceed.
12. `daily_master` ET-day binning.

**Round 8 (deferred / requires design):**
- Trader complexity refactor (`run` is 184 on radon).
- Portal complexity + template-var cleanup.
- `decimal`-based money math replacement.
- Validator missing-timestamp staleness semantics (needs confirmed
  policy).

---

## 8. Summary by subsystem

| Subsystem | CRIT | HIGH | MED | LOW | Total real |
|---|---|---|---|---|---|
| Trader | 0 | 3 | 4 | 2 | 9 |
| Market daemon | 0 | 4 | 4 | 1 | 9 |
| Database | 2 | 4 | 3 | 2 | 11 |
| Phase 4/5 modules | 0 | 4 | 5 | 1 | 10 |
| Enrichment pipeline | 0 | 4 | 4 | 1 | 9 |
| **Total** | **2** | **19** | **20** | **7** | **48** |

Down from ~139 raw agent findings to 48 verified. Biggest bucket is
**HIGH latent issues** (19) — patterns that aren't firing today but
will under specific load or failure conditions.
