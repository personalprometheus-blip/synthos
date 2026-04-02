"""
shutdown.py — Graceful Pre-Maintenance Shutdown
Synthos

Runs: Saturday 3:55 AM ET via cron (before 4:00 AM maintenance reboot)
  55 3 * * 6  python3 /home/pi/synthos/shutdown.py

Tasks:
  - Log planned shutdown event
  - Run database integrity check
  - Mark any in-progress operations as INTERRUPTED
  - Flush all pending writes
  - Exit cleanly so the maintenance reboot can proceed

Safe to run manually:
  python3 shutdown.py
"""

import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('shutdown')


def run():
    log.info("Synthos graceful shutdown starting — Saturday maintenance window")

    try:
        from retail_database import get_db
        db = get_db()
    except Exception as e:
        log.error(f"Cannot load database: {e}")
        sys.exit(1)

    # 1. Log the shutdown
    db.log_event(
        "PLANNED_SHUTDOWN",
        agent="shutdown",
        details="Saturday maintenance window — 4:00 AM reboot scheduled",
    )

    # 2. Mark any in-progress operations
    import sqlite3
    try:
        with sqlite3.connect(db.path) as c:
            result = c.execute("""
                UPDATE signals SET status='INTERRUPTED', updated_at=?
                WHERE status='PROCESSING'
            """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
            if result.rowcount:
                log.info(f"Marked {result.rowcount} in-progress signal(s) as INTERRUPTED")
            c.commit()
    except Exception as e:
        log.warning(f"Could not mark interrupted signals: {e}")

    # 3. Integrity check before shutdown
    if db.integrity_check():
        log.info("Pre-shutdown integrity check: PASSED")
    else:
        log.error("Pre-shutdown integrity check: FAILED — proceeding anyway but investigate after reboot")
        db.log_event("INTEGRITY_FAIL", agent="shutdown",
                     details="Integrity check failed before maintenance shutdown")

    # 4. Log final portfolio state
    try:
        portfolio = db.get_portfolio()
        positions = db.get_open_positions()
        total = portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions)
        db.log_event(
            "SHUTDOWN_PORTFOLIO_SNAPSHOT",
            agent="shutdown",
            details=f"cash={portfolio['cash']:.2f} positions={len(positions)}",
            portfolio_value=total,
        )
        log.info(f"Portfolio snapshot: ${total:.2f} | {len(positions)} open positions")
    except Exception as e:
        log.warning(f"Could not snapshot portfolio: {e}")

    log.info("Graceful shutdown complete — safe to proceed with maintenance")


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        log.error(f"Shutdown script error: {e}", exc_info=True)
        # Don't sys.exit(1) here — don't block the maintenance reboot
