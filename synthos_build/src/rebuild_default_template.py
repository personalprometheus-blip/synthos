#!/usr/bin/env python3
"""
rebuild_default_template.py — Nightly Default Template Rebuild
==============================================================
Destroys and recreates data/customers/default/signals.db from the
current schema in retail_database.py. This ensures new customers
always get the latest table structure + default settings.

Cron: 0 3 * * * python3 rebuild_default_template.py

Safe: the default DB has no real customer data. Only used as a
template copied by approve_signup().
"""
import os
import sys
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

_SRC_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _SRC_DIR.parent
sys.path.insert(0, str(_SRC_DIR))

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
log = logging.getLogger('template_rebuild')

DEFAULT_DB = _ROOT_DIR / 'data' / 'customers' / 'default' / 'signals.db'

def rebuild():
    log.info(f"Rebuilding default template: {DEFAULT_DB}")

    # Delete old template
    if DEFAULT_DB.exists():
        DEFAULT_DB.unlink()
        log.info("Old template deleted")

    # Create fresh DB via retail_database module (triggers _init_schema + _run_migrations)
    from retail_database import DB
    db = DB(str(DEFAULT_DB))

    # Set conservative defaults in customer_settings
    defaults = {
        'TRADING_MODE': 'PAPER',
        'OPERATING_MODE': 'SUPERVISED',
        'PRESET_NAME': 'conservative',
        'MIN_CONFIDENCE': 'HIGH',
        'MAX_POSITION_PCT': '0.05',
        'MAX_TRADE_USD': '500',
        'MAX_POSITIONS': '5',
        'MAX_DAILY_LOSS': '200',
        'MAX_SECTOR_PCT': '20',
        'CLOSE_SESSION_MODE': 'conservative',
        'MAX_STALENESS': 'Fresh',
        'MAX_DRAWDOWN_PCT': '8',
        'MAX_HOLDING_DAYS': '10',
        'MAX_GROSS_EXPOSURE': '60',
        'PROFIT_TARGET_MULTIPLE': '2.5',
        'ENABLE_BIL_RESERVE': '1',
        'IDLE_RESERVE_PCT': '30',
        'SETUP_COMPLETE': '0',
    }

    with db.conn() as c:
        for k, v in defaults.items():
            c.execute("INSERT OR REPLACE INTO customer_settings (key, value, updated_at) VALUES (?, ?, ?)",
                      (k, v, datetime.utcnow().isoformat()))

    # Seed portfolio
    conn = sqlite3.connect(str(DEFAULT_DB))
    try:
        conn.execute("INSERT INTO portfolio (cash, realized_gains, tax_withdrawn, month_start, updated_at) VALUES (1000, 0, 0, 1000, ?)",
                     (datetime.utcnow().isoformat(),))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already has a row
    conn.close()

    # Verify
    conn = sqlite3.connect(str(DEFAULT_DB))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    settings = conn.execute("SELECT COUNT(*) FROM customer_settings").fetchone()[0]
    conn.close()

    log.info(f"Template rebuilt: {len(tables)} tables, {settings} default settings")
    log.info(f"Tables: {', '.join(sorted(tables))}")


if __name__ == '__main__':
    rebuild()
