"""
timekeeper.py — Timekeeper > Scheduler Agent
Synthos Company Pi | /home/pi/synthos-company/agents/timekeeper.py

Role:
  Resource coordinator for the Company Pi. Prevents database contention
  by serializing write access to company.db via a request/grant model.

  Agents submit work requests to the work_requests table.
  Timekeeper grants or queues based on time of day, priority, and load.
  Agents poll their request status before touching the database.

Protocol:
  1. Agent inserts row into work_requests with status=PENDING
  2. Timekeeper reads PENDING requests, evaluates, sets status=GRANTED or QUEUED
  3. Agent polls until GRANTED, then begins work
  4. Agent updates status=COMPLETE when done (or FAILED)
  5. Timekeeper force-releases any slot held >2 minutes (timeout guard)

Priority matrix (1=highest, 10=lowest):
  1  CRITICAL    Emergency — Watchdog alerts, CRITICAL suggestions
  2  HIGH        Interface Agent (heartbeats), Mail Agent (customer-facing)
  3  MEDIUM_HIGH Vault (key operations)
  4  MEDIUM      Blueprint nightly run, Patches deep scan
  5  MEDIUM_LOW  Patches light scan, Sentinel checks
  6  LOW         Librarian audit, Fidget reporting
  7  BACKGROUND  Blueprint weekend hot-fix, any deferred work

Market hours (9:30am–4pm ET):
  Priority 1-3 agents run freely
  Priority 4-6 agents are queued, max 1 concurrent
  Priority 7 agents are deferred to off-hours

Off-hours (4pm–9:30am ET):
  All agents may run
  Blueprint and Patches get extended slots
  Interface Agent remains protected (late heartbeats still arrive)

Emergency override:
  Priority 1 requests jump the queue immediately
  Cannot be deferred more than 15 minutes regardless of load

Runs: Continuously as a background process
  @reboot sleep 120 && python3 /home/pi/synthos-company/agents/timekeeper.py &

USAGE:
  python3 timekeeper.py            # start scheduling loop
  python3 timekeeper.py --status   # show current queue and granted slots
  python3 timekeeper.py --history  # show recent grant/queue decisions
  python3 timekeeper.py --release <agent_name>  # force-release a stuck slot
"""

import os
import sys
import time
import json
import signal
import logging
import sqlite3
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Paths resolved dynamically — works for any username or install directory.
# Override with SYNTHOS_BASE_DIR env var if needed.

import sys as _sys
import os.path as _osp
_AGENTS_DIR = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in _sys.path:
    _sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import (
    BASE_DIR, DATA_DIR, LOGS_DIR, CONFIG_DIR, DB_PATH, ENV_PATH,
)

LOG_FILE    = LOGS_DIR   / "scheduler.log"
POLICY_FILE = CONFIG_DIR / "agent_policies.json"

SYNTHOS_VERSION = "1.0"
ET = ZoneInfo("America/New_York")

# Scheduler loop interval — how often Timekeeper evaluates the queue
LOOP_INTERVAL_SEC = 15

# Max time any agent may hold a granted slot before force-release
MAX_SLOT_DURATION_SEC = 120   # 2 minutes

# Emergency requests cannot be deferred longer than this
EMERGENCY_DEFER_MAX_SEC = 900  # 15 minutes

# Max concurrent granted slots (SQLite: 1 writer, N readers)
MAX_CONCURRENT_WRITERS = 1
MAX_CONCURRENT_READERS = 5

# Market hours (ET)
MARKET_OPEN  = (9, 30)   # 9:30am
MARKET_CLOSE = (16, 0)   # 4:00pm

# Priority thresholds
PRIORITY_EMERGENCY    = 1
PRIORITY_HIGH         = 2
PRIORITY_MEDIUM_HIGH  = 3
PRIORITY_MEDIUM       = 4
PRIORITY_MEDIUM_LOW   = 5
PRIORITY_LOW          = 6
PRIORITY_BACKGROUND   = 7

# Agents deferred during market hours unless priority <= this threshold
MARKET_HOURS_PRIORITY_CUTOFF = 3

