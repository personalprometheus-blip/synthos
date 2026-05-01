"""
market_sentiment_agent.py — Market Sentiment Agent (Pulse)
Synthos · Agent 3

Runs:
  Every 30 minutes during market hours (9am-4pm ET weekdays)

Responsibilities:
  - 27-gate deterministic market sentiment analysis spine (Phase 1)
  - Monitor open positions for market deterioration (Phase 2)
  - Check pre-trade queue sentiment before trader acts (Phase 3)
  - Detect cascade patterns (multiple sellers simultaneously)
  - Raise urgent flags that bypass normal session schedule
  - Log all scan results to scan_log table

No LLM in any decision path. All gate logic is deterministic and traceable.

Free data sources:
  - Alpaca Data API (Iex feed — free tier)
  - Yahoo Finance (VIX via chart API)
  - SEC EDGAR (insider transactions)
  - CBOE put/call ratio (public data)
  - Finviz (volume and price data)
  - Internal news_feed DB table (populated by Agent 2)

Usage:
  python3 market_sentiment_agent.py
"""

import os
import sys
import time
import json
import re
import math
import logging
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, get_shared_db, acquire_agent_lock, release_agent_lock

def _master_db():
    """2026-04-27: returns the shared market-intel DB. Was previously routing
    through get_customer_db(OWNER_CUSTOMER_ID) which coupled shared output
    to the owner customer's DB.  See retail_database.get_shared_db()."""
    return get_shared_db()

# ── CONFIG ────────────────────────────────────────────────────────────────
ET                = ZoneInfo("America/New_York")
MAX_RETRIES       = 3
REQUEST_TIMEOUT   = 10

# Alpaca Data API
ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL   = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')

# Cascade thresholds — tuned to distinguish noise from real deterioration
CASCADE_PUT_CALL_THRESHOLD    = 1.10   # put/call > 110% of 30d avg
CASCADE_SELLER_DOM_THRESHOLD  = 0.70   # 70%+ seller dominance
CASCADE_VOLUME_THRESHOLD      = 2.50   # 250%+ of average volume
CASCADE_INSIDER_SELLS_MIN     = 4      # at least 4 insider sells with 0 buys

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('market_sentiment_agent')


# ── RETRY HELPERS ─────────────────────────────────────────────────────────

# Yahoo RSS removed — only VIX + treasury chart calls retained

def fetch_with_retry(url, params=None, headers=None, max_retries=MAX_RETRIES):
    """Fetch URL with exponential backoff. Returns response or None.

    Audit Round 7.3 — tuple timeout (5s connect, REQUEST_TIMEOUT read) so
    a slow DNS / SYN failure doesn't eat the full budget on every retry.
    Logs the HTTP status code explicitly when available so 429 rate
    limits and 5xx upstream issues are traceable (previously the generic
    'Fetch failed' didn't surface it). Retry-on-any-exception behavior
    retained — raise_for_status() raises HTTPError for 4xx/5xx which
    triggers the backoff.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=(5, REQUEST_TIMEOUT))
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(
                    f"Fetch failed ({url[:60]}) attempt {attempt+1}/{max_retries}"
                    f" status={status} — retry in {wait}s: {e}"
                )
                time.sleep(wait)
    log.error(
        f"Fetch permanently failed after {max_retries} attempts "
        f"({url[:80]}): {last_error}"
    )
    return None


# ── DATA FETCHERS ─────────────────────────────────────────────────────────

# Per-run cache for the market-wide put/call ratio.  CBOE only provides
# the aggregate equity ratio on the free tier (per-ticker requires a
# paid feed), so the same ratio applies to every ticker we score in a
# given screener run.  Without caching we hit CBOE 300+ times per
# screener pass — Cloudflare blocks that pattern, every call returns
# None, and every screener-sentiment fulfillment falls into the
# error-handler path (score=0.5).  Caching to 1 fetch/run fixes the
# reliability + the data-quality at once.  Reset at run() start in
# parallel with _EDGAR_CACHE / _FINVIZ_CACHE / _VOLUME_CACHE.
_PUT_CALL_CACHE: dict = {}


def fetch_put_call_ratio(ticker):
    """
    Fetch put/call ratio from CBOE public data.
    Returns (current_ratio, avg_30d) or (None, None) on failure.

    Note: CBOE provides aggregate market put/call. Per-ticker options
    data requires a paid feed. On free tier we use market-wide ratio
    as a proxy and note this limitation in the analysis.

    The `ticker` arg is ignored on the free tier (CBOE only provides the
    market-wide aggregate); we cache the result for the run so 330
    sentiment fulfilments share one HTTP call.
    """
    cached = _PUT_CALL_CACHE.get("_market")
    if cached is not None:
        return cached

    url = "https://www.cboe.com/us/options/market_statistics/daily/"
    r = fetch_with_retry(url)
    try:
        _master_db().log_api_call('sentiment_agent', '/us/options/market_statistics/daily/', 'GET', 'cboe', status_code=getattr(r, 'status_code', None))
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    if not r:
        _PUT_CALL_CACHE["_market"] = (None, None)
        return None, None

    # CBOE returns HTML — parse the equity put/call ratio
    # Free tier: market-wide equity put/call ratio
    try:
        text = r.text
        # Look for equity put/call ratio in the page
        match = re.search(r'Equity Put/Call Ratio.*?(\d+\.\d+)', text, re.DOTALL)
        if match:
            ratio = float(match.group(1))
            # Approximate 30d avg (CBOE historical avg ~0.60-0.65)
            avg30d = 0.62
            log.info(f"CBOE put/call ratio: {ratio:.2f} (30d avg ~{avg30d:.2f})")
            _PUT_CALL_CACHE["_market"] = (ratio, avg30d)
            return ratio, avg30d
    except Exception as e:
        log.warning(f"CBOE parse error: {e}")

    # No fallback — Finviz doesn't expose per-ticker put/call in free HTML.
    # Previously we hit Finviz anyway and parsed it just to return (None, None);
    # that was a wasted HTTP call per CBOE failure. Now we bail cleanly.
    _PUT_CALL_CACHE["_market"] = (None, None)
    return None, None


# Per-run cache for EDGAR + Finviz + Alpaca volume — same ticker can be
# scanned in Phase 2 (positions), Phase 3 (queued signals), and
# screening-request fulfillment. Cleared at the start of each run()
# invocation.
#
# Cache access is concurrent-safe: Phase 3 now dispatches fetches to a
# ThreadPoolExecutor (see SENTIMENT_FETCH_WORKERS below), so multiple
# threads may check-then-set these dicts simultaneously. Python's GIL
# makes `dict[key] = value` atomic, but the "check then set" in
# fetch_* helpers is not — lost updates would mean duplicate HTTP
# calls, not corruption. Adding the lock keeps the cache-hit rate
# deterministic.
_EDGAR_CACHE: dict = {}
_FINVIZ_CACHE: dict = {}
_VOLUME_CACHE: dict = {}   # Alpaca-bars-derived volume; primary source
_CACHE_LOCK = threading.Lock()

# Phase 3 parallel-fetch pool size. 5 workers = 5 concurrent HTTP
# requests. Enough to hide network latency on EDGAR/Finviz/Alpaca
# without overwhelming their rate limits (all three permit ~10
# req/sec). Can be tuned via env var for incident response.
SENTIMENT_FETCH_WORKERS = int(os.environ.get('SENTIMENT_FETCH_WORKERS', '5'))


def fetch_sec_insider_transactions(ticker, days_back=30):
    """
    Fetch recent insider transactions from SEC EDGAR.
    Returns dict with buys, sells, net_dollar.
    Free API — no key required.  Cached per-run on (ticker, days_back).
    Thread-safe for Phase 3's parallel fetcher; cache ops hold
    _CACHE_LOCK so concurrent calls for the same ticker coalesce
    into a single HTTP fetch.
    """
    cache_key = (ticker.upper(), days_back)
    with _CACHE_LOCK:
        cached = _EDGAR_CACHE.get(cache_key)
        if cached is not None:
            return cached

    url = "https://data.sec.gov/submissions"
    # SEC EDGAR full-text search for Form 4 (insider transactions)
    search_url = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=4"
    start = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    end   = datetime.now().strftime('%Y-%m-%d')

    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=4"
    headers = {
        "User-Agent": "Synthos/1.0 research@synthos.local",  # SEC requires User-Agent
        "Accept": "application/json",
    }
    r = fetch_with_retry(url, headers=headers)
    try:
        _master_db().log_api_call('sentiment_agent', '/LATEST/search-index', 'GET', 'sec_edgar', status_code=getattr(r, 'status_code', None))
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    if not r:
        result = {"buys": 0, "sells": 0, "net_dollar": "$0", "available": False}
        with _CACHE_LOCK:
            _EDGAR_CACHE[cache_key] = result
        return result

    try:
        data  = r.json()
        hits  = data.get("hits", {}).get("hits", [])
        buys  = 0
        sells = 0
        for hit in hits:
            src = hit.get("_source", {})
            # Form 4 transaction type: P=purchase, S=sale
            tx_type = src.get("period_of_report", "")
            # Simplified — count filings as proxy for transaction count
            # Full parsing requires downloading each filing XML
            sells += 1  # conservative: assume sells until proven otherwise

        log.info(f"SEC EDGAR {ticker}: found {len(hits)} Form 4 filings in last {days_back}d")
        result = {
            "buys": buys,
            "sells": sells,
            "net_dollar": f"-${sells * 100000:,}" if sells > buys else f"+${buys * 100000:,}",
            "available": True,
            "filing_count": len(hits),
        }
        with _CACHE_LOCK:
            _EDGAR_CACHE[cache_key] = result
        return result
    except Exception as e:
        log.warning(f"SEC EDGAR parse error ({ticker}): {e}")
        result = {"buys": 0, "sells": 0, "net_dollar": "$0", "available": False}
        with _CACHE_LOCK:
            _EDGAR_CACHE[cache_key] = result
        return result


def fetch_volume_profile(ticker, is_position=False):
    """Return relative-volume + seller-dominance for a ticker.

    Primary source: Alpaca bars API (official, API-keyed, stable shape).
    Fallback:       Finviz HTML scrape (fragile — Finviz can silently
                    change page structure and break our regex any day).

    Cached per-run on ticker. Thread-safe — called from the Phase-3
    ThreadPoolExecutor; cache ops hold _CACHE_LOCK.
    """
    cache_key = ticker.upper()
    with _CACHE_LOCK:
        cached = _VOLUME_CACHE.get(cache_key)
        if cached is not None:
            return cached

    volume_data = _fetch_volume_from_alpaca(ticker)
    if not volume_data.get("available"):
        # Alpaca returned nothing usable (keys missing, stale ticker,
        # 404, etc). Fall back to Finviz scrape — accept the fragility
        # rather than ship with no data.
        volume_data = _fetch_volume_from_finviz(ticker)

    with _CACHE_LOCK:
        # Mirror into the legacy _FINVIZ_CACHE name so any stray caller
        # that still reaches it gets a consistent answer.
        _VOLUME_CACHE[cache_key] = volume_data
        _FINVIZ_CACHE[cache_key] = volume_data
    return volume_data


def _fetch_volume_from_alpaca(ticker):
    """Build the volume_profile dict from 30-ish daily bars via Alpaca.

    Rel-volume = today's volume / mean(previous 30 sessions' volume).
    Seller dominance = heuristic bump based on today's close-to-open
    negative move combined with elevated volume — same shape detect_
    cascade expects, just sourced from an API instead of an HTML scrape.
    """
    out = {
        "today_vs_avg":     "+0%",
        "seller_dominance": "50%",
        "cascade_detected": False,
        "available":        False,
        "source":           "alpaca",
    }
    bars = fetch_alpaca_bars(ticker, days=35)
    if not bars or len(bars) < 5:
        return out

    try:
        # Alpaca returns oldest → newest. Last bar is "today" (or the
        # most recent session). Average over the prior 30 bars.
        today = bars[-1]
        prior = bars[-31:-1] if len(bars) >= 31 else bars[:-1]
        avg_v = sum(b.get('v') or 0 for b in prior) / max(1, len(prior))
        today_v = float(today.get('v') or 0)
        if avg_v <= 0:
            return out
        rel_vol = today_v / avg_v

        pct_change = f"+{int((rel_vol - 1) * 100)}%" if rel_vol > 1 \
                     else f"{int((rel_vol - 1) * 100)}%"
        out["today_vs_avg"] = pct_change
        out["available"]    = True

        # Seller-dominance heuristic — same shape the old Finviz path
        # produced. Uses intraday close-vs-open change as a proxy.
        o = float(today.get('o') or 0)
        c = float(today.get('c') or 0)
        if o > 0:
            price_change_pct = ((c - o) / o) * 100
            if price_change_pct < 0 and rel_vol > 1.5:
                dom = min(50 + abs(price_change_pct) * 5, 90)
                out["seller_dominance"] = f"{dom:.0f}%"
                if (rel_vol > CASCADE_VOLUME_THRESHOLD
                        and dom > CASCADE_SELLER_DOM_THRESHOLD * 100):
                    out["cascade_detected"] = True

        log.debug(f"Alpaca volume {ticker}: rel_vol={pct_change} sellers={out['seller_dominance']}")
        return out
    except Exception as e:
        log.warning(f"Alpaca volume parse error ({ticker}): {e}")
        return out


def _fetch_volume_from_finviz(ticker):
    """Legacy Finviz HTML scrape — used only when Alpaca returns
    nothing. Kept narrow because the regex is one page-change away
    from breaking silently."""
    out = {
        "today_vs_avg":     "+0%",
        "seller_dominance": "50%",
        "cascade_detected": False,
        "available":        False,
        "source":           "finviz",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Synthos/1.0)"}
    fv_url  = f"https://finviz.com/quote.ashx?t={ticker}"
    fv_r    = fetch_with_retry(fv_url, headers=headers)
    try:
        _master_db().log_api_call('sentiment_agent',
                                  f'/quote.ashx?t={ticker}', 'GET',
                                  'finviz',
                                  status_code=getattr(fv_r, 'status_code', None))
    except Exception as e:
        log.debug(f"api_call log failed for finviz {ticker}: {e}")

    if not fv_r:
        return out

    try:
        text = fv_r.text
        vol_match = re.search(r'Rel Volume.*?(\d+\.\d+)', text, re.DOTALL)
        if not vol_match:
            return out
        rel_vol = float(vol_match.group(1))
        pct_change = f"+{int((rel_vol - 1) * 100)}%" if rel_vol > 1 \
                     else f"{int((rel_vol - 1) * 100)}%"
        out["today_vs_avg"] = pct_change
        out["available"]    = True

        price_match = re.search(r'Change.*?(-?\d+\.\d+)%', text, re.DOTALL)
        if price_match:
            price_change = float(price_match.group(1))
            if price_change < 0 and rel_vol > 1.5:
                dom = min(50 + abs(price_change) * 5, 90)
                out["seller_dominance"] = f"{dom:.0f}%"
                if (rel_vol > CASCADE_VOLUME_THRESHOLD
                        and dom > CASCADE_SELLER_DOM_THRESHOLD * 100):
                    out["cascade_detected"] = True
        log.info(f"Finviz {ticker}: rel_vol={out['today_vs_avg']} sellers={out['seller_dominance']}")
    except Exception as e:
        log.warning(f"Finviz parse error ({ticker}): {e}")
    return out


def fetch_alpaca_bars(ticker, days=60):
    """
    Fetch daily OHLCV bars from Alpaca Data API (IEX feed — free tier).
    Returns list of bar dicts or [] on failure.
    Each bar: {t, o, h, l, c, v}
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("Alpaca API keys not configured — skipping bar fetch")
        return []

    end_dt   = datetime.now(ET)
    start_dt = end_dt - timedelta(days=days + 10)  # buffer for weekends/holidays
    url      = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars"
    params   = {
        "timeframe": "1Day",
        "start":     start_dt.strftime('%Y-%m-%d'),
        "end":       end_dt.strftime('%Y-%m-%d'),
        "feed":      "iex",
        "limit":     500,
    }
    headers  = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        r = fetch_with_retry(url, params=params, headers=headers)
        try:
            _master_db().log_api_call('sentiment_agent', f'/v2/stocks/{ticker}/bars', 'GET', 'alpaca_data', status_code=getattr(r, 'status_code', None))
        except Exception as _e:
            log.debug(f"suppressed exception: {_e}")
        if not r:
            return []
        data = r.json()
        bars = data.get("bars", [])
        log.info(f"Alpaca bars {ticker}: {len(bars)} bars fetched")
        return bars
    except Exception as e:
        log.warning(f"fetch_alpaca_bars error ({ticker}): {e}")
        return []


def fetch_vix():
    """
    Fetch VIX index from Yahoo Finance chart API.
    Returns (current_vix, vix_closes_list) or (None, []).
    """
    url     = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
    params  = {"interval": "1d", "range": "1mo"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Synthos/1.0)"}
    try:
        r = fetch_with_retry(url, params=params, headers=headers)
        try:
            _master_db().log_api_call('sentiment_agent', '/v8/finance/chart/%5EVIX', 'GET', 'yahoo', status_code=getattr(r, 'status_code', None))
        except Exception as _e:
            log.debug(f"suppressed exception: {_e}")
        if not r:
            return None, []
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None, []
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]
        if not closes:
            return None, []
        current = closes[-1]
        log.info(f"VIX fetched: current={current:.2f}, {len(closes)} days history")
        return current, closes
    except Exception as e:
        log.warning(f"fetch_vix error: {e}")
        return None, []


