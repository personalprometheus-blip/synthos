"""
trade_logic_agent.py — Trade Logic Agent (ExecutionAgent)
Synthos · Agent 1 · Version 2.0

Runs hourly 24/7 weekdays via retail_scheduler.py --session trade.
Accepts --session (open/midday/close/hourly) for backward compatibility;
actual behavior is driven by time of day (ET).

Decision architecture: 13-gate deterministic control spine
(gates 1-8, 10, 11, 13, 14 + 5.5 news-veto sub-check; gate 9/12
numbers are skipped — the slots existed in an earlier design but
were consolidated into 10/14 respectively).
No LLM calls in any decision path.
Every decision produces a structured human-readable audit log.

Regulatory reference: synthos-company/documentation/governance/AGENT1_SYSTEM_DESCRIPTION.md

SUPERSEDED from v1.x:
  - call_claude() removed — was used for reasoning text only; decision was already rule-based
  - analyze_signal_with_claude() removed — replaced by gates 4–8
  - build_learning_context() removed — learning context now flows via DB metrics (gate 14)
  - fetch_price_history_5yr() removed — price context now computed per-gate without LLM prompt

KEPT from v1.x (flagged for later review):
  - AlpacaClient — unchanged; broker interface is stable          [KEEP]
  - BIL reserve logic (sync_bil_reserve) — unchanged              [KEEP — REVIEW: integrate into gate 11]
  - Supervised/autonomous mode queue — unchanged                  [KEEP]
  - Protective exit (send_protective_exit_email) — unchanged      [KEEP]
  - Monthly tax sweep — unchanged                                 [KEEP]
  - Daily report POST — unchanged                                 [KEEP]
  - PORTFOLIO_TIERS — unchanged                                   [KEEP — REVIEW: unify with gate 7 sizing]
  - PROFIT_RULES — unchanged                                      [KEEP — REVIEW: unify with gate 10 exit]

FLAG — LOG WRITE LOCATION:
  Trade decision logs currently written to system_log table in signals.db.
  A dedicated `trade_decisions` table is recommended for regulatory export.
  Tracked as future work. See AGENT1_SYSTEM_DESCRIPTION.md §5.
"""

import os
import sys
import time
import json
import logging
import argparse
import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone
from calendar import monthrange
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, acquire_agent_lock, release_agent_lock
# Phase C / D6 — shared helpers (2026-04-20)
from retail_shared import (
    kill_switch_active,
    is_market_hours as _is_market_hours_shared,
)

# ── ENVIRONMENT ───────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_BASE_URL   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
ALPACA_DATA_URL   = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')
TRADING_MODE      = os.environ.get('TRADING_MODE', 'PAPER')
OPERATING_MODE    = os.environ.get('OPERATING_MODE', 'MANAGED').upper()
AUTONOMOUS_KEY    = os.environ.get('AUTONOMOUS_UNLOCK_KEY', '')
RESEND_API_KEY    = os.environ.get('RESEND_API_KEY', '')
ALERT_FROM        = os.environ.get('ALERT_FROM', '')
USER_EMAIL        = os.environ.get('USER_EMAIL', '')
# COMPANY_URL routes Scoop events to the Company Node (Pi 4B running company_server.py).
# Falls back to MONITOR_URL if not set (monitor will proxy if it has COMPANY_URL configured).
COMPANY_URL       = os.environ.get('COMPANY_URL', '').rstrip('/')
ET                = ZoneInfo("America/New_York")
MAX_RETRIES       = 2   # Alpaca retry budget per call.
                        # Was 3 → with 15s timeout + 2^n backoff a single failing
                        # call could burn 48s. At 50+ API calls per trader run,
                        # this stacked into the 60-min dispatch hangs we saw on
                        # 2026-04-17. Now at most: 15 + 1 + 15 = 31s worst case.

# ── TRADER WALL-CLOCK BUDGET ──────────────────────────────────────────────
# The daemon's dispatch pool now kills any trader subprocess after 240s
# (see TRADE_INDIVIDUAL_TIMEOUT_SEC in retail_market_daemon.py). We enforce
# a shorter 180s soft budget inside the trader itself so we can commit
# decision logs and exit cleanly before the daemon hard-kills us.
TRADER_RUNTIME_BUDGET_SEC = 180
# If Alpaca fails this many consecutive calls, stop calling Alpaca for the
# rest of this run — no point retrying the same unreachable endpoint 30x.
ALPACA_CIRCUIT_BREAKER_N  = 3

KILL_SWITCH_FILE  = os.path.join(_ROOT_DIR, '.kill_switch')

if TRADING_MODE not in ('PAPER', 'LIVE'):
    print(f"ERROR: Invalid TRADING_MODE '{TRADING_MODE}'. Must be PAPER or LIVE.")
    sys.exit(1)
if TRADING_MODE == 'LIVE' and 'paper' in ALPACA_BASE_URL:
    print("ERROR: TRADING_MODE=LIVE but ALPACA_BASE_URL points to paper endpoint.")
    sys.exit(1)
if OPERATING_MODE == 'AUTOMATIC' and not AUTONOMOUS_KEY:
    print(f"ERROR: OPERATING_MODE=AUTOMATIC requires AUTONOMOUS_UNLOCK_KEY in .env")
    sys.exit(1)

# ── MULTI-TENANT ROUTING ──────────────────────────────────────────────────────
# Set at startup from --customer-id arg. None = single-tenant / env-based (legacy).
_CUSTOMER_ID: 'str | None' = None

def _db():
    """Return per-customer signals.db if --customer-id was given, else the shared system DB."""
    if _CUSTOMER_ID:
        from retail_database import get_customer_db
        return get_customer_db(_CUSTOMER_ID)
    return get_db()


_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')

def _shared_db():
    """Return the master/owner customer DB for shared data (signals, news, intel)."""
    from retail_database import get_customer_db
    return get_customer_db(_OWNER_CID)


def _mark_signal_evaluated(signal_id, reason: str = ''):
    """Mark a QUEUED signal as EVALUATED so it is not re-processed on future runs.

    Called after Gates 4/5/6/11 decide SKIP or WATCH — the signal was seen,
    evaluated, and intentionally not acted on. Tier-dependent expiry
    (30d/7d/2d/1d per retail_database.py:1318) auto-moves unacted signals
    to EXPIRED as a backstop for any that slip through this bookkeeping.
    """
    try:
        sdb = _shared_db()
        with sdb.conn() as c:
            c.execute(
                "UPDATE signals SET status='EVALUATED', updated_at=? WHERE id=? AND status IN ('QUEUED','VALIDATED')",
                (sdb.now(), signal_id),
            )
        log.debug(f"Signal {signal_id} → EVALUATED ({reason})")
    except Exception as exc:
        log.warning(f"Failed to mark signal {signal_id} as EVALUATED: {exc}")


def _customer_email() -> str:
    """Resolve the notification email for this run.

    Multi-tenant: look up the customer's verified email from auth.db
    (email is stored encrypted — auth.decrypt_field handles decryption).
    Single-tenant / env-based fallback: return USER_EMAIL env var.
    """
    if _CUSTOMER_ID:
        try:
            import auth as _auth
            customer = _auth.get_customer_by_id(_CUSTOMER_ID)
            if customer and customer['email_enc']:
                return _auth.decrypt_field(customer['email_enc'])
        except Exception as _e:
            log.warning(f"Could not resolve customer email from auth.db: {_e}")
    return USER_EMAIL


def _is_supervised() -> bool:
    """True when the active operating mode requires trade approval (MANAGED mode)."""
    return OPERATING_MODE == 'MANAGED'

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('trade_logic_agent')


# ── TRADING CONTROLS (all configurable thresholds) ────────────────────────────
# These are the system parameters. Changes here are the ONLY way to modify
# decision behaviour. No logic is embedded in prompt strings.

class TradingControls:
    # Benchmark (Gate 2)
    BENCHMARK_SYMBOL          = os.environ.get('BENCHMARK_SYMBOL', 'SPY')
    BENCHMARK_MA_SHORT        = int(os.environ.get('BENCHMARK_MA_SHORT', '20'))
    BENCHMARK_MA_LONG         = int(os.environ.get('BENCHMARK_MA_LONG', '50'))
    BENCHMARK_DD_THRESHOLD    = float(os.environ.get('BENCHMARK_DD_THRESHOLD', '0.05'))
    BENCHMARK_VOL_THRESHOLD   = float(os.environ.get('BENCHMARK_VOL_THRESHOLD', '0.018'))

    # Regime (Gate 3)
    VOL_HIGH_THRESHOLD        = float(os.environ.get('VOL_HIGH_THRESHOLD', '0.020'))
    MA_FLAT_THRESHOLD         = float(os.environ.get('MA_FLAT_THRESHOLD', '0.005'))
    CORR_SPIKE_THRESHOLD      = float(os.environ.get('CORR_SPIKE_THRESHOLD', '0.75'))
    # TODO: DATA_DEPENDENCY — VIX_HIGH_THRESHOLD requires VIX data feed
    # TODO: DATA_DEPENDENCY — RISK_OFF detection using bonds/credit spreads (TLT proxy in use)

    # Trade Eligibility (Gate 4)
    MIN_AVG_VOLUME            = int(os.environ.get('MIN_AVG_VOLUME', '500000'))
    MAX_SPREAD_PCT            = float(os.environ.get('MAX_SPREAD_PCT', '0.005'))
    MAX_PORTFOLIO_CORR        = float(os.environ.get('MAX_PORTFOLIO_CORR', '0.70'))
    # Phase 5.a — business-day window for Gate 4 EVENT_RISK calendar block.
    # Earnings or macro event within this many biz days → block entry.
    EVENT_CALENDAR_WINDOW_DAYS = int(os.environ.get('EVENT_CALENDAR_WINDOW_DAYS', '2'))

    # Signal (Gate 5)
    MIN_CONFIDENCE_SCORE      = float(os.environ.get('MIN_CONFIDENCE_SCORE', '0.55'))
    SIGNAL_WEIGHTS            = {
        'source_tier':         float(os.environ.get('W_SOURCE_TIER', '0.25')),
        'politician_weight':   float(os.environ.get('W_POLITICIAN', '0.20')),
        'staleness':           float(os.environ.get('W_STALENESS', '0.15')),
        'interrogation':       float(os.environ.get('W_INTERROGATION', '0.20')),
        'sentiment':           float(os.environ.get('W_SENTIMENT', '0.20')),
    }

    # Entry (Gate 6)
    MOMENTUM_ROC_THRESHOLD    = float(os.environ.get('MOMENTUM_ROC_THRESHOLD', '0.02'))
    MEAN_REV_ZSCORE           = float(os.environ.get('MEAN_REV_ZSCORE', '-1.5'))
    BREAKOUT_LOOKBACK         = int(os.environ.get('BREAKOUT_LOOKBACK', '20'))
    PULLBACK_RETRACE_PCT      = float(os.environ.get('PULLBACK_RETRACE_PCT', '0.05'))
    # Anchor-proximity chase caps (2026-04-23). Each entry type uses a
    # historical anchor already computed inside gate6_entry (MA20, rolling
    # breakout high, rolling mean, recent 10-day high). These caps reject
    # entries where current price has already run too far above the anchor —
    # i.e. block "chasing peaks" after a move has already happened.
    # 7-day audit of owner account found no explicit anchor-proximity gate
    # on MOMENTUM or BREAKOUT paths — this closes that.
    # Set a value to 999 (basically unbounded) to disable a given cap while
    # keeping the anchor logging for post-hoc analysis.
    MAX_MOMENTUM_CHASE_PCT    = float(os.environ.get('MAX_MOMENTUM_CHASE_PCT', '0.02'))   # 2% above MA20
    MAX_BREAKOUT_CHASE_PCT    = float(os.environ.get('MAX_BREAKOUT_CHASE_PCT', '0.015'))  # 1.5% above breakout level
    MAX_MEANREV_CHASE_PCT     = float(os.environ.get('MAX_MEANREV_CHASE_PCT', '0.01'))    # 1% above rolling mean (belt-and-suspenders; z-score also gates)

    # Sizing (Gate 7)
    BASE_RISK_PER_TRADE       = float(os.environ.get('BASE_RISK_PER_TRADE', '0.01'))
    MAX_POSITION_PCT          = float(os.environ.get('MAX_POSITION_PCT', '0.10'))
    MAX_TRADE_USD             = float(os.environ.get('MAX_TRADE_USD', '0'))    # 0 = no dollar cap
    DEFENSIVE_SIZE_FACTOR     = float(os.environ.get('DEFENSIVE_SIZE_FACTOR', '0.50'))
    AGGRESSIVE_SIZE_FACTOR    = float(os.environ.get('AGGRESSIVE_SIZE_FACTOR', '1.20'))
    TARGET_VOLATILITY         = float(os.environ.get('TARGET_VOLATILITY', '0.015'))

    # Risk setup (Gate 8)
    ATR_STOP_MULTIPLIER       = float(os.environ.get('ATR_STOP_MULTIPLIER', '2.0'))
    PROFIT_TARGET_MULTIPLE    = float(os.environ.get('PROFIT_TARGET_MULTIPLE', '2.0'))
    ATR_TRAIL_MULTIPLIER      = float(os.environ.get('ATR_TRAIL_MULTIPLIER', '2.0'))
    MAX_HOLDING_DAYS          = int(os.environ.get('MAX_HOLDING_DAYS', '15'))

    # Portfolio (Gate 11)
    MAX_DAILY_LOSS            = float(os.environ.get('MAX_DAILY_LOSS', '-500.0'))
    MAX_DRAWDOWN_PCT          = float(os.environ.get('MAX_DRAWDOWN_PCT', '0.15'))
    MAX_GROSS_EXPOSURE        = float(os.environ.get('MAX_GROSS_EXPOSURE', '0.80'))
    MAX_SECTOR_PCT            = float(os.environ.get('MAX_SECTOR_PCT', '0.25'))
    # MAX_POSITIONS kept as an env-var default for admin tooling / debugging,
    # but NOT enforced in the trade path — Gate 11 uses tier["max_positions"]
    # from PORTFOLIO_TIERS (see below). Customer settings no longer write
    # to this field.
    MAX_POSITIONS             = int(os.environ.get('MAX_POSITIONS', '10'))
    MAX_LEVERAGE              = float(os.environ.get('MAX_LEVERAGE', '1.0'))

    # Adaptive kill-condition inputs — consumed by Gate 14 (Evaluation)
    # for rolling Sharpe / drawdown suspension. "Gate 12" was a planned
    # section in an earlier design; that logic was rolled into Gate 14
    # and this header preserves that lineage without a phantom gate.
    MIN_SHARPE_THRESHOLD      = float(os.environ.get('MIN_SHARPE_THRESHOLD', '0.5'))
    PERFORMANCE_WINDOW_DAYS   = int(os.environ.get('PERFORMANCE_WINDOW_DAYS', '30'))

    # Stress (Gate 13)
    FLASH_CRASH_PCT           = float(os.environ.get('FLASH_CRASH_PCT', '0.03'))
    FLASH_CRASH_MINUTES       = int(os.environ.get('FLASH_CRASH_MINUTES', '10'))
    BENCHMARK_CRASH_PCT       = float(os.environ.get('BENCHMARK_CRASH_PCT', '0.05'))

    # Session timing
    CONSERVATIVE_AFTER_HOUR   = int(os.environ.get('CONSERVATIVE_AFTER_HOUR', '15'))
    LATE_DAY_TIGHTEN_PCT      = float(os.environ.get('LATE_DAY_TIGHTEN_PCT', '0.25'))
    # Open-hour stop-loss grace: skip stop-loss enforcement for the first N
    # minutes after 09:30 ET. Overnight ratcheted stops often sit inside the
    # market-open gap band — a gap-down that reverses within minutes would
    # otherwise trigger them immediately. Set to 0 to disable (= prior behavior).
    # 7-day audit 2026-04-23 found all 7 opening-hour stops fired at 09:32 ET
    # with standard trail mechanics (not SPY-corr tightening).
    STOP_LOSS_OPEN_GRACE_MINUTES = int(os.environ.get('STOP_LOSS_OPEN_GRACE_MINUTES', '15'))

    # Benchmark correlation
    BENCHMARK_CORR_WIDEN      = float(os.environ.get('BENCHMARK_CORR_WIDEN', '1.50'))
    BENCHMARK_CORR_TIGHTEN    = float(os.environ.get('BENCHMARK_CORR_TIGHTEN', '0.75'))

    # Evaluation (Gate 14)
    EVAL_MIN_SHARPE           = float(os.environ.get('EVAL_MIN_SHARPE', '0.3'))
    EVAL_MAX_DRAWDOWN         = float(os.environ.get('EVAL_MAX_DRAWDOWN', '0.20'))

    # Legacy (KEEP — review for unification)
    IDLE_RESERVE_PCT          = float(os.environ.get('IDLE_RESERVE_PCT', '0.20'))
    TRADEABLE_PCT             = float(os.environ.get('TRADEABLE_PCT', '0.80'))
    BIL_TICKER                = os.environ.get('BIL_TICKER', 'BIL')
    BIL_REBALANCE_THRESHOLD   = float(os.environ.get('BIL_REBALANCE_THRESHOLD', '10.0'))
    CLOSE_SESSION_MODE        = os.environ.get('CLOSE_SESSION_MODE', 'conservative')
    SPOUSAL_WEIGHT            = os.environ.get('SPOUSAL_WEIGHT', 'reduced')
    MONTHLY_INFRA_COST        = float(os.environ.get('MONTHLY_INFRA_COST', '20.0'))
    GAIN_TAX_PCT              = float(os.environ.get('GAIN_TAX_PCT', '0.10'))


C = TradingControls()  # Global defaults — overridden per-customer in run()



def _apply_customer_settings():
    """Override TradingControls with per-customer settings from signals.db.
    Hierarchy: customer_settings DB → global .env (already loaded) → hardcoded default.
    Only overrides the settings exposed in the config panel."""
    global C
    if not _CUSTOMER_ID:
        return  # single-tenant mode — use global env as-is

    try:
        db = _db()
        settings = db.get_all_settings()
        if not settings:
            return  # no customer overrides — use global

        # Map DB keys to Controls attributes with type conversion
        overrides = {
            'MIN_CONFIDENCE':       ('MIN_CONFIDENCE_SCORE',   lambda v: {'LOW': 0.30, 'MEDIUM': 0.55, 'HIGH': 0.75}[v] if v in ('LOW','MEDIUM','HIGH') else float(v)),
            'MAX_POSITION_PCT':     ('MAX_POSITION_PCT',       float),
            'MAX_TRADE_USD':        ('MAX_TRADE_USD',          float),
            # MAX_POSITIONS deliberately omitted — position count is tier-based
            # via PORTFOLIO_TIERS (enforced in Gate 11). The customer setting
            # was never read by the gate — this removes the ghost plumbing.
            'MAX_DAILY_LOSS':       ('MAX_DAILY_LOSS',         lambda v: -abs(float(v))),
            'MAX_SECTOR_PCT':       ('MAX_SECTOR_PCT',         lambda v: float(v) / 100 if float(v) > 1 else float(v)),
            'CLOSE_SESSION_MODE':   ('CLOSE_SESSION_MODE',     str),
            'MAX_STALENESS':        ('MAX_STALENESS',          str),
            'MAX_DRAWDOWN_PCT':     ('MAX_DRAWDOWN_PCT',       lambda v: float(v) / 100 if float(v) > 1 else float(v)),
            'MAX_HOLDING_DAYS':     ('MAX_HOLDING_DAYS',       int),
            'MAX_GROSS_EXPOSURE':   ('MAX_GROSS_EXPOSURE',     lambda v: float(v) / 100 if float(v) > 1 else float(v)),
            'PROFIT_TARGET_MULTIPLE':('PROFIT_TARGET_MULTIPLE', float),
            'TRADING_MODE':         ('TRADING_MODE',           str),
            'ENABLE_BIL_RESERVE':   ('ENABLE_BIL_RESERVE',     lambda v: v != '0'),
            'IDLE_RESERVE_PCT':     ('IDLE_RESERVE_PCT',       lambda v: float(v) / 100 if float(v) > 1 else float(v)),
            # Optimizer-tuned parameters
            'ATR_TRAIL_MULTIPLIER': ('ATR_TRAIL_MULTIPLIER',   float),
            'ATR_STOP_MULTIPLIER':  ('ATR_STOP_MULTIPLIER',    float),
            'LATE_DAY_TIGHTEN_PCT': ('LATE_DAY_TIGHTEN_PCT',   float),
            'BENCHMARK_CORR_WIDEN': ('BENCHMARK_CORR_WIDEN',   float),
            'BENCHMARK_CORR_TIGHTEN':('BENCHMARK_CORR_TIGHTEN', float),
            'VOL_MULT_LOW':         ('_VOL_MULT_LOW',          float),
            'VOL_MULT_MID':         ('_VOL_MULT_MID',          float),
            'VOL_MULT_HIGH':        ('_VOL_MULT_HIGH',         float),
            'PROFIT_TIER_1_PCT':    ('_PROFIT_TIER_1_PCT',     float),
            'PROFIT_TIER_2_PCT':    ('_PROFIT_TIER_2_PCT',     float),
            'PROFIT_TIER_3_PCT':    ('_PROFIT_TIER_3_PCT',     float),
        }

        applied = []
        for db_key, (attr, converter) in overrides.items():
            if db_key in settings:
                try:
                    val = converter(settings[db_key])
                    setattr(C, attr, val)
                    applied.append(f"{attr}={val}")
                except (ValueError, TypeError) as e:
                    print(f"[Controls] Bad value for {db_key}: {settings[db_key]} ({e})")

        if applied:
            print(f"[Controls] Customer {_CUSTOMER_ID[:8]} overrides: {', '.join(applied)}")

        # Per-customer kill switch
        if settings.get('KILL_SWITCH') == '1':
            # Create a temporary kill switch indicator
            C._customer_kill_switch = True
            print(f"[Controls] Customer {_CUSTOMER_ID[:8]} has KILL SWITCH engaged")
        else:
            C._customer_kill_switch = False

        # Per-customer operating mode
        if 'OPERATING_MODE' in settings:
            global OPERATING_MODE
            OPERATING_MODE = settings['OPERATING_MODE'].upper()
            print(f"[Controls] Customer {_CUSTOMER_ID[:8]} mode: {OPERATING_MODE}")

    except Exception as e:
        print(f"[Controls] Could not load customer settings: {e} — using global defaults")