# Known agents and their default priorities
DEFAULT_AGENT_PRIORITIES = {
    "Watchdog":   PRIORITY_EMERGENCY,
    "Sentinel":   PRIORITY_HIGH,
    "Scoop":      PRIORITY_HIGH,
    "Vault":      PRIORITY_MEDIUM_HIGH,
    "Blueprint":  PRIORITY_MEDIUM,
    "Patches":    PRIORITY_MEDIUM,
    "Librarian":  PRIORITY_LOW,
    "Fidget":     PRIORITY_LOW,
    "Timekeeper": PRIORITY_MEDIUM_HIGH,   # for its own maintenance ops
}

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s timekeeper: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("timekeeper")


# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Open a connection to company.db.
    Uses WAL mode for better concurrent read performance.
    Timeout of 10s — if we can't get a connection, something is seriously wrong.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema() -> None:
    """Create work_requests table if it doesn't exist."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_requests (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name       TEXT NOT NULL,
                request_time     DATETIME NOT NULL,
                duration_requested INTEGER NOT NULL,
                priority         INTEGER NOT NULL,
                task_type        TEXT NOT NULL,
                access_type      TEXT NOT NULL DEFAULT 'WRITE',
                status           TEXT NOT NULL DEFAULT 'PENDING',
                scheduled_start  DATETIME,
                actual_start     DATETIME,
                actual_end       DATETIME,
                grant_expires_at DATETIME,
                notes            TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_requests_status
            ON work_requests(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_requests_agent
            ON work_requests(agent_name, status)
        """)
        conn.commit()
    log.info("Schema verified")


# ── TIME UTILITIES ────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(ET)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def is_market_hours() -> bool:
    """True if current ET time is within market hours (9:30am–4:00pm Mon–Fri)."""
    now  = now_et()
    if now.weekday() >= 5:   # Saturday/Sunday
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def is_build_window() -> bool:
    """True if current ET time is in Blueprint's Mon–Thu 8pm–midnight build window."""
    now = now_et()
    if now.weekday() >= 4:   # Friday, Saturday, Sunday
        return False
    return 20 <= now.hour < 24


def is_friday_push_window() -> bool:
    """True on Friday after market close — push day."""
    now = now_et()
    return now.weekday() == 4 and now.hour >= 16


# ── PRIORITY LOGIC ────────────────────────────────────────────────────────────

def effective_priority(request: sqlite3.Row) -> int:
    """
    Adjust priority based on context.
    Emergency requests gain urgency over time if deferred too long.
    """
    priority = request["priority"]

    # Emergency escalation: priority 1 requests that have waited >10 min
    # get flagged — Timekeeper will log a warning
    if priority <= PRIORITY_EMERGENCY:
        request_time = datetime.fromisoformat(request["request_time"])
        wait_sec = (now_utc() - request_time.replace(tzinfo=timezone.utc)).total_seconds()
        if wait_sec > EMERGENCY_DEFER_MAX_SEC:
            log.warning(
                f"EMERGENCY request from {request['agent_name']} has waited "
                f"{wait_sec:.0f}s — exceeds {EMERGENCY_DEFER_MAX_SEC}s limit. "
                f"Granting immediately regardless of load."
            )
            return 0   # Effectively highest possible priority

    return priority


def should_defer_for_market_hours(request: sqlite3.Row) -> bool:
    """
    During market hours, defer agents with priority > cutoff.
    Returns True if the request should be queued for off-hours.
    """
    if not is_market_hours():
        return False
    if request["priority"] <= MARKET_HOURS_PRIORITY_CUTOFF:
        return False
    # Exception: emergency override ignores market hours
    if request["priority"] <= PRIORITY_EMERGENCY:
        return False
    return True


def is_friday_blackout(request: sqlite3.Row) -> bool:
    """
    On Friday after market close, Blueprint does not run new work
    (push day is for review, not new implementation).
    Exceptions: emergency requests and Watchdog alerts always go through.
    """
    if not is_friday_push_window():
        return False
    if request["agent_name"] in ("Watchdog", "Sentinel", "Scoop"):
        return False
    if request["priority"] <= PRIORITY_EMERGENCY:
        return False
    if request["agent_name"] == "Blueprint":
        log.info(
            f"Blueprint request deferred — Friday push window. "
            f"New Blueprint work resumes Monday."
        )
        return True
    return False


