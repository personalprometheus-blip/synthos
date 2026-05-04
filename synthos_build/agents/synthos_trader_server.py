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
SCHEMA_VERSION      = "1.0.0"  # mirrors work_packet.WORK_PACKET_SCHEMA_VERSION


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


def _execute_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """SKELETON execution path — wires the work packet through the trader's
    in-process functions and harvests the resulting delta.

    The current implementation:
      1. Re-applies packet creds to the trader module globals (so the
         AlpacaClient picks them up on next instantiation).
      2. Calls the trader's run() function for the cycle.
      3. Drains _PENDING_ACKS and _GATE_TIMINGS_MS into the delta.
      4. Returns the WorkResult shape.

    The full I/O extraction (gates reading from packet fields instead of
    DB) is task 26 (mock-retail end-to-end test) — that's where the wiring
    really gets exercised. This skeleton gives the dispatcher something to
    POST against right now.
    """
    import retail_trade_logic_agent as t

    customer_id = packet.get("customer_id", "")
    creds = packet.get("alpaca_creds", {}) or {}
    state = packet.get("state_snapshot", {}) or {}

    # Stamp the trader module globals — the AlpacaClient will read these
    # on construction. In the skeleton, this is the simplest credential
    # injection. A future revision should accept creds as an arg.
    t.ALPACA_API_KEY    = creds.get("key", "")
    t.ALPACA_SECRET_KEY = creds.get("secret", "")
    t.ALPACA_BASE_URL   = creds.get("base_url", "https://paper-api.alpaca.markets")
    t.ALPACA_DATA_URL   = creds.get("data_url", "https://data.alpaca.markets")
    t._CUSTOMER_ID      = customer_id
    t.OPERATING_MODE    = state.get("operating_mode", "MANAGED")
    t.TRADING_MODE      = state.get("trading_mode", "PAPER")

    # Reset per-cycle accumulators
    t._PENDING_ACKS.clear()
    t._reset_gate_timings()

    # SKELETON: actually invoking t.run() right now would call back into
    # the local DBs (which is what we want for daemon mode but NOT for
    # distributed mode where state lives in the packet). Until task 26
    # rewires the gates against packet fields, return an empty delta and
    # report what we received so the dispatcher → server contract is
    # provably wired. Real execution arrives with the mock-retail test.
    delta = {
        "positions_added":         [],
        "positions_closed":        [],
        "cash_delta":              0.0,
        "cooling_off_added":       [],
        "recent_bot_orders_added": [],
        "acknowledged_signal_ids": list(t._PENDING_ACKS),
        "log_events":              [],
        "trade_outcomes_for_gate14": [],
    }
    return {
        "work_id": packet.get("work_id", "?"),
        "customer_id": customer_id,
        "schema_version": SCHEMA_VERSION,
        "executed_by": os.environ.get("NODE_ID", "retail-?"),
        "actions": [],
        "delta": delta,
        "alpaca_reconciliation": None,
        "gate_timings_ms": t.get_gate_timings_ms(),
        "skeleton_note": (
            "task 22+23 skeleton: contract wired, full gate execution "
            "against packet fields is task 26"
        ),
    }


# ── MAIN ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("trader_server", long_running=True)
    except Exception:
        pass

    log.info(
        f"trader server starting — host={SERVER_HOST} port={SERVER_PORT} "
        f"auth={'on' if DISPATCH_AUTH_TOKEN else 'OFF (dev)'} "
        f"schema_version={SCHEMA_VERSION}"
    )
    uvicorn.run(
        "synthos_trader_server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
        # Single worker for skeleton — multiple workers would each have
        # their own AlpacaClient state, which is fine (per-customer keys
        # are independent), but adds complexity for the first wire-up.
        workers=1,
    )
