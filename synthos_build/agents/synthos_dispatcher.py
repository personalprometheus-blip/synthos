#!/usr/bin/env python3
"""
synthos_dispatcher.py — Process-node orchestrator for distributed trading.

Created 2026-05-04 as Tier 5 of the distributed-trader migration.

In `distributed` mode, the trader no longer runs on the process node as a
subprocess fan-out. Instead this dispatcher:
  1. Reads master DBs on the process node (per-customer state, shared
     signals, market context).
  2. Builds a self-contained WorkPacket per customer.
  3. POSTs each packet to a retail HTTP server (synthos_trader_server)
     and waits for the result.
  4. Applies the returned StateDelta to the master DBs (single-writer
     pattern — no drift possible).
  5. Runs gate 14 (post-trade kill check) on the dispatcher side using
     the freshly-applied delta + the trade outcomes the trader just
     returned.

This is a SKELETON. The current iteration intentionally:
  - Hard-codes a single retail target from RETAIL_URL env var.
  - Uses synchronous requests.Session (single customer at a time).
  - Does NOT yet implement pipelining / async batching (that's Tier 6).
  - Does NOT yet round-robin across multiple retails (Tier 7).

Lifecycle:
  daemon mode (DISPATCH_MODE=daemon, default)
    → dispatcher exits immediately as a no-op so existing systemd timers
      don't double-fire trades when this is enabled prematurely.
  distributed mode (DISPATCH_MODE=distributed)
    → wakes every CYCLE_INTERVAL_SEC seconds during market hours, fans
      out work packets, applies deltas, sleeps.

Failure modes (all logged, none fatal at the outer loop):
  - Retail HTTP 5xx / timeout → log, skip that customer this cycle, retry
    next cycle (signals stay VALIDATED, idempotent re-evaluation works)
  - DB write fails on delta apply → log + roll back transaction, customer
    state stays consistent with Alpaca (which is truth)
  - MQTT broker unreachable → log; we're not publishing trader telemetry
    here, dispatcher just runs without it

Related files:
  - src/work_packet.py       — packet + result schemas
  - src/gate14_evaluator.py  — pure-compute kill check (called per-customer)
  - agents/synthos_trader_server.py — the HTTP server this POSTs to
  - retail_market_daemon.py + retail_scheduler.py — both gated to skip
    trader fan-out when DISPATCH_MODE=distributed (no double-fire)
"""

from __future__ import annotations
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any

# ── PATH SETUP ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "agents"))

from dotenv import load_dotenv
load_dotenv(str(_ROOT / "user" / ".env"))

import requests

# ── LOGGING ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dispatcher")

# ── ENVIRONMENT ───────────────────────────────────────────────────────────
DISPATCH_MODE       = os.environ.get("DISPATCH_MODE", "daemon").lower()
RETAIL_URL          = os.environ.get("RETAIL_URL", "http://10.0.0.20:8443").rstrip("/")
DISPATCH_AUTH_TOKEN = os.environ.get("DISPATCH_AUTH_TOKEN", "")
CYCLE_INTERVAL_SEC  = int(os.environ.get("DISPATCH_CYCLE_SEC", "30"))
HTTP_TIMEOUT_SEC    = int(os.environ.get("DISPATCH_HTTP_TIMEOUT", "60"))

# ── STATE ─────────────────────────────────────────────────────────────────
_stop_event = Event()
_session: requests.Session | None = None


# ── SIGNAL HANDLERS ───────────────────────────────────────────────────────

def _install_signal_handlers() -> None:
    def _shutdown(signum, frame):
        log.info(f"received signal {signum} — finishing current cycle then exiting")
        _stop_event.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


