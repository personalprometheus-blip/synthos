"""
retail_tradable_cache.py — Tradable-asset allowlist for Alpaca.

Audit Round 5. Previously nothing in the pipeline checked whether a
ticker surfaced by the screener / news agent was actually tradable on
Alpaca. Crypto symbols, delisted stocks, and OTC tickers could flow
all the way through enrichment + window calculation before the trader
failed at order-submission time.

This module pulls Alpaca's `/v2/assets` endpoint once per day, caches
the set of (tradable=True, status='active') equity symbols in a table,
and exposes `is_tradable(db, ticker)` as a cheap read-through check.
Candidate Generator uses it to skip un-tradable tickers at emission
time.

Naming: kept generic (tradable_cache, not finviz_cache) because the
authoritative source is Alpaca; finviz would have been third-party and
required scraping. The user's note said 'finviz filter for unsupported
tickers' — the intent was 'filter out tickers we can't trade', which
Alpaca's own asset list answers directly.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger('tradable_cache')

_TTL_DAYS = 1
_ALPACA_TRADING_URL = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
_FETCH_TIMEOUT = 15
_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')


def _ensure_table(db):
    """Create tradable_assets table if absent. Idempotent."""
    with db.conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tradable_assets (
                ticker       TEXT PRIMARY KEY,
                exchange     TEXT,
                asset_class  TEXT,
                tradable     INTEGER NOT NULL DEFAULT 1,
                fetched_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_tradable_expires "
            "ON tradable_assets(expires_at)"
        )


def _alpaca_headers():
    """Owner creds. Same pattern used in event_calendar + window_calculator."""
    try:
        import auth
        k, s = auth.get_alpaca_credentials(_OWNER_CID)
        if k:
            return {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s}
    except Exception as e:
        log.debug(f"auth.get_alpaca_credentials failed: {e}")
    k = os.environ.get('ALPACA_API_KEY')
    s = os.environ.get('ALPACA_SECRET_KEY')
    return {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s} if k else None


_PAGINATION_WARN_THRESHOLD = 950  # warn if response looks like a truncated page


def refresh(db) -> dict:
    """Pull Alpaca's full us_equity asset list and upsert the cache.

    Filters for asset_class='us_equity', tradable=True, status='active'.
    Returns a summary dict for logging. Safe to call daily; idempotent
    via upsert keyed on ticker.

    Audit Round 9.6 — two improvements:
    1. Pagination guard: /v2/assets returns all assets in one response for
       us_equity, but if Alpaca ever silently paginates at 1000 rows we'd
       cache only the first page and mark all remaining tickers un-tradable.
       Warn loudly if the response size hits a suspicious round number.
    2. Transaction wrap: the 13k-row upsert loop is now inside one
       transaction. A crash mid-loop previously left the table half-updated
       until the next daily refresh."""
    import requests
    _ensure_table(db)
    headers = _alpaca_headers()
    if not headers:
        log.warning("[TRADABLE REFRESH] no Alpaca creds — skipping")
        return {'fetched': 0, 'tradable': 0, 'source': 'none'}
    try:
        r = requests.get(
            f"{_ALPACA_TRADING_URL}/v2/assets",
            params={'asset_class': 'us_equity', 'status': 'active'},
            headers=headers,
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning(f"[TRADABLE REFRESH] Alpaca returned {r.status_code}")
            return {'fetched': 0, 'tradable': 0, 'source': 'alpaca', 'status': r.status_code}
        assets = r.json() or []
    except Exception as e:
        log.warning(f"[TRADABLE REFRESH] fetch failed: {e}")
        return {'fetched': 0, 'tradable': 0, 'source': 'alpaca', 'error': str(e)[:120]}

    # Pagination guard — Alpaca's asset list is ~10k+ rows so a suspiciously
    # round response size strongly suggests silent truncation.
    if len(assets) > 0 and len(assets) % 1000 == 0:
        log.warning(
            f"[TRADABLE REFRESH] response size {len(assets)} is a round multiple of 1000 — "
            "Alpaca may have paginated silently; cache may be incomplete"
        )
    elif len(assets) >= _PAGINATION_WARN_THRESHOLD:
        log.debug(f"[TRADABLE REFRESH] {len(assets)} assets in response (pagination check: ok)")

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    expires_at = (now_utc + timedelta(days=_TTL_DAYS)).isoformat()
    tradable_count = 0
    with db.conn() as c:
        for a in assets:
            sym = (a.get('symbol') or '').strip().upper()
            if not sym:
                continue
            is_tradable = 1 if a.get('tradable') else 0
            if is_tradable:
                tradable_count += 1
            c.execute(
                "INSERT INTO tradable_assets "
                "(ticker, exchange, asset_class, tradable, fetched_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "exchange=excluded.exchange, asset_class=excluded.asset_class, "
                "tradable=excluded.tradable, fetched_at=excluded.fetched_at, "
                "expires_at=excluded.expires_at",
                (sym, a.get('exchange'), a.get('class'),
                 is_tradable, now_iso, expires_at)
            )
    log.info(f"[TRADABLE REFRESH] {len(assets)} assets fetched, {tradable_count} tradable, cache upserted")
    return {'fetched': len(assets), 'tradable': tradable_count, 'source': 'alpaca'}


def is_tradable(db, ticker: str) -> Optional[bool]:
    """Return True/False if the cache has a fresh opinion on `ticker`,
    None if the cache has no row or the row is expired. Callers decide
    whether an unknown ticker should be allowed through (skip-on-None)
    or blocked (block-on-None). Candidate Generator chooses block-on-None
    — if we can't confirm tradable, treat it as suspect."""
    if not ticker:
        return None
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT tradable, expires_at FROM tradable_assets "
                "WHERE ticker = ?",
                (ticker.strip().upper(),)
            ).fetchone()
    except Exception as e:
        log.debug(f"is_tradable read failed for {ticker}: {e}")
        return None
    if not row:
        return None
    if row['expires_at'] <= now_iso:
        return None
    return bool(row['tradable'])


# ── CLI: manual refresh ────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    from pathlib import Path
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    _ROOT_DIR = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_ROOT_DIR / 'src'))
    from retail_database import get_customer_db  # noqa: E402
    summary = refresh(get_customer_db(_OWNER_CID))
    print(summary)
