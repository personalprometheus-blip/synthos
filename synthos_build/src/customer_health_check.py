"""Customer DB health checker — called by auditor via SSH."""
import sqlite3, os, json, sys
from datetime import datetime, timedelta, timezone

customers_dir = '/home/pi516gb/synthos/synthos_build/data/customers'
auth_db_path = '/home/pi516gb/synthos/synthos_build/data/auth.db'
results = []

# Phase 7L (2026-04-25): cross-reference on-disk customer directories
# against auth.db so stale dirs left behind by deleted test customers
# don't generate spurious MISSING_DB / NO_SETTINGS findings on the
# auditor (each iteration was producing 14+ HIGH and 9 MEDIUM rows
# until they aged out — auditor finding bucket "test debris").
# Active customer set is the source of truth; anything on disk without
# a matching auth row is treated as debris and skipped silently.
active_cids: set = set()
try:
    with sqlite3.connect(auth_db_path, timeout=5) as _ac:
        _ac.row_factory = sqlite3.Row
        for r in _ac.execute("SELECT id FROM customers"):
            active_cids.add(r['id'])
except Exception:
    # If auth.db is unreachable, fall back to scanning every dir as
    # before so we don't go silent during a real outage.
    active_cids = None

for cid in os.listdir(customers_dir):
    if cid == 'default':
        continue
    # Skip directories whose customer id no longer exists in auth.db —
    # these are leftover test-customer dirs from smoke tests, not real
    # missing data. (active_cids=None means auth.db lookup failed; in
    # that case we keep the old behavior to avoid false negatives.)
    if active_cids is not None and cid not in active_cids:
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
            # BIL over-allocation alert — fires when BIL exceeds 50% of equity.
            # Exempt sub-$500 accounts: on a $61 test account, "BIL 65% of
            # equity ($39/$61)" is noise, not a real concentration risk.
            # Real customer accounts are well above $500, so this keeps the
            # defensive-posture signal (the $39,997/$59,995 case) while
            # silencing near-empty test accounts.
            if equity >= 500 and bil_value / equity > 0.5:
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