def fetch_etf_returns(tickers, bars_per_ticker=5):
    """
    Fetch 1-day returns for a list of ETF tickers using Alpaca's multi-symbol
    bars endpoint — one HTTP call for the whole list instead of one per ticker.
    Cuts sentiment-agent Alpaca bar calls from 19 down to 2.

    Returns dict: {ticker: 1d_return_float} or {} on failure.
    """
    if not tickers:
        return {}
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("Alpaca API keys not configured — skipping ETF returns")
        return {}

    end_dt   = datetime.now(ET)
    start_dt = end_dt - timedelta(days=bars_per_ticker + 10)  # weekend/holiday buffer
    url      = f"{ALPACA_DATA_URL}/v2/stocks/bars"
    # Chunk to 50 to respect Alpaca's multi-symbol limit
    CHUNK    = 50
    results  = {}
    headers  = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i+CHUNK]
        params = {
            "symbols":   ",".join(batch),
            "timeframe": "1Day",
            "start":     start_dt.strftime('%Y-%m-%d'),
            "end":       end_dt.strftime('%Y-%m-%d'),
            "feed":      "iex",
            "limit":     500,
        }
        try:
            r = fetch_with_retry(url, params=params, headers=headers)
            try:
                _master_db().log_api_call(
                    'sentiment_agent', '/v2/stocks/bars', 'GET', 'alpaca_data',
                    status_code=getattr(r, 'status_code', None)
                )
            except Exception as _e:
                log.debug(f"suppressed exception: {_e}")
            if not r:
                continue
            data = r.json()
            by_symbol = data.get("bars", {}) or {}
            for ticker in batch:
                bars = by_symbol.get(ticker) or []
                if len(bars) >= 2:
                    prev_close = bars[-2].get('c', 0)
                    last_close = bars[-1].get('c', 0)
                    if prev_close and prev_close != 0:
                        results[ticker] = (last_close - prev_close) / prev_close
        except Exception as e:
            log.warning(f"fetch_etf_returns batch error: {e}")

    log.info(f"fetch_etf_returns: {len(results)}/{len(tickers)} tickers resolved via multi-symbol endpoint")
    return results


def fetch_news_sentiment_from_db(db, hours_back=4):
    """
    Read recent news_feed entries from DB and compute aggregate sentiment.
    Returns (avg_score, entry_count, macro_count, micro_count) or (0.0, 0, 0, 0).
    """
    try:
        cutoff = datetime.now(ET) - timedelta(hours=hours_back)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        rows = db.query(
            "SELECT sentiment_score, tags FROM news_feed WHERE created_at >= ? ORDER BY created_at DESC LIMIT 200",
            (cutoff_str,)
        )
        if not rows:
            return 0.0, 0, 0, 0
        scores      = []
        macro_count = 0
        micro_count = 0
        for row in rows:
            score = row[0] if row[0] is not None else 0.0
            tags  = row[1] or ""
            scores.append(float(score))
            if "macro" in tags.lower() or "fed" in tags.lower() or "economy" in tags.lower():
                macro_count += 1
            else:
                micro_count += 1
        avg_score = sum(scores) / len(scores) if scores else 0.0
        log.info(f"News sentiment DB: {len(scores)} entries, avg={avg_score:.3f}, macro={macro_count}, micro={micro_count}")
        return avg_score, len(scores), macro_count, micro_count
    except Exception as e:
        log.warning(f"fetch_news_sentiment_from_db error: {e}")
        return 0.0, 0, 0, 0


def fetch_sector_returns(bars_per_sector=5):
    """
    Fetch 1-day returns for sector ETFs and key cross-asset instruments.
    Sectors: XLK XLF XLE XLC XLB XLY XLV XLI XLP XLU
    Also: RSP QQQ USMV TLT GLD UUP SPY HYG LQD
    Returns dict: {ticker: return_float}.
    """
    tickers = [
        "XLK", "XLF", "XLE", "XLC", "XLB", "XLY", "XLV", "XLI", "XLP", "XLU",
        "RSP", "QQQ", "USMV", "TLT", "GLD", "UUP", "SPY", "HYG", "LQD",
    ]
    return fetch_etf_returns(tickers, bars_per_ticker=bars_per_sector)


# ── HELPER MATH ───────────────────────────────────────────────────────────

def _compute_sma(closes, window):
    """Simple moving average of last `window` closes. Returns float or None."""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _compute_atr(bars, window=14):
    """Average True Range over last `window` bars. Returns float or 0.0."""
    if len(bars) < window + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i].get('h', 0)
        l = bars[i].get('l', 0)
        pc = bars[i-1].get('c', 0)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-window:]) / min(window, len(trs))


def _compute_roc(closes, lookback):
    """Rate of change over `lookback` periods. Returns float or 0.0."""
    if len(closes) < lookback + 1:
        return 0.0
    base = closes[-lookback - 1]
    if base == 0:
        return 0.0
    return (closes[-1] - base) / base


def _compute_realized_vol(closes, window=20):
    """
    Annualized realized volatility from daily log returns.
    Returns float or 0.0.
    """
    if len(closes) < window + 1:
        return 0.0
    try:
        log_rets = [math.log(closes[i] / closes[i-1]) for i in range(len(closes)-window, len(closes)) if closes[i-1] > 0]
        if len(log_rets) < 2:
            return 0.0
        mean = sum(log_rets) / len(log_rets)
        var  = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
        return math.sqrt(var * 252)
    except Exception:
        return 0.0


# ── SENTIMENT CONTROLS ────────────────────────────────────────────────────

class SentimentControls:
    """All configurable thresholds for the 27-gate market sentiment spine."""
    # Gate 1 — System
    MIN_REQUIRED_INPUTS      = int(os.environ.get('MIN_REQUIRED_INPUTS', '3'))
    MAX_SNAPSHOT_AGE_MIN     = int(os.environ.get('MAX_SNAPSHOT_AGE_MIN', '60'))

    # Gate 3 — Benchmark
    SPX_TICKER               = os.environ.get('SPX_TICKER', 'SPY')
    SPX_SMA_SHORT            = int(os.environ.get('SPX_SMA_SHORT', '20'))
    SPX_SMA_LONG             = int(os.environ.get('SPX_SMA_LONG', '50'))
    TREND_NEUTRAL_BAND       = float(os.environ.get('TREND_NEUTRAL_BAND', '0.002'))
    ROC_LOOKBACK             = int(os.environ.get('ROC_LOOKBACK', '5'))
    SPX_VOL_THRESHOLD        = float(os.environ.get('SPX_VOL_THRESHOLD', '0.015'))
    DRAWDOWN_THRESHOLD       = float(os.environ.get('DRAWDOWN_THRESHOLD', '0.05'))
    VIX_HIGH_THRESHOLD       = float(os.environ.get('VIX_HIGH_THRESHOLD', '20.0'))
    # TODO: DATA_DEPENDENCY — VIX real-time integration requires paid/premium feed

    # Gate 4 — Price Action
    PRICE_TREND_POS_THRESH   = float(os.environ.get('PRICE_TREND_POS_THRESH', '0.55'))
    PRICE_TREND_NEG_THRESH   = float(os.environ.get('PRICE_TREND_NEG_THRESH', '0.45'))
    ACCELERATION_THRESHOLD   = float(os.environ.get('ACCELERATION_THRESHOLD', '0.005'))
    ROC_SHORT                = int(os.environ.get('ROC_SHORT', '3'))
    ROC_LONG_BARS            = int(os.environ.get('ROC_LONG_BARS', '10'))
    DISPERSION_THRESHOLD     = float(os.environ.get('DISPERSION_THRESHOLD', '0.015'))

    # Gate 5 — Breadth
    BREADTH_SLOPE_THRESHOLD  = float(os.environ.get('BREADTH_SLOPE_THRESHOLD', '0.001'))
    ADVANCER_RATIO_THRESH    = float(os.environ.get('ADVANCER_RATIO_THRESH', '0.60'))
    DECLINER_RATIO_THRESH    = float(os.environ.get('DECLINER_RATIO_THRESH', '0.60'))
    BREADTH_RETURN_THRESH    = float(os.environ.get('BREADTH_RETURN_THRESH', '0.005'))
    # TODO: DATA_DEPENDENCY — advance/decline line requires exchange breadth data feed

    # Gate 6 — Volume
    UP_VOLUME_THRESHOLD      = float(os.environ.get('UP_VOLUME_THRESHOLD', '0.60'))
    DOWN_VOLUME_THRESHOLD    = float(os.environ.get('DOWN_VOLUME_THRESHOLD', '0.60'))
    VOLUME_GROWTH_THRESH     = float(os.environ.get('VOLUME_GROWTH_THRESH', '0.20'))
    VOLUME_ANOMALY_THRESH    = float(os.environ.get('VOLUME_ANOMALY_THRESH', '2.0'))

    # Gate 7 — Volatility
    VIX_ROC_THRESHOLD        = float(os.environ.get('VIX_ROC_THRESHOLD', '0.10'))
    REALIZED_VOL_GAP         = float(os.environ.get('REALIZED_VOL_GAP', '0.005'))
    VVIX_THRESHOLD           = float(os.environ.get('VVIX_THRESHOLD', '95.0'))
    # TODO: DATA_DEPENDENCY — VVIX, vol term structure require options feed

    # Gate 8 — Options
    PUT_CALL_HIGH_THRESH     = float(os.environ.get('PUT_CALL_HIGH_THRESH', '1.10'))
    PUT_CALL_LOW_THRESH      = float(os.environ.get('PUT_CALL_LOW_THRESH', '0.70'))
    SKEW_THRESHOLD           = float(os.environ.get('SKEW_THRESHOLD', '10.0'))
    CALL_SPEC_THRESHOLD      = float(os.environ.get('CALL_SPEC_THRESHOLD', '0.65'))
    # TODO: DATA_DEPENDENCY — dealer gamma, skew, implied correlation require paid options data

    # Gate 9 — Safe-Haven
    TREASURY_THRESHOLD       = float(os.environ.get('TREASURY_THRESHOLD', '0.003'))
    GOLD_THRESHOLD           = float(os.environ.get('GOLD_THRESHOLD', '0.005'))
    DXY_THRESHOLD            = float(os.environ.get('DXY_THRESHOLD', '0.003'))
    SPX_WEAK_THRESHOLD       = float(os.environ.get('SPX_WEAK_THRESHOLD', '0.005'))
    SPX_POSITIVE_THRESHOLD   = float(os.environ.get('SPX_POSITIVE_THRESHOLD', '0.005'))
    CYCLICAL_THRESHOLD       = float(os.environ.get('CYCLICAL_THRESHOLD', '0.005'))
    DEFENSIVE_THRESHOLD      = float(os.environ.get('DEFENSIVE_THRESHOLD', '0.005'))
    BETA_THRESHOLD           = float(os.environ.get('BETA_THRESHOLD', '0.005'))

    # Gate 10 — Credit
    SPREAD_WIDENING_THRESH   = float(os.environ.get('SPREAD_WIDENING_THRESH', '0.003'))
    SPREAD_TIGHTENING_THRESH = float(os.environ.get('SPREAD_TIGHTENING_THRESH', '0.003'))
    HY_IG_THRESHOLD          = float(os.environ.get('HY_IG_THRESHOLD', '0.005'))
    # TODO: DATA_DEPENDENCY — CDS/CDX index, funding spreads require credit data feed

    # Gate 11 — Sector Rotation
    LEADERSHIP_THRESHOLD     = float(os.environ.get('LEADERSHIP_THRESHOLD', '0.005'))
    CYCLICAL_LEAD_THRESH     = float(os.environ.get('CYCLICAL_LEAD_THRESH', '0.005'))
    DEFENSIVE_LEAD_THRESH    = float(os.environ.get('DEFENSIVE_LEAD_THRESH', '0.005'))
    FINANCIALS_CONFIRM_THRESH = float(os.environ.get('FINANCIALS_CONFIRM_THRESH', '0.003'))
    DEFENSIVE_CONFIRM_THRESH = float(os.environ.get('DEFENSIVE_CONFIRM_THRESH', '0.003'))

    # Gate 12 — Macro
    INFLATION_FEAR_THRESH    = float(os.environ.get('INFLATION_FEAR_THRESH', '0.5'))
    GROWTH_FEAR_THRESH       = float(os.environ.get('GROWTH_FEAR_THRESH', '-0.5'))
    EASING_THRESHOLD         = float(os.environ.get('EASING_THRESHOLD', '0.05'))
    TIGHTENING_THRESHOLD     = float(os.environ.get('TIGHTENING_THRESHOLD', '0.05'))
    # TODO: DATA_DEPENDENCY — economic surprise indices, Fed funds futures require FRED/Bloomberg

    # Gate 13 — News
    NEWS_POSITIVE_THRESH     = float(os.environ.get('NEWS_POSITIVE_THRESH', '0.10'))
    NEWS_NEGATIVE_THRESH     = float(os.environ.get('NEWS_NEGATIVE_THRESH', '-0.10'))
    NEWS_DOMINANCE_THRESH    = float(os.environ.get('NEWS_DOMINANCE_THRESH', '0.60'))
    MIN_CONFIRMATIONS        = int(os.environ.get('MIN_CONFIRMATIONS', '2'))

    # Gate 14 — Social
    SOCIAL_POSITIVE_THRESH   = float(os.environ.get('SOCIAL_POSITIVE_THRESH', '0.10'))
    SOCIAL_NEGATIVE_THRESH   = float(os.environ.get('SOCIAL_NEGATIVE_THRESH', '-0.10'))
    SOCIAL_ATTENTION_THRESH  = float(os.environ.get('SOCIAL_ATTENTION_THRESH', '0.90'))
    MANIPULATION_THRESHOLD   = float(os.environ.get('MANIPULATION_THRESHOLD', '0.70'))
    # TODO: DATA_DEPENDENCY — social sentiment requires paid feed (StockTwits, Twitter/X API)

    # Gate 18 — Composite Score weights (should sum to ~1.0)
    WEIGHT_PRICE             = float(os.environ.get('WEIGHT_PRICE',       '0.20'))
    WEIGHT_BREADTH           = float(os.environ.get('WEIGHT_BREADTH',     '0.15'))
    WEIGHT_VOLUME            = float(os.environ.get('WEIGHT_VOLUME',      '0.10'))
    WEIGHT_VOLATILITY        = float(os.environ.get('WEIGHT_VOLATILITY',  '0.15'))
    WEIGHT_OPTIONS           = float(os.environ.get('WEIGHT_OPTIONS',     '0.10'))
    WEIGHT_CROSS_ASSET       = float(os.environ.get('WEIGHT_CROSS_ASSET', '0.10'))
    WEIGHT_CREDIT            = float(os.environ.get('WEIGHT_CREDIT',      '0.10'))
    WEIGHT_MACRO             = float(os.environ.get('WEIGHT_MACRO',       '0.05'))
    WEIGHT_NEWS              = float(os.environ.get('WEIGHT_NEWS',        '0.03'))
    WEIGHT_SOCIAL            = float(os.environ.get('WEIGHT_SOCIAL',      '0.02'))

    # Composite sentiment thresholds
    BULLISH_THRESHOLD        = float(os.environ.get('BULLISH_THRESHOLD',  '0.20'))
    BEARISH_THRESHOLD        = float(os.environ.get('BEARISH_THRESHOLD',  '-0.20'))
    EUPHORIC_THRESHOLD       = float(os.environ.get('EUPHORIC_THRESHOLD', '0.60'))
    PANIC_THRESHOLD          = float(os.environ.get('PANIC_THRESHOLD',    '-0.60'))

    # Gate 19 — Confidence
    MIN_CONFIDENT_COMPONENTS  = int(os.environ.get('MIN_CONFIDENT_COMPONENTS', '4'))
    AGREEMENT_THRESHOLD       = float(os.environ.get('AGREEMENT_THRESHOLD',    '0.20'))
    DISAGREEMENT_THRESHOLD    = float(os.environ.get('DISAGREEMENT_THRESHOLD', '0.35'))
    DATA_QUALITY_THRESHOLD    = float(os.environ.get('DATA_QUALITY_THRESHOLD', '0.60'))
    HIGH_CONF_THRESHOLD       = float(os.environ.get('HIGH_CONF_THRESHOLD',    '0.70'))
    LOW_CONF_THRESHOLD        = float(os.environ.get('LOW_CONF_THRESHOLD',     '0.40'))
    DIVERGENCE_CONF_THRESHOLD = float(os.environ.get('DIVERGENCE_CONF_THRESHOLD', '0.50'))

    # Gate 22 — Action
    WARNING_COUNT_THRESHOLD   = int(os.environ.get('WARNING_COUNT_THRESHOLD', '2'))

    # Gate 23 — Risk Discounts
    SOCIAL_RISK_DISCOUNT      = float(os.environ.get('SOCIAL_RISK_DISCOUNT',      '0.90'))
    LOW_DATA_DISCOUNT         = float(os.environ.get('LOW_DATA_DISCOUNT',          '0.80'))
    WARNING_DISCOUNT          = float(os.environ.get('WARNING_DISCOUNT',           '0.85'))
    DRAWDOWN_DISCOUNT         = float(os.environ.get('DRAWDOWN_DISCOUNT',          '0.80'))
    VOL_INSTABILITY_DISCOUNT  = float(os.environ.get('VOL_INSTABILITY_DISCOUNT',   '0.90'))

    # Gate 24 — Temporal Persistence
    PERSISTENCE_THRESHOLD     = int(os.environ.get('PERSISTENCE_THRESHOLD', '3'))
    FLIP_THRESHOLD            = int(os.environ.get('FLIP_THRESHOLD',        '3'))
    IMPROVEMENT_THRESHOLD     = float(os.environ.get('IMPROVEMENT_THRESHOLD',    '0.05'))
    DETERIORATION_THRESHOLD   = float(os.environ.get('DETERIORATION_THRESHOLD',  '0.05'))

    # Gate 27 — Final Signal thresholds
    STRONG_BULL_THRESHOLD     = float(os.environ.get('STRONG_BULL_THRESHOLD', '0.50'))
    BULL_THRESHOLD_FINAL      = float(os.environ.get('BULL_THRESHOLD_FINAL',  '0.20'))
    BEAR_THRESHOLD_FINAL      = float(os.environ.get('BEAR_THRESHOLD_FINAL',  '-0.20'))
    STRONG_BEAR_THRESHOLD     = float(os.environ.get('STRONG_BEAR_THRESHOLD', '-0.50'))
    NEUTRAL_LOW               = float(os.environ.get('NEUTRAL_LOW',  '-0.10'))
    NEUTRAL_HIGH              = float(os.environ.get('NEUTRAL_HIGH',  '0.10'))


