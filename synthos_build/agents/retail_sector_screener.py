"""
sector_screener.py — Sector Screening Agent
Synthos · Screening Layer · Version 2.0

Runs once per day during the prep session (pre-market) — NOT per cycle.
Sector momentum is a multi-week signal; hourly refreshes are wasted calls.

What this agent does:
  1. Iterates all 11 S&P Select Sector SPDR ETFs (XLE, XLK, XLV, XLF,
     XLY, XLP, XLI, XLU, XLRE, XLB, XLC).
  2. Fetches 5-year return for each sector ETF.
  3. Scores each of the ETF's top-10 holdings using recent price momentum.
  4. Writes candidates to the sector_screening table in the DB.
  5. Issues screening requests so Scout (news) and Pulse (sentiment)
     enrich each candidate on their next run.
  6. Checks for congressional signals already in the DB for these tickers
     and flags them as supplemental context.
  7. Writes a human-readable audit log to logs/logic_audits/.

Holdings source: hand-curated top-10 per sector. Stable — SPDR top-10
holdings shift quarterly at most. TODO: swap to FMP /etf-holdings when
we upgrade from free tier (currently paywalled).

Usage:
  python3 retail_sector_screener.py                 # all sectors
  python3 retail_sector_screener.py --sector=Energy # one sector (dev only)

Logic audit log: logs/logic_audits/YYYY-MM-DD_sector_screener.log
"""

import os
import sys
import json
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, get_shared_db

def _master_db():
    """2026-04-27: returns the shared market-intel DB. See get_shared_db()."""
    return get_shared_db()

# ── ENVIRONMENT ───────────────────────────────────────────────────────────────
ALPACA_API_KEY  = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET   = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')
ET              = ZoneInfo("America/New_York")

LOG_DIR         = os.path.join(_ROOT_DIR, 'logs', 'logic_audits')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('sector_screener')


# ── SECTOR CONFIGURATION ──────────────────────────────────────────────────────
# Hand-curated top-10 holdings for each of the 11 S&P Select Sector SPDR ETFs.
# etf_weight_pct figures are approximate (last reviewed April 2026).
# Source: SPDR ETF fact sheets. Holdings shift slowly — review quarterly.
# TODO: replace with FMP /etf-holdings once we're on a paid tier.

