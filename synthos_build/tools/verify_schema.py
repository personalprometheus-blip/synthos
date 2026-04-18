#!/usr/bin/env python3
"""Check that a column exists across master + every customer signals.db.

Originally built to verify the screener_score migration. Generalised
so any future column add can be verified the same way.

Read-only. Does not alter any schema.

Run:
    cd ~/synthos/synthos_build && python3 tools/verify_schema.py
    # → defaults to signals.screener_score

    python3 tools/verify_schema.py signals screener_score
    python3 tools/verify_schema.py positions exit_reason
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import iter_customers, project_root

from src.retail_database import get_db, get_customer_db


def _has_column(cdb, table: str, column: str) -> bool:
    with cdb.conn() as c:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def main() -> int:
    table  = sys.argv[1] if len(sys.argv) > 1 else 'signals'
    column = sys.argv[2] if len(sys.argv) > 2 else 'screener_score'

    print(f"Verifying {table}.{column} across master + fleet\n")

    # Master
    try:
        ok_master = _has_column(get_db(), table, column)
    except Exception as e:
        print(f"master: ERROR {e}")
        return 2
    print(f"  master {table}.{column}: {'OK' if ok_master else 'MISSING'}")

    # Per-customer
    all_ok = ok_master
    for cid, name, tier in iter_customers():
        try:
            cdb = get_customer_db(cid)
            ok  = _has_column(cdb, table, column)
        except Exception as e:
            print(f"  {name} [{tier}] ({cid[:8]}): ERROR {e}")
            all_ok = False
            continue
        status = 'OK' if ok else 'MISSING'
        print(f"  {name:30s} [{tier}] ({cid[:8]}) {table}.{column}: {status}")
        all_ok = all_ok and ok

    print()
    print(f"Overall: {'OK' if all_ok else 'MISSING in at least one DB'}")
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
