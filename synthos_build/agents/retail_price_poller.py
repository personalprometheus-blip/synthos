#!/usr/bin/env python3
"""
retail_price_poller.py — Shared Live Price Service
===================================================
Lightweight poller that fetches current prices for ALL held tickers across
ALL customers from Alpaca, writes to a shared `live_prices` table in the
master customer's signals.db.

Portal and sentiment agent read from this table instead of hitting Alpaca
per-request. Runs every 30s during market hours via cron.

Table: live_prices (in _shared_db / master customer DB)
    ticker TEXT PRIMARY KEY
    price REAL
    prev_close REAL
    day_change REAL
    day_change_pct REAL
    market_value REAL     -- per the customer who holds it (largest position wins)
    volume INTEGER
    updated_at TEXT
"""
import os, sys, sqlite3, json, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Setup ──
_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_DIR), 'src')
_DATA = os.path.join(os.path.dirname(_DIR), 'data')
sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(_DIR), 'user', '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('price_poller')

ET = ZoneInfo("America/New_York")
MASTER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')
CUSTOMERS_DIR = os.path.join(_DATA, 'customers')


def _shared_db_path():
    return os.path.join(CUSTOMERS_DIR, MASTER_CID, 'signals.db')


def _ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_prices (
            ticker          TEXT PRIMARY KEY,
            price           REAL NOT NULL DEFAULT 0,
            prev_close      REAL,
            day_change      REAL DEFAULT 0,
            day_change_pct  REAL DEFAULT 0,
            volume          INTEGER DEFAULT 0,
            updated_at      TEXT NOT NULL
        )
    """)
    db.commit()


def _get_all_customer_ids():
    """Return list of customer IDs that have a signals.db."""
    ids = []
    if not os.path.isdir(CUSTOMERS_DIR):
        return ids
    for d in os.listdir(CUSTOMERS_DIR):
        if d == 'default':
            continue
        db_path = os.path.join(CUSTOMERS_DIR, d, 'signals.db')
        if os.path.exists(db_path):
            ids.append(d)
    return ids


def _get_held_tickers():
    """Collect all unique tickers from OPEN positions across all customers."""
    tickers = set()
    for cid in _get_all_customer_ids():
        db_path = os.path.join(CUSTOMERS_DIR, cid, 'signals.db')
        try:
            db = sqlite3.connect(db_path, timeout=5)
            rows = db.execute(
                "SELECT DISTINCT ticker FROM positions WHERE status='OPEN'"
            ).fetchall()
            for r in rows:
                tickers.add(r[0])
            db.close()
        except Exception as e:
            log.warning(f"Could not read positions for {cid[:8]}: {e}")
    return tickers


def _get_alpaca_creds_for_customer(cid):
    """Get Alpaca credentials for a customer from auth.db."""
    try:
        import auth
        api_key, secret_key = auth.get_alpaca_credentials(cid)
        if api_key:
            return api_key, secret_key
    except Exception:
        pass
    # Fallback to env for admin
    if cid == MASTER_CID:
        return os.environ.get('ALPACA_API_KEY', ''), os.environ.get('ALPACA_SECRET_KEY', '')
    return None, None


def _fetch_prices_from_alpaca():
    """Fetch current prices for all held tickers. Uses Alpaca positions API
    for customers who have keys, giving us real-time prices."""
    import requests

    prices = {}  # ticker → {price, prev_close, day_change, ...}
    alpaca_url = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

    for cid in _get_all_customer_ids():
        api_key, secret_key = _get_alpaca_creds_for_customer(cid)
        if not api_key:
            continue

        headers = {
            'APCA-API-KEY-ID': api_key,
            'APCA-API-SECRET-KEY': secret_key,
        }
        try:
            r = requests.get(f"{alpaca_url}/v2/positions", headers=headers, timeout=8)
            # Track API call
            try:
                _db = sqlite3.connect(_shared_db_path(), timeout=5)
                _db.execute(
                    "INSERT INTO api_calls (timestamp, agent, service, endpoint, method, customer_id, status_code) "
                    "VALUES (datetime('now'), 'price_poller', 'alpaca', '/v2/positions', 'GET', ?, ?)",
                    (cid, r.status_code))
                _db.commit()
                _db.close()
            except Exception:
                pass
            if r.status_code != 200:
                continue
            for pos in r.json():
                sym = pos['symbol']
                prices[sym] = {
                    'price':          float(pos.get('current_price', 0) or 0),
                    'prev_close':     float(pos.get('lastday_price', 0) or 0),
                    'day_change':     float(pos.get('unrealized_intraday_pl', 0) or 0),
                    'day_change_pct': float(pos.get('unrealized_intraday_plpc', 0) or 0) * 100,
                    'volume':         0,  # Not available from positions endpoint
                }
        except Exception as e:
            log.warning(f"Alpaca fetch failed for {cid[:8]}: {e}")

    return prices


def run():
    """Main polling cycle."""
    now = datetime.now(ET)

    # Skip outside market hours (extended: 8am-6pm ET for pre/post market)
    hour = now.hour
    weekday = now.weekday()
    if weekday > 4 or hour < 8 or hour >= 18:
        log.debug("Outside market hours — skipping")
        return

    tickers = _get_held_tickers()
    if not tickers:
        log.debug("No held tickers across any customer")
        return

    log.info(f"Polling prices for {len(tickers)} tickers")
    prices = _fetch_prices_from_alpaca()

    if not prices:
        log.warning("No prices returned from Alpaca")
        return

    # Write to shared DB
    db = sqlite3.connect(_shared_db_path(), timeout=10)
    _ensure_table(db)

    ts = now.isoformat()
    updated = 0
    for ticker, data in prices.items():
        db.execute("""
            INSERT INTO live_prices (ticker, price, prev_close, day_change, day_change_pct, volume, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                price=excluded.price,
                prev_close=excluded.prev_close,
                day_change=excluded.day_change,
                day_change_pct=excluded.day_change_pct,
                volume=excluded.volume,
                updated_at=excluded.updated_at
        """, (ticker, data['price'], data['prev_close'], data['day_change'],
              data['day_change_pct'], data['volume'], ts))
        updated += 1

    # Clean stale tickers no longer held
    held = _get_held_tickers()
    if held:
        placeholders = ','.join('?' * len(held))
        db.execute(f"DELETE FROM live_prices WHERE ticker NOT IN ({placeholders})", list(held))

    db.commit()
    db.close()
    log.info(f"Updated {updated} prices in live_prices table")

    # Post a heartbeat so fault detection's GATE1_LIVENESS check can see us.
    # Price poller runs every 60s from the daemon, so the default
    # HEARTBEAT_STALE_MINUTES (45) is comfortable.
    try:
        from retail_database import get_db
        get_db().log_heartbeat("price_poller", "OK")
    except Exception as _e:
        log.debug(f"price_poller heartbeat write failed: {_e}")


if __name__ == '__main__':
    run()
