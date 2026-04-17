"""
preset_migrate_bil.py — One-shot migration for the aggressive-preset BIL flip

Flips ENABLE_BIL_RESERVE to '0' for customers whose PRESET_NAME is
'aggressive'. Prior to this change the aggressive preset wrote BIL=on;
new presets default BIL=off for aggressive only. This script aligns
existing aggressive-preset customers with the new default.

Customers on conservative/moderate presets: unchanged (BIL stays on).
Customers on custom: unchanged (their explicit choice wins).

Pre-live the user has said we can modify existing customer accounts as
needed. Post-live this kind of silent change requires customer
notification. Script is re-runnable (idempotent).

Usage:
  python3 preset_migrate_bil.py                # dry-run (shows what would change)
  python3 preset_migrate_bil.py --apply        # perform the update
"""

import os
import sys
import argparse
import logging

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

import auth as _auth
from retail_database import get_customer_db


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('preset_migrate_bil')


def main():
    parser = argparse.ArgumentParser(description='Flip aggressive preset BIL to off')
    parser.add_argument('--apply', action='store_true',
                        help='Actually perform the update (default: dry-run)')
    args = parser.parse_args()
    dry = not args.apply

    customers = _auth.list_customers()
    active = [c for c in customers if c.get('is_active')]
    log.info(f"Scanning {len(active)} active customer DBs (mode={'DRY-RUN' if dry else 'APPLY'})")

    changed = 0
    skipped = 0
    for cust in active:
        cid = cust['id']
        try:
            db = get_customer_db(cid)
            settings = db.get_all_settings()
            preset  = settings.get('PRESET_NAME', '')
            bil     = settings.get('ENABLE_BIL_RESERVE', '1')
            if preset != 'aggressive':
                skipped += 1
                continue
            if bil == '0':
                log.info(f"  {cid[:12]} — already BIL=off, no change")
                skipped += 1
                continue
            log.info(f"  {cid[:12]} — preset=aggressive, BIL={bil} → {'would update' if dry else 'updating'} to 0")
            if not dry:
                db.set_setting('ENABLE_BIL_RESERVE', '0')
            changed += 1
        except Exception as e:
            log.error(f"  {cid[:12]} — error: {e}")

    verb = 'would change' if dry else 'changed'
    log.info(f"Summary: {verb} {changed} customers, skipped {skipped}")
    if dry:
        log.info("Re-run with --apply to perform the update")


if __name__ == '__main__':
    main()