PORTFOLIO_TIERS = [
    # Tier is the infrastructure's cap on BOTH position count and portfolio-
    # wide deployment. Position count scales with equity (Seed 3 → Mature 12)
    # so small accounts don't over-diversify into noise. Deployment cap is
    # uniform at 95% — the customer's preset (MAX_GROSS_EXPOSURE, typically
    # 60/80/95) is the narrower lever. The prior 30–50% tier cap was a
    # silent ceiling that blocked even aggressive customers from deploying
    # what their preset said they should.
    {"threshold": 0,      "max_deployed": 0.95, "max_positions": 3,  "label": "Seed"   },
    {"threshold": 1000,   "max_deployed": 0.95, "max_positions": 5,  "label": "Early"  },
    {"threshold": 5000,   "max_deployed": 0.95, "max_positions": 8,  "label": "Growth" },
    {"threshold": 20000,  "max_deployed": 0.95, "max_positions": 10, "label": "Scaled" },
    {"threshold": 50000,  "max_deployed": 0.95, "max_positions": 12, "label": "Mature" },
]

VOLATILITY_BUCKETS = {
    "low":  {"multiplier": 1.5, "label": "Low Vol",
             "sectors": ["Utilities","Industrials","Consumer Staples","Real Estate"]},
    "mid":  {"multiplier": 1.1, "label": "Mid Vol",
             "sectors": ["Defense","Financials","Healthcare","Materials","Energy"]},
    "high": {"multiplier": 0.85,"label": "High Vol",
             "sectors": ["Technology","Consumer Discretionary","Communication"]},
}

STALENESS_DISCOUNTS = {"Fresh": 0.0, "Aging": 0.15, "Stale": 0.30, "Expired": 0.50}

PROFIT_RULES_DEFAULT = [
    {"gain_pct": 0.08, "sell_pct": 0.33, "label": "8% — sell ⅓"},
    {"gain_pct": 0.15, "sell_pct": 0.50, "label": "15% — sell ½"},
    {"gain_pct": 0.25, "sell_pct": 0.75, "label": "25% — sell ¾"},
]

def get_profit_rules():
    """Return profit rules using optimizer-tuned thresholds if available."""
    t1 = getattr(C, '_PROFIT_TIER_1_PCT', None) or 0.08
    t2 = getattr(C, '_PROFIT_TIER_2_PCT', None) or 0.15
    t3 = getattr(C, '_PROFIT_TIER_3_PCT', None) or 0.25
    return [
        {"gain_pct": t1, "sell_pct": 0.33, "label": f"{t1*100:.0f}% — sell ⅓"},
        {"gain_pct": t2, "sell_pct": 0.50, "label": f"{t2*100:.0f}% — sell ½"},
        {"gain_pct": t3, "sell_pct": 0.75, "label": f"{t3*100:.0f}% — sell ¾"},
    ]


# ── TRADE DECISION LOG ────────────────────────────────────────────────────────

class TradeDecisionLog:
    """
    Builds a structured, human-readable + machine-readable record of every
    decision made during signal evaluation and position management.

    FLAG — LOG WRITE LOCATION:
      Currently: written to system_log table via db.log_event().
      Recommended: dedicated `trade_decisions` table or .jsonl file for
      regulatory export and long-term storage. Tracked as future work.
    """

    def __init__(self, session: str, ticker: str = None, signal_id=None):
        self.session    = session
        self.ticker     = ticker
        self.signal_id  = signal_id
        self.timestamp  = datetime.now(ET).isoformat()
        self.gates      = []
        self.final      = None
        self.notes      = []

    def gate(self, name: str, result, inputs: dict, reason: str):
        """Record a gate evaluation."""
        self.gates.append({
            "gate":   name,
            "result": str(result),
            "inputs": inputs,
            "reason": reason,
        })
        log.info(f"[GATE:{name}] result={result} reason={reason}")

    def decide(self, decision: str, detail: str = ""):
        self.final = decision
        self.notes.append(detail)
        log.info(f"[DECISION] {self.ticker or '—'} → {decision} | {detail}")

    def note(self, text: str):
        self.notes.append(text)

    def to_human(self) -> str:
        lines = [
            "=" * 72,
            f"TRADE DECISION LOG",
            f"  Timestamp : {self.timestamp}",
            f"  Session   : {self.session.upper()}",
            f"  Ticker    : {self.ticker or '—'}",
            f"  Signal ID : {self.signal_id or '—'}",
            f"  Mode      : {TRADING_MODE} / {OPERATING_MODE}",
            "-" * 72,
        ]
        for g in self.gates:
            lines.append(f"  GATE {g['gate']}")
            lines.append(f"    Result : {g['result']}")
            lines.append(f"    Reason : {g['reason']}")
            for k, v in g['inputs'].items():
                lines.append(f"    {k:<22}: {v}")
        lines.append("-" * 72)
        lines.append(f"  FINAL DECISION : {self.final or 'NONE'}")
        for n in self.notes:
            if n:
                lines.append(f"    → {n}")
        lines.append("=" * 72)
        return "\n".join(lines)

    def to_machine(self) -> dict:
        return {
            "timestamp":  self.timestamp,
            "session":    self.session,
            "ticker":     self.ticker,
            "signal_id":  self.signal_id,
            "gates":      self.gates,
            "decision":   self.final,
            "notes":      self.notes,
        }

    def commit(self, db):
        """Write to system_log and logic_audits/. FLAG: move to trade_decisions table."""
        human   = self.to_human()
        machine = self.to_machine()
        log.info("\n" + human)
        try:
            db.log_event(
                "TRADE_DECISION",
                agent="trade_logic_agent",
                details=json.dumps(machine)[:2000],  # FLAG: truncation — need dedicated table
            )
        except Exception as e:
            log.warning(f"TradeDecisionLog.commit failed: {e}")
        # Write to scan_log for per-ticker gate breakdown visibility
        if self.ticker:
            try:
                # Map gate results to scan_log schema
                gate_map = {g['gate']: g for g in self.gates}
                liquidity = gate_map.get('4_LIQUIDITY', {})
                score_gate = gate_map.get('5_SIGNAL_SCORE', {})
                entry_gate = gate_map.get('6_ENTRY', {})

                vol_str = liquidity.get('inputs', {}).get('avg_volume_30d', '')
                score_val = score_gate.get('inputs', {}).get('composite_score', '')

                # Determine tier: 1=passed all gates, 2=passed score, 3=passed liquidity, 4=failed early
                if self.final in ('MIRROR', 'ROTATE'):
                    tier = 1
                elif score_gate and float(score_gate.get('result', '0') or '0') > 0:
                    tier = 2
                elif liquidity.get('result') == 'True':
                    tier = 3
                else:
                    tier = 4

                summary_parts = []
                if self.final:
                    summary_parts.append(self.final)
                if score_val:
                    summary_parts.append(f"score={score_val}")
                for n in self.notes[:2]:
                    if n:
                        summary_parts.append(n[:60])

                db.log_scan(
                    ticker=self.ticker,
                    put_call_ratio=None,
                    put_call_avg30d=None,
                    insider_net=None,
                    volume_vs_avg=vol_str or None,
                    seller_dominance=None,
                    cascade_detected=False,
                    tier=tier,
                    event_summary=' | '.join(summary_parts)[:200],
                )
            except Exception as e:
                log.debug(f"scan_log write failed (non-fatal): {e}")

        # Also write to human-readable logic audit log
        try:
            import os as _os
            audit_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                      'logs', 'logic_audits')
            _os.makedirs(audit_dir, exist_ok=True)
            today     = datetime.now(ET).strftime('%Y-%m-%d')
            log_path  = _os.path.join(audit_dir, f"{today}_bolt_decisions.log")
            with open(log_path, 'a') as f:
                f.write(human + "\n")
        except Exception as e:
            log.warning(f"Audit log write failed (non-fatal): {e}")


# ── REGIME STATE ──────────────────────────────────────────────────────────────

@dataclass
class RegimeState:
    volatility:  str = "NORMAL"   # LOW / NORMAL / HIGH
    trend:       str = "NEUTRAL"  # BULL / BEAR / SIDEWAYS
    risk_posture:str = "RISK_ON"  # RISK_ON / RISK_OFF
    mode:        str = "NEUTRAL"  # DEFENSIVE / NEUTRAL / AGGRESSIVE


# ── KILL SWITCH ───────────────────────────────────────────────────────────────
# Two halt layers (see docs/specs/HALT_AGENT_REWRITE.md):
#   Admin halt:     system_halt row in MASTER DB (applies to every customer)
#                   + legacy .kill_switch file check for emergency SSH kills
#   Customer halt:  system_halt row in this customer's own DB
#                   + legacy KILL_SWITCH='1' setting for backwards compat
# The trader's run() entry-point skip check reads both and exits clean if
# either is active, BEFORE opening an Alpaca client or doing any real work.

# kill_switch_active: imported from retail_shared above

def clear_kill_switch():
    try:
        if os.path.exists(KILL_SWITCH_FILE) or getattr(C, "_customer_kill_switch", False):
            os.remove(KILL_SWITCH_FILE)
            log.info("Kill switch cleared")
    except Exception as e:
        log.error(f"Could not clear kill switch: {e}")


def _check_halt_state() -> 'tuple[str, str] | None':
    """Return (source, reason) if halted, else None.

    source ∈ {'admin', 'customer'}. Called at the very top of run() before
    any other DB read or Alpaca call. Three checks layered in order:
      1. Admin halt row in shared DB (master customer's signals.db)
      2. Legacy .kill_switch file (emergency SSH kill — bypasses DB)
      3. Customer halt row in this customer's own DB
      4. Legacy KILL_SWITCH='1' setting in this customer's settings (bwd-compat)

    Any failure reading the DB falls soft — we don't fabricate a halt state
    just because a DB read hiccupped."""
    # 1. Admin halt via system_halt table in master DB
    try:
        shared = _shared_db()
        admin = shared.get_halt()
        if admin and admin.get('active'):
            return ('admin', admin.get('reason') or '')
    except Exception as e:
        log.debug(f"admin halt check (shared DB) failed: {e}")

    # 2. Legacy .kill_switch file — still honored as emergency fallback
    if kill_switch_active():
        return ('admin', 'legacy .kill_switch file present')

    # 3. Customer halt via system_halt table in this customer's DB
    try:
        db = _db()
        cust = db.get_halt()
        if cust and cust.get('active'):
            return ('customer', cust.get('reason') or '')
    except Exception as e:
        log.debug(f"customer halt check (per-customer DB) failed: {e}")

    # 4. Legacy per-customer KILL_SWITCH='1' setting (backwards-compat bridge)
    try:
        db = _db()
        if db.get_setting('KILL_SWITCH') == '1':
            return ('customer', 'legacy KILL_SWITCH=1 setting')
    except Exception as e:
        log.debug(f"legacy KILL_SWITCH setting check failed: {e}")

    return None


# ── HELPERS ───────────────────────────────────────────────────────────────────

def get_portfolio_tier(total_value):
    tier = PORTFOLIO_TIERS[0]
    for t in PORTFOLIO_TIERS:
        if total_value >= t["threshold"]:
            tier = t
    return tier

def get_volatility_bucket(sector):
    sector = (sector or "").strip()
    for key, bucket in VOLATILITY_BUCKETS.items():
        if sector in bucket["sectors"]:
            return key, bucket
    return "mid", VOLATILITY_BUCKETS["mid"]

def calculate_trail_stop(atr, price, sector):
    key, bucket = get_volatility_bucket(sector)
    # Check for optimizer-tuned multiplier override
    override_attr = f'_VOL_MULT_{key.upper()}'
    multiplier = getattr(C, override_attr, None) or bucket["multiplier"]
    amt = round(atr * multiplier, 2)
    pct = round((amt / price) * 100, 2)
    return amt, pct, bucket["label"]

def is_last_trading_day_of_month():
    today   = datetime.now(ET).date()
    _, days = monthrange(today.year, today.month)
    last    = date(today.year, today.month, days)
    while last.weekday() > 4:
        last -= timedelta(days=1)
    return today == last


def get_market_time_regime(now=None):
    """
    Derive market time regime from current ET time.
    Replaces all session-name-based branching.
    """
    if now is None:
        now = datetime.now(ET)
    hour, minute = now.hour, now.minute
    mins = hour * 60 + minute
    return {
        "is_market_hours": 570 <= mins <= 960,  # 9:30-16:00
        "is_premarket":    mins < 570,
        "is_afterhours":   mins > 960,
        "is_late_day":     hour >= C.CONSERVATIVE_AFTER_HOUR,
        "is_overnight":    hour >= 16 or hour < 8,
        "hour": hour,
        "minute": minute,
    }


def compute_spy_correlation(alpaca, ticker, spy_bars_cache=None, lookback=20):
    """
    Compute rolling correlation between ticker and SPY daily returns.
    Returns correlation coefficient (-1 to 1) or None if insufficient data.
    Pass spy_bars_cache to avoid redundant API calls in a loop.
    """
    spy_bars = spy_bars_cache or alpaca.get_bars(C.BENCHMARK_SYMBOL, days=lookback + 5)
    ticker_bars = alpaca.get_bars(ticker, days=lookback + 5)
    if not spy_bars or not ticker_bars:
        return None
    if len(spy_bars) < lookback or len(ticker_bars) < lookback:
        return None

    spy_ret = [(spy_bars[i]["c"] - spy_bars[i-1]["c"]) / spy_bars[i-1]["c"]
               for i in range(1, len(spy_bars))][-lookback:]
    tkr_ret = [(ticker_bars[i]["c"] - ticker_bars[i-1]["c"]) / ticker_bars[i-1]["c"]
               for i in range(1, len(ticker_bars))][-lookback:]

    n = min(len(spy_ret), len(tkr_ret))
    if n < 10:
        return None
    spy_ret, tkr_ret = spy_ret[-n:], tkr_ret[-n:]

    mean_s = sum(spy_ret) / n
    mean_t = sum(tkr_ret) / n
    cov   = sum((s - mean_s) * (t - mean_t) for s, t in zip(spy_ret, tkr_ret)) / n
    std_s = (sum((s - mean_s)**2 for s in spy_ret) / n) ** 0.5
    std_t = (sum((t - mean_t)**2 for t in tkr_ret) / n) ** 0.5

    if std_s < 1e-10 or std_t < 1e-10:
        return None
    return round(cov / (std_s * std_t), 4)


def confidence_to_score(confidence_str: str) -> float:
    """Map legacy confidence label to numeric score."""
    return {"HIGH": 0.85, "MEDIUM": 0.60, "LOW": 0.35, "NOISE": 0.10}.get(
        (confidence_str or "LOW").upper(), 0.35
    )

def staleness_to_score(staleness_str: str) -> float:
    return {"Fresh": 1.0, "Aging": 0.75, "Stale": 0.45, "Expired": 0.10}.get(
        staleness_str or "Fresh", 0.75
    )

def interrogation_to_score(status: str) -> float:
    return {"VALIDATED": 1.0, "CORROBORATED": 0.85, "UNVALIDATED": 0.50,
            "CHALLENGED": 0.20, "REJECTED": 0.0}.get(status or "UNVALIDATED", 0.50)


# ── MARKET HOURS + OVERNIGHT QUEUE ────────────────────────────────────────────
# The overnight-queue gate below sits at the AlpacaClient boundary so every
# market-order submit path goes through one check. Orders submitted outside
# market hours land in pending_approvals with queue_origin='overnight' and
# get re-evaluated by run_pre_open_reeval() (in retail_market_daemon.py) on
# the next market-open cycle. See docs/overnight_queue_plan.md.

# US regular market session boundaries in ET. DST handled automatically by
# the ZoneInfo comparison in is_market_hours_utc_now.
_MARKET_OPEN_HOUR  = 9
_MARKET_OPEN_MIN   = 30
_MARKET_CLOSE_HOUR = 16
_MARKET_CLOSE_MIN  = 0


def is_market_hours_utc_now() -> bool:
    """True during US regular session hours (9:30-16:00 ET, weekdays).

    Naming kept `_utc_now` for backward-compat with internal call sites;
    delegates to retail_shared canonical. Phase C / D6 (2026-04-20).
    """
    return _is_market_hours_shared()


def _queue_overnight_order(ticker: str, qty, side: str,
                           order_type: str = "market",
                           notional: float = None,
                           close_position: bool = False) -> None:
    """Write a QUEUED_FOR_OPEN row to the shared pending_approvals table
    in place of submitting an order to Alpaca. Returns None so callers
    that check `if order:` treat this as "no order placed" and skip
    the downstream DB position-open/close work — which is correct: the
    actual fill happens at market open after pre-open re-evaluation.

    The signal_id has a deterministic-ish shape
    (`overnight_<side>_<ticker>_<ts>_<rand6>`) so logs can be grepped
    and duplicates within the same minute don't collide."""
    import uuid as _uuid
    ts  = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    sid = f"overnight_{side}_{ticker}_{ts}_{_uuid.uuid4().hex[:6]}"
    reason_bits = [f"overnight queue: {side} {ticker}"]
    if close_position:
        reason_bits.append("close_position=True")
    elif notional is not None:
        reason_bits.append(f"notional=${notional:.2f}")
    elif qty is not None:
        reason_bits.append(f"qty={qty}")
    reason_bits.append(f"order_type={order_type}")
    reasoning = " | ".join(reason_bits)
    try:
        # Write to THIS customer's own DB — pending_approvals is
        # already per-customer (matches how managed-mode approvals
        # work). The daemon's pre-open re-evaluation walks active
        # customer DBs and processes each one's overnight queue
        # independently, so the customer's portal surfaces the right
        # rows and the re-eval step doesn't need a customer_id filter
        # on a shared table.
        _db().queue_approval(
            signal_id=sid, ticker=ticker, shares=qty,
            reasoning=reasoning, session="overnight",
            queue_origin="overnight", status="QUEUED_FOR_OPEN",
        )
        log.info(f"[OVERNIGHT] Queued {side} {ticker} — id={sid} ({reasoning})")
    except Exception as e:
        # Queue write failure is serious — the alternative is that the
        # order silently vanishes. Log at ERROR so auditor / fault
        # detector sees it.
        log.error(f"[OVERNIGHT] Failed to queue {side} {ticker}: {e}")
    return None


# ── RUNTIME WATCHDOG ──────────────────────────────────────────────────────────
# Two jobs:
#   1. Track the current phase ("reconciliation", "gate_10_position_review",
#      etc.) so if the trader ever hangs again, the last-logged phase tells
#      us exactly where it died — no more mystery hangs.
#   2. Enforce TRADER_RUNTIME_BUDGET_SEC by flipping _BUDGET_EXCEEDED, which
#      gate loops check so they bail between iterations instead of mid-SQL.
#
# This replaces the old "trader runs until the daemon kills it at 240s"
# behavior. The budget is a soft wall-clock ceiling inside the trader;
# the daemon's hard kill is the safety net if the soft ceiling misses.
import threading as _threading

_PHASE_STATE = {
    'current':   'unstarted',
    'started_at': 0.0,     # time.monotonic()
    'detail':    '',
}
_BUDGET_EXCEEDED = False
_WATCHDOG_STOP   = _threading.Event()


def _set_phase(name: str, detail: str = '') -> None:
    """Update the current-phase marker so the watchdog's next tick logs it."""
    _PHASE_STATE['current']    = name
    _PHASE_STATE['started_at'] = time.monotonic()
    _PHASE_STATE['detail']     = detail


def budget_exceeded() -> bool:
    """Gate loops check this between iterations and short-circuit when True.

    A trader that blows past TRADER_RUNTIME_BUDGET_SEC commits whatever
    decision/scan state it has and returns, rather than being hard-killed
    mid-transaction by the daemon's 240s pool timeout.
    """
    return _BUDGET_EXCEEDED


def _runtime_watchdog(t0: float, budget_sec: int) -> None:
    """Background thread: logs phase every 15s, trips the budget flag at the
    ceiling. No daemon thread surprise — stops cleanly when _WATCHDOG_STOP
    is set by the run() finally block."""
    global _BUDGET_EXCEEDED
    while not _WATCHDOG_STOP.wait(timeout=15):
        elapsed = time.monotonic() - t0
        phase_elapsed = time.monotonic() - _PHASE_STATE['started_at']
        detail = f" ({_PHASE_STATE['detail']})" if _PHASE_STATE['detail'] else ""
        log.info(
            f"[WATCHDOG] t={elapsed:.0f}s  phase={_PHASE_STATE['current']}"
            f"{detail}  phase_elapsed={phase_elapsed:.0f}s"
        )
        if elapsed > budget_sec and not _BUDGET_EXCEEDED:
            _BUDGET_EXCEEDED = True
            log.warning(
                f"[WATCHDOG] Runtime budget {budget_sec}s exceeded in phase "
                f"'{_PHASE_STATE['current']}' — gate loops will short-circuit"
            )


# ── ALPACA CLIENT (KEEP — unchanged from v1.x) ────────────────────────────────

