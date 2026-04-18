#!/usr/bin/env python3
"""Apply the 5-tier calibration ladder to the active retail fleet.

⚠️  WRITE TOOL — this is the only script in tools/ that mutates state.
    It overwrites per-customer settings, tags the fleet with TIER=Tn and
    EXPERIMENT_FREEZE=true, and logs every knob's old→new value so the
    experiment can be reversed cleanly at the end of the week.

The tier assignment below is the calibration-experiment version we
defined on 2026-04-16. When the experiment is re-run with a different
fleet composition, edit FLEET at the bottom of this file — the TIERS
dict is the ladder itself and should stay stable.

Tier ladder (see TIERS below for the full knob set):
    T1 Conservative        — Jean-floor
    T2 Mod-Conservative
    T3 Moderate
    T4 Mod-Aggressive
    T5 Aggressive          — noise-seeking

Run:
    cd ~/synthos/synthos_build && python3 tools/apply_tier_ladder.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import project_root

from src.retail_database import DB


# ── Tier ladder ────────────────────────────────────────────────────────
# Keys match what the trader reads from customer_settings. Values are
# written as strings to match the storage convention across the fleet.
# MAX_POSITION_PCT stays fractional (0.03 = 3 %) because that's the
# storage convention for that key.
TIERS = {
    'T1': {  # Conservative — Jean-floor
        'MIN_CONFIDENCE':         'HIGH',
        'MAX_POSITION_PCT':       '0.03',
        'MAX_POSITIONS':          '3',
        'MAX_TRADE_USD':          '500.0',
        'MAX_GROSS_EXPOSURE':     '50',
        'MAX_SECTOR_PCT':         '20',
        'MAX_DAILY_LOSS':         '100',
        'MAX_DRAWDOWN_PCT':       '5',
        'MAX_HOLDING_DAYS':       '10',
        'IDLE_RESERVE_PCT':       '40',
        'PROFIT_TARGET_MULTIPLE': '2.5',
        'CLOSE_SESSION_MODE':     'conservative',
        'ENABLE_BIL_RESERVE':     '1',
        'MAX_STALENESS':          'Fresh',
        'PRESET_NAME':            'custom',
    },
    'T2': {  # Mod-Conservative
        'MIN_CONFIDENCE':         'HIGH',
        'MAX_POSITION_PCT':       '0.06',
        'MAX_POSITIONS':          '6',
        'MAX_TRADE_USD':          '1500.0',
        'MAX_GROSS_EXPOSURE':     '65',
        'MAX_SECTOR_PCT':         '25',
        'MAX_DAILY_LOSS':         '300',
        'MAX_DRAWDOWN_PCT':       '10',
        'MAX_HOLDING_DAYS':       '15',
        'IDLE_RESERVE_PCT':       '25',
        'PROFIT_TARGET_MULTIPLE': '2.0',
        'CLOSE_SESSION_MODE':     'conservative',
        'ENABLE_BIL_RESERVE':     '1',
        'MAX_STALENESS':          'Fresh',
        'PRESET_NAME':            'custom',
    },
    'T3': {  # Moderate
        'MIN_CONFIDENCE':         'MEDIUM',
        'MAX_POSITION_PCT':       '0.10',
        'MAX_POSITIONS':          '10',
        'MAX_TRADE_USD':          '3000.0',
        'MAX_GROSS_EXPOSURE':     '75',
        'MAX_SECTOR_PCT':         '30',
        'MAX_DAILY_LOSS':         '500',
        'MAX_DRAWDOWN_PCT':       '15',
        'MAX_HOLDING_DAYS':       '20',
        'IDLE_RESERVE_PCT':       '20',
        'PROFIT_TARGET_MULTIPLE': '1.75',
        'CLOSE_SESSION_MODE':     'moderate',
        'ENABLE_BIL_RESERVE':     '1',
        'MAX_STALENESS':          'Aging',
        'PRESET_NAME':            'custom',
    },
    'T4': {  # Mod-Aggressive
        'MIN_CONFIDENCE':         'MEDIUM',
        'MAX_POSITION_PCT':       '0.15',
        'MAX_POSITIONS':          '15',
        'MAX_TRADE_USD':          '7500.0',
        'MAX_GROSS_EXPOSURE':     '85',
        'MAX_SECTOR_PCT':         '40',
        'MAX_DAILY_LOSS':         '800',
        'MAX_DRAWDOWN_PCT':       '20',
        'MAX_HOLDING_DAYS':       '30',
        'IDLE_RESERVE_PCT':       '15',
        'PROFIT_TARGET_MULTIPLE': '1.5',
        'CLOSE_SESSION_MODE':     'aggressive',
        'ENABLE_BIL_RESERVE':     '0',
        'MAX_STALENESS':          'Aging',
        'PRESET_NAME':            'custom',
    },
    'T5': {  # Aggressive — noise-seeking
        'MIN_CONFIDENCE':         'LOW',
        'MAX_POSITION_PCT':       '0.25',
        'MAX_POSITIONS':          '25',
        'MAX_TRADE_USD':          '15000.0',
        'MAX_GROSS_EXPOSURE':     '95',
        'MAX_SECTOR_PCT':         '50',
        'MAX_DAILY_LOSS':         '1500',
        'MAX_DRAWDOWN_PCT':       '25',
        'MAX_HOLDING_DAYS':       '45',
        'IDLE_RESERVE_PCT':       '5',
        'PROFIT_TARGET_MULTIPLE': '1.3',
        'CLOSE_SESSION_MODE':     'aggressive',
        'ENABLE_BIL_RESERVE':     '0',
        'MAX_STALENESS':          'Stale',
        'PRESET_NAME':            'custom',
    },
}


# ── Fleet assignment ───────────────────────────────────────────────────
# name is cosmetic — only customer_id matters for the write. Update this
# list when the fleet composition changes before re-running the ladder.
FLEET = [
    ('f313a3d9-e073-4185-8d18-a6550a4e9adc', 'Jean Philippe',     'T1'),
    ('80419c9e-b8c9-4885-8c65-42a77a0a6879', 'test_01 (paper)',   'T1'),
    ('46b10ff0-56ef-431c-94da-30949d02df42', 'Gary Elliott',      'T2'),
    ('0e90f7a3-5f2b-49e2-ba4d-ba7a35faa762', 'Kevin Lynn',        'T3'),
    ('c5fc97cc-439c-4222-a0fd-63fdaa8cb79f', 'Eliana Santamaria', 'T4'),
    ('30eff008-c27a-4c71-a788-05f883e4e3a0', 'Patrick (admin)',   'T5'),
    ('e327ce1b-21d0-4bcf-a5ca-a69db987cddf', 'test_02 (paper)',   'T5'),
]


def main() -> int:
    experiment_id    = datetime.now(timezone.utc).strftime('%Y-%m-%d') + '_tier_ladder'
    experiment_start = datetime.now(timezone.utc).isoformat()

    print("=" * 78)
    print(f"TIER LADDER APPLICATION  —  experiment_id={experiment_id}")
    print("=" * 78)

    root = project_root()
    for cid, name, tier in FLEET:
        path = root / 'data' / 'customers' / cid / 'signals.db'
        try:
            cdb = DB(path=str(path))
        except Exception as e:
            print(f"\n{name}  ({cid[:8]})  [{tier}]  ERROR opening db: {e}")
            continue

        target = TIERS[tier]

        # Read current so we can log before/after
        with cdb.conn() as c:
            current_rows = c.execute(
                "SELECT key, value FROM customer_settings"
            ).fetchall()
        current = dict(current_rows)

        print(f"\n=== {name}  ({cid[:8]})  →  {tier} ===")
        for k, new in target.items():
            old = current.get(k)
            if old != new:
                print(f"  {k:24s} {str(old)!r:>12s}  →  {new!r}")
            cdb.set_setting(k, new)

        # Experiment metadata
        cdb.set_setting('TIER',              tier)
        cdb.set_setting('EXPERIMENT_ID',     experiment_id)
        cdb.set_setting('EXPERIMENT_START',  experiment_start)
        cdb.set_setting('EXPERIMENT_FREEZE', 'true')
        cdb.log_event(
            'EXPERIMENT_TIER_APPLIED',
            agent='tier_ladder_script',
            details=f"tier={tier} experiment_id={experiment_id}",
        )

    print("\n" + "=" * 78)
    print("Done. Fleet frozen under EXPERIMENT_FREEZE=true.")
    print("Run tier_readout.py at any time to see per-tier behavior.")
    print("=" * 78)
    return 0


if __name__ == '__main__':
    sys.exit(main())