# ── SLOT MANAGEMENT ──────────────────────────────────────────────────────────

def get_active_slots(conn: sqlite3.Connection) -> list:
    """Return currently GRANTED or EXECUTING slots."""
    return conn.execute("""
        SELECT * FROM work_requests
        WHERE status IN ('GRANTED', 'EXECUTING')
        ORDER BY actual_start ASC
    """).fetchall()


def get_pending_requests(conn: sqlite3.Connection) -> list:
    """Return PENDING requests ordered by priority then request_time."""
    return conn.execute("""
        SELECT * FROM work_requests
        WHERE status = 'PENDING'
        ORDER BY priority ASC, request_time ASC
    """).fetchall()


def count_active_writers(conn: sqlite3.Connection) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM work_requests
        WHERE status IN ('GRANTED', 'EXECUTING')
        AND access_type = 'WRITE'
    """).fetchone()[0]


def count_active_readers(conn: sqlite3.Connection) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM work_requests
        WHERE status IN ('GRANTED', 'EXECUTING')
        AND access_type = 'READ'
    """).fetchone()[0]


def grant_slot(conn: sqlite3.Connection, request_id: int,
               duration_sec: int) -> None:
    """Grant a work slot — update status to GRANTED with expiry."""
    now     = now_iso()
    expires = (now_utc() + timedelta(seconds=duration_sec)).isoformat().replace("+00:00", "Z")
    conn.execute("""
        UPDATE work_requests
        SET status='GRANTED', actual_start=?, grant_expires_at=?
        WHERE id=?
    """, (now, expires, request_id))
    conn.commit()


def queue_slot(conn: sqlite3.Connection, request_id: int,
               reason: str, estimated_start: datetime = None) -> None:
    """Mark a request as QUEUED with a reason note."""
    eta_str = estimated_start.isoformat() if estimated_start else None
    conn.execute("""
        UPDATE work_requests
        SET status='QUEUED', scheduled_start=?, notes=?
        WHERE id=?
    """, (eta_str, reason, request_id))
    conn.commit()


def force_release_expired(conn: sqlite3.Connection) -> int:
    """
    Force-release any slots that have exceeded their grant duration.
    Returns count of slots released.
    Returns expired slots as TIMED_OUT so they're visible in history.
    """
    now = now_iso()
    expired = conn.execute("""
        SELECT id, agent_name, task_type, actual_start, grant_expires_at
        FROM work_requests
        WHERE status IN ('GRANTED', 'EXECUTING')
        AND grant_expires_at IS NOT NULL
        AND grant_expires_at < ?
    """, (now,)).fetchall()

    for slot in expired:
        log.warning(
            f"Force-releasing expired slot: {slot['agent_name']} "
            f"({slot['task_type']}) — exceeded grant window. "
            f"Started: {slot['actual_start']}, Expired: {slot['grant_expires_at']}"
        )
        conn.execute("""
            UPDATE work_requests
            SET status='TIMED_OUT', actual_end=?, notes='Force-released by Timekeeper — exceeded slot duration'
            WHERE id=?
        """, (now, slot["id"]))

    if expired:
        conn.commit()

    return len(expired)


# ── DECISION ENGINE ───────────────────────────────────────────────────────────

