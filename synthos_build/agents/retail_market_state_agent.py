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
  - Persist to the shared market-intel DB settings for downstream
    consumption (Trade Logic, Validator Stack, portal dashboard).

No LLM in any decision path. All gate logic is deterministic and traceable.

Data sources:
  - Internal DB only — scan_log, signals, system_log, settings
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
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_shared_db, acquire_agent_lock, release_agent_lock
from retail_shared import emit_admin_alert

# ── CONFIG ────────────────────────────────────────────────────────────────
ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Synthesis weights — must sum to 1.0
W_SENTIMENT = 0.40    # most immediate market read (The Pulse)
W_NEWS      = 0.25    # congressional/insider intelligence (News Agent)
W_MACRO     = 0.35    # structural backdrop (Macro Regime Agent)

# Staleness thresholds
SENTIMENT_STALE_HOURS = 2     # scan_log older than this = stale
NEWS_STALE_HOURS      = 24    # signals window for news scoring
MACRO_STALE_HOURS     = 24    # regime data older than this = stale

# Degraded streak — when 2+ of 3 components are stale for this many
# consecutive runs, fire an admin_alert. At a 30-min cadence that's
# ~90 minutes of degraded synthesis — long enough to filter transient
# upstream blips, short enough to surface real problems.
DEGRADED_STREAK_ALERT = 3

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('market_state_agent')


# ── DB HELPERS ────────────────────────────────────────────────────────────

def _master_db():
    """Shared market-intelligence DB.
    2026-04-27: was previously get_customer_db(OWNER_CUSTOMER_ID).  See
    retail_database.get_shared_db() for the architectural rationale."""
    return get_shared_db()


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
    # True when 2+ of 3 components are stale — downstream consumers
    # (validator gate, trader, portal) read this to distinguish
    # "we know it's neutral" from "we have no idea, the score is a default."
    # Written to _MARKET_STATE_DEGRADED setting by persist_state.
    degraded: bool = False


# ── TIMESTAMP HELPERS ─────────────────────────────────────────────────────

def _parse_db_timestamp(ts_str):
    """Parse a DB timestamp string into a UTC-aware datetime.

    db.now() always returns UTC formatted as 'YYYY-MM-DD HH:MM:SS' (naive
    string, no offset). Some upstream agents (e.g. macro regime detail
    JSON) write with the ISO 'T' separator instead. Both forms map to
    the same UTC instant — accept either and tag tzinfo=UTC.
    """
    if not ts_str:
        return None
    # Strip a trailing 'Z' if present (ISO UTC marker — datetime parses
    # better without it than with it on older Python versions).
    s = ts_str.strip().rstrip('Z')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
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

    # ── Read regime from dedicated _PULSE_REGIME_STATE setting ────────
    # Was previously parsed out of system_log AGENT_COMPLETE details by
    # tokenising the string — fragile if upstream log format changed.
    # The Pulse now writes the regime_state to a dedicated setting after
    # AGENT_COMPLETE, which is what we read here.
    try:
        pulse_regime = db.get_setting('_PULSE_REGIME_STATE')
    except Exception as e:
        log.warning(f"Gate 1: _PULSE_REGIME_STATE read failed: {e}")
        pulse_regime = None
    if pulse_regime:
        result.regime = pulse_regime
        log.info(f"  regime from The Pulse: {result.regime}")
    else:
        log.info("  regime: _PULSE_REGIME_STATE not set — using UNKNOWN")

    if result.stale:
        log.info("  NOTE: sentiment data is STALE")

    return result


# ── GATE 2: NEWS SENTIMENT INGESTION ─────────────────────────────────────

