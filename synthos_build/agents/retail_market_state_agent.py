"""
retail_market_state_agent.py — Market-State Aggregator
Synthos · Agent 9

Runs:
  Every enrichment cycle (30 min during market hours) via market daemon

Responsibilities:
  - 4-gate deterministic market state synthesis spine
  - Ingest latest sentiment scan results (The Pulse / Agent 3)
  - Ingest recent news signal distribution (News Agent / Agent 2)
  - Ingest macro regime classification (Macro Regime Agent / Agent 8)
  - Synthesize weighted composite score → unified market state label
  - Store result in customer_settings for downstream consumption
    (Trade Logic, Validator Stack, portal dashboard)

No LLM in any decision path. All gate logic is deterministic and traceable.

Data sources:
  - Internal DB only — scan_log, signals, system_log, customer_settings
  - No external API calls; aggregates what other agents have already collected

Usage:
  python3 retail_market_state_agent.py
"""

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')

# Synthesis weights — must sum to 1.0
W_SENTIMENT = 0.40    # most immediate market read (The Pulse)
W_NEWS      = 0.25    # congressional/insider intelligence (News Agent)
W_MACRO     = 0.35    # structural backdrop (Macro Regime Agent)

# Staleness thresholds
SENTIMENT_STALE_HOURS = 2     # scan_log older than this = stale
NEWS_STALE_HOURS      = 24    # signals window for news scoring
MACRO_STALE_HOURS     = 24    # regime data older than this = stale

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('market_state_agent')


# ── DB HELPERS ────────────────────────────────────────────────────────────

def _master_db():
    """Shared intelligence DB (owner customer)."""
    if OWNER_CUSTOMER_ID:
        return get_customer_db(OWNER_CUSTOMER_ID)
    return get_db()


# ── DATA CLASSES ──────────────────────────────────────────────────────────

@dataclass
class SentimentInput:
    """Result of Gate 1: sentiment ingestion."""
    score: int = 50
    stale: bool = True
    put_call_ratio: float = 0.0
    volume_vs_avg: str = ""
    seller_dominance: str = ""
    cascade_detected: bool = False
    tier: int = 3
    regime: str = "UNKNOWN"
    scan_age_minutes: float = -1.0


@dataclass
class NewsInput:
    """Result of Gate 2: news sentiment ingestion."""
    score: int = 50
    stale: bool = True
    total_signals: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0


@dataclass
class MacroInput:
    """Result of Gate 3: macro regime ingestion."""
    score: int = 50
    stale: bool = True
    regime: str = "UNCERTAIN"
    detail: str = ""


@dataclass
class MarketState:
    """Final synthesized state from Gate 4."""
    sentiment_score: int = 50
    news_score: int = 50
    macro_score: int = 50
    composite: float = 50.0
    label: str = "NEUTRAL"
    stale_components: list = field(default_factory=list)


# ── TIMESTAMP HELPERS ─────────────────────────────────────────────────────

def _parse_db_timestamp(ts_str):
    """Parse a DB timestamp string into a UTC-aware datetime.
    DB stores UTC timestamps as naive-looking strings via self.now()."""
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        # DB timestamps are local time (server TZ); treat as UTC for age checks
        return dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _now_utc():
    """Current time, timezone-aware UTC."""
    return datetime.now(tz=UTC)


# ── GATE 1: SENTIMENT INGESTION ──────────────────────────────────────────