SECTOR_CONFIG = {
    "Energy": {
        "etf": "XLE",
        "holdings": [
            {"ticker": "XOM",  "company": "ExxonMobil Corp",          "etf_weight_pct": 22.1},
            {"ticker": "CVX",  "company": "Chevron Corp",              "etf_weight_pct": 16.8},
            {"ticker": "COP",  "company": "ConocoPhillips",            "etf_weight_pct":  8.1},
            {"ticker": "EOG",  "company": "EOG Resources",             "etf_weight_pct":  5.2},
            {"ticker": "SLB",  "company": "SLB (Schlumberger)",        "etf_weight_pct":  4.9},
            {"ticker": "MPC",  "company": "Marathon Petroleum Corp",   "etf_weight_pct":  4.3},
            {"ticker": "PSX",  "company": "Phillips 66",               "etf_weight_pct":  4.1},
            {"ticker": "OXY",  "company": "Occidental Petroleum",      "etf_weight_pct":  3.9},
            {"ticker": "WMB",  "company": "Williams Companies",        "etf_weight_pct":  3.8},
            {"ticker": "KMI",  "company": "Kinder Morgan",             "etf_weight_pct":  3.2},
        ],
    },
    "Technology": {
        "etf": "XLK",
        "holdings": [
            {"ticker": "AAPL", "company": "Apple Inc",                 "etf_weight_pct": 14.5},
            {"ticker": "MSFT", "company": "Microsoft Corp",            "etf_weight_pct": 13.8},
            {"ticker": "NVDA", "company": "NVIDIA Corp",               "etf_weight_pct": 12.4},
            {"ticker": "AVGO", "company": "Broadcom Inc",              "etf_weight_pct":  5.2},
            {"ticker": "ORCL", "company": "Oracle Corp",               "etf_weight_pct":  2.8},
            {"ticker": "CRM",  "company": "Salesforce Inc",            "etf_weight_pct":  2.6},
            {"ticker": "ADBE", "company": "Adobe Inc",                 "etf_weight_pct":  2.1},
            {"ticker": "CSCO", "company": "Cisco Systems",             "etf_weight_pct":  2.0},
            {"ticker": "ACN",  "company": "Accenture plc",             "etf_weight_pct":  1.9},
            {"ticker": "AMD",  "company": "Advanced Micro Devices",    "etf_weight_pct":  1.8},
        ],
    },
    "Healthcare": {
        "etf": "XLV",
        "holdings": [
            {"ticker": "LLY",  "company": "Eli Lilly & Co",            "etf_weight_pct": 12.1},
            {"ticker": "UNH",  "company": "UnitedHealth Group",        "etf_weight_pct":  7.9},
            {"ticker": "JNJ",  "company": "Johnson & Johnson",         "etf_weight_pct":  7.2},
            {"ticker": "ABBV", "company": "AbbVie Inc",                "etf_weight_pct":  5.8},
            {"ticker": "MRK",  "company": "Merck & Co",                "etf_weight_pct":  4.9},
            {"ticker": "TMO",  "company": "Thermo Fisher Scientific",  "etf_weight_pct":  3.8},
            {"ticker": "ABT",  "company": "Abbott Laboratories",       "etf_weight_pct":  3.6},
            {"ticker": "PFE",  "company": "Pfizer Inc",                "etf_weight_pct":  3.4},
            {"ticker": "DHR",  "company": "Danaher Corp",              "etf_weight_pct":  3.0},
            {"ticker": "AMGN", "company": "Amgen Inc",                 "etf_weight_pct":  2.7},
        ],
    },
    "Financial Services": {
        "etf": "XLF",
        "holdings": [
            {"ticker": "BRK.B","company": "Berkshire Hathaway",        "etf_weight_pct": 13.2},
            {"ticker": "JPM",  "company": "JPMorgan Chase",            "etf_weight_pct": 10.5},
            {"ticker": "V",    "company": "Visa Inc",                  "etf_weight_pct":  7.1},
            {"ticker": "MA",   "company": "Mastercard Inc",            "etf_weight_pct":  6.3},
            {"ticker": "BAC",  "company": "Bank of America",           "etf_weight_pct":  4.4},
            {"ticker": "WFC",  "company": "Wells Fargo",               "etf_weight_pct":  3.5},
            {"ticker": "GS",   "company": "Goldman Sachs",             "etf_weight_pct":  2.9},
            {"ticker": "MS",   "company": "Morgan Stanley",            "etf_weight_pct":  2.6},
            {"ticker": "AXP",  "company": "American Express",          "etf_weight_pct":  2.4},
            {"ticker": "C",    "company": "Citigroup Inc",             "etf_weight_pct":  2.2},
        ],
    },
    "Consumer Cyclical": {
        "etf": "XLY",
        "holdings": [
            {"ticker": "AMZN", "company": "Amazon.com Inc",            "etf_weight_pct": 22.8},
            {"ticker": "TSLA", "company": "Tesla Inc",                 "etf_weight_pct": 15.2},
            {"ticker": "HD",   "company": "Home Depot Inc",            "etf_weight_pct":  7.1},
            {"ticker": "MCD",  "company": "McDonald's Corp",           "etf_weight_pct":  4.6},
            {"ticker": "LOW",  "company": "Lowe's Cos",                "etf_weight_pct":  3.4},
            {"ticker": "BKNG", "company": "Booking Holdings",          "etf_weight_pct":  3.2},
            {"ticker": "NKE",  "company": "Nike Inc",                  "etf_weight_pct":  2.8},
            {"ticker": "SBUX", "company": "Starbucks Corp",            "etf_weight_pct":  2.5},
            {"ticker": "TJX",  "company": "TJX Companies",             "etf_weight_pct":  2.3},
            {"ticker": "ABNB", "company": "Airbnb Inc",                "etf_weight_pct":  1.8},
        ],
    },
    "Consumer Defensive": {
        "etf": "XLP",
        "holdings": [
            {"ticker": "PG",   "company": "Procter & Gamble",          "etf_weight_pct": 12.4},
            {"ticker": "COST", "company": "Costco Wholesale",          "etf_weight_pct": 11.2},
            {"ticker": "WMT",  "company": "Walmart Inc",               "etf_weight_pct": 10.8},
            {"ticker": "KO",   "company": "Coca-Cola Co",              "etf_weight_pct":  9.6},
            {"ticker": "PEP",  "company": "PepsiCo Inc",               "etf_weight_pct":  8.3},
            {"ticker": "PM",   "company": "Philip Morris International","etf_weight_pct":  5.1},
            {"ticker": "MO",   "company": "Altria Group",              "etf_weight_pct":  3.9},
            {"ticker": "MDLZ", "company": "Mondelez International",    "etf_weight_pct":  3.8},
            {"ticker": "CL",   "company": "Colgate-Palmolive",         "etf_weight_pct":  2.9},
            {"ticker": "TGT",  "company": "Target Corp",               "etf_weight_pct":  2.5},
        ],
    },
    "Industrials": {
        "etf": "XLI",
        "holdings": [
            {"ticker": "GE",   "company": "GE Aerospace",              "etf_weight_pct":  4.8},
            {"ticker": "CAT",  "company": "Caterpillar Inc",           "etf_weight_pct":  4.4},
            {"ticker": "RTX",  "company": "RTX Corp",                  "etf_weight_pct":  4.1},
            {"ticker": "UBER", "company": "Uber Technologies",         "etf_weight_pct":  3.7},
            {"ticker": "HON",  "company": "Honeywell International",   "etf_weight_pct":  3.5},
            {"ticker": "UNP",  "company": "Union Pacific",             "etf_weight_pct":  3.3},
            {"ticker": "BA",   "company": "Boeing Co",                 "etf_weight_pct":  3.0},
            {"ticker": "ETN",  "company": "Eaton Corp",                "etf_weight_pct":  2.9},
            {"ticker": "LMT",  "company": "Lockheed Martin",           "etf_weight_pct":  2.6},
            {"ticker": "DE",   "company": "Deere & Co",                "etf_weight_pct":  2.5},
        ],
    },
    "Utilities": {
        "etf": "XLU",
        "holdings": [
            {"ticker": "NEE",  "company": "NextEra Energy",            "etf_weight_pct": 13.2},
            {"ticker": "SO",   "company": "Southern Co",               "etf_weight_pct":  8.4},
            {"ticker": "DUK",  "company": "Duke Energy",               "etf_weight_pct":  8.0},
            {"ticker": "CEG",  "company": "Constellation Energy",      "etf_weight_pct":  6.8},
            {"ticker": "AEP",  "company": "American Electric Power",   "etf_weight_pct":  4.8},
            {"ticker": "SRE",  "company": "Sempra",                    "etf_weight_pct":  4.4},
            {"ticker": "D",    "company": "Dominion Energy",           "etf_weight_pct":  4.2},
            {"ticker": "EXC",  "company": "Exelon Corp",               "etf_weight_pct":  3.9},
            {"ticker": "PEG",  "company": "Public Service Enterprise", "etf_weight_pct":  3.3},
            {"ticker": "XEL",  "company": "Xcel Energy",               "etf_weight_pct":  3.2},
        ],
    },
    "Real Estate": {
        "etf": "XLRE",
        "holdings": [
            {"ticker": "PLD",  "company": "Prologis Inc",              "etf_weight_pct":  9.2},
            {"ticker": "AMT",  "company": "American Tower",            "etf_weight_pct":  8.8},
            {"ticker": "EQIX", "company": "Equinix Inc",               "etf_weight_pct":  7.4},
            {"ticker": "WELL", "company": "Welltower Inc",             "etf_weight_pct":  6.8},
            {"ticker": "SPG",  "company": "Simon Property Group",      "etf_weight_pct":  5.2},
            {"ticker": "DLR",  "company": "Digital Realty Trust",      "etf_weight_pct":  5.0},
            {"ticker": "PSA",  "company": "Public Storage",            "etf_weight_pct":  4.8},
            {"ticker": "O",    "company": "Realty Income",             "etf_weight_pct":  4.4},
            {"ticker": "CCI",  "company": "Crown Castle Inc",          "etf_weight_pct":  3.8},
            {"ticker": "CBRE", "company": "CBRE Group",                "etf_weight_pct":  3.4},
        ],
    },
    "Basic Materials": {
        "etf": "XLB",
        "holdings": [
            {"ticker": "LIN",  "company": "Linde plc",                 "etf_weight_pct": 16.2},
            {"ticker": "SHW",  "company": "Sherwin-Williams",          "etf_weight_pct":  7.5},
            {"ticker": "APD",  "company": "Air Products & Chemicals",  "etf_weight_pct":  5.9},
            {"ticker": "ECL",  "company": "Ecolab Inc",                "etf_weight_pct":  5.5},
            {"ticker": "FCX",  "company": "Freeport-McMoRan",          "etf_weight_pct":  5.1},
            {"ticker": "NEM",  "company": "Newmont Corp",              "etf_weight_pct":  4.8},
            {"ticker": "NUE",  "company": "Nucor Corp",                "etf_weight_pct":  3.4},
            {"ticker": "DD",   "company": "DuPont de Nemours",         "etf_weight_pct":  3.1},
            {"ticker": "DOW",  "company": "Dow Inc",                   "etf_weight_pct":  2.9},
            {"ticker": "VMC",  "company": "Vulcan Materials",          "etf_weight_pct":  2.7},
        ],
    },
    "Communication Services": {
        "etf": "XLC",
        "holdings": [
            {"ticker": "META", "company": "Meta Platforms",            "etf_weight_pct": 22.5},
            {"ticker": "GOOGL","company": "Alphabet Inc Class A",      "etf_weight_pct": 12.8},
            {"ticker": "GOOG", "company": "Alphabet Inc Class C",      "etf_weight_pct": 10.6},
            {"ticker": "NFLX", "company": "Netflix Inc",               "etf_weight_pct":  7.4},
            {"ticker": "DIS",  "company": "Walt Disney Co",            "etf_weight_pct":  4.9},
            {"ticker": "TMUS", "company": "T-Mobile US",               "etf_weight_pct":  4.5},
            {"ticker": "VZ",   "company": "Verizon Communications",    "etf_weight_pct":  4.3},
            {"ticker": "CMCSA","company": "Comcast Corp",              "etf_weight_pct":  3.8},
            {"ticker": "T",    "company": "AT&T Inc",                  "etf_weight_pct":  3.5},
            {"ticker": "CHTR", "company": "Charter Communications",    "etf_weight_pct":  2.8},
        ],
    },
}