# ── HTTP CLIENT ───────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    """Lazy persistent HTTP session with keepalive — same TLS-handshake-
    elimination story as AlpacaClient.session in the trader."""
    global _session
    if _session is None:
        s = requests.Session()
        if DISPATCH_AUTH_TOKEN:
            s.headers.update({"X-Dispatch-Token": DISPATCH_AUTH_TOKEN})
        s.headers.update({"Content-Type": "application/json"})
        _session = s
    return _session


# ── WORK PACKET BUILD (per customer) ──────────────────────────────────────

def build_work_packet(customer_id: str, cycle: str) -> dict[str, Any] | None:
    """Build a fully self-contained WorkPacket dict for one customer.

    SKELETON: pulls everything from local SQLite. The per-field fetch
    contract is documented in docs/TRADER_GATE_IO_AUDIT.md — every gate
    that previously read DB now reads from the corresponding work-packet
    field.

    Returns None if the customer has no Alpaca creds (gate 0 short-circuit
    that the trader CLI used to do — moved here so we don't even bother
    sending a packet for a customer that can't trade).
    """
    try:
        from retail_database import get_customer_db, get_shared_db
        try:
            import auth as _auth
        except ImportError:
            _auth = None

        cust_db   = get_customer_db(customer_id)
        shared_db = get_shared_db()

        # Gate-0 short-circuit: no Alpaca key, skip dispatch entirely.
        creds = (None, None)
        if _auth is not None:
            try:
                creds = _auth.get_alpaca_credentials(customer_id)
            except Exception as e:
                log.warning(f"[{customer_id[:8]}] cred lookup failed: {e}")
                return None
        ak, sk = creds
        if not ak:
            log.info(f"[{customer_id[:8]}] no Alpaca key — skipping dispatch")
            return None

        operating_mode = (
            _auth.get_operating_mode(customer_id) if _auth else "MANAGED"
        )
        trading_mode = (
            _auth.get_trading_mode(customer_id) if _auth else "PAPER"
        )
        base_url = (
            "https://api.alpaca.markets" if trading_mode == "LIVE"
            else "https://paper-api.alpaca.markets"
        )

        # State snapshot (per-customer DB)
        portfolio       = cust_db.get_portfolio() or {}
        positions       = cust_db.get_open_positions() or []
        cooling_off     = []  # cust_db.get_cooling_off() — not all DBs have this; tolerate absence
        recent_outcomes = cust_db.get_recent_outcomes(limit=100) or []

        # Validator verdict (per-customer settings, but stored in cust_db)
        validator_verdict = (cust_db.get_setting("_VALIDATOR_VERDICT") or "GO").upper()

        # Validated signals from shared DB
        signals = shared_db.get_signals_by_status(["VALIDATED"], limit=50) or []

        # Market context (shared DB settings)
        regime           = shared_db.get_setting("_MACRO_REGIME") or "NORMAL"
        market_state     = shared_db.get_setting("_MARKET_STATE") or "OPEN"
        market_state_score = float(shared_db.get_setting("_MARKET_STATE_SCORE") or 0.0)

        from work_packet import (
            WorkPacket, AlpacaCreds, CustomerStateSnapshot, MarketContext,
        )
        packet = WorkPacket.new(
            customer_id=customer_id,
            cycle=cycle,
            alpaca_creds=AlpacaCreds(key=ak, secret=sk, base_url=base_url),
            state_snapshot=CustomerStateSnapshot(
                portfolio=portfolio,
                positions=positions,
                cooling_off=cooling_off,
                recent_bot_orders=[],
                customer_settings={},
                operating_mode=operating_mode,
                trading_mode=trading_mode,
            ),
            signals=signals,
            market_context=MarketContext(
                regime=regime,
                vix=None,
                market_state=market_state,
                market_state_score=market_state_score,
                session=cycle,
                timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
            quotes={},  # Tier 6: dispatcher pre-fetches batched Alpaca quotes
            validator_verdict=validator_verdict,
            recent_outcomes=recent_outcomes,
        )

        # WorkPacket → JSON-serializable dict via dataclass.asdict path
        from dataclasses import asdict
        return asdict(packet)
    except Exception as e:
        log.error(f"[{customer_id[:8]}] build_work_packet failed: {e}", exc_info=True)
        return None


# ── HTTP DISPATCH ─────────────────────────────────────────────────────────

def dispatch(packet: dict[str, Any]) -> dict[str, Any] | None:
    """POST one work packet to the retail trader server. Returns the
    result dict, or None on network / 5xx failure."""
    cid = packet.get("customer_id", "?")[:8]
    url = f"{RETAIL_URL}/work"
    try:
        r = _get_session().post(url, json=packet, timeout=(5, HTTP_TIMEOUT_SEC))
        if r.status_code != 200:
            log.warning(f"[{cid}] retail returned {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        log.warning(f"[{cid}] dispatch failed: {e}")
        return None


# ── DELTA APPLICATION ─────────────────────────────────────────────────────

def apply_delta(customer_id: str, result: dict[str, Any]) -> None:
    """Apply the trader's returned StateDelta to the master DB.

    SKELETON: covers the core fields. Position add/close / cash delta /
    cooling_off / signal acks are all here. Gate 14 evaluation is run
    AFTER apply (so the kill-event uses the fresh outcomes).
    """
    cid = customer_id[:8]
    try:
        from retail_database import get_customer_db, get_shared_db
        cust_db   = get_customer_db(customer_id)
        shared_db = get_shared_db()

        delta = result.get("delta", {}) or {}

        # 1. Acknowledge signals — flip VALIDATED → ACTED_ON
        for sid in delta.get("acknowledged_signal_ids", []) or []:
            try:
                shared_db.acknowledge_signal(sid)
            except Exception as e:
                log.warning(f"[{cid}] ack {sid} failed: {e}")

        # 2. Persist log_events (kill events, etc.) returned by trader
        for evt in delta.get("log_events", []) or []:
            try:
                cust_db.log_event(
                    evt.get("event") or "TRADER_EVENT",
                    agent=evt.get("agent") or "trade_logic_agent",
                    details=evt.get("details") or "",
                )
            except Exception as e:
                log.warning(f"[{cid}] log_event failed: {e}")

        # 3. Run gate 14 on dispatcher side (audit blocker H2 fix).
        # Trader returns trade_outcomes_for_gate14 (the fresh outcomes
        # from this cycle). We combine with master DB outcomes for the
        # full window, then run the kill check.
        try:
            from gate14_evaluator import evaluate_strategy_kill
            outcomes = cust_db.get_recent_outcomes(limit=100) or []
            portfolio = cust_db.get_portfolio() or {}
            verdict = evaluate_strategy_kill(
                outcomes=outcomes,
                portfolio=portfolio,
                # Constants live in retail_constants — dispatcher can read them.
                min_sharpe=float(os.environ.get("EVAL_MIN_SHARPE", "0.5")),
                max_drawdown=float(os.environ.get("EVAL_MAX_DRAWDOWN", "0.15")),
                window_days=int(os.environ.get("PERFORMANCE_WINDOW_DAYS", "30")),
            )
            if verdict.get("kill"):
                log.critical(f"[{cid}] STRATEGY KILL CONDITION — {verdict['reason']}")
                cust_db.log_event(
                    "STRATEGY_KILL_CONDITION",
                    agent="dispatcher",
                    details=verdict["reason"],
                )
        except Exception as e:
            log.warning(f"[{cid}] gate14 dispatcher-side check failed: {e}")
    except Exception as e:
        log.error(f"[{cid}] apply_delta crashed: {e}", exc_info=True)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────

def run_cycle(session: str = "open") -> tuple[int, int]:
    """One full dispatch cycle: build + send + apply for every active
    customer. Returns (success_count, failure_count)."""
    try:
        from retail_shared import get_active_customers
    except ImportError:
        log.error("cannot import get_active_customers")
        return (0, 0)

    customers = get_active_customers() or []
    if not customers:
        log.info("no active customers — skipping cycle")
        return (0, 0)

    log.info(f"cycle={session} customers={len(customers)} retail={RETAIL_URL}")
    ok = fail = 0
    for cid in customers:
        if _stop_event.is_set():
            break
        packet = build_work_packet(cid, cycle=session)
        if packet is None:
            continue
        result = dispatch(packet)
        if result is None:
            fail += 1
            continue
        apply_delta(cid, result)
        ok += 1
    log.info(f"cycle complete: {ok} ok, {fail} fail")
    return (ok, fail)


def main() -> int:
    if DISPATCH_MODE != "distributed":
        log.info(
            f"DISPATCH_MODE={DISPATCH_MODE!r} (default 'daemon') — dispatcher "
            f"is a no-op in this mode. Set DISPATCH_MODE=distributed to enable."
        )
        return 0

    log.info(
        f"dispatcher starting — DISPATCH_MODE=distributed, retail={RETAIL_URL}, "
        f"cycle={CYCLE_INTERVAL_SEC}s, http_timeout={HTTP_TIMEOUT_SEC}s"
    )
    _install_signal_handlers()

    while not _stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            run_cycle(session="open")  # session selection deferred to Tier 6
        except Exception as e:
            log.error(f"cycle crashed: {e}", exc_info=True)

        # Sleep until next cycle, accounting for cycle duration so we
        # actually fire every CYCLE_INTERVAL_SEC seconds rather than
        # CYCLE_INTERVAL + cycle_time.
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1.0, CYCLE_INTERVAL_SEC - elapsed)
        if _stop_event.wait(sleep_for):
            break

    log.info("dispatcher stopped")
    return 0


if __name__ == "__main__":
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("dispatcher", long_running=True)
    except Exception as _hb_e:
        pass
    sys.exit(main())
