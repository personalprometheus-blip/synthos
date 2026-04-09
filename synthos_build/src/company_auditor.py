"""
company_auditor.py — Synthos Operations Auditor
Synthos · Company Pi Agent

Continuous log scanner and health reporter. Watches agent logs for genuine
errors, deduplicates findings, and produces a daily morning report.

Design principles:
    - NEVER flood the alert queue. One summary notification per scan cycle max.
    - Deduplicate: same error repeated 100x = 1 issue with a hit count.
    - Ignore known-good patterns (connection retries, graceful shutdowns, etc.)
    - Detect log rotation: if file shrank, reset offset and scan from top.
    - All thresholds configurable via .env.

Schedule:
    --daemon    : poll every AUDITOR_POLL_SECS (default 300s)
                  Morning report generated once daily at AUDITOR_REPORT_HOUR ET
    (no flag)   : run one scan, print summary, exit
    --status    : print issue counts and recent findings, exit

.env optional:
    AUDITOR_POLL_SECS=300          — seconds between scan cycles
    AUDITOR_REPORT_HOUR=6          — hour (0-23) ET for morning report
    AUDITOR_LOG_DIR=logs           — directory to scan (relative to project root)
    AUDITOR_DB_PATH=data/auditor.db
    AUDITOR_ALERT_THRESHOLD=3      — min critical issues to trigger scoop alert
    AUDITOR_DEDUP_WINDOW_MINS=60   — dedup window (same file+pattern = 1 issue)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_SRC_DIR_ENV = Path(__file__).resolve().parent
# Load .env from src/ (where it lives on pi4b), fall back to project root
_env_path = _SRC_DIR_ENV / '.env'
if not _env_path.exists():
    _env_path = _PROJECT_DIR / '.env'
load_dotenv(_env_path)

log = logging.getLogger('auditor')
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s auditor: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# ── CONFIG ─────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

POLL_SECS           = int(os.getenv('AUDITOR_POLL_SECS', '300'))
REPORT_HOUR         = int(os.getenv('AUDITOR_REPORT_HOUR', '6'))
ALERT_THRESHOLD     = int(os.getenv('AUDITOR_ALERT_THRESHOLD', '3'))
DEDUP_WINDOW_MINS   = int(os.getenv('AUDITOR_DEDUP_WINDOW_MINS', '60'))

_SRC_DIR    = Path(__file__).resolve().parent
DATA_DIR    = _SRC_DIR / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH     = Path(os.getenv('AUDITOR_DB_PATH', str(DATA_DIR / 'auditor.db')))
LOG_DIR     = _PROJECT_DIR / os.getenv('AUDITOR_LOG_DIR', 'logs')

# ── ERROR PATTERNS ────────────────────────────────────────────────────────
# Each tuple: (compiled regex, severity)
# Patterns are tested in order; first match wins for a given line.
# Use word boundaries (\b) to avoid matching substrings like "ERROR_HANDLER".
ERROR_PATTERNS = [
    # Critical — system-level failures
    (re.compile(r'\bCRITICAL\b|\bFATAL\b'), 'critical'),
    # High — real errors (word-boundary prevents matching ERROR_HANDLER etc.)
    (re.compile(r'Traceback \(most recent call last\)'), 'high'),
    (re.compile(r'\bOperationalError\b|\bIntegrityError\b|\bDatabaseError\b'), 'high'),
    (re.compile(r'^\[.*?\]\s+ERROR\s', re.MULTILINE), 'high'),  # structured log: [timestamp] ERROR msg
    # Medium — connectivity problems
    (re.compile(r'\bConnectionError\b|\bTimeoutError\b|\bConnectionRefused\b'), 'medium'),
    # Low — warnings with failure context
    (re.compile(r'\bWARNING\b.*\b(?:retry|failed|unavailable|unreachable)\b', re.IGNORECASE), 'low'),
]

# Lines matching these patterns are ALWAYS ignored, even if they match above.
# Add entries here for known-good log output that contains error-like words.
IGNORE_PATTERNS = [
    re.compile(r'heartbeat.*non-fatal', re.IGNORECASE),
    re.compile(r'Monitor unreachable.*skipped', re.IGNORECASE),
    re.compile(r'DB read skipped in heartbeat', re.IGNORECASE),
    re.compile(r'graceful\s+shutdown', re.IGNORECASE),
    re.compile(r'Signal \d+ received', re.IGNORECASE),
    re.compile(r'error_handler|error_recovery|ErrorBoundary', re.IGNORECASE),
    re.compile(r'auditor:', re.IGNORECASE),  # don't audit our own log lines
]


# ── DATABASE ───────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detected_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    severity        TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    context         TEXT,
    hit_count       INTEGER DEFAULT 1,
    acknowledged    INTEGER DEFAULT 0,
    resolved        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS morning_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,
    report          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_state (
    log_file        TEXT PRIMARY KEY,
    last_offset     INTEGER NOT NULL DEFAULT 0,
    file_size       INTEGER NOT NULL DEFAULT 0,
    last_scanned    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_severity ON detected_issues(severity);
CREATE INDEX IF NOT EXISTS idx_issues_resolved ON detected_issues(resolved);
CREATE INDEX IF NOT EXISTS idx_issues_dedup ON detected_issues(source_file, pattern, resolved);
"""


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with _db() as c:
        c.executescript(_SCHEMA)
    log.info("Auditor DB ready at %s", DB_PATH)