# ── ALPACA DATA HELPERS ───────────────────────────────────────────────────────

def _alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


# Circuit breaker on primary bar source (Alpaca). After this many
# consecutive failures the screener gives up on Alpaca for the rest
# of the run and routes every subsequent ticker through the Yahoo
# fallback. Keeps a rate-limit incident or Alpaca outage from
# chewing 70+ × timeout seconds before we notice.
_ALPACA_CIRCUIT_BREAKER_N = 3
_alpaca_consecutive_failures = 0
_alpaca_circuit_open         = False

# Retry / timeout matching the pattern in sentiment + trader.
# (connect_timeout=5, read_timeout=20) — fast-fail on DNS/SYN.
_FETCH_MAX_RETRIES = 2
_FETCH_TIMEOUT     = (5, 20)


def fetch_bars(ticker, days):
    """Fetch daily OHLCV bars for `ticker` going back `days` calendar days.

    Primary:  Alpaca Data API (IEX feed, authenticated).
    Fallback: Yahoo Finance chart API (public, unauthenticated).

    Both paths return the Alpaca bar shape: list of dicts with keys
    t/o/h/l/c/v. Yahoo's structure is flattened to match. Downstream
    callers (calc_return, calc_momentum_score) don't know which source
    provided the data.
    """
    global _alpaca_consecutive_failures, _alpaca_circuit_open

    bars = []
    if not _alpaca_circuit_open:
        bars = _fetch_bars_alpaca(ticker, days)
        if bars:
            _alpaca_consecutive_failures = 0
        else:
            _alpaca_consecutive_failures += 1
            if _alpaca_consecutive_failures >= _ALPACA_CIRCUIT_BREAKER_N:
                _alpaca_circuit_open = True
                log.warning(
                    f"[CIRCUIT] Alpaca bars breaker opened after "
                    f"{_alpaca_consecutive_failures} consecutive empties — "
                    f"remaining fetches will use Yahoo fallback"
                )

    if not bars:
        # Either primary failed or circuit is open. Try the free Yahoo
        # chart API — same data, stable-ish shape, no auth required.
        bars = _fetch_bars_yahoo(ticker, days)
        if bars:
            log.info(f"fetch_bars({ticker}): Yahoo fallback filled in {len(bars)} bars")

    return bars


