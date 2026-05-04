"""
preset_disable_bil_all.py — Fleet-wide BIL reserve deactivation.

The original preset_migrate_bil.py flipped ENABLE_BIL_RESERVE='0' only
for customers on the aggressive preset (the rest kept BIL on). This
follow-up script disables BIL for ALL active customers (2026-05-04).

Reasoning: the BIL reserve function has been proven (gate trip
behavior, target-tracking, post-trade reconcile all working). The
operator now wants to test the off-ramp UX — what happens when a
customer turns BIL off — and wants idle cash returned to the
deployable pool while we tune cash-utilization.

Trader-side behavior change shipped alongside this script:
  When ENABLE_BIL_RESERVE='0' AND a BIL position still exists, the
  trader's sync_bil_reserve() now liquidates the position on the
  next cycle (single-shot close). After the wind-down it no-ops, so
  flipping back to '1' later starts the rebuild from zero.

Pre-live we can modify customer accounts as needed. Post-live this
silent change requires customer notification. Re-runnable (idempotent).

Usage:
  python3 preset_disable_bil_all.py             # dry-run
  python3 preset_disable_bil_all.py --apply     # perform
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
log = logging.getLogger('preset_disable_bil_all')


def main():
    parser = argparse.ArgumentParser(description='Fleet-wide BIL reserve deactivation')
    parser.add_argument('--apply', action='store_true',
                        help='Actually perform the update (default: dry-run)')
    args = parser.parse_args()
    dry = not args.apply

    customers = _auth.list_customers()
    active = [c for c in customers if c.get('is_active')]
    log.info(f"Scanning {len(active)} active customer DBs (mode={'DRY-RUN' if dry else 'APPLY'})")

    changed = 0
    already_off = 0
    errors = 0
    for cust in active:
        cid = cust['id']
        try:
            db = get_customer_db(cid)
            settings = db.get_all_settings()
            current = settings.get('ENABLE_BIL_RESERVE', '1')
            if current == '0':
                log.info(f"  {cid[:12]} — already BIL=off")
                already_off += 1
                continue
            log.info(f"  {cid[:12]} — BIL={current!r} → "
                     f"{'would set' if dry else 'setting'} '0'")
            if not dry:
                db.set_setting('ENABLE_BIL_RESERVE', '0')
            changed += 1
        except Exception as e:
            log.error(f"  {cid[:12]} — error: {e}")
            errors += 1

    log.info("---")
    log.info(f"Summary (mode={'DRY-RUN' if dry else 'APPLIED'}):")
    log.info(f"  flipped to off: {changed}")
    log.info(f"  already off:    {already_off}")
    log.info(f"  errors:         {errors}")
    if dry:
        log.info("Re-run with --apply to perform the update.")
    else:
        log.info("Trader will wind down existing BIL positions on next cycle.")


if __name__ == '__main__':
    main()
