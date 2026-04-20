# Post-Phase-C Audit — 5-agent review

Fresh audit run after all 4 Phase C patches shipped (D1 trader refactor,
D2 portal refactor, D4 utcnow, D5 dead IDs, D6 retail_shared.py).
Companion doc to `AUDIT_2026-04-20-phase-b-rerun.md`.

5 reviewer agents (Explore type) ran in parallel — one each on: trader,
portal, database, daemon+shared, enrichment pipeline. Each was briefed on
what Round 9 + Phase C had already shipped so they'd focus on what's
still broken rather than re-flag completed work.

---

## 1. Raw yield

| Subsystem | Agent findings | Real after triage |
|---|---:|---:|
| Trader (`retail_trade_logic_agent.py`) | 12 | 3 |
| Portal (`retail_portal.py`) | 10 | 4 |
| Database (`retail_database.py`) | 6 | 3 |
| Daemon + shared (`retail_market_daemon.py`, `retail_scheduler.py`, `retail_shared.py`) | 6 | 2 |
| Enrichment (news, sector, event_calendar, etc.) | 6 | 2 |
| **Total** | **40** | **14** |

65% false-positive rate — consistent with past audits where reviewers
over-flag defensive-coding suggestions as bugs.

---

## 2. What the refactors preserved

Reviewers explicitly confirmed no regressions from the Phase C patches:

- **D1 (trader `run()` 897→15)** — all 11 helper extractions pass behavior
  preservation review. Parameter flow (`now`, `session`, `session_log`,
  `trade_events`) threads correctly. No lost `session_log.commit()` or
  `db.log_heartbeat()` calls.
- **D2 (portal hotspots)** — all 12 extracted helpers preserve response
  shape. `_handle_checkout_completed` return-tuple handling is correct.
  `_aggregate_customer_market_activity` correctly re-implements the
  former inner functions as module-level helpers.
- **D4 (utcnow)** — zero remaining `datetime.utcnow()` sites confirmed.
  (But see R10-6: a separate `utcfromtimestamp` site was missed.)
- **D5 (dead IDs)** — all 14 removed IDs confirmed unused; `snb-*`/`tab-*`
  correctly preserved (dynamic `showTab()` string concat).
- **D6 (retail_shared.py)** — 3 consolidated helpers behaviorally identical
  to their pre-consolidation versions. (But see R10-7: scheduler has
  its own divergent `is_market_hours()` that D6 missed.)

---

## 3. Real findings — Round 10 candidate work

### Sibling sites we missed when fixing (same pattern as Round 9)

**R10-1 (HIGH)** — `retail_database.py::open_position()` (lines 1146–1194)
has the same read-then-write race as `close_position`/`reduce_position`/
`update_portfolio`. Reads portfolio at line 1167 outside the transaction,
inserts position inside a `with self.conn()` at 1170–1183, then calls
`update_portfolio(cash=new_cash)` at 1185 in a separate connection. Two
concurrent opens can both read cash=X, each insert a position, then both
write cash=X-their_cost — losing one position's cash impact.
**Fix:** same pattern as R9-1/R9-3 — read portfolio, insert position,
update portfolio all within one `with self.conn()` block.

**R10-2 (HIGH)** — `retail_database.py::sweep_monthly_tax()` (lines
1106–1126) has the same race. Reads portfolio at 1111, calls
`update_portfolio()` at 1114 in a separate transaction. Monthly frequency
makes this low-incidence, but cash-destroying when it hits.
**Fix:** inline the portfolio read and update into a single transaction.

**R10-3 (HIGH)** — `_run_monthly_tax_sweep()` in trader line 3650:
`total_gains = portfolio['realized_gains'] + unrealized` — direct subscript
on portfolio dict. Round 9 defended this pattern in gates 4/5/5.5/6 but
missed this site in the tax sweep.
**Fix:** `portfolio.get('realized_gains', 0)`.

### Code introduced in Phase C with residual bugs

