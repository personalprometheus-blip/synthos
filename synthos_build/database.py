"""
database.py — Shared SQLite foundation for the Synthos.

All three agents import this module. It handles:
  - Schema creation on first run (cold start safe)
  - Read/write helpers used by every agent
  - Portfolio state persistence and recovery
  - Position reconciliation helpers
  - System logging

Usage:
  from database import DB
  db = DB()
  db.log_heartbeat("agent1_trader", "OK", portfolio_value=102.34)
"""

import sqlite3
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── AGENT DB LOCK ──────────────────────────────────────────────────────────
# Agents write a lock file before running so the portal backs off.
# Priority order: agent1 > agent3 > agent4 > agent2 > portal
# The portal waits up to 10 minutes — agents typically finish in 1-2 min.

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_LOCK_FILE = os.path.join(_PROJECT_DIR, '.agent_lock')

# Callers that get DB priority (write the lock file)
PRIORITY_AGENTS = {
    'agent1_trader.py':    1,
    'agent3_sentiment.py': 2,
    'agent4_audit.py':     3,
    'agent2_research.py':  4,
}

# Callers that back off when locked (portal, heartbeat, digest)
BACKOFF_CALLERS = {'portal.py', 'heartbeat.py', 'daily_digest.py', 'health_check.py'}


def acquire_agent_lock(agent_name):
    """
    Write the agent lock file. Call at start of agent run.
    Returns the lock path so caller can release it.
    """
    try:
        priority = PRIORITY_AGENTS.get(agent_name, 99)
        with open(AGENT_LOCK_FILE, 'w') as f:
            f.write(f"{agent_name}\n{priority}\n{time.time()}\n")
    except Exception:
        pass
    return AGENT_LOCK_FILE


def release_agent_lock():
    """Remove the agent lock file. Call at end of agent run."""
    try:
        if os.path.exists(AGENT_LOCK_FILE):
            os.remove(AGENT_LOCK_FILE)
    except Exception:
        pass


def _wait_for_agent_lock(caller='unknown', max_wait=None):
    """
    If a higher-priority agent holds the lock, wait for it to finish.
    Never waits for itself. Clears stale/malformed locks immediately.
    Portal and backoff callers wait max 5s — agents wait up to 10 min.
    """
    if not os.path.exists(AGENT_LOCK_FILE):
        return

    # Portal and background callers bail out fast — never block a request thread
    if max_wait is None:
        max_wait = 5 if caller in BACKOFF_CALLERS else 600

    caller_priority = PRIORITY_AGENTS.get(caller, 99)

    try:
        content = open(AGENT_LOCK_FILE).read().strip()
        parts = content.split('\n')

        # Malformed lock file — clear it
        if len(parts) < 3:
            release_agent_lock()
            return

        lock_agent    = parts[0]
        lock_priority = int(parts[1])
        lock_time     = float(parts[2])

        # Stale lock (over 10 min old) — remove it
        if time.time() - lock_time > 600:
            import logging as _log
            _log.getLogger('database').warning(
                f"Clearing stale lock from {lock_agent} ({int(time.time()-lock_time)}s old)"
            )
            release_agent_lock()
            return

        # Never wait for yourself
        if lock_agent == caller:
            import logging as _log
            _log.getLogger('database').warning(
                f"{caller} found its own lock — clearing and proceeding"
            )
            release_agent_lock()
            return

        # Higher priority caller — take over the lock
        if caller_priority < lock_priority:
            import logging as _log
            _log.getLogger('database').info(
                f"{caller} (p{caller_priority}) preempting {lock_agent} (p{lock_priority})"
            )
            acquire_agent_lock(caller)
            time.sleep(2)
            return

        # Lower or equal priority — wait up to max_wait
        if lock_priority <= caller_priority:
            waited = 0
            while os.path.exists(AGENT_LOCK_FILE) and waited < max_wait:
                try:
                    current = open(AGENT_LOCK_FILE).read().strip().split('\n')
                    if current[0] == caller:
                        release_agent_lock()
                        return
                    if time.time() - float(current[2]) > 600:
                        release_agent_lock()
                        return
                except Exception:
                    release_agent_lock()
                    return
                if waited > 0 and waited % 30 == 0:
                    import logging as _log
                    _log.getLogger('database').info(
                        f"Waiting for {lock_agent} to release DB lock... ({waited}s)"
                    )
                time.sleep(5)
                waited += 5
            # Timed out — log and proceed anyway
            if waited >= max_wait and max_wait > 10:
                import logging as _log
                _log.getLogger('database').warning(
                    f"{caller} timed out waiting for {lock_agent} after {max_wait}s — proceeding anyway"
                )

    except Exception:
        release_agent_lock()

DB_PATH = os.path.join(os.path.dirname(__file__), 'signals.db')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('database')