class AlpacaClient:
    def __init__(self):
        self.base_url = ALPACA_BASE_URL.rstrip('/')
        self.headers  = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type":        "application/json",
        }
        self._bar_cache = {}  # (ticker, days) → [bars]
        # Circuit breaker: after ALPACA_CIRCUIT_BREAKER_N consecutive failures
        # we stop calling Alpaca for the rest of this trader run. This cuts
        # the worst-case hang path (Alpaca slow → retry × 3 × 50 API calls)
        # from ~40 minutes to ~90 seconds. Downstream callers check the
        # return value, so circuit-open returns None just like a failure —
        # the trader degrades to "use DB data / skip this evaluation" rather
        # than stalling.
        self._consecutive_failures = 0
        self._circuit_open         = False

    def _circuit_check(self) -> bool:
        """Return True if the circuit is OK to call Alpaca. Used by the
        direct `requests.get` paths (prefetch_bars, get_bars, get_position_safe)
        that don't go through _request(). Increment on failure via the same
        _consecutive_failures counter so all Alpaca paths share one breaker."""
        return not self._circuit_open

    def _circuit_record(self, success: bool) -> None:
        if success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            if (self._consecutive_failures >= ALPACA_CIRCUIT_BREAKER_N
                    and not self._circuit_open):
                self._circuit_open = True
                log.warning(
                    f"[CIRCUIT] Alpaca circuit breaker opened after "
                    f"{self._consecutive_failures} consecutive failures"
                )

    def prefetch_bars(self, tickers, days=70):
        """Batch-fetch daily bars for multiple tickers in one API call.
        Populates _bar_cache so subsequent get_bars() calls are instant.
        Uses Alpaca multi-symbol endpoint: /v2/stocks/bars?symbols=A,B,C"""
        if not tickers:
            return
        if not self._circuit_check():
            log.info("[PREFETCH] Skipped — Alpaca circuit open")
            return
        unique = sorted(set(t.upper() for t in tickers))
        # Alpaca reads the 'Z' suffix as UTC. ET labeled as Z was a 4-5 hour
        # silent offset — use UTC for API timestamps.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        end   = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (now_utc - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        # Alpaca allows up to ~100 symbols per request; chunk if needed
        CHUNK = 50
        for i in range(0, len(unique), CHUNK):
            if not self._circuit_check():
                log.info("[PREFETCH] Circuit opened mid-sweep — bailing")
                return
            chunk = unique[i:i + CHUNK]
            try:
                r = requests.get(
                    f"{ALPACA_DATA_URL}/v2/stocks/bars",
                    params={
                        "symbols": ",".join(chunk),
                        "timeframe": "1Day",
                        "start": start, "end": end,
                        "limit": 10000,
                        "feed": "iex",
                    },
                    headers=headers, timeout=(5, 15),
                )
                try:
                    _shared_db().log_api_call(
                        agent='trade_logic',
                        endpoint=f'/v2/stocks/bars?symbols={len(chunk)}tickers',
                        method='GET', service='alpaca_data',
                        customer_id=_CUSTOMER_ID, status_code=r.status_code)
                except Exception:
                    pass
                if r.status_code == 200:
                    data = r.json().get("bars") or {}
                    for sym, bars in data.items():
                        # Cache with the max days requested — get_bars() slices as needed
                        self._bar_cache[(sym.upper(), days)] = bars
                    log.info(f"[PREFETCH] {len(data)} tickers cached ({len(chunk)} requested, {days}d)")
                    self._circuit_record(True)
                else:
                    log.warning(f"[PREFETCH] Alpaca returned {r.status_code}")
                    self._circuit_record(False)
            except Exception as e:
                log.warning(f"[PREFETCH] Failed: {e}")
                self._circuit_record(False)

    def _request(self, method, endpoint, **kwargs):
        # Circuit breaker — once tripped, return None immediately without
        # touching the network. The trader treats None as "Alpaca call
        # failed, degrade gracefully" which is exactly what we want when
        # Alpaca is flaky.
        if self._circuit_open:
            return None

        url = f"{self.base_url}{endpoint}"
        last_error = None
        status_code = None
        # (connect_timeout, read_timeout) — a slow connect fails fast (3s)
        # while a stalled read still gets the 15s it needs to complete a
        # partial response. Keeps the blocked-on-DNS / blocked-on-SYN cases
        # from chewing the full timeout budget.
        timeouts = (5, 15)
        for attempt in range(MAX_RETRIES):
            try:
                r = getattr(requests, method)(
                    url, headers=self.headers, timeout=timeouts, **kwargs
                )
                status_code = r.status_code
                r.raise_for_status()
                # Success — reset the circuit breaker counter and log.
                self._consecutive_failures = 0
                try:
                    _shared_db().log_api_call(
                        agent='trade_logic', endpoint=endpoint,
                        method=method.upper(), service='alpaca',
                        customer_id=_CUSTOMER_ID, status_code=status_code)
                except Exception:
                    pass
                return r.json() if r.text else {}
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
        # All retries exhausted for this call.
        self._consecutive_failures += 1
        if self._consecutive_failures >= ALPACA_CIRCUIT_BREAKER_N:
            self._circuit_open = True
            log.warning(
                f"[CIRCUIT] Alpaca circuit breaker opened after "
                f"{self._consecutive_failures} consecutive failures — "
                f"remaining Alpaca calls in this run will short-circuit"
            )
        try:
            _shared_db().log_api_call(
                agent='trade_logic', endpoint=endpoint,
                method=method.upper(), service='alpaca',
                customer_id=_CUSTOMER_ID, status_code=status_code)
        except Exception:
            pass
        log.error(f"Alpaca {method.upper()} {endpoint} failed: {last_error}")
        return None

    def get_account(self):
        return self._request("get", "/v2/account")

    def get_positions(self):
        return self._request("get", "/v2/positions") or []

    def get_position(self, ticker):
        return self._request("get", f"/v2/positions/{ticker}")

    def get_latest_quote(self, ticker):
        """Return (bid, ask, mid) or (None, None, None)."""
        r = self._request("get", f"/v2/stocks/{ticker}/quotes/latest")
        if r and "quote" in r:
            bid = float(r["quote"].get("bp", 0) or 0)
            ask = float(r["quote"].get("ap", 0) or 0)
            mid = (bid + ask) / 2 if bid and ask else (bid or ask)
            return bid, ask, mid
        return None, None, None

    def get_latest_price(self, ticker):
        _, _, mid = self.get_latest_quote(ticker)
        return mid

    def get_bars(self, ticker, days=60):
        """Fetch daily bars for ticker. Returns list of bar dicts.
        Uses cache from prefetch_bars() when available."""
        t_upper = ticker.upper()
        # Check cache — return cached bars if we have enough days
        for (cached_t, cached_d), bars in self._bar_cache.items():
            if cached_t == t_upper and cached_d >= days:
                if not bars:
                    return []  # Negative cache — ticker has no bars
                return bars[-days:] if len(bars) > days else bars
        # Check negative cache (ticker returned empty before)
        if (t_upper, 0) in self._bar_cache:
            return []
        # Short-circuit on open breaker — don't chew timeout budget on calls
        # we already know will fail. Negative-cache so we don't retry same
        # ticker within this run.
        if not self._circuit_check():
            self._bar_cache[(t_upper, 0)] = []
            return []
        # Cache miss — fetch individually. UTC for Alpaca.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        end   = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (now_utc - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        try:
            r = requests.get(
                f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
                params={"timeframe": "1Day", "start": start, "end": end,
                        "limit": days + 10, "feed": "iex"},
                headers=headers, timeout=(5, 10),
            )
            try:
                _shared_db().log_api_call(
                    agent='trade_logic', endpoint=f'/v2/stocks/{ticker}/bars',
                    method='GET', service='alpaca_data',
                    customer_id=_CUSTOMER_ID, status_code=r.status_code)
            except Exception as _e:
                log.debug(f"suppressed exception: {_e}")
            if r.status_code == 200:
                bars = r.json().get("bars") or []
                # Cache for reuse within this run (including empty = negative cache)
                self._bar_cache[(t_upper, days)] = bars
                if not bars:
                    self._bar_cache[(t_upper, 0)] = []  # Negative cache marker
                self._circuit_record(True)
                return bars
            else:
                self._circuit_record(False)
        except Exception as e:
            log.warning(f"get_bars({ticker}): {e}")
            # Negative cache on timeout/error — don't retry this ticker
            self._bar_cache[(t_upper, 0)] = []
            self._circuit_record(False)
        return []

    def get_atr(self, ticker, period=14):
        bars = self.get_bars(ticker, days=period + 10)
        if len(bars) < 2:
            log.debug(f"get_atr({ticker}): insufficient bars ({len(bars)}) — returning None")
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        result = round(sum(trs[-period:]) / min(len(trs), period), 2) if trs else None
        if result is None:
            log.debug(f"get_atr({ticker}): no true-range values computed — returning None")
        return result

    def get_sma(self, ticker, window: int, days_back: int = None) -> float | None:
        bars = self.get_bars(ticker, days=days_back or window + 10)
        closes = [b["c"] for b in bars]
        if len(closes) < window:
            return None
        return round(sum(closes[-window:]) / window, 4)

    def get_rolling_high(self, ticker, lookback: int) -> float | None:
        bars = self.get_bars(ticker, days=lookback + 5)
        if len(bars) < lookback:
            return None
        return max(b["h"] for b in bars[-lookback:])

    def get_volume_avg(self, ticker, days=30) -> int:
        bars = self.get_bars(ticker, days=days + 5)
        if not bars:
            log.debug(f"get_volume_avg({ticker}): no bars returned — returning 0")
            return 0
        vols = [b["v"] for b in bars[-days:]]
        if not vols:
            log.debug(f"get_volume_avg({ticker}): empty volume list — returning 0")
            return 0
        return int(sum(vols) / len(vols))

    def submit_order(self, ticker, qty, side, order_type="market",
                     trail_price=None, trail_percent=None):
        # ── OVERNIGHT QUEUE GATE ─────────────────────────────────────
        # Market/notional orders submitted outside market hours sit in
        # Alpaca as `new` and fill at the next open anyway — zero
        # information/timing advantage and they de-sync DB state when
        # the fill price differs from what we assumed. Instead, queue
        # the decision for pre-open re-evaluation.
        #
        # Gate applies only to unconditional market orders. Trailing
        # stops and limit orders stay on Alpaca across sessions and
        # trigger at their own condition — submitting those off-hours
        # is intentional and correct.
        if (order_type == "market"
                and not is_market_hours_utc_now()):
            return _queue_overnight_order(
                ticker=ticker, qty=qty, side=side, order_type=order_type,
            )

        if TRADING_MODE == "PAPER":
            log.info(f"[PAPER] Would {side} {qty} shares of {ticker}")
        payload = {
            "symbol": ticker, "qty": str(qty), "side": side,
            "type": order_type, "time_in_force": "day",
        }
        if order_type == "trailing_stop":
            if trail_price:    payload["trail_price"]   = str(trail_price)
            elif trail_percent:payload["trail_percent"] = str(trail_percent)
        result = self._request("post", "/v2/orders", json=payload)
        if result:
            log.info(f"Order submitted: {side} {qty} {ticker} — id={result.get('id','?')}")
        return result

    def cancel_order(self, order_id):
        return self._request("delete", f"/v2/orders/{order_id}")

    def get_position_safe(self, ticker):
        if not self._circuit_check():
            return None
        url = f"{self.base_url}/v2/positions/{ticker}"
        try:
            r = requests.get(url, headers=self.headers, timeout=(5, 15))
            try:
                _shared_db().log_api_call(
                    agent='trade_logic', endpoint=f'/v2/positions/{ticker}',
                    method='GET', service='alpaca',
                    customer_id=_CUSTOMER_ID, status_code=r.status_code)
            except Exception as _e:
                log.debug(f"suppressed exception: {_e}")
            if r.status_code == 404:
                # 404 is a valid "no such position" response, not a failure —
                # don't count it against the circuit breaker.
                self._circuit_record(True)
                return None
            r.raise_for_status()
            self._circuit_record(True)
            return r.json()
        except Exception as e:
            log.warning(f"get_position_safe({ticker}): {e}")
            self._circuit_record(False)
            return None

    def _submit_notional(self, ticker, notional, side):
        # Same overnight-queue gate as submit_order — notional orders are
        # market orders under the hood and carry the same fill-at-open
        # drift risk if submitted off-hours.
        if not is_market_hours_utc_now():
            return _queue_overnight_order(
                ticker=ticker, qty=None, side=side,
                order_type="market", notional=notional,
            )

        if TRADING_MODE == "PAPER":
            log.info(f"[PAPER] Would {side} ${notional:.2f} notional of {ticker}")
        payload = {
            "symbol": ticker, "notional": str(round(notional, 2)),
            "side": side, "type": "market", "time_in_force": "day",
        }
        result = self._request("post", "/v2/orders", json=payload)
        if result:
            log.info(f"Notional order: {side} ${notional:.2f} {ticker}")
        return result

    def get_filled_orders(self, ticker, after_date=None):
        """Fetch recently filled sell orders for a ticker."""
        params = {"status": "closed", "symbols": ticker, "direction": "desc", "limit": 10}
        if after_date:
            params["after"] = after_date
        try:
            return self._request("get", "/v2/orders", params=params) or []
        except Exception:
            return []

    def close_position(self, ticker):
        # close_position is a market-close via Alpaca's position endpoint.
        # Same off-hours drift concern as submit_order market — queue
        # instead of submitting so the fill price matches real execution.
        if not is_market_hours_utc_now():
            return _queue_overnight_order(
                ticker=ticker, qty=None, side="sell",
                order_type="market", close_position=True,
            )
        return self._request("delete", f"/v2/positions/{ticker}")


# ── GATE 1 — SYSTEM GATE ─────────────────────────────────────────────────────

def gate1_system(db, alpaca, session: str, decision_log: TradeDecisionLog) -> bool:
    """
    Hard stops. Returns True = proceed, False = halt.
    Logic: Doc 3 §1
    """
    now = datetime.now(ET)

    # Market time regime check (hourly runs — no fixed session windows)
    mtr = get_market_time_regime(now)
    decision_log.gate("1_MARKET_TIME", mtr["is_market_hours"], {
        "current_time": now.strftime("%H:%M ET"),
        "market_hours": "9:30–16:00",
        "regime": "market" if mtr["is_market_hours"] else ("premarket" if mtr["is_premarket"] else "afterhours"),
    }, "within market hours" if mtr["is_market_hours"] else "outside market hours (evaluation only)")
    # Non-fatal — agent runs 24/7, evaluates signals any time

    # Halt check redundancy removed — run()'s first-line _check_halt_state
    # already exited before this code ran. Keeping a legacy belt-and-
    # suspenders check here would only fire in the impossible case that
    # the halt state flipped between run() entry and Gate 1 (~milliseconds).
    # If we ever want that defense-in-depth, it's a single _check_halt_state()
    # call — no need for the old global-file / per-customer-setting split.

    # Portfolio drawdown limit
    # current_equity <= peak_equity * (1 - max_drawdown_pct)
    try:
        portfolio = db.get_portfolio()
        peak      = portfolio.get('peak_equity') or portfolio.get('cash', 0)
        current   = portfolio.get('cash', 0)
        drawdown  = (peak - current) / peak if peak > 0 else 0
        dd_breach = drawdown >= C.MAX_DRAWDOWN_PCT
        decision_log.gate("1_DRAWDOWN", not dd_breach, {
            "current_equity": f"${current:.2f}",
            "peak_equity":    f"${peak:.2f}",
            "drawdown_pct":   f"{drawdown*100:.2f}%",
            "limit":          f"{C.MAX_DRAWDOWN_PCT*100:.0f}%",
        }, f"drawdown {drawdown*100:.1f}% {'EXCEEDS' if dd_breach else 'within'} limit")
        if dd_breach:
            decision_log.decide("HALT", "Portfolio drawdown limit reached")
            return False
    except Exception as e:
        log.warning(f"Gate 1 drawdown check error: {e}")

    # Daily loss limit
    # realized_pnl_today <= -daily_loss_limit
    try:
        today_str = now.strftime('%Y-%m-%d')
        outcomes_today = db.get_recent_outcomes(limit=50)
        pnl_today = sum(
            o.get('pnl_dollar', 0) for o in outcomes_today
            if o.get('created_at', '').startswith(today_str)
        )
        loss_breach = pnl_today <= C.MAX_DAILY_LOSS
        decision_log.gate("1_DAILY_LOSS", not loss_breach, {
            "pnl_today":       f"${pnl_today:+.2f}",
            "daily_loss_limit":f"${C.MAX_DAILY_LOSS:.2f}",
        }, f"daily P&L {'BREACHED' if loss_breach else 'within'} limit")
        if loss_breach:
            decision_log.decide("HALT", f"Daily loss limit hit (${pnl_today:+.2f})")
            return False
    except Exception as e:
        log.warning(f"Gate 1 daily loss check error: {e}")

    # API health check
    account = alpaca.get_account()
    api_ok = account is not None
    decision_log.gate("1_API_HEALTH", api_ok, {
        "broker": "Alpaca",
        "cash":   f"${float(account.get('cash',0)):.2f}" if api_ok else "unreachable",
    }, "broker API reachable" if api_ok else "API failure — halting")
    if not api_ok:
        decision_log.decide("HALT", "Broker API unreachable")
        return False

    decision_log.gate("1_SYSTEM", True, {}, "all system gates passed")
    return True


# ── GATE 2 — BENCHMARK GATE ───────────────────────────────────────────────────

def gate2_benchmark(alpaca, decision_log: TradeDecisionLog) -> str:
    """
    Sets operating mode: DEFENSIVE / NEUTRAL / AGGRESSIVE.
    Logic: Doc 3 §2
    """
    sym = C.BENCHMARK_SYMBOL
    bars = alpaca.get_bars(sym, days=max(C.BENCHMARK_MA_LONG, 60) + 10)

    if len(bars) < C.BENCHMARK_MA_LONG:
        decision_log.gate("2_BENCHMARK", "NEUTRAL", {
            "reason": f"insufficient bars ({len(bars)}) for {sym}",
        }, "defaulting to NEUTRAL — data unavailable")
        return "NEUTRAL"

    closes = [b["c"] for b in bars]
    ma_short = sum(closes[-C.BENCHMARK_MA_SHORT:]) / C.BENCHMARK_MA_SHORT
    ma_long  = sum(closes[-C.BENCHMARK_MA_LONG:])  / C.BENCHMARK_MA_LONG

    # Rolling drawdown from peak in window
    window_closes = closes[-60:]
    peak    = max(window_closes)
    current = closes[-1]
    dd      = (peak - current) / peak if peak > 0 else 0

    # Volatility: ATR / price
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else trs[-1] if trs else 0
    bm_vol = atr / current if current > 0 else 0

    trend_up = ma_short > ma_long
    vol_ok   = bm_vol <= C.BENCHMARK_VOL_THRESHOLD

    if dd >= C.BENCHMARK_DD_THRESHOLD:
        mode = "DEFENSIVE"
    elif trend_up and vol_ok:
        mode = "AGGRESSIVE"
    else:
        mode = "NEUTRAL"

    decision_log.gate("2_BENCHMARK", mode, {
        "benchmark":       sym,
        "current_price":   f"${current:.2f}",
        "ma_short":        f"${ma_short:.2f} ({C.BENCHMARK_MA_SHORT}d)",
        "ma_long":         f"${ma_long:.2f} ({C.BENCHMARK_MA_LONG}d)",
        "trend":           "UP" if trend_up else "DOWN",
        "rolling_dd":      f"{dd*100:.2f}%",
        "dd_threshold":    f"{C.BENCHMARK_DD_THRESHOLD*100:.0f}%",
        "bm_volatility":   f"{bm_vol*100:.3f}%",
        "vol_threshold":   f"{C.BENCHMARK_VOL_THRESHOLD*100:.3f}%",
    }, f"mode={mode}: dd={dd*100:.1f}% trend={'UP' if trend_up else 'DOWN'} vol={'OK' if vol_ok else 'HIGH'}")

    return mode


# ── GATE 3 — REGIME DETECTION ────────────────────────────────────────────────

def gate3_regime(alpaca, mode: str, decision_log: TradeDecisionLog) -> RegimeState:
    """
    Classifies volatility, trend, and risk regime.
    Logic: Doc 3 §3
    """
    regime = RegimeState(mode=mode)
    sym    = C.BENCHMARK_SYMBOL
    bars   = alpaca.get_bars(sym, days=60)

    if len(bars) < 20:
        decision_log.gate("3_REGIME", "NEUTRAL/NORMAL/RISK_ON", {
            "reason": "insufficient data",
        }, "defaulting to neutral regime")
        return regime

    closes = [b["c"] for b in bars]
    ma_s   = sum(closes[-C.BENCHMARK_MA_SHORT:]) / C.BENCHMARK_MA_SHORT
    ma_l   = sum(closes[-C.BENCHMARK_MA_LONG:]) / C.BENCHMARK_MA_LONG if len(closes) >= C.BENCHMARK_MA_LONG else ma_s

    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else trs[-1] if trs else 0
    vol = atr / closes[-1] if closes[-1] > 0 else 0

    # Volatility regime
    # IF vol > threshold → HIGH; ELSE NORMAL
    # TODO: DATA_DEPENDENCY — replace with VIX when data feed available
    if vol > C.VOL_HIGH_THRESHOLD:
        regime.volatility = "HIGH"
    elif vol < C.VOL_HIGH_THRESHOLD * 0.6:
        regime.volatility = "LOW"
    else:
        regime.volatility = "NORMAL"

    # Trend regime
    # IF abs(ma_short - ma_long) < flat_threshold → SIDEWAYS
    separation_pct = abs(ma_s - ma_l) / ma_l if ma_l > 0 else 0
    if separation_pct < C.MA_FLAT_THRESHOLD:
        regime.trend = "SIDEWAYS"
    elif ma_s > ma_l:
        regime.trend = "BULL"
    else:
        regime.trend = "BEAR"

    # Risk posture: use TLT as bond proxy
    # TODO: DATA_DEPENDENCY — add credit spread data for fuller risk-off detection
    tlt_bars = alpaca.get_bars("TLT", days=10)
    tlt_trend = "UP" if (len(tlt_bars) >= 5 and
                         tlt_bars[-1]["c"] > tlt_bars[-5]["c"]) else "FLAT_OR_DOWN"
    risk_off = (regime.trend in ("BEAR", "SIDEWAYS") and tlt_trend == "UP")
    regime.risk_posture = "RISK_OFF" if risk_off else "RISK_ON"

    decision_log.gate("3_REGIME", f"vol={regime.volatility} trend={regime.trend} posture={regime.risk_posture}", {
        "volatility_atr_pct": f"{vol*100:.3f}%",
        "vol_threshold":      f"{C.VOL_HIGH_THRESHOLD*100:.3f}%",
        "ma_separation_pct":  f"{separation_pct*100:.3f}%",
        "flat_threshold":     f"{C.MA_FLAT_THRESHOLD*100:.3f}%",
        "tlt_trend":          tlt_trend,
        "risk_posture":       regime.risk_posture,
        # TODO: DATA_DEPENDENCY — VIX, credit spreads not yet included
    }, f"volatility={regime.volatility} trend={regime.trend} risk={regime.risk_posture}")

    return regime


# ── GATE 4 — TRADE ELIGIBILITY ────────────────────────────────────────────────

