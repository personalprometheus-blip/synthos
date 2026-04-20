# System Audit — Phase B re-run (post-Round-6/7/8)

Same 5 reviewer agents, same files, briefed on what Rounds 6-8 fixed so
they'd focus on what's still broken rather than re-flag the fixes.
Companion doc to `AUDIT_2026-04-20-phase-b.md`.

---

## 1. Delta summary

| Subsystem | Phase B (raw) | Phase B re-run | Delta |
|---|---|---|---|
| Trader | 23 | 11 | −12 (−52%) |
| Market daemon | 22 | 11 | −11 (−50%) |
| Database | 20 | 15 | −5 (−25%) |
| Phase 4/5 modules | 37 | 18 | −19 (−51%) |
| Enrichment pipeline | 37 | 15 | −22 (−59%) |
| **Total** | **139** | **70** | **−69 (−50%)** |

Reviewers explicitly confirmed these fixes stuck:
- Candidate-gen `tx_date` dedup (Round 1)
- Gate 4 ticker dedup (Round 1)
- `import time` in sector_screener (Round 1)
- `timezone` / `trade_events` in trader (Round 1)
- `.known_good/` deletion (Round 4)
- Macro window anchor pin (Round 4)
- BIL concentration alert (Round 5)
- Tradable-asset cache (Round 5)
- Composite index `signals(ticker, tx_date, status)` (Round 6)
- `api_calls` date-range query rewrite for **get_api_call_counts** (Round 6)
- Same-day guard on earnings refresh (Round 6)
- `if True:` trader placeholders deleted (Round 6)
- `log_api_call` bare-excepts downgraded to `log.debug` (Round 6)
- `entry_signal_score` TEXT→REAL + migration (Round 7)
- `close_position()` atomic transaction (Round 7)
- Sentiment `fetch_with_retry` tuple timeout + status-code log (Round 7)
- Gate 11 equity source alignment (Round 7)
- `daily_master` ET-day binning (Round 7)
- Validator missing-timestamp = STALE (Round 8)

---

## 2. New real findings the re-run caught

These are genuine issues the re-run caught (either I missed a sibling
site when fixing, or the reviewer found something neither pass had
surfaced before). Triaged down from the 70 raw findings.

### Sibling sites I missed when fixing (clear Round 9 work)

**R9-1 (CRIT)** — `retail_database.py::reduce_position()` has the same
read-then-write race I just fixed in `close_position()`. Reads
portfolio outside the transaction, then calls `update_portfolio()` in
a separate connection. Two concurrent partial exits can race-read
cash=X and each write X+their_proceeds, losing one. **Fix: same pattern
as Round 7.2 on `close_position` — read portfolio inside the single
transaction, inline the updates.**

**R9-2 (HIGH)** — `retail_database.py::get_api_call_history()` still
uses `date(timestamp)` in its WHERE / GROUP BY. Round 6 fixed this for
`get_api_call_counts()` but missed the sibling `_history()` method.
Same rewrite (timestamp range) fixes it.

**R9-3 (HIGH)** — `retail_database.py::update_portfolio()` keeps the
read-modify-write pattern even after Round 7 fixed close_position.
It's called from multiple places (sweep_monthly_tax, admin adjust,
etc.) so the race is real. **Fix: read + update in a single
`with self.conn() as c:` block.**

### Transactional boundaries on multi-row upsert loops

**R9-4 (HIGH)** — `retail_price_poller.py`: the stale-ticker cleanup
`DELETE FROM live_prices WHERE ticker NOT IN (...)` happens **after**
the per-ticker INSERT loop. If the process crashes between (after N
inserts), stale rows stay until the next full poll.

**R9-5 (MED)** — Three upsert loops not wrapped in explicit
transactions: `price_poller`'s INSERT loop, `event_calendar`'s
earnings_cache upsert, `tradable_cache`'s 13k-row refresh. On a crash
mid-loop, the table is half-updated. Fix: `BEGIN ... COMMIT` wrap or
`executemany`.

### Missing index

**R9-6 (HIGH)** — `cooling_off` has `idx_cooling_off_until` but
`is_cooling_off()` queries on `(ticker, cool_until)`. Needs a
composite index to cover both filter columns. Trader hits this on
every Gate 4 call.

