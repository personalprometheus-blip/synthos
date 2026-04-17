"""
retail_bias_detection_agent.py — Bias Detection Agent
Synthos · Agent 7

Runs:
  Every enrichment cycle (30 min during market hours) via market daemon
  Also: once at pre-market open, once at close

Responsibilities:
  - 6-gate deterministic cognitive/systematic bias detection spine
  - Detect sector concentration risk in open positions
  - Detect recency bias (trading the same narrow set of tickers)
  - Detect loss aversion (holding losers far longer than winners)
  - Detect overtrading (excessive trade frequency)
  - Detect disposition effect (cutting winners short, letting losers run)
  - Detect confidence clustering (only acting on HIGH signals)
  - Write notifications for actionable findings

No LLM in any decision path. All gate logic is deterministic and traceable.

Data sources:
  - Internal DB (positions, signals, ledger, system_log)

Usage:
  python3 retail_bias_detection_agent.py
  python3 retail_bias_detection_agent.py --customer-id <uuid>
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
from retail_sector_map import is_excluded_from_concentration

# ── CONFIG ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')
_CUSTOMER_ID      = None   # set from --customer-id arg

# Gate 1: Sector concentration thresholds
SECTOR_CONC_WARN_PCT     = 40     # >40% of portfolio in one sector = WARNING
SECTOR_CONC_CRIT_PCT     = 60     # >60% = CRITICAL

# Gate 2: Recency bias
RECENCY_LOOKBACK_DAYS    = 5      # how far back to look for recent trades
RECENCY_TRADE_COUNT      = 10     # last N trades to examine
RECENCY_UNIQUE_WARN      = 3      # fewer unique tickers than this = WARNING

# Gate 3: Loss aversion
LOSS_AVERSION_DAYS       = 30     # closed positions in last N days
LOSS_AVERSION_RATIO      = 2.0    # losers held 2x+ longer than winners = WARNING

# Gate 4: Overtrading
OVERTRADE_DAYS           = 5      # look at last N trading days
OVERTRADE_WARN_PER_DAY   = 10     # avg trades/day threshold for WARNING
OVERTRADE_CRIT_PER_DAY   = 20     # avg trades/day threshold for CRITICAL

# Gate 5: Disposition effect (no separate threshold — logic-based)

# Gate 6: Confidence clustering
CONFIDENCE_HIGH_ONLY_PCT = 80     # >80% of recent trades from HIGH-only signals = INFO

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('bias_detection_agent')


# ── SEVERITY ──────────────────────────────────────────────────────────────

class Severity:
    OK       = "OK"
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class Finding:
    gate: str
    severity: str
    code: str
    message: str
    detail: str = ""


@dataclass
class BiasReport:
    """Aggregated output of all bias detection gates."""
    findings: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    def add(self, finding: Finding):
        self.findings.append(finding)

    @property
    def worst_severity(self):
        severities = [Severity.OK, Severity.INFO, Severity.WARNING, Severity.CRITICAL]
        worst = 0
        for f in self.findings:
            idx = severities.index(f.severity) if f.severity in severities else 0
            worst = max(worst, idx)
        return severities[worst]

    @property
    def critical_count(self):
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self):
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    def summary(self):
        return (f"Bias scan complete: {self.critical_count} critical, "
                f"{self.warning_count} warning, {len(self.findings)} total checks")


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


def _now_et():
    return datetime.now(ET)


def _now_str():
    return datetime.now(tz=ZoneInfo("UTC")).strftime('%Y-%m-%d %H:%M:%S')


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1: SECTOR CONCENTRATION
#  Check if open positions are over-concentrated in one sector
# ══════════════════════════════════════════════════════════════════════════

def gate1_sector_concentration(report: BiasReport, db):
    """Flag when too much portfolio weight sits in a single sector."""
    log.info("[GATE 1] Sector concentration check")

    positions = db.get_open_positions()
    if not positions:
        report.add(Finding(
            gate="GATE1_SECTOR_CONC",
            severity=Severity.OK,
            code="NO_POSITIONS",
            message="No open positions — sector concentration N/A"
        ))
        return

    # Calculate portfolio value per sector.
    # Reserves (Cash/Reserve, Fixed Income), broad-market ETFs (Diversified),
    # and Unknown (data quality gap) are tracked separately and NEVER contribute
    # to behavioral-bias concentration findings — they are not sector bets.
    sector_value = {}           # equity sector → value  (used for concentration)
    excluded_value = {}         # reserves/unknown → value (tracked for transparency)
    total_value = 0.0
    unknown_value = 0.0
    for pos in positions:
        sector = (pos.get('sector') or 'Unknown').strip() or 'Unknown'
        price = float(pos.get('current_price') or pos.get('entry_price') or 0)
        shares = float(pos.get('shares') or 0)
        value = price * shares
        total_value += value
        if is_excluded_from_concentration(sector):
            excluded_value[sector] = excluded_value.get(sector, 0.0) + value
            if sector in ('Unknown', ''):
                unknown_value += value
        else:
            sector_value[sector] = sector_value.get(sector, 0.0) + value

    if total_value <= 0:
        report.add(Finding(
            gate="GATE1_SECTOR_CONC",
            severity=Severity.OK,
            code="ZERO_VALUE",
            message="Portfolio value is $0 — sector concentration N/A"
        ))
        return

    # Data-quality signal: positions with unresolved sector. This is a WARNING
    # (worth surfacing so the backfill agent can fill the gap) but NEVER
    # CRITICAL and NEVER produces a sector-block restriction.
    if unknown_value > 0:
        unknown_pct = round((unknown_value / total_value) * 100, 1)
        if unknown_pct >= 25.0:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.WARNING,
                code="SECTOR_DATA_INCOMPLETE",
                message=f"{unknown_pct}% of portfolio has no sector classification",
                detail=f"${unknown_value:,.2f} of ${total_value:,.2f} — "
                       f"retail_sector_backfill_agent will fill this on next run"
            ))

    # Check each equity sector's percentage — reserves/unknowns are already
    # excluded from sector_value above, so concentration math is honest.
    flagged = False
    for sector, value in sorted(sector_value.items(), key=lambda x: -x[1]):
        pct = round((value / total_value) * 100, 1)

        if pct > SECTOR_CONC_CRIT_PCT:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.CRITICAL,
                code=f"SECTOR_CRIT_{sector[:12].upper().replace(' ', '_')}",
                message=f"Sector '{sector}' is {pct}% of portfolio (>${SECTOR_CONC_CRIT_PCT}%)",
                detail=f"Value: ${value:,.2f} of ${total_value:,.2f} total across {len(positions)} positions"
            ))
            flagged = True
        elif pct > SECTOR_CONC_WARN_PCT:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.WARNING,
                code=f"SECTOR_WARN_{sector[:12].upper().replace(' ', '_')}",
                message=f"Sector '{sector}' is {pct}% of portfolio (>{SECTOR_CONC_WARN_PCT}%)",
                detail=f"Value: ${value:,.2f} of ${total_value:,.2f} total across {len(positions)} positions"
            ))
            flagged = True

    if not flagged:
        top_sector = max(sector_value, key=sector_value.get)
        top_pct = round((sector_value[top_sector] / total_value) * 100, 1)
        report.add(Finding(
            gate="GATE1_SECTOR_CONC",
            severity=Severity.OK,
            code="SECTOR_BALANCED",
            message=f"Sector balance OK — largest is '{top_sector}' at {top_pct}%",
            detail=f"{len(sector_value)} sectors across {len(positions)} positions"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2: RECENCY BIAS
#  Check if recent trades are concentrated in the same 1-2 tickers/sectors
# ══════════════════════════════════════════════════════════════════════════

def gate2_recency_bias(report: BiasReport, db):
    """Flag when the last N trades are all in the same narrow set of tickers."""
    log.info("[GATE 2] Recency bias check")

    # Use ledger ENTRY rows as proxy for trades (each open_position writes one)
    with db.conn() as c:
        cutoff = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=RECENCY_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
        rows = c.execute(
            "SELECT * FROM ledger WHERE type='ENTRY' AND date >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, RECENCY_TRADE_COUNT)
        ).fetchall()

    trades = [dict(r) for r in rows]
    if len(trades) < 3:
        report.add(Finding(
            gate="GATE2_RECENCY",
            severity=Severity.OK,
            code="TOO_FEW_TRADES",
            message=f"Only {len(trades)} trade(s) in last {RECENCY_LOOKBACK_DAYS} days — recency check N/A"
        ))
        return

    # Extract tickers from ledger descriptions (format: "AAPL · 1.2345 sh @ $150.00")
    tickers = set()
    for t in trades:
        desc = t.get('description', '')
        parts = desc.split(' · ')
        if parts:
            ticker = parts[0].strip()
            if ticker:
                tickers.add(ticker)

    unique_count = len(tickers)
    if unique_count < RECENCY_UNIQUE_WARN:
        report.add(Finding(
            gate="GATE2_RECENCY",
            severity=Severity.WARNING,
            code="RECENCY_BIAS",
            message=f"Only {unique_count} unique ticker(s) in last {len(trades)} trades — possible recency bias",
            detail=f"Tickers traded: {', '.join(sorted(tickers))}"
        ))
    else:
        report.add(Finding(
            gate="GATE2_RECENCY",
            severity=Severity.OK,
            code="RECENCY_OK",
            message=f"{unique_count} unique tickers in last {len(trades)} trades — diversified"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 3: LOSS AVERSION
#  Check if losing positions are held much longer than winning ones
# ══════════════════════════════════════════════════════════════════════════

def gate3_loss_aversion(report: BiasReport, db):
    """Flag when losers are held significantly longer than winners."""
    log.info("[GATE 3] Loss aversion check")

    closed = db.get_closed_positions(limit=200)

    # Filter to last N days
    now = datetime.now(tz=ZoneInfo("UTC"))
    cutoff = now - timedelta(days=LOSS_AVERSION_DAYS)
    recent_closed = []
    for pos in closed:
        closed_at_str = pos.get('closed_at', '')
        if not closed_at_str:
            continue
        try:
            closed_dt = datetime.fromisoformat(closed_at_str.replace('Z', ''))
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=ZoneInfo("UTC"))
            if closed_dt >= cutoff:
                recent_closed.append(pos)
        except (ValueError, TypeError):
            continue

    if len(recent_closed) < 4:
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.OK,
            code="TOO_FEW_CLOSED",
            message=f"Only {len(recent_closed)} closed position(s) in last {LOSS_AVERSION_DAYS} days — loss aversion check N/A"
        ))
        return

    # Separate winners and losers, calculate hold times
    winner_hold_days = []
    loser_hold_days = []

    for pos in recent_closed:
        pnl = float(pos.get('pnl', 0))
        opened_str = pos.get('opened_at', '')
        closed_str = pos.get('closed_at', '')
        if not opened_str or not closed_str:
            continue
        try:
            opened_dt = datetime.fromisoformat(opened_str.replace('Z', ''))
            closed_dt = datetime.fromisoformat(closed_str.replace('Z', ''))
            hold_days = max((closed_dt - opened_dt).total_seconds() / 86400, 0.01)
        except (ValueError, TypeError):
            continue

        if pnl >= 0:
            winner_hold_days.append(hold_days)
        else:
            loser_hold_days.append(hold_days)

    if not winner_hold_days or not loser_hold_days:
        label = "all winners" if not loser_hold_days else "all losers"
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.OK,
            code="ONE_SIDED_RESULTS",
            message=f"Recent closed positions are {label} — loss aversion ratio N/A"
        ))
        return

    avg_winner_hold = sum(winner_hold_days) / len(winner_hold_days)
    avg_loser_hold = sum(loser_hold_days) / len(loser_hold_days)

    if avg_winner_hold > 0:
        ratio = avg_loser_hold / avg_winner_hold
    else:
        ratio = 0

    if ratio >= LOSS_AVERSION_RATIO:
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.WARNING,
            code="LOSS_AVERSION",
            message=f"Losers held {ratio:.1f}x longer than winners (threshold: {LOSS_AVERSION_RATIO}x)",
            detail=(f"Avg winner hold: {avg_winner_hold:.1f}d ({len(winner_hold_days)} trades) | "
                    f"Avg loser hold: {avg_loser_hold:.1f}d ({len(loser_hold_days)} trades)")
        ))
    else:
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.OK,
            code="LOSS_AVERSION_OK",
            message=f"Loser/winner hold ratio: {ratio:.1f}x (threshold: {LOSS_AVERSION_RATIO}x)",
            detail=(f"Avg winner: {avg_winner_hold:.1f}d | Avg loser: {avg_loser_hold:.1f}d")
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 4: OVERTRADING
#  Check if trade frequency is excessive
# ══════════════════════════════════════════════════════════════════════════

def gate4_overtrading(report: BiasReport, db):
    """Flag when average trades per day over the lookback window is too high."""
    log.info("[GATE 4] Overtrading check")

    # Count ENTRY ledger rows per day over the last N trading days
    now = datetime.now(tz=ZoneInfo("UTC"))
    cutoff = (now - timedelta(days=OVERTRADE_DAYS)).strftime('%Y-%m-%d')

    with db.conn() as c:
        rows = c.execute(
            "SELECT date, COUNT(*) as cnt FROM ledger "
            "WHERE type='ENTRY' AND date >= ? "
            "GROUP BY date ORDER BY date DESC",
            (cutoff,)
        ).fetchall()

    day_counts = [dict(r) for r in rows]

    if not day_counts:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.OK,
            code="NO_TRADES",
            message=f"No trades in last {OVERTRADE_DAYS} days — overtrading check N/A"
        ))
        return

    total_trades = sum(d['cnt'] for d in day_counts)
    trading_days = len(day_counts)
    avg_per_day = total_trades / trading_days if trading_days > 0 else 0

    if avg_per_day > OVERTRADE_CRIT_PER_DAY:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.CRITICAL,
            code="OVERTRADING_CRIT",
            message=f"Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) (>{OVERTRADE_CRIT_PER_DAY})",
            detail=f"Total: {total_trades} trades across {trading_days} trading day(s)"
        ))
    elif avg_per_day > OVERTRADE_WARN_PER_DAY:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.WARNING,
            code="OVERTRADING_WARN",
            message=f"Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) (>{OVERTRADE_WARN_PER_DAY})",
            detail=f"Total: {total_trades} trades across {trading_days} trading day(s)"
        ))
    else:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.OK,
            code="TRADE_FREQ_OK",
            message=f"Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) — normal"
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 5: DISPOSITION EFFECT
#  Check if profits are taken too early while losses are allowed to run
# ══════════════════════════════════════════════════════════════════════════

def gate5_disposition_effect(report: BiasReport, db):
    """Flag when winners are cut short and losers run — the disposition effect."""
    log.info("[GATE 5] Disposition effect check")

    closed = db.get_closed_positions(limit=200)

    # Filter to last 30 days
    now = datetime.now(tz=ZoneInfo("UTC"))
    cutoff = now - timedelta(days=30)
    recent = []
    for pos in closed:
        closed_at_str = pos.get('closed_at', '')
        if not closed_at_str:
            continue
        try:
            closed_dt = datetime.fromisoformat(closed_at_str.replace('Z', ''))
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=ZoneInfo("UTC"))
            if closed_dt >= cutoff:
                recent.append(pos)
        except (ValueError, TypeError):
            continue

    if len(recent) < 4:
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.OK,
            code="TOO_FEW_CLOSED",
            message=f"Only {len(recent)} closed position(s) in last 30 days — disposition check N/A"
        ))
        return

    # Calculate gain/loss percentages
    winners = []
    losers = []
    for pos in recent:
        entry = float(pos.get('entry_price', 0))
        if entry <= 0:
            continue
        pnl = float(pos.get('pnl', 0))
        shares = float(pos.get('shares', 0))
        if shares <= 0:
            continue
        # pnl is total dollar P&L; calculate percentage
        cost = entry * shares
        pnl_pct = (pnl / cost) * 100 if cost > 0 else 0

        if pnl >= 0:
            winners.append(pnl_pct)
        else:
            losers.append(abs(pnl_pct))  # store as positive for comparison

    if not winners or not losers:
        label = "all winners" if not losers else "all losers"
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.OK,
            code="ONE_SIDED",
            message=f"Recent closed positions are {label} — disposition check N/A"
        ))
        return

    avg_winner_gain = sum(winners) / len(winners)
    avg_loser_loss = sum(losers) / len(losers)

    # Disposition effect: small winners + big losers AND more winners than losers
    # (taking profits too early, letting losses run)
    if avg_winner_gain < avg_loser_loss and len(winners) > len(losers):
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.WARNING,
            code="DISPOSITION_EFFECT",
            message=(f"Disposition effect: avg winner +{avg_winner_gain:.1f}% vs "
                     f"avg loser -{avg_loser_loss:.1f}% with {len(winners)}W/{len(losers)}L"),
            detail="Cutting winners short while letting losers run — consider wider profit targets or tighter stop losses"
        ))
    else:
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.OK,
            code="DISPOSITION_OK",
            message=(f"No disposition effect: avg winner +{avg_winner_gain:.1f}% vs "
                     f"avg loser -{avg_loser_loss:.1f}% ({len(winners)}W/{len(losers)}L)")
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 6: CONFIDENCE CLUSTERING
#  Check if the system only acts on HIGH confidence signals
# ══════════════════════════════════════════════════════════════════════════

def gate6_confidence_clustering(report: BiasReport, db):
    """Flag when the vast majority of acted-on signals are HIGH confidence only."""
    log.info("[GATE 6] Confidence clustering check")

    # Query signals that have been acted on (ACTED_ON status) in recent history
    now = datetime.now(tz=ZoneInfo("UTC"))
    cutoff = (now - timedelta(days=30)).strftime('%Y-%m-%d')

    with db.conn() as c:
        rows = c.execute(
            "SELECT confidence, COUNT(*) as cnt FROM signals "
            "WHERE status='ACTED_ON' AND updated_at >= ? "
            "GROUP BY confidence",
            (cutoff,)
        ).fetchall()

    if not rows:
        report.add(Finding(
            gate="GATE6_CONFIDENCE",
            severity=Severity.OK,
            code="NO_ACTED_SIGNALS",
            message="No acted-on signals in last 30 days — confidence clustering N/A"
        ))
        return

    conf_counts = {r['confidence']: r['cnt'] for r in rows}
    total = sum(conf_counts.values())
    high_count = conf_counts.get('HIGH', 0)

    if total <= 0:
        return

    high_pct = round((high_count / total) * 100, 1)

    if high_pct > CONFIDENCE_HIGH_ONLY_PCT:
        other_str = ', '.join(f"{k}={v}" for k, v in conf_counts.items() if k != 'HIGH')
        report.add(Finding(
            gate="GATE6_CONFIDENCE",
            severity=Severity.INFO,
            code="CONFIDENCE_CLUSTERING",
            message=f"{high_pct}% of acted-on signals are HIGH confidence ({high_count}/{total})",
            detail=f"Other: {other_str or 'none'} — system may be ignoring viable MEDIUM signals"
        ))
    else:
        report.add(Finding(
            gate="GATE6_CONFIDENCE",
            severity=Severity.OK,
            code="CONFIDENCE_OK",
            message=f"Confidence mix: {high_pct}% HIGH ({high_count}/{total}) — balanced",
            detail=', '.join(f"{k}={v}" for k, v in sorted(conf_counts.items()))
        ))


# ══════════════════════════════════════════════════════════════════════════
#  MAIN RUN FUNCTION
# ══════════════════════════════════════════════════════════════════════════

def _get_active_customer_ids():
    """Get all active customer IDs from the auth database."""
    try:
        sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
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


def run():
    """Execute the 6-gate bias detection spine."""
    db = _master_db()

    # ── Lifecycle: START ──────────────────────────────────────────────
    db.log_event("AGENT_START", agent="Bias Detection", details="bias scan")
    db.log_heartbeat("bias_detection_agent", "RUNNING")
    log.info("=" * 70)
    log.info("BIAS DETECTION AGENT — Starting 6-gate cognitive bias scan")
    log.info("=" * 70)

    report = BiasReport(started_at=_now_str())

    # ── Per-customer gates (1-5) ─────────────────────────────────────
    customer_ids = _get_active_customer_ids()
    if not customer_ids:
        log.warning("No active customers found — running gates against master DB only")
        customer_ids = [OWNER_CUSTOMER_ID] if OWNER_CUSTOMER_ID else []

    for cid in customer_ids:
        try:
            cdb = get_customer_db(cid) if cid else db
            short_id = cid[:8] if cid else "master"
            log.info(f"--- Customer {short_id} ---")

            # ── Gate 1: Sector concentration ─────────────────────────
            try:
                gate1_sector_concentration(report, cdb)
            except Exception as e:
                log.error(f"Gate 1 failed for {short_id}: {e}", exc_info=True)
                report.add(Finding("GATE1_SECTOR_CONC", Severity.WARNING, "GATE1_ERROR",
                                   f"Sector concentration check failed ({short_id}): {e}"))

            # ── Gate 2: Recency bias ─────────────────────────────────
            try:
                gate2_recency_bias(report, cdb)
            except Exception as e:
                log.error(f"Gate 2 failed for {short_id}: {e}", exc_info=True)
                report.add(Finding("GATE2_RECENCY", Severity.WARNING, "GATE2_ERROR",
                                   f"Recency bias check failed ({short_id}): {e}"))

            # ── Gate 3: Loss aversion ────────────────────────────────
            try:
                gate3_loss_aversion(report, cdb)
            except Exception as e:
                log.error(f"Gate 3 failed for {short_id}: {e}", exc_info=True)
                report.add(Finding("GATE3_LOSS_AVERSION", Severity.WARNING, "GATE3_ERROR",
                                   f"Loss aversion check failed ({short_id}): {e}"))

            # ── Gate 4: Overtrading ──────────────────────────────────
            try:
                gate4_overtrading(report, cdb)
            except Exception as e:
                log.error(f"Gate 4 failed for {short_id}: {e}", exc_info=True)
                report.add(Finding("GATE4_OVERTRADING", Severity.WARNING, "GATE4_ERROR",
                                   f"Overtrading check failed ({short_id}): {e}"))

            # ── Gate 5: Disposition effect ───────────────────────────
            try:
                gate5_disposition_effect(report, cdb)
            except Exception as e:
                log.error(f"Gate 5 failed for {short_id}: {e}", exc_info=True)
                report.add(Finding("GATE5_DISPOSITION", Severity.WARNING, "GATE5_ERROR",
                                   f"Disposition effect check failed ({short_id}): {e}"))

        except Exception as e:
            log.error(f"Customer {cid[:8] if cid else '?'} failed: {e}", exc_info=True)
            report.add(Finding("BIAS_CUSTOMER", Severity.WARNING, "CUSTOMER_ERROR",
                               f"Customer {cid[:8] if cid else '?'}: bias checks failed: {e}"))

    # ── Gate 6: Confidence clustering (shared signals DB) ────────────
    try:
        gate6_confidence_clustering(report, db)
    except Exception as e:
        log.error(f"Gate 6 failed: {e}", exc_info=True)
        report.add(Finding("GATE6_CONFIDENCE", Severity.WARNING, "GATE6_ERROR",
                           f"Confidence clustering check failed: {e}"))

    # ── Aggregate results ─────────────────────────────────────────────
    report.completed_at = _now_str()

    # Log each finding
    for f in report.findings:
        if f.severity == Severity.CRITICAL:
            log.error(f"  [{f.gate}] CRITICAL: {f.message}")
        elif f.severity == Severity.WARNING:
            log.warning(f"  [{f.gate}] WARNING: {f.message}")
        elif f.severity == Severity.INFO:
            log.info(f"  [{f.gate}] INFO: {f.message}")
        else:
            log.info(f"  [{f.gate}] OK: {f.message}")

    # ── Raise admin alerts for WARNING+ bias findings ────────────────
    # Bias findings describe portfolio-level problems (concentration,
    # loss aversion, overtrading). By policy these go to admin, not the
    # customer — the idea is the customer forgets the account exists
    # until someone asks them about it. Constant flags erode confidence.
    # Admin can decide whether / how to coach the customer.
    admin_db = _master_db()
    actionable = [f for f in report.findings
                  if f.severity in (Severity.WARNING, Severity.CRITICAL)]
    for af in actionable:
        try:
            admin_db.add_admin_alert(
                category='bias',
                severity=af.severity,
                title=f"Bias Alert: {af.code}",
                body=f"{af.message}\n{af.detail}" if af.detail else af.message,
                source_agent='bias_detection_agent',
                source_customer_id=_CUSTOMER_ID or OWNER_CUSTOMER_ID,
                code=af.code,
                meta={"gate": af.gate, "code": af.code, "severity": af.severity},
            )
        except Exception as e:
            log.warning(f"Failed to write bias admin alert: {e}")

    # ── Store scan summary for portal access ─────────────────────────
    scan_summary = {
        "timestamp": report.completed_at,
        "worst_severity": report.worst_severity,
        "critical": report.critical_count,
        "warnings": report.warning_count,
        "total_checks": len(report.findings),
        "findings": [
            {"gate": f.gate, "severity": f.severity, "code": f.code, "message": f.message}
            for f in report.findings
            if f.severity in (Severity.CRITICAL, Severity.WARNING, Severity.INFO)
        ]
    }
    db.set_setting('_BIAS_SCAN_LAST', json.dumps(scan_summary))

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    summary = report.summary()
    log.info("=" * 70)
    log.info(f"BIAS DETECTION — {summary}")
    log.info(f"  Worst severity: {report.worst_severity}")
    log.info("=" * 70)

    db.log_heartbeat("bias_detection_agent", "OK")
    db.log_event(
        "AGENT_COMPLETE",
        agent="Bias Detection",
        details=f"severity={report.worst_severity}, critical={report.critical_count}, "
                f"warn={report.warning_count}, checks={len(report.findings)}"
    )

    # ── Monitor heartbeat POST ────────────────────────────────────────
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="bias_detection_agent", status="OK")
    except Exception as e:
        log.warning(f"Monitor heartbeat POST failed: {e}")

    return report


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — Bias Detection Agent')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (for per-customer mode)')
    args = parser.parse_args()

    if args.customer_id:
        _CUSTOMER_ID = args.customer_id

    acquire_agent_lock("retail_bias_detection_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
