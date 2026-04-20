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
import json
import logging
import argparse
import requests as _req
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, acquire_agent_lock, release_agent_lock

def _master_db():
    owner_id = os.environ.get('OWNER_CUSTOMER_ID', '')
    if owner_id:
        return get_customer_db(owner_id)
    return get_db()

# ── CONFIG ────────────────────────────────────────────────────────────────
ET              = ZoneInfo("America/New_York")
UTC             = ZoneInfo("UTC")
REQUEST_TIMEOUT = 10

ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL   = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')

YAHOO_HEADERS = {'User-Agent': 'Mozilla/5.0'}

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('macro_regime_agent')


# ── HELPERS ───────────────────────────────────────────────────────────────

def _now_utc():
    return datetime.now(tz=UTC)


def _now_str():
    return datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S')


def _alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


# ── YAHOO FINANCE FETCHERS ───────────────────────────────────────────────

def _fetch_yahoo_chart(symbol, range_str="5d", interval="1d"):
    """
    Fetch OHLCV data from Yahoo Finance v8 chart API.
    Returns list of close prices or empty list on failure.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_str, "interval": interval}
    try:
        r = _req.get(url, params=params, headers=YAHOO_HEADERS, timeout=REQUEST_TIMEOUT)
        try:
            _master_db().log_api_call(
                agent='macro_regime', endpoint=f'/chart/{symbol}',
                method='GET', service='yahoo')
        except Exception as _e:
            log.debug(f"suppressed exception: {_e}")
        if r.status_code != 200:
            log.warning(f"Yahoo chart {symbol}: HTTP {r.status_code}")
            return []
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        # Filter out None values
        return [c for c in closes if c is not None]
    except Exception as e:
        log.warning(f"Yahoo chart fetch failed ({symbol}): {e}")
        return []


def _fetch_yahoo_last_close(symbol):
    """Fetch the most recent close price for a Yahoo symbol. Returns float or None."""
    closes = _fetch_yahoo_chart(symbol, range_str="5d", interval="1d")
    if closes:
        return closes[-1]
    return None


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
    now_utc = datetime.utcnow()
    start = (now_utc - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = _req.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "start": start, "end": end,
                    "limit": days + 10, "feed": "iex"},
            headers=_alpaca_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            _master_db().log_api_call(
                agent='macro_regime', endpoint=f'/v2/stocks/{ticker}/bars',
                method='GET', service='alpaca_data',
                status_code=r.status_code)
        except Exception as _e:
            log.debug(f"suppressed exception: {_e}")
        if r.status_code == 200:
            return r.json().get("bars", []) or []
        log.warning(f"Alpaca bars {ticker}: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Alpaca fetch_bars({ticker}): {e}")
    return []


def _calc_return_from_bars(bars):
    """Calculate return from first to last bar close. Returns float or None."""
    closes = [b["c"] for b in bars if "c" in b]
    if len(closes) < 2:
        return None
    return round((closes[-1] - closes[0]) / closes[0], 4)


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
    Fetch VIX from Yahoo Finance.  Classify level + 5-day trend.
    VIX < 15 = CALM, 15-25 = NORMAL, 25-35 = ELEVATED, > 35 = CRISIS.
    """
    log.info("[GATE 1] VIX regime classification")

    closes = _fetch_yahoo_chart("%5EVIX", range_str="5d", interval="1d")
    if not closes:
        log.warning("[GATE 1] VIX data unavailable")
        report.add(GateResult(
            gate="GATE1_VIX", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail="Could not fetch VIX data from Yahoo Finance"
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
    detail = f"VIX={vix_current:.2f}, 5d trend={trend} ({trend_pct:+.1%})"
    log.info(f"  VIX={vix_current:.2f} level={level} trend={trend} ({trend_pct:+.1%})")

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
    Yield spread = 10Y - 13W.
    Inverted (<0) = contractionary.  Flat (0 to 0.20) = late-cycle.
    Normal (0.20 to 1.50) = neutral.  Steep (>1.50) = expansionary.
    """
    log.info("[GATE 2] Treasury yield curve analysis")

    # ^TNX = 10-year yield (quoted as yield * 10, e.g. 45.2 = 4.52%)
    # ^IRX = 13-week T-bill yield (quoted as yield * 100, e.g. 4.35 = 4.35%)
    tnx_closes = _fetch_yahoo_chart("%5ETNX", range_str="5d", interval="1d")
    irx_closes = _fetch_yahoo_chart("%5EIRX", range_str="5d", interval="1d")

    if not tnx_closes or not irx_closes:
        missing = []
        if not tnx_closes:
            missing.append("10Y (^TNX)")
        if not irx_closes:
            missing.append("13W (^IRX)")
        log.warning(f"[GATE 2] Yield data unavailable: {', '.join(missing)}")
        report.add(GateResult(
            gate="GATE2_YIELD_CURVE", status="UNAVAILABLE", signal="UNAVAILABLE",
            detail=f"Missing: {', '.join(missing)}"
        ))
        return None

    # Yahoo quotes TNX as yield * 10 (e.g. 43.5 = 4.35%)
    yield_10y = tnx_closes[-1] / 10.0
    # Yahoo quotes IRX as yield in percent (e.g. 4.35 = 4.35%)
    yield_13w = irx_closes[-1]

    spread = yield_10y - yield_13w

    # Also check 5d trend in the spread
    if len(tnx_closes) >= 2 and len(irx_closes) >= 2:
        spread_start = (tnx_closes[0] / 10.0) - irx_closes[0]
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
    detail = (f"10Y={yield_10y:.2f}%, 13W={yield_13w:.2f}%, "
              f"spread={spread:+.2f}%, shape={shape}, trend={curve_trend}")
    log.info(f"  10Y={yield_10y:.2f}% 13W={yield_13w:.2f}% spread={spread:+.2f}% "
             f"shape={shape} trend={curve_trend}")

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
    elif (vix_level in ("ELEVATED", "NORMAL") and vix_trend == "FALLING"
          and curve_trend == "STEEPENING"
          and breadth_result in ("BROAD_STRENGTH", "ROTATION_TO_SMALL")):
        regime = "RECOVERY"
        confidence = 0.75
        reason = (f"VIX {vix_level} but falling, curve steepening, "
                  f"breadth={breadth_result}")
        log.info(f"  Regime=RECOVERY — VIX falling + curve steepening + strength")

    # ── CONTRACTION ───────────────────────────────────────────────────
    elif (vix_level in ("ELEVATED", "CRISIS")
          and curve_shape in ("INVERTED", "FLAT")
          and breadth_result in ("BROAD_WEAKNESS", "NARROW_BREADTH")
          and rotation_result in ("RISK_OFF", "NEUTRAL")):
        regime = "CONTRACTION"
        confidence = 0.85
        reason = (f"VIX {vix_level}, curve {curve_shape}, "
                  f"breadth={breadth_result}, rotation={rotation_result}")
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

    # Adjust confidence by data availability
    confidence = round(confidence * (available_gates / 4.0), 2)

    report.add(GateResult(
        gate="GATE5_REGIME", status="OK", signal=regime,
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
    db.set_setting('_MACRO_REGIME', report.regime)
    db.set_setting('_MACRO_REGIME_DETAIL', json.dumps(regime_detail))

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
    parser = argparse.ArgumentParser(description='Synthos — Macro Regime Agent')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (passed by scheduler — agent is shared, value ignored)')
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
