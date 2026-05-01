"""
retail_validator_stack_agent.py — Validator Stack Agent
Synthos · Agent 10

Runs:
  Every enrichment cycle (30 min during market hours) via market daemon
  Called per-customer after upstream agents (Fault Detection, Bias Detection,
  Market-State Aggregator, Macro Regime) have completed their scans.

Responsibilities:
  - 5-gate deterministic pre-trade validation spine
  - Gate 1: System health check (reads Fault Detection output)
  - Gate 2: Bias guard (reads Bias Detection output, per-customer)
  - Gate 3: Market state check (reads Market-State Aggregator output)
  - Gate 4: Macro regime guard (reads Macro Regime output)
  - Gate 5: Final verdict aggregation — GO / CAUTION / NO_GO
  - Write verdict + restrictions to customer DB for Trade Logic consumption
  - Degrade gracefully — missing upstream data produces CAUTION, never NO_GO

No LLM in any decision path. All gate logic is deterministic and traceable.

Data sources:
  - Master DB: _FAULT_SCAN_LAST, _MARKET_STATE, _MARKET_STATE_SCORE, _MACRO_REGIME
  - Customer DB: _BIAS_SCAN_LAST, positions, customer_settings

Usage:
  python3 retail_validator_stack_agent.py
  python3 retail_validator_stack_agent.py --customer-id <uuid>
"""

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, get_shared_db, acquire_agent_lock, release_agent_lock
from retail_shared import emit_admin_alert, get_active_customers

# ── CONFIG ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')
_CUSTOMER_ID      = None   # set from --customer-id arg

# Staleness thresholds
FAULT_SCAN_STALE_MINUTES  = 120   # fault scan older than 2h = degraded
BIAS_SCAN_STALE_MINUTES   = 120   # bias scan older than 2h = ignore (pass-through)
MARKET_STATE_STALE_MINUTES = 60   # market state older than 1h = degraded
# Macro regime is daily — 48h tolerates a weekend gap. The agent runs
# pre-market each weekday; a successful Monday 8:30 ET run refreshes the
# timestamp before validator first runs at 9:30. Without weekend
# tolerance, every Monday morning validator marked the regime stale even
# though Friday's classification was the freshest data we could possibly
# have. agent 8's MACRO_REGIME_DEGRADED admin_alert (streak counter)
# catches the genuine "agent has been failing" case independently.
MACRO_REGIME_STALE_HOURS   = 48

# Defensive sectors allowed during CONTRACTION regime
DEFENSIVE_SECTORS = {'BIL', 'XLU', 'XLP', 'SHV', 'TLT', 'SGOV'}
DEFENSIVE_INDUSTRIES = {'utilities', 'consumer staples', 'consumer defensive', 'treasury'}

# Finding codes that are informational-only — they tell us something is
# off in the data, but the trader has no business gating on them. Keeping
# these out of the "WARNING count" means the validator verdict reflects
# actual system health rather than lifetime cosmetic warnings.
#
# Add to this set when we confirm a warning is non-blocking; remove if we
# realize we want it to gate trades.
INFORMATIONAL_FAULT_CODES = frozenset({
    'STALE_SIGNALS',         # in-flight signals >48h old — will be expired
    'STUCK_SIGNALS',         # QUEUED with missing stamps — self-clears once pipeline catches up or signals expire
    'LOG_BLOAT',             # rotate/archive, never affects trading
    'SECTOR_DATA_INCOMPLETE',# data quality — backfill agent handles async
})

# Bias codes since the agent-7 audit carry a `_<short_id>` suffix
# (e.g. DISPOSITION_EFFECT_30eff008) so per-customer dedup works on
# admin_alerts. We match against the base prefix here so the filter
# survives the suffix. Listed without suffix; _is_informational_bias_code
# does the prefix match.
INFORMATIONAL_BIAS_CODE_PREFIXES = (
    'SECTOR_DATA_INCOMPLETE',  # data quality (INFO-level in bias agent, belt-and-suspenders here)
    'DISPOSITION_EFFECT',      # behavioral observation — worth surfacing but shouldn't stop trades
    'DISPOSITION_OK',          # pass-through OK finding
    'TOO_FEW_CLOSED',          # explicitly-N/A finding from gate5
    'ONE_SIDED_RESULTS',       # gate3 N/A — all winners or all losers in window
    'ONE_SIDED',               # gate5 N/A — same shape
    'LOSS_AVERSION_OK',        # gate3 pass-through
    'RECENCY_OK',              # gate2 pass-through
    'TRADE_FREQ_OK',           # gate4 pass-through
    'NO_TRADES',               # gate4 N/A — no trades in window
    'NO_POSITIONS',            # gate1 N/A — no open positions
    'NO_CLASSIFIED_SECTORS',   # gate1 N/A — only reserves/unknowns
    'SECTOR_BALANCED',         # gate1 pass-through
    'ZERO_VALUE',              # gate1 N/A
    'TOO_FEW_TRADES',          # gate2 N/A
)


