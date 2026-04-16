"""
trade_logic_agent.py — Trade Logic Agent (ExecutionAgent)
Synthos · Agent 1 · Version 2.0

Runs hourly 24/7 weekdays via retail_scheduler.py --session trade.
Accepts --session (open/midday/close/hourly) for backward compatibility;
actual behavior is driven by time of day (ET).

Decision architecture: 14-gate deterministic control spine.
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
from datetime import datetime, timedelta, date
from calendar import monthrange
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, acquire_agent_lock, release_agent_lock

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
MAX_RETRIES       = 3

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
    # TODO: DATA_DEPENDENCY — EVENT_CALENDAR requires FOMC/CPI/earnings API

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

    # Execution (Gate 9)
    SLIPPAGE_TOLERANCE        = float(os.environ.get('SLIPPAGE_TOLERANCE', '0.002'))

    # Portfolio (Gate 11)
    MAX_DAILY_LOSS            = float(os.environ.get('MAX_DAILY_LOSS', '-500.0'))
    MAX_DRAWDOWN_PCT          = float(os.environ.get('MAX_DRAWDOWN_PCT', '0.15'))
    MAX_GROSS_EXPOSURE        = float(os.environ.get('MAX_GROSS_EXPOSURE', '0.80'))
    MAX_SECTOR_PCT            = float(os.environ.get('MAX_SECTOR_PCT', '0.25'))
    MAX_POSITIONS             = int(os.environ.get('MAX_POSITIONS', '10'))
    MAX_LEVERAGE              = float(os.environ.get('MAX_LEVERAGE', '1.0'))

    # Adaptive (Gate 12)
    MIN_SHARPE_THRESHOLD      = float(os.environ.get('MIN_SHARPE_THRESHOLD', '0.5'))
    PERFORMANCE_WINDOW_DAYS   = int(os.environ.get('PERFORMANCE_WINDOW_DAYS', '30'))

    # Stress (Gate 13)
    FLASH_CRASH_PCT           = float(os.environ.get('FLASH_CRASH_PCT', '0.03'))
    FLASH_CRASH_MINUTES       = int(os.environ.get('FLASH_CRASH_MINUTES', '10'))
    BENCHMARK_CRASH_PCT       = float(os.environ.get('BENCHMARK_CRASH_PCT', '0.05'))

    # Session timing
    CONSERVATIVE_AFTER_HOUR   = int(os.environ.get('CONSERVATIVE_AFTER_HOUR', '15'))
    LATE_DAY_TIGHTEN_PCT      = float(os.environ.get('LATE_DAY_TIGHTEN_PCT', '0.25'))

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
            'MAX_POSITIONS':        ('MAX_POSITIONS',          int),
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
    {"threshold": 0,      "max_deployed": 0.30, "max_positions": 3,  "label": "Seed"   },
    {"threshold": 1000,   "max_deployed": 0.35, "max_positions": 5,  "label": "Early"  },
    {"threshold": 5000,   "max_deployed": 0.40, "max_positions": 8,  "label": "Growth" },
    {"threshold": 20000,  "max_deployed": 0.45, "max_positions": 10, "label": "Scaled" },
    {"threshold": 50000,  "max_deployed": 0.50, "max_positions": 12, "label": "Mature" },
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

def kill_switch_active():
    return os.path.exists(KILL_SWITCH_FILE)

def clear_kill_switch():
    try:
        if os.path.exists(KILL_SWITCH_FILE) or getattr(C, "_customer_kill_switch", False):
            os.remove(KILL_SWITCH_FILE)
            log.info("Kill switch cleared")
    except Exception as e:
        log.error(f"Could not clear kill switch: {e}")


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

    def prefetch_bars(self, tickers, days=70):
        """Batch-fetch daily bars for multiple tickers in one API call.
        Populates _bar_cache so subsequent get_bars() calls are instant.
        Uses Alpaca multi-symbol endpoint: /v2/stocks/bars?symbols=A,B,C"""
        if not tickers:
            return
        unique = sorted(set(t.upper() for t in tickers))
        end   = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (datetime.now(ET) - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        # Alpaca allows up to ~100 symbols per request; chunk if needed
        CHUNK = 50
        for i in range(0, len(unique), CHUNK):
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
                    headers=headers, timeout=15,
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
                else:
                    log.warning(f"[PREFETCH] Alpaca returned {r.status_code}")
            except Exception as e:
                log.warning(f"[PREFETCH] Failed: {e}")

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}{endpoint}"
        last_error = None
        status_code = None
        for attempt in range(MAX_RETRIES):
            try:
                r = getattr(requests, method)(
                    url, headers=self.headers, timeout=15, **kwargs
                )
                status_code = r.status_code
                r.raise_for_status()
                # Track API call
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
        # Track failed call too
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
            if cached_t == t_upper and cached_d >= days and bars:
                return bars[-days:] if len(bars) > days else bars
        # Cache miss — fetch individually
        end   = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (datetime.now(ET) - timedelta(days=days + 5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        try:
            r = requests.get(
                f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars",
                params={"timeframe": "1Day", "start": start, "end": end,
                        "limit": days + 10, "feed": "iex"},
                headers=headers, timeout=10,
            )
            try:
                _shared_db().log_api_call(
                    agent='trade_logic', endpoint=f'/v2/stocks/{ticker}/bars',
                    method='GET', service='alpaca_data',
                    customer_id=_CUSTOMER_ID, status_code=r.status_code)
            except Exception:
                pass
            if r.status_code == 200:
                bars = r.json().get("bars") or []
                # Cache for reuse within this run
                self._bar_cache[(t_upper, days)] = bars
                return bars
        except Exception as e:
            log.warning(f"get_bars({ticker}): {e}")
        return []

    def get_atr(self, ticker, period=14):
        bars = self.get_bars(ticker, days=period + 10)
        if len(bars) < 2:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return round(sum(trs[-period:]) / min(len(trs), period), 2) if trs else None

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
            return 0
        vols = [b["v"] for b in bars[-days:]]
        return int(sum(vols) / len(vols)) if vols else 0

    def submit_order(self, ticker, qty, side, order_type="market",
                     trail_price=None, trail_percent=None):
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
        url = f"{self.base_url}/v2/positions/{ticker}"
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            try:
                _shared_db().log_api_call(
                    agent='trade_logic', endpoint=f'/v2/positions/{ticker}',
                    method='GET', service='alpaca',
                    customer_id=_CUSTOMER_ID, status_code=r.status_code)
            except Exception:
                pass
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"get_position_safe({ticker}): {e}")
            return None

    def _submit_notional(self, ticker, notional, side):
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

    # Kill switch — global file OR per-customer DB flag
    _global_kill = kill_switch_active()
    _customer_kill = getattr(C, '_customer_kill_switch', False)
    if _global_kill or _customer_kill:
        source = "global file" if _global_kill else "per-customer setting"
        decision_log.gate("1_KILL_SWITCH", False, {"source": source}, f"kill switch active ({source})")
        decision_log.decide("HALT", f"Kill switch active ({source})")
        return False

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
    ticker = signal['ticker']
    dlog = TradeDecisionLog(decision_log.session, ticker, signal.get('id'))

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

    # Event risk
    # TODO: DATA_DEPENDENCY — automated FOMC/CPI/earnings calendar not yet integrated
    # Currently: manual exclusion only — flagged for future implementation
    decision_log.gate("4_EVENT_RISK", "NOT_CHECKED", {
        "TODO": "EVENT_CALENDAR not yet integrated",
    }, "event risk check skipped — TODO: DATA_DEPENDENCY")

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

    return True


# ── GATE 5 — SIGNAL EVALUATION ───────────────────────────────────────────────

def gate5_signal_score(signal: dict, positions: list, alpaca,
                       decision_log: TradeDecisionLog) -> float:
    """
    Compute composite confidence score. Returns score in [0, 1].
    Logic: Doc 3 §5
    """
    ticker = signal['ticker']

    # Component scores
    tier_score   = max(0.0, 1.0 - (int(signal.get('source_tier', 2) or 2) - 1) * 0.3)
    pol_weight   = float(signal.get('politician_weight') or 0.5)
    stale_score  = staleness_to_score(signal.get('staleness', 'Fresh'))
    interr_score = interrogation_to_score(signal.get('interrogation_status', 'UNVALIDATED'))
    conf_score   = confidence_to_score(signal.get('confidence', 'MEDIUM'))

    # Sentiment from corroboration_note (written by agent3)
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

    passes = final_score >= C.MIN_CONFIDENCE_SCORE

    decision_log.gate("5_SIGNAL_SCORE", f"{final_score:.4f}", {
        "ticker":             ticker,
        "tier_score":         f"{tier_score:.2f} × {W['source_tier']}",
        "politician_weight":  f"{pol_weight:.2f} × {W['politician_weight']}",
        "staleness_score":    f"{stale_score:.2f} × {W['staleness']}",
        "interrogation_score":f"{interr_score:.2f} × {W['interrogation']}",
        "sentiment_score":    f"{sentiment_score:.2f} × {W['sentiment']}",
        "screening_adj":      f"{screening_adj:+.2f} ({screening_info})",
        "composite_score":    f"{final_score:.4f}",
        "rel_strength_5d":    f"{rel_str*100:.2f}%" if rel_str is not None else "N/A",
        "threshold":          f"{C.MIN_CONFIDENCE_SCORE:.2f}",
        "result":             "PASS" if passes else "SKIP",
    }, f"score {final_score:.4f} {'≥' if passes else '<'} threshold {C.MIN_CONFIDENCE_SCORE}")

    return final_score if passes else 0.0


# ── GATE 6 — ENTRY DECISION ──────────────────────────────────────────────────

def gate6_entry(signal: dict, score: float, regime: RegimeState, alpaca,
                decision_log: TradeDecisionLog) -> dict | None:
    """
    Select entry type (momentum / mean-reversion / breakout / pullback).
    Returns candidate dict or None.
    Logic: Doc 3 §6
    """
    ticker  = signal['ticker']
    bars    = alpaca.get_bars(ticker, days=max(C.BREAKOUT_LOOKBACK, 30) + 10)
    if len(bars) < 10:
        decision_log.gate("6_ENTRY", "SKIP", {"reason": "insufficient price data"}, "no price data")
        return None

    closes  = [b["c"] for b in bars]
    current = closes[-1]
    ma20    = sum(closes[-20:]) / 20 if len(closes) >= 20 else current
    roc     = (current - closes[-6]) / closes[-6] if len(closes) >= 6 else 0

    candidates = []

    # Momentum: price > MA AND ROC > threshold
    # Disabled in BEAR regime
    if regime.trend != "BEAR":
        momentum_ok = current > ma20 and roc >= C.MOMENTUM_ROC_THRESHOLD
        if momentum_ok:
            candidates.append({"type": "MOMENTUM", "score": score * 1.0,
                                "detail": f"price ${current:.2f} > MA20 ${ma20:.2f}, ROC {roc*100:.2f}%"})

    # Mean-reversion: z-score(price, mean) < -threshold
    # Only in SIDEWAYS regime
    if regime.trend == "SIDEWAYS" and len(closes) >= 20:
        mean = sum(closes[-20:]) / 20
        std  = (sum((c - mean)**2 for c in closes[-20:]) / 20) ** 0.5
        z    = (current - mean) / std if std > 0 else 0
        if z <= C.MEAN_REV_ZSCORE:
            candidates.append({"type": "MEAN_REVERSION", "score": score * 0.9,
                                "detail": f"z-score {z:.2f} ≤ threshold {C.MEAN_REV_ZSCORE}"})

    # Breakout: price > rolling N-period high
    # Disabled in SIDEWAYS regime
    if regime.trend != "SIDEWAYS" and len(bars) >= C.BREAKOUT_LOOKBACK:
        rolling_high = max(b["h"] for b in bars[-(C.BREAKOUT_LOOKBACK + 1):-1])
        if current > rolling_high:
            candidates.append({"type": "BREAKOUT", "score": score * 0.95,
                                "detail": f"price ${current:.2f} > {C.BREAKOUT_LOOKBACK}d high ${rolling_high:.2f}"})

    # Pullback: retraced X% within uptrend
    if regime.trend == "BULL" and len(closes) >= 10:
        recent_high = max(closes[-10:])
        retrace     = (recent_high - current) / recent_high if recent_high > 0 else 0
        if 0 < retrace <= C.PULLBACK_RETRACE_PCT and current > ma20:
            candidates.append({"type": "PULLBACK", "score": score * 0.92,
                                "detail": f"retraced {retrace*100:.2f}% from ${recent_high:.2f} in uptrend"})

    if not candidates:
        decision_log.gate("6_ENTRY", "WATCH", {
            "ticker":    ticker,
            "price":     f"${current:.2f}",
            "ma20":      f"${ma20:.2f}",
            "roc_5d":    f"{roc*100:.2f}%",
            "regime":    f"{regime.trend}/{regime.volatility}",
            "reason":    "no entry condition met",
        }, "WATCH — no entry condition triggered")
        return None

    best = max(candidates, key=lambda x: x["score"])
    decision_log.gate("6_ENTRY", best["type"], {
        "ticker":           ticker,
        "entry_type":       best["type"],
        "entry_score":      f"{best['score']:.4f}",
        "regime":           f"{regime.trend}/{regime.volatility}",
        "candidates_found": len(candidates),
        "detail":           best["detail"],
    }, f"entry={best['type']} score={best['score']:.4f}")

    return {"ticker": ticker, "type": best["type"],
            "score": best["score"], "price": current}


# ── GATE 7 — POSITION SIZING ─────────────────────────────────────────────────

def gate7_sizing(candidate: dict, regime: RegimeState, portfolio: dict,
                 positions: list, atr: float, decision_log: TradeDecisionLog) -> float:
    """
    Compute final position size in shares.
    Logic: Doc 3 §7
    """
    price    = candidate["price"]
    equity   = portfolio.get("cash", 0)
    tier     = get_portfolio_tier(equity)

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

    # Max cap: size <= max_position_pct * portfolio
    max_shares = (equity * C.MAX_POSITION_PCT) / price if price > 0 else 0
    size       = min(size, max_shares)

    # Hard dollar cap: MAX_TRADE_USD overrides all sizing if set
    if C.MAX_TRADE_USD > 0 and price > 0:
        max_by_usd = C.MAX_TRADE_USD / price
        size = min(size, max_by_usd)

    size       = round(max(size, 0.0001), 4)

    dollar_value = size * price

    decision_log.gate("7_SIZING", f"{size:.4f} shares (${dollar_value:.2f})", {
        "equity":          f"${equity:.2f}",
        "tier":            tier["label"],
        "atr":             f"${atr:.2f}" if atr else "estimated",
        "stop_distance":   f"${stop_dist:.2f}",
        "base_risk":       f"${base_risk:.2f} ({C.BASE_RISK_PER_TRADE*100:.1f}%)",
        "base_size":       f"{base_size:.4f}",
        "vol_adjustment":  f"×{vol_adj:.3f}",
        "mode_adjustment": f"×{C.DEFENSIVE_SIZE_FACTOR if regime.mode=='DEFENSIVE' else C.AGGRESSIVE_SIZE_FACTOR if regime.mode=='AGGRESSIVE' else 1.0:.2f}",
        "drawdown_scale":  f"×{1.0-drawdown:.3f} (dd={drawdown*100:.1f}%)",
        "max_cap":         f"{max_shares:.4f} shares",
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
                     size: float, alpaca, decision_log: TradeDecisionLog) -> bool:
    """
    Enforce portfolio-wide limits before allowing new entry.
    Logic: Doc 3 §11
    """
    equity         = portfolio.get("cash", 0)
    tier           = get_portfolio_tier(equity)

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
            if buy >= C.BIL_REBALANCE_THRESHOLD:
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
    """
    ROTATION_THRESHOLD = 0.20
    MAX_ROTATIONS = 1

    try:
        signals = shared_db.get_queued_signals()
        if not signals:
            return

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
            return

        # Only consider losing positions for rotation
        losers = [h for h in held if h['pnl_pct'] < 0]
        if not losers:
            log.info("[ROTATION] All positions in profit — no rotation candidates")
            return

        losers.sort(key=lambda x: x['entry_score'])
        weakest = losers[0]
        rotations = 0

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

            # ── BUY the stronger signal ──
            atr = alpaca.get_atr(signal['ticker'])
            if not atr:
                atr = candidate['price'] * 0.02

            # Refresh positions list after sell (so sizing doesn't count sold position)
            fresh_positions = db.get_open_positions()

            size = gate7_sizing(candidate, regime, portfolio, fresh_positions, atr, sig_log)
            risk = gate8_risk(candidate, atr, session, sig_log)

            if _is_supervised():
                trail_amt, trail_pct, vol_label = calculate_trail_stop(
                    atr, candidate['price'], signal.get('sector', ''))
                decision_data = {
                    "price": candidate['price'], "shares": size,
                    "max_trade": round(size * candidate['price'], 2),
                    "trail_amt": trail_amt, "trail_pct": trail_pct,
                    "vol_label": vol_label,
                    "reasoning": f"ROTATION: replaces {weakest['ticker']} | Score gap: {score_gap:.3f}",
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
                            entry_signal_score=str(score),
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

def run(session="open"):
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

    # ── GATE 0: Account Health Check
    # Runs every cycle. Syncs with Alpaca (source of truth), self-heals
    # discrepancies, handles customer-initiated trades, skips unfunded accounts.
    account = alpaca.get_account()
    if not account:
        log.warning("[GATE 0] Cannot reach Alpaca account API — skipping this run")
        session_log.gate("0_HEALTH", "SKIP", {}, "Alpaca account unreachable")
        session_log.commit(db)
        db.log_heartbeat("trade_logic_agent", "OK")
        return

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
        return

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
            # Look up sector from screening data or Alpaca position asset_class
            _orphan_sector = ''
            try:
                scr = _shared_db().get_screening_score(t)
                if scr and scr.get('sector'):
                    _orphan_sector = scr['sector']
            except Exception:
                pass
            log.warning(f"[GATE 0] ORPHAN: {t} {shares:.4f}sh @ ${entry:.2f} sector={_orphan_sector or '?'} — auto-adopting")
            db.open_position(
                ticker=t, company=t, sector=_orphan_sector,
                entry_price=entry, shares=shares,
                trail_stop_amt=0, trail_stop_pct=0, vol_bucket='normal',
                signal_id=None, entry_signal_score=None,
                entry_sentiment_score=None, interrogation_status=None,
            )
            db.log_event("ORPHAN_ADOPTED", agent="Trade Logic",
                         details=f"{t} {shares:.4f}sh @ ${entry:.2f} adopted from Alpaca")
            db.add_notification('account', f'{t} position detected',
                f'{shares:.2f} shares @ ${entry:.2f} added to your portfolio',
                meta={'ticker': t, 'type': 'orphan_adopted'})
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

    # Backfill empty sectors on existing positions from screening data
    for pos in db.get_open_positions():
        if not pos.get('sector'):
            try:
                scr = _shared_db().get_screening_score(pos['ticker'])
                if scr and scr.get('sector'):
                    with db.conn() as _c:
                        _c.execute("UPDATE positions SET sector=? WHERE id=?",
                                   (scr['sector'], pos['id']))
                    log.info(f"[GATE 0] Sector backfill: {pos['ticker']} → {scr['sector']}")
            except Exception:
                pass

    # Check for first-run (no history at all) — setup only, don't trade yet
    positions_after = db.get_open_positions()
    portfolio = db.get_portfolio()
    has_history = len(positions_after) > 0 or portfolio.get('realized_gains', 0) != 0
    if not has_history and not orphans:
        log.info(f"[GATE 0] First run — account setup only (equity ${alpaca_equity:.2f})")
        session_log.gate("0_HEALTH", "FIRST_RUN", {
            "equity": alpaca_equity, "cash": alpaca_cash,
        }, "first-run setup — will trade on next cycle")
        session_log.commit(db)
        db.log_event("FIRST_RUN_COMPLETE", agent="Trade Logic",
                     details=f"Account initialized — equity ${alpaca_equity:.2f}")
        db.add_notification('system', 'Account Ready',
            f'Your account has been initialized with ${alpaca_equity:,.2f} equity. Trading begins next session.',
            meta={'type': 'first_run', 'equity': alpaca_equity})
        db.log_heartbeat("trade_logic_agent", "OK")
        return

    session_log.gate("0_HEALTH", "OK", {
        "equity": alpaca_equity, "cash": alpaca_cash,
        "positions_db": len(positions_after), "positions_alpaca": len(alpaca_positions),
        "orphans_adopted": len(orphans), "ghosts_closed": len(ghosts), "healed": healed,
    }, f"health check OK — {len(positions_after)} positions, ${alpaca_equity:.0f} equity")

    # ── PREFETCH: Batch-load bars for all tickers we'll need this run
    # One multi-symbol API call replaces dozens of individual get_bars() calls
    _prefetch_tickers = set()
    _prefetch_tickers.add(C.BENCHMARK_SYMBOL)  # SPY — used in Gates 2,3,5,10,13
    _prefetch_tickers.add('TLT')               # Gate 3 bond proxy
    _prefetch_tickers.add('BIL')               # BIL reserve
    for p in positions_after:
        _prefetch_tickers.add(p['ticker'])      # All held positions
    # Collect signal tickers from shared DB
    try:
        _sig_tickers = _shared_db().get_queued_signals()
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

    portfolio = db.get_portfolio()
    positions = db.get_open_positions()
    _equity   = float(db.get_setting('_ALPACA_EQUITY') or portfolio['cash'])
    tier      = get_portfolio_tier(_equity)

    # ── GATE 10: Active trade management (every session — open positions)
    urgent_flags    = db.get_urgent_flags()
    urgent_tickers  = {f['ticker'] for f in urgent_flags}

    # Cache SPY bars for benchmark-relative checks (one API call for all positions)
    _spy_bars_cache = alpaca.get_bars(C.BENCHMARK_SYMBOL, days=30)

    for pos in positions:
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

            if current_price <= effective_stop:
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
                if True:  # Gate 0 already verified account health
                    order = alpaca.submit_order(pos['ticker'], sell_shares, "sell")
                    if order:
                        pnl = db.reduce_position(pos['id'], sell_shares, current_price,
                                                  exit_reason="PROFIT_TAKE")
                        db.update_profit_tier(pos['id'], rule['gain_pct'])
                        try:
                            sig = db.get_signal_by_id(pos.get('signal_id'))
                            if sig and sig.get('politician'):
                                db.update_member_weight_after_trade(sig['politician'], pnl)
                        except Exception:
                            pass
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
            if True:  # Gate 0 already verified account health
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
                        except Exception:
                            pass
                        send_protective_exit_email(
                            ticker=pos['ticker'], reason=exit_reason,
                            reasoning=f"Cascade signal detected. Exit triggered per pre-authorized ruleset.",
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
            pos_log.commit(db)

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
                else:
                    log.error(f"[MANAGED] Order failed: {ticker}")
            except Exception as e:
                log.error(f"[MANAGED] Execution error: {e}")

    # ── NEW SIGNAL EVALUATION (Gates 4–9 + 11)
    if True:  # Gate 0 already verified — always proceed
        positions = db.get_open_positions()
        portfolio = db.get_portfolio()
        equity    = float(db.get_setting('_ALPACA_EQUITY') or portfolio['cash'])
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
            _rotate_positions(db, _shared_db(), alpaca, positions, regime, tier,
                              portfolio, tradeable, session, now)

        if can_enter:
            signals = _shared_db().get_queued_signals()
            log.info(f"Evaluating {len(signals)} queued signal(s)")

            for signal in signals:
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

                sig_log = TradeDecisionLog(session=session, ticker=signal['ticker'],
                                           signal_id=(signal.get('id') if _CUSTOMER_ID == _OWNER_CID else None))
                sig_log.note(f"politician={signal.get('politician','?')} "
                             f"staleness={signal.get('staleness','?')} "
                             f"headline={signal.get('headline','')[:60]}")

                # Gate 4: Eligibility
                if not gate4_eligibility(signal, positions, alpaca, sig_log):
                    sig_log.decide("SKIP", "failed eligibility gate")
                    sig_log.commit(db)
                    continue

                # Gate 5: Signal score
                score = gate5_signal_score(signal, positions, alpaca, sig_log)
                if score == 0.0:
                    sig_log.decide("SKIP", f"score below threshold {C.MIN_CONFIDENCE_SCORE}")
                    sig_log.commit(db)
                    continue

                # Gate 6: Entry decision
                candidate = gate6_entry(signal, score, regime, alpaca, sig_log)
                if candidate is None:
                    sig_log.decide("WATCH", "no entry condition met — signal retained in queue")
                    sig_log.commit(db)
                    continue

                # Get ATR for sizing and risk
                atr = alpaca.get_atr(signal['ticker'])
                if not atr:
                    atr = candidate['price'] * 0.02

                # Gate 7: Position sizing
                size = gate7_sizing(candidate, regime, portfolio, positions, atr, sig_log)

                # Gate 8: Risk setup
                risk = gate8_risk(candidate, atr, session, sig_log)

                # Gate 11: Portfolio controls
                if not gate11_portfolio(positions, portfolio, signal, size, alpaca, sig_log):
                    sig_log.decide("SKIP", "portfolio limits block entry")
                    sig_log.commit(db)
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
                    "reasoning": f"Entry type: {candidate['type']} | Score: {score:.4f} | "
                                 f"Mode: {regime.mode} | Regime: {regime.trend}/{regime.volatility}",
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
                            entry_signal_score=str(score),
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
                    else:
                        log.error(f"Order failed: {signal['ticker']}")

                sig_log.commit(db)

    # ── Monthly tax sweep — runs once on last trading day, after 3pm
    if is_last_trading_day_of_month() and now.hour >= 15:
        today_str = now.strftime('%Y-%m-%d')
        if not db.has_event_today('TAX_SWEEP', today_str):
            portfolio = db.get_portfolio()
            positions = db.get_open_positions()
            unrealized  = sum(p.get('pnl', 0) for p in positions)
            total_gains = portfolio['realized_gains'] + unrealized
            if total_gains > 0:
                tax = round(total_gains * C.GAIN_TAX_PCT, 2)
                log.info(f"Month-end tax sweep: ${tax:.2f}")
                db.sweep_monthly_tax(tax)
                db.log_event("TAX_SWEEP", agent="Trade Logic",
                             details=f"monthly sweep ${tax:.2f}")

    # ── Session complete
    portfolio   = db.get_portfolio()
    positions   = db.get_open_positions()
    total_value = portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions)
    log.info(f"Session complete — portfolio=${total_value:.2f} "
             f"positions={len(positions)} cash=${portfolio['cash']:.2f}")
    db.log_heartbeat("trade_logic_agent", "OK", portfolio_value=total_value)
    db.log_event("AGENT_COMPLETE", agent="Trade Logic",
                 details=f"session={session} positions={len(positions)}",
                 portfolio_value=total_value)
    db.add_notification('daily', 'Session Complete',
        f'{session.title()} session: {len(positions)} positions, portfolio ${total_value:,.2f}',
        meta={'session': session, 'positions': len(positions), 'portfolio': round(total_value, 2)})

    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="trade_logic_agent", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")

    # Daily report — runs once per day after 4pm
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


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — ExecutionAgent (Agent 1)')
    parser.add_argument('--session', choices=['open', 'midday', 'close', 'hourly'], default='hourly')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID — routes DB and Alpaca credentials to per-customer sources')
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
        release_agent_lock()