# ── SENTIMENT STATE ───────────────────────────────────────────────────────

@dataclass
class SentimentState:
    """State machine tracking all 27 gate outputs for each market sentiment snapshot."""

    # Gate 1 — System
    system_status: str = "unknown"

    # Gate 2 — Input Universe
    input_price:      str = "inactive"
    input_volume:     str = "inactive"
    input_breadth:    str = "inactive"
    input_volatility: str = "inactive"
    input_options:    str = "inactive"
    input_safe_haven: str = "inactive"
    input_credit:     str = "inactive"
    input_macro:      str = "inactive"
    input_news:       str = "inactive"
    input_social:     str = "inactive"
    active_input_count: int = 0

    # Gate 3 — Benchmark
    benchmark_state:          str = "neutral"
    benchmark_momentum_state: str = "flat"
    benchmark_risk_state:     str = "normal"
    benchmark_vol_state:      str = "normal"

    # Gate 4 — Price Action
    price_sentiment_state:  str   = "neutral"
    price_structure_state:  str   = "neutral"
    price_momentum_state:   str   = "neutral"
    price_dispersion_state: str   = "low"
    price_score:            float = 0.0

    # Gate 5 — Breadth
    breadth_state:          str   = "neutral"
    breadth_momentum_state: str   = "neutral"
    breadth_quality_state:  str   = "neutral"
    breadth_score:          float = 0.0

    # Gate 6 — Volume
    volume_sentiment_state:    str   = "neutral"
    volume_pattern_state:      str   = "neutral"
    volume_confirmation_state: str   = "neutral"
    volume_state:              str   = "normal"
    volume_score:              float = 0.0

    # Gate 7 — Volatility
    vol_sentiment_state:   str   = "neutral"
    vol_structure_state:   str   = "calm"
    realized_vol_state:    str   = "neutral"
    vol_instability_state: str   = "normal"
    vol_score:             float = 0.0

    # Gate 8 — Options
    options_sentiment_state:   str   = "neutral"
    options_tail_risk_state:   str   = "normal"
    options_speculation_state: str   = "neutral"
    options_flow_state:        str   = "neutral"
    options_corr_state:        str   = "normal"
    options_score:             float = 0.0

    # Gate 9 — Safe-Haven / Cross-Asset
    cross_asset_state:   str   = "neutral"
    rotation_state:      str   = "neutral"
    risk_appetite_state: str   = "neutral"
    cross_asset_score:   float = 0.0

    # Gate 10 — Credit
    credit_state:        str   = "neutral"
    credit_risk_state:   str   = "neutral"
    credit_stress_state: str   = "normal"
    liquidity_state:     str   = "normal"
    credit_score:        float = 0.0

    # Gate 11 — Sector Rotation
    sector_leadership_state:   str   = "neutral"
    sector_rotation_state:     str   = "neutral"
    sector_confirmation_state: str   = "neutral"
    sector_score:              float = 0.0

    # Gate 12 — Macro
    macro_state:           str   = "neutral"
    macro_policy_state:    str   = "neutral"
    macro_sentiment_state: str   = "neutral"
    macro_score:           float = 0.0

    # Gate 13 — News
    news_sentiment_state:  str   = "neutral"
    news_driver_state:     str   = "neutral"
    news_conviction_state: str   = "neutral"
    news_score:            float = 0.0

    # Gate 14 — Social
    social_sentiment_state: str   = "neutral"
    social_attention_state: str   = "normal"
    social_news_state:      str   = "aligned"
    social_price_state:     str   = "aligned"
    social_quality_state:   str   = "normal"
    social_score:           float = 0.0

    # Gate 15 — Breadth-Price Divergence
    divergence_state: str = "none"

    # Gate 16 — Component Scores
    component_scores: dict = field(default_factory=dict)

    # Gate 17 — Effective Weights (after adjustments)
    effective_weights: dict = field(default_factory=dict)

    # Gate 18 — Composite Sentiment
    raw_sentiment_score:    float = 0.0
    market_sentiment_state: str   = "neutral"

    # Gate 19 — Confidence
    confidence_input_state: str   = "weak"
    confidence_state:       str   = "neutral"
    data_confidence_state:  str   = "normal"
    sentiment_confidence:   float = 0.0

    # Gate 20 — Regime
    regime_state: str = "indecisive"

    # Gate 21 — Divergence Warnings
    warning_state:        str = "none"
    active_warning_count: int = 0

    # Gate 22 — Action Classification
    classification: str = "no_clear_signal"

    # Gate 23 — Risk Discounts
    discounted_sentiment_score: float = 0.0
    discounted_confidence:      float = 0.0
    discounts_applied:          list  = field(default_factory=list)

    # Gate 24 — Temporal Persistence
    persistence_state:     str = "unknown"
    sentiment_trend_state: str = "neutral"

    # Gate 25 — Evaluation Loop
    snapshot_retained: bool = False
    evaluation_note:   str  = ""

    # Gate 26 — Output Controls
    output_action:   str = "emit_neutral_state"
    output_modifier: str = ""
    output_priority: str = "article_first"

    # Gate 27 — Final Signal
    final_sentiment_signal: float = 0.0
    final_market_state:     str   = "neutral"

    # Internal scratch — not a gate output; stores SPY 1d return for gate15
    _spy_1d_return: float = 0.0


# ── SENTIMENT DECISION LOG ────────────────────────────────────────────────

from datetime import datetime as _dt
import json as _json

class SentimentDecisionLog:
    """
    Records gate-by-gate decisions for the full market sentiment snapshot audit trail.
    Human-readable + machine-readable output for logging and compliance.

    # FLAG — LOG WRITE LOCATION: Results written to system_log table via
    # db.log_event("MARKET_SENTIMENT_CLASSIFIED", ...).
    """

    def __init__(self):
        self.gates      = []
        self.decision   = None
        self.confidence = None
        self.reason     = None
        self.ts         = datetime.now(ET).isoformat()

    def gate(self, num, name, inputs, result, reason):
        """Record one gate decision."""
        self.gates.append({
            "gate":   num,
            "name":   name,
            "inputs": inputs,
            "result": result,
            "reason": reason,
        })

    def decide(self, classification, confidence, reason):
        self.decision   = classification
        self.confidence = confidence
        self.reason     = reason

    def to_human(self):
        lines = [f"MARKET_SENTIMENT_SNAPSHOT @ {self.ts}"]
        for g in self.gates:
            lines.append(f"  Gate {g['gate']:>2} [{g['name']:<36}]: {g['result']}  — {g['reason']}")
        lines.append(f"  DECISION: {self.decision}  confidence={self.confidence:.2f}  reason={self.reason}")
        return "\n".join(lines)

    def to_machine(self):
        return {
            "ts":         self.ts,
            "gates":      self.gates,
            "decision":   self.decision,
            "confidence": self.confidence,
            "reason":     self.reason,
        }

    def commit(self, db):
        log.info(f"[SENTIMENT_DECISION]\n{self.to_human()}")
        try:
            db.log_event(
                "MARKET_SENTIMENT_CLASSIFIED",
                agent="The Pulse",
                details=_json.dumps(self.to_machine()),
            )
        except Exception as e:
            log.debug(f"SentimentDecisionLog.commit failed (non-fatal): {e}")


# ── SCAN DECISION LOG ────────────────────────────────────────────────────

class ScanDecisionLog:
    """
    Records the sentiment scan result for each scanned position.
    Human-readable + machine-readable output for audit.

    # FLAG — LOG WRITE LOCATION: Results written to scan_log table via db.log_scan().
    # The human-readable summary is included in the event_summary field.
    # A dedicated sentiment_decisions table is recommended for volume management
    # and regulatory export. Tracked as future work item.
    """

    def __init__(self, ticker):
        self.ticker   = ticker
        self.signals  = []
        self.tier     = None
        self.label    = None
        self.cascade  = None
        self.analysis = None
        self.ts       = datetime.now(ET).isoformat()

    def signal(self, name, value, status, note=""):
        self.signals.append({"name": name, "value": str(value), "status": status, "note": note})
        return self

    def conclude(self, tier, label, cascade, analysis):
        self.tier     = tier
        self.label    = label
        self.cascade  = cascade
        self.analysis = analysis
        return self

    def to_human(self):
        lines = [
            f"SENTIMENT SCAN — {self.ticker} @ {self.ts}",
        ]
        for s in self.signals:
            note = f"  [{s['note']}]" if s["note"] else ""
            lines.append(f"  {s['name']:<24}: {s['value']}  [{s['status']}]{note}")
        lines.append(f"  {'TIER':<24}: {self.tier} ({self.label}) | cascade={self.cascade}")
        lines.append(f"  {'ANALYSIS':<24}: {self.analysis}")
        return "\n".join(lines)

    def to_machine(self):
        return {
            "ts":       self.ts,
            "ticker":   self.ticker,
            "signals":  self.signals,
            "tier":     self.tier,
            "label":    self.label,
            "cascade":  self.cascade,
            "analysis": self.analysis,
        }

    def commit(self, db):
        log.info(f"[SCAN_DECISION]\n{self.to_human()}")
        try:
            db.log_event(
                "SCAN_CLASSIFIED",
                agent="The Pulse",
                details=_json.dumps(self.to_machine()),
            )
        except Exception as e:
            log.debug(f"ScanDecisionLog.commit failed (non-fatal): {e}")


# ── CASCADE DETECTION ─────────────────────────────────────────────────────

def detect_cascade(put_call, put_call_avg, insider_data, volume_data):
    """
    Determine sentiment tier based on signal alignment.

    Tier 1 CRITICAL: cascade — multiple signals aligning
    Tier 2 ELEVATED: notable activity, one or two signals elevated
    Tier 3 NEUTRAL:  normal market behavior
    Tier 4 QUIET:    below average activity, healthy

    Returns: (tier, tier_label, cascade_detected, summary)
    """
    signals_critical = 0
    signals_elevated = 0
    notes = []

    # Put/call ratio check
    if put_call and put_call_avg:
        ratio_change = put_call / put_call_avg
        if ratio_change > CASCADE_PUT_CALL_THRESHOLD:
            signals_critical += 1
            notes.append(f"Put/call {put_call:.2f} vs avg {put_call_avg:.2f} (+{(ratio_change-1)*100:.0f}%)")
        elif ratio_change > 1.20:
            signals_elevated += 1
            notes.append(f"Put/call elevated: {put_call:.2f} vs avg {put_call_avg:.2f}")

    # Insider transaction check
    sells = insider_data.get("sells", 0)
    buys  = insider_data.get("buys", 0)
    if sells >= CASCADE_INSIDER_SELLS_MIN and buys == 0:
        signals_critical += 1
        notes.append(f"Insider net: {buys}B/{sells}S = {insider_data.get('net_dollar', '?')}")
    elif sells > buys + 2:
        signals_elevated += 1
        notes.append(f"Insider net selling: {buys}B/{sells}S")

    # Volume profile check
    cascade_vol = volume_data.get("cascade_detected", False)
    if cascade_vol:
        signals_critical += 1
        notes.append(f"Volume cascade: {volume_data.get('today_vs_avg')} avg, {volume_data.get('seller_dominance')} sellers")
    elif "+" in str(volume_data.get("today_vs_avg", "")) and int(str(volume_data.get("today_vs_avg", "+0%")).replace("+","").replace("%","") or 0) > 80:
        signals_elevated += 1
        notes.append(f"Elevated volume: {volume_data.get('today_vs_avg')} avg")

    # Determine tier
    cascade_detected = signals_critical >= 2
    if cascade_detected or signals_critical >= 3:
        tier, label = 1, "CRITICAL"
    elif signals_critical == 1 or signals_elevated >= 2:
        tier, label = 2, "ELEVATED"
    elif signals_elevated == 1:
        tier, label = 3, "NEUTRAL"
    else:
        tier, label = 4, "QUIET"

    summary = f"Tier {tier} ({label}): " + ("; ".join(notes) if notes else "All signals normal")
    return tier, label, cascade_detected, summary


# ── SCAN ANALYSIS ─────────────────────────────────────────────────────────

def format_scan_analysis(pos, put_call, put_call_avg, insider_data,
                         volume_data, tier, tier_label, cascade_detected, db):
    """
    Generate a structured sentiment analysis summary from raw signal data.
    Replaces: analyze_position_with_claude()

    All logic is deterministic. No external calls.
    Returns: analysis string for inclusion in scan log event_summary.
    """
    sdl = ScanDecisionLog(ticker=pos.get("ticker", "UNKNOWN"))

    # ── Record each signal and its status ─────────────────────────────────
    if put_call is not None and put_call_avg is not None:
        ratio_pct = (put_call / put_call_avg - 1) * 100
        status = "ELEVATED" if put_call / put_call_avg > CASCADE_PUT_CALL_THRESHOLD else (
                 "ABOVE_AVG" if ratio_pct > 0 else "NORMAL")
        sdl.signal("put_call_ratio",
                   f"{put_call:.2f} vs {put_call_avg:.2f} avg ({ratio_pct:+.0f}%)", status)
    else:
        sdl.signal("put_call_ratio", "unavailable", "UNKNOWN")

    insider_sells = insider_data.get("sells", 0)
    insider_buys  = insider_data.get("buys", 0)
    insider_net   = insider_data.get("net_dollar", "$0")
    if insider_sells >= CASCADE_INSIDER_SELLS_MIN and insider_buys == 0:
        ins_status = "CRITICAL"
    elif insider_sells > insider_buys + 2:
        ins_status = "ELEVATED"
    elif insider_buys > insider_sells:
        ins_status = "POSITIVE"
    else:
        ins_status = "NORMAL"
    sdl.signal("insider_transactions",
               f"{insider_buys}B / {insider_sells}S = {insider_net}", ins_status)

    vol_cascade = volume_data.get("cascade_detected", False)
    sdl.signal("volume_cascade",
               f"{volume_data.get('today_vs_avg','?')} vs avg, {volume_data.get('seller_dominance','?')} sellers",
               "CRITICAL" if vol_cascade else "NORMAL")

    # ── Actor assessment (single large sell vs. multiple sellers) ──────────
    # KEY RULE: multiple independent signals aligning = cascade.
    # Single elevated signal = likely one actor, not cascade.
    cascade_str = "multiple signals aligning — broad seller pressure" if cascade_detected else (
                  "single signal elevated — isolated actor likely")

    # ── Stop level context ─────────────────────────────────────────────────
    current_price = pos.get("current_price", pos.get("entry_price", 0.0))
    trail_dist    = pos.get("trail_stop_amt", 0.0)
    stop_level    = current_price - trail_dist if trail_dist else 0.0

    # ── Recommendation by tier ─────────────────────────────────────────────
    if tier == 1:
        aligned = [s["name"] for s in sdl.signals if s["status"] in ("CRITICAL", "ELEVATED")]
        rec = (
            f"CASCADE DETECTED. Trail stop at ~${stop_level:.2f}. "
            f"Signals aligning: {', '.join(aligned) or 'multiple'}. "
            "Tighten trailing stop or prepare protective exit."
        )
    elif tier == 2:
        top = next((s for s in sdl.signals if s["status"] in ("ELEVATED",)), None)
        src = top["name"] if top else "unknown signal"
        rec = (
            f"Single signal elevated ({src}). "
            f"Trail stop at ~${stop_level:.2f}. "
            "Hold stops — monitor for additional confirmation."
        )
    else:
        rec = f"No action required — tier {tier} ({tier_label})."

    analysis = f"cascade_assessment={cascade_str} | {rec}"
    sdl.conclude(tier, tier_label, cascade_detected, analysis)
    sdl.commit(db)

    return analysis