def gate2_news(db):
    """
    Read recent signals from the signals table, score the news environment.

    Counts signals by confidence (HIGH/MEDIUM/LOW) in the last 24 hours.
    NOISE confidence is excluded from the query (S4 — NOISE rows previously
    counted in total but not in any bucket, dragging confidence_score
    toward 0).

    Direction: uses signals.sentiment_score (0.0-1.0) populated by the
    Pulse via stamp_signals_sentiment. Replaces the prior naive headline-
    keyword matching which had systematic biases (no negation handling,
    M&A context confusion, single-token false positives like "miss"
    matching both "earnings miss" and "doesn't miss"). When sentiment_score
    is NULL on a signal (not yet stamped), we treat that signal as
    direction-neutral (excluded from direction calc) but still count it
    in confidence buckets.

    Scoring (0-100):
      Heavy HIGH confidence + positive direction → 80+
      Mostly MEDIUM confidence                   → 50
      Bearish signals dominant                   → 20
      No signals in window                       → 50 (default, marked stale)
    """
    log.info("Gate 2: News sentiment ingestion")
    result = NewsInput()

    # DB timestamps are UTC — compare against UTC.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEWS_STALE_HOURS)
              ).strftime('%Y-%m-%d %H:%M:%S')

    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT confidence, sentiment_score, status FROM signals "
                "WHERE created_at >= ? "
                "AND status NOT IN ('DISCARDED', 'EXPIRED') "
                "AND confidence != 'NOISE'",
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

    # ── Count by confidence level + collect sentiment_scores ──────────
    sentiment_values = []   # signals where sentiment was stamped (non-NULL)
    for row in rows:
        conf = (row['confidence'] or '').upper()
        if conf == 'HIGH':
            result.high_count += 1
        elif conf == 'MEDIUM':
            result.medium_count += 1
        elif conf == 'LOW':
            result.low_count += 1
        # signals.sentiment_score is REAL (nullable). 0.0-1.0 scale,
        # 0.5 = neutral. Bucket into pos/neutral/neg for the existing
        # NewsInput counters (kept for portal/debug display).
        sscore = row['sentiment_score']
        if sscore is None:
            result.neutral_count += 1
            continue
        try:
            sval = float(sscore)
        except (TypeError, ValueError):
            result.neutral_count += 1
            continue
        sentiment_values.append(sval)
        if sval > 0.55:
            result.positive_count += 1
        elif sval < 0.45:
            result.negative_count += 1
        else:
            result.neutral_count += 1

    # ── Compute news score ────────────────────────────────────────────
    total = result.total_signals
    high_pct = result.high_count / total if total else 0
    med_pct  = result.medium_count / total if total else 0
    low_pct  = result.low_count / total if total else 0

    # Base score from confidence distribution.
    # HIGH signals carry more weight: 80 * high_pct + 50 * med_pct + 30 * low_pct
    confidence_score = 80 * high_pct + 50 * med_pct + 30 * low_pct

    # Direction modifier from real per-signal sentiment scores. Mean of
    # stamped sentiments → distance from 0.5 → ±20 points. Skip if no
    # signal has been stamped yet (direction-modifier = 0).
    if sentiment_values:
        mean_sent = sum(sentiment_values) / len(sentiment_values)
        # mean_sent in [0,1]; distance from 0.5 in [-0.5,0.5];
        # scale to ±20 points → multiply by 40.
        direction_modifier = (mean_sent - 0.5) * 40
    else:
        mean_sent = None
        direction_modifier = 0.0

    raw_score = confidence_score + direction_modifier
    result.score = max(0, min(100, int(round(raw_score))))

    sent_str = f"mean_sent={mean_sent:.2f}" if mean_sent is not None else "no_stamps"
    log.info(
        f"  signals: {total} total (H={result.high_count} M={result.medium_count} "
        f"L={result.low_count}) pos={result.positive_count} "
        f"neg={result.negative_count} neutral={result.neutral_count} "
        f"{sent_str} → score={result.score}"
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

    # ── Check staleness ───────────────────────────────────────────────
    # Prefer the timestamp inside _MACRO_REGIME_DETAIL JSON (it's the
    # canonical scan-completion time). Fall back to _MACRO_REGIME_UPDATED
    # setting (added in the agent 8 audit) when detail JSON is missing or
    # malformed — pre-fix Gate 3 would mark stale unconditionally in that
    # case, depressing market state score even when macro data was fresh.
    macro_ts_str = None
    if regime_detail:
        try:
            detail_obj = json.loads(regime_detail)
            macro_ts_str = (detail_obj.get('timestamp')
                            or detail_obj.get('updated_at'))
        except (json.JSONDecodeError, TypeError):
            macro_ts_str = None
    if not macro_ts_str:
        try:
            macro_ts_str = db.get_setting('_MACRO_REGIME_UPDATED')
        except Exception as e:
            log.debug(f"_MACRO_REGIME_UPDATED read failed: {e}")
            macro_ts_str = None

    if macro_ts_str:
        macro_dt = _parse_db_timestamp(macro_ts_str)
        if macro_dt:
            age = now - macro_dt
            result.stale = age > timedelta(hours=MACRO_STALE_HOURS)
            if result.stale:
                log.info(f"  macro regime is {age.total_seconds()/3600:.1f}h old — STALE")
        else:
            result.stale = True
    else:
        # No timestamp from any source — can't tell, mark stale to be safe.
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
        # 2+ stale of 3 = the synthesis is mostly defaults and shouldn't
        # be trusted. Single stale component is recoverable (other two
        # still informative).
        degraded=len(stale_components) >= 2,
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
    # Explicit freshness timestamp — validator_stack_agent.py reads this
    # at ~line 459 to decide if market state is stale. Without it, every
    # validator run flagged DEGRADED_STALE_MARKET_STATE. Pairs with
    # `_MARKET_STATE` and `_MARKET_STATE_SCORE` above. Added 2026-04-21.
    db.set_setting('_MARKET_STATE_UPDATED', now_str)
    # Degraded flag — '1' when 2+ of 3 components are stale. Without this
    # flag, downstream consumers can't distinguish "fresh data showing
    # NEUTRAL" from "all 3 sources stale, defaults to NEUTRAL." Validator
    # / trader can decide independently whether to trust a degraded label.
    db.set_setting('_MARKET_STATE_DEGRADED', '1' if state.degraded else '0')

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
        "degraded": state.degraded,
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

    # ── Degraded streak → admin_alert ─────────────────────────────────
    # When 2+ of 3 components are stale for N consecutive runs, fire an
    # admin_alert. Single stale component is recoverable; sustained
    # multi-component staleness means the synthesis output is mostly
    # defaults and the trader / validator are operating blind.
    try:
        if state.degraded:
            streak = int(db.get_setting('_MARKET_STATE_DEGRADED_STREAK') or 0) + 1
            db.set_setting('_MARKET_STATE_DEGRADED_STREAK', str(streak))
            if streak >= DEGRADED_STREAK_ALERT:
                from types import SimpleNamespace
                f = SimpleNamespace(
                    severity="WARNING",
                    code="MARKET_STATE_DEGRADED",
                    gate="GATE4_SYNTHESIS",
                    message=(f"Market state synthesis degraded for {streak} "
                             f"consecutive run(s) — {len(state.stale_components)}/3 "
                             f"components stale ({', '.join(state.stale_components)})"),
                    detail=("Sustained degraded state means the validator/trader "
                            "are operating on default scores rather than real "
                            "sentiment/news/macro signal. Investigate the upstream "
                            "agents: sentiment scan_log, news signals, macro regime."),
                    meta={
                        "streak": streak,
                        "label": state.label,
                        "composite": state.composite,
                        "stale_components": state.stale_components,
                    },
                    customer_id=None,
                )
                emit_admin_alert(db, f,
                                 source_agent='market_state_agent',
                                 category='system')
                log.warning(f"degraded streak={streak} → admin_alert raised")
        else:
            db.set_setting('_MARKET_STATE_DEGRADED_STREAK', '0')
    except Exception as _e:
        log.warning(f"degraded streak tracking failed (non-fatal): {_e}")

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"MARKET STATE AGGREGATOR — Complete: {state.label} ({state.composite})"
             f"{' [DEGRADED]' if state.degraded else ''}")
    if state.stale_components:
        log.info(f"  Stale inputs: {', '.join(state.stale_components)}")
    log.info("=" * 70)

    db.log_heartbeat("market_state_agent", "OK")
    db.log_event(
        "AGENT_COMPLETE",
        agent="Market State Aggregator",
        details=(
            f"state={state.label} composite={state.composite} "
            f"degraded={'1' if state.degraded else '0'} "
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
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('market_state_agent', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

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
