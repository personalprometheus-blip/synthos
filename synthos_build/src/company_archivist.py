"""
company_archivist.py — Synthos Historical Data Archiver
========================================================
Runs on the Company Pi as a nightly daemon (or one-shot via --run-now).
Archives aged-out rows from company.db to compressed JSON files, then
deletes the originals to keep the live database lean.

Tables archived:
    scoop_queue  — rows with status in ('sent','skipped','failed')
                   older than ARCHIVE_AFTER_DAYS (default 30 days)
    pi_events    — all rows older than PI_EVENTS_ARCHIVE_DAYS (default 60 days)

Archive layout:
    data/archives/YYYY-MM/scoop_queue_YYYY-MM-DD.json.gz
    data/archives/YYYY-MM/pi_events_YYYY-MM-DD.json.gz
    data/archives/index.json     ← manifest updated after each run

Each archive file is a gzip-compressed JSON array of row dicts.
Existing archive files for the same date are merged (not overwritten).

.env required:
    (none — reads COMPANY_DB_PATH if set, falls back to data/company.db)

.env optional:
    ARCHIVE_AFTER_DAYS=30         — scoop_queue retention in live DB
    PI_EVENTS_ARCHIVE_DAYS=60     — pi_events retention in live DB
    ARCHIVE_DIR=data/archives     — root directory for archive files
    ARCHIVIST_RUN_HOUR=2          — hour (0–23) to run nightly (default 2)
    ARCHIVIST_LOG=logs/archivist.log

Usage:
    python3 company_archivist.py               # daemon mode — runs nightly
    python3 company_archivist.py --run-now     # single archive run, then exit
    python3 company_archivist.py --status      # print last-run info, then exit
"""

