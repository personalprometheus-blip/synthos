#!/usr/bin/env python3
"""
retail_window_calculator.py — Compute macro + minor entry/exit windows.

Phase 3b of TRADER_RESTRUCTURE_PLAN. Replaces the "news-signal is
the trigger" model with precomputed entry zones the trader acts on
when live prices enter the zone.

TWO MODES:

  enrichment (default) — called by retail_market_daemon each
    enrichment tick (~30 min). For every active customer × every
    VALIDATED or CANDIDATE_PENDING signal, compute (macro, minor)
    windows and upsert into trade_windows.

  refresh               — called by retail_trade_daemon each cycle
    (~30s, Phase 3c). Minor windows only. Cheap recompute from live
    prices, macro unchanged.

Phase 3b ships enrichment mode. Refresh mode is stubbed for Phase 3c.

PHASE 3b WINDOW COMPUTATION — INTENTIONALLY SIMPLE:

  Percentage-based bands around the current live price, asymmetric
  (wider down than up) to bias toward pullback entries:

      macro.entry_low   = price × (1 - 0.015)    # 1.5% below
      macro.entry_high  = price × (1 + 0.005)    # 0.5% above
      macro.stop        = price × (1 - 0.03)     # 3% below current
      macro.tp          = price × (1 + 0.05)     # 5% take profit

      minor.entry_low   = price × (1 - 0.005)    # 0.5% below
      minor.entry_high  = price × (1 + 0.002)    # 0.2% above
      minor.stop        = price × (1 - 0.03)     # same as macro
      minor.tp          = None

  Phase 4 (ATR stops + sizing) replaces these with:
      width         = k × ATR_14
      stop_distance = max(1.5 × ATR_14, floor_pct)
      minor anchor  = VWAP ± (0.25..0.5 × ATR)

  That refinement lives in Phase 4 because it also touches sizing
  (risk-per-trade dollars / stop-distance dollars). Doing it in 3b
  would couple two separate refactors.

PHASE 3b TRADER DOES NOT READ THESE WINDOWS. The trader cutover
happens in Phase 3c (Gate 5 rebalance + trader window consumer +
v1 trader logic delete). Phase 3b is pure infrastructure — we
populate the table, prove the flow, observe the numbers in the
trade_windows table for a few days, then cut over.

USAGE:

  python3 retail_window_calculator.py                 # enrichment mode
  python3 retail_window_calculator.py --mode=refresh  # refresh mode (3c)
  python3 retail_window_calculator.py --customer-id=<uuid>  # single customer
"""

import os
import sys
import logging
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_DIR / 'src'))

from retail_database import get_customer_db  # noqa: E402

ET = ZoneInfo("America/New_York")

_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')


def _shared_db():
    """Master/owner customer DB — shared state (signals, live_prices, etc).
    Mirrors the pattern used by retail_trade_logic_agent."""
    return get_customer_db(_OWNER_CID)


def get_active_customers() -> list:
    """Return list of active customer IDs from auth.db.
    Mirrors retail_market_daemon.get_active_customers()."""
    try:
        import auth
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active')]
    except Exception as e:
        log.error(f"Could not list customers: {e}")
        return []

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s window_calc: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('window_calc')

# ── Window computation (Phase 3b: percentage-based; Phase 4: ATR-based) ──

MACRO_LOW_PCT   = 0.015   # 1.5% below current
MACRO_HIGH_PCT  = 0.005   # 0.5% above current
STOP_PCT        = 0.030   # 3% below current
TP_PCT          = 0.050   # 5% above current (macro only)

MINOR_LOW_PCT   = 0.005   # 0.5% below current
MINOR_HIGH_PCT  = 0.002   # 0.2% above current


def _compute_windows(current_price: float) -> tuple[dict, dict]:
    """Return (macro, minor) window dicts computed from current price.
    Pure function — no DB access. Callers supply the price."""
    p = float(current_price)
    macro = {
        'entry_low':  round(p * (1 - MACRO_LOW_PCT), 4),
        'entry_high': round(p * (1 + MACRO_HIGH_PCT), 4),
        'stop':       round(p * (1 - STOP_PCT), 4),
        'tp':         round(p * (1 + TP_PCT), 4),
    }
    minor = {
        'entry_low':  round(p * (1 - MINOR_LOW_PCT), 4),
        'entry_high': round(p * (1 + MINOR_HIGH_PCT), 4),
        'stop':       round(p * (1 - STOP_PCT), 4),
        'tp':         None,
    }
    return macro, minor


# ── Live price lookup ────────────────────────────────────────────────────

def _live_prices_map() -> dict:
    """Return {ticker: price} from the master signals.db live_prices table.
    Empty dict if the table is missing / query fails — callers skip those
    tickers rather than crash."""
    try:
        sdb = _shared_db()
        with sdb.conn() as c:
            rows = c.execute(
                "SELECT ticker, price FROM live_prices WHERE price IS NOT NULL"
            ).fetchall()
        return {r['ticker']: float(r['price']) for r in rows if r['price']}
    except Exception as e:
        log.warning(f"live_prices lookup failed: {e}")
        return {}