# ── LOG SCANNING ──────────────────────────────────────────────────────────

def _is_ignored(line: str) -> bool:
    """Return True if line matches any ignore pattern."""
    return any(p.search(line) for p in IGNORE_PATTERNS)


def _match_pattern(line: str) -> tuple[str, str] | None:
    """Return (pattern_text, severity) for first matching error pattern, or None."""
    for pattern, severity in ERROR_PATTERNS:
        if pattern.search(line):
            return (pattern.pattern, severity)
    return None


def scan_log_file(log_path: Path) -> list[dict]:
    """
    Scan a single log file from last known offset.
    Detects log rotation (file shrank → reset to 0).
    Returns list of raw issue dicts (before dedup).
    """
    if not log_path.exists():
        return []

    file_size = log_path.stat().st_size

    # Get last scan state
    with _db() as c:
        row = c.execute(
            "SELECT last_offset, file_size FROM scan_state WHERE log_file = ?",
            (str(log_path),),
        ).fetchone()

    if row:
        old_offset = row['last_offset']
        old_size   = row['file_size']
        # Detect rotation: file is smaller than our stored offset
        if file_size < old_offset:
            log.info("Log rotation detected for %s (was %d, now %d) — resetting",
                     log_path.name, old_offset, file_size)
            offset = 0
        else:
            offset = old_offset
    else:
        offset = 0

    issues = []
    try:
        with open(log_path, 'r', errors='replace') as f:
            f.seek(offset)
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if _is_ignored(stripped):
                    continue
                match = _match_pattern(stripped)
                if match:
                    pat_text, severity = match
                    issues.append({
                        'source_file': log_path.name,
                        'severity':    severity,
                        'pattern':     pat_text,
                        'context':     stripped[:300],
                    })
            new_offset = f.tell()
    except OSError as e:
        log.error("Failed to scan %s: %s", log_path, e)
        return issues

    # Update scan state
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO scan_state (log_file, last_offset, file_size, last_scanned) "
            "VALUES (?, ?, ?, ?)",
            (str(log_path), new_offset, file_size,
             datetime.now(timezone.utc).isoformat()),
        )

    return issues


