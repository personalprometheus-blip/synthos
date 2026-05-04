# Trader Gate I/O Audit — Distributed Migration Prep

**Created:** 2026-05-03
**Source:** `synthos_build/agents/retail_trade_logic_agent.py` (worktree: modest-moore-ac6c93)
**Purpose:** Catalog every DB read/write inside trader gates, map each to its destination in distributed mode (work packet preload, dispatcher post-processing, or removed). Required precursor to:
- Task 21 (extract gate 14 to dispatcher)
- Task 22 (build synthos_dispatcher.py)
- Task 23 (build synthos_trader_server.py)
- Task 30 (pre-fetch Alpaca quotes into packet)

---

## Methodology

Searched all gate function bodies (`gate1_system` through `gate14_evaluation`) for any call against `db.` (per-customer signals.db) or `_shared_db()` (process-node shared DB). Captured at line numbers from the live trader file.

---

## Per-Gate I/O Inventory

### gate1_system (line 1679)
**Reads (4):**
| Line | Call | Source DB | Disposition in distributed mode |
|---|---|---|---|
| 1705 | `db.get_portfolio()` | customer signals.db | **Preload** in `WorkPacket.state_snapshot.portfolio` |
| 1726 | `db.get_recent_outcomes(limit=50)` | customer signals.db | **Preload** in `WorkPacket.recent_outcomes` (cap at 100 to also feed gate 14) |
| 1767 | `db.get_setting('_VALIDATOR_VERDICT')` | customer signals.db | **Preload** as `WorkPacket.validator_verdict` (string field) |
| 1768 | `db.get_setting('_VALIDATOR_RESTRICTIONS')` | customer signals.db | **Preload** as new field `WorkPacket.validator_restrictions: list[str]` |

**Writes:** none.

**Notes:** Gate 1 is the cleanest case — pure pre-trade read against per-customer state. All four reads are bundled at packet build, gate runs in-memory.

---

### gate2_benchmark (line 1794)
**Reads:** none (Alpaca only — `alpaca.get_bars(BENCHMARK_SYMBOL, ...)`)
**Writes:** none.
**Disposition:** Gate runs unchanged. Alpaca call is the only I/O — handled either by pre-fetched bars in packet or live call (this gate runs once per cycle, not per signal — acceptable).

---

### gate3_regime (line 1853)
**Reads:** none (Alpaca only — `alpaca.get_bars(sym, days=60)`, `alpaca.get_bars("TLT", days=10)`)
**Writes:** none.
**Disposition:** Same as gate 2 — Alpaca-only, runs once per cycle. Pre-fetched bars eliminate the live call.

---

### gate4_eligibility (line 1939)
**Reads (3):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2015 | `_shared_db().get_fresh_news_flags_for_ticker(ticker)` | shared signals.db | **Preload** per-ticker map: `WorkPacket.news_flags: dict[ticker, list]` |
| 2031 | `_shared_db()` ref passed to helper | shared signals.db | Same — replace with the preloaded map |
| 2076 | `_shared_db().is_cooling_off(ticker)` | shared signals.db | **Already in customer state** (`cooling_off`) — refactor to look up locally |

**Writes:** none.

**Notes:** The `is_cooling_off` call duplicates state already in `customer_settings`/`cooling_off` — refactor to compute from the snapshot instead of re-querying.

---

### gate5_signal_score (line 2095)
**Reads:** none directly in gate — but reads from signal dict which carries pre-stamped `news_signal_score`, `sentiment_score`, etc.
**Writes:** none.
**Disposition:** Already pure-compute. No change needed.

---

### gate5_5_news_veto (line 2480)
**Reads (1):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2488 | `_shared_db().get_fresh_news_flags_for_ticker(ticker)` | shared signals.db | **Preload** — same `news_flags` map as gate4 |

**Writes:** none.
**Notes:** Single shared preload covers gate4 + gate5_5 — build the news_flags map once per cycle.

---

### gate6_entry (line 2510)
**Reads:** none (Alpaca quotes only)
**Writes:** none.
**Disposition:** No DB I/O. Quote fetches are the only network call.

---

### gate7_sizing (line 2653)
**Reads (1):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2681 | `db.get_setting('_ALPACA_EQUITY')` | customer signals.db | **Preload** in `WorkPacket.state_snapshot.portfolio['equity']` (already there — wire the gate to read it from the snapshot dict) |

