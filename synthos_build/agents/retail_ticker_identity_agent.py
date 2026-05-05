"""retail_ticker_identity_agent.py — fill ticker_state identity gaps.

Owner of: ticker_state.sector, ticker_state.company, ticker_state.exchange.
Refresh policy: once_on_first_seen (gap-fill only, never overwrites).

Why this exists
  Identity fields are filled synchronously by retail_database.mark_ticker_active
  on first-INSERT (read from ticker_sectors + tradable_assets). But those
  reference tables aren't always populated at the moment a ticker first
  appears — tradable_assets is refreshed daily by retail_market_daemon, and
  ticker_sectors is filled by retail_sector_backfill_agent (also nightly).

  This agent is the nightly backstop: it sweeps ticker_state for rows where
  any identity field is NULL, re-runs the lookup, and fills whatever the
  reference tables now know.

  Strict gap-fill: SQL uses COALESCE(field, ?) so an existing non-NULL value
  is NEVER overwritten. Multiple writers can't conflict on identity.

Usage
  python3 retail_ticker_identity_agent.py
  python3 retail_ticker_identity_agent.py --dry-run
"""
import os
import sys
import logging
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'src'))

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, 'user', '.env'))

from retail_database import get_shared_db, acquire_agent_lock, release_agent_lock

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s ticker_identity_agent: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


def run(dry_run: bool = False) -> dict:
    """Sweep ticker_state for identity gaps and fill what reference tables know.
    Returns stats dict for logging / audit cross-reference."""
    db = get_shared_db()

    with db.conn() as c:
        gaps = c.execute(
            "SELECT ticker FROM ticker_state "
            "WHERE sector IS NULL OR company IS NULL OR exchange IS NULL "
            "ORDER BY is_active DESC, last_active_at DESC"
        ).fetchall()

    log.info(f"sweep starting: {len(gaps)} tickers have at least one NULL identity field")

    stats = {
        'tickers_scanned':   len(gaps),
        'sector_filled':     0,
        'company_filled':    0,
        'exchange_filled':   0,
        'fully_resolved':    0,
        'still_unresolved':  0,
    }
    if not gaps:
        return stats

    now = db.now()
    for row in gaps:
        ticker = row[0] if not hasattr(row, 'keys') else row['ticker']
        ident = db.resolve_ticker_identity(ticker)
        if not ident:
            stats['still_unresolved'] += 1
            continue

        if dry_run:
            log.info(f"  [dry] {ticker}: would fill {sorted(ident.keys())}")
            continue

        # COALESCE keeps any existing non-NULL value untouched. We only
        # write the lookup result when the column is NULL — gap-fill
        # contract, not refresh.
        with db.conn() as c:
            cur = c.execute(
                "SELECT sector, company, exchange FROM ticker_state WHERE ticker=?",
                (ticker,)
            ).fetchone()
            if not cur:
                continue
            c.execute(
                "UPDATE ticker_state SET "
                "  sector   = COALESCE(sector,   ?), "
                "  company  = COALESCE(company,  ?), "
                "  exchange = COALESCE(exchange, ?), "
                "  updated_at = ? "
                "WHERE ticker = ?",
                (ident.get('sector'),
                 ident.get('company'),
                 ident.get('exchange'),
                 now, ticker)
            )
        # Tally what was actually new (cur was NULL, ident has value)
        if cur['sector']   is None and ident.get('sector'):   stats['sector_filled']   += 1
        if cur['company']  is None and ident.get('company'):  stats['company_filled']  += 1
        if cur['exchange'] is None and ident.get('exchange'): stats['exchange_filled'] += 1

        # Was every previously-NULL field resolved?
        nulls_before = sum(1 for f in ('sector','company','exchange') if cur[f] is None)
        gaps_filled  = sum(
            1 for f in ('sector','company','exchange')
            if cur[f] is None and ident.get(f)
        )
        if nulls_before == gaps_filled:
            stats['fully_resolved'] += 1
        elif nulls_before > gaps_filled:
            stats['still_unresolved'] += 1

    log.info(
        f"sweep done — sector:{stats['sector_filled']} "
        f"company:{stats['company_filled']} exchange:{stats['exchange_filled']} "
        f"fully_resolved:{stats['fully_resolved']} "
        f"still_unresolved:{stats['still_unresolved']}"
    )
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ticker_state identity sweep")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would be filled but don't write")
    args = parser.parse_args()

    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("ticker_identity_agent", long_running=False)
    except Exception:
        pass

    acquire_agent_lock("retail_ticker_identity_agent.py")
    try:
        run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.error(f"fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
