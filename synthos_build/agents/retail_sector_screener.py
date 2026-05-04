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
# Top-10 holdings for each of the 11 S&P Select Sector SPDR ETFs.
# Refresh source: SSGA holdings xlsx (canonical). Generate updates with
#   tools/refresh_etf_holdings.py and paste the proposed block here.
# Last refreshed: 2026-05-01 (via refresh_etf_holdings.py).
# Note: SSGA labels XLV as "Health Care"; we keep "Healthcare" (one word) for
# readability — sector keys here are the canonical names used across the system.

SECTOR_CONFIG = {
    "Energy": {
        "etf": "XLE",
        "holdings": [
            {"ticker": "XOM", "company": "EXXON MOBIL CORP", "etf_weight_pct": 22.21},
            {"ticker": "CVX", "company": "CHEVRON CORP", "etf_weight_pct": 16.63},
            {"ticker": "COP", "company": "CONOCOPHILLIPS", "etf_weight_pct":  7.07},
            {"ticker": "SLB", "company": "SLB LTD", "etf_weight_pct":  4.63},
            {"ticker": "WMB", "company": "WILLIAMS COS INC", "etf_weight_pct":  4.38},
            {"ticker": "VLO", "company": "VALERO ENERGY CORP", "etf_weight_pct":  4.19},
            {"ticker": "EOG", "company": "EOG RESOURCES INC", "etf_weight_pct":  4.15},
            {"ticker": "MPC", "company": "MARATHON PETROLEUM CORP", "etf_weight_pct":  3.98},
            {"ticker": "PSX", "company": "PHILLIPS 66", "etf_weight_pct":  3.93},
            {"ticker": "BKR", "company": "BAKER HUGHES CO", "etf_weight_pct":  3.75},
        ],
    },
    "Technology": {
        "etf": "XLK",
        "holdings": [
            {"ticker": "NVDA", "company": "NVIDIA CORP", "etf_weight_pct": 14.80},
            {"ticker": "AAPL", "company": "APPLE INC", "etf_weight_pct": 12.15},
            {"ticker": "MSFT", "company": "MICROSOFT CORP", "etf_weight_pct":  9.24},
            {"ticker": "AVGO", "company": "BROADCOM INC", "etf_weight_pct":  6.04},
            {"ticker": "MU", "company": "MICRON TECHNOLOGY INC", "etf_weight_pct":  4.33},
            {"ticker": "AMD", "company": "ADVANCED MICRO DEVICES", "etf_weight_pct":  4.29},
            {"ticker": "INTC", "company": "INTEL CORP", "etf_weight_pct":  3.30},
            {"ticker": "CSCO", "company": "CISCO SYSTEMS INC", "etf_weight_pct":  2.69},
            {"ticker": "LRCX", "company": "LAM RESEARCH CORP", "etf_weight_pct":  2.39},
            {"ticker": "PLTR", "company": "PALANTIR TECHNOLOGIES INC A", "etf_weight_pct":  2.36},
        ],
    },
    "Financials": {
        "etf": "XLF",
        "holdings": [
            {"ticker": "BRK.B", "company": "BERKSHIRE HATHAWAY INC CL B", "etf_weight_pct": 11.67},
            {"ticker": "JPM", "company": "JPMORGAN CHASE + CO", "etf_weight_pct": 11.35},
            {"ticker": "V", "company": "VISA INC CLASS A SHARES", "etf_weight_pct":  7.45},
            {"ticker": "MA", "company": "MASTERCARD INC   A", "etf_weight_pct":  5.51},
            {"ticker": "BAC", "company": "BANK OF AMERICA CORP", "etf_weight_pct":  4.77},
            {"ticker": "GS", "company": "GOLDMAN SACHS GROUP INC", "etf_weight_pct":  3.72},
            {"ticker": "WFC", "company": "WELLS FARGO + CO", "etf_weight_pct":  3.42},
            {"ticker": "MS", "company": "MORGAN STANLEY", "etf_weight_pct":  3.08},
            {"ticker": "C", "company": "CITIGROUP INC", "etf_weight_pct":  3.01},
            {"ticker": "AXP", "company": "AMERICAN EXPRESS CO", "etf_weight_pct":  2.33},
        ],
    },
    "Healthcare": {
        "etf": "XLV",
        "holdings": [
            {"ticker": "LLY", "company": "ELI LILLY + CO", "etf_weight_pct": 14.09},
            {"ticker": "JNJ", "company": "JOHNSON + JOHNSON", "etf_weight_pct": 10.54},
            {"ticker": "ABBV", "company": "ABBVIE INC", "etf_weight_pct":  7.10},
            {"ticker": "UNH", "company": "UNITEDHEALTH GROUP INC", "etf_weight_pct":  6.38},
            {"ticker": "MRK", "company": "MERCK + CO. INC.", "etf_weight_pct":  5.15},
            {"ticker": "AMGN", "company": "AMGEN INC", "etf_weight_pct":  3.55},
            {"ticker": "TMO", "company": "THERMO FISHER SCIENTIFIC INC", "etf_weight_pct":  3.42},
            {"ticker": "ISRG", "company": "INTUITIVE SURGICAL INC", "etf_weight_pct":  3.09},
            {"ticker": "GILD", "company": "GILEAD SCIENCES INC", "etf_weight_pct":  3.09},
            {"ticker": "ABT", "company": "ABBOTT LABORATORIES", "etf_weight_pct":  3.00},
        ],
    },
    "Industrials": {
        "etf": "XLI",
        "holdings": [
            {"ticker": "CAT", "company": "CATERPILLAR INC", "etf_weight_pct":  7.61},
            {"ticker": "GE", "company": "GENERAL ELECTRIC", "etf_weight_pct":  5.59},
            {"ticker": "GEV", "company": "GE VERNOVA INC", "etf_weight_pct":  5.36},
            {"ticker": "RTX", "company": "RTX CORP", "etf_weight_pct":  4.34},
            {"ticker": "BA", "company": "BOEING CO/THE", "etf_weight_pct":  3.30},
            {"ticker": "ETN", "company": "EATON CORP PLC", "etf_weight_pct":  3.09},
            {"ticker": "UNP", "company": "UNION PACIFIC CORP", "etf_weight_pct":  2.94},
            {"ticker": "UBER", "company": "UBER TECHNOLOGIES INC", "etf_weight_pct":  2.82},
            {"ticker": "DE", "company": "DEERE + CO", "etf_weight_pct":  2.73},
            {"ticker": "HON", "company": "HONEYWELL INTERNATIONAL INC", "etf_weight_pct":  2.50},
        ],
    },
    "Consumer Discretionary": {
        "etf": "XLY",
        "holdings": [
            {"ticker": "AMZN", "company": "AMAZON.COM INC", "etf_weight_pct": 27.60},
            {"ticker": "TSLA", "company": "TESLA INC", "etf_weight_pct": 17.95},
            {"ticker": "HD", "company": "HOME DEPOT INC", "etf_weight_pct":  5.47},
            {"ticker": "TJX", "company": "TJX COMPANIES INC", "etf_weight_pct":  3.99},
            {"ticker": "MCD", "company": "MCDONALD S CORP", "etf_weight_pct":  3.91},
            {"ticker": "BKNG", "company": "BOOKING HOLDINGS INC", "etf_weight_pct":  3.11},
            {"ticker": "LOW", "company": "LOWE S COS INC", "etf_weight_pct":  3.07},
            {"ticker": "SBUX", "company": "STARBUCKS CORP", "etf_weight_pct":  2.75},
            {"ticker": "ORLY", "company": "O REILLY AUTOMOTIVE INC", "etf_weight_pct":  1.92},
            {"ticker": "MAR", "company": "MARRIOTT INTERNATIONAL  CL A", "etf_weight_pct":  1.82},
        ],
    },
    "Consumer Staples": {
        "etf": "XLP",
        "holdings": [
            {"ticker": "WMT", "company": "WALMART INC", "etf_weight_pct": 12.16},
            {"ticker": "COST", "company": "COSTCO WHOLESALE CORP", "etf_weight_pct":  9.46},
            {"ticker": "PG", "company": "PROCTER + GAMBLE CO/THE", "etf_weight_pct":  7.19},
            {"ticker": "KO", "company": "COCA COLA CO/THE", "etf_weight_pct":  6.41},
            {"ticker": "PM", "company": "PHILIP MORRIS INTERNATIONAL", "etf_weight_pct":  5.40},
            {"ticker": "MDLZ", "company": "MONDELEZ INTERNATIONAL INC A", "etf_weight_pct":  4.94},
            {"ticker": "MO", "company": "ALTRIA GROUP INC", "etf_weight_pct":  4.88},
            {"ticker": "PEP", "company": "PEPSICO INC", "etf_weight_pct":  4.56},
            {"ticker": "CL", "company": "COLGATE PALMOLIVE CO", "etf_weight_pct":  4.24},
            {"ticker": "TGT", "company": "TARGET CORP", "etf_weight_pct":  3.84},
        ],
    },
    "Materials": {
        "etf": "XLB",
        "holdings": [
            {"ticker": "LIN", "company": "LINDE PLC", "etf_weight_pct": 14.16},
            {"ticker": "NEM", "company": "NEWMONT CORP", "etf_weight_pct":  7.34},
            {"ticker": "NUE", "company": "NUCOR CORP", "etf_weight_pct":  5.69},
            {"ticker": "FCX", "company": "FREEPORT MCMORAN INC", "etf_weight_pct":  5.03},
            {"ticker": "CRH", "company": "CRH PLC", "etf_weight_pct":  4.94},
            {"ticker": "VMC", "company": "VULCAN MATERIALS CO", "etf_weight_pct":  4.86},
            {"ticker": "APD", "company": "AIR PRODUCTS + CHEMICALS INC", "etf_weight_pct":  4.69},
            {"ticker": "MLM", "company": "MARTIN MARIETTA MATERIALS", "etf_weight_pct":  4.49},
            {"ticker": "SHW", "company": "SHERWIN WILLIAMS CO/THE", "etf_weight_pct":  4.49},
            {"ticker": "CTVA", "company": "CORTEVA INC", "etf_weight_pct":  4.47},
        ],
    },
    "Utilities": {
        "etf": "XLU",
        "holdings": [
            {"ticker": "NEE", "company": "NEXTERA ENERGY INC", "etf_weight_pct": 14.03},
            {"ticker": "SO", "company": "SOUTHERN CO/THE", "etf_weight_pct":  7.33},
            {"ticker": "DUK", "company": "DUKE ENERGY CORP", "etf_weight_pct":  6.93},
            {"ticker": "CEG", "company": "CONSTELLATION ENERGY", "etf_weight_pct":  6.71},
            {"ticker": "AEP", "company": "AMERICAN ELECTRIC POWER", "etf_weight_pct":  5.10},
            {"ticker": "SRE", "company": "SEMPRA", "etf_weight_pct":  4.27},
            {"ticker": "D", "company": "DOMINION ENERGY INC", "etf_weight_pct":  3.79},
            {"ticker": "ETR", "company": "ENTERGY CORP", "etf_weight_pct":  3.65},
            {"ticker": "VST", "company": "VISTRA CORP", "etf_weight_pct":  3.46},
            {"ticker": "XEL", "company": "XCEL ENERGY INC", "etf_weight_pct":  3.38},
        ],
    },
    "Real Estate": {
        "etf": "XLRE",
        "holdings": [
            {"ticker": "WELL", "company": "WELLTOWER INC", "etf_weight_pct": 10.34},
            {"ticker": "PLD", "company": "PROLOGIS INC", "etf_weight_pct":  9.00},
            {"ticker": "EQIX", "company": "EQUINIX INC", "etf_weight_pct":  7.25},
            {"ticker": "AMT", "company": "AMERICAN TOWER CORP", "etf_weight_pct":  5.83},
            {"ticker": "DLR", "company": "DIGITAL REALTY TRUST INC", "etf_weight_pct":  4.77},
            {"ticker": "SPG", "company": "SIMON PROPERTY GROUP INC", "etf_weight_pct":  4.61},
            {"ticker": "VTR", "company": "VENTAS INC", "etf_weight_pct":  4.37},
            {"ticker": "CBRE", "company": "CBRE GROUP INC   A", "etf_weight_pct":  4.34},
            {"ticker": "PSA", "company": "PUBLIC STORAGE", "etf_weight_pct":  4.32},
            {"ticker": "O", "company": "REALTY INCOME CORP", "etf_weight_pct":  4.26},
        ],
    },
    "Communication Services": {
        "etf": "XLC",
        "holdings": [
            {"ticker": "META", "company": "META PLATFORMS INC CLASS A", "etf_weight_pct": 13.51},
            {"ticker": "GOOGL", "company": "ALPHABET INC CL A", "etf_weight_pct":  9.90},
            {"ticker": "GOOG", "company": "ALPHABET INC CL C", "etf_weight_pct":  7.89},
            {"ticker": "TTWO", "company": "TAKE TWO INTERACTIVE SOFTWRE", "etf_weight_pct":  4.63},
            {"ticker": "DIS", "company": "WALT DISNEY CO/THE", "etf_weight_pct":  4.60},
            {"ticker": "LYV", "company": "LIVE NATION ENTERTAINMENT IN", "etf_weight_pct":  4.44},
            {"ticker": "SATS", "company": "ECHOSTAR CORP A", "etf_weight_pct":  4.38},
            {"ticker": "OMC", "company": "OMNICOM GROUP", "etf_weight_pct":  4.26},
            {"ticker": "NFLX", "company": "NETFLIX INC", "etf_weight_pct":  4.21},
            {"ticker": "EA", "company": "ELECTRONIC ARTS INC", "etf_weight_pct":  4.18},
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


def check_liquidity_floor(bars, min_avg_dollar_volume: float = 1_000_000.0):
    """
    G2 — liquidity floor (added 2026-04-30).

    Rejects tickers whose 30-day average daily dollar volume falls below
    `min_avg_dollar_volume`. Returns (passed: bool, avg_dollar_vol: float,
    reason: str). Defensive — at the current hand-curated universe of
    top-10 SPDR sector ETF holdings, every name is large-cap and clears
    this floor by orders of magnitude. The check matters whenever the
    universe expands (small-cap inclusion, new sectors, FMP-driven
    auto-refresh, etc.) so an illiquid name doesn't silently get
    momentum-scored.

    Default floor is $1M/day — set deliberately low so the current
    universe never trips it, while still excluding genuinely illiquid
    names. Override via env MIN_DOLLAR_VOLUME if you want a stricter
    floor. Floor of $0 disables the gate (escape hatch for testing).
    """
    if min_avg_dollar_volume <= 0:
        return True, 0.0, "liquidity gate disabled"
    closes  = [b["c"] for b in bars if "c" in b]
    volumes = [b["v"] for b in bars if "v" in b]
    if len(closes) < 30 or len(volumes) < 30:
        # Insufficient history — fail loud rather than silently passing.
        return False, 0.0, "insufficient history (<30 days) for liquidity check"
    # Average daily dollar volume = avg(close * volume) over last 30 days.
    dollar_vols = [closes[i] * volumes[i] for i in range(len(closes) - 30, len(closes))
                   if i < len(volumes)]
    avg = sum(dollar_vols) / max(len(dollar_vols), 1)
    if avg < min_avg_dollar_volume:
        return False, avg, f"avg daily dollar volume ${avg:,.0f} < ${min_avg_dollar_volume:,.0f} floor"
    return True, avg, f"avg daily dollar volume ${avg:,.0f}"


def _sigmoid(x: float, k: float = 1.0) -> float:
    """Numerically stable logistic. Centers at 0.5 when x=0; saturates
    smoothly at 0/1 as |x*k| grows. k controls steepness."""
    import math
    z = max(min(k * x, 50.0), -50.0)  # avoid overflow
    return 1.0 / (1.0 + math.exp(-z))


def calc_momentum_score(bars):
    """
    Momentum score 0.0-1.0 based on:
      - 3-month price return            (weight 50%)
      - SMA structure (price/20d/50d)   (weight 30%)
      - Recent volume trend             (weight 20%)

    Returns (score, ret_3m, reasoning_text). ret_3m is the raw
    3-month price return as a decimal (e.g. 0.124 = +12.4%) — caller
    persists it to sector_screening.ret_3m so the screener page can
    show actual % change instead of just the composite score. None
    when fewer than 63 bars are available.

    2026-04-30 — G3 smoothing pass. The previous implementation used
    step-function bucketing (0.1 / 0.3 / 0.5 / 0.7 / 1.0) which made a
    1.99% return score 0.5 while a 2.01% return scored 0.7 — same
    economic signal, ~40 percentile-points apart. Replaced with
    continuous sigmoid mappings so similar inputs produce similar
    scores, and ranking gradients track underlying signal magnitude.

    Reasoning text still uses bucket-based human language ("strong
    positive momentum", etc.) since prose buckets read better than
    "score = 0.62". The score and the prose are now independent paths.
    """
    closes  = [b["c"] for b in bars if "c" in b]
    volumes = [b["v"] for b in bars if "v" in b]
    reasoning = []

    if len(closes) < 60:
        return 0.5, None, "Insufficient price history — defaulting to neutral score."

    # ── Component 1: 3-month return ────────────────────────────────────
    # Smooth sigmoid centered at 0% return. k=10 gives:
    #   ret=-0.30 → 0.05    (severe decline)
    #   ret=-0.10 → 0.27
    #   ret=-0.02 → 0.45
    #   ret= 0.00 → 0.50    (neutral)
    #   ret=+0.02 → 0.55
    #   ret=+0.10 → 0.73
    #   ret=+0.20 → 0.88
    #   ret=+0.30 → 0.95    (strong)
    # Smoother than the old 5-bucket function while preserving the
    # rough magnitude relationship.
    ret_3m    = (closes[-1] - closes[-63]) / closes[-63] if len(closes) >= 63 else 0.0
    ret_score = _sigmoid(ret_3m, k=10.0)
    if ret_3m > 0.10:
        reasoning.append(f"3-month return +{ret_3m:.1%} (strong positive momentum)")
    elif ret_3m > 0.02:
        reasoning.append(f"3-month return +{ret_3m:.1%} (mild positive momentum)")
    elif ret_3m > -0.02:
        reasoning.append(f"3-month return {ret_3m:.1%} (flat)")
    elif ret_3m > -0.10:
        reasoning.append(f"3-month return {ret_3m:.1%} (mild negative momentum)")
    else:
        reasoning.append(f"3-month return {ret_3m:.1%} (weak — significant price decline)")

    # ── Component 2: SMA structure ─────────────────────────────────────
    # Combine three continuous distance-from-baseline measurements:
    #   d20    = (price - sma20) / sma20   (price above/below short MA)
    #   d50    = (price - sma50) / sma50   (price above/below long MA)
    #   trend  = (sma20 - sma50) / sma50   (short MA above/below long MA)
    # Each fed through sigmoid; weighted blend is the SMA component.
    # Old discrete logic (close > sma20 > sma50 → 1.0, etc.) is still
    # the framework for prose reasoning, but the score is now smooth.
    sma_20 = sum(closes[-20:]) / 20
    sma_50 = sum(closes[-50:]) / 50
    last   = closes[-1]
    d20    = (last - sma_20) / sma_20 if sma_20 > 0 else 0.0
    d50    = (last - sma_50) / sma_50 if sma_50 > 0 else 0.0
    trend  = (sma_20 - sma_50) / sma_50 if sma_50 > 0 else 0.0
    sma_score = (
        _sigmoid(d20,   k=20.0) * 0.40
      + _sigmoid(d50,   k=20.0) * 0.40
      + _sigmoid(trend, k=20.0) * 0.20
    )
    if last > sma_20 > sma_50:
        reasoning.append("Price above 20-day and 50-day moving averages (uptrend confirmed)")
    elif last > sma_50:
        reasoning.append("Price above 50-day MA but below 20-day MA (mixed trend)")
    elif last > sma_20:
        reasoning.append("Price above 20-day MA but below 50-day MA (short-term bounce only)")
    else:
        reasoning.append("Price below both moving averages (downtrend)")

    # ── Component 3: Volume trend ──────────────────────────────────────
    # Sigmoid over log(10d/30d ratio). At ratio=1.0 (no change),
    # log(1)=0, sigmoid=0.5. Above-average volume pushes toward 1.0;
    # below toward 0.0. Smoother than the old 4-bucket threshold.
    vol_score = 0.5
    if len(volumes) >= 30:
        import math
        avg_10 = sum(volumes[-10:]) / 10
        avg_30 = sum(volumes[-30:]) / 30
        ratio  = avg_10 / avg_30 if avg_30 > 0 else 1.0
        # log(ratio) maps ratio=1→0, ratio=1.5→0.41, ratio=0.5→-0.69
        log_r  = math.log(max(ratio, 1e-6))
        vol_score = _sigmoid(log_r, k=4.0)
        if ratio > 1.20:
            reasoning.append("Volume 20% above 30-day average (institutional interest)")
        elif ratio > 1.05:
            reasoning.append("Volume slightly above average (normal activity)")
        elif ratio > 0.80:
            reasoning.append("Volume near average (no unusual activity)")
        else:
            reasoning.append("Volume below average (fading interest)")

    score = round(ret_score * 0.50 + sma_score * 0.30 + vol_score * 0.20, 4)
    return score, round(ret_3m, 4), " | ".join(reasoning)


# ── CONGRESSIONAL SIGNAL CHECK ────────────────────────────────────────────────

def check_congressional_signals(db, tickers):
    """
    Look for recent congressional buy/sell disclosures in the signals
    table for any of our screened tickers.

    Returns dict: {ticker: 'recent_buy' | 'recent_sell' | 'none'}

    2026-04-30 — bug fix. The previous query had no `source` filter and
    no politician check, so it matched ANY recent signal with a buy/sell
    transaction_type, including news rows whose transaction_type happened
    to contain those substrings. Result: tickers got `recent_buy` /
    `recent_sell` flags with no underlying politician/amount data, then
    Gate 5 boosted scores based on a flag whose backing was empty.

    Fix: require `source='CONGRESS'` (canonical marker for STOCK Act
    disclosures) AND `politician IS NOT NULL` (defense-in-depth — a
    real disclosure always has an attributed politician). News rows
    can no longer trigger the congressional flag regardless of how
    their transaction_type field happens to be populated.
    """
    cutoff = (datetime.now(ET) - timedelta(days=90)).strftime('%Y-%m-%d')
    results = {}
    with db.conn() as c:
        for ticker in tickers:
            row = c.execute("""
                SELECT transaction_type, disc_date FROM signals
                WHERE ticker=?
                  AND disc_date >= ?
                  AND source = 'CONGRESS'
                  AND politician IS NOT NULL
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

    # Configurable liquidity floor — env override for tuning. Default
    # $1M/day is below every current universe member by orders of
    # magnitude; serves as a defensive gate for future universe
    # expansion (FMP-refreshed holdings, manually added small-caps, etc.)
    min_dollar_vol = float(os.environ.get('MIN_DOLLAR_VOLUME', '1000000'))

    for holding in holdings:
        ticker = holding['ticker']
        bars   = fetch_bars(ticker, days=260)  # ~1 year of trading days
        # G2 — liquidity floor. Reject before scoring; an illiquid name
        # that clears the rest of the pipeline is tradable garbage.
        passes_liq, avg_vol, liq_reason = check_liquidity_floor(bars, min_dollar_vol)
        if not passes_liq:
            log.warning(f"  {ticker}: SKIPPED by liquidity floor — {liq_reason}")
            continue
        score, ret_3m, reasoning = calc_momentum_score(bars)
        momentum_details[ticker] = {"score": score, "ret_3m": ret_3m, "reasoning": reasoning,
                                     "avg_dollar_volume": avg_vol}
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
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('sector_screener', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

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
