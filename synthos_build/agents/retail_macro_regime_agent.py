"""
retail_macro_regime_agent.py — Macro Regime Agent
Synthos · Agent 8

Runs:
  Once daily before market open (suggested: 8:30 AM ET weekdays)
  Optional: refresh at midday (12:30 PM ET) for intraday regime shifts

Responsibilities:
  - 5-gate deterministic macro regime classification spine
  - Gate 1: VIX regime (calm / normal / elevated / crisis)
  - Gate 2: Treasury yield curve shape (steep / flat / inverted)
  - Gate 3: Market breadth (SPY vs IWM divergence)
  - Gate 4: Sector rotation signal (defensive vs cyclical leadership)
  - Gate 5: Aggregate regime classification
  - Writes regime to _MACRO_REGIME and _MACRO_REGIME_DETAIL settings
  - Downstream consumers: Market-State Aggregator, Validator Stack

No LLM in any decision path. All gate logic is deterministic and traceable.

Free data sources:
  - Yahoo Finance chart API (VIX, treasury yields, ETF prices)
  - Alpaca Data API (SPY, IWM, sector ETF bars — Iex feed, free tier)

Usage:
  python3 retail_macro_regime_agent.py
  python3 retail_macro_regime_agent.py --customer-id <uuid>
"""

import os
import sys
import time
import json
import logging
import argparse
import requests as _req
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, get_shared_db, acquire_agent_lock, release_agent_lock
from retail_shared import emit_admin_alert

def _master_db():
    """2026-04-27: returns the shared market-intel DB. See get_shared_db()."""
    return get_shared_db()

# ── CONFIG ────────────────────────────────────────────────────────────────
ET              = ZoneInfo("America/New_York")
UTC             = ZoneInfo("UTC")
REQUEST_TIMEOUT = 10

ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL   = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')

# FRED (Federal Reserve Economic Data) — primary source for VIX (gate 1)
# and treasury yields (gate 2). Government infrastructure, free, doesn't
# IP-block. Yahoo stays as fallback because the redundancy is cheap.
# Register: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY      = os.environ.get('FRED_API_KEY', '')
FRED_BASE_URL     = "https://api.stlouisfed.org/fred"

YAHOO_HEADERS = {'User-Agent': 'Mozilla/5.0'}

# When gate5 confidence drops below this threshold the scan is treated as
# degraded. Used by gate5 (status=WARNING below this) and run() (streak
# counter feeds the admin_alert).
GATE5_LOW_CONFIDENCE_THRESHOLD = 0.50

# When confidence is below threshold for this many consecutive runs, fire an
# admin_alert. At the daily cadence that's ~3 days of degraded macro signal
# — long enough to filter transient outages, short enough to surface a
# sustained Yahoo block / network issue.
LOW_CONFIDENCE_STREAK_ALERT = 3

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('macro_regime_agent')


# ── HELPERS ───────────────────────────────────────────────────────────────

def _now_str():
    return datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S')


def _alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


# ── HTTP RETRY HELPER ────────────────────────────────────────────────────

