"""
retail_sector_backfill_agent.py — Sector Backfill Agent
Synthos · Enrichment Layer · Version 1.0

Runs nightly (out of market-hours hot path) to populate the
ticker_sectors cache for any ticker referenced by positions or signals
that has no sector classification.

Resolution order:
  1. retail_sector_map.HARDCODED_MAP       (curated, free, instant)
  2. sector_screening table                (existing screener output)
  3. FMP /stable/profile?symbol=X          (external API, rate-limited)

Rate limit: FMP free tier = 250 requests/day. We cap each run at
REQUEST_CAP_PER_RUN and stop early if FMP returns 429. The vast majority
of customer tickers are covered by the hardcoded map, so the expected
steady-state is <20 FMP calls/day.

Usage:
  python3 retail_sector_backfill_agent.py
  python3 retail_sector_backfill_agent.py --dry-run
  python3 retail_sector_backfill_agent.py --ticker AAPL   # single ticker
"""

import os
import sys
import time
import logging
import argparse
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, acquire_agent_lock, release_agent_lock
from retail_sector_map import HARDCODED_MAP, CASH_RESERVE


FMP_API_KEY      = os.environ.get('FMP_API_KEY', '')
FMP_BASE_URL     = 'https://financialmodelingprep.com/stable'
OWNER_CID        = os.environ.get('OWNER_CUSTOMER_ID',
                                  '30eff008-c27a-4c71-a788-05f883e4e3a0')
ET               = ZoneInfo("America/New_York")

REQUEST_CAP_PER_RUN = 150     # Leave headroom under FMP's 250/day free-tier cap
FMP_TIMEOUT_S       = 10
INTER_REQUEST_SLEEP = 0.3     # Gentle pacing to avoid 429s

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('sector_backfill')


def _shared_db():
    return get_customer_db(OWNER_CID)


def _seed_hardcoded_map(db):
    """On first run, seed ticker_sectors from the hardcoded map so downstream
    readers hit the DB cache uniformly. Idempotent: ON CONFLICT DO UPDATE."""
    seeded = 0
    for ticker, sector in HARDCODED_MAP.items():
        try:
            existing = db.get_ticker_sector(ticker)
            # Don't overwrite an FMP-sourced entry with the map unless the map
            # explicitly says Cash/Reserve — the map is authoritative for reserves.
            if existing and existing.get('source') != 'manual_map':
                if sector != CASH_RESERVE:
                    continue
            db.set_ticker_sector(
                ticker=ticker, sector=sector,
                industry=None, source='manual_map', confidence='high',
            )
            seeded += 1
        except Exception as e:
            log.warning(f"Failed to seed {ticker}: {e}")
    if seeded:
        log.info(f"Seeded/refreshed {seeded} hardcoded map entries into ticker_sectors")


def _fmp_lookup(ticker):
    """Call FMP /profile endpoint. Return (sector, industry) or (None, None).
    Raises requests.HTTPError on 429 so the caller can stop early."""
    if not FMP_API_KEY:
        return None, None
    url = f"{FMP_BASE_URL}/profile?symbol={ticker}&apikey={FMP_API_KEY}"
    try:
        r = requests.get(url, timeout=FMP_TIMEOUT_S)
        if r.status_code == 429:
            raise requests.HTTPError("FMP rate limit hit (429) — stopping run")
        if r.status_code == 403:
            log.error("FMP returned 403 — check FMP_API_KEY in .env")
            return None, None
        r.raise_for_status()
        data = r.json()
        if not data or not isinstance(data, list):
            return None, None
        record = data[0]
        return record.get('sector'), record.get('industry')
    except requests.HTTPError:
        raise
    except Exception as e:
        log.warning(f"FMP lookup failed for {ticker}: {e}")
        return None, None


def _resolve_ticker(db, ticker):
    """Resolve a single ticker through the cascade. Returns (sector, industry, source)
    or (None, None, None) if unresolved. Never raises."""
    t = ticker.upper().strip()

    # 1. Hardcoded map
    if t in HARDCODED_MAP:
        return HARDCODED_MAP[t], None, 'manual_map'

    # 2. sector_screening
    try:
        scr = db.get_screening_score(t)
        if scr and scr.get('sector'):
            return scr['sector'], None, 'screener'
    except Exception:
        pass

    # 3. FMP
    try:
        sector, industry = _fmp_lookup(t)
        if sector:
            return sector, industry, 'fmp'
    except requests.HTTPError:
        raise  # bubble up 429
    return None, None, None