**Writes:** none.

---

### gate8_risk (line 2773)
**Reads:** none (uses ATR/portfolio passed in)
**Writes:** none.
**Disposition:** Pure compute.

---

### gate11_portfolio (line 2821)
**Reads (1):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2838 | `db.get_setting('_ALPACA_EQUITY')` | customer signals.db | Same as gate7 — pull from snapshot |

**Writes:** none.

---

### gate13_stress (line 2889)
**Reads:** none (Alpaca only)
**Writes:** none.
**Disposition:** Alpaca-only. Same as gates 2/3/6.

---

### gate14_evaluation (line 2928)  ⚠️ MOVE TO DISPATCHER
**Reads (1):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2934 | `db.get_recent_outcomes(limit=100)` | customer signals.db | **N/A** — entire gate moves out of trader |

**Writes (1):**
| Line | Call | Source DB | Disposition |
|---|---|---|---|
| 2972 | `db.log_event("STRATEGY_KILL_CONDITION", ...)` | customer signals.db | **N/A** — write happens on dispatcher side after applying delta |

**Disposition:** Per conflict audit H2, this gate runs AFTER all trades and computes Sharpe/drawdown to decide whether to set a kill condition. It does not affect any trade decision in the current cycle. **Move it to dispatcher post-processing entirely** (Task 21). Trader returns `trade_outcomes_for_gate14: [...]` in the result delta; dispatcher invokes the gate14 logic locally with full DB access.

---

### Outside gates: signal acknowledgment writes
**5 call sites** to `_shared_db().acknowledge_signal(signal_id)` — outside any gate function but in the trader execution path:

| Line | Context | Disposition |
|---|---|---|
| 3562 | After position open (entry path) | **Append** to `delta.acknowledged_signal_ids` |
| 3604 | After position open (alternate entry) | Same |
| 4439 | After managed-mode approval queued | Same |
| 4690 | Main signal eval loop, success path | Same |
| 4719 | Main signal eval loop, alternate path | Same |

**Disposition:** All 5 sites swap `_shared_db().acknowledge_signal(id)` for `_PENDING_ACKS.append(id)` (or equivalent context-passed list). Dispatcher iterates the list when applying the delta, calling `acknowledge_signal()` server-side. (Task 14.)

---

## Summary By Disposition

### Preloaded into WorkPacket (no I/O during gate)
- portfolio, positions, cooling_off, recent_bot_orders (already planned)
- recent_outcomes (cap 100; serves both gate1 + gate14 input)
- validator_verdict, validator_restrictions
- news_flags map (per-ticker, covers gate4 + gate5_5)
- account equity (already in portfolio dict, just wire the gates)

### Written into result delta (deferred to dispatcher)
- acknowledged_signal_ids (all 5 ack sites)
- log_events (gate14 kill condition + any other `db.log_event` paths)
- positions_added, positions_closed, cash_delta, cooling_off_added, recent_bot_orders_added (already planned)
- trade_outcomes_for_gate14 (raw outcomes from this cycle's trades)

### Removed from trader entirely (moved to dispatcher post-processing)
- gate14_evaluation function body (Task 21)

### Unchanged (no I/O)
- gate2_benchmark, gate3_regime, gate5_signal_score, gate6_entry,
  gate8_risk, gate13_stress

---

## Implementation Order

1. **Task 14** (now): Wrap acknowledge_signal calls in DISPATCH_MODE branch — distributed appends to list, daemon mode unchanged.
2. **Task 21** (post-trip): Extract gate14_evaluation body into a standalone helper module that dispatcher imports and calls with master DB access.
3. **Task 22** (post-trip): Dispatcher builds WorkPacket with preloaded fields per the table above.
4. **Task 23** (post-trip): Trader server uses preloaded fields where currently it queries DB; daemon mode keeps current path via DISPATCH_MODE check.

---

## Verification

When all the above is done, run:
```bash
grep -n "db\.\|_shared_db()" synthos_build/agents/retail_trade_logic_agent.py \
  | grep -v "^.*: *#" \
  | grep -E "gate[0-9_]+_"
```
Should return empty (no DB calls inside gate function bodies).

The trader's main `run()` and execution-path code outside gates may still use DB — those go on a separate audit pass when building the dispatcher (Task 22).