def gate4_eligibility(signal: dict, positions: list, alpaca,
                      decision_log: TradeDecisionLog) -> bool:
    """
    Filter out signals that fail liquidity, spread, event, or correlation checks.
    Logic: Doc 3 §4
    """
    ticker = signal.get('ticker') or ''
    if not ticker:
        decision_log.gate("4_ELIGIBILITY", "SKIP", {"reason": "missing ticker in signal"}, "no ticker")
        return False
    dlog = TradeDecisionLog(decision_log.session, ticker, signal.get('id'))

    # Ticker dedup — Phase 5 post-audit fix. Previously absent, which
    # is the root cause of the AAPL runaway: nothing in Gate 4 prevented
    # the trader from re-entering a ticker it already held. Checked
    # first because it's the cheapest reject (pure in-memory set lookup,
    # no API calls).
    _held_tickers = {p['ticker'] for p in positions
                     if (p.get('status') or 'OPEN') == 'OPEN'}
    ticker_dedup_ok = ticker not in _held_tickers
    decision_log.gate("4_TICKER_DEDUP", ticker_dedup_ok, {
        "ticker":        ticker,
        "open_tickers":  ",".join(sorted(_held_tickers)) if _held_tickers else "none",
    }, "ticker not in open positions" if ticker_dedup_ok
       else f"SKIP — {ticker} already held in an OPEN position")
    if not ticker_dedup_ok:
        return False

    # Liquidity: avg_volume < volume_threshold
    avg_vol = alpaca.get_volume_avg(ticker, days=30)
    liq_ok  = avg_vol >= C.MIN_AVG_VOLUME
    decision_log.gate("4_LIQUIDITY", liq_ok, {
        "ticker":         ticker,
        "avg_volume_30d": f"{avg_vol:,}",
        "min_required":   f"{C.MIN_AVG_VOLUME:,}",
    }, "liquidity OK" if liq_ok else f"SKIP — volume {avg_vol:,} below {C.MIN_AVG_VOLUME:,}")
    if not liq_ok:
        return False

    # Spread: (ask - bid) / mid > spread_threshold
    bid, ask, mid = alpaca.get_latest_quote(ticker)
    if bid and ask and mid:
        spread_pct = (ask - bid) / mid
        spread_ok  = spread_pct <= C.MAX_SPREAD_PCT
        decision_log.gate("4_SPREAD", spread_ok, {
            "bid":        f"${bid:.4f}",
            "ask":        f"${ask:.4f}",
            "spread_pct": f"{spread_pct*100:.4f}%",
            "max_allowed":f"{C.MAX_SPREAD_PCT*100:.3f}%",
        }, "spread OK" if spread_ok else f"SKIP — spread {spread_pct*100:.3f}% too wide")
        if not spread_ok:
            return False
    else:
        decision_log.gate("4_SPREAD", "SKIP_CHECK", {"reason": "no quote data"}, "spread check skipped — no quote")

    # Event risk — Phase 3a (TRADER_RESTRUCTURE_PLAN) partial implementation.
    # Reads news_flags from the master DB for this ticker. Any *fresh*
    # flag in an event-risk category blocks entry, because entering
    # right before/after an earnings, a regulatory probe, or a legal
    # action puts the position at the whim of unknown news-driven
    # volatility we shouldn't absorb.
    #
    # Phase 2 currently writes ONLY 'catalyst' category flags (positive
    # score), so in practice this check is a no-op today. It activates
    # when the news agent starts classifying into specific categories
    # in a future patch — the integration is here so that refinement
    # lands in one place.
    #
    # FOMC/CPI scheduled-events calendar is still a DATA_DEPENDENCY
    # TODO; this check covers the news-flagged portion only.
    event_risk_categories = {
        'earnings_miss', 'earnings_raise',   # recent earnings = volatility
        'regulatory_probe', 'litigation',
        'management_change', 'guidance_cut', 'guidance_raise',
    }
    try:
        _flags = _shared_db().get_fresh_news_flags_for_ticker(ticker)
        _risk_hits = [f for f in _flags if f.get('category') in event_risk_categories]
    except Exception as _e:
        log.debug(f"news_flags read failed for {ticker} at G4: {_e}")
        _flags = []
        _risk_hits = []

    # Phase 5.a — scheduled event calendar (earnings + macro). Blocks
    # entries whose ticker has a known earnings release, or where an
    # FOMC/CPI-class macro event, falls within EVENT_CALENDAR_WINDOW_DAYS
    # business days. News-flag hits above are retrospective ("just
    # happened"); this is prospective ("about to happen").
    _calendar = {'blocked': False, 'reasons': [], 'next_earnings': None, 'macro_events': []}
    try:
        from retail_event_calendar import check_event_risk  # noqa: E402
        _calendar = check_event_risk(
            _shared_db(), ticker,
            within_biz_days=getattr(C, 'EVENT_CALENDAR_WINDOW_DAYS', 2),
        )
    except Exception as _e:
        log.debug(f"event_calendar check failed for {ticker} at G4: {_e}")

    event_risk_ok = (len(_risk_hits) == 0) and (not _calendar['blocked'])
    decision_log.gate("4_EVENT_RISK", event_risk_ok, {
        "ticker":              ticker,
        "fresh_flags":         len(_flags),
        "event_risk_hits":     len(_risk_hits),
        "blocking_categories": ",".join(sorted({f['category'] for f in _risk_hits})) or "none",
        "next_earnings":       _calendar.get('next_earnings') or "none",
        "macro_within_window": ",".join(f"{m['event_type']}@{m['event_date']}"
                                        for m in _calendar.get('macro_events', [])) or "none",
        "calendar_reasons":    "; ".join(_calendar.get('reasons', [])) or "none",
    }, ("event risk OK — no flags and no scheduled events" if event_risk_ok
        else "SKIP — " + "; ".join(
            ([f"{len(_risk_hits)} fresh flag(s): "
              + ",".join(sorted({f['category'] for f in _risk_hits}))]
             if _risk_hits else [])
            + _calendar.get('reasons', [])
        )))
    if not event_risk_ok:
        return False

    # Correlated exposure: corr(new_trade, portfolio) > corr_limit
    # Simplified: check if sector already highly concentrated
    sig_sector = signal.get('sector', '')
    sector_positions = [p for p in positions if p.get('sector') == sig_sector]
    sector_count_ok  = len(sector_positions) < 3  # simple proxy for correlation
    decision_log.gate("4_CORRELATION", sector_count_ok, {
        "ticker_sector":        sig_sector,
        "positions_in_sector":  len(sector_positions),
        "max_sector_positions": 3,
        # TODO: compute actual pairwise correlation when multi-asset data available
    }, "correlation OK" if sector_count_ok else f"SKIP — sector {sig_sector} already concentrated")
    if not sector_count_ok:
        return False

    # Phase 5.b — cooling off. Block re-entry for COOLING_OFF_HOURS after
    # a loss-closed position. Prevents stop → re-buy → stop chop on the
    # same ticker. Wins don't set cooling_off; only losses register.
    _cool = None
    try:
        _cool = _shared_db().is_cooling_off(ticker)
    except Exception as _e:
        log.debug(f"cooling_off read failed for {ticker} at G4: {_e}")
    cool_ok = _cool is None
    decision_log.gate("4_COOLING_OFF", cool_ok, {
        "ticker":      ticker,
        "cool_until":  (_cool or {}).get('cool_until') or "n/a",
        "reason":      (_cool or {}).get('reason') or "n/a",
        "pnl_pct":     f"{(_cool or {}).get('pnl_pct'):+.2f}%" if (_cool and _cool.get('pnl_pct') is not None) else "n/a",
    }, "cooling-off clear" if cool_ok
       else f"SKIP — {ticker} cooling off until {_cool['cool_until']} ({_cool.get('reason')})")
    if not cool_ok:
        return False

    return True


# ── GATE 5 — SIGNAL EVALUATION ───────────────────────────────────────────────

def gate5_signal_score(signal: dict, positions: list, alpaca,
                       decision_log: TradeDecisionLog) -> float:
    """
    Compute composite confidence score. Returns score in [0, 1].
    Logic: Doc 3 §5
    """
    ticker = signal.get('ticker') or ''
    if not ticker:
        decision_log.gate("5_SIGNAL_SCORE", "SKIP", {"reason": "missing ticker in signal"}, "no ticker")
        return 0.0

    # Component scores
    tier_score   = max(0.0, 1.0 - (int(signal.get('source_tier', 2) or 2) - 1) * 0.3)
    pol_weight   = float(signal.get('politician_weight') or 0.5)
    stale_score  = staleness_to_score(signal.get('staleness', 'Fresh'))
    interr_score = interrogation_to_score(signal.get('interrogation_status', 'UNVALIDATED'))
    conf_score   = confidence_to_score(signal.get('confidence', 'MEDIUM'))

    # Sentiment — prefer the new per-signal sentiment stamp if present,
    # else fall back to parsing corroboration_note (legacy path).
    # stamp_signals_sentiment() writes signal.sentiment_score in [0.10, 0.85].
    stamped_sent = signal.get('sentiment_score')
    if stamped_sent is not None:
        try:
            sentiment_score = max(0.0, min(1.0, float(stamped_sent)))
        except (ValueError, TypeError):
            sentiment_score = 0.55
    else:
        corr_note = signal.get('corroboration_note', '') or ''
        if '[PULSE_POSITIVE' in corr_note:
            sentiment_score = 0.90
        elif '[PULSE' in corr_note or '[PULSE_NEGATIVE' in corr_note:
            sentiment_score = 0.25
        else:
            sentiment_score = 0.55  # neutral — no pulse data

    # Weighted composite
    W = C.SIGNAL_WEIGHTS
    final_score = (
        W['source_tier']       * tier_score   +
        W['politician_weight'] * pol_weight    +
        W['staleness']         * stale_score   +
        W['interrogation']     * interr_score  +
        W['sentiment']         * sentiment_score
    )
    final_score = round(min(max(final_score, 0.0), 1.0), 4)

    # Benchmark-relative strength
    # asset_return - SPX_return < 0 over rolling window → penalise
    asset_bars = alpaca.get_bars(ticker, days=20)
    bm_bars    = alpaca.get_bars(C.BENCHMARK_SYMBOL, days=20)
    if len(asset_bars) >= 5 and len(bm_bars) >= 5:
        asset_ret = (asset_bars[-1]["c"] - asset_bars[-5]["c"]) / asset_bars[-5]["c"]
        bm_ret    = (bm_bars[-1]["c"]    - bm_bars[-5]["c"])    / bm_bars[-5]["c"]
        rel_str   = asset_ret - bm_ret
        if rel_str < -0.02:
            final_score = round(final_score * 0.85, 4)
    else:
        rel_str = None

    # Sector screener boost — if this ticker was screened and scored well, nudge the score
    screening_adj = 0.0
    screening_info = "not screened"
    try:
        scr = _shared_db().get_screening_score(ticker)
        if scr:
            cs = scr.get('combined_score') or 0.5
            cong = scr.get('congressional_flag', 'none')
            if cs >= 0.7:
                screening_adj = 0.06
                screening_info = f"strong ({cs:.2f}) +6%"
            elif cs >= 0.55:
                screening_adj = 0.03
                screening_info = f"moderate ({cs:.2f}) +3%"
            elif cs < 0.3:
                screening_adj = -0.03
                screening_info = f"weak ({cs:.2f}) -3%"
            else:
                screening_info = f"neutral ({cs:.2f})"
            if cong == 'recent_buy':
                screening_adj += 0.04
                screening_info += " +congress_buy"
            elif cong == 'recent_sell':
                screening_adj -= 0.04
                screening_info += " -congress_sell"
            final_score = round(min(max(final_score + screening_adj, 0.0), 1.0), 4)
    except Exception as _e:
        log.debug(f"Screening lookup failed for {ticker}: {_e}")

    # Per-agent stamp bonus — the screener writes screener_score
    # (0.0-1.0) to every in-flight signal:
    #   - Top-N candidates get their actual momentum score
    #   - Everyone else gets a sector-baseline derived from the sector
    #     ETF's 5yr return
    # We convert that score into a centered ±3% Gate 5 nudge so strong
    # sectors/candidates get a boost and weak ones get a penalty, with
    # a null-safe fallback to the legacy +0.02 boolean bonus if the
    # column hasn't been populated yet (first run after migration).
    scr_score = signal.get('screener_score')
    if scr_score is not None:
        try:
            # (score - 0.5) × 0.06 →  range is roughly -0.03 .. +0.03
            # given the baseline table and momentum score distribution
            screener_stamp_adj = round((float(scr_score) - 0.5) * 0.06, 4)
        except Exception:
            screener_stamp_adj = 0.0
        final_score = round(max(0.0, min(final_score + screener_stamp_adj, 1.0)), 4)
    elif signal.get('screener_evaluated_at'):
        # Legacy path — signals stamped before the screener_score
        # column landed. Preserves old +0.02 boolean bonus so we don't
        # silently drop Gate 5 output on the migration boundary.
        final_score = round(min(final_score + 0.02, 1.0), 4)

    # News flags modifier — Phase 3a of TRADER_RESTRUCTURE_PLAN.
    # Aggregates news_flags.score across all fresh flags for this ticker
    # that are NOT in event-risk categories (those are handled by Gate 4).
    # Result is clamped to ±0.2 and added to the composite as a mild
    # adjustment. Positive news = small boost, negative = small penalty.
    # Severe negative (score < -0.7) blocks later at Gate 5.5 VETO.
    #
    # Phase 2 only populates 'catalyst' category (positive scored), so in
    # practice this currently only adds upside. Negative-category writes
    # land in a future patch.
    _excluded_for_modifier = {
        'earnings_miss', 'earnings_raise', 'regulatory_probe',
        'litigation', 'management_change', 'guidance_cut', 'guidance_raise',
    }
    news_modifier = 0.0
    news_modifier_info = "no flags"
    try:
        _flags = _shared_db().get_fresh_news_flags_for_ticker(ticker)
        _mod_flags = [f for f in _flags if f.get('category') not in _excluded_for_modifier]
        if _mod_flags:
            raw = sum(float(f.get('score') or 0.0) for f in _mod_flags)
            news_modifier = max(-0.2, min(0.2, round(raw, 4)))
            news_modifier_info = (f"{len(_mod_flags)} flag(s), "
                                  f"raw {raw:+.3f}, clamped {news_modifier:+.3f}")
    except Exception as _e:
        log.debug(f"news_flags read failed for {ticker} at G5: {_e}")
    final_score = round(max(0.0, min(final_score + news_modifier, 1.0)), 4)

    # (Removed 2026-04-24) Window-proximity scoring bonus — used to grant
    # up to +0.04 to composite score based on how deep in the minor band
    # the live price sat. Removed with window_calculator. The anti-chase
    # signal previously encoded here is now enforced with stricter intent
    # by Gate 6's chase caps (MOMENTUM/BREAKOUT/MEANREV), which block
    # extended entries outright rather than down-weighting them.

    passes = final_score >= C.MIN_CONFIDENCE_SCORE

    decision_log.gate("5_SIGNAL_SCORE", f"{final_score:.4f}", {
        "ticker":             ticker,
        "tier_score":         f"{tier_score:.2f} × {W['source_tier']}",
        "politician_weight":  f"{pol_weight:.2f} × {W['politician_weight']}",
        "staleness_score":    f"{stale_score:.2f} × {W['staleness']}",
        "interrogation_score":f"{interr_score:.2f} × {W['interrogation']}",
        "sentiment_score":    f"{sentiment_score:.2f} × {W['sentiment']}",
        "screening_adj":      f"{screening_adj:+.2f} ({screening_info})",
        "news_flags_mod":     f"{news_modifier:+.3f} ({news_modifier_info})",
        "composite_score":    f"{final_score:.4f}",
        "rel_strength_5d":    f"{rel_str*100:.2f}%" if rel_str is not None else "N/A",
        "threshold":          f"{C.MIN_CONFIDENCE_SCORE:.2f}",
        "result":             "PASS" if passes else "SKIP",
    }, f"score {final_score:.4f} {'≥' if passes else '<'} threshold {C.MIN_CONFIDENCE_SCORE}")

    return final_score if passes else 0.0


# ── GATE 5.5 — NEWS VETO ─────────────────────────────────────────────────────

# Phase 3a of TRADER_RESTRUCTURE_PLAN. Gate 5.5 is the safety net — any
# ticker with a severe negative news_flag (score < SEVERITY_VETO_THRESHOLD,
# default -0.7) gets blocked regardless of composite score. Protects against
# entering positions where negative news materially outweighs the bullish
# signal that drove composite past threshold.
#
# Currently latent: Phase 2's news_agent writes positive-scored 'catalyst'
# flags only, so this gate doesn't fire today. It activates when future
# news-classification writes negative-scored flags (litigation=-0.9,
# regulatory_probe=-0.8, etc.). Integration lands now so that refinement
# lands in one place later.

NEWS_VETO_THRESHOLD = -0.7


def gate5_5_news_veto(signal: dict, decision_log: TradeDecisionLog) -> bool:
    """Return True if the signal passes (no severe negative news_flag).
    False means veto — reject the signal regardless of composite score."""
    ticker = signal.get('ticker') or ''
    if not ticker:
        decision_log.gate("5_5_NEWS_VETO", "SKIP", {"reason": "missing ticker in signal"}, "no ticker")
        return False
    try:
        flags = _shared_db().get_fresh_news_flags_for_ticker(ticker)
    except Exception as e:
        log.debug(f"news_flags read failed for {ticker} at G5.5: {e}")
        flags = []
    severe = [f for f in flags if float(f.get('score') or 0.0) < NEWS_VETO_THRESHOLD]
    veto_ok = len(severe) == 0
    decision_log.gate("5.5_NEWS_VETO", veto_ok, {
        "ticker":       ticker,
        "fresh_flags":  len(flags),
        "severe_count": len(severe),
        "threshold":    f"< {NEWS_VETO_THRESHOLD}",
        "worst_score":  f"{min([float(f.get('score') or 0.0) for f in flags], default=0.0):.3f}"
                        if flags else "n/a",
        "blocking_categories": ",".join(sorted({f['category'] for f in severe})) or "none",
    }, ("veto clear — no severe negative flags" if veto_ok
        else f"VETO — {len(severe)} flag(s) below {NEWS_VETO_THRESHOLD}: "
             + ",".join(sorted({f['category'] for f in severe}))))
    return veto_ok


# ── GATE 6 — ENTRY DECISION ──────────────────────────────────────────────────

def gate6_entry(signal: dict, score: float, regime: RegimeState, alpaca,
                decision_log: TradeDecisionLog) -> dict | None:
    """
    Select entry type (momentum / mean-reversion / breakout / pullback).
    Returns candidate dict or None.
    Logic: Doc 3 §6
    """
    ticker  = signal.get('ticker') or ''
    if not ticker:
        decision_log.gate("6_ENTRY", "SKIP", {"reason": "missing ticker in signal"}, "no ticker")
        return None
    bars    = alpaca.get_bars(ticker, days=max(C.BREAKOUT_LOOKBACK, 30) + 10)
    if len(bars) < 10:
        decision_log.gate("6_ENTRY", "SKIP", {"reason": "insufficient price data"}, "no price data")
        return None

    closes  = [b["c"] for b in bars]
    current = closes[-1]
    ma20    = sum(closes[-20:]) / 20 if len(closes) >= 20 else current
    roc     = (current - closes[-6]) / closes[-6] if len(closes) >= 6 else 0

    candidates = []
    # Collect chase-rejections so we can log aggregate context on WATCH exit —
    # helps the post-hoc audit see which caps are firing and how often.
    rejected_chase = []

    # Momentum: price > MA AND ROC > threshold
    # Disabled in BEAR regime
    # Anchor: MA20. Chase cap rejects entries > MAX_MOMENTUM_CHASE_PCT above anchor.
    if regime.trend != "BEAR":
        momentum_ok = current > ma20 and roc >= C.MOMENTUM_ROC_THRESHOLD
        if momentum_ok:
            chase_pct = (current - ma20) / ma20 if ma20 > 0 else 0
            if chase_pct <= C.MAX_MOMENTUM_CHASE_PCT:
                candidates.append({
                    "type": "MOMENTUM", "score": score * 1.0,
                    "anchor_type": "MA20", "anchor_price": ma20, "chase_pct": chase_pct,
                    "detail": f"price ${current:.2f} > MA20 ${ma20:.2f} (chase {chase_pct*100:.2f}%), ROC {roc*100:.2f}%",
                })
            else:
                rejected_chase.append(f"MOMENTUM: price ${current:.2f} is {chase_pct*100:.2f}% above MA20 ${ma20:.2f} "
                                      f"(cap {C.MAX_MOMENTUM_CHASE_PCT*100:.1f}%)")

    # Mean-reversion: z-score(price, mean) < -threshold
    # Only in SIDEWAYS regime
    # Anchor: 20-day rolling mean. z-score already anti-chase; cap is belt-and-suspenders.
    if regime.trend == "SIDEWAYS" and len(closes) >= 20:
        mean = sum(closes[-20:]) / 20
        std  = (sum((c - mean)**2 for c in closes[-20:]) / 20) ** 0.5
        z    = (current - mean) / std if std > 0 else 0
        if z <= C.MEAN_REV_ZSCORE:
            chase_pct = (current - mean) / mean if mean > 0 else 0
            # Mean-rev usually enters BELOW anchor (z < 0 → price < mean → chase_pct < 0).
            # Only reject if somehow above mean by >cap (unusual but possible if z
            # threshold is relaxed). Normal path: chase_pct is negative, cap trivially passes.
            if chase_pct <= C.MAX_MEANREV_CHASE_PCT:
                candidates.append({
                    "type": "MEAN_REVERSION", "score": score * 0.9,
                    "anchor_type": "MEAN20", "anchor_price": mean, "chase_pct": chase_pct,
                    "detail": f"z-score {z:.2f} ≤ threshold {C.MEAN_REV_ZSCORE}, anchor mean ${mean:.2f}",
                })
            else:
                rejected_chase.append(f"MEAN_REVERSION: price ${current:.2f} is {chase_pct*100:.2f}% above mean ${mean:.2f} "
                                      f"(cap {C.MAX_MEANREV_CHASE_PCT*100:.1f}%)")

    # Breakout: price > rolling N-period high
    # Disabled in SIDEWAYS regime
    # Anchor: N-day rolling high (the breakout level). Chase cap rejects breakouts
    # that have already run > MAX_BREAKOUT_CHASE_PCT above the level — biggest
    # historical chase risk on this path.
    if regime.trend != "SIDEWAYS" and len(bars) >= C.BREAKOUT_LOOKBACK:
        rolling_high = max(b["h"] for b in bars[-(C.BREAKOUT_LOOKBACK + 1):-1])
        if current > rolling_high:
            chase_pct = (current - rolling_high) / rolling_high if rolling_high > 0 else 0
            if chase_pct <= C.MAX_BREAKOUT_CHASE_PCT:
                candidates.append({
                    "type": "BREAKOUT", "score": score * 0.95,
                    "anchor_type": f"HIGH_{C.BREAKOUT_LOOKBACK}D", "anchor_price": rolling_high, "chase_pct": chase_pct,
                    "detail": f"price ${current:.2f} > {C.BREAKOUT_LOOKBACK}d high ${rolling_high:.2f} (chase {chase_pct*100:.2f}%)",
                })
            else:
                rejected_chase.append(f"BREAKOUT: price ${current:.2f} is {chase_pct*100:.2f}% above {C.BREAKOUT_LOOKBACK}d high ${rolling_high:.2f} "
                                      f"(cap {C.MAX_BREAKOUT_CHASE_PCT*100:.1f}%)")

    # Pullback: retraced X% within uptrend
    # Anchor: recent 10-day high. Entry BELOW anchor by design (retrace_pct > 0).
    # No separate chase cap — the retrace gate already enforces anti-chase.
    if regime.trend == "BULL" and len(closes) >= 10:
        recent_high = max(closes[-10:])
        retrace     = (recent_high - current) / recent_high if recent_high > 0 else 0
        if 0 < retrace <= C.PULLBACK_RETRACE_PCT and current > ma20:
            # chase_pct negative by construction (we're below recent_high)
            chase_pct = (current - recent_high) / recent_high if recent_high > 0 else 0
            candidates.append({
                "type": "PULLBACK", "score": score * 0.92,
                "anchor_type": "HIGH_10D", "anchor_price": recent_high, "chase_pct": chase_pct,
                "detail": f"retraced {retrace*100:.2f}% from ${recent_high:.2f} in uptrend",
            })

    if not candidates:
        # Distinguish "no entry condition met" from "blocked by chase cap" —
        # the latter means the signal was classifiable but price already ran.
        watch_reason = "no entry condition met"
        if rejected_chase:
            watch_reason = "blocked by chase cap (price extended from anchor)"
        decision_log.gate("6_ENTRY", "WATCH", {
            "ticker":         ticker,
            "price":          f"${current:.2f}",
            "ma20":           f"${ma20:.2f}",
            "roc_5d":         f"{roc*100:.2f}%",
            "regime":         f"{regime.trend}/{regime.volatility}",
            "reason":         watch_reason,
            "chase_rejected": "; ".join(rejected_chase) if rejected_chase else "—",
        }, f"WATCH — {watch_reason}")
        return None

    best = max(candidates, key=lambda x: x["score"])
    decision_log.gate("6_ENTRY", best["type"], {
        "ticker":           ticker,
        "entry_type":       best["type"],
        "entry_score":      f"{best['score']:.4f}",
        "regime":           f"{regime.trend}/{regime.volatility}",
        "candidates_found": len(candidates),
        "anchor_type":      best["anchor_type"],
        "anchor_price":     f"${best['anchor_price']:.2f}",
        "chase_pct":        f"{best['chase_pct']*100:+.2f}%",
        "detail":           best["detail"],
    }, f"entry={best['type']} score={best['score']:.4f} chase={best['chase_pct']*100:+.2f}% vs {best['anchor_type']}")

    # Return anchor metadata so downstream persist can stamp it on the position row.
    return {
        "ticker":       ticker,
        "type":         best["type"],
        "score":        best["score"],
        "price":        current,
        "anchor_type":  best["anchor_type"],
        "anchor_price": best["anchor_price"],
        "chase_pct":    best["chase_pct"],
    }