def evaluate_request(conn: sqlite3.Connection, request: sqlite3.Row) -> str:
    """
    Core decision: GRANT or QUEUE a pending request.

    Returns 'GRANTED' or reason string for queuing.
    """
    agent    = request["agent_name"]
    priority = effective_priority(request)
    atype    = request["access_type"]

    # Friday blackout check
    if is_friday_blackout(request):
        return f"Friday push window — Blueprint deferred to Monday"

    # Market hours deferral
    if should_defer_for_market_hours(request):
        return f"Market hours — {agent} (priority {priority}) deferred to off-hours"

    # Check concurrent slot limits
    active_writers = count_active_writers(conn)
    active_readers = count_active_readers(conn)

    if atype == "WRITE":
        if active_writers >= MAX_CONCURRENT_WRITERS:
            # Emergency can displace lower-priority writer
            if priority <= PRIORITY_EMERGENCY:
                # Find the active writer
                active = conn.execute("""
                    SELECT id, agent_name, priority FROM work_requests
                    WHERE status IN ('GRANTED', 'EXECUTING') AND access_type='WRITE'
                    ORDER BY priority DESC LIMIT 1
                """).fetchone()
                if active and active["priority"] > priority:
                    log.warning(
                        f"EMERGENCY override: {agent} displacing "
                        f"{active['agent_name']} (lower priority)"
                    )
                    conn.execute("""
                        UPDATE work_requests SET status='PREEMPTED',
                        actual_end=?, notes='Preempted by emergency request'
                        WHERE id=?
                    """, (now_iso(), active["id"]))
                    conn.commit()
                    return "GRANTED"
            return f"Writer slot occupied by another agent — queued"

    elif atype == "READ":
        if active_writers >= MAX_CONCURRENT_WRITERS:
            # Readers block behind active writers too (WAL helps but we're conservative)
            if priority <= PRIORITY_HIGH:
                # High priority readers can run alongside writers in WAL mode
                return "GRANTED"
            return f"Write in progress — reader queued"
        if active_readers >= MAX_CONCURRENT_READERS:
            return f"Max concurrent readers reached — queued"

    return "GRANTED"


def run_scheduling_cycle(conn: sqlite3.Connection) -> dict:
    """
    One full scheduling cycle:
    1. Force-release expired slots
    2. Evaluate all pending requests
    3. Grant or queue each

    Returns summary dict for logging.
    """
    released = force_release_expired(conn)
    pending  = get_pending_requests(conn)

    granted = 0
    queued  = 0

    for request in pending:
        decision = evaluate_request(conn, request)

        if decision == "GRANTED":
            grant_slot(conn, request["id"], request["duration_requested"])
            granted += 1
            log.info(
                f"GRANTED: {request['agent_name']} | "
                f"{request['task_type']} | "
                f"{request['access_type']} | "
                f"priority={request['priority']} | "
                f"duration={request['duration_requested']}s"
            )
        else:
            # Estimate when this might be granted
            # Simple heuristic: sum of active + higher-priority queued slots
            est_wait = estimate_wait_seconds(conn, request)
            est_start = now_utc() + timedelta(seconds=est_wait)
            queue_slot(conn, request["id"], decision, est_start)
            queued += 1
            log.info(
                f"QUEUED: {request['agent_name']} | "
                f"{request['task_type']} | "
                f"reason={decision} | "
                f"est_wait={est_wait}s"
            )

    return {
        "cycle_time":    now_iso(),
        "released":      released,
        "pending_found": len(pending),
        "granted":       granted,
        "queued":        queued,
    }


def estimate_wait_seconds(conn: sqlite3.Connection, request: sqlite3.Row) -> int:
    """
    Rough estimate of how long this request will wait.
    Based on sum of remaining time in active slots + higher-priority queue.
    """
    total = 0

    # Remaining time in active slots
    active = conn.execute("""
        SELECT grant_expires_at FROM work_requests
        WHERE status IN ('GRANTED', 'EXECUTING')
        AND access_type = ?
    """, (request["access_type"],)).fetchall()

    for slot in active:
        if slot["grant_expires_at"]:
            try:
                expires = datetime.fromisoformat(slot["grant_expires_at"])
                remaining = (expires.replace(tzinfo=timezone.utc) - now_utc()).total_seconds()
                total += max(0, remaining)
            except Exception:
                total += 60   # assume 60s if parse fails

    # Higher-priority requests ahead in queue
    higher = conn.execute("""
        SELECT duration_requested FROM work_requests
        WHERE status = 'QUEUED'
        AND priority < ?
        AND access_type = ?
    """, (request["priority"], request["access_type"])).fetchall()

    for req in higher:
        total += req["duration_requested"]

    return int(total) or 30   # floor of 30s


# ── RE-QUEUE STALE REQUESTS ───────────────────────────────────────────────────