def _fetch_bars_alpaca(ticker, days):
    """Primary Alpaca path, with retry + tuple timeout. Returns [] on
    permanent failure (so the caller can try Yahoo)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    end   = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    start = (now_utc - timedelta(days=days + 10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url   = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars"
    params = {"timeframe": "1Day", "start": start, "end": end,
              "limit": days + 20, "feed": "iex"}
    status = None
    for attempt in range(_FETCH_MAX_RETRIES):
        try:
            r = requests.get(url, params=params,
                             headers=_alpaca_headers(), timeout=_FETCH_TIMEOUT)
            status = r.status_code
            if r.status_code == 200:
                try:
                    _master_db().log_api_call(
                        'sector_screener',
                        f'/v2/stocks/{ticker}/bars',
                        'GET', 'alpaca_data', status_code=status)
                except Exception as _e:
                    log.debug(f"api_call log failed for alpaca {ticker}: {_e}")
                return r.json().get("bars", []) or []
            # 429 rate limit / 5xx → retry with backoff
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                if attempt < _FETCH_MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
            break
        except Exception as e:
            log.debug(f"_fetch_bars_alpaca({ticker}) attempt {attempt + 1}: {e}")
            if attempt < _FETCH_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
    log.warning(f"Alpaca bars {ticker}: exhausted retries (last status={status})")
    try:
        _master_db().log_api_call(
            'sector_screener', f'/v2/stocks/{ticker}/bars',
            'GET', 'alpaca_data', status_code=status)
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    return []


# Yahoo range keyword mapping — API accepts symbolic ranges rather
# than absolute dates. Picking the smallest range that covers `days`
# keeps payload size sane.
def _yahoo_range_for_days(days):
    if days <=   7: return "5d"
    if days <=  32: return "1mo"
    if days <=  95: return "3mo"
    if days <= 190: return "6mo"
    if days <= 400: return "1y"
    if days <= 760: return "2y"
    if days <=1830: return "5y"
    return "max"


def _fetch_bars_yahoo(ticker, days):
    """Yahoo Finance chart API fallback.

    Public endpoint, no auth required; needs a browser-ish User-Agent
    or Yahoo will 401 us. Returns [] on any failure. Response shape
    is flattened into the Alpaca bar dict format so downstream
    consumers don't care which source won.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": _yahoo_range_for_days(days)}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Synthos/1.0)"}
    status = None
    for attempt in range(_FETCH_MAX_RETRIES):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=_FETCH_TIMEOUT)
            status = r.status_code
            if r.status_code == 200:
                break
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                if attempt < _FETCH_MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
            return []
        except Exception as e:
            log.debug(f"_fetch_bars_yahoo({ticker}) attempt {attempt + 1}: {e}")
            if attempt < _FETCH_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return []

    try:
        _master_db().log_api_call(
            'sector_screener', f'/v8/finance/chart/{ticker}',
            'GET', 'yahoo', status_code=status)
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")

    try:
        data = r.json()
        results = data.get("chart", {}).get("result") or []
        if not results:
            return []
        first = results[0]
        timestamps = first.get("timestamp") or []
        quote = (first.get("indicators", {}).get("quote") or [{}])[0]
        opens  = quote.get("open")   or []
        highs  = quote.get("high")   or []
        lows   = quote.get("low")    or []
        closes = quote.get("close")  or []
        vols   = quote.get("volume") or []

        bars = []
        for i, ts in enumerate(timestamps):
            # Yahoo sometimes returns None for individual fields on
            # partial-session bars (e.g. mid-day volume). Skip rather
            # than poison downstream math.
            try:
                c = closes[i]
                if c is None:
                    continue
                bar_t = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%dT%H:%M:%SZ')
                bars.append({
                    "t": bar_t,
                    "o": opens[i]  if i < len(opens)  and opens[i]  is not None else c,
                    "h": highs[i]  if i < len(highs)  and highs[i]  is not None else c,
                    "l": lows[i]   if i < len(lows)   and lows[i]   is not None else c,
                    "c": c,
                    "v": vols[i]   if i < len(vols)   and vols[i]   is not None else 0,
                })
            except (IndexError, TypeError):
                continue
        return bars
    except Exception as e:
        log.warning(f"_fetch_bars_yahoo({ticker}): parse error {e}")
        return []


