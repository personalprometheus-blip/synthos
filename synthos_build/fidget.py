"""
fidget.py — Fidget > Token Monitor
Synthos Company Pi | /home/<user>/synthos-company/agents/fidget.py

Role:
  Track API usage and costs across all agents. Detect anomalies.
  Suggest optimizations to Blueprint. Report to morning digest.

  Fidget is accountable for:
    - Knowing what every API call costs before it becomes a surprise
    - Flagging usage spikes that indicate bugs (10x normal = likely loop)
    - Building the data that justifies optimization decisions
    - Protecting margins as customer count scales

  Fidget does NOT make optimization decisions — it surfaces data and
  submits suggestions to suggestions.json for Blueprint to act on.

Data sources:
  - company.db api_usage table (logged by each agent after API calls)
  - token_ledger table (Anthropic-specific cost tracking)
  - Agent log files (fallback if db logging missed a call)

Schedule:
  Hourly summary:    every hour (quick anomaly check)
  Daily report:      6:30am ET (feeds morning digest)
  Monthly rollup:    1st of month, 6am ET

USAGE:
  python3 fidget.py --mode hourly   # quick anomaly check
  python3 fidget.py --mode daily    # full daily report
  python3 fidget.py --mode monthly  # monthly cost rollup
  python3 fidget.py --status        # show current month summary
"""

import os
import sys
import logging
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Paths via shared authority — no hardcoded or manually derived paths.

import os.path as _osp
_AGENTS_DIR  = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in sys.path:
    sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import (
    BASE_DIR, DATA_DIR, LOGS_DIR, ENV_PATH,
)
from db_helpers import DB as _DB

_db = _DB()   # single shared instance — bootstraps schema on first call

LOG_FILE      = LOGS_DIR / "token_monitor.log"
FIDGET_REPORT = DATA_DIR / "fidget_latest.json"

load_dotenv(ENV_PATH, override=True)

# Anthropic pricing (per million tokens) — update when pricing changes
ANTHROPIC_INPUT_COST_PER_M  = 3.00    # claude-sonnet-4
ANTHROPIC_OUTPUT_COST_PER_M = 15.00   # claude-sonnet-4
ANTHROPIC_HAIKU_INPUT       = 0.25    # claude-haiku
ANTHROPIC_HAIKU_OUTPUT      = 1.25    # claude-haiku

# Anomaly detection thresholds
SPIKE_MULTIPLIER     = 10.0    # 10x rolling average = flag
DAILY_COST_WARN      = 5.00    # $5/day warning
DAILY_COST_CRITICAL  = 20.00   # $20/day critical alert
MONTHLY_COST_WARN    = 50.00   # $50/month warning

SYNTHOS_VERSION = "1.0"

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s fidget: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("fidget")


# Schema is managed by db_helpers.DB() — no local schema definitions.


# ── TIME HELPERS ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")

def current_month() -> str:
    return now_utc().strftime("%Y-%m")


# ── COST CALCULATION ──────────────────────────────────────────────────────────

def estimate_cost(tokens_input: int, tokens_output: int,
                  model: str = "sonnet") -> float:
    """Estimate cost in USD for an Anthropic API call."""
    model = model.lower()
    if "haiku" in model:
        input_rate  = ANTHROPIC_HAIKU_INPUT  / 1_000_000
        output_rate = ANTHROPIC_HAIKU_OUTPUT / 1_000_000
    else:
        input_rate  = ANTHROPIC_INPUT_COST_PER_M  / 1_000_000
        output_rate = ANTHROPIC_OUTPUT_COST_PER_M / 1_000_000

    return round(tokens_input * input_rate + tokens_output * output_rate, 6)


# ── LOGGING HELPER (called by other agents) ──────────────────────────────────

def log_api_call(agent_name: str, provider: str, operation: str,
                 tokens_input: int = 0, tokens_output: int = 0,
                 model: str = "sonnet", pi_id: str = None) -> None:
    """
    Called by other agents after every API call to log usage.
    Delegates to db_helpers.DB.log_api_call() — direct write, never blocks.

    Usage in any agent:
        from agents.fidget import log_api_call
        log_api_call("Blueprint", "anthropic", "implement_suggestion",
                     tokens_input=2500, tokens_output=4000, model="sonnet")
    """
    cost = estimate_cost(tokens_input, tokens_output, model)
    _db.log_api_call(
        agent_name=agent_name,
        provider=provider,
        operation=operation,
        token_count=tokens_input + tokens_output,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_estimate=cost,
        model=model,
        pi_id=pi_id,
    )


# ── ANALYSIS ──────────────────────────────────────────────────────────────────
# All data retrieval delegates to db_helpers — no raw SQL in this file.

def get_daily_usage(date: str = None) -> list:
    """API usage grouped by agent for a given date. Delegates to db_helpers."""
    return _db.get_daily_api_usage(date)