def requeue_stale(conn: sqlite3.Connection) -> None:
    """
    QUEUED requests that have passed their estimated start time
    are moved back to PENDING so Timekeeper re-evaluates them.
    Prevents requests from sitting in QUEUED forever.
    """
    now = now_iso()
    stale = conn.execute("""
        SELECT id, agent_name, task_type FROM work_requests
        WHERE status = 'QUEUED'
        AND scheduled_start IS NOT NULL
        AND scheduled_start < ?
    """, (now,)).fetchall()

    for req in stale:
        conn.execute("""
            UPDATE work_requests SET status='PENDING', scheduled_start=NULL, notes=NULL
            WHERE id=?
        """, (req["id"],))
        log.debug(f"Re-queued stale request: {req['agent_name']} / {req['task_type']}")

    if stale:
        conn.commit()


# ── REPORTING ─────────────────────────────────────────────────────────────────

def get_queue_depth(conn: sqlite3.Connection) -> dict:
    """Summary of current queue state for portal and morning report."""
    row = conn.execute("""
        SELECT
            SUM(CASE WHEN status='PENDING'   THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='QUEUED'    THEN 1 ELSE 0 END) as queued,
            SUM(CASE WHEN status='GRANTED'   THEN 1 ELSE 0 END) as granted,
            SUM(CASE WHEN status='EXECUTING' THEN 1 ELSE 0 END) as executing,
            SUM(CASE WHEN status='TIMED_OUT' AND
                     request_time > datetime('now', '-1 hour') THEN 1 ELSE 0 END) as timed_out_1h
        FROM work_requests
    """).fetchone()
    return dict(row)


def write_status_json() -> None:
    """
    Write current queue state to a JSON file for the command portal.
    Called at the end of each scheduling cycle.
    """
    status_path = BASE_DIR / "data" / "timekeeper_status.json"
    try:
        with get_db() as conn:
            depth   = get_queue_depth(conn)
            active  = [dict(r) for r in get_active_slots(conn)]
            pending = [dict(r) for r in get_pending_requests(conn)]

        status = {
            "timestamp":     now_iso(),
            "market_hours":  is_market_hours(),
            "build_window":  is_build_window(),
            "friday_push":   is_friday_push_window(),
            "queue_depth":   depth,
            "active_slots":  active[:10],    # cap for file size
            "pending_queue": pending[:20],
        }
        status_path.write_text(json.dumps(status, indent=2))
    except Exception as e:
        log.warning(f"Could not write status JSON: {e}")


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run_loop() -> None:
    """
    Main Timekeeper loop.
    Runs every LOOP_INTERVAL_SEC seconds, evaluating the work_requests queue.
    """
    log.info(
        f"Timekeeper v{SYNTHOS_VERSION} started — "
        f"loop interval {LOOP_INTERVAL_SEC}s | "
        f"max slot {MAX_SLOT_DURATION_SEC}s | "
        f"market hours: {'yes' if is_market_hours() else 'no'}"
    )

    cycle_count    = 0
    last_report_at = now_utc()

    while True:
        try:
            with get_db() as conn:
                requeue_stale(conn)
                summary = run_scheduling_cycle(conn)

            cycle_count += 1

            # Write status JSON every 5 cycles (~75s)
            if cycle_count % 5 == 0:
                write_status_json()

            # Hourly summary log
            if (now_utc() - last_report_at).total_seconds() >= 3600:
                with get_db() as conn:
                    depth = get_queue_depth(conn)
                log.info(
                    f"Hourly summary — "
                    f"pending={depth['pending']} queued={depth['queued']} "
                    f"granted={depth['granted']} "
                    f"timed_out_1h={depth['timed_out_1h']} | "
                    f"market_hours={is_market_hours()}"
                )
                last_report_at = now_utc()

        except sqlite3.OperationalError as e:
            log.error(f"Database error in scheduling cycle: {e}")
        except Exception as e:
            log.error(f"Scheduling loop error: {e}", exc_info=True)

        time.sleep(LOOP_INTERVAL_SEC)


# ── CLI ───────────────────────────────────────────────────────────────────────