### Unsafe dict access in trader

**R9-7 (MED)** — Several trader gate entries use `signal['ticker']`
(and similar `portfolio['cash']`) direct-subscript access. If the
upstream dict is missing the key (malformed signal / corrupt
portfolio row), KeyError instead of a logged skip. Low current-risk
because the DB layer populates reliably; still worth tightening to
`.get(key, default)` or explicit guard.

### API / response handling

**R9-8 (HIGH)** — `retail_market_daemon.py`: `run_exit_backfill()`
and `_send_sms/_email` don't check `r.status_code` / `.ok` before
consuming the response. A 401 / 429 / 500 silently returns success.
Fix: explicit `if not r.ok: log + continue`.

**R9-9 (MED)** — `retail_tradable_cache.refresh()` doesn't check
pagination on `/v2/assets`. If Alpaca silently paginates at 1000 rows,
we'd be caching only the first page and calling every ticker past
that "untradable." Fix: check for `next_page_token` or `Link` header;
raise warning if response size hits a suspicious round number.

### Observability gaps

**R9-10 (MED)** — `retail_trade_logic_agent.py::get_atr()` and
`get_volume_avg()` return None / 0 on data failure without logging
the reason. Gate 4 liquidity and Gate 5 sizing silently behave as if
ATR is missing. Fix: `log.debug` with ticker + "insufficient bars" /
"fetch failed".

**R9-11 (MED)** — Several `except Exception: pass` / `log.debug`
sites across housekeeping methods (`cleanup_api_calls`,
`get_api_call_summary`, `get_halt`) still swallow without surfacing
context. Lower priority than the earlier bare-except sweep (these are
less hot-path) but same principle.

---

## 3. Subsystem-level observations from the reviewers

### Trader — "substantially cleaner"
Reviewer explicitly called this out. No more bare-except in critical
paths. Remaining items are edge-case state mismatches (unsafe dict
subscripts) rather than structural bugs.

### Market daemon — Round 6 fixes held
No more `log_api_call` bare-except flags. Remaining items are
categories I deferred (stderr tail truncation, SIGKILL without
SIGTERM grace, overly broad HTTP exception handling).

### Database — 4 specific misses caught
The reviewer found the sibling sites I missed (reduce_position race,
get_api_call_history date(), is_cooling_off index, update_portfolio
race). These are the clearest Round 9 wins.

### Phase 4/5 modules — "No regressions detected"
All recent fixes confirmed intact. Remaining items cluster on
transactional atomicity of multi-row upsert loops — a category
Round 7 only addressed for `close_position`.

### Enrichment pipeline — "Clears for Phase 6"
Reviewer's own summary. Zero CRIT findings across all 8 files.
Biggest remaining item is tech-debt on stale TODO markers (pending
paid data feeds) — not a bug.

---

## 4. Recommended Round 9 (if you want it)

Tight scope — 4 small fixes and 3 transactional wraps, all parallel
to patterns we already applied:

1. `reduce_position()` → single-transaction (mirror Round 7.2)
2. `update_portfolio()` → read+write inside one transaction
3. `get_api_call_history()` → replace `date(timestamp) = ?` with range (mirror Round 6.2)
4. Composite index on `cooling_off(ticker, cool_until)`
5. `price_poller` upsert → wrap loop in `BEGIN/COMMIT` + move DELETE-stale before INSERT
6. `tradable_cache` → pagination check on `/v2/assets` + transaction wrap
7. `event_calendar` → transaction wrap on earnings_cache upsert

These should land on one patch branch. Each has a clear pattern from
earlier rounds and the test burden is low (same smoke path as before).

---

## 5. Still deferred (unchanged)

The 6 items documented in `AUDIT_2026-04-20-deferred.md` are unchanged:
- Trader `run()` complexity refactor (radon 184)
- Portal complexity refactor
- Decimal money math replacement
- Blanket `utcnow()` → `now(timezone.utc)` replacement
- Portal template var / unused-ID hand audit
- Duplicate-helper consolidation

Each has a trigger documented for when to revisit.
