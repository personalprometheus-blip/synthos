#!/usr/bin/env python3
"""
set_all_customers_to_moderate.py — Fleet-wide reset to the moderate
preset. Used 2026-04-30 to establish a stable middle-ground baseline
across all real customers while V2 (parallel-trader experiment) runs
on its own internal account.

What it does
------------
For each customer in data/customers/, if the customer is NOT internal
(`is_internal != 'true'` in their signals.db customer_settings),
overwrites the preset-related settings with the moderate values:

    PRESET_NAME            = moderate
    MIN_CONFIDENCE         = MEDIUM   (=> trader threshold 0.55)
    MAX_POSITIONS          = 10
    MAX_TRADE_USD          = 2000
    MAX_DAILY_LOSS         = 500
    CLOSE_SESSION_MODE     = moderate
    MAX_DRAWDOWN_PCT       = 15
    MAX_SECTOR_PCT         = 30
    MAX_HOLDING_DAYS       = 15
    MAX_GROSS_EXPOSURE     = 80
    PROFIT_TARGET_MULTIPLE = 2
    MAX_STALENESS          = Aging
    IDLE_RESERVE_PCT       = 20
    ENABLE_BIL_RESERVE     = true

Does NOT touch:
- TRADING_MODE  (paper/live is independent of preset; flipping it
                 would unilaterally move LIVE customers to PAPER or
                 vice-versa — wrong move)
- OPERATING_MODE (auto/managed is also independent)
- KILL_SWITCH    (don't override an active halt)
- The internal V2 test customer (is_internal=true)

Idempotent: re-running just rewrites the same values.

Run on whichever host has the customer signals.db files (pi5):
    cd ~/synthos/synthos_build
    python3 tools/set_all_customers_to_moderate.py
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

MODERATE = {
    'PRESET_NAME':            'moderate',
    'MIN_CONFIDENCE':         'MEDIUM',
    'MAX_POSITIONS':          '10',
    'MAX_TRADE_USD':          '2000',
    'MAX_DAILY_LOSS':         '500',
    'CLOSE_SESSION_MODE':     'moderate',
    'MAX_DRAWDOWN_PCT':       '15',
    'MAX_SECTOR_PCT':         '30',
    'MAX_HOLDING_DAYS':       '15',
    'MAX_GROSS_EXPOSURE':     '80',
    'PROFIT_TARGET_MULTIPLE': '2',
    'MAX_STALENESS':          'Aging',
    'IDLE_RESERVE_PCT':       '20',
    'ENABLE_BIL_RESERVE':     'true',
}


def upsert(conn, key: str, value: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO customer_settings (key, value, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "                                updated_at=excluded.updated_at",
        (key, value, ts)
    )


def main() -> int:
    customers_dir = os.path.join(_HERE, '..', 'data', 'customers')
    if not os.path.isdir(customers_dir):
        print(f'ERROR: {customers_dir} not found', file=sys.stderr)
        return 1

    ts = datetime.now(timezone.utc).isoformat()
    flipped = []
    skipped_internal = []
    skipped_no_db = []

    for cid in sorted(os.listdir(customers_dir)):
        if cid == 'default':
            continue
        db_path = os.path.join(customers_dir, cid, 'signals.db')
        if not os.path.exists(db_path):
            skipped_no_db.append(cid)
            continue

        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            # Read the gate flag + current preset for the report.
            settings = dict(
                (r['key'], r['value'])
                for r in conn.execute(
                    "SELECT key, value FROM customer_settings"
                ).fetchall()
            )
            is_internal = settings.get('is_internal', 'false').lower() == 'true'
            old_preset  = settings.get('PRESET_NAME', '?')

            if is_internal:
                skipped_internal.append((cid, old_preset))
                continue

            for k, v in MODERATE.items():
                upsert(conn, k, v, ts)
            conn.commit()
            flipped.append((cid, old_preset))

    # Report
    print('=' * 78)
    print(f'Fleet preset reset → moderate  ·  {ts}')
    print('=' * 78)
    print()
    print(f'Flipped:  {len(flipped)} customers')
    for cid, was in flipped:
        print(f'   {cid[:8]}   {was:>14}  →  moderate')
    print()
    print(f'Skipped (internal):  {len(skipped_internal)} customers')
    for cid, was in skipped_internal:
        print(f'   {cid[:8]}   {was:>14}  (is_internal=true; preserved)')
    if skipped_no_db:
        print()
        print(f'Skipped (no signals.db):  {len(skipped_no_db)}')
        for cid in skipped_no_db:
            print(f'   {cid[:8]}')
    print()
    print('Settings written per flipped customer:')
    for k, v in MODERATE.items():
        print(f'   {k:<24}  {v}')
    print()
    print('Untouched (per-customer choice preserved):')
    print('   TRADING_MODE     paper/live independent of preset')
    print('   OPERATING_MODE   auto/managed independent of preset')
    print('   KILL_SWITCH      do not override an active halt')

    return 0


if __name__ == '__main__':
    sys.exit(main())