**R10-4 (HIGH)** — `_status_locked_mode()` at `retail_portal.py:3397–3443`
omits the `user_warnings` field that the normal path returns at line 3540.
If any JS reads `data.user_warnings.length` or similar, the locked-mode
response (served when the trader is actively running) throws
`Cannot read property 'length' of undefined`.
**Fix:** add `"user_warnings": []` to the locked-mode response dict.

**R10-5 (MED)** — `retail_portal.py::_enrich_single_db_position` at lines
3152–3169: `_sc = _sql.connect(...)` then `_sc.execute(...)` then
`_sc.close()` all inside a single try block. If the execute raises, close
never runs — connection leaks until garbage collection. Same pattern in
`_aggregate_customer_market_activity` at ~13927–13971.
**Fix:** wrap in `try/finally` or use the `with sqlite3.connect(...) as _sc:`
context-manager idiom.

### Pre-existing bugs reviewers found

**R10-6 (HIGH)** — `retail_sector_screener.py:428` uses
`datetime.utcfromtimestamp(int(ts))`. This method is deprecated in Python
3.12 and removed in 3.14. Phase C D4 only caught `utcnow()` — a grep for
the sibling `utcfromtimestamp` method was never run. Single site, 10-line
fix.
**Fix:** `datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)`.

**R10-7 (HIGH)** — `retail_scheduler.py:170–177` has its own
`is_market_hours()` that D6 missed. Scheduler's version uses
`dtime(9,30) <= t <= dtime(16,0)` (inclusive both ends); the shared
version at `retail_shared.py:111` uses `open_time <= now < close_time`
(exclusive close). At exactly 16:00:00 ET, scheduler says "open" but
shared says "closed" — divergent session dispatch for one second per day.
**Fix:** delete the local definition and `from retail_shared import
is_market_hours` (same pattern applied to market_daemon / trader / watchdog).

