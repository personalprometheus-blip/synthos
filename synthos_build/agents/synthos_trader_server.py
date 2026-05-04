#!/usr/bin/env python3
"""
synthos_trader_server.py — FastAPI HTTP server on retail node.

Created 2026-05-04 as Tier 5 of the distributed-trader migration.

Receives WorkPackets from synthos_dispatcher (process node) over HTTP,
runs the trader gates against the in-memory packet data, places orders
on Alpaca with the per-customer credentials carried in the packet, and
returns a WorkResult with the StateDelta the dispatcher should apply.

This is a SKELETON. The current iteration intentionally:
  - Runs trader logic SYNCHRONOUSLY (one request at a time per worker).
    Async + asyncio.gather over multiple customers is Tier 6.
  - Does NOT rebuild the trader's full _run_signal_evaluation — instead
    it provides the contract surface (HTTP route, schema validation,
    delta accumulation, response). Wiring the actual gate execution
    against packet fields is the bulk of Tier 5 mock-test work (task 26).

Endpoints:
  POST /work       — execute one work packet, return WorkResult
  GET  /healthz    — liveness check (200 if process is up)
  GET  /readyz     — readiness check (200 if ready to accept /work)
  GET  /version    — schema version + git-rev for debugging

Auth:
  X-Dispatch-Token header must match DISPATCH_AUTH_TOKEN env var if set.
  Empty token = open (development only — set the token in production).

Failure modes:
  - Bad packet schema → 400 with error detail
  - Missing trader code → 500 with error detail
  - Trader crash mid-cycle → 200 with {error: "..."} so dispatcher can
    log + advance; partial deltas still applied to be safe
  - Auth failure → 401

Lifecycle:
  - Single uvicorn process, configurable workers (default 1 for skeleton)
  - DISPATCH_MODE env on this side should be 'distributed' so the trader
    module's CLI guard doesn't fire when imported here
"""

from __future__ import annotations
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── PATH SETUP ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "agents"))

from dotenv import load_dotenv
load_dotenv(str(_ROOT / "user" / ".env"))

# Force distributed mode BEFORE importing the trader so its CLI guard
# and acquire_agent_lock skip logic do the right thing on import.
os.environ.setdefault("DISPATCH_MODE", "distributed")

# ── LOGGING ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("trader_server")

# ── ENVIRONMENT ───────────────────────────────────────────────────────────
DISPATCH_AUTH_TOKEN = os.environ.get("DISPATCH_AUTH_TOKEN", "")
SERVER_HOST         = os.environ.get("TRADER_SERVER_HOST", "0.0.0.0")
SERVER_PORT         = int(os.environ.get("TRADER_SERVER_PORT", "8443"))
SCHEMA_VERSION      = "1.1.0"  # mirrors work_packet.WORK_PACKET_SCHEMA_VERSION

# 2026-05-04 — Phase A of retail-1-readiness: TRADER_DB_MODE controls
# whether trader_server runs the trader against real local DBs or
# against a WorkPacketDB mock.
#
#   "local"   (default) — loopback case (retail-N == process node).
#             Trader uses its normal _db() / _shared_db() handles which
#             open the local SQLite files. Works because trader_server
#             runs on Pi5 where all customer DBs live.
#
#   "packet"  remote case (retail-1 on separate hardware, post-cutover).
#             Trader's _db() and _shared_db() are monkey-patched to
#             return WorkPacketDB instances built from each work packet.
#             Reads come from packet snapshot; writes accumulate into
#             delta dicts that we return in WorkResult for the dispatcher
#             to apply to master DBs single-writer.
#
# Set on the retail node's environment (systemd EnvironmentFile or
# Environment= line). Each request resolves the mode at execution time
# so the same server binary works in both modes.
TRADER_DB_MODE = os.environ.get("TRADER_DB_MODE", "local").lower()
if TRADER_DB_MODE not in ("local", "packet"):
    log.warning(f"unknown TRADER_DB_MODE={TRADER_DB_MODE!r}; falling back to 'local'")
    TRADER_DB_MODE = "local"