def calc_return(bars):
    """Return decimal price return between first and last bar close. None if insufficient data."""
    closes = [b["c"] for b in bars if "c" in b]
    if len(closes) < 2:
        return None
    return round((closes[-1] - closes[0]) / closes[0], 4)


def calc_momentum_score(bars):
    """
    Simple momentum score 0.0-1.0 based on:
      - 3-month price return (weight 50%)
      - 20-day vs 50-day SMA relationship (weight 30%)
      - Recent volume trend (weight 20%)

    Returns (score, ret_3m, reasoning_text).  ret_3m is the raw
    3-month price return as a decimal (e.g. 0.124 = +12.4%) — caller
    persists it to sector_screening.ret_3m so the screener page can
    show actual % change instead of just the composite score.  None
    when fewer than 63 bars are available.
    """
    closes = [b["c"] for b in bars if "c" in b]
    volumes = [b["v"] for b in bars if "v" in b]
    reasoning = []

    if len(closes) < 60:
        return 0.5, None, "Insufficient price history — defaulting to neutral score."

    # 3-month return
    ret_3m = (closes[-1] - closes[-63]) / closes[-63] if len(closes) >= 63 else 0.0
    if ret_3m > 0.10:
        ret_score = 1.0
        reasoning.append(f"3-month return +{ret_3m:.1%} (strong positive momentum)")
    elif ret_3m > 0.02:
        ret_score = 0.7
        reasoning.append(f"3-month return +{ret_3m:.1%} (mild positive momentum)")
    elif ret_3m > -0.02:
        ret_score = 0.5
        reasoning.append(f"3-month return {ret_3m:.1%} (flat)")
    elif ret_3m > -0.10:
        ret_score = 0.3
        reasoning.append(f"3-month return {ret_3m:.1%} (mild negative momentum)")
    else:
        ret_score = 0.1
        reasoning.append(f"3-month return {ret_3m:.1%} (weak — significant price decline)")

    # SMA relationship
    sma_20 = sum(closes[-20:]) / 20
    sma_50 = sum(closes[-50:]) / 50
    if closes[-1] > sma_20 > sma_50:
        sma_score = 1.0
        reasoning.append("Price above 20-day and 50-day moving averages (uptrend confirmed)")
    elif closes[-1] > sma_50:
        sma_score = 0.6
        reasoning.append("Price above 50-day MA but below 20-day MA (mixed trend)")
    elif closes[-1] > sma_20:
        sma_score = 0.5
        reasoning.append("Price above 20-day MA but below 50-day MA (short-term bounce only)")
    else:
        sma_score = 0.2
        reasoning.append("Price below both moving averages (downtrend)")

    # Volume trend: recent 10-day avg vs 30-day avg
    vol_score = 0.5
    if len(volumes) >= 30:
        avg_10 = sum(volumes[-10:]) / 10
        avg_30 = sum(volumes[-30:]) / 30
        ratio  = avg_10 / avg_30 if avg_30 > 0 else 1.0
        if ratio > 1.20:
            vol_score = 1.0
            reasoning.append(f"Volume 20% above 30-day average (institutional interest)")
        elif ratio > 1.05:
            vol_score = 0.7
            reasoning.append(f"Volume slightly above average (normal activity)")
        elif ratio > 0.80:
            vol_score = 0.5
            reasoning.append(f"Volume near average (no unusual activity)")
        else:
            vol_score = 0.3
            reasoning.append(f"Volume below average (fading interest)")

    score = round(ret_score * 0.50 + sma_score * 0.30 + vol_score * 0.20, 4)
    return score, round(ret_3m, 4), " | ".join(reasoning)