# ── GATE 7 — POSITION SIZING ─────────────────────────────────────────────────

def gate7_sizing(candidate: dict, regime: RegimeState, portfolio: dict,
                 positions: list, atr: float, decision_log: TradeDecisionLog,
                 db=None) -> float:
    """
    Compute final position size in shares.
    Logic: Doc 3 §7 + AUTO/USER tagging (Model B sizing + Model C cash guard).

    Sizing math runs off TOTAL account equity (Model B) so risk-per-trade
    stays consistent regardless of whether the user has large USER-managed
    positions taking up cash. Final size is then capped at available cash
    (Model C) so orders don't fail at submission. If the cash cap shrinks
    the order below 70% of intended, we SKIP this trade entirely — a
    partially-sized order at sub-intended risk is worse than waiting a
    cycle.

    Returns 0 as a sentinel for "skip due to insufficient cash after USER
    allocation." Callers that execute buys must guard with `if size > 0`.
    """
    price = candidate["price"]
    cash  = portfolio.get("cash", 0)

    # Model B: size off total account equity (Alpaca ground truth, cached
    # to _ALPACA_EQUITY setting by GATE 0 earlier this cycle). Falls back
    # to cash if equity unavailable — which preserves the pre-feature
    # behavior for customers who haven't hit GATE 0 yet.
    total_equity = 0.0
    if db is not None:
        try:
            total_equity = float(db.get_setting('_ALPACA_EQUITY') or 0)
        except (TypeError, ValueError):
            total_equity = 0.0
    if total_equity <= 0:
        total_equity = cash
    equity = total_equity
    tier   = get_portfolio_tier(equity)

    # Drawdown-based scaling
    peak     = portfolio.get("peak_equity") or equity
    drawdown = (peak - equity) / peak if peak > 0 else 0

    # Base size: risk_per_trade / stop_distance
    stop_dist = atr * C.ATR_STOP_MULTIPLIER if atr else price * 0.02
    base_risk  = equity * C.BASE_RISK_PER_TRADE
    base_size  = base_risk / stop_dist if stop_dist > 0 else 0

    # Volatility adjustment: size *= target_vol / asset_vol
    asset_vol = atr / price if (atr and price) else C.TARGET_VOLATILITY
    vol_adj   = C.TARGET_VOLATILITY / asset_vol if asset_vol > 0 else 1.0
    size      = base_size * vol_adj

    # Mode adjustment
    if regime.mode == "DEFENSIVE":
        size *= C.DEFENSIVE_SIZE_FACTOR
    elif regime.mode == "AGGRESSIVE":
        size *= C.AGGRESSIVE_SIZE_FACTOR

    # Drawdown scaling: size *= (1 - current_drawdown_pct)
    size *= max(0.1, 1.0 - drawdown)

    # Max cap: size <= max_position_pct * TOTAL equity
    max_shares = (equity * C.MAX_POSITION_PCT) / price if price > 0 else 0
    size       = min(size, max_shares)

    # Hard dollar cap: MAX_TRADE_USD overrides all sizing if set
    if C.MAX_TRADE_USD > 0 and price > 0:
        max_by_usd = C.MAX_TRADE_USD / price
        size = min(size, max_by_usd)

    intended = size  # shares we wanted before the cash guard

    # Model C guard: cap at available cash. If cash has been consumed by
    # USER-managed positions (or just by other auto positions), we may
    # not be able to execute the intended size. Skip (not partial-fill)
    # if the constraint cuts below 70% of intent — a heavily under-sized
    # trade has worse risk math than waiting.
    if price > 0 and cash > 0:
        max_by_cash = cash / price
        size = min(size, max_by_cash)
    else:
        size = 0

    if intended > 0 and size < intended * 0.70:
        # Skip — cash is too tight after USER/other-auto allocation.
        decision_log.gate("7_SIZING", "SKIP_INSUFFICIENT_CASH_AFTER_MANUAL", {
            "intended_shares":  f"{intended:.4f}",
            "intended_dollars": f"${intended * price:.2f}",
            "cash_cap_shares":  f"{size:.4f}",
            "available_cash":   f"${cash:.2f}",
            "total_equity":     f"${equity:.2f}",
            "equity_minus_cash":f"${equity - cash:.2f} (in positions)",
            "price":            f"${price:.2f}",
        }, f"SKIP — would size {intended:.4f}sh but cash caps at {size:.4f}sh "
           f"(<70% of intent); insufficient cash after manual/auto allocation")
        return 0

    size = round(max(size, 0.0001), 4)
    dollar_value = size * price

    decision_log.gate("7_SIZING", f"{size:.4f} shares (${dollar_value:.2f})", {
        "total_equity":    f"${equity:.2f}",
        "available_cash":  f"${cash:.2f}",
        "tier":            tier["label"],
        "atr":             f"${atr:.2f}" if atr else "estimated",
        "stop_distance":   f"${stop_dist:.2f}",
        "base_risk":       f"${base_risk:.2f} ({C.BASE_RISK_PER_TRADE*100:.1f}%)",
        "base_size":       f"{base_size:.4f}",
        "vol_adjustment":  f"×{vol_adj:.3f}",
        "mode_adjustment": f"×{C.DEFENSIVE_SIZE_FACTOR if regime.mode=='DEFENSIVE' else C.AGGRESSIVE_SIZE_FACTOR if regime.mode=='AGGRESSIVE' else 1.0:.2f}",
        "drawdown_scale":  f"×{1.0-drawdown:.3f} (dd={drawdown*100:.1f}%)",
        "max_cap_equity":  f"{max_shares:.4f} shares (Model B)",
        "cash_cap":        f"{cash/price:.4f} shares (Model C)" if price > 0 else "n/a",
        "usd_cap":         f"${C.MAX_TRADE_USD:.2f}" if C.MAX_TRADE_USD > 0 else "none",
        "final_size":      f"{size:.4f} shares @ ${price:.2f}",
    }, f"{size:.4f} shares × ${price:.2f} = ${dollar_value:.2f}")

    return size


# ── GATE 8 — RISK SETUP ──────────────────────────────────────────────────────

def gate8_risk(candidate: dict, atr: float, session: str,
               decision_log: TradeDecisionLog) -> dict:
    """
    Set stop loss, profit target, trailing stop.
    Logic: Doc 3 §8
    """
    price    = candidate["price"]
    stop_d   = atr * C.ATR_STOP_MULTIPLIER if atr else price * 0.02
    stop_lvl = round(price - stop_d, 4)
    target   = round(price + stop_d * C.PROFIT_TARGET_MULTIPLE, 4)
    trail    = round(atr * C.ATR_TRAIL_MULTIPLIER if atr else price * 0.02, 4)

    # Overnight risk: flag if after 4pm ET (position held overnight)
    overnight_flag = get_market_time_regime(datetime.now(ET))['is_overnight']

    # Gap risk: use ATR as proxy for gap std
    gap_risk = atr and (atr / price) > 0.03  # >3% ATR/price = elevated gap risk

    risk = {
        "stop_loss":    stop_lvl,
        "profit_target":target,
        "trail_stop":   trail,
        "stop_distance":round(stop_d, 4),
        "overnight":    overnight_flag,
        "gap_risk":     bool(gap_risk),
    }

    decision_log.gate("8_RISK_SETUP", f"stop=${stop_lvl} target=${target} trail=${trail}", {
        "entry_price":    f"${price:.4f}",
        "atr":            f"${atr:.4f}" if atr else "N/A",
        "stop_loss":      f"${stop_lvl:.4f} (−${stop_d:.4f})",
        "profit_target":  f"${target:.4f} (+${stop_d*C.PROFIT_TARGET_MULTIPLE:.4f})",
        "trailing_stop":  f"${trail:.4f}",
        "r_r_ratio":      f"1:{C.PROFIT_TARGET_MULTIPLE:.1f}",
        "overnight_flag": str(overnight_flag),
        "gap_risk":       str(bool(gap_risk)),
    }, "risk parameters set")

    if overnight_flag:
        decision_log.note("Close session — position held overnight. Review position sizing.")
    if gap_risk:
        decision_log.note(f"Gap risk elevated (ATR/price = {(atr/price)*100:.1f}%). Stops set wider.")

    return risk


# ── GATE 11 — PORTFOLIO CONTROLS ─────────────────────────────────────────────

def gate11_portfolio(positions: list, portfolio: dict, signal: dict,
                     size: float, alpaca, decision_log: TradeDecisionLog,
                     db=None) -> bool:
    """
    Enforce portfolio-wide limits before allowing new entry.
    Logic: Doc 3 §11

    Audit Round 7.4 — use total equity (`_ALPACA_EQUITY` setting written
    by Gate 0) instead of cash-only. Previously Gate 7 sized off equity
    but Gate 11 checked exposure against cash, so two gates disagreed
    when USER-managed positions tied up cash. Falls back to cash when
    equity isn't in settings (preserves old behavior for pre-Gate-0
    callers).
    """
    equity = 0.0
    if db is not None:
        try:
            equity = float(db.get_setting('_ALPACA_EQUITY') or 0)
        except (TypeError, ValueError):
            equity = 0.0
    if equity <= 0:
        equity = float(portfolio.get("cash", 0) or 0)
    tier = get_portfolio_tier(equity)

    # Total gross exposure cap
    deployed = sum(p["entry_price"] * p["shares"] for p in positions)
    new_val  = size * (signal.get("price") or 1)
    gross    = (deployed + new_val) / equity if equity > 0 else 0
    gross_ok = gross <= C.MAX_GROSS_EXPOSURE

    decision_log.gate("11_GROSS_EXPOSURE", gross_ok, {
        "current_deployed": f"${deployed:.2f}",
        "new_position":     f"${new_val:.2f}",
        "projected_gross":  f"{gross*100:.1f}%",
        "limit":            f"{C.MAX_GROSS_EXPOSURE*100:.0f}%",
    }, "exposure OK" if gross_ok else f"BLOCK — would breach {C.MAX_GROSS_EXPOSURE*100:.0f}% cap")
    if not gross_ok:
        return False

    # Max position count
    pos_ok = len(positions) < tier["max_positions"]
    decision_log.gate("11_POSITION_COUNT", pos_ok, {
        "open_positions": len(positions),
        "max_positions":  tier["max_positions"],
        "tier":           tier["label"],
    }, "position count OK" if pos_ok else "BLOCK — max positions reached")
    if not pos_ok:
        return False

    # Sector exposure
    sig_sector    = signal.get("sector", "")
    sector_val    = sum(p["entry_price"] * p["shares"]
                        for p in positions if p.get("sector") == sig_sector)
    sector_pct    = (sector_val + new_val) / equity if equity > 0 else 0
    sector_ok     = sector_pct <= C.MAX_SECTOR_PCT
    decision_log.gate("11_SECTOR_EXPOSURE", sector_ok, {
        "sector":              sig_sector,
        "projected_sector_pct":f"{sector_pct*100:.1f}%",
        "limit":               f"{C.MAX_SECTOR_PCT*100:.0f}%",
    }, "sector OK" if sector_ok else f"BLOCK — sector {sig_sector} would reach {sector_pct*100:.1f}%")
    if not sector_ok:
        return False

    return True


# ── GATE 13 — STRESS OVERRIDES ───────────────────────────────────────────────

def gate13_stress(alpaca, decision_log: TradeDecisionLog) -> bool:
    """
    Detect extreme market conditions. Returns True = safe, False = halt/de-risk.
    Logic: Doc 3 §13
    """
    sym  = C.BENCHMARK_SYMBOL
    bars = alpaca.get_bars(sym, days=3)

    if not bars:
        decision_log.gate("13_STRESS", True, {"reason": "no data for stress check"}, "stress check skipped")
        return True

    current   = bars[-1]["c"]
    prev_close = bars[-2]["c"] if len(bars) >= 2 else current
    intraday_drop = (prev_close - current) / prev_close if prev_close > 0 else 0

    # Benchmark crash: intraday drop > threshold
    if intraday_drop >= C.BENCHMARK_CRASH_PCT:
        decision_log.gate("13_STRESS", False, {
            "benchmark":        sym,
            "prev_close":       f"${prev_close:.2f}",
            "current":          f"${current:.2f}",
            "intraday_drop":    f"{intraday_drop*100:.2f}%",
            "crash_threshold":  f"{C.BENCHMARK_CRASH_PCT*100:.0f}%",
        }, f"STRESS — benchmark crash {intraday_drop*100:.1f}% detected → DEFENSIVE mode forced")
        return False

    # TODO: DATA_DEPENDENCY — Flash crash detection requires intraday bar data
    # TODO: DATA_DEPENDENCY — Liquidity collapse requires real-time spread monitoring

    decision_log.gate("13_STRESS", True, {
        "benchmark":     sym,
        "intraday_drop": f"{intraday_drop*100:.2f}%",
    }, "no stress condition detected")
    return True


# ── GATE 14 — EVALUATION LOOP ────────────────────────────────────────────────

def gate14_evaluation(db, portfolio: dict, decision_log: TradeDecisionLog) -> bool:
    """
    Update performance metrics. Check kill condition.
    Returns True = continue trading, False = strategy suspended.
    Logic: Doc 3 §14
    """
    outcomes = db.get_recent_outcomes(limit=100)
    if len(outcomes) < 5:
        decision_log.gate("14_EVALUATION", True, {"reason": "insufficient trade history"}, "evaluation skipped — need ≥5 trades")
        return True

    window = [o for o in outcomes[-C.PERFORMANCE_WINDOW_DAYS:]]
    wins   = [o for o in window if o.get("verdict") == "WIN"]
    losses = [o for o in window if o.get("verdict") == "LOSS"]
    win_rate  = len(wins) / len(window) if window else 0
    avg_win   = sum(o.get("pnl_dollar", 0) for o in wins)  / len(wins)   if wins   else 0
    avg_loss  = sum(o.get("pnl_dollar", 0) for o in losses) / len(losses) if losses else 0
    expectancy = avg_win * win_rate + avg_loss * (1 - win_rate)

    pnl_series = [o.get("pnl_pct", 0) for o in window]
    mean_ret   = sum(pnl_series) / len(pnl_series) if pnl_series else 0
    std_ret    = (sum((r - mean_ret)**2 for r in pnl_series) / len(pnl_series))**0.5 if pnl_series else 0
    sharpe     = (mean_ret / std_ret * (252**0.5)) if std_ret > 0 else 0

    equity = portfolio.get("cash", 0)
    peak   = portfolio.get("peak_equity") or equity
    dd     = (peak - equity) / peak if peak > 0 else 0

    kill = sharpe < C.EVAL_MIN_SHARPE and dd > C.EVAL_MAX_DRAWDOWN

    decision_log.gate("14_EVALUATION", "SUSPEND" if kill else "CONTINUE", {
        "trades_in_window":  len(window),
        "win_rate":          f"{win_rate*100:.1f}%",
        "avg_win":           f"${avg_win:.2f}",
        "avg_loss":          f"${avg_loss:.2f}",
        "expectancy":        f"${expectancy:.2f}",
        "rolling_sharpe":    f"{sharpe:.3f}",
        "current_drawdown":  f"{dd*100:.2f}%",
        "sharpe_threshold":  f"{C.EVAL_MIN_SHARPE:.2f}",
        "dd_threshold":      f"{C.EVAL_MAX_DRAWDOWN*100:.0f}%",
        "kill_condition":    str(kill),
    }, "STRATEGY SUSPENDED — kill condition met" if kill else "performance within limits")

    if kill:
        db.log_event("STRATEGY_KILL_CONDITION", agent="trade_logic_agent",
                     details=f"Sharpe={sharpe:.3f} DD={dd*100:.1f}% — suspended pending human review")
        log.critical("STRATEGY KILL CONDITION: Sharpe and drawdown both outside limits. Suspending new entries.")
        return False

    return True


# ── MANAGED MODE (trade approval queue) ───────────────────────────────────────

def queue_for_approval(signal, decision_data):
    try:
        _db().queue_approval(
            signal_id  = signal['id'],
            ticker     = signal['ticker'],
            company    = signal.get('company', ''),
            sector     = signal.get('sector', ''),
            politician = signal.get('politician', ''),
            confidence = signal.get('confidence', ''),
            staleness  = signal.get('staleness', ''),
            headline   = signal.get('headline', ''),
            price      = decision_data.get('price'),
            shares     = decision_data.get('shares'),
            max_trade  = decision_data.get('max_trade'),
            trail_amt  = decision_data.get('trail_amt'),
            trail_pct  = decision_data.get('trail_pct'),
            vol_label  = decision_data.get('vol_label'),
            reasoning  = decision_data.get('reasoning', ''),
            session    = decision_data.get('session', ''),
        )
        log.info(f"[MANAGED] Trade queued: {signal['ticker']} ${decision_data.get('max_trade',0):.2f}")
    except Exception as e:
        log.error(f"queue_for_approval error: {e}")
        raise

    _notify_approval_request(signal, decision_data)


def _notify_approval_request(signal, decision_data):
    """
    Send email notification when a trade is queued for approval.

    Dedup guard: if this signal_id already has a PENDING_APPROVAL row that
    predates this session (i.e. was inserted more than 60s ago), skip the
    email — it was already notified and the user hasn't acted on it yet.
    This is a secondary safety net; the primary guard is db.acknowledge_signal()
    being called immediately after queue_for_approval() in the main loop.
    """
    recipient = _customer_email()
    if not RESEND_API_KEY or not recipient:
        return

    # Secondary dedup: check if already notified for this signal
    try:
        existing = _db().get_pending_approvals(status_filter=['PENDING_APPROVAL'])
        for row in existing:
            if str(row.get('id')) == str(signal.get('id')):
                import time as _time
                try:
                    from datetime import timezone as _tz
                    queued_at = row.get('queued_at', '')
                    if queued_at:
                        queued_ts = datetime.fromisoformat(
                            queued_at.replace('Z', '+00:00')
                        ).timestamp()
                        age_s = _time.time() - queued_ts
                        if age_s > 60:
                            log.info(
                                f"[NOTIFY] Skipping duplicate approval email for "
                                f"{signal.get('ticker','?')} — already notified "
                                f"{int(age_s)}s ago (id={signal.get('id','?')})"
                            )
                            return
                except Exception:
                    pass
    except Exception:
        pass
    ticker     = signal.get('ticker', '?')
    company    = signal.get('company', '')
    confidence = signal.get('confidence', '')
    politician = signal.get('politician', '')
    price      = decision_data.get('price', 0)
    max_trade  = decision_data.get('max_trade', 0)
    shares     = decision_data.get('shares', 0)
    vol_label  = decision_data.get('vol_label', '')
    reasoning  = decision_data.get('reasoning', '')
    headline   = signal.get('headline', '')
    subject    = f"[Synthos] Trade approval required — {ticker}"
    body = (
        f"A trade signal is waiting for your approval in the portal.\n\n"
        f"Ticker:      {ticker}"
        + (f" ({company})" if company else "") + "\n"
        f"Politician:  {politician}\n"
        f"Price:       ${price:.2f}\n"
        f"Shares:      {shares:.4f}\n"
        f"Max trade:   ${max_trade:.2f}\n"
        f"Volatility:  {vol_label}\n"
        f"Confidence:  {confidence}\n\n"
        f"Signal:\n{headline}\n\n"
        f"Reasoning:\n{reasoning}\n\n"
        f"Approve or reject at the portal (port {os.environ.get('PORTAL_PORT', '5001')})."
    )
    try:
        requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'from':    ALERT_FROM or 'alerts@synthos.local',
                'to':      [recipient],
                'subject': subject,
                'text':    body,
            },
            timeout=10,
        )
        log.info(f"[MANAGED] Approval notification sent: {ticker} -> {recipient}")
    except Exception as e:
        log.warning(f"[MANAGED] Approval notification failed: {e}")

