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
# OWNER_CUSTOMER_ID is retained as the customer identity for owner-scoped
# Alpaca creds (see _get_alpaca_creds_for_customer below) — it no longer
# determines where shared market-intel data is stored.
MASTER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')
CUSTOMERS_DIR = os.path.join(_DATA, 'customers')
# 2026-04-27: shared DB path moved off the owner customer's signals.db
# onto the system-wide user/signals.db (same file get_shared_db() returns).
# Live prices, screener output, and signals all live here now.
_SHARED_DB_PATH = os.path.join(os.path.dirname(_DIR), 'user', 'signals.db')


def _shared_db_path():
    return _SHARED_DB_PATH


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
    """Collect all unique tickers that need fresh prices.

    Returns the UNION of:
      - OPEN positions across all customers (existing behavior — needed
        for dashboard P&L and trader exit logic)
      - VALIDATED signals from the shared master DB (added for AUTO/USER
        tagging: trader's bulk prefetch reads prices from live_prices,
        so signals awaiting trader action also need fresh prices here)

    This keeps all hot-path price fetches inside the 60s poller so the
    trader itself never blocks on Alpaca HTTP during a dispatch cycle.
    """
    tickers = set()

    # Positions — per-customer DB
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

    # In-flight signals — master DB is the single source of truth for
    # intel the trader may act on. Includes WATCHING (Candidate Generator
    # output) so prices are fresh the moment a candidate is promoted to
    # VALIDATED and hits the trader's pool. Originally expanded to
    # support window_calculator's band computations (Phase 3c.b, since
    # removed 2026-04-24); the expanded poll set is retained because the
    # trader + portal both benefit from fresh candidate-ticker prices.
    try:
        sdb = sqlite3.connect(_shared_db_path(), timeout=5)
        rows = sdb.execute(
            "SELECT DISTINCT ticker FROM signals "
            "WHERE status IN ('VALIDATED', 'WATCHING')"
        ).fetchall()
        for r in rows:
            if r[0]:
                tickers.add(r[0])
        sdb.close()
    except Exception as e:
        log.warning(f"Could not read VALIDATED/WATCHING signals from shared DB: {e}")

    return tickers


def _get_alpaca_creds_for_customer(cid):
    """Get Alpaca credentials for a customer from auth.db."""
    try:
        import auth
        api_key, secret_key = auth.get_alpaca_credentials(cid)
        if api_key:
            return api_key, secret_key
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    # Fallback to env for admin
    if cid == MASTER_CID:
        return os.environ.get('ALPACA_API_KEY', ''), os.environ.get('ALPACA_SECRET_KEY', '')
    return None, None