def gate1_sentiment(db):
    """
    Read the latest sentiment scan results from the DB.

    Queries:
      - scan_log: most recent row for put/call, volume, seller dominance, cascade, tier
      - system_log: most recent AGENT_COMPLETE from The Pulse for regime string

    Scoring (0-100):
      cascade detected   →  0
      tier 1 (bearish)   → 20
      tier 2 (elevated)  → 40
      tier 3 (neutral)   → 50
      tier 4 (quiet)     → 70
      no recent scan     → 50 (default, marked stale)
    """
    log.info("Gate 1: Sentiment ingestion")
    result = SentimentInput()
    now = _now_utc()

    # ── Read latest scan_log entry ────────────────────────────────────
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        log.warning(f"Gate 1: scan_log query failed: {e}")
        row = None

    if row:
        scanned_at = _parse_db_timestamp(row['scanned_at'])
        if scanned_at:
            age = now - scanned_at
            result.scan_age_minutes = age.total_seconds() / 60.0
            result.stale = age > timedelta(hours=SENTIMENT_STALE_HOURS)
        else:
            result.stale = True

        result.put_call_ratio = float(row['put_call_ratio'] or 0)
        result.volume_vs_avg = str(row['volume_vs_avg'] or '')
        result.seller_dominance = str(row['seller_dominance'] or '')
        result.cascade_detected = bool(row['cascade_detected'])
        result.tier = int(row['tier'] or 3)

        # Score based on tier and cascade
        if result.cascade_detected:
            result.score = 0
        elif result.tier == 1:
            result.score = 20
        elif result.tier == 2:
            result.score = 40
        elif result.tier == 3:
            result.score = 50
        elif result.tier == 4:
            result.score = 70
        else:
            result.score = 50

        log.info(
            f"  scan_log: tier={result.tier} cascade={result.cascade_detected} "
            f"put_call={result.put_call_ratio:.2f} age={result.scan_age_minutes:.0f}m "
            f"→ score={result.score}"
        )
    else:
        log.info("  scan_log: no rows — using default score=50 (stale)")

    # ── Read regime from system_log (The Pulse AGENT_COMPLETE) ────────
    try:
        with db.conn() as c:
            regime_row = c.execute(
                "SELECT details FROM system_log "
                "WHERE agent='The Pulse' AND event='AGENT_COMPLETE' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        log.warning(f"Gate 1: system_log regime query failed: {e}")
        regime_row = None

    if regime_row and regime_row['details']:
        details = regime_row['details']
        # Parse "regime=XXX" from details string like:
        # "market_state=CAUTION regime=ELEVATED score=0.423 confidence=0.81"
        for part in details.split():
            if part.startswith('regime='):
                result.regime = part.split('=', 1)[1]
                break
        log.info(f"  regime from The Pulse: {result.regime}")
    else:
        log.info("  regime: not found in system_log — using UNKNOWN")

    if result.stale:
        log.info("  NOTE: sentiment data is STALE")

    return result


# ── GATE 2: NEWS SENTIMENT INGESTION ─────────────────────────────────────

def gate2_news(db):
    """
    Read recent signals from the signals table, score the news environment.

    Counts signals by confidence (HIGH/MEDIUM/LOW) in the last 24 hours.
    Infers direction from headline keywords (buy-side vs sell-side language).

    Scoring (0-100):
      Heavy HIGH confidence + positive direction → 80+
      Mostly MEDIUM confidence                   → 50
      Bearish signals dominant                   → 20
      No signals in window                       → 50 (default, marked stale)
    """
    log.info("Gate 2: News sentiment ingestion")
    result = NewsInput()
    now = _now_utc()

    # DB timestamps are UTC — compare against UTC.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEWS_STALE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')

    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT confidence, headline, status FROM signals "
                "WHERE created_at >= ? AND status NOT IN ('DISCARDED', 'EXPIRED')",
                (cutoff,)
            ).fetchall()
    except Exception as e:
        log.warning(f"Gate 2: signals query failed: {e}")
        rows = []

    if not rows:
        log.info("  signals: no recent rows in window — using default score=50 (stale)")
        return result

    result.stale = False
    result.total_signals = len(rows)

    # ── Count by confidence level ─────────────────────────────────────
    for row in rows:
        conf = (row['confidence'] or '').upper()
        if conf == 'HIGH':
            result.high_count += 1
        elif conf == 'MEDIUM':
            result.medium_count += 1
        elif conf == 'LOW':
            result.low_count += 1

    # ── Infer direction from headline keywords ────────────────────────
    _BULLISH_KEYWORDS = {
        'buy', 'bought', 'purchase', 'acquired', 'upgrade', 'upgrades',
        'bullish', 'rally', 'surge', 'soar', 'gain', 'gains', 'growth',
        'positive', 'beat', 'beats', 'exceeds', 'outperform', 'strong',
        'recovery', 'rebound', 'breakout', 'expansion', 'profit',
    }
    _BEARISH_KEYWORDS = {
        'sell', 'sold', 'sale', 'downgrade', 'downgrades', 'bearish',
        'crash', 'plunge', 'drop', 'decline', 'loss', 'losses', 'weak',
        'miss', 'misses', 'underperform', 'warning', 'warns', 'risk',
        'recession', 'contraction', 'layoff', 'layoffs', 'cut', 'cuts',
        'negative', 'concern', 'fears', 'slump', 'tumble',
    }

    for row in rows:
        headline = (row['headline'] or '').lower()
        tokens = set(headline.split())
        bull_hits = len(tokens & _BULLISH_KEYWORDS)
        bear_hits = len(tokens & _BEARISH_KEYWORDS)

        if bull_hits > bear_hits:
            result.positive_count += 1
        elif bear_hits > bull_hits:
            result.negative_count += 1
        else:
            result.neutral_count += 1

    # ── Compute news score ────────────────────────────────────────────
    total = result.total_signals
    high_pct = result.high_count / total if total else 0
    pos_ratio = result.positive_count / total if total else 0.5
    neg_ratio = result.negative_count / total if total else 0.5

    # Base score from confidence distribution
    # HIGH signals carry more weight: 80 * high_pct + 50 * med_pct + 30 * low_pct
    med_pct = result.medium_count / total if total else 0
    low_pct = result.low_count / total if total else 0
    confidence_score = 80 * high_pct + 50 * med_pct + 30 * low_pct

    # Direction modifier: shift score toward bullish/bearish
    # net direction ranges from -1 (all bearish) to +1 (all bullish)
    direction_net = pos_ratio - neg_ratio  # range [-1, +1]
    # Scale direction impact: +-20 points max
    direction_modifier = direction_net * 20

    raw_score = confidence_score + direction_modifier
    result.score = max(0, min(100, int(round(raw_score))))

    log.info(
        f"  signals: {total} total (H={result.high_count} M={result.medium_count} "
        f"L={result.low_count}) pos={result.positive_count} neg={result.negative_count} "
        f"neutral={result.neutral_count} → score={result.score}"
    )

    return result