# Status codes worth retrying — transient server / rate-limit conditions.
# 4xx codes other than 429 indicate a client error (bad URL, bad key) and
# won't get better on retry.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _http_get_with_retry(url, *, params=None, headers=None,
                         timeout=None, max_attempts=3, what=""):
    """HTTP GET with exponential backoff (1s → 2s → 4s).

    Retries on 429/5xx and on raised exceptions (connection error, timeout).
    Returns the final `requests.Response` on terminal status (success or
    non-retryable failure), or None if every attempt raised an exception.

    `what` is a short descriptor used in retry log messages (e.g. "Yahoo
    ^VIX") so a transient retry is traceable in the agent log without
    decoding the URL by hand.
    """
    if timeout is None:
        timeout = REQUEST_TIMEOUT
    backoff = 1.0
    for attempt in range(max_attempts):
        try:
            r = _req.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in _RETRYABLE_STATUS and attempt < max_attempts - 1:
                log.info(f"{what or url} got HTTP {r.status_code}, "
                         f"retry {attempt + 2}/{max_attempts} in {backoff:.1f}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            return r
        except Exception as e:
            if attempt < max_attempts - 1:
                log.info(f"{what or url} raised {type(e).__name__}, "
                         f"retry {attempt + 2}/{max_attempts} in {backoff:.1f}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning(f"{what or url} all {max_attempts} attempts failed: {e}")
            return None
    return None


# ── YAHOO FINANCE FETCHERS ───────────────────────────────────────────────

def _fetch_yahoo_chart(symbol, range_str="5d", interval="1d"):
    """
    Fetch OHLCV data from Yahoo Finance v8 chart API with retry/backoff.
    Returns list of close prices or empty list on terminal failure.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_str, "interval": interval}
    r = _http_get_with_retry(url, params=params, headers=YAHOO_HEADERS,
                             what=f"Yahoo chart {symbol}")
    try:
        _master_db().log_api_call(
            agent='macro_regime', endpoint=f'/chart/{symbol}',
            method='GET', service='yahoo',
            status_code=getattr(r, 'status_code', None))
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    if r is None or r.status_code != 200:
        if r is not None:
            log.warning(f"Yahoo chart {symbol}: HTTP {r.status_code}")
        return []
    try:
        data = r.json()
    except Exception as e:
        log.warning(f"Yahoo chart {symbol}: JSON decode failed: {e}")
        return []
    result = data.get("chart", {}).get("result", [])
    if not result:
        return []
    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
    # Filter out None values
    return [c for c in closes if c is not None]


def _fetch_yahoo_last_close(symbol):
    """Fetch the most recent close price for a Yahoo symbol. Returns float or None."""
    closes = _fetch_yahoo_chart(symbol, range_str="5d", interval="1d")
    if closes:
        return closes[-1]
    return None


# ── FRED FETCHER ─────────────────────────────────────────────────────────

def _fetch_fred_series(series_id, days=10):
    """
    Fetch the most recent N daily observations for a FRED series.
    Returns list of float values (oldest first), or [] on failure.

    FRED returns a "." for missing observations (e.g. weekends, holidays);
    those are filtered out so the caller can rely on len() reflecting
    actual data points.

    Uses limit/sort parameters to fetch only the tail of the series —
    FRED's free tier is unlimited per key but we don't need all-history.

    Series IDs we use:
      VIXCLS    — CBOE VIX close (daily)
      DGS10     — 10-year Treasury constant maturity (daily, percent)
      DGS3MO    — 3-month Treasury bill secondary market rate (daily, percent)
    """
    if not FRED_API_KEY:
        log.debug(f"FRED_API_KEY not set — skipping FRED fetch for {series_id}")
        return []
    # sort_order=desc + limit returns the newest N observations.
    # We then reverse so caller sees oldest-first (matches Yahoo helpers).
    r = _http_get_with_retry(
        f"{FRED_BASE_URL}/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": max(days + 5, 10),   # buffer for missing/holiday days
        },
        what=f"FRED {series_id}",
    )
    try:
        _master_db().log_api_call(
            agent='macro_regime',
            endpoint=f'/fred/series/observations?series_id={series_id}',
            method='GET', service='fred',
            status_code=getattr(r, 'status_code', None))
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    if r is None or r.status_code != 200:
        if r is not None:
            log.warning(f"FRED {series_id}: HTTP {r.status_code}")
        return []
    try:
        obs = r.json().get("observations", [])
    except Exception as e:
        log.warning(f"FRED {series_id}: JSON decode failed: {e}")
        return []
    # Newest-first → oldest-first for consistency with Yahoo helpers
    values = []
    for ob in reversed(obs):
        v = ob.get("value", "")
        if v in (".", "", None):
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    return values


# ── ALPACA DATA FETCHERS ─────────────────────────────────────────────────

def _fetch_alpaca_bars(ticker, days=10):
    """
    Fetch daily bars from Alpaca Data API.
    Returns list of bar dicts or empty list on failure.
    """
    if not ALPACA_API_KEY:
        log.warning("ALPACA_API_KEY not set — skipping Alpaca fetch")
        return []
    # UTC — Alpaca reads the 'Z' suffix as UTC. Using datetime.now(ET) here
    # would encode ET-local time but label it UTC, yielding a 4-5 hour
    # offset and either missed or duplicated bars around market boundaries.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    start = (now_utc - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    r = _http_get_with_retry(
        f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
        params={"timeframe": "1Day", "start": start, "end": end,
                "limit": days + 10, "feed": "iex"},
        headers=_alpaca_headers(),
        what=f"Alpaca {ticker} bars",
    )
    try:
        _master_db().log_api_call(
            agent='macro_regime', endpoint=f'/v2/stocks/{ticker}/bars',
            method='GET', service='alpaca_data',
            status_code=getattr(r, 'status_code', None))
    except Exception as _e:
        log.debug(f"suppressed exception: {_e}")
    if r is None:
        return []
    if r.status_code == 200:
        try:
            return r.json().get("bars", []) or []
        except Exception as e:
            log.warning(f"Alpaca bars {ticker}: JSON decode failed: {e}")
            return []
    log.warning(f"Alpaca bars {ticker}: HTTP {r.status_code}")
    return []


def _fetch_5d_return(ticker):
    """
    Get 5-day return for a ticker.  Try Alpaca first, fall back to Yahoo.
    Returns float or None.
    """
    # Try Alpaca
    bars = _fetch_alpaca_bars(ticker, days=10)
    if len(bars) >= 5:
        closes = [b["c"] for b in bars[-5:] if "c" in b]
        if len(closes) >= 2:
            return round((closes[-1] - closes[0]) / closes[0], 4)

    # Fallback: Yahoo Finance
    closes = _fetch_yahoo_chart(ticker, range_str="5d", interval="1d")
    if len(closes) >= 2:
        return round((closes[-1] - closes[0]) / closes[0], 4)

    return None


# ══════════════════════════════════════════════════════════════════════════
#  GATE RESULTS
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class GateResult:
    gate: str
    status: str          # OK, WARNING, UNAVAILABLE
    signal: str          # gate-specific signal label
    value: object = None # primary numeric value
    detail: str = ""     # human-readable explanation


@dataclass
class RegimeReport:
    """Aggregated output of all 5 gates."""
    gates: list = field(default_factory=list)
    regime: str = "UNCERTAIN"
    confidence: float = 0.0
    started_at: str = ""
    completed_at: str = ""

    def add(self, gate_result: GateResult):
        self.gates.append(gate_result)

    def summary_dict(self):
        return {
            "regime": self.regime,
            "confidence": self.confidence,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "gates": [
                {"gate": g.gate, "status": g.status, "signal": g.signal,
                 "value": g.value, "detail": g.detail}
                for g in self.gates
            ],
        }


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1: VIX REGIME
#  Classify current volatility environment from VIX level and trend
# ══════════════════════════════════════════════════════════════════════════

def gate1_vix_regime(report: RegimeReport):
    """
    Fetch VIX from FRED (primary, VIXCLS series) with Yahoo fallback.
    Classify level + 5-day trend.
    VIX < 15 = CALM, 15-25 = NORMAL, 25-35 = ELEVATED, > 35 = CRISIS.

    Both sources publish the same CBOE VIX close, so thresholds are
    source-agnostic. Source tracked in report.detail for provenance.
    """
    log.info("[GATE 1] VIX regime classification")

    # Primary: FRED VIXCLS — government infrastructure, no IP blocks
    source = "fred"
    closes = _fetch_fred_series("VIXCLS", days=7)
    if not closes:
        # Fallback: Yahoo Finance ^VIX — undocumented but historically reliable
        log.info("[GATE 1] FRED VIXCLS empty — falling back to Yahoo ^VIX")
        source = "yahoo"
        closes = _fetch_yahoo_chart("%5EVIX", range_str="5d", interval="1d")

    if not closes:
        log.warning("[GATE 1] VIX data unavailable from both FRED and Yahoo")
        report.add(GateResult(
            gate="GATE1_VIX", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail="Could not fetch VIX from FRED (VIXCLS) or Yahoo (^VIX)"
        ))
        return None, None

    vix_current = closes[-1]

    # Trend direction: compare first vs last close over 5d window
    if len(closes) >= 2:
        vix_first = closes[0]
        trend_pct = (vix_current - vix_first) / vix_first if vix_first > 0 else 0.0
        if trend_pct > 0.05:
            trend = "RISING"
        elif trend_pct < -0.05:
            trend = "FALLING"
        else:
            trend = "FLAT"
    else:
        trend = "UNKNOWN"
        trend_pct = 0.0

    # Classify VIX level
    if vix_current < 15:
        level = "CALM"
    elif vix_current < 25:
        level = "NORMAL"
    elif vix_current < 35:
        level = "ELEVATED"
    else:
        level = "CRISIS"

    signal = f"{level}_{trend}"
    detail = f"VIX={vix_current:.2f}, 5d trend={trend} ({trend_pct:+.1%}), source={source}"
    log.info(f"  VIX={vix_current:.2f} level={level} trend={trend} ({trend_pct:+.1%}) source={source}")

    report.add(GateResult(
        gate="GATE1_VIX", status="OK", signal=signal,
        value=round(vix_current, 2), detail=detail
    ))
    return level, trend


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2: TREASURY YIELD CURVE
#  Fetch 13-week (^IRX) and 10-year (^TNX) yields, compute spread
# ══════════════════════════════════════════════════════════════════════════

def gate2_yield_curve(report: RegimeReport):
    """
    Yield spread = 10Y - 3M (or 13W proxy).
    Inverted (<0) = contractionary.  Flat (0 to 0.20) = late-cycle.
    Normal (0.20 to 1.50) = neutral.  Steep (>1.50) = expansionary.

    Primary source: FRED (DGS10 + DGS3MO, both in percent).
    Fallback: Yahoo (^TNX scaled /10, ^IRX in percent).
    Both yield identical economic content — the curve shape derived from
    Treasury constant-maturity rates is the same regardless of source.
    """
    log.info("[GATE 2] Treasury yield curve analysis")

    # Primary: FRED — DGS10 (10Y constant maturity) + DGS3MO (3M T-bill).
    # FRED returns both in percent already, no scaling needed.
    source = "fred"
    long_closes  = _fetch_fred_series("DGS10",  days=7)   # 10Y
    short_closes = _fetch_fred_series("DGS3MO", days=7)   # 3M

    # Fallback: Yahoo — ^TNX (10Y, quoted as yield*10) + ^IRX (13W, percent).
    # 13W vs 3M is close enough — both are at the very short end of the
    # curve and move in lockstep, so the shape signal is preserved.
    if not long_closes or not short_closes:
        log.info("[GATE 2] FRED yield series empty — falling back to Yahoo ^TNX/^IRX")
        source = "yahoo"
        tnx_closes = _fetch_yahoo_chart("%5ETNX", range_str="5d", interval="1d")
        irx_closes = _fetch_yahoo_chart("%5EIRX", range_str="5d", interval="1d")
        # ^TNX quoted as yield * 10; normalise so downstream math is uniform.
        long_closes  = [c / 10.0 for c in tnx_closes] if tnx_closes else []
        short_closes = irx_closes or []

    if not long_closes or not short_closes:
        missing = []
        if not long_closes:
            missing.append("10Y")
        if not short_closes:
            missing.append("3M/13W")
        log.warning(f"[GATE 2] Yield data unavailable from both sources: {', '.join(missing)}")
        report.add(GateResult(
            gate="GATE2_YIELD_CURVE", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail=f"Missing: {', '.join(missing)} (tried FRED + Yahoo)"
        ))
        return None

    yield_10y = long_closes[-1]
    yield_13w = short_closes[-1]

    spread = yield_10y - yield_13w

    # Also check 5d trend in the spread
    if len(long_closes) >= 2 and len(short_closes) >= 2:
        spread_start = long_closes[0] - short_closes[0]
        spread_change = spread - spread_start
        if spread_change > 0.05:
            curve_trend = "STEEPENING"
        elif spread_change < -0.05:
            curve_trend = "FLATTENING"
        else:
            curve_trend = "STABLE"
    else:
        curve_trend = "UNKNOWN"

    # Classify
    if spread < 0:
        shape = "INVERTED"
    elif spread < 0.20:
        shape = "FLAT"
    elif spread < 1.50:
        shape = "NORMAL"
    else:
        shape = "STEEP"

    signal = f"{shape}_{curve_trend}"
    detail = (f"10Y={yield_10y:.2f}%, 3M={yield_13w:.2f}%, "
              f"spread={spread:+.2f}%, shape={shape}, trend={curve_trend}, source={source}")
    log.info(f"  10Y={yield_10y:.2f}% 3M={yield_13w:.2f}% spread={spread:+.2f}% "
             f"shape={shape} trend={curve_trend} source={source}")

    report.add(GateResult(
        gate="GATE2_YIELD_CURVE", status="OK", signal=signal,
        value=round(spread, 4), detail=detail
    ))
    return shape, curve_trend, round(spread, 4)


# ══════════════════════════════════════════════════════════════════════════
#  GATE 3: MARKET BREADTH
#  Compare SPY (large cap) vs IWM (small cap) 5-day returns
# ══════════════════════════════════════════════════════════════════════════

def gate3_market_breadth(report: RegimeReport):
    """
    SPY up + IWM up = BROAD_STRENGTH.
    SPY up + IWM down = NARROW_BREADTH (warning — large cap only rally).
    SPY down + IWM up = ROTATION_TO_SMALL (possible recovery signal).
    Both down = BROAD_WEAKNESS.
    """
    log.info("[GATE 3] Market breadth (SPY vs IWM)")

    spy_ret = _fetch_5d_return("SPY")
    iwm_ret = _fetch_5d_return("IWM")

    if spy_ret is None and iwm_ret is None:
        log.warning("[GATE 3] Breadth data unavailable for both SPY and IWM")
        report.add(GateResult(
            gate="GATE3_BREADTH", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail="Could not fetch SPY or IWM returns"
        ))
        return None

    if spy_ret is None or iwm_ret is None:
        # Partial data — degrade gracefully
        available = f"SPY={spy_ret}" if spy_ret is not None else f"IWM={iwm_ret}"
        log.warning(f"[GATE 3] Partial breadth data: {available}")
        report.add(GateResult(
            gate="GATE3_BREADTH", status="WARNING", signal="PARTIAL",
            detail=f"Only partial data available: {available}"
        ))
        return "PARTIAL"

    spy_up = spy_ret > 0.001   # > +0.1% threshold to filter noise
    iwm_up = iwm_ret > 0.001

    if spy_up and iwm_up:
        breadth = "BROAD_STRENGTH"
    elif spy_up and not iwm_up:
        breadth = "NARROW_BREADTH"
    elif not spy_up and iwm_up:
        breadth = "ROTATION_TO_SMALL"
    else:
        breadth = "BROAD_WEAKNESS"

    # Divergence magnitude
    divergence = abs(spy_ret - iwm_ret)
    if divergence > 0.03:
        breadth_quality = "HIGH_DIVERGENCE"
    elif divergence > 0.01:
        breadth_quality = "MODERATE_DIVERGENCE"
    else:
        breadth_quality = "LOW_DIVERGENCE"

    signal = f"{breadth}_{breadth_quality}"
    detail = (f"SPY 5d={spy_ret:+.2%}, IWM 5d={iwm_ret:+.2%}, "
              f"divergence={divergence:.2%}, breadth={breadth}")
    log.info(f"  SPY={spy_ret:+.2%} IWM={iwm_ret:+.2%} breadth={breadth} "
             f"divergence={divergence:.2%}")

    report.add(GateResult(
        gate="GATE3_BREADTH", status="OK", signal=signal,
        value={"spy_5d": spy_ret, "iwm_5d": iwm_ret, "divergence": round(divergence, 4)},
        detail=detail
    ))
    return breadth


# ══════════════════════════════════════════════════════════════════════════
#  GATE 4: SECTOR ROTATION SIGNAL
#  Defensive (XLU, XLP) vs Cyclical (XLY, XLI) 5-day performance
# ══════════════════════════════════════════════════════════════════════════

DEFENSIVE_ETFS = ["XLU", "XLP"]   # Utilities, Consumer Staples
CYCLICAL_ETFS  = ["XLY", "XLI"]   # Consumer Discretionary, Industrials

def gate4_sector_rotation(report: RegimeReport):
    """
    If defensives outperform cyclicals by >1% = RISK_OFF rotation.
    If cyclicals outperform defensives by >1% = RISK_ON rotation.
    Otherwise = NEUTRAL rotation.
    """
    log.info("[GATE 4] Sector rotation signal (defensive vs cyclical)")

    def_returns = {}
    cyc_returns = {}

    for ticker in DEFENSIVE_ETFS:
        ret = _fetch_5d_return(ticker)
        if ret is not None:
            def_returns[ticker] = ret
            log.info(f"  {ticker} (defensive) 5d={ret:+.2%}")

    for ticker in CYCLICAL_ETFS:
        ret = _fetch_5d_return(ticker)
        if ret is not None:
            cyc_returns[ticker] = ret
            log.info(f"  {ticker} (cyclical) 5d={ret:+.2%}")

    if not def_returns and not cyc_returns:
        log.warning("[GATE 4] No sector rotation data available")
        report.add(GateResult(
            gate="GATE4_ROTATION", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail="Could not fetch any sector ETF returns"
        ))
        return None

    if not def_returns or not cyc_returns:
        log.warning("[GATE 4] Partial sector data — cannot compare defensive vs cyclical")
        report.add(GateResult(
            gate="GATE4_ROTATION", status="WARNING", signal="PARTIAL",
            detail="Partial sector ETF data; cannot compute rotation signal"
        ))
        return "PARTIAL"

    avg_def = sum(def_returns.values()) / len(def_returns)
    avg_cyc = sum(cyc_returns.values()) / len(cyc_returns)
    rotation_spread = avg_cyc - avg_def  # positive = cyclicals leading

    if rotation_spread > 0.01:
        rotation = "RISK_ON"
    elif rotation_spread < -0.01:
        rotation = "RISK_OFF"
    else:
        rotation = "NEUTRAL"

    detail = (f"Avg defensive 5d={avg_def:+.2%} ({', '.join(f'{k}={v:+.2%}' for k, v in def_returns.items())}), "
              f"Avg cyclical 5d={avg_cyc:+.2%} ({', '.join(f'{k}={v:+.2%}' for k, v in cyc_returns.items())}), "
              f"spread={rotation_spread:+.2%}, signal={rotation}")
    log.info(f"  Defensive avg={avg_def:+.2%} Cyclical avg={avg_cyc:+.2%} "
             f"spread={rotation_spread:+.2%} => {rotation}")

    report.add(GateResult(
        gate="GATE4_ROTATION", status="OK", signal=rotation,
        value=round(rotation_spread, 4), detail=detail
    ))
    return rotation


# ══════════════════════════════════════════════════════════════════════════
#  GATE 5: AGGREGATE REGIME CLASSIFICATION
#  Combine gates 1-4 into a single macro regime label
# ══════════════════════════════════════════════════════════════════════════

# Regime definitions (all conditions that must be true):
# EXPANSION:    VIX calm/normal + normal/steep curve + broad strength + cyclicals leading
# LATE_CYCLE:   VIX normal/elevated + flat/flattening curve + narrow breadth + mixed
# CONTRACTION:  VIX elevated + inverted/flat curve + broad weakness + defensives leading
# CRISIS:       VIX > 35 (crisis level) + any other negative signals
# RECOVERY:     VIX falling from elevated + steepening curve + broad strength
# UNCERTAIN:    Mixed signals


# Fitness tables: per-regime, per-gate score for each possible signal value.
# 1.0 = textbook fit, 0.0 = doesn't fit at all. Used to scale gate-5 base
# confidence so a borderline regime call gets a lower confidence than a
# textbook one — even when both fall in the same branch with the same
# data availability.
#
# Without fitness scaling: a clean EXPANSION (CALM/STEEP/BROAD/RISK_ON) and
# a marginal EXPANSION (NORMAL/NORMAL/ROTATION/NEUTRAL) both report
# confidence 0.80. With fitness scaling, the marginal one drops to ~0.52
# and now correctly trips gate-5's WARNING threshold (< 0.50) when it
# barely qualifies.
#
# Tables only include the 5 confident regimes; UNCERTAIN is the catch-all
# and keeps its current confidence calculation unchanged.
_REGIME_FITNESS = {
    "CRISIS": {
        "vix":      {"CRISIS": 1.00, "ELEVATED": 0.50, "NORMAL": 0.10, "CALM": 0.00},
        "curve":    {"INVERTED": 1.00, "FLAT": 0.70, "NORMAL": 0.30, "STEEP": 0.00},
        "breadth":  {"BROAD_WEAKNESS": 1.00, "NARROW_BREADTH": 0.60,
                     "ROTATION_TO_SMALL": 0.20, "BROAD_STRENGTH": 0.00},
        "rotation": {"RISK_OFF": 1.00, "NEUTRAL": 0.30, "RISK_ON": 0.00},
    },
    "CONTRACTION": {
        "vix":      {"CRISIS": 1.00, "ELEVATED": 0.85, "NORMAL": 0.20, "CALM": 0.00},
        "curve":    {"INVERTED": 1.00, "FLAT": 0.70, "NORMAL": 0.20, "STEEP": 0.00},
        "breadth":  {"BROAD_WEAKNESS": 1.00, "NARROW_BREADTH": 0.70,
                     "ROTATION_TO_SMALL": 0.30, "BROAD_STRENGTH": 0.00},
        "rotation": {"RISK_OFF": 1.00, "NEUTRAL": 0.50, "RISK_ON": 0.00},
    },
    "LATE_CYCLE": {
        "vix":      {"NORMAL": 1.00, "ELEVATED": 0.80, "CALM": 0.30, "CRISIS": 0.00},
        "curve":    {"FLAT": 1.00, "INVERTED": 0.70, "NORMAL": 0.50, "STEEP": 0.00},
        "breadth":  {"NARROW_BREADTH": 1.00, "ROTATION_TO_SMALL": 0.60,
                     "BROAD_WEAKNESS": 0.50, "BROAD_STRENGTH": 0.40},
        "rotation": {"NEUTRAL": 1.00, "RISK_OFF": 0.70, "RISK_ON": 0.40},
    },
    "EXPANSION": {
        "vix":      {"CALM": 1.00, "NORMAL": 0.70, "ELEVATED": 0.00, "CRISIS": 0.00},
        "curve":    {"STEEP": 1.00, "NORMAL": 0.70, "FLAT": 0.00, "INVERTED": 0.00},
        "breadth":  {"BROAD_STRENGTH": 1.00, "ROTATION_TO_SMALL": 0.60,
                     "NARROW_BREADTH": 0.20, "BROAD_WEAKNESS": 0.00},
        "rotation": {"RISK_ON": 1.00, "NEUTRAL": 0.60, "RISK_OFF": 0.00},
    },
    "RECOVERY": {
        # vix_trend is required to be FALLING by the branch logic, so the
        # vix-level fitness here reflects "level to fall FROM" — recovery
        # is most clearly identified when VIX is falling from ELEVATED back
        # toward NORMAL.
        "vix":      {"ELEVATED": 1.00, "NORMAL": 0.70, "CRISIS": 0.50, "CALM": 0.30},
        "curve":    {"STEEP": 1.00, "NORMAL": 0.70, "FLAT": 0.30, "INVERTED": 0.00},
        "breadth":  {"BROAD_STRENGTH": 1.00, "ROTATION_TO_SMALL": 0.80,
                     "NARROW_BREADTH": 0.30, "BROAD_WEAKNESS": 0.00},
        "rotation": {"RISK_ON": 1.00, "NEUTRAL": 0.50, "RISK_OFF": 0.10},
    },
}


def _compute_fitness(regime: str, vix_level, curve_shape,
                     breadth_result, rotation_result) -> float:
    """Mean fitness across the inputs we have data for. Missing inputs
    contribute neither to numerator nor denominator (so confidence reflects
    only the gates that actually fired). Returns 1.0 for regimes without
    a fitness table (UNCERTAIN) so the legacy confidence path is preserved.
    """
    table = _REGIME_FITNESS.get(regime)
    if not table:
        return 1.0
    inputs = (
        ("vix",      vix_level),
        ("curve",    curve_shape),
        ("breadth",  breadth_result),
        ("rotation", rotation_result),
    )
    scores = []
    for key, value in inputs:
        if value in (None, "PARTIAL", "UNAVAILABLE"):
            continue
        per_value = table.get(key, {})
        # If a value isn't in the lookup (e.g. an unexpected new label),
        # treat it as a 0.5 mid-tier match rather than 0 — avoids
        # silently zeroing the confidence on a string we didn't anticipate.
        scores.append(per_value.get(value, 0.5))
    if not scores:
        return 1.0
    return sum(scores) / len(scores)

def gate5_regime_classification(report: RegimeReport, vix_result, yield_result,
                                 breadth_result, rotation_result):
    """
    Deterministic regime classification from the 4 upstream gates.
    Returns the classified regime string.
    """
    log.info("[GATE 5] Aggregate regime classification")

    vix_level, vix_trend = (vix_result if vix_result else (None, None))

    if yield_result and len(yield_result) == 3:
        curve_shape, curve_trend, spread_val = yield_result
    else:
        curve_shape, curve_trend, spread_val = None, None, None

    # Count available gates
    available_gates = sum(1 for x in [vix_level, curve_shape, breadth_result, rotation_result]
                          if x is not None and x not in ("PARTIAL", "UNAVAILABLE"))

    if available_gates == 0:
        regime = "UNCERTAIN"
        confidence = 0.0
        reason = "No gate data available — all sources failed"
        log.warning(f"  Regime={regime} (no data)")
        report.add(GateResult(
            gate="GATE5_REGIME", status="WARNING", signal=regime,
            value=confidence, detail=reason
        ))
        report.regime = regime
        report.confidence = confidence
        return regime

    # ── CRISIS ────────────────────────────────────────────────────────
    if vix_level == "CRISIS":
        regime = "CRISIS"
        confidence = 0.95
        reason = (f"VIX at crisis level (>35), curve={curve_shape or '?'}, "
                  f"breadth={breadth_result or '?'}, rotation={rotation_result or '?'}")
        log.info(f"  Regime=CRISIS — VIX crisis level")

    # ── RECOVERY ──────────────────────────────────────────────────────
    # Accept None for any individual input (matches EXPANSION's pattern).
    # Without this softening, a single missing data source dropped the
    # regime to UNCERTAIN even when 3 of 4 signals were textbook recovery.
    elif (vix_level in ("ELEVATED", "NORMAL") and vix_trend == "FALLING"
          and curve_trend in ("STEEPENING", None)
          and breadth_result in ("BROAD_STRENGTH", "ROTATION_TO_SMALL", None)):
        regime = "RECOVERY"
        confidence = 0.75
        reason = (f"VIX {vix_level} but falling, curve_trend={curve_trend or '?'}, "
                  f"breadth={breadth_result or '?'}")
        log.info(f"  Regime=RECOVERY — VIX falling + curve steepening + strength")

    # ── CONTRACTION ───────────────────────────────────────────────────
    # Same softening — accept None for breadth or rotation if other
    # contraction signals are present.
    elif (vix_level in ("ELEVATED", "CRISIS")
          and curve_shape in ("INVERTED", "FLAT")
          and breadth_result in ("BROAD_WEAKNESS", "NARROW_BREADTH", None)
          and rotation_result in ("RISK_OFF", "NEUTRAL", None)):
        regime = "CONTRACTION"
        confidence = 0.85
        reason = (f"VIX {vix_level}, curve {curve_shape}, "
                  f"breadth={breadth_result or '?'}, rotation={rotation_result or '?'}")
        log.info(f"  Regime=CONTRACTION")

    # ── LATE_CYCLE ────────────────────────────────────────────────────
    elif (vix_level in ("NORMAL", "ELEVATED")
          and curve_shape in ("FLAT", "NORMAL")
          and (curve_trend == "FLATTENING" or breadth_result == "NARROW_BREADTH")):
        regime = "LATE_CYCLE"
        confidence = 0.70
        reason = (f"VIX {vix_level}, curve {curve_shape}/{curve_trend or '?'}, "
                  f"breadth={breadth_result or '?'}, rotation={rotation_result or '?'}")
        log.info(f"  Regime=LATE_CYCLE")

    # ── EXPANSION ─────────────────────────────────────────────────────
    elif (vix_level in ("CALM", "NORMAL")
          and curve_shape in ("NORMAL", "STEEP")
          and breadth_result in ("BROAD_STRENGTH", "ROTATION_TO_SMALL", None)
          and rotation_result in ("RISK_ON", "NEUTRAL", None)):
        regime = "EXPANSION"
        confidence = 0.80
        reason = (f"VIX {vix_level}, curve {curve_shape or '?'}, "
                  f"breadth={breadth_result or '?'}, rotation={rotation_result or '?'}")
        log.info(f"  Regime=EXPANSION")

    # ── UNCERTAIN ─────────────────────────────────────────────────────
    else:
        regime = "UNCERTAIN"
        confidence = 0.40
        reason = (f"Mixed signals: VIX={vix_level}/{vix_trend}, "
                  f"curve={curve_shape}/{curve_trend}, "
                  f"breadth={breadth_result}, rotation={rotation_result}")
        log.info(f"  Regime=UNCERTAIN — mixed signals")

    # Adjust confidence by:
    #   1. Data availability — fewer gates = lower confidence in the result.
    #   2. Fitness — how strongly the gate signals match the picked regime
    #      branch. A textbook EXPANSION (CALM/STEEP/BROAD/RISK_ON) scores
    #      higher than a marginal one (NORMAL/NORMAL/ROTATION/NEUTRAL)
    #      even though both fall in the same branch with full data.
    fitness = _compute_fitness(regime, vix_level, curve_shape,
                               breadth_result, rotation_result)
    base_confidence = confidence
    confidence = round(confidence * fitness * (available_gates / 4.0), 2)

    # Status reflects classification quality — when more than half the
    # upstream gates failed (or fitness is poor), downstream consumers
    # should see this scan as suspect even though gate 5 produced a
    # label. Previously always "OK" → _MACRO_SCAN_LAST claimed "5/5 OK"
    # while the underlying regime call was confidence ≤ 0.10.
    gate5_status = "WARNING" if confidence < GATE5_LOW_CONFIDENCE_THRESHOLD else "OK"

    # Append fitness/availability breakdown to the reason so the admin
    # alert detail tells the operator WHY confidence is what it is.
    reason = (f"{reason} | base={base_confidence:.2f} × "
              f"fitness={fitness:.2f} × avail={available_gates}/4")

    report.add(GateResult(
        gate="GATE5_REGIME", status=gate5_status, signal=regime,
        value=confidence, detail=reason
    ))
    report.regime = regime
    report.confidence = confidence
    return regime


# ══════════════════════════════════════════════════════════════════════════
#  MAIN RUN
# ══════════════════════════════════════════════════════════════════════════

def run():
    """Execute the 5-gate macro regime classification spine."""
    db = _master_db()

    # ── Lifecycle: START ──────────────────────────────────────────────
    db.log_event("AGENT_START", agent="Macro Regime", details="macro regime classification")
    db.log_heartbeat("macro_regime_agent", "RUNNING")
    log.info("=" * 70)
    log.info("MACRO REGIME AGENT — Starting 5-gate regime classification")
    log.info("=" * 70)

    report = RegimeReport(started_at=_now_str())

    # ── Gate 1: VIX regime ────────────────────────────────────────────
    try:
        vix_result = gate1_vix_regime(report)
    except Exception as e:
        log.error(f"Gate 1 failed: {e}", exc_info=True)
        report.add(GateResult("GATE1_VIX", "UNAVAILABLE", "ERROR",
                               detail=f"VIX gate failed: {e}"))
        vix_result = None

    # ── Gate 2: Treasury yield curve ──────────────────────────────────
    try:
        yield_result = gate2_yield_curve(report)
    except Exception as e:
        log.error(f"Gate 2 failed: {e}", exc_info=True)
        report.add(GateResult("GATE2_YIELD_CURVE", "UNAVAILABLE", "ERROR",
                               detail=f"Yield curve gate failed: {e}"))
        yield_result = None

    # ── Gate 3: Market breadth ────────────────────────────────────────
    try:
        breadth_result = gate3_market_breadth(report)
    except Exception as e:
        log.error(f"Gate 3 failed: {e}", exc_info=True)
        report.add(GateResult("GATE3_BREADTH", "UNAVAILABLE", "ERROR",
                               detail=f"Breadth gate failed: {e}"))
        breadth_result = None

    # ── Gate 4: Sector rotation ───────────────────────────────────────
    try:
        rotation_result = gate4_sector_rotation(report)
    except Exception as e:
        log.error(f"Gate 4 failed: {e}", exc_info=True)
        report.add(GateResult("GATE4_ROTATION", "UNAVAILABLE", "ERROR",
                               detail=f"Rotation gate failed: {e}"))
        rotation_result = None

    # ── Gate 5: Aggregate regime ──────────────────────────────────────
    try:
        regime = gate5_regime_classification(report, vix_result, yield_result,
                                             breadth_result, rotation_result)
    except Exception as e:
        log.error(f"Gate 5 failed: {e}", exc_info=True)
        report.add(GateResult("GATE5_REGIME", "UNAVAILABLE", "ERROR",
                               detail=f"Regime classification failed: {e}"))
        regime = "UNCERTAIN"

    report.completed_at = _now_str()

    # ── Build detail payload for downstream consumers ─────────────────
    # Extract key values from gate results for the detail JSON
    vix_value = None
    yield_spread = None
    for g in report.gates:
        if g.gate == "GATE1_VIX" and isinstance(g.value, (int, float)):
            vix_value = g.value
        if g.gate == "GATE2_YIELD_CURVE" and isinstance(g.value, (int, float)):
            yield_spread = g.value

    regime_detail = {
        "regime": report.regime,
        "confidence": report.confidence,
        "vix": vix_value,
        "yield_spread": yield_spread,
        "breadth": breadth_result if isinstance(breadth_result, str) else None,
        "rotation": rotation_result if isinstance(rotation_result, str) else None,
        "timestamp": report.completed_at,
        "gates": [
            {"gate": g.gate, "status": g.status, "signal": g.signal, "detail": g.detail}
            for g in report.gates
        ],
    }

    # ── Write regime to DB settings ───────────────────────────────────
    # _MACRO_REGIME_UPDATED is the timestamp surface the validator's
    # staleness check looks at (retail_validator_stack_agent gate4).
    # Without it, the validator's age check silently no-ops and a regime
    # could go days stale while the validator treats it as fresh.
    db.set_setting('_MACRO_REGIME', report.regime)
    db.set_setting('_MACRO_REGIME_DETAIL', json.dumps(regime_detail))
    db.set_setting('_MACRO_REGIME_UPDATED', report.completed_at)

    # ── DUAL-WRITE: broadcast regime via MQTT (Tier 4 of distributed-
    # trader migration). DB above remains the source of truth — this is
    # additive for low-latency subscribers (auditor, future retail nodes,
    # dashboard live-feed). retain=True so a subscriber connecting
    # mid-day immediately gets the current regime without polling.
    # Best-effort: silent failure if the broker is unreachable.
    try:
        from mqtt_client import get_publisher
        _mqtt = get_publisher()
        if _mqtt is not None:
            _mqtt.publish("process/regime", {
                "regime": report.regime,
                "confidence": getattr(report, "confidence", None),
                "detail": regime_detail,
                "completed_at": report.completed_at,
            }, qos=0, retain=True)
    except Exception as _mqtt_e:
        log.debug(f"MQTT regime publish failed (non-fatal): {_mqtt_e}")

    # ── Stamp all QUEUED signals with the current macro regime ────────
    # Part of the validation chain: signals need this stamp before being
    # promoted to VALIDATED. One bulk UPDATE — constant-time regardless
    # of queue size.
    try:
        stamped = db.stamp_signals_macro(report.regime)
        log.info(f"macro_regime stamped {stamped} QUEUED signal(s) with '{report.regime}'")
    except Exception as _e:
        log.warning(f"macro stamp failed (non-fatal): {_e}")

    # ── Store scan summary for portal access ──────────────────────────
    scan_summary = {
        "timestamp": report.completed_at,
        "regime": report.regime,
        "confidence": report.confidence,
        "gates_ok": sum(1 for g in report.gates if g.status == "OK"),
        "gates_unavailable": sum(1 for g in report.gates if g.status == "UNAVAILABLE"),
        "gates_total": len(report.gates),
    }
    db.set_setting('_MACRO_SCAN_LAST', json.dumps(scan_summary))

    # ── Streak tracking → admin_alert on sustained data outage ────────
    # Single low-confidence run isn't worth alerting (transient Yahoo
    # blip). N consecutive runs is worth admin attention because it means
    # we've been validating signals against degraded regime data.
    try:
        if report.confidence < GATE5_LOW_CONFIDENCE_THRESHOLD:
            streak = int(db.get_setting('_MACRO_LOW_CONFIDENCE_STREAK') or 0) + 1
            db.set_setting('_MACRO_LOW_CONFIDENCE_STREAK', str(streak))
            if streak >= LOW_CONFIDENCE_STREAK_ALERT:
                # Synthesise a Finding-shaped object for the shared alert
                # router. emit_admin_alert dedups by code so subsequent
                # runs at higher streak don't spam the inbox.
                from types import SimpleNamespace
                f = SimpleNamespace(
                    severity="WARNING",
                    code="MACRO_REGIME_DEGRADED",
                    gate="GATE5_REGIME",
                    message=(f"Macro regime degraded for {streak} consecutive run(s) "
                             f"— confidence {report.confidence:.2f}, regime={report.regime}"),
                    detail=("Gate 1/2 (VIX/yield) depend on Yahoo Finance which has "
                            "no Alpaca fallback on the free feed. Sustained UNAVAILABLE "
                            "→ validator chain operating on stale or missing macro "
                            "context. Investigate Yahoo connectivity from pi5."),
                    meta={
                        "streak": streak,
                        "confidence": report.confidence,
                        "regime": report.regime,
                        "gates_unavailable": scan_summary["gates_unavailable"],
                    },
                    customer_id=None,
                )
                emit_admin_alert(_master_db(), f,
                                 source_agent='macro_regime_agent',
                                 category='system')
                log.warning(f"low-confidence streak={streak} → admin_alert raised")
        else:
            db.set_setting('_MACRO_LOW_CONFIDENCE_STREAK', '0')
    except Exception as _e:
        log.warning(f"streak tracking failed (non-fatal): {_e}")

    # ── Log gate results ──────────────────────────────────────────────
    for g in report.gates:
        if g.status == "UNAVAILABLE":
            log.warning(f"  [{g.gate}] UNAVAILABLE: {g.detail}")
        elif g.status == "WARNING":
            log.warning(f"  [{g.gate}] WARNING: {g.signal} — {g.detail}")
        else:
            log.info(f"  [{g.gate}] {g.signal} — {g.detail}")

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"MACRO REGIME — {report.regime} (confidence={report.confidence:.2f})")
    log.info(f"  VIX={vix_value}  Yield spread={yield_spread}  "
             f"Breadth={breadth_result}  Rotation={rotation_result}")
    log.info("=" * 70)

    db.log_heartbeat("macro_regime_agent", "OK")
    db.log_event(
        "AGENT_COMPLETE",
        agent="Macro Regime",
        details=(f"regime={report.regime}, confidence={report.confidence:.2f}, "
                 f"vix={vix_value}, yield_spread={yield_spread}, "
                 f"breadth={breadth_result}, rotation={rotation_result}")
    )

    # ── Monitor heartbeat POST ────────────────────────────────────────
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="macro_regime_agent", status="OK")
    except Exception as e:
        log.warning(f"Monitor heartbeat POST failed: {e}")

    return report


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('macro_regime_agent', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

    parser = argparse.ArgumentParser(description='Synthos — Macro Regime Agent')
    # Macro regime is system-wide — the scheduler passes --customer-id to
    # every agent uniformly but this one runs once per scheduler tick and
    # writes to the shared DB. We accept the arg to keep the scheduler
    # contract consistent and just don't use it.
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (accepted for scheduler compat; macro regime is shared)')
    args = parser.parse_args()

    acquire_agent_lock("retail_macro_regime_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
