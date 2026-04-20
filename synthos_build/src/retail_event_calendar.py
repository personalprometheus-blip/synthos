"""
retail_event_calendar.py — Earnings dates + macro event schedule.

Phase 5.a of TRADER_RESTRUCTURE_PLAN. Gate 4 EVENT_RISK previously logged
`"TODO: FOMC/CPI/earnings calendar still unintegrated"` and let every
signal through. This module fills that gap.

Two data sources:
  1. earnings_cache — per-ticker next earnings date. Populated by a
     daily bulk refresh against Nasdaq's calendar API (public, no auth)
     covering the next N business days. Gate 4 just reads the cache —
     no per-signal HTTP call on the hot path.
  2. macro_events — manually populated calendar of FOMC / CPI / NFP
     release dates. Schema is here so admin can INSERT rows; a separate
     task can automate via a BLS/Fed feed.

Gate 4 calls `check_event_risk(ticker, within_biz_days=2)` and blocks
entry if either source returns a hit within the window.

Nasdaq's endpoint (https://api.nasdaq.com/api/calendar/earnings) is
queryable only by date — one HTTP call returns every ticker reporting
that day. Fetching per ticker would be wasteful; instead the daily
refresh walks N business days forward, merges results, and upserts the
earliest date per ticker into earnings_cache. Callers invoke
`refresh_earnings_calendar(db)` once per trading day (wired into
market_daemon startup or pre-market tick).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger('event_calendar')

_EARNINGS_TTL_DAYS = 7
_NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
_NASDAQ_TIMEOUT = 8
_NASDAQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (synthos-retail-stack)',
    'Accept': 'application/json',
}


# ── Nasdaq bulk fetch ───────────────────────────────────────────────────

def _fetch_nasdaq_day(d: date) -> list[str]:
    """Return the list of tickers reporting earnings on date `d`.
    Empty list on any failure."""
    import requests
    try:
        r = requests.get(
            _NASDAQ_URL,
            params={'date': d.isoformat()},
            headers=_NASDAQ_HEADERS,
            timeout=_NASDAQ_TIMEOUT,
        )
        if r.status_code != 200:
            log.debug(f"nasdaq calendar {d} returned {r.status_code}")
            return []
        rows = ((r.json() or {}).get('data') or {}).get('rows') or []
        tickers = []
        for row in rows:
            sym = (row or {}).get('symbol')
            if sym:
                tickers.append(sym.strip().upper())
        return tickers
    except Exception as e:
        log.debug(f"nasdaq fetch {d} raised: {e}")
        return []


def refresh_earnings_calendar(db, horizon_biz_days: int = 10) -> dict:
    """Bulk-refresh earnings_cache covering the next `horizon_biz_days`
    business days. Upserts the *earliest* date per ticker seen across
    the walked range. Returns a summary dict for logging.

    Safe to run multiple times per day — cache TTL prevents stale rows
    from surviving, and the upsert keeps the earliest date found.
    """
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    expires_at = (now_utc + timedelta(days=_EARNINGS_TTL_DAYS)).isoformat()

    # First earnings date per ticker in the horizon (earliest wins)
    first_seen: dict[str, str] = {}
    days_scanned = 0
    cur = today
    while days_scanned < horizon_biz_days:
        if cur.weekday() < 5:
            tickers = _fetch_nasdaq_day(cur)
            iso = cur.isoformat()
            for t in tickers:
                first_seen.setdefault(t, iso)
            days_scanned += 1
        cur = cur + timedelta(days=1)
        # Safety: never walk more than 30 calendar days regardless of weekends
        if (cur - today).days > 30:
            break

    # Upsert into cache
    written = 0
    with db.conn() as c:
        for t, iso in first_seen.items():
            c.execute(
                "INSERT INTO earnings_cache "
                "(ticker, next_earnings, fetched_at, expires_at, source) "
                "VALUES (?, ?, ?, ?, 'nasdaq') "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "next_earnings=excluded.next_earnings, "
                "fetched_at=excluded.fetched_at, "
                "expires_at=excluded.expires_at, "
                "source=excluded.source",
                (t, iso, now_utc.isoformat(), expires_at)
            )
            written += 1

    log.info(
        f"[EARNINGS REFRESH] {days_scanned} biz day(s), "
        f"{len(first_seen)} ticker(s), {written} cache row(s) upserted"
    )
    return {
        'days_scanned': days_scanned,
        'tickers_seen': len(first_seen),
        'cache_rows':   written,
    }


# ── Earnings cache reads ───────────────────────────────────────────────

def get_next_earnings(db, ticker: str) -> Optional[date]:
    """Return the next earnings date for `ticker` or None. Pure cache
    read — no HTTP on the hot path. Returns None if the cache has no
    row, has an expired row, or stored an explicit None."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db.conn() as c:
        row = c.execute(
            "SELECT next_earnings, expires_at FROM earnings_cache WHERE ticker = ?",
            (ticker,)
        ).fetchone()
    if not row:
        return None
    if row['expires_at'] <= now_iso:
        return None
    ne = row['next_earnings']
    if not ne:
        return None
    try:
        return date.fromisoformat(ne)
    except ValueError:
        return None