**R10-8 (HIGH)** — `retail_trade_logic_agent.py::sync_bil_reserve` lines
2532–2534 has dead code:
```python
if buy < C.BIL_REBALANCE_THRESHOLD:
    return
if buy >= C.BIL_REBALANCE_THRESHOLD:   # always True if we got here
    if alpaca._submit_notional(...):
```
The second `if` is unreachable as written (we'd have returned). Suggests
a refactor left a vestigial guard. Functionally harmless but misleading.
**Fix:** delete the inner `if buy >= C.BIL_REBALANCE_THRESHOLD:` wrapper
(keep the body).

**R10-9 (CRIT)** — `retail_news_agent.py::fetch_with_retry` at lines
545–587 — docstring comment at line 531–533 says "Resets on every
successful fetch", but the code only resets `_fetch_consecutive_failures`
at line 565, NOT `_fetch_circuit_open`. Once opened (3 consecutive
failures), the circuit stays open for the rest of the process lifetime —
ALL subsequent fetches short-circuit to None even after the upstream
recovers. One transient upstream outage → entire news-agent run dead in
the water.
**Fix:** add `_fetch_circuit_open = False` alongside the reset at line 565.

### Performance / robustness nits

**R10-10 (MED)** — `retail_database.py` — `pending_approvals` has single-column
indexes on `status` and `queued_at`, but `get_pending_approvals()` filters
on `status IN (...)` AND orders by `queued_at`. A composite
`(status, queued_at)` index would eliminate the post-filter sort. Portal
hits this on every dashboard render.
**Fix:** add `CREATE INDEX IF NOT EXISTS idx_approvals_status_queued ON
pending_approvals(status, queued_at)` to MIGRATIONS.

**R10-11 (MED)** — `retail_scheduler.py::SessionLock` doesn't detect stale
lock files. If the scheduler crashes while holding the lock (SIGKILL,
OOM, power loss), the `.scheduler_<session>.lock` file remains and the
next invocation waits 10 seconds then silently skips that session.
**Fix:** write the PID into the lock file and check `os.kill(pid, 0)` on
acquire; if the owner process is dead, clear and take the lock.

**R10-12 (MED)** — `retail_news_agent.py::fetch_with_retry` circuit breaker
is module-level mutable state — safe for the single-process news agent,
but `retail_sentiment_agent` has its own copy and the two can drift. D6
intentionally left this one unconsolidated (stateful globals); if we ever
need to share the breaker across agents, it needs lock protection.
**Fix:** document in `retail_shared.py` why `fetch_with_retry` is NOT
shared so a future reviewer doesn't paper over the divergence.

### Minor / optional

**R10-13 (LOW)** — `retail_trade_logic_agent.py` several catch-all
`except Exception: pass` blocks for politician weight updates and similar
non-critical accounting. Round 9 downgraded the worst offenders to
`log.debug`; a few remain.

**R10-14 (LOW)** — `retail_event_calendar.py::_business_day_offset`
potentially off-by-one for "earnings today, market not yet open"
scenarios. Not fully verified; reviewer flagged as plausible edge case
worth a unit test.

---

## 4. False positives (dismissed, with rationale)

Documented so a future audit doesn't re-flag them:

| Finding | Agent | Why false |
|---|---|---|
| Budget flag never reset between runs | Trader | Trader is cron-invoked as a fresh process; `_BUDGET_EXCEEDED = False` at module load is correct. |
| `sys.exit(0)` bypasses lock/watchdog cleanup | Trader | `if __name__ == '__main__':` block has `finally: _WATCHDOG_STOP.set(); release_agent_lock()` — `SystemExit` is caught and finally runs. |
| Watchdog left running on `run()` early return | Trader | Same — main block's finally catches it. |
| Portal timezone ternary "inverted" at line 10266 | Portal | Agent misread; logic is actually correct (`aware now if aware created else naive now`). |
| Confidence dict.get chain (line 3489) | Trader | `(signal.get('confidence') or 'LOW').upper()` is idiomatic, not unsafe. |
| `filled_orders` null check | Trader | Already handled with `(filled or [])` fallback. |
| Session activity lock race (portal) | Portal | Agent gave no concrete race scenario; 15-min timeout is approximate by design. |
| `_send_sms` 5xx escalation | Daemon | Round 9's `r.ok` check is sufficient; SMS is best-effort by design. |
| `now_et()` / `fetch_with_retry()` not consolidated | Daemon | Intentionally deferred in D6 (diverged return types / stateful globals). |

---

## 5. Triage → Round 10 patch plan

All 4 patches shipped and merged 2026-04-20 (same day as audit):

**Patch 10A (mechanical)** — SHIPPED `290c629`
- [x] R10-6: `datetime.utcfromtimestamp` → `fromtimestamp(tz=utc)` in sector_screener
- [x] R10-7: scheduler `is_market_hours` now delegates to `retail_shared`
- [x] R10-8: dead BIL rebalance guard removed
- [x] R10-9: news_agent circuit breaker now closes on recovery

**Patch 10B (database atomicity)** — SHIPPED `9eab677`
- [x] R10-1: `open_position()` — portfolio read + writes in one transaction
- [x] R10-2: `sweep_monthly_tax()` — same fix
- [x] R10-10: composite index `idx_approvals_status_queued`

**Patch 10C (portal hardening)** — SHIPPED `1758d89`
- [x] R10-3: `_run_monthly_tax_sweep` → `portfolio.get('realized_gains', 0)`
- [x] R10-4: `_status_locked_mode` returns `user_warnings=[]` (and fixed
  missing `critical_flags` + `user_warnings` on the error path too)
- [x] R10-5: two sqlite3 connections wrapped in context managers

**Patch 10D (cleanup + anti-re-flag docs)** — SHIPPED `18a05d8`
- [x] R10-12: `retail_shared.py` docstring explains why `fetch_with_retry`
  stays local
- [x] R10-13: politician-weight bare-excepts now `log.debug`
- [~] R10-11: NOOP — fcntl.flock is kernel-released on process death;
  docstring now explains so it's not re-flagged
- [~] R10-14: NOOP — `_business_day_offset` pairs with inclusive range
  check, "earnings today" IS blocked; docstring now explains

D3 (decimal money math) remains deferred until live capital.

---

_Last updated: 2026-04-20 (Round 10 complete — all 14 findings addressed,
  of which 12 were real code changes and 2 were doc-only triage NOOPs)_
