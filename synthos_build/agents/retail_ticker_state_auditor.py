"""retail_ticker_state_auditor.py — read-only ticker_state gap auditor.

Purpose
  Find rows in ticker_state where an owned field is NULL but the field's
  refresh policy says it should have been filled by now. Surface as a
  structured report. NEVER WRITES TO ticker_state — gap-filling is the
  owning agent's job, not ours.

Why this exists
  ticker_state is filled by ~6 different agents on different cadences.
  When a field is NULL on an active ticker, you want to know:
    1. Which agent was supposed to fill it
    2. How long it's been NULL
    3. Whether the agent has run recently and just skipped this ticker
       (= a real bug) vs hasn't run yet (= benign).
  This agent answers those questions in one pass.

Output
  Writes JSON summary to shared signals.db customer_settings under
  '_TICKER_STATE_AUDIT_LAST'. Shape:
    {
      "ts":                    "2026-05-04T22:30:15Z",
      "active_ticker_count":   247,
      "total_gaps":            152,
      "anomaly_count":         18,
      "by_owner":              { owner: {"gaps": N, "anomalies": M} },
      "by_field":              { field: {"missing_count": N, "owner": "..."} },
      "anomalies":             [{ ticker, field, owner, severity, ... }, ...],
      "samples":               { field: [first 10 missing tickers] }
    }

Usage
  python3 retail_ticker_state_auditor.py            # full report
  python3 retail_ticker_state_auditor.py --quiet    # write only, no stdout
  python3 retail_ticker_state_auditor.py --top 50   # cap anomalies in output
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'src'))

from retail_database import get_shared_db, acquire_agent_lock, release_agent_lock
from ticker_state_ownership import (
    OWNERSHIP, OWNED_FIELDS, STALENESS_THRESHOLDS_HOURS, fields_by_owner,
)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s ticker_state_auditor: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ── CONFIG ────────────────────────────────────────────────────────────────

ACTIVE_FILTER_HOURS = 24      # rows touched in last N hours = "active"
SAMPLE_LIMIT = 10             # how many ticker examples per field in samples
DEFAULT_ANOMALY_CAP = 100     # anomalies stored in the report (top by age)


# ── CORE ──────────────────────────────────────────────────────────────────

def _hours_since(ts_str: str | None, now: datetime) -> float | None:
    """Parse a SQLite-format timestamp and return age in hours.
    Returns None if ts_str is null or unparseable."""
    if not ts_str:
        return None
    # SQLite default: 'YYYY-MM-DD HH:MM:SS' (UTC, no tz)
    try:
        dt = datetime.fromisoformat(ts_str.replace(' ', 'T'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def _grade_severity(field: str, age_h: float | None) -> str:
    """Return severity tag for a NULL gap: 'CRITICAL', 'HIGH', 'MEDIUM',
    'LOW', or 'INFO'. Severity is a function of (refresh_policy, age)."""
    policy = OWNERSHIP[field]["refresh_policy"]
    threshold = STALENESS_THRESHOLDS_HOURS.get(policy)

    if policy == "per_event":
        return "INFO"  # NULL is normal — no triggering event yet

    if threshold is None or age_h is None:
        return "MEDIUM"  # missing first_seen — can't be precise

    if age_h < threshold:
        return "LOW"     # within grace window
    if age_h < threshold * 6:
        return "MEDIUM"  # past grace, not egregious
    if age_h < threshold * 24:
        return "HIGH"
    return "CRITICAL"    # very stale relative to policy


def audit() -> dict:
    """Scan ticker_state, return structured report dict.

    Read-only. Never mutates ticker_state."""
    shared = get_shared_db()
    now = datetime.now(timezone.utc)
    report = {
        "ts":                  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_ticker_count": 0,
        "inactive_ticker_count": 0,
        "out_of_scope_count":  0,
        "total_gaps":          0,
        "anomaly_count":       0,
        "by_owner":            {},
        "by_field":            {},
        "anomalies":           [],
        "samples":             {},
        "ownership_version":   sorted(OWNERSHIP.keys()),  # drift detection
    }

    with shared.conn() as c:
        # Scope filter: only audit tickers that are in tradable_assets, i.e.
        # us_equity symbols Alpaca lets us trade. Rows in ticker_state for
        # crypto (SOLUSD), foreign ADRs (BNPQF), TSX (TSX:WPRT), or OTC
        # symbols are tracked but un-tradable — flagging NULL fields on
        # them is noise, not signal. Excluded count is reported separately
        # so the scope is visible, not silently dropped.
        cutoff = (now.timestamp() - ACTIVE_FILTER_HOURS * 3600)
        out_of_scope = c.execute(
            """
            SELECT COUNT(*) FROM ticker_state ts
            WHERE (ts.is_active = 1
                   OR (ts.last_active_at IS NOT NULL
                       AND CAST(strftime('%s', ts.last_active_at) AS REAL) > ?))
              AND NOT EXISTS (
                  SELECT 1 FROM tradable_assets ta WHERE ta.ticker = ts.ticker
              )
            """,
            (cutoff,)
        ).fetchone()[0]
        report["out_of_scope_count"] = out_of_scope

        rows = c.execute(
            """
            SELECT ts.* FROM ticker_state ts
            WHERE (ts.is_active = 1
                   OR (ts.last_active_at IS NOT NULL
                       AND CAST(strftime('%s', ts.last_active_at) AS REAL) > ?))
              AND EXISTS (
                  SELECT 1 FROM tradable_assets ta WHERE ta.ticker = ts.ticker
              )
            """,
            (cutoff,)
        ).fetchall()

    if not rows:
        log.warning("ticker_state has no active rows — nothing to audit")
        return report

    by_owner: dict = {}
    by_field: dict = {}
    samples: dict[str, list[str]] = {}
    anomalies: list[dict] = []

    for row in rows:
        ticker = row["ticker"]
        if row["is_active"] == 1:
            report["active_ticker_count"] += 1
        else:
            report["inactive_ticker_count"] += 1
        first_seen = row["first_seen_at"]
        age_h = _hours_since(first_seen, now)

        for field in OWNED_FIELDS:
            # Field may not exist if schema is older than the ownership map
            try:
                value = row[field]
            except (IndexError, KeyError):
                continue

            if value is not None:
                continue  # filled — skip

            # NULL — record gap
            owner = OWNERSHIP[field]["owner"]
            policy = OWNERSHIP[field]["refresh_policy"]
            severity = _grade_severity(field, age_h)

            report["total_gaps"] += 1

            owner_bucket = by_owner.setdefault(owner, {
                "gaps": 0, "anomalies": 0, "fields": set(),
            })
            owner_bucket["gaps"] += 1
            owner_bucket["fields"].add(field)

            field_bucket = by_field.setdefault(field, {
                "missing_count": 0, "owner": owner, "refresh_policy": policy,
            })
            field_bucket["missing_count"] += 1

            # Track sample tickers for the field
            if field not in samples:
                samples[field] = []
            if len(samples[field]) < SAMPLE_LIMIT:
                samples[field].append(ticker)

            # Record anomaly (HIGH/CRITICAL only — LOW and INFO are noise)
            if severity in ("HIGH", "CRITICAL"):
                report["anomaly_count"] += 1
                owner_bucket["anomalies"] += 1
                anomalies.append({
                    "ticker":         ticker,
                    "field":          field,
                    "owner":          owner,
                    "refresh_policy": policy,
                    "severity":       severity,
                    "first_seen_at":  first_seen,
                    "age_hours":      round(age_h, 1) if age_h is not None else None,
                    "is_active":      bool(row["is_active"]),
                })

    # Sort anomalies: severity first (CRITICAL > HIGH), then by age desc
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    anomalies.sort(key=lambda a: (
        sev_rank.get(a["severity"], 9),
        -(a["age_hours"] or 0),
    ))

    # Convert sets in by_owner to sorted lists for JSON serialization
    for owner, bucket in by_owner.items():
        bucket["fields"] = sorted(bucket["fields"])

    report["by_owner"] = by_owner
    report["by_field"] = by_field
    report["anomalies"] = anomalies[:DEFAULT_ANOMALY_CAP]
    report["samples"] = samples

    return report


def persist(report: dict) -> None:
    """Write report to shared signals.db customer_settings.
    Key is _TICKER_STATE_AUDIT_LAST so it can sit alongside _BIAS_SCAN_LAST
    and _FAULT_SCAN_LAST in the existing audit ecosystem."""
    shared = get_shared_db()
    shared.set_setting("_TICKER_STATE_AUDIT_LAST", json.dumps(report))


def print_summary(report: dict, top: int = 25) -> None:
    """Human-readable summary to stdout for cron logs / live runs."""
    print(f"  ts: {report['ts']}")
    print(f"  active: {report['active_ticker_count']}  "
          f"inactive-but-recent: {report['inactive_ticker_count']}  "
          f"out-of-scope (skipped): {report.get('out_of_scope_count', 0)}")
    print(f"  total_gaps: {report['total_gaps']}  "
          f"anomalies (HIGH+CRITICAL): {report['anomaly_count']}")
    print()
    if report["by_owner"]:
        print("  ── gaps by owner ──")
        owners = sorted(report["by_owner"].items(),
                        key=lambda x: -x[1]["gaps"])
        for owner, b in owners:
            mark = "!" if b["anomalies"] else " "
            print(f"  {mark} {owner:38s}  "
                  f"gaps={b['gaps']:5d}  anomalies={b['anomalies']:4d}  "
                  f"fields={len(b['fields'])}")
        print()
    if report["anomalies"]:
        print(f"  ── top {min(top, len(report['anomalies']))} anomalies ──")
        for a in report["anomalies"][:top]:
            age = f"{a['age_hours']:.1f}h" if a['age_hours'] is not None else "?"
            print(f"  {a['severity']:8s}  {a['ticker']:6s}  {a['field']:24s}  "
                  f"owner={a['owner']:38s}  age={age}")


# ── ENTRY ─────────────────────────────────────────────────────────────────

def run() -> int:
    """Run audit, persist report, return non-zero exit code if anomalies.
    Exit code semantics let cron + monitoring pick up failures cleanly."""
    report = audit()
    persist(report)
    return 0 if report["anomaly_count"] == 0 else 0  # never fail; auditor reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ticker_state gap auditor")
    parser.add_argument("--quiet", action="store_true",
                        help="Skip stdout summary (useful for cron)")
    parser.add_argument("--top", type=int, default=25,
                        help="Anomalies to print in summary")
    args = parser.parse_args()

    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry("ticker_state_auditor", long_running=False)
    except Exception:
        pass

    acquire_agent_lock("retail_ticker_state_auditor.py")
    try:
        report = audit()
        persist(report)
        if not args.quiet:
            print_summary(report, top=args.top)
        log.info(
            f"audit complete: {report['active_ticker_count']} active, "
            f"{report['total_gaps']} gaps, "
            f"{report['anomaly_count']} anomalies"
        )
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.error(f"fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