def get_approved_trades():
    try:
        return _db().get_pending_approvals(status_filter=['APPROVED'])
    except Exception as e:
        log.error(f"get_approved_trades error: {e}")
        return []

def mark_approval_executed(signal_id):
    try:
        _db().mark_approval_executed(signal_id)
    except Exception as e:
        log.error(f"mark_approval_executed error: {e}")


# ── PROTECTIVE EXIT (KEEP from v1.x) ─────────────────────────────────────────

def _enqueue_p0_alert(subject, body, event_type, related_ticker=None, related_signal_id=None):
    """
    Route a P0 alert to the Scoop queue on the Company Node.

    Routing priority:
      1. COMPANY_URL  — direct to company_server.py (preferred)
      2. MONITOR_URL  — monitor proxies to company node if COMPANY_URL is set there
                        (backward-compat: works during transition before COMPANY_URL is deployed)

    Includes customer_email in payload so Scoop can dispatch without a
    separate auth.db lookup on the Company Pi.
    """
    pi_id  = os.environ.get('PI_ID', 'synthos-pi')
    # Prefer Company Node; fall back to Monitor Node proxy
    target_url = COMPANY_URL or os.environ.get('MONITOR_URL', '').rstrip('/')
    token      = (
        os.environ.get('SECRET_TOKEN')
        or os.environ.get('COMPANY_TOKEN')
        or os.environ.get('MONITOR_TOKEN', '')
    )
    if not target_url:
        log.warning("[ENQUEUE] Neither COMPANY_URL nor MONITOR_URL set — cannot enqueue")
        return False

    # Pre-resolve customer email so Scoop doesn't need auth.db access
    customer_email = ""
    try:
        customer_email = _customer_email() or ""
    except Exception:
        pass

    payload = {
        "event_type":        event_type,
        "priority":          0,
        "subject":           subject,
        "body":              body,
        "source_agent":      "Trade Logic",
        "pi_id":             pi_id,
        "audience":          "customer",
        "related_ticker":    related_ticker,
        "related_signal_id": str(related_signal_id) if related_signal_id else None,
        "payload": {
            "ticker":           related_ticker,
            "signal_id":        related_signal_id,
            "pi_id":            pi_id,
            "customer_email":   customer_email,   # Scoop uses this as To: address
        },
    }
    try:
        r = requests.post(
            f"{target_url}/api/enqueue",
            json=payload,
            headers={"X-Token": token, "Content-Type": "application/json"},
            timeout=5,
        )
        if r.status_code == 200:
            log.info(f"[ENQUEUE] P0 queued via {'company' if COMPANY_URL else 'monitor'} node")
            return True
        log.warning(f"[ENQUEUE] Non-200 response: {r.status_code} {r.text[:120]}")
        return False
    except Exception as e:
        log.warning(f"[ENQUEUE] Request failed: {e}")
        return False

def _direct_send_fallback(subject, body, reason="enqueue_failed"):
    log.warning(f"[FALLBACK] Direct send triggered — reason: {reason}")
    try:
        _db().log_event("P0_DIRECT_SEND_FALLBACK", agent="trade_logic_agent",
                        details=f"reason={reason} subject={subject[:80]}")
    except Exception:
        pass
    recipient = _customer_email()
    if RESEND_API_KEY and recipient:
        try:
            requests.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {RESEND_API_KEY}',
                    'Content-Type':  'application/json',
                },
                json={
                    'from':    ALERT_FROM or 'alerts@synthos.local',
                    'to':      [recipient],
                    'subject': subject,
                    'text':    body,
                },
                timeout=10,
            )
            return True
        except Exception as e:
            log.error(f"[FALLBACK] Resend failed: {e}")
    return False

def send_protective_exit_email(ticker, reason, reasoning, entry_price,
                                exit_price, shares, pnl_dollar):
    pnl_sign  = "+" if pnl_dollar >= 0 else ""
    direction = "profit" if pnl_dollar >= 0 else "loss"
    subject   = f"[Synthos] Protective Exit — {ticker} ({pnl_sign}${abs(pnl_dollar):.2f})"
    body = (
        f"Synthos executed a Layer 1 protective exit.\n\n"
        f"Ticker: {ticker}\nExit reason: {reason}\n"
        f"Entry: ${entry_price:.2f} | Exit: ${exit_price:.2f} | "
        f"Shares: {shares:.4f} | P&L: {pnl_sign}${abs(pnl_dollar):.2f} ({direction})\n\n"
        f"Reasoning: {reasoning}"
    )
    if not _enqueue_p0_alert(subject, body, "PROTECTIVE_EXIT_TRIGGERED", ticker):
        _direct_send_fallback(subject, body, "enqueue_failed_protective_exit")


# ── BIL RESERVE (KEEP from v1.x — REVIEW: integrate into Gate 11) ────────────

def sync_bil_reserve(db, alpaca):
    # Check per-customer BIL setting
    if not getattr(C, 'ENABLE_BIL_RESERVE', True):
        log.info('[BIL] Treasury reserve disabled by customer setting')
        return
    try:
        account   = alpaca.get_account()
        if not account:
            return
        equity    = float(account.get('equity', 0))
        free_cash = float(account.get('cash', 0))
        bil_pos   = alpaca.get_position_safe(C.BIL_TICKER)
        bil_value = float(bil_pos.get('market_value', 0)) if bil_pos else 0.0
        # Use equity (not cash + bil) to avoid margin-inflated totals
        total_liq = equity
        target    = round(total_liq * C.IDLE_RESERVE_PCT, 2)
        delta     = round(target - bil_value, 2)
        log.info(f"[BIL] Liquid: ${total_liq:.2f} | Current: ${bil_value:.2f} | "
                 f"Target: ${target:.2f} | Delta: ${delta:+.2f}")
        if abs(delta) < C.BIL_REBALANCE_THRESHOLD:
            return
        if delta > 0:
            # Safety: never buy more BIL than actual cash allows (prevent margin usage)
            # Also preserve tradeable capital — don't lock more than target in BIL
            max_bil = equity * C.IDLE_RESERVE_PCT
            if free_cash < 0:
                log.warning(f"[BIL] Negative cash (${free_cash:.2f}) — skipping buy")
                return
            # Never spend more than 50% of free cash on BIL in one go
            buy = min(delta, max(0.0, free_cash * 0.5), max_bil - bil_value)
            if buy < C.BIL_REBALANCE_THRESHOLD:
                return
            # R10-8: the prior `if buy >= C.BIL_REBALANCE_THRESHOLD:` wrapper
            # was unreachable dead code (we returned on the inverse above);
            # removed to avoid suggesting a meaningful guard exists here.
            if alpaca._submit_notional(C.BIL_TICKER, buy, "buy"):
                db.log_event("BIL_BUY", agent="Trade Logic",
                             details=f"Bought ${buy:.2f} BIL")
        else:
            sell = abs(delta)
            if sell >= bil_value * 0.99:
                if alpaca.close_position(C.BIL_TICKER):
                    db.log_event("BIL_SELL", agent="Trade Logic",
                                 details=f"Sold all BIL (${bil_value:.2f})")
            else:
                if alpaca._submit_notional(C.BIL_TICKER, sell, "sell"):
                    db.log_event("BIL_SELL", agent="Trade Logic",
                                 details=f"Sold ${sell:.2f} BIL")
    except Exception as e:
        log.error(f"[BIL] sync error: {e}")



# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────



# ── POSITION ROTATION ─────────────────────────────────────────────────────────

def _rotate_positions(db, shared_db, alpaca, positions, regime, tier,
                      portfolio, tradeable, session, now):
    """
    When portfolio is full, evaluate top signals against weakest holdings.
    Sell losing positions if a new signal significantly outscores them.
    Rules: only rotate losers, score gap >= 0.20, max 1 per session, never BIL.

    Returns the number of rotations performed (int) so the caller can
    update its trade_events counter. Previously the function referenced
    an out-of-scope `trade_events` and would NameError if it ever fired.
    """
    ROTATION_THRESHOLD = 0.20
    MAX_ROTATIONS = 1
    rotations = 0  # Hoisted so every early-return path returns a valid count

    try:
        # Signal pool for rotation — same source the main loop uses
        # (post-window-calculator-deletion 2026-04-24). Pulls VALIDATED
        # signals with tier quotas applied, then filters to those with
        # a current live price (required by downstream scoring).
        signals = shared_db.get_validated_signals()
        _price_map = {}
        try:
            with shared_db.conn() as _c:
                _pr = _c.execute(
                    "SELECT ticker, price FROM live_prices WHERE price IS NOT NULL"
                ).fetchall()
                _price_map = {r['ticker']: float(r['price']) for r in _pr if r['price']}
        except Exception:
            pass
        signals = [s for s in signals if _price_map.get(s.get('ticker')) is not None]
        if not signals:
            return rotations

        # Build list of held positions with scores (excluding BIL)
        held = []
        for p in positions:
            if p['ticker'] == C.BIL_TICKER:
                continue
            entry_score = float(p.get('entry_signal_score') or 0)
            current_price = float(p.get('current_price') or p['entry_price'])
            entry_price = float(p['entry_price'])
            pnl_pct = (current_price - entry_price) / entry_price if entry_price else 0
            held.append({
                'id': p['id'], 'ticker': p['ticker'],
                'entry_score': entry_score, 'pnl_pct': pnl_pct,
                'shares': float(p['shares']), 'entry_price': entry_price,
                'current_price': current_price,
            })

        if not held:
            return rotations

        # Only consider losing positions for rotation
        losers = [h for h in held if h['pnl_pct'] < 0]
        if not losers:
            log.info("[ROTATION] All positions in profit — no rotation candidates")
            return rotations

        losers.sort(key=lambda x: x['entry_score'])
        weakest = losers[0]
        # rotations initialized at function top for early-return safety

        for signal in signals[:20]:
            if rotations >= MAX_ROTATIONS:
                break

            # Don't try to rotate into a ticker we already hold
            held_tickers = {h['ticker'] for h in held}
            if signal['ticker'] in held_tickers:
                continue

            sig_log = TradeDecisionLog(session=session, ticker=signal['ticker'],
                                       signal_id=(signal.get('id') if _CUSTOMER_ID == _OWNER_CID else None))

            # Gates 4-6: must pass eligibility, scoring, and entry
            if not gate4_eligibility(signal, positions, alpaca, sig_log):
                continue

            score = gate5_signal_score(signal, positions, alpaca, sig_log)
            if score == 0.0:
                continue

            # Gate 5.5 — severe-negative news veto (Phase 3a)
            if not gate5_5_news_veto(signal, sig_log):
                continue

            candidate = gate6_entry(signal, score, regime, alpaca, sig_log)
            if candidate is None:
                continue

            # Compare against weakest
            score_gap = score - weakest['entry_score']
            if score_gap < ROTATION_THRESHOLD:
                continue

            log.info(f"[ROTATION] {signal['ticker']} (score {score:.3f}) vs "
                     f"{weakest['ticker']} (score {weakest['entry_score']:.3f}, "
                     f"P&L {weakest['pnl_pct']*100:+.1f}%) — gap {score_gap:.3f}")

            # ── SELL the weak position ──
            try:
                sell_result = alpaca.close_position(weakest['ticker'])
                if not sell_result:
                    log.warning(f"[ROTATION] Failed to sell {weakest['ticker']} — aborting rotation")
                    continue
            except Exception as e:
                log.error(f"[ROTATION] Sell error for {weakest['ticker']}: {e}")
                continue

            # Record exit at current market price (not entry price)
            exit_price = weakest['current_price']
            _rot_pnl = db.close_position(weakest['id'], exit_price, exit_reason='ROTATED_OUT')
            db.log_event("POSITION_ROTATED", agent="Trade Logic",
                         details=f"Sold {weakest['ticker']} (score {weakest['entry_score']:.3f}, "
                                 f"P&L {weakest['pnl_pct']*100:+.1f}%) for {signal['ticker']} (score {score:.3f})")
            _rp_sign = '+' if _rot_pnl >= 0 else ''
            db.add_notification('trade', f'Sold {weakest["ticker"]}',
                f'Rotated out for stronger signal — P&L {_rp_sign}${_rot_pnl:.2f}',
                meta={'ticker': weakest['ticker'], 'side': 'sell', 'pnl': round(_rot_pnl, 2), 'reason': 'ROTATED_OUT', 'replaced_by': signal['ticker']})
            # Sell counted as part of the rotation; caller increments
            # trade_events once per complete rotation (see return value).

            # ── BUY the stronger signal ──
            atr = alpaca.get_atr(signal['ticker'])
            if not atr:
                atr = candidate['price'] * 0.02

            # Refresh positions list after sell (so sizing doesn't count sold position)
            fresh_positions = db.get_open_positions()

            size = gate7_sizing(candidate, regime, portfolio, fresh_positions, atr, sig_log, db=db)
            if size <= 0:
                # Insufficient cash after manual/auto allocation — skip.
                # Decision already logged inside gate7_sizing.
                sig_log.decide("SKIP", "insufficient cash after manual/auto allocation")
                sig_log.commit(db)
                # Also log to signal_decisions so the cash-starved warning
                # banner (phase 7) can count these skips over a rolling window.
                try:
                    db.log_signal_decision(
                        agent='trade_logic', action='SKIP_INSUFFICIENT_CASH_AFTER_MANUAL',
                        ticker=signal.get('ticker'), signal_id=signal.get('id'),
                        reason='cash insufficient after manual/auto allocation',
                    )
                except Exception:
                    pass
                _mark_signal_evaluated(signal['id'], 'SKIP_INSUFFICIENT_CASH_AFTER_MANUAL')
                continue
            risk = gate8_risk(candidate, atr, session, sig_log)

            if _is_supervised():
                trail_amt, trail_pct, vol_label = calculate_trail_stop(
                    atr, candidate['price'], signal.get('sector', ''))
                decision_data = {
                    "price": candidate['price'], "shares": size,
                    "max_trade": round(size * candidate['price'], 2),
                    "trail_amt": trail_amt, "trail_pct": trail_pct,
                    "vol_label": vol_label,
                    "reasoning": (
                        f"ROTATION: replaces {weakest['ticker']} | Score gap: {score_gap:.3f} | "
                        f"{candidate['type']} anchor={candidate.get('anchor_type','?')} "
                        f"${candidate.get('anchor_price',0):.2f} chase={candidate.get('chase_pct',0)*100:+.2f}%"
                    ),
                    "session": session,
                }
                queue_for_approval(signal, decision_data)
                _shared_db().acknowledge_signal(signal['id'])
                log.info(f"[ROTATION/SUPERVISED] {signal['ticker']} queued for approval "
                         f"(replacing {weakest['ticker']})")
                rotations += 1
                sig_log.decide("ROTATE", f"Replaced {weakest['ticker']} (gap {score_gap:.3f})")
                sig_log.commit(db)
            else:
                try:
                    trail_amt, trail_pct, vol_label = calculate_trail_stop(
                        atr, candidate['price'], signal.get('sector', ''))
                    order = alpaca.submit_order(signal['ticker'], size, "buy")
                    if order:
                        # signal_id=None for non-owner customers (FK references local signals table,
                        # but signals live in shared DB)
                        _sig_id = signal['id'] if _CUSTOMER_ID == _OWNER_CID else None
                        db.open_position(
                            ticker=signal['ticker'], company=signal.get('company'),
                            sector=signal.get('sector'), entry_price=candidate['price'],
                            shares=size, trail_stop_amt=trail_amt,
                            trail_stop_pct=trail_pct, vol_bucket=vol_label,
                            signal_id=_sig_id,
                            entry_signal_score=round(float(score), 4),
                            entry_sentiment_score=signal.get('sentiment_score'),
                            interrogation_status=signal.get('interrogation_status'),
                        )
                        alpaca.submit_order(signal['ticker'], size, "sell",
                                            order_type="trailing_stop", trail_price=trail_amt)
                        _shared_db().acknowledge_signal(signal['id'])
                        log.info(f"[ROTATION] COMPLETE: Sold {weakest['ticker']} → "
                                 f"BUY {size:.4f} {signal['ticker']} @ ${candidate['price']:.2f}")
                        _cost = round(candidate['price'] * size, 2)
                        db.add_notification('trade', f'Bought {signal["ticker"]}',
                            f'{size:.2f} shares @ ${candidate["price"]:.2f} — ${_cost:.2f} invested (rotated from {weakest["ticker"]})',
                            meta={'ticker': signal['ticker'], 'side': 'buy', 'shares': round(size, 4), 'price': round(candidate['price'], 2), 'rotation_from': weakest['ticker']})
                        rotations += 1
                        sig_log.decide("ROTATE", f"Replaced {weakest['ticker']} (gap {score_gap:.3f})")
                        sig_log.commit(db)
                    else:
                        log.error(f"[ROTATION] Buy order failed for {signal['ticker']} — "
                                  f"{weakest['ticker']} already sold, position count reduced")
                        sig_log.decide("ROTATE_PARTIAL", f"Sold {weakest['ticker']} but buy failed")
                        sig_log.commit(db)
                except Exception as e:
                    log.error(f"[ROTATION] Buy error after sell: {e} — "
                              f"{weakest['ticker']} sold, {signal['ticker']} buy failed")
                    sig_log.decide("ROTATE_PARTIAL", f"Sell ok, buy error: {e}")
                    sig_log.commit(db)

        if rotations == 0:
            log.info("[ROTATION] No signals strong enough to justify rotation")

    except Exception as e:
        log.error(f"[ROTATION] Unexpected error: {e}", exc_info=True)

    return rotations


def _run_halt_check():
    """Absolute first check. If admin or customer halt is active, log and exit."""
    _halt = _check_halt_state()
    if _halt is not None:
        _src, _reason = _halt
        log.info(f"[HALT] {_src} halt active (reason={_reason!r}) — skipping trader run")
        try:
            _db().log_event("TRADER_SKIPPED_HALT", agent="Trade Logic",
                            details=f"src={_src} reason={_reason[:200]}")
        except Exception:
            pass
        sys.exit(0)


def _start_watchdog():
    """Start background runtime watchdog. Returns the thread."""
    _set_phase('startup')
    _t0 = time.monotonic()
    _wd = _threading.Thread(
        target=_runtime_watchdog,
        args=(_t0, TRADER_RUNTIME_BUDGET_SEC),
        daemon=True,
    )
    _wd.start()
    return _wd


def _init_clients(session):
    """Create DB/Alpaca clients, log AGENT_START, run Gate 1. Returns (db, alpaca, now, session_log)."""
    db     = _db()
    alpaca = AlpacaClient()
    now    = datetime.now(ET)

    log.info(f"ExecutionAgent starting — session={session} mode={TRADING_MODE} "
             f"operating={OPERATING_MODE} time={now.strftime('%H:%M ET')}")
    db.log_event("AGENT_START", agent="Trade Logic",
                 details=f"session={session} mode={TRADING_MODE} operating={OPERATING_MODE}")
    db.log_heartbeat("trade_logic_agent", "RUNNING")

    session_log = TradeDecisionLog(session=session)

    # ── GATE 1: System Gate
    if not gate1_system(db, alpaca, session, session_log):
        session_log.commit(db)
        sys.exit(0)

    return db, alpaca, now, session_log


