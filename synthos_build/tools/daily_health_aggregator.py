#!/usr/bin/env python3
"""
daily_health_aggregator.py — roll up today's pipeline health into one DB row.

Runs via cron at 23:55 ET, BEFORE logs rotate at midnight. Parses the day's
log files + queries the shared signals.db, then writes a single summary row
to `system_health_daily` in the master customer's signals.db. The table
survives log rotation, so we can query rolling N-day stability windows for
refactor-readiness gates (backlog C8).

Metrics captured per day:
  - pipeline_stalls   — PIPELINE_STALL events in watchdog.log
  - caution_verdicts  — CAUTION markers in scheduler.log
  - agent_errors      — ERROR-level lines in watchdog.log + scheduler.log
  - agent_completes   — AGENT_COMPLETE rows in system_log (DB-resident)

Usage:
  python3 tools/daily_health_aggregator.py              # aggregate today
  python3 tools/daily_health_aggregator.py --date YYYY-MM-DD

Query for C8 readiness (2-week clean window):
  SELECT COUNT(*) FROM system_health_daily
   WHERE date >= date('now','-14 days')
     AND (pipeline_stalls > 0 OR caution_verdicts > 0);
  -- 0 = clean, any positive count = not yet eligible
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SYNTHOS_HOME = Path(__file__).resolve().parent.parent
LOG_DIR = SYNTHOS_HOME / "logs"

# Load OWNER_CUSTOMER_ID so we target the master customer's shared DB.
try:
    from dotenv import load_dotenv
    load_dotenv(SYNTHOS_HOME / "user" / ".env", override=True)
except Exception:
    pass
MASTER_CUSTOMER_ID = os.environ.get("OWNER_CUSTOMER_ID", "")


def _count_log_pattern(path: Path, pattern: str, date_prefix: str) -> int:
    """Count lines whose timestamp starts with date_prefix and match pattern."""
    if not path.exists():
        return 0
    rx = re.compile(pattern)
    count = 0
    with open(path, "r", errors="ignore") as f:
        for line in f:
            if date_prefix in line[:25] and rx.search(line):
                count += 1
    return count


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_health_daily (
          date              TEXT PRIMARY KEY,
          pipeline_stalls   INTEGER NOT NULL DEFAULT 0,
          caution_verdicts  INTEGER NOT NULL DEFAULT 0,
          agent_errors      INTEGER NOT NULL DEFAULT 0,
          agent_completes   INTEGER NOT NULL DEFAULT 0,
          captured_at       TEXT    NOT NULL
        );
        """
    )
    conn.commit()


def _get_shared_db_path() -> Path:
    """Master customer's signals.db — the portal's _shared_db()."""
    if MASTER_CUSTOMER_ID:
        candidate = SYNTHOS_HOME / "data" / "customers" / MASTER_CUSTOMER_ID / "signals.db"
        if candidate.exists():
            return candidate
    # Fallback — user/signals.db (admin DB)
    return SYNTHOS_HOME / "user" / "signals.db"


def _count_agent_completes(db_path: Path, date_prefix: str) -> int:
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM system_log "
            "WHERE event='AGENT_COMPLETE' AND timestamp LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def aggregate(target_date: date) -> dict:
    date_prefix = target_date.strftime("%Y-%m-%d")
    watchdog_log = LOG_DIR / "watchdog.log"
    scheduler_log = LOG_DIR / "scheduler.log"

    stats = {
        "date":             date_prefix,
        "pipeline_stalls":  _count_log_pattern(watchdog_log,  r"\bPIPELINE_STALL\b", date_prefix),
        "caution_verdicts": _count_log_pattern(scheduler_log, r"\bCAUTION\b",        date_prefix),
        "agent_errors":     (_count_log_pattern(scheduler_log, r"\bERROR\b", date_prefix)
                             + _count_log_pattern(watchdog_log,  r"\bERROR\b", date_prefix)),
        "agent_completes":  _count_agent_completes(_get_shared_db_path(), date_prefix),
    }
    return stats


def write_row(stats: dict) -> Path:
    db = _get_shared_db_path()
    if not db.exists():
        raise SystemExit(f"shared DB not found at {db}")
    conn = sqlite3.connect(str(db))
    try:
        _init_schema(conn)
        conn.execute(
            """
            INSERT INTO system_health_daily
              (date, pipeline_stalls, caution_verdicts, agent_errors,
               agent_completes, captured_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
              pipeline_stalls  = excluded.pipeline_stalls,
              caution_verdicts = excluded.caution_verdicts,
              agent_errors     = excluded.agent_errors,
              agent_completes  = excluded.agent_completes,
              captured_at      = excluded.captured_at
            """,
            (
                stats["date"],
                stats["pipeline_stalls"],
                stats["caution_verdicts"],
                stats["agent_errors"],
                stats["agent_completes"],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else date.today()
    )

    stats = aggregate(target)
    db = write_row(stats)

    print(
        f"[{stats['date']}] pipeline_stalls={stats['pipeline_stalls']} "
        f"caution={stats['caution_verdicts']} errors={stats['agent_errors']} "
        f"completes={stats['agent_completes']} → {db}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
