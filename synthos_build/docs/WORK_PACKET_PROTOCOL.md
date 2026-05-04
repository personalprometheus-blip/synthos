# Work Packet Protocol

**Created:** 2026-05-04
**Audience:** anyone building/modifying the dispatcher → trader_server path, or a third-party trader_server replacement
**Schema source of truth:** `src/work_packet.py` — `WorkPacket`, `WorkResult`, `StateDelta`, `TradeAction`, `AlpacaCreds`, `CustomerStateSnapshot`, `MarketContext` dataclasses
**Companion docs:** [CUTOVER_RUNBOOK.md](CUTOVER_RUNBOOK.md), [TRADER_GATE_IO_AUDIT.md](TRADER_GATE_IO_AUDIT.md), [MQTT_BROKER_OPERATIONS.md](MQTT_BROKER_OPERATIONS.md)

---

## Why this protocol exists

The distributed-trader path runs the trader on a different process
(potentially a different machine — future retail-N nodes) than the
process node that owns the master DBs. The trader needs every byte of
input it would have read from local SQLite, packaged into a single HTTP
request payload, plus a return shape that lets the dispatcher apply the
trader's mutations back to the master DBs in one transaction.

This protocol is HTTP/JSON, not MQTT. MQTT is reserved for telemetry
fan-out (heartbeats, prices, regime broadcasts). See
[MQTT_BROKER_OPERATIONS.md](MQTT_BROKER_OPERATIONS.md).

---

## Wire shape

### Request: POST /work

```http
POST http://<retail_url>:8443/work
Content-Type: application/json
X-Dispatch-Token: <DISPATCH_AUTH_TOKEN>      # required if server has token set

<WorkPacket as JSON body>
```

### Response: 200 OK with WorkResult JSON

```http
HTTP/1.1 200 OK
Content-Type: application/json

<WorkResult as JSON body>
```

The server returns 200 even on trader-side errors — the error is in the
response body's `error` field. This lets the dispatcher log + advance
without retry storms on persistent crashes. Network-level errors
(timeout, 5xx, connection refused) DO return non-200 and the dispatcher
treats those as "skip this customer this cycle, retry next cycle".

---

## WorkPacket schema (request body)

```jsonc
{
  // Identity
  "work_id":         "wrk-1714780800-a3b4c5d6",   // unique per dispatch
  "schema_version":  "1.0.0",                     // SemVer; major-mismatch = refuse
  "cycle":           "open",                      // open|midday|close|overnight
  "customer_id":     "30eff008-c27a-4c71-...",

  // Per-customer credentials (loaded from auth.db by dispatcher; trader uses them
  // to talk to Alpaca during this packet's execution; never persisted on retail)
  "alpaca_creds": {
    "key":      "PKAPI...",
    "secret":   "...",
    "base_url": "https://paper-api.alpaca.markets",  // or api.alpaca.markets for LIVE
    "data_url": "https://data.alpaca.markets"
  },

  // Frozen view of customer state at packet-build time. Trader runs gates
  // against this; mutations accumulate into the result delta (NOT mutated here).
  "state_snapshot": {
    "portfolio":          { "cash": 12345.67, "equity": 14000.00, "peak_equity": 14500.00 },
    "positions":          [ { "ticker": "AAPL", "qty": 10, "avg_entry_price": 200.10 } ],
    "cooling_off":        [ { "ticker": "TSLA", "expires_at": "2026-05-04T13:00:00Z" } ],
    "recent_bot_orders":  [ ],
    "customer_settings":  { /* per-customer trading params */ },
    "operating_mode":     "MANAGED",                // MANAGED | AUTOMATIC
    "trading_mode":       "PAPER"                   // PAPER | LIVE
  },

  // Validated signals to evaluate this cycle
  "signals": [ /* signal dicts as returned by get_signals_by_status(['VALIDATED']) */ ],

  // Cycle-wide market context — same across all customers in this batch
  "market_context": {
    "regime":             "BULL",
    "vix":                14.2,
    "market_state":       "OPEN",
    "market_state_score": 0.75,
    "session":            "open",
    "timestamp_utc":      "2026-05-04T13:30:00Z"
  },

  // Pre-fetched Alpaca quotes — dispatcher batches one multi-symbol call across
  // all customers' tickers, distributes per-customer subset here. Trader uses
  // these for in-memory gate eval instead of re-fetching.
  "quotes": {
    "AAPL": { "bid": 200.05, "ask": 200.10, "mid": 200.075, "ts": "..." },
    "MSFT": { "bid": 410.50, "ask": 410.60, "mid": 410.55, "ts": "..." }
  },

  // Validator stack output (per-customer in customer_settings)
  "validator_verdict": "GO",                         // GO | CAUTION | NO_GO

  // Last 100 outcomes for gate1 + gate14 inputs
  "recent_outcomes": [ /* outcome dicts */ ],

  // Hard deadline — trader must bail if exceeded
  "deadline_ts":      "2026-05-04T13:34:00Z",
  "dispatched_at_ts": "2026-05-04T13:30:00Z"
}
```

