"""
ticker_state_backfill.py — Phase 1 backfill (one-shot, idempotent).

Walks signals + sector_screening to construct initial ticker_state rows
for every ticker that's been active in the last N days. Uses the latest
non-null value per ticker for each enrichment field, preserving the
original *_evaluated_at timestamp so rebuilt history reflects when the
data was actually computed.

Re-runnable: upserts via the DB helper, so running twice doesn't
double-stamp. New signals after the run get picked up by Phase 2
writers (when they ship).

Spec: synthos_build/docs/TICKER_STATE_ARCHITECTURE.md

Usage:
    python3 ticker_state_backfill.py             # dry-run
    python3 ticker_state_backfill.py --apply     # commit
    python3 ticker_state_backfill.py --apply --days 30   # narrower window
"""

import os
import sys
import argparse
import logging
import sqlite3
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from retail_database import get_shared_db


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('ticker_state_backfill')


# Map signals-table column → ticker_state column (where they differ).
# When the names match, the column appears once.
SIGNALS_TO_STATE = {
    'sector':                   'sector',
    'company':                  'company',
    'sentiment_score':          'sentiment_score',
    'sentiment_evaluated_at':   'sentiment_evaluated_at',
    'screener_score':           'screener_score',
    'screener_evaluated_at':    'screener_evaluated_at',
}


def _latest_per_ticker_from_signals(conn, days):
    """Return {ticker: {field: value, ...}} where each field's value comes
    from the most recent signal for that ticker that had it set."""
    cutoff = f"-{int(days)} days"
    out = defaultdict(dict)

    # Pull all signals in window with their enrichment fields
    rows = conn.execute(f"""
        SELECT ticker, sector, company,
               sentiment_score, sentiment_evaluated_at,
               screener_score,  screener_evaluated_at,
               created_at
        FROM signals
        WHERE created_at > datetime('now', ?)
        ORDER BY ticker ASC, id DESC
    """, (cutoff,)).fetchall()

    # For each ticker, walk newest→oldest and take first non-null per field
    for r in rows:
        ticker = r['ticker']
        if not ticker:
            continue
        agg = out[ticker]
        for sig_col, st_col in SIGNALS_TO_STATE.items():
            if st_col in agg:
                continue  # already taken (we walk newest first)
            v = r[sig_col]
            if v is None or v == '':
                continue
            agg[st_col] = v
    return dict(out)


def _latest_per_ticker_from_screening(conn):
    """Return {ticker: {screener_score, momentum_score, sector_score, ...}}
    from sector_screening's latest run for each ticker."""
    out = defaultdict(dict)
    rows = conn.execute("""
        SELECT ticker, combined_score, sentiment_score, momentum_score,
               sector, MAX(id) AS id
        FROM sector_screening
        WHERE ticker IS NOT NULL
        GROUP BY ticker
    """).fetchall()
    for r in rows:
        ticker = r['ticker']
        if not ticker:
            continue
        if r['combined_score'] is not None:
            out[ticker]['screener_score'] = float(r['combined_score'])
        if r['momentum_score'] is not None:
            out[ticker]['momentum_score'] = float(r['momentum_score'])
        # sector_screening sometimes has sector populated too — useful fallback
        if r['sector'] and 'sector' not in out[ticker]:
            out[ticker]['sector'] = r['sector']
    return dict(out)


def main():
    ap = argparse.ArgumentParser(description='Backfill ticker_state from signals + sector_screening.')
    ap.add_argument('--apply', action='store_true', help='Commit (default: dry-run)')
    ap.add_argument('--days', type=int, default=60,
                    help='How far back to walk signals (default 60)')
    args = ap.parse_args()
    dry = not args.apply

    log.info(f"Mode: {'DRY-RUN' if dry else 'APPLY'} | window: last {args.days} days")

    db = get_shared_db()
    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row

    log.info("Reading signals table...")
    sig_data = _latest_per_ticker_from_signals(conn, args.days)
    log.info(f"  collected enrichment for {len(sig_data)} tickers")

    log.info("Reading sector_screening...")
    scr_data = _latest_per_ticker_from_screening(conn)
    log.info(f"  collected screener data for {len(scr_data)} tickers")

    # Merge: signals data wins for fields that overlap (more recent generally),
    # screener data fills in screener_score/momentum_score for tickers that
    # had no signals or whose signals lacked screener data.
    all_tickers = set(sig_data.keys()) | set(scr_data.keys())
    log.info(f"Total tickers to backfill: {len(all_tickers)}")

    merged = {}
    for tk in all_tickers:
        m = {}
        for src in (scr_data.get(tk, {}), sig_data.get(tk, {})):
            for k, v in src.items():
                if k not in m or m[k] in (None, ''):
                    m[k] = v
        merged[tk] = m

    # Stats
    field_counts = defaultdict(int)
    for tk, fields in merged.items():
        for k in fields:
            field_counts[k] += 1
    log.info("Field-fill rate across backfill set:")
    for k in sorted(field_counts.keys()):
        log.info(f"  {k:30s} {field_counts[k]:5d} / {len(merged)} ({field_counts[k]*100//max(len(merged),1)}%)")

    if dry:
        log.info("--- DRY-RUN sample (first 8 tickers) ---")
        for tk in sorted(merged.keys())[:8]:
            log.info(f"  {tk}: {merged[tk]}")
        log.info("Re-run with --apply to commit.")
        return 0

    # APPLY: walk merged dict, call upsert_ticker_state for each
    upserted = 0
    failed = 0
    for tk, fields in merged.items():
        ok = db.upsert_ticker_state(tk, **fields)
        if ok:
            upserted += 1
        else:
            failed += 1
    log.info(f"APPLY complete: {upserted} upserted, {failed} failed")

    # Final state count
    cur = conn.execute("SELECT COUNT(*) FROM ticker_state")
    n_rows = cur.fetchone()[0]
    log.info(f"ticker_state row count after backfill: {n_rows}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
