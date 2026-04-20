"""
retail_event_calendar.py — Earnings dates + macro event schedule.

Phase 5.a of TRADER_RESTRUCTURE_PLAN. Gate 4 EVENT_RISK previously logged
`"TODO: FOMC/CPI/earnings calendar still unintegrated"` and let every
signal through. This module fills that gap.

Two sources:
  1. earnings_cache — per-ticker next earnings date. Lazily fetched
     from Yahoo Finance's quoteSummary JSON endpoint (public, no auth),
     cached 7 days. Returns None if the source has no forward earnings
     listed for the ticker.
  2. macro_events — manually populated calendar of FOMC / CPI / NFP
     release dates. Schema is here so admin can INSERT rows; a separate
     task (post-5) can automate via a BLS/Fed feed.

Gate 4 calls `check_event_risk(ticker, within_biz_days=2)` and blocks
entry if either source returns a hit within the window. "Within 2
business days" means: don't buy if earnings are today/tomorrow/after —
hold off until after the volatility event clears.

Cache TTL rationale (7 days): earnings dates move occasionally but not
daily. 7 days is short enough to catch most reschedules, long enough to
avoid hammering Yahoo every gate check.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger('event_calendar')

_EARNINGS_TTL_DAYS = 7
_YAHOO_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
_YAHOO_TIMEOUT = 6


# ── Yahoo earnings fetch ────────────────────────────────────────────────

def _fetch_earnings_yahoo(ticker: str) -> Optional[str]:
    """Return the next earnings date as ISO string, or None.

    Uses Yahoo Finance's quoteSummary with module=calendarEvents. Needs a
    User-Agent header or Yahoo returns 401. Returns None on any failure —
    caller records the miss and tries again on next cache refresh.
    """
    import requests
    try:
        r = requests.get(
            _YAHOO_URL.format(ticker=ticker),
            params={'modules': 'calendarEvents'},
            headers={'User-Agent': 'Mozilla/5.0 (synthos-retail-stack)'},
            timeout=_YAHOO_TIMEOUT,
        )
        if r.status_code != 200:
            log.debug(f"yahoo calendarEvents {ticker} returned {r.status_code}")
            return None
        data = r.json() or {}
        result = (((data.get('quoteSummary') or {}).get('result') or []) or [{}])[0]
        ev = (result.get('calendarEvents') or {}).get('earnings') or {}
        dates = ev.get('earningsDate') or []
        if not dates:
            return None
        # earningsDate can be [{'raw': unix_ts, 'fmt': 'YYYY-MM-DD'}, ...]
        # (range when exact date unconfirmed). Take the first element.
        first = dates[0]
        fmt = first.get('fmt')
        if fmt:
            return fmt
        raw = first.get('raw')
        if raw:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).date().isoformat()
    except Exception as e:
        log.debug(f"yahoo earnings fetch {ticker} raised: {e}")
    return None


# ── Earnings cache (read-through) ──────────────────────────────────────

def get_next_earnings(db, ticker: str) -> Optional[date]:
    """Return the next earnings date for `ticker` or None.

    Read-through cache: hits earnings_cache first, refetches via Yahoo on
    miss or expiry, stores the result (even None — saves an outbound
    request next time). Uses the master/shared DB because the cache is
    global, not per-customer.
    """
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    with db.conn() as c:
        row = c.execute(
            "SELECT next_earnings, expires_at FROM earnings_cache WHERE ticker = ?",
            (ticker,)
        ).fetchone()

    if row and row['expires_at'] > now_iso:
        ne = row['next_earnings']
        if ne:
            try:
                return date.fromisoformat(ne)
            except ValueError:
                pass
        return None

    # Cache miss or expired — refetch
    fresh = _fetch_earnings_yahoo(ticker)
    expires_at = (now_utc + timedelta(days=_EARNINGS_TTL_DAYS)).isoformat()
    with db.conn() as c:
        c.execute(
            "INSERT INTO earnings_cache (ticker, next_earnings, fetched_at, expires_at, source) "
            "VALUES (?, ?, ?, ?, 'yahoo') "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "next_earnings=excluded.next_earnings, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (ticker, fresh, now_iso, expires_at)
        )

    if fresh:
        try:
            return date.fromisoformat(fresh)
        except ValueError:
            return None
    return None


# ── Macro events ───────────────────────────────────────────────────────

def get_upcoming_macro_events(db, within_biz_days: int = 2) -> list[dict]:
    """Return macro_events rows with event_date within `within_biz_days`
    business days of today (inclusive). List of dicts {event_date, event_type, notes}.
    Empty list if table is unpopulated — safely degrades to 'no macro
    risk' in that case."""
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
