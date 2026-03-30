"""
agent2_research.py — Scout (Research Agent)
Synthos · Agent 2

Runs:
  - Every hour during market hours (9am-4pm ET weekdays)
  - Every 4 hours overnight

Responsibilities:
  - Fetch congressional disclosures and legislative news from free APIs
  - Score signals through 18-gate deterministic classification spine
  - Apply per-member reliability weight → adjusted score
  - Pull 1yr price history for ticker + industry/sector ETF
  - Write all signals to news_feed table for portal display
  - Announce for peer interrogation (UDP broadcast, 30s wait)
  - Post metadata to company Pi if COMPANY_SUBSCRIPTION=true
  - Queue validated signals for Bolt

No AI inference in any decision path — all decisions are rule-based and traceable.
Every article produces a structured NewsDecisionLog recording each gate's inputs
and result.

Usage:
  python3 agent2_research.py
  python3 agent2_research.py --session=overnight
"""

import os
import re
import sys
import json
import time
import socket
import logging
import argparse
import requests
import feedparser
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from database import get_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
# ANTHROPIC_API_KEY removed — Scout uses no LLM in classification decisions.
# All decisions are rule-based and traceable. See gate1-gate18 functions.
CONGRESS_API_KEY     = os.environ.get('CONGRESS_API_KEY', '')
ALPACA_API_KEY       = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY    = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL      = "https://data.alpaca.markets"
ET                   = ZoneInfo("America/New_York")
MAX_RETRIES          = 3
REQUEST_TIMEOUT      = 10

COMPANY_SUBSCRIPTION = os.environ.get('COMPANY_SUBSCRIPTION', 'true').lower() == 'true'
MONITOR_URL          = os.environ.get('MONITOR_URL', '').rstrip('/')
MONITOR_TOKEN        = os.environ.get('MONITOR_TOKEN', '')

MIN_SIGNAL_THRESHOLD  = float(os.environ.get('MIN_SIGNAL_THRESHOLD', '0.1'))
INTERROGATION_PORT    = 5556
INTERROGATION_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('agent2_research')


# ── RESEARCH CONTROLS ─────────────────────────────────────────────────────

class ResearchControls:
    """
    All configurable thresholds for the 18-gate news classification spine.
    Loaded from environment variables with documented defaults.
    """
    # Gate 1 — System
    MAX_NEWS_AGE_HOURS   = float(os.environ.get('MAX_NEWS_AGE_HOURS', '24'))
    DUPLICATE_THRESHOLD  = float(os.environ.get('DUPLICATE_THRESHOLD', '0.60'))

    # Gate 2 — Benchmark
    SPX_TICKER           = os.environ.get('SPX_TICKER', 'SPY')
    SPX_SMA_SHORT        = int(os.environ.get('SPX_SMA_SHORT', '20'))
    SPX_SMA_LONG         = int(os.environ.get('SPX_SMA_LONG', '50'))
    SPX_VOL_THRESHOLD    = float(os.environ.get('SPX_VOL_THRESHOLD', '0.018'))  # ATR/price
    SPX_DRAWDOWN_THRESH  = float(os.environ.get('SPX_DRAWDOWN_THRESH', '0.05'))
    # TODO: DATA_DEPENDENCY — VIX integration; using SPX ATR as proxy until feed available

    # Gate 3 — Eligibility
    CREDIBILITY_TIER_MAX = int(os.environ.get('CREDIBILITY_TIER_MAX', '3'))  # tiers 1-3 pass
    MIN_WORD_COUNT       = int(os.environ.get('MIN_WORD_COUNT', '8'))

    # Gate 5 — Event detection
    BREAKING_BURST_THRESH = int(os.environ.get('BREAKING_BURST_THRESH', '3'))
    FOLLOW_UP_SIMILARITY  = float(os.environ.get('FOLLOW_UP_SIMILARITY', '0.50'))
    # TODO: DATA_DEPENDENCY — automated event calendar; using manual exclusion list

    # Gate 6 — Sentiment
    POSITIVE_THRESHOLD    = float(os.environ.get('POSITIVE_THRESHOLD', '0.10'))
    NEGATIVE_THRESHOLD    = float(os.environ.get('NEGATIVE_THRESHOLD', '-0.10'))
    SENTIMENT_CONF_MIN    = float(os.environ.get('SENTIMENT_CONF_MIN', '0.25'))
    MIXED_MIN_THRESHOLD   = float(os.environ.get('MIXED_MIN_THRESHOLD', '0.05'))

    # Gate 7 — Novelty
    NOVELTY_THRESHOLD     = float(os.environ.get('NOVELTY_THRESHOLD', '0.40'))
    MIN_INCREMENTAL_INFO  = float(os.environ.get('MIN_INCREMENTAL_INFO', '0.25'))

    # Gate 10 — Credibility
    MIN_CONFIRMATIONS     = int(os.environ.get('MIN_CONFIRMATIONS', '2'))

    # Gate 11 — Timing
    TRADEABLE_WINDOW_HOURS = float(os.environ.get('TRADEABLE_WINDOW_HOURS', '8'))

    # Gate 12 — Crowding
    CLUSTER_VOL_THRESHOLD  = int(os.environ.get('CLUSTER_VOL_THRESHOLD', '8'))

    # Gate 13 — Contradiction
    UNCERTAINTY_DENSITY_MAX  = float(os.environ.get('UNCERTAINTY_DENSITY_MAX', '0.12'))
    HEAD_BODY_MISMATCH_LIMIT = float(os.environ.get('HEAD_BODY_MISMATCH_LIMIT', '0.30'))


# ── KEYWORD DICTIONARIES ──────────────────────────────────────────────────

_POSITIVE = frozenset({
    'beat', 'beats', 'exceeded', 'exceeds', 'surpassed', 'surpasses',
    'growth', 'expanding', 'expansion', 'upgrade', 'upgraded', 'approved',
    'approval', 'awarded', 'wins', 'won', 'partnership', 'bullish',
    'recovery', 'recovers', 'strong', 'strength', 'gains', 'record',
    'outperform', 'raised', 'raises', 'increased', 'increases', 'boost',
    'boosted', 'profitable', 'profit', 'optimistic', 'upbeat', 'momentum',
    'accelerating', 'breakout', 'advancing', 'progress', 'breakthrough',
    'contract', 'deal', 'launch', 'launched', 'dividend', 'buyback',
    'acquisition', 'merger', 'positive', 'upside', 'rebound', 'rally',
})

_NEGATIVE = frozenset({
    'missed', 'miss', 'below', 'declined', 'decline', 'declining',
    'loss', 'losses', 'layoffs', 'layoff', 'downgrade', 'downgraded',
    'investigation', 'lawsuit', 'sanction', 'sanctions', 'bearish',
    'concern', 'concerns', 'weak', 'weakness', 'fell', 'fall', 'falling',
    'cut', 'cuts', 'reduce', 'reduces', 'suspend', 'suspends', 'halted',
    'halt', 'recall', 'warning', 'default', 'bankruptcy', 'bankrupt',
    'crisis', 'crash', 'downside', 'failure', 'failed', 'disappointing',
    'disappoint', 'headwinds', 'headwind', 'negative', 'selloff',
    'plunged', 'plunge', 'collapse', 'collapsed', 'probe', 'fine',
    'penalty', 'charged', 'indicted', 'delisting', 'downward',
})

_UNCERTAINTY = frozenset({
    'may', 'might', 'could', 'possible', 'possibly', 'potential',
    'potentially', 'uncertain', 'uncertainty', 'unclear', 'unknown',
    'pending', 'conditional', 'tentative', 'alleged', 'reportedly',
    'rumored', 'expected', 'anticipated', 'likely', 'unlikely',
    'if', 'whether', 'contingent', 'provisional', 'unconfirmed',
})

_MACRO_TERMS = (
    'federal reserve', 'interest rate', 'inflation', 'gdp', 'unemployment',
    'fomc', 'central bank', 'monetary policy', 'rate hike', 'rate cut',
    'yield curve', 'treasury', 'deficit', 'fiscal', 'jobs report',
    'payroll', 'recession', 'stagflation', 'quantitative easing',
)

_EARNINGS_TERMS = (
    'earnings', 'revenue', ' eps ', 'guidance', 'quarterly results',
    'net income', 'operating income', 'full year', 'annual results',
    'beat estimates', 'missed estimates', 'first quarter', 'second quarter',
    'third quarter', 'fourth quarter',
)

_GEOPOLITICAL_TERMS = (
    'sanctions', 'tariff', 'election', 'diplomacy', 'geopolitical',
    'military', 'invasion', 'nato', 'trade war', 'embargo', 'ceasefire',
    'escalation', 'missile', 'nuclear',
)

_REGULATORY_TERMS = (
    ' sec ', ' doj ', ' ftc ', ' fda ', ' epa ', 'legislation', 'compliance',
    'antitrust', 'investigation', 'enforcement action', 'class action',
    'subpoena', 'consent decree', 'indictment',
)

_PRIMARY_SOURCE_SIGNALS = frozenset({
    'filing', 'press release', 'official statement', 'transcript',
    'form 4', 'sec filing', '8-k', '10-k', '10-q', 'annual report',
    'confirmed by', 'announced by', 'congress.gov', 'sec.gov',
    'federal register', 'capitol trades',
})


# ── SECTOR / ETF MAPS ─────────────────────────────────────────────────────

SECTOR_ETF_MAP = {
    "technology":             ("QQQ",  "XLK"),
    "defense":                ("ITA",  "XLI"),
    "healthcare":             ("IBB",  "XLV"),
    "energy":                 ("XOP",  "XLE"),
    "financials":             ("KRE",  "XLF"),
    "materials":              ("PICK", "XLB"),
    "industrials":            ("XLI",  "XLI"),
    "consumer staples":       ("XLP",  "XLP"),
    "consumer discretionary": ("XLY",  "XLY"),
    "real estate":            ("XLRE", "XLRE"),
    "utilities":              ("XLU",  "XLU"),
    "communication":          ("XLC",  "XLC"),
}

