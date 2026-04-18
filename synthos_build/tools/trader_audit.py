#!/usr/bin/env python3
"""Recent trade-decision events per customer.

For each on-disk customer, prints the last few rows from system_log
that correspond to the trader's work — TRADE_DECISION, ACCOUNT_SKIP,
HALT, TRADE_LOOP_COMPLETE, AGENT_COMPLETE, plus anything where the
agent column mentions 'trade'.

Useful when you want to see "why did (or didn't) the trader act this
cycle?" without ssh-tailing journald.

Read-only. Fleet discovery is dynamic via _fleet.iter_customers().

Run:
    cd ~/synthos/synthos_build && python3 tools/trader_audit.py [LIMIT]

LIMIT defaults to 6 rows per customer.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import iter_customers, project_root

from src.retail_database import DB


def _dump(cid: str, name: str, tier: str, limit: int) -> None:
    path = project_root() / 'data' / 'customers' / cid / 'signals.db'
    try:
        cdb = DB(path=str(path))
        with cdb.conn() as c:
            rows = c.execute(
                "SELECT timestamp, event, details FROM system_log "
                "WHERE event IN ('TRADE_DECISION','ACCOUNT_SKIP','HALT',"
                "                 'TRADE_LOOP_COMPLETE','AGENT_COMPLETE') "
                "   OR agent LIKE '%trade%' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception as e:
        print(f"\n=== {name} [{tier}] ===  ERROR {e}")
        return

    print(f"\n=== {name} [{tier}] ===")
    if not rows:
        print("  (no trader log rows)")
        return
    for ts, ev, det in rows:
        ts_s  = (ts or '')[:19]
        ev_s  = str(ev or '')[:25]
        det_s = (det or '')[:110]
        print(f"  {ts_s}  {ev_s.ljust(25)}  {det_s}")


def main() -> int:
    limit = 6
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(f"usage: {sys.argv[0]} [LIMIT]")
            return 1

    for cid, name, tier in iter_customers():
        _dump(cid, name, tier, limit)
    return 0


if __name__ == '__main__':
    sys.exit(main())