def show_status() -> None:
    """Display current queue state in the terminal."""
    try:
        with get_db() as conn:
            depth   = get_queue_depth(conn)
            active  = get_active_slots(conn)
            pending = get_pending_requests(conn)

        print(f"\n{'=' * 64}")
        print(f"TIMEKEEPER STATUS — v{SYNTHOS_VERSION}")
        print(f"Time:         {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
        print(f"Market hours: {'YES' if is_market_hours() else 'NO'}")
        print(f"Build window: {'YES' if is_build_window() else 'NO'}")
        print(f"Friday push:  {'YES' if is_friday_push_window() else 'NO'}")
        print(f"{'=' * 64}")
        print(f"Queue:  {depth['pending']} pending | {depth['queued']} queued | "
              f"{depth['granted']} granted | {depth['timed_out_1h']} timed out (1h)")
        print(f"{'=' * 64}")

        if active:
            print("ACTIVE SLOTS:")
            for slot in active:
                print(
                    f"  ● {slot['agent_name']:15} {slot['task_type']:20} "
                    f"{slot['access_type']:5} expires={slot['grant_expires_at'] or 'unknown'}"
                )

        if pending:
            print("PENDING:")
            for req in pending[:10]:
                print(
                    f"  · {req['agent_name']:15} {req['task_type']:20} "
                    f"p={req['priority']} {req['access_type']}"
                )

        print(f"{'=' * 64}\n")

    except Exception as e:
        print(f"Error reading status: {e}")


def show_history(hours: int = 4) -> None:
    """Display recent scheduling decisions."""
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT agent_name, task_type, access_type, priority,
                       status, request_time, actual_start, actual_end,
                       duration_requested, notes
                FROM work_requests
                WHERE request_time > datetime('now', ?)
                ORDER BY request_time DESC
                LIMIT 50
            """, (f"-{hours} hours",)).fetchall()

        print(f"\n{'=' * 64}")
        print(f"TIMEKEEPER HISTORY — last {hours}h")
        print(f"{'=' * 64}")
        for row in rows:
            icon = {
                "GRANTED":   "✓",
                "EXECUTING": "▶",
                "COMPLETE":  "●",
                "QUEUED":    "·",
                "TIMED_OUT": "⚠",
                "PREEMPTED": "↑",
                "FAILED":    "✗",
                "PENDING":   "○",
            }.get(row["status"], "?")
            print(
                f"  {icon} {row['agent_name']:15} {row['task_type']:20} "
                f"{row['status']:10} p={row['priority']} {row['access_type']}"
            )
            if row["notes"]:
                print(f"    └ {row['notes'][:80]}")
        print(f"{'=' * 64}\n")

    except Exception as e:
        print(f"Error reading history: {e}")


def force_release(agent_name: str) -> None:
    """Manually force-release a stuck slot for a named agent."""
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT id, task_type FROM work_requests
                WHERE agent_name=? AND status IN ('GRANTED', 'EXECUTING')
            """, (agent_name,)).fetchall()

            if not rows:
                print(f"No active slots found for {agent_name}")
                return

            for row in rows:
                conn.execute("""
                    UPDATE work_requests
                    SET status='COMPLETE', actual_end=?,
                    notes='Manually released via --release flag'
                    WHERE id=?
                """, (now_iso(), row["id"]))
                print(f"Released: {agent_name} / {row['task_type']}")

            conn.commit()

    except Exception as e:
        print(f"Error releasing slot: {e}")


# ── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

def handle_shutdown(signum, frame):
    log.info(f"Timekeeper received signal {signum} — shutting down cleanly")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)


# ── CLIENT HELPER (for other agents to import) ────────────────────────────────

