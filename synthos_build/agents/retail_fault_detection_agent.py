"""
retail_fault_detection_agent.py — Fault Detection Agent
Synthos · Agent 6

Runs:
  Every enrichment cycle (30 min during market hours) via market daemon
  Also: once at pre-market open, once at close

Responsibilities:
  - 8-gate deterministic system health analysis spine
  - Detect agent liveness failures (stale heartbeats, missed runs)
  - Detect data freshness degradation (stale prices, stale signals)
  - Verify API connectivity (Alpaca reachability)
  - Monitor system resources (disk, memory, CPU temp)
  - Audit per-customer account health (equity anomalies, orphan states)
  - Verify DB integrity (table bloat, WAL size, stale locks)
  - Check schedule compliance (did expected agents run today?)
  - Raise urgent flags for critical faults
  - Write notifications for actionable findings

No LLM in any decision path. All gate logic is deterministic and traceable.

Data sources:
  - Internal DB (system_log, positions, customer_settings, signals)
  - Filesystem (/proc for system metrics, .agent_lock, .agent_running)
  - Alpaca API (connectivity ping only)

Usage:
  python3 retail_fault_detection_agent.py
  python3 retail_fault_detection_agent.py --customer-id <uuid>
"""

import os
import sys
import json
import logging
import time
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_ROOT_DIR, 'src'))
load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

from retail_database import get_db, get_customer_db, get_shared_db, acquire_agent_lock, release_agent_lock
from retail_shared import emit_admin_alert, get_active_customers

# ── CONFIG ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_BASE_URL   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')
_CUSTOMER_ID      = None   # set from --customer-id arg

# Thresholds — all in minutes unless noted
HEARTBEAT_STALE_MINUTES     = 45      # agent heartbeat older than this = stale
PRICE_STALE_MINUTES         = 10      # live_prices older than this during market hours
SIGNAL_STALE_HOURS          = 48      # queued signals older than this = warn
AGENT_LOCK_STALE_MINUTES    = 12      # .agent_lock file older than this = stuck
# Stuck-signal diagnostics — a QUEUED signal older than STUCK_SIGNAL_MINUTES
# that never accumulated all 5 required stamps points at a broken upstream
# agent. Threshold defines how many stuck rows elevate the finding from INFO
# to WARNING.
STUCK_SIGNAL_MINUTES        = 120
STUCK_SIGNAL_THRESHOLD      = 5

# Maps missing-stamp column → agent that owns writing it. Used to translate
# a "most-common missing stamp" diagnostic into a named upstream culprit.
_STAMP_OWNER = {
    'interrogation_status':        'news',
    'sentiment_evaluated_at':      'sentiment',
    'macro_regime_at_validation':  'macro_regime',
    'market_state_at_validation':  'market_state',
    'validator_stamped_at':        'validator_stack',
}
DISK_WARN_PCT               = 85      # disk usage % threshold
DISK_CRITICAL_PCT           = 95
MEMORY_WARN_PCT             = 85
CPU_TEMP_WARN_C             = 75.0    # Raspberry Pi thermal throttle starts at 80°C
CPU_TEMP_CRITICAL_C         = 82.0
DB_WAL_WARN_MB              = 50      # WAL file larger than this = needs checkpoint
SYSTEM_LOG_WARN_ROWS        = 50000   # table bloat warning
POSITIONS_ORPHAN_DAYS       = 14      # open position with no price update in N days

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('fault_detection_agent')


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
    # Optional extra metadata stored on the admin_alert when the central
    # router writes one. Gate 8 uses this to record baseline_median,
    # pct_of_baseline, etc. Other gates leave it empty and the router
    # falls back to {gate, code, severity}.
    meta: dict = field(default_factory=dict)


@dataclass
class FaultReport:
    """Aggregated output of all gates."""
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
        return (f"Fault scan complete: {self.critical_count} critical, "
                f"{self.warning_count} warning, {len(self.findings)} total checks")


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


def _now_et():
    return datetime.now(ET)


def _now_str():
    return datetime.now(tz=ZoneInfo("UTC")).strftime('%Y-%m-%d %H:%M:%S')


