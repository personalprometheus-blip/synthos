#!/usr/bin/env python3
"""
cleanup_customer_shared_table_rows.py
======================================

Patch D-rows (2026-04-27).

Drops orphaned rows from per-customer DBs in tables that, post-Patch-A,
are written exclusively to the shared market-intel DB (user/signals.db).

Why this is needed
------------------
Before Patch A, shared agents (news, sentiment, screener, etc.) wrote
their output into the OWNER customer's signals.db.  Patch A re-routed
those agents to user/signals.db and copied the OWNER's rows over via
tools/migrate_owner_to_shared_db.py.

That left:
  - 30eff008 (owner): 58,521 rows in shared tables, now duplicated
    in user/signals.db. Owner archive is redundant.
  - f313a3d9: 530 stale rows from the pre-OWNER era. Orphans.
  - 13 other customers: 0 rows. Schema exists, never used.

Nothing reads these orphan rows anymore — every reader was redirected
to get_shared_db() in Patch A. They're just bytes on disk.

What this does NOT do
---------------------
Does NOT drop the tables themselves. The schema in retail_database.py
still has CREATE TABLE IF NOT EXISTS for these — dropping the tables
would be undone on the next DB() open. Removing the tables structurally
is Patch D-full (schema split — separate task, see TODO.md).

Tables affected (19, mirrors the Patch A migration list)
--------------------------------------------------------
  signals, news_feed, news_flags, signal_decisions,
  signal_attribution_flags, member_weights, sector_screening,
  screening_requests, ticker_sectors, fetch_cursors, tradable_assets,
  earnings_cache, macro_events, live_prices, sparkline_bars,
  admin_alerts, system_halt, behavior_baselines, system_health_daily

Backup
------
Before any DELETE, takes a SQLite-native backup of every customer DB
that has non-zero rows in any shared table to
data/migrations/customer_shared_prune_<cid8>_<ts>.db.

Usage
-----
  python3 tools/cleanup_customer_shared_table_rows.py             # dry-run
  python3 tools/cleanup_customer_shared_table_rows.py --confirm   # apply

Pre-requisites
--------------
  Stop services that hold customer DB locks (portal/watchdog) before
  --confirm. Trader/market-daemon should already be off (off-hours).
"""
import argparse
import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

_DIR   = os.path.dirname(os.path.abspath(__file__))
_BUILD = os.path.dirname(_DIR)
_DATA  = os.path.join(_BUILD, 'data')
CUSTOMERS_DIR = os.path.join(_DATA, 'customers')
BACKUP_DIR    = os.path.join(_DATA, 'migrations')

SHARED_TABLES = [
    # Reference / cache (populated by backfill, sentiment, news)
    'tradable_assets', 'ticker_sectors', 'fetch_cursors',
    'earnings_cache',  'macro_events',
    # Member reliability (news agent)
    'member_weights',
    # Signal pipeline
    'signals', 'news_feed', 'news_flags', 'signal_decisions',
    'signal_attribution_flags',
    # Sector screener
    'sector_screening', 'screening_requests',
    # Caches
    'live_prices', 'sparkline_bars',
    # System-global state
    'admin_alerts', 'system_halt', 'behavior_baselines',
    'system_health_daily',
]


def table_count(db, name):
    try:
        return db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    except sqlite3.OperationalError:
        return None  # table missing


def backup_db(src_path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cid8 = os.path.basename(os.path.dirname(src_path))[:8]
    ts   = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dst  = os.path.join(BACKUP_DIR, f"customer_shared_prune_{cid8}_{ts}.db")
    shutil.copy2(src_path, dst)
    return dst


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--confirm', action='store_true',
                        help='Actually delete (default: dry-run)')
    parser.add_argument('--no-backup', action='store_true',
                        help='Skip per-customer backup')
    args = parser.parse_args()

    print(f"== customer-DB shared-table prune ==")
    print(f"  customers dir: {CUSTOMERS_DIR}")
    print(f"  mode         : {'LIVE RUN' if args.confirm else 'dry-run'}")
    print()

    paths = sorted(glob.glob(os.path.join(CUSTOMERS_DIR, '*', 'signals.db')))
    if not paths:
        print("no customer DBs found")
        return 0

    # Plan first
    plan = []  # list of (path, cid8, {table: count})
    grand_total = 0
    for p in paths:
        cid8 = os.path.basename(os.path.dirname(p))[:8]
        db   = sqlite3.connect(p, timeout=30)
        per  = {}
        total = 0
        for t in SHARED_TABLES:
            n = table_count(db, t)
            if n is None or n == 0:
                continue
            per[t] = n
            total += n
        db.close()
        plan.append((p, cid8, per, total))
        grand_total += total

    # Print plan
    affected = [pl for pl in plan if pl[2]]
    print(f"  {'cid':10}  {'rows':>10}  detail")
    print(f"  {'-'*10}  {'-'*10}")
    for p, cid8, per, total in plan:
        if total == 0:
            print(f"  {cid8:10}  {'(empty)':>10}")
        else:
            detail = ' '.join(f"{t}:{n}" for t, n in sorted(per.items(), key=lambda kv: -kv[1])[:5])
            more   = '' if len(per) <= 5 else f" +{len(per)-5} more"
            print(f"  {cid8:10}  {total:>10}  {detail}{more}")
    print()
    print(f"  customers with rows : {len(affected)}/{len(plan)}")
    print(f"  grand total to delete: {grand_total}")
    print()

    if not args.confirm:
        print("Dry-run complete. Re-run with --confirm to apply.")
        return 0

    if grand_total == 0:
        print("Nothing to delete.")
        return 0

    # Apply
    print("Applying...")
    print()
    failures = []
    for p, cid8, per, total in plan:
        if total == 0:
            continue

        if not args.no_backup:
            try:
                bk = backup_db(p)
                print(f"  ✓ backup {cid8}  → {bk}")
            except Exception as e:
                print(f"  ✗ backup FAILED {cid8}: {e} — skipping this DB")
                failures.append((cid8, f"backup failed: {e}"))
                continue

        try:
            db = sqlite3.connect(p, timeout=30)
            with db:
                for t in per:
                    db.execute(f"DELETE FROM {t}")
            # Verify
            db_v = sqlite3.connect(p, timeout=30)
            remaining = sum((table_count(db_v, t) or 0) for t in per)
            db_v.close()
            db.close()
            print(f"  ✓ delete {cid8}  rows: {total} → {remaining}")
            if remaining != 0:
                failures.append((cid8, f"remaining rows after delete: {remaining}"))
        except Exception as e:
            print(f"  ✗ delete FAILED {cid8}: {e}")
            failures.append((cid8, str(e)))

    print()
    if failures:
        print(f"=== Failures ({len(failures)}) ===")
        for cid8, msg in failures:
            print(f"  {cid8}: {msg}")
        return 1
    print("Cleanup complete.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