# ── CONGRESSIONAL SIGNAL CHECK ────────────────────────────────────────────────

def check_congressional_signals(db, tickers):
    """
    Look for recent congressional buy/sell signals in the existing signals table
    for any of our screened tickers.
    Returns dict: {ticker: 'recent_buy' | 'recent_sell' | 'none'}
    """
    cutoff = (datetime.now(ET) - timedelta(days=90)).strftime('%Y-%m-%d')
    results = {}
    with db.conn() as c:
        for ticker in tickers:
            row = c.execute("""
                SELECT transaction_type, disc_date FROM signals
                WHERE ticker=? AND disc_date >= ?
                ORDER BY disc_date DESC LIMIT 1
            """, (ticker, cutoff)).fetchone()
            if row:
                tx = (row['transaction_type'] or '').lower()
                if 'buy' in tx or 'purchase' in tx:
                    results[ticker] = 'recent_buy'
                elif 'sell' in tx or 'sale' in tx:
                    results[ticker] = 'recent_sell'
                else:
                    results[ticker] = 'none'
            else:
                results[ticker] = 'none'
    return results


# ── AUDIT LOG ─────────────────────────────────────────────────────────────────

def write_audit_log(run_id, sector, etf, etf_5yr_return, candidates,
                    momentum_details, congressional_flags):
    """Write a human-readable audit log for this screener run."""
    today = datetime.now(ET).strftime('%Y-%m-%d')
    log_path = os.path.join(LOG_DIR, f"{today}_sector_screener.log")

    lines = [
        "=" * 70,
        "SECTOR SCREENER — LOGIC AUDIT LOG",
        f"Run ID   : {run_id}",
        f"Sector   : {sector}",
        f"ETF      : {etf}",
        f"Date     : {today}",
        "=" * 70,
        "",
        f"SECTOR PERFORMANCE",
        f"  {etf} 5-Year Return: {etf_5yr_return:+.1%}" if etf_5yr_return is not None
            else f"  {etf} 5-Year Return: unavailable",
        "",
        "CANDIDATES UNDER REVIEW (ranked by momentum score)",
        "-" * 70,
    ]

    for i, cd in enumerate(candidates, 1):
        ticker   = cd['ticker']
        company  = cd.get('company', '')
        weight   = cd.get('etf_weight_pct', 0.0)
        detail   = momentum_details.get(ticker, {})
        score    = detail.get('score', 0.0)
        reason   = detail.get('reasoning', 'No data')
        cong     = congressional_flags.get(ticker, 'none')
        cong_str = {
            'recent_buy':  '  [CONGRESSIONAL] Recent BUY signal in last 90 days — supplemental bullish',
            'recent_sell': '  [CONGRESSIONAL] Recent SELL signal in last 90 days — supplemental bearish',
            'none':        '  [CONGRESSIONAL] No recent congressional activity',
        }.get(cong, '  [CONGRESSIONAL] Unknown')

        lines += [
            f"  {i:2}. {ticker:<5} — {company}",
            f"      ETF Weight   : {weight:.1f}%",
            f"      Momentum     : {score:.2f} / 1.00",
            f"      Reasoning    : {reason}",
            cong_str,
            "",
        ]

    lines += [
        "SCREENING REQUESTS ISSUED",
        "-" * 70,
        "  Scout  (news agent) : requested news signal for all 10 candidates",
        "  Pulse  (sentiment)  : requested sentiment score for all 10 candidates",
        "  Bolt   (trader)     : will receive congressional flags as supplemental context",
        "",
        "Next step: Scout and Pulse will fill in their signals on next run.",
        "Combined scores will update in real time as signals arrive.",
        "Portal — Screening tab shows live status of all candidates.",
        "=" * 70,
        "",
    ]

    with open(log_path, 'a') as f:
        f.write('\n'.join(lines) + '\n')

    log.info(f"Audit log written: {log_path}")