def _run_gate0_account_health(db, alpaca, session_log):
    """Gate 0: account health, reconciliation. Returns (account, equity, cash, positions) or (None, None, None, None)."""
    account = alpaca.get_account()
    if not account:
        log.warning("[GATE 0] Cannot reach Alpaca account API — skipping this run")
        session_log.gate("0_HEALTH", "SKIP", {}, "Alpaca account unreachable")
        session_log.commit(db)
        db.log_heartbeat("trade_logic_agent", "OK")
        return None, None, None, None

    alpaca_equity = float(account.get('equity', 0))
    alpaca_cash = float(account.get('cash', 0))

    # Skip accounts with insufficient equity
    if alpaca_equity < 10:
        log.info(f"[GATE 0] Account equity ${alpaca_equity:.2f} < $10 — skipping")
        session_log.gate("0_HEALTH", "SKIP", {"equity": alpaca_equity}, "insufficient equity")
        session_log.commit(db)
        db.log_event("ACCOUNT_SKIP", agent="Trade Logic",
                     details=f"Equity ${alpaca_equity:.2f} below $10 minimum")
        db.log_heartbeat("trade_logic_agent", "OK")
        return None, None, None, None

    # Sync cash — Alpaca is always truth
    db.update_portfolio(cash=alpaca_cash)
    db.set_setting('_ALPACA_EQUITY', str(alpaca_equity))

    # Clear new-customer flag once account is funded
    if alpaca_equity >= 1.0 and db.get_setting('NEW_CUSTOMER') != 'false':
        db.set_setting('NEW_CUSTOMER', 'false')
        log.info(f"[GATE 0] Account funded (${alpaca_equity:.2f}) — cleared NEW_CUSTOMER flag")

    # Full position reconciliation
    alpaca_positions = alpaca.get_positions() or []
    alpaca_tickers = {p['symbol'] for p in alpaca_positions}
    db_positions = db.get_open_positions()
    db_tickers = {p['ticker'] for p in db_positions}

    orphans = alpaca_tickers - db_tickers   # On Alpaca, not in DB
    ghosts = db_tickers - alpaca_tickers     # In DB, not on Alpaca
    healed = 0

    # Auto-adopt orphans (customer bought on Alpaca directly, or previous adopt failed)
    for t in orphans:
        ap = next((p for p in alpaca_positions if p['symbol'] == t), None)
        if ap:
            shares = float(ap.get('qty', 0))
            entry = float(ap.get('avg_entry_price', 0))
            # Resolve sector via the map → ticker_sectors → screener cascade.
            # retail_sector_backfill_agent fills gaps via FMP on its own schedule.
            _orphan_sector = ''
            try:
                from retail_sector_map import lookup_sector
                _orphan_sector = lookup_sector(t, _shared_db()) or ''
            except Exception:
                pass
            log.warning(f"[GATE 0] ORPHAN: {t} {shares:.4f}sh @ ${entry:.2f} sector={_orphan_sector or '?'} — auto-adopting")
            # Orphans are positions on Alpaca with no matching DB row — by
            # construction these are user-initiated (bot buys always record
            # their own position row atomically). Tag managed_by='user' so
            # the trader doesn't treat them as its own to manage.
            db.open_position(
                ticker=t, company=t, sector=_orphan_sector,
                entry_price=entry, shares=shares,
                trail_stop_amt=0, trail_stop_pct=0, vol_bucket='normal',
                signal_id=None, entry_signal_score=None,
                entry_sentiment_score=None, interrogation_status=None,
                managed_by='user',
            )
            db.log_event("ORPHAN_ADOPTED", agent="Trade Logic",
                         details=f"{t} {shares:.4f}sh @ ${entry:.2f} adopted from Alpaca as USER-managed")
            db.add_notification('account', f'{t} position detected',
                f'{shares:.2f} shares @ ${entry:.2f} added as user-managed',
                meta={'ticker': t, 'type': 'orphan_adopted', 'managed_by': 'user'})
            healed += 1

    # Auto-close ghosts (customer sold on Alpaca directly, or trailing stop filled)
    for t in ghosts:
        db_pos = next((p for p in db_positions if p['ticker'] == t), None)
        if not db_pos:
            continue
        # Check for trailing stop fill first
        filled = alpaca.get_filled_orders(t, after_date=db_pos.get('opened_at'))
        sell_fill = next(
            (o for o in (filled or [])
             if o.get('side') == 'sell' and o.get('status') == 'filled'),
            None
        )
        if sell_fill:
            fill_price = float(sell_fill.get('filled_avg_price', 0))
            reason = 'TRAILING_STOP_FILLED' if sell_fill.get('type') == 'trailing_stop' else 'CUSTOMER_SOLD'
        else:
            # No sell order found — customer may have sold via Alpaca UI
            fill_price = float(db_pos.get('current_price') or db_pos.get('entry_price') or 0)
            reason = 'CUSTOMER_SOLD'

        try:
            pnl = db.close_position(db_pos['id'], fill_price, exit_reason=reason)
            log.warning(f"[GATE 0] GHOST: {t} closed — reason={reason} price=${fill_price:.2f} pnl=${pnl:+.2f}")
            db.log_event("GHOST_CLOSED", agent="Trade Logic",
                         details=f"{t} {reason} @ ${fill_price:.2f} pnl=${pnl:+.2f}")
            _pnl_sign = '+' if pnl >= 0 else ''
            db.add_notification('account', f'{t} position closed',
                f'Position no longer on Alpaca — P&L {_pnl_sign}${pnl:.2f}',
                meta={'ticker': t, 'type': 'ghost_closed', 'pnl': round(pnl, 2), 'reason': reason})
            healed += 1
        except Exception as _e:
            log.error(f"[GATE 0] Failed to close ghost {t}: {_e}")

    # Update prices on all matched positions
    for ap in alpaca_positions:
        cp = float(ap.get('current_price', 0))
        if cp:
            for pos in db.get_open_positions():
                if pos['ticker'] == ap['symbol']:
                    db.update_position_price(pos['id'], cp)

    # Backfill empty sectors on existing positions via the resolution cascade:
    #   hardcoded map → ticker_sectors cache → sector_screening
    # Tickers still unresolved after this are picked up by
    # retail_sector_backfill_agent (runs nightly, uses FMP).
    try:
        from retail_sector_map import lookup_sector as _lookup_sector
    except Exception:
        _lookup_sector = None
    if _lookup_sector is not None:
        _sdb = _shared_db()
        for pos in db.get_open_positions():
            if not pos.get('sector'):
                try:
                    resolved = _lookup_sector(pos['ticker'], _sdb)
                    if resolved:
                        with db.conn() as _c:
                            _c.execute("UPDATE positions SET sector=? WHERE id=?",
                                       (resolved, pos['id']))
                        log.info(f"[GATE 0] Sector backfill: {pos['ticker']} → {resolved}")
                except Exception:
                    pass

    # Check for first-run (no history at all) — setup only, don't trade yet.
    # BUG FIX 2026-04-17: `has_history` was False forever for customers who
    # never traded (no positions, no realized gains), re-firing the first-run
    # gate every cycle and blocking them from ever trading. We now also set
    # a persistent FIRST_RUN_COMPLETED customer setting the first time the
    # gate fires, and skip the gate in all subsequent runs.
    positions_after = db.get_open_positions()
    portfolio = db.get_portfolio()
    has_history = len(positions_after) > 0 or portfolio.get('realized_gains', 0) != 0
    first_run_done = (db.get_setting('FIRST_RUN_COMPLETED') == 'true')
    if not has_history and not orphans and not first_run_done:
        log.info(f"[GATE 0] First run — account setup only (equity ${alpaca_equity:.2f})")
        session_log.gate("0_HEALTH", "FIRST_RUN", {
            "equity": alpaca_equity, "cash": alpaca_cash,
        }, "first-run setup — will trade on next cycle")
        session_log.commit(db)
        db.set_setting('FIRST_RUN_COMPLETED', 'true')
        db.log_event("FIRST_RUN_COMPLETE", agent="Trade Logic",
                     details=f"Account initialized — equity ${alpaca_equity:.2f}")
        # Dedup key with no window → fires exactly once per customer, ever.
        db.add_notification('system', 'Account Ready',
            f'Your account has been initialized with ${alpaca_equity:,.2f} equity. Trading begins next session.',
            meta={'type': 'first_run', 'equity': alpaca_equity},
            dedup_key='account_ready_bootstrap')
        db.log_heartbeat("trade_logic_agent", "OK")
        return None, None, None, None

    # Audit Round 5 — BIL concentration alert. Informational sub-gate:
    # if the customer is parking > BIL_CONCENTRATION_THRESHOLD (default
    # 65%) of capital in BIL, surface it in the decision log and log a
    # warning. Doesn't block anything — just makes an otherwise-silent
    # condition visible.
    try:
        _bil = db.check_bil_concentration()
        session_log.gate(
            "0_BIL_CONCENTRATION",
            "HIGH" if _bil['over_threshold'] else "OK",
            {
                "bil_value":    f"${_bil['bil_value']:.2f}",
                "total_value":  f"${_bil['total_value']:.2f}",
                "bil_pct":      f"{_bil['bil_pct']*100:.1f}%",
                "threshold":    f"{_bil['threshold_pct']*100:.0f}%",
            },
            (f"BIL at {_bil['bil_pct']*100:.1f}% of portfolio — "
             f"above {_bil['threshold_pct']*100:.0f}% threshold"
             if _bil['over_threshold']
             else f"BIL at {_bil['bil_pct']*100:.1f}% of portfolio"),
        )
        if _bil['over_threshold']:
            log.warning(
                f"[BIL ALERT] concentration {_bil['bil_pct']*100:.1f}% "
                f"(${_bil['bil_value']:.0f} of ${_bil['total_value']:.0f}) "
                f">= threshold {_bil['threshold_pct']*100:.0f}% — "
                f"check for signal starvation or regime hold"
            )
            try:
                db.add_notification(
                    'alert',
                    f'BIL concentration {_bil["bil_pct"]*100:.0f}%',
                    f"Portfolio parking ${_bil['bil_value']:.0f} of "
                    f"${_bil['total_value']:.0f} in BIL. Usually means no "
                    f"entries qualifying or intentional risk-off stance.",
                    meta={
                        'bil_pct':     round(_bil['bil_pct'], 4),
                        'bil_value':   _bil['bil_value'],
                        'total_value': _bil['total_value'],
                    },
                    dedup_key=f'bil_concentration_{datetime.now().strftime("%Y%m%d")}',
                )
            except Exception as _e:
                log.debug(f"BIL notification write failed: {_e}")
    except Exception as _e:
        log.debug(f"BIL concentration check failed: {_e}")

    session_log.gate("0_HEALTH", "OK", {
        "equity": alpaca_equity, "cash": alpaca_cash,
        "positions_db": len(positions_after), "positions_alpaca": len(alpaca_positions),
        "orphans_adopted": len(orphans), "ghosts_closed": len(ghosts), "healed": healed,
    }, f"health check OK — {len(positions_after)} positions, ${alpaca_equity:.0f} equity")

    return account, alpaca_equity, alpaca_cash, positions_after


def _run_market_gates(db, alpaca, positions, session_log):
    """Prefetch bars, run Gates 2+3+13+14, BIL sync, expire stale approvals. Returns regime."""
    # ── PREFETCH: Batch-load bars for all tickers we'll need this run
    # One multi-symbol API call replaces dozens of individual get_bars() calls
    _prefetch_tickers = set()
    _prefetch_tickers.add(C.BENCHMARK_SYMBOL)  # SPY — used in Gates 2,3,5,10,13
    _prefetch_tickers.add('TLT')               # Gate 3 bond proxy
    _prefetch_tickers.add('BIL')               # BIL reserve
    for p in positions:
        _prefetch_tickers.add(p['ticker'])      # All held positions
    # Collect signal tickers from shared DB
    try:
        _sig_tickers = _shared_db().get_validated_signals()
        for s in (_sig_tickers or []):
            if s.get('ticker'):
                _prefetch_tickers.add(s['ticker'])
    except Exception:
        pass
    _prefetch_tickers.discard('')
    _prefetch_tickers.discard(None)
    # Fetch 70 days (covers max lookback: Gate 2 uses 60+, Gate 6 uses 40+)
    alpaca.prefetch_bars(list(_prefetch_tickers), days=70)

    # ── GATE 2: Benchmark Gate
    mode = gate2_benchmark(alpaca, session_log)

    # ── GATE 3: Regime Detection
    regime = gate3_regime(alpaca, mode, session_log)
    regime.mode = mode

    # ── GATE 13: Stress Overrides (early check — can override mode)
    if not gate13_stress(alpaca, session_log):
        regime.mode = "DEFENSIVE"
        log.warning("Stress condition — forcing DEFENSIVE mode")

    # ── GATE 14: Evaluation (check kill condition before trading)
    portfolio = db.get_portfolio()
    if not gate14_evaluation(db, portfolio, session_log):
        session_log.commit(db)
        log.warning("Strategy suspended — skipping new entries this session")
        # Still run position management below

    session_log.commit(db)

    # ── PRE-TRADE: BIL Reserve (reconciliation already done in Gate 0)
    sync_bil_reserve(db, alpaca)

    # ── EXPIRE STALE APPROVALS
    try:
        db.expire_stale_approvals(max_age_hours=48)
    except Exception as e:
        log.warning(f"expire_stale_approvals error: {e}")

    return regime


def _run_position_management(db, alpaca, regime, session_log, now, session):
    """Gate 10: active trade management over all open positions. Returns trade_events count."""
    trade_events = 0

    portfolio = db.get_portfolio()
    positions = db.get_open_positions()
    _equity   = float(db.get_setting('_ALPACA_EQUITY') or portfolio.get('cash', 0))
    tier      = get_portfolio_tier(_equity)

    # ── GATE 10: Active trade management (every session — open positions)
    _set_phase('gate_10_position_review', f"{len(positions)} positions")
    urgent_flags    = db.get_urgent_flags()
    urgent_tickers  = {f['ticker'] for f in urgent_flags}

    # Cache SPY bars for benchmark-relative checks (one API call for all positions)
    _spy_bars_cache = alpaca.get_bars(C.BENCHMARK_SYMBOL, days=30)

    for pos in positions:
        # Budget ceiling — exits run before entries, so if we hit the wall
        # we'd rather skip fresh-entry evaluation than leave an exit pending.
        # Still, bail early if we exceed runtime so decision log / heartbeat
        # get written cleanly.
        if budget_exceeded():
            log.warning("[GATE 10] Runtime budget exceeded — skipping remaining position reviews")
            break
        # USER-managed positions are user's responsibility — trader does not
        # apply trailing stops, stop-loss adjustments, or protective exits.
        # Price is still updated in the pre-loop sync (line ~2552) so the
        # dashboard shows correct P&L. See AUTO/USER tagging spec.
        if (pos.get('managed_by') or 'bot') == 'user':
            continue
        _set_phase('gate_10_position_review', f"ticker={pos['ticker']}")
        pos_log = TradeDecisionLog(session=session, ticker=pos['ticker'],
                                   signal_id=pos.get('signal_id'))
        current_price = pos.get('current_price') or pos['entry_price']
        try:
            _opened = datetime.fromisoformat(
                pos.get('opened_at', now.isoformat()).replace('Z', '+00:00'))
            if _opened.tzinfo is None:
                _opened = _opened.replace(tzinfo=timezone.utc)
            holding_days = (now - _opened).days
        except Exception:
            holding_days = 0

        exit_reason = None

        # ── Trailing stop ratchet: move stop up as price increases
        if current_price > pos['entry_price']:
            atr = alpaca.get_atr(pos['ticker'])
            if atr:
                new_stop = current_price - (atr * C.ATR_TRAIL_MULTIPLIER)
                current_stop = pos.get('trail_stop_amt', 0) or 0
                if new_stop > current_stop:
                    db.update_trail_stop(pos['id'], new_stop)
                    pos['trail_stop_amt'] = new_stop
                    pos_log.note(f"Trailing stop ratcheted: ${current_stop:.2f} -> ${new_stop:.2f}")

        # ── Late-day stop tightening: reduce gap risk before close
        _mtr_exit = get_market_time_regime(now)
        if _mtr_exit['is_late_day']:
            tighten = C.LATE_DAY_TIGHTEN_PCT
            distance = current_price - (pos.get('trail_stop_amt', 0) or 0)
            if distance > 0:
                tightened = current_price - distance * (1 - tighten)
                if tightened > (pos.get('trail_stop_amt', 0) or 0):
                    db.update_trail_stop(pos['id'], tightened)
                    pos['trail_stop_amt'] = tightened
                    pos_log.note(f"Late-day tightening ({tighten*100:.0f}%): stop -> ${tightened:.2f}")

        # Protective exit (urgent flag)
        if pos['ticker'] in urgent_tickers:
            exit_reason = "PULSE_EXIT"
            flag_info   = next((f for f in urgent_flags if f['ticker'] == pos['ticker']), {})
            pos_log.gate("10_STOP_PULSE", True, {
                "ticker": pos['ticker'],
                "flag_tier": flag_info.get('tier', 1),
                "detected": flag_info.get('detected_at', 'unknown'),
            }, "CASCADE signal — protective exit triggered")

        # Stop loss hit (with benchmark-relative adjustment)
        elif not exit_reason:
            effective_stop = pos.get('trail_stop_amt', 0) or 0
            # Adjust stop based on SPY correlation
            corr = compute_spy_correlation(alpaca, pos['ticker'],
                                           spy_bars_cache=_spy_bars_cache)
            if corr is not None and abs(corr) > 0.01:
                spy_change = 0
                if _spy_bars_cache and len(_spy_bars_cache) >= 2:
                    spy_change = (_spy_bars_cache[-1]["c"] - _spy_bars_cache[-2]["c"]) / _spy_bars_cache[-2]["c"]

                if corr > C.MAX_PORTFOLIO_CORR and spy_change < -0.01:
                    # High correlation + SPY dropping → widen stop (market-wide move)
                    effective_stop = pos['entry_price'] - (pos['entry_price'] - effective_stop) * C.BENCHMARK_CORR_WIDEN
                    pos_log.note(f"SPY corr={corr:.2f}, SPY={spy_change*100:+.1f}% — stop widened to ${effective_stop:.2f}")
                elif corr < 0.3 and spy_change >= -0.005:
                    # Low correlation + SPY flat = idiosyncratic risk → tighten
                    distance = current_price - effective_stop
                    if distance > 0:
                        effective_stop = current_price - distance * C.BENCHMARK_CORR_TIGHTEN
                        pos_log.note(f"SPY corr={corr:.2f}, SPY flat — stop tightened to ${effective_stop:.2f}")

            # Open-hour grace: suppress stop-loss triggering during the first
            # N minutes after 09:30 ET to ride out opening-gap noise that
            # otherwise false-triggers ratcheted trailing stops (see 2026-04-23
            # audit: 7/7 opening-hour stops fired at 09:32 ET).
            _now_et = datetime.now(ET)
            _grace_min = C.STOP_LOSS_OPEN_GRACE_MINUTES
            _in_open_grace = (
                _grace_min > 0
                and _now_et.hour == 9
                and 30 <= _now_et.minute < 30 + _grace_min
            )

            if current_price <= effective_stop:
                if _in_open_grace:
                    pos_log.note(
                        f"Stop-loss suppressed ({_grace_min}-min open grace): "
                        f"price=${current_price:.2f} stop=${effective_stop:.2f} "
                        f"entry=${pos['entry_price']:.2f}"
                    )
                else:
                    exit_reason = "STOP_LOSS"
                    pos_log.gate("10_STOP_LOSS", True, {
                        "current":    f"${current_price:.2f}",
                        "stop_level": f"${effective_stop:.2f}",
                        "trail_stop": f"${pos.get('trail_stop_amt', 0):.2f}",
                        "entry":      f"${pos['entry_price']:.2f}",
                        "spy_corr":   f"{corr:.2f}" if corr is not None else "N/A",
                    }, "stop loss triggered")

        # Max holding time
        elif holding_days > C.MAX_HOLDING_DAYS:
            exit_reason = "MAX_HOLDING_TIME"
            pos_log.gate("10_MAX_TIME", True, {
                "holding_days": holding_days,
                "max_days":     C.MAX_HOLDING_DAYS,
            }, f"max holding time {C.MAX_HOLDING_DAYS}d exceeded")

        else:
            # Profit-taking: tiered partial sells, reduce shares in-place
            gain_pct = (current_price - pos['entry_price']) / pos['entry_price']
            last_tier = float(pos.get('last_profit_tier') or 0)
            triggered = [r for r in get_profit_rules()
                         if gain_pct >= r["gain_pct"] and r["gain_pct"] > last_tier]
            if triggered:
                rule = triggered[-1]
                sell_shares = round(pos['shares'] * rule['sell_pct'], 4)
                pos_log.gate("10_PROFIT_TAKE", True, {
                    "ticker":     pos['ticker'],
                    "gain_pct":   f"{gain_pct*100:.2f}%",
                    "rule":       rule['label'],
                    "sell_shares":f"{sell_shares:.4f}",
                    "last_tier":  f"{last_tier*100:.0f}%",
                }, f"profit target {rule['label']} triggered")
                pos_log.decide("PARTIAL_EXIT", rule['label'])
                # Gate 0 already verified account health at this cycle's start.
                order = alpaca.submit_order(pos['ticker'], sell_shares, "sell")
                if order:
                    pnl = db.reduce_position(pos['id'], sell_shares, current_price,
                                              exit_reason="PROFIT_TAKE")
                    db.update_profit_tier(pos['id'], rule['gain_pct'])
                    try:
                        sig = db.get_signal_by_id(pos.get('signal_id'))
                        if sig and sig.get('politician'):
                            db.update_member_weight_after_trade(sig['politician'], pnl)
                    except Exception as _e:
                        # R10-13 — politician accounting is best-effort; don't
                        # swallow silently so a systemic breakage is visible.
                        log.debug(f"update_member_weight_after_trade (profit-take) failed: {_e}")
                pos_log.commit(db)
                continue

            pos_log.gate("10_ACTIVE", False, {
                "ticker":       pos['ticker'],
                "current":      f"${current_price:.2f}",
                "entry":        f"${pos['entry_price']:.2f}",
                "gain_pct":     f"{gain_pct*100:.2f}%",
                "holding_days": holding_days,
            }, "HOLD — no exit condition met")
            pos_log.decide("HOLD")
            pos_log.commit(db)
            continue

        # Execute exit
        if exit_reason:
            pos_log.decide("EXIT", f"reason={exit_reason} price=${current_price:.2f}")
            # Gate 0 already verified account health at this cycle's start.
            order = alpaca.close_position(pos['ticker'])
            if order is not None:
                _ac = {
                    'atr_trail_multiplier': C.ATR_TRAIL_MULTIPLIER,
                    'late_day_tighten_pct': C.LATE_DAY_TIGHTEN_PCT,
                    'benchmark_corr_widen': C.BENCHMARK_CORR_WIDEN,
                    'benchmark_corr_tighten': C.BENCHMARK_CORR_TIGHTEN,
                    'max_holding_days': C.MAX_HOLDING_DAYS,
                }
                pnl = db.close_position(pos['id'], current_price, exit_reason=exit_reason, active_controls=_ac)
                if exit_reason == "PULSE_EXIT":
                    flag_info = next((f for f in urgent_flags
                                      if f['ticker'] == pos['ticker']), {})
                    db.acknowledge_urgent_flag(flag_info.get('id'))
                    try:
                        sig = db.get_signal_by_id(pos.get('signal_id'))
                        if sig and sig.get('politician'):
                            db.update_member_weight_after_trade(sig['politician'], pnl)
                    except Exception as _e:
                        # R10-13 — best-effort politician accounting.
                        log.debug(f"update_member_weight_after_trade (pulse-exit) failed: {_e}")
                    send_protective_exit_email(
                        ticker=pos['ticker'], reason=exit_reason,
                        reasoning="Cascade signal detected. Exit triggered per pre-authorized ruleset.",
                        entry_price=pos['entry_price'], exit_price=current_price,
                        shares=pos['shares'], pnl_dollar=pnl,
                    )
                db.log_event(exit_reason, agent="Trade Logic",
                             details=f"{pos['ticker']} exit=${current_price:.2f} pnl=${pnl:+.2f}")
                log.info(f"Exit complete: {pos['ticker']} reason={exit_reason} P&L=${pnl:+.2f}")
                _exit_sign = '+' if pnl >= 0 else ''
                db.add_notification('trade', f'Sold {pos["ticker"]}',
                    f'Exit @ ${current_price:.2f} — P&L {_exit_sign}${pnl:.2f} ({exit_reason.replace("_"," ").lower()})',
                    meta={'ticker': pos['ticker'], 'side': 'sell', 'pnl': round(pnl, 2), 'reason': exit_reason})
                trade_events += 1
            pos_log.commit(db)

    return trade_events


