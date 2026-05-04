"""
preset_migrate_to_moderate.py — Fleet-wide reset to the moderate preset.

The conservative/moderate/aggressive experiment is being shut down
(2026-05-04). The portal UI has been locked to moderate-only since
2026-04-30, but customer DBs may still hold the PRESET_NAME they
last selected. This script normalizes those rows so the DB matches
what the UI displays.

Two passes per customer:
  1. PRESET_NAME → 'moderate'
  2. Apply the moderate preset's per-setting values to customer_settings,
     so the trader's runtime configuration matches the displayed preset.
     Skipped per-customer if PRESET_NAME was already 'custom' (custom
     overrides preset values by design).

The full preset values are mirrored from synthos_build/src/templates/portal.html
(PRESETS.moderate). When that template's preset values change, update
MODERATE_VALUES below to match.

Pre-live-trading we can modify customer accounts as needed. Post-live
this kind of silent change requires customer notification.

Usage:
  python3 preset_migrate_to_moderate.py            # dry-run (shows what would change)
  python3 preset_migrate_to_moderate.py --apply    # perform the update
"""

import os
import sys
import argparse
import logging

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

import auth as _auth
from retail_database import get_customer_db


# Mirrors PRESETS.moderate in synthos_build/src/templates/portal.html.
# UI form-id → setting-key + preset value.
# Note: cfg-bil-enabled is intentionally excluded here — BIL is being
# disabled fleet-wide separately by preset_disable_bil_all.py, so we
# don't want this script to silently re-enable it on customers who
# previously held a non-moderate preset.
MODERATE_VALUES = {
    'MIN_CONFIDENCE':       'MEDIUM',
    'MAX_POSITION_PCT':     '0.10',     # 10% — stored as decimal
    'MAX_TRADE_USD':        '2000',
    'MAX_DAILY_LOSS':       '500',
    'CLOSE_SESSION_MODE':   'moderate',
    'MAX_DRAWDOWN_PCT':     '15',
    'MAX_SECTOR_PCT':       '30',
    'MAX_HOLD_DAYS':        '15',
    'MAX_EXPOSURE_PCT':     '80',
    'PROFIT_TARGET_PCT':    '2',
    'STALENESS_BUCKET':     'Aging',
    'TRADING_MODE':         'PAPER',
    'BIL_RESERVE_PCT':      '20',
}


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('preset_migrate_to_moderate')


def main():
    parser = argparse.ArgumentParser(description='Reset all customers to moderate preset')
    parser.add_argument('--apply', action='store_true',
                        help='Actually perform the update (default: dry-run)')
    args = parser.parse_args()
    dry = not args.apply

    customers = _auth.list_customers()
    active = [c for c in customers if c.get('is_active')]
    log.info(f"Scanning {len(active)} active customer DBs (mode={'DRY-RUN' if dry else 'APPLY'})")

    preset_changed = 0
    settings_changed = 0
    custom_skipped = 0
    already_moderate = 0
    for cust in active:
        cid = cust['id']
        try:
            db = get_customer_db(cid)
            settings = db.get_all_settings()
            current_preset = settings.get('PRESET_NAME', '')

            if current_preset == 'custom':
                log.info(f"  {cid[:12]} — preset=custom, leaving alone (explicit choice wins)")
                custom_skipped += 1
                continue

            # Step 1: PRESET_NAME → moderate
            if current_preset != 'moderate':
                log.info(f"  {cid[:12]} — preset={current_preset!r} → "
                         f"{'would set' if dry else 'setting'} 'moderate'")
                if not dry:
                    db.set_setting('PRESET_NAME', 'moderate')
                preset_changed += 1
            else:
                already_moderate += 1

            # Step 2: apply moderate values where customer drifted
            drift = []
            for key, target in MODERATE_VALUES.items():
                current = settings.get(key)
                if current != target:
                    drift.append((key, current, target))
            if drift:
                log.info(f"  {cid[:12]} — {len(drift)} setting(s) drifted from moderate:")
                for key, cur, tgt in drift:
                    log.info(f"      {key}: {cur!r} → {tgt!r}")
                if not dry:
                    for key, _cur, tgt in drift:
                        db.set_setting(key, tgt)
                settings_changed += len(drift)
        except Exception as e:
            log.error(f"  {cid[:12]} — error: {e}")

    log.info("---")
    log.info(f"Summary (mode={'DRY-RUN' if dry else 'APPLIED'}):")
    log.info(f"  customers already on moderate: {already_moderate}")
    log.info(f"  customers PRESET_NAME flipped: {preset_changed}")
    log.info(f"  individual settings updated:   {settings_changed}")
    log.info(f"  custom-preset customers left alone: {custom_skipped}")
    if dry:
        log.info("Re-run with --apply to perform the update.")


if __name__ == '__main__':
    main()