import argparse
import gzip
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ARCHIVE_AFTER_DAYS      = int(os.getenv("ARCHIVE_AFTER_DAYS", 30))
PI_EVENTS_ARCHIVE_DAYS  = int(os.getenv("PI_EVENTS_ARCHIVE_DAYS", 60))
ARCHIVIST_RUN_HOUR      = int(os.getenv("ARCHIVIST_RUN_HOUR", 2))
ARCHIVIST_LOG           = os.getenv("ARCHIVIST_LOG", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH     = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", os.path.join(DATA_DIR, "archives")))
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH  = ARCHIVE_DIR / "index.json"

_BUILD_DIR = os.path.dirname(_HERE)
LOG_DIR    = os.path.join(_BUILD_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if ARCHIVIST_LOG:
    os.makedirs(os.path.dirname(os.path.abspath(ARCHIVIST_LOG)), exist_ok=True)
    _log_handlers.append(logging.FileHandler(ARCHIVIST_LOG))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARCHIVIST] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger("archivist")

# ── Shutdown flag ─────────────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum: int, frame) -> None:
    global _shutdown
    log.info("Signal %s received — shutting down after current operation.", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    """Thread-safe SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Archive helpers ───────────────────────────────────────────────────────────
def _archive_path(table: str, date_str: str) -> Path:
    """Return the gzipped archive path for a given table and YYYY-MM-DD date string."""
    month_dir = ARCHIVE_DIR / date_str[:7]   # YYYY-MM
    month_dir.mkdir(parents=True, exist_ok=True)
    return month_dir / f"{table}_{date_str}.json.gz"


def _read_archive(path: Path) -> list:
    """Read an existing gzipped JSON archive. Returns empty list if missing."""
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("Could not read existing archive %s: %s — starting fresh.", path.name, exc)
        return []


def _write_archive(path: Path, rows: list) -> None:
    """Write rows to a gzipped JSON archive atomically."""
    tmp = path.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh, default=str)
    tmp.replace(path)
    log.debug("Wrote %d rows → %s", len(rows), path.name)


def _load_index() -> dict:
    """Load the archive index (manifest). Returns empty structure if missing."""
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            log.warning("Could not read index.json: %s — rebuilding.", exc)
    return {"runs": [], "files": []}


def _save_index(index: dict) -> None:
    """Write the archive index atomically."""
    tmp = INDEX_PATH.with_suffix(".tmp.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, default=str)
    tmp.replace(INDEX_PATH)


def _register_in_index(index: dict, table: str, path: Path, count: int, date_str: str) -> None:
    """Add or update an entry in the index for a given archive file."""
    rel = str(path.relative_to(ARCHIVE_DIR))
    # Update existing entry if present
    for entry in index.get("files", []):
        if entry.get("file") == rel:
            entry["rows"] = entry.get("rows", 0) + count
            entry["last_updated"] = datetime.now(timezone.utc).isoformat()
            return
    # New entry
    index.setdefault("files", []).append({
        "file":         rel,
        "table":        table,
        "date":         date_str,
        "rows":         count,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    })


# ── Archive routines ──────────────────────────────────────────────────────────
def _archive_scoop_queue(cutoff_iso: str) -> int:
    """
    Archive sent/skipped/failed scoop_queue rows older than cutoff_iso.
    Rows are grouped by their queued_at date and written to dated files.
    Returns total rows archived.
    """
    total = 0
    with _db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM scoop_queue
               WHERE status IN ('sent','skipped','failed')
               AND queued_at < ?
               ORDER BY queued_at""",
            (cutoff_iso,),
        ).fetchall()

        if not rows:
            log.info("scoop_queue: no rows to archive (cutoff=%s)", cutoff_iso[:10])
            return 0

        # Group by date
        by_date: dict[str, list] = {}
        for row in rows:
            date_str = (row["queued_at"] or "unknown")[:10]
            by_date.setdefault(date_str, []).append(dict(row))

        index = _load_index()

        for date_str, date_rows in sorted(by_date.items()):
            path     = _archive_path("scoop_queue", date_str)
            existing = _read_archive(path)
            merged   = existing + date_rows
            _write_archive(path, merged)
            _register_in_index(index, "scoop_queue", path, len(date_rows), date_str)
            log.info("scoop_queue: archived %d rows → %s", len(date_rows), path.name)
            total += len(date_rows)

        # Delete archived rows
        ids = [r["id"] for r in rows]
        conn.execute(
            f"DELETE FROM scoop_queue WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        log.info("scoop_queue: deleted %d rows from live DB.", total)

        # Record run in index
        index.setdefault("runs", []).append({
            "table":     "scoop_queue",
            "ran_at":    datetime.now(timezone.utc).isoformat(),
            "cutoff":    cutoff_iso,
            "archived":  total,
        })
        _save_index(index)

    return total


def _archive_pi_events(cutoff_iso: str) -> int:
    """
    Archive pi_events rows older than cutoff_iso.
    Returns total rows archived.
    """
    total = 0
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pi_events WHERE recorded_at < ? ORDER BY recorded_at",
            (cutoff_iso,),
        ).fetchall()

        if not rows:
            log.info("pi_events: no rows to archive (cutoff=%s)", cutoff_iso[:10])
            return 0

        # Group by date
        by_date: dict[str, list] = {}
        for row in rows:
            date_str = (row["recorded_at"] or "unknown")[:10]
            by_date.setdefault(date_str, []).append(dict(row))

        index = _load_index()

        for date_str, date_rows in sorted(by_date.items()):
            path     = _archive_path("pi_events", date_str)
            existing = _read_archive(path)
            merged   = existing + date_rows
            _write_archive(path, merged)
            _register_in_index(index, "pi_events", path, len(date_rows), date_str)
            log.info("pi_events: archived %d rows → %s", len(date_rows), path.name)
            total += len(date_rows)

        # Delete archived rows
        ids = [r["id"] for r in rows]
        conn.execute(
            f"DELETE FROM pi_events WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        log.info("pi_events: deleted %d rows from live DB.", total)

        # Record run in index
        index.setdefault("runs", []).append({
            "table":    "pi_events",
            "ran_at":   datetime.now(timezone.utc).isoformat(),
            "cutoff":   cutoff_iso,
            "archived": total,
        })
        _save_index(index)

    return total


# ── Main archive run ──────────────────────────────────────────────────────────
def run_archive() -> dict:
    """
    Execute one full archive run.
    Returns a summary dict with row counts and any errors encountered.
    """
    now = datetime.now(timezone.utc)
    log.info("Archive run starting at %s", now.isoformat())

    scoop_cutoff = (now - timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat()
    events_cutoff = (now - timedelta(days=PI_EVENTS_ARCHIVE_DAYS)).isoformat()

    summary = {
        "ran_at":         now.isoformat(),
        "scoop_archived": 0,
        "events_archived": 0,
        "errors":         [],
    }

    if not os.path.exists(DB_PATH):
        msg = f"Database not found: {DB_PATH}"
        log.error(msg)
        summary["errors"].append(msg)
        return summary

    try:
        summary["scoop_archived"] = _archive_scoop_queue(scoop_cutoff)
    except Exception as exc:
        msg = f"scoop_queue archive failed: {exc}"
        log.error(msg, exc_info=True)
        summary["errors"].append(msg)

    try:
        summary["events_archived"] = _archive_pi_events(events_cutoff)
    except Exception as exc:
        msg = f"pi_events archive failed: {exc}"
        log.error(msg, exc_info=True)
        summary["errors"].append(msg)

    log.info(
        "Archive run complete — scoop=%d events=%d errors=%d",
        summary["scoop_archived"],
        summary["events_archived"],
        len(summary["errors"]),
    )
    return summary


# ── Daemon scheduler ──────────────────────────────────────────────────────────
def _seconds_until_next_run() -> int:
    """Return seconds until the next ARCHIVIST_RUN_HOUR:00 UTC."""
    now  = datetime.now(timezone.utc)
    next_run = now.replace(hour=ARCHIVIST_RUN_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return int((next_run - now).total_seconds())


def run_daemon() -> None:
    """Run in daemon mode — perform one archive run per night at ARCHIVIST_RUN_HOUR."""
    log.info(
        "Archivist daemon starting — run_hour=%02d:00 UTC  "
        "scoop_retain=%dd  events_retain=%dd",
        ARCHIVIST_RUN_HOUR,
        ARCHIVE_AFTER_DAYS,
        PI_EVENTS_ARCHIVE_DAYS,
    )

    while not _shutdown:
        wait_s = _seconds_until_next_run()
        log.info(
            "Next archive run in %.1f hours (at %02d:00 UTC).",
            wait_s / 3600,
            ARCHIVIST_RUN_HOUR,
        )

        # Sleep until run time in 1-second ticks for prompt SIGTERM response
        for _ in range(wait_s):
            if _shutdown:
                break
            time.sleep(1)

        if _shutdown:
            break

        try:
            run_archive()
        except Exception as exc:
            log.error("Unexpected error during archive run: %s", exc, exc_info=True)

    log.info("Archivist stopped.")


# ── Status report ─────────────────────────────────────────────────────────────
def print_status() -> None:
    """Print a summary of the archive index to stdout and exit."""
    index = _load_index()
    runs  = index.get("runs", [])
    files = index.get("files", [])

    print(f"\n[Archivist] Archive directory: {ARCHIVE_DIR}")
    print(f"[Archivist] Index: {INDEX_PATH}")
    print(f"[Archivist] Total archive files: {len(files)}")
    print(f"[Archivist] Total recorded runs:  {len(runs)}")

    if runs:
        last = runs[-1]
        print(f"\nLast run: {last.get('ran_at', '?')}")
        print(f"  table={last.get('table','?')}  archived={last.get('archived',0)}")

    if files:
        total_rows = sum(f.get("rows", 0) for f in files)
        print(f"\nTotal archived rows across all files: {total_rows}")
        print("\nRecent archive files:")
        for entry in sorted(files, key=lambda x: x.get("last_updated", ""), reverse=True)[:10]:
            print(f"  {entry['file']}  rows={entry.get('rows',0)}  updated={entry.get('last_updated','?')[:19]}")
    else:
        print("\nNo archive files recorded yet.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Synthos Company Archivist")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run one archive pass immediately and exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print archive index summary and exit.",
    )
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.run_now:
        log.info("--run-now: executing single archive pass.")
        summary = run_archive()
        log.info("--run-now complete: %s", json.dumps(summary))
        return

    run_daemon()


if __name__ == "__main__":
    main()