def get_monthly_usage(month: str = None) -> list:
    """Token ledger summary for a month. Delegates to db_helpers."""
    return _db.get_monthly_token_usage(month)


def get_rolling_daily_average(agent_name: str, days: int = 7) -> float:
    """Rolling daily average tokens for an agent. Delegates to db_helpers."""
    return _db.get_rolling_daily_avg_tokens(agent_name, days)


def detect_anomalies() -> list:
    """
    Compare today's usage against rolling 7-day average.
    Flags agents running at SPIKE_MULTIPLIER× or more.
    """
    today     = now_utc().strftime("%Y-%m-%d")
    anomalies = []

    for row in _db.get_daily_api_usage(today):
        agent  = row["agent_name"]
        tokens = row["total_tokens"] or 0
        cost   = row["total_cost"] or 0.0
        avg    = get_rolling_daily_average(agent)

        if avg > 0 and tokens > avg * SPIKE_MULTIPLIER:
            anomalies.append({
                "agent":      agent,
                "today":      tokens,
                "avg_7d":     round(avg),
                "multiplier": round(tokens / avg, 1),
                "cost_today": round(cost, 4),
                "type":       "token_spike",
                "severity":   "CRITICAL" if tokens > avg * 20 else "HIGH",
            })

    return anomalies


def get_total_daily_cost(date: str = None) -> float:
    """Total estimated cost for a day. Delegates to db_helpers."""
    return _db.get_total_daily_cost(date)


def get_total_monthly_cost(month: str = None) -> float:
    """Total estimated cost for a month. Delegates to db_helpers."""
    return _db.get_total_monthly_cost(month)


# ── REPORTING ─────────────────────────────────────────────────────────────────

def run_hourly() -> dict:
    """Quick anomaly check — runs every hour."""
    anomalies      = detect_anomalies()
    daily_cost     = get_total_daily_cost()
    cost_severity  = None

    if daily_cost >= DAILY_COST_CRITICAL:
        cost_severity = "CRITICAL"
    elif daily_cost >= DAILY_COST_WARN:
        cost_severity = "WARN"

    result = {
        "mode":        "hourly",
        "timestamp":   now_iso(),
        "daily_cost":  round(daily_cost, 4),
        "anomalies":   anomalies,
        "cost_alert":  cost_severity,
    }

    for anomaly in anomalies:
        _submit_suggestion(
            title=f"Token spike: {anomaly['agent']} at {anomaly['multiplier']}× normal",
            description=(
                f"{anomaly['agent']} used {anomaly['today']:,} tokens today vs "
                f"{anomaly['avg_7d']:,} daily average ({anomaly['multiplier']}× normal). "
                f"Estimated cost: ${anomaly['cost_today']}. "
                f"Possible infinite loop, missing cache, or configuration error."
            ),
            risk_level=anomaly["severity"],
            effort="1 hour",
        )
        log.warning(
            f"ANOMALY: {anomaly['agent']} — "
            f"{anomaly['today']:,} tokens ({anomaly['multiplier']}× avg)"
        )

    if cost_severity == "CRITICAL":
        _submit_suggestion(
            title=f"Daily cost CRITICAL: ${daily_cost:.2f} today",
            description=(
                f"Total API spend today is ${daily_cost:.2f}, exceeding the "
                f"${DAILY_COST_CRITICAL}/day critical threshold. "
                f"Investigate anomalies immediately."
            ),
            risk_level="CRITICAL",
            effort="Immediate",
        )

    # Write latest for portal
    FIDGET_REPORT.write_text(json.dumps(result, indent=2))
    log.info(f"Hourly: daily_cost=${daily_cost:.4f} anomalies={len(anomalies)}")
    return result


def run_daily() -> dict:
    """Full daily report — feeds morning digest."""
    today    = now_utc().strftime("%Y-%m-%d")
    month    = current_month()

    daily    = get_daily_usage(today)
    monthly  = get_monthly_usage(month)
    anomalies = detect_anomalies()

    daily_cost   = sum(r["total_cost"] or 0 for r in daily)
    monthly_cost = sum(r["total_cost"] or 0 for r in monthly)

    # Optimization suggestions
    _generate_optimization_suggestions(daily, monthly)

    result = {
        "mode":          "daily",
        "timestamp":     now_iso(),
        "date":          today,
        "month":         month,
        "daily_cost":    round(daily_cost, 4),
        "monthly_cost":  round(monthly_cost, 4),
        "monthly_projection": round(monthly_cost / max(now_utc().day, 1) * 30, 2),
        "daily_by_agent":  daily,
        "monthly_by_agent": monthly,
        "anomalies":     anomalies,
    }

    FIDGET_REPORT.write_text(json.dumps(result, indent=2))
    log.info(
        f"Daily report: today=${daily_cost:.4f} "
        f"month=${monthly_cost:.4f} "
        f"projected=${result['monthly_projection']:.2f}"
    )
    return result