# ── COMPONENT SCORING HELPERS ─────────────────────────────────────────────

def _score_price(state):
    scores = {
        "price_sentiment": {"positive": 0.5, "negative": -0.5, "neutral": 0.0},
        "price_structure": {"strong": 0.3, "weak": -0.3, "neutral": 0.0},
        "price_momentum":  {"improving": 0.2, "deteriorating": -0.2, "neutral": 0.0},
    }
    total = (scores["price_sentiment"].get(state.price_sentiment_state, 0.0) +
             scores["price_structure"].get(state.price_structure_state, 0.0) +
             scores["price_momentum"].get(state.price_momentum_state, 0.0))
    return max(-1.0, min(1.0, total))


def _score_breadth(state):
    breadth_map  = {"supportive": 0.4, "broad_participation": 0.5, "neutral": 0.0,
                    "weakening": -0.4, "narrow_negative": -0.5}
    momentum_map = {"positive": 0.3, "neutral": 0.0, "negative": -0.3}
    quality_map  = {"healthy": 0.2, "neutral": 0.0, "narrow_leadership": -0.3}
    return max(-1.0, min(1.0,
        breadth_map.get(state.breadth_state, 0.0) +
        momentum_map.get(state.breadth_momentum_state, 0.0) +
        quality_map.get(state.breadth_quality_state, 0.0)))


def _score_volume(state):
    sent_map    = {"positive": 0.4, "neutral": 0.0, "negative": -0.4}
    pattern_map = {"accumulation": 0.4, "neutral": 0.0, "distribution": -0.4}
    confirm_map = {"bullish_confirm": 0.2, "neutral": 0.0, "bearish_confirm": -0.2}
    return max(-1.0, min(1.0,
        sent_map.get(state.volume_sentiment_state, 0.0) +
        pattern_map.get(state.volume_pattern_state, 0.0) +
        confirm_map.get(state.volume_confirmation_state, 0.0)))


def _score_volatility(state):
    # NOTE: for vol, risk_on = positive for equities, risk_off = negative
    sent_map   = {"risk_on": 0.5, "neutral": 0.0, "risk_off": -0.5}
    struct_map = {"calm": 0.3, "stress": -0.3}
    real_map   = {"easing": 0.2, "neutral": 0.0, "worsening": -0.2}
    return max(-1.0, min(1.0,
        sent_map.get(state.vol_sentiment_state, 0.0) +
        struct_map.get(state.vol_structure_state, 0.0) +
        real_map.get(state.realized_vol_state, 0.0)))


def _score_options(state):
    # fearful = over-hedged (near-term bullish contrarian); complacent = risky
    sent_map = {"complacent": -0.4, "neutral": 0.0, "fearful": 0.3}
    tail_map = {"high_demand_for_protection": 0.2, "normal": 0.0}
    flow_map = {"stabilizing": 0.3, "neutral": 0.0, "destabilizing": -0.4}
    return max(-1.0, min(1.0,
        sent_map.get(state.options_sentiment_state, 0.0) +
        tail_map.get(state.options_tail_risk_state, 0.0) +
        flow_map.get(state.options_flow_state, 0.0)))


def _score_cross_asset(state):
    cross_map = {"risk_off": -0.5, "defensive_flow": -0.4, "stress_dollar_bid": -0.5, "neutral": 0.0}
    rot_map   = {"risk_on": 0.4, "neutral": 0.0, "risk_off": -0.4}
    app_map   = {"elevated": 0.3, "neutral": 0.0, "defensive": -0.3}
    return max(-1.0, min(1.0,
        cross_map.get(state.cross_asset_state, 0.0) +
        rot_map.get(state.rotation_state, 0.0) +
        app_map.get(state.risk_appetite_state, 0.0)))


def _score_credit(state):
    cred_map   = {"improving": 0.5, "neutral": 0.0, "deteriorating": -0.5}
    risk_map   = {"healthy_risk_appetite": 0.3, "neutral": 0.0, "weak_risk_appetite": -0.3}
    stress_map = {"normal": 0.2, "elevated": -0.3}
    liq_map    = {"normal": 0.0, "stress": -0.3}
    return max(-1.0, min(1.0,
        cred_map.get(state.credit_state, 0.0) +
        risk_map.get(state.credit_risk_state, 0.0) +
        stress_map.get(state.credit_stress_state, 0.0) +
        liq_map.get(state.liquidity_state, 0.0)))


def _score_macro(state):
    macro_map  = {"supportive": 0.5, "neutral": 0.0, "unsupportive": -0.5,
                  "inflation_risk": -0.3, "growth_risk": -0.5}
    policy_map = {"easing_expected": 0.3, "neutral": 0.0, "tightening_expected": -0.3}
    return max(-1.0, min(1.0,
        macro_map.get(state.macro_state, 0.0) +
        policy_map.get(state.macro_policy_state, 0.0)))


def _score_news(state):
    news_map = {"positive": 0.5, "neutral": 0.0, "negative": -0.5}
    conv_map = {"strong_positive": 0.3, "neutral": 0.0, "strong_negative": -0.3}
    return max(-1.0, min(1.0,
        news_map.get(state.news_sentiment_state, 0.0) +
        conv_map.get(state.news_conviction_state, 0.0)))


def _score_social(state):
    # Low weight and low trust by default on free tier
    if state.social_quality_state == "low_trust":
        return 0.0
    sent_map = {"positive": 0.3, "neutral": 0.0, "negative": -0.3}
    return max(-1.0, min(1.0, sent_map.get(state.social_sentiment_state, 0.0)))


# ── GATE FUNCTIONS ────────────────────────────────────────────────────────

def gate1_system(ctrl, state, sdl, snapshot_ts, snapshot_hash, processed_hashes, data_available):
    """
    Gate 1 — System.
    Checks all system pre-conditions before any data processing begins.
    Returns True to proceed, False to halt.
    """
    now = datetime.now(ET)

    # IF market data unavailable → halt
    if not data_available:
        state.system_status = "halt_sentiment_calc"
        sdl.gate(1, "system", {"data_available": data_available},
                 "halt", "market data unavailable")
        return False

    # IF timestamp null → reject
    if snapshot_ts is None:
        state.system_status = "reject_snapshot"
        sdl.gate(1, "system", {"snapshot_ts": None},
                 "reject_snapshot", "timestamp is null")
        return False

    # IF snapshot stale
    age_min = (now - snapshot_ts).total_seconds() / 60 if hasattr(snapshot_ts, 'tzinfo') else 0
    if age_min > ctrl.MAX_SNAPSHOT_AGE_MIN:
        state.system_status = "stale_snapshot"
        sdl.gate(1, "system", {"age_min": age_min, "max": ctrl.MAX_SNAPSHOT_AGE_MIN},
                 "stale_snapshot", f"snapshot age {age_min:.1f}m > {ctrl.MAX_SNAPSHOT_AGE_MIN}m")
        return False

    # IF duplicate
    if snapshot_hash in processed_hashes:
        state.system_status = "suppress_duplicate"
        sdl.gate(1, "system", {"snapshot_hash": snapshot_hash},
                 "suppress_duplicate", "hash already processed this session")
        return False

    # All checks pass
    state.system_status = "ok"
    sdl.gate(1, "system",
             {"data_available": data_available, "age_min": age_min, "hash": snapshot_hash},
             "ok", "all system checks passed")
    return True


def gate2_input_universe(ctrl, state, sdl, data):
    """
    Gate 2 — Input Universe.
    Marks each input channel active or inactive based on data availability.
    """
    price_bars    = data.get("price_bars", [])
    volume_bars   = data.get("volume_bars", [])
    breadth_data  = data.get("breadth_data", {})
    vol_data      = data.get("vol_data", {})
    options_data  = data.get("options_data", {})
    safe_haven    = data.get("safe_haven_data", {})
    credit_data   = data.get("credit_data", {})
    macro_data    = data.get("macro_data", {})
    news_data     = data.get("news_data", {})
    social_data   = data.get("social_data", {})

    state.input_price      = "active" if price_bars else "inactive"
    state.input_volume     = "active" if volume_bars else "inactive"
    # TODO: DATA_DEPENDENCY — advance/decline line requires exchange breadth data feed
    state.input_breadth    = "active" if breadth_data else "inactive"
    state.input_volatility = "active" if vol_data else "inactive"
    state.input_options    = "active" if options_data.get("put_call_ratio") else "inactive"
    state.input_safe_haven = "active" if safe_haven else "inactive"
    state.input_credit     = "active" if credit_data else "inactive"
    # TODO: DATA_DEPENDENCY — macro data requires FRED/Bloomberg
    state.input_macro      = "active" if macro_data else "inactive"
    state.input_news       = "active" if news_data.get("count", 0) > 0 else "inactive"
    # TODO: DATA_DEPENDENCY — social sentiment requires paid feed (StockTwits, Twitter/X API)
    state.input_social     = "active" if social_data else "inactive"

    state.active_input_count = sum(1 for v in [
        state.input_price, state.input_volume, state.input_breadth,
        state.input_volatility, state.input_options, state.input_safe_haven,
        state.input_credit, state.input_macro, state.input_news, state.input_social,
    ] if v == "active")

    # Check minimum required inputs
    if state.active_input_count < ctrl.MIN_REQUIRED_INPUTS:
        # Gate 1 already passed but we note insufficient for downstream gating
        sdl.gate(2, "input_universe",
                 {"active": state.active_input_count, "min": ctrl.MIN_REQUIRED_INPUTS},
                 "insufficient_inputs",
                 f"only {state.active_input_count} active inputs (min={ctrl.MIN_REQUIRED_INPUTS})")
    else:
        sdl.gate(2, "input_universe",
                 {"active": state.active_input_count},
                 f"active={state.active_input_count}",
                 f"inputs: price={state.input_price} vol={state.input_volatility} "
                 f"options={state.input_options} credit={state.input_credit} "
                 f"news={state.input_news}")


def gate3_benchmark(ctrl, state, sdl, spx_bars):
    """
    Gate 3 — Benchmark Context.
    Computes SPY trend, momentum, drawdown, and volatility regime.
    """
    if not spx_bars or len(spx_bars) < 5:
        state.benchmark_state          = "neutral"
        state.benchmark_momentum_state = "flat"
        state.benchmark_risk_state     = "normal"
        state.benchmark_vol_state      = "normal"
        sdl.gate(3, "benchmark", {"bars": len(spx_bars) if spx_bars else 0},
                 "neutral", "insufficient bars for benchmark analysis")
        return

    closes = [b.get('c', 0) for b in spx_bars if b.get('c')]

    # Trend: SMA20 vs SMA50 with neutral band
    sma_short = _compute_sma(closes, ctrl.SPX_SMA_SHORT)
    sma_long  = _compute_sma(closes, ctrl.SPX_SMA_LONG)
    if sma_short is not None and sma_long is not None and sma_long != 0:
        diff = (sma_short - sma_long) / sma_long
        if diff > ctrl.TREND_NEUTRAL_BAND:
            state.benchmark_state = "bullish"
        elif diff < -ctrl.TREND_NEUTRAL_BAND:
            state.benchmark_state = "bearish"
        else:
            state.benchmark_state = "neutral"
    else:
        state.benchmark_state = "neutral"

    # Momentum: ROC over ROC_LOOKBACK periods
    roc = _compute_roc(closes, ctrl.ROC_LOOKBACK)
    if roc > ctrl.TREND_NEUTRAL_BAND:
        state.benchmark_momentum_state = "positive"
    elif roc < -ctrl.TREND_NEUTRAL_BAND:
        state.benchmark_momentum_state = "negative"
    else:
        state.benchmark_momentum_state = "flat"

    # Drawdown from recent high
    recent_high = max(closes[-50:]) if len(closes) >= 50 else max(closes)
    current     = closes[-1]
    drawdown    = (recent_high - current) / recent_high if recent_high > 0 else 0.0
    state.benchmark_risk_state = "drawdown" if drawdown > ctrl.DRAWDOWN_THRESHOLD else "normal"

    # Volatility: realized vol from bars
    realized_vol = _compute_realized_vol(closes, window=20)
    state.benchmark_vol_state = "high" if realized_vol > ctrl.SPX_VOL_THRESHOLD else "normal"

    sdl.gate(3, "benchmark",
             {"trend": state.benchmark_state, "roc": f"{roc:.4f}",
              "drawdown": f"{drawdown:.4f}", "realized_vol": f"{realized_vol:.4f}"},
             f"trend={state.benchmark_state} momentum={state.benchmark_momentum_state} "
             f"risk={state.benchmark_risk_state} vol={state.benchmark_vol_state}",
             f"SMA{ctrl.SPX_SMA_SHORT}/{ctrl.SPX_SMA_LONG} spread={diff:.4f}" if sma_long else "SMA unavailable")


def gate4_price_action(ctrl, state, sdl, spx_bars, sector_returns):
    """
    Gate 4 — Price Action.
    Computes price sentiment, structure, momentum, and dispersion.
    """
    if not spx_bars or len(spx_bars) < 10:
        state.price_score = 0.0
        sdl.gate(4, "price_action", {"bars": len(spx_bars) if spx_bars else 0},
                 "neutral", "insufficient bars")
        return

    closes = [b.get('c', 0) for b in spx_bars if b.get('c')]

    # 1-day return
    spy_1d = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] else 0.0
    state._spy_1d_return = spy_1d

    # Short ROC
    roc_short = _compute_roc(closes, ctrl.ROC_SHORT)
    roc_long  = _compute_roc(closes, ctrl.ROC_LONG_BARS)

    # Price sentiment state: 1d return + short ROC alignment
    if spy_1d > 0 and roc_short > 0:
        state.price_sentiment_state = "positive"
    elif spy_1d < 0 and roc_short < 0:
        state.price_sentiment_state = "negative"
    else:
        state.price_sentiment_state = "neutral"

    # Price structure: above MA50 and MA200
    sma50  = _compute_sma(closes, 50)
    sma200 = _compute_sma(closes, 200)
    above50  = sma50  is not None and closes[-1] > sma50
    above200 = sma200 is not None and closes[-1] > sma200
    if above50 and above200:
        state.price_structure_state = "strong"
    elif not above50 and not above200:
        state.price_structure_state = "weak"
    else:
        state.price_structure_state = "neutral"

    # Price momentum: short ROC accelerating vs long ROC
    if roc_short > roc_long + ctrl.ACCELERATION_THRESHOLD:
        state.price_momentum_state = "improving"
    elif roc_short < roc_long - ctrl.ACCELERATION_THRESHOLD:
        state.price_momentum_state = "deteriorating"
    else:
        state.price_momentum_state = "neutral"

    # Price dispersion: std of sector returns
    sec_vals = [v for v in sector_returns.values() if v is not None]
    if len(sec_vals) >= 3:
        mean_r = sum(sec_vals) / len(sec_vals)
        var_r  = sum((r - mean_r) ** 2 for r in sec_vals) / len(sec_vals)
        std_r  = math.sqrt(var_r)
        state.price_dispersion_state = "high" if std_r > ctrl.DISPERSION_THRESHOLD else "low"
    else:
        state.price_dispersion_state = "low"

    state.price_score = _score_price(state)
    sdl.gate(4, "price_action",
             {"spy_1d": f"{spy_1d:.4f}", "roc_short": f"{roc_short:.4f}",
              "above50": above50, "above200": above200},
             f"sentiment={state.price_sentiment_state} structure={state.price_structure_state} "
             f"momentum={state.price_momentum_state} score={state.price_score:.3f}",
             f"1d={spy_1d:.4f} roc_s={roc_short:.4f} roc_l={roc_long:.4f}")


def gate5_breadth(ctrl, state, sdl, sector_returns, rsp_spy_spread):
    """
    Gate 5 — Market Breadth.
    Uses sector ETF proxy for breadth (no free advance/decline data).
    TODO: DATA_DEPENDENCY — advance/decline line requires exchange breadth data feed.
    """
    # Proxy: count positive-returning sector ETFs (XLK XLF XLE XLC XLB XLY XLV XLI XLP XLU)
    sector_tickers = ["XLK", "XLF", "XLE", "XLC", "XLB", "XLY", "XLV", "XLI", "XLP", "XLU"]
    available = [t for t in sector_tickers if t in sector_returns]
    positive  = [t for t in available if sector_returns.get(t, 0) > 0]
    neg_ratio = (len(available) - len(positive)) / len(available) if available else 0.5
    pos_ratio = len(positive) / len(available) if available else 0.5

    # Breadth state from positive sector count
    if pos_ratio >= ctrl.ADVANCER_RATIO_THRESH:
        state.breadth_state = "broad_participation"
    elif neg_ratio >= ctrl.DECLINER_RATIO_THRESH:
        state.breadth_state = "narrow_negative"
    elif pos_ratio > 0.5:
        state.breadth_state = "supportive"
    elif pos_ratio < 0.5:
        state.breadth_state = "weakening"
    else:
        state.breadth_state = "neutral"

    # Breadth momentum: RSP vs SPY spread (equal-weight outperformance)
    if rsp_spy_spread > ctrl.BREADTH_RETURN_THRESH:
        state.breadth_momentum_state = "positive"
    elif rsp_spy_spread < -ctrl.BREADTH_RETURN_THRESH:
        state.breadth_momentum_state = "negative"
    else:
        state.breadth_momentum_state = "neutral"

    # Breadth quality: RSP vs SPY determines leadership breadth
    if rsp_spy_spread > ctrl.BREADTH_RETURN_THRESH:
        state.breadth_quality_state = "healthy"
    elif rsp_spy_spread < -ctrl.BREADTH_RETURN_THRESH:
        state.breadth_quality_state = "narrow_leadership"
    else:
        state.breadth_quality_state = "neutral"

    state.breadth_score = _score_breadth(state)
    sdl.gate(5, "breadth",
             {"positive_sectors": len(positive), "of": len(available),
              "rsp_spy_spread": f"{rsp_spy_spread:.4f}"},
             f"breadth={state.breadth_state} momentum={state.breadth_momentum_state} "
             f"quality={state.breadth_quality_state} score={state.breadth_score:.3f}",
             # TODO: DATA_DEPENDENCY — advance/decline line requires exchange breadth data feed
             "proxy breadth via sector ETFs (TODO: replace with A/D line when data available)")