class TimekeeperClient:
    """
    Lightweight client for other agents to request and release work slots.

    Usage:
        from agents.timekeeper import TimekeeperClient

        tk = TimekeeperClient(agent_name="Blueprint", task_type="nightly_run")
        with tk.slot(priority=4, duration_sec=7200, access_type="WRITE"):
            # do your database work here
            pass   # slot auto-released on exit
    """

    def __init__(self, agent_name: str, task_type: str):
        self.agent_name = agent_name
        self.task_type  = task_type
        self.request_id = None

    def request_slot(
        self,
        priority: int = 5,
        duration_sec: int = 300,
        access_type: str = "WRITE",
        poll_interval: int = 5,
        timeout_sec: int = 1800,   # 30 min max wait
    ) -> bool:
        """
        Submit a work request and wait until GRANTED.
        Returns True when granted, False if timed out.
        """
        # Submit request
        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO work_requests
                (agent_name, request_time, duration_requested, priority,
                 task_type, access_type, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                self.agent_name, now_iso(), duration_sec,
                priority, self.task_type, access_type
            ))
            self.request_id = cursor.lastrowid
            conn.commit()

        log.info(
            f"[{self.agent_name}] Work request submitted: "
            f"id={self.request_id} priority={priority} "
            f"duration={duration_sec}s {access_type}"
        )

        # Poll until GRANTED or timeout
        start    = now_utc()
        waited   = 0

        while waited < timeout_sec:
            time.sleep(poll_interval)
            waited += poll_interval

            with get_db() as conn:
                row = conn.execute("""
                    SELECT status, notes FROM work_requests WHERE id=?
                """, (self.request_id,)).fetchone()

            if not row:
                log.error(f"[{self.agent_name}] Request {self.request_id} not found — aborting")
                return False

            status = row["status"]

            if status == "GRANTED":
                # Mark as EXECUTING
                with get_db() as conn:
                    conn.execute("""
                        UPDATE work_requests SET status='EXECUTING' WHERE id=?
                    """, (self.request_id,))
                    conn.commit()
                log.info(f"[{self.agent_name}] Slot GRANTED after {waited}s wait")
                return True

            if status in ("TIMED_OUT", "PREEMPTED", "FAILED"):
                log.warning(f"[{self.agent_name}] Request ended with status {status}")
                return False

            # Still PENDING or QUEUED — keep waiting
            if waited % 60 == 0:
                log.info(
                    f"[{self.agent_name}] Still waiting for slot "
                    f"({waited}s / {timeout_sec}s) — status={status}"
                )

        log.warning(
            f"[{self.agent_name}] Timed out waiting for slot after {timeout_sec}s"
        )
        return False

    def release_slot(self) -> None:
        """Mark the work slot as COMPLETE."""
        if not self.request_id:
            return
        try:
            with get_db() as conn:
                conn.execute("""
                    UPDATE work_requests
                    SET status='COMPLETE', actual_end=?
                    WHERE id=?
                """, (now_iso(), self.request_id))
                conn.commit()
            log.info(f"[{self.agent_name}] Slot released (id={self.request_id})")
            self.request_id = None
        except Exception as e:
            log.warning(f"[{self.agent_name}] Error releasing slot: {e}")

    class _SlotContext:
        def __init__(self, client, **kwargs):
            self.client = client
            self.kwargs = kwargs
            self.granted = False

        def __enter__(self):
            self.granted = self.client.request_slot(**self.kwargs)
            if not self.granted:
                raise RuntimeError(
                    f"{self.client.agent_name} could not acquire work slot — "
                    f"Timekeeper did not grant within timeout"
                )
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.client.release_slot()
            return False   # do not suppress exceptions

    def slot(self, **kwargs) -> "_SlotContext":
        """Context manager: request slot, do work, release on exit."""
        return self._SlotContext(self, **kwargs)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Timekeeper — Synthos resource scheduler"
    )
    parser.add_argument("--status",          action="store_true",
                        help="Show current queue and active slots")
    parser.add_argument("--history",         action="store_true",
                        help="Show recent scheduling decisions")
    parser.add_argument("--history-hours",   type=int, default=4,
                        help="Hours of history to show (default: 4)")
    parser.add_argument("--release",         type=str, metavar="AGENT_NAME",
                        help="Force-release stuck slot for named agent")
    args = parser.parse_args()

    ensure_schema()

    if args.status:
        show_status()
    elif args.history:
        show_history(hours=args.history_hours)
    elif args.release:
        force_release(args.release)
    else:
        run_loop()
