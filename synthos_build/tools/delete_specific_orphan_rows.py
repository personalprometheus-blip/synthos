#!/usr/bin/env python3
"""
delete_specific_orphan_rows.py
==============================

Targeted, opt-in cleanup of specific known-spurious-orphan position rows.

Why not the general cleanup_settlement_lag_orphans.py?
------------------------------------------------------
That script's auto-detection finds 5 affected positions but only 1
(CSCO at 30eff008, already closed) is unambiguously safe to delete.
The 4 still-open ones (BIL × 3, JOBY × 1) represent REAL Alpaca
positions — deleting them just causes re-adoption on the next cycle
(post-fix-window-expiry).  Different cleanup is needed for each:

  * CSCO (CLOSED, 30eff008) — pure spurious double-count, safe DELETE
  * BIL × 3 (OPEN)          — real cash reserves, bot doesn't actively
                              manage BIL anyway → leave as user-tag
                              for now; sync_bil_reserve fix is the
                              proper resolution
  * JOBY (OPEN, f313a3d9)   — real bot-bought stock, no automated
                              exit while tagged user → needs manual
                              re-tag to bot + trail-stop setup OR
                              manual close via Alpaca

This script does ONE thing only: deletes a hardcoded list of position
ROW IDs verified by the operator.  Each ID is one specific row.  No
heuristics, no auto-detection.  Adds a CSV backup before delete so
nothing is unrecoverable.

Operator sets the list at the top of this file.  Re-run this file with
no edits and a non-matching ID set is a no-op.

Usage
-----
  python3 tools/delete_specific_orphan_rows.py            # dry-run
  python3 tools/delete_specific_orphan_rows.py --confirm  # apply
"""
from __future__ import annotations
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

_DIR    = os.path.dirname(os.path.abspath(__file__))
_BUILD  = os.path.dirname(_DIR)
_DATA   = os.path.join(_BUILD, "data")
CUSTOMERS_DIR = os.path.join(_DATA, "customers")
BACKUP_DIR    = os.path.join(_DATA, "migrations")


# Operator-curated list of (customer_id, position_id) pairs to delete.
# Each entry is a single specific row that has been independently
# confirmed as spurious (e.g., via audit + Alpaca-order verification).
# Format: list of tuples (customer_id, position_id, audit_note).
#
# Already-applied entries are commented-out for audit history.
TARGETS = [
    # 2026-04-28 — applied in commit 8dd5924 + cleanup run.  Removed
    # the spurious user-managed double-count of CSCO's ROTATED_OUT
    # close at 30eff008.  Backup: data/migrations/
    # orphan_targeted_delete_20260428_144046.csv
    # (
    #     "30eff008-c27a-4c71-a788-05f883e4e3a0",
    #     "pos_CSCO_20260428133124",
    #     "spurious double-count of bot's ROTATED_OUT close — applied",
    # ),
]


