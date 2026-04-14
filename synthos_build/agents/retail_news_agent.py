"""
retail_news_agent.py — News Agent
Synthos · v3.0

Runs:
  - Every hour during market hours (9am-4pm ET weekdays) — via systemd synthos-news.timer
  - Every 4 hours overnight

Responsibilities:
  - Fetch financial news via Alpaca News API (REST historical + streaming)
  - Score signals through 22-gate deterministic classification spine
  - Apply per-member reliability weight → adjusted score
  - Pull 1yr price history for ticker + industry/sector ETF
  - Write all signals to news_feed table for portal display
  - Announce for peer interrogation (UDP broadcast, 30s wait)
  - Post metadata to company Pi if COMPANY_SUBSCRIPTION=true
  - Queue validated signals for Trade Logic

No AI inference in any decision path — all decisions are rule-based and traceable.
Every article produces a structured NewsDecisionLog recording each gate's inputs
and result.

Usage:
  python3 news_agent.py
  python3 news_agent.py --session=overnight
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
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
# ANTHROPIC_API_KEY removed — News agent uses no LLM in classification decisions.
# All decisions are rule-based and traceable. See gate1-gate22 functions.
ALPACA_API_KEY       = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY    = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL      = "https://data.alpaca.markets"

# ── MULTI-TENANT ROUTING ──────────────────────────────────────────────────────
_CUSTOMER_ID: 'str | None' = None

def _db():
    """Return per-customer signals.db if --customer-id was given, else the master customer DB.
    Shared agents (news/sentiment/screener) write to the master customer's DB
    so all customers can read from a single intelligence source."""
    if _CUSTOMER_ID:
        from retail_database import get_customer_db
        return get_customer_db(_CUSTOMER_ID)
    # Default to master customer DB (OWNER_CUSTOMER_ID) for shared intelligence
    owner_id = os.environ.get('OWNER_CUSTOMER_ID', '')
    if owner_id:
        from retail_database import get_customer_db
        return get_customer_db(owner_id)
    return get_db()
ET                   = ZoneInfo("America/New_York")
MAX_RETRIES          = 3
REQUEST_TIMEOUT      = 10

COMPANY_SUBSCRIPTION = os.environ.get('COMPANY_SUBSCRIPTION', 'true').lower() == 'true'
MONITOR_URL          = os.environ.get('MONITOR_URL', '').rstrip('/')
MONITOR_TOKEN        = os.environ.get('MONITOR_TOKEN', '')

MIN_SIGNAL_THRESHOLD  = float(os.environ.get('MIN_SIGNAL_THRESHOLD', '0.1'))
INTERROGATION_PORT    = 5556
INTERROGATION_TIMEOUT = 5

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('news_agent')


# ── RESEARCH CONTROLS ─────────────────────────────────────────────────────

class ResearchControls:
    """
    All configurable thresholds for the 22-gate news classification spine.
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
    TREND_NEUTRAL_BAND   = float(os.environ.get('TREND_NEUTRAL_BAND', '0.002'))
    ROC_LOOKBACK         = int(os.environ.get('ROC_LOOKBACK', '5'))
    # TODO: DATA_DEPENDENCY — VIX integration; using SPX ATR as proxy until feed available

    # Gate 3 — Source Relevance
    CREDIBILITY_TIER_MAX = int(os.environ.get('CREDIBILITY_TIER_MAX', '3'))  # tiers 1-3 pass
    MIN_WORD_COUNT       = int(os.environ.get('MIN_WORD_COUNT', '8'))
    MIN_CREDIBILITY      = float(os.environ.get('MIN_CREDIBILITY', '0.35'))
    MIN_RELEVANCE        = float(os.environ.get('MIN_RELEVANCE', '0.20'))

    # Gate 6 — Event detection
    BREAKING_BURST_THRESH = int(os.environ.get('BREAKING_BURST_THRESH', '3'))
    FOLLOW_UP_SIMILARITY  = float(os.environ.get('FOLLOW_UP_SIMILARITY', '0.50'))
    # TODO: DATA_DEPENDENCY — automated event calendar; using manual exclusion list

    # Gate 7 — Sentiment
    POSITIVE_THRESHOLD    = float(os.environ.get('POSITIVE_THRESHOLD', '0.10'))
    NEGATIVE_THRESHOLD    = float(os.environ.get('NEGATIVE_THRESHOLD', '-0.10'))
    SENTIMENT_CONF_MIN    = float(os.environ.get('SENTIMENT_CONF_MIN', '0.25'))
    MIXED_MIN_THRESHOLD   = float(os.environ.get('MIXED_MIN_THRESHOLD', '0.05'))
    EXAGGERATION_DELTA    = float(os.environ.get('EXAGGERATION_DELTA', '0.15'))

    # Gate 8 — Novelty
    NOVELTY_THRESHOLD     = float(os.environ.get('NOVELTY_THRESHOLD', '0.40'))
    MIN_INCREMENTAL_INFO  = float(os.environ.get('MIN_INCREMENTAL_INFO', '0.25'))
    SURPRISE_THRESHOLD    = float(os.environ.get('SURPRISE_THRESHOLD', '0.65'))

    # Gate 12 — Confirmation
    MIN_CONFIRMATIONS     = int(os.environ.get('MIN_CONFIRMATIONS', '2'))

    # Gate 13 — Timing
    TRADEABLE_WINDOW_HOURS = float(os.environ.get('TRADEABLE_WINDOW_HOURS', '8'))

    # Gate 14 — Crowding
    CLUSTER_VOL_THRESHOLD  = int(os.environ.get('CLUSTER_VOL_THRESHOLD', '8'))
    EXTREME_ATTENTION_MULT = float(os.environ.get('EXTREME_ATTENTION_MULT', '2.0'))

    # Gate 15 — Contradiction
    UNCERTAINTY_DENSITY_MAX  = float(os.environ.get('UNCERTAINTY_DENSITY_MAX', '0.12'))
    HEAD_BODY_MISMATCH_LIMIT = float(os.environ.get('HEAD_BODY_MISMATCH_LIMIT', '0.30'))

    # Gate 18 — Risk discounts (multiplicative, applied to impact_score)
    DISCOUNT_SENTIMENT_CONF  = float(os.environ.get('DISCOUNT_SENTIMENT_CONF', '0.70'))
    DISCOUNT_BENCHMARK_VOL   = float(os.environ.get('DISCOUNT_BENCHMARK_VOL', '0.80'))
    DISCOUNT_NOISY_EVENT     = float(os.environ.get('DISCOUNT_NOISY_EVENT', '0.60'))
    DISCOUNT_SOURCE_LOW      = float(os.environ.get('DISCOUNT_SOURCE_LOW', '0.50'))
    DISCOUNT_CONTRADICTION   = float(os.environ.get('DISCOUNT_CONTRADICTION', '0.50'))

    # Gate 22 — Composite weights
    COMPOSITE_W1             = float(os.environ.get('COMPOSITE_W1', '0.20'))   # impact_score
    COMPOSITE_W2             = float(os.environ.get('COMPOSITE_W2', '0.15'))   # credibility_score
    COMPOSITE_W3             = float(os.environ.get('COMPOSITE_W3', '0.15'))   # novelty_score
    COMPOSITE_W4             = float(os.environ.get('COMPOSITE_W4', '0.20'))   # sentiment_confidence
    COMPOSITE_W5             = float(os.environ.get('COMPOSITE_W5', '0.15'))   # confirmation_score
    COMPOSITE_W6             = float(os.environ.get('COMPOSITE_W6', '0.10'))   # (1-crowding_discount)
    COMPOSITE_W7             = float(os.environ.get('COMPOSITE_W7', '0.05'))   # (1-ambiguity_score)
    COMPOSITE_QUALITY_THRESH = float(os.environ.get('COMPOSITE_QUALITY_THRESH', '0.45'))


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
    'confirmed by', 'announced by', 'sec.gov', 'alpaca news',
})

_OPINION_SIGNALS = frozenset({
    'opinion', 'analysis', 'commentary', 'editorial', 'column',
    'perspective', 'viewpoint', 'argues', 'believes', 'thinks',
    'according to analysts', 'analysts say', 'experts say',
})

_MARKET_STRUCTURE_TERMS = (
    'circuit breaker', 'market maker', 'high frequency trading', 'hft',
    'liquidity', 'market mechanics', 'order flow', 'dark pool',
    'market structure', 'exchange halt', 'trading halt', 'market open',
    'market close', 'settlement', 'clearing',
)


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
    trend:           str  = "neutral"    # bullish / bearish / neutral
    volatility:      str  = "NORMAL"     # HIGH / NORMAL
    drawdown_active: bool = False
    momentum:        str  = "flat"       # positive / negative / flat
    spx_price:       float = 0.0
    raw:             dict = field(default_factory=dict)


# ── ARTICLE STATE ─────────────────────────────────────────────────────────

@dataclass
class ArticleState:
    """
    State machine tracking all 22 gate outputs for a single article.
    Enables Gate 22 composite scoring and complete audit trail.
    """
    system_status: str = "unknown"
    trend_state: str = "neutral"
    volatility_state: str = "normal_vol"
    drawdown_state: bool = False
    momentum_state: str = "flat"
    credibility_score: float = 0.0
    relevance_score: float = 0.0
    opinion_flag: bool = False
    relevance_ok: bool = False
    topic_state: str = "uncertain"
    entity_state: str = "non_actionable"
    event_state: str = "unscheduled"
    sentiment_state: str = "neutral"
    sentiment_score: float = 0.0
    sentiment_confidence: float = 0.0
    headline_exaggeration: bool = False
    novelty_state: str = "incremental_update"
    novelty_score: float = 0.0
    scope_state: str = "unclear"
    benchmark_corr: str = "MEDIUM"
    horizon_state: str = "multi_day"
    decay_state: str = "medium_decay"
    benchmark_rel_state: str = "neutral"
    signal_type: str = "beta"
    dominance_state: str = "benchmark_dominant"
    confirmation_state: str = "weak"
    confirmation_score: float = 0.0
    timing_state: str = "unknown"
    timing_tradeable: bool = True
    crowding_state: str = "still_open"
    crowding_discount: float = 0.0
    cluster_volume: int = 1
    ambiguity_state: str = "clear"
    ambiguity_score: float = 0.0
    impact_magnitude: str = "low"
    impact_link_state: str = "benchmark_weak"
    base_impact_score: float = 0.0
    action_state: str = "ignore"
    action_reason: str = ""
    impact_score: float = 0.0
    discounts_applied: list = field(default_factory=list)
    persistence_state: str = "slow"
    evaluation_note: str = ""
    output_mode: str = "uncertain"
    output_priority: str = "article_first"
    output_action: str = "no_signal"
    routing: str = "DISCARD"
    composite_score: float = 0.0
    final_signal: str = "neutral_or_watch"


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
            db.log_event("NEWS_CLASSIFIED", agent="News",
                         details=json.dumps(self.to_machine()))
        except Exception as e:
            log.debug(f"NewsDecisionLog.commit failed (non-fatal): {e}")


# ── TEMP PATCH: PER-SOURCE DAILY FETCH GUARD ──────────────────────────────
# Prevents the News agent from hitting any external source more than once per day.
# Remove this block once a proper rate-limiting layer is in place.

_FETCH_GUARD_EVENT      = "SCOUT_SOURCE_FETCH"
_FETCH_GUARD_RESET_HOUR = 8    # daily reset at 08:00 ET — fetches before this are discarded