# ── Signal selection ─────────────────────────────────────────────────────

def _candidate_signals_for_customer(db) -> list:
    """
    Signals that should have windows computed right now. Phase 3b scope:
    VALIDATED signals (the ones trader currently acts on) + any future
    CANDIDATE_PENDING signals (Candidate Generator output). This keeps the
    Window Calculator relevant once Candidate Generator starts emitting in
    Phase 3b without needing a schema change.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, ticker FROM signals "
            "WHERE status IN ('VALIDATED', 'CANDIDATE_PENDING') "
            "AND expires_at > datetime('now')"
        ).fetchall()
    return [{'id': r['id'], 'ticker': r['ticker']} for r in rows]


# ── Main mode handlers ──────────────────────────────────────────────────

def run_enrichment_pass(customer_id: str | None = None) -> dict:
    """
    Full (macro + minor) recompute for every active customer × every
    in-flight signal. Called from retail_market_daemon's enrichment tick.
    Returns a summary dict for logging.
    """
    prices = _live_prices_map()
    if not prices:
        log.warning("No live_prices available — enrichment pass a no-op")
        return {'customers': 0, 'signals': 0, 'windows_written': 0, 'skipped_no_price': 0}

    customers = [customer_id] if customer_id else get_active_customers()
    total_signals = 0
    windows_written = 0
    skipped_no_price = 0

    for cid in customers:
        try:
            db = get_customer_db(cid)
            signals = _candidate_signals_for_customer(db)
            total_signals += len(signals)
            for sig in signals:
                ticker = sig['ticker']
                price = prices.get(ticker)
                if not price:
                    skipped_no_price += 1
                    continue
                macro, minor = _compute_windows(price)
                # Windows live on the per-customer DB so per-customer
                # price-history variance (when Phase 4 adds ATR) can
                # differ. For now the computation is price-only so rows
                # are identical across customers, but write per-customer
                # anyway to match the forward shape.
                db.write_trade_window(
                    signal_id=sig['id'], customer_id=cid, tier='macro',
                    entry_low=macro['entry_low'], entry_high=macro['entry_high'],
                    stop=macro['stop'], tp=macro['tp'],
                )
                db.write_trade_window(
                    signal_id=sig['id'], customer_id=cid, tier='minor',
                    entry_low=minor['entry_low'], entry_high=minor['entry_high'],
                    stop=minor['stop'], tp=minor['tp'],
                )
                windows_written += 2
        except Exception as e:
            log.warning(f"enrichment pass failed for {cid[:8]}: {e}")
            continue

    return {
        'customers':         len(customers),
        'signals':           total_signals,
        'windows_written':   windows_written,
        'skipped_no_price':  skipped_no_price,
    }


def run_refresh_pass(customer_id: str | None = None) -> dict:
    """
    Minor-tier-only recompute for the trade daemon's cycle cadence.
    Phase 3b ships this as a stub — not called by anything yet. The
    trade daemon cutover in Phase 3c wires it in.
    """
    # TODO Phase 3c: implement lightweight minor refresh that skips
    # macro recompute.
    log.info("refresh mode is a stub — Phase 3c wires this in")
    return {'stubbed': True}


# ── Housekeeping ────────────────────────────────────────────────────────

def expire_stale_pass(customer_id: str | None = None) -> int:
    """Prune stale trade_windows rows across active customers.
    Called as part of enrichment pass."""
    customers = [customer_id] if customer_id else get_active_customers()
    total = 0
    for cid in customers:
        try:
            db = get_customer_db(cid)
            total += db.expire_stale_trade_windows()
        except Exception as e:
            log.debug(f"expire_stale failed for {cid[:8]}: {e}")
    return total


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['enrichment', 'refresh'],
                    default='enrichment')
    ap.add_argument('--customer-id', default=None,
                    help='Limit to a single customer (default: all active)')
    args = ap.parse_args()

    t0 = datetime.now(ET)
    expired = expire_stale_pass(args.customer_id)
    log.info(f"expired {expired} stale window row(s)")

    if args.mode == 'enrichment':
        summary = run_enrichment_pass(args.customer_id)
        log.info(
            f"[ENRICHMENT] customers={summary['customers']} "
            f"signals={summary['signals']} "
            f"windows_written={summary['windows_written']} "
            f"skipped_no_price={summary['skipped_no_price']} "
            f"in {(datetime.now(ET) - t0).total_seconds():.1f}s"
        )
    else:
        summary = run_refresh_pass(args.customer_id)
        log.info(f"[REFRESH] {summary}")


if __name__ == '__main__':
    main()