def _fetch_prices_from_alpaca(needed_tickers=None):
    """Fetch current prices for tickers.

    Two-stage fetch:
      1. Per-customer /v2/positions — returns current price AND unrealized
         P&L for held tickers. Preferred source for held positions because
         the P&L data feeds the dashboard and trader exit logic.
      2. Market-data /v2/stocks/trades/latest — bulk fetch for any ticker
         still missing after stage 1. Covers WATCHING candidate signals
         and VALIDATED signals that aren't yet held, so the trader has
         fresh prices when it evaluates the active signal pool.

    Stage 2 uses owner/master credentials since market-data is account-
    agnostic for equities. Falls through silently if owner creds missing.
    """
    import requests

    prices = {}  # ticker → {price, prev_close, day_change, ...}
    alpaca_url = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
    alpaca_data_url = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')

    # Stage 1: per-customer positions (gives P&L for held)
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
            try:
                _db = sqlite3.connect(_shared_db_path(), timeout=5)
                _db.execute(
                    "INSERT INTO api_calls (timestamp, agent, service, endpoint, method, customer_id, status_code) "
                    "VALUES (datetime('now'), 'price_poller', 'alpaca', '/v2/positions', 'GET', ?, ?)",
                    (cid, r.status_code))
                _db.commit()
                _db.close()
            except Exception as _e:
                log.debug(f"suppressed exception: {_e}")
            if r.status_code != 200:
                continue
            for pos in r.json():
                sym = pos['symbol']
                prices[sym] = {
                    'price':          float(pos.get('current_price', 0) or 0),
                    'prev_close':     float(pos.get('lastday_price', 0) or 0),
                    'day_change':     float(pos.get('unrealized_intraday_pl', 0) or 0),
                    'day_change_pct': float(pos.get('unrealized_intraday_plpc', 0) or 0) * 100,
                    'volume':         0,
                }
        except Exception as e:
            log.warning(f"Alpaca positions fetch failed for {cid[:8]}: {e}")

    # Stage 2: market-data fallback for requested-but-unheld tickers
    if needed_tickers:
        missing = set(needed_tickers) - set(prices.keys())
        if missing:
            api_key, secret_key = _get_alpaca_creds_for_customer(MASTER_CID)
            if not api_key:
                log.debug(f"No owner creds for market-data fallback — {len(missing)} ticker(s) will lack prices")
            else:
                headers = {
                    'APCA-API-KEY-ID': api_key,
                    'APCA-API-SECRET-KEY': secret_key,
                }
                try:
                    r = requests.get(
                        f"{alpaca_data_url}/v2/stocks/trades/latest",
                        params={'symbols': ','.join(sorted(missing))},
                        headers=headers, timeout=8,
                    )
                    try:
                        _db = sqlite3.connect(_shared_db_path(), timeout=5)
                        _db.execute(
                            "INSERT INTO api_calls (timestamp, agent, service, endpoint, method, customer_id, status_code) "
                            "VALUES (datetime('now'), 'price_poller', 'alpaca_data', '/v2/stocks/trades/latest', 'GET', ?, ?)",
                            (MASTER_CID, r.status_code))
                        _db.commit()
                        _db.close()
                    except Exception as _e:
                        log.debug(f"suppressed exception: {_e}")
                    if r.status_code == 200:
                        trades = (r.json() or {}).get('trades', {}) or {}
                        for sym, t in trades.items():
                            if t is None:
                                continue
                            p = t.get('p')
                            if p is None:
                                continue
                            prices[sym] = {
                                'price':          float(p or 0),
                                'prev_close':     0.0,
                                'day_change':     0.0,
                                'day_change_pct': 0.0,
                                'volume':         int(t.get('s', 0) or 0),
                            }
                    else:
                        log.warning(f"Market-data fallback returned {r.status_code}")
                except Exception as e:
                    log.warning(f"Market-data fallback fetch failed: {e}")

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
    prices = _fetch_prices_from_alpaca(needed_tickers=tickers)

    if not prices:
        log.warning("No prices returned from Alpaca")
        return

    # Write to shared DB — single transaction so a mid-loop crash leaves
    # the table consistent. Audit Round 9.5: DELETE-stale moved before the
    # upsert loop so stale rows are gone before new ones land (previously
    # they were cleaned after, leaving a window where the poller could crash
    # after N inserts and leave stale rows until the next full poll).
    db = sqlite3.connect(_shared_db_path(), timeout=10)
    _ensure_table(db)

    ts = now.isoformat()
    updated = 0

    db.execute("BEGIN")
    try:
        # 1. Prune stale tickers first (re-use the already-computed set)
        if tickers:
            placeholders = ','.join('?' * len(tickers))
            db.execute(
                f"DELETE FROM live_prices WHERE ticker NOT IN ({placeholders})",
                list(tickers)
            )

        # 2. Upsert fresh prices
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

        db.execute("COMMIT")
    except Exception as _e:
        db.execute("ROLLBACK")
        log.error(f"live_prices write failed, rolled back: {_e}")
    finally:
        db.close()

    log.info(f"Updated {updated} prices in live_prices table")

    # Post a heartbeat so fault detection's GATE1_LIVENESS check can see us.
    # Price poller runs every 60s from the daemon, so the default
    # HEARTBEAT_STALE_MINUTES (45) is comfortable.
    # 2026-04-27: heartbeats now go to the shared DB (same place fault
    # detection reads from after the get_shared_db() routing change).
    try:
        from retail_database import get_shared_db
        get_shared_db().log_heartbeat("price_poller", "OK")
    except Exception as _e:
        log.debug(f"price_poller heartbeat write failed: {_e}")


if __name__ == '__main__':
    run()