def _run_managed_mode_approvals(db, alpaca, session_log):
    """Execute user-approved trades in managed/supervised mode. Returns trade_events count."""
    trade_events = 0

    # ── MANAGED MODE: execute user-approved trades
    if _is_supervised():
        approved = get_approved_trades()
        for approval in approved:
            try:
                ticker    = approval['ticker']
                shares    = float(approval['shares'])
                price     = float(approval['price'])
                trail_amt = float(approval['trail_amt'])
                trail_pct = float(approval['trail_pct'])
                vol_label = approval['vol_label']
                sig_id    = approval['id']
                order = alpaca.submit_order(ticker=ticker, qty=shares, side="buy")
                if order:
                    _appr_sig = db.get_signal_by_id(sig_id) or {}
                    db.open_position(ticker=ticker, company=approval.get('company'),
                                     sector=approval.get('sector'), entry_price=price,
                                     shares=shares, trail_stop_amt=trail_amt,
                                     trail_stop_pct=trail_pct, vol_bucket=vol_label,
                                     signal_id=sig_id,
                                     entry_signal_score=_appr_sig.get('entry_signal_score',
                                                                       approval.get('confidence')),
                                     entry_sentiment_score=_appr_sig.get('sentiment_score'),
                                     interrogation_status=_appr_sig.get('interrogation_status'))
                    alpaca.submit_order(ticker=ticker, qty=shares, side="sell",
                                        order_type="trailing_stop", trail_price=trail_amt)
                    _shared_db().acknowledge_signal(sig_id)
                    mark_approval_executed(sig_id)
                    log.info(f"[MANAGED] Executed: BUY {shares:.4f} {ticker} @ ${price:.2f}")
                    _cost = round(price * shares, 2)
                    db.add_notification('trade', f'Bought {ticker}',
                        f'{shares:.2f} shares @ ${price:.2f} — ${_cost:.2f} invested',
                        meta={'ticker': ticker, 'side': 'buy', 'shares': round(shares, 4), 'price': round(price, 2)})
                    trade_events += 1
                else:
                    log.error(f"[MANAGED] Order failed: {ticker}")
            except Exception as e:
                log.error(f"[MANAGED] Execution error: {e}")

    return trade_events


def _run_signal_evaluation(db, alpaca, regime, session_log, now, session):
    """Gates 4–9+11: new signal evaluation including rotation check. Returns trade_events count."""
    trade_events = 0

    # ── NEW SIGNAL EVALUATION (Gates 4–9 + 11)
    # Gate 0 already verified at top of run() — proceed to new signal eval.
    positions = db.get_open_positions()
    portfolio = db.get_portfolio()
    equity    = float(db.get_setting('_ALPACA_EQUITY') or portfolio.get('cash', 0))
    tier      = get_portfolio_tier(equity)
    deployed  = sum(p['entry_price'] * p['shares'] for p in positions if p['ticker'] != C.BIL_TICKER)
    tradeable = equity * C.TRADEABLE_PCT
    deployed_pct = deployed / tradeable if tradeable > 0 else 0

    can_enter = (
        deployed_pct < tier["max_deployed"] and
        len([p for p in positions if p['ticker'] != C.BIL_TICKER]) < tier["max_positions"]
    )

    if not can_enter:
        log.info("Deployment/position cap reached — checking for position rotation")
        _rot_count = _rotate_positions(db, _shared_db(), alpaca, positions, regime, tier,
                                       portfolio, tradeable, session, now)
        trade_events += int(_rot_count or 0)

    if can_enter:
        # Expire stale signals on different windows per status:
        #   QUEUED     — 72h (never made it through validation; keep
        #                around long enough for a slow agent to catch up)
        #   VALIDATED  — 12h (news-driven trade signals decay in hours;
        #                anything validated but unacted after 12h is
        #                noise, and letting it linger pushes the cap's
        #                quota against younger, still-actionable signals)
        try:
            with _shared_db().conn() as _c:
                _q_expired = _c.execute(
                    "UPDATE signals SET status='EXPIRED', updated_at=datetime('now') "
                    "WHERE status='QUEUED' "
                    "AND created_at < datetime('now', '-3 days')"
                ).rowcount
                _v_expired = _c.execute(
                    "UPDATE signals SET status='EXPIRED', updated_at=datetime('now') "
                    "WHERE status IN ('VALIDATED', 'WATCHING') "
                    "AND created_at < datetime('now', '-12 hours')"
                ).rowcount
                if _q_expired or _v_expired:
                    log.info(
                        f"[SIGNALS] Expired {_q_expired} QUEUED (>72h) + "
                        f"{_v_expired} VALIDATED/WATCHING (>12h) stale signal(s)"
                    )
        except Exception:
            pass

        # Signal-source: pull VALIDATED signals directly from the master DB
        # with tier-weighted quotas applied. Replaces the 2026 Phase 3c.b
        # window-driven pre-filter that was removed 2026-04-24 — the
        # trade_windows pre-filter was redundant with Gate 6 chase caps
        # (which log every block with reason, whereas the window filter
        # silently dropped out-of-band signals). Tier-quota capping lives
        # inside get_validated_signals(); no-ops when pool is small.
        signals = _shared_db().get_validated_signals()

        # Live prices map — still needed downstream in the gate chain for
        # current_price lookups and logging. Kept as a one-shot pull so we
        # don't run a per-signal live_prices query inside the loop.
        _price_map = {}
        try:
            with _shared_db().conn() as _c:
                _pr = _c.execute(
                    "SELECT ticker, price FROM live_prices WHERE price IS NOT NULL"
                ).fetchall()
                _price_map = {r['ticker']: float(r['price']) for r in _pr if r['price']}
        except Exception as _e:
            log.warning(f"live_prices read failed: {_e}")

        # Drop signals with no live price — gate chain can't evaluate them.
        _no_price = sum(1 for s in signals if _price_map.get(s.get('ticker')) is None)
        signals = [s for s in signals if _price_map.get(s.get('ticker')) is not None]

        _set_phase('signal_evaluation', f"{len(signals)} signals to evaluate")
        log.info(
            f"[SIGNAL_POOL] {len(signals)} VALIDATED signal(s) ready "
            f"for gate chain | skipped {_no_price} with no live price"
        )

        for signal in signals:
            if budget_exceeded():
                log.warning(
                    f"[EVAL] Runtime budget exceeded — stopping signal evaluation "
                    f"(processed before ticker={signal.get('ticker')})"
                )
                break
            _set_phase('signal_evaluation', f"ticker={signal.get('ticker')}")
            positions = db.get_open_positions()
            deployed  = sum(p['entry_price'] * p['shares'] for p in positions if p['ticker'] != C.BIL_TICKER)
            deployed_pct = deployed / tradeable if tradeable > 0 else 0

            if (deployed_pct >= tier["max_deployed"] or
                    len([p for p in positions if p['ticker'] != C.BIL_TICKER]) >= tier["max_positions"]):
                log.info("Deployment cap reached mid-session — stopping")
                break

            # Late-day conservatism: after CONSERVATIVE_AFTER_HOUR, only HIGH confidence
            _mtr = get_market_time_regime(now)
            if _mtr['is_late_day'] and C.CLOSE_SESSION_MODE == 'conservative':
                if (signal.get('confidence', 'LOW') or 'LOW').upper() != 'HIGH':
                    log.info(f"Signal {signal['ticker']} skipped — late-day conservative (after {C.CONSERVATIVE_AFTER_HOUR}:00 ET)")
                    continue

            # Spousal filter (KEEP from v1.x)
            if signal.get('is_spousal') and C.SPOUSAL_WEIGHT == 'skip':
                log.info(f"Signal {signal['ticker']} skipped — spousal (SPOUSAL_WEIGHT=skip)")
                continue

            # Sticky USER preference — user has marked this ticker "never auto".
            # Bot respects this across all signals for the ticker, regardless of
            # whether they currently hold a position.
            _sticky = db.get_ticker_sticky(signal['ticker'])
            if _sticky == 'user':
                log.info(f"Signal {signal['ticker']} skipped — sticky USER preference")
                db.log_event("SIGNAL_SKIPPED_STICKY_USER", agent="Trade Logic",
                             details=f"ticker={signal['ticker']} signal_id={signal.get('id')}")
                try:
                    db.log_signal_decision(
                        agent='trade_logic', action='SKIP_STICKY_USER',
                        ticker=signal['ticker'], signal_id=signal.get('id'),
                        reason='ticker has sticky=user preference',
                    )
                except Exception:
                    pass
                _mark_signal_evaluated(signal['id'], 'SKIP_STICKY_USER')
                continue

            sig_log = TradeDecisionLog(session=session, ticker=signal['ticker'],
                                       signal_id=(signal.get('id') if _CUSTOMER_ID == _OWNER_CID else None))
            sig_log.note(f"politician={signal.get('politician','?')} "
                         f"staleness={signal.get('staleness','?')} "
                         f"headline={signal.get('headline','')[:60]}")

            # Gate 4: Eligibility
            if not gate4_eligibility(signal, positions, alpaca, sig_log):
                sig_log.decide("SKIP", "failed eligibility gate")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'SKIP_ELIGIBILITY')
                continue

            # Gate 5: Signal score
            score = gate5_signal_score(signal, positions, alpaca, sig_log)
            if score == 0.0:
                sig_log.decide("SKIP", f"score below threshold {C.MIN_CONFIDENCE_SCORE}")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'SKIP_SCORE')
                continue

            # Gate 5.5: Severe-negative news veto (Phase 3a)
            if not gate5_5_news_veto(signal, sig_log):
                sig_log.decide("SKIP", "severe-negative news veto")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'SKIP_NEWS_VETO')
                continue

            # Gate 6: Entry decision
            candidate = gate6_entry(signal, score, regime, alpaca, sig_log)
            if candidate is None:
                sig_log.decide("WATCH", "no entry condition met — signal retained in queue")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'NO_ENTRY')
                continue

            # Get ATR for sizing and risk. Fresh Alpaca daily bars each
            # call. The prior Phase 4.d optimization that read ATR from
            # the macro window was removed with window_calculator on
            # 2026-04-24 — the per-signal HTTP cost is negligible
            # (cached by AlpacaClient) and fresh ATR is more accurate.
            atr = alpaca.get_atr(signal['ticker'])
            if not atr:
                atr = candidate['price'] * 0.02

            # Gate 7: Position sizing (Model B + Model C cash guard)
            size = gate7_sizing(candidate, regime, portfolio, positions, atr, sig_log, db=db)
            if size <= 0:
                # Insufficient cash — skip; gate7_sizing already logged reason.
                sig_log.decide("SKIP", "insufficient cash after manual/auto allocation")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'SKIP_INSUFFICIENT_CASH_AFTER_MANUAL')
                continue

            # Gate 8: Risk setup
            risk = gate8_risk(candidate, atr, session, sig_log)

            # Gate 11: Portfolio controls
            if not gate11_portfolio(positions, portfolio, signal, size, alpaca, sig_log, db=db):
                sig_log.decide("SKIP", "portfolio limits block entry")
                sig_log.commit(db)
                _mark_signal_evaluated(signal['id'], 'SKIP_PORTFOLIO')
                continue

            # Entry approved — MIRROR
            sig_log.decide("MIRROR", f"{candidate['type']} entry | "
                           f"{size:.4f} shares @ ${candidate['price']:.2f} | "
                           f"stop=${risk['stop_loss']} target=${risk['profit_target']}")

            trail_amt, trail_pct, vol_label = calculate_trail_stop(
                atr, candidate['price'], signal.get('sector', ''))

            decision_data = {
                "price":     candidate['price'],
                "shares":    size,
                "max_trade": round(size * candidate['price'], 2),
                "trail_amt": trail_amt,
                "trail_pct": trail_pct,
                "vol_label": vol_label,
                "reasoning": (
                    f"Entry type: {candidate['type']} | Score: {score:.4f} | "
                    f"Anchor: {candidate.get('anchor_type','?')} ${candidate.get('anchor_price',0):.2f} "
                    f"(chase {candidate.get('chase_pct',0)*100:+.2f}%) | "
                    f"Mode: {regime.mode} | Regime: {regime.trend}/{regime.volatility}"
                ),
                "session":   session,
            }

            if _is_supervised():
                queue_for_approval(signal, decision_data)
                # Acknowledge the signal so it leaves the QUEUED pool and is not
                # re-processed on the next session run. The approval row in
                # pending_approvals is the source of truth until user decides.
                _shared_db().acknowledge_signal(signal['id'])
                log.info(f"[MANAGED] {signal['ticker']} queued for portal approval")
            else:
                # AUTOMATIC MODE ⚠️ UNDER REVIEW — live trading not yet authorized
                order = alpaca.submit_order(signal['ticker'], size, "buy")
                if order:
                    db.open_position(
                        ticker=signal['ticker'], company=signal.get('company'),
                        sector=signal.get('sector'), entry_price=candidate['price'],
                        shares=size, trail_stop_amt=trail_amt,
                        trail_stop_pct=trail_pct, vol_bucket=vol_label,
                        signal_id=(signal['id'] if _CUSTOMER_ID == _OWNER_CID else None),
                        entry_signal_score=round(float(score), 4),
                        entry_sentiment_score=signal.get('sentiment_score'),
                        interrogation_status=signal.get('interrogation_status'),
                    )
                    alpaca.submit_order(signal['ticker'], size, "sell",
                                        order_type="trailing_stop", trail_price=trail_amt)
                    _shared_db().acknowledge_signal(signal['id'])
                    log.info(f"TRADE EXECUTED: BUY {size:.4f} {signal['ticker']} "
                             f"@ ${candidate['price']:.2f} | stop ${trail_amt:.2f}")
                    _cost = round(candidate['price'] * size, 2)
                    db.add_notification('trade', f'Bought {signal["ticker"]}',
                        f'{size:.2f} shares @ ${candidate["price"]:.2f} — ${_cost:.2f} invested',
                        meta={'ticker': signal['ticker'], 'side': 'buy', 'shares': round(size, 4), 'price': round(candidate['price'], 2)})
                    trade_events += 1
                else:
                    log.error(f"Order failed: {signal['ticker']}")

            sig_log.commit(db)

    return trade_events


def _run_monthly_tax_sweep(db, now):
    """Monthly tax sweep — runs once on last trading day after 3pm."""
    if is_last_trading_day_of_month() and now.hour >= 15:
        today_str = now.strftime('%Y-%m-%d')
        if not db.has_event_today('TAX_SWEEP', today_str):
            portfolio = db.get_portfolio()
            positions = db.get_open_positions()
            unrealized  = sum(p.get('pnl', 0) for p in positions)
            # R10-3 — defensive .get() to match the Round 9 pattern applied
            # elsewhere; a corrupt/partial portfolio row would KeyError here.
            total_gains = float(portfolio.get('realized_gains') or 0) + unrealized
            if total_gains > 0:
                tax = round(total_gains * C.GAIN_TAX_PCT, 2)
                log.info(f"Month-end tax sweep: ${tax:.2f}")
                db.sweep_monthly_tax(tax)
                db.log_event("TAX_SWEEP", agent="Trade Logic",
                             details=f"monthly sweep ${tax:.2f}")


def _send_session_summary(db, trade_events, session_log, now, session):
    """Log session complete, write heartbeat, add notification. Returns (total_value, positions)."""
    portfolio   = db.get_portfolio()
    positions   = db.get_open_positions()
    _cash       = float(portfolio.get('cash') or 0)
    total_value = _cash + sum(float(p.get('entry_price', 0)) * float(p.get('shares', 0)) for p in positions)
    log.info(f"Session complete — portfolio=${total_value:.2f} "
             f"positions={len(positions)} cash=${_cash:.2f}")
    db.log_heartbeat("trade_logic_agent", "OK", portfolio_value=total_value)
    db.log_event("AGENT_COMPLETE", agent="Trade Logic",
                 details=f"session={session} positions={len(positions)}",
                 portfolio_value=total_value)
    # Only notify on session end if there was something actionable to report.
    # Routine "nothing happened" sessions stay silent to avoid Notification
    # Center spam. When we do notify, title reflects the actual trade count.
    if trade_events > 0:
        _title = f"{trade_events} trade{'s' if trade_events != 1 else ''} this session"
        db.add_notification('daily', _title,
            f'{session.title()} session: {len(positions)} positions, portfolio ${total_value:,.2f}',
            meta={'session': session, 'positions': len(positions),
                  'portfolio': round(total_value, 2), 'trade_events': trade_events})

    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="trade_logic_agent", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")

    return total_value, positions


def _send_daily_report(db, session, now, total_value, positions):
    """POST daily report to monitor — runs once per day after 4pm."""
    _mtr_report = get_market_time_regime(now)
    today_str_rpt = now.strftime('%Y-%m-%d')
    if _mtr_report['hour'] >= 16 and not db.has_event_today('DAILY_REPORT', today_str_rpt):
        try:
            monitor_url   = os.environ.get('MONITOR_URL', '')
            monitor_token = os.environ.get('MONITOR_TOKEN', '')
            pi_id         = os.environ.get('PI_ID', 'synthos-pi')
            if monitor_url:
                outcomes_today = db.get_recent_outcomes(limit=20)
                today_str = now.strftime('%Y-%m-%d')
                today_out = [o for o in outcomes_today
                             if o.get('created_at', '').startswith(today_str)]
                wins     = sum(1 for o in today_out if o.get('verdict') == 'WIN')
                losses   = sum(1 for o in today_out if o.get('verdict') == 'LOSS')
                realized = round(sum(o.get('pnl_dollar', 0) for o in today_out), 2)
                requests.post(
                    f"{monitor_url.rstrip('/')}/report",
                    json={"pi_id": pi_id, "date": today_str,
                          "portfolio_value": round(total_value, 2),
                          "realized_pnl": realized, "open_positions": len(positions),
                          "trades_today": len(today_out), "wins": wins, "losses": losses,
                          "summary": f"{len(today_out)} trades — {wins}W/{losses}L — ${total_value:.2f}"},
                    headers={"X-Token": monitor_token}, timeout=10,
                )
                db.log_event("DAILY_REPORT", agent="Trade Logic", details=f"sent to {monitor_url}")
        except Exception as e:
            log.warning(f"Daily report POST failed: {e}")


def run(session='open'):
    _run_halt_check()
    _wd = _start_watchdog()
    db, alpaca, now, session_log = _init_clients(session)
    account, equity, cash, positions = _run_gate0_account_health(db, alpaca, session_log)
    if account is None:
        return
    regime = _run_market_gates(db, alpaca, positions, session_log)
    trade_events  = _run_position_management(db, alpaca, regime, session_log, now, session)
    trade_events += _run_managed_mode_approvals(db, alpaca, session_log)
    trade_events += _run_signal_evaluation(db, alpaca, regime, session_log, now, session)
    _run_monthly_tax_sweep(db, now)
    total_value, positions = _send_session_summary(db, trade_events, session_log, now, session)
    _send_daily_report(db, session, now, total_value, positions)
    _wd.join(timeout=0)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — ExecutionAgent (Agent 1)')
    parser.add_argument('--session', choices=['open', 'midday', 'close', 'hourly'], default='hourly')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID — routes DB and Alpaca credentials to per-customer sources')
    parser.add_argument('--dry-run', action='store_true',
                        help='Force MANAGED mode — trades queue as pending_approvals instead '
                             'of executing. Used by retail_dry_run.py so pipeline tests do '
                             'not submit real paper orders on AUTOMATIC customers.')
    args = parser.parse_args()

    # ── Multi-tenant: load per-customer credentials if --customer-id is given ──
    if args.customer_id:
        _CUSTOMER_ID = args.customer_id
        try:
            import auth as _auth
            _ak, _sk = _auth.get_alpaca_credentials(args.customer_id)
            if not _ak:
                # Gate 0: no Alpaca key → skip this customer entirely
                log.info(f"Gate 0 SKIP: customer {args.customer_id[:8]} has no Alpaca key — cannot trade")
                sys.exit(0)
            ALPACA_API_KEY = _ak
            ALPACA_SECRET_KEY  = _sk
            OPERATING_MODE = _auth.get_operating_mode(args.customer_id)
            _cust_trading_mode = _auth.get_trading_mode(args.customer_id)
            if _cust_trading_mode in ('PAPER', 'LIVE'):
                TRADING_MODE = _cust_trading_mode
                if TRADING_MODE == 'LIVE':
                    ALPACA_BASE_URL = 'https://api.alpaca.markets'
                else:
                    ALPACA_BASE_URL = 'https://paper-api.alpaca.markets'
            log.info(f"Multi-tenant mode: customer={args.customer_id} operating={OPERATING_MODE} trading={TRADING_MODE}")
        except SystemExit:
            raise  # let exit(0) from gate above propagate
        except Exception as _e:
            log.warning(f"Could not load customer credentials from auth.db: {_e}")
            sys.exit(1)  # fail closed — do not fall back to global key
        # Apply per-customer trading parameters from customer_settings DB
        _apply_customer_settings()

    # ── Dry-run: force MANAGED so trades queue for approval, not execute ──
    # Applied AFTER _apply_customer_settings() so it overrides the customer's
    # configured mode. Trades will be written to pending_approvals for the
    # admin to inspect/approve, instead of being submitted to Alpaca.
    if args.dry_run:
        if OPERATING_MODE != 'MANAGED':
            log.info(f"--dry-run: overriding OPERATING_MODE {OPERATING_MODE} → MANAGED "
                     f"(trades will queue for approval, no paper orders submitted)")
        OPERATING_MODE = 'MANAGED'

    if not ALPACA_API_KEY:
        log.error("ALPACA_API_KEY not set — check .env or provide --customer-id with stored credentials")
        sys.exit(1)

    acquire_agent_lock("retail_trade_logic_agent.py")
    try:
        run(session=args.session)
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Signal the runtime watchdog to exit so the process terminates
        # cleanly — otherwise the daemon thread would keep the interpreter
        # alive until its next wake (up to 15s) and subprocess retirement
        # could stall the dispatch pool.
        _WATCHDOG_STOP.set()
        release_agent_lock()