def _is_informational_bias_code(code: str) -> bool:
    """True if a bias-finding code matches a base prefix listed in
    INFORMATIONAL_BIAS_CODE_PREFIXES. Handles per-customer suffixes
    (e.g. DISPOSITION_EFFECT_30eff008) so the validator filter keeps
    working after the agent-7 audit added customer-scoped codes."""
    if not code:
        return False
    return any(code == p or code.startswith(p + '_')
               for p in INFORMATIONAL_BIAS_CODE_PREFIXES)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('validator_stack_agent')


# ── STATUS LEVELS ────────────────────────────────────────────────────────

class GateStatus:
    GO      = "GO"
    CAUTION = "CAUTION"
    NO_GO   = "NO_GO"


@dataclass
class GateResult:
    gate: str
    status: str           # GO, CAUTION, NO_GO
    message: str
    restrictions: list = field(default_factory=list)


@dataclass
class ValidationReport:
    """Aggregated output of all 5 gates."""
    gates: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    def add(self, result: GateResult):
        self.gates.append(result)

    @property
    def verdict(self) -> str:
        """NO_GO if any gate is NO_GO, CAUTION if any CAUTION, else GO."""
        statuses = [g.status for g in self.gates]
        if GateStatus.NO_GO in statuses:
            return GateStatus.NO_GO
        if GateStatus.CAUTION in statuses:
            return GateStatus.CAUTION
        return GateStatus.GO

    @property
    def all_restrictions(self) -> list:
        """Deduplicated list of restrictions from all gates."""
        seen = set()
        restrictions = []
        for g in self.gates:
            for r in g.restrictions:
                if r not in seen:
                    seen.add(r)
                    restrictions.append(r)
        return restrictions

    def summary(self):
        gate_verdicts = ", ".join(f"{g.gate}={g.status}" for g in self.gates)
        return (f"Validator verdict: {self.verdict} | "
                f"Gates: {gate_verdicts} | "
                f"Restrictions: {self.all_restrictions or 'none'}")


# ── DB HELPERS ────────────────────────────────────────────────────────────

def _master_db():
    """Shared market-intelligence DB.
    2026-04-27: was previously get_customer_db(OWNER_CUSTOMER_ID).  See
    retail_database.get_shared_db() for the architectural rationale.
    Per-customer findings still go to _customer_db() below."""
    return get_shared_db()


def _customer_db(customer_id=None):
    """Per-customer DB."""
    cid = customer_id or _CUSTOMER_ID or OWNER_CUSTOMER_ID
    if cid:
        return get_customer_db(cid)
    return get_db()


def _now_utc():
    return datetime.now(tz=ZoneInfo("UTC"))


def _now_str():
    return datetime.now(tz=ZoneInfo("UTC")).strftime('%Y-%m-%d %H:%M:%S')


def _now_et():
    return datetime.now(ET)


def _is_market_hours():
    """True if currently within US market hours (9:30-16:00 ET, weekdays)."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def _parse_timestamp(ts_str):
    """Parse a timestamp string into a UTC-aware datetime, or None."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt
    except (ValueError, TypeError):
        return None


def _age_minutes(ts_str):
    """Return age in minutes of a timestamp string, or None if unparseable."""
    dt = _parse_timestamp(ts_str)
    if dt is None:
        return None
    return (_now_utc() - dt).total_seconds() / 60.0


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1: SYSTEM HEALTH CHECK
#  Read _FAULT_SCAN_LAST from master DB (written by Fault Detection Agent)
# ══════════════════════════════════════════════════════════════════════════

