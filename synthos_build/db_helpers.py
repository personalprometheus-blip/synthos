"""
db_helpers.py — Company Pi Shared Database Utilities
Synthos Company Pi | /home/<user>/synthos-company/utils/db_helpers.py

Single import for all company Pi agents. Provides:
  - Connection management (WAL mode, consistent settings)
  - Schema bootstrap (idempotent, safe to call on every startup)
  - Suggestions table operations (thread-safe inserts, status updates)
  - Scoop queue operations (event posting, delivery tracking)
  - Deploy watches (Blueprint posts, Watchdog/Patches reads)
  - Silence alert dedup (Sentinel uses this)
  - Timekeeper slot request/release (all scheduled agents use this)
  - Audit trail writes

Design principles:
  - Every write goes through this module — no agent opens raw connections
    for write operations without going through here
  - Sentinel exception: heartbeat writes use _direct_write() with a
    5-second timeout — can't block on Timekeeper for HTTP requests
  - All other writes: agents call request_slot() before any DB mutation,
    release_slot() when done
  - Reads are always allowed without a slot (WAL mode supports concurrent
    readers alongside a writer)
  - Deduplication is built into suggestion/scoop inserts — agents don't
    need to check themselves

Usage:
    from utils.db_helpers import DB

    db = DB()                          # connects, bootstraps schema

    # Timekeeper-coordinated write
    with db.slot("Patches", "light_scan", priority=5):
        db.post_suggestion(...)        # safe inside slot

    # Sentinel direct write (heartbeat path only)
    db.heartbeat_write(pi_id, payload)

    # Read (no slot needed)
    suggestions = db.get_pending_suggestions()
"""

import os
import sys
import json
import uuid
import time
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("db_helpers")

# ── PATH RESOLUTION ───────────────────────────────────────────────────────────

_HERE    = Path(__file__).resolve().parent    # utils/
BASE_DIR = _HERE.parent                       # synthos-company/
DB_PATH  = BASE_DIR / "data" / "company.db"
SCHEMA_SQL = _HERE.parent / "config" / "company_schema.sql"

# ── CONNECTION ────────────────────────────────────────────────────────────────