CONFIDENCE_NUMERIC = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "NOISE": 0.0}

SECTOR_TICKER_MAP = {
    "defense":     ["LMT", "RTX", "NOC", "GD", "BA", "KTOS", "LHX"],
    "technology":  ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMD", "INTC"],
    "healthcare":  ["LLY", "JNJ", "UNH", "PFE", "ABBV", "MRK"],
    "energy":      ["XOM", "CVX", "NEE", "SO", "DUK"],
    "financials":  ["JPM", "BAC", "WFC", "GS", "KRE"],
    "materials":   ["MP", "ALB", "FCX", "NEM"],
    "industrials": ["DE", "CAT", "GE", "HON"],
}


# ── BENCHMARK REGIME ──────────────────────────────────────────────────────

@dataclass
class BenchmarkRegime:
    trend:           str  = "NEUTRAL"   # UP / DOWN / NEUTRAL
    volatility:      str  = "NORMAL"    # HIGH / NORMAL
    drawdown_active: bool = False
    spx_price:       float = 0.0
    raw:             dict = field(default_factory=dict)


# ── NEWS DECISION LOG ─────────────────────────────────────────────────────

class NewsDecisionLog:
    """
    Records the classification decision for every processed article/disclosure.
    Human-readable + machine-readable output for regulatory audit.

    # FLAG — LOG WRITE LOCATION: Currently written via db.log_event() to
    # system_log table. A dedicated news_decisions table is recommended
    # for regulatory export and volume management. Tracked as future work.
    """

    def __init__(self, headline, source, source_tier, ticker=None):
        self.headline    = (headline or "")[:120]
        self.source      = source
        self.source_tier = source_tier
        self.ticker      = ticker
        self.gates       = []
        self.notes       = []
        self.decision    = None
        self.confidence  = None
        self.reason      = None
        self.ts          = datetime.now(ET).isoformat()

    def gate(self, num, name, inputs, result, reason=""):
        self.gates.append({
            "gate":   f"GATE {num:02d} — {name}",
            "inputs": {k: str(v) for k, v in (inputs or {}).items()},
            "result": str(result),
            "reason": reason,
        })
        return self

    def decide(self, classification, confidence, reason):
        self.decision   = classification
        self.confidence = confidence
        self.reason     = reason
        return self

    def note(self, text):
        self.notes.append(text)
        return self

    def to_human(self):
        tier_label = {1: "OFFICIAL", 2: "WIRE", 3: "PRESS", 4: "OPINION"}.get(
            self.source_tier, "UNKNOWN"
        )
        lines = [
            f"NEWS CLASSIFICATION — {self.ticker or 'NO-TICKER'} @ {self.ts}",
            f"  Headline  : {self.headline}",
            f"  Source    : {self.source} (Tier {self.source_tier} — {tier_label})",
        ]
        for g in self.gates:
            lines.append(f"  {g['gate']:<38}: {g['result']}")
            if g.get("reason"):
                lines.append(f"    {'reason':<34}: {g['reason']}")
        for n in self.notes:
            lines.append(f"  NOTE: {n}")
        lines.append(f"  {'DECISION':<38}: {self.decision} | confidence={self.confidence}")
        lines.append(f"  {'REASON':<38}: {self.reason}")
        return "\n".join(lines)

    def to_machine(self):
        return {
            "ts": self.ts, "ticker": self.ticker, "headline": self.headline,
            "source": self.source, "source_tier": self.source_tier,
            "gates": self.gates, "notes": self.notes,
            "decision": self.decision, "confidence": self.confidence,
            "reason": self.reason,
        }

    def commit(self, db):
        log.info(f"[NEWS_DECISION]\n{self.to_human()}")
        try:
            db.log_event("NEWS_CLASSIFIED", agent="Scout",
                         details=json.dumps(self.to_machine()))
        except Exception as e:
            log.debug(f"NewsDecisionLog.commit failed (non-fatal): {e}")


# ── RETRY HELPERS ─────────────────────────────────────────────────────────

def fetch_with_retry(url, params=None, headers=None, max_retries=MAX_RETRIES):
    """Fetch a URL with exponential backoff. Returns response or None."""
    last_error = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"Fetch failed ({url[:60]}) attempt {attempt+1}/{max_retries}"
                            f" — retrying in {wait}s: {e}")
                time.sleep(wait)
    log.error(f"Fetch permanently failed after {max_retries} attempts: "
              f"{url[:80]} — {last_error}")
    return None


# ── TEXT UTILITIES ────────────────────────────────────────────────────────

def _tokenize(text):
    """Lowercase word tokens from text."""
    return re.findall(r'\b[a-z]{2,}\b', (text or "").lower())


def _jaccard(text_a, text_b):
    """Word-level Jaccard similarity between two strings."""
    a = set(_tokenize(text_a))
    b = set(_tokenize(text_b))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _count_keywords(tokens, keyword_set):
    """Count token hits against a frozenset of keywords."""
    return sum(1 for t in tokens if t in keyword_set)


def _match_phrases(text, phrase_list):
    """Check how many phrases from a list appear in text (lowercased)."""
    text_lower = text.lower()
    return sum(1 for p in phrase_list if p in text_lower)


# ── STALENESS ─────────────────────────────────────────────────────────────

def get_staleness(tx_date_str, disc_date_str):
    """Returns (staleness_label, discount_fraction) based on disclosure delay."""
    try:
        tx   = datetime.strptime(tx_date_str,   '%Y-%m-%d')
        disc = datetime.strptime(disc_date_str, '%Y-%m-%d')
        days = (disc - tx).days
    except Exception:
        return "Unknown", 0.0

    if days <= 3:  return "Fresh",   0.0
    if days <= 7:  return "Aging",   0.15
    if days <= 14: return "Stale",   0.30
    return "Expired", 0.50


# ── MEMBER WEIGHT ─────────────────────────────────────────────────────────

def apply_member_weight(base_confidence, member_weight):
    """
    Apply member reliability weight to base confidence score.
    Returns (adjusted_text, adjusted_numeric).
    Weight floor 0.5, ceiling 1.5. Requires 5+ trades before weight deviates.

    # FLAG: member_weight interaction with news classification — currently applied
    # after Gate 18 as a final adjustment. Future: integrate into Gate 10 (credibility).
    """
    base_numeric = CONFIDENCE_NUMERIC.get(base_confidence, 0.0)
    adj_numeric  = round(base_numeric * member_weight, 4)
    if   adj_numeric >= 0.85: adj_text = "HIGH"
    elif adj_numeric >= 0.45: adj_text = "MEDIUM"
    elif adj_numeric >= 0.10: adj_text = "LOW"
    else:                     adj_text = "NOISE"
    return adj_text, adj_numeric


# ── PRICE HISTORY ─────────────────────────────────────────────────────────

