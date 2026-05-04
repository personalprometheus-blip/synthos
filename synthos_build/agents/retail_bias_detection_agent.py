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
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_customer_db, get_shared_db, acquire_agent_lock, release_agent_lock
from retail_sector_map import is_excluded_from_concentration
from retail_shared import emit_admin_alert, get_active_customers

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

# Gates 3 & 5: Behavioral-bias sample floor.
# At small N a single outlier dominates the avg and produces noise findings
# (e.g. "3W/2L disposition effect!" that's pure small-sample artifact).
# 10 closed trades is the floor where the avg starts reflecting actual
# behavior. Used by both gate3_loss_aversion and gate5_disposition_effect.
MIN_BEHAVIORAL_SAMPLE    = 10

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
    # Bias findings are per-customer almost always (gate 6 confidence
    # clustering is the only system-wide one). When set, retail_shared.
    # emit_admin_alert routes the admin_alert with the right
    # source_customer_id so admin can tell which customer has the bias.
    customer_id: str | None = None
    meta: dict = field(default_factory=dict)


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
    """Shared market-intelligence DB. 2026-04-27: was previously
    get_customer_db(OWNER_CUSTOMER_ID). See retail_database.get_shared_db()
    for rationale."""
    return get_shared_db()


def _now_et():
    return datetime.now(ET)


def _now_str():
    return datetime.now(tz=ZoneInfo("UTC")).strftime('%Y-%m-%d %H:%M:%S')


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1: SECTOR CONCENTRATION
#  Check if open positions are over-concentrated in one sector
# ══════════════════════════════════════════════════════════════════════════

def _sector_token(sector: str) -> str:
    """Sector → uppercase-snake token for use inside admin_alert codes.
    Full-length (no truncation) — collisions across sectors with similar
    prefixes were a real risk before."""
    return sector.upper().replace(' ', '_').replace('-', '_')


