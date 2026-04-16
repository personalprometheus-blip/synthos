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
from pathlib import Path
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')
_CUSTOMER_ID      = None   # set from --customer-id arg

# Staleness thresholds
FAULT_SCAN_STALE_MINUTES  = 120   # fault scan older than 2h = degraded
BIAS_SCAN_STALE_MINUTES   = 120   # bias scan older than 2h = ignore (pass-through)
MARKET_STATE_STALE_MINUTES = 60   # market state older than 1h = degraded
MACRO_REGIME_STALE_HOURS   = 24   # macro regime older than 24h = degraded

# Defensive sectors allowed during CONTRACTION regime
DEFENSIVE_SECTORS = {'BIL', 'XLU', 'XLP', 'SHV', 'TLT', 'SGOV'}
DEFENSIVE_INDUSTRIES = {'utilities', 'consumer staples', 'consumer defensive', 'treasury'}

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
    """Shared intelligence DB (owner customer)."""
    if OWNER_CUSTOMER_ID:
        return get_customer_db(OWNER_CUSTOMER_ID)
    return get_db()


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

def gate1_system_health(report: ValidationReport, master_db):
    """Check system health from Fault Detection Agent output."""
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

    # Evaluate severity
    worst = scan.get('worst_severity', 'OK')
    critical_count = scan.get('critical', 0)
    warning_count = scan.get('warnings', 0)

    if worst == 'CRITICAL' or critical_count > 0:
        # Build detail from critical findings
        critical_msgs = [
            f.get('message', f.get('code', 'unknown'))
            for f in scan.get('findings', [])
            if f.get('severity') == 'CRITICAL'
        ]
        detail = "; ".join(critical_msgs[:3]) if critical_msgs else f"{critical_count} critical issue(s)"
        log.error(f"  System health RED: {detail}")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.NO_GO,
            message=f"System health CRITICAL: {detail}",
            restrictions=["NO_NEW_POSITIONS", "SYSTEM_CRITICAL"]
        ))
    elif worst == 'WARNING' or warning_count > 0:
        log.warning(f"  System health YELLOW: {warning_count} warning(s)")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.CAUTION,
            message=f"System health degraded: {warning_count} warning(s)",
            restrictions=["SYSTEM_WARNINGS_ACTIVE"]
        ))
    else:
        log.info("  System health GREEN")
        report.add(GateResult(
            gate="GATE1_SYSTEM_HEALTH",
            status=GateStatus.GO,
            message="System health OK"
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

    # Evaluate bias findings
    findings = scan.get('findings', [])
    if not findings:
        findings = scan.get('biases', [])

    has_critical = False
    has_warning = False
    restrictions = []
    messages = []

    for f in findings:
        severity = f.get('severity', 'OK')
        code = f.get('code', '')
        bias_type = f.get('type', code)

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

    # Check staleness via _MARKET_STATE_UPDATED if available
    state_ts = master_db.get_setting('_MARKET_STATE_UPDATED')
    age = _age_minutes(state_ts)
    if age is not None and age > MARKET_STATE_STALE_MINUTES:
        log.warning(f"  Market state stale ({int(age)}m) — CAUTION")
        report.add(GateResult(
            gate="GATE3_MARKET_STATE",
            status=GateStatus.CAUTION,
            message=f"Market state stale ({int(age)}m old) — proceeding with caution",
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
    """Check macro regime from Macro Regime Agent output."""
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

    # Try to parse as JSON (may be a simple string or a JSON object)
    regime = None
    regime_ts = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            regime = data.get('regime', data.get('state', '')).upper().strip()
            regime_ts = data.get('timestamp', data.get('updated_at', ''))
        elif isinstance(data, str):
            regime = data.upper().strip()
    except (json.JSONDecodeError, TypeError, AttributeError):
        regime = raw.upper().strip()

    if not regime:
        log.info("  Macro regime data empty — pass-through")
        report.add(GateResult(
            gate="GATE4_MACRO_REGIME",
            status=GateStatus.GO,
            message="Macro regime data empty — pass-through"
        ))
        return

    # Check staleness if we have a timestamp
    if not regime_ts:
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
    """Get all active customer IDs from the auth database."""
    try:
        import auth
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active', True)]
    except Exception as e:
        log.warning(f"Could not load customer list: {e}")
        # Fallback: scan data/customers directory
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
        gate1_system_health(report, master)
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

    # ── Notifications for NO_GO ───────────────────────────────────────
    if verdict == GateStatus.NO_GO:
        no_go_gates = [g for g in report.gates if g.status == GateStatus.NO_GO]
        body_lines = [f"- {g.gate}: {g.message}" for g in no_go_gates]
        try:
            cust_db.add_notification(
                category='alert',
                title='Trading Blocked — Validator NO_GO',
                body='\n'.join(body_lines),
                meta=json.dumps({
                    "source": "validator_stack_agent",
                    "verdict": verdict,
                    "restrictions": restrictions
                })
            )
        except Exception as e:
            log.warning(f"Failed to write NO_GO notification: {e}")

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