# ── GATE 3: MACRO REGIME INGESTION ───────────────────────────────────────

# Regime-to-score mapping
_REGIME_SCORES = {
    'EXPANSION':   85,
    'RECOVERY':    75,
    'UNCERTAIN':   50,
    'LATE_CYCLE':  40,
    'CONTRACTION': 20,
    'CRISIS':      10,
}


def gate3_macro(db):
    """
    Read macro regime from customer_settings.

    Keys:
      _MACRO_REGIME        — regime label (e.g., "EXPANSION")
      _MACRO_REGIME_DETAIL — JSON with timestamp, indicators, etc.

    Scoring:
      EXPANSION=85, RECOVERY=75, UNCERTAIN=50, LATE_CYCLE=40,
      CONTRACTION=20, CRISIS=10

    If no regime data or stale (>24h), defaults to UNCERTAIN=50.
    """
    log.info("Gate 3: Macro regime ingestion")
    result = MacroInput()
    now = _now_utc()

    regime_label = None
    regime_detail = None

    try:
        regime_label = db.get_setting('_MACRO_REGIME')
        regime_detail = db.get_setting('_MACRO_REGIME_DETAIL')
    except Exception as e:
        log.warning(f"Gate 3: customer_settings query failed: {e}")

    if not regime_label:
        log.info("  macro regime: not set — using default UNCERTAIN=50 (stale)")
        return result

    result.regime = regime_label.upper().strip()
    result.detail = regime_detail or ""

    # ── Check staleness from detail JSON ──────────────────────────────
    if regime_detail:
        try:
            detail_obj = json.loads(regime_detail)
            ts_str = detail_obj.get('timestamp') or detail_obj.get('updated_at', '')
            if ts_str:
                detail_dt = _parse_db_timestamp(ts_str)
                if detail_dt:
                    age = now - detail_dt
                    result.stale = age > timedelta(hours=MACRO_STALE_HOURS)
                    if result.stale:
                        log.info(f"  macro regime detail is {age.total_seconds()/3600:.1f}h old — STALE")
                else:
                    result.stale = True
            else:
                result.stale = True
        except (json.JSONDecodeError, TypeError):
            result.stale = True
    else:
        # No detail JSON — check if setting was updated recently via updated_at
        # Without detail, we can't determine staleness — mark stale to be safe
        result.stale = True

    # ── Map regime to score ───────────────────────────────────────────
    result.score = _REGIME_SCORES.get(result.regime, 50)

    if result.stale:
        log.info(f"  NOTE: macro regime data is STALE — still using {result.regime}={result.score}")
    else:
        log.info(f"  macro regime: {result.regime} → score={result.score}")

    return result


