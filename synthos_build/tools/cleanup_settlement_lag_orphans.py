#!/usr/bin/env python3
"""
cleanup_settlement_lag_orphans.py
==================================

One-time cleanup for the settlement-lag orphan-adoption bug discovered
2026-04-28 (see retail_trade_logic_agent.py:3258-3290 fix).

Identifies user-managed positions that were created within 10 minutes
of a bot-managed position for the SAME ticker being closed — i.e.,
they are mis-adopted "orphans" caused by Alpaca settlement lag, not
actual user-initiated trades.

Three categories:
  * STILL_OPEN  — distorting current portfolio view; these get the
                  detection alert and (with --confirm) get DELETED
                  from the positions table because they correspond to
                  no actual Alpaca position any more (the Alpaca
                  position was the bot's, which already settled).
  * ALREADY_CLOSED — the spurious orphan was later ghost-closed; the
                  rows stay (audit history) but their P&L is misleading.
                  We DON'T touch closed rows in this cleanup; reporting
                  layer can filter on managed_by='user' + matched-pair
                  flag if we add one.

Usage
-----
  python3 tools/cleanup_settlement_lag_orphans.py             # dry-run
  python3 tools/cleanup_settlement_lag_orphans.py --confirm   # apply

Backup before delete: each affected position row is exported to a CSV
under data/migrations/ so it can be restored if needed.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sqlite3
import sys
from datetime import datetime, timezone


_DIR    = os.path.dirname(os.path.abspath(__file__))
_BUILD  = os.path.dirname(_DIR)
_DATA   = os.path.join(_BUILD, "data")
CUSTOMERS_DIR = os.path.join(_DATA, "customers")
BACKUP_DIR    = os.path.join(_DATA, "migrations")

# Pair-window: a user-managed position opened within this many minutes
# of a bot-managed close for the same ticker is considered a spurious
# settlement-lag adoption.  Generous — production race window is
# typically seconds.
PAIR_WINDOW_MIN = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete the spurious-orphan position rows")
    args = parser.parse_args()

    print(f"== settlement-lag spurious-orphan cleanup ==")
    print(f"  customers dir: {CUSTOMERS_DIR}")
    print(f"  pair window:    {PAIR_WINDOW_MIN} min")
    print(f"  mode:           {'LIVE RUN' if args.confirm else 'dry-run'}")
    print()

    paths = sorted(glob.glob(os.path.join(CUSTOMERS_DIR, "*", "signals.db")))
    candidates: list[dict] = []  # rows to be touched

    for p in paths:
        cid = p.split("/")[-2]
        cid8 = cid[:8]
        db = sqlite3.connect(p, timeout=10)
        db.row_factory = sqlite3.Row
        rows = db.execute(f"""
            SELECT b.ticker AS ticker,
                   b.id AS bot_id, b.closed_at AS bot_closed_at,
                   b.pnl AS bot_pnl, b.exit_reason AS bot_exit,
                   u.id AS user_id, u.opened_at AS user_opened,
                   u.closed_at AS user_closed,
                   u.status AS user_status,
                   u.pnl AS user_pnl, u.entry_price AS user_entry,
                   u.shares AS user_shares
            FROM positions b
            JOIN positions u ON u.ticker = b.ticker
                             AND COALESCE(u.managed_by,'bot') = 'user'
                             AND u.opened_at >= b.closed_at
                             AND u.opened_at <= datetime(b.closed_at, '+{PAIR_WINDOW_MIN} minutes')
            WHERE b.status = 'CLOSED'
              AND COALESCE(b.managed_by,'bot') = 'bot'
        """).fetchall()
        for r in rows:
            candidates.append({"cid": cid, "cid8": cid8, **dict(r)})
        db.close()

    if not candidates:
        print("Nothing to clean up.")
        return 0

    open_rows  = [c for c in candidates if c["user_status"] == "OPEN"]
    closed_rows = [c for c in candidates if c["user_status"] == "CLOSED"]

    print(f"  candidates total : {len(candidates)}")
    print(f"  STILL_OPEN       : {len(open_rows)}  ← would be deleted")
    print(f"  ALREADY_CLOSED   : {len(closed_rows)}  ← left alone (audit history)")
    print()

    if open_rows:
        print(f"  STILL_OPEN rows that would be DELETED:")
        print(f"  {'cid':10}  {'ticker':6}  {'opened_at':19}  {'shares':>10}  {'entry':>8}  {'pnl':>8}")
        for c in open_rows:
            print(f"  {c['cid8']:10}  {c['ticker']:6}  {c['user_opened']:<19}  "
                  f"{c['user_shares']:>10.4f}  {c['user_entry']:>8.2f}  "
                  f"{c['user_pnl']:>+8.2f}")
        print()

    if closed_rows:
        print(f"  ALREADY_CLOSED rows (kept for audit; bot's pnl was {sum(c['bot_pnl'] for c in closed_rows):+.2f}, spurious user pnl was {sum(c['user_pnl'] or 0 for c in closed_rows):+.2f}):")
        for c in closed_rows:
            print(f"    {c['cid8']:10}  {c['ticker']:6}  user closed {c['user_closed']:19}  "
                  f"user pnl {c['user_pnl']:+.2f}  vs bot pnl {c['bot_pnl']:+.2f}")
        print()

    if not args.confirm:
        print("Dry-run complete. Re-run with --confirm to delete STILL_OPEN spurious orphans.")
        return 0

    if not open_rows:
        print("No STILL_OPEN rows to delete.")
        return 0

    # Apply: per-customer DB DELETE + per-customer CSV backup
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(BACKUP_DIR,
                             f"settlement_lag_orphans_deleted_{ts}.csv")
    by_cust: dict[str, list[dict]] = {}
    for c in open_rows:
        by_cust.setdefault(c["cid"], []).append(c)

    print("Applying...")
    print()
    deleted_total = 0
    failures: list[tuple[str, str]] = []
    rows_written = []

    for cid, items in by_cust.items():
        path = os.path.join(CUSTOMERS_DIR, cid, "signals.db")
        try:
            db = sqlite3.connect(path, timeout=10)
            with db:
                # Pull full rows for backup
                ids = [c["user_id"] for c in items]
                ph = ",".join("?" for _ in ids)
                full = db.execute(
                    f"SELECT * FROM positions WHERE id IN ({ph})", ids
                ).fetchall()
                cols = [d[0] for d in db.execute(
                    f"SELECT * FROM positions WHERE id IN ({ph}) LIMIT 1", ids
                ).description]
                for r in full:
                    rows_written.append({"_customer_id": cid, **{c: r[i] for i, c in enumerate(cols)}})
                # DELETE
                cur = db.execute(
                    f"DELETE FROM positions WHERE id IN ({ph})", ids
                )
                deleted_total += cur.rowcount
                print(f"  ✓ {cid[:8]}  deleted {cur.rowcount} row(s) "
                      f"({', '.join(c['ticker'] for c in items)})")
            db.close()
        except Exception as e:
            print(f"  ✗ {cid[:8]}  FAILED: {e}")
            failures.append((cid[:8], str(e)))

    # Write backup CSV
    if rows_written:
        all_cols = ["_customer_id"] + [c for c in rows_written[0].keys() if c != "_customer_id"]
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=all_cols)
            w.writeheader()
            for r in rows_written:
                w.writerow(r)
        print()
        print(f"  backup CSV: {csv_path}  ({len(rows_written)} rows)")

    print()
    print(f"=== Summary ===")
    print(f"  deleted: {deleted_total}")
    print(f"  failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