# ── FASTAPI APP ───────────────────────────────────────────────────────────

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError as e:
    log.error(
        "FastAPI / uvicorn not installed. On the Pi: "
        "sudo apt install python3-fastapi python3-uvicorn  "
        "(or via pip in a venv). Error: %s", e
    )
    sys.exit(2)


app = FastAPI(
    title="Synthos Trader Server",
    description="Stateless retail-side trader for distributed mode",
    version=SCHEMA_VERSION,
)


# ── AUTH MIDDLEWARE ───────────────────────────────────────────────────────

def _check_auth(token_header: str | None) -> None:
    if not DISPATCH_AUTH_TOKEN:
        return  # auth disabled (dev only)
    if not token_header or token_header != DISPATCH_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid X-Dispatch-Token")


# ── HEALTH / READINESS / VERSION ──────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "ts": time.time()}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    """200 if we can import the trader module and it's in distributed mode."""
    try:
        import retail_trade_logic_agent as t
        return {
            "status": "ready",
            "dispatch_mode": t.DISPATCH_MODE,
            "schema_version": SCHEMA_VERSION,
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(e)})


@app.get("/version")
async def version() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "service": "synthos_trader_server",
    }


# ── WORK ENDPOINT ─────────────────────────────────────────────────────────

@app.post("/work")
async def work_endpoint(
    request: Request,
    x_dispatch_token: str | None = Header(None),
) -> dict[str, Any]:
    _check_auth(x_dispatch_token)

    payload = await request.json()
    work_id = payload.get("work_id", "?")
    customer_id = payload.get("customer_id", "?")
    cid_short = customer_id[:8]
    log.info(f"[{cid_short}] work {work_id} received")

    # Schema-version handshake: refuse mismatched majors so a dispatcher
    # update doesn't silently push a packet shape we can't parse.
    incoming_version = payload.get("schema_version", "0.0.0")
    if incoming_version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        return {
            "work_id": work_id,
            "customer_id": customer_id,
            "schema_version": SCHEMA_VERSION,
            "error": f"schema_version mismatch: incoming={incoming_version} server={SCHEMA_VERSION}",
            "delta": {},
            "actions": [],
            "alpaca_reconciliation": None,
            "completed_at_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "executed_by": os.environ.get("NODE_ID", "retail-?"),
        }

    # Tier 6 task 27: run the (sync) trader in a thread so the asyncio
    # event loop stays free to accept and process other concurrent /work
    # requests. uvicorn already runs each request in its own coroutine
    # context — to_thread is what gives us actual cross-customer
    # parallelism on the same retail node. The trader code itself remains
    # synchronous (Alpaca calls, SQLite reads in daemon mode); each
    # customer cycle gets its own thread, and Python's GIL releases
    # during I/O so concurrency is real.
    import asyncio
    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(_execute_packet, payload)
    except Exception as e:
        log.error(f"[{cid_short}] work {work_id} crashed: {e}", exc_info=True)
        result = {
            "work_id": work_id,
            "customer_id": customer_id,
            "schema_version": SCHEMA_VERSION,
            "error": f"trader crash: {type(e).__name__}: {e}",
            "delta": {},
            "actions": [],
            "alpaca_reconciliation": None,
            "executed_by": os.environ.get("NODE_ID", "retail-?"),
        }
    elapsed_ms = (time.perf_counter() - t0) * 1000
    result["completed_at_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["wall_clock_ms"] = round(elapsed_ms, 2)
    log.info(f"[{cid_short}] work {work_id} done in {elapsed_ms:.0f}ms")
    return result


# 2026-05-04 — Trader execution serialization lock (within one worker).
#
# The trader module reads ALPACA_API_KEY / ALPACA_SECRET_KEY /
# ALPACA_BASE_URL / ALPACA_DATA_URL / OPERATING_MODE / TRADING_MODE /
# _CUSTOMER_ID via Python's LOAD_GLOBAL opcode (bare-name lookups
# inside trader functions). LOAD_GLOBAL bypasses module __getattr__
# and reads the module __dict__ directly. So stamps must be on the
# module dict, and concurrent stamps would race across customers.
#
# The fix would be the "73-callsite refactor" — change every bare
# reference in the trader to an accessor call (`ALPACA_API_KEY` →
# `_get_api_key()`). Investigated 2026-05-04 and DEFERRED: high
# maintenance cost, marginal gain over uvicorn multi-worker.
#
# Live concurrency story:
#   - Within ONE worker process: this _TRADER_LOCK serializes execution.
#     Customer A finishes, stamps cleared, Customer B starts. Correct
#     by construction — at no point are two customer's stamps live.
#   - Across N worker processes (UVICORN_WORKERS=N, set in the
#     install_retail_node.py systemd unit): each worker has its own
#     module globals, its own lock. N customers run truly in parallel.
#     Live test on Pi5: 3.3x speedup with WORKERS=3 + 5 concurrent
#     requests.
#
# So the lock is the single-process correctness boundary; multi-worker
# uvicorn is the parallelism. Both compose cleanly.
import threading
_TRADER_LOCK = threading.Lock()


def _execute_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Execute one work packet end-to-end. WIRED 2026-05-04.

    Flow (loopback case, retail-N == process node):
      1. Set per-thread request context (creds + modes) via
         t._set_request_context — thread-local, no global mutation
      2. Call trader._apply_customer_settings() to pick up per-customer
         trading params (the CLI normally does this from auth.db)
      3. Reset per-cycle accumulators (_PENDING_ACKS, _GATE_TIMINGS_MS)
      4. Invoke t.run(session=cycle) — trader writes to local DB as usual
      5. Drain accumulators into result delta
      6. Return WorkResult
      7. Clear per-thread context in `finally` so worker thread doesn't
         carry stale creds into next request

    For the loopback case (retail-N is the process node, all DBs local),
    the trader writes positions/cash/cooling_off/orders to the customer
    DB during the cycle. The dispatcher's apply_delta does NOT re-apply
    those — it only handles fields that the trader couldn't write
    itself in distributed mode (signal acks via _ack_signal accumulator,
    gate14 dispatcher-side post-trade).

    When retail-2 hardware exists and trader_server runs on a different
    machine without local DB access, _execute_packet uses TRADER_DB_MODE
    =packet to inject a WorkPacketDB mock for _db()/_shared_db().
    """
    import retail_trade_logic_agent as t

    work_id     = packet.get("work_id", "?")
    customer_id = packet.get("customer_id", "")
    cycle       = packet.get("cycle", "open")
    creds       = packet.get("alpaca_creds", {}) or {}
    state       = packet.get("state_snapshot", {}) or {}

    # Within-process serialization: trader reads ALPACA_API_KEY etc. via
    # LOAD_GLOBAL (bypasses module __getattr__), so concurrent stamps
    # would race. The lock makes Customer A's run + cleanup complete
    # before Customer B's stamp lands. Across worker processes
    # (UVICORN_WORKERS=N), each worker has its own lock — true
    # parallelism via the OS process boundary.
    _TRADER_LOCK.acquire()
    try:
        # Set request context (also writes to module globals — the trader's
        # bare-name LOAD_GLOBAL reads see them). _clear_request_context in
        # finally wipes thread-local, but module globals are overwritten by
        # the next request's stamps anyway.
        t._set_request_context(
            ALPACA_API_KEY    = creds.get("key", ""),
            ALPACA_SECRET_KEY = creds.get("secret", ""),
            ALPACA_BASE_URL   = creds.get("base_url", "https://paper-api.alpaca.markets"),
            ALPACA_DATA_URL   = creds.get("data_url", "https://data.alpaca.markets"),
            _CUSTOMER_ID      = customer_id,
            OPERATING_MODE    = state.get("operating_mode", "MANAGED"),
            TRADING_MODE      = state.get("trading_mode", "PAPER"),
        )

        # Apply per-customer trading parameters from customer_settings DB
        # (the CLI normally calls this after credential load). Best-effort —
        # function isn't always present in older deploys.
        try:
            if hasattr(t, "_apply_customer_settings"):
                t._apply_customer_settings()
        except Exception as e:
            log.warning(f"[{customer_id[:8]}] _apply_customer_settings raised: {e}")

        # Reset per-cycle accumulators
        t._PENDING_ACKS.clear()
        t._reset_gate_timings()

        # ── DB-mock injection for TRADER_DB_MODE=packet ────────────────
        # In packet mode, replace the trader's _db() and _shared_db()
        # with WorkPacketDB instances built from the packet. The
        # original handles get restored in `finally` so subsequent
        # requests aren't poisoned.
        cust_mock_db: "WorkPacketDB | None" = None
        shared_mock_db: "WorkPacketDB | None" = None
        original_db_fn = None
        original_shared_db_fn = None
        if TRADER_DB_MODE == "packet":
            from work_packet_db import WorkPacketDB
            cust_mock_db   = WorkPacketDB(packet, is_shared=False)
            shared_mock_db = WorkPacketDB(packet, is_shared=True)
            original_db_fn        = t._db
            original_shared_db_fn = t._shared_db
            t._db        = lambda: cust_mock_db
            t._shared_db = lambda: shared_mock_db
            log.info(f"[{customer_id[:8]}] using WorkPacketDB (TRADER_DB_MODE=packet)")

        # ── Invoke the trader ────────────────────────────────────────────
        # t.run() can sys.exit on certain halt conditions (kill switch,
        # halt file, validator NO_GO with no positions to close). Catch
        # SystemExit so the HTTP server doesn't die.
        run_error: str | None = None
        try:
            t.run(session=cycle)
        except SystemExit as se:
            log.info(f"[{customer_id[:8]}] trader sys.exit({se.code}) — halt path; treating as clean cycle")
        except Exception as e:
            log.error(f"[{customer_id[:8]}] trader crashed: {type(e).__name__}: {e}", exc_info=True)
            run_error = f"{type(e).__name__}: {e}"
        finally:
            # Restore original DB handles regardless of how t.run() exited
            if TRADER_DB_MODE == "packet":
                if original_db_fn is not None:
                    t._db = original_db_fn
                if original_shared_db_fn is not None:
                    t._shared_db = original_shared_db_fn

        # ── Drain accumulators ──────────────────────────────────────────
        acked_signals = list(t._PENDING_ACKS)
        gate_timings  = t.get_gate_timings_ms()
    finally:
        # ALWAYS clean up per-thread context AND release the lock —
        # even if t.run() crashed mid-cycle. A worker thread serves
        # many requests over its lifetime; stale creds from a crashed
        # cycle would silently apply to the next customer.
        try:
            t._clear_request_context()
        finally:
            try:
                _TRADER_LOCK.release()
            except RuntimeError:
                pass  # already released (paranoid; shouldn't happen)

    # ── Build delta ─────────────────────────────────────────────────────
    if TRADER_DB_MODE == "packet" and cust_mock_db is not None:
        # In packet mode the trader couldn't write anywhere (no local DB),
        # so every mutation lives in the mock's delta_* fields. Combine
        # mocks' deltas with the trader's _PENDING_ACKS list (which is
        # populated independently via _ack_signal regardless of DB mode).
        cust_delta   = cust_mock_db.extract_delta()
        shared_delta = shared_mock_db.extract_delta() if shared_mock_db else {}
        # Merge: shared-mock acks + cust-mock acks + module-level _PENDING_ACKS
        # (de-duped). The shared mock catches calls that go through
        # _shared_db().acknowledge_signal() if any path bypasses _ack_signal.
        merged_acks = list(dict.fromkeys(
            (cust_delta.get("acknowledged_signal_ids") or [])
            + (shared_delta.get("acknowledged_signal_ids") or [])
            + acked_signals
        ))
        delta = {
            "positions_added":           cust_delta.get("positions_added", []),
            "positions_closed":          cust_delta.get("positions_closed", []),
            "cash_delta":                cust_delta.get("cash_delta", 0.0),
            "cooling_off_added":         cust_delta.get("cooling_off_added", []),
            "recent_bot_orders_added":   cust_delta.get("recent_bot_orders_added", []),
            "acknowledged_signal_ids":   merged_acks,
            "log_events":                cust_delta.get("log_events", []) + shared_delta.get("log_events", []),
            "trade_outcomes_for_gate14": [],  # built by dispatcher post-trade
            "setting_changes":           cust_delta.get("setting_changes", {}),
            "signal_decisions":          cust_delta.get("signal_decisions", []),
        }
    else:
        # Loopback (TRADER_DB_MODE=local): trader wrote positions / cash /
        # cooling_off / recent_bot_orders directly to the local customer
        # DB during the cycle. We don't include them in delta — dispatcher
        # would double-apply.
        delta = {
            "positions_added":         [],
            "positions_closed":        [],
            "cash_delta":              0.0,
            "cooling_off_added":       [],
            "recent_bot_orders_added": [],
            "acknowledged_signal_ids": acked_signals,
            "log_events":              [],
            "trade_outcomes_for_gate14": [],
        }

    return {
        "work_id":               work_id,
        "customer_id":           customer_id,
        "schema_version":        SCHEMA_VERSION,
        "executed_by":           os.environ.get("NODE_ID", "retail-?"),
        "trader_db_mode":        TRADER_DB_MODE,
        "actions":               [],
        "delta":                 delta,
        "alpaca_reconciliation": None,
        "gate_timings_ms":       gate_timings,
        "error":                 run_error,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("trader_server", long_running=True)
    except Exception:
        pass

    # 2026-05-04 — Multi-worker concurrency strategy.
    #
    # The trader module has process-level globals that get stamped per
    # request (ALPACA_API_KEY, _CUSTOMER_ID, etc.). Within a single
    # process we serialize execution via _TRADER_LOCK to avoid races.
    # That caps concurrency at one customer at a time per process.
    #
    # uvicorn supports running multiple worker processes for the same
    # app. Each worker is an independent Python process with its own
    # module globals — so the lock is per-worker, not global. Setting
    # UVICORN_WORKERS=N gives N customers true concurrent execution
    # at the cost of N× memory.
    #
    # Recommended values:
    #   - retail-N node (Pi5 8GB, 4 cores): UVICORN_WORKERS=3 (leaves
    #     1 core for OS + dispatcher round-trips)
    #   - process node loopback (Pi5 16GB, 4 cores, also runs broker +
    #     all signal agents): UVICORN_WORKERS=1 or 2 — most cycles only
    #     touch one customer (Eliana today), so multi-worker is overkill
    #     and costs memory
    #   - dev / Mac: 1 (default)
    #
    # ALSO — when a worker is multi-process, you can't share Python
    # state across workers (no shared lock, no shared cache). The
    # trader doesn't depend on cross-customer in-process state, so
    # this is fine. The MQTT broker, master DBs, and dispatcher are
    # all out-of-process anyway.
    workers = int(os.environ.get("UVICORN_WORKERS", "1"))

    log.info(
        f"trader server starting — host={SERVER_HOST} port={SERVER_PORT} "
        f"auth={'on' if DISPATCH_AUTH_TOKEN else 'OFF (dev)'} "
        f"schema_version={SCHEMA_VERSION} workers={workers} "
        f"trader_db_mode={TRADER_DB_MODE}"
    )
    uvicorn.run(
        "synthos_trader_server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
        workers=workers,
    )