def gate6_volume(ctrl, state, sdl, spx_bars):
    """
    Gate 6 — Volume Analysis.
    Volume patterns from SPY OHLCV bars.
    """
    if not spx_bars or len(spx_bars) < 5:
        state.volume_score = 0.0
        sdl.gate(6, "volume", {"bars": len(spx_bars) if spx_bars else 0},
                 "neutral", "insufficient bars")
        return

    closes  = [b.get('c', 0) for b in spx_bars]
    volumes = [b.get('v', 0) for b in spx_bars]

    # Average volume over last 20 bars
    avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    today_vol = volumes[-1] if volumes else 0
    today_close = closes[-1] if closes else 0
    prev_close  = closes[-2] if len(closes) >= 2 else today_close

    # Volume sentiment: is today up or down on volume?
    up_day = today_close >= prev_close
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    if up_day and vol_ratio > 1.0:
        state.volume_sentiment_state = "positive"
    elif not up_day and vol_ratio > 1.0:
        state.volume_sentiment_state = "negative"
    else:
        state.volume_sentiment_state = "neutral"

    # Volume pattern: accumulation / distribution
    if up_day and vol_ratio > (1.0 + ctrl.VOLUME_GROWTH_THRESH):
        state.volume_pattern_state = "accumulation"
    elif not up_day and vol_ratio > (1.0 + ctrl.VOLUME_GROWTH_THRESH):
        state.volume_pattern_state = "distribution"
    else:
        state.volume_pattern_state = "neutral"

    # Volume confirmation: breadth + accumulation alignment
    if (state.volume_pattern_state == "accumulation" and
            state.breadth_state in ("supportive", "broad_participation")):
        state.volume_confirmation_state = "bullish_confirm"
    elif (state.volume_pattern_state == "distribution" and
            state.breadth_state in ("weakening", "narrow_negative")):
        state.volume_confirmation_state = "bearish_confirm"
    else:
        state.volume_confirmation_state = "neutral"

    # Anomaly detection: z-score of today's volume vs 20d window
    if len(volumes) >= 5:
        vol_slice = volumes[-20:]
        mean_v = sum(vol_slice) / len(vol_slice)
        var_v  = sum((v - mean_v) ** 2 for v in vol_slice) / len(vol_slice)
        std_v  = math.sqrt(var_v) if var_v > 0 else 1
        zscore = (today_vol - mean_v) / std_v if std_v > 0 else 0.0
        state.volume_state = "abnormal_activity" if abs(zscore) > ctrl.VOLUME_ANOMALY_THRESH else "normal"
    else:
        state.volume_state = "normal"

    state.volume_score = _score_volume(state)
    sdl.gate(6, "volume",
             {"vol_ratio": f"{vol_ratio:.2f}", "up_day": up_day,
              "pattern": state.volume_pattern_state, "anomaly": state.volume_state},
             f"sentiment={state.volume_sentiment_state} pattern={state.volume_pattern_state} "
             f"confirm={state.volume_confirmation_state} score={state.volume_score:.3f}",
             f"today_vol={today_vol:,} avg={avg_vol:.0f} ratio={vol_ratio:.2f}")


def gate7_volatility(ctrl, state, sdl, spx_bars, vix_current, vix_history):
    """
    Gate 7 — Volatility Analysis.
    VIX level/trend + realized vol from SPY bars.
    TODO: DATA_DEPENDENCY — VVIX and vol term structure require options feed.
    """
    closes = [b.get('c', 0) for b in spx_bars if b.get('c')] if spx_bars else []

    # VIX-based sentiment
    if vix_current is not None:
        # VIX ROC (rate of change)
        vix_roc = _compute_roc(vix_history, 5) if len(vix_history) >= 6 else 0.0
        if vix_roc > ctrl.VIX_ROC_THRESHOLD:
            state.vol_sentiment_state = "risk_off"
        elif vix_roc < -ctrl.VIX_ROC_THRESHOLD:
            state.vol_sentiment_state = "risk_on"
        else:
            state.vol_sentiment_state = "neutral"

        # Vol structure from absolute VIX level
        state.vol_structure_state = "stress" if vix_current > ctrl.VIX_HIGH_THRESHOLD else "calm"
    else:
        # No VIX — fall back to realized vol from bars
        state.vol_sentiment_state = "neutral"
        state.vol_structure_state = "calm"

    # Realized vol: short vs long window comparison
    if len(closes) >= 25:
        rvol_short = _compute_realized_vol(closes, window=10)
        rvol_long  = _compute_realized_vol(closes, window=20)
        if rvol_short > rvol_long + ctrl.REALIZED_VOL_GAP:
            state.realized_vol_state = "worsening"
        elif rvol_short < rvol_long - ctrl.REALIZED_VOL_GAP:
            state.realized_vol_state = "easing"
        else:
            state.realized_vol_state = "neutral"
    else:
        state.realized_vol_state = "neutral"

    # Vol instability: high realized vol AND VIX elevated
    if (state.vol_structure_state == "stress" or
            state.realized_vol_state == "worsening"):
        state.vol_instability_state = "elevated"
    else:
        state.vol_instability_state = "normal"

    # TODO: DATA_DEPENDENCY — VVIX (vol-of-vol) requires options data feed
    # TODO: DATA_DEPENDENCY — vol term structure (VIX3M/VIX ratio) requires futures data

    state.vol_score = _score_volatility(state)
    sdl.gate(7, "volatility",
             {"vix": vix_current, "vol_sentiment": state.vol_sentiment_state,
              "vol_structure": state.vol_structure_state, "realized": state.realized_vol_state},
             f"sentiment={state.vol_sentiment_state} structure={state.vol_structure_state} "
             f"realized={state.realized_vol_state} instability={state.vol_instability_state} "
             f"score={state.vol_score:.3f}",
             "VIX data available" if vix_current else "VIX unavailable — using realized vol proxy")


def gate8_options(ctrl, state, sdl, options_data):
    """
    Gate 8 — Options Market.
    Put/call ratio analysis. Other options metrics require paid data.
    TODO: DATA_DEPENDENCY — dealer gamma, skew, implied correlation require paid options data.
    """
    put_call = options_data.get("put_call_ratio") if options_data else None
    pc_avg   = options_data.get("put_call_avg", 0.62) if options_data else 0.62

    if put_call is not None:
        if put_call > ctrl.PUT_CALL_HIGH_THRESH:
            state.options_sentiment_state  = "fearful"
            state.options_tail_risk_state  = "high_demand_for_protection"
        elif put_call < ctrl.PUT_CALL_LOW_THRESH:
            state.options_sentiment_state  = "complacent"
            state.options_tail_risk_state  = "normal"
        else:
            state.options_sentiment_state  = "neutral"
            state.options_tail_risk_state  = "normal"

        # Flow state: fearful = stabilizing (options demand is a floor); complacent = destabilizing
        if state.options_sentiment_state == "fearful":
            state.options_flow_state = "stabilizing"
        elif state.options_sentiment_state == "complacent":
            state.options_flow_state = "destabilizing"
        else:
            state.options_flow_state = "neutral"
    else:
        state.options_sentiment_state  = "neutral"
        state.options_tail_risk_state  = "normal"
        state.options_flow_state       = "neutral"

    # TODO: DATA_DEPENDENCY — call speculation ratio requires per-contract volume
    state.options_speculation_state = "neutral"
    # TODO: DATA_DEPENDENCY — implied correlation index requires options feed
    state.options_corr_state = "normal"
    # TODO: DATA_DEPENDENCY — skew index requires options chain data

    state.options_score = _score_options(state)
    sdl.gate(8, "options",
             {"put_call": put_call, "threshold_high": ctrl.PUT_CALL_HIGH_THRESH,
              "threshold_low": ctrl.PUT_CALL_LOW_THRESH},
             f"sentiment={state.options_sentiment_state} tail={state.options_tail_risk_state} "
             f"flow={state.options_flow_state} score={state.options_score:.3f}",
             "put/call from CBOE; skew/gamma/corr TODO:DATA_DEPENDENCY")


def gate9_safe_haven(ctrl, state, sdl, etf_returns):
    """
    Gate 9 — Safe-Haven / Cross-Asset.
    Uses GLD, TLT, UUP returns relative to SPY.
    """
    if not etf_returns:
        sdl.gate(9, "safe_haven", {}, "neutral", "no ETF return data")
        return

    tlt_ret = etf_returns.get("TLT", 0.0)
    gld_ret = etf_returns.get("GLD", 0.0)
    uup_ret = etf_returns.get("UUP", 0.0)
    spy_ret = etf_returns.get("SPY", 0.0)
    qqq_ret = etf_returns.get("QQQ", 0.0)
    usmv_ret = etf_returns.get("USMV", 0.0)

    # Cyclical vs defensive for rotation
    cyclical_rets  = [etf_returns.get(t, 0) for t in ["XLK", "XLF", "XLY", "XLB", "XLC", "XLE"]]
    defensive_rets = [etf_returns.get(t, 0) for t in ["XLP", "XLV", "XLU"]]
    cyc_avg  = sum(cyclical_rets) / len(cyclical_rets) if cyclical_rets else 0.0
    def_avg  = sum(defensive_rets) / len(defensive_rets) if defensive_rets else 0.0

    # Cross-asset state: risk-off flows
    spy_negative = spy_ret < -ctrl.SPX_WEAK_THRESHOLD
    if tlt_ret > ctrl.TREASURY_THRESHOLD and spy_negative:
        state.cross_asset_state = "risk_off"
    elif gld_ret > ctrl.GOLD_THRESHOLD and spy_negative:
        state.cross_asset_state = "defensive_flow"
    elif uup_ret > ctrl.DXY_THRESHOLD and spy_negative:
        state.cross_asset_state = "stress_dollar_bid"
    else:
        state.cross_asset_state = "neutral"

    # Rotation state: cyclical vs defensive leadership
    spread = cyc_avg - def_avg
    if spread > ctrl.CYCLICAL_THRESHOLD:
        state.rotation_state = "risk_on"
    elif spread < -ctrl.DEFENSIVE_THRESHOLD:
        state.rotation_state = "risk_off"
    else:
        state.rotation_state = "neutral"

    # Risk appetite: QQQ (high-beta growth) vs USMV (low-vol)
    beta_spread = qqq_ret - usmv_ret
    if beta_spread > ctrl.BETA_THRESHOLD:
        state.risk_appetite_state = "elevated"
    elif beta_spread < -ctrl.BETA_THRESHOLD:
        state.risk_appetite_state = "defensive"
    else:
        state.risk_appetite_state = "neutral"

    state.cross_asset_score = _score_cross_asset(state)
    sdl.gate(9, "safe_haven",
             {"TLT": f"{tlt_ret:.4f}", "GLD": f"{gld_ret:.4f}",
              "UUP": f"{uup_ret:.4f}", "SPY": f"{spy_ret:.4f}",
              "cyc_avg": f"{cyc_avg:.4f}", "def_avg": f"{def_avg:.4f}"},
             f"cross_asset={state.cross_asset_state} rotation={state.rotation_state} "
             f"risk_appetite={state.risk_appetite_state} score={state.cross_asset_score:.3f}",
             f"cyc-def_spread={spread:.4f} beta_spread={beta_spread:.4f}")


def gate10_credit(ctrl, state, sdl, etf_returns):
    """
    Gate 10 — Credit Conditions.
    Uses HYG (high-yield) and LQD (investment-grade) as credit spread proxy.
    TODO: DATA_DEPENDENCY — CDS/CDX index, funding spreads require credit data feed.
    """
    if not etf_returns:
        sdl.gate(10, "credit", {}, "neutral", "no ETF return data")
        return

    hyg_ret = etf_returns.get("HYG", 0.0)
    lqd_ret = etf_returns.get("LQD", 0.0)
    spy_ret = etf_returns.get("SPY", 0.0)

    # HY vs IG spread proxy
    hy_ig_spread = hyg_ret - lqd_ret

    if hy_ig_spread < -ctrl.SPREAD_WIDENING_THRESH:
        # HY underperforming IG → spreads widening → credit deteriorating
        state.credit_state = "deteriorating"
    elif hy_ig_spread > ctrl.SPREAD_TIGHTENING_THRESH:
        # HY outperforming IG → spreads tightening → credit improving
        state.credit_state = "improving"
    else:
        state.credit_state = "neutral"

    # Credit risk appetite: HYG absolute performance
    if hyg_ret > ctrl.HY_IG_THRESHOLD and spy_ret > 0:
        state.credit_risk_state = "healthy_risk_appetite"
    elif hyg_ret < -ctrl.HY_IG_THRESHOLD:
        state.credit_risk_state = "weak_risk_appetite"
    else:
        state.credit_risk_state = "neutral"

    # Credit stress: use credit_state and vol as proxy
    if state.credit_state == "deteriorating" and state.vol_structure_state == "stress":
        state.credit_stress_state = "elevated"
    else:
        state.credit_stress_state = "normal"

    # TODO: DATA_DEPENDENCY — CDS/CDX index requires credit data feed
    # TODO: DATA_DEPENDENCY — funding spreads (LIBOR-OIS / SOFR) require rates feed
    state.liquidity_state = "normal"

    state.credit_score = _score_credit(state)
    sdl.gate(10, "credit",
             {"HYG": f"{hyg_ret:.4f}", "LQD": f"{lqd_ret:.4f}",
              "hy_ig_spread": f"{hy_ig_spread:.4f}"},
             f"credit={state.credit_state} risk={state.credit_risk_state} "
             f"stress={state.credit_stress_state} score={state.credit_score:.3f}",
             "HYG/LQD proxy (TODO: replace CDS/CDX when data available)")


def gate11_sector_rotation(ctrl, state, sdl, sector_returns):
    """
    Gate 11 — Sector Rotation.
    Cyclical vs defensive sector leadership analysis.
    """
    if not sector_returns:
        sdl.gate(11, "sector_rotation", {}, "neutral", "no sector return data")
        return

    cyclical_tickers  = ["XLK", "XLF", "XLE", "XLC", "XLB", "XLY"]
    defensive_tickers = ["XLP", "XLV", "XLU"]

    cyc_rets  = [sector_returns.get(t, 0) for t in cyclical_tickers if t in sector_returns]
    def_rets  = [sector_returns.get(t, 0) for t in defensive_tickers if t in sector_returns]
    xlf_ret   = sector_returns.get("XLF", 0.0)

    cyc_avg = sum(cyc_rets) / len(cyc_rets) if cyc_rets else 0.0
    def_avg = sum(def_rets) / len(def_rets) if def_rets else 0.0
    spread  = cyc_avg - def_avg

    # Leadership state
    if cyc_avg > def_avg + ctrl.CYCLICAL_LEAD_THRESH:
        state.sector_leadership_state = "growth_led"
    elif def_avg > cyc_avg + ctrl.DEFENSIVE_LEAD_THRESH:
        state.sector_leadership_state = "defensive_led"
    else:
        state.sector_leadership_state = "neutral"

    # Rotation regime
    if spread > ctrl.LEADERSHIP_THRESHOLD:
        state.sector_rotation_state = "expansionary"
    elif spread < -ctrl.LEADERSHIP_THRESHOLD:
        state.sector_rotation_state = "contractionary"
    else:
        state.sector_rotation_state = "neutral"

    # Confirmation: financials (XLF) confirm cyclical or defensive bias
    if state.sector_leadership_state == "growth_led" and xlf_ret > ctrl.FINANCIALS_CONFIRM_THRESH:
        state.sector_confirmation_state = "pro_growth"
    elif state.sector_leadership_state == "defensive_led" and def_avg > ctrl.DEFENSIVE_CONFIRM_THRESH:
        state.sector_confirmation_state = "defensive_bias"
    else:
        state.sector_confirmation_state = "neutral"

    state.sector_score = (
        0.5 * (1.0 if state.sector_leadership_state == "growth_led"
               else -1.0 if state.sector_leadership_state == "defensive_led" else 0.0) +
        0.3 * (1.0 if state.sector_rotation_state == "expansionary"
               else -1.0 if state.sector_rotation_state == "contractionary" else 0.0) +
        0.2 * (1.0 if state.sector_confirmation_state == "pro_growth"
               else -1.0 if state.sector_confirmation_state == "defensive_bias" else 0.0)
    )
    state.sector_score = max(-1.0, min(1.0, state.sector_score))

    sdl.gate(11, "sector_rotation",
             {"cyc_avg": f"{cyc_avg:.4f}", "def_avg": f"{def_avg:.4f}",
              "spread": f"{spread:.4f}", "XLF": f"{xlf_ret:.4f}"},
             f"leadership={state.sector_leadership_state} rotation={state.sector_rotation_state} "
             f"confirm={state.sector_confirmation_state} score={state.sector_score:.3f}",
             f"cyc-def={spread:.4f}")