def _connect(timeout: float = 30.0) -> sqlite3.Connection:
    """Open a WAL-mode connection to company.db."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _direct_connect() -> sqlite3.Connection:
    """
    Short-timeout connection for Sentinel heartbeat writes.
    Never blocks more than 5 seconds — heartbeat ACK can't wait.
    """
    return _connect(timeout=5.0)


# ── SCHEMA BOOTSTRAP ──────────────────────────────────────────────────────────

def bootstrap_schema() -> None:
    """
    Create all tables if they don't exist. Safe to call on every startup.
    Reads from company_schema.sql if available, otherwise uses inline DDL.
    """
    conn = _connect()
    try:
        # Try reading from SQL file first
        schema_path = BASE_DIR / "config" / "company_schema.sql"
        if schema_path.exists():
            sql = schema_path.read_text()
            # Execute each statement (split on ; but skip empty)
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "already exists" not in str(e):
                            raise
            conn.commit()
        else:
            _bootstrap_inline(conn)
        log.debug("Schema verified")
    finally:
        conn.close()


def _bootstrap_inline(conn: sqlite3.Connection) -> None:
    """Inline schema bootstrap when SQL file is not available."""
    statements = [
        """CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pi_id TEXT UNIQUE NOT NULL,
            license_key TEXT, customer_name TEXT, email TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat DATETIME, payment_status TEXT NOT NULL DEFAULT 'PAID',
            github_fork_access INTEGER NOT NULL DEFAULT 1,
            mail_alerts_enabled INTEGER NOT NULL DEFAULT 1,
            archived_at DATETIME, notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pi_id TEXT NOT NULL, timestamp DATETIME NOT NULL,
            portfolio_value REAL, agent_statuses TEXT, uptime_seconds INTEGER,
            received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL, pi_id TEXT NOT NULL,
            issued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME, status TEXT NOT NULL DEFAULT 'ACTIVE',
            issued_by TEXT NOT NULL DEFAULT 'Vault', notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS audit_trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            agent TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT, details TEXT, outcome TEXT NOT NULL DEFAULT 'SUCCESS')""",
        """CREATE TABLE IF NOT EXISTS suggestions (
            id TEXT PRIMARY KEY, timestamp DATETIME NOT NULL,
            agent TEXT NOT NULL, category TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL,
            risk_level TEXT NOT NULL DEFAULT 'LOW',
            affected_component TEXT, affected_customers INTEGER,
            tokens_saved_per_week INTEGER, execution_time_saved TEXT,
            estimated_improvement TEXT, effort TEXT, complexity TEXT,
            approver_needed TEXT NOT NULL DEFAULT 'you',
            trial_run_recommended INTEGER NOT NULL DEFAULT 0,
            breaking_changes INTEGER NOT NULL DEFAULT 0,
            rollback_difficulty TEXT NOT NULL DEFAULT 'EASY',
            root_cause TEXT, solution_approach TEXT,
            alternative_approaches TEXT, dependencies TEXT,
            metrics_to_track TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            status_updated_at DATETIME NOT NULL,
            approver_notes TEXT, implementation_status TEXT,
            implementation_notes TEXT,
            has_disagreement INTEGER NOT NULL DEFAULT 0,
            disagreement_details TEXT)""",
        """CREATE TABLE IF NOT EXISTS scoop_queue (
            id TEXT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT NOT NULL,
            audience TEXT NOT NULL DEFAULT 'internal',
            pi_id TEXT, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_attempt DATETIME, sent_at DATETIME, error_msg TEXT)""",
        """CREATE TABLE IF NOT EXISTS deploy_watches (
            id TEXT PRIMARY KEY, suggestion_id TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deployed_at DATETIME NOT NULL,
            watch_duration_hours INTEGER NOT NULL DEFAULT 48,
            expires_at DATETIME NOT NULL,
            affected_files TEXT NOT NULL, watch_for TEXT NOT NULL,
            rollback_trigger TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            triggered_at DATETIME, triggered_by TEXT,
            rollback_executed INTEGER NOT NULL DEFAULT 0, notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS work_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            request_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            duration_requested INTEGER NOT NULL, priority INTEGER NOT NULL,
            task_type TEXT NOT NULL, access_type TEXT NOT NULL DEFAULT 'WRITE',
            status TEXT NOT NULL DEFAULT 'PENDING',
            scheduled_start DATETIME, actual_start DATETIME,
            actual_end DATETIME, grant_expires_at DATETIME, notes TEXT)""",
        """CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pi_id TEXT, agent_name TEXT NOT NULL, api_provider TEXT NOT NULL,
            operation TEXT, token_count INTEGER NOT NULL DEFAULT 0,
            call_count INTEGER NOT NULL DEFAULT 1,
            cost_estimate REAL NOT NULL DEFAULT 0.0,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS token_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            tokens_used INTEGER NOT NULL,
            tokens_input INTEGER NOT NULL DEFAULT 0,
            tokens_output INTEGER NOT NULL DEFAULT 0,
            model TEXT, operation TEXT,
            cost_estimate REAL NOT NULL DEFAULT 0.0,
            month TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS backup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pi_id TEXT NOT NULL,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL, size_kb REAL, remote_path TEXT,
            files_included INTEGER, error_msg TEXT)""",
        """CREATE TABLE IF NOT EXISTS silence_alerts (
            pi_id TEXT PRIMARY KEY,
            last_alerted DATETIME NOT NULL,
            alert_count INTEGER NOT NULL DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            description TEXT)""",
    ]
    for stmt in statements:
        conn.execute(stmt)

    # Indexes
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status)",
        "CREATE INDEX IF NOT EXISTS idx_heartbeats_pi_time ON heartbeats(pi_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)",
        "CREATE INDEX IF NOT EXISTS idx_suggestions_agent ON suggestions(agent, status)",
        "CREATE INDEX IF NOT EXISTS idx_suggestions_risk ON suggestions(risk_level, status)",
        "CREATE INDEX IF NOT EXISTS idx_scoop_status ON scoop_queue(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_scoop_pi ON scoop_queue(pi_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_deploy_watches ON deploy_watches(status, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_work_requests_status ON work_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_api_usage_time ON api_usage(agent_name, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_token_ledger_month ON token_ledger(month, agent_name)",
        "CREATE INDEX IF NOT EXISTS idx_backup_log ON backup_log(pi_id, timestamp)",
    ]
    for idx in indexes:
        conn.execute(idx)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, 'v2.0')"
    )
    conn.commit()


# ── MAIN DB CLASS ─────────────────────────────────────────────────────────────

class DB:
    """
    Company Pi database interface.
    All agents instantiate this class and use its methods.
    Never open raw connections for writes — use the slot context manager.
    """

    def __init__(self):
        bootstrap_schema()

    # ── TIMEKEEPER SLOT ───────────────────────────────────────────────────────

    @contextmanager
    def slot(self, agent_name: str, task_type: str,
             priority: int = 5, duration_sec: int = 300,
             access_type: str = "WRITE"):
        """
        Request a Timekeeper work slot before doing DB writes.
        Releases automatically on exit (including on exception).

        Usage:
            with db.slot("Patches", "light_scan", priority=5):
                db.post_suggestion(...)
        """
        request_id = self._request_slot(agent_name, task_type, priority,
                                        duration_sec, access_type)
        try:
            yield
        finally:
            self._release_slot(request_id)

    def _request_slot(self, agent_name: str, task_type: str,
                      priority: int, duration_sec: int,
                      access_type: str) -> int:
        """
        Insert work request and poll until GRANTED.
        Returns request_id for later release.
        Raises RuntimeError if not granted within 30 minutes.
        """
        now = _now_iso()
        conn = _connect()
        try:
            cursor = conn.execute("""
                INSERT INTO work_requests
                (agent_name, request_time, duration_requested, priority,
                 task_type, access_type, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
            """, (agent_name, now, duration_sec, priority, task_type, access_type))
            request_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        # Poll until GRANTED
        timeout  = 1800   # 30 minutes
        waited   = 0
        interval = 5

        while waited < timeout:
            time.sleep(interval)
            waited += interval
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT status FROM work_requests WHERE id=?", (request_id,)
                ).fetchone()
            finally:
                conn.close()

            if not row:
                raise RuntimeError(f"Work request {request_id} disappeared from DB")

            if row["status"] == "GRANTED":
                conn = _connect()
                try:
                    conn.execute(
                        "UPDATE work_requests SET status='EXECUTING', actual_start=? WHERE id=?",
                        (_now_iso(), request_id)
                    )
                    conn.commit()
                finally:
                    conn.close()
                log.debug(f"[{agent_name}] Slot granted after {waited}s")
                return request_id

            if row["status"] in ("TIMED_OUT", "PREEMPTED", "FAILED"):
                raise RuntimeError(
                    f"[{agent_name}] Work slot ended with status {row['status']}"
                )

            if waited % 60 == 0:
                log.info(f"[{agent_name}] Waiting for slot ({waited}s)...")

        raise RuntimeError(
            f"[{agent_name}] Timed out waiting for Timekeeper slot after {timeout}s"
        )

    def _release_slot(self, request_id: int) -> None:
        """Mark slot as COMPLETE."""
        if request_id is None:
            return
        try:
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE work_requests SET status='COMPLETE', actual_end=? WHERE id=?",
                    (_now_iso(), request_id)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"Could not release slot {request_id}: {e}")

    # ── SUGGESTIONS ───────────────────────────────────────────────────────────

    def post_suggestion(self,
                        agent: str,
                        category: str,
                        title: str,
                        description: str,
                        risk_level: str = "LOW",
                        affected_component: str = None,
                        affected_customers: int = None,
                        effort: str = None,
                        complexity: str = "SIMPLE",
                        approver_needed: str = "you",
                        trial_run_recommended: bool = False,
                        breaking_changes: bool = False,
                        rollback_difficulty: str = "EASY",
                        root_cause: str = None,
                        solution_approach: str = None,
                        tokens_saved_per_week: int = None,
                        estimated_improvement: str = None,
                        metrics_to_track: list = None,
                        dedupe_hours: int = 24) -> str | None:
        """
        Insert a suggestion. Thread-safe — must be called inside a slot.
        Returns suggestion ID on success, None if deduplicated.

        Deduplication: if an identical title prefix from the same agent
        already exists as pending/approved within dedupe_hours, skip.
        """
        title_prefix = title[:60]
        cutoff       = (
            datetime.now(timezone.utc) - timedelta(hours=dedupe_hours)
        ).isoformat().replace("+00:00", "Z")

        conn = _connect()
        try:
            existing = conn.execute("""
                SELECT id FROM suggestions
                WHERE agent=? AND substr(title,1,60)=?
                AND status IN ('pending','approved')
                AND timestamp > ?
            """, (agent, title_prefix, cutoff)).fetchone()

            if existing:
                log.debug(f"Duplicate suggestion skipped: {title_prefix}")
                return None

            sid = str(uuid.uuid4())
            now = _now_iso()

            conn.execute("""
                INSERT INTO suggestions (
                    id, timestamp, agent, category, title, description,
                    risk_level, affected_component, affected_customers,
                    tokens_saved_per_week, estimated_improvement,
                    effort, complexity, approver_needed,
                    trial_run_recommended, breaking_changes, rollback_difficulty,
                    root_cause, solution_approach,
                    metrics_to_track, status, status_updated_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                sid, now, agent, category, title[:80], description,
                risk_level, affected_component, affected_customers,
                tokens_saved_per_week, estimated_improvement,
                effort, complexity, approver_needed,
                1 if trial_run_recommended else 0,
                1 if breaking_changes else 0,
                rollback_difficulty, root_cause, solution_approach,
                json.dumps(metrics_to_track or []),
                "pending", now,
            ))
            conn.commit()
            log.info(f"Suggestion posted [{agent}]: {title[:60]}")
            return sid
        finally:
            conn.close()

    def get_pending_suggestions(self) -> list[dict]:
        """Read-only — no slot needed."""
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT * FROM suggestions
                WHERE status='pending'
                ORDER BY
                    CASE risk_level
                        WHEN 'CRITICAL' THEN 1
                        WHEN 'HIGH'     THEN 2
                        WHEN 'MEDIUM'   THEN 3
                        WHEN 'LOW'      THEN 4
                        ELSE 5
                    END,
                    timestamp ASC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_approved_suggestions(self,
                                  implementation_status: str = None) -> list[dict]:
        """Return approved suggestions, optionally filtered by impl status."""
        conn = _connect()
        try:
            if implementation_status is None:
                rows = conn.execute("""
                    SELECT * FROM suggestions
                    WHERE status='approved'
                    ORDER BY timestamp ASC
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM suggestions
                    WHERE status='approved'
                    AND (implementation_status=? OR
                         (? IS NULL AND implementation_status IS NULL))
                    ORDER BY timestamp ASC
                """, (implementation_status, implementation_status)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_suggestion_status(self, suggestion_id: str,
                                  status: str = None,
                                  implementation_status: str = None,
                                  notes: str = None) -> None:
        """Update suggestion lifecycle fields. Must be inside a slot."""
        conn = _connect()
        try:
            updates = []
            params  = []
            if status:
                updates.append("status=?")
                params.append(status)
            if implementation_status:
                updates.append("implementation_status=?")
                params.append(implementation_status)
            if notes:
                updates.append("implementation_notes=?")
                params.append(notes)
            updates.append("status_updated_at=?")
            params.append(_now_iso())
            params.append(suggestion_id)

            conn.execute(
                f"UPDATE suggestions SET {', '.join(updates)} WHERE id=?",
                params
            )
            conn.commit()
        finally:
            conn.close()

    # ── SCOOP QUEUE ───────────────────────────────────────────────────────────

    def post_scoop_event(self, event_type: str, payload: dict,
                          audience: str = "internal",
                          pi_id: str = None) -> str:
        """
        Queue an event for Scoop to deliver.
        Must be called inside a slot (except from Sentinel heartbeat path).
        Returns event ID.
        """
        eid = str(uuid.uuid4())
        conn = _connect()
        try:
            conn.execute("""
                INSERT INTO scoop_queue
                (id, event_type, audience, pi_id, payload, status)
                VALUES (?,?,?,?,?,'pending')
            """, (eid, event_type, audience, pi_id, json.dumps(payload)))
            conn.commit()
            log.debug(f"Scoop event queued: {event_type} [{audience}]")
            return eid
        finally:
            conn.close()

    def post_scoop_event_direct(self, event_type: str, payload: dict,
                                 audience: str = "internal",
                                 pi_id: str = None) -> str:
        """
        Direct-write scoop event for Sentinel heartbeat path.
        Uses short timeout — never blocks more than 5 seconds.
        """
        eid  = str(uuid.uuid4())
        conn = _direct_connect()
        try:
            conn.execute("""
                INSERT INTO scoop_queue
                (id, event_type, audience, pi_id, payload, status)
                VALUES (?,?,?,?,?,'pending')
            """, (eid, event_type, audience, pi_id, json.dumps(payload)))
            conn.commit()
            return eid
        finally:
            conn.close()

    def get_pending_scoop_events(self, limit: int = 50) -> list[dict]:
        """Read pending and retry events for Scoop to process."""
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT * FROM scoop_queue
                WHERE status IN ('pending','retry')
                ORDER BY created_at ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_scoop_event(self, event_id: str, status: str,
                            error_msg: str = None) -> None:
        """Update delivery status of a Scoop event. Must be inside a slot."""
        conn = _connect()
        try:
            now = _now_iso()
            if status == "sent":
                conn.execute(
                    "UPDATE scoop_queue SET status='sent', sent_at=? WHERE id=?",
                    (now, event_id)
                )
            elif status == "retry":
                conn.execute("""
                    UPDATE scoop_queue
                    SET status='retry', retry_count=retry_count+1,
                        last_attempt=?, error_msg=?
                    WHERE id=?
                """, (now, error_msg, event_id))
            elif status == "failed":
                conn.execute("""
                    UPDATE scoop_queue
                    SET status='failed', last_attempt=?, error_msg=?
                    WHERE id=?
                """, (now, error_msg, event_id))
            else:
                conn.execute(
                    "UPDATE scoop_queue SET status=? WHERE id=?",
                    (status, event_id)
                )
            conn.commit()
        finally:
            conn.close()

    # ── DEPLOY WATCHES ────────────────────────────────────────────────────────

    def post_deploy_watch(self, suggestion_id: str, deployed_at: str,
                           affected_files: list, watch_for: list,
                           rollback_trigger: str = None,
                           watch_duration_hours: int = 48) -> str:
        """Blueprint calls this after Friday push. Must be inside a slot."""
        wid     = str(uuid.uuid4())
        expires = (
            datetime.now(timezone.utc) + timedelta(hours=watch_duration_hours)
        ).isoformat().replace("+00:00", "Z")

        conn = _connect()
        try:
            conn.execute("""
                INSERT INTO deploy_watches
                (id, suggestion_id, deployed_at, watch_duration_hours,
                 expires_at, affected_files, watch_for, rollback_trigger)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                wid, suggestion_id, deployed_at, watch_duration_hours,
                expires, json.dumps(affected_files),
                json.dumps(watch_for), rollback_trigger
            ))
            conn.commit()
            log.info(f"Deploy watch posted: {suggestion_id[:8]} ({watch_duration_hours}h)")
            return wid
        finally:
            conn.close()

    def get_active_deploy_watches(self) -> list[dict]:
        """Read active watches for Watchdog/Patches to monitor."""
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT * FROM deploy_watches
                WHERE status='active' AND expires_at > ?
            """, (_now_iso(),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def trigger_deploy_watch(self, watch_id: str, triggered_by: str,
                              rollback_executed: bool = False) -> None:
        """Mark a deploy watch as triggered. Must be inside a slot."""
        conn = _connect()
        try:
            conn.execute("""
                UPDATE deploy_watches
                SET status='triggered', triggered_at=?,
                    triggered_by=?, rollback_executed=?
                WHERE id=?
            """, (_now_iso(), triggered_by, 1 if rollback_executed else 0, watch_id))
            conn.commit()
        finally:
            conn.close()

    # ── HEARTBEATS (Sentinel direct writes) ───────────────────────────────────

    def heartbeat_write(self, pi_id: str, payload: dict) -> None:
        """
        Direct write for Sentinel's HTTP heartbeat endpoint.
        Uses short timeout — never blocks on Timekeeper.
        Registers customer if first heartbeat, updates last_heartbeat.
        """
        conn = _direct_connect()
        try:
            # Auto-register new customer Pis
            conn.execute("""
                INSERT OR IGNORE INTO customers (pi_id, license_key, status)
                VALUES (?, ?, 'ACTIVE')
            """, (pi_id, payload.get("license_key", "")))

            # Write heartbeat record
            conn.execute("""
                INSERT INTO heartbeats
                (pi_id, timestamp, portfolio_value, agent_statuses, uptime_seconds)
                VALUES (?,?,?,?,?)
            """, (
                pi_id,
                payload.get("timestamp", _now_iso()),
                payload.get("portfolio_value"),
                json.dumps(payload.get("agents", {})),
                payload.get("uptime_seconds"),
            ))

            # Update customer last_heartbeat
            conn.execute("""
                UPDATE customers
                SET last_heartbeat=?, status='ACTIVE'
                WHERE pi_id=?
            """, (payload.get("timestamp", _now_iso()), pi_id))

            conn.commit()
        finally:
            conn.close()

    def get_customer_heartbeat_age(self, pi_id: str) -> float | None:
        """Return hours since last heartbeat for a given Pi. Read-only."""
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT last_heartbeat FROM customers WHERE pi_id=?", (pi_id,)
            ).fetchone()
            if not row or not row["last_heartbeat"]:
                return None
            last = datetime.fromisoformat(
                row["last_heartbeat"].replace("Z", "+00:00")
            )
            return (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        finally:
            conn.close()

    # ── SILENCE ALERT DEDUP ───────────────────────────────────────────────────

    def silence_alert_needed(self, pi_id: str,
                              cooldown_hours: int = 4) -> bool:
        """
        Returns True if a silence alert should be filed for this Pi.
        Updates the dedup record if True.
        Uses direct write — called from Sentinel's monitoring thread.
        """
        conn = _direct_connect()
        try:
            row = conn.execute(
                "SELECT last_alerted FROM silence_alerts WHERE pi_id=?",
                (pi_id,)
            ).fetchone()

            now     = datetime.now(timezone.utc)
            cutoff  = (now - timedelta(hours=cooldown_hours)).isoformat().replace("+00:00", "Z")

            if row and row["last_alerted"] > cutoff:
                return False

            # Upsert dedup record
            conn.execute("""
                INSERT INTO silence_alerts (pi_id, last_alerted, alert_count)
                VALUES (?, ?, 1)
                ON CONFLICT(pi_id) DO UPDATE SET
                    last_alerted=excluded.last_alerted,
                    alert_count=alert_count+1
            """, (pi_id, now.isoformat().replace("+00:00", "Z")))
            conn.commit()
            return True
        finally:
            conn.close()

    def clear_silence_alert(self, pi_id: str) -> None:
        """Clear dedup record when Pi comes back online. Direct write."""
        conn = _direct_connect()
        try:
            conn.execute("DELETE FROM silence_alerts WHERE pi_id=?", (pi_id,))
            conn.commit()
        finally:
            conn.close()

    # ── AUDIT TRAIL ───────────────────────────────────────────────────────────

    def audit(self, agent: str, action: str, target: str = None,
              details: str = None, outcome: str = "SUCCESS") -> None:
        """
        Write immutable audit record.
        Uses direct write — audit must never fail silently or block.
        """
        conn = _direct_connect()
        try:
            conn.execute("""
                INSERT INTO audit_trail (agent, action, target, details, outcome)
                VALUES (?,?,?,?,?)
            """, (agent, action, target, details, outcome))
            conn.commit()
        except Exception as e:
            log.error(f"Audit write failed: {e} — action={action} target={target}")
        finally:
            conn.close()

    # ── BACKUP LOG ────────────────────────────────────────────────────────────

    def log_backup(self, pi_id: str, status: str, size_kb: float = None,
                   remote_path: str = None, files_included: int = None,
                   error_msg: str = None) -> None:
        """Log a backup run result. Must be inside a slot."""
        conn = _connect()
        try:
            conn.execute("""
                INSERT INTO backup_log
                (pi_id, status, size_kb, remote_path, files_included, error_msg)
                VALUES (?,?,?,?,?,?)
            """, (pi_id, status, size_kb, remote_path, files_included, error_msg))
            conn.commit()
        finally:
            conn.close()

    def get_backup_status(self) -> dict:
        """Return backup health summary. Read-only."""
        conn = _connect()
        try:
            last = conn.execute("""
                SELECT pi_id, status, timestamp, size_kb, error_msg
                FROM backup_log
                ORDER BY timestamp DESC LIMIT 20
            """).fetchall()

            if not last:
                return {"status": "no_runs", "last_run": None}

            most_recent = last[0]
            age_hours   = (
                datetime.now(timezone.utc) -
                datetime.fromisoformat(
                    most_recent["timestamp"].replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)
            ).total_seconds() / 3600

            failed = [r for r in last if r["status"] == "failed"]
            health = "healthy"
            if age_hours > 48:
                health = "stale"
            elif failed:
                health = "degraded"

            return {
                "status":    health,
                "age_hours": round(age_hours, 1),
                "last_run":  most_recent["timestamp"],
                "failed":    len(failed),
                "recent":    [dict(r) for r in last],
            }
        finally:
            conn.close()

    # ── API USAGE (Fidget) ────────────────────────────────────────────────────

    def log_api_call(self, agent_name: str, provider: str,
                     operation: str, token_count: int = 0,
                     tokens_input: int = 0, tokens_output: int = 0,
                     cost_estimate: float = 0.0, model: str = None,
                     pi_id: str = None) -> None:
        """
        Log an API call. Direct write — must never block agent execution.
        Called by agents after every external API call.
        """
        conn = _direct_connect()
        try:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            conn.execute("""
                INSERT INTO api_usage
                (pi_id, agent_name, api_provider, operation,
                 token_count, call_count, cost_estimate)
                VALUES (?,?,?,?,?,1,?)
            """, (pi_id or "company", agent_name, provider,
                  operation, token_count, cost_estimate))

            if provider.lower() == "anthropic" and token_count > 0:
                conn.execute("""
                    INSERT INTO token_ledger
                    (agent_name, tokens_used, tokens_input, tokens_output,
                     model, operation, cost_estimate, month)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (agent_name, token_count, tokens_input, tokens_output,
                      model, operation, cost_estimate, month))

            conn.commit()
        except Exception as e:
            log.warning(f"API usage log failed: {e}")
        finally:
            conn.close()

    # ── GENERAL READ ──────────────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """
        Generic read query. No slot needed — reads are always safe in WAL mode.
        Use for portal queries and agent status checks.
        """
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_customers(self, status: str = None) -> list[dict]:
        """Return customer list, optionally filtered by status."""
        if status:
            return self.query(
                "SELECT * FROM customers WHERE status=? ORDER BY pi_id",
                (status,)
            )
        return self.query("SELECT * FROM customers ORDER BY pi_id")


# ── UTILITY ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