def gate1_sector_concentration(report: BiasReport, db, cid: str):
    """Flag when too much portfolio weight sits in a single sector."""
    short_id = cid[:8] if cid else 'master'
    log.info(f"[GATE 1] Sector concentration check ({short_id})")

    positions = db.get_open_positions()
    if not positions:
        report.add(Finding(
            gate="GATE1_SECTOR_CONC",
            severity=Severity.OK,
            code=f"NO_POSITIONS_{short_id}",
            message=f"{short_id}: No open positions — sector concentration N/A",
            customer_id=cid,
        ))
        return

    # Calculate portfolio value per sector.
    # Reserves (Cash/Reserve, Fixed Income), broad-market ETFs (Diversified),
    # and Unknown (data quality gap) are tracked separately and NEVER contribute
    # to behavioral-bias concentration findings — they are not sector bets.
    #
    # Concentration is a *portfolio-level risk* (different from the behavioral
    # gates 2-5 which only score the bot's own trades). Both bot- and user-
    # controlled positions count toward concentration, but the message
    # attributes them separately so admin can tell whether the bot is driving
    # the concentration or just inheriting it from the customer's own buys.
    sector_value = {}           # equity sector → total value (bot + user)
    sector_bot   = {}           # equity sector → bot-managed value
    sector_user  = {}           # equity sector → user-managed value
    total_value = 0.0
    unknown_value = 0.0
    for pos in positions:
        sector = (pos.get('sector') or 'Unknown').strip() or 'Unknown'
        price = float(pos.get('current_price') or pos.get('entry_price') or 0)
        shares = float(pos.get('shares') or 0)
        value = price * shares
        managed_by = pos.get('managed_by', 'bot')
        total_value += value
        if is_excluded_from_concentration(sector):
            if sector in ('Unknown', ''):
                unknown_value += value
        else:
            sector_value[sector] = sector_value.get(sector, 0.0) + value
            if managed_by == 'user':
                sector_user[sector] = sector_user.get(sector, 0.0) + value
            else:
                sector_bot[sector] = sector_bot.get(sector, 0.0) + value

    if total_value <= 0:
        report.add(Finding(
            gate="GATE1_SECTOR_CONC",
            severity=Severity.OK,
            code=f"ZERO_VALUE_{short_id}",
            message=f"{short_id}: Portfolio value is $0 — sector concentration N/A",
            customer_id=cid,
        ))
        return

    # Data-quality signal: positions with unresolved sector. This is INFO
    # (data-quality observation, not a trading-relevant finding) — the
    # backfill agent fills this async and the trader doesn't gate on it.
    # Previously WARNING, which rolled up into SYSTEM_WARNINGS_ACTIVE on
    # the validator and tripped every customer's verdict to CAUTION even
    # though nothing was blocking trades.
    if unknown_value > 0:
        unknown_pct = round((unknown_value / total_value) * 100, 1)
        if unknown_pct >= 25.0:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.INFO,
                code=f"SECTOR_DATA_INCOMPLETE_{short_id}",
                message=f"{short_id}: {unknown_pct}% of portfolio has no sector classification",
                detail=f"${unknown_value:,.2f} of ${total_value:,.2f} — "
                       f"retail_sector_backfill_agent will fill this on next run",
                customer_id=cid,
            ))

    # Check each equity sector's percentage — reserves/unknowns are already
    # excluded from sector_value above, so concentration math is honest.
    flagged = False
    for sector, value in sorted(sector_value.items(), key=lambda x: -x[1]):
        pct = round((value / total_value) * 100, 1)
        sector_tok = _sector_token(sector)
        bot_v  = sector_bot.get(sector, 0.0)
        user_v = sector_user.get(sector, 0.0)
        bot_pct  = round((bot_v / total_value) * 100, 1) if total_value else 0
        user_pct = round((user_v / total_value) * 100, 1) if total_value else 0

        # Attribution detail tells admin whether the bot is driving the
        # concentration or inheriting it from user-controlled buys.
        attribution = f"bot {bot_pct}% / user {user_pct}%"

        if pct > SECTOR_CONC_CRIT_PCT:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.CRITICAL,
                code=f"SECTOR_CRIT_{short_id}_{sector_tok}",
                message=(f"{short_id}: Sector '{sector}' is {pct}% of portfolio "
                         f"(>{SECTOR_CONC_CRIT_PCT}%) — {attribution}"),
                detail=(f"Value: ${value:,.2f} of ${total_value:,.2f} "
                        f"(bot ${bot_v:,.2f}, user ${user_v:,.2f}) "
                        f"across {len(positions)} positions"),
                customer_id=cid,
                meta={"sector": sector, "pct": pct,
                      "bot_pct": bot_pct, "user_pct": user_pct},
            ))
            flagged = True
        elif pct > SECTOR_CONC_WARN_PCT:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.WARNING,
                code=f"SECTOR_WARN_{short_id}_{sector_tok}",
                message=(f"{short_id}: Sector '{sector}' is {pct}% of portfolio "
                         f"(>{SECTOR_CONC_WARN_PCT}%) — {attribution}"),
                detail=(f"Value: ${value:,.2f} of ${total_value:,.2f} "
                        f"(bot ${bot_v:,.2f}, user ${user_v:,.2f}) "
                        f"across {len(positions)} positions"),
                customer_id=cid,
                meta={"sector": sector, "pct": pct,
                      "bot_pct": bot_pct, "user_pct": user_pct},
            ))
            flagged = True

    if not flagged:
        # Empty-dict guard: if every position is in an excluded category
        # (reserves / broad ETFs / Unknown) then sector_value is empty and
        # max() would raise "iterable argument is empty" — the real cause
        # of the GATE1_ERROR warning we were seeing on customers with all
        # positions in BIL reserves or unresolved sectors.
        if not sector_value:
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.OK,
                code=f"NO_CLASSIFIED_SECTORS_{short_id}",
                message=f"{short_id}: No classified equity sectors to evaluate",
                detail=(f"${total_value:,.2f} sits in reserves or unresolved "
                        f"positions — nothing to concentrate on yet"),
                customer_id=cid,
            ))
        else:
            top_sector = max(sector_value, key=sector_value.get)
            top_pct = round((sector_value[top_sector] / total_value) * 100, 1)
            report.add(Finding(
                gate="GATE1_SECTOR_CONC",
                severity=Severity.OK,
                code=f"SECTOR_BALANCED_{short_id}",
                message=f"{short_id}: Sector balance OK — largest is '{top_sector}' at {top_pct}%",
                detail=f"{len(sector_value)} sectors across {len(positions)} positions",
                customer_id=cid,
            ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2: RECENCY BIAS
#  Check if recent trades are concentrated in the same 1-2 tickers/sectors
# ══════════════════════════════════════════════════════════════════════════

def gate2_recency_bias(report: BiasReport, db, cid: str):
    """Flag when the last N trades are all in the same narrow set of tickers."""
    short_id = cid[:8] if cid else 'master'
    log.info(f"[GATE 2] Recency bias check ({short_id})")

    # Use ledger ENTRY rows as proxy for trades (each open_position writes one).
    # Filter to bot-managed positions only — Gates 2-5 score the BOT's
    # behavioral biases. User-controlled trades aren't algorithmic biases to
    # detect; they're customer choices we can't fix in code.
    with db.conn() as c:
        cutoff = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(days=RECENCY_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
        rows = c.execute(
            "SELECT l.* FROM ledger l "
            "JOIN positions p ON p.id = l.position_id "
            "WHERE l.type='ENTRY' AND l.date >= ? AND p.managed_by='bot' "
            "ORDER BY l.created_at DESC LIMIT ?",
            (cutoff, RECENCY_TRADE_COUNT)
        ).fetchall()

    trades = [dict(r) for r in rows]
    if len(trades) < 3:
        report.add(Finding(
            gate="GATE2_RECENCY",
            severity=Severity.OK,
            code=f"TOO_FEW_TRADES_{short_id}",
            message=f"{short_id}: Only {len(trades)} bot trade(s) in last {RECENCY_LOOKBACK_DAYS} days — recency check N/A",
            customer_id=cid,
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
            code=f"RECENCY_BIAS_{short_id}",
            message=f"{short_id}: Only {unique_count} unique ticker(s) in last {len(trades)} trades — possible recency bias",
            detail=f"Tickers traded: {', '.join(sorted(tickers))}",
            customer_id=cid,
        ))
    else:
        report.add(Finding(
            gate="GATE2_RECENCY",
            severity=Severity.OK,
            code=f"RECENCY_OK_{short_id}",
            message=f"{short_id}: {unique_count} unique tickers in last {len(trades)} trades — diversified",
            customer_id=cid,
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 3: LOSS AVERSION
#  Check if losing positions are held much longer than winning ones
# ══════════════════════════════════════════════════════════════════════════

def gate3_loss_aversion(report: BiasReport, db, cid: str):
    """Flag when losers are held significantly longer than winners."""
    short_id = cid[:8] if cid else 'master'
    log.info(f"[GATE 3] Loss aversion check ({short_id})")

    # Filter to bot-managed positions only — loss aversion is a behavioral
    # bias of the algorithm. User-controlled exits are customer choice.
    closed = [p for p in db.get_closed_positions(limit=200)
              if p.get('managed_by', 'bot') == 'bot']

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

    # Aligned with gate5 — 10 closed trades is the floor where avg starts
    # reflecting behavior rather than a single-outlier coin flip.
    if len(recent_closed) < MIN_BEHAVIORAL_SAMPLE:
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.OK,
            code=f"TOO_FEW_CLOSED_{short_id}",
            message=(f"{short_id}: Only {len(recent_closed)} bot-closed position(s) in last "
                     f"{LOSS_AVERSION_DAYS} days — loss aversion check needs ≥{MIN_BEHAVIORAL_SAMPLE}"),
            customer_id=cid,
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
            code=f"ONE_SIDED_RESULTS_{short_id}",
            message=f"{short_id}: Recent closed positions are {label} — loss aversion ratio N/A",
            customer_id=cid,
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
            code=f"LOSS_AVERSION_{short_id}",
            message=f"{short_id}: Losers held {ratio:.1f}x longer than winners (threshold: {LOSS_AVERSION_RATIO}x)",
            detail=(f"Avg winner hold: {avg_winner_hold:.1f}d ({len(winner_hold_days)} trades) | "
                    f"Avg loser hold: {avg_loser_hold:.1f}d ({len(loser_hold_days)} trades)"),
            customer_id=cid,
        ))
    else:
        report.add(Finding(
            gate="GATE3_LOSS_AVERSION",
            severity=Severity.OK,
            code=f"LOSS_AVERSION_OK_{short_id}",
            message=f"{short_id}: Loser/winner hold ratio: {ratio:.1f}x (threshold: {LOSS_AVERSION_RATIO}x)",
            detail=(f"Avg winner: {avg_winner_hold:.1f}d | Avg loser: {avg_loser_hold:.1f}d"),
            customer_id=cid,
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 4: OVERTRADING
#  Check if trade frequency is excessive
# ══════════════════════════════════════════════════════════════════════════

def gate4_overtrading(report: BiasReport, db, cid: str):
    """Flag when average trades per day over the lookback window is too high."""
    short_id = cid[:8] if cid else 'master'
    log.info(f"[GATE 4] Overtrading check ({short_id})")

    # Count ENTRY ledger rows per day over the last N trading days.
    # Filter to bot-managed positions — overtrading by a USER on their own
    # picks isn't an algorithmic bias to detect.
    now = datetime.now(tz=ZoneInfo("UTC"))
    cutoff = (now - timedelta(days=OVERTRADE_DAYS)).strftime('%Y-%m-%d')

    with db.conn() as c:
        rows = c.execute(
            "SELECT l.date, COUNT(*) as cnt FROM ledger l "
            "JOIN positions p ON p.id = l.position_id "
            "WHERE l.type='ENTRY' AND l.date >= ? AND p.managed_by='bot' "
            "GROUP BY l.date ORDER BY l.date DESC",
            (cutoff,)
        ).fetchall()

    day_counts = [dict(r) for r in rows]

    if not day_counts:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.OK,
            code=f"NO_TRADES_{short_id}",
            message=f"{short_id}: No bot trades in last {OVERTRADE_DAYS} days — overtrading check N/A",
            customer_id=cid,
        ))
        return

    total_trades = sum(d['cnt'] for d in day_counts)
    trading_days = len(day_counts)
    avg_per_day = total_trades / trading_days if trading_days > 0 else 0

    if avg_per_day > OVERTRADE_CRIT_PER_DAY:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.CRITICAL,
            code=f"OVERTRADING_CRIT_{short_id}",
            message=f"{short_id}: Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) (>{OVERTRADE_CRIT_PER_DAY})",
            detail=f"Total: {total_trades} trades across {trading_days} trading day(s)",
            customer_id=cid,
        ))
    elif avg_per_day > OVERTRADE_WARN_PER_DAY:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.WARNING,
            code=f"OVERTRADING_WARN_{short_id}",
            message=f"{short_id}: Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) (>{OVERTRADE_WARN_PER_DAY})",
            detail=f"Total: {total_trades} trades across {trading_days} trading day(s)",
            customer_id=cid,
        ))
    else:
        report.add(Finding(
            gate="GATE4_OVERTRADING",
            severity=Severity.OK,
            code=f"TRADE_FREQ_OK_{short_id}",
            message=f"{short_id}: Avg {avg_per_day:.1f} trades/day over {trading_days} day(s) — normal",
            customer_id=cid,
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 5: DISPOSITION EFFECT
#  Check if profits are taken too early while losses are allowed to run
# ══════════════════════════════════════════════════════════════════════════

def gate5_disposition_effect(report: BiasReport, db, cid: str):
    """Flag when winners are cut short and losers run — the disposition effect."""
    short_id = cid[:8] if cid else 'master'
    log.info(f"[GATE 5] Disposition effect check ({short_id})")

    # Filter to bot-managed positions only — disposition effect is a
    # behavioral bias of the algorithm's exit timing. User-controlled
    # exits aren't relevant to scoring the bot.
    closed = [p for p in db.get_closed_positions(limit=200)
              if p.get('managed_by', 'bot') == 'bot']

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

    # MIN_BEHAVIORAL_SAMPLE (=10) is shared with gate3 — see config block
    # for rationale (small-sample artifacts dominate at lower N).
    if len(recent) < MIN_BEHAVIORAL_SAMPLE:
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.OK,
            code=f"TOO_FEW_CLOSED_{short_id}",
            message=(f"{short_id}: Only {len(recent)} bot-closed position(s) in last 30 days "
                     f"— disposition check needs ≥{MIN_BEHAVIORAL_SAMPLE}"),
            customer_id=cid,
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
            code=f"ONE_SIDED_{short_id}",
            message=f"{short_id}: Recent closed positions are {label} — disposition check N/A",
            customer_id=cid,
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
            code=f"DISPOSITION_EFFECT_{short_id}",
            message=(f"{short_id}: Disposition effect — avg winner +{avg_winner_gain:.1f}% vs "
                     f"avg loser -{avg_loser_loss:.1f}% with {len(winners)}W/{len(losers)}L"),
            detail="Cutting winners short while letting losers run — consider wider profit targets or tighter stop losses",
            customer_id=cid,
        ))
    else:
        report.add(Finding(
            gate="GATE5_DISPOSITION",
            severity=Severity.OK,
            code=f"DISPOSITION_OK_{short_id}",
            message=(f"{short_id}: No disposition effect — avg winner +{avg_winner_gain:.1f}% vs "
                     f"avg loser -{avg_loser_loss:.1f}% ({len(winners)}W/{len(losers)}L)"),
            customer_id=cid,
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
    """Active customer IDs (delegates to retail_shared.get_active_customers).
    Falls back to scanning data/customers/ when auth is unreachable so a
    bias scan can still run on cached customer state."""
    ids = get_active_customers()
    if ids:
        return ids
    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    if os.path.isdir(customers_dir):
        return [d for d in os.listdir(customers_dir)
                if d != 'default' and os.path.isdir(os.path.join(customers_dir, d))]
    return []


def _per_customer_summary(report: BiasReport, cid: str) -> dict:
    """Filter `report.findings` down to those belonging to `cid` and build
    a scan_summary dict suitable for `_BIAS_SCAN_LAST` on that customer's
    signals.db. Gate 6 is system-wide (no customer_id) and is intentionally
    excluded — it doesn't describe a single customer's bias."""
    cust_findings = [f for f in report.findings if f.customer_id == cid]
    crit = sum(1 for f in cust_findings if f.severity == Severity.CRITICAL)
    warn = sum(1 for f in cust_findings if f.severity == Severity.WARNING)
    severities = [Severity.OK, Severity.INFO, Severity.WARNING, Severity.CRITICAL]
    worst_idx = 0
    for f in cust_findings:
        if f.severity in severities:
            worst_idx = max(worst_idx, severities.index(f.severity))
    return {
        "timestamp": report.completed_at,
        "worst_severity": severities[worst_idx],
        "critical": crit,
        "warnings": warn,
        "total_checks": len(cust_findings),
        "findings": [
            {"gate": f.gate, "severity": f.severity, "code": f.code, "message": f.message}
            for f in cust_findings
            if f.severity in (Severity.CRITICAL, Severity.WARNING, Severity.INFO)
        ],
    }


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

    # Per-gate definitions to keep the loop small and the gate-error
    # routing consistent (CRITICAL on gate crash so emit_admin_alert routes
    # the failure — silent gate failures are gone).
    PER_CUSTOMER_GATES = [
        ("GATE1_SECTOR_CONC",    gate1_sector_concentration),
        ("GATE2_RECENCY",        gate2_recency_bias),
        ("GATE3_LOSS_AVERSION",  gate3_loss_aversion),
        ("GATE4_OVERTRADING",    gate4_overtrading),
        ("GATE5_DISPOSITION",    gate5_disposition_effect),
    ]

    for cid in customer_ids:
        short_id = cid[:8] if cid else "master"
        try:
            cdb = get_customer_db(cid) if cid else db
            log.info(f"--- Customer {short_id} ---")

            for gate_label, gate_fn in PER_CUSTOMER_GATES:
                try:
                    gate_fn(report, cdb, cid)
                except Exception as e:
                    log.error(f"{gate_label} failed for {short_id}: {e}", exc_info=True)
                    # CRITICAL (was WARNING) so emit_admin_alert routes it.
                    report.add(Finding(
                        gate=gate_label,
                        severity=Severity.CRITICAL,
                        code=f"{gate_label}_ERROR_{short_id}",
                        message=f"{short_id}: {gate_label} crashed: {e}",
                        customer_id=cid,
                    ))

        except Exception as e:
            log.error(f"Customer {short_id} failed: {e}", exc_info=True)
            report.add(Finding(
                gate="BIAS_CUSTOMER",
                severity=Severity.CRITICAL,
                code=f"CUSTOMER_ERROR_{short_id}",
                message=f"{short_id}: bias checks failed: {e}",
                customer_id=cid,
            ))

    # ── Gate 6: Confidence clustering (shared signals DB, system-wide) ─
    try:
        gate6_confidence_clustering(report, db)
    except Exception as e:
        log.error(f"Gate 6 failed: {e}", exc_info=True)
        report.add(Finding(
            gate="GATE6_CONFIDENCE",
            severity=Severity.CRITICAL,
            code="GATE6_ERROR",
            message=f"Confidence clustering check failed: {e}",
        ))

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
    # emit_admin_alert (in retail_shared) does per-code dedup so a sticky
    # bias finding doesn't spam the inbox at the 30-min scan cadence.
    admin_db = _master_db()
    fallback_cid = _CUSTOMER_ID or OWNER_CUSTOMER_ID
    written = 0
    deduped = 0
    for f in report.findings:
        if f.severity not in (Severity.WARNING, Severity.CRITICAL):
            continue
        if emit_admin_alert(admin_db, f,
                            source_agent='bias_detection_agent',
                            category='bias',
                            fallback_customer_id=fallback_cid):
            written += 1
        else:
            deduped += 1
    if written or deduped:
        log.info(f"admin_alerts: wrote {written}, deduped {deduped}")

    # ── Store scan summary for portal access ─────────────────────────
    # Shared DB summary covers everything (system-wide rollup). Per-
    # customer summaries land on each customer's signals.db so individual
    # dashboards see their own findings, not the system aggregate.
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

    for cid in customer_ids:
        if not cid:
            continue
        try:
            cdb = get_customer_db(cid)
            cdb.set_setting('_BIAS_SCAN_LAST', json.dumps(_per_customer_summary(report, cid)))
        except Exception as e:
            log.warning(f"per-customer scan summary write failed for {cid[:8]}: {e}")

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
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('bias_detection_agent', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

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