# ── GATE 4: STATE SYNTHESIS ──────────────────────────────────────────────

def _classify_state(composite):
    """Map composite score (0-100) to a market state label."""
    if composite >= 75:
        return "RISK_ON"
    elif composite >= 55:
        return "CAUTIOUS_BULL"
    elif composite >= 40:
        return "NEUTRAL"
    elif composite >= 20:
        return "CAUTIOUS_BEAR"
    else:
        return "RISK_OFF"


def gate4_synthesis(sentiment, news, macro):
    """
    Combine three component scores with weights into a unified market state.

    Weights:
      Sentiment: 0.40 (most immediate market read)
      News:      0.25 (congressional/insider intelligence)
      Macro:     0.35 (structural backdrop)

    Classification:
      75-100: RISK_ON        — favorable for new positions
      55-74:  CAUTIOUS_BULL  — selective new positions, tighter stops
      40-54:  NEUTRAL        — hold existing, no new positions
      20-39:  CAUTIOUS_BEAR  — reduce exposure, tighten stops
       0-19:  RISK_OFF       — close weak positions, no new entries
    """
    log.info("Gate 4: State synthesis")

    composite = (
        W_SENTIMENT * sentiment.score
        + W_NEWS    * news.score
        + W_MACRO   * macro.score
    )
    composite = max(0.0, min(100.0, composite))
    label = _classify_state(composite)

    stale_components = []
    if sentiment.stale:
        stale_components.append("sentiment")
    if news.stale:
        stale_components.append("news")
    if macro.stale:
        stale_components.append("macro")

    state = MarketState(
        sentiment_score=sentiment.score,
        news_score=news.score,
        macro_score=macro.score,
        composite=round(composite, 1),
        label=label,
        stale_components=stale_components,
    )

    log.info(
        f"  sentiment={sentiment.score} * {W_SENTIMENT} "
        f"+ news={news.score} * {W_NEWS} "
        f"+ macro={macro.score} * {W_MACRO}"
    )
    log.info(f"  composite={state.composite} → {state.label}")
    if stale_components:
        log.info(f"  stale components: {', '.join(stale_components)}")

    return state


# ── PERSIST STATE ─────────────────────────────────────────────────────────

