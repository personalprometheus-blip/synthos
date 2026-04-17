"""
notification_cleanup.py — One-shot (re-runnable) notification spam cleanup

Deduplicates the `notifications` table across all active customer DBs.
Keeps the most-recent N rows per (title, category) combo for high-noise
categories (daily / system / alert), and leaves genuine trade/account/
approval notifications untouched.

The forward fix (dedup_key column + add_notification guards) prevents new
spam; this script cleans up the accumulated historical spam from the many
dry-run sessions before the fix landed.

Usage:
  python3 notification_cleanup.py --dry-run       # show what would be deleted
  python3 notification_cleanup.py --apply         # actually delete
  python3 notification_cleanup.py --apply --keep 2   # keep N per duplicate (default 1)

Safe to re-run; idempotent — second run finds nothing left to prune.
"""

import os
import sys
import argparse
import logging

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

import auth as _auth
from retail_database import get_customer_db


# Categories this cleanup touches. Trade / account / approval notifications
# are real events (one per execution or milestone) — don't dedupe those.
NOISY_CATEGORIES = ('daily', 'system', 'alert')


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('notif_cleanup')


def cleanup_customer(db, keep=1, dry_run=True):
    """Remove duplicate notifications for one customer, keeping the most recent
    `keep` rows per (title, category) combo within NOISY_CATEGORIES.
    Returns (deleted_count, duplicate_groups)."""
    placeholders = ','.join('?' * len(NOISY_CATEGORIES))
    with db.conn() as c:
        groups = c.execute(f"""
            SELECT title, category, COUNT(*) AS n
            FROM notifications
            WHERE category IN ({placeholders})
            GROUP BY title, category
            HAVING n > ?
            ORDER BY n DESC
        """, (*NOISY_CATEGORIES, keep)).fetchall()

        to_delete = []
        for g in groups:
            rows = c.execute("""
                SELECT id FROM notifications
                WHERE title=? AND category=?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            """, (g['title'], g['category'], keep)).fetchall()
            to_delete.extend(r['id'] for r in rows)

        if not to_delete:
            return 0, 0

        if dry_run:
            for g in groups:
                excess = g['n'] - keep
                log.info(f"    would delete x{excess:3d}  [{g['category']:7s}] {g['title'][:60]}")
        else:
            # Delete in batches of 500 to stay within SQLite parameter limits
            for i in range(0, len(to_delete), 500):
                batch = to_delete[i:i+500]
                placeholders = ','.join('?' * len(batch))
                c.execute(f"DELETE FROM notifications WHERE id IN ({placeholders})", batch)
            log.info(f"    deleted {len(to_delete)} rows across {len(groups)} duplicate groups")

        return len(to_delete), len(groups)


def main():
    parser = argparse.ArgumentParser(description='Deduplicate notification spam')
    parser.add_argument('--apply', action='store_true',
                        help='Actually perform deletes (default: dry-run)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be deleted (explicit, default)')
    parser.add_argument('--keep', type=int, default=1,
                        help='Keep most-recent N rows per duplicate group (default 1)')
    args = parser.parse_args()

    dry = not args.apply
    mode = 'DRY-RUN' if dry else 'APPLY'
    log.info(f"Notification cleanup — mode={mode} keep={args.keep}")
    log.info(f"Targeting categories: {', '.join(NOISY_CATEGORIES)}")

    total_deleted = 0
    total_groups = 0
    customers = _auth.list_customers()
    active = [c for c in customers if c.get('is_active')]
    log.info(f"Scanning {len(active)} active customer DBs")

    for cust in active:
        cid = cust['id']
        try:
            db = get_customer_db(cid)
            deleted, groups = cleanup_customer(db, keep=args.keep, dry_run=dry)
            if deleted:
                log.info(f"  {cid[:12]} — {deleted} rows in {groups} groups")
                total_deleted += deleted
                total_groups += groups
        except Exception as e:
            log.error(f"  {cid[:12]} — error: {e}")

    verb = 'would delete' if dry else 'deleted'
    log.info(f"Summary: {verb} {total_deleted} rows across {total_groups} duplicate groups")
    if dry:
        log.info("Re-run with --apply to actually delete")


if __name__ == '__main__':
    main()