def _dedup_and_store(issues: list[dict]) -> dict:
    """
    Deduplicate issues against recent DB entries. If the same source_file +
    pattern was seen within DEDUP_WINDOW_MINS, increment hit_count instead
    of inserting a new row.

    Returns summary: {severity: count_of_new_issues}
    """
    if not issues:
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff  = (datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINS)).isoformat()
    summary = {}

    with _db() as c:
        for issue in issues:
            sf  = issue['source_file']
            pat = issue['pattern']
            sev = issue['severity']

            # Check for existing unresolved issue with same file+pattern in window
            existing = c.execute(
                "SELECT id FROM detected_issues "
                "WHERE source_file = ? AND pattern = ? AND resolved = 0 AND last_seen >= ?",
                (sf, pat, cutoff),
            ).fetchone()

            if existing:
                # Bump hit count + timestamp
                c.execute(
                    "UPDATE detected_issues SET hit_count = hit_count + 1, last_seen = ? WHERE id = ?",
                    (now_iso, existing['id']),
                )
            else:
                # New issue
                c.execute(
                    "INSERT INTO detected_issues "
                    "(first_seen, last_seen, source_file, severity, pattern, context) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now_iso, now_iso, sf, sev, pat, issue.get('context', '')),
                )
                summary[sev] = summary.get(sev, 0) + 1

    return summary


def scan_all_logs() -> dict:
    """Scan all *.log files. Returns summary of NEW (non-deduped) issues."""
    if not LOG_DIR.exists():
        log.warning("Log directory not found: %s", LOG_DIR)
        return {}

    all_issues = []
    for log_file in sorted(LOG_DIR.glob('*.log')):
        issues = scan_log_file(log_file)
        all_issues.extend(issues)

    summary = _dedup_and_store(all_issues)
    return summary


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────

def _notify_scoop(subject: str, body: str, severity: str = 'high'):
    """Insert directly into company.db scoop_queue. Never throws."""
    try:
        import uuid
        company_db = os.getenv('COMPANY_DB_PATH',
                               str(_PROJECT_DIR / 'data' / 'company.db'))
        eid       = str(uuid.uuid4())
        priority  = 0 if severity == 'critical' else 1
        queued_at = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(company_db, timeout=10)
        conn.execute(
            "INSERT INTO scoop_queue "
            "(id, event_type, priority, subject, body, source_agent, "
            " audience, status, queued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, 'auditor_alert', priority, subject, body,
             'company_auditor', 'ops', 'queued', queued_at),
        )
        conn.commit()
        conn.close()
        log.info("Scoop alert queued: %s", subject)
    except Exception as e:
        log.warning("Could not queue scoop alert: %s", e)


# ── SCAN CYCLE ────────────────────────────────────────────────────────────

def run_scan() -> dict:
    """Run one audit scan. Returns summary dict."""
    summary = scan_all_logs()

    total_new = sum(summary.values())
    crit_new  = summary.get('critical', 0)
    high_new  = summary.get('high', 0)

    result = {
        'scanned_at': datetime.now(timezone.utc).isoformat(),
        'new_issues': total_new,
        'by_severity': summary,
    }

    if total_new == 0:
        log.info("Scan complete — no new issues")
    else:
        log.warning("Scan found %d new issue(s): %s", total_new, summary)

    # Single batched notification if critical count exceeds threshold
    if crit_new >= ALERT_THRESHOLD:
        _notify_scoop(
            subject=f"Auditor: {crit_new} critical + {high_new} high issues detected",
            body=json.dumps(summary),
            severity='critical',
        )

    return result


# ── MORNING REPORT ────────────────────────────────────────────────────────