def _finding_applies_to_customer(code: str, short_id: str) -> bool:
    """True if a fault-finding code applies to the current customer.

    Fault detection's audit (agent 6) added per-customer suffixes to
    several codes — EQUITY_ZERO_<short_id>, KILL_SWITCH_ON_<short_id>,
    ORPHAN_POSITION_<short_id>, ALPACA_AUTH_FAIL_<short_id>,
    WAL_BLOAT_CUST_<short_id>, ACCOUNT_CHECK_FAIL_<short_id>. Without
    this filter the validator treats ALL findings as global, so one
    customer's Alpaca 401 flags every other customer NO_GO.

    A code "applies to" the current customer when:
      - it has no 8-char hex short_id suffix (system-wide finding), OR
      - the trailing 8-char short_id matches `short_id`.
    Findings tagged for OTHER customers are skipped.
    """
    if not code:
        return False
    # Codes ending with "_<8 hex chars>" are per-customer.
    parts = code.rsplit('_', 1)
    if len(parts) == 2 and len(parts[1]) == 8 and all(
            c in '0123456789abcdef' for c in parts[1].lower()):
        return parts[1] == short_id
    return True   # no per-customer suffix = system-wide, applies to everyone


def gate1_system_health(report: ValidationReport, master_db, customer_id=None):
    """Check system health from Fault Detection Agent output. Per-customer
    fault findings (those with a `_<short_id>` suffix) are filtered to
    only those matching `customer_id` so one customer's broken Alpaca
    creds doesn't NO_GO everyone."""
    log.info("[GATE 1] System health check")

    raw = master_db.get_setting('_FAULT_SCAN_LAST')

    if not raw:
        log.warning("  No fault scan data found — degraded mode")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.CAUTION,
            message="No fault scan data available — system health unknown",
            restrictions=["DEGRADED_NO_FAULT_DATA"]
        ))
        return

    try:
        scan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("  Fault scan data corrupt — degraded mode")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.CAUTION,
            message="Fault scan data corrupt or unreadable",
            restrictions=["DEGRADED_CORRUPT_FAULT_DATA"]
        ))
        return

    # Check staleness
    scan_ts = scan.get('timestamp', '')
    age = _age_minutes(scan_ts)
    if age is not None and age > FAULT_SCAN_STALE_MINUTES:
        log.warning(f"  Fault scan is {int(age)}m old (threshold: {FAULT_SCAN_STALE_MINUTES}m)")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.CAUTION,
            message=f"Fault scan stale ({int(age)}m old, threshold {FAULT_SCAN_STALE_MINUTES}m)",
            restrictions=["DEGRADED_STALE_FAULT_SCAN"]
        ))
        return

    # Evaluate severity, filtering out:
    #   1. Findings tagged for OTHER customers (per-customer suffix
    #      doesn't match this run's customer)
    #   2. Informational-only codes that shouldn't gate the trader
    # worst_severity and the raw counts in the payload reflect the
    # entire fleet's findings; we re-count what actually applies to
    # this specific customer.
    short_id = (customer_id or 'master')[:8] if customer_id else 'master'
    findings = [
        f for f in scan.get('findings', [])
        if _finding_applies_to_customer(f.get('code', ''), short_id)
    ]
    critical_findings = [
        f for f in findings
        if f.get('severity') == 'CRITICAL'
        and f.get('code') not in INFORMATIONAL_FAULT_CODES
    ]
    actionable_warnings = [
        f for f in findings
        if f.get('severity') == 'WARNING'
        and f.get('code') not in INFORMATIONAL_FAULT_CODES
    ]
    info_warnings = [
        f for f in findings
        if f.get('severity') == 'WARNING'
        and f.get('code') in INFORMATIONAL_FAULT_CODES
    ]

    if critical_findings:
        critical_msgs = [f.get('message', f.get('code', 'unknown')) for f in critical_findings]
        detail = "; ".join(critical_msgs[:3])
        log.error(f"  System health RED: {detail}")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.NO_GO,
            message=f"System health CRITICAL: {detail}",
            restrictions=["NO_NEW_POSITIONS", "SYSTEM_CRITICAL"]
        ))
    elif actionable_warnings:
        warn_msgs = [f.get('message', f.get('code', 'unknown')) for f in actionable_warnings]
        log.warning(f"  System health YELLOW: {len(actionable_warnings)} actionable warning(s)")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.CAUTION,
            message=f"System health degraded: {len(actionable_warnings)} warning(s) — "
                    + "; ".join(warn_msgs[:2]),
            restrictions=["SYSTEM_WARNINGS_ACTIVE"]
        ))
    else:
        note = ""
        if info_warnings:
            note = f" ({len(info_warnings)} informational-only finding(s) ignored)"
        log.info(f"  System health GREEN{note}")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.GO,
            message=f"System health OK{note}"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2: BIAS GUARD
