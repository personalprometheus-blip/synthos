#!/usr/bin/env python3
"""
migrate_owner_to_shared_db.py
==============================

One-time migration that moves shared market-intelligence data from the
OWNER customer's signals.db to the system-wide shared signals.db.

Background
----------
Until 2026-04-27 the news / sentiment / macro / screener / market-state /
validator / bias / fault / candidate / sector-backfill / price-poller
agents all wrote their output into the OWNER customer's per-customer
signals.db (resolved via OWNER_CUSTOMER_ID).  Every other customer then
reached *across* into the owner's customer DB to read shared news.

That coupling is now removed: get_shared_db() in retail_database.py
points at user/signals.db, and every shared agent has been re-routed to
call it.  This script copies the historical rows from
data/customers/<owner_id>/signals.db over to user/signals.db so the
master starts populated rather than empty.

Tables copied (shared market-intelligence only)
-----------------------------------------------
  signals, news_feed, news_flags, signal_decisions,
  signal_attribution_flags, member_weights, sector_screening,
  screening_requests, ticker_sectors, fetch_cursors, tradable_assets,
  earnings_cache, macro_events, live_prices, admin_alerts, system_halt,
  behavior_baselines

Tables explicitly skipped (per-customer, stays where it is)
-----------------------------------------------------------
  portfolio, positions, position_preferences, ledger, outcomes,
  handshakes, urgent_flags, pending_approvals, notifications,
  support_tickets, support_messages, customer_settings,
  session_history, exit_performance, optimizer_log, cooling_off,
  trade_windows, scan_log, system_log, api_calls

Telemetry tables (scan_log / system_log / api_calls) are skipped
intentionally: they're append-only logs, copying historical rows
clutters the timeline, and new entries from shared agents will start
landing in the master DB on next run anyway.

Usage
-----
  # Dry-run (default) — prints what would happen, no writes
  python3 tools/migrate_owner_to_shared_db.py

  # Real run — requires --confirm and stops services first
  python3 tools/migrate_owner_to_shared_db.py --confirm

Pre-requisites
--------------
  Stop services before running with --confirm:
      sudo systemctl stop synthos-market-daemon synthos-news.timer \\
          synthos-portal synthos-trade-daemon
  Re-start after migration:
      sudo systemctl start synthos-market-daemon synthos-news.timer \\
          synthos-portal synthos-trade-daemon
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────────────────
_DIR    = os.path.dirname(os.path.abspath(__file__))
_BUILD  = os.path.dirname(_DIR)
_SRC    = os.path.join(_BUILD, 'src')
_DATA   = os.path.join(_BUILD, 'data')
_USER   = os.path.join(_BUILD, 'user')
sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv(os.path.join(_USER, '.env'))

OWNER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')
if not OWNER_ID:
    print("ERROR: OWNER_CUSTOMER_ID not set in user/.env", file=sys.stderr)
    sys.exit(2)

OWNER_DB    = os.path.join(_DATA, 'customers', OWNER_ID, 'signals.db')
SHARED_DB   = os.path.join(_USER, 'signals.db')
BACKUP_DIR  = os.path.join(_DATA, 'migrations')

# ── Tables to migrate (shared market-intelligence) ────────────────────────
# Order chosen so any FK-like references resolve naturally; SQLite has no
# real FK enforcement here, but keeping parents before children is good
# hygiene if we add constraints later.
SHARED_TABLES = [
    # Reference / cache tables (populated by backfill, sentiment, news)
    'tradable_assets',
    'ticker_sectors',
    'fetch_cursors',
    'earnings_cache',
    'macro_events',
    # Member reliability (news agent)
    'member_weights',
    # Signal pipeline
    'signals',
    'news_feed',
    'news_flags',
    'signal_decisions',
    'signal_attribution_flags',
    # Sector screener
    'sector_screening',
    'screening_requests',
    # Live prices (price poller)
    'live_prices',
    # Sparkline cache (portal — 15-min bars for ticker mini-charts)
    'sparkline_bars',
    # System-global state
    'admin_alerts',
    'system_halt',
    'behavior_baselines',
    'system_health_daily',
]


# ── Helpers ──────────────────────────────────────────────────────────────

def table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None

def row_count(conn, name):
    if not table_exists(conn, name):
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]

def column_intersection(owner, shared, table):
    """Return common-column list — owner DB schema may have extra cols a
    fresh master doesn't, or vice versa; only copy intersection."""
    owner_cols  = [r[1] for r in owner.execute(f"PRAGMA table_info({table})")]
    shared_cols = [r[1] for r in shared.execute(f"PRAGMA table_info({table})")]
    common = [c for c in owner_cols if c in shared_cols]
    return common, owner_cols, shared_cols

def ensure_master_schema():
    """Instantiate DB() once so all CREATE TABLE IF NOT EXISTS run on the
    shared DB before we start copying. Otherwise tables added by ad-hoc
    migrations elsewhere (live_prices, behavior_baselines) might be
    missing."""
    from retail_database import get_shared_db
    db = get_shared_db()
    # Schema is created in DB.__init__ + DB._migrate_schema; touching one
    # method that exercises both is enough.
    with db.conn() as c:
        c.execute("SELECT 1")
    return db.path