# Operator-curated list of position rows to RE-TAG (not delete).  Used
# when an orphan-adoption mis-attributed a bot-bought position as
# user-managed, leaving the position open with no bot management
# (no trail-stop, no exit logic).
#
# Each entry: (customer_id, position_id, new_fields_dict, audit_note)
# new_fields supports: managed_by, shares, trail_stop_amt,
#                       trail_stop_pct, vol_bucket
# Other fields are left untouched.
#
# After re-tag, gate 10 on the next trader cycle will pick up the
# position again and start ratcheting its trail.
RETAG_TARGETS = [
    (
        "f313a3d9-e073-4185-8d18-a6550a4e9adc",
        "pos_JOBY_20260428133250",
        {
            "managed_by":     "bot",
            "shares":          55.2486,    # Alpaca actual qty
            "trail_stop_amt":  0.47,       # ~5% of $9.36 — conservative; gate-10 will ratchet
            "trail_stop_pct":  5.0,
        },
        "Verified bot-only via Alpaca order audit 2026-04-28: only 1 "
        "JOBY order on f313a3d9's account today, bot-submitted at "
        "13:30:37, slow market fill completed 13:33:40 (3-minute fill "
        "window).  Settlement-lag race: bot's first DB row was "
        "ghost-closed at 13:31:24 (Alpaca didn't yet show partial fill); "
        "second cycle adopted Alpaca's partial-fill state as user-"
        "managed orphan at 13:32:50 with shares=50.  Final Alpaca "
        "qty = 55.2486 @ $9.356 avg.  Re-tag to bot + correct shares + "
        "set conservative 5% trail so position has automated exit again."
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete the listed rows")
    args = parser.parse_args()

    print("== targeted spurious-orphan cleanup ==")
    print(f"  mode: {'LIVE RUN' if args.confirm else 'dry-run'}")
    print(f"  delete targets: {len(TARGETS)}")
    print(f"  retag  targets: {len(RETAG_TARGETS)}")
    print()

    if not TARGETS and not RETAG_TARGETS:
        print("Empty target lists — nothing to do.")
        return 0

    # Resolve DELETE targets
    rows_to_delete: list[tuple[str, str, str, dict]] = []
    for cid, pid, note in TARGETS:
        path = os.path.join(CUSTOMERS_DIR, cid, "signals.db")
        if not os.path.exists(path):
            print(f"  ✗ MISSING DB: {cid[:8]} → {path}")
            continue
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        db.close()
        if row is None:
            print(f"  ✗ NOT FOUND (delete): {cid[:8]} {pid}")
            continue
        rows_to_delete.append((cid, pid, note, dict(row)))

    # Resolve RETAG targets
    rows_to_retag: list[tuple[str, str, dict, str, dict]] = []
    for cid, pid, fields, note in RETAG_TARGETS:
        path = os.path.join(CUSTOMERS_DIR, cid, "signals.db")
        if not os.path.exists(path):
            print(f"  ✗ MISSING DB: {cid[:8]} → {path}")
            continue
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        db.close()
        if row is None:
            print(f"  ✗ NOT FOUND (retag): {cid[:8]} {pid}")
            continue
        rows_to_retag.append((cid, pid, fields, note, dict(row)))

    if not rows_to_delete and not rows_to_retag:
        print("None of the targets exist — nothing to do.")
        return 0

    if rows_to_delete:
        print()
        print("── DELETE plan ──")
        print(f"  {'cid':10}  {'pid':36}  {'ticker':6}  {'status':7}  {'mb':5}  {'pnl':>10}")
        print(f"  {'-'*10}  {'-'*36}  {'-'*6}  {'-'*7}  {'-'*5}  {'-'*10}")
        for cid, pid, note, row in rows_to_delete:
            print(f"  {cid[:8]:10}  {pid:36}  {row['ticker']:6}  {row['status']:7}  "
                  f"{(row.get('managed_by') or 'bot'):5}  {row.get('pnl', 0):>+10.2f}")
            print(f"      audit: {note}")
            print()

    if rows_to_retag:
        print()
        print("── RETAG plan ──")
        for cid, pid, fields, note, row in rows_to_retag:
            print(f"  {cid[:8]}  {pid}  {row['ticker']}")
            print(f"    BEFORE: managed_by={row.get('managed_by') or 'bot':5}  "
                  f"shares={row.get('shares', 0):.4f}  "
                  f"trail_amt={row.get('trail_stop_amt', 0):.2f}  "
                  f"trail_pct={row.get('trail_stop_pct', 0):.2f}")
            after_parts = []
            for k in ("managed_by", "shares", "trail_stop_amt", "trail_stop_pct", "vol_bucket"):
                if k in fields:
                    cur_val = row.get(k)
                    new_val = fields[k]
                    after_parts.append(f"{k}: {cur_val!r} → {new_val!r}")
            print(f"    CHANGES: " + ("; ".join(after_parts) if after_parts else "(none)"))
            print(f"    audit: {note}")
            print()

    if not args.confirm:
        print("Dry-run complete. Re-run with --confirm to apply.")
        return 0

    # Backup CSVs (one for delete, one for retag pre-state)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    delete_csv_path = os.path.join(BACKUP_DIR,
                                    f"orphan_targeted_delete_{ts}.csv")
    retag_csv_path  = os.path.join(BACKUP_DIR,
                                    f"orphan_targeted_retag_{ts}.csv")

    deleted = 0
    retagged = 0
    failures: list[tuple[str, str]] = []
    delete_backup_rows = []
    retag_backup_rows  = []
    cols_seen: list[str] = []

    print()
    print("Applying...")
    print()

    # ── DELETE phase ──
    if rows_to_delete:
        by_cid: dict[str, list[tuple[str, dict]]] = {}
        for cid, pid, note, row in rows_to_delete:
            by_cid.setdefault(cid, []).append((pid, row))
            if not cols_seen:
                cols_seen = list(row.keys())
        for cid, items in by_cid.items():
            path = os.path.join(CUSTOMERS_DIR, cid, "signals.db")
            try:
                db = sqlite3.connect(path, timeout=10)
                with db:
                    for pid, row in items:
                        delete_backup_rows.append({"_customer_id": cid, **row})
                    ids = [pid for pid, _ in items]
                    ph = ",".join("?" for _ in ids)
                    cur = db.execute(
                        f"DELETE FROM positions WHERE id IN ({ph})", ids,
                    )
                    deleted += cur.rowcount
                    print(f"  ✓ DELETE {cid[:8]}  removed {cur.rowcount} row(s) ({', '.join(ids)})")
                db.close()
            except Exception as e:
                print(f"  ✗ DELETE {cid[:8]}  FAILED: {e}")
                failures.append((cid[:8], f"delete: {e}"))

    # ── RETAG phase ──
    for cid, pid, fields, note, row in rows_to_retag:
        path = os.path.join(CUSTOMERS_DIR, cid, "signals.db")
        try:
            # Restrict to known-safe fields for re-tag
            allowed = {"managed_by", "shares", "trail_stop_amt",
                       "trail_stop_pct", "vol_bucket", "entry_price"}
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                print(f"  ✗ RETAG {cid[:8]} {pid} no allowed fields to update")
                failures.append((cid[:8], f"retag: no allowed fields"))
                continue
            db = sqlite3.connect(path, timeout=10)
            with db:
                retag_backup_rows.append({"_customer_id": cid, **row})
                set_clause = ", ".join(f"{k}=?" for k in updates)
                params = list(updates.values()) + [pid]
                # positions table has no updated_at column; just SET the
                # explicit fields.  Schema check 2026-04-28.
                cur = db.execute(
                    f"UPDATE positions SET {set_clause} WHERE id=?",
                    params,
                )
                if cur.rowcount == 1:
                    retagged += 1
                    print(f"  ✓ RETAG  {cid[:8]} {pid}  ({', '.join(f'{k}→{v}' for k,v in updates.items())})")
                else:
                    print(f"  ✗ RETAG  {cid[:8]} {pid}  rowcount={cur.rowcount}")
                    failures.append((cid[:8], f"retag rowcount={cur.rowcount}"))
            db.close()
        except Exception as e:
            print(f"  ✗ RETAG  {cid[:8]} {pid}  FAILED: {e}")
            failures.append((cid[:8], f"retag: {e}"))

    # Backup files
    if delete_backup_rows:
        all_cols = ["_customer_id"] + (cols_seen or list(delete_backup_rows[0].keys()))
        with open(delete_csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=all_cols, extrasaction="ignore")
            w.writeheader()
            for r in delete_backup_rows:
                w.writerow(r)
        print()
        print(f"  delete-backup CSV: {delete_csv_path}  ({len(delete_backup_rows)} rows)")

    if retag_backup_rows:
        all_cols = ["_customer_id"] + list(retag_backup_rows[0].keys())
        with open(retag_csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=all_cols, extrasaction="ignore")
            w.writeheader()
            for r in retag_backup_rows:
                w.writerow(r)
        print()
        print(f"  retag-pre-state CSV: {retag_csv_path}  ({len(retag_backup_rows)} rows)")

    print()
    print(f"=== Summary ===")
    print(f"  deleted: {deleted}  retagged: {retagged}  failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