# ── SCHEMA ────────────────────────────────────────────────────────────────
SCHEMA = """
-- ── PORTFOLIO ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio (
    id              INTEGER PRIMARY KEY,
    cash            REAL    NOT NULL,
    realized_gains  REAL    NOT NULL DEFAULT 0.0,
    tax_withdrawn   REAL    NOT NULL DEFAULT 0.0,
    month_start     REAL    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- ── POSITIONS ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT    PRIMARY KEY,   -- e.g. "pos_AAPL_20240214"
    ticker          TEXT    NOT NULL,
    company         TEXT,
    sector          TEXT,
    entry_price     REAL    NOT NULL,
    current_price   REAL,
    shares          REAL    NOT NULL,
    trail_stop_amt  REAL    NOT NULL,      -- ATR-based dollar amount
    trail_stop_pct  REAL    NOT NULL,      -- percentage for display
    vol_bucket      TEXT,                  -- Low Vol / Mid Vol / High Vol
    pnl             REAL    NOT NULL DEFAULT 0.0,
    status          TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED / RECONCILE_NEEDED
    opened_at       TEXT    NOT NULL,
    closed_at       TEXT,
    exit_reason     TEXT,                  -- TRAILING_STOP / PROFIT_TAKE / MANUAL
    signal_id       INTEGER,               -- FK to signals table
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

-- ── SIGNALS ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    company         TEXT,
    sector          TEXT,
    source          TEXT    NOT NULL,
    source_tier     INTEGER NOT NULL,      -- 1=Official 2=Wire 3=Press 4=Opinion
    headline        TEXT,
    politician      TEXT,
    tx_date         TEXT,                  -- transaction date from disclosure
    disc_date       TEXT,                  -- disclosure filing date
    amount_range    TEXT,
    confidence      TEXT    NOT NULL,      -- HIGH / MEDIUM / LOW / NOISE
    staleness       TEXT,                  -- Fresh / Aging / Stale / Expired
    corroborated    INTEGER NOT NULL DEFAULT 0,  -- 0/1
    corroboration_note TEXT,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
                                           -- PENDING / WATCHING / QUEUED /
                                           -- ACTED_ON / DISCARDED / EXPIRED /
                                           -- INTERRUPTED
    is_amended      INTEGER NOT NULL DEFAULT 0,
    is_spousal      INTEGER NOT NULL DEFAULT 0,
    needs_reeval    INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT,
    discard_delete_at TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- ── LEDGER ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    type            TEXT    NOT NULL,      -- DEPOSIT / ENTRY / EXIT / TAX / BIL
    description     TEXT,
    amount          REAL    NOT NULL,
    balance         REAL    NOT NULL,
    position_id     TEXT,
    created_at      TEXT    NOT NULL
);

-- ── OUTCOMES ───────────────────────────────────────────────────────────
-- Closed trade outcomes — feeds the learning loop
CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    shares          REAL    NOT NULL,
    hold_days       INTEGER,
    pnl_pct         REAL,
    pnl_dollar      REAL,
    signal_tier     INTEGER,
    staleness       TEXT,
    vol_bucket      TEXT,
    exit_reason     TEXT,
    verdict         TEXT,                  -- WIN / LOSS
    lesson          TEXT,                  -- Claude-generated lesson
    created_at      TEXT    NOT NULL
);

-- ── HANDSHAKES ─────────────────────────────────────────────────────────
-- Tracks signal passing between The Daily and The Trader
CREATE TABLE IF NOT EXISTS handshakes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER NOT NULL,
    ticker          TEXT    NOT NULL,
    from_agent      TEXT    NOT NULL,      -- "The Daily"
    to_agent        TEXT    NOT NULL,      -- "The Trader"
    queued_at       TEXT    NOT NULL,
    acknowledged_at TEXT,
    ack             INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

-- ── SCAN LOG ───────────────────────────────────────────────────────────
-- The Pulse 30-min scan results — capped by cleanup.py
CREATE TABLE IF NOT EXISTS scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    put_call_ratio  REAL,
    put_call_avg30d REAL,
    insider_net     TEXT,
    volume_vs_avg   TEXT,
    seller_dominance TEXT,
    cascade_detected INTEGER NOT NULL DEFAULT 0,
    tier            INTEGER NOT NULL,      -- 1=Critical 2=Elevated 3=Neutral 4=Quiet
    event_summary   TEXT,
    scanned_at      TEXT    NOT NULL
);

-- ── SYSTEM LOG ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    event           TEXT    NOT NULL,      -- HEARTBEAT / SHUTDOWN / REBOOT_OK / etc
    agent           TEXT,
    details         TEXT,
    portfolio_value REAL
);

-- ── URGENT FLAGS ───────────────────────────────────────────────────────
-- Cascade alerts that bypass normal session schedule
CREATE TABLE IF NOT EXISTS urgent_flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    detected_at     TEXT    NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 1,
    acknowledged    INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    scan_log_id     INTEGER,
    FOREIGN KEY (scan_log_id) REFERENCES scan_log(id)
);

-- ── PENDING APPROVALS ─────────────────────────────────────────────────
-- Supervised mode trade approval queue.
-- Full lifecycle preserved — rows are never deleted.
-- Active queue: filter WHERE status = 'PENDING_APPROVAL'
-- Statuses: PENDING_APPROVAL / APPROVED / REJECTED / EXECUTED / EXPIRED
CREATE TABLE IF NOT EXISTS pending_approvals (
    id              TEXT    PRIMARY KEY,   -- signal id (e.g. "42" or "sig_AAPL_...")
    ticker          TEXT    NOT NULL,
    company         TEXT,
    sector          TEXT,
    politician      TEXT,
    confidence      TEXT,                  -- HIGH / MEDIUM / LOW
    staleness       TEXT,                  -- Fresh / Aging / Stale / Expired
    headline        TEXT,
    price           REAL,
    shares          REAL,
    max_trade       REAL,
    trail_amt       REAL,
    trail_pct       REAL,
    vol_label       TEXT,
    reasoning       TEXT,
    session         TEXT,
    status          TEXT    NOT NULL DEFAULT 'PENDING_APPROVAL',
    queued_at       TEXT    NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,                  -- 'portal' or agent name
    executed_at     TEXT,
    decision_note   TEXT
);

-- ── INDEXES ────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_signals_status        ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_ticker        ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_positions_status      ON positions(status);
CREATE INDEX IF NOT EXISTS idx_scan_log_scanned      ON scan_log(scanned_at);
CREATE INDEX IF NOT EXISTS idx_system_log_event      ON system_log(event);
CREATE INDEX IF NOT EXISTS idx_urgent_flags_ack      ON urgent_flags(acknowledged);
CREATE INDEX IF NOT EXISTS idx_approvals_status      ON pending_approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_queued      ON pending_approvals(queued_at);
"""