def gate12_skip_macro(ctrl, state, sdl, macro_data):
    """
    [SKIP / PLACEHOLDER] Gate 12 — Macro Conditions.

    MARKED SKIP 2026-04-24: this gate has no free-tier proxy. Every
    state is hardcoded to "neutral" with macro_score=0.0; the function
    is effectively a no-op that exists to preserve the gate slot and
    the state-field surface for when a real data feed (FRED, CME,
    Bloomberg) is wired in. Do NOT mistake a neutral macro_state
    entry in the decision log for a real macro check.

    TODO: DATA_DEPENDENCY — economic surprise indices, Fed funds
    futures, yield curve shape all require FRED or paid Bloomberg.
    """
    # TODO: DATA_DEPENDENCY — CPI surprise, GDP surprise require FRED or paid Bloomberg
    # TODO: DATA_DEPENDENCY — Fed funds futures require CME data
    # TODO: DATA_DEPENDENCY — yield curve data requires Treasury/FRED feed
    state.macro_state           = "neutral"
    state.macro_policy_state    = "neutral"
    state.macro_sentiment_state = "neutral"
    state.macro_score           = 0.0

    sdl.gate(12, "SKIP_macro",
             {"macro_data_keys": list(macro_data.keys()) if macro_data else []},
             "neutral",
             "SKIP:DATA_DEPENDENCY — macro data (CPI/GDP surprise, Fed futures) requires FRED/Bloomberg")


def gate13_news(ctrl, state, sdl, news_data):
    """
    Gate 13 — News Sentiment.
    Uses Agent 2's DB news_feed for aggregate market news sentiment.
    """
    avg_score   = news_data.get("score", 0.0) if news_data else 0.0
    entry_count = news_data.get("count", 0) if news_data else 0
    macro_count = news_data.get("macro_count", 0) if news_data else 0
    micro_count = news_data.get("micro_count", 0) if news_data else 0

    if entry_count == 0:
        state.news_sentiment_state  = "neutral"
        state.news_conviction_state = "neutral"
        state.news_driver_state     = "neutral"
        state.news_score            = 0.0
        sdl.gate(13, "news", {"entry_count": 0}, "neutral", "no news entries in DB")
        return

    # Sentiment state from avg score
    if avg_score >= ctrl.NEWS_POSITIVE_THRESH:
        state.news_sentiment_state = "positive"
    elif avg_score <= ctrl.NEWS_NEGATIVE_THRESH:
        state.news_sentiment_state = "negative"
    else:
        state.news_sentiment_state = "neutral"

    # Conviction: strong if entry count >= MIN_CONFIRMATIONS and score magnitude is high
    if (entry_count >= ctrl.MIN_CONFIRMATIONS and
            avg_score >= ctrl.NEWS_POSITIVE_THRESH * 2):
        state.news_conviction_state = "strong_positive"
    elif (entry_count >= ctrl.MIN_CONFIRMATIONS and
            avg_score <= ctrl.NEWS_NEGATIVE_THRESH * 2):
        state.news_conviction_state = "strong_negative"
    else:
        state.news_conviction_state = "neutral"

    # Driver state: macro vs micro dominant
    total = macro_count + micro_count
    if total > 0:
        macro_ratio = macro_count / total
        micro_ratio = micro_count / total
        if macro_ratio >= ctrl.NEWS_DOMINANCE_THRESH:
            state.news_driver_state = "macro_dominant"
        elif micro_ratio >= ctrl.NEWS_DOMINANCE_THRESH:
            state.news_driver_state = "micro_dominant"
        else:
            state.news_driver_state = "neutral"
    else:
        state.news_driver_state = "neutral"

    state.news_score = _score_news(state)
    sdl.gate(13, "news",
             {"avg_score": f"{avg_score:.3f}", "count": entry_count,
              "macro": macro_count, "micro": micro_count},
             f"sentiment={state.news_sentiment_state} conviction={state.news_conviction_state} "
             f"driver={state.news_driver_state} score={state.news_score:.3f}",
             f"entries={entry_count} avg={avg_score:.3f}")


def gate14_skip_social(ctrl, state, sdl, social_data):
    """
    [SKIP / PLACEHOLDER] Gate 14 — Social Sentiment.

    MARKED SKIP 2026-04-24: social sentiment has no free-tier
    equivalent and our tier does not include StockTwits / X / Reddit
    access. Every state is hardcoded to neutral/aligned/normal with
    social_score=0.0; the gate is a no-op retained to keep the
    state-field surface intact for future paid-feed integration.
    Do NOT read `social_*` state values as signal; they are placeholders.

    TODO: DATA_DEPENDENCY — social sentiment requires paid feed
    (StockTwits, Twitter/X API, Reddit WallStreetBets scraper).
    """
    # TODO: DATA_DEPENDENCY — StockTwits requires paid API access
    # TODO: DATA_DEPENDENCY — Twitter/X API requires paid developer access
    # TODO: DATA_DEPENDENCY — Reddit WallStreetBets analysis requires API access
    state.social_sentiment_state = "neutral"
    state.social_attention_state = "normal"
    state.social_news_state      = "aligned"
    state.social_price_state     = "aligned"
    state.social_quality_state   = "normal"
    state.social_score           = 0.0

    sdl.gate(14, "SKIP_social",
             {"social_data_keys": list(social_data.keys()) if social_data else []},
             "neutral",
             "SKIP:DATA_DEPENDENCY — social data requires paid feed (StockTwits/Twitter/Reddit)")


def gate15_breadth_price_divergence(ctrl, state, sdl, spx_bars):
    """
    Gate 15 — Breadth-Price Divergence.
    Pure logic gate using states computed in gates 3-14.
    Detects divergences between price action and underlying market health.
    """
    spy_ret = state._spy_1d_return

    # Fragile rally: price up but breadth weak
    if (spy_ret > 0 and
            state.breadth_state in ("weakening", "narrow_negative")):
        state.divergence_state = "fragile_rally"

    # Hidden stabilization: price down but broad breadth
    elif (spy_ret < 0 and
            state.breadth_state in ("supportive", "broad_participation")):
        state.divergence_state = "hidden_stabilization"

    # Narrow index strength: strong price but narrow breadth quality
    elif (state.price_structure_state == "strong" and
            state.breadth_quality_state == "narrow_leadership"):
        state.divergence_state = "narrow_index_strength"

    # False calm: vol says risk-on but credit is deteriorating
    elif (state.vol_sentiment_state == "risk_on" and
            state.credit_state == "deteriorating"):
        state.divergence_state = "false_calm_risk"

    # Conflicted risk signal: price positive but cross-asset says risk-off
    elif (spy_ret > 0 and
            state.cross_asset_state in ("risk_off", "defensive_flow", "stress_dollar_bid")):
        state.divergence_state = "conflicted_risk_signal"

    else:
        state.divergence_state = "none"

    sdl.gate(15, "breadth_price_divergence",
             {"spy_1d": f"{spy_ret:.4f}", "breadth": state.breadth_state,
              "price_structure": state.price_structure_state,
              "cross_asset": state.cross_asset_state,
              "credit": state.credit_state},
             f"divergence={state.divergence_state}",
             f"spy_ret={spy_ret:.4f} breadth={state.breadth_state} "
             f"vol_sent={state.vol_sentiment_state}")

def gate16_composite_construction(ctrl, state, sdl):
    """
    Gate 16 — Composite Score Construction.
    Calls all component scoring functions and stores results.
    Inactive inputs score 0.0.
    """
    scores = {}

    scores["price"]       = _score_price(state)      if state.input_price      == "active" else 0.0
    scores["breadth"]     = _score_breadth(state)    if state.input_breadth    == "active" else 0.0
    scores["volume"]      = _score_volume(state)     if state.input_volume     == "active" else 0.0
    scores["volatility"]  = _score_volatility(state) if state.input_volatility == "active" else 0.0
    scores["options"]     = _score_options(state)    if state.input_options    == "active" else 0.0
    scores["cross_asset"] = _score_cross_asset(state) if state.input_safe_haven == "active" else 0.0
    scores["credit"]      = _score_credit(state)     if state.input_credit     == "active" else 0.0
    scores["macro"]       = _score_macro(state)      if state.input_macro      == "active" else 0.0
    scores["news"]        = _score_news(state)       if state.input_news       == "active" else 0.0
    scores["social"]      = _score_social(state)     if state.input_social     == "active" else 0.0

    state.component_scores = scores
    sdl.gate(16, "composite_construction",
             {k: f"{v:.3f}" for k, v in scores.items()},
             f"scores built for {sum(1 for v in scores.values() if v != 0.0)} active components",
             "inactive inputs scored 0.0")


def gate17_weighting(ctrl, state, sdl):
    """
    Gate 17 — Effective Weight Computation.
    Applies adjustments and normalises to sum = 1.0 across active components.
    """
    weights = {
        "price":       ctrl.WEIGHT_PRICE,
        "breadth":     ctrl.WEIGHT_BREADTH,
        "volume":      ctrl.WEIGHT_VOLUME,
        "volatility":  ctrl.WEIGHT_VOLATILITY,
        "options":     ctrl.WEIGHT_OPTIONS,
        "cross_asset": ctrl.WEIGHT_CROSS_ASSET,
        "credit":      ctrl.WEIGHT_CREDIT,
        "macro":       ctrl.WEIGHT_MACRO,
        "news":        ctrl.WEIGHT_NEWS,
        "social":      ctrl.WEIGHT_SOCIAL,
    }

    adjustments = []

    # Adjustment: high vol regime → boost volatility weight, cut social
    if state.benchmark_vol_state == "high":
        weights["volatility"] *= 1.5
        weights["social"]     *= 0.5
        adjustments.append("vol_high: volatility*1.5 social*0.5")

    # Adjustment: credit stress or liquidity stress → boost credit weight
    if state.credit_stress_state == "elevated" or state.liquidity_state == "stress":
        weights["credit"] *= 1.5
        adjustments.append("credit_stress: credit*1.5")

    # Adjustment: strong news conviction → boost news weight
    if state.news_conviction_state in ("strong_positive", "strong_negative"):
        weights["news"] *= 2.0
        adjustments.append("news_conviction: news*2.0")

    # Adjustment: low-trust social → heavily discount social
    if state.social_quality_state == "low_trust":
        weights["social"] *= 0.3
        adjustments.append("low_trust_social: social*0.3")

    # Adjustment: macro fear state → boost macro weight
    if state.macro_state in ("inflation_risk", "growth_risk"):
        weights["macro"] *= 2.0
        adjustments.append("macro_fear: macro*2.0")

    # Adjustment: narrow leadership → boost breadth weight
    if state.breadth_quality_state == "narrow_leadership":
        weights["breadth"] *= 1.5
        adjustments.append("narrow_leadership: breadth*1.5")

    # Zero-out inactive components
    input_map = {
        "price":       state.input_price,
        "breadth":     state.input_breadth,
        "volume":      state.input_volume,
        "volatility":  state.input_volatility,
        "options":     state.input_options,
        "cross_asset": state.input_safe_haven,
        "credit":      state.input_credit,
        "macro":       state.input_macro,
        "news":        state.input_news,
        "social":      state.input_social,
    }
    for k, active in input_map.items():
        if active != "active":
            weights[k] = 0.0

    # Normalize so active components sum to 1.0
    total_w = sum(weights.values())
    if total_w > 0:
        weights = {k: v / total_w for k, v in weights.items()}
    else:
        # All inactive — equal weight fallback (won't matter since all scores are 0)
        n = len(weights)
        weights = {k: 1.0 / n for k in weights}

    state.effective_weights = weights
    sdl.gate(17, "weighting",
             {k: f"{v:.4f}" for k, v in weights.items()},
             f"weights normalized (total before norm={total_w:.4f})",
             "; ".join(adjustments) if adjustments else "no adjustments")


def gate18_composite_score(ctrl, state, sdl):
    """
    Gate 18 — Composite Sentiment Score.
    Weighted sum of component scores → raw_sentiment_score → market_sentiment_state.
    """
    scores  = state.component_scores
    weights = state.effective_weights

    raw = sum(weights.get(k, 0.0) * scores.get(k, 0.0) for k in scores)
    state.raw_sentiment_score = raw

    if raw >= ctrl.EUPHORIC_THRESHOLD:
        state.market_sentiment_state = "euphoric"
    elif raw >= ctrl.BULLISH_THRESHOLD:
        state.market_sentiment_state = "bullish"
    elif raw <= ctrl.PANIC_THRESHOLD:
        state.market_sentiment_state = "panic"
    elif raw <= ctrl.BEARISH_THRESHOLD:
        state.market_sentiment_state = "bearish"
    else:
        state.market_sentiment_state = "neutral"

    sdl.gate(18, "composite_score",
             {"raw": f"{raw:.4f}", "bullish_thresh": ctrl.BULLISH_THRESHOLD,
              "bearish_thresh": ctrl.BEARISH_THRESHOLD},
             f"raw_score={raw:.4f} market_sentiment={state.market_sentiment_state}",
             f"euphoric>={ctrl.EUPHORIC_THRESHOLD} bullish>={ctrl.BULLISH_THRESHOLD} "
             f"bearish<={ctrl.BEARISH_THRESHOLD} panic<={ctrl.PANIC_THRESHOLD}")


def gate19_confidence(ctrl, state, sdl):
    """
    Gate 19 — Confidence Assessment.
    Measures how much trust to place in the composite score.
    """
    active_count = state.active_input_count
    scores       = list(state.component_scores.values())

    # Input confidence
    state.confidence_input_state = (
        "sufficient" if active_count >= ctrl.MIN_CONFIDENT_COMPONENTS else "weak"
    )

    # Agreement among components: std deviation of scores
    if len(scores) >= 2:
        mean_s = sum(scores) / len(scores)
        var_s  = sum((s - mean_s) ** 2 for s in scores) / len(scores)
        std_s  = math.sqrt(var_s)
        if std_s < ctrl.AGREEMENT_THRESHOLD:
            state.confidence_state = "high_agreement"
        elif std_s >= ctrl.DISAGREEMENT_THRESHOLD:
            state.confidence_state = "conflicted"
        else:
            state.confidence_state = "neutral"
    else:
        std_s = 0.0
        state.confidence_state = "neutral"

    # Data quality: fraction of total possible inputs that are active
    data_quality = active_count / 10.0
    state.data_confidence_state = (
        "high" if data_quality >= ctrl.DATA_QUALITY_THRESHOLD else "low"
    )

    # Composite confidence score
    agreement_factor = (1.0 if state.confidence_state == "high_agreement"
                        else 0.7 if state.confidence_state == "neutral"
                        else 0.5)
    quality_factor   = 1.0 if state.data_confidence_state == "high" else 0.7

    state.sentiment_confidence = (active_count / 10.0) * agreement_factor * quality_factor
    state.sentiment_confidence = max(0.0, min(1.0, state.sentiment_confidence))

    sdl.gate(19, "confidence",
             {"active": active_count, "std_scores": f"{std_s:.4f}",
              "data_quality": f"{data_quality:.2f}"},
             f"input={state.confidence_input_state} agreement={state.confidence_state} "
             f"data={state.data_confidence_state} confidence={state.sentiment_confidence:.3f}",
             f"active={active_count}/10 agreement_factor={agreement_factor:.1f} "
             f"quality_factor={quality_factor:.1f}")


def gate20_regime(ctrl, state, sdl):
    """
    Gate 20 — Market Regime Classification.
    Six regime states based on breadth, price, credit, and volatility.
    """
    score  = state.raw_sentiment_score
    broad  = state.breadth_state in ("supportive", "broad_participation")
    narrow = state.breadth_quality_state == "narrow_leadership"
    credit_ok  = state.credit_state in ("improving", "neutral")
    credit_bad = state.credit_state == "deteriorating"
    vol_stress = state.vol_structure_state == "stress"
    panic_mkt  = state.market_sentiment_state == "panic"
    euphoric   = state.market_sentiment_state == "euphoric"

    if euphoric and narrow:
        state.regime_state = "unstable_euphoria"
    elif panic_mkt or (credit_bad and vol_stress):
        state.regime_state = "stress_event"
    elif score >= ctrl.BULLISH_THRESHOLD and broad and credit_ok:
        state.regime_state = "healthy_risk_on"
    elif score >= ctrl.BULLISH_THRESHOLD and narrow:
        state.regime_state = "narrow_risk_on"
    elif credit_bad or vol_stress or score <= ctrl.BEARISH_THRESHOLD:
        state.regime_state = "defensive_risk_off"
    else:
        state.regime_state = "indecisive"

    sdl.gate(20, "regime",
             {"score": f"{score:.4f}", "broad_breadth": broad,
              "narrow_leadership": narrow, "credit": state.credit_state,
              "vol_structure": state.vol_structure_state},
             f"regime={state.regime_state}",
             f"mkt_sentiment={state.market_sentiment_state} credit_ok={credit_ok}")