def persist_state(db, state, sentiment, news, macro):
    """Write the synthesized market state to customer_settings."""
    now_str = datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S')

    # Top-level settings for fast reads by downstream agents
    db.set_setting('_MARKET_STATE', state.label)
    db.set_setting('_MARKET_STATE_SCORE', str(state.composite))

    # Stamp all QUEUED signals with the current aggregate market state.
    # Required stamp for QUEUED → VALIDATED promotion.
    try:
        stamped = db.stamp_signals_market_state(state.label)
        log.info(f"market_state stamped {stamped} QUEUED signal(s) with '{state.label}'")
    except Exception as _e:
        log.warning(f"market_state stamp failed (non-fatal): {_e}")

    # Detailed JSON blob for portal display and debugging
    detail = {
        "sentiment_score": state.sentiment_score,
        "news_score": state.news_score,
        "macro_score": state.macro_score,
        "composite": state.composite,
        "state": state.label,
        "timestamp": now_str,
        "stale_components": state.stale_components,
        "weights": {
            "sentiment": W_SENTIMENT,
            "news": W_NEWS,
            "macro": W_MACRO,
        },
        "components": {
            "sentiment": {
                "score": sentiment.score,
                "stale": sentiment.stale,
                "tier": sentiment.tier,
                "cascade_detected": sentiment.cascade_detected,
                "put_call_ratio": sentiment.put_call_ratio,
                "volume_vs_avg": sentiment.volume_vs_avg,
                "seller_dominance": sentiment.seller_dominance,
                "regime": sentiment.regime,
                "scan_age_minutes": round(sentiment.scan_age_minutes, 1),
            },
            "news": {
                "score": news.score,
                "stale": news.stale,
                "total_signals": news.total_signals,
                "high_count": news.high_count,
                "medium_count": news.medium_count,
                "low_count": news.low_count,
                "positive_count": news.positive_count,
                "negative_count": news.negative_count,
                "neutral_count": news.neutral_count,
            },
            "macro": {
                "score": macro.score,
                "stale": macro.stale,
                "regime": macro.regime,
            },
        },
    }
    db.set_setting('_MARKET_STATE_DETAIL', json.dumps(detail))

    log.info(f"  Persisted: _MARKET_STATE={state.label} _MARKET_STATE_SCORE={state.composite}")


# ── RUN ───────────────────────────────────────────────────────────────────

def run():
    """Execute the 4-gate market state synthesis spine."""
    db = _master_db()

    # ── Lifecycle: START ──────────────────────────────────────────────
    db.log_event("AGENT_START", agent="Market State Aggregator")
    db.log_heartbeat("market_state_agent", "RUNNING")
    log.info("=" * 70)
    log.info("MARKET STATE AGGREGATOR — Starting 4-gate synthesis")
    log.info("=" * 70)

    # ── Gate 1: Sentiment ─────────────────────────────────────────────
    sentiment = gate1_sentiment(db)

    # ── Gate 2: News ──────────────────────────────────────────────────
    news = gate2_news(db)

    # ── Gate 3: Macro Regime ──────────────────────────────────────────
    macro = gate3_macro(db)

    # ── Gate 4: Synthesis ─────────────────────────────────────────────
    state = gate4_synthesis(sentiment, news, macro)

    # ── Persist ───────────────────────────────────────────────────────
    persist_state(db, state, sentiment, news, macro)

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"MARKET STATE AGGREGATOR — Complete: {state.label} ({state.composite})")
    if state.stale_components:
        log.info(f"  Degraded inputs: {', '.join(state.stale_components)}")
    log.info("=" * 70)

    db.log_heartbeat("market_state_agent", "OK")
    db.log_event(
        "AGENT_COMPLETE",
        agent="Market State Aggregator",
        details=(
            f"state={state.label} composite={state.composite} "
            f"sentiment={state.sentiment_score} news={state.news_score} "
            f"macro={state.macro_score} "
            f"stale=[{','.join(state.stale_components)}]"
        ),
    )

    # ── Monitor heartbeat POST ────────────────────────────────────────
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="market_state_agent", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")

    return state


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — Market State Aggregator (Agent 9)')
    parser.parse_args()

    acquire_agent_lock("retail_market_state_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
