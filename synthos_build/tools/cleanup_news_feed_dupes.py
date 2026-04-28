#!/usr/bin/env python3
"""
cleanup_news_feed_dupes.py
==========================

One-time cleanup of duplicate rows in news_feed accumulated under the
old write-time-no-dedup pattern (pre-Patch-B, 2026-04-27).

What it does
------------
Finds clusters where the same (ticker, raw_headline) was written more
than once within a 24-hour window, keeps the OLDEST row, deletes the
rest. Mirrors the dedup window the new write_news_feed_entry() now
enforces at insert time.

Defaults to dry-run; --confirm applies the deletes. Backs up news_feed
to a timestamped CSV before any deletion.

Usage
-----
  python3 tools/cleanup_news_feed_dupes.py
  python3 tools/cleanup_news_feed_dupes.py --confirm

Pre-requisites
--------------
  Stop the portal + market daemon before --confirm so no concurrent
  writes are interleaved with the cleanup.
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

_DIR   = os.path.dirname(os.path.abspath(__file__))
_BUILD = os.path.dirname(_DIR)
_USER  = os.path.join(_BUILD, 'user')
SHARED_DB   = os.path.join(_USER, 'signals.db')
BACKUP_DIR  = os.path.join(_BUILD, 'data', 'migrations')

DEDUP_WINDOW_HOURS = 24


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--confirm', action='store_true',
                        help='Actually delete duplicates (default: dry-run)')
    parser.add_argument('--window-hours', type=int, default=DEDUP_WINDOW_HOURS,
                        help=f'Cluster window in hours (default {DEDUP_WINDOW_HOURS})')
    args = parser.parse_args()

    print(f"== news_feed dup cleanup ==")
    print(f"  shared DB    : {SHARED_DB}")
    print(f"  window hours : {args.window_hours}")
    print(f"  mode         : {'LIVE RUN' if args.confirm else 'dry-run'}")
    print()

    if not os.path.exists(SHARED_DB):
        print(f"ERROR: shared DB does not exist at {SHARED_DB}", file=sys.stderr)
        sys.exit(2)

    db = sqlite3.connect(SHARED_DB, timeout=30)
    db.row_factory = sqlite3.Row

    total = db.execute("SELECT COUNT(*) FROM news_feed").fetchone()[0]
    print(f"  news_feed total rows: {total}")
    print()

    # Find duplicate clusters: rows that share (ticker, raw_headline) with
    # at least one other row within `window_hours` of them.
    #
    # Strategy:
    #   1. Pull all rows ordered by (ticker, raw_headline, created_at).
    #   2. For each (ticker, raw_headline) cluster, if it has >1 row,
    #      walk forward in time and mark every row as "delete" if the
    #      previous-keeper's created_at is within `window_hours`.
    #   3. The first (oldest) row in a window is always kept.
    #
    # This handles the "6x in 4 hours" pattern AND legitimate
    # republish-after-30-days cases (each triggers a new "kept" row).

    rows = db.execute("""
        SELECT id, ticker, raw_headline, created_at
        FROM news_feed
        WHERE raw_headline IS NOT NULL AND raw_headline != ''
        ORDER BY ticker, raw_headline, created_at
    """).fetchall()

    to_delete = []           # row ids
    cluster_summary = {}     # (ticker, headline) → (kept_count, deleted_count)
    prev_key  = None
    keeper_ts = None
    window_s  = args.window_hours * 3600

    for r in rows:
        key = (r['ticker'], r['raw_headline'])
        try:
            ts = datetime.fromisoformat(r['created_at'].replace(' ', 'T'))
        except Exception:
            continue
        if key != prev_key:
            # New cluster — this row is the keeper
            keeper_ts = ts
            prev_key  = key
            cluster_summary[key] = [1, 0]
            continue
        # Same cluster as previous — check window
        if (ts - keeper_ts).total_seconds() <= window_s:
            to_delete.append(r['id'])
            cluster_summary[key][1] += 1
        else:
            # Outside the window — start a new keeper
            keeper_ts = ts
            cluster_summary[key][0] += 1

    # Show top offenders
    worst = sorted(
        ((k, v) for k, v in cluster_summary.items() if v[1] > 0),
        key=lambda kv: -kv[1][1]
    )[:15]
    print(f"  Top 15 clusters by deletions:")
    print(f"  {'ticker':8} {'kept':>5} {'del':>5}  headline")
    print(f"  {'-'*8} {'-'*5} {'-'*5}  {'-'*60}")
    for (ticker, headline), (kept, deleted) in worst:
        print(f"  {ticker or '-':8} {kept:>5} {deleted:>5}  {(headline or '')[:60]!r}")

    print()
    print(f"  Total rows to delete : {len(to_delete)}")
    print(f"  Rows after cleanup   : {total - len(to_delete)}")
    print(f"  Reduction            : {len(to_delete)/max(total,1)*100:.1f}%")
    print()

    if not args.confirm:
        print("Dry-run complete. Re-run with --confirm to delete.")
        return 0

    if not to_delete:
        print("Nothing to delete.")
        return 0

    # Backup the rows we're about to delete to a CSV
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(BACKUP_DIR, f"news_feed_dupes_deleted_{ts_str}.csv")

    placeholders = ",".join("?" for _ in to_delete)
    deleted_rows = db.execute(
        f"SELECT * FROM news_feed WHERE id IN ({placeholders})",
        to_delete
    ).fetchall()
    cols = list(deleted_rows[0].keys()) if deleted_rows else []
    with open(csv_path, 'w', newline='') as fh:
        if cols:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in deleted_rows:
                w.writerow([r[c] for c in cols])
    print(f"  backup: {csv_path}  ({len(deleted_rows)} rows)")

    # Delete in chunks (SQLite has ~999 var limit per statement)
    deleted_total = 0
    chunk = 500
    with db:
        for i in range(0, len(to_delete), chunk):
            ids = to_delete[i:i+chunk]
            ph = ",".join("?" for _ in ids)
            cur = db.execute(f"DELETE FROM news_feed WHERE id IN ({ph})", ids)
            deleted_total += cur.rowcount

    new_total = db.execute("SELECT COUNT(*) FROM news_feed").fetchone()[0]
    print(f"  deleted  : {deleted_total}")
    print(f"  new total: {new_total}")
    print()
    print("Cleanup complete.")
    db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