def _alpaca_bars(ticker, days):
    """Fetch daily OHLCV bars from Alpaca Data API. Returns list or []."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []
    end   = datetime.now(ET).strftime('%Y-%m-%dT00:00:00Z')
    start = (datetime.now(ET) - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')
    url   = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars"
    params = {"timeframe": "1Day", "start": start, "end": end,
               "limit": min(days, 365), "feed": "iex"}
    headers = {"APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception as e:
        log.debug(f"Alpaca bars fetch failed ({ticker}): {e}")
    return []


def _summarise_bars(bars):
    """Compute summary stats from bar list. Returns dict or None."""
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    vols   = [b.get("v", 0) for b in bars]
    last, first = closes[-1], closes[0]
    high   = max(b["h"] for b in bars)
    low    = min(b["l"] for b in bars)
    avg_v  = round(sum(vols) / len(vols), 0) if vols else 0
    chg    = round((last - first) / first * 100, 2) if first else 0.0
    return {"last_close": round(last, 2), "change_pct_1yr": chg,
             "high_52w": round(high, 2), "low_52w": round(low, 2),
             "avg_volume": int(avg_v), "bars_available": len(bars)}


def fetch_price_history_1yr(ticker, industry_etf, sector_etf):
    """
    Pull 1yr daily bars for ticker + industry ETF + sector ETF.
    Returns (summary_dict, tickers_pulled_list).
    """
    tickers_pulled = []
    summaries = {}
    for sym in {ticker, industry_etf, sector_etf}:
        if not sym:
            continue
        bars = _alpaca_bars(sym, days=365)
        s    = _summarise_bars(bars)
        if s:
            summaries[sym] = s
            tickers_pulled.append(sym)
            log.info(f"[PRICE HISTORY] {sym}: {s['bars_available']} bars, "
                     f"{s['change_pct_1yr']:+.1f}% 1yr")
        else:
            log.debug(f"[PRICE HISTORY] No bars for {sym}")
    return summaries, tickers_pulled


# ── SECTOR / TICKER IDENTIFICATION ────────────────────────────────────────

def identify_industry_etf(ticker, sector):
    """Map ticker/sector to (industry_ETF, sector_ETF)."""
    sector_lower = (sector or "").lower()
    for key, (ind_etf, sec_etf) in SECTOR_ETF_MAP.items():
        if key in sector_lower:
            return ind_etf, sec_etf
    for sec_name, tickers in SECTOR_TICKER_MAP.items():
        if ticker.upper() in tickers:
            return SECTOR_ETF_MAP.get(sec_name.lower(), ('', ''))
    return '', ''


def extract_ticker_from_headline(headline, existing_ticker=None):
    """Returns existing ticker if provided; otherwise infers from headline."""
    if existing_ticker and existing_ticker.upper() not in ("", "UNKNOWN"):
        return existing_ticker.upper()
    headline_lower = headline.lower()
    for sector, tickers in SECTOR_TICKER_MAP.items():
        if sector in headline_lower:
            return tickers[0]
    for m in re.findall(r'\b([A-Z]{2,5})\b', headline):
        for tickers in SECTOR_TICKER_MAP.values():
            if m in tickers:
                return m
    return None


# ── SOURCE FETCHERS ───────────────────────────────────────────────────────

def fetch_capitol_trades():
    """Fetch recent congressional trades from Capitol Trades free tier."""
    url = "https://capitoltrades.com/api/trades"
    params = {"pageSize": 50, "page": 1}
    r = fetch_with_retry(url, params=params)
    if not r:
        return []
    try:
        data = r.json()
        results = []
        for t in data.get("data", []):
            results.append({
                "ticker":      t.get("issuer", {}).get("tickerSymbol", ""),
                "company":     t.get("issuer", {}).get("name", ""),
                "politician":  f"{t.get('politician', {}).get('name', '')} "
                               f"({t.get('politician', {}).get('party', '')}·"
                               f"{t.get('politician', {}).get('chamber', '')})",
                "tx_date":     t.get("txDate", ""),
                "disc_date":   t.get("pubDate", ""),
                "tx_type":     t.get("txType", ""),
                "amount":      t.get("value", ""),
                "source":      "Capitol Trades API",
                "source_tier": 1,
                "source_url":  "https://capitoltrades.com",
                "headline":    (f"{t.get('politician', {}).get('name', 'Unknown')} "
                               f"{t.get('txType', 'traded')} "
                               f"{t.get('issuer', {}).get('tickerSymbol', '')}"),
            })
        log.info(f"Capitol Trades: fetched {len(results)} disclosures")
        return results
    except Exception as e:
        log.error(f"Capitol Trades parse error: {e}")
        return []


def fetch_congress_gov_activity():
    """Fetch recent bill activity from Congress.gov API."""
    if not CONGRESS_API_KEY:
        log.warning("No CONGRESS_API_KEY set — skipping Congress.gov fetch")
        return []
    results = []
    url = "https://api.congress.gov/v3/bill"
    params = {"api_key": CONGRESS_API_KEY, "format": "json",
               "limit": 20, "sort": "updateDate+desc"}
    r = fetch_with_retry(url, params=params)
    if r:
        try:
            for bill in r.json().get("bills", []):
                title = bill.get("title", "")
                if not title:
                    continue
                sponsors = bill.get("sponsors", [{}])
                results.append({
                    "headline":    title,
                    "source":      "Congress.gov API",
                    "source_tier": 1,
                    "source_url":  "https://api.congress.gov",
                    "politician":  sponsors[0].get("fullName", "Multiple sponsors")
                                   if sponsors else "Multiple sponsors",
                    "tx_date":     bill.get("introducedDate", ""),
                    "disc_date":   (bill.get("updateDate", "")[:10]
                                   if bill.get("updateDate") else ""),
                    "raw":         bill,
                })
        except Exception as e:
            log.error(f"Congress.gov parse error: {e}")
    log.info(f"Congress.gov: fetched {len(results)} bill actions")
    return results


def fetch_federal_register():
    """Fetch recent procurement and regulatory notices from Federal Register."""
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "per_page": 20, "order": "newest",
        "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url"],
        "conditions[type][]": ["Notice", "Proposed Rule"],
    }
    r = fetch_with_retry(url, params=params)
    if not r:
        return []
    try:
        results = []
        for doc in r.json().get("results", []):
            results.append({
                "headline":    doc.get("title", ""),
                "subhead":     (doc.get("abstract", "") or "")[:120],
                "source":      "Federal Register API",
                "source_tier": 1,
                "source_url":  doc.get("html_url", "https://www.federalregister.gov/api/v1"),
                "politician":  ", ".join(a.get("name", "") for a in doc.get("agencies", [])[:2]),
                "disc_date":   doc.get("publication_date", ""),
                "tx_date":     doc.get("publication_date", ""),
            })
        log.info(f"Federal Register: fetched {len(results)} notices")
        return results
    except Exception as e:
        log.error(f"Federal Register parse error: {e}")
        return []


def get_rss_feeds():
    """Return RSS feed list. Reads RSS_FEEDS_JSON env var if set."""
    rss_json = os.environ.get('RSS_FEEDS_JSON', '')
    if rss_json:
        try:
            custom = json.loads(rss_json)
            if isinstance(custom, list) and custom:
                log.info(f"Using {len(custom)} custom RSS feeds from RSS_FEEDS_JSON")
                return custom
        except Exception as e:
            log.warning(f"RSS_FEEDS_JSON parse error — using defaults: {e}")
    return [
        ["Reuters RSS",          "https://feeds.reuters.com/reuters/politicsNews", 2],
        ["Associated Press RSS", "https://apnews.com/rss",                         2],
        ["Politico RSS",         "https://www.politico.com/rss/politicopicks.xml", 3],
        ["The Hill RSS",         "https://thehill.com/feed",                       3],
        ["Roll Call RSS",        "https://rollcall.com/feed",                      3],
        ["Bloomberg RSS",        "https://feeds.bloomberg.com/politics/news.rss",  3],
    ]


def fetch_rss_feeds():
    """Fetch and parse all RSS feeds. Returns list of signal dicts."""
    results = []
    for source_name, url, tier in get_rss_feeds():
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                log.warning(f"RSS parse issue: {source_name}")
                continue
            for entry in feed.entries[:5]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                results.append({
                    "headline":    title,
                    "subhead":     entry.get("summary", "")[:120].strip(),
                    "source":      source_name,
                    "source_tier": tier,
                    "source_url":  entry.get("link", url),
                    "politician":  "",
                    "disc_date":   datetime.now().strftime('%Y-%m-%d'),
                    "tx_date":     datetime.now().strftime('%Y-%m-%d'),
                })
            log.info(f"{source_name}: fetched {min(5, len(feed.entries))} articles")
        except Exception as e:
            log.error(f"RSS fetch failed ({source_name}): {e}")
    return results


# ── INTERROGATION BROADCAST ───────────────────────────────────────────────

def announce_for_interrogation(signal_id, ticker, price_summary_json):
    """
    Broadcast HAS_DATA_FOR_INTERROGATION on the local network via UDP.
    Returns True if a peer acknowledged (VALIDATED), False otherwise (UNVALIDATED).
    """
    message = json.dumps({
        "event": "HAS_DATA_FOR_INTERROGATION",
        "signal_id": signal_id, "ticker": ticker,
        "price_summary": price_summary_json,
    }).encode("utf-8")
    response_received = False
    sock = reply_sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(INTERROGATION_TIMEOUT)
        reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reply_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reply_sock.settimeout(INTERROGATION_TIMEOUT)
        try:
            reply_sock.bind(('', INTERROGATION_PORT + 1))
        except OSError:
            pass
        sock.sendto(message, ('<broadcast>', INTERROGATION_PORT))
        log.info(f"[INTERROGATION] Announced {ticker} (signal {signal_id})"
                 f" — waiting {INTERROGATION_TIMEOUT}s for peer")
        deadline = time.time() + INTERROGATION_TIMEOUT
        while time.time() < deadline:
            try:
                data, addr = reply_sock.recvfrom(4096)
                reply = json.loads(data.decode("utf-8"))
                if (reply.get("event") == "INTERROGATION_ACK"
                        and str(reply.get("signal_id")) == str(signal_id)):
                    log.info(f"[INTERROGATION] Peer {addr[0]} acknowledged — VALIDATED")
                    response_received = True
                    break
            except (socket.timeout, Exception):
                break
    except Exception as e:
        log.debug(f"[INTERROGATION] Socket error (non-fatal): {e}")
    finally:
        for s in (sock, reply_sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass
    if not response_received:
        log.info(f"[INTERROGATION] No peer response for {ticker} — UNVALIDATED")
    return response_received


# ── COMPANY POST ──────────────────────────────────────────────────────────

def post_to_company_pi(ticker, signal_id, congress_member, adjusted_score,
                       headline, price_summary, interrogation_status):
    """Fire-and-forget POST of signal metadata to company Pi news intake."""
    if not COMPANY_SUBSCRIPTION or not MONITOR_URL:
        return
    payload = {
        "event": "SCOUT_SIGNAL", "ticker": ticker, "signal_id": signal_id,
        "congress_member": congress_member, "adjusted_score": adjusted_score,
        "headline": headline, "price_summary": price_summary,
        "interrogation_status": interrogation_status,
        "timestamp": datetime.now(ET).isoformat(),
    }
    try:
        requests.post(f"{MONITOR_URL}/api/news-feed", json=payload,
                      headers={"X-Token": MONITOR_TOKEN}, timeout=3)
        log.debug(f"[COMPANY] Metadata posted for {ticker} signal {signal_id}")
    except Exception as e:
        log.debug(f"[COMPANY] Post failed (non-fatal): {e}")


# ── SPX BENCHMARK HELPERS ─────────────────────────────────────────────────

def _compute_sma(closes, window):
    """Simple moving average of last `window` closes."""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _compute_atr(bars, window=14):
    """Average True Range over last `window` bars."""
    if len(bars) < window + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return sum(trs[-window:]) / min(len(trs), window)


# ── GATE 1 — SYSTEM ───────────────────────────────────────────────────────

def gate1_system(item, ctrl, ndl, seen_headlines):
    """
    System gate — data quality checks before any analysis.
    Returns True to PROCEED, False to HALT this item.

    Checks:
      news_source_status — was the item parsed successfully?
      timestamp          — is the article within MAX_NEWS_AGE_HOURS?
      duplicate          — Jaccard similarity against seen headlines
    """
    headline = (item.get("headline") or "").strip()
    subhead  = (item.get("subhead") or "").strip()

    # ── Parse failure check ────────────────────────────────────────────────
    if not headline or len(headline) < 5:
        ndl.gate(1, "SYSTEM", {"headline_len": len(headline)},
                 "HALT", "headline null or too short — parse failure")
        return False

    # ── Timestamp / staleness check ────────────────────────────────────────
    disc_date_str = item.get("disc_date", "")
    news_age_ok   = True
    if disc_date_str:
        try:
            disc_dt   = datetime.strptime(disc_date_str, '%Y-%m-%d')
            age_hours = (datetime.now() - disc_dt).total_seconds() / 3600
            if age_hours > ctrl.MAX_NEWS_AGE_HOURS:
                ndl.gate(1, "SYSTEM",
                         {"disc_date": disc_date_str, "age_hours": f"{age_hours:.1f}",
                          "max": ctrl.MAX_NEWS_AGE_HOURS},
                         "HALT", "article timestamp exceeds MAX_NEWS_AGE_HOURS")
                return False
        except ValueError:
            # Unparseable date — treat as missing (not a hard stop for Tier 1)
            news_age_ok = False

    # ── Duplicate detection ────────────────────────────────────────────────
    full_text = f"{headline} {subhead}"
    best_sim  = max((_jaccard(full_text, h) for h in seen_headlines), default=0.0)
    if best_sim > ctrl.DUPLICATE_THRESHOLD:
        ndl.gate(1, "SYSTEM",
                 {"similarity": f"{best_sim:.2f}", "threshold": ctrl.DUPLICATE_THRESHOLD},
                 "HALT", "duplicate article — similarity above threshold")
        return False

    seen_headlines.append(full_text)

    ndl.gate(1, "SYSTEM",
             {"headline_len": len(headline), "disc_date": disc_date_str or "unknown",
              "age_ok": news_age_ok, "best_sim": f"{best_sim:.2f}"},
             "PROCEED")
    return True


# ── GATE 2 — BENCHMARK ────────────────────────────────────────────────────

def gate2_benchmark(ctrl):
    """
    Benchmark gate — compute SPX regime for the current session.
    Returns BenchmarkRegime. Called once per run, applied to all articles.

    TODO: DATA_DEPENDENCY — VIX threshold not yet integrated; ATR/price used as proxy.
    """
    ndl_stub = []   # local log; no per-article NDL at session level
    bars = _alpaca_bars(ctrl.SPX_TICKER, days=ctrl.SPX_SMA_LONG + 10)
    if not bars:
        log.warning(f"[GATE 2] Benchmark data unavailable for {ctrl.SPX_TICKER}"
                    f" — defaulting to NEUTRAL regime")
        return BenchmarkRegime(trend="NEUTRAL", volatility="NORMAL",
                               drawdown_active=False, raw={"status": "offline"})

    closes    = [b["c"] for b in bars]
    sma_short = _compute_sma(closes, ctrl.SPX_SMA_SHORT)
    sma_long  = _compute_sma(closes, ctrl.SPX_SMA_LONG)
    atr       = _compute_atr(bars)
    spx_price = closes[-1]

    # Trend
    if sma_short is not None and sma_long is not None:
        if sma_short > sma_long:
            trend = "UP"
        elif sma_short < sma_long:
            trend = "DOWN"
        else:
            trend = "NEUTRAL"
    else:
        trend = "NEUTRAL"

    # Volatility (ATR/price ratio proxy for VIX)
    vol_ratio = (atr / spx_price) if (atr and spx_price) else 0.0
    volatility = "HIGH" if vol_ratio > ctrl.SPX_VOL_THRESHOLD else "NORMAL"

    # Drawdown
    rolling_peak  = max(closes)
    drawdown      = (spx_price - rolling_peak) / rolling_peak if rolling_peak else 0.0
    drawdown_active = drawdown <= -ctrl.SPX_DRAWDOWN_THRESH

    regime = BenchmarkRegime(
        trend=trend, volatility=volatility,
        drawdown_active=drawdown_active, spx_price=spx_price,
        raw={"sma_short": sma_short, "sma_long": sma_long,
             "vol_ratio": round(vol_ratio, 4), "drawdown": round(drawdown, 4)},
    )
    log.info(f"[GATE 2] Benchmark regime: trend={trend} vol={volatility} "
             f"drawdown_active={drawdown_active} spx=${spx_price:.2f}")
    return regime


# ── GATE 3 — ELIGIBILITY ──────────────────────────────────────────────────

def gate3_eligibility(item, ctrl, ndl):
    """
    News eligibility filter — reject before analysis if basic quality not met.
    Returns True to proceed, False to skip.

    Checks: source credibility tier, minimum word count, Tier 4 opinion exclusion.
    TODO: DATA_DEPENDENCY — language detection, topic universe filtering.
    """
    source_tier = item.get("source_tier", 2)
    headline    = (item.get("headline") or "")
    subhead     = (item.get("subhead") or "")
    word_count  = len(_tokenize(f"{headline} {subhead}"))

    # Tier 4 — opinion sources always excluded
    if source_tier >= 4:
        ndl.gate(3, "ELIGIBILITY", {"source_tier": source_tier},
                 "SKIP", "Tier 4+ opinion source — excluded")
        return False

    # Source tier above allowed maximum
    if source_tier > ctrl.CREDIBILITY_TIER_MAX:
        ndl.gate(3, "ELIGIBILITY",
                 {"source_tier": source_tier, "max_tier": ctrl.CREDIBILITY_TIER_MAX},
                 "SKIP", "source tier exceeds credibility maximum")
        return False

    # Minimum word count
    if word_count < ctrl.MIN_WORD_COUNT:
        ndl.gate(3, "ELIGIBILITY",
                 {"word_count": word_count, "min": ctrl.MIN_WORD_COUNT},
                 "SKIP", "article below minimum word count — insufficient for signal extraction")
        return False

    ndl.gate(3, "ELIGIBILITY",
             {"source_tier": source_tier, "word_count": word_count},
             "PROCEED")
    return True


# ── GATE 4 — CLASSIFICATION ───────────────────────────────────────────────

def gate4_classification(item, ctrl, ndl):
    """
    Topic classification — identify the article's primary topic category.
    Priority order: company > sector > regulatory > earnings > geopolitical > macro > unknown.
    Returns dict: {topic, scope, entity_match}.
    """
    text = f"{item.get('headline','')} {item.get('subhead','')}".lower()
    ticker = (item.get("ticker") or "").upper()

    macro_hits    = _match_phrases(text, _MACRO_TERMS)
    earnings_hits = _match_phrases(text, _EARNINGS_TERMS)
    geo_hits      = _match_phrases(text, _GEOPOLITICAL_TERMS)
    reg_hits      = _match_phrases(text, _REGULATORY_TERMS)

    # Company-specific (named ticker found)
    if ticker and ticker in {t for tl in SECTOR_TICKER_MAP.values() for t in tl}:
        topic = "company"
        scope = "single_name"
        entity_match = True
    # Sector-specific
    elif any(sec in text for sec in SECTOR_TICKER_MAP):
        topic = "sector"
        scope = "sector_subset"
        entity_match = True
    elif reg_hits >= 1:
        topic = "regulatory"
        scope = "sector_subset"
        entity_match = bool(ticker)
    elif earnings_hits >= 1:
        topic = "earnings"
        scope = "single_name" if ticker else "broad"
        entity_match = bool(ticker)
    elif geo_hits >= 1:
        topic = "geopolitical"
        scope = "broad_market"
        entity_match = False
    elif macro_hits >= 1:
        topic = "macro"
        scope = "broad_market"
        entity_match = False
    else:
        topic = "unknown"
        scope = "unknown"
        entity_match = False

    ndl.gate(4, "CLASSIFICATION",
             {"macro": macro_hits, "earnings": earnings_hits,
              "geo": geo_hits, "reg": reg_hits, "ticker": ticker},
             f"topic={topic} scope={scope}")
    return {"topic": topic, "scope": scope, "entity_match": entity_match}


# ── GATE 5 — EVENT DETECTION ──────────────────────────────────────────────

def gate5_event_detection(item, ctrl, ndl, db, seen_headlines):
    """
    Event detection — classify the article's event type.
    Returns dict: {event_type, breaking, follow_up, rumor, scheduled}.

    TODO: DATA_DEPENDENCY — automated event calendar integration pending.
    Currently uses source urgency and burst count as proxies.
    """
    headline    = item.get("headline", "")
    source_tier = item.get("source_tier", 2)
    subhead     = item.get("subhead", "")
    full_text   = f"{headline} {subhead}".lower()

    # Breaking news: Tier 1 or 2 + short article (wire breaking = brief)
    breaking = source_tier <= 2 and len(_tokenize(full_text)) < 50

    # Follow-up: similarity to recently processed items in this batch
    follow_up_sims = [_jaccard(headline, h) for h in seen_headlines[:-1]]
    follow_up_sim  = max(follow_up_sims, default=0.0)
    follow_up      = follow_up_sim > ctrl.FOLLOW_UP_SIMILARITY

    # Rumor: high uncertainty term density AND Tier 3 source
    tokens         = _tokenize(full_text)
    uncertainty_ct = _count_keywords(tokens, _UNCERTAINTY)
    uncertainty_density = uncertainty_ct / max(len(tokens), 1)
    rumor = source_tier == 3 and uncertainty_density > 0.10

    # Scheduled: TODO: DATA_DEPENDENCY — would use event calendar
    # For now: assume official government publications are scheduled events
    scheduled = item.get("source_tier", 2) == 1

    event_type = "breaking" if breaking else (
                 "follow_up" if follow_up else (
                 "rumor" if rumor else (
                 "scheduled" if scheduled else "unscheduled")))

    ndl.gate(5, "EVENT_DETECTION",
             {"breaking": breaking, "follow_up": f"{follow_up_sim:.2f}",
              "rumor": rumor, "uncertainty_density": f"{uncertainty_density:.3f}",
              "scheduled": scheduled},
             f"event_type={event_type}")
    return {
        "event_type": event_type, "breaking": breaking, "follow_up": follow_up,
        "rumor": rumor, "scheduled": scheduled,
        "uncertainty_density": uncertainty_density,
    }


# ── GATE 6 — SENTIMENT EXTRACTION ────────────────────────────────────────

def gate6_sentiment(item, ctrl, ndl):
    """
    Sentiment extraction — keyword-based scoring.
    Returns dict: {direction, score, confidence, positive_count, negative_count}.
    """
    text   = f"{item.get('headline','')} {item.get('subhead','')} "
    tokens = _tokenize(text)
    total  = max(len(tokens), 1)

    pos_ct = _count_keywords(tokens, _POSITIVE)
    neg_ct = _count_keywords(tokens, _NEGATIVE)

    raw_score = (pos_ct - neg_ct) / total
    # Confidence: proportion of tokens that are sentiment-bearing
    confidence = min((pos_ct + neg_ct) / max(total / 15, 1), 1.0)

    if raw_score > ctrl.POSITIVE_THRESHOLD:
        direction = "POSITIVE"
    elif raw_score < ctrl.NEGATIVE_THRESHOLD:
        direction = "NEGATIVE"
    elif (pos_ct > 0 and neg_ct > 0
          and pos_ct / total > ctrl.MIXED_MIN_THRESHOLD
          and neg_ct / total > ctrl.MIXED_MIN_THRESHOLD):
        direction = "MIXED"
    else:
        direction = "NEUTRAL"

    conf_flag = confidence >= ctrl.SENTIMENT_CONF_MIN

    ndl.gate(6, "SENTIMENT",
             {"pos_tokens": pos_ct, "neg_tokens": neg_ct,
              "score": f"{raw_score:.3f}", "confidence": f"{confidence:.2f}"},
             f"direction={direction} conf_ok={conf_flag}")
    return {
        "direction": direction, "score": round(raw_score, 4),
        "confidence": round(confidence, 4), "conf_ok": conf_flag,
        "positive_count": pos_ct, "negative_count": neg_ct,
    }


# ── GATE 7 — NOVELTY ─────────────────────────────────────────────────────

def gate7_novelty(item, ctrl, ndl, db, seen_headlines):
    """
    Novelty / surprise controls — detect repetitive or already-priced content.
    Returns dict: {novelty_score, is_repetition, new_info_ok}.

    TODO: DATA_DEPENDENCY — 'already priced' detection requires market price
    data correlation with prior articles. Currently based on cluster volume only.
    """
    headline = item.get("headline", "")

    # Compare against seen headlines in current batch
    batch_sims = [_jaccard(headline, h) for h in seen_headlines[:-1]]
    max_batch_sim = max(batch_sims, default=0.0)

    # Also compare against recent DB headlines
    try:
        cutoff = (datetime.now(ET) - timedelta(hours=4)).isoformat()
        with db.conn() as c:
            rows = c.execute(
                "SELECT headline FROM signals WHERE created_at > ? LIMIT 80",
                (cutoff,)
            ).fetchall()
        db_headlines = [r["headline"] for r in rows if r.get("headline")]
        db_sims      = [_jaccard(headline, h) for h in db_headlines]
        max_db_sim   = max(db_sims, default=0.0)
    except Exception:
        max_db_sim = 0.0

    max_sim      = max(max_batch_sim, max_db_sim)
    novelty      = round(1.0 - max_sim, 4)
    is_repetition = novelty < ctrl.MIN_INCREMENTAL_INFO
    new_info_ok   = novelty >= ctrl.NOVELTY_THRESHOLD

    ndl.gate(7, "NOVELTY",
             {"max_similarity": f"{max_sim:.2f}",
              "novelty_score": f"{novelty:.2f}",
              "novelty_threshold": ctrl.NOVELTY_THRESHOLD},
             f"novelty={novelty:.2f} repetition={is_repetition} ok={new_info_ok}")
    return {
        "novelty_score": novelty, "is_repetition": is_repetition,
        "new_info_ok": new_info_ok,
    }


# ── GATE 8 — MARKET IMPACT ESTIMATION ────────────────────────────────────

def gate8_impact(item, topic, regime, ctrl, ndl):
    """
    Market impact estimation — assess breadth and expected duration of impact.
    Returns dict: {scope, horizon, magnitude_est, benchmark_corr}.

    TODO: DATA_DEPENDENCY — historical correlation between topic type and
    realized SPX/sector moves not yet available. Using heuristics.
    """
    scope   = topic.get("scope", "unknown")
    t       = topic.get("topic", "unknown")

    # Impact horizon by topic
    if t in ("regulatory", "geopolitical"):
        horizon = "multi-day"
    elif t == "earnings":
        horizon = "multi-day"
    elif t == "macro":
        horizon = "multi-day"
    else:
        horizon = "intraday"

    # Benchmark correlation estimate (heuristic)
    if t == "macro":
        benchmark_corr = "HIGH"
    elif t == "geopolitical":
        benchmark_corr = "HIGH"
    elif t == "sector":
        benchmark_corr = "MEDIUM"
    elif t == "company":
        benchmark_corr = "LOW"
    else:
        benchmark_corr = "MEDIUM"

    # Magnitude: elevated in high-vol benchmark regime
    magnitude_est = "HIGH" if regime.volatility == "HIGH" else "NORMAL"

    ndl.gate(8, "IMPACT",
             {"scope": scope, "topic": t, "benchmark_vol": regime.volatility},
             f"horizon={horizon} benchmark_corr={benchmark_corr} magnitude={magnitude_est}")
    return {
        "scope": scope, "horizon": horizon,
        "benchmark_corr": benchmark_corr, "magnitude_est": magnitude_est,
    }


# ── GATE 9 — BENCHMARK-RELATIVE INTERPRETATION ───────────────────────────

def gate9_benchmark_relative(sentiment, impact, regime, ctrl, ndl):
    """
    Benchmark-relative interpretation — adjust signal value based on SPX backdrop.
    Returns dict: {interpretation, alpha_signal, overwhelmed_by_benchmark}.
    """
    direction     = sentiment.get("direction", "NEUTRAL")
    benchmark_corr = impact.get("benchmark_corr", "MEDIUM")
    scope         = impact.get("scope", "unknown")

    # Headwind: positive news against a down or high-vol benchmark
    if direction == "POSITIVE" and regime.trend == "DOWN":
        interpretation = "momentum_headwind"
        overwhelmed = benchmark_corr == "HIGH"
    # Tailwind against: negative news in an up-trending benchmark
    elif direction == "NEGATIVE" and regime.trend == "UP":
        interpretation = "counter_trend"
        overwhelmed = benchmark_corr == "HIGH"
    elif direction == "POSITIVE" and regime.trend == "UP":
        interpretation = "aligned"
        overwhelmed = False
    elif direction == "NEGATIVE" and regime.trend == "DOWN":
        interpretation = "aligned"
        overwhelmed = False
    else:
        interpretation = "neutral_backdrop"
        overwhelmed = False

    # Alpha opportunity: company-specific with low benchmark correlation
    alpha_signal = scope == "single_name" and benchmark_corr == "LOW"

    ndl.gate(9, "BENCHMARK_RELATIVE",
             {"sentiment": direction, "spx_trend": regime.trend,
              "benchmark_corr": benchmark_corr, "scope": scope},
             f"interpretation={interpretation} alpha={alpha_signal} overwhelmed={overwhelmed}")
    return {
        "interpretation": interpretation, "alpha_signal": alpha_signal,
        "overwhelmed_by_benchmark": overwhelmed,
    }


# ── GATE 10 — CREDIBILITY & CONFIRMATION ─────────────────────────────────

def gate10_credibility(item, ctrl, ndl, db):
    """
    Credibility and confirmation controls.
    Returns dict: {source_count, has_primary_source, conf_adj, misinformation_risk}.

    TODO: DATA_DEPENDENCY — cross-source claim validation requires multi-source
    aggregation not yet implemented. Current: DB-based source count for same ticker.
    """
    ticker      = (item.get("ticker") or "").upper()
    source_tier = item.get("source_tier", 2)
    text        = f"{item.get('headline','')} {item.get('subhead','')}".lower()

    # Primary source indicators in article text
    primary_hits = sum(1 for s in _PRIMARY_SOURCE_SIGNALS if s in text)
    has_primary_source = primary_hits > 0 or source_tier == 1

    # Source count: how many other recent signals exist for this ticker
    source_count = 1  # this article
    if ticker:
        try:
            cutoff = (datetime.now(ET) - timedelta(hours=8)).isoformat()
            with db.conn() as c:
                row = c.execute("""
                    SELECT COUNT(*) as cnt FROM signals
                    WHERE ticker = ? AND created_at > ?
                """, (ticker, cutoff)).fetchone()
            source_count = (row["cnt"] if row else 0) + 1
        except Exception:
            pass

    confirmed = source_count >= ctrl.MIN_CONFIRMATIONS

    # Misinformation risk: Tier 3 with no primary source and high uncertainty
    tokens = _tokenize(text)
    unc_ct = _count_keywords(tokens, _UNCERTAINTY)
    unc_density = unc_ct / max(len(tokens), 1)
    misinformation_risk = source_tier == 3 and not has_primary_source and unc_density > 0.08

    # Confidence adjustment
    if source_tier == 1:
        conf_adj = "HIGH"
    elif source_tier == 2 and (has_primary_source or confirmed):
        conf_adj = "HIGH"
    elif source_tier == 2:
        conf_adj = "MEDIUM"
    elif source_tier == 3 and has_primary_source and confirmed:
        conf_adj = "MEDIUM"
    else:
        conf_adj = "LOW"

    ndl.gate(10, "CREDIBILITY",
             {"source_tier": source_tier, "source_count": source_count,
              "has_primary_source": has_primary_source, "confirmed": confirmed,
              "misinfo_risk": misinformation_risk},
             f"conf_adj={conf_adj}")
    return {
        "source_count": source_count, "has_primary_source": has_primary_source,
        "conf_adj": conf_adj, "misinformation_risk": misinformation_risk,
        "confirmed": confirmed,
    }


# ── GATE 11 — TIMING ──────────────────────────────────────────────────────

def gate11_timing(item, ctrl, ndl):
    """
    Timing controls — assess publication timing relative to market windows.
    Returns dict: {tradeable, publication_timing, stale}.
    """
    disc_date_str = item.get("disc_date", "")
    now           = datetime.now(ET)

    # Staleness check
    stale   = False
    pub_dt  = None
    if disc_date_str:
        try:
            pub_dt    = datetime.strptime(disc_date_str, '%Y-%m-%d').replace(tzinfo=ET)
            age_hours = (now - pub_dt).total_seconds() / 3600
            stale     = age_hours > ctrl.TRADEABLE_WINDOW_HOURS
        except ValueError:
            pass

    # Publication timing classification
    mkt_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    mkt_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if pub_dt:
        pub_naive = pub_dt.replace(tzinfo=None)
        open_naive  = mkt_open.replace(tzinfo=None)
        close_naive = mkt_close.replace(tzinfo=None)
        if pub_naive < open_naive:
            publication_timing = "premarket"
        elif pub_naive > close_naive:
            publication_timing = "postmarket"
        else:
            publication_timing = "intraday"
    else:
        publication_timing = "unknown"

    tradeable = not stale

    ndl.gate(11, "TIMING",
             {"disc_date": disc_date_str or "unknown",
              "publication_timing": publication_timing,
              "stale": stale},
             f"tradeable={tradeable}")
    return {"tradeable": tradeable, "publication_timing": publication_timing, "stale": stale}


# ── GATE 12 — CROWDING / SATURATION ──────────────────────────────────────

def gate12_crowding(item, ctrl, ndl, db):
    """
    Crowding and saturation controls.
    Returns dict: {cluster_volume, saturated}.

    TODO: DATA_DEPENDENCY — social mention count requires external API.
    Current: DB-based cluster volume for same topic keyword.
    """
    headline = (item.get("headline") or "").lower()
    tokens   = set(_tokenize(headline))

    # Count articles on same topic recently
    cluster_volume = 1
    try:
        cutoff = (datetime.now(ET) - timedelta(hours=4)).isoformat()
        with db.conn() as c:
            rows = c.execute(
                "SELECT headline FROM signals WHERE created_at > ? LIMIT 100",
                (cutoff,)
            ).fetchall()
        for row in rows:
            if row.get("headline"):
                h_tokens = set(_tokenize(row["headline"]))
                if len(tokens & h_tokens) >= 3:
                    cluster_volume += 1
    except Exception:
        pass

    saturated = cluster_volume >= ctrl.CLUSTER_VOL_THRESHOLD

    ndl.gate(12, "CROWDING",
             {"cluster_volume": cluster_volume,
              "threshold": ctrl.CLUSTER_VOL_THRESHOLD},
             f"saturated={saturated}")
    return {"cluster_volume": cluster_volume, "saturated": saturated}


# ── GATE 13 — CONTRADICTION / AMBIGUITY ──────────────────────────────────

def gate13_contradiction(item, ctrl, ndl):
    """
    Contradiction and ambiguity controls.
    Returns dict: {has_contradiction, uncertainty_density, head_body_mismatch}.

    TODO: DATA_DEPENDENCY — analyst view dispersion requires aggregated
    expert consensus data not yet available.
    """
    headline = item.get("headline", "")
    subhead  = item.get("subhead", "")

    # Uncertainty term density
    head_tokens = _tokenize(headline)
    unc_ct      = _count_keywords(head_tokens, _UNCERTAINTY)
    uncertainty_density = unc_ct / max(len(head_tokens), 1)
    high_uncertainty = uncertainty_density > ctrl.UNCERTAINTY_DENSITY_MAX

    # Headline / body sentiment mismatch
    head_pos = _count_keywords(head_tokens, _POSITIVE)
    head_neg = _count_keywords(head_tokens, _NEGATIVE)
    head_dir  = 1 if head_pos > head_neg else (-1 if head_neg > head_pos else 0)

    sub_tokens = _tokenize(subhead)
    sub_pos  = _count_keywords(sub_tokens, _POSITIVE)
    sub_neg  = _count_keywords(sub_tokens, _NEGATIVE)
    sub_dir  = 1 if sub_pos > sub_neg else (-1 if sub_neg > sub_pos else 0)

    # Mismatch: headline positive and subhead negative (or vice versa)
    head_body_mismatch = (bool(sub_tokens) and head_dir != 0 and sub_dir != 0
                          and head_dir != sub_dir)

    has_contradiction = high_uncertainty or head_body_mismatch

    ndl.gate(13, "CONTRADICTION",
             {"uncertainty_density": f"{uncertainty_density:.3f}",
              "head_body_mismatch": head_body_mismatch},
             f"contradiction={has_contradiction}")
    return {
        "has_contradiction": has_contradiction,
        "uncertainty_density": uncertainty_density,
        "head_body_mismatch": head_body_mismatch,
    }


# ── GATE 14 — ACTION CLASSIFICATION ──────────────────────────────────────

def gate14_classification(sentiment, novelty, credibility, impact,
                           contradiction, regime, ctrl, ndl):
    """
    Action classification — determine what to do with the article.
    Returns dict: {classification, confidence_label, reason}.

    Classifications:
      bullish_signal      — positive, credible, novel, relevant
      bearish_signal      — negative, credible, novel, relevant
      relative_alpha      — company-specific, benchmark-neutral
      spx_regime_signal   — broad macro, high benchmark linkage
      watch_only          — mixed or uncertain
      ignore              — low credibility or repetition
    """
    direction   = sentiment.get("direction", "NEUTRAL")
    conf_ok     = sentiment.get("conf_ok", False)
    novelty_ok  = novelty.get("new_info_ok", False)
    repetition  = novelty.get("is_repetition", False)
    conf_adj    = credibility.get("conf_adj", "LOW")
    misinfo     = credibility.get("misinformation_risk", False)
    contradiction_detected = contradiction.get("has_contradiction", False)
    scope       = impact.get("scope", "unknown")
    bench_corr  = impact.get("benchmark_corr", "MEDIUM")
    overwhelmed = False  # set by gate9 — not passed here; TODO: pass regime result

    # ── Discard paths ──────────────────────────────────────────────────────
    if repetition:
        ndl.gate(14, "CLASSIFICATION", {"repetition": True},
                 "ignore", "article is repetition — no new information")
        return {"classification": "ignore", "confidence_label": "NOISE",
                "reason": "repetition — no incremental information"}

    if misinfo:
        ndl.gate(14, "CLASSIFICATION", {"misinfo_risk": True},
                 "ignore", "misinformation risk high — Tier 3, no primary source, high uncertainty")
        return {"classification": "ignore", "confidence_label": "NOISE",
                "reason": "misinformation risk — source quality insufficient"}

    if conf_adj == "LOW" and not novelty_ok:
        ndl.gate(14, "CLASSIFICATION",
                 {"conf_adj": conf_adj, "novelty_ok": novelty_ok},
                 "ignore", "low credibility and low novelty")
        return {"classification": "ignore", "confidence_label": "NOISE",
                "reason": "low credibility and low novelty — ignore"}

    # ── Watch paths ────────────────────────────────────────────────────────
    if contradiction_detected:
        ndl.gate(14, "CLASSIFICATION", {"contradiction": True},
                 "watch_only", "contradiction or high ambiguity detected")
        return {"classification": "watch_only", "confidence_label": "LOW",
                "reason": "contradiction or ambiguity — watch for resolution"}

    if direction in ("NEUTRAL", "MIXED") or not conf_ok:
        ndl.gate(14, "CLASSIFICATION",
                 {"direction": direction, "conf_ok": conf_ok},
                 "watch_only", "neutral or mixed sentiment / low confidence")
        return {"classification": "watch_only", "confidence_label": "LOW",
                "reason": f"sentiment {direction} or confidence below threshold"}

    # ── Signal paths ───────────────────────────────────────────────────────
    # Broad macro with high benchmark linkage
    if scope == "broad_market" and bench_corr == "HIGH":
        classification = "spx_regime_signal"
        confidence_label = "MEDIUM" if conf_adj != "LOW" else "LOW"
        reason = "broad macro with high SPX linkage — benchmark regime signal"

    # Company-specific alpha
    elif scope == "single_name" and bench_corr == "LOW":
        classification = "relative_alpha"
        confidence_label = conf_adj
        reason = (f"company-specific {direction} signal with low benchmark correlation"
                  f" — alpha opportunity")

    # Directional signals
    elif direction == "POSITIVE":
        classification = "bullish_signal"
        confidence_label = conf_adj
        reason = f"positive + credible ({conf_adj}) + novel + relevant"

    elif direction == "NEGATIVE":
        classification = "bearish_signal"
        confidence_label = conf_adj
        reason = f"negative + credible ({conf_adj}) + novel + relevant"

    else:
        classification = "watch_only"
        confidence_label = "LOW"
        reason = "unclassified — defaulting to watch"

    ndl.gate(14, "CLASSIFICATION",
             {"direction": direction, "scope": scope,
              "bench_corr": bench_corr, "conf_adj": conf_adj},
             classification, reason)
    return {"classification": classification, "confidence_label": confidence_label,
            "reason": reason}


# ── GATE 15 — RISK CONTROLS ───────────────────────────────────────────────

def gate15_risk(action, sentiment, regime, event, ctrl, ndl):
    """
    Risk controls — apply discounts that reduce confidence without changing classification.
    Returns dict: {final_confidence, discounts_applied}.
    """
    confidence = action.get("confidence_label", "LOW")
    discounts  = []

    CONFIDENCE_ORDER = ["HIGH", "MEDIUM", "LOW", "NOISE"]

    def _downgrade(conf, reason):
        """Move confidence one step down the scale."""
        idx = CONFIDENCE_ORDER.index(conf) if conf in CONFIDENCE_ORDER else 3
        new_conf = CONFIDENCE_ORDER[min(idx + 1, 3)]
        discounts.append(f"{reason} → {conf} → {new_conf}")
        return new_conf

    # Low sentiment confidence
    if not sentiment.get("conf_ok", True):
        confidence = _downgrade(confidence, "low_sentiment_confidence")

    # High benchmark volatility — downweight all signals
    if regime.volatility == "HIGH":
        confidence = _downgrade(confidence, "benchmark_vol_HIGH")

    # Rumor discount
    if event.get("rumor", False):
        confidence = _downgrade(confidence, "rumor_source")

    # Frozen classification: contradictory updates — keep as watch
    # (handled by Gate 13/14 — no additional action here)

    ndl.gate(15, "RISK_CONTROLS",
             {"input_confidence": action.get("confidence_label"), "discounts": len(discounts)},
             f"final_confidence={confidence}",
             "; ".join(discounts) or "none")
    return {"final_confidence": confidence, "discounts_applied": discounts}


# ── GATE 16 — PERSISTENCE ─────────────────────────────────────────────────

def gate16_persistence(topic, event, ctrl, ndl):
    """
    Persistence controls — classify expected decay rate of the signal's market impact.
    Returns dict: {persistence, decay_rate}.
    """
    t = topic.get("topic", "unknown")

    if t in ("regulatory",):
        persistence = "HIGH"
        decay_rate  = "low"
        reason      = "regulatory/policy change — structural repricing expected"
    elif t == "macro":
        persistence = "HIGH"
        decay_rate  = "low"
        reason      = "macro shift — persistent repricing"
    elif t == "geopolitical":
        persistence = "DYNAMIC"
        decay_rate  = "variable"
        reason      = "geopolitical — update per follow-up flow"
    elif t == "earnings":
        persistence = "MEDIUM"
        decay_rate  = "medium"
        reason      = "earnings one-off — fades unless guidance revision broad"
    elif event.get("breaking"):
        persistence = "LOW"
        decay_rate  = "high"
        reason      = "breaking news — typically transient"
    else:
        persistence = "LOW"
        decay_rate  = "high"
        reason      = "default — assume transient"

    ndl.gate(16, "PERSISTENCE",
             {"topic": t, "breaking": event.get("breaking", False)},
             f"persistence={persistence} decay={decay_rate}", reason)
    return {"persistence": persistence, "decay_rate": decay_rate}


# ── GATE 17 — EVALUATION LOOP ─────────────────────────────────────────────

def gate17_evaluation(item, action, ctrl, ndl, db):
    """
    Evaluation loop — update relevance and confidence based on prior articles
    and record the classification for future comparison.

    TODO: DATA_DEPENDENCY — comparing predicted vs. realized market response
    requires post-trade outcome data. Currently updates relevance score only.
    """
    classification = action.get("classification", "ignore")
    ticker         = (item.get("ticker") or "").upper()

    # Recompute relevance: is ticker still active in signals DB?
    ticker_active = False
    if ticker:
        try:
            with db.conn() as c:
                row = c.execute("""
                    SELECT id FROM signals
                    WHERE ticker = ?
                      AND status NOT IN ('DISCARDED', 'EXPIRED')
                      AND (expires_at IS NULL OR expires_at > ?)
                    LIMIT 1
                """, (ticker, datetime.now(ET).isoformat())).fetchone()
            ticker_active = row is not None
        except Exception:
            pass

    # If same article type has historically been noisy, note it
    # TODO: DATA_DEPENDENCY — event_class accuracy tracking not yet implemented
    accuracy_note = "accuracy_tracking_pending"

    ndl.gate(17, "EVALUATION_LOOP",
             {"classification": classification, "ticker_active": ticker_active},
             f"relevance_ok={ticker_active or classification in ('spx_regime_signal','macro')}",
             accuracy_note)
    return {"ticker_active": ticker_active}


# ── GATE 18 — OUTPUT CONTROLS ─────────────────────────────────────────────

def gate18_output(action, risk, regime, topic, ndl):
    """
    Output controls — shape final output based on confidence level and regime.
    Returns dict: {classification, confidence, explanation, routing}.
    """
    classification  = action.get("classification", "ignore")
    final_confidence = risk.get("final_confidence", "LOW")
    bench_corr       = topic.get("benchmark_corr") if isinstance(topic, dict) else "MEDIUM"

    # Certainty of output
    if final_confidence == "HIGH":
        output_type = "decisive"
    elif final_confidence == "MEDIUM":
        output_type = "probabilistic"
    else:
        output_type = "uncertain"

    # Explanation framing
    if bench_corr == "HIGH" and regime.trend != "NEUTRAL":
        explanation = f"benchmark-first: SPX trend={regime.trend}, signal={classification}"
    else:
        explanation = f"asset-first: {classification} signal, SPX backdrop={regime.trend}"

    # Routing decision
    if classification in ("bullish_signal", "relative_alpha"):
        routing = "QUEUE"
    elif classification in ("watch_only", "spx_regime_signal", "bearish_signal"):
        routing = "WATCH"
    else:
        routing = "DISCARD"

    ndl.gate(18, "OUTPUT",
             {"classification": classification, "final_confidence": final_confidence,
              "output_type": output_type},
             f"routing={routing}", explanation)
    return {
        "classification": classification, "confidence": final_confidence,
        "explanation": explanation, "routing": routing, "output_type": output_type,
    }


# ── CLASSIFICATION → LEGACY CONFIDENCE MAPPING ───────────────────────────

def _output_to_confidence(output):
    """Map Gate 18 classification to legacy HIGH/MEDIUM/LOW/NOISE for DB schema."""
    c = output.get("confidence", "LOW")
    return c if c in ("HIGH", "MEDIUM", "LOW", "NOISE") else "LOW"


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run(session="market"):
    db   = get_db()
    ctrl = ResearchControls()
    now  = datetime.now(ET)
    log.info(f"Scout starting — session={session} time={now.strftime('%H:%M ET')}")

    db.log_event("AGENT_START", agent="Scout", details=f"session={session}")
    db.log_heartbeat("agent2_research", "RUNNING")

    # ── Gate 2: Benchmark regime (session-level, once per run) ────────────
    regime = gate2_benchmark(ctrl)

    # ── Expire stale signals ───────────────────────────────────────────────
    db.expire_old_signals()

    # ── Fetch all sources ─────────────────────────────────────────────────
    all_raw = []
    all_raw.extend(fetch_capitol_trades())

    if session == "market":
        all_raw.extend(fetch_congress_gov_activity())
        all_raw.extend(fetch_federal_register())
        all_raw.extend(fetch_rss_feeds())
    else:
        # Overnight: disclosures + congress only
        all_raw.extend(fetch_congress_gov_activity())

    log.info(f"Fetched {len(all_raw)} raw items across all sources")

    # ── Process each item through 18-gate spine ───────────────────────────
    new_signals  = 0
    queued       = 0
    discarded    = 0
    skipped      = 0
    seen_headlines = []   # for duplicate detection within this run

    for item in all_raw:
        headline    = (item.get("headline") or "").strip()
        source_tier = item.get("source_tier", 2)

        ndl = NewsDecisionLog(
            headline    = headline,
            source      = item.get("source", ""),
            source_tier = source_tier,
            ticker      = item.get("ticker"),
        )

        # ── Gate 1: System ────────────────────────────────────────────────
        if not gate1_system(item, ctrl, ndl, seen_headlines):
            ndl.decide("DISCARD", "NOISE", "gate1_system halt")
            ndl.commit(db)
            skipped += 1
            continue

        # ── Gate 3: Eligibility ───────────────────────────────────────────
        if not gate3_eligibility(item, ctrl, ndl):
            ndl.decide("DISCARD", "NOISE", "gate3_eligibility skip")
            ndl.commit(db)
            skipped += 1
            continue

        # ── Ticker resolution ─────────────────────────────────────────────
        ticker = extract_ticker_from_headline(
            headline, existing_ticker=item.get("ticker")
        )
        if not ticker:
            ndl.gate(0, "TICKER_RESOLUTION", {"headline": headline[:60]},
                     "SKIP", "no ticker resolved")
            ndl.decide("DISCARD", "NOISE", "no ticker resolved")
            ndl.commit(db)
            skipped += 1
            continue
        ndl.ticker = ticker
        item["ticker"] = ticker

        # ── Ancillary data ────────────────────────────────────────────────
        is_amended = (bool(item.get("is_amended"))
                      or any(w in headline.lower()
                             for w in ["amend", "corrected", "revised"]))
        is_spousal = (bool(item.get("is_spousal"))
                      or any(w in (item.get("politician", "")).lower()
                             for w in ["spouse", "joint", "dependent"]))
        staleness, discount = get_staleness(
            item.get("tx_date", ""),
            item.get("disc_date", datetime.now().strftime('%Y-%m-%d')),
        )
        item["staleness"] = staleness
        item["is_amended"] = is_amended
        item["is_spousal"] = is_spousal

        # ── Gates 4-18: Full spine ────────────────────────────────────────
        topic        = gate4_classification(item, ctrl, ndl)
        event        = gate5_event_detection(item, ctrl, ndl, db, seen_headlines)
        sentiment    = gate6_sentiment(item, ctrl, ndl)
        novelty      = gate7_novelty(item, ctrl, ndl, db, seen_headlines)
        impact       = gate8_impact(item, topic, regime, ctrl, ndl)
        interp       = gate9_benchmark_relative(sentiment, impact, regime, ctrl, ndl)
        credibility  = gate10_credibility(item, ctrl, ndl, db)
        timing       = gate11_timing(item, ctrl, ndl)
        crowding     = gate12_crowding(item, ctrl, ndl, db)
        contradiction = gate13_contradiction(item, ctrl, ndl)

        # ── Gate 11: Timing exit ──────────────────────────────────────────
        if not timing["tradeable"]:
            ndl.decide("DISCARD", "NOISE", "gate11_timing: article too old / not tradeable")
            ndl.commit(db)
            skipped += 1
            continue

        # ── Gates 14-18: Classification and output ────────────────────────
        action      = gate14_classification(sentiment, novelty, credibility,
                                            impact, contradiction, regime, ctrl, ndl)
        risk        = gate15_risk(action, sentiment, regime, event, ctrl, ndl)
        persistence = gate16_persistence(topic, event, ctrl, ndl)
        gate17_evaluation(item, action, ctrl, ndl, db)
        output      = gate18_output(action, risk, regime, impact, ndl)

        # ── Member weight (kept — FLAG: integrate into Gate 10 in future) ─
        congress_member = item.get("politician", "")
        member_data     = db.get_member_weight(congress_member) if congress_member else {"weight": 1.0}
        member_weight   = member_data.get("weight", 1.0)
        base_confidence = _output_to_confidence(output)
        adj_text, adj_numeric = apply_member_weight(base_confidence, member_weight)

        routing = output["routing"]
        log.info(f"{ticker} classification={output['classification']} "
                 f"base_conf={base_confidence} weight={member_weight:.2f} "
                 f"adj={adj_text}({adj_numeric:.3f}) routing={routing}")

        # ── Write to news_feed (all signals, regardless of routing) ───────
        sector = item.get("sector", "")
        ind_etf, sec_etf = identify_industry_etf(ticker, sector)
        try:
            db.write_news_feed_entry(
                congress_member = congress_member,
                ticker          = ticker,
                signal_score    = adj_text,
                sentiment_score = sentiment.get("score"),
                raw_headline    = headline,
                metadata        = {
                    "source":          item.get("source"),
                    "source_tier":     source_tier,
                    "staleness":       staleness,
                    "base_confidence": base_confidence,
                    "member_weight":   member_weight,
                    "adj_numeric":     adj_numeric,
                    "is_amended":      is_amended,
                    "is_spousal":      is_spousal,
                    "ind_etf":         ind_etf,
                    "sec_etf":         sec_etf,
                    "classification":  output["classification"],
                    "routing":         routing,
                    "persistence":     persistence["persistence"],
                },
                source = "CONGRESS" if source_tier == 1 else "RSS",
            )
        except Exception as e:
            log.warning(f"news_feed write failed (non-fatal): {e}")

        ndl.decide(routing, adj_text, output["reason"])
        ndl.commit(db)

        # ── Discard path ──────────────────────────────────────────────────
        if routing == "DISCARD" or adj_text == "NOISE":
            db.upsert_signal(
                ticker=ticker, source=item.get("source"),
                source_tier=source_tier, headline=headline,
                confidence="NOISE", staleness=staleness,
                tx_date=item.get("tx_date"), disc_date=item.get("disc_date"),
                is_amended=is_amended, is_spousal=is_spousal,
            )
            discarded += 1
            continue

        # ── Threshold check ───────────────────────────────────────────────
        if adj_numeric < MIN_SIGNAL_THRESHOLD:
            log.info(f"{ticker} below MIN_SIGNAL_THRESHOLD "
                     f"({adj_numeric:.3f} < {MIN_SIGNAL_THRESHOLD}) — dropping")
            discarded += 1
            continue

        # ── Pull 1yr price history ────────────────────────────────────────
        price_summary, tickers_pulled = fetch_price_history_1yr(ticker, ind_etf, sec_etf)
        price_history_used = ",".join(tickers_pulled) if tickers_pulled else ""

        # ── Write signal to DB ────────────────────────────────────────────
        sig_id = db.upsert_signal(
            ticker        = ticker,
            company       = item.get("company"),
            sector        = sector,
            source        = item.get("source"),
            source_tier   = source_tier,
            headline      = headline,
            politician    = congress_member,
            tx_date       = item.get("tx_date"),
            disc_date     = item.get("disc_date"),
            amount_range  = str(item.get("amount", "")),
            confidence    = adj_text,
            staleness     = staleness,
            corroborated  = credibility.get("confirmed", False),
            corroboration_note = output.get("explanation"),
            is_amended    = is_amended,
            is_spousal    = is_spousal,
        )
        if not sig_id:
            continue

        new_signals += 1

        # Annotate with score and price history
        try:
            with db.conn() as c:
                c.execute("""
                    UPDATE signals
                    SET entry_signal_score = ?, price_history_used = ?, updated_at = ?
                    WHERE id = ?
                """, (adj_text, price_history_used, db.now(), sig_id))
        except Exception as e:
            log.warning(f"Signal annotation failed (non-fatal): {e}")

        # ── Interrogation broadcast ───────────────────────────────────────
        price_summary_for_announce = dict(price_summary) if price_summary else {}
        validated = announce_for_interrogation(sig_id, ticker, price_summary_for_announce)
        interrogation_status = "VALIDATED" if validated else "UNVALIDATED"
        del price_summary, price_summary_for_announce

        try:
            with db.conn() as c:
                c.execute(
                    "UPDATE signals SET interrogation_status = ?, updated_at = ? WHERE id = ?",
                    (interrogation_status, db.now(), sig_id)
                )
        except Exception as e:
            log.warning(f"interrogation_status write failed (non-fatal): {e}")

        # ── Post to company Pi ────────────────────────────────────────────
        post_to_company_pi(
            ticker               = ticker,
            signal_id            = sig_id,
            congress_member      = congress_member,
            adjusted_score       = adj_text,
            headline             = headline,
            price_summary        = None,
            interrogation_status = interrogation_status,
        )

        # ── Route to Bolt ─────────────────────────────────────────────────
        if routing in ("QUEUE", "WATCH"):
            db.queue_signal_for_trader(sig_id)
            queued += 1
            log.info(f"Routed to Bolt: {ticker} routing={routing} adj={adj_text} {interrogation_status}")
        else:
            db.discard_signal(sig_id, reason=output.get("reason", "gate18 discard"))
            discarded += 1

    # ── Re-evaluate WATCH signals ─────────────────────────────────────────
    reeval_count = 0
    try:
        with db.conn() as c:
            watching = c.execute("""
                SELECT * FROM signals
                WHERE status IN ('PENDING','WATCHING')
                  AND source_tier IN (2, 3)
                  AND needs_reeval = 1
                  AND expires_at > ?
                ORDER BY created_at ASC
                LIMIT 10
            """, (db.now(),)).fetchall()
            watching = [dict(r) for r in watching]

        for sig in watching:
            log.info(f"Re-evaluating WATCH signal: {sig['ticker']} T{sig['source_tier']} (id={sig['id']})")
            reeval_item = {
                "headline":    sig.get("headline", ""),
                "subhead":     "",
                "source":      sig.get("source", ""),
                "source_tier": sig.get("source_tier", 2),
                "ticker":      sig.get("ticker", ""),
                "disc_date":   sig.get("disc_date", ""),
                "tx_date":     sig.get("tx_date", ""),
                "politician":  sig.get("politician", ""),
            }
            ndl_re = NewsDecisionLog(
                headline=reeval_item["headline"], source=reeval_item["source"],
                source_tier=reeval_item["source_tier"], ticker=reeval_item["ticker"]
            )
            ndl_re.note("re-evaluation of WATCH signal")

            topic_re       = gate4_classification(reeval_item, ctrl, ndl_re)
            event_re       = gate5_event_detection(reeval_item, ctrl, ndl_re, db, [])
            sentiment_re   = gate6_sentiment(reeval_item, ctrl, ndl_re)
            novelty_re     = gate7_novelty(reeval_item, ctrl, ndl_re, db, [])
            impact_re      = gate8_impact(reeval_item, topic_re, regime, ctrl, ndl_re)
            credibility_re = gate10_credibility(reeval_item, ctrl, ndl_re, db)
            contradiction_re = gate13_contradiction(reeval_item, ctrl, ndl_re)
            action_re      = gate14_classification(sentiment_re, novelty_re, credibility_re,
                                                   impact_re, contradiction_re, regime, ctrl, ndl_re)
            risk_re        = gate15_risk(action_re, sentiment_re, regime, event_re, ctrl, ndl_re)
            output_re      = gate18_output(action_re, risk_re, regime, impact_re, ndl_re)

            reeval_count += 1
            with db.conn() as c:
                c.execute("UPDATE signals SET needs_reeval=0, updated_at=? WHERE id=?",
                          (db.now(), sig["id"]))

            ndl_re.decide(output_re["routing"], output_re["confidence"], output_re["reason"])
            ndl_re.commit(db)

            if output_re["routing"] == "QUEUE":
                db.queue_signal_for_trader(sig["id"])
                queued += 1
                log.info(f"Re-eval promoted to queue: {sig['ticker']}")
            elif output_re["routing"] == "DISCARD":
                db.discard_signal(sig["id"], reason=output_re.get("reason", "re-eval discard"))
                discarded += 1
                log.info(f"Re-eval discarded: {sig['ticker']}")

    except Exception as e:
        log.warning(f"Re-evaluation step failed: {e}")

    portfolio = db.get_portfolio()
    log.info(
        f"Run complete — new={new_signals} queued={queued} "
        f"discarded={discarded} skipped={skipped} reeval={reeval_count} "
        f"portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("agent2_research", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="Scout",
        details=f"new={new_signals} queued={queued} discarded={discarded} skipped={skipped}",
        portfolio_value=portfolio['cash'],
    )

    try:
        from heartbeat import write_heartbeat
        write_heartbeat(agent_name="agent2_research", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — Scout (Research Agent)')
    parser.add_argument('--session', choices=['market', 'overnight'], default='market',
                        help='market=full scan, overnight=disclosures+congress only')
    args = parser.parse_args()

    acquire_agent_lock("agent2_research.py")
    try:
        run(session=args.session)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
