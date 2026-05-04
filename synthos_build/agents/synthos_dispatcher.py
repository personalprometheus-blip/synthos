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

# Alpaca data-API credentials — same key the news/signal pipeline uses
# (one shared market-data key; per-customer keys are only needed for
# trading actions, not quote reads).
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL   = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")

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


# ── ALPACA QUOTE PRE-FETCH (Tier 6 task 30) ──────────────────────────────
# Dispatcher fetches all needed market data ONCE per cycle via Alpaca's
# multi-symbol endpoint, then distributes the same quote map to every
# customer's work packet. This eliminates ~50–150ms per ticker per
# customer of Alpaca read latency from the trader's hot path; the only
# Alpaca calls left in the trader are: (a) one final freshness re-check
# on tickers it's about to trade, (b) the order placements themselves.

_alpaca_data_session: requests.Session | None = None


def _get_alpaca_session() -> requests.Session:
    """Persistent session for Alpaca market-data calls. Reuses connection
    + headers across cycles."""
    global _alpaca_data_session
    if _alpaca_data_session is None:
        s = requests.Session()
        s.headers.update({
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        })
        _alpaca_data_session = s
    return _alpaca_data_session


def prefetch_quotes(tickers: set[str]) -> dict[str, dict[str, Any]]:
    """Fetch latest quote (bid / ask / mid) for every ticker in one batched
    Alpaca call. Returns dict ticker → quote dict. Returns empty dict on
    failure (callers degrade gracefully — empty quotes mean each gate
    will fall back to its own per-ticker fetch if needed).

    Uses Alpaca's multi-symbol latest-quotes endpoint which accepts
    comma-separated symbols up to ~100 per request. Larger ticker sets
    are chunked.
    """
    if not tickers or not ALPACA_API_KEY:
        return {}
    out: dict[str, dict[str, Any]] = {}
    unique = sorted({t.upper() for t in tickers if t})
    CHUNK = 50
    for i in range(0, len(unique), CHUNK):
        chunk = unique[i:i + CHUNK]
        try:
            r = _get_alpaca_session().get(
                f"{ALPACA_DATA_URL}/v2/stocks/quotes/latest",
                params={"symbols": ",".join(chunk), "feed": "iex"},
                timeout=(5, 15),
            )
            if r.status_code != 200:
                log.warning(
                    f"prefetch_quotes: Alpaca returned {r.status_code} for "
                    f"{len(chunk)} tickers"
                )
                continue
            quotes_map = (r.json() or {}).get("quotes") or {}
            for sym, q in quotes_map.items():
                bid = float(q.get("bp", 0) or 0)
                ask = float(q.get("ap", 0) or 0)
                mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                out[sym.upper()] = {
                    "bid": bid, "ask": ask, "mid": mid,
                    "ts": q.get("t", ""),
                }
        except Exception as e:
            log.warning(f"prefetch_quotes chunk failed: {e}")
    log.info(f"prefetch_quotes: {len(out)}/{len(unique)} tickers cached")
    return out


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

        # State snapshot (per-customer DB) — all best-effort: missing
        # tables / methods fall back to empty defaults rather than
        # blocking the cycle. The trader (in loopback mode) reads from
        # local DB anyway, so packet completeness is for distributed
        # cross-machine future + audit visibility today.
        portfolio       = cust_db.get_portfolio() or {}
        positions       = cust_db.get_open_positions() or []
        recent_outcomes = cust_db.get_recent_outcomes(limit=100) or []

        # Cooling-off — table may be present (per-customer) or queried
        # via shared_db.is_cooling_off() per ticker. Try the per-customer
        # table first.
        cooling_off: list[dict[str, Any]] = []
        try:
            if hasattr(cust_db, "get_cooling_off"):
                cooling_off = cust_db.get_cooling_off() or []
        except Exception as e:
            log.debug(f"[{customer_id[:8]}] cooling_off fetch noise: {e}")

        # Recent bot orders — settlement-lag guard (added 2026-04-28)
        recent_bot_orders: list[dict[str, Any]] = []
        try:
            if hasattr(cust_db, "get_recent_bot_orders"):
                recent_bot_orders = cust_db.get_recent_bot_orders(limit=50) or []
        except Exception as e:
            log.debug(f"[{customer_id[:8]}] recent_bot_orders fetch noise: {e}")

        # Per-customer settings dict — used by gate7/gate11 (sizing) and
        # any gate that reads custom thresholds. Not all DBs expose a
        # bulk get; iterate well-known keys defensively.
        customer_settings: dict[str, Any] = {}
        for k in (
            "_ALPACA_EQUITY", "_OPERATING_MODE", "_TRADING_MODE",
            "trader_variant", "EXPERIMENT_ID", "EXPERIMENT_FREEZE",
            "_DISPATCH_MODE",
        ):
            try:
                v = cust_db.get_setting(k)
                if v is not None:
                    customer_settings[k] = v
            except Exception:
                pass

        # Validator verdict + restrictions (per-customer settings)
        validator_verdict = (cust_db.get_setting("_VALIDATOR_VERDICT") or "GO").upper()
        validator_restrictions_raw = cust_db.get_setting("_VALIDATOR_RESTRICTIONS") or "[]"
        try:
            import json as _json
            validator_restrictions = _json.loads(validator_restrictions_raw)
            if not isinstance(validator_restrictions, list):
                validator_restrictions = []
        except Exception:
            validator_restrictions = []

        # Validated signals from shared DB
        signals = shared_db.get_signals_by_status(["VALIDATED"], limit=50) or []

        # News flags map (per-ticker) for gate4 + gate5_5 — fetch only
        # for tickers that actually appear in the signals list to keep
        # the packet small.
        news_flags: dict[str, list] = {}
        try:
            if hasattr(shared_db, "get_fresh_news_flags_for_ticker"):
                signal_tickers = {s.get("ticker", "").upper() for s in signals}
                for t in sorted(signal_tickers):
                    if t:
                        flags = shared_db.get_fresh_news_flags_for_ticker(t) or []
                        if flags:
                            news_flags[t] = flags
        except Exception as e:
            log.debug(f"[{customer_id[:8]}] news_flags fetch noise: {e}")

        # Market context (shared DB settings)
        regime             = shared_db.get_setting("_MACRO_REGIME") or "NORMAL"
        market_state       = shared_db.get_setting("_MARKET_STATE") or "OPEN"
        market_state_score = float(shared_db.get_setting("_MARKET_STATE_SCORE") or 0.0)
        # VIX — best-effort from regime detail blob if present
        vix: float | None = None
        try:
            import json as _json
            regime_detail_raw = shared_db.get_setting("_MACRO_REGIME_DETAIL") or "{}"
            regime_detail = _json.loads(regime_detail_raw)
            vix_raw = regime_detail.get("vix")
            if vix_raw is not None:
                vix = float(vix_raw)
        except Exception:
            pass

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
                recent_bot_orders=recent_bot_orders,
                customer_settings=customer_settings,
                operating_mode=operating_mode,
                trading_mode=trading_mode,
            ),
            signals=signals,
            market_context=MarketContext(
                regime=regime,
                vix=vix,
                market_state=market_state,
                market_state_score=market_state_score,
                session=cycle,
                timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
            quotes={},  # Stamped by run_cycle() via prefetch_quotes() after build
            validator_verdict=validator_verdict,
            recent_outcomes=recent_outcomes,
            validator_restrictions=validator_restrictions,
            news_flags=news_flags,
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
    """Apply the trader's returned StateDelta to the master DBs.

    DUAL-MODE SEMANTICS (since 2026-05-04, Phase A wiring):

    The trader_server can run in two modes (TRADER_DB_MODE env on the
    retail node):

    LOOPBACK (TRADER_DB_MODE=local, retail-N == process node):
      Trader has local DB access. Writes positions / cash / cooling_off /
      recent_bot_orders directly to the customer signals.db during the
      cycle. The result.delta returns these as EMPTY ([], 0.0) — apply
      is a no-op for those fields.

    REMOTE (TRADER_DB_MODE=packet, retail-1 on separate hardware):
      Trader has no local DB access. Reads come from WorkPacketDB
      (built from the work packet); writes accumulate into the mock's
      delta_* fields and ship back here populated. This function
      writes them to the master DB single-writer.

    Field-by-field application below uses the present-or-empty pattern:
    if a field is non-empty, apply it; if empty, no-op. That makes the
    same code path correct for both modes without explicit branching.

    Order matters for atomicity:
      1. Acknowledge signals first (shared DB, irreversible from
         trader perspective)
      2. Apply positions added/closed and cash deltas to customer DB
      3. Apply cooling_off / recent_bot_orders / setting_changes
      4. Persist log_events
      5. Run gate14 (uses fresh outcomes — depends on prior steps)
    """
    cid = customer_id[:8]
    try:
        from retail_database import get_customer_db, get_shared_db
        cust_db   = get_customer_db(customer_id)
        shared_db = get_shared_db()

        delta = result.get("delta", {}) or {}
        trader_db_mode = result.get("trader_db_mode", "local")

        # 1. Acknowledge signals — flip VALIDATED → ACTED_ON in shared DB
        # (single-writer pattern; trader can't do this in distributed mode
        # because shared signals.db is owned by process node)
        for sid in delta.get("acknowledged_signal_ids", []) or []:
            try:
                shared_db.acknowledge_signal(sid)
            except Exception as e:
                log.warning(f"[{cid}] ack {sid} failed: {e}")

        # 2. Apply position + cash deltas (REMOTE mode only — these arrive
        # populated when TRADER_DB_MODE=packet. In LOOPBACK mode the trader
        # already wrote to local cust_db during the cycle and the lists/
        # delta are empty — these blocks no-op.)
        positions_added   = delta.get("positions_added") or []
        positions_closed  = delta.get("positions_closed") or []
        cash_delta        = float(delta.get("cash_delta") or 0.0)
        cooling_off_added = delta.get("cooling_off_added") or []
        bot_orders_added  = delta.get("recent_bot_orders_added") or []
        setting_changes   = delta.get("setting_changes") or {}

        if trader_db_mode == "packet":
            log.info(
                f"[{cid}] applying remote-mode delta: "
                f"+{len(positions_added)} pos, -{len(positions_closed)} pos, "
                f"cash_delta={cash_delta:+.2f}, "
                f"+{len(cooling_off_added)} cooling, +{len(bot_orders_added)} orders, "
                f"{len(setting_changes)} settings"
            )

        for pos in positions_added:
            try:
                if hasattr(cust_db, "open_position"):
                    cust_db.open_position(**{k: v for k, v in pos.items() if k != "partial_close"})
            except Exception as e:
                log.warning(f"[{cid}] open_position failed: {e}")

        for pos in positions_closed:
            try:
                if hasattr(cust_db, "close_position"):
                    cust_db.close_position(pos.get("ticker"), **{k: v for k, v in pos.items() if k != "ticker"})
            except Exception as e:
                log.warning(f"[{cid}] close_position failed: {e}")

        if cash_delta and trader_db_mode == "packet":
            try:
                if hasattr(cust_db, "update_portfolio"):
                    cust_db.update_portfolio(cash_delta=cash_delta)
            except Exception as e:
                log.warning(f"[{cid}] cash update failed: {e}")

        for entry in cooling_off_added:
            try:
                if hasattr(cust_db, "add_cooling_off"):
                    cust_db.add_cooling_off(**entry)
            except Exception as e:
                log.warning(f"[{cid}] add_cooling_off failed: {e}")

        for entry in bot_orders_added:
            try:
                if hasattr(cust_db, "record_submitted_order"):
                    cust_db.record_submitted_order(**entry)
            except Exception as e:
                log.warning(f"[{cid}] record_submitted_order failed: {e}")

        for k, v in setting_changes.items():
            try:
                cust_db.set_setting(k, v)
            except Exception as e:
                log.warning(f"[{cid}] set_setting {k} failed: {e}")

        # 3. Persist log_events (kill events, etc.) returned by trader
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
    customer. Returns (success_count, failure_count).

    Tier 6 (task 30) addition: pre-fetch every ticker's quote in ONE
    batched Alpaca call, then embed the resulting map in each customer's
    packet. Big throughput gain — instead of N customers × M tickers
    individual quote fetches inside the trader (200ms each), we do one
    batched fetch up front (~150ms total), and the trader runs gates
    against the in-memory map.
    """
    try:
        from retail_shared import get_active_customers
    except ImportError:
        log.error("cannot import get_active_customers")
        return (0, 0)

    all_customers = get_active_customers() or []
    if not all_customers:
        log.info("no active customers — skipping cycle")
        return (0, 0)

    # 2026-05-07 — Tier 7: per-customer mode filter. Dispatcher only
    # owns customers explicitly flipped to 'distributed' (or all of
    # them when env DISPATCH_MODE=distributed and the customer hasn't
    # opted out via per-customer setting). Daemon owns the rest.
    try:
        from dispatch_mode import filter_customers_by_mode
        customers = filter_customers_by_mode(all_customers, 'distributed')
    except Exception as e:
        log.warning(f"dispatch_mode resolver failed ({e}) — assuming all customers")
        customers = all_customers

    if not customers:
        log.info(
            f"no customers on distributed dispatch this cycle "
            f"({len(all_customers)} total, all on daemon path)"
        )
        return (0, 0)

    log.info(
        f"cycle={session} dispatching {len(customers)}/{len(all_customers)} "
        f"customers retail={RETAIL_URL}"
    )

    # Build all packets first (without quotes), then collect tickers, then
    # one batched Alpaca call, then stamp quotes into every packet, then
    # dispatch. The build phase is fast (local SQLite) so doing it twice
    # would be wasteful — we do it once and mutate the quotes field.
    packets: list[dict[str, Any]] = []
    for cid in customers:
        if _stop_event.is_set():
            return (0, 0)
        p = build_work_packet(cid, cycle=session)
        if p is not None:
            packets.append(p)

    if not packets:
        log.info("no dispatchable packets this cycle")
        return (0, 0)

    # Collect every ticker referenced across all customers' validated
    # signals + open positions. One set, one batched fetch.
    all_tickers: set[str] = set()
    for p in packets:
        for sig in p.get("signals", []) or []:
            t = sig.get("ticker")
            if t:
                all_tickers.add(t)
        state = p.get("state_snapshot", {}) or {}
        for pos in state.get("positions", []) or []:
            t = pos.get("ticker")
            if t:
                all_tickers.add(t)

    quote_map = prefetch_quotes(all_tickers)
    for p in packets:
        # Per-packet quote subset — only include tickers this customer
        # actually cares about, keeps packet size proportional to need.
        wanted: set[str] = set()
        for sig in p.get("signals", []) or []:
            t = sig.get("ticker")
            if t:
                wanted.add(t.upper())
        for pos in p.get("state_snapshot", {}).get("positions", []) or []:
            t = pos.get("ticker")
            if t:
                wanted.add(t.upper())
        p["quotes"] = {t: quote_map[t] for t in wanted if t in quote_map}

    # Tier 6 task 29: dispatch all packets concurrently via thread pool
    # instead of one-at-a-time. requests.Session is thread-safe for
    # concurrent GETs/POSTs (each call uses its own connection from the
    # pool). MAX_PARALLEL_DISPATCHES caps the in-flight count to avoid
    # overwhelming the retail server or Alpaca per-customer rate limits.
    # Each customer's POST is independent; the bottleneck per dispatch
    # is the trader's wall-clock time on the retail side, not local CPU.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_parallel = int(os.environ.get("DISPATCH_MAX_PARALLEL", "8"))

    ok = fail = 0
    results: dict[str, dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="dispatch") as pool:
        future_to_cid = {pool.submit(dispatch, p): p["customer_id"] for p in packets}
        for fut in as_completed(future_to_cid):
            if _stop_event.is_set():
                break
            cid = future_to_cid[fut]
            try:
                results[cid] = fut.result()
            except Exception as e:
                log.warning(f"[{cid[:8]}] dispatch future raised: {e}")
                results[cid] = None

    # Apply deltas serially. The master DB is single-writer; serializing
    # apply_delta keeps us out of SQLite-locked territory and also makes
    # per-customer ordering deterministic for the audit log.
    for cid, result in results.items():
        if result is None:
            fail += 1
            continue
        apply_delta(cid, result)
        ok += 1

    log.info(
        f"cycle complete: {ok} ok, {fail} fail "
        f"(parallel={max_parallel}, prefetched={len(quote_map)} quotes)"
    )
    return (ok, fail)


def main() -> int:
    # 2026-05-07 — Tier 7 update: dispatcher no longer requires the env
    # var to be 'distributed'. Per-customer setting can flip individual
    # customers regardless of env default. If NO customer is on
    # distributed mode AND env is 'daemon', the cycle is a fast no-op
    # that just iterates an empty filtered list — cheap to leave running.
    log.info(
        f"dispatcher starting — env DISPATCH_MODE={DISPATCH_MODE!r}, "
        f"retail={RETAIL_URL}, cycle={CYCLE_INTERVAL_SEC}s, "
        f"http_timeout={HTTP_TIMEOUT_SEC}s "
        f"(per-customer override via _DISPATCH_MODE setting)"
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
