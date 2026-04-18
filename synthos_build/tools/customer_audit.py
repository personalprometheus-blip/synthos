#!/usr/bin/env python3
"""Per-customer settings + recent trades snapshot.

Dumps, for every on-disk customer:
  - headline settings (trading_mode, risk budget, per-trade caps, account
    gating flags)
  - cash balance from the latest portfolio snapshot
  - the 5 most recent positions (open or closed)

Read-only. Intended as a quick "what does the fleet look like right
now?" check without clicking through the portal.

Run:
    cd ~/synthos/synthos_build && python3 tools/customer_audit.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import iter_customers, project_root

from src.retail_database import DB


# Settings keys surfaced on every customer. Order matters — this is
# ordered by "how often do I want to see it" rather than alphabetical.
_HEADLINE_KEYS = (
    'TIER',
    'trading_mode',
    'operating_mode',
    'MIN_CONFIDENCE',
    'MAX_POSITION_PCT',
    'MAX_POSITIONS',
    'MAX_TRADE_USD',
    'MAX_DAILY_LOSS',
    'risk_budget',
    'daily_loss_limit',
    'max_position_pct',
    'tos_accepted',
    'account_enabled',
    'new_account',
    'first_trade_gate',
    'per_trade_dollar_cap',
    'min_trade_value',
    'EXPERIMENT_ID',
)


def _dump_customer(cid: str, name: str, tier: str) -> None:
    path = project_root() / 'data' / 'customers' / cid / 'signals.db'
    try:
        cdb = DB(path=str(path))
    except Exception as e:
        print(f"\n{name} [{tier}]  ({cid[:8]})  ERROR opening db: {e}")
        return

    with cdb.conn() as c:
        settings_rows = c.execute(
            "SELECT key, value FROM customer_settings"
        ).fetchall()
        settings = dict(settings_rows)

        trades = c.execute(
            "SELECT ticker, shares, entry_price, status, opened_at, exit_reason "
            "FROM positions ORDER BY opened_at DESC LIMIT 5"
        ).fetchall()

        port = c.execute(
            "SELECT cash FROM portfolio ORDER BY id DESC LIMIT 1"
        ).fetchone()

    cash = port[0] if port else '?'
    print(f"\n{name} [{tier}]  ({cid[:8]})  cash=${cash}")

    for k in _HEADLINE_KEYS:
        if k in settings:
            v = (settings[k] or '')[:80]
            print(f"    [{k}] = {v}")

    if trades:
        for ticker, shares, entry_price, status, opened_at, exit_reason in trades:
            dollars = (shares or 0) * (entry_price or 0)
            exit_s  = exit_reason or ''
            print(
                f"    trade {(opened_at or '?')[:19]:19s} "
                f"{(ticker or '?'):6s} shares={shares} @ ${entry_price} "
                f"(${dollars:.2f}) {status or '?'}  {exit_s}"
            )
    else:
        print("    (no positions)")


def main() -> int:
    for cid, name, tier in iter_customers():
        _dump_customer(cid, name, tier)
    return 0


if __name__ == '__main__':
    sys.exit(main())