def clone_missing_tables_from_owner(owner_path, shared_path, table_names):
    """For each table in `table_names`, if it exists in owner DB but not
    in shared DB, copy the CREATE TABLE statement (and any indexes) over.
    Lets the migration handle tables that are created lazily by their
    writers (live_prices by price_poller, sparkline_bars by portal,
    behavior_baselines by Database.set_behavior_baseline,
    system_health_daily by daily_health_aggregator) and don't appear in
    the canonical Database schema setup."""
    owner  = sqlite3.connect(owner_path,  timeout=30)
    shared = sqlite3.connect(shared_path, timeout=30)
    cloned = []
    for name in table_names:
        owner_sql = owner.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (name,)
        ).fetchone()
        shared_has = shared.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)
        ).fetchone()
        if owner_sql and not shared_has and owner_sql[0]:
            shared.execute(owner_sql[0])
            # Also clone any indexes
            for ix in owner.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (name,)
            ).fetchall():
                try:
                    shared.execute(ix[0])
                except sqlite3.OperationalError:
                    pass  # index name conflict is fine
            shared.commit()
            cloned.append(name)
    owner.close()
    shared.close()
    return cloned


def backup_shared(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts  = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dst = os.path.join(BACKUP_DIR, f"signals_master_pre_owner_migration_{ts}.db")
    shutil.copy2(path, dst)
    return dst


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--confirm', action='store_true',
                        help='Actually perform the migration (default: dry-run)')
    parser.add_argument('--no-backup', action='store_true',
                        help='Skip the master DB backup (only use if you have one already)')
    args = parser.parse_args()

    print(f"== Owner → Shared DB migration ==")
    print(f"  owner_id : {OWNER_ID}")
    print(f"  owner DB : {OWNER_DB}")
    print(f"  shared DB: {SHARED_DB}")
    print(f"  mode     : {'LIVE RUN' if args.confirm else 'dry-run (use --confirm to apply)'}")
    print()

    if not os.path.exists(OWNER_DB):
        print(f"ERROR: owner DB does not exist at {OWNER_DB}", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(SHARED_DB):
        # First-time run: master DB will be created when we touch DB()
        print(f"NOTE: shared DB does not exist yet — will be created at {SHARED_DB}")

    # Ensure master schema exists
    master_path = ensure_master_schema()
    actual_shared = os.path.realpath(master_path)
    expected     = os.path.realpath(SHARED_DB)
    if actual_shared != expected:
        print(f"WARNING: get_shared_db() returned {master_path} but expected "
              f"{SHARED_DB} — proceeding with the resolved path.")

    # Clone any tables that owner has but master is missing (live_prices,
    # sparkline_bars, behavior_baselines, system_health_daily — these are
    # created lazily by their writers, not by Database.__init__).
    cloned = clone_missing_tables_from_owner(OWNER_DB, actual_shared, SHARED_TABLES)
    if cloned:
        print(f"  cloned schema for: {', '.join(cloned)}")
        print()

    # Backup master before we touch it
    if args.confirm and not args.no_backup:
        bk = backup_shared(actual_shared)
        print(f"  backup   : {bk}")
        print()
    elif args.confirm and args.no_backup:
        print("  backup   : SKIPPED (--no-backup)")
        print()

    # Open both DBs
    owner  = sqlite3.connect(OWNER_DB,    timeout=30)
    shared = sqlite3.connect(actual_shared, timeout=30)
    owner.row_factory  = sqlite3.Row
    shared.row_factory = sqlite3.Row

    plan = []
    for tbl in SHARED_TABLES:
        owner_n  = row_count(owner,  tbl)
        shared_n = row_count(shared, tbl)
        plan.append((tbl, owner_n, shared_n))

    print(f"  {'table':30}  {'owner':>10}  {'shared (before)':>18}  action")
    print(f"  {'-'*30}  {'-'*10}  {'-'*18}  {'-'*40}")
    for tbl, owner_n, shared_n in plan:
        if owner_n is None:
            action = "skip — owner table missing"
        elif owner_n == 0:
            action = "skip — owner empty"
        elif shared_n is None:
            action = "skip — master schema missing this table"
        else:
            action = f"DELETE master ({shared_n}) + INSERT from owner ({owner_n})"
        print(f"  {tbl:30}  {str(owner_n or '-'):>10}  {str(shared_n or '-'):>18}  {action}")

    if not args.confirm:
        print()
        print("Dry-run complete. Re-run with --confirm to apply.")
        return 0

    # Real run — perform the migration
    print()
    print("Performing migration...")
    print()

    transferred = []
    errors      = []
    for tbl, owner_n, shared_n in plan:
        if not owner_n or shared_n is None:
            continue
        try:
            cols, owner_cols, shared_cols = column_intersection(owner, shared, tbl)
            if not cols:
                errors.append(f"{tbl}: no overlapping columns ({owner_cols=} vs {shared_cols=})")
                continue
            col_csv  = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)

            # Read all rows from owner using common columns
            owner_rows = owner.execute(f"SELECT {col_csv} FROM {tbl}").fetchall()

            with shared:
                shared.execute(f"DELETE FROM {tbl}")
                shared.executemany(
                    f"INSERT INTO {tbl} ({col_csv}) VALUES ({placeholders})",
                    [tuple(r) for r in owner_rows]
                )
            new_n = shared.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            transferred.append((tbl, owner_n, new_n))
            print(f"  ✓ {tbl:30}  {owner_n} → {new_n}")
        except Exception as e:
            errors.append(f"{tbl}: {type(e).__name__}: {e}")
            print(f"  ✗ {tbl:30}  FAILED: {e}")

    # VACUUM
    print()
    print("Vacuuming shared DB...")
    shared.execute("VACUUM")

    owner.close()
    shared.close()

    print()
    print(f"=== Migration summary ===")
    print(f"  tables migrated: {len(transferred)}")
    for tbl, before, after in transferred:
        print(f"    {tbl:30}  {before} → {after}")
    if errors:
        print(f"  errors: {len(errors)}")
        for e in errors:
            print(f"    {e}")
        return 1
    print()
    print("Migration complete. Restart services:")
    print("  sudo systemctl start synthos-market-daemon synthos-news.timer "
          "synthos-portal synthos-trade-daemon")
    return 0


if __name__ == '__main__':
    sys.exit(main())