def _fetch_guard_cutoff() -> str:
    """
    Return the ISO timestamp of today's 08:00 ET reset point.
    If it's currently before 08:00 ET, use yesterday's 08:00 ET —
    so the window always covers the period since the last 08:00 reset.
    """
    now   = datetime.now(ET)
    reset = now.replace(hour=_FETCH_GUARD_RESET_HOUR, minute=0, second=0, microsecond=0)
    if now < reset:
        reset -= timedelta(days=1)
    return reset.isoformat()


def _source_fetched_recently(source_name: str, db) -> bool:
    """Return True if this source was fetched since the most recent 08:00 ET reset."""
    try:
        cutoff = _fetch_guard_cutoff()
        rows = db.query(
            "SELECT id FROM event_log WHERE event_type = ? AND details LIKE ? AND created_at > ? LIMIT 1",
            (_FETCH_GUARD_EVENT, f"%{source_name}%", cutoff),
        )
        return bool(rows)
    except Exception:
        return False   # fail open — allow fetch if DB check errors


def _record_source_fetch(source_name: str, db) -> None:
    """Record a successful fetch so the guard fires until the next 08:00 ET reset."""
    try:
        db.log_event(_FETCH_GUARD_EVENT, agent="News", details=f"source={source_name}")
    except Exception:
        pass


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
    # after Gate 22 as a final adjustment. Future: integrate into Gate 12 (confirmation).
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


# ── ALPACA NEWS FETCHERS ───────────────────────────────────────────────────

_ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
_ALPACA_HEADERS  = lambda: {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

# Alpaca news source → tier mapping.
# Benzinga is the primary content provider via Alpaca's news feed.
_ALPACA_SOURCE_TIERS = {
    "benzinga":  2,   # wire-level financial news
    "reuters":   2,
    "ap":        2,
    "dow jones": 2,
    "wsj":       2,
    "ft":        2,
    "bloomberg": 2,
}


def _alpaca_news_tier(source_name: str) -> int:
    """Map Alpaca article source string to internal source tier (1–4)."""
    s = (source_name or "").lower()
    for key, tier in _ALPACA_SOURCE_TIERS.items():
        if key in s:
            return tier
    return 3   # default: press-release tier


def _alpaca_article_to_item(article: dict) -> dict:
    """
    Normalise an Alpaca news article dict to the pipeline's standard item format.

    Alpaca article fields:
      id, headline, summary, author, created_at, updated_at,
      content, url, symbols (list), source (e.g. 'Benzinga')
    """
    symbols   = article.get("symbols") or []
    ticker    = symbols[0].upper() if symbols else None
    pub_date  = (article.get("created_at") or "")[:10]   # "YYYY-MM-DD"
    source    = article.get("source", "Alpaca News")
    tier      = _alpaca_news_tier(source)
    images    = article.get("images") or []
    image_url = next(
        (img["url"] for img in images if img.get("size") == "small"),
        images[0].get("url", "") if images else ""
    )
    return {
        "headline":    (article.get("headline") or "").strip(),
        "subhead":     (article.get("summary")  or "")[:120].strip(),
        "source":      f"Alpaca News ({source})",
        "source_tier": tier,
        "source_url":  article.get("url", _ALPACA_NEWS_URL),
        "politician":  "",
        "disc_date":   pub_date,
        "tx_date":     pub_date,
        "ticker":      ticker,
        "all_symbols": symbols,
        "image_url":   image_url,
    }


def fetch_alpaca_news_historical(
    symbols: list | None = None,
    start:   str  | None = None,
    end:     str  | None = None,
    limit:   int  = 50,
    sort:    str  = "desc",
) -> list[dict]:
    """
    Fetch news articles from Alpaca's historical news REST API.

    Endpoint: GET https://data.alpaca.markets/v1beta1/news
    Auth:     APCA-API-KEY-ID + APCA-API-SECRET-KEY headers
    Rate:     ~130 articles/day available; max 50 per call.
    Coverage: 2015–present via Benzinga content feed.

    Args:
        symbols:  Optional list of tickers to filter by (e.g. ['AAPL','MSFT']).
                  If None, fetches market-wide news.
        start:    ISO-8601 start datetime (e.g. '2026-04-01T00:00:00Z').
                  Defaults to 24 h ago.
        end:      ISO-8601 end datetime. Defaults to now.
        limit:    Max articles per call (1–50).
        sort:     'desc' (newest first) or 'asc'.

    Returns:
        List of normalised item dicts ready for the 22-gate classification spine.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — skipping news fetch")
        return []

    if not start:
        start = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    if not end:
        end = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

    params: dict = {
        "start":               start,
        "end":                 end,
        "limit":               min(limit, 50),
        "sort":                sort,
        "include_content":     "false",
        "exclude_contentless": "true",
    }
    if symbols:
        params["symbols"] = ",".join(s.upper() for s in symbols)

    results   = []
    page_token = None
    pages_fetched = 0
    max_pages = 10   # safety ceiling — at 50/page this is 500 articles max per run

    while pages_fetched < max_pages:
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(
                _ALPACA_NEWS_URL,
                params=params,
                headers=_ALPACA_HEADERS(),
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as exc:
            log.error(f"Alpaca news fetch error (page {pages_fetched + 1}): {exc}")
            break

        data       = r.json()
        articles   = data.get("news", [])
        page_token = data.get("next_page_token")
        pages_fetched += 1

        for article in articles:
            item = _alpaca_article_to_item(article)
            if item["headline"]:
                results.append(item)

        log.debug(f"Alpaca news page {pages_fetched}: {len(articles)} articles "
                  f"(total so far: {len(results)})")

        if not page_token or len(articles) == 0:
            break

    log.info(f"Alpaca news: fetched {len(results)} articles "
             f"({pages_fetched} page{'s' if pages_fetched != 1 else ''})")
    return results


def fetch_alpaca_news_for_ticker(ticker: str, limit: int = 10) -> list[dict]:
    """
    Fetch recent Alpaca news articles for a specific ticker.
    Used by the screening request handler and per-ticker lookups.
    Returns a list of normalised item dicts.
    """
    start = (datetime.now() - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return fetch_alpaca_news_historical(
        symbols=[ticker], start=start, limit=limit, sort="desc"
    )


def fetch_and_store_alpaca_display_news(db) -> int:
    """
    Fetch recent broad-market Alpaca news and store to news_feed table for
    portal display (Intel page).  Articles bypass the 22-gate signal pipeline
    entirely — routing='NEWS'.

    Deduplication: Jaccard similarity against headlines stored in the last 24 h.
    Returns number of new articles stored.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return 0

    # Pull last-24h stored headlines for dedup
    stored_keys: list[str] = []
    try:
        with db.conn() as c:
            rows = c.execute("""
                SELECT raw_headline FROM news_feed
                WHERE source='NEWS'
                  AND created_at >= datetime('now','-24 hours')
            """).fetchall()
        stored_keys = [re.sub(r'[^a-z0-9 ]', '', (r[0] or "").lower()).strip()
                       for r in rows if r[0]]
    except Exception:
        pass

    start    = (datetime.now() - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
    articles = fetch_alpaca_news_historical(start=start, limit=50, sort="desc")

    stored = 0
    for item in articles:
        title = item["headline"]
        if not title:
            continue
        key = re.sub(r'[^a-z0-9 ]', '', title.lower()).strip()
        if not key:
            continue
        # Skip if >70% similar to anything already stored
        if any(_jaccard(key, sk) > 0.70 for sk in stored_keys):
            continue
        stored_keys.append(key)
        try:
            db.write_news_feed_entry(
                congress_member = "",
                ticker          = item.get("ticker") or "",
                signal_score    = "NEWS",
                sentiment_score = None,
                raw_headline    = title,
                metadata        = {
                    "source":     item["source"],
                    "category":   "Markets",
                    "link":       item["source_url"],
                    "pub_date":   item["disc_date"],
                    "routing":    "NEWS",
                    "staleness":  "fresh",
                    "symbols":    item.get("all_symbols", []),
                    "image_url":  item.get("image_url", ""),
                },
                source = "NEWS",
            )
            stored += 1
        except Exception as e:
            log.warning(f"news_feed write failed (Alpaca display news): {e}")

    log.info(f"Alpaca display news: {stored} new headlines stored")
    return stored


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


def _compute_roc(closes, lookback):
    """
    Rate of Change over `lookback` periods.
    Returns float ROC value, or 0.0 if insufficient data.
    """
    if len(closes) < lookback + 1:
        return 0.0
    base = closes[-(lookback + 1)]
    if not base:
        return 0.0
    return (closes[-1] - base) / base


# ── GATE 1 — SYSTEM ───────────────────────────────────────────────────────

def gate1_system(item, ctrl, ndl, seen_headlines, state):
    """
    System gate — data quality checks before any analysis.
    Returns True to PROCEED, False to HALT this item.

    Checks:
      news_source_status — was the item parsed successfully?
      timestamp          — is the article within MAX_NEWS_AGE_HOURS?
      duplicate          — Jaccard similarity against seen headlines
      word_count         — minimum body length check
    """
    headline = (item.get("headline") or "").strip()
    subhead  = (item.get("subhead") or "").strip()

    # ── Parse failure check ────────────────────────────────────────────────
    if not headline or len(headline) < 5:
        state.system_status = "parse_failure"
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
                state.system_status = "timestamp_rejected"
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
        state.system_status = "duplicate"
        ndl.gate(1, "SYSTEM",
                 {"similarity": f"{best_sim:.2f}", "threshold": ctrl.DUPLICATE_THRESHOLD},
                 "HALT", "duplicate article — similarity above threshold")
        return False

    # ── Minimum word count check ───────────────────────────────────────────
    word_count = len(_tokenize(full_text))
    if word_count < ctrl.MIN_WORD_COUNT:
        state.system_status = "body_too_short"
        ndl.gate(1, "SYSTEM",
                 {"word_count": word_count, "min": ctrl.MIN_WORD_COUNT},
                 "HALT", "article below minimum word count — body too short")
        return False

    seen_headlines.append(full_text)

    state.system_status = "system_ok"
    ndl.gate(1, "SYSTEM",
             {"headline_len": len(headline), "disc_date": disc_date_str or "unknown",
              "age_ok": news_age_ok, "best_sim": f"{best_sim:.2f}",
              "word_count": word_count},
             "PROCEED")
    return True


# ── GATE 2 — BENCHMARK ────────────────────────────────────────────────────

def gate2_benchmark(ctrl):
    """
    Benchmark gate — compute SPX regime for the current session.
    Returns BenchmarkRegime. Called once per run, applied to all articles.

    TODO: DATA_DEPENDENCY — VIX threshold not yet integrated; ATR/price used as proxy.
    """
    bars = _alpaca_bars(ctrl.SPX_TICKER, days=ctrl.SPX_SMA_LONG + 10)
    if not bars:
        log.warning(f"[GATE 2] Benchmark data unavailable for {ctrl.SPX_TICKER}"
                    f" — defaulting to neutral regime")
        return BenchmarkRegime(trend="neutral", volatility="NORMAL",
                               drawdown_active=False, momentum="flat",
                               raw={"status": "offline"})

    closes    = [b["c"] for b in bars]
    sma_short = _compute_sma(closes, ctrl.SPX_SMA_SHORT)
    sma_long  = _compute_sma(closes, ctrl.SPX_SMA_LONG)
    atr       = _compute_atr(bars)
    spx_price = closes[-1]

    # Trend with neutral band
    if sma_short is not None and sma_long is not None:
        if sma_short > sma_long * (1 + ctrl.TREND_NEUTRAL_BAND):
            trend = "bullish"
        elif sma_short < sma_long * (1 - ctrl.TREND_NEUTRAL_BAND):
            trend = "bearish"
        else:
            trend = "neutral"
    else:
        trend = "neutral"

    # Volatility (ATR/price ratio proxy for VIX)
    vol_ratio = (atr / spx_price) if (atr and spx_price) else 0.0
    volatility = "HIGH" if vol_ratio > ctrl.SPX_VOL_THRESHOLD else "NORMAL"

    # Drawdown
    rolling_peak    = max(closes)
    drawdown        = (spx_price - rolling_peak) / rolling_peak if rolling_peak else 0.0
    drawdown_active = drawdown <= -ctrl.SPX_DRAWDOWN_THRESH

    # ROC momentum
    roc = _compute_roc(closes, ctrl.ROC_LOOKBACK)
    if roc > 0.001:
        momentum = "positive"
    elif roc < -0.001:
        momentum = "negative"
    else:
        momentum = "flat"

    regime = BenchmarkRegime(
        trend=trend, volatility=volatility,
        drawdown_active=drawdown_active, momentum=momentum,
        spx_price=spx_price,
        raw={"sma_short": sma_short, "sma_long": sma_long,
             "vol_ratio": round(vol_ratio, 4), "drawdown": round(drawdown, 4),
             "roc": round(roc, 5)},
    )
    log.info(f"[GATE 2] Benchmark regime: trend={trend} vol={volatility} "
             f"drawdown_active={drawdown_active} momentum={momentum} spx=${spx_price:.2f}")
    return regime


# ── GATE 3 — SOURCE RELEVANCE ─────────────────────────────────────────────

def gate3_source_relevance(item, ctrl, ndl, state):
    """
    Source relevance filter — compute credibility and relevance scores.
    Returns True to proceed, False to skip.

    Credibility: tier1=1.0, tier2=0.7, tier3=0.4, tier4+=0.1 SKIP.
    +0.1 if primary source signals found (cap 1.0). -0.1 if opinion/analysis.
    Relevance: min(topic_hits/3.0, 1.0) across keyword categories.

    TODO: DATA_DEPENDENCY — language detection, topic universe filtering.
    """
    source_tier = item.get("source_tier", 2)
    headline    = (item.get("headline") or "")
    subhead     = (item.get("subhead") or "")
    text        = f"{headline} {subhead}".lower()
    tokens      = _tokenize(text)

    # Tier 4 — opinion sources always excluded
    if source_tier >= 4:
        state.credibility_score = 0.1
        ndl.gate(3, "SOURCE_RELEVANCE", {"source_tier": source_tier},
                 "SKIP", "Tier 4+ opinion source — excluded")
        return False

    # Base credibility by tier
    tier_scores = {1: 1.0, 2: 0.7, 3: 0.4}
    credibility = tier_scores.get(source_tier, 0.1)

    # Primary source bonus
    primary_hits = sum(1 for s in _PRIMARY_SOURCE_SIGNALS if s in text)
    if primary_hits > 0:
        credibility = min(credibility + 0.1, 1.0)

    # Opinion/analysis penalty
    opinion_flag = any(op in text for op in _OPINION_SIGNALS)
    if opinion_flag:
        credibility = max(credibility - 0.1, 0.0)

    # Relevance: count keyword category hits
    macro_hits    = _match_phrases(text, _MACRO_TERMS)
    earnings_hits = _match_phrases(text, _EARNINGS_TERMS)
    geo_hits      = _match_phrases(text, _GEOPOLITICAL_TERMS)
    reg_hits      = _match_phrases(text, _REGULATORY_TERMS)
    # Sector / ticker hits
    sector_hits = sum(1 for sec in SECTOR_TICKER_MAP if sec in text)
    ticker       = (item.get("ticker") or "").upper()
    ticker_hits  = 1 if (ticker and ticker in {t for tl in SECTOR_TICKER_MAP.values() for t in tl}) else 0
    total_hits   = macro_hits + earnings_hits + geo_hits + reg_hits + sector_hits + ticker_hits
    relevance    = min(total_hits / 3.0, 1.0)

    state.credibility_score = round(credibility, 4)
    state.relevance_score   = round(relevance, 4)
    state.opinion_flag      = opinion_flag
    state.relevance_ok      = relevance >= ctrl.MIN_RELEVANCE

    # Skip if credibility too low
    if credibility < ctrl.MIN_CREDIBILITY:
        ndl.gate(3, "SOURCE_RELEVANCE",
                 {"source_tier": source_tier, "credibility": f"{credibility:.2f}",
                  "min_credibility": ctrl.MIN_CREDIBILITY},
                 "SKIP", f"credibility {credibility:.2f} below MIN_CREDIBILITY")
        return False

    # Source tier above allowed maximum
    if source_tier > ctrl.CREDIBILITY_TIER_MAX:
        ndl.gate(3, "SOURCE_RELEVANCE",
                 {"source_tier": source_tier, "max_tier": ctrl.CREDIBILITY_TIER_MAX},
                 "SKIP", "source tier exceeds credibility maximum")
        return False

    ndl.gate(3, "SOURCE_RELEVANCE",
             {"source_tier": source_tier, "credibility": f"{credibility:.2f}",
              "relevance": f"{relevance:.2f}", "opinion_flag": opinion_flag},
             "PROCEED")
    return True


# ── GATE 4 — TOPIC CLASSIFICATION ────────────────────────────────────────

def gate4_topic(item, ctrl, ndl, state):
    """
    Topic classification — identify the article's primary topic category.
    Priority order: company > sector > regulatory > earnings > geopolitical >
                    macro > market_structure > unknown.
    Returns dict: {topic, scope, entity_match}.
    """
    text   = f"{item.get('headline','')} {item.get('subhead','')}".lower()
    ticker = (item.get("ticker") or "").upper()

    macro_hits    = _match_phrases(text, _MACRO_TERMS)
    earnings_hits = _match_phrases(text, _EARNINGS_TERMS)
    geo_hits      = _match_phrases(text, _GEOPOLITICAL_TERMS)
    reg_hits      = _match_phrases(text, _REGULATORY_TERMS)
    mktstr_hits   = _match_phrases(text, _MARKET_STRUCTURE_TERMS)

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
    elif mktstr_hits >= 1:
        topic = "market_structure"
        scope = "broad_market"
        entity_match = False
    else:
        topic = "unknown"
        scope = "unknown"
        entity_match = False

    state.topic_state = topic

    ndl.gate(4, "TOPIC",
             {"macro": macro_hits, "earnings": earnings_hits,
              "geo": geo_hits, "reg": reg_hits, "mktstr": mktstr_hits,
              "ticker": ticker},
             f"topic={topic} scope={scope}")
    return {"topic": topic, "scope": scope, "entity_match": entity_match}


# ── GATE 5 — ENTITY CLASSIFICATION ───────────────────────────────────────

def gate5_entity(item, topic, ctrl, ndl, state):
    """
    Entity classification — determine the actionability of the entity referenced.
    Sets state.entity_state to: company_linked / multi_company / sector_linked /
                                 benchmark_relevant / non_actionable.
    Returns entity_state string.
    """
    ticker     = (item.get("ticker") or "").upper()
    text       = f"{item.get('headline','')} {item.get('subhead','')}".lower()
    topic_name = topic.get("topic", "unknown")

    # All known tickers
    all_tickers = {t for tl in SECTOR_TICKER_MAP.values() for t in tl}

    # Check for multiple tickers in text
    found_tickers = [m for m in re.findall(r'\b([A-Z]{2,5})\b',
                     f"{item.get('headline','')} {item.get('subhead','')}")
                     if m in all_tickers]
    unique_tickers = set(found_tickers)

    if ticker and ticker in all_tickers:
        entity_state = "company_linked"
    elif len(unique_tickers) > 1:
        entity_state = "multi_company"
    elif any(sec in text for sec in SECTOR_TICKER_MAP):
        entity_state = "sector_linked"
    elif topic_name in ("macro", "geopolitical", "market_structure"):
        entity_state = "benchmark_relevant"
    else:
        entity_state = "non_actionable"

    state.entity_state = entity_state

    ndl.gate(5, "ENTITY",
             {"ticker": ticker, "unique_tickers": len(unique_tickers),
              "topic": topic_name},
             f"entity_state={entity_state}")
    return entity_state


# ── GATE 6 — EVENT DETECTION ──────────────────────────────────────────────

def gate6_event(item, ctrl, ndl, db, seen_headlines, state):
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
    tokens          = _tokenize(full_text)
    uncertainty_ct  = _count_keywords(tokens, _UNCERTAINTY)
    uncertainty_density = uncertainty_ct / max(len(tokens), 1)
    rumor = source_tier == 3 and uncertainty_density > 0.10

    # Scheduled: TODO: DATA_DEPENDENCY — would use event calendar
    # For now: assume official government publications are scheduled events
    scheduled = item.get("source_tier", 2) == 1

    # Official: Tier 1 source AND NOT breaking
    official = source_tier == 1 and not breaking

    if official:
        event_type = "official"
    elif breaking:
        event_type = "breaking"
    elif follow_up:
        event_type = "follow_up"
    elif rumor:
        event_type = "rumor"
    elif scheduled:
        event_type = "scheduled"
    else:
        event_type = "unscheduled"

    state.event_state = event_type

    ndl.gate(6, "EVENT_DETECTION",
             {"breaking": breaking, "follow_up": f"{follow_up_sim:.2f}",
              "rumor": rumor, "uncertainty_density": f"{uncertainty_density:.3f}",
              "scheduled": scheduled, "official": official},
             f"event_type={event_type}")
    return {
        "event_type": event_type, "breaking": breaking, "follow_up": follow_up,
        "rumor": rumor, "scheduled": scheduled, "official": official,
        "uncertainty_density": uncertainty_density,
    }


# ── GATE 7 — SENTIMENT EXTRACTION ─────────────────────────────────────────

def gate7_sentiment(item, ctrl, ndl, state):
    """
    Sentiment extraction — keyword-based scoring with exaggeration detection.
    Returns dict: {direction, score, confidence, positive_count, negative_count}.
    """
    headline = item.get('headline', '')
    subhead  = item.get('subhead', '')
    full_text = f"{headline} {subhead} "
    tokens    = _tokenize(full_text)
    total     = max(len(tokens), 1)

    pos_ct = _count_keywords(tokens, _POSITIVE)
    neg_ct = _count_keywords(tokens, _NEGATIVE)

    raw_score = (pos_ct - neg_ct) / total
    # Confidence: proportion of tokens that are sentiment-bearing
    confidence = min((pos_ct + neg_ct) / max(total / 15, 1), 1.0)

    # Uncertainty direction
    unc_ct      = _count_keywords(tokens, _UNCERTAINTY)
    unc_density = unc_ct / total

    if raw_score > ctrl.POSITIVE_THRESHOLD:
        direction = "POSITIVE"
    elif raw_score < ctrl.NEGATIVE_THRESHOLD:
        direction = "NEGATIVE"
    elif (pos_ct > 0 and neg_ct > 0
          and pos_ct / total > ctrl.MIXED_MIN_THRESHOLD
          and neg_ct / total > ctrl.MIXED_MIN_THRESHOLD):
        direction = "MIXED"
    elif unc_density > ctrl.UNCERTAINTY_DENSITY_MAX:
        direction = "UNCERTAIN"
    else:
        direction = "NEUTRAL"

    conf_flag = confidence >= ctrl.SENTIMENT_CONF_MIN

    # Headline exaggeration check: compare head_score vs full_score
    head_tokens  = _tokenize(headline)
    head_total   = max(len(head_tokens), 1)
    head_pos_ct  = _count_keywords(head_tokens, _POSITIVE)
    head_neg_ct  = _count_keywords(head_tokens, _NEGATIVE)
    head_score   = (head_pos_ct - head_neg_ct) / head_total

    full_only_tokens = _tokenize(subhead + " ")
    full_only_total  = max(len(full_only_tokens), 1)
    full_pos_ct      = _count_keywords(full_only_tokens, _POSITIVE)
    full_neg_ct      = _count_keywords(full_only_tokens, _NEGATIVE)
    full_score       = (full_pos_ct - full_neg_ct) / full_only_total if full_only_tokens else head_score

    headline_exaggeration = abs(head_score - full_score) > ctrl.EXAGGERATION_DELTA

    state.sentiment_state        = direction.lower()
    state.sentiment_score        = round(raw_score, 4)
    state.sentiment_confidence   = round(confidence, 4)
    state.headline_exaggeration  = headline_exaggeration

    ndl.gate(7, "SENTIMENT",
             {"pos_tokens": pos_ct, "neg_tokens": neg_ct,
              "score": f"{raw_score:.3f}", "confidence": f"{confidence:.2f}",
              "unc_density": f"{unc_density:.3f}",
              "exaggeration": headline_exaggeration},
             f"direction={direction} conf_ok={conf_flag}")
    return {
        "direction": direction, "score": round(raw_score, 4),
        "confidence": round(confidence, 4), "conf_ok": conf_flag,
        "positive_count": pos_ct, "negative_count": neg_ct,
        "headline_exaggeration": headline_exaggeration,
    }


# ── GATE 8 — NOVELTY ──────────────────────────────────────────────────────

def gate8_novelty(item, sentiment, ctrl, ndl, db, seen_headlines, state):
    """
    Novelty / surprise controls — detect repetitive or already-priced content.
    Returns dict: {novelty_score, is_repetition, new_info_ok, novelty_state}.

    TODO: DATA_DEPENDENCY — 'already priced' detection requires market price
    data correlation with prior articles. Currently based on cluster volume only.
    """
    headline  = item.get("headline", "")
    direction = sentiment.get("direction", "NEUTRAL")

    # Compare against seen headlines in current batch
    batch_sims    = [_jaccard(headline, h) for h in seen_headlines[:-1]]
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

    max_sim       = max(max_batch_sim, max_db_sim)
    novelty       = round(1.0 - max_sim, 4)
    is_repetition = novelty < ctrl.MIN_INCREMENTAL_INFO
    new_info_ok   = novelty >= ctrl.NOVELTY_THRESHOLD

    # Novelty state classification
    if novelty > ctrl.SURPRISE_THRESHOLD and direction == "POSITIVE":
        novelty_state = "positive_surprise"
    elif novelty > ctrl.SURPRISE_THRESHOLD and direction == "NEGATIVE":
        novelty_state = "negative_surprise"
    elif novelty > ctrl.NOVELTY_THRESHOLD:
        novelty_state = "novelty_high"
    elif is_repetition:
        novelty_state = "repetitive"
    else:
        novelty_state = "incremental_update"

    state.novelty_state = novelty_state
    state.novelty_score = novelty

    ndl.gate(8, "NOVELTY",
             {"max_similarity": f"{max_sim:.2f}",
              "novelty_score": f"{novelty:.2f}",
              "novelty_threshold": ctrl.NOVELTY_THRESHOLD},
             f"novelty={novelty:.2f} repetition={is_repetition} ok={new_info_ok} "
             f"novelty_state={novelty_state}")
    return {
        "novelty_score": novelty, "is_repetition": is_repetition,
        "new_info_ok": new_info_ok, "novelty_state": novelty_state,
    }


# ── GATE 9 — SCOPE ────────────────────────────────────────────────────────

def gate9_scope(topic, entity_state, ctrl, ndl, state):
    """
    Scope classification — determine market breadth of the article's impact.
    Sets state.scope_state and state.benchmark_corr.
    Returns dict: {scope_state, benchmark_corr}.

    Scope mapping:
      macro/geo → marketwide (HIGH)
      sector/sector_linked → sector_only (MEDIUM)
      company + company_linked → single_name (LOW)
      multi_company → peer_group (MEDIUM)
      benchmark_relevant → marketwide (HIGH)
      else → unclear (MEDIUM)
    """
    topic_name = topic.get("topic", "unknown")

    if topic_name in ("macro", "geopolitical", "market_structure"):
        scope_state    = "marketwide"
        benchmark_corr = "HIGH"
    elif entity_state == "benchmark_relevant":
        scope_state    = "marketwide"
        benchmark_corr = "HIGH"
    elif entity_state == "multi_company":
        scope_state    = "peer_group"
        benchmark_corr = "MEDIUM"
    elif topic_name in ("sector",) or entity_state == "sector_linked":
        scope_state    = "sector_only"
        benchmark_corr = "MEDIUM"
    elif topic_name in ("company", "earnings", "regulatory") and entity_state == "company_linked":
        scope_state    = "single_name"
        benchmark_corr = "LOW"
    else:
        scope_state    = "unclear"
        benchmark_corr = "MEDIUM"

    state.scope_state    = scope_state
    state.benchmark_corr = benchmark_corr

    ndl.gate(9, "SCOPE",
             {"topic": topic_name, "entity_state": entity_state},
             f"scope_state={scope_state} benchmark_corr={benchmark_corr}")
    return {"scope_state": scope_state, "benchmark_corr": benchmark_corr}


# ── GATE 10 — HORIZON ─────────────────────────────────────────────────────

def gate10_horizon(topic, event, ctrl, ndl, state):
    """
    Horizon classification — compute expected duration and decay of signal impact.
    Sets state.horizon_state and state.decay_state.
    Returns dict: {horizon_state, decay_state}.

    Horizon mapping:
      regulatory/macro → structural + persistent
      earnings → multi_day + medium_decay
      geopolitical → multi_day + medium_decay
      breaking → intraday + fast_decay
      else → multi_day + medium_decay
    """
    topic_name = topic.get("topic", "unknown")
    breaking   = event.get("breaking", False)

    if topic_name in ("regulatory", "macro"):
        horizon_state = "structural"
        decay_state   = "persistent"
    elif topic_name == "geopolitical":
        horizon_state = "multi_day"
        decay_state   = "medium_decay"
    elif topic_name == "earnings":
        horizon_state = "multi_day"
        decay_state   = "medium_decay"
    elif breaking:
        horizon_state = "intraday"
        decay_state   = "fast_decay"
    else:
        horizon_state = "multi_day"
        decay_state   = "medium_decay"

    state.horizon_state = horizon_state
    state.decay_state   = decay_state

    ndl.gate(10, "HORIZON",
             {"topic": topic_name, "breaking": breaking},
             f"horizon_state={horizon_state} decay_state={decay_state}")
    return {"horizon_state": horizon_state, "decay_state": decay_state}


# ── GATE 11 — BENCHMARK-RELATIVE INTERPRETATION ──────────────────────────

def gate11_benchmark_relative(sentiment, scope, regime, ctrl, ndl, state):
    """
    Benchmark-relative interpretation — adjust signal value based on SPX backdrop.
    Uses state.sentiment_state (lowercase) and state.trend_state (bullish/bearish/neutral).
    Sets state.benchmark_rel_state, state.signal_type, state.dominance_state.
    Returns dict: {benchmark_rel_state, signal_type, dominance_state}.
    """
    direction      = state.sentiment_state   # lowercase from state
    trend          = state.trend_state        # bullish/bearish/neutral
    benchmark_corr = state.benchmark_corr
    scope_state    = state.scope_state

    # Determine alignment
    if direction == "positive" and trend == "bullish":
        benchmark_rel_state = "aligned_positive"
    elif direction == "negative" and trend == "bearish":
        benchmark_rel_state = "aligned_negative"
    elif direction == "positive" and trend == "bearish":
        benchmark_rel_state = "countertrend_positive"
    elif direction == "negative" and trend == "bullish":
        benchmark_rel_state = "countertrend_negative"
    else:
        benchmark_rel_state = "neutral"

    # Signal type: alpha if single_name with LOW benchmark_corr
    signal_type = "alpha" if (scope_state == "single_name"
                              and benchmark_corr == "LOW") else "beta"

    # Dominance
    dominance_state = ("benchmark_dominant" if benchmark_corr == "HIGH"
                       else "idiosyncratic_dominant")

    state.benchmark_rel_state = benchmark_rel_state
    state.signal_type         = signal_type
    state.dominance_state     = dominance_state

    ndl.gate(11, "BENCHMARK_RELATIVE",
             {"sentiment": direction, "spx_trend": trend,
              "benchmark_corr": benchmark_corr, "scope_state": scope_state},
             f"benchmark_rel_state={benchmark_rel_state} signal_type={signal_type} "
             f"dominance={dominance_state}")
    return {
        "benchmark_rel_state": benchmark_rel_state,
        "signal_type": signal_type,
        "dominance_state": dominance_state,
    }

# ── GATE 12 — CONFIRMATION ────────────────────────────────────────────────

def gate12_confirmation(item, ctrl, ndl, db, state):
    """
    Credibility and confirmation controls.
    Sets state.confirmation_state and state.confirmation_score.
    Returns dict: {source_count, has_primary_source, conf_adj, misinformation_risk,
                   confirmed, confirmation_state, confirmation_score}.

    States: primary_confirmed(1.0), strong(0.7), weak(0.4),
            high_misinformation_risk(0.0), expired_unconfirmed(0.1), contradictory(0.0).

    TODO: DATA_DEPENDENCY — cross-source claim validation requires multi-source
    aggregation not yet implemented. Current: DB-based source count for same ticker.
    """
    ticker      = (item.get("ticker") or "").upper()
    source_tier = item.get("source_tier", 2)
    text        = f"{item.get('headline','')} {item.get('subhead','')}".lower()

    # Primary source indicators in article text
    primary_hits       = sum(1 for s in _PRIMARY_SOURCE_SIGNALS if s in text)
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
    tokens      = _tokenize(text)
    unc_ct      = _count_keywords(tokens, _UNCERTAINTY)
    unc_density = unc_ct / max(len(tokens), 1)
    misinformation_risk = source_tier == 3 and not has_primary_source and unc_density > 0.08

    # Confirmation state with scores
    if misinformation_risk:
        confirmation_state = "high_misinformation_risk"
        confirmation_score = 0.0
    elif source_tier == 1 and has_primary_source:
        confirmation_state = "primary_confirmed"
        confirmation_score = 1.0
    elif source_tier <= 2 and (has_primary_source or confirmed):
        confirmation_state = "strong"
        confirmation_score = 0.7
    elif source_tier == 3 and has_primary_source and confirmed:
        confirmation_state = "strong"
        confirmation_score = 0.7
    else:
        confirmation_state = "weak"
        confirmation_score = 0.4

    # Legacy conf_adj for backward compat
    if confirmation_state in ("primary_confirmed", "strong"):
        conf_adj = "HIGH" if source_tier <= 2 else "MEDIUM"
    elif confirmation_state == "weak":
        conf_adj = "LOW"
    else:
        conf_adj = "LOW"

    state.confirmation_state = confirmation_state
    state.confirmation_score = round(confirmation_score, 4)

    ndl.gate(12, "CONFIRMATION",
             {"source_tier": source_tier, "source_count": source_count,
              "has_primary_source": has_primary_source, "confirmed": confirmed,
              "misinfo_risk": misinformation_risk},
             f"confirmation_state={confirmation_state} score={confirmation_score:.2f}")
    return {
        "source_count": source_count, "has_primary_source": has_primary_source,
        "conf_adj": conf_adj, "misinformation_risk": misinformation_risk,
        "confirmed": confirmed, "confirmation_state": confirmation_state,
        "confirmation_score": confirmation_score,
    }


# ── GATE 13 — TIMING ──────────────────────────────────────────────────────

def gate13_timing(item, ctrl, ndl, state):
    """
    Timing controls — assess publication timing relative to market windows.
    Sets state.timing_state and state.timing_tradeable.
    Returns dict: {tradeable, timing_state, stale}.

    States: premarket, intraday, postmarket, expired (not tradeable),
            delayed_distribution (source_tier==3 aggregator), active_flow.
    """
    disc_date_str = item.get("disc_date", "")
    source_tier   = item.get("source_tier", 2)
    now           = datetime.now(ET)

    # Staleness check
    stale  = False
    pub_dt = None
    if disc_date_str:
        try:
            pub_dt    = datetime.strptime(disc_date_str, '%Y-%m-%d').replace(tzinfo=ET)
            age_hours = (now - pub_dt).total_seconds() / 3600
            stale     = age_hours > ctrl.TRADEABLE_WINDOW_HOURS
        except ValueError:
            pass

    if stale:
        timing_state    = "expired"
        timing_tradeable = False
    elif source_tier == 3:
        # Tier 3 aggregators introduce distribution delay
        timing_state    = "delayed_distribution"
        timing_tradeable = True
    elif pub_dt:
        mkt_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        mkt_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        pub_naive   = pub_dt.replace(tzinfo=None)
        open_naive  = mkt_open.replace(tzinfo=None)
        close_naive = mkt_close.replace(tzinfo=None)
        if pub_naive < open_naive:
            timing_state = "premarket"
        elif pub_naive > close_naive:
            timing_state = "postmarket"
        else:
            timing_state = "intraday"
        timing_tradeable = True
    else:
        timing_state    = "active_flow"
        timing_tradeable = True

    state.timing_state    = timing_state
    state.timing_tradeable = timing_tradeable

    ndl.gate(13, "TIMING",
             {"disc_date": disc_date_str or "unknown",
              "timing_state": timing_state, "stale": stale},
             f"tradeable={timing_tradeable}")
    return {"tradeable": timing_tradeable, "timing_state": timing_state, "stale": stale}


# ── GATE 14 — CROWDING / SATURATION ──────────────────────────────────────

def gate14_crowding(item, ctrl, ndl, db, state):
    """
    Crowding and saturation controls.
    Sets state.crowding_state, state.crowding_discount, state.cluster_volume.
    Returns dict: {cluster_volume, crowding_state, crowding_discount}.

    States: exhausted (cluster>=EXTREME_ATTENTION_MULT*threshold, discount=0.9),
            crowded (cluster>=threshold, discount=0.5),
            still_open (discount=0.0).

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

    exhausted_threshold = int(ctrl.EXTREME_ATTENTION_MULT * ctrl.CLUSTER_VOL_THRESHOLD)

    if cluster_volume >= exhausted_threshold:
        crowding_state   = "exhausted"
        crowding_discount = 0.9
    elif cluster_volume >= ctrl.CLUSTER_VOL_THRESHOLD:
        crowding_state   = "crowded"
        crowding_discount = 0.5
    else:
        crowding_state   = "still_open"
        crowding_discount = 0.0

    state.crowding_state    = crowding_state
    state.crowding_discount = crowding_discount
    state.cluster_volume    = cluster_volume

    ndl.gate(14, "CROWDING",
             {"cluster_volume": cluster_volume,
              "threshold": ctrl.CLUSTER_VOL_THRESHOLD,
              "exhausted_threshold": exhausted_threshold},
             f"crowding_state={crowding_state} discount={crowding_discount}")
    return {
        "cluster_volume": cluster_volume,
        "crowding_state": crowding_state,
        "crowding_discount": crowding_discount,
    }


# ── GATE 15 — CONTRADICTION / AMBIGUITY ──────────────────────────────────

def gate15_contradiction(item, ctrl, ndl, state):
    """
    Contradiction and ambiguity controls.
    Sets state.ambiguity_state and state.ambiguity_score.
    Returns dict: {ambiguity_state, ambiguity_score, has_contradiction,
                   uncertainty_density, head_body_mismatch}.

    States with ambiguity_score:
      internally_conflicted (both pos+neg>=2, score=1.0)
      headline_body_mismatch (score=0.8)
      uncertain_language (score=0.5)
      clear (score=0.0)

    TODO: DATA_DEPENDENCY — analyst view dispersion requires aggregated
    expert consensus data not yet available.
    """
    headline = item.get("headline", "")
    subhead  = item.get("subhead", "")

    # Uncertainty term density
    head_tokens     = _tokenize(headline)
    unc_ct          = _count_keywords(head_tokens, _UNCERTAINTY)
    uncertainty_density = unc_ct / max(len(head_tokens), 1)
    high_uncertainty    = uncertainty_density > ctrl.UNCERTAINTY_DENSITY_MAX

    # Headline / body sentiment mismatch
    head_pos = _count_keywords(head_tokens, _POSITIVE)
    head_neg = _count_keywords(head_tokens, _NEGATIVE)
    head_dir = 1 if head_pos > head_neg else (-1 if head_neg > head_pos else 0)

    sub_tokens = _tokenize(subhead)
    sub_pos    = _count_keywords(sub_tokens, _POSITIVE)
    sub_neg    = _count_keywords(sub_tokens, _NEGATIVE)
    sub_dir    = 1 if sub_pos > sub_neg else (-1 if sub_neg > sub_pos else 0)

    # Mismatch: headline positive and subhead negative (or vice versa)
    head_body_mismatch = (bool(sub_tokens) and head_dir != 0 and sub_dir != 0
                          and head_dir != sub_dir)

    # Internal conflict: both strongly positive AND negative (>=2 each)
    internally_conflicted = head_pos >= 2 and head_neg >= 2

    # Determine ambiguity state and score
    if internally_conflicted:
        ambiguity_state = "internally_conflicted"
        ambiguity_score = 1.0
    elif head_body_mismatch:
        ambiguity_state = "headline_body_mismatch"
        ambiguity_score = 0.8
    elif high_uncertainty:
        ambiguity_state = "uncertain_language"
        ambiguity_score = 0.5
    else:
        ambiguity_state = "clear"
        ambiguity_score = 0.0

    has_contradiction = ambiguity_state != "clear"

    state.ambiguity_state = ambiguity_state
    state.ambiguity_score = round(ambiguity_score, 4)

    ndl.gate(15, "CONTRADICTION",
             {"uncertainty_density": f"{uncertainty_density:.3f}",
              "head_body_mismatch": head_body_mismatch,
              "internally_conflicted": internally_conflicted},
             f"ambiguity_state={ambiguity_state} score={ambiguity_score:.2f}")
    return {
        "ambiguity_state": ambiguity_state,
        "ambiguity_score": ambiguity_score,
        "has_contradiction": has_contradiction,
        "uncertainty_density": uncertainty_density,
        "head_body_mismatch": head_body_mismatch,
    }


# ── GATE 16 — IMPACT MAGNITUDE ───────────────────────────────────────────

def gate16_impact_magnitude(scope, topic, regime, ctrl, ndl, state):
    """
    Impact magnitude estimation — assess breadth and expected magnitude of impact.
    Sets state.impact_magnitude, state.impact_link_state, state.base_impact_score.
    Returns dict: {impact_magnitude, impact_link_state, base_impact_score}.

    Mapping:
      marketwide + macro/geo → high + benchmark_linked
      sector_only/peer_group/earnings → medium + benchmark_weak
      single_name → low + benchmark_weak
    Boost: high_vol or drawdown → low→medium, medium→high
    """
    scope_state  = state.scope_state
    topic_name   = topic.get("topic", "unknown")
    high_vol     = regime.volatility == "HIGH"
    drawdown     = regime.drawdown_active

    # Base magnitude by scope + topic
    if scope_state == "marketwide" and topic_name in ("macro", "geopolitical", "market_structure"):
        impact_magnitude = "high"
        impact_link_state = "benchmark_linked"
    elif scope_state in ("sector_only", "peer_group") or topic_name == "earnings":
        impact_magnitude = "medium"
        impact_link_state = "benchmark_weak"
    elif scope_state == "single_name":
        impact_magnitude = "low"
        impact_link_state = "benchmark_weak"
    else:
        impact_magnitude = "medium"
        impact_link_state = "benchmark_weak"

    # Volatility / drawdown boost
    if high_vol or drawdown:
        if impact_magnitude == "low":
            impact_magnitude = "medium"
        elif impact_magnitude == "medium":
            impact_magnitude = "high"

    # Base score
    score_map = {"high": 1.0, "medium": 0.5, "low": 0.2}
    base_impact_score = score_map.get(impact_magnitude, 0.2)

    state.impact_magnitude  = impact_magnitude
    state.impact_link_state = impact_link_state
    state.base_impact_score = round(base_impact_score, 4)

    ndl.gate(16, "IMPACT_MAGNITUDE",
             {"scope_state": scope_state, "topic": topic_name,
              "high_vol": high_vol, "drawdown": drawdown},
             f"impact_magnitude={impact_magnitude} link={impact_link_state} "
             f"base_score={base_impact_score:.2f}")
    return {
        "impact_magnitude": impact_magnitude,
        "impact_link_state": impact_link_state,
        "base_impact_score": base_impact_score,
    }


# ── GATE 17 — ACTION CLASSIFICATION ──────────────────────────────────────

def gate17_action(sentiment, novelty, confirmation, contradiction,
                  regime, event, ctrl, ndl, state):
    """
    Action classification — determine what to do with the article.
    Sets state.action_state and state.action_reason.
    Returns dict: {action_state, confidence_label, reason}.

    States:
      bullish_signal      — positive, credible, novel, relevant
      bearish_signal      — negative, credible, novel, relevant
      relative_alpha      — company-specific, benchmark-neutral
      benchmark_signal    — broad macro, high benchmark linkage
      provisional_watch   — rumor + weak confirmation
      freeze              — internally_conflicted/contradictory + weak/contradictory confirmation
      watch_only          — mixed or uncertain
      ignore              — low credibility or repetition
    """
    direction    = sentiment.get("direction", "NEUTRAL")
    conf_ok      = sentiment.get("conf_ok", False)
    novelty_ok   = novelty.get("new_info_ok", False)
    repetition   = novelty.get("is_repetition", False)
    conf_state   = state.confirmation_state
    misinfo      = confirmation.get("misinformation_risk", False)
    conf_adj     = confirmation.get("conf_adj", "LOW")
    amb_state    = state.ambiguity_state
    rumor        = event.get("rumor", False)
    scope_state  = state.scope_state
    bench_corr   = state.benchmark_corr

    # ── Discard paths ──────────────────────────────────────────────────────
    if repetition:
        ndl.gate(17, "ACTION", {"repetition": True},
                 "ignore", "article is repetition — no new information")
        state.action_state  = "ignore"
        state.action_reason = "repetition — no incremental information"
        return {"action_state": "ignore", "confidence_label": "NOISE",
                "reason": "repetition — no incremental information"}

    if misinfo:
        ndl.gate(17, "ACTION", {"misinfo_risk": True},
                 "ignore", "misinformation risk high — Tier 3, no primary source, high uncertainty")
        state.action_state  = "ignore"
        state.action_reason = "misinformation risk — source quality insufficient"
        return {"action_state": "ignore", "confidence_label": "NOISE",
                "reason": "misinformation risk — source quality insufficient"}

    if conf_adj == "LOW" and not novelty_ok:
        ndl.gate(17, "ACTION",
                 {"conf_adj": conf_adj, "novelty_ok": novelty_ok},
                 "ignore", "low credibility and low novelty")
        state.action_state  = "ignore"
        state.action_reason = "low credibility and low novelty — ignore"
        return {"action_state": "ignore", "confidence_label": "NOISE",
                "reason": "low credibility and low novelty — ignore"}

    # ── Freeze path ────────────────────────────────────────────────────────
    if amb_state in ("internally_conflicted",) and conf_state in ("weak", "high_misinformation_risk"):
        ndl.gate(17, "ACTION",
                 {"ambiguity_state": amb_state, "confirmation_state": conf_state},
                 "freeze", "internally conflicted + weak confirmation")
        state.action_state  = "freeze"
        state.action_reason = "internally conflicted with weak confirmation — freeze"
        return {"action_state": "freeze", "confidence_label": "LOW",
                "reason": "contradiction or ambiguity + weak confirmation — freeze"}

    # ── Provisional watch path ─────────────────────────────────────────────
    if rumor and conf_state == "weak":
        ndl.gate(17, "ACTION",
                 {"rumor": rumor, "confirmation_state": conf_state},
                 "provisional_watch", "rumor + weak confirmation")
        state.action_state  = "provisional_watch"
        state.action_reason = "rumor with weak confirmation — provisional watch"
        return {"action_state": "provisional_watch", "confidence_label": "LOW",
                "reason": "rumor with weak confirmation — provisional watch"}

    # ── Watch paths ────────────────────────────────────────────────────────
    if amb_state in ("headline_body_mismatch", "uncertain_language"):
        ndl.gate(17, "ACTION", {"ambiguity_state": amb_state},
                 "watch_only", "ambiguity detected")
        state.action_state  = "watch_only"
        state.action_reason = f"ambiguity state={amb_state} — watch for resolution"
        return {"action_state": "watch_only", "confidence_label": "LOW",
                "reason": f"ambiguity {amb_state} — watch for resolution"}

    if direction in ("NEUTRAL", "MIXED", "UNCERTAIN") or not conf_ok:
        ndl.gate(17, "ACTION",
                 {"direction": direction, "conf_ok": conf_ok},
                 "watch_only", "neutral or mixed sentiment / low confidence")
        state.action_state  = "watch_only"
        state.action_reason = f"sentiment {direction} or confidence below threshold"
        return {"action_state": "watch_only", "confidence_label": "LOW",
                "reason": f"sentiment {direction} or confidence below threshold"}

    # ── Signal paths ───────────────────────────────────────────────────────
    # Broad macro with high benchmark linkage
    if scope_state == "marketwide" and bench_corr == "HIGH":
        action_state     = "benchmark_signal"
        confidence_label = "MEDIUM" if conf_adj != "LOW" else "LOW"
        reason           = "broad macro with high SPX linkage — benchmark regime signal"

    # Company-specific alpha
    elif scope_state == "single_name" and bench_corr == "LOW":
        action_state     = "relative_alpha"
        confidence_label = conf_adj
        reason           = (f"company-specific {direction} signal with low benchmark correlation"
                            f" — alpha opportunity")

    # Directional signals
    elif direction == "POSITIVE":
        action_state     = "bullish_signal"
        confidence_label = conf_adj
        reason           = f"positive + credible ({conf_adj}) + novel + relevant"

    elif direction == "NEGATIVE":
        action_state     = "bearish_signal"
        confidence_label = conf_adj
        reason           = f"negative + credible ({conf_adj}) + novel + relevant"

    else:
        action_state     = "watch_only"
        confidence_label = "LOW"
        reason           = "unclassified — defaulting to watch"

    state.action_state  = action_state
    state.action_reason = reason

    ndl.gate(17, "ACTION",
             {"direction": direction, "scope_state": scope_state,
              "bench_corr": bench_corr, "conf_adj": conf_adj},
             action_state, reason)
    return {"action_state": action_state, "confidence_label": confidence_label,
            "reason": reason}


# ── GATE 18 — RISK DISCOUNTS ──────────────────────────────────────────────

def gate18_risk_discounts(action, sentiment, regime, event, ctrl, ndl, state):
    """
    Risk discounts — apply multiplicative discounts to base_impact_score.
    Sets state.impact_score and state.discounts_applied.
    Returns dict: {impact_score, discounts_applied, final_confidence}.

    Discounts:
      *DISCOUNT_SENTIMENT_CONF if sentiment_confidence < SENTIMENT_CONF_MIN
      *DISCOUNT_BENCHMARK_VOL  if high volatility regime
      *DISCOUNT_NOISY_EVENT    if rumor
      *DISCOUNT_SOURCE_LOW     if credibility_score < 0.5
      *DISCOUNT_CONTRADICTION  if ambiguity != clear
    """
    impact_score = state.base_impact_score
    discounts    = []

    # Low sentiment confidence
    if state.sentiment_confidence < ctrl.SENTIMENT_CONF_MIN:
        impact_score *= ctrl.DISCOUNT_SENTIMENT_CONF
        discounts.append(f"low_sentiment_conf *{ctrl.DISCOUNT_SENTIMENT_CONF}")

    # High benchmark volatility
    if regime.volatility == "HIGH":
        impact_score *= ctrl.DISCOUNT_BENCHMARK_VOL
        discounts.append(f"benchmark_vol_HIGH *{ctrl.DISCOUNT_BENCHMARK_VOL}")

    # Rumor discount
    if event.get("rumor", False):
        impact_score *= ctrl.DISCOUNT_NOISY_EVENT
        discounts.append(f"rumor_source *{ctrl.DISCOUNT_NOISY_EVENT}")

    # Low credibility source
    if state.credibility_score < 0.5:
        impact_score *= ctrl.DISCOUNT_SOURCE_LOW
        discounts.append(f"source_low_credibility *{ctrl.DISCOUNT_SOURCE_LOW}")

    # Contradiction / ambiguity discount
    if state.ambiguity_state != "clear":
        impact_score *= ctrl.DISCOUNT_CONTRADICTION
        discounts.append(f"contradiction({state.ambiguity_state}) *{ctrl.DISCOUNT_CONTRADICTION}")

    impact_score = round(impact_score, 4)

    state.impact_score       = impact_score
    state.discounts_applied  = discounts

    # Legacy final_confidence label for backward compat
    if impact_score > 0.7:
        final_confidence = "HIGH"
    elif impact_score > 0.3:
        final_confidence = "MEDIUM"
    else:
        final_confidence = "LOW"

    ndl.gate(18, "RISK_DISCOUNTS",
             {"base_impact_score": state.base_impact_score,
              "discounts": len(discounts)},
             f"impact_score={impact_score:.4f} final_confidence={final_confidence}",
             "; ".join(discounts) or "none")
    return {
        "impact_score": impact_score,
        "discounts_applied": discounts,
        "final_confidence": final_confidence,
    }


# ── GATE 19 — PERSISTENCE ─────────────────────────────────────────────────

def gate19_persistence(topic, event, ctrl, ndl, state):
    """
    Persistence controls — classify expected decay rate of the signal's market impact.
    Sets state.persistence_state.
    Returns dict: {persistence_state, decay_rate}.

    States: structural(regulatory/macro), dynamically_updated(geo),
            medium_or_fast(earnings), rapid(breaking), slow(default).
    """
    t = topic.get("topic", "unknown")

    if t in ("regulatory", "macro"):
        persistence_state = "structural"
        decay_rate        = "low"
        reason            = "regulatory/policy change — structural repricing expected"
    elif t == "geopolitical":
        persistence_state = "dynamically_updated"
        decay_rate        = "variable"
        reason            = "geopolitical — update per follow-up flow"
    elif t == "earnings":
        persistence_state = "medium_or_fast"
        decay_rate        = "medium"
        reason            = "earnings one-off — fades unless guidance revision broad"
    elif event.get("breaking"):
        persistence_state = "rapid"
        decay_rate        = "high"
        reason            = "breaking news — typically transient"
    else:
        persistence_state = "slow"
        decay_rate        = "high"
        reason            = "default — assume slow decay"

    state.persistence_state = persistence_state

    ndl.gate(19, "PERSISTENCE",
             {"topic": t, "breaking": event.get("breaking", False)},
             f"persistence_state={persistence_state} decay={decay_rate}", reason)
    return {"persistence_state": persistence_state, "decay_rate": decay_rate}


# ── GATE 20 — EVALUATION LOOP ─────────────────────────────────────────────

def gate20_evaluation(item, action, ctrl, ndl, db, state):
    """
    Evaluation loop — update relevance and confidence based on prior articles
    and record the classification for future comparison.
    Sets state.evaluation_note.

    TODO: DATA_DEPENDENCY — comparing predicted vs. realized market response
    requires post-trade outcome data. Currently updates relevance score only.
    """
    action_state = action.get("action_state", "ignore")
    ticker       = (item.get("ticker") or "").upper()

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

    evaluation_note = (f"ticker_active={ticker_active} action_state={action_state} "
                       f"accuracy={accuracy_note}")
    state.evaluation_note = evaluation_note

    ndl.gate(20, "EVALUATION_LOOP",
             {"action_state": action_state, "ticker_active": ticker_active},
             f"relevance_ok={ticker_active or action_state in ('benchmark_signal','watch_only')}",
             accuracy_note)
    return {"ticker_active": ticker_active}


# ── GATE 21 — OUTPUT CONTROLS ─────────────────────────────────────────────

def gate21_output(action, risk, regime, scope, ctrl, ndl, state):
    """
    Output controls — shape final output based on confidence level and regime.
    Sets state.output_mode, state.output_priority, state.output_action, state.routing.
    Returns dict: {classification, confidence, explanation, routing, output_type}.

    Output modes: decisive(impact_score>0.7) / probabilistic(>0.3) / uncertain
    Output priority: benchmark_first if benchmark_corr==HIGH
    Output actions: wait_for_confirmation, no_signal, positive_signal,
                    negative_signal, benchmark_context_signal, idiosyncratic_alpha_signal
    Routing: QUEUE for bullish/relative_alpha, WATCH for others, DISCARD for ignore
    """
    action_state     = state.action_state
    impact_score     = state.impact_score
    bench_corr       = state.benchmark_corr

    # Output mode
    if impact_score > 0.7:
        output_mode = "decisive"
    elif impact_score > 0.3:
        output_mode = "probabilistic"
    else:
        output_mode = "uncertain"

    # Output priority
    output_priority = "benchmark_first" if bench_corr == "HIGH" else "article_first"

    # Output action mapping
    if action_state == "freeze":
        output_action = "wait_for_confirmation"
    elif action_state == "ignore":
        output_action = "no_signal"
    elif action_state == "bullish_signal":
        output_action = "positive_signal"
    elif action_state == "bearish_signal":
        output_action = "negative_signal"
    elif action_state == "benchmark_signal":
        output_action = "benchmark_context_signal"
    elif action_state == "relative_alpha":
        output_action = "idiosyncratic_alpha_signal"
    else:
        output_action = "no_signal"

    # Routing
    if output_action in ("positive_signal", "idiosyncratic_alpha_signal"):
        routing = "QUEUE"
    elif action_state in ("watch_only", "provisional_watch", "bearish_signal",
                          "benchmark_signal"):
        routing = "WATCH"
    else:
        routing = "DISCARD"

    # Explanation framing
    if bench_corr == "HIGH" and regime.trend != "neutral":
        explanation = (f"benchmark-first: SPX trend={regime.trend}, "
                       f"action={action_state}")
    else:
        explanation = (f"article-first: {action_state} signal, "
                       f"SPX backdrop={regime.trend}")

    state.output_mode     = output_mode
    state.output_priority = output_priority
    state.output_action   = output_action
    state.routing         = routing

    # Backward compat confidence
    final_confidence = risk.get("final_confidence", "LOW")

    ndl.gate(21, "OUTPUT",
             {"action_state": action_state, "impact_score": f"{impact_score:.4f}",
              "output_mode": output_mode},
             f"routing={routing} output_action={output_action}", explanation)
    return {
        "classification": action_state, "confidence": final_confidence,
        "explanation": explanation, "routing": routing, "output_type": output_mode,
    }


# ── GATE 22 — COMPOSITE SCORING ───────────────────────────────────────────

def gate22_composite(ctrl, ndl, state):
    """
    Composite scoring — final arbiter combining all gate outputs into a single score.
    Sets state.composite_score, state.final_signal, and optionally overrides state.routing.

    Composite weights:
      W1=0.20  impact_score
      W2=0.15  credibility_score
      W3=0.15  novelty_score
      W4=0.20  sentiment_confidence
      W5=0.15  confirmation_score
      W6=0.10  (1 - crowding_discount)
      W7=0.05  (1 - ambiguity_score)

    If composite_score < COMPOSITE_QUALITY_THRESH → downgrade routing to DISCARD
    unless action_state is watch_only/provisional_watch (→ WATCH).
    """
    composite_score = (
        ctrl.COMPOSITE_W1 * state.impact_score
        + ctrl.COMPOSITE_W2 * state.credibility_score
        + ctrl.COMPOSITE_W3 * state.novelty_score
        + ctrl.COMPOSITE_W4 * state.sentiment_confidence
        + ctrl.COMPOSITE_W5 * state.confirmation_score
        + ctrl.COMPOSITE_W6 * (1.0 - state.crowding_discount)
        + ctrl.COMPOSITE_W7 * (1.0 - state.ambiguity_score)
    )
    composite_score = round(composite_score, 4)
    state.composite_score = composite_score

    # Final signal classification
    if state.action_state == "ignore":
        final_signal = "no_signal"
    elif composite_score >= ctrl.COMPOSITE_QUALITY_THRESH:
        if state.action_state == "bullish_signal":
            final_signal = "bullish_signal"
        elif state.action_state == "bearish_signal":
            final_signal = "bearish_signal"
        elif state.action_state == "relative_alpha":
            final_signal = "alpha_signal"
        elif state.action_state == "benchmark_signal":
            final_signal = "benchmark_regime_signal"
        elif state.action_state == "freeze":
            final_signal = "frozen_watch"
        else:
            final_signal = "neutral_or_watch"
    else:
        final_signal = "neutral_or_watch"

    state.final_signal = final_signal

    # Override routing based on composite quality
    if composite_score < ctrl.COMPOSITE_QUALITY_THRESH:
        if state.routing == "QUEUE":
            # Downgrade: not enough composite quality for a queue
            if state.action_state in ("watch_only", "provisional_watch"):
                state.routing = "WATCH"
            else:
                state.routing = "DISCARD"

    ndl.gate(22, "COMPOSITE",
             {"composite_score": f"{composite_score:.4f}",
              "quality_thresh": ctrl.COMPOSITE_QUALITY_THRESH,
              "action_state": state.action_state},
             f"final_signal={final_signal} routing={state.routing}")
    return {"composite_score": composite_score, "final_signal": final_signal}


# ── STATE → CONFIDENCE MAPPING ────────────────────────────────────────────

def _state_to_confidence(state):
    """Map state.output_mode to legacy HIGH/MEDIUM/LOW/NOISE for DB schema."""
    if state.output_mode == "decisive":
        return "HIGH"
    elif state.output_mode == "probabilistic":
        return "MEDIUM"
    elif state.action_state == "ignore":
        return "NOISE"
    else:
        return "LOW"


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def _fetch_alpaca_headlines_for_ticker(ticker: str, max_items: int = 10) -> list[str]:
    """
    Fetch recent Alpaca news headlines for a ticker.
    Returns a list of headline strings (newest first).
    Used by the screening request handler as a fast per-ticker headline source.
    """
    try:
        items = fetch_alpaca_news_for_ticker(ticker, limit=max_items)
        return [i["headline"] for i in items if i.get("headline")]
    except Exception as e:
        log.warning(f"Alpaca headlines {ticker}: {e}")
        return []


def _score_headlines_for_screening(headlines):
    """
    Score a list of headlines using the News agent's existing keyword logic.
    Returns (signal_str, score_float, top_headline).
      signal_str: 'bullish' | 'bearish' | 'neutral'
      score_float: 0.0 – 1.0
    """
    if not headlines:
        return 'neutral', 0.5, None

    all_text = ' '.join(headlines)
    tokens   = _tokenize(all_text)
    total    = max(len(tokens), 1)
    pos_ct   = _count_keywords(tokens, _POSITIVE)
    neg_ct   = _count_keywords(tokens, _NEGATIVE)
    raw      = (pos_ct - neg_ct) / total

    # Normalise to 0-1 range (raw typically -0.2 to +0.2)
    score = round(min(max((raw + 0.15) / 0.30, 0.0), 1.0), 4)

    if raw > 0.02:
        signal = 'bullish'
    elif raw < -0.02:
        signal = 'bearish'
    else:
        signal = 'neutral'

    return signal, score, headlines[0]


def _handle_screening_requests(db):
    """
    Fulfill pending 'news' screening requests from the sector screener.
    Fetches recent Alpaca news headlines for each ticker, scores them, writes
    results back to sector_screening, and appends to the logic audit log.
    """
    import os as _os
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    pending = db.get_pending_screening_requests('news')
    if not pending:
        return

    log.info(f"Screening requests: fulfilling {len(pending)} news requests")
    ET_tz   = _ZI("America/New_York")
    today   = _dt.now(ET_tz).strftime('%Y-%m-%d')
    log_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                            'logs', 'logic_audits')
    _os.makedirs(log_dir, exist_ok=True)
    audit_path = _os.path.join(log_dir, f"{today}_scout_screening.log")

    audit_lines = [
        "=" * 70,
        f"SCOUT — SCREENING NEWS AUDIT  ({_dt.now(ET_tz).strftime('%Y-%m-%d %H:%M ET')})",
        "-" * 70,
    ]

    for req in pending:
        ticker = req['ticker']
        run_id = req['run_id']
        headlines = _fetch_alpaca_headlines_for_ticker(ticker)
        signal, score, top_headline = _score_headlines_for_screening(headlines)

        db.fulfill_screening_request(
            run_id=run_id,
            ticker=ticker,
            request_type='news',
            signal=signal,
            score=score,
            headline=top_headline,
        )

        audit_lines += [
            f"  {ticker}",
            f"    Signal    : {signal.upper()}  (score {score:.2f})",
            f"    Top headline: {top_headline or 'No recent news found'}",
            f"    All headlines reviewed: {len(headlines)}",
            "",
        ]
        log.info(f"Screening news {ticker}: {signal} score={score:.2f}")

    audit_lines.append("=" * 70 + "\n")
    with open(audit_path, 'a') as f:
        f.write('\n'.join(audit_lines) + '\n')

    log.info(f"Screening news audit written: {audit_path}")


def run(session="market"):
    db   = _db()
    ctrl = ResearchControls()
    now  = datetime.now(ET)
    log.info(f"News agent starting — session={session} time={now.strftime('%H:%M ET')}")

    db.log_event("AGENT_START", agent="News", details=f"session={session}")
    db.log_heartbeat("news_agent", "RUNNING")

    # Fulfill screening requests FIRST (before main pipeline which may take a while)
    try:
        _handle_screening_requests(db)
    except Exception as e:
        log.warning(f"Screening request enrichment failed: {e}")

    # ── Gate 2: Benchmark regime (session-level, once per run) ────────────
    regime = gate2_benchmark(ctrl)

    # ── Expire stale signals ───────────────────────────────────────────────
    db.expire_old_signals()

    # ── Fetch all sources (guarded — max 1 fetch per source per 24 h) ────────
    all_raw = []

    # ── Fetch all news from Alpaca (guarded — max once per hour per session) ─
    # Market session: last 2 h of news, broad market + tracked tickers.
    # Overnight session: last 8 h of news to catch afterhours moves.
    _source_key = "Alpaca News API"
    if _source_fetched_recently(_source_key, db):
        log.info("GUARD: Alpaca News API skipped — fetched within last reset window")
    else:
        if session == "market":
            news_start = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            news_start = (datetime.now() - timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%SZ')

        results = fetch_alpaca_news_historical(start=news_start, limit=50, sort="desc")
        all_raw.extend(results)
        if results is not None:
            _record_source_fetch(_source_key, db)

    if session == "market":
        # ── Display-only news for portal Intel page ────────────────────────
        # No daily guard — dedup inside handles repeat fetches; updates hourly.
        news_stored = fetch_and_store_alpaca_display_news(db)
        log.info(f"Alpaca display news: {news_stored} new headlines stored")
    else:
        # Also refresh display-only news headlines overnight so portal is populated by morning
        alpaca_stored = fetch_and_store_alpaca_display_news(db)
        log.info(f"Overnight news refresh: {alpaca_stored} new headlines stored")

    log.info(f"Fetched {len(all_raw)} raw items across all sources")

    # ── Process each item through 22-gate spine ───────────────────────────
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

        # ── Initialize article state ──────────────────────────────────────
        state = ArticleState()

        # ── Gate 1: System ────────────────────────────────────────────────
        if not gate1_system(item, ctrl, ndl, seen_headlines, state):
            ndl.decide("DISCARD", "NOISE", "gate1_system halt")
            ndl.commit(db)
            skipped += 1
            continue

        # ── Copy benchmark regime into state ──────────────────────────────
        state.trend_state      = regime.trend
        state.volatility_state = "high_vol" if regime.volatility == "HIGH" else "normal_vol"
        state.drawdown_state   = regime.drawdown_active
        state.momentum_state   = regime.momentum

        # ── Gate 3: Source relevance ──────────────────────────────────────
        if not gate3_source_relevance(item, ctrl, ndl, state):
            ndl.decide("DISCARD", "NOISE", "gate3_source_relevance skip")
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
            # Write no-ticker articles to news_feed for Intel page display.
            # These are macro/regulatory news items (Fed, BEA, EDGAR, etc.)
            # that have no stock ticker but are still informative.
            try:
                db.write_news_feed_entry(
                    congress_member = item.get("politician", ""),
                    ticker          = "MACRO",
                    signal_score    = "NOISE",
                    sentiment_score = None,
                    raw_headline    = headline,
                    metadata        = {
                        "source":      item.get("source"),
                        "source_tier": source_tier,
                        "staleness":   "unknown",
                        "routing":     "STALE",
                        "is_amended":  False,
                        "is_spousal":  False,
                        "image_url":   item.get("image_url", ""),
                    },
                    source = "ALPACA",
                )
            except Exception:
                pass
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

        # ── Gates 4-11: Topic, entity, event, sentiment, novelty, scope,
        #               horizon, benchmark-relative ─────────────────────────
        topic    = gate4_topic(item, ctrl, ndl, state)
        entity_s = gate5_entity(item, topic, ctrl, ndl, state)
        event    = gate6_event(item, ctrl, ndl, db, seen_headlines, state)
        sentiment = gate7_sentiment(item, ctrl, ndl, state)
        novelty   = gate8_novelty(item, sentiment, ctrl, ndl, db, seen_headlines, state)
        scope     = gate9_scope(topic, entity_s, ctrl, ndl, state)
        gate10_horizon(topic, event, ctrl, ndl, state)
        gate11_benchmark_relative(sentiment, scope, regime, ctrl, ndl, state)

        # ── Gates 12-16: Confirmation, timing, crowding, contradiction,
        #                impact magnitude ────────────────────────────────────
        confirmation = gate12_confirmation(item, ctrl, ndl, db, state)
        timing       = gate13_timing(item, ctrl, ndl, state)
        crowding     = gate14_crowding(item, ctrl, ndl, db, state)
        contradiction = gate15_contradiction(item, ctrl, ndl, state)
        gate16_impact_magnitude(scope, topic, regime, ctrl, ndl, state)

        # ── Gate 13: Timing exit ──────────────────────────────────────────
        if not state.timing_tradeable:
            # Write to news_feed before discarding — stale articles still appear
            # on the Intelligence page (portal enforces a 30-article floor).
            _cm = item.get("politician", "")
            _mw = db.get_member_weight(_cm).get("weight", 1.0) if _cm else 1.0
            _bc = _state_to_confidence(state)
            _at, _an = apply_member_weight(_bc, _mw)
            try:
                db.write_news_feed_entry(
                    congress_member = _cm,
                    ticker          = ticker,
                    signal_score    = _at,
                    sentiment_score = sentiment.get("score"),
                    raw_headline    = headline,
                    metadata        = {
                        "source":          item.get("source"),
                        "source_tier":     source_tier,
                        "staleness":       staleness,
                        "routing":         "STALE",
                        "base_confidence": _bc,
                        "member_weight":   _mw,
                        "adj_numeric":     _an,
                        "is_amended":      is_amended,
                        "is_spousal":      is_spousal,
                        "ind_etf":         item.get("ind_etf", ""),
                        "sec_etf":         item.get("sector", ""),
                        "image_url":       item.get("image_url", ""),
                    },
                    source = "ALPACA",
                )
            except Exception:
                pass
            ndl.decide("DISCARD", "NOISE", "gate13_timing: article too old / not tradeable")
            ndl.commit(db)
            skipped += 1
            continue

        # ── Gates 17-22: Action, risk, persistence, evaluation, output,
        #                composite scoring ────────────────────────────────────
        action      = gate17_action(sentiment, novelty, confirmation, contradiction,
                                    regime, event, ctrl, ndl, state)
        risk        = gate18_risk_discounts(action, sentiment, regime, event, ctrl, ndl, state)
        persistence = gate19_persistence(topic, event, ctrl, ndl, state)
        gate20_evaluation(item, action, ctrl, ndl, db, state)
        output      = gate21_output(action, risk, regime, scope, ctrl, ndl, state)
        gate22_composite(ctrl, ndl, state)

        # Routing now comes from gate22 composite (may have overridden gate21)
        routing = state.routing

        # ── Member weight (kept — FLAG: integrate into Gate 12 in future) ─
        congress_member = item.get("politician", "")
        member_data     = db.get_member_weight(congress_member) if congress_member else {"weight": 1.0}
        member_weight   = member_data.get("weight", 1.0)
        base_confidence = _state_to_confidence(state)
        adj_text, adj_numeric = apply_member_weight(base_confidence, member_weight)

        log.info(f"{ticker} action_state={state.action_state} final_signal={state.final_signal} "
                 f"composite={state.composite_score:.3f} "
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
                    "source":            item.get("source"),
                    "source_tier":       source_tier,
                    "staleness":         staleness,
                    "base_confidence":   base_confidence,
                    "member_weight":     member_weight,
                    "adj_numeric":       adj_numeric,
                    "is_amended":        is_amended,
                    "is_spousal":        is_spousal,
                    "ind_etf":           ind_etf,
                    "sec_etf":           sec_etf,
                    "action_state":      state.action_state,
                    "final_signal":      state.final_signal,
                    "composite_score":   state.composite_score,
                    "entity_state":      state.entity_state,
                    "horizon_state":     state.horizon_state,
                    "benchmark_rel":     state.benchmark_rel_state,
                    "signal_type":       state.signal_type,
                    "impact_score":      state.impact_score,
                    "routing":           routing,
                    "persistence_state": state.persistence_state,
                    "image_url":       item.get("image_url", ""),
                },
                source = "CONGRESS" if source_tier == 1 else "RSS",
            )
        except Exception as e:
            log.warning(f"news_feed write failed (non-fatal): {e}")

        ndl.decide(routing, adj_text, output.get("explanation", state.action_reason))
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
            corroborated  = confirmation.get("confirmed", False),
            corroboration_note = output.get("explanation"),
            is_amended    = is_amended,
            is_spousal    = is_spousal,
            image_url     = item.get("image_url"),
            source_url    = item.get("source_url"),
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

        # ── Interrogation broadcast (only for trade candidates, not WATCH) ─
        if routing == "QUEUE":
            price_summary_for_announce = dict(price_summary) if price_summary else {}
            validated = announce_for_interrogation(sig_id, ticker, price_summary_for_announce)
            interrogation_status = "VALIDATED" if validated else "UNVALIDATED"
            del price_summary_for_announce
        else:
            interrogation_status = "SKIPPED"
        del price_summary

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

        # ── Route to Trade Logic ──────────────────────────────────────────
        if routing in ("QUEUE", "WATCH"):
            db.queue_signal_for_trader(sig_id)
            queued += 1
            log.info(f"Routed to Trade Logic: {ticker} routing={routing} adj={adj_text} "
                     f"{interrogation_status}")
        else:
            db.discard_signal(sig_id, reason=output.get("explanation", "gate22 discard"))
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

            state_re = ArticleState()
            state_re.trend_state      = regime.trend
            state_re.volatility_state = "high_vol" if regime.volatility == "HIGH" else "normal_vol"
            state_re.drawdown_state   = regime.drawdown_active
            state_re.momentum_state   = regime.momentum

            topic_re         = gate4_topic(reeval_item, ctrl, ndl_re, state_re)
            entity_re        = gate5_entity(reeval_item, topic_re, ctrl, ndl_re, state_re)
            event_re         = gate6_event(reeval_item, ctrl, ndl_re, db, [], state_re)
            sentiment_re     = gate7_sentiment(reeval_item, ctrl, ndl_re, state_re)
            novelty_re       = gate8_novelty(reeval_item, sentiment_re, ctrl, ndl_re, db, [], state_re)
            scope_re         = gate9_scope(topic_re, entity_re, ctrl, ndl_re, state_re)
            gate10_horizon(topic_re, event_re, ctrl, ndl_re, state_re)
            gate11_benchmark_relative(sentiment_re, scope_re, regime, ctrl, ndl_re, state_re)
            confirmation_re  = gate12_confirmation(reeval_item, ctrl, ndl_re, db, state_re)
            gate13_timing(reeval_item, ctrl, ndl_re, state_re)
            gate14_crowding(reeval_item, ctrl, ndl_re, db, state_re)
            contradiction_re = gate15_contradiction(reeval_item, ctrl, ndl_re, state_re)
            gate16_impact_magnitude(scope_re, topic_re, regime, ctrl, ndl_re, state_re)
            action_re        = gate17_action(sentiment_re, novelty_re, confirmation_re,
                                             contradiction_re, regime, event_re, ctrl, ndl_re, state_re)
            risk_re          = gate18_risk_discounts(action_re, sentiment_re, regime, event_re,
                                                     ctrl, ndl_re, state_re)
            gate19_persistence(topic_re, event_re, ctrl, ndl_re, state_re)
            gate20_evaluation(reeval_item, action_re, ctrl, ndl_re, db, state_re)
            output_re        = gate21_output(action_re, risk_re, regime, scope_re, ctrl, ndl_re, state_re)
            gate22_composite(ctrl, ndl_re, state_re)

            reeval_count += 1
            with db.conn() as c:
                c.execute("UPDATE signals SET needs_reeval=0, updated_at=? WHERE id=?",
                          (db.now(), sig["id"]))

            ndl_re.decide(state_re.routing, output_re["confidence"],
                          output_re.get("explanation", state_re.action_reason))
            ndl_re.commit(db)

            if state_re.routing == "QUEUE":
                db.queue_signal_for_trader(sig["id"])
                queued += 1
                log.info(f"Re-eval promoted to queue: {sig['ticker']}")
            elif state_re.routing == "DISCARD":
                db.discard_signal(sig["id"],
                                  reason=output_re.get("explanation", "re-eval discard"))
                discarded += 1
                log.info(f"Re-eval discarded: {sig['ticker']}")

    except Exception as e:
        log.warning(f"Re-evaluation step failed: {e}")

    # ── Cross-validate: boost signals that reinforce each other ──
    try:
        xval = db.cross_validate_signals(hours_back=96)
        xval_tickers = xval.get('tickers_corroborated', [])
        xval_sectors = xval.get('sector_clusters', [])
        if xval_tickers or xval_sectors:
            log.info(f'Cross-validation: {len(xval_tickers)} ticker(s) corroborated, '
                     f'{len(xval_sectors)} sector cluster(s)')
    except Exception as e:
        log.warning(f'Cross-validation error (non-fatal): {e}')

    portfolio = db.get_portfolio()
    log.info(
        f"Run complete — new={new_signals} queued={queued} "
        f"discarded={discarded} skipped={skipped} reeval={reeval_count} "
        f"portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("news_agent", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="News",
        details=f"new={new_signals} queued={queued} discarded={discarded} skipped={skipped}",
        portfolio_value=portfolio['cash'],
    )

    # ── SCREENING REQUEST HANDLER ──────────────────────────────────────────
    # Check for pending news screening requests from the sector screener.
    # For each ticker, fetch recent Alpaca news headlines and score sentiment.
    _handle_screening_requests(db)

    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="news_agent", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — News Agent')
    parser.add_argument('--session', choices=['market', 'overnight', 'seed'], default='market',
                        help='market=full scan, overnight=disclosures+congress only, seed=45-day historical seed (maps to market)')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID — routes DB and Alpaca credentials to per-customer sources')
    args = parser.parse_args()
    if args.session == 'seed': args.session = 'market'

    # ── Multi-tenant: load per-customer credentials if --customer-id is given ──
    if args.customer_id:
        _CUSTOMER_ID = args.customer_id
        try:
            import auth as _auth
            _ak, _sk = _auth.get_alpaca_credentials(args.customer_id)
            if _ak:
                ALPACA_API_KEY    = _ak
                ALPACA_SECRET_KEY = _sk
            log.info(f"Multi-tenant mode: customer={args.customer_id}")
        except Exception as _e:
            log.warning(f"Could not load customer credentials from auth.db: {_e}")

    acquire_agent_lock("retail_news_agent.py")
    try:
        run(session=args.session)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