### Schema-version handshake

Server compares incoming `schema_version` MAJOR to its own
`SCHEMA_VERSION` ("1.0.0"). If they differ, server returns 200 with
`error: "schema_version mismatch: incoming=X.Y.Z server=A.B.C"` and
empty delta. Dispatcher logs and skips the customer for this cycle.

This prevents silent partial trades when dispatcher and retail are at
different deploy versions during a rolling update.

---

## WorkResult schema (response body)

```jsonc
{
  // Echo of request identity
  "work_id":        "wrk-1714780800-a3b4c5d6",
  "customer_id":    "30eff008-c27a-4c71-...",
  "schema_version": "1.0.0",
  "executed_by":    "retail-1",                   // NODE_ID of the server
  "completed_at_ts":"2026-05-04T13:30:18Z",
  "wall_clock_ms":  342.7,

  // What actually happened on Alpaca
  "actions": [
    {
      "signal_id":         "SIG-...",
      "ticker":            "AAPL",
      "side":              "buy",
      "qty":               5,
      "order_type":        "bracket",
      "requested_at_ts":   "...",
      "alpaca_order_id":   "ord-xyz",
      "fill_price":        200.10,
      "status":            "filled",
      "error_msg":         null
    }
  ],

  // Mutations the dispatcher MUST apply to master DBs
  "delta": {
    "positions_added":         [ /* position dicts */ ],
    "positions_closed":        [ /* position dicts */ ],
    "cash_delta":              -1000.50,                   // +/- to portfolio.cash
    "cooling_off_added":       [ /* {ticker, expires_at} */ ],
    "recent_bot_orders_added": [ /* settlement-lag guard rows */ ],
    "acknowledged_signal_ids": [ "SIG-001", "SIG-002" ],   // → flip VALIDATED→ACTED_ON in shared signals.db
    "log_events":              [ { "event": "STRATEGY_KILL_CONDITION", "agent": "trade_logic_agent", "details": "..." } ],
    "trade_outcomes_for_gate14": [ /* outcome dicts to feed gate14_evaluator */ ]
  },

  // Post-trade Alpaca snapshot — dispatcher can compare to delta to detect divergence
  "alpaca_reconciliation": {
    "queried_at": "2026-05-04T13:30:19Z",
    "positions_after": [ { "ticker": "AAPL", "qty": 15 } ],
    "cash_after": 11345.17
  },

  // Per-gate timings (from _GATE_TIMINGS_MS in trader)
  "gate_timings_ms": {
    "gate1_system":      { "total_ms": 12.4, "calls": 1, "avg_ms": 12.4, "max_ms": 12.4 },
    "gate5_signal_score":{ "total_ms":  8.1, "calls": 3, "avg_ms":  2.7, "max_ms":  3.4 }
  },

  "error": null    // populated if trader crashed mid-cycle; delta + actions are still applied
}
```

---

## How the dispatcher applies the delta

Single-writer pattern on master DBs. Order matters for atomicity:

1. **Acknowledge signals first** — flip `signals.status` `VALIDATED→ACTED_ON`
   in shared signals.db for every id in `acknowledged_signal_ids`. This
   prevents the next cycle from re-issuing the same signal.
2. **Apply position deltas** to per-customer DB (`positions` table).
3. **Apply cash delta** to per-customer `portfolio.cash`.
4. **Insert `cooling_off_added` + `recent_bot_orders_added`** rows.
5. **Persist `log_events`** to per-customer `events` table.
6. **Run gate14 dispatcher-side** using `gate14_evaluator.evaluate_strategy_kill`
   with master-DB `recent_outcomes` (which now includes the trades just
   added via `trade_outcomes_for_gate14`). If kill condition met, write
   STRATEGY_KILL_CONDITION event.

Each step is best-effort + logged. A failure on step N does not roll
back steps 1..N-1 — Alpaca is the truth, and the next cycle reconciles
state from the Alpaca snapshot regardless.

---

## Authentication

| Server-side | Dispatcher-side |
|---|---|
| `DISPATCH_AUTH_TOKEN` env var on retail node. If empty, auth disabled (dev only). | `DISPATCH_AUTH_TOKEN` env var on process node, sent in `X-Dispatch-Token` header. |
| Mismatched token → 401 with `{"detail": "invalid X-Dispatch-Token"}`. | On 401, dispatcher logs + skips that customer. |

In production both nodes read from `synthos_build/user/.env`. The token
should be ≥32 chars, generated via `openssl rand -base64 24`.

---

## Lifecycle endpoints

| Method | Path | Purpose | Returns |
|---|---|---|---|
| GET | `/healthz` | Liveness probe | `{"status": "ok", "ts": <epoch>}` |
| GET | `/readyz` | Readiness — can we accept /work? | `{"status": "ready", "dispatch_mode": "distributed", "schema_version": "1.0.0"}` or 503 |
| GET | `/version` | Service version | `{"schema_version": "1.0.0", "service": "synthos_trader_server"}` |
| POST | `/work` | Execute one work packet | `WorkResult` (always 200 unless network error) |

---

## Failure semantics

| Scenario | Server response | Dispatcher action |
|---|---|---|
| Token mismatch | 401 | Log + skip customer this cycle |
| Schema major mismatch | 200 with `error: "..."`, empty delta | Log + skip customer |
| Trader crashed mid-cycle | 200 with `error: "trader crash: X"`, partial delta | Apply partial delta; log; advance |
| Alpaca call from trader failed | 200 with `actions[i].status="rejected"`, no fill_price | Apply delta normally; signal stays VALIDATED for re-eval |
| Network timeout (no response) | (no response — connection drops) | Log; treat as skip; retry next cycle |
| Server returns non-200, non-401 | propagated | Log; treat as skip |

**Idempotency:** A repeated POST with the same `work_id` is NOT
deduplicated server-side. Dispatcher must not retry within a cycle. The
30s cycle interval is the natural retry boundary.

---

## Concurrency model

Server-side: each `/work` request runs in `asyncio.to_thread` (Tier 6
addition). FastAPI/uvicorn handles the asyncio event loop; the trader
itself stays sync inside the thread. Multiple concurrent `/work`
requests overlap on the event loop — measured 4.9x speedup with 5
concurrent customers.

Dispatcher-side: `ThreadPoolExecutor` with `DISPATCH_MAX_PARALLEL=8`
(default). Each thread holds one HTTP POST in flight. requests.Session
is thread-safe.

---

## Adding a field to the protocol

1. Bump `WORK_PACKET_SCHEMA_VERSION` in `src/work_packet.py`:
   - **Patch** (1.0.0 → 1.0.1): add an optional field; existing clients ignore it
   - **Minor** (1.0.0 → 1.1.0): add a required-by-server field with sane default
   - **Major** (1.0.0 → 2.0.0): break the wire shape; coordinated deploy required (server first, then dispatcher)
2. Update the dataclass + `to_json()`/`from_json()` if needed.
3. Update this doc and `system_architecture.json`.
4. Server's schema-version handshake will refuse mismatched majors —
   leverage that for safe rolling updates.