# ── Macro events ───────────────────────────────────────────────────────

def get_upcoming_macro_events(db, within_biz_days: int = 2) -> list[dict]:
    """Return macro_events rows with event_date within `within_biz_days`
    business days of today (inclusive). List of dicts {event_date,
    event_type, notes}. Empty list if table is unpopulated — degrades
    safely to 'no macro risk'."""
    today = datetime.now(timezone.utc).date()
    end = _business_day_offset(today, within_biz_days)
    with db.conn() as c:
        rows = c.execute(
            "SELECT event_date, event_type, notes FROM macro_events "
            "WHERE event_date >= ? AND event_date <= ? "
            "ORDER BY event_date",
            (today.isoformat(), end.isoformat())
        ).fetchall()
    return [dict(r) for r in rows]


def _business_day_offset(d: date, n: int) -> date:
    """Add n business days to d (Mon-Fri only)."""
    added = 0
    cur = d
    while added < n:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


# ── Combined check used by Gate 4 ──────────────────────────────────────

def check_event_risk(db, ticker: str, within_biz_days: int = 2) -> dict:
    """
    Return a dict the gate decision log can consume:
      {
        'blocked':      bool,
        'reasons':      [str, ...],   # empty if not blocked
        'next_earnings': 'YYYY-MM-DD' or None,
        'macro_events':  [ {event_date, event_type, notes}, ... ]
      }

    Blocks when:
      - Earnings date is today or within `within_biz_days` business days
      - Any macro event is within `within_biz_days` business days
    """
    reasons = []
    today = datetime.now(timezone.utc).date()
    cutoff = _business_day_offset(today, within_biz_days)

    next_earnings = get_next_earnings(db, ticker)
    if next_earnings and today <= next_earnings <= cutoff:
        reasons.append(f"earnings on {next_earnings.isoformat()}")

    macro = get_upcoming_macro_events(db, within_biz_days=within_biz_days)
    for m in macro:
        reasons.append(f"{m['event_type']} on {m['event_date']}")

    return {
        'blocked':       bool(reasons),
        'reasons':       reasons,
        'next_earnings': next_earnings.isoformat() if next_earnings else None,
        'macro_events':  macro,
    }


# ── CLI: manual refresh (useful for testing / one-off admin runs) ─────

if __name__ == '__main__':
    import os
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    from pathlib import Path
    _ROOT_DIR = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_ROOT_DIR / 'src'))
    from retail_database import get_customer_db  # noqa: E402
    owner = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')
    summary = refresh_earnings_calendar(get_customer_db(owner))
    print(summary)
