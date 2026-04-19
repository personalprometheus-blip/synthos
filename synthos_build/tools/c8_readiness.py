#!/usr/bin/env python3
"""
c8_readiness.py — print whether the news-agent gate-pipeline refactor is eligible to start.

Reads the system_health_daily table (populated by daily_health_aggregator.py)
and checks the C8 entry conditions spelled out in docs/backlog.md:

  1. ≥10 trading days with zero PIPELINE_STALL
  2. Zero unexplained validator CAUTION verdicts in the window
  3. Golden-file baseline captured (tests/news_baseline/*.json present, ≥5)
  4. Tier-calibration readout run at least once weekly (logs/tier_readout/*.log)

Prints a one-screen verdict. Exit 0 = ready, 1 = not ready, 2 = table missing.

Usage:
  python3 tools/c8_readiness.py            # default: 10 trading-days window
  python3 tools/c8_readiness.py --days 14  # wider window
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SYNTHOS_HOME = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(SYNTHOS_HOME / "user" / ".env", override=True)
except Exception:
    pass
MASTER_CUSTOMER_ID = os.environ.get("OWNER_CUSTOMER_ID", "")


def _shared_db_path() -> Path:
    if MASTER_CUSTOMER_ID:
        p = SYNTHOS_HOME / "data" / "customers" / MASTER_CUSTOMER_ID / "signals.db"
        if p.exists():
            return p
    return SYNTHOS_HOME / "user" / "signals.db"


def _trading_days_back(n: int) -> list[str]:
    """Return the last N weekdays (excludes Sat/Sun) as YYYY-MM-DD strings."""
    out = []
    d = date.today()
    while len(out) < n:
        # weekday(): Mon=0 … Sun=6
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return out


def _fmt(ok: bool) -> str:
    return "  PASS" if ok else "  FAIL"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10,
                        help="trading days required clean (C8 condition #1 asks 10)")
    args = parser.parse_args()

    db = _shared_db_path()
    print(f"Checking C8 readiness against {db.name}")
    print(f"Window: last {args.days} trading days")
    print()

    # ── Condition 1 & 2: log-derived counters from system_health_daily ──
    needed_days = _trading_days_back(args.days)
    earliest = needed_days[-1]
    if not db.exists():
        print(f"  ERROR: shared DB missing at {db}")
        return 2

    conn = sqlite3.connect(str(db))
    try:
        try:
            rows = conn.execute(
                "SELECT date, pipeline_stalls, caution_verdicts, agent_errors, agent_completes "
                "FROM system_health_daily WHERE date >= ? ORDER BY date DESC",
                (earliest,),
            ).fetchall()
        except sqlite3.OperationalError:
            print("  ERROR: system_health_daily table missing — "
                  "run tools/daily_health_aggregator.py at least once.")
            return 2
    finally:
        conn.close()

    by_date = {r[0]: r for r in rows}
    missing_days = [d for d in needed_days if d not in by_date]
    stalls_total  = sum(r[1] for r in rows if r[0] in needed_days)
    caution_total = sum(r[2] for r in rows if r[0] in needed_days)
    errors_total  = sum(r[3] for r in rows if r[0] in needed_days)

    cond1 = not missing_days and stalls_total == 0
    cond2 = not missing_days and caution_total == 0

    print(f"[1] PIPELINE_STALL = 0 for {args.days} trading days")
    print(f"{_fmt(cond1)}   observed stalls in window: {stalls_total}")
    if missing_days:
        print(f"         missing days (no aggregator run): {', '.join(missing_days)}")

    print()
    print(f"[2] Validator CAUTION = 0 for {args.days} trading days")
    print(f"{_fmt(cond2)}   observed CAUTION verdicts in window: {caution_total}")

    # ── Condition 3: baseline fixtures ──
    baseline_dir = SYNTHOS_HOME / "tests" / "news_baseline"
    baseline_count = 0
    if baseline_dir.exists():
        baseline_count = len([p for p in baseline_dir.glob("cycle_*.json")])
    cond3 = baseline_count >= 5

    print()
    print("[3] Golden-file baseline ≥ 5 cycles")
    print(f"{_fmt(cond3)}   tests/news_baseline/cycle_*.json count: {baseline_count}")

    # ── Condition 4: weekly tier_readout run ──
    readout_dir = SYNTHOS_HOME / "logs" / "tier_readout"
    readout_runs = 0
    if readout_dir.exists():
        readout_runs = len(list(readout_dir.glob("*.log")))
    cond4 = readout_runs >= 1

    print()
    print("[4] Tier-calibration readout ≥ 1 run")
    print(f"{_fmt(cond4)}   logs/tier_readout/*.log count: {readout_runs}")

    # ── Summary + extras ──
    all_ok = cond1 and cond2 and cond3 and cond4
    print()
    print("─" * 56)
    print(f"OVERALL: {'READY for C8' if all_ok else 'NOT ready for C8'}")
    print("─" * 56)

    print()
    print(f"Agent activity in window: {errors_total} errors, "
          f"{sum(r[4] for r in rows if r[0] in needed_days)} AGENT_COMPLETE events")
    if rows:
        most_recent = rows[0]
        print(f"Most recent day captured: {most_recent[0]} "
              f"(stalls={most_recent[1]}, caution={most_recent[2]}, "
              f"errors={most_recent[3]}, completes={most_recent[4]})")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
