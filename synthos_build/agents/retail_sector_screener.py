"""
sector_screener.py — Sector Screening Agent
Synthos · Screening Layer · Version 1.0

Runs before the main trading session (suggested: 9:00 AM ET daily).

What this agent does:
  1. Fetches 5-year return for the target sector ETF (XLE for Energy).
  2. Scores each of the ETF's top holdings using recent price momentum.
  3. Writes candidates to the sector_screening table in the DB.
  4. Issues screening requests so Scout (news) and Pulse (sentiment)
     enrich each candidate on their next run.
  5. Checks for congressional signals already in the DB for these tickers
     and flags them as supplemental context.
  6. Writes a human-readable audit log to logs/logic_audits/.

Current configuration: Energy sector only (XLE ETF).
To add more sectors, extend SECTOR_CONFIG.

Logic audit log: logs/logic_audits/YYYY-MM-DD_sector_screener.log
"""

import os
import sys
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db

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
# Add new sectors here when expanding beyond Energy.
# etf_weight_pct is the approximate % weight within the ETF (used for scoring).
# Source: XLE (Energy Select Sector SPDR) top holdings.

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
}


# ── ALPACA DATA HELPERS ───────────────────────────────────────────────────────

def _alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def fetch_bars(ticker, days):
    """Fetch daily OHLCV bars for ticker going back `days` calendar days."""
    end   = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%SZ')
    start = (datetime.now(ET) - timedelta(days=days + 10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "start": start, "end": end,
                    "limit": days + 20, "feed": "iex"},
            headers=_alpaca_headers(), timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("bars", [])
        log.warning(f"Alpaca bars {ticker}: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"fetch_bars({ticker}): {e}")
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

    Returns (score, reasoning_text).
    """
    closes = [b["c"] for b in bars if "c" in b]
    volumes = [b["v"] for b in bars if "v" in b]
    reasoning = []

    if len(closes) < 60:
        return 0.5, "Insufficient price history — defaulting to neutral score."

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
    return score, " | ".join(reasoning)


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

def run(sector="Energy"):
    """Run the sector screener for the specified sector."""
    config = SECTOR_CONFIG.get(sector)
    if not config:
        log.error(f"No configuration found for sector '{sector}'")
        return

    etf      = config['etf']
    holdings = config['holdings']
    run_id   = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%S')

    log.info(f"Sector Screener starting — {sector} / {etf}")

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
        score, reasoning = calc_momentum_score(bars)
        momentum_details[ticker] = {"score": score, "reasoning": reasoning}
        scored_candidates.append({**holding, "momentum_score": score})
        log.info(f"  {ticker}: momentum score {score:.2f}")

    # Sort by momentum score descending
    scored_candidates.sort(key=lambda x: x['momentum_score'], reverse=True)

    # Step 3: Check congressional signals (supplemental)
    tickers = [cd['ticker'] for cd in scored_candidates]
    log.info("Checking congressional signals for all candidates...")
    congressional_flags = check_congressional_signals(get_db(), tickers)
    flagged = [t for t, f in congressional_flags.items() if f != 'none']
    if flagged:
        log.info(f"Congressional activity found: {flagged}")
    else:
        log.info("No recent congressional activity for these tickers")

    # Step 4: Write to DB
    log.info("Writing candidates to sector_screening table...")
    db = get_db()
    db.write_screening_run(run_id, sector, etf, etf_5yr_return, scored_candidates)

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

    log.info(
        f"Sector Screener complete. {len(scored_candidates)} candidates written. "
        f"Scout and Pulse will enrich on next run."
    )


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Synthos Sector Screener')
    parser.add_argument('--sector', default='Energy',
                        help='Sector to screen (default: Energy)')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (passed by scheduler — screener is shared, value ignored)')
    args, _ = parser.parse_known_args()
    run(sector=args.sector)