#  Read _BIAS_SCAN_LAST from customer DB (written by Bias Detection Agent)
# ══════════════════════════════════════════════════════════════════════════

def gate2_bias_guard(report: ValidationReport, cust_db):
    """Check for active bias warnings from Bias Detection Agent."""
    log.info("[GATE 2] Bias guard check")

    raw = cust_db.get_setting('_BIAS_SCAN_LAST')

    if not raw:
        # Don't block on missing bias data — agent may not have run yet
        log.info("  No bias scan data — pass-through")
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=GateStatus.GO,
            message="No bias scan data available — pass-through (no block on missing data)"
        ))
        return

    try:
        scan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.info("  Bias scan data corrupt — pass-through")
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=GateStatus.GO,
            message="Bias scan data unreadable — pass-through"
        ))
        return

    # Check staleness — stale bias data = pass-through
    scan_ts = scan.get('timestamp', '')
    age = _age_minutes(scan_ts)
    if age is not None and age > BIAS_SCAN_STALE_MINUTES:
        log.info(f"  Bias scan stale ({int(age)}m) — pass-through")
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=GateStatus.GO,
            message=f"Bias scan stale ({int(age)}m old) — pass-through"
        ))
        return

    # Evaluate bias findings. Informational-only codes (data-quality
    # observations) are reported to the caller but never elevate the gate
    # from GO — they don't affect trading decisions.
    findings = scan.get('findings', [])
    if not findings:
        findings = scan.get('biases', [])

    has_critical = False
    has_warning = False
    restrictions = []
    messages = []
    info_findings = 0

    for f in findings:
        severity = f.get('severity', 'OK')
        code = f.get('code', '')
        bias_type = f.get('type', code)

        if _is_informational_bias_code(code):
            info_findings += 1
            continue

        if severity == 'CRITICAL':
            has_critical = True
            # Sector concentration CRITICAL → block new trades in that sector
            if 'sector' in bias_type.lower() or 'concentration' in bias_type.lower():
                sector = f.get('sector', f.get('detail', 'unknown'))
                messages.append(f"Sector concentration CRITICAL: {sector}")
                restrictions.append(f"BLOCK_SECTOR_{sector.upper().replace(' ', '_')}")
            # Overtrading CRITICAL → block all new trades
            elif 'overtrad' in bias_type.lower():
                messages.append("Overtrading CRITICAL — blocking all new trades")
                restrictions.append("NO_NEW_POSITIONS")
            else:
                messages.append(f"Bias CRITICAL: {f.get('message', bias_type)}")
                restrictions.append("NO_NEW_POSITIONS")
        elif severity == 'WARNING':
            has_warning = True
            messages.append(f"Bias WARNING: {f.get('message', bias_type)}")

    if has_critical:
        # If overtrading is critical, block everything
        if "NO_NEW_POSITIONS" in restrictions:
            status = GateStatus.NO_GO
        else:
            # Sector-specific blocks are CAUTION (trades in other sectors still OK)
            status = GateStatus.CAUTION
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=status,
            message="; ".join(messages[:3]),
            restrictions=restrictions
        ))
    elif has_warning:
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=GateStatus.CAUTION,
            message="; ".join(messages[:3]),
            restrictions=["BIAS_WARNINGS_ACTIVE"]
        ))
    else:
        log.info("  No active bias warnings")
        report.add(GateResult(
            gate="GATE2_BIAS_GUARD",
            status=GateStatus.GO,
            message="No active bias warnings"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 3: MARKET STATE CHECK
#  Read _MARKET_STATE and _MARKET_STATE_SCORE from master DB
# ══════════════════════════════════════════════════════════════════════════

# Score → regime mapping
MARKET_STATE_THRESHOLDS = [
    # (min_score, max_score, state_name, gate_status, restrictions)
    (0,   19, "RISK_OFF",       GateStatus.NO_GO,    ["NO_NEW_POSITIONS", "EXITS_ONLY"]),
    (20,  39, "CAUTIOUS_BEAR",  GateStatus.CAUTION,  ["REDUCE_SIZE_50", "DEFENSIVE_PREFERRED"]),
    (40,  54, "NEUTRAL",        GateStatus.CAUTION,  ["SELECTIVE_ENTRIES"]),
    (55,  74, "CAUTIOUS_BULL",  GateStatus.GO,       []),
    (75, 100, "RISK_ON",        GateStatus.GO,       []),
]


def gate3_market_state(report: ValidationReport, master_db):
    """Check market state from Market-State Aggregator output."""
    log.info("[GATE 3] Market state check")

    state_raw = master_db.get_setting('_MARKET_STATE')
    score_raw = master_db.get_setting('_MARKET_STATE_SCORE')

    if not state_raw and not score_raw:
        # No market state data — CAUTION but don't block
        log.warning("  No market state data — CAUTION")
        report.add(GateResult(
            gate="GATE3_MARKET_STATE",
            status=GateStatus.CAUTION,
            message="No market state data available — proceeding with caution",
            restrictions=["DEGRADED_NO_MARKET_STATE"]
        ))
        return

    # Check staleness via _MARKET_STATE_UPDATED. Audit Round 8 — a
    # missing _MARKET_STATE_UPDATED setting was previously treated as
    # "age unknown, assume fresh" (age=None let the CAUTION branch be
    # skipped). Correct policy: missing timestamp = STALE (we have no
    # evidence the data is fresh).
    state_ts = master_db.get_setting('_MARKET_STATE_UPDATED')
    age = _age_minutes(state_ts)
    stale = (age is None) or (age > MARKET_STATE_STALE_MINUTES)
    if stale:
        reason = (
            f"{int(age)}m old" if age is not None
            else "_MARKET_STATE_UPDATED setting missing"
        )
        log.warning(f"  Market state stale ({reason}) — CAUTION")
        report.add(GateResult(
            gate="GATE3_MARKET_STATE",
            status=GateStatus.CAUTION,
            message=f"Market state stale ({reason}) — proceeding with caution",
            restrictions=["DEGRADED_STALE_MARKET_STATE"]
        ))
        return

    # Use score if available, else map from state name
    score = None
    if score_raw:
        try:
            score = float(score_raw)
        except (ValueError, TypeError):
            pass

    state_name = (state_raw or '').upper().strip()

    if score is not None:
        # Map score to regime
        for min_s, max_s, regime, gate_status, restrictions in MARKET_STATE_THRESHOLDS:
            if min_s <= score <= max_s:
                msg = f"Market state: {regime} (score {score:.0f})"
                if gate_status == GateStatus.NO_GO:
                    msg += " — blocking new positions, exits only"
                elif gate_status == GateStatus.CAUTION and restrictions:
                    msg += f" — {', '.join(restrictions)}"
                log.info(f"  {msg}")
                report.add(GateResult(
                    gate="GATE3_MARKET_STATE",
                    status=gate_status,
                    message=msg,
                    restrictions=restrictions
                ))
                return

        # Score out of expected range — CAUTION
        log.warning(f"  Market state score {score} out of range")
        report.add(GateResult(
            gate="GATE3_MARKET_STATE",
            status=GateStatus.CAUTION,
            message=f"Market state score {score:.0f} outside expected 0-100 range",
            restrictions=["DEGRADED_INVALID_SCORE"]
        ))
    elif state_name:
        # Fallback: map from state name without score
        state_map = {
            "RISK_OFF":      (GateStatus.NO_GO,    ["NO_NEW_POSITIONS", "EXITS_ONLY"]),
            "CAUTIOUS_BEAR": (GateStatus.CAUTION,  ["REDUCE_SIZE_50", "DEFENSIVE_PREFERRED"]),
            "NEUTRAL":       (GateStatus.CAUTION,  ["SELECTIVE_ENTRIES"]),
            "CAUTIOUS_BULL": (GateStatus.GO,       []),
            "RISK_ON":       (GateStatus.GO,       []),
        }
        if state_name in state_map:
            gate_status, restrictions = state_map[state_name]
            report.add(GateResult(
                gate="GATE3_MARKET_STATE",
                status=gate_status,
                message=f"Market state: {state_name} (no score available)",
                restrictions=restrictions
            ))
        else:
            # Unknown state — CAUTION
            log.warning(f"  Unknown market state '{state_name}' — CAUTION")
            report.add(GateResult(
                gate="GATE3_MARKET_STATE",
                status=GateStatus.CAUTION,
                message=f"Unknown market state '{state_name}' — proceeding with caution",
                restrictions=["DEGRADED_UNKNOWN_MARKET_STATE"]
            ))
    else:
        report.add(GateResult(
            gate="GATE3_MARKET_STATE",
            status=GateStatus.CAUTION,
            message="Market state data present but empty — proceeding with caution",
            restrictions=["DEGRADED_EMPTY_MARKET_STATE"]
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 4: MACRO REGIME GUARD
#  Read _MACRO_REGIME from master DB
# ══════════════════════════════════════════════════════════════════════════

MACRO_REGIME_MAP = {
    # regime → (gate_status, restrictions, message_suffix)
    "CRISIS":      (GateStatus.NO_GO,    ["NO_NEW_POSITIONS", "CRISIS_MODE"],
                    "macro crisis — blocking all new positions"),
    "CONTRACTION": (GateStatus.CAUTION,  ["DEFENSIVE_ONLY", "REDUCE_SIZE_50"],
                    "contraction — defensive sectors only, reduced sizing"),
    "LATE_CYCLE":  (GateStatus.CAUTION,  ["LATE_CYCLE_CAUTION"],
                    "late cycle — increased caution, tighter stops recommended"),
    "UNCERTAIN":   (GateStatus.GO,       [],
                    "uncertain — pass-through, no restrictions"),
    "RECOVERY":    (GateStatus.GO,       [],
                    "recovery — favorable conditions"),
    "EXPANSION":   (GateStatus.GO,       [],
                    "expansion — favorable conditions"),
}


def gate4_macro_regime(report: ValidationReport, master_db):
    """Check macro regime from Macro Regime Agent output.

    Reads `_MACRO_REGIME` (bare regime label) and `_MACRO_REGIME_UPDATED`
    (ISO timestamp) from the shared DB. Pre-2026-05 the macro_regime agent
    was assumed to potentially write JSON, so this gate had a 12-line
    JSON-parse path that never fired (agent-8 has always written a plain
    string). Simplified now — just read the label and the dedicated
    timestamp setting.
    """
    log.info("[GATE 4] Macro regime guard")

    raw = master_db.get_setting('_MACRO_REGIME')
    if not raw:
        # No macro data — pass-through (don't block on missing data)
        log.info("  No macro regime data — pass-through")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=GateStatus.GO,
            message="No macro regime data available — pass-through"
        ))
        return

    regime = str(raw).upper().strip()
    if not regime:
        log.info("  Macro regime data empty — pass-through")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=GateStatus.GO,
            message="Macro regime data empty — pass-through"
        ))
        return

    # Staleness — _MACRO_REGIME_UPDATED is the dedicated timestamp added
    # in the agent-8 audit. Pre-fix the validator would silently treat
    # missing-timestamp as "fresh" (age=None skipped the > comparison).
    regime_ts = master_db.get_setting('_MACRO_REGIME_UPDATED')
    age = _age_minutes(regime_ts)
    if age is not None and age > MACRO_REGIME_STALE_HOURS * 60:
        log.info(f"  Macro regime stale ({int(age)}m) — pass-through")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=GateStatus.GO,
            message=f"Macro regime stale ({int(age / 60)}h old) — pass-through"
        ))
        return

    # Map regime to verdict
    if regime in MACRO_REGIME_MAP:
        gate_status, restrictions, msg_suffix = MACRO_REGIME_MAP[regime]
        log.info(f"  Macro regime: {regime} — {msg_suffix}")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=gate_status,
            message=f"Macro regime: {regime} — {msg_suffix}",
            restrictions=restrictions
        ))
    else:
        # Unknown regime — pass-through
        log.warning(f"  Unknown macro regime '{regime}' — pass-through")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=GateStatus.GO,
            message=f"Unknown macro regime '{regime}' — pass-through"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 5: FINAL VERDICT AGGREGATION