def _backfill_positions(customer_db, shared_db):
    """Sweep this customer's open positions and fill any empty sectors from
    the shared ticker_sectors cache (which was just updated)."""
    filled = 0
    for pos in customer_db.get_open_positions():
        if pos.get('sector'):
            continue
        cached = shared_db.get_ticker_sector(pos['ticker'])
        if cached and cached.get('sector'):
            with customer_db.conn() as c:
                c.execute("UPDATE positions SET sector=? WHERE id=?",
                          (cached['sector'], pos['id']))
            filled += 1
    if filled:
        log.info(f"  → filled {filled} position sector(s) from cache")


def run(dry_run=False, single_ticker=None):
    acquire_agent_lock('retail_sector_backfill_agent')

    try:
        db = _shared_db()
        log.info(f"Sector backfill agent starting (dry_run={dry_run})")

        # 1. Seed the hardcoded map into ticker_sectors (idempotent, free)
        if not single_ticker:
            _seed_hardcoded_map(db)

        # 2. Collect tickers needing resolution
        if single_ticker:
            tickers = [single_ticker.upper().strip()]
        else:
            tickers = db.get_tickers_needing_sector(limit=REQUEST_CAP_PER_RUN * 2)
        log.info(f"Tickers needing sector resolution: {len(tickers)}")

        # 3. Resolve each via cascade; stop if FMP hits 429
        stats = {'manual_map': 0, 'screener': 0, 'fmp': 0, 'unresolved': 0, 'fmp_calls': 0}
        try:
            for ticker in tickers:
                # Cascade: hardcoded → screener → FMP. Count FMP calls against cap.
                fmp_used = False
                try:
                    # Cheap paths first
                    if ticker in HARDCODED_MAP:
                        sector, industry, source = HARDCODED_MAP[ticker], None, 'manual_map'
                    else:
                        scr = None
                        try:
                            scr = db.get_screening_score(ticker)
                        except Exception:
                            pass
                        if scr and scr.get('sector'):
                            sector, industry, source = scr['sector'], None, 'screener'
                        else:
                            # FMP fallback — gated by cap
                            if stats['fmp_calls'] >= REQUEST_CAP_PER_RUN:
                                log.info(f"Hit REQUEST_CAP_PER_RUN ({REQUEST_CAP_PER_RUN}) — deferring remaining tickers to next run")
                                break
                            sector, industry = _fmp_lookup(ticker)
                            stats['fmp_calls'] += 1
                            fmp_used = True
                            source = 'fmp' if sector else None
                except requests.HTTPError:
                    log.warning("FMP 429 — stopping run early; remaining tickers carry over")
                    break

                if sector:
                    stats[source] += 1
                    log.info(f"  {ticker:8s} → {sector:30s} ({source})")
                    if not dry_run:
                        try:
                            db.set_ticker_sector(
                                ticker=ticker, sector=sector, industry=industry,
                                source=source, confidence='high',
                            )
                        except Exception as e:
                            log.warning(f"Failed to persist {ticker}: {e}")
                else:
                    stats['unresolved'] += 1
                    log.warning(f"  {ticker:8s} → UNRESOLVED")

                if fmp_used:
                    time.sleep(INTER_REQUEST_SLEEP)
        finally:
            pass

        # 4. Backfill position rows across all customer DBs from the updated cache
        if not dry_run and (stats['manual_map'] + stats['screener'] + stats['fmp']) > 0:
            try:
                # Iterate customer DBs via auth.db
                import auth as _auth
                for cust in _auth.list_customers():
                    cid = cust.get('id') or cust.get('customer_id')
                    if not cid or not cust.get('is_active'):
                        continue
                    try:
                        cdb = get_customer_db(cid)
                        _backfill_positions(cdb, db)
                    except Exception as e:
                        log.warning(f"Customer backfill failed for {cid}: {e}")
            except Exception as e:
                log.warning(f"Cross-customer backfill skipped: {e}")

        # 5. Summary
        log.info(
            f"Run complete — map:{stats['manual_map']} screener:{stats['screener']} "
            f"fmp:{stats['fmp']} unresolved:{stats['unresolved']} "
            f"fmp_calls:{stats['fmp_calls']}/{REQUEST_CAP_PER_RUN}"
        )
        return 0
    finally:
        release_agent_lock('retail_sector_backfill_agent')


def main():
    parser = argparse.ArgumentParser(description='Synthos Sector Backfill Agent')
    parser.add_argument('--dry-run', action='store_true',
                        help='Resolve and log but do not write to ticker_sectors')
    parser.add_argument('--ticker', type=str, default=None,
                        help='Backfill a single ticker (skips the queue)')
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, single_ticker=args.ticker))


if __name__ == '__main__':
    main()