def generate_morning_report() -> dict:
    """Generate daily summary. Counts last-24h issues by severity."""
    now      = datetime.now(timezone.utc)
    cutoff   = (now - timedelta(days=1)).isoformat()

    with _db() as c:
        rows = c.execute(
            "SELECT severity, COUNT(*) as cnt, SUM(hit_count) as hits "
            "FROM detected_issues WHERE last_seen >= ? GROUP BY severity",
            (cutoff,),
        ).fetchall()

        unresolved = c.execute(
            "SELECT COUNT(*) FROM detected_issues WHERE resolved = 0",
        ).fetchone()[0]

        # Top 5 noisiest unresolved issues
        top = c.execute(
            "SELECT source_file, severity, pattern, hit_count, context "
            "FROM detected_issues WHERE resolved = 0 "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END, hit_count DESC LIMIT 5",
        ).fetchall()

    counts = {r['severity']: {'unique': r['cnt'], 'hits': r['hits']} for r in rows}

    report = {
        'date':             now.strftime('%Y-%m-%d'),
        'generated_at':     now.isoformat(),
        'last_24h':         counts,
        'total_unresolved': unresolved,
        'top_issues':       [dict(r) for r in top],
        'status':           'healthy' if not counts.get('critical') else 'needs_attention',
    }

    # Store report
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO morning_reports (date, report, created_at) VALUES (?, ?, ?)",
            (report['date'], json.dumps(report, default=str), now.isoformat()),
        )

    # Alert only if critical issues exist
    crit = counts.get('critical', {})
    if crit.get('unique', 0) > 0:
        _notify_scoop(
            subject=f"Morning report: {crit['unique']} critical issues ({crit.get('hits',0)} total hits)",
            body=json.dumps(report, default=str, indent=2),
            severity='critical',
        )

    log.info("Morning report: %s", json.dumps(counts, default=str))
    return report


# ── DAEMON ────────────────────────────────────────────────────────────────

def run_daemon():
    """Poll continuously, generate morning report at REPORT_HOUR ET."""
    log.info("Auditor daemon starting — poll=%ds, report_hour=%d ET, "
             "alert_threshold=%d, dedup_window=%dm",
             POLL_SECS, REPORT_HOUR, ALERT_THRESHOLD, DEDUP_WINDOW_MINS)

    last_report_date = None

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        # Check if it's time for morning report
        now_et = datetime.now(ET)
        today  = now_et.strftime('%Y-%m-%d')
        if now_et.hour == REPORT_HOUR and last_report_date != today:
            try:
                generate_morning_report()
                last_report_date = today
            except Exception as e:
                log.error("Morning report error: %s", e, exc_info=True)

        time.sleep(POLL_SECS)


# ── STATUS ────────────────────────────────────────────────────────────────

def print_status():
    """Print current issue summary."""
    with _db() as c:
        by_sev = c.execute(
            "SELECT severity, COUNT(*) as cnt, SUM(hit_count) as hits "
            "FROM detected_issues WHERE resolved = 0 GROUP BY severity"
        ).fetchall()

        recent = c.execute(
            "SELECT source_file, severity, hit_count, context, last_seen "
            "FROM detected_issues WHERE resolved = 0 "
            "ORDER BY last_seen DESC LIMIT 10"
        ).fetchall()

        scans = c.execute(
            "SELECT log_file, last_offset, file_size, last_scanned "
            "FROM scan_state ORDER BY last_scanned DESC"
        ).fetchall()

    print(f"\n[Auditor] DB: {DB_PATH}")
    print(f"[Auditor] Log dir: {LOG_DIR}")
    print(f"\nUnresolved issues by severity:")
    for r in by_sev:
        print(f"  {r['severity']:10s}  {r['cnt']} unique  ({r['hits']} total hits)")

    if recent:
        print(f"\nMost recent unresolved:")
        for r in recent:
            print(f"  [{r['severity']:8s}] {r['source_file']}  x{r['hit_count']}  "
                  f"{r['context'][:80]}...")

    if scans:
        print(f"\nScan state:")
        for s in scans:
            print(f"  {Path(s['log_file']).name:30s}  offset={s['last_offset']:>8d}  "
                  f"size={s['file_size']:>8d}  scanned={s['last_scanned'][:19]}")


# ── ENTRY POINT ──────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Synthos Operations Auditor")
    parser.add_argument('--daemon', action='store_true', help='Run continuously')
    parser.add_argument('--morning-report', action='store_true', help='Generate morning report')
    parser.add_argument('--status', action='store_true', help='Print current issue summary')
    args = parser.parse_args()

    init_db()

    if args.status:
        print_status()
        return 0

    if args.morning_report:
        report = generate_morning_report()
        print(json.dumps(report, indent=2, default=str))
        return 0

    if args.daemon:
        run_daemon()
        return 0

    # Default: single scan
    summary = run_scan()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