# ── MAIN RUN ──────────────────────────────────────────────────────────────────

def run_all_sectors():
    """Run the sector screener across every configured sector. One shared
    run_id for the whole sweep so the portal can show 'as of T' uniformly.

    Returns the list of (sector, etf_5yr_return, top_candidate) tuples
    for summary logging."""
    run_id = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%S')
    summary = []
    total_sectors = len(SECTOR_CONFIG)
    log.info(f"Sector Screener starting — sweeping {total_sectors} sectors "
             f"(run_id={run_id})")

    for idx, sector in enumerate(SECTOR_CONFIG.keys(), 1):
        log.info(f"── [{idx}/{total_sectors}] {sector} ──")
        try:
            result = run_single(sector, run_id=run_id)
            if result:
                summary.append(result)
        except Exception as e:
            log.error(f"  sector {sector} failed: {e}", exc_info=True)

    # Summary log — sorted by ETF 5yr return so the best-performing sector
    # surfaces at the top of the audit output.
    log.info("=" * 60)
    log.info("Sector Screener — cross-sector summary (by 5yr return):")
    for sector, ret_5y, top in sorted(summary, key=lambda x: -(x[1] or 0)):
        ret_str = f"{ret_5y:+.1%}" if ret_5y is not None else "n/a"
        top_str = f"{top['ticker']} ({top['momentum_score']:.2f})" if top else "—"
        log.info(f"  {sector:24s} 5yr={ret_str:>8s}  top={top_str}")
    log.info("=" * 60)

    # ── BLANKET BASELINE STAMP ────────────────────────────────────────
    # The per-sector stamp above only covers tickers that appear in a
    # sector's top-N momentum candidates. Most news signals land on
    # tickers that are NOT in any top-N (80%+ sparse). Without a
    # sector-baseline fallback the screener stamp is effectively
    # missing from most validated signals — a cliff if the trader ever
    # graduates screener_evaluated_at to a promotion requirement.
    #
    # Baseline formula: map each sector's ETF 5yr return to a 0-1
    # score. Strong sectors (ETF > +50%) score 0.7; negative sectors
    # score 0.3; around-flat sectors score 0.5. Gives downstream a
    # usable per-sector signal without implying we actually evaluated
    # each ticker individually.
    def _etf_return_to_baseline(ret):
        if ret is None:
            return 0.5
        if ret >=  0.80: return 0.75
        if ret >=  0.40: return 0.65
        if ret >=  0.15: return 0.55
        if ret >= -0.10: return 0.50
        if ret >= -0.30: return 0.40
        return 0.30

    sector_baselines = {
        sector: _etf_return_to_baseline(ret_5y)
        for sector, ret_5y, _top in summary
    }
    if sector_baselines:
        try:
            n = _master_db().stamp_signals_screener_baseline(sector_baselines)
            if n:
                log.info(f"Baseline stamp: {n} in-flight signal(s) stamped with "
                         f"sector-baseline scores (across {len(sector_baselines)} sectors)")
        except Exception as e:
            log.warning(f"Baseline screener stamp failed (non-fatal): {e}")

    # Post a heartbeat so the fault detector's GATE1_LIVENESS check can see
    # us. The screener runs once per day, so the EXPECTED_AGENTS entry has a
    # 30-hour staleness window — one heartbeat per pre-market run is enough.
    try:
        _master_db().log_heartbeat("sector_screener", "OK")
    except Exception as _e:
        log.debug(f"sector_screener heartbeat write failed: {_e}")