def gate21_divergence_warnings(ctrl, state, sdl):
    """
    Gate 21 — Divergence Warning Flags.
    Checks five warning conditions; logs first and counts all.
    """
    warnings = []

    # Credit divergence: price bullish but credit deteriorating
    if (state.market_sentiment_state in ("bullish", "euphoric") and
            state.credit_state == "deteriorating"):
        warnings.append("credit_divergence")

    # Breadth divergence: price strong but breadth narrow
    if (state.price_structure_state == "strong" and
            state.breadth_quality_state == "narrow_leadership" and
            state.breadth_state in ("weakening", "narrow_negative")):
        warnings.append("breadth_divergence")

    # Bearish exhaustion risk: heavy put buying but price still up
    if (state.options_sentiment_state == "fearful" and
            state.market_sentiment_state in ("bullish", "euphoric")):
        warnings.append("bearish_exhaustion_risk")

    # Sentiment source conflict: divergence detected but composite bullish
    if (state.divergence_state in ("fragile_rally", "false_calm_risk",
                                    "conflicted_risk_signal") and
            state.market_sentiment_state in ("bullish", "euphoric")):
        warnings.append("sentiment_source_conflict")

    # Complacency mispricing: low put/call, low vol, euphoric → likely priced-in upside
    if (state.options_sentiment_state == "complacent" and
            state.vol_structure_state == "calm" and
            state.market_sentiment_state == "euphoric"):
        warnings.append("complacency_mispricing")

    state.active_warning_count = len(warnings)
    state.warning_state        = warnings[0] if warnings else "none"

    sdl.gate(21, "divergence_warnings",
             {"warnings_found": warnings},
             f"warning={state.warning_state} count={state.active_warning_count}",
             f"credit={state.credit_state} breadth={state.breadth_quality_state} "
             f"options={state.options_sentiment_state}")


def gate22_action(ctrl, state, sdl):
    """
    Gate 22 — Action Classification.
    Maps regime + warnings + score to an actionable classification.
    """
    score    = state.raw_sentiment_score
    warnings = state.active_warning_count

    if state.market_sentiment_state == "panic":
        state.classification = "panic_signal"
    elif state.market_sentiment_state == "euphoric":
        state.classification = "euphoria_warning"
    elif (state.market_sentiment_state in ("bullish",) and
            warnings <= ctrl.WARNING_COUNT_THRESHOLD and
            state.confidence_input_state == "sufficient"):
        state.classification = "bullish_signal"
    elif (state.market_sentiment_state in ("bearish",) and
            warnings <= ctrl.WARNING_COUNT_THRESHOLD and
            state.confidence_input_state == "sufficient"):
        state.classification = "bearish_signal"
    elif warnings > ctrl.WARNING_COUNT_THRESHOLD:
        state.classification = "divergence_watch"
    elif state.confidence_state == "conflicted":
        state.classification = "conflicted_signal"
    else:
        state.classification = "no_clear_signal"

    sdl.gate(22, "action",
             {"market_state": state.market_sentiment_state,
              "warnings": warnings, "confidence": state.confidence_input_state},
             f"classification={state.classification}",
             f"score={score:.4f} regime={state.regime_state} warnings={warnings}")


def gate23_risk_discounts(ctrl, state, sdl):
    """
    Gate 23 — Risk Discounts.
    Multiplicative discounts applied to sentiment score and confidence.
    """
    discounted_score = state.raw_sentiment_score
    discounted_conf  = state.sentiment_confidence
    discounts        = []

    # Social quality discount on score
    if state.social_quality_state == "low_trust":
        discounted_score *= ctrl.SOCIAL_RISK_DISCOUNT
        discounts.append(f"social_low_trust: score*{ctrl.SOCIAL_RISK_DISCOUNT}")

    # Low data quality discount on score
    if state.data_confidence_state == "low":
        discounted_score *= ctrl.LOW_DATA_DISCOUNT
        discounts.append(f"low_data: score*{ctrl.LOW_DATA_DISCOUNT}")

    # Warning count discount on confidence
    if state.active_warning_count > ctrl.WARNING_COUNT_THRESHOLD:
        discounted_conf *= ctrl.WARNING_DISCOUNT
        discounts.append(f"warnings({state.active_warning_count}): conf*{ctrl.WARNING_DISCOUNT}")

    # Drawdown discount: reduce positive sentiment strength in drawdown
    if state.benchmark_risk_state == "drawdown" and discounted_score > 0:
        discounted_score *= ctrl.DRAWDOWN_DISCOUNT
        discounts.append(f"drawdown: score*{ctrl.DRAWDOWN_DISCOUNT}")

    # Vol instability discount on confidence
    if state.vol_instability_state == "elevated":
        discounted_conf *= ctrl.VOL_INSTABILITY_DISCOUNT
        discounts.append(f"vol_instability: conf*{ctrl.VOL_INSTABILITY_DISCOUNT}")

    state.discounted_sentiment_score = discounted_score
    state.discounted_confidence      = max(0.0, min(1.0, discounted_conf))
    state.discounts_applied          = discounts

    sdl.gate(23, "risk_discounts",
             {"raw_score": f"{state.raw_sentiment_score:.4f}",
              "raw_conf": f"{state.sentiment_confidence:.4f}"},
             f"discounted_score={discounted_score:.4f} discounted_conf={discounted_conf:.4f}",
             "; ".join(discounts) if discounts else "no discounts applied")


def gate24_persistence(ctrl, state, sdl, db):
    """
    Gate 24 — Temporal Persistence.
    Reads historical sentiment from DB to classify stability.
    TODO: DATA_DEPENDENCY — requires historical sentiment_log table in DB.
    """
    # TODO: DATA_DEPENDENCY — requires sentiment_log table with historical records
    # Minimal implementation: query recent MARKET_SENTIMENT_CLASSIFIED events
    try:
        rows = db.query(
            "SELECT details FROM system_log WHERE event_type='MARKET_SENTIMENT_CLASSIFIED' "
            "ORDER BY created_at DESC LIMIT 10"
        )
    except Exception:
        rows = []

    prior_scores = []
    prior_states = []
    if rows:
        for row in rows[:10]:
            try:
                rec = _json.loads(row[0]) if row[0] else {}
                gates_data = rec.get("gates", [])
                for g in gates_data:
                    if g.get("gate") == 27:
                        # Extract final signal from gate 27 result
                        result_str = g.get("result", "")
                        if "final_signal=" in result_str:
                            sig_part = result_str.split("final_signal=")[1].split(" ")[0]
                            try:
                                prior_scores.append(float(sig_part))
                            except ValueError:
                                pass
                        if "state=" in result_str:
                            st_part = result_str.split("state=")[1].split(" ")[0]
                            prior_states.append(st_part)
            except Exception:
                continue

    current_score = state.discounted_sentiment_score

    if len(prior_scores) >= ctrl.PERSISTENCE_THRESHOLD:
        recent  = prior_scores[:ctrl.PERSISTENCE_THRESHOLD]
        all_pos = all(s > 0 for s in recent)
        all_neg = all(s < 0 for s in recent)
        # Flip detection: sign changes
        flips = sum(1 for i in range(1, len(prior_scores[:ctrl.FLIP_THRESHOLD]))
                    if (prior_scores[i] > 0) != (prior_scores[i-1] > 0))

        if flips >= ctrl.FLIP_THRESHOLD - 1:
            state.persistence_state = "unstable_sentiment"
        elif all_pos and current_score > 0:
            state.persistence_state = "persistent_bullish"
        elif all_neg and current_score < 0:
            state.persistence_state = "persistent_bearish"
        elif state.market_sentiment_state == "panic":
            state.persistence_state = "transient_panic"
        else:
            state.persistence_state = "unknown"

        # Trend vs prior session
        if prior_scores:
            delta = current_score - prior_scores[0]
            if delta > ctrl.IMPROVEMENT_THRESHOLD:
                state.sentiment_trend_state = "improving"
            elif delta < -ctrl.DETERIORATION_THRESHOLD:
                state.sentiment_trend_state = "worsening"
            else:
                state.sentiment_trend_state = "neutral"
    else:
        state.persistence_state    = "unknown"
        state.sentiment_trend_state = "neutral"

    sdl.gate(24, "persistence",
             {"prior_records": len(prior_scores), "current_score": f"{current_score:.4f}"},
             f"persistence={state.persistence_state} trend={state.sentiment_trend_state}",
             "TODO:DATA_DEPENDENCY — sentiment_log table needed for full persistence tracking")


def gate25_evaluation(ctrl, state, sdl, db):
    """
    Gate 25 — Snapshot Evaluation.
    Determines whether to retain this snapshot in the historical record.
    TODO: DATA_DEPENDENCY — prediction vs realized requires post-session price data.
    """
    # Retain snapshot if system status is ok and it's a valid decision state
    retain_statuses = {"ok", "benchmark_context_disabled"}
    state.snapshot_retained = state.system_status in retain_statuses

    if state.snapshot_retained:
        state.evaluation_note = (
            f"retained: classification={state.classification} "
            f"regime={state.regime_state} confidence={state.sentiment_confidence:.2f}"
        )
    else:
        state.evaluation_note = f"not_retained: system_status={state.system_status}"

    # TODO: DATA_DEPENDENCY — post-session prediction accuracy tracking requires
    # comparing this snapshot's prediction against realized next-day returns.
    # Requires a scheduled evaluation pass after market close.

    sdl.gate(25, "evaluation",
             {"retained": state.snapshot_retained, "system_status": state.system_status},
             f"retained={state.snapshot_retained}",
             state.evaluation_note)


def gate26_output(ctrl, state, sdl):
    """
    Gate 26 — Output Controls.
    Maps classification to output action, modifier, and priority.
    """
    action_map = {
        "bullish_signal":    "emit_bullish_sentiment",
        "bearish_signal":    "emit_bearish_sentiment",
        "panic_signal":      "emit_panic_alert",
        "euphoria_warning":  "emit_euphoria_alert",
        "divergence_watch":  "emit_divergence_warning",
        "conflicted_signal": "emit_conflicted_state",
        "no_clear_signal":   "emit_neutral_state",
    }
    state.output_action = action_map.get(state.classification, "emit_neutral_state")

    # Low confidence modifier
    state.output_modifier = (
        "low_confidence_flag"
        if state.sentiment_confidence < ctrl.LOW_CONF_THRESHOLD
        else ""
    )

    # Output priority: if volatility dominates price in magnitude → benchmark_first
    vol_score   = abs(state.component_scores.get("volatility", 0.0))
    price_score = abs(state.component_scores.get("price", 0.0))
    state.output_priority = "benchmark_first" if vol_score > price_score else "article_first"

    sdl.gate(26, "output",
             {"classification": state.classification,
              "confidence": f"{state.sentiment_confidence:.3f}"},
             f"action={state.output_action} modifier={state.output_modifier!r} "
             f"priority={state.output_priority}",
             f"vol_score={vol_score:.3f} price_score={price_score:.3f}")


def gate27_final_signal(ctrl, state, sdl):
    """
    Gate 27 — Final Signal.
    Combines discounted score and confidence into a single tradeable signal float.
    """
    final = state.discounted_sentiment_score * state.discounted_confidence
    state.final_sentiment_signal = final

    # Override classification for extreme market states
    if state.classification == "panic_signal":
        state.final_market_state = "panic_override"
    elif state.classification == "euphoria_warning":
        state.final_market_state = "euphoric_warning_override"
    elif final >= ctrl.STRONG_BULL_THRESHOLD:
        state.final_market_state = "strong_bullish"
    elif final >= ctrl.BULL_THRESHOLD_FINAL:
        state.final_market_state = "mild_bullish"
    elif final <= ctrl.STRONG_BEAR_THRESHOLD:
        state.final_market_state = "strong_bearish"
    elif final <= ctrl.BEAR_THRESHOLD_FINAL:
        state.final_market_state = "mild_bearish"
    elif ctrl.NEUTRAL_LOW <= final <= ctrl.NEUTRAL_HIGH:
        state.final_market_state = "neutral"
    else:
        state.final_market_state = "neutral"

    sdl.gate(27, "final_signal",
             {"discounted_score": f"{state.discounted_sentiment_score:.4f}",
              "discounted_conf": f"{state.discounted_confidence:.4f}"},
             f"final_signal={final:.4f} state={state.final_market_state}",
             f"strong_bull>={ctrl.STRONG_BULL_THRESHOLD} bull>={ctrl.BULL_THRESHOLD_FINAL} "
             f"bear<={ctrl.BEAR_THRESHOLD_FINAL} strong_bear<={ctrl.STRONG_BEAR_THRESHOLD}")