def run_monthly() -> dict:
    """Monthly cost rollup — runs 1st of month."""
    prev_month = (now_utc().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    monthly    = get_monthly_usage(prev_month)
    total      = sum(r["total_cost"] or 0 for r in monthly)

    result = {
        "mode":     "monthly",
        "month":    prev_month,
        "timestamp": now_iso(),
        "total_cost": round(total, 4),
        "by_agent": monthly,
    }

    log.info(f"Monthly rollup for {prev_month}: ${total:.4f}")

    if total > MONTHLY_COST_WARN:
        _submit_suggestion(
            title=f"Monthly cost {prev_month}: ${total:.2f}",
            description=(
                f"Total API spend for {prev_month} was ${total:.2f}, "
                f"exceeding the ${MONTHLY_COST_WARN}/month warning threshold. "
                f"Review optimization opportunities in the suggestions queue."
            ),
            risk_level="WARN",
            effort="30 min review",
        )

    return result


def _generate_optimization_suggestions(daily: list, monthly: list) -> None:
    """
    Identify patterns that suggest caching or batching opportunities.
    Submits suggestions to Blueprint.
    """
    # Check for agents making many small calls vs. a few larger ones
    for row in daily:
        agent  = row["agent_name"]
        calls  = row["total_calls"] or 0
        tokens = row["total_tokens"] or 0

        if calls > 50 and tokens > 0:
            avg_per_call = tokens / calls
            if avg_per_call < 500:
                _submit_suggestion(
                    title=f"Cache opportunity: {agent} making {calls} small calls/day",
                    description=(
                        f"{agent} made {calls} API calls today with avg {avg_per_call:.0f} "
                        f"tokens per call. High call count with small payloads often "
                        f"indicates repeated queries that could be cached. "
                        f"Estimated saving: 50-70% of current call volume."
                    ),
                    risk_level="LOW",
                    effort="2 hours",
                    dedupe_days=7,   # don't re-suggest more than once per week
                )


# ── SUGGESTIONS ───────────────────────────────────────────────────────────────

def _submit_suggestion(title: str, description: str, risk_level: str,
                       effort: str, dedupe_days: int = 1) -> None:
    """
    Submit optimization suggestion via db_helpers.
    Deduplication and slot coordination handled by db_helpers.post_suggestion().
    """
    try:
        with _db.slot("Fidget", "post_suggestion", priority=6):
            _db.post_suggestion(
                agent="Fidget",
                category="cost",
                title=title,
                description=description,
                risk_level=risk_level,
                affected_component="API usage",
                estimated_improvement="Cost reduction",
                effort=effort,
                complexity="SIMPLE",
                root_cause=description,
                solution_approach="Blueprint to analyze and optimize",
                metrics_to_track=["Token count after optimization"],
                dedupe_hours=dedupe_days * 24,
            )
    except Exception as e:
        log.error(f"Failed to submit suggestion: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status() -> None:
    month  = current_month()
    today  = now_utc().strftime("%Y-%m-%d")
    daily  = get_daily_usage(today)
    monthly = get_monthly_usage(month)
    anomalies = detect_anomalies()

    daily_cost   = sum(r["total_cost"] or 0 for r in daily)
    monthly_cost = sum(r["total_cost"] or 0 for r in monthly)
    projected    = round(monthly_cost / max(now_utc().day, 1) * 30, 2)

    print(f"\n{'=' * 60}")
    print(f"FIDGET STATUS — {today}")
    print(f"{'=' * 60}")
    print(f"  Today:     ${daily_cost:.4f}")
    print(f"  This month: ${monthly_cost:.4f}  (projected: ${projected:.2f})")
    if anomalies:
        print(f"  ANOMALIES: {len(anomalies)}")
        for a in anomalies:
            print(f"    ⚠ {a['agent']}: {a['multiplier']}× normal")
    else:
        print(f"  No anomalies detected")
    print(f"\n  BY AGENT (today):")
    for row in daily[:8]:
        print(
            f"    {row['agent_name']:20} "
            f"{row['total_tokens'] or 0:>8,} tokens "
            f"${row['total_cost'] or 0:.4f}"
        )
    print(f"{'=' * 60}\n")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fidget — Token Monitor")
    parser.add_argument(
        "--mode",
        choices=["hourly", "daily", "monthly"],
        default="hourly",
        help="hourly=anomaly check, daily=full report, monthly=rollup"
    )
    parser.add_argument("--status", action="store_true",
                        help="Show current month summary")
    args = parser.parse_args()

    # Schema bootstrapped by _db = _DB() at module load — no explicit call needed.

    if args.status:
        show_status()
    elif args.mode == "hourly":
        run_hourly()
    elif args.mode == "daily":
        run_daily()
    elif args.mode == "monthly":
        run_monthly()