def _is_market_hours():
    """True if currently within US market hours (9:30-16:00 ET, weekdays)."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1: AGENT LIVENESS
#  Check that each core agent has heartbeated recently
# ══════════════════════════════════════════════════════════════════════════

# Single source of truth for the agents fault-detection knows about.
# Replaces what used to be three separate constants (EXPECTED_AGENTS,
# PER_CUSTOMER_AGENTS, EXPECTED_DAILY_COMPLETIONS) that drifted apart.
#
# Phase H+ (2026-04-28) — `per_customer=True` means the agent writes its
# heartbeat / AGENT_COMPLETE to *each customer's* DB rather than the shared
# market-intel DB. fault_detection's _master_db() reads shared, so for those
# agents we have to aggregate across all active customer DBs (otherwise gate1
# and gate7 see zero trader activity even when the trader is running).
@dataclass(frozen=True)
class ExpectedAgent:
    hb_name: str                                 # name in db.log_heartbeat
    complete_name: str                           # name in db.log_event AGENT_COMPLETE
    label: str                                   # human-readable
    stale_minutes: int = HEARTBEAT_STALE_MINUTES # gate1 staleness threshold
    per_customer: bool = False
    daily_completion_required: bool = False      # gate7 expects ≥1 AGENT_COMPLETE today


EXPECTED_AGENTS = [
    ExpectedAgent("market_sentiment_agent",  "The Pulse",
                  "Market Sentiment",        daily_completion_required=True),
    ExpectedAgent("news_agent",              "News",
                  "News",                    daily_completion_required=True),
    ExpectedAgent("trade_logic_agent",       "Trade Logic",
                  "Trade Logic",
                  per_customer=True,         daily_completion_required=True),
    ExpectedAgent("sector_screener",         "Sector Screener",
                  "Sector Screener",         stale_minutes=1800),   # 30h window
    ExpectedAgent("price_poller",            "Price Poller",
                  "Price Poller"),
    # Interrogation listener posts heartbeat every 60s while running. If it
    # dies, new news signals get interrogation_status='UNVALIDATED' and —
    # with the tightened promoter check — stop promoting, so a dead listener
    # degrades the signal pipeline silently unless we catch it here.
    ExpectedAgent("interrogation_listener",  "Interrogation Listener",
                  "Interrogation Listener"),
]


# ── Per-run scratch state ─────────────────────────────────────────────────
# Reset at the start of every run() invocation. Holds:
#   - active_customer_ids: list of UUIDs (auth.list_customers, fetched once)
#   - scan_cache: {(event, agent, since_iso): (latest_ts, count)}
# Avoids reopening every customer DB once per gate that needs per-customer
# event aggregation.
_RUN = {"active_customer_ids": None, "scan_cache": {}}


def _reset_run_state():
    _RUN["active_customer_ids"] = None
    _RUN["scan_cache"] = {}


def _active_customer_ids():
    """Memoized accessor for the customer ID list."""
    if _RUN["active_customer_ids"] is None:
        _RUN["active_customer_ids"] = _get_active_customer_ids()
    return _RUN["active_customer_ids"]


def _scan_per_customer_for_event(event: str, agent_name: str,
                                 since_iso: str | None = None) -> tuple[str | None, int]:
    """Find the most recent (timestamp) and count of system_log events
    matching `event` + `agent_name` across ALL active customer DBs.
    Returns (latest_timestamp_iso_or_None, total_count). Used by
    gate1_liveness + gate7_schedule for the per-customer agents.
    Memoized within a single run() — the same (event, agent, since_iso)
    lookup from gate1 + gate7 only opens each customer DB once."""
    cache_key = (event, agent_name, since_iso)
    if cache_key in _RUN["scan_cache"]:
        return _RUN["scan_cache"][cache_key]

    latest_ts = None
    total = 0
    try:
        for cid in _active_customer_ids():
            try:
                cdb = get_customer_db(cid)
                with cdb.conn() as c:
                    if since_iso:
                        rows = c.execute(
                            "SELECT timestamp FROM system_log "
                            "WHERE event=? AND agent=? AND timestamp >= ?",
                            (event, agent_name, since_iso)
                        ).fetchall()
                    else:
                        rows = c.execute(
                            "SELECT timestamp FROM system_log "
                            "WHERE event=? AND agent=?",
                            (event, agent_name)
                        ).fetchall()
                for r in rows:
                    total += 1
                    ts = r['timestamp'] if hasattr(r, 'keys') else r[0]
                    if not latest_ts or ts > latest_ts:
                        latest_ts = ts
            except Exception as e:
                log.debug(f"per-customer scan {cid} {event}/{agent_name}: {e}")
    except Exception as e:
        log.warning(f"_scan_per_customer_for_event({event},{agent_name}): {e}")

    result = (latest_ts, total)
    _RUN["scan_cache"][cache_key] = result
    return result


def gate1_agent_liveness(report: FaultReport, db):
    """Check last heartbeat timestamp for each known agent."""
    log.info("[GATE 1] Agent liveness check")

    now = datetime.now(tz=ZoneInfo("UTC"))

    for agent in EXPECTED_AGENTS:
        if agent.per_customer:
            latest_ts, _ = _scan_per_customer_for_event(
                event='HEARTBEAT', agent_name=agent.hb_name
            )
            row = {'timestamp': latest_ts, 'details': ''} if latest_ts else None
        else:
            with db.conn() as c:
                row = c.execute(
                    "SELECT timestamp, details FROM system_log "
                    "WHERE event='HEARTBEAT' AND agent=? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (agent.hb_name,)
                ).fetchone()

        code_key = agent.hb_name.upper().replace(' ', '_')
        agent_label = agent.label
        stale_threshold = agent.stale_minutes

        if not row:
            report.add(Finding(
                gate="GATE1_LIVENESS",
                severity=Severity.WARNING,
                code=f"NO_HEARTBEAT_{code_key}",
                message=f"{agent_label}: No heartbeat found",
                detail="Agent may have never run or DB was cleared"
            ))
            continue

        try:
            last_ts = datetime.fromisoformat(row['timestamp'].replace('Z', ''))
            # Make naive timestamps UTC-aware for comparison
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=ZoneInfo("UTC"))
            age_min = (now - last_ts).total_seconds() / 60.0
        except (ValueError, TypeError):
            age_min = 9999

        if age_min > stale_threshold and _is_market_hours():
            severity = Severity.CRITICAL if age_min > stale_threshold * 3 else Severity.WARNING
            report.add(Finding(
                gate="GATE1_LIVENESS",
                severity=severity,
                code=f"STALE_HEARTBEAT_{code_key}",
                message=f"{agent_label}: Last heartbeat {int(age_min)}m ago",
                detail=f"Threshold: {stale_threshold}m | Last: {row['timestamp']}"
            ))
        else:
            report.add(Finding(
                gate="GATE1_LIVENESS",
                severity=Severity.OK,
                code=f"HEARTBEAT_OK_{code_key}",
                message=f"{agent_label}: Alive ({int(age_min)}m ago)"
            ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2: DATA FRESHNESS
#  Verify prices, signals, and news are being refreshed
# ══════════════════════════════════════════════════════════════════════════

def gate2_data_freshness(report: FaultReport, db):
    """Check age of live prices, queued signals, and news feed entries."""
    log.info("[GATE 2] Data freshness check")

    now = datetime.now(tz=ZoneInfo("UTC"))

    # 2a. Live prices freshness (only matters during market hours)
    if _is_market_hours():
        with db.conn() as c:
            price_row = c.execute(
                "SELECT MAX(updated_at) as latest FROM live_prices"
            ).fetchone()

        if price_row and price_row['latest']:
            try:
                last_price = datetime.fromisoformat(price_row['latest'].replace('Z', ''))
                if last_price.tzinfo is None:
                    last_price = last_price.replace(tzinfo=ZoneInfo("UTC"))
                price_age_min = (now - last_price).total_seconds() / 60.0
            except (ValueError, TypeError):
                price_age_min = 9999

            if price_age_min > PRICE_STALE_MINUTES:
                report.add(Finding(
                    gate="GATE2_FRESHNESS",
                    severity=Severity.WARNING,
                    code="STALE_PRICES",
                    message=f"Live prices are {int(price_age_min)}m stale",
                    detail=f"Threshold: {PRICE_STALE_MINUTES}m | Last: {price_row['latest']}"
                ))
            else:
                report.add(Finding(
                    gate="GATE2_FRESHNESS",
                    severity=Severity.OK,
                    code="PRICES_FRESH",
                    message=f"Live prices updated {int(price_age_min)}m ago"
                ))
        else:
            report.add(Finding(
                gate="GATE2_FRESHNESS",
                severity=Severity.WARNING,
                code="NO_PRICES",
                message="No live price data found"
            ))

    # 2b. In-flight signal freshness — any signal that hasn't reached a
    # terminal status (QUEUED awaiting validation, or VALIDATED awaiting
    # trader action) is "in flight" and eligible for staleness flagging.
    with db.conn() as c:
        stale_signals = c.execute(
            "SELECT COUNT(*) as cnt FROM signals "
            "WHERE status IN ('QUEUED','VALIDATED') AND created_at < ?",
            ((now - timedelta(hours=SIGNAL_STALE_HOURS)).strftime('%Y-%m-%d %H:%M:%S'),)
        ).fetchone()

    stale_count = stale_signals['cnt'] if stale_signals else 0
    if stale_count > 0:
        report.add(Finding(
            gate="GATE2_FRESHNESS",
            severity=Severity.INFO,
            code="STALE_SIGNALS",
            message=f"{stale_count} in-flight signal(s) older than {SIGNAL_STALE_HOURS}h",
            detail="May need expiry or manual review"
        ))

    # 2b-bis. Stuck signals — QUEUED rows older than the stuck threshold that
    # never accumulated all five required stamps. This is the diagnostic that
    # actually surfaces WHICH upstream agent is failing: we look at the set of
    # missing stamps across all stuck rows and rank them so the most frequent
    # missing stamp (= most likely broken agent) shows up in the finding.
    try:
        stuck = db.get_stuck_signals(min_age_minutes=STUCK_SIGNAL_MINUTES)
    except Exception as e:
        stuck = []
        log.debug(f"get_stuck_signals skipped: {e}")

    if stuck:
        # Count missing-stamp occurrences to identify the bottleneck agent.
        miss_count: dict = {}
        for row in stuck:
            for stamp in row.get('missing_stamps', []):
                miss_count[stamp] = miss_count.get(stamp, 0) + 1
        top = sorted(miss_count.items(), key=lambda x: -x[1])
        bottleneck_stamp = top[0][0] if top else 'unknown'
        bottleneck_agent = _STAMP_OWNER.get(bottleneck_stamp, 'unknown')

        severity = Severity.WARNING if len(stuck) >= STUCK_SIGNAL_THRESHOLD else Severity.INFO
        report.add(Finding(
            gate="GATE2_FRESHNESS",
            severity=severity,
            code="STUCK_SIGNALS",
            message=(f"{len(stuck)} signal(s) stuck at QUEUED (>{STUCK_SIGNAL_MINUTES}m) "
                     f"— bottleneck: {bottleneck_agent} "
                     f"(most-missing stamp: {bottleneck_stamp})"),
            detail=("Top tickers: "
                    + ", ".join(f"{r['ticker']}({r['age_minutes']}m)" for r in stuck[:5])
                    + f" | Missing-stamp histogram: {dict(top[:5])}")
        ))
    else:
        report.add(Finding(
            gate="GATE2_FRESHNESS",
            severity=Severity.OK,
            code="NO_STUCK_SIGNALS",
            message=f"No signals stuck at QUEUED beyond {STUCK_SIGNAL_MINUTES}m"
        ))

    # 2c. News feed freshness (during market hours, news should be <2h old)
    if _is_market_hours():
        with db.conn() as c:
            news_row = c.execute(
                "SELECT MAX(created_at) as latest FROM news_feed"
            ).fetchone()

        if news_row and news_row['latest']:
            try:
                last_news = datetime.fromisoformat(news_row['latest'].replace('Z', ''))
                if last_news.tzinfo is None:
                    last_news = last_news.replace(tzinfo=ZoneInfo("UTC"))
                news_age_min = (now - last_news).total_seconds() / 60.0
            except (ValueError, TypeError):
                news_age_min = 9999

            if news_age_min > 120:
                report.add(Finding(
                    gate="GATE2_FRESHNESS",
                    severity=Severity.WARNING,
                    code="STALE_NEWS",
                    message=f"News feed is {int(news_age_min)}m stale",
                    detail=f"Last entry: {news_row['latest']}"
                ))
            else:
                report.add(Finding(
                    gate="GATE2_FRESHNESS",
                    severity=Severity.OK,
                    code="NEWS_FRESH",
                    message=f"News feed updated {int(news_age_min)}m ago"
                ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 3: API CONNECTIVITY
#  Ping Alpaca API to verify connectivity
# ══════════════════════════════════════════════════════════════════════════

def gate3_api_connectivity(report: FaultReport):
    """Verify Alpaca API is reachable and credentials are valid."""
    log.info("[GATE 3] API connectivity check")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        report.add(Finding(
            gate="GATE3_API",
            severity=Severity.INFO,
            code="NO_ALPACA_KEYS",
            message="No Alpaca credentials configured (owner level)",
            detail="Per-customer keys checked in Gate 5"
        ))
        return

    try:
        import requests as _req
        resp = _req.get(
            f"{ALPACA_BASE_URL}/v2/clock",
            headers={
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            timeout=10
        )
        if resp.status_code == 200:
            clock = resp.json()
            is_open = clock.get('is_open', False)
            report.add(Finding(
                gate="GATE3_API",
                severity=Severity.OK,
                code="ALPACA_OK",
                message=f"Alpaca API reachable (market {'open' if is_open else 'closed'})"
            ))
        elif resp.status_code == 401:
            report.add(Finding(
                gate="GATE3_API",
                severity=Severity.CRITICAL,
                code="ALPACA_AUTH_FAIL",
                message="Alpaca API returned 401 — credentials invalid",
                detail="Check ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            ))
        else:
            report.add(Finding(
                gate="GATE3_API",
                severity=Severity.WARNING,
                code="ALPACA_HTTP_ERROR",
                message=f"Alpaca API returned HTTP {resp.status_code}",
                detail=resp.text[:200]
            ))
    except ImportError:
        report.add(Finding(
            gate="GATE3_API",
            severity=Severity.WARNING,
            code="REQUESTS_MISSING",
            message="requests module not available"
        ))
    except Exception as e:
        report.add(Finding(
            gate="GATE3_API",
            severity=Severity.CRITICAL,
            code="ALPACA_UNREACHABLE",
            message=f"Alpaca API connection failed: {type(e).__name__}",
            detail=str(e)[:200]
        ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 4: SYSTEM RESOURCES
#  Monitor disk, memory, CPU temperature on the Pi
# ══════════════════════════════════════════════════════════════════════════

def gate4_system_resources(report: FaultReport):
    """Check disk usage, memory pressure, and CPU temperature."""
    log.info("[GATE 4] System resources check")

    # 4a. Disk usage
    try:
        stat = os.statvfs('/')
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used_pct = round((1 - free / total) * 100, 1)

        if used_pct >= DISK_CRITICAL_PCT:
            report.add(Finding(
                gate="GATE4_RESOURCES",
                severity=Severity.CRITICAL,
                code="DISK_CRITICAL",
                message=f"Disk usage at {used_pct}% — critically low",
                detail=f"Free: {free // (1024**2)}MB / {total // (1024**2)}MB"
            ))
        elif used_pct >= DISK_WARN_PCT:
            report.add(Finding(
                gate="GATE4_RESOURCES",
                severity=Severity.WARNING,
                code="DISK_WARNING",
                message=f"Disk usage at {used_pct}%",
                detail=f"Free: {free // (1024**2)}MB / {total // (1024**2)}MB"
            ))
        else:
            report.add(Finding(
                gate="GATE4_RESOURCES",
                severity=Severity.OK,
                code="DISK_OK",
                message=f"Disk usage at {used_pct}%"
            ))
    except Exception as e:
        log.warning(f"Disk check failed: {e}")

    # 4b. Memory
    try:
        meminfo_path = Path('/proc/meminfo')
        if meminfo_path.exists():
            lines = meminfo_path.read_text().splitlines()
            mem = {}
            for line in lines:
                parts = line.split(':')
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]  # kB value
                    mem[key] = int(val)

            total_kb = mem.get('MemTotal', 1)
            avail_kb = mem.get('MemAvailable', total_kb)
            used_pct = round((1 - avail_kb / total_kb) * 100, 1)

            if used_pct >= MEMORY_WARN_PCT:
                report.add(Finding(
                    gate="GATE4_RESOURCES",
                    severity=Severity.WARNING,
                    code="MEMORY_WARNING",
                    message=f"Memory usage at {used_pct}%",
                    detail=f"Available: {avail_kb // 1024}MB / {total_kb // 1024}MB"
                ))
            else:
                report.add(Finding(
                    gate="GATE4_RESOURCES",
                    severity=Severity.OK,
                    code="MEMORY_OK",
                    message=f"Memory usage at {used_pct}%"
                ))
    except Exception as e:
        log.warning(f"Memory check failed: {e}")

    # 4c. CPU temperature (Raspberry Pi)
    try:
        temp_path = Path('/sys/class/thermal/thermal_zone0/temp')
        if temp_path.exists():
            temp_c = int(temp_path.read_text().strip()) / 1000.0

            if temp_c >= CPU_TEMP_CRITICAL_C:
                report.add(Finding(
                    gate="GATE4_RESOURCES",
                    severity=Severity.CRITICAL,
                    code="CPU_TEMP_CRITICAL",
                    message=f"CPU temperature {temp_c:.1f}°C — thermal throttling imminent",
                    detail=f"Critical: {CPU_TEMP_CRITICAL_C}°C | Throttle: 80°C"
                ))
            elif temp_c >= CPU_TEMP_WARN_C:
                report.add(Finding(
                    gate="GATE4_RESOURCES",
                    severity=Severity.WARNING,
                    code="CPU_TEMP_WARNING",
                    message=f"CPU temperature {temp_c:.1f}°C — elevated",
                    detail=f"Warning threshold: {CPU_TEMP_WARN_C}°C"
                ))
            else:
                report.add(Finding(
                    gate="GATE4_RESOURCES",
                    severity=Severity.OK,
                    code="CPU_TEMP_OK",
                    message=f"CPU temperature {temp_c:.1f}°C"
                ))
    except Exception as e:
        log.warning(f"CPU temp check failed: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  GATE 5: ACCOUNT HEALTH (per-customer)
#  Check equity consistency, orphan positions, kill switch
# ══════════════════════════════════════════════════════════════════════════

def _ping_customer_alpaca(key: str, secret: str) -> tuple[str, str | None]:
    """Per-customer Alpaca credential ping. Hits /v2/account with a short
    timeout. Returns one of:
      ('ok', None)               — 200, keys authenticate
      ('skip_no_keys', None)     — no creds stored (handled elsewhere)
      ('auth_fail', detail_str)  — 401/403 (revoked or wrong keys)
      ('connect_fail', detail)   — anything else / connection error
    Connectivity failures are intentionally suppressed by the caller because
    Gate 3 already covers reachability — this helper exists to surface
    PER-CUSTOMER credential rejection, which Gate 3's owner-key ping can't
    detect."""
    if not key or not secret:
        return ('skip_no_keys', None)
    try:
        import requests as _req
        resp = _req.get(
            f"{ALPACA_BASE_URL}/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=5,
        )
        if resp.status_code == 200:
            return ('ok', None)
        if resp.status_code in (401, 403):
            return ('auth_fail', f"HTTP {resp.status_code}")
        return ('connect_fail', f"HTTP {resp.status_code}")
    except Exception as e:
        return ('connect_fail', f"{type(e).__name__}: {str(e)[:80]}")


def gate5_account_health(report: FaultReport, customer_ids):
    """Per-customer account health checks."""
    log.info(f"[GATE 5] Account health check ({len(customer_ids)} customers)")

    # Lazy import — auth lives in src/ which is on sys.path; avoid pulling it
    # at module load time so unit tests of the helpers above stay light.
    try:
        import auth as _auth
    except Exception as e:
        _auth = None
        log.debug(f"auth import skipped (cred ping disabled): {e}")

    for cid in customer_ids:
        try:
            db = get_customer_db(cid)
            short_id = cid[:8]

            # 5a. Equity check — does setting match reasonable value?
            equity_str = db.get_setting('_ALPACA_EQUITY')
            equity = float(equity_str) if equity_str else 0.0
            new_customer = db.get_setting('NEW_CUSTOMER')

            if equity < 1.0 and new_customer != 'true':
                # Was funded before but now $0 — potential API issue
                report.add(Finding(
                    gate="GATE5_ACCOUNT",
                    severity=Severity.WARNING,
                    code=f"EQUITY_ZERO_{short_id}",
                    message=f"Customer {short_id}: Equity $0 but not flagged as new customer",
                    detail="Possible API credential issue or Alpaca account problem"
                ))

            # 5b. Kill switch left on
            kill = db.get_setting('KILL_SWITCH')
            if kill == '1':
                report.add(Finding(
                    gate="GATE5_ACCOUNT",
                    severity=Severity.INFO,
                    code=f"KILL_SWITCH_ON_{short_id}",
                    message=f"Customer {short_id}: Kill switch is active",
                    detail="Trading halted — verify this is intentional"
                ))

            # 5b-bis. Per-customer Alpaca credential ping. Catches the case
            # Gate 3 misses: customer's own keys revoked / mistyped while
            # the owner data-fetch keys (Gate 3) still authenticate fine.
            # Connection failures are skipped here — Gate 3 owns reachability.
            if _auth is not None:
                try:
                    key, secret = _auth.get_alpaca_credentials(cid)
                    status, detail = _ping_customer_alpaca(key, secret)
                    if status == 'auth_fail':
                        report.add(Finding(
                            gate="GATE5_ACCOUNT",
                            severity=Severity.CRITICAL,
                            code=f"ALPACA_AUTH_FAIL_{short_id}",
                            message=f"Customer {short_id}: Alpaca credentials rejected ({detail})",
                            detail="Per-customer keys may be revoked or mistyped — trader will fail until rotated."
                        ))
                    # 'ok' / 'skip_no_keys' / 'connect_fail' → silent here.
                except Exception as e:
                    log.debug(f"alpaca ping skipped for {short_id}: {e}")

            # 5c. Orphan positions (open but no price update in N days)
            positions = db.get_open_positions()
            now = datetime.now(tz=ZoneInfo("UTC"))
            for pos in positions:
                updated = pos.get('updated_at') or pos.get('opened_at', '')
                if updated:
                    try:
                        last_update = datetime.fromisoformat(updated.replace('Z', ''))
                        age_days = (now - last_update).total_seconds() / 86400
                        if age_days > POSITIONS_ORPHAN_DAYS:
                            report.add(Finding(
                                gate="GATE5_ACCOUNT",
                                severity=Severity.WARNING,
                                code=f"ORPHAN_POSITION_{short_id}",
                                message=f"Customer {short_id}: {pos.get('ticker', '?')} not updated in {int(age_days)}d",
                                detail=f"Position ID {pos.get('id', '?')} | Last update: {updated}"
                            ))
                    except (ValueError, TypeError):
                        pass

            # 5d. Unacknowledged urgent flags
            flags = db.get_urgent_flags()
            if len(flags) > 3:
                report.add(Finding(
                    gate="GATE5_ACCOUNT",
                    severity=Severity.WARNING,
                    code=f"MANY_FLAGS_{short_id}",
                    message=f"Customer {short_id}: {len(flags)} unacknowledged urgent flags",
                    detail="Flags may be piling up without resolution"
                ))

        except Exception as e:
            report.add(Finding(
                gate="GATE5_ACCOUNT",
                severity=Severity.WARNING,
                code=f"ACCOUNT_CHECK_FAIL_{cid[:8]}",
                message=f"Customer {cid[:8]}: Health check failed",
                detail=str(e)[:200]
            ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 6: DATABASE INTEGRITY
#  Check table sizes, WAL bloat, stale locks
# ══════════════════════════════════════════════════════════════════════════

def gate6_db_integrity(report: FaultReport, db):
    """Check database health indicators."""
    log.info("[GATE 6] Database integrity check")

    # 6a. System log table size
    with db.conn() as c:
        log_count = c.execute("SELECT COUNT(*) as cnt FROM system_log").fetchone()
        count = log_count['cnt'] if log_count else 0

    if count > SYSTEM_LOG_WARN_ROWS:
        report.add(Finding(
            gate="GATE6_DB",
            severity=Severity.INFO,
            code="SYSTEM_LOG_LARGE",
            message=f"system_log has {count:,} rows (threshold: {SYSTEM_LOG_WARN_ROWS:,})",
            detail="Consider running cleanup to purge old heartbeats"
        ))
    else:
        report.add(Finding(
            gate="GATE6_DB",
            severity=Severity.OK,
            code="SYSTEM_LOG_OK",
            message=f"system_log table: {count:,} rows"
        ))

    # 6b. WAL file size — shared DB
    db_path = Path(db.path)
    wal_path = db_path.with_suffix('.db-wal')
    if wal_path.exists():
        wal_mb = wal_path.stat().st_size / (1024 * 1024)
        if wal_mb > DB_WAL_WARN_MB:
            report.add(Finding(
                gate="GATE6_DB",
                severity=Severity.WARNING,
                code="WAL_BLOAT_SHARED",
                message=f"Shared DB WAL file is {wal_mb:.1f}MB",
                detail=f"Threshold: {DB_WAL_WARN_MB}MB — needs PRAGMA wal_checkpoint"
            ))
        else:
            report.add(Finding(
                gate="GATE6_DB",
                severity=Severity.OK,
                code="WAL_OK_SHARED",
                message=f"Shared DB WAL: {wal_mb:.1f}MB"
            ))

    # 6b-bis. WAL file size — per-customer DBs. The trader writes most volume
    # to per-customer signals.db files; if a checkpoint stops happening their
    # WAL can grow unbounded and a shared-only check misses it entirely.
    for cid in _active_customer_ids():
        try:
            cdb = get_customer_db(cid)
            cwal = Path(cdb.path).with_suffix('.db-wal')
            if not cwal.exists():
                continue
            cwal_mb = cwal.stat().st_size / (1024 * 1024)
            if cwal_mb > DB_WAL_WARN_MB:
                report.add(Finding(
                    gate="GATE6_DB",
                    severity=Severity.WARNING,
                    code=f"WAL_BLOAT_CUST_{cid[:8]}",
                    message=f"Customer {cid[:8]} signals.db WAL is {cwal_mb:.1f}MB",
                    detail=f"Threshold: {DB_WAL_WARN_MB}MB — needs PRAGMA wal_checkpoint"
                ))
        except Exception as e:
            log.debug(f"per-customer WAL check {cid[:8]}: {e}")

    # 6c. Stale agent lock file
    lock_path = Path(os.path.join(_ROOT_DIR, 'src', '.agent_lock'))
    if lock_path.exists():
        lock_age_min = (time.time() - lock_path.stat().st_mtime) / 60.0
        if lock_age_min > AGENT_LOCK_STALE_MINUTES:
            try:
                lock_content = lock_path.read_text().strip().split('\n')
                lock_agent = lock_content[0] if lock_content else 'unknown'
            except Exception:
                lock_agent = 'unknown'

            report.add(Finding(
                gate="GATE6_DB",
                severity=Severity.WARNING,
                code="STALE_AGENT_LOCK",
                message=f"Agent lock held for {int(lock_age_min)}m by {lock_agent}",
                detail=f"Threshold: {AGENT_LOCK_STALE_MINUTES}m — may be stuck"
            ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 7: SCHEDULE COMPLIANCE
#  Verify expected agents have run today
# ══════════════════════════════════════════════════════════════════════════

def gate7_schedule_compliance(report: FaultReport, db):
    """Check that each expected agent has completed at least once today.
    Iterates EXPECTED_AGENTS where daily_completion_required=True and
    branches on per_customer for shared-vs-customer DB lookup."""
    log.info("[GATE 7] Schedule compliance check")

    now_et = _now_et()

    # Only check compliance after 10:00 ET on weekdays
    if now_et.weekday() >= 5 or now_et.hour < 10:
        report.add(Finding(
            gate="GATE7_SCHEDULE",
            severity=Severity.OK,
            code="SCHEDULE_SKIP",
            message="Outside compliance window (pre-10am or weekend)"
        ))
        return

    today_str = now_et.strftime('%Y-%m-%d')

    for agent in EXPECTED_AGENTS:
        if not agent.daily_completion_required:
            continue
        if agent.per_customer:
            _, completions = _scan_per_customer_for_event(
                event='AGENT_COMPLETE',
                agent_name=agent.complete_name,
                since_iso=today_str,
            )
        else:
            with db.conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) as cnt FROM system_log "
                    "WHERE event='AGENT_COMPLETE' AND agent=? AND timestamp LIKE ?",
                    (agent.complete_name, f"{today_str}%")
                ).fetchone()
            completions = row['cnt'] if row else 0

        code_label = agent.label.upper().replace(' ', '_')

        if completions == 0:
            severity = Severity.WARNING if now_et.hour < 12 else Severity.CRITICAL
            report.add(Finding(
                gate="GATE7_SCHEDULE",
                severity=severity,
                code=f"NO_RUN_{code_label}",
                message=f"{agent.label}: Has not completed today",
                detail=f"Expected at least 1 run by {now_et.strftime('%H:%M')} ET"
            ))
        else:
            report.add(Finding(
                gate="GATE7_SCHEDULE",
                severity=Severity.OK,
                code=f"RUN_OK_{code_label}",
                message=f"{agent.label}: {completions} run(s) today"
            ))


# ══════════════════════════════════════════════════════════════════════════
#  GATE 8 — TRADE ACTIVITY BASELINE (DEGRADED DETECTOR)
# ══════════════════════════════════════════════════════════════════════════
# Catches the scenario the DOWN gates miss: trader is up, heartbeats fine,
# logs flowing — but it's quietly producing zero new decisions for days.
# Common causes: over-restrictive gate (e.g. chase caps tightened too far),
# empty validated-signal pool (validator stuck CAUTION), regime locked into
# permanent BEAR. Without this gate, the system can degrade silently.
#
# Strategy: compare today's TRADE_DECISION count (so far) to the 30-day
# median. Fire WARNING if today < 30% of baseline AND baseline is large
# enough (≥10) that the comparison is statistically meaningful.
#
# Skips that prevent false positives:
#   - Pre-14:00 ET (current session hasn't accumulated enough yet)
#   - Weekends (markets closed)
#   - Warm-up: <14 weekdays of non-zero history → INFO, not WARNING
#   - Low-traffic regime: baseline median < 10/day → INFO + skip
#
# Tunable via env: DEGRADED_THRESHOLD_PCT (default 0.30),
# DEGRADED_MIN_BASELINE (default 10), DEGRADED_MIN_HISTORY_DAYS (default 14).

DEGRADED_THRESHOLD_PCT     = float(os.environ.get('DEGRADED_THRESHOLD_PCT', '0.30'))
DEGRADED_MIN_BASELINE      = int(os.environ.get('DEGRADED_MIN_BASELINE', '10'))
DEGRADED_MIN_HISTORY_DAYS  = int(os.environ.get('DEGRADED_MIN_HISTORY_DAYS', '14'))


def gate8_trade_activity_baseline(report: FaultReport, db):
    """DEGRADED detector — alert when trader is producing far fewer
    decisions than its own historical baseline."""
    log.info("[GATE 8] Trade-decision activity baseline")

    now_et = _now_et()

    # Skip on weekends — by design no decisions get made.
    if now_et.weekday() >= 5:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.OK,
            code="ACTIVITY_SKIP_WEEKEND",
            message="Outside trading week — gate skipped",
        ))
        return

    # Pre-14:00 ET there isn't enough session data to compare meaningfully.
    if now_et.hour < 14:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.OK,
            code="ACTIVITY_SKIP_EARLY",
            message=f"Pre-14:00 ET ({now_et.strftime('%H:%M')}) — too early to assess",
        ))
        return

    today_str = now_et.strftime('%Y-%m-%d')

    # Today's count so far
    try:
        with db.conn() as c:
            today_row = c.execute(
                "SELECT COUNT(*) as cnt FROM system_log "
                "WHERE event='TRADE_DECISION' AND timestamp LIKE ?",
                (f"{today_str}%",),
            ).fetchone()
        today_count = today_row['cnt'] if today_row else 0
    except Exception as e:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.WARNING,
            code="ACTIVITY_QUERY_FAIL",
            message=f"Could not read TRADE_DECISION count: {e}",
        ))
        return

    # 30-day weekday baseline
    daily_counts = []
    for d in range(1, 31):
        day = (now_et - timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        try:
            with db.conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) as cnt FROM system_log "
                    "WHERE event='TRADE_DECISION' AND timestamp LIKE ?",
                    (f"{day.isoformat()}%",),
                ).fetchone()
            daily_counts.append(row['cnt'] if row else 0)
        except Exception:
            continue   # missing day = skip; don't poison the baseline

    nonzero_days = [c for c in daily_counts if c > 0]
    if len(nonzero_days) < DEGRADED_MIN_HISTORY_DAYS:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.INFO,
            code="ACTIVITY_WARMUP",
            message=(f"Insufficient history: {len(nonzero_days)} non-zero day(s) "
                     f"in last 30 weekdays (need {DEGRADED_MIN_HISTORY_DAYS}). "
                     f"Today: {today_count} decision(s)."),
            detail="Detector activates after enough trading days accumulate.",
        ))
        return

    nonzero_days.sort()
    median_baseline = nonzero_days[len(nonzero_days) // 2]

    if median_baseline < DEGRADED_MIN_BASELINE:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.OK,
            code="ACTIVITY_LOW_TRAFFIC",
            message=(f"Baseline median {median_baseline}/day below alert floor "
                     f"({DEGRADED_MIN_BASELINE}). Today: {today_count}."),
            detail="Low-traffic regime — degradation detector requires more volume to be reliable.",
        ))
        return

    threshold = median_baseline * DEGRADED_THRESHOLD_PCT
    pct_of_baseline = (today_count / median_baseline) * 100 if median_baseline else 0

    if today_count < threshold:
        message = (f"Today {today_count} decision(s) vs 30-day median "
                   f"{median_baseline} ({pct_of_baseline:.0f}% of baseline)")
        detail = (f"Threshold: {DEGRADED_THRESHOLD_PCT*100:.0f}% of baseline = "
                  f"{threshold:.1f}. Likely causes: over-restrictive gate "
                  f"(chase caps too tight, news veto firing too often), empty "
                  f"VALIDATED signal pool, validator stuck on CAUTION, regime "
                  f"locked into BEAR, or candidate generator producing too few "
                  f"signals. Investigate: count VALIDATED signals, scan trade_logic_agent.log "
                  f"for skip reasons.")
        # Bespoke metadata travels on the Finding; the central admin_alert
        # router (run() → _emit_admin_alert) handles dedup + write.
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.WARNING,
            code="ACTIVITY_DEGRADED",
            message=message,
            detail=detail,
            meta={
                "gate": "GATE8_ACTIVITY",
                "today_count": today_count,
                "baseline_median": median_baseline,
                "pct_of_baseline": round(pct_of_baseline, 1),
                "threshold_pct": DEGRADED_THRESHOLD_PCT,
            },
        ))
    else:
        report.add(Finding(
            gate="GATE8_ACTIVITY",
            severity=Severity.OK,
            code="ACTIVITY_NORMAL",
            message=(f"Today {today_count} decision(s) vs 30-day median "
                     f"{median_baseline} ({pct_of_baseline:.0f}% of baseline)"),
        ))


# ══════════════════════════════════════════════════════════════════════════
#  MAIN RUN FUNCTION
# ══════════════════════════════════════════════════════════════════════════

def _get_active_customer_ids():
    """Active customer IDs. Uses retail_shared.get_active_customers() and
    falls back to scanning data/customers/ when auth is unreachable — fault
    detection itself must keep running when auth is down."""
    ids = get_active_customers()
    if ids:
        return ids
    customers_dir = os.path.join(_ROOT_DIR, 'data', 'customers')
    if os.path.isdir(customers_dir):
        return [d for d in os.listdir(customers_dir)
                if d != 'default' and os.path.isdir(os.path.join(customers_dir, d))]
    return []


def run():
    """Execute the 8-gate fault detection spine."""
    _reset_run_state()                                  # clear per-run cache
    db = _master_db()

    # ── Lifecycle: START ──────────────────────────────────────────────
    db.log_event("AGENT_START", agent="Fault Detection", details="fault scan")
    db.log_heartbeat("fault_detection_agent", "RUNNING")
    log.info("=" * 70)
    log.info("FAULT DETECTION AGENT — Starting 8-gate system health scan")
    log.info("=" * 70)

    report = FaultReport(started_at=_now_str())

    # ── Gate 1: Agent liveness ────────────────────────────────────────
    try:
        gate1_agent_liveness(report, db)
    except Exception as e:
        log.error(f"Gate 1 failed: {e}", exc_info=True)
        report.add(Finding("GATE1_LIVENESS", Severity.CRITICAL, "GATE1_ERROR",
                           f"Agent liveness check failed: {e}"))

    # ── Gate 2: Data freshness ────────────────────────────────────────
    try:
        gate2_data_freshness(report, db)
    except Exception as e:
        log.error(f"Gate 2 failed: {e}", exc_info=True)
        report.add(Finding("GATE2_FRESHNESS", Severity.CRITICAL, "GATE2_ERROR",
                           f"Data freshness check failed: {e}"))

    # ── Gate 3: API connectivity ──────────────────────────────────────
    try:
        gate3_api_connectivity(report)
    except Exception as e:
        log.error(f"Gate 3 failed: {e}", exc_info=True)
        report.add(Finding("GATE3_API", Severity.CRITICAL, "GATE3_ERROR",
                           f"API connectivity check failed: {e}"))

    # ── Gate 4: System resources ──────────────────────────────────────
    try:
        gate4_system_resources(report)
    except Exception as e:
        log.error(f"Gate 4 failed: {e}", exc_info=True)
        report.add(Finding("GATE4_RESOURCES", Severity.CRITICAL, "GATE4_ERROR",
                           f"System resources check failed: {e}"))

    # ── Gate 5: Account health ────────────────────────────────────────
    try:
        gate5_account_health(report, _active_customer_ids())
    except Exception as e:
        log.error(f"Gate 5 failed: {e}", exc_info=True)
        report.add(Finding("GATE5_ACCOUNT", Severity.CRITICAL, "GATE5_ERROR",
                           f"Account health check failed: {e}"))

    # ── Gate 6: DB integrity ──────────────────────────────────────────
    try:
        gate6_db_integrity(report, db)
    except Exception as e:
        log.error(f"Gate 6 failed: {e}", exc_info=True)
        report.add(Finding("GATE6_DB", Severity.CRITICAL, "GATE6_ERROR",
                           f"DB integrity check failed: {e}"))

    # ── Gate 7: Schedule compliance ───────────────────────────────────
    try:
        gate7_schedule_compliance(report, db)
    except Exception as e:
        log.error(f"Gate 7 failed: {e}", exc_info=True)
        report.add(Finding("GATE7_SCHEDULE", Severity.CRITICAL, "GATE7_ERROR",
                           f"Schedule compliance check failed: {e}"))

    # ── Gate 8: Trade activity baseline (DEGRADED detector) ───────────
    try:
        gate8_trade_activity_baseline(report, db)
    except Exception as e:
        log.error(f"Gate 8 failed: {e}", exc_info=True)
        report.add(Finding("GATE8_ACTIVITY", Severity.CRITICAL, "GATE8_ERROR",
                           f"Trade activity baseline check failed: {e}"))

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

    # ── Raise admin alerts for actionable findings ────────────────────
    # Fault findings are always system-health problems (stale heartbeats,
    # DB lock, price staleness, etc.). Never customer-actionable. Route
    # to the shared admin_alerts stream on the master DB. emit_admin_alert
    # (in retail_shared) handles per-code dedup so a sticky fault doesn't
    # spam the inbox at the 30-min fault-scan cadence.
    admin_db = _master_db()
    fallback_cid = _CUSTOMER_ID or OWNER_CUSTOMER_ID
    written = 0
    deduped = 0
    for f in report.findings:
        if f.severity not in (Severity.CRITICAL, Severity.WARNING):
            continue
        if emit_admin_alert(admin_db, f,
                            source_agent='fault_detection_agent',
                            category='fault',
                            fallback_customer_id=fallback_cid):
            written += 1
        else:
            deduped += 1
    if written or deduped:
        log.info(f"admin_alerts: wrote {written}, deduped {deduped}")

    # ── Store scan summary in customer_settings for portal access ─────
    scan_summary = {
        "timestamp": report.completed_at,
        "worst_severity": report.worst_severity,
        "critical": report.critical_count,
        "warnings": report.warning_count,
        "total_checks": len(report.findings),
        "findings": [
            {"gate": f.gate, "severity": f.severity, "code": f.code, "message": f.message}
            for f in report.findings
            if f.severity in (Severity.CRITICAL, Severity.WARNING)
        ]
    }
    db.set_setting('_FAULT_SCAN_LAST', json.dumps(scan_summary))

    # ── Lifecycle: COMPLETE ───────────────────────────────────────────
    summary = report.summary()
    log.info("=" * 70)
    log.info(f"FAULT DETECTION — {summary}")
    log.info(f"  Worst severity: {report.worst_severity}")
    log.info("=" * 70)

    db.log_heartbeat("fault_detection_agent", "OK")
    db.log_event(
        "AGENT_COMPLETE",
        agent="Fault Detection",
        details=f"severity={report.worst_severity}, critical={report.critical_count}, "
                f"warn={report.warning_count}, checks={len(report.findings)}"
    )

    # ── Monitor heartbeat POST ────────────────────────────────────────
    try:
        from retail_heartbeat import write_heartbeat
        write_heartbeat(agent_name="fault_detection_agent", status="OK")
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
        _register_telemetry('fault_detection_agent', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

    parser = argparse.ArgumentParser(description='Synthos — Fault Detection Agent')
    parser.add_argument('--customer-id', default=None,
                        help='Customer UUID (for per-customer mode)')
    args = parser.parse_args()

    if args.customer_id:
        _CUSTOMER_ID = args.customer_id

    acquire_agent_lock("retail_fault_detection_agent.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