def _handle_screening_requests(db):
    """
    Fulfill pending 'sentiment' screening requests from the sector screener.
    Runs per-ticker put/call ratio, insider transaction, and volume analysis
    using existing Pulse functions. Writes results back to sector_screening
    and appends to the logic audit log.
    """
    import os as _os
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    pending = db.get_pending_screening_requests('sentiment')
    if not pending:
        return

    log.info(f"Screening requests: fulfilling {len(pending)} sentiment requests")
    ET_tz      = _ZI("America/New_York")
    today      = _dt.now(ET_tz).strftime('%Y-%m-%d')
    log_dir    = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                               'logs', 'logic_audits')
    _os.makedirs(log_dir, exist_ok=True)
    audit_path = _os.path.join(log_dir, f"{today}_pulse_screening.log")

    audit_lines = [
        "=" * 70,
        f"PULSE — SCREENING SENTIMENT AUDIT  ({_dt.now(ET_tz).strftime('%Y-%m-%d %H:%M ET')})",
        "-" * 70,
    ]

    for req in pending:
        ticker = req['ticker']
        run_id = req['run_id']

        try:
            # EDGAR + Finviz each have per-run caches and fetch_with_retry()
            # already does exponential backoff on 429/5xx — the 1s courtesy
            # sleep was just wasted wall time.
            put_call, put_call_avg = fetch_put_call_ratio(ticker)
            insider                = fetch_sec_insider_transactions(ticker, days_back=30)
            volume                 = fetch_volume_profile(ticker)

            tier, tier_label, cascade, summary = detect_cascade(
                put_call, put_call_avg, insider, volume
            )

            # Normalise tier to 0-1 score (tier 1=critical=bearish, tier 4=quiet=bullish)
            score = round({1: 0.1, 2: 0.35, 3: 0.60, 4: 0.85}.get(tier, 0.5), 4)

            if tier <= 2:
                signal = 'bearish'
            elif tier == 3:
                signal = 'neutral'
            else:
                signal = 'bullish'

            # 2026-04-28: notes formatting is None-safe.  When CBOE is
            # unreachable (which has been every screener run lately —
            # likely Cloudflare blocking), put_call/put_call_avg come back
            # as None.  Earlier code did f"{put_call:.2f}" unconditionally,
            # which throws `unsupported format string passed to NoneType`
            # and tipped EVERY screener-sentiment fulfilment into the
            # error-fallback (score=0.5).  detect_cascade itself handles
            # None inputs correctly — the bug was downstream of it.
            pc_str  = f"{put_call:.2f}"     if put_call     is not None else "N/A"
            pca_str = f"{put_call_avg:.2f}" if put_call_avg is not None else "N/A"
            notes = (
                f"Tier {tier} ({tier_label}). {summary}. "
                f"Put/call={pc_str} vs 30d avg={pca_str}. "
                f"Insider net={insider.get('net_dollar', 'N/A')}. "
                f"Volume vs avg={volume.get('today_vs_avg', 'N/A')}."
            )

            db.fulfill_screening_request(
                run_id=run_id,
                ticker=ticker,
                request_type='sentiment',
                signal=signal,
                score=score,
                notes=notes,
            )
            # Intentional dual-write — same `score` lands in two tables, each
            # serving a different consumer:
            #
            #   sector_screening.sentiment_score  (written above by
            #     fulfill_screening_request)        — per-ticker, screener
            #     display + composite-score aggregation. Covers every
            #     screener candidate, regardless of whether a tradable
            #     signal exists.
            #
            #   signals.sentiment_score           (written here by
            #     stamp_signals_sentiment)         — per-signal, consumed
            #     by the trader at Gate 5 (signal_score). Only stamps rows
            #     that have a QUEUED/active signal for this ticker.
            #
            # Both come from the same detect_cascade() computation, so
            # they're always in sync — never genuinely diverging values.
            # The split exists because the two tables live on different
            # cardinalities (per-ticker vs per-signal) and feed different
            # downstream code paths.
            try:
                db.stamp_signals_sentiment(ticker, score)
            except Exception as _e:
                log.debug(f"sentiment stamp failed for {ticker}: {_e}")

            audit_lines += [
                f"  {ticker}",
                f"    Signal     : {signal.upper()}  (score {score:.2f}  |  tier {tier} — {tier_label})",
                f"    Put/Call   : {pc_str} (30d avg {pca_str})",
                f"    Insider    : net {insider.get('net_dollar', 'unavailable')}",
                f"    Volume     : {volume.get('today_vs_avg', 'unavailable')} vs average",
                f"    Summary    : {summary}",
                "",
            ]
            log.info(f"Screening sentiment {ticker}: {signal} tier={tier} score={score:.2f}")

        except Exception as e:
            log.warning(f"Screening sentiment failed for {ticker}: {e}")
            db.fulfill_screening_request(
                run_id=run_id, ticker=ticker, request_type='sentiment',
                signal='neutral', score=0.5, notes=f"Error: {e}",
            )
            audit_lines += [f"  {ticker}: ERROR — {e}", ""]

    audit_lines.append("=" * 70 + "\n")
    with open(audit_path, 'a') as f:
        f.write('\n'.join(audit_lines) + '\n')

    log.info(f"Screening sentiment audit written: {audit_path}")


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run():
    db   = _master_db()
    ctrl = SentimentControls()
    now  = datetime.now(ET)
    log.info(f"The Pulse starting — {now.strftime('%H:%M ET')}")

    # Per-run caches — cleared at run start so Phase 2/3 + screening-request
    # fulfillment can each call EDGAR/Finviz without hitting the network twice
    # for the same ticker. Module-level (single-threaded agent) so a shared
    # dict is safe.
    _EDGAR_CACHE.clear()
    _FINVIZ_CACHE.clear()
    _PUT_CALL_CACHE.clear()

    db.log_event("AGENT_START", agent="The Pulse")
    db.log_heartbeat("market_sentiment_agent", "RUNNING")

    # Fulfill screening requests FIRST (before general scan which may timeout)
    try:
        _handle_screening_requests(db)
    except Exception as e:
        log.warning(f"Screening request enrichment failed: {e}")

    # ── Phase 1: 27-gate Market Sentiment Spine ───────────────────────────
    sdl   = SentimentDecisionLog()
    state = SentimentState()

    # Fetch all data inputs
    spx_bars                     = fetch_alpaca_bars(ctrl.SPX_TICKER, days=220)  # need 200d MA
    vix_current, vix_history     = fetch_vix()
    sector_rets                  = fetch_sector_returns()
    etf_rets                     = fetch_etf_returns(
        ["GLD", "TLT", "HYG", "LQD", "UUP", "QQQ", "RSP", "USMV"]
    )
    news_score, news_ct, macro_ct, micro_ct = fetch_news_sentiment_from_db(db)

    # Build data availability map
    data = {
        "price_bars":    spx_bars,
        "volume_bars":   spx_bars,
        "breadth_data":  {},   # TODO: DATA_DEPENDENCY — advance/decline requires exchange feed
        "vol_data":      ({"vix": vix_current, "vix_history": vix_history}
                          if vix_current else {}),
        "options_data":  {},   # filled by put/call below
        "safe_haven_data": etf_rets,
        "credit_data":   etf_rets,
        "macro_data":    {},   # TODO: DATA_DEPENDENCY — requires FRED/Bloomberg
        "news_data":     {
            "score":       news_score,
            "count":       news_ct,
            "macro_count": macro_ct,
            "micro_count": micro_ct,
        },
        "social_data": {},     # TODO: DATA_DEPENDENCY — requires StockTwits/Twitter/X
    }

    # Fetch put/call for options gate
    put_call, put_call_avg = fetch_put_call_ratio(ctrl.SPX_TICKER)
    if put_call is not None:
        data["options_data"]["put_call_ratio"] = put_call
        data["options_data"]["put_call_avg"]   = put_call_avg

    # Gate 1: System
    snapshot_hash    = str(hash(f"{now.strftime('%Y-%m-%d %H')}"))  # hour-level dedup
    processed_hashes = set()  # TODO: persist across runs via DB
    market_data_ok   = bool(spx_bars)

    if not gate1_system(ctrl, state, sdl, now, snapshot_hash, processed_hashes,
                        data_available=market_data_ok):
        sdl.decide(state.system_status, 0.0, f"gate1 halt: {state.system_status}")
        sdl.commit(db)
        log.warning(f"Market sentiment spine halted at gate1: {state.system_status}")
        # Still run position scan below even if market snapshot rejected
    else:
        # Gates 2-27
        gate2_input_universe(ctrl, state, sdl, data)
        gate3_benchmark(ctrl, state, sdl, spx_bars)
        gate4_price_action(ctrl, state, sdl, spx_bars, sector_rets)

        rsp_spy_spread = (
            etf_rets.get("RSP", 0.0) - etf_rets.get("SPY", 0.0)
            if etf_rets else 0.0
        )
        gate5_breadth(ctrl, state, sdl, sector_rets, rsp_spy_spread)
        gate6_volume(ctrl, state, sdl, spx_bars)
        gate7_volatility(ctrl, state, sdl, spx_bars, vix_current, vix_history)
        gate8_options(ctrl, state, sdl, data["options_data"])
        gate9_safe_haven(ctrl, state, sdl, etf_rets)
        gate10_credit(ctrl, state, sdl, etf_rets)
        gate11_sector_rotation(ctrl, state, sdl, sector_rets)
        gate12_skip_macro(ctrl, state, sdl, data["macro_data"])
        gate13_news(ctrl, state, sdl, data["news_data"])
        gate14_skip_social(ctrl, state, sdl, data["social_data"])
        gate15_breadth_price_divergence(ctrl, state, sdl, spx_bars)
        gate16_composite_construction(ctrl, state, sdl)
        gate17_weighting(ctrl, state, sdl)
        gate18_composite_score(ctrl, state, sdl)
        gate19_confidence(ctrl, state, sdl)
        gate20_regime(ctrl, state, sdl)
        gate21_divergence_warnings(ctrl, state, sdl)
        gate22_action(ctrl, state, sdl)
        gate23_risk_discounts(ctrl, state, sdl)
        gate24_persistence(ctrl, state, sdl, db)
        gate25_evaluation(ctrl, state, sdl, db)
        gate26_output(ctrl, state, sdl)
        gate27_final_signal(ctrl, state, sdl)

        sdl.decide(state.classification, state.sentiment_confidence, state.output_action)
        sdl.commit(db)

        # Write market sentiment snapshot to scan_log (ticker="MARKET")
        # so Agent 1 can read aggregate sentiment context in Gate 10
        try:
            db.log_scan(
                ticker="MARKET",
                put_call_ratio=put_call,
                put_call_avg30d=put_call_avg,
                insider_net=None,
                volume_vs_avg=None,
                seller_dominance=None,
                cascade_detected=state.benchmark_risk_state == "drawdown",
                tier=(1 if state.final_market_state in ("strong_bearish", "panic_override")
                      else 2 if state.final_market_state in ("mild_bearish",)
                      else 3 if state.final_market_state == "neutral"
                      else 4),
                event_summary=(
                    f"MARKET_SENTIMENT: {state.final_market_state} | "
                    f"regime={state.regime_state} | "
                    f"classification={state.classification} | "
                    f"confidence={state.sentiment_confidence:.2f} | "
                    f"score={state.final_sentiment_signal:.3f} | "
                    f"warning={state.warning_state}"
                ),
            )
        except Exception as e:
            log.warning(f"Market sentiment log_scan write failed (non-fatal): {e}")

        log.info(
            f"Market sentiment: {state.final_market_state} | "
            f"regime={state.regime_state} | score={state.final_sentiment_signal:.3f} | "
            f"confidence={state.sentiment_confidence:.2f} | {state.classification}"
        )

    # ── Phase 2: Per-position cascade scan ───────────────────────────────
    positions = db.get_open_positions()
    if not positions:
        log.info("No open positions to scan")
    else:
        log.info(f"Scanning {len(positions)} open position(s)")
        critical_count = 0
        elevated_count = 0

        for pos in positions:
            ticker = pos['ticker']
            log.info(f"Scanning {ticker}...")

            # Fetch all three signals
            # reuse market-wide put/call from Phase 1 — same CBOE page regardless of ticker
            pos_put_call, pos_put_call_avg = put_call, put_call_avg
            insider_data                   = fetch_sec_insider_transactions(ticker)
            volume_data                    = fetch_volume_profile(ticker, is_position=True)

            # Small delay between tickers to avoid rate limiting
            time.sleep(2)

            # Detect cascade
            tier, tier_label, cascade_detected, summary = detect_cascade(
                pos_put_call, pos_put_call_avg, insider_data, volume_data
            )

            # Deep analysis for elevated/critical positions
            analysis = None
            if tier <= 2:
                analysis = format_scan_analysis(
                    pos, pos_put_call, pos_put_call_avg, insider_data,
                    volume_data, tier, tier_label, cascade_detected, db
                )

            # Log scan result to database
            db.log_scan(
                ticker=ticker,
                put_call_ratio=pos_put_call,
                put_call_avg30d=pos_put_call_avg,
                insider_net=insider_data.get("net_dollar"),
                volume_vs_avg=volume_data.get("today_vs_avg"),
                seller_dominance=volume_data.get("seller_dominance"),
                cascade_detected=cascade_detected,
                tier=tier,
                event_summary=summary + (f" | Analysis: {analysis[:100]}" if analysis else ""),
            )

            if tier == 1:
                critical_count += 1
                log.warning(f"CRITICAL — {ticker}: {summary}")
            elif tier == 2:
                elevated_count += 1
                log.info(f"ELEVATED — {ticker}: {summary}")
            else:
                log.info(f"{tier_label} — {ticker}: normal scan")

        log.info(
            f"Position scan complete — critical={critical_count} elevated={elevated_count} "
            f"positions={len(positions)}"
        )

    # ── Phase 3: Pre-trade queue check ───────────────────────────────────
    # Dedup by ticker, fetch in parallel, serialize DB writes.
    # Each ticker's fetch (EDGAR + volume) applies to every QUEUED
    # signal for that ticker; the tier annotation still writes to each
    # individual signal row. Network IO is the dominant cost so
    # SENTIMENT_FETCH_WORKERS concurrent fetches scale linearly with
    # ticker count until we hit rate limits.
    #
    # The time budget keeps the daemon's 600s hard kill from leaving us
    # with a half-written state. Old single-threaded behavior: 268 ×
    # sleep(1) + 2 HTTP each → exceeded at ticker 119, blocking the
    # promoter. New parallel behavior with 5 workers and Alpaca-primary
    # volume: ~5× speedup, budget comfortable for 300+ tickers.
    PHASE3_BUDGET_SEC = 480
    queued_signals = db.get_queued_signals()
    if queued_signals:
        by_ticker: dict = {}
        for sig in queued_signals:
            by_ticker.setdefault(sig['ticker'], []).append(sig)
        log.info(
            f"Checking sentiment for {len(queued_signals)} queued signal(s) "
            f"across {len(by_ticker)} unique ticker(s) "
            f"(workers={SENTIMENT_FETCH_WORKERS})"
        )

        # Ticker → fetch result bundle. Populated by the thread pool.
        # Only network IO lives inside the worker — detect_cascade is
        # cheap so we leave it in the worker too so the main thread
        # sees a ready-to-write bundle.
        def _fetch_ticker_bundle(ticker):
            t0 = time.monotonic()
            insider = fetch_sec_insider_transactions(ticker, days_back=7)
            volume  = fetch_volume_profile(ticker)
            tier, label, cascade_detected, summary = detect_cascade(
                put_call, put_call_avg, insider, volume
            )
            # tier → score mapping (same as legacy single-thread path)
            score = {1: 0.10, 2: 0.35, 3: 0.60, 4: 0.85}.get(tier, 0.50)
            return {
                'insider':    insider,
                'volume':     volume,
                'tier':       tier,
                'label':      label,
                'cascade':    cascade_detected,
                'summary':    summary,
                'score':      score,
                'fetch_sec':  round(time.monotonic() - t0, 2),
            }

        phase3_start  = time.monotonic()
        processed     = 0
        stopped_early = False

        # Submit all tickers up front; process as they complete.
        # as_completed's timeout bounds the TOTAL wait, giving us the
        # soft-budget semantics we had single-threaded. On timeout we
        # break out, write a TIME_BUDGET decision-log row, and let the
        # rest of run() commit cleanly.
        pool = ThreadPoolExecutor(max_workers=SENTIMENT_FETCH_WORKERS,
                                  thread_name_prefix='pulse-fetch')
        future_to_ticker = {}
        try:
            for ticker, sigs in by_ticker.items():
                fut = pool.submit(_fetch_ticker_bundle, ticker)
                future_to_ticker[fut] = (ticker, sigs)

            try:
                for fut in as_completed(future_to_ticker,
                                        timeout=PHASE3_BUDGET_SEC):
                    ticker, sigs = future_to_ticker[fut]
                    try:
                        bundle = fut.result()
                    except Exception as e:
                        log.warning(f"Phase 3 fetch failed for {ticker}: {e}")
                        continue

                    # DB writes in the main thread — sqlite under WAL
                    # handles concurrency but keeping writes serial
                    # here keeps the per-customer DB lock contention
                    # predictable (matches other agents).
                    db.log_scan(
                        ticker=ticker,
                        put_call_ratio=put_call,
                        put_call_avg30d=put_call_avg,
                        insider_net=bundle['insider'].get('net_dollar'),
                        volume_vs_avg=bundle['volume'].get('today_vs_avg'),
                        seller_dominance=bundle['volume'].get('seller_dominance'),
                        cascade_detected=bundle['cascade'],
                        tier=bundle['tier'],
                        event_summary=f"PRE-TRADE CHECK: {bundle['summary']}",
                    )

                    if bundle['tier'] <= 2:
                        for sig in sigs:
                            log.warning(
                                f"Pre-trade warning: {ticker} (signal {sig['id']}) — "
                                f"{bundle['label']}: {bundle['summary']}"
                            )
                            db.annotate_signal_pulse(sig['id'], bundle['tier'],
                                                     bundle['summary'])

                    try:
                        db.stamp_signals_sentiment(ticker, bundle['score'])
                    except Exception as _e:
                        log.debug(f"sentiment stamp failed for {ticker}: {_e}")

                    processed += 1
            except FuturesTimeoutError:
                stopped_early = True
                log.warning(
                    f"Phase 3 time budget exceeded ({PHASE3_BUDGET_SEC}s) — "
                    f"processed {processed}/{len(by_ticker)} tickers, "
                    f"remainder left unstamped so the agent can commit cleanly"
                )
        finally:
            # Drop the pool. cancel_futures=True (Py 3.9+) drops anything
            # still queued; workers mid-HTTP can't be cancelled but will
            # exit on their own REQUEST_TIMEOUT since fetch_with_retry
            # has bounded per-call timeouts. wait=False lets the run()
            # tail continue immediately rather than blocking on stuck
            # workers.
            pool.shutdown(wait=False, cancel_futures=True)

        elapsed = time.monotonic() - phase3_start
        log.info(
            f"Phase 3 complete — {processed}/{len(by_ticker)} tickers "
            f"in {elapsed:.1f}s (parallel={SENTIMENT_FETCH_WORKERS})"
            + (" (stopped on budget)" if stopped_early else "")
        )

        # Surface budget exhaustion in the decision log so the
        # promoter's STUCK rows and the fault detector's bottleneck
        # reading both show 'sentiment time budget' rather than a
        # silent miss.
        if stopped_early:
            try:
                db.log_signal_decision(
                    agent='sentiment', action='TIME_BUDGET',
                    value=f"{processed}/{len(by_ticker)}",
                    reason=(f"budget {PHASE3_BUDGET_SEC}s exceeded after {elapsed:.0f}s; "
                            f"remaining tickers left unstamped this cycle")
                )
            except Exception as _e:
                log.debug(f"TIME_BUDGET decision-log write failed: {_e}")

    portfolio = db.get_portfolio()
    log.info(
        f"Scan complete — "
        f"positions={len(positions) if positions else 0} "
        f"portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("market_sentiment_agent", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="The Pulse",
        details=(
            f"market_state={state.final_market_state} "
            f"regime={state.regime_state} "
            f"score={state.final_sentiment_signal:.3f} "
            f"confidence={state.sentiment_confidence:.2f}"
        ),
        portfolio_value=portfolio['cash'],
    )

    # Dedicated regime-state setting for downstream consumers (notably
    # the market state aggregator, agent 9). Replaces a brittle string-
    # parse of this very AGENT_COMPLETE details line — if the format above
    # ever changed, downstream regime extraction would silently break.
    try:
        db.set_setting('_PULSE_REGIME_STATE', state.regime_state or 'indecisive')
    except Exception as _e:
        log.warning(f"_PULSE_REGIME_STATE write failed: {_e}")

    # ── SCREENING REQUEST HANDLER ──────────────────────────────────────────
    # Check for pending sentiment screening requests from the sector screener.
    # Reuses existing per-ticker scan functions (put/call, insider, volume).
    _handle_screening_requests(db)

    # Post heartbeat to monitor server
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="market_sentiment_agent", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    acquire_agent_lock("retail_market_sentiment_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