#  Combine all gates into a single GO / CAUTION / NO_GO
# ══════════════════════════════════════════════════════════════════════════

def gate5_final_verdict(report: ValidationReport):
    """Aggregate gate results and log the final verdict.

    This gate doesn't add a new GateResult — it reads the existing ones.
    The verdict is computed from the report's .verdict property.
    """
    log.info("[GATE 5] Final verdict aggregation")

    verdict = report.verdict
    restrictions = report.all_restrictions

    if verdict == GateStatus.NO_GO:
        no_go_gates = [g for g in report.gates if g.status == GateStatus.NO_GO]
        reasons = [g.message for g in no_go_gates]
        log.error(f"  VERDICT: NO_GO — {'; '.join(reasons[:3])}")
    elif verdict == GateStatus.CAUTION:
        caution_gates = [g for g in report.gates if g.status == GateStatus.CAUTION]
        reasons = [g.message for g in caution_gates]
        log.warning(f"  VERDICT: CAUTION — {'; '.join(reasons[:3])}")
        if restrictions:
            log.warning(f"  Restrictions: {restrictions}")
    else:
        log.info("  VERDICT: GO — all gates passed")

    return verdict, restrictions


# ══════════════════════════════════════════════════════════════════════════
#  CUSTOMER DISCOVERY
# ══════════════════════════════════════════════════════════════════════════

