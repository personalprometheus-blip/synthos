#!/usr/bin/env python3
"""
Sector backfill for signals + positions.

Sources (in priority order):
  1. ticker_sectors (228 manual high-confidence mappings)
  2. sector_screening (recent, may have variant names)

Sector-name normalization: maps sector_screening's variants
  ('Consumer Discretionary' → 'Consumer Cyclical', etc.) to the
  canonical ticker_sectors convention so the bias agent treats them
  consistently.

Run with --dry-run to preview, --apply to commit.
"""
import sqlite3, glob, argparse, sys
from collections import defaultdict

DATA_ROOT = '/home/pi516gb/synthos/synthos_build/data'
SHARED = '/home/pi516gb/synthos/synthos_build/user/signals.db'

# Map variant sector names to canonical (ticker_sectors style)
SECTOR_NORMALIZE = {
    'Consumer Discretionary': 'Consumer Cyclical',
    'Consumer Staples':       'Consumer Defensive',
    'Materials':              'Basic Materials',
    'Communications':         'Communication Services',
    'Information Technology': 'Technology',
    'Financials':             'Financial Services',
}

def build_lookup():
    """Build ticker → sector mapping from all available sources."""
    s = sqlite3.connect(SHARED)
    lookup = {}
    # 1. ticker_sectors — primary, high confidence
    for ticker, sector in s.execute("SELECT ticker, sector FROM ticker_sectors WHERE sector IS NOT NULL"):
        sector = SECTOR_NORMALIZE.get(sector, sector)
        lookup[ticker] = (sector, 'ticker_sectors')
    # 2. sector_screening — fallback, take most recent per ticker
    for ticker, sector in s.execute("""
        SELECT ticker, sector FROM sector_screening
        WHERE sector IS NOT NULL AND ticker IS NOT NULL
        GROUP BY ticker HAVING MAX(id)
    """):
        if ticker not in lookup:
            sector = SECTOR_NORMALIZE.get(sector, sector)
            lookup[ticker] = (sector, 'sector_screening')
    s.close()
    return lookup

def backfill_signals(lookup, dry):
    s = sqlite3.connect(SHARED)
    rows = s.execute("""
        SELECT id, ticker FROM signals
        WHERE (sector IS NULL OR sector='')
        AND created_at > datetime('now','-30 days')
    """).fetchall()
    by_sector = defaultdict(int)
    skipped = 0
    for sig_id, ticker in rows:
        if ticker in lookup:
            sec, src = lookup[ticker]
            by_sector[sec] += 1
            if not dry:
                s.execute("UPDATE signals SET sector=? WHERE id=?", (sec, sig_id))
        else:
            skipped += 1
    if not dry:
        s.commit()
    s.close()
    return len(rows) - skipped, skipped, dict(by_sector)

def backfill_positions(lookup, dry):
    """Backfill positions.sector across all customer DBs."""
    fleet_updated = 0
    fleet_skipped = 0
    by_sector = defaultdict(int)
    by_customer = {}
    for d in sorted(glob.glob(f'{DATA_ROOT}/customers/*')):
        cid = d.split('/')[-1]
        if len(cid) != 36:
            continue
        db = f'{d}/signals.db'
        try:
            c = sqlite3.connect(db)
            rows = c.execute("""
                SELECT id, ticker FROM positions
                WHERE sector IS NULL OR sector=''
            """).fetchall()
            updated = 0
            skipped = 0
            for pos_id, ticker in rows:
                if ticker in lookup:
                    sec, src = lookup[ticker]
                    by_sector[sec] += 1
                    if not dry:
                        c.execute("UPDATE positions SET sector=? WHERE id=?", (sec, pos_id))
                    updated += 1
                else:
                    skipped += 1
            if not dry:
                c.commit()
            c.close()
            if updated or skipped:
                by_customer[cid[:8]] = (updated, skipped)
                fleet_updated += updated
                fleet_skipped += skipped
        except Exception as e:
            print(f'  ERROR for {cid[:8]}: {e}', file=sys.stderr)
    return fleet_updated, fleet_skipped, dict(by_sector), by_customer

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='Actually update DBs')
    args = ap.parse_args()
    dry = not args.apply
    print(f"=== Sector backfill (mode={'DRY-RUN' if dry else 'APPLY'}) ===")

    print()
    print("Building combined sector lookup...")
    lookup = build_lookup()
    print(f"  {len(lookup)} ticker → sector mappings ready")
    by_source = defaultdict(int)
    for tk, (sec, src) in lookup.items():
        by_source[src] += 1
    for src, n in by_source.items():
        print(f"    {src}: {n}")

    print()
    print("=== Backfilling signals (last 30 days) ===")
    upd, skp, dist = backfill_signals(lookup, dry)
    print(f"  signals updated: {upd}    skipped (no lookup): {skp}")
    if dist:
        print(f"  distribution by assigned sector:")
        for sec, n in sorted(dist.items(), key=lambda x: -x[1]):
            print(f"    {sec:25s} {n}")

    print()
    print("=== Backfilling positions (all customers, all-time) ===")
    upd, skp, dist, by_cust = backfill_positions(lookup, dry)
    print(f"  fleet positions updated: {upd}   skipped: {skp}")
    if by_cust:
        print(f"  per-customer (updated, skipped):")
        for cid, (u, s) in sorted(by_cust.items()):
            print(f"    {cid}: updated={u} skipped={s}")
    if dist:
        print(f"  fleet distribution by assigned sector:")
        for sec, n in sorted(dist.items(), key=lambda x: -x[1]):
            print(f"    {sec:25s} {n}")
    print()
    if dry:
        print("DRY-RUN — re-run with --apply to commit.")
    else:
        print("APPLIED. Validator will re-evaluate on next cycle (~30s).")

if __name__ == '__main__':
    main()