# ── DB CLASS ──────────────────────────────────────────────────────────────
class DB:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._persistent_conn = None
        self._init_schema()

    @contextmanager
    def conn(self):
        """
        Context manager — always commits or rolls back cleanly.
        Waits for agent lock to clear before proceeding (portal backs off).
        """
        if self.path == ':memory:':
            try:
                yield self._persistent_conn
                self._persistent_conn.commit()
            except Exception as e:
                self._persistent_conn.rollback()
                log.error(f"DB error — rolled back: {e}")
                raise
            return

        # Wait for agent lock — portal backs off in 5s, agents wait up to 10min
        _wait_for_agent_lock(caller=os.path.basename(sys.argv[0] if sys.argv else 'unknown'))

        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception as e:
            c.rollback()
            log.error(f"DB error — rolled back: {e}")
            raise
        finally:
            c.close()

    def _init_schema(self):
        """Create all tables on first run. Safe to call every startup."""
        if self.path == ':memory:':
            self._persistent_conn = sqlite3.connect(':memory:')
            self._persistent_conn.row_factory = sqlite3.Row
            self._persistent_conn.execute("PRAGMA foreign_keys=ON")
            self._persistent_conn.executescript(SCHEMA)
            self._persistent_conn.commit()
        else:
            # Note: no lock check here — _init_schema is called during get_db()
            # which may be called by the agent that already holds the lock.
            # Only conn() checks the lock for individual operations.
            c = sqlite3.connect(self.path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(SCHEMA)
            c.commit()
            c.close()
        log.info(f"Schema verified: {self.path}")
        self._run_migrations()

    def _run_migrations(self):
        """
        Apply any schema changes that can't be handled by CREATE TABLE IF NOT EXISTS.
        Safe to run every startup — each migration checks before applying.
        Add new migrations as ALTER TABLE statements at the bottom of MIGRATIONS list.
        """
        if self.path == ':memory:':
            return  # in-memory DB always starts fresh, no migrations needed

        MIGRATIONS = [
            # v1.1 — add lesson column to outcomes if missing
            "ALTER TABLE outcomes ADD COLUMN lesson TEXT",
            # v1.1 — add needs_reeval column to signals if missing
            "ALTER TABLE signals ADD COLUMN needs_reeval INTEGER NOT NULL DEFAULT 0",
            # v1.1 — add label column to urgent_flags if missing
            "ALTER TABLE urgent_flags ADD COLUMN label TEXT",
        ]

        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        for sql in MIGRATIONS:
            try:
                c.execute(sql)
                c.commit()
                log.info(f"Migration applied: {sql[:60]}")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass  # column already exists — skip silently
                else:
                    log.warning(f"Migration skipped ({sql[:40]}): {e}")
        c.close()

    def now(self):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── PORTFOLIO ──────────────────────────────────────────────────────────

    def get_portfolio(self):
        """Returns current portfolio row or seeds it with starting capital."""
        with self.conn() as c:
            row = c.execute("SELECT * FROM portfolio ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                return dict(row)
            # Cold start — seed with starting capital, return directly (no recursion)
            starting = float(os.environ.get('STARTING_CAPITAL', 100000.0))
            c.execute("""
                INSERT INTO portfolio (cash, realized_gains, tax_withdrawn, month_start, updated_at)
                VALUES (?, 0.0, 0.0, ?, ?)
            """, (starting, starting, self.now()))
            log.info(f"Cold start — portfolio seeded at ${starting:.2f}")
            row_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {
                'id': row_id, 'cash': starting, 'realized_gains': 0.0,
                'tax_withdrawn': 0.0, 'month_start': starting, 'updated_at': self.now()
            }

    def update_portfolio(self, cash=None, realized_gains=None, tax_withdrawn=None, month_start=None):
        """Partial update — only changes fields you pass."""
        portfolio = self.get_portfolio()
        with self.conn() as c:
            c.execute("""
                UPDATE portfolio SET
                    cash            = ?,
                    realized_gains  = ?,
                    tax_withdrawn   = ?,
                    month_start     = ?,
                    updated_at      = ?
                WHERE id = ?
            """, (
                cash            if cash            is not None else portfolio['cash'],
                realized_gains  if realized_gains  is not None else portfolio['realized_gains'],
                tax_withdrawn   if tax_withdrawn    is not None else portfolio['tax_withdrawn'],
                month_start     if month_start      is not None else portfolio['month_start'],
                self.now(),
                portfolio['id'],
            ))

    def sweep_monthly_tax(self, tax_amount):
        """
        Sweep 10% of gains at month end.
        Reduces cash, logs to ledger, resets realized_gains.
        """
        portfolio = self.get_portfolio()
        new_cash = portfolio['cash'] - tax_amount
        new_withdrawn = portfolio['tax_withdrawn'] + tax_amount
        self.update_portfolio(
            cash=new_cash,
            tax_withdrawn=new_withdrawn,
            realized_gains=0.0,
            month_start=new_cash,
        )
        self.add_ledger_entry(
            entry_type="TAX",
            description=f"10% monthly gain sweep",
            amount=-tax_amount,
            balance=new_cash,
        )
        log.info(f"Tax sweep: ${tax_amount:.2f} — new cash: ${new_cash:.2f}")

    # ── POSITIONS ──────────────────────────────────────────────────────────

    def get_open_positions(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def open_position(self, ticker, company, sector, entry_price, shares,
                      trail_stop_amt, trail_stop_pct, vol_bucket, signal_id=None):
        """
        Opens a new position. Also deducts cost from portfolio cash
        and writes a ledger entry.
        """
        pos_id   = f"pos_{ticker}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        cost     = round(entry_price * shares, 2)
        portfolio = self.get_portfolio()
        new_cash  = round(portfolio['cash'] - cost, 2)

        with self.conn() as c:
            c.execute("""
                INSERT INTO positions
                    (id, ticker, company, sector, entry_price, current_price,
                     shares, trail_stop_amt, trail_stop_pct, vol_bucket,
                     pnl, status, opened_at, signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,0.0,'OPEN',?,?)
            """, (pos_id, ticker, company, sector, entry_price, entry_price,
                  shares, trail_stop_amt, trail_stop_pct, vol_bucket,
                  self.now(), signal_id))

        self.update_portfolio(cash=new_cash)
        self.add_ledger_entry(
            entry_type="ENTRY",
            description=f"{ticker} · {shares:.4f} sh @ ${entry_price:.2f}",
            amount=-cost,
            balance=new_cash,
            position_id=pos_id,
        )
        log.info(f"Opened position: {ticker} {shares:.4f}sh @ ${entry_price:.2f} — cost ${cost:.2f}")
        return pos_id

    def close_position(self, pos_id, exit_price, exit_reason):
        """
        Closes a position. Adds proceeds to cash, records outcome,
        writes ledger entry.
        """
        with self.conn() as c:
            pos = c.execute(
                "SELECT * FROM positions WHERE id=?", (pos_id,)
            ).fetchone()
            if not pos:
                raise ValueError(f"Position {pos_id} not found")
            pos = dict(pos)

        proceeds   = round(exit_price * float(pos['shares']), 2)
        cost       = round(float(pos['entry_price']) * float(pos['shares']), 2)
        pnl_dollar = round(proceeds - cost, 2)
        pnl_pct    = round((pnl_dollar / cost) * 100, 2)
        hold_days  = (datetime.now() - datetime.strptime(pos['opened_at'], '%Y-%m-%d %H:%M:%S')).days
        verdict    = "WIN" if pnl_dollar >= 0 else "LOSS"
        portfolio  = self.get_portfolio()
        new_cash   = round(portfolio['cash'] + proceeds, 2)
        new_gains  = round(portfolio['realized_gains'] + pnl_dollar, 2)

        with self.conn() as c:
            c.execute("""
                UPDATE positions SET
                    current_price=?, pnl=?, status='CLOSED',
                    closed_at=?, exit_reason=?
                WHERE id=?
            """, (exit_price, pnl_dollar, self.now(), exit_reason, pos_id))

            c.execute("""
                INSERT INTO outcomes
                    (position_id, ticker, entry_price, exit_price, shares,
                     hold_days, pnl_pct, pnl_dollar, signal_tier, staleness,
                     vol_bucket, exit_reason, verdict, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (pos_id, pos['ticker'], pos['entry_price'], exit_price,
                  pos['shares'], hold_days, pnl_pct, pnl_dollar,
                  None, None, pos['vol_bucket'],
                  exit_reason, verdict, self.now()))

        self.update_portfolio(cash=new_cash, realized_gains=new_gains)
        self.add_ledger_entry(
            entry_type="EXIT",
            description=f"{pos['ticker']} · {exit_reason} · {'+' if pnl_dollar>=0 else ''}{pnl_dollar:.2f}",
            amount=proceeds,
            balance=new_cash,
            position_id=pos_id,
        )
        log.info(f"Closed position: {pos['ticker']} {verdict} {pnl_pct:+.2f}% (${pnl_dollar:+.2f})")
        return pnl_dollar

    def update_position_price(self, pos_id, current_price):
        """Update mark-to-market price and recalculate unrealized P&L."""
        with self.conn() as c:
            pos = c.execute(
                "SELECT entry_price, shares FROM positions WHERE id=?", (pos_id,)
            ).fetchone()
            if not pos:
                return
            pnl = round((current_price - pos['entry_price']) * pos['shares'], 2)
            c.execute(
                "UPDATE positions SET current_price=?, pnl=? WHERE id=?",
                (current_price, pnl, pos_id)
            )

    def flag_orphan(self, pos_id):
        """Mark a position as needing human reconciliation."""
        with self.conn() as c:
            c.execute(
                "UPDATE positions SET status='RECONCILE_NEEDED' WHERE id=?",
                (pos_id,)
            )
        log.warning(f"Position {pos_id} flagged as RECONCILE_NEEDED — human review required")

    # ── SIGNALS ────────────────────────────────────────────────────────────

    def upsert_signal(self, ticker, source, source_tier, headline,
                      politician=None, tx_date=None, disc_date=None,
                      amount_range=None, confidence="MEDIUM",
                      staleness="Fresh", corroborated=False,
                      corroboration_note=None, sector=None, company=None,
                      is_amended=False, is_spousal=False):
        """
        Insert a new signal or update if same ticker+tx_date already exists.
        Returns signal id.
        """
        # Deduplication check — same ticker + tx_date = same disclosure
        with self.conn() as c:
            existing = c.execute("""
                SELECT id, status FROM signals
                WHERE ticker=? AND tx_date=? AND status NOT IN ('DISCARDED','EXPIRED')
                LIMIT 1
            """, (ticker, tx_date or '')).fetchone()

            if existing:
                log.info(f"Signal dedup: {ticker} {tx_date} already exists (id={existing['id']}, status={existing['status']})")
                return existing['id']

            # Calculate expiry based on tier
            expiry_days = {1:30, 2:7, 3:2, 4:1}.get(source_tier, 7)
            expires_at  = (datetime.now() + timedelta(days=expiry_days)).strftime('%Y-%m-%d %H:%M:%S')
            discard_del = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

            # Tier 4 always discarded immediately
            status = "DISCARDED" if source_tier == 4 else "PENDING"

            c.execute("""
                INSERT INTO signals
                    (ticker, company, sector, source, source_tier, headline,
                     politician, tx_date, disc_date, amount_range, confidence,
                     staleness, corroborated, corroboration_note,
                     is_amended, is_spousal, status, expires_at,
                     discard_delete_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, company, sector, source, source_tier, headline,
                  politician, tx_date, disc_date, amount_range, confidence,
                  staleness, int(corroborated), corroboration_note,
                  int(is_amended), int(is_spousal), status, expires_at,
                  discard_del, self.now(), self.now()))

            sig_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            log.info(f"New signal: {ticker} T{source_tier} {confidence} — id={sig_id}")
            return sig_id

    def queue_signal_for_trader(self, signal_id):
        """Mark signal as QUEUED and create handshake record."""
        with self.conn() as c:
            sig = c.execute(
                "SELECT ticker, status FROM signals WHERE id=?", (signal_id,)
            ).fetchone()
            if not sig:
                raise ValueError(f"Signal {signal_id} not found")
            if sig['status'] == 'QUEUED':
                log.info(f"Signal {signal_id} already queued")
                return

            c.execute("""
                UPDATE signals SET status='QUEUED', updated_at=? WHERE id=?
            """, (self.now(), signal_id))

            c.execute("""
                INSERT INTO handshakes (signal_id, ticker, from_agent, to_agent, queued_at)
                VALUES (?,?,'The Daily','The Trader',?)
            """, (signal_id, sig['ticker'], self.now()))

        log.info(f"Signal {signal_id} ({sig['ticker']}) queued for trader")

    def acknowledge_signal(self, signal_id):
        """Trader acknowledges it has acted on a signal."""
        with self.conn() as c:
            c.execute("""
                UPDATE signals SET status='ACTED_ON', updated_at=? WHERE id=?
            """, (self.now(), signal_id))
            c.execute("""
                UPDATE handshakes SET ack=1, acknowledged_at=?
                WHERE signal_id=? AND ack=0
            """, (self.now(), signal_id))
        log.info(f"Signal {signal_id} acknowledged by trader")

    def get_queued_signals(self):
        """All signals ready for the trader to act on."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM signals
                WHERE status='QUEUED'
                ORDER BY source_tier ASC, created_at ASC
            """).fetchall()
            return [dict(r) for r in rows]

    def expire_old_signals(self):
        """Move signals past their expiry to EXPIRED status."""
        with self.conn() as c:
            result = c.execute("""
                UPDATE signals SET status='EXPIRED', updated_at=?
                WHERE status IN ('PENDING','WATCHING')
                AND expires_at < ?
                AND expires_at IS NOT NULL
            """, (self.now(), self.now()))
            if result.rowcount:
                log.info(f"Expired {result.rowcount} signal(s)")

    def discard_signal(self, signal_id, reason=None):
        with self.conn() as c:
            c.execute("""
                UPDATE signals SET status='DISCARDED', updated_at=?,
                corroboration_note=COALESCE(?, corroboration_note)
                WHERE id=?
            """, (self.now(), reason, signal_id))

    # ── LEDGER ─────────────────────────────────────────────────────────────

    def add_ledger_entry(self, entry_type, description, amount, balance, position_id=None):
        with self.conn() as c:
            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now().strftime('%Y-%m-%d'), entry_type,
                  description, amount, balance, position_id, self.now()))

    def get_ledger(self, limit=100):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM ledger ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_portfolio_history(self, days=30):
        """
        Return portfolio value over time for the graph.
        Uses system_log heartbeat entries that record portfolio_value.
        Falls back to ledger balance snapshots.
        """
        with self.conn() as c:
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            rows = c.execute("""
                SELECT DATE(timestamp) as date,
                       MAX(portfolio_value) as value
                FROM system_log
                WHERE portfolio_value IS NOT NULL
                  AND portfolio_value > 0
                  AND DATE(timestamp) >= ?
                GROUP BY DATE(timestamp)
                ORDER BY date ASC
            """, (cutoff,)).fetchall()
            if rows:
                return [{'date': r['date'], 'value': round(r['value'], 2)} for r in rows]
            # Fallback: ledger balance snapshots
            rows = c.execute("""
                SELECT date, balance as value
                FROM ledger
                WHERE date >= ?
                ORDER BY date ASC
            """, (cutoff,)).fetchall()
            return [{'date': r['date'], 'value': round(r['value'], 2)} for r in rows]

    def get_watching_signals(self, limit=20):
        """Signals Claude has scored — up to 45 days back, all confidence levels."""
        with self.conn() as c:
            cutoff = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')
            rows = c.execute("""
                SELECT * FROM signals
                WHERE confidence IN ('HIGH', 'MEDIUM', 'LOW')
                  AND created_at >= ?
                ORDER BY
                  CASE confidence WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                  created_at DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()
            return [dict(r) for r in rows]

    # ── SCAN LOG ───────────────────────────────────────────────────────────

    def log_scan(self, ticker, put_call_ratio, put_call_avg30d, insider_net,
                 volume_vs_avg, seller_dominance, cascade_detected, tier, event_summary):
        with self.conn() as c:
            c.execute("""
                INSERT INTO scan_log
                    (ticker, put_call_ratio, put_call_avg30d, insider_net,
                     volume_vs_avg, seller_dominance, cascade_detected,
                     tier, event_summary, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (ticker, put_call_ratio, put_call_avg30d, insider_net,
                  volume_vs_avg, seller_dominance, int(cascade_detected),
                  tier, event_summary, self.now()))

        if cascade_detected:
            self.raise_urgent_flag(ticker, tier)

    def raise_urgent_flag(self, ticker, tier=1):
        """Raises an urgent flag — bypasses normal session schedule."""
        with self.conn() as c:
            existing = c.execute("""
                SELECT id FROM urgent_flags
                WHERE ticker=? AND acknowledged=0
            """, (ticker,)).fetchone()
            if existing:
                log.info(f"Urgent flag already active for {ticker}")
                return
            c.execute("""
                INSERT INTO urgent_flags (ticker, detected_at, tier, acknowledged)
                VALUES (?,?,?,0)
            """, (ticker, self.now(), tier))
        log.warning(f"URGENT FLAG raised: {ticker} Tier {tier}")

    def get_urgent_flags(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM urgent_flags WHERE acknowledged=0 ORDER BY detected_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def acknowledge_urgent_flag(self, flag_id):
        with self.conn() as c:
            c.execute("""
                UPDATE urgent_flags SET acknowledged=1, acknowledged_at=? WHERE id=?
            """, (self.now(), flag_id))

    # ── SYSTEM LOG & HEARTBEAT ─────────────────────────────────────────────

    def log_heartbeat(self, agent_name, status="OK", portfolio_value=None):
        with self.conn() as c:
            c.execute("""
                INSERT INTO system_log (timestamp, event, agent, details, portfolio_value)
                VALUES (?,?,?,?,?)
            """, (self.now(), 'HEARTBEAT', agent_name, status, portfolio_value))

    def log_event(self, event, agent=None, details=None, portfolio_value=None):
        with self.conn() as c:
            c.execute("""
                INSERT INTO system_log (timestamp, event, agent, details, portfolio_value)
                VALUES (?,?,?,?,?)
            """, (self.now(), event, agent, details, portfolio_value))
        log.info(f"System event: {event} — {details or ''}")

    def get_last_heartbeat(self, agent_name=None):
        with self.conn() as c:
            if agent_name:
                row = c.execute("""
                    SELECT * FROM system_log
                    WHERE event='HEARTBEAT' AND agent=?
                    ORDER BY timestamp DESC LIMIT 1
                """, (agent_name,)).fetchone()
            else:
                row = c.execute("""
                    SELECT * FROM system_log
                    WHERE event='HEARTBEAT'
                    ORDER BY timestamp DESC LIMIT 1
                """).fetchone()
            return dict(row) if row else None

    # ── OUTCOMES / LEARNING ────────────────────────────────────────────────

    def get_recent_outcomes(self, limit=10):
        """Returns closed trade outcomes for learning context."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM outcomes ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def update_outcome_lesson(self, outcome_id, lesson):
        """Store Claude-generated lesson for a closed trade."""
        with self.conn() as c:
            c.execute(
                "UPDATE outcomes SET lesson=? WHERE id=?", (lesson, outcome_id)
            )

    # ── INTEGRITY & RECONCILIATION ─────────────────────────────────────────

    def integrity_check(self):
        """Run SQLite PRAGMA integrity_check. Returns True if clean."""
        with self.conn() as c:
            result = c.execute("PRAGMA integrity_check").fetchone()
            ok = result[0] == 'ok'
            if not ok:
                log.error(f"Integrity check FAILED: {result[0]}")
            return ok

    def get_open_tickers(self):
        """Returns set of tickers with open positions — for Alpaca reconciliation."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT ticker FROM positions WHERE status='OPEN'"
            ).fetchall()
            return {r['ticker'] for r in rows}

    # ── PENDING APPROVALS ──────────────────────────────────────────────────

    def queue_approval(self, signal_id, ticker, company='', sector='',
                       politician='', confidence='', staleness='',
                       headline='', price=None, shares=None, max_trade=None,
                       trail_amt=None, trail_pct=None, vol_label='',
                       reasoning='', session=''):
        """
        Insert or replace a pending approval entry.
        Deduplicates by signal_id — re-queuing the same signal id
        replaces the existing row only if it is still PENDING_APPROVAL.
        """
        now = self.now()
        with self.conn() as c:
            c.execute("""
                INSERT INTO pending_approvals (
                    id, ticker, company, sector, politician,
                    confidence, staleness, headline,
                    price, shares, max_trade, trail_amt, trail_pct,
                    vol_label, reasoning, session,
                    status, queued_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                ON CONFLICT(id) DO UPDATE SET
                    ticker        = excluded.ticker,
                    company       = excluded.company,
                    sector        = excluded.sector,
                    politician    = excluded.politician,
                    confidence    = excluded.confidence,
                    staleness     = excluded.staleness,
                    headline      = excluded.headline,
                    price         = excluded.price,
                    shares        = excluded.shares,
                    max_trade     = excluded.max_trade,
                    trail_amt     = excluded.trail_amt,
                    trail_pct     = excluded.trail_pct,
                    vol_label     = excluded.vol_label,
                    reasoning     = excluded.reasoning,
                    session       = excluded.session,
                    status        = 'PENDING_APPROVAL',
                    queued_at     = excluded.queued_at,
                    decided_at    = NULL,
                    decided_by    = NULL,
                    executed_at   = NULL,
                    decision_note = NULL
                WHERE pending_approvals.status = 'PENDING_APPROVAL'
            """, (
                str(signal_id), ticker, company, sector, politician,
                confidence, staleness, headline,
                price, shares, max_trade, trail_amt, trail_pct,
                vol_label, reasoning, session,
                'PENDING_APPROVAL', now
            ))
        log.info(f"[DB] Approval queued: {ticker} id={signal_id}")

    def get_pending_approvals(self, status_filter=None):
        """
        Return approval rows as dicts.
        status_filter: list of statuses, e.g. ['PENDING_APPROVAL']
                       None returns all rows.
        """
        with self.conn() as c:
            if status_filter:
                placeholders = ','.join('?' * len(status_filter))
                rows = c.execute(
                    f"SELECT * FROM pending_approvals "
                    f"WHERE status IN ({placeholders}) ORDER BY queued_at ASC",
                    status_filter
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pending_approvals ORDER BY queued_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def update_approval_status(self, signal_id, status, decided_by='portal',
                                decision_note=None):
        """
        Set status to APPROVED or REJECTED with audit fields.
        Returns True if a row was updated, False if not found or wrong state.
        """
        if status not in ('APPROVED', 'REJECTED'):
            raise ValueError(f"Invalid status for decision: {status}")
        now = self.now()
        with self.conn() as c:
            result = c.execute("""
                UPDATE pending_approvals
                SET status        = ?,
                    decided_at    = ?,
                    decided_by    = ?,
                    decision_note = ?
                WHERE id = ? AND status = 'PENDING_APPROVAL'
            """, (status, now, decided_by, decision_note, str(signal_id)))
            updated = result.rowcount > 0
        if updated:
            log.info(f"[DB] Approval {status}: id={signal_id} by={decided_by}")
        else:
            log.warning(
                f"[DB] update_approval_status: no PENDING_APPROVAL row for id={signal_id}"
            )
        return updated

    def mark_approval_executed(self, signal_id):
        """
        Transition an APPROVED entry to EXECUTED.
        Preserves the row for audit — does not delete.
        Returns True if updated.
        """
        now = self.now()
        with self.conn() as c:
            result = c.execute("""
                UPDATE pending_approvals
                SET status      = 'EXECUTED',
                    executed_at = ?
                WHERE id = ? AND status = 'APPROVED'
            """, (now, str(signal_id)))
            updated = result.rowcount > 0
        if updated:
            log.info(f"[DB] Approval EXECUTED: id={signal_id}")
        else:
            log.warning(
                f"[DB] mark_approval_executed: no APPROVED row for id={signal_id}"
            )
        return updated

    def expire_stale_approvals(self, max_age_hours=48):
        """
        Mark PENDING_APPROVAL entries older than max_age_hours as EXPIRED.
        Called at agent startup to clean the actionable queue.
        Returns count of rows expired.
        """
        cutoff = (
            datetime.now() - timedelta(hours=max_age_hours)
        ).strftime('%Y-%m-%d %H:%M:%S')
        with self.conn() as c:
            result = c.execute("""
                UPDATE pending_approvals
                SET status = 'EXPIRED'
                WHERE status = 'PENDING_APPROVAL'
                  AND queued_at < ?
            """, (cutoff,))
            count = result.rowcount
        if count:
            log.info(
                f"[DB] Expired {count} stale pending approvals (>{max_age_hours}h old)"
            )
        return count

    # ── CLEANUP ────────────────────────────────────────────────────────────

    def cleanup(self):
        """
        Nightly maintenance:
        - Expire stale signals
        - Delete discarded signals older than 180 days
        - Delete scan_log entries older than 180 days
        - Keep heartbeat/system_log for 180 days (6 months)
        - Vacuum the database
        """
        self.expire_old_signals()
        cutoff_180 = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d %H:%M:%S')
        cutoff_30  = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

        with self.conn() as c:
            r1 = c.execute(
                "DELETE FROM signals WHERE status='DISCARDED' AND discard_delete_at < ?",
                (cutoff_180,)
            )
            r2 = c.execute(
                "DELETE FROM scan_log WHERE scanned_at < ?", (cutoff_30,)
            )
            r3 = c.execute(
                # Keep heartbeats 180 days for portfolio history graph
                "DELETE FROM system_log WHERE event='HEARTBEAT' AND timestamp < ?",
                (cutoff_180,)
            )
            r4 = c.execute(
                # Keep other system events 180 days
                "DELETE FROM system_log WHERE event != 'HEARTBEAT' AND timestamp < ?",
                (cutoff_180,)
            )

        log.info(
            f"Cleanup: removed {r1.rowcount} discarded signals, "
            f"{r2.rowcount} scan log entries, "
            f"{r3.rowcount + r4.rowcount} old system log entries (180d retention)"
        )

        # Compact the database
        with sqlite3.connect(self.path, timeout=30) as c:
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("VACUUM")
        log.info("VACUUM complete")


# ── CONVENIENCE SINGLETON ─────────────────────────────────────────────────
_db_instance = None

def get_db():
    """Returns a shared DB instance — safe to call from multiple places."""
    global _db_instance
    if _db_instance is None:
        _db_instance = DB()
    return _db_instance


# ── SELF-TEST ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Running database self-test...")
    db = DB(path=':memory:')   # in-memory — does not write to disk

    # Portfolio
    p = db.get_portfolio()
    assert p['cash'] == 100.0, f"Expected $100 starting cash, got ${p['cash']}"
    print(f"  ✓ Portfolio cold start: ${p['cash']:.2f}")

    # Signal
    sig_id = db.upsert_signal(
        ticker="NVDA", source="Capitol Trades API", source_tier=1,
        headline="Test signal", politician="Nancy Pelosi",
        tx_date="2024-01-08", disc_date="2024-01-15",
        confidence="HIGH", staleness="Fresh",
    )
    assert sig_id is not None
    print(f"  ✓ Signal created: id={sig_id}")

    # Deduplication
    sig_id2 = db.upsert_signal(
        ticker="NVDA", source="Capitol Trades API", source_tier=1,
        headline="Duplicate signal", politician="Nancy Pelosi",
        tx_date="2024-01-08", disc_date="2024-01-15",
        confidence="HIGH", staleness="Fresh",
    )
    assert sig_id2 == sig_id, "Deduplication failed — created duplicate signal"
    print(f"  ✓ Deduplication: blocked duplicate signal")

    # Queue signal
    db.queue_signal_for_trader(sig_id)
    queued = db.get_queued_signals()
    assert len(queued) == 1
    print(f"  ✓ Signal queued: {queued[0]['ticker']}")

    # Open position
    pos_id = db.open_position(
        ticker="NVDA", company="NVIDIA Corp", sector="Technology",
        entry_price=495.00, shares=0.0162,
        trail_stop_amt=14.45, trail_stop_pct=2.92,
        vol_bucket="High Vol", signal_id=sig_id,
    )
    p2 = db.get_portfolio()
    assert p2['cash'] < 100.0, "Cash should have decreased after opening position"
    print(f"  ✓ Position opened: {pos_id} — cash now ${p2['cash']:.2f}")

    # Close position
    pnl = db.close_position(pos_id, exit_price=510.00, exit_reason="PROFIT_TAKE")
    print(f"  ✓ Position closed: P&L ${pnl:+.2f}")

    # Outcomes
    outcomes = db.get_recent_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]['verdict'] == "WIN"
    print(f"  ✓ Outcome recorded: {outcomes[0]['verdict']}")

    # Integrity check
    assert db.integrity_check()
    print(f"  ✓ Integrity check: passed")

    # Heartbeat
    db.log_heartbeat("test_agent", "OK", portfolio_value=101.23)
    hb = db.get_last_heartbeat("test_agent")
    assert hb is not None
    print(f"  ✓ Heartbeat logged: {hb['timestamp']}")

    # Cleanup
    db.cleanup()
    print(f"  ✓ Cleanup: ran without errors")

    print("\n✅ All database tests passed.")