def run_single(sector, run_id=None):
    """Screen one sector. Returns (sector, etf_5yr_return, top_candidate)
    or None if the sector config is missing."""
    config = SECTOR_CONFIG.get(sector)
    if not config:
        log.error(f"No configuration found for sector '{sector}'")
        return None
    if run_id is None:
        run_id = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%S')

    etf      = config['etf']
    holdings = config['holdings']

    log.info(f"  {sector} / {etf} — scoring {len(holdings)} holdings")

    # Step 1: Fetch ETF 5-year return
    log.info(f"Fetching {etf} 5-year price history...")
    etf_bars      = fetch_bars(etf, days=1825)  # ~5 years
    etf_5yr_return = calc_return(etf_bars)
    if etf_5yr_return is not None:
        log.info(f"{etf} 5-year return: {etf_5yr_return:+.1%}")
    else:
        log.warning(f"Could not compute {etf} 5-year return — insufficient data")

    # Step 2: Score each holding by momentum
    log.info(f"Scoring {len(holdings)} {etf} holdings...")
    momentum_details = {}
    scored_candidates = []

    for holding in holdings:
        ticker = holding['ticker']
        bars   = fetch_bars(ticker, days=260)  # ~1 year of trading days
        score, ret_3m, reasoning = calc_momentum_score(bars)
        momentum_details[ticker] = {"score": score, "ret_3m": ret_3m, "reasoning": reasoning}
        # 2026-04-29 — capture the last 30 daily closes for the screener
        # page sparkline. Bars are already in hand from the momentum
        # fetch above; pulling closes is free. Round to 2dp to keep the
        # JSON payload tight (~30 floats × ~110 tickers).
        recent_closes = []
        try:
            closes_all = [b["c"] for b in (bars or []) if "c" in b]
            recent_closes = [round(c, 2) for c in closes_all[-30:]]
        except Exception:
            recent_closes = []
        price_history_json = json.dumps(recent_closes) if recent_closes else None
        scored_candidates.append({
            **holding,
            "momentum_score":  score,
            "ret_3m":          ret_3m,
            "price_history":   price_history_json,
        })
        if ret_3m is not None:
            log.info(f"  {ticker}: momentum score {score:.2f}  (3m: {ret_3m*100:+.1f}%)")
        else:
            log.info(f"  {ticker}: momentum score {score:.2f}  (insufficient history)")

    # Sort by momentum score descending
    scored_candidates.sort(key=lambda x: x['momentum_score'], reverse=True)

    # Step 3: Check congressional signals (supplemental)
    tickers = [cd['ticker'] for cd in scored_candidates]
    log.info("Checking congressional signals for all candidates...")
    congressional_flags = check_congressional_signals(_master_db(), tickers)
    flagged = [t for t, f in congressional_flags.items() if f != 'none']
    if flagged:
        log.info(f"Congressional activity found: {flagged}")
    else:
        log.info("No recent congressional activity for these tickers")

    # Step 4: Write to DB
    log.info("Writing candidates to sector_screening table...")
    db = _master_db()
    db.write_screening_run(run_id, sector, etf, etf_5yr_return, scored_candidates)

    # Stamp any in-flight signals for these candidate tickers with
    # their individual momentum score. Trader reads screener_score
    # (0.0-1.0) as a weighted Gate 5 input rather than the old
    # boolean bonus, so the actual momentum number matters here.
    ticker_scores = {cd['ticker']: cd['momentum_score']
                     for cd in scored_candidates}
    try:
        stamped = db.stamp_signals_screener(ticker_scores)
        if stamped:
            log.info(f"  {sector}: stamped {stamped} in-flight signal(s) "
                     f"with per-ticker momentum scores")
    except Exception as _e:
        log.debug(f"screener stamp failed: {_e}")

    # Step 5: Write congressional flags
    for ticker, flag in congressional_flags.items():
        if flag != 'none':
            db.flag_congressional_screening(ticker, flag)

    # Step 6: Write audit log
    write_audit_log(run_id, sector, etf, etf_5yr_return, scored_candidates,
                    momentum_details, congressional_flags)

    db.log_event(
        "SECTOR_SCREENER_RUN",
        agent="sector_screener",
        details=f"sector={sector} etf={etf} candidates={len(scored_candidates)} "
                f"etf_5yr={etf_5yr_return:+.1%}" if etf_5yr_return else
                f"sector={sector} etf={etf} candidates={len(scored_candidates)}",
    )

    top = scored_candidates[0] if scored_candidates else None
    log.info(f"  {sector} done — {len(scored_candidates)} candidates, "
             f"top: {top['ticker'] if top else '—'}")
    return (sector, etf_5yr_return, top)


# Backward-compatible alias (older callers still expect run())
def run(sector=None):
    """If sector is provided, run just that one; else run all configured sectors."""
    if sector and sector in SECTOR_CONFIG:
        run_single(sector)
    else:
        run_all_sectors()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Synthos Sector Screener')
    parser.add_argument('--sector', default=None,
                        help='Restrict to a single sector (default: sweep all 11)')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (passed by scheduler — screener is shared, value ignored)')
    args, _ = parser.parse_known_args()
    if args.sector:
        run_single(args.sector)
    else:
        run_all_sectors()