def _get_active_customer_ids():
    """Active customer IDs (delegates to retail_shared.get_active_customers).
    Falls back to scanning data/customers/ when auth is unreachable so the
    validator can still run on cached customer state."""
    ids = get_active_customers()
    if ids:
        return ids
    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    if os.path.isdir(customers_dir):
        return [d for d in os.listdir(customers_dir)
                if d != 'default' and os.path.isdir(os.path.join(customers_dir, d))]
    return []


# ══════════════════════════════════════════════════════════════════════════
#  RUN (PER-CUSTOMER)
# ══════════════════════════════════════════════════════════════════════════

def run_for_customer(customer_id):
    """Execute the 5-gate validation spine for a single customer."""
    cust_db = _customer_db(customer_id)
    master = _master_db()

    short_id = (customer_id or 'default')[:8]

    # ── Lifecycle: START ──────────────────────────────────────────────
    cust_db.log_event("AGENT_START", agent="Validator Stack", details="pre-trade validation")
    cust_db.log_heartbeat("validator_stack_agent", "RUNNING")
    log.info("=" * 70)
    log.info(f"VALIDATOR STACK AGENT — Customer {short_id}")
    log.info("=" * 70)

    report = ValidationReport(started_at=_now_str())

    # ── Gate 1: System health ─────────────────────────────────────────
    try:
        gate1_system_health(report, master, customer_id=customer_id)
    except Exception as e:
        log.error(f"Gate 1 failed: {e}", exc_info=True)
        report.add(GateResult("GATE1_SYSTEM_HEALTH", GateStatus.CAUTION,
                               f"System health check failed: {e}",
                               ["DEGRADED_GATE1_ERROR"]))

    # ── Gate 2: Bias guard ────────────────────────────────────────────
    try:
        gate2_bias_guard(report, cust_db)
    except Exception as e:
        log.error(f"Gate 2 failed: {e}", exc_info=True)
        report.add(GateResult("GATE2_BIAS_GUARD", GateStatus.GO,
                               f"Bias guard check failed: {e} — pass-through"))

    # ── Gate 3: Market state ──────────────────────────────────────────
    try:
        gate3_market_state(report, master)
    except Exception as e:
        log.error(f"Gate 3 failed: {e}", exc_info=True)
        report.add(GateResult("GATE3_MARKET_STATE", GateStatus.CAUTION,
                               f"Market state check failed: {e}",
                               ["DEGRADED_GATE3_ERROR"]))

    # ── Gate 4: Macro regime ──────────────────────────────────────────
    try:
        gate4_macro_regime(report, master)
    except Exception as e:
        log.error(f"Gate 4 failed: {e}", exc_info=True)
        report.add(GateResult("GATE4_MACRO_REGIME", GateStatus.GO,
                               f"Macro regime check failed: {e} — pass-through"))

    # ── Gate 5: Final verdict ─────────────────────────────────────────
    verdict, restrictions = gate5_final_verdict(report)

    # ── Aggregate and store results ───────────────────────────────────
    report.completed_at = _now_str()

    # Build detail payload
    detail = {
        "verdict": verdict,
        "gates": [
            {
                "gate": g.gate,
                "status": g.status,
                "message": g.message,
                "restrictions": g.restrictions
            }
            for g in report.gates
        ],
        "restrictions": restrictions,
        "timestamp": report.completed_at,
        "customer_id": customer_id or 'default'
    }

    # Write verdict settings to customer DB
    cust_db.set_setting('_VALIDATOR_VERDICT', verdict)
    cust_db.set_setting('_VALIDATOR_DETAIL', json.dumps(detail))
    cust_db.set_setting('_VALIDATOR_RESTRICTIONS', json.dumps(restrictions))

    # ── Admin alerts for NO_GO ────────────────────────────────────────
    # Validator NO_GO means the system decided this customer can't trade
    # right now — a plumbing issue the customer can't fix. Route to the
    # shared admin_alerts stream on the master DB via emit_admin_alert
    # so per-customer codes are deduped independently (12h window). Was
    # previously a raw add_admin_alert call with a non-customer-scoped
    # code, which produced N alerts every cycle for N active customers.
    if verdict == GateStatus.NO_GO:
        no_go_gates = [g for g in report.gates if g.status == GateStatus.NO_GO]
        body_lines = [f"- {g.gate}: {g.message}" for g in no_go_gates]
        # Synthesise a Finding-shaped object for the shared alert router.
        # customer_id and per-customer code suffix ensure each customer
        # gets independent dedup tracking.
        from types import SimpleNamespace
        f = SimpleNamespace(
            severity="CRITICAL",
            code=f"VALIDATOR_NO_GO_{short_id}",
            gate="GATE5_VERDICT",
            message=f"{short_id}: Trading blocked — Validator NO_GO",
            detail='\n'.join(body_lines),
            meta={
                "verdict":      verdict,
                "restrictions": restrictions,
                "customer_id":  customer_id,
            },
            customer_id=customer_id,
        )
        emit_admin_alert(
            _master_db(), f,
            source_agent='validator_stack_agent',
            category='validator',
            fallback_customer_id=customer_id,
        )

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    summary = report.summary()
    log.info("=" * 70)
    log.info(f"VALIDATOR — {summary}")
    log.info("=" * 70)

    cust_db.log_heartbeat("validator_stack_agent", "OK")
    cust_db.log_event(
        "AGENT_COMPLETE",
        agent="Validator Stack",
        details=f"verdict={verdict}, restrictions={restrictions}, "
                f"gates={len(report.gates)}"
    )

    return report


# ══════════════════════════════════════════════════════════════════════════
#  MAIN RUN FUNCTION
# ══════════════════════════════════════════════════════════════════════════

def run():
    """Run the validator for a single customer or all active customers."""
    if _CUSTOMER_ID:
        report = run_for_customer(_CUSTOMER_ID)
    else:
        # No customer specified — loop through all active customers
        customer_ids = _get_active_customer_ids()
        if not customer_ids:
            log.warning("No active customers found — running for default/owner")
            report = run_for_customer(OWNER_CUSTOMER_ID or None)
        else:
            log.info(f"Running validator for {len(customer_ids)} customer(s)")
            report = None
            for cid in customer_ids:
                try:
                    report = run_for_customer(cid)
                except Exception as e:
                    log.error(f"Validator failed for customer {cid[:8]}: {e}", exc_info=True)

    # ── Monitor heartbeat POST ────────────────────────────────────────
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="validator_stack_agent", status="OK")
    except Exception as e:
        log.warning(f"Monitor heartbeat POST failed: {e}")

    return report


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — Validator Stack Agent')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (for per-customer mode)')
    args = parser.parse_args()

    if args.customer_id:
        _CUSTOMER_ID = args.customer_id

    acquire_agent_lock("retail_validator_stack_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
