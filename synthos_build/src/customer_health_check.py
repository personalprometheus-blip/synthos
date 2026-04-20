"""Customer DB health checker — called by auditor via SSH."""
import sqlite3, os, json, sys
from datetime import datetime, timedelta, timezone

customers_dir = '/home/pi516gb/synthos/synthos_build/data/customers'
results = []

for cid in os.listdir(customers_dir):
    if cid == 'default':
        continue
    db_path = os.path.join(customers_dir, cid, 'signals.db')
    if not os.path.exists(db_path):
        results.append({'cid': cid[:12], 'severity': 'high', 'issue': 'MISSING_DB', 'detail': 'signals.db does not exist'})
        continue

    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row

        # Check portfolio cash
        port = db.execute('SELECT cash FROM portfolio LIMIT 1').fetchone()
        if port:
            cash = float(port['cash'])
            if cash < 0:
                results.append({'cid': cid[:12], 'severity': 'critical', 'issue': 'NEGATIVE_CASH', 'detail': f'Cash is ${cash:,.2f}'})

        # Check BIL over-allocation
        positions = db.execute("SELECT ticker, entry_price, shares FROM positions WHERE status='OPEN'").fetchall()
        bil_value = 0
        total_value = 0
        for p in positions:
            val = float(p['entry_price']) * float(p['shares'])
            total_value += val
            if p['ticker'] == 'BIL':
                bil_value = val

        if port and total_value > 0:
            equity = float(port['cash']) + total_value
            if equity > 0 and bil_value / equity > 0.5:
                results.append({'cid': cid[:12], 'severity': 'high', 'issue': 'BIL_OVER_ALLOCATED',
                    'detail': f'BIL is {bil_value/equity*100:.0f}% of equity (${bil_value:,.0f}/${equity:,.0f})'})

        # Check customer_settings
        try:
            settings_count = db.execute('SELECT COUNT(*) FROM customer_settings').fetchone()[0]
            if settings_count == 0:
                results.append({'cid': cid[:12], 'severity': 'medium', 'issue': 'NO_SETTINGS', 'detail': 'customer_settings table is empty'})
        except Exception:
            results.append({'cid': cid[:12], 'severity': 'high', 'issue': 'NO_SETTINGS_TABLE', 'detail': 'customer_settings table missing'})

        # Check for stale agent activity (no events in 24h during weekdays)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if now.weekday() < 5:
            cutoff = (now - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            recent = db.execute("SELECT COUNT(*) FROM system_log WHERE timestamp > ?", (cutoff,)).fetchone()[0]
            if recent == 0:
                results.append({'cid': cid[:12], 'severity': 'medium', 'issue': 'STALE_ACTIVITY', 'detail': 'No agent activity in 24h'})

        db.close()
    except Exception as e:
        results.append({'cid': cid[:12], 'severity': 'high', 'issue': 'DB_ERROR', 'detail': str(e)[:80]})

print(json.dumps(results))
