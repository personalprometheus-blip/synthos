"""
database.py — Shared SQLite foundation for the Synthos.

All three agents import this module. It handles:
  - Schema creation on first run (cold start safe)
  - Read/write helpers used by every agent
  - Portfolio state persistence and recovery
  - Position reconciliation helpers
  - System logging

Usage:
  from retail_database import DB
  db = DB()
  db.log_heartbeat("agent1_trader", "OK", portfolio_value=102.34)
"""

import sqlite3
import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── AGENT DB LOCK ──────────────────────────────────────────────────────────
# Agents write a lock file before running so the portal backs off.
# Priority order: agent1 > agent3 > agent4 > agent2 > portal
# The portal waits up to 10 minutes — agents typically finish in 1-2 min.

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_LOCK_FILE = os.path.join(_PROJECT_DIR, '.agent_lock')

# Callers that get DB priority (write the lock file).
# Lower number = higher priority. Agents not listed default to 99 (lowest).
PRIORITY_AGENTS = {
    'retail_trade_logic_agent.py':      1,   # trader   — trade execution
    'retail_market_sentiment_agent.py': 2,   # sentiment — cascade/deterioration
    'retail_news_agent.py':             3,   # research  — signal scoring
    'retail_sector_screener.py':        4,   # screener  — sector enrichment
}

# Callers that back off fast when DB is locked (max 5s wait, never block a request)
BACKOFF_CALLERS = {
    'retail_portal.py',
    'retail_heartbeat.py',
    'retail_health_check.py',
    'daily_digest.py',
}


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

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'user', 'signals.db')

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

-- ── POSITION PREFERENCES ──────────────────────────────────────────────
-- Sticky per-ticker overrides for AUTO/USER tagging. If a row exists with
-- sticky='user', bot never takes positions in this ticker regardless of
-- signal — instead, logs SIGNAL_SKIPPED_STICKY_USER in signal_decisions.
-- sticky='bot' is reserved for a future iteration (see backlog Q2 deferral).
CREATE TABLE IF NOT EXISTS position_preferences (
    ticker          TEXT PRIMARY KEY,
    sticky          TEXT NOT NULL CHECK (sticky IN ('user','bot')),
    set_by          TEXT NOT NULL,         -- 'user' (manual) | 'system' (auto)
    set_at          TEXT NOT NULL
);

-- ── SYSTEM HALT (kill switch v2) ──────────────────────────────────────
-- Singleton row. Two separate halt layers:
--  * Admin halt:     the row in the MASTER customer's signals.db is the
--                    authoritative source. All trader subprocesses read it
--                    via _shared_db(). Applies to every customer.
--  * Customer halt:  the row in that customer's OWN signals.db applies
--                    only to that customer's trader subprocess.
-- Trader's entry-point skip checks both — either one true = skip that
-- customer's run. See HALT_AGENT_REWRITE.md for the full spec.
CREATE TABLE IF NOT EXISTS system_halt (
    id                INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    active            INTEGER NOT NULL DEFAULT 0,           -- boolean 0/1
    reason            TEXT,
    set_by            TEXT,                                 -- operator identifier
    set_at            TEXT,
    expected_return   TEXT                                  -- admin-only optional
);

-- ── SIGNALS ────────────────────────────────────────────────────────────
-- Per-agent stamp ownership (enforce this convention when adding new fields):
--   news_agent:      status (initial QUEUED), interrogation_status, needs_reeval,
--                    staleness, expires_at, corroboration_note
--   market_sentiment: sentiment_score, sentiment_evaluated_at
--   sector_screener:  screener_evaluated_at
--   trade_logic:      status transitions (QUEUED → EVALUATED | ACTED_ON | EXPIRED)
--
-- Rule: no agent reads another agent's tag as a hard SQL filter. If an agent
-- needs another's output, it must be consumed as a scoring input / soft
-- signal, not a gate. See get_queued_signals() docstring.
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
                                           -- Lifecycle (forward-only):
                                           --   PENDING  → WATCHING  → QUEUED
                                           --        ↓                   ↓
                                           --    DISCARDED          VALIDATED
                                           --                           ↓
                                           --          EVALUATED / ACTED_ON / EXPIRED
                                           -- Terminal states: ACTED_ON, DISCARDED,
                                           --   EVALUATED, EXPIRED, INTERRUPTED.
                                           -- Trader reads ONLY status='VALIDATED'.
                                           -- See promote_validated_signals() for the
                                           -- QUEUED → VALIDATED transition gate.
    is_amended      INTEGER NOT NULL DEFAULT 0,
    is_spousal      INTEGER NOT NULL DEFAULT 0,
    needs_reeval    INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT,
    discard_delete_at TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- ── NEWS FLAGS (Phase 2 of TRADER_RESTRUCTURE_PLAN) ────────────────────
-- Durable annotations per ticker written by the news agent. Replaces
-- the "news is the trigger" model: news now contributes as (a) modifier
-- to Gate 5 composite score, (b) veto at Gate 5.5 for severe negatives,
-- and (c) event-risk gating at Gate 4 EVENT_RISK. Trader reads from
-- news_flags when evaluating candidates; news itself no longer
-- originates the trigger (that's the sector-driven candidate
-- generator in Phase 3).
--
-- Phase 2 scope: create table + news agent writes to it. Trader
-- does NOT read yet — that lands in Phase 3.
--
-- Score semantics: -1.0 to +1.0. Positive = bullish / supports entry,
-- Negative = bearish / blocks or exits. |score| represents magnitude
-- (mild 0.3, strong 0.7, severe >0.8).
--
-- fresh_until: ISO timestamp. Category-specific TTLs encoded in
-- write_news_flag() at the caller. Queries filter by fresh_until > now.
CREATE TABLE IF NOT EXISTS news_flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    category        TEXT    NOT NULL,   -- earnings_raise, analyst_upgrade,
                                        -- guidance_raise, breakout, catalyst,
                                        -- earnings_miss, guidance_cut,
                                        -- regulatory_probe, management_change,
                                        -- litigation, other
    score           REAL    NOT NULL,   -- -1.0 to +1.0
    fresh_until     TEXT    NOT NULL,   -- ISO timestamp
    notes           TEXT,
    source_signal_id INTEGER,           -- FK to signals.id if derived from a
                                        -- specific signal; NULL for standalone
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_news_flags_ticker_fresh ON news_flags(ticker, fresh_until);
CREATE INDEX IF NOT EXISTS idx_news_flags_created ON news_flags(created_at);

-- ── TRADE WINDOWS (Phase 3b of TRADER_RESTRUCTURE_PLAN) ────────────────
-- Precomputed entry/exit price windows per candidate, per customer,
-- in two tiers:
--   macro: thesis-level window, recomputed every enrichment tick
--          (30 min). Represents "where we'd like to enter if the
--          thesis holds." Long-lived (valid through end of day).
--   minor: tactical window within the macro, recomputed every
--          trader cycle (~30 s). Represents "right now, this specific
--          price zone is a reasonable entry." Short-lived (2 cycles).
--
-- Trade daemon (Phase 3c) consults `minor` for fire decisions: only
-- acts when live price falls inside [entry_low, entry_high] AND the
-- minor window nests inside the macro (sanity check).
--
-- Phase 3b populates this table but trader does NOT read from it yet —
-- that cutover lands in Phase 3c along with Gate 5 rebalance and trade
-- daemon refactor to window-driven dispatch.
CREATE TABLE IF NOT EXISTS trade_windows (
    signal_id     INTEGER NOT NULL,
    customer_id   TEXT    NOT NULL,
    tier          TEXT    NOT NULL,    -- 'macro' | 'minor'
    entry_low     REAL    NOT NULL,
    entry_high    REAL    NOT NULL,
    stop          REAL    NOT NULL,
    tp            REAL,                -- nullable (optional take-profit)
    computed_at   TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    atr           REAL,                -- ATR_14 at compute time (Phase 4.a)
    PRIMARY KEY (signal_id, customer_id, tier)
);
CREATE INDEX IF NOT EXISTS idx_trade_windows_customer_tier
    ON trade_windows(customer_id, tier, expires_at);

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
-- Supervised mode trade approval queue + overnight-queue holding area.
-- Full lifecycle preserved — rows are never deleted.
-- Active queue: filter WHERE status IN ('PENDING_APPROVAL','QUEUED_FOR_OPEN')
-- Statuses:
--   PENDING_APPROVAL      — supervised-mode trade awaiting user decision
--   QUEUED_FOR_OPEN       — automatic-mode decision made outside market hours;
--                           awaits pre-open re-evaluation before execution
--   APPROVED              — user approved / re-eval passed, ready to execute
--   REJECTED              — user rejected
--   CANCELLED_PROTECTIVE  — pre-open re-eval determined the trade is no longer
--                           valid; shown to user with an explicit reason
--   EXECUTED              — order placed on Alpaca
--   EXPIRED               — queued > max_age_hours, never acted on
-- queue_origin: 'market' (default, generated during market hours) or
--               'overnight' (generated outside hours, needs pre-open re-eval).
CREATE TABLE IF NOT EXISTS pending_approvals (
    id                TEXT    PRIMARY KEY,   -- signal id (e.g. "42" or "sig_AAPL_...")
    ticker            TEXT    NOT NULL,
    company           TEXT,
    sector            TEXT,
    politician        TEXT,
    confidence        TEXT,                  -- HIGH / MEDIUM / LOW
    staleness         TEXT,                  -- Fresh / Aging / Stale / Expired
    headline          TEXT,
    price             REAL,
    shares            REAL,
    max_trade         REAL,
    trail_amt         REAL,
    trail_pct         REAL,
    vol_label         TEXT,
    reasoning         TEXT,
    session           TEXT,
    status            TEXT    NOT NULL DEFAULT 'PENDING_APPROVAL',
    queued_at         TEXT    NOT NULL,
    decided_at        TEXT,
    decided_by        TEXT,                  -- 'portal' or agent name
    executed_at       TEXT,
    decision_note     TEXT,
    queue_origin      TEXT    DEFAULT 'market',  -- 'market' | 'overnight'
    reevaluated_at    TEXT,                      -- when pre-open re-eval ran
    cancelled_reason  TEXT                       -- why CANCELLED_PROTECTIVE fired
);

-- ── MEMBER WEIGHTS ──────────────────────────────────────────────────────
-- Per-member signal reliability scores updated after each trade closes.
CREATE TABLE IF NOT EXISTS member_weights (
    congress_member TEXT    PRIMARY KEY,
    win_count       INTEGER NOT NULL DEFAULT 0,
    loss_count      INTEGER NOT NULL DEFAULT 0,
    weight          REAL    NOT NULL DEFAULT 1.0,  -- floor 0.5, ceiling 1.5
    last_updated    TEXT
);

-- ── NEWS FEED ────────────────────────────────────────────────────────────
-- All signals seen by Scout, good and bad, before Bolt acts.
-- Displayed in portal /news page. Cleared after 30 days by cleanup.
CREATE TABLE IF NOT EXISTS news_feed (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    congress_member  TEXT,
    ticker           TEXT,
    signal_score     TEXT,                  -- adjusted score: HIGH/MEDIUM/LOW/NOISE
    sentiment_score  REAL,
    raw_headline     TEXT,
    metadata         TEXT,                  -- JSON blob
    source           TEXT,                  -- CONGRESS / RSS
    created_at       TEXT    NOT NULL
);

-- ── SECTOR SCREENING ────────────────────────────────────────────────────
-- Candidates identified by the sector screener for review before trading.
-- Each run inserts a fresh set of rows. Portal reads the latest run_id.
-- Status: considering / passed_to_bolt / rejected
CREATE TABLE IF NOT EXISTS sector_screening (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL,      -- ISO timestamp of screener run
    sector              TEXT    NOT NULL,      -- e.g. "Energy"
    etf                 TEXT    NOT NULL,      -- e.g. "XLE"
    etf_5yr_return      REAL,                  -- decimal e.g. 0.42 = +42%
    ticker              TEXT    NOT NULL,
    company             TEXT,
    etf_weight_pct      REAL,                  -- weight in the ETF
    news_signal         TEXT,                  -- "bullish" / "bearish" / "neutral" / "pending"
    news_headline       TEXT,                  -- top headline from Scout
    news_score          REAL,                  -- numeric score from Scout (0-1)
    sentiment_signal    TEXT,                  -- "bullish" / "bearish" / "neutral" / "pending"
    sentiment_score     REAL,                  -- numeric score from Pulse (0-1)
    congressional_flag  TEXT,                  -- "recent_buy" / "recent_sell" / "none"
    combined_score      REAL,                  -- weighted composite (news+sentiment+weight)
    momentum_score      REAL,                  -- per-ticker momentum 0-1 from sector_screener's
                                               -- calc_momentum_score (ret_3m+SMA+volume trend)
                                               -- Primary filter for candidate_generator (2026-04-21+)
    status              TEXT    NOT NULL DEFAULT 'considering',
    notes               TEXT,
    created_at          TEXT    NOT NULL
);

-- ── SCREENING REQUESTS ───────────────────────────────────────────────────
-- Inter-agent request queue. Sector screener writes requests here;
-- Scout and Pulse read pending rows on each run and write results back
-- to sector_screening. Rows are never deleted — archive is useful.
-- Status: pending / fulfilled / failed
CREATE TABLE IF NOT EXISTS screening_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,      -- links back to sector_screening run_id
    requested_by    TEXT    NOT NULL,      -- e.g. "sector_screener"
    ticker          TEXT    NOT NULL,
    request_type    TEXT    NOT NULL,      -- "news" or "sentiment"
    status          TEXT    NOT NULL DEFAULT 'pending',
    created_at      TEXT    NOT NULL,
    fulfilled_at    TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT    NOT NULL,           -- 'system' | 'daily' | 'account' | 'trade' | 'alert'
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    read_at     TEXT,
    meta        TEXT                        -- JSON blob for structured data
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
CREATE INDEX IF NOT EXISTS idx_news_feed_created     ON news_feed(created_at);
CREATE INDEX IF NOT EXISTS idx_news_feed_ticker      ON news_feed(ticker);
CREATE INDEX IF NOT EXISTS idx_screening_run         ON sector_screening(run_id);
CREATE INDEX IF NOT EXISTS idx_screening_ticker      ON sector_screening(ticker);
CREATE INDEX IF NOT EXISTS idx_screen_req_status     ON screening_requests(status);
CREATE INDEX IF NOT EXISTS idx_screen_req_type       ON screening_requests(request_type);
CREATE INDEX IF NOT EXISTS idx_notif_unread          ON notifications(is_read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notif_category        ON notifications(category);


CREATE TABLE IF NOT EXISTS support_tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL UNIQUE,
    category        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    priority        TEXT NOT NULL DEFAULT 'normal',
    beta_test_id    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticket_status ON support_tickets(status);
CREATE INDEX IF NOT EXISTS idx_ticket_category ON support_tickets(category);

CREATE TABLE IF NOT EXISTS support_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    sender          TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES support_tickets(ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_ticket ON support_messages(ticket_id, created_at);

CREATE TABLE IF NOT EXISTS customer_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
        # DEBUG: fires on every DB open (every API request that opens a
        # customer DB). INFO made portal.log unreadably noisy.
        log.debug(f"Schema verified: {self.path}")
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
            # v1.2 — member weight tracking and interrogation fields on signals
            "ALTER TABLE signals ADD COLUMN price_history_used TEXT",
            "ALTER TABLE signals ADD COLUMN interrogation_status TEXT",
            "ALTER TABLE signals ADD COLUMN entry_signal_score TEXT",
            # v1.2 — trade-entry context fields on positions
            "ALTER TABLE positions ADD COLUMN entry_sentiment_score REAL",
            "ALTER TABLE positions ADD COLUMN entry_signal_score TEXT",
            "ALTER TABLE positions ADD COLUMN price_history_used TEXT",
            "ALTER TABLE positions ADD COLUMN interrogation_status TEXT",
            # v1.3 — profit tier tracking for idempotent partial sells
            "ALTER TABLE positions ADD COLUMN last_profit_tier REAL DEFAULT 0.0",
            # v2.0 — customer settings, support, notifications (table creation)
            """CREATE TABLE IF NOT EXISTS customer_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
                subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal', beta_test_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL, sender TEXT NOT NULL,
                message TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES support_tickets(ticket_id))""",
            """CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT, type TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL, message TEXT, category TEXT DEFAULT 'system',
                meta TEXT, read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            # v2.1 — session history for market activity chart
            """CREATE TABLE IF NOT EXISTS session_history (
                ts TEXT PRIMARY KEY, count INTEGER, names TEXT)""",
            # v2.2 — memo field on pending_approvals for admin notes
            "ALTER TABLE pending_approvals ADD COLUMN memo TEXT",
            # v2.2 — image URL on signals for visual display
            "ALTER TABLE signals ADD COLUMN image_url TEXT",
            "ALTER TABLE signals ADD COLUMN source_url TEXT",
            # v3.0 — exit performance tracking for trailing stop optimizer
            """CREATE TABLE IF NOT EXISTS exit_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                sector TEXT,
                vol_bucket TEXT,
                entry_price REAL NOT NULL,
                entry_atr REAL,
                entry_atr_pct REAL,
                entry_multiplier REAL,
                trail_stop_amt_at_entry REAL,
                trail_stop_amt_at_exit REAL,
                exit_price REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL,
                hold_days INTEGER,
                pnl_dollar REAL,
                pnl_pct REAL,
                peak_price_during_hold REAL,
                trough_price_during_hold REAL,
                price_5d_after_exit REAL,
                price_10d_after_exit REAL,
                stop_too_tight INTEGER DEFAULT 0,
                stop_too_loose INTEGER DEFAULT 0,
                optimal_exit_price REAL,
                missed_gain_pct REAL,
                excess_loss_pct REAL,
                realized_vol_pct REAL,
                atr_accuracy REAL,
                atr_trail_multiplier REAL,
                late_day_tighten_pct REAL,
                benchmark_corr_widen REAL,
                benchmark_corr_tighten REAL,
                max_holding_days INTEGER,
                last_profit_tier REAL,
                created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_exit_perf_ticker ON exit_performance(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_exit_perf_bucket ON exit_performance(vol_bucket)",
            "CREATE INDEX IF NOT EXISTS idx_exit_perf_reason ON exit_performance(exit_reason)",
            """CREATE TABLE IF NOT EXISTS optimizer_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                sample_size INTEGER NOT NULL,
                parameters_before TEXT NOT NULL,
                parameters_after TEXT NOT NULL,
                adjustments TEXT NOT NULL,
                applied INTEGER DEFAULT 0,
                created_at TEXT NOT NULL)""",
            # v3.1 — API call tracking for rate limit monitoring
            """CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                agent TEXT NOT NULL,
                service TEXT NOT NULL DEFAULT 'alpaca',
                endpoint TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'GET',
                customer_id TEXT,
                status_code INTEGER)""",
            "CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_api_calls_agent ON api_calls(agent)",
            # v3.2 — ticker → sector cache populated by retail_sector_backfill_agent
            """CREATE TABLE IF NOT EXISTS ticker_sectors (
                ticker      TEXT PRIMARY KEY,
                sector      TEXT NOT NULL,
                industry    TEXT,
                source      TEXT,
                confidence  TEXT,
                updated_at  TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_ticker_sectors_updated ON ticker_sectors(updated_at)",
            # v3.3 — notification deduplication (suppress repeated 'Account Ready',
            # 'Session Complete', etc.). Null dedup_key = legacy blind insert.
            "ALTER TABLE notifications ADD COLUMN dedup_key TEXT",
            "CREATE INDEX IF NOT EXISTS idx_notif_dedup ON notifications(dedup_key, created_at DESC)",
            # v3.4 — incremental-fetch cursors (news agent, others).
            # One row per data source. cursor_value is a source-specific string
            # (ISO-8601 timestamp for news; could be a page token for others).
            """CREATE TABLE IF NOT EXISTS fetch_cursors (
                source_name   TEXT    PRIMARY KEY,
                cursor_value  TEXT    NOT NULL,
                articles_seen INTEGER NOT NULL DEFAULT 0,
                updated_at    TEXT    NOT NULL)""",
            # v3.6 — per-agent stamps on signals. Each processing agent writes
            # its own completion marker. News stamps via interrogation_status.
            # Sentiment stamps via sentiment_score + sentiment_evaluated_at.
            # Screener stamps via screener_evaluated_at.
            # Macro/market_state/validator stamp system-wide snapshots.
            # A dedicated promote_validated_signals() step (run after the
            # enrichment pipeline, before trader dispatch) checks required
            # stamps and transitions QUEUED → VALIDATED. Trader reads ONLY
            # status='VALIDATED' — the unvalidated pool is invisible.
            "ALTER TABLE signals ADD COLUMN sentiment_score REAL",
            "ALTER TABLE signals ADD COLUMN sentiment_evaluated_at TEXT",
            "ALTER TABLE signals ADD COLUMN screener_evaluated_at TEXT",
            "ALTER TABLE signals ADD COLUMN macro_regime_at_validation TEXT",
            "ALTER TABLE signals ADD COLUMN market_state_at_validation TEXT",
            "ALTER TABLE signals ADD COLUMN validator_stamped_at TEXT",

            # v3.5 — admin alerts. System-health / validator / bias findings go
            # here instead of per-customer notifications. Customers never see
            # these; the admin portal reads and resolves them.
            """CREATE TABLE IF NOT EXISTS admin_alerts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                category       TEXT    NOT NULL,    -- 'validator'|'fault'|'bias'|'system'
                severity       TEXT    NOT NULL,    -- 'CRITICAL'|'WARNING'|'INFO'
                source_agent   TEXT,                -- which agent raised it
                source_customer_id TEXT,            -- null if system-wide
                code           TEXT,                -- short identifier (e.g. STALE_HEARTBEAT_NEWS)
                title          TEXT    NOT NULL,
                body           TEXT    NOT NULL DEFAULT '',
                meta           TEXT,                -- JSON blob
                resolved       INTEGER NOT NULL DEFAULT 0,
                resolved_at    TEXT,
                created_at     TEXT    NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_admin_alerts_resolved ON admin_alerts(resolved, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_admin_alerts_severity ON admin_alerts(severity)",

            # v3.7 — per-agent signal decision log. Every stamp/promotion/skip
            # writes one row so we can replay the validation chain for any
            # signal or cycle. The stamp_signals_* methods record batch-level
            # events; the promoter records per-signal PROMOTED/STUCK events
            # with the set of missing stamps on stuck rows.
            """CREATE TABLE IF NOT EXISTS signal_decisions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,           -- ISO UTC
                cycle_id   TEXT,                       -- groups events from one enrichment cycle
                agent      TEXT    NOT NULL,           -- 'news'|'sentiment'|'screener'|'macro'|'market_state'|'validator'|'promoter'
                action     TEXT    NOT NULL,           -- 'STAMPED_BATCH'|'STAMPED_TICKER'|'PROMOTED'|'STUCK'|'SKIPPED'
                ticker     TEXT,                       -- null for system-wide bulk ops
                signal_id  INTEGER,                    -- null for batch-level rows
                value      TEXT,                       -- e.g. '0.60', 'EXPANSION', 'QUEUED→VALIDATED'
                reason     TEXT,                       -- human-readable
                meta       TEXT                        -- optional JSON
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sd_cycle    ON signal_decisions(cycle_id)",
            "CREATE INDEX IF NOT EXISTS idx_sd_agent    ON signal_decisions(agent)",
            "CREATE INDEX IF NOT EXISTS idx_sd_signal   ON signal_decisions(signal_id)",
            "CREATE INDEX IF NOT EXISTS idx_sd_ticker   ON signal_decisions(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_sd_ts       ON signal_decisions(ts DESC)",

            # v3.8 — overnight queue + pre-open re-evaluation extension to
            # pending_approvals. Decisions generated outside market hours
            # land here as QUEUED_FOR_OPEN, get re-evaluated at the next
            # market open, and either execute or flip to CANCELLED_PROTECTIVE
            # with a visible reason. See docs/overnight_queue_plan.md.
            "ALTER TABLE pending_approvals ADD COLUMN queue_origin TEXT DEFAULT 'market'",
            "ALTER TABLE pending_approvals ADD COLUMN reevaluated_at TEXT",
            "ALTER TABLE pending_approvals ADD COLUMN cancelled_reason TEXT",
            "CREATE INDEX IF NOT EXISTS idx_approvals_queue_origin ON pending_approvals(queue_origin, status)",

            # v3.9 — screener_score on signals. screener_evaluated_at
            # previously carried a timestamp-only "this ticker was a top
            # candidate" meaning, which stamped only 5-10 signals per
            # cycle out of 40+ in-flight — 80% of validated signals had
            # no sector-quality stamp. Adding a score column so every
            # in-flight signal can be sector-scored (top candidates get
            # their momentum score, remaining get a sector baseline)
            # closes that gap and lets the trader lift the stamp from
            # a duplicative boolean bonus to a proportional ranking input.
            "ALTER TABLE signals ADD COLUMN screener_score REAL",
            # v3.10 — AUTO/USER per-position tagging. Default 'bot' so all
            # existing positions treat as bot-managed (pre-feature behavior).
            # Check constraint not applicable via ALTER TABLE in SQLite; the
            # application enforces the 'bot'|'user' domain (retail_trade_logic_agent
            # + retail_portal). New positions inserted post-migration should set
            # managed_by explicitly per the who-initiated-the-buy rule.
            "ALTER TABLE positions ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'bot'",

            # Phase 2 of TRADER_RESTRUCTURE_PLAN (2026-04-20) — news_flags
            # durable-annotation table. See the SCHEMA section for the full
            # rationale. Migration is additive only — never drops existing
            # columns or data. Idempotent via IF NOT EXISTS.
            """CREATE TABLE IF NOT EXISTS news_flags (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                category        TEXT    NOT NULL,
                score           REAL    NOT NULL,
                fresh_until     TEXT    NOT NULL,
                notes           TEXT,
                source_signal_id INTEGER,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_news_flags_ticker_fresh ON news_flags(ticker, fresh_until)",
            "CREATE INDEX IF NOT EXISTS idx_news_flags_created ON news_flags(created_at)",

            # trade_windows table — originally populated by
            # retail_window_calculator.py (Phase 3b of TRADER_RESTRUCTURE_PLAN).
            # DORMANT since 2026-04-24: window_calculator was removed along
            # with the trader's window-driven pre-filter — trader now reads
            # VALIDATED signals directly and relies on Gate 6 chase caps
            # for anchor-based entry protection. Schema + helper methods
            # (write_trade_window, get_fresh_macro_windows,
            # expire_stale_trade_windows, get_windows_for_signal) retained
            # for historical audit rows and in case the pre-filter needs
            # to be resurrected. No active writers.
            """CREATE TABLE IF NOT EXISTS trade_windows (
                signal_id     INTEGER NOT NULL,
                customer_id   TEXT    NOT NULL,
                tier          TEXT    NOT NULL,
                entry_low     REAL    NOT NULL,
                entry_high    REAL    NOT NULL,
                stop          REAL    NOT NULL,
                tp            REAL,
                computed_at   TEXT    NOT NULL,
                expires_at    TEXT    NOT NULL,
                PRIMARY KEY (signal_id, customer_id, tier)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_trade_windows_customer_tier ON trade_windows(customer_id, tier, expires_at)",

            # Phase 4.a of TRADER_RESTRUCTURE_PLAN (2026-04-20) — attach
            # ATR_14 to each window row. Nullable so rows written before
            # window_calculator starts populating it stay valid; fresh
            # computes after this migration always fill it.
            "ALTER TABLE trade_windows ADD COLUMN atr REAL",

            # Phase 5.a of TRADER_RESTRUCTURE_PLAN (2026-04-20) — event
            # calendar. earnings_cache is per-ticker (TTL 7 days, refreshed
            # by the event_calendar module); macro_events is a manual
            # schedule (FOMC/CPI) admin-populated or auto-updated on a
            # separate cadence.
            """CREATE TABLE IF NOT EXISTS earnings_cache (
                ticker          TEXT PRIMARY KEY,
                next_earnings   TEXT,           -- ISO date or NULL if none known
                fetched_at      TEXT NOT NULL,
                expires_at      TEXT NOT NULL,
                source          TEXT DEFAULT 'yahoo'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_earnings_cache_expires ON earnings_cache(expires_at)",
            """CREATE TABLE IF NOT EXISTS macro_events (
                event_date      TEXT NOT NULL,  -- ISO date
                event_type      TEXT NOT NULL,  -- 'FOMC' | 'CPI' | 'NFP' | ...
                notes           TEXT,
                PRIMARY KEY (event_date, event_type)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_macro_events_date ON macro_events(event_date)",

            # Phase 5.b of TRADER_RESTRUCTURE_PLAN (2026-04-20) — cooling
            # off. When a position closes at a loss, register the ticker
            # with a cool_until timestamp. Gate 4 reads this and blocks
            # re-entry until the window elapses, preventing stop → re-buy
            # → stop chop patterns.
            """CREATE TABLE IF NOT EXISTS cooling_off (
                ticker      TEXT PRIMARY KEY,
                cool_until  TEXT NOT NULL,
                reason      TEXT,
                pnl_pct     REAL,
                created_at  TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_cooling_off_until ON cooling_off(cool_until)",
            # Audit Round 9.4 — composite index covering is_cooling_off()'s
            # WHERE clause which filters on both ticker AND cool_until.
            # The single-column idx_cooling_off_until only helps if ticker
            # is the leading column; this composite covers both filter columns.
            "CREATE INDEX IF NOT EXISTS idx_cooling_off_ticker_until ON cooling_off(ticker, cool_until)",

            # R10-10 — get_pending_approvals() filters on `status IN (...)`
            # and orders by queued_at; the single-column indexes on each
            # make the planner pick one and still scan-and-sort. Composite
            # covers both, eliminating the sort. Portal hits this on every
            # dashboard render.
            "CREATE INDEX IF NOT EXISTS idx_approvals_status_queued ON pending_approvals(status, queued_at)",

            # 2026-04-21 — momentum_score is now the primary filter column
            # for candidate_generator (was combined_score which mixed news,
            # sentiment, and ETF weight but omitted the per-ticker momentum
            # from sector_screener's own calc_momentum_score). Null for
            # historical rows; filled in on all new screener runs.
            "ALTER TABLE sector_screening ADD COLUMN momentum_score REAL",

            # Audit Round 5 (2026-04-20) — tradable-asset cache. Populated
            # daily by retail_tradable_cache.refresh() from Alpaca's
            # /v2/assets endpoint. Candidate Generator reads via
            # is_tradable() to skip un-tradable tickers. Created here so
            # is_tradable never fails on 'no such table' before the first
            # refresh has run.
            """CREATE TABLE IF NOT EXISTS tradable_assets (
                ticker       TEXT PRIMARY KEY,
                exchange     TEXT,
                asset_class  TEXT,
                tradable     INTEGER NOT NULL DEFAULT 1,
                fetched_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_tradable_expires ON tradable_assets(expires_at)",

            # Audit Round 6 (2026-04-20) — news-agent upsert_signal()
            # dedup hot path queries `WHERE ticker=? AND tx_date=? AND
            # status NOT IN (...)`. Previously covered by the separate
            # single-column idx_signals_ticker / idx_signals_status
            # (SQLite uses at most one), forcing a ticker-scan then
            # filter. Composite index lets the whole predicate resolve
            # against the index directly.
            "CREATE INDEX IF NOT EXISTS idx_signals_ticker_txdate_status ON signals(ticker, tx_date, status)",

            # Audit Round 7.1 (2026-04-20) — entry_signal_score was
            # declared TEXT and written as f"{x:.4f}". Writers now pass
            # floats directly (SQLite dynamic typing accepts either).
            # One-shot: cast all pre-existing string values to REAL so
            # ORDER BY / comparisons use numeric semantics. Idempotent
            # because CAST on an already-REAL value is a no-op.
            "UPDATE signals   SET entry_signal_score = CAST(entry_signal_score AS REAL) WHERE entry_signal_score IS NOT NULL AND typeof(entry_signal_score) = 'text'",
            "UPDATE positions SET entry_signal_score = CAST(entry_signal_score AS REAL) WHERE entry_signal_score IS NOT NULL AND typeof(entry_signal_score) = 'text'",

            # 2026-04-21 — news-attribution patch.
            # `name` column on tradable_assets: captures the company name
            # from Alpaca's /v2/assets endpoint so the news agent can
            # validate that ticker tags in Benzinga articles actually
            # match the headline. Populated by retail_tradable_cache.py.
            "ALTER TABLE tradable_assets ADD COLUMN name TEXT",

            # `signal_attribution_flags` table — audit log of suspicious
            # ticker assignments from Alpaca's news feed. Written by the
            # news agent during _alpaca_article_to_item(). Reasons:
            #   'untradable'    — all tagged symbols non-tradable / crypto (Fix A, enforced)
            #   'remap_differs' — re-ranker would pick a different ticker (Fix C, shadow)
            #   'conflict'      — 2+ tagged symbols tie on headline score
            #   'no_match'      — zero tagged symbols match the headline
            # 90-day retention (cleanup migration below).
            """CREATE TABLE IF NOT EXISTS signal_attribution_flags (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                headline        TEXT NOT NULL,
                article_url     TEXT,
                alpaca_symbols  TEXT NOT NULL,
                chosen_ticker   TEXT,
                would_choose    TEXT,
                reason          TEXT NOT NULL,
                tie_candidates  TEXT,
                best_score      INTEGER,
                resolved        INTEGER NOT NULL DEFAULT 0,
                resolution_note TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_attrib_flags_created ON signal_attribution_flags(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_attrib_flags_reason  ON signal_attribution_flags(reason, resolved)",

            # Retention sweep — keep the flag table bounded. Runs every
            # time a DB is opened. At the expected ~30-40 flags/day the
            # table stays under ~4k rows at steady state.
            "DELETE FROM signal_attribution_flags WHERE created_at < datetime('now','-90 days')",
        ]

        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        for sql in MIGRATIONS:
            try:
                c.execute(sql)
                c.commit()
                # Demoted to DEBUG: CREATE TABLE IF NOT EXISTS and CREATE INDEX
                # IF NOT EXISTS silently succeed even when the object already
                # exists, so logging at INFO fires every time the DB is opened.
                # Portal DB opens happen on every API call → this single line
                # was writing 25 entries × every request = 223 MB/day of noise.
                log.debug(f"Migration applied: {sql[:60]}")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass  # column already exists — skip silently
                else:
                    log.warning(f"Migration skipped ({sql[:40]}): {e}")
        c.close()

    def now(self):
        """Return the current UTC time as a naive-looking string.
        All DB timestamps are UTC as of the unification. SQLite comparisons
        like datetime('now','-X hours') are UTC by default, so this now
        matches consistently on both sides of age queries."""
        return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    # ── PORTFOLIO ──────────────────────────────────────────────────────────

    def get_portfolio(self):
        """Returns current portfolio row or seeds it with starting capital.
        Uses INSERT OR IGNORE + unique id=1 to prevent duplicate rows from race conditions."""
        with self.conn() as c:
            # Clean up duplicates if they exist (legacy fix)
            count = c.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
            if count > 1:
                log.warning(f"Portfolio has {count} rows — cleaning duplicates, keeping id=1")
                c.execute("DELETE FROM portfolio WHERE id != (SELECT MIN(id) FROM portfolio)")
            row = c.execute("SELECT * FROM portfolio ORDER BY id LIMIT 1").fetchone()
            if row:
                return dict(row)
            # Cold start — seed with starting capital
            starting = float(os.environ.get('STARTING_CAPITAL', 100000.0))
            c.execute("""
                INSERT OR IGNORE INTO portfolio (id, cash, realized_gains, tax_withdrawn, month_start, updated_at)
                VALUES (1, ?, 0.0, 0.0, ?, ?)
            """, (starting, starting, self.now()))
            log.info(f"Cold start — portfolio seeded at ${starting:.2f}")
            return {
                'id': 1, 'cash': starting, 'realized_gains': 0.0,
                'tax_withdrawn': 0.0, 'month_start': starting, 'updated_at': self.now()
            }

    def update_portfolio(self, cash=None, realized_gains=None, tax_withdrawn=None, month_start=None):
        """Partial update — only changes fields you pass.

        Audit Round 9.2 — the portfolio read and the UPDATE are now inside a
        single transaction so concurrent callers (sweep_monthly_tax, admin
        adjust, etc.) can't race-read the current values before either write
        lands. Previously get_portfolio() and the UPDATE were in separate
        connections."""
        with self.conn() as c:
            pf_row = c.execute(
                "SELECT id, cash, realized_gains, tax_withdrawn, month_start "
                "FROM portfolio WHERE id=1"
            ).fetchone()
            if not pf_row:
                raise RuntimeError("portfolio row missing — DB not initialized")
            c.execute("""
                UPDATE portfolio SET
                    cash            = ?,
                    realized_gains  = ?,
                    tax_withdrawn   = ?,
                    month_start     = ?,
                    updated_at      = ?
                WHERE id = ?
            """, (
                cash            if cash            is not None else float(pf_row['cash']),
                realized_gains  if realized_gains  is not None else float(pf_row['realized_gains']),
                tax_withdrawn   if tax_withdrawn    is not None else float(pf_row['tax_withdrawn']),
                month_start     if month_start      is not None else pf_row['month_start'],
                self.now(),
                pf_row['id'],
            ))

    def sweep_monthly_tax(self, tax_amount):
        """
        Sweep 10% of gains at month end.
        Reduces cash, logs to ledger, resets realized_gains.

        R10-2 — portfolio read + UPDATE + ledger INSERT all inside a single
        transaction. Previously get_portfolio() and update_portfolio() ran
        in separate connections; two concurrent sweeps could both read
        cash=X and each write cash=X-tax_amount, losing one deduction.
        Monthly cadence made the race rare but cash-destroying when it hit.
        """
        with self.conn() as c:
            pf = c.execute(
                "SELECT cash, tax_withdrawn FROM portfolio WHERE id=1"
            ).fetchone()
            if not pf:
                raise RuntimeError("portfolio row missing — DB not initialized")
            new_cash      = round(float(pf['cash']) - tax_amount, 2)
            new_withdrawn = round(float(pf['tax_withdrawn']) + tax_amount, 2)

            c.execute("""
                UPDATE portfolio SET
                    cash            = ?,
                    tax_withdrawn   = ?,
                    realized_gains  = 0.0,
                    month_start     = ?,
                    updated_at      = ?
                WHERE id = 1
            """, (new_cash, new_withdrawn, new_cash, self.now()))

            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now(timezone.utc).strftime('%Y-%m-%d'), "TAX",
                  "10% monthly gain sweep", -tax_amount, new_cash, None, self.now()))

        log.info(f"Tax sweep: ${tax_amount:.2f} — new cash: ${new_cash:.2f}")

    # ── POSITIONS ──────────────────────────────────────────────────────────

    def get_open_positions(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_closed_positions(self, limit=200):
        """Return closed positions newest-first for performance summary."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def open_position(self, ticker, company, sector, entry_price, shares,
                      trail_stop_amt, trail_stop_pct, vol_bucket, signal_id=None,
                      entry_sentiment_score=None, entry_signal_score=None,
                      interrogation_status=None, price_history_used=None,
                      managed_by='bot'):
        """
        Opens a new position. Also deducts cost from portfolio cash
        and writes a ledger entry.

        managed_by: 'bot' (default — trader's own buys) or 'user' (adopted
        from a direct Alpaca purchase). Trader skips buy/sell/stop logic on
        'user' rows; sticky preferences on a ticker further harden this.
        If sticky='user' is set for this ticker, caller should normally NOT
        be here — but if it gets called anyway, the trader's signal-eval
        path already blocks sticky-user tickers upstream; we don't second-
        guess the caller here.
        """
        if managed_by not in ('bot', 'user'):
            raise ValueError(f"managed_by must be 'bot' or 'user', got {managed_by!r}")
        pos_id   = f"pos_{ticker}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        cost     = round(entry_price * shares, 2)

        # R10-1 — portfolio read + position INSERT + portfolio UPDATE +
        # ledger INSERT all happen in a single transaction. Previously the
        # get_portfolio() read and the update_portfolio() write were in
        # separate connections: two concurrent opens could each read cash=X,
        # each insert their position, then each write cash=X-their_cost,
        # losing one position's cash impact. Same fix pattern as R9-1/R9-3.
        with self.conn() as c:
            pf = c.execute(
                "SELECT cash, realized_gains, tax_withdrawn, month_start "
                "FROM portfolio WHERE id=1"
            ).fetchone()
            if not pf:
                raise RuntimeError("portfolio row missing — DB not initialized")
            new_cash = round(float(pf['cash']) - cost, 2)

            c.execute("""
                INSERT INTO positions
                    (id, ticker, company, sector, entry_price, current_price,
                     shares, trail_stop_amt, trail_stop_pct, vol_bucket,
                     pnl, status, opened_at, signal_id,
                     entry_sentiment_score, entry_signal_score,
                     interrogation_status, price_history_used, managed_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,0.0,'OPEN',?,?,?,?,?,?,?)
            """, (pos_id, ticker, company, sector, entry_price, entry_price,
                  shares, trail_stop_amt, trail_stop_pct, vol_bucket,
                  self.now(), signal_id,
                  entry_sentiment_score, entry_signal_score,
                  interrogation_status, price_history_used, managed_by))

            c.execute("""
                UPDATE portfolio SET cash=?, updated_at=? WHERE id=1
            """, (new_cash, self.now()))

            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now(timezone.utc).strftime('%Y-%m-%d'), "ENTRY",
                  f"{ticker} · {shares:.4f} sh @ ${entry_price:.2f}",
                  -cost, new_cash, pos_id, self.now()))

        log.info(f"Opened position: {ticker} {shares:.4f}sh @ ${entry_price:.2f} — cost ${cost:.2f}")
        return pos_id

    # ── POSITION PREFERENCES (sticky AUTO/USER per ticker) ────────────────

    def get_ticker_sticky(self, ticker: str) -> str | None:
        """Return the sticky override for a ticker, or None if no preference set.
        Possible return values: 'user' (sticky — bot never takes), 'bot' (reserved
        for future use — currently unused in v1)."""
        with self.conn() as c:
            row = c.execute(
                "SELECT sticky FROM position_preferences WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        return row['sticky'] if row else None

    def set_ticker_sticky(self, ticker: str, sticky: str | None, set_by: str = 'user') -> None:
        """Set (or clear when sticky is None) a ticker's sticky preference.
        set_by: 'user' (operator clicked the lock icon) or 'system' (auto-set)."""
        if sticky is not None and sticky not in ('user', 'bot'):
            raise ValueError(f"sticky must be 'user', 'bot', or None — got {sticky!r}")
        with self.conn() as c:
            if sticky is None:
                c.execute("DELETE FROM position_preferences WHERE ticker = ?", (ticker,))
            else:
                c.execute(
                    "INSERT INTO position_preferences (ticker, sticky, set_by, set_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(ticker) DO UPDATE SET "
                    "sticky=excluded.sticky, set_by=excluded.set_by, set_at=excluded.set_at",
                    (ticker, sticky, set_by, self.now()),
                )

    def set_position_managed_by(self, pos_id: str, managed_by: str) -> None:
        """Flip a position's AUTO/USER tag. Called by the portal's toggle endpoint."""
        if managed_by not in ('bot', 'user'):
            raise ValueError(f"managed_by must be 'bot' or 'user', got {managed_by!r}")
        with self.conn() as c:
            c.execute(
                "UPDATE positions SET managed_by = ? WHERE id = ?",
                (managed_by, pos_id),
            )

    # ── SYSTEM HALT (kill switch v2) ──────────────────────────────────────

    def get_halt(self) -> dict:
        """Return this DB's halt row as a dict, or an 'inactive' placeholder.
        Keys: active (bool), reason (str|None), set_by (str|None),
              set_at (iso str|None), expected_return (iso str|None)."""
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT active, reason, set_by, set_at, expected_return "
                    "FROM system_halt WHERE id = 1"
                ).fetchone()
        except Exception as _e:
            log.debug(f"get_halt suppressed: {_e}")
            row = None
        if not row:
            return {'active': False, 'reason': None, 'set_by': None,
                    'set_at': None, 'expected_return': None}
        return {
            'active':          bool(row['active']),
            'reason':          row['reason'],
            'set_by':          row['set_by'],
            'set_at':          row['set_at'],
            'expected_return': row['expected_return'],
        }

    def set_halt(self, active: bool, reason: 'str | None' = None,
                 set_by: str = 'unknown',
                 expected_return: 'str | None' = None) -> None:
        """Set this DB's halt state. Upsert the singleton row.
        Clearing halt (active=False): reason/set_by still recorded for audit."""
        now = self.now()
        with self.conn() as c:
            c.execute(
                "INSERT INTO system_halt "
                "(id, active, reason, set_by, set_at, expected_return) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "active=excluded.active, reason=excluded.reason, "
                "set_by=excluded.set_by, set_at=excluded.set_at, "
                "expected_return=excluded.expected_return",
                (1 if active else 0, reason, set_by, now, expected_return),
            )

    # ── NEWS FLAGS (Phase 2 of TRADER_RESTRUCTURE_PLAN) ───────────────────
    # Category → default TTL in days. Keep the longest TTL on the most
    # persistent categories (regulatory probes linger; breakouts go stale
    # fast). Adjust as real-world signal decay gets measured.
    NEWS_FLAG_TTL_DAYS = {
        # Positive
        'earnings_raise':   5,
        'analyst_upgrade':  3,
        'guidance_raise':   7,
        'breakout':         2,
        'catalyst':         3,
        # Negative
        'earnings_miss':    10,
        'guidance_cut':     14,
        'regulatory_probe': 30,
        'management_change':14,
        'litigation':       30,
        # Fallback
        'other':            2,
    }

    def write_news_flag(self, ticker, category, score,
                        notes=None, source_signal_id=None, ttl_days=None):
        """
        Write a news_flags row. Phase 2 primitive — news agent calls this
        after classifying an event.

        Args:
            ticker: upper-case symbol
            category: one of NEWS_FLAG_TTL_DAYS keys (or 'other' as fallback)
            score: -1.0 to +1.0 (sign = direction, magnitude = strength)
            notes: optional human-readable explanation
            source_signal_id: FK to signals.id if derived from a specific signal
            ttl_days: override the default TTL for this category

        Returns: the new row's id.
        """
        if ttl_days is None:
            ttl_days = self.NEWS_FLAG_TTL_DAYS.get(category, self.NEWS_FLAG_TTL_DAYS['other'])
        # fresh_until = now + ttl_days
        fresh_until = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO news_flags "
                "(ticker, category, score, fresh_until, notes, source_signal_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ticker.upper(), category, float(score), fresh_until, notes, source_signal_id),
            )
            return cur.lastrowid

    def get_fresh_news_flags_for_ticker(self, ticker):
        """
        Return all non-expired news_flags for a given ticker, newest first.
        Trader (Phase 3) will call this at Gate 4 EVENT_RISK, Gate 5
        composite, and Gate 5.5 VETO.

        Filters fresh_until > now. Silently skips expired rows — caller
        doesn't need to think about TTL.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, ticker, category, score, fresh_until, notes, "
                "source_signal_id, created_at "
                "FROM news_flags WHERE ticker = ? AND fresh_until > ? "
                "ORDER BY created_at DESC",
                (ticker.upper(), now),
            ).fetchall()
            return [dict(r) for r in rows]

    def expire_stale_news_flags(self):
        """
        Housekeeping — prune news_flags rows whose fresh_until has passed.
        Intended for the enrichment daemon's periodic tick. Returns the
        count of rows deleted.

        We DELETE rather than mark-as-expired because the archivist
        already captures any audit trail we'd need via its row-archival
        pass over outcomes + system_log.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM news_flags WHERE fresh_until <= ?", (now,)
            )
            return cur.rowcount

    # ── TRADE WINDOWS (Phase 3b of TRADER_RESTRUCTURE_PLAN) ───────────────
    # Window TTL defaults. Macro = end-of-day-ish so enrichment tick
    # (every 30 min) always finds a valid macro row to work from. Minor =
    # 2 trade-daemon cycles at 30s each = 60s, with a small grace buffer.
    WINDOW_TTL_SECONDS = {
        'macro': 8 * 3600,     # 8h — covers 9:30 → 16:00 + morning pre-comp
        'minor': 90,           # 1.5 min — ~2 trade daemon cycles at 30s
    }

    def write_trade_window(self, signal_id, customer_id, tier,
                           entry_low, entry_high, stop, tp=None,
                           ttl_seconds=None, atr=None):
        """
        Write a window row. Primary key is (signal_id, customer_id, tier).

        Tier-specific semantics (Audit Round 4 — anchor-pinning fix):
          - 'macro': INSERT-only. Once a macro band is written for a
            signal+customer, subsequent writes are NO-OPs. This gives
            the intended "wait for a real pullback into the entry
            band" behavior — without pinning, every enrichment tick
            recomputed the band around current price and the trader
            effectively fired on any in-flight signal.
          - 'minor': upsert. Short-TTL (90s) refire zone; designed to
            track current price on every enrichment pass.

        Args:
            signal_id: FK to signals.id
            customer_id: per-customer
            tier: 'macro' | 'minor'
            entry_low / entry_high: price range where entry is acceptable
            stop: stop-loss level
            tp: optional take-profit level
            ttl_seconds: override default TTL for this tier
            atr: ATR_14 at compute time (Phase 4.a)
        """
        if tier not in ('macro', 'minor'):
            raise ValueError(f"tier must be 'macro' or 'minor', got {tier!r}")
        if ttl_seconds is None:
            ttl_seconds = self.WINDOW_TTL_SECONDS[tier]
        now_dt = datetime.now(timezone.utc)
        computed_at = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()

        if tier == 'macro':
            # Pin the macro band: INSERT OR IGNORE. Stale-by-TTL rows
            # still re-insert because expire_stale_trade_windows() has
            # deleted them, so the PK is free.
            sql = (
                "INSERT OR IGNORE INTO trade_windows "
                "(signal_id, customer_id, tier, entry_low, entry_high, "
                "stop, tp, computed_at, expires_at, atr) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
        else:
            # Minor: upsert — refire zone follows current price
            sql = (
                "INSERT INTO trade_windows "
                "(signal_id, customer_id, tier, entry_low, entry_high, "
                "stop, tp, computed_at, expires_at, atr) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(signal_id, customer_id, tier) DO UPDATE SET "
                "entry_low=excluded.entry_low, entry_high=excluded.entry_high, "
                "stop=excluded.stop, tp=excluded.tp, "
                "computed_at=excluded.computed_at, expires_at=excluded.expires_at, "
                "atr=excluded.atr"
            )
        with self.conn() as c:
            c.execute(
                sql,
                (int(signal_id), customer_id, tier,
                 float(entry_low), float(entry_high), float(stop),
                 None if tp is None else float(tp),
                 computed_at, expires_at,
                 None if atr is None else float(atr)),
            )

    def get_windows_for_signal(self, signal_id, customer_id):
        """Return {'macro': row, 'minor': row} for the given signal+customer.
        Missing tier returns None. Fresh_until filter applied — stale rows
        excluded from the result."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            rows = c.execute(
                "SELECT tier, entry_low, entry_high, stop, tp, "
                "computed_at, expires_at, atr "
                "FROM trade_windows "
                "WHERE signal_id = ? AND customer_id = ? AND expires_at > ?",
                (int(signal_id), customer_id, now),
            ).fetchall()
        out = {'macro': None, 'minor': None}
        for r in rows:
            out[r['tier']] = dict(r)
        return out

    def get_fresh_minor_windows(self, customer_id):
        """
        Used by trade daemon (Phase 3c) to find candidates ready to fire.
        Returns all non-expired minor rows for the given customer.
        Caller then joins to signals + live_prices to check entry bands.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            rows = c.execute(
                "SELECT signal_id, entry_low, entry_high, stop, tp, "
                "computed_at, expires_at, atr "
                "FROM trade_windows "
                "WHERE customer_id = ? AND tier = 'minor' AND expires_at > ?",
                (customer_id, now),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_fresh_macro_windows(self, customer_id):
        """
        Used by trader (Phase 3c.b) as the entry filter. Returns all
        non-expired macro rows for the given customer. Caller joins to
        signals (master) + live_prices to find tickers currently within
        their entry band, then runs the 13-gate evaluation.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            rows = c.execute(
                "SELECT signal_id, entry_low, entry_high, stop, tp, "
                "computed_at, expires_at, atr "
                "FROM trade_windows "
                "WHERE customer_id = ? AND tier = 'macro' AND expires_at > ?",
                (customer_id, now),
            ).fetchall()
        return [dict(r) for r in rows]

    def expire_stale_trade_windows(self):
        """Housekeeping — prune expired window rows. Returns count deleted."""
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM trade_windows WHERE expires_at <= ?", (now,)
            )
            return cur.rowcount

    # ── CANDIDATE SIGNALS (Phase 3b of TRADER_RESTRUCTURE_PLAN) ───────────
    # Separate from upsert_signal() so we don't entangle candidate-source
    # flow with news-source flow. Candidates enter the signals table with
    # source='candidate', status='WATCHING' — explicitly NOT 'VALIDATED'.
    # Trader's existing query (status='VALIDATED') skips them naturally
    # until Phase 3c's cutover makes candidates a trader-visible source.
    def add_candidate_signal(self, ticker, combined_score,
                             sector=None, headline='sector-driven candidate',
                             ttl_days=2):
        """
        Insert a sector-driven candidate signal. Returns the new signal id,
        or None if a matching candidate already exists today.

        Dedup: one candidate per ticker per day, keyed on source='candidate'
        and tx_date=today. Subsequent calls for the same ticker today
        update the combined_score (treated as entry_signal_score) rather
        than inserting a duplicate.
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # Round 7.1: store as REAL (SQLite's dynamic typing accepts a
        # float in a TEXT-affinity column; comparisons then use numeric
        # semantics). Previously used f"{x:.4f}" which forced lexicographic
        # sort on SELECT ... ORDER BY entry_signal_score.
        score_num = round(float(combined_score), 4)
        with self.conn() as c:
            existing = c.execute(
                "SELECT id FROM signals "
                "WHERE ticker = ? AND source = 'candidate' AND tx_date = ? "
                "LIMIT 1",
                (ticker, today),
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE signals SET entry_signal_score = ?, updated_at = ? "
                    "WHERE id = ?",
                    (score_num, self.now(), existing['id']),
                )
                return None
            expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).strftime(
                '%Y-%m-%d %H:%M:%S'
            )
            discard_del = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
                '%Y-%m-%d %H:%M:%S'
            )
            # IMPORTANT: tx_date is what the dedup SELECT above filters
            # on. Earlier versions omitted it, so tx_date stayed NULL,
            # and the per-day dedup silently never matched — every run
            # emitted a fresh signal for the same ticker.
            cur = c.execute(
                "INSERT INTO signals "
                "(ticker, company, sector, source, source_tier, headline, "
                "confidence, staleness, corroborated, status, "
                "tx_date, expires_at, discard_delete_at, "
                "created_at, updated_at, entry_signal_score) "
                "VALUES (?, ?, ?, 'candidate', 2, ?, "
                "'MEDIUM', 'Fresh', 0, 'WATCHING', "
                "?, ?, ?, ?, ?, ?)",
                (ticker, None, sector, headline,
                 today, expires_at, discard_del,
                 self.now(), self.now(),
                 score_num),
            )
            return cur.lastrowid

    # ── BIL CONCENTRATION ALERT (Audit Round 5) ─────────────────────────
    # BIL is our cash-parking ETF. In normal operation it ranges 5-30%
    # of portfolio. Above ~65% usually means Alpaca holding most of the
    # funds in BIL because the trader isn't finding entries — could be
    # a regime issue (risk-off), a deployment cap being hit, or an upstream
    # data problem (no signals flowing). Alert is informational, not a
    # block — we don't want to force capital deployment in a bear tape.
    BIL_CONCENTRATION_THRESHOLD = float(
        os.environ.get('BIL_CONCENTRATION_THRESHOLD', '0.65')
    )

    def check_bil_concentration(self, threshold_pct=None):
        """Return {'bil_pct', 'bil_value', 'total_value', 'over_threshold'}.

        total_value = cash + sum(position market values). bil_value uses
        the current_price snapshot when available, falling back to
        entry_price for OPEN positions that haven't been priced yet."""
        if threshold_pct is None:
            threshold_pct = self.BIL_CONCENTRATION_THRESHOLD
        portfolio = self.get_portfolio() or {}
        positions = self.get_open_positions() or []
        cash = float(portfolio.get('cash') or 0)
        total = cash
        bil_value = 0.0
        for p in positions:
            shares = float(p.get('shares') or 0)
            price = float(p.get('current_price') or p.get('entry_price') or 0)
            mv = shares * price
            total += mv
            if p.get('ticker') == 'BIL':
                bil_value += mv
        pct = (bil_value / total) if total > 0 else 0.0
        return {
            'bil_pct':        round(pct, 4),
            'bil_value':      round(bil_value, 2),
            'total_value':    round(total, 2),
            'threshold_pct':  threshold_pct,
            'over_threshold': pct >= threshold_pct,
        }

    # ── COOLING OFF (Phase 5.b of TRADER_RESTRUCTURE_PLAN) ────────────────
    # Default hold = 24 hours after a loss. Long enough to skip the
    # same-ticker re-entry on the next trader cycle, short enough not
    # to miss a legitimate next-day setup. Tunable via env var.
    COOLING_OFF_HOURS = int(os.environ.get('COOLING_OFF_HOURS', '24'))

    def register_cooling_off(self, ticker, reason, pnl_pct=None, hours=None):
        """Mark `ticker` as cooling off for `hours` hours from now.
        Upserts — a second stop-out extends/reinforces the cool-until
        timestamp rather than stacking rows."""
        if hours is None:
            hours = self.COOLING_OFF_HOURS
        now_dt = datetime.now(timezone.utc)
        cool_until = (now_dt + timedelta(hours=int(hours))).isoformat()
        with self.conn() as c:
            c.execute(
                "INSERT INTO cooling_off (ticker, cool_until, reason, pnl_pct, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "cool_until=excluded.cool_until, reason=excluded.reason, "
                "pnl_pct=excluded.pnl_pct, created_at=excluded.created_at",
                (ticker, cool_until, reason, pnl_pct, now_dt.isoformat())
            )

    def is_cooling_off(self, ticker):
        """Return dict {cool_until, reason, pnl_pct} if `ticker` is in
        a fresh cooling-off window, else None."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            row = c.execute(
                "SELECT cool_until, reason, pnl_pct FROM cooling_off "
                "WHERE ticker = ? AND cool_until > ?",
                (ticker, now_iso)
            ).fetchone()
        return dict(row) if row else None

    def expire_cooling_off(self):
        """Prune rows whose cool_until has passed. Returns count deleted."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            cur = c.execute("DELETE FROM cooling_off WHERE cool_until <= ?", (now_iso,))
            return cur.rowcount

    def close_position(self, pos_id, exit_price, exit_reason, active_controls=None):
        """
        Closes a position. Adds proceeds to cash, records outcome,
        writes ledger entry.

        Audit Round 7.2 — runs as a single transaction so concurrent
        closes against the same customer DB can't race-read the
        portfolio before either write lands. Previously the portfolio
        read + positions update + outcomes insert + portfolio update
        + ledger insert were split across multiple `with self.conn()`
        blocks, each auto-committing in order. Two parallel closes
        could both read `cash=X` and each write `cash=X+their_proceeds`,
        losing one close.
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
            # Stored timestamps are naive-UTC strings. Parse, attach UTC tz so
            # the subtraction against datetime.now(timezone.utc) works.
            _opened = datetime.fromisoformat(pos['opened_at'].replace('Z', '+00:00'))
            if _opened.tzinfo is None:
                _opened = _opened.replace(tzinfo=timezone.utc)
            hold_days  = (datetime.now(timezone.utc) - _opened).days
            verdict    = "WIN" if pnl_dollar >= 0 else "LOSS"

            # Read portfolio INSIDE the transaction so the subsequent
            # update sees the fresh value, not a pre-read snapshot.
            pf_row = c.execute(
                "SELECT cash, realized_gains FROM portfolio WHERE id=1"
            ).fetchone()
            if not pf_row:
                raise RuntimeError("portfolio row missing — DB not initialized")
            new_cash  = round(float(pf_row['cash']) + proceeds, 2)
            new_gains = round(float(pf_row['realized_gains']) + pnl_dollar, 2)

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

            # Inline the portfolio + ledger writes so the whole sequence
            # (positions → outcomes → portfolio → ledger) commits atomically.
            c.execute(
                "UPDATE portfolio SET cash=?, realized_gains=?, updated_at=? WHERE id=1",
                (new_cash, new_gains, self.now())
            )
            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                  'EXIT',
                  f"{pos['ticker']} · {exit_reason} · {'+' if pnl_dollar>=0 else ''}{pnl_dollar:.2f}",
                  proceeds, new_cash, pos_id, self.now()))
        log.info(f"Closed position: {pos['ticker']} {verdict} {pnl_pct:+.2f}% (${pnl_dollar:+.2f})")

        # Record exit performance for trailing stop optimizer
        try:
            self.record_exit_performance(
                position_id=pos_id, ticker=pos['ticker'],
                sector=pos.get('sector', ''), vol_bucket=pos.get('vol_bucket', ''),
                entry_price=float(pos['entry_price']), exit_price=exit_price,
                exit_reason=exit_reason, hold_days=hold_days,
                pnl_dollar=pnl_dollar, pnl_pct=pnl_pct,
                trail_stop_amt_at_entry=float(pos.get('trail_stop_amt') or 0),
                last_profit_tier=float(pos.get('last_profit_tier') or 0),
                active_controls=active_controls,
            )
        except Exception as _e:
            log.debug(f"record_exit_performance: {_e}")

        # Phase 5.b — register cooling-off on losses. Keeps the trader
        # from re-entering the same ticker on its next 30s cycle after a
        # stop-out. Wins don't cool off; we're not trying to throttle
        # winning tickers.
        if verdict == "LOSS":
            try:
                self.register_cooling_off(
                    ticker=pos['ticker'],
                    reason=f"{exit_reason} {pnl_pct:+.2f}%",
                    pnl_pct=pnl_pct,
                )
            except Exception as _e:
                log.debug(f"register_cooling_off failed for {pos['ticker']}: {_e}")

        return pnl_dollar

    def record_exit_performance(self, position_id, ticker, sector, vol_bucket,
                                entry_price, exit_price, exit_reason, hold_days,
                                pnl_dollar, pnl_pct, trail_stop_amt_at_entry=0,
                                last_profit_tier=0, active_controls=None):
        """Record exit metrics for the trailing stop optimizer.
        active_controls is an optional dict of TradingControls values at exit time."""
        ac = active_controls or {}
        with self.conn() as c:
            c.execute("""
                INSERT INTO exit_performance
                    (position_id, ticker, sector, vol_bucket,
                     entry_price, exit_price, exit_reason, hold_days,
                     pnl_dollar, pnl_pct,
                     trail_stop_amt_at_entry,
                     atr_trail_multiplier, late_day_tighten_pct,
                     benchmark_corr_widen, benchmark_corr_tighten,
                     max_holding_days, entry_multiplier,
                     last_profit_tier, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                position_id, ticker, sector, vol_bucket,
                entry_price, exit_price, exit_reason, hold_days,
                round(pnl_dollar, 2), round(pnl_pct, 2),
                trail_stop_amt_at_entry,
                ac.get('atr_trail_multiplier'),
                ac.get('late_day_tighten_pct'),
                ac.get('benchmark_corr_widen'),
                ac.get('benchmark_corr_tighten'),
                ac.get('max_holding_days'),
                ac.get('entry_multiplier'),
                last_profit_tier,
                self.now(),
            ))

    def get_exit_performance_needing_backfill(self, min_days=5):
        """Return exit_performance rows missing post-exit price data that are old enough."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT id, ticker, exit_price, exit_reason, exit_timestamp, entry_price
                FROM exit_performance
                WHERE price_5d_after_exit IS NULL
                  AND exit_timestamp <= datetime('now', ?)
                ORDER BY exit_timestamp ASC LIMIT 50
            """, (f'-{min_days} days',)).fetchall()
            return [dict(r) for r in rows]

    def backfill_exit_performance(self, row_id, price_5d=None, price_10d=None,
                                  peak_during_hold=None, trough_during_hold=None):
        """Fill in hindsight price data and compute stop quality metrics."""
        with self.conn() as c:
            row = c.execute("SELECT * FROM exit_performance WHERE id=?", (row_id,)).fetchone()
            if not row:
                return
            row = dict(row)

            entry = row['entry_price'] or 0
            exit_p = row['exit_price'] or 0

            # Compute stop quality
            stop_too_tight = 0
            stop_too_loose = 0
            missed_gain_pct = 0.0
            excess_loss_pct = 0.0
            optimal_exit = exit_p

            if price_5d is not None and entry > 0:
                recovery_pct = (price_5d - exit_p) / entry if entry else 0
                # Stop too tight: price recovered >3% of entry within 5 days
                if recovery_pct > 0.03 and row['exit_reason'] in ('TRAILING_STOP_FILLED', 'STOP_LOSS'):
                    stop_too_tight = 1
                # Stop too loose: price fell >3% further after exit (we should have exited earlier)
                if recovery_pct < -0.03:
                    stop_too_loose = 1

            if peak_during_hold and entry > 0:
                optimal_exit = peak_during_hold
                missed_gain_pct = round((peak_during_hold - exit_p) / entry * 100, 2)

            if price_5d is not None and exit_p > 0:
                excess_loss_pct = round((price_5d - exit_p) / exit_p * 100, 2)

            c.execute("""
                UPDATE exit_performance SET
                    price_5d_after_exit = ?,
                    price_10d_after_exit = ?,
                    peak_price_during_hold = ?,
                    trough_price_during_hold = ?,
                    stop_too_tight = ?,
                    stop_too_loose = ?,
                    optimal_exit_price = ?,
                    missed_gain_pct = ?,
                    excess_loss_pct = ?
                WHERE id = ?
            """, (
                price_5d, price_10d,
                peak_during_hold, trough_during_hold,
                stop_too_tight, stop_too_loose,
                round(optimal_exit, 2),
                missed_gain_pct, excess_loss_pct,
                row_id,
            ))

    def reduce_position(self, pos_id, sell_shares, sell_price, exit_reason="PROFIT_TAKE"):
        """
        Partial sell: reduce shares in-place, record outcome, update cash.
        Keeps original entry_price and position ID intact.
        Returns pnl_dollar for the sold portion.

        Audit Round 9.1 — runs as a single transaction so concurrent partial
        exits can't race-read the portfolio. Previously the portfolio read
        and the subsequent update were in separate connections (same pattern
        as close_position before Round 7.2). Fix mirrors Round 7.2.
        """
        with self.conn() as c:
            pos = c.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if not pos:
                raise ValueError(f"Position {pos_id} not found")
            pos = dict(pos)

            remaining = round(float(pos['shares']) - sell_shares, 4)
            if remaining < 0.0001:
                # Delegate to close_position; it handles its own transaction.
                # Must return before this context exits (no writes yet).
                return self.close_position(pos_id, sell_price, exit_reason)

            proceeds   = round(sell_price * sell_shares, 2)
            cost_basis = round(float(pos['entry_price']) * sell_shares, 2)
            pnl_dollar = round(proceeds - cost_basis, 2)
            pnl_pct    = round((pnl_dollar / cost_basis) * 100, 2) if cost_basis else 0
            verdict    = "WIN" if pnl_dollar >= 0 else "LOSS"

            try:
                # Stored naive UTC string → attach UTC tz for aware subtraction.
                _opened_aware = datetime.strptime(
                    pos['opened_at'][:19], '%Y-%m-%d %H:%M:%S'
                ).replace(tzinfo=timezone.utc)
                hold_days = (datetime.now(timezone.utc) - _opened_aware).days
            except (ValueError, TypeError):
                hold_days = 0

            # Read portfolio INSIDE the transaction so the update sees the
            # fresh value, not a snapshot that may be stale under concurrency.
            pf_row = c.execute(
                "SELECT cash, realized_gains FROM portfolio WHERE id=1"
            ).fetchone()
            if not pf_row:
                raise RuntimeError("portfolio row missing — DB not initialized")
            new_cash  = round(float(pf_row['cash']) + proceeds, 2)
            new_gains = round(float(pf_row['realized_gains']) + pnl_dollar, 2)

            c.execute(
                "UPDATE positions SET shares=?, current_price=?, updated_at=? "
                "WHERE id=? AND status='OPEN'",
                (remaining, sell_price, self.now(), pos_id)
            )
            c.execute("""
                INSERT INTO outcomes
                    (position_id, ticker, entry_price, exit_price, shares,
                     hold_days, pnl_pct, pnl_dollar, vol_bucket,
                     exit_reason, verdict, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (pos_id, pos['ticker'], pos['entry_price'], sell_price,
                  sell_shares, hold_days, pnl_pct, pnl_dollar,
                  pos.get('vol_bucket'), exit_reason, verdict, self.now()))

            c.execute(
                "UPDATE portfolio SET cash=?, realized_gains=?, updated_at=? WHERE id=1",
                (new_cash, new_gains, self.now())
            )
            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                  'PARTIAL_EXIT',
                  f"{pos['ticker']} · {exit_reason} · sold {sell_shares:.4f}sh · "
                  f"{'+' if pnl_dollar>=0 else ''}{pnl_dollar:.2f}",
                  proceeds, new_cash, pos_id, self.now()))

        log.info(f"Partial exit: {pos['ticker']} sold {sell_shares:.4f}sh "
                 f"{verdict} {pnl_pct:+.2f}% (${pnl_dollar:+.2f}) — {remaining:.4f}sh remaining")
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


    def update_trail_stop(self, pos_id, new_stop_amt):
        """Ratchet trailing stop upward. Only increases, never decreases."""
        with self.conn() as c:
            c.execute(
                "UPDATE positions SET trail_stop_amt=? WHERE id=? AND status='OPEN' "
                "AND (trail_stop_amt IS NULL OR trail_stop_amt < ?)",
                (round(new_stop_amt, 4), pos_id, round(new_stop_amt, 4))
            )

    def update_profit_tier(self, pos_id, tier_pct):
        """Record which profit-taking tier was last triggered for this position."""
        with self.conn() as c:
            c.execute(
                "UPDATE positions SET last_profit_tier=? WHERE id=?",
                (tier_pct, pos_id)
            )

    # ── SIGNALS ────────────────────────────────────────────────────────────

    def upsert_signal(self, ticker, source, source_tier, headline,
                      politician=None, tx_date=None, disc_date=None,
                      amount_range=None, confidence="MEDIUM",
                      staleness="Fresh", corroborated=False,
                      corroboration_note=None, sector=None, company=None,
                      is_amended=False, is_spousal=False, image_url=None, source_url=None):
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
            expires_at  = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime('%Y-%m-%d %H:%M:%S')
            discard_del = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

            # Tier 4 always discarded immediately
            status = "DISCARDED" if source_tier == 4 else "PENDING"

            c.execute("""
                INSERT INTO signals
                    (ticker, company, sector, source, source_tier, headline,
                     politician, tx_date, disc_date, amount_range, confidence,
                     staleness, corroborated, corroboration_note,
                     is_amended, is_spousal, status, expires_at,
                     discard_delete_at, created_at, updated_at, image_url, source_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, company, sector, source, source_tier, headline,
                  politician, tx_date, disc_date, amount_range, confidence,
                  staleness, int(corroborated), corroboration_note,
                  int(is_amended), int(is_spousal), status, expires_at,
                  discard_del, self.now(), self.now(), image_url, source_url))

            sig_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            log.info(f"New signal: {ticker} T{source_tier} {confidence} — id={sig_id}")
            return sig_id

    # ── PER-AGENT STAMPS ───────────────────────────────────────────────────
    # Each analysis agent writes its own completion marker on signals it
    # processes. No agent reads another agent's tag as a hard filter — the
    # trader uses these as Gate 5 scoring inputs only.

    # All stamp_signals_* methods update both QUEUED and VALIDATED rows so a
    # promoted signal's context stays fresh if the agent re-runs during its
    # validity window. Terminal statuses (ACTED_ON, EVALUATED, EXPIRED) are
    # never re-stamped.
    _STAMPABLE_STATUSES = "('QUEUED','VALIDATED')"

    def stamp_signals_sentiment(self, ticker, sentiment_score, cycle_id=None):
        """Sentiment agent attaches a numeric score (0.0-1.0) and timestamp to
        all in-flight signals for `ticker`. Records a STAMPED_TICKER decision
        log row. Returns count stamped."""
        with self.conn() as c:
            result = c.execute(
                f"UPDATE signals SET sentiment_score=?, sentiment_evaluated_at=? "
                f"WHERE ticker=? AND status IN {self._STAMPABLE_STATUSES}",
                (float(sentiment_score), self.now(), ticker.upper())
            )
            count = result.rowcount
        if count > 0:
            self.log_signal_decision(
                agent='sentiment', action='STAMPED_TICKER',
                ticker=ticker.upper(), value=f"{float(sentiment_score):.2f}",
                reason=f"{count} signal(s) stamped", cycle_id=cycle_id,
            )
        return count

    def stamp_signals_screener(self, tickers, cycle_id=None, score=None):
        """Sector screener marks all in-flight signals for the given tickers as
        screener-evaluated.

        Two calling conventions supported:
          - list of tickers + optional single `score` (0.0-1.0) — stamps
            every matching signal with the same timestamp and score.
            Used for a sector's top-N candidate pool where all share
            the same momentum_score bucket, or for a "set to baseline"
            bulk write.
          - dict of {ticker: score} — per-ticker scoring (used when
            each candidate has its own momentum score). Iterates and
            calls the list-form internally so decision-log rows stay
            one-per-ticker-set.

        Returns count of signal rows stamped.
        """
        # Dict form → delegate per-score
        if isinstance(tickers, dict):
            total = 0
            for tkr, sc in tickers.items():
                total += self.stamp_signals_screener(
                    [tkr], cycle_id=cycle_id, score=sc
                )
            return total

        if not tickers:
            return 0
        upper = [t.upper() for t in tickers]
        with self.conn() as c:
            placeholders = ','.join('?' * len(upper))
            if score is not None:
                result = c.execute(
                    f"UPDATE signals SET screener_evaluated_at=?, screener_score=? "
                    f"WHERE ticker IN ({placeholders}) AND status IN {self._STAMPABLE_STATUSES}",
                    (self.now(), float(score), *upper)
                )
            else:
                # Back-compat: old callers pass just a list. Stamp the
                # timestamp without a score — trader's Gate 5 treats a
                # null score the same as "legacy stamp" (boolean bonus).
                result = c.execute(
                    f"UPDATE signals SET screener_evaluated_at=? "
                    f"WHERE ticker IN ({placeholders}) AND status IN {self._STAMPABLE_STATUSES}",
                    (self.now(), *upper)
                )
            count = result.rowcount
        if count > 0:
            summary = ','.join(upper[:10]) + (f"+{len(upper)-10}" if len(upper) > 10 else "")
            reason  = f"candidates: {summary}"
            if score is not None:
                reason += f" (score={score:.2f})"
            self.log_signal_decision(
                agent='screener', action='STAMPED_BATCH',
                value=str(count), reason=reason, cycle_id=cycle_id,
            )
        return count

    def stamp_signals_screener_baseline(self, sector_scores, cycle_id=None):
        """Blanket-stamp every in-flight signal that doesn't already have
        a screener_score with a per-sector baseline.

        sector_scores: dict {sector_name: baseline_score}. Each signal's
        ticker is resolved to its sector (via the signals.sector column
        written at news-agent classification time), then gets the
        corresponding baseline. Signals whose sector isn't in the map
        default to 0.5 (neutral).

        Intended to run AFTER all sectors' top-N candidates have been
        stamped — the WHERE clause skips any row with screener_score
        already set so top-candidate momentum scores aren't overwritten
        by the blanket baseline. Closes the "most signals have no
        screener stamp" sparsity gap without flattening per-ticker
        ranking info.

        Returns count of signal rows stamped.
        """
        if not sector_scores:
            return 0
        total_stamped = 0
        now = self.now()

        # Per-sector UPDATE: matches signals whose sector column equals
        # the sector name AND screener_score IS NULL (don't overwrite
        # top-candidate scores written earlier in the same cycle).
        with self.conn() as c:
            for sector, baseline in sector_scores.items():
                result = c.execute(
                    f"UPDATE signals "
                    f"SET screener_evaluated_at=?, screener_score=? "
                    f"WHERE sector=? "
                    f"AND status IN {self._STAMPABLE_STATUSES} "
                    f"AND screener_score IS NULL",
                    (now, float(baseline), sector)
                )
                total_stamped += result.rowcount

            # Remaining: signals in-flight but with no sector match at
            # all (sector='Unknown' or missing). Give them the neutral
            # 0.5 baseline so they're still flagged as evaluated.
            result = c.execute(
                f"UPDATE signals "
                f"SET screener_evaluated_at=?, screener_score=? "
                f"WHERE status IN {self._STAMPABLE_STATUSES} "
                f"AND screener_score IS NULL",
                (now, 0.5)
            )
            unknown_stamped = result.rowcount
            total_stamped += unknown_stamped

        if total_stamped > 0:
            self.log_signal_decision(
                agent='screener', action='STAMPED_BASELINE',
                value=str(total_stamped),
                reason=(f"blanket baseline for {len(sector_scores)} sector(s), "
                        f"{unknown_stamped} unknown-sector fallbacks"),
                cycle_id=cycle_id,
            )
        return total_stamped

    def stamp_signals_macro(self, regime_label, cycle_id=None):
        """Macro regime agent snapshots the current market regime onto all
        in-flight signals. One SQL call; O(1) regardless of queue size.
        Stamp format: 'timestamp|regime_label'.
        Records a STAMPED_BATCH decision log row."""
        with self.conn() as c:
            result = c.execute(
                f"UPDATE signals SET macro_regime_at_validation=? "
                f"WHERE status IN {self._STAMPABLE_STATUSES}",
                (f"{self.now()}|{regime_label}",)
            )
            count = result.rowcount
        if count > 0:
            self.log_signal_decision(
                agent='macro', action='STAMPED_BATCH',
                value=regime_label, reason=f"{count} in-flight signal(s) stamped",
                cycle_id=cycle_id,
            )
        return count

    def stamp_signals_market_state(self, state_label, cycle_id=None):
        """Market state agent snapshots the current aggregate state onto all
        in-flight signals. Stamp format: 'timestamp|state_label'.
        Records a STAMPED_BATCH decision log row."""
        with self.conn() as c:
            result = c.execute(
                f"UPDATE signals SET market_state_at_validation=? "
                f"WHERE status IN {self._STAMPABLE_STATUSES}",
                (f"{self.now()}|{state_label}",)
            )
            count = result.rowcount
        if count > 0:
            self.log_signal_decision(
                agent='market_state', action='STAMPED_BATCH',
                value=state_label, reason=f"{count} in-flight signal(s) stamped",
                cycle_id=cycle_id,
            )
        return count

    def stamp_signals_validator(self, verdict='OK', cycle_id=None):
        """Validator stack stamps completion of its pass on all in-flight
        signals. Called once per pipeline cycle (not per-customer) from the
        daemon, immediately before the promoter step.
        Stamp format: 'timestamp|verdict'.
        Records a STAMPED_BATCH decision log row."""
        with self.conn() as c:
            result = c.execute(
                f"UPDATE signals SET validator_stamped_at=? "
                f"WHERE status IN {self._STAMPABLE_STATUSES}",
                (f"{self.now()}|{verdict}",)
            )
            count = result.rowcount
        if count > 0:
            self.log_signal_decision(
                agent='validator', action='STAMPED_BATCH',
                value=verdict, reason=f"{count} in-flight signal(s) stamped",
                cycle_id=cycle_id,
            )
        return count

    # Required stamps for promotion (used by promote_validated_signals and
    # the fault detection "stuck signal" gate).
    REQUIRED_STAMPS = (
        'interrogation_status',
        'sentiment_evaluated_at',
        'macro_regime_at_validation',
        'market_state_at_validation',
        'validator_stamped_at',
    )

    # interrogation_status values that are considered genuinely promotable.
    # VALIDATED    = peer corroborated via UDP broadcast.
    # CORROBORATED = multiple independent sources confirmed the signal.
    # SKIPPED      = news agent set this for WATCH-routed signals that were
    #                never sent for interrogation (they weren't trade
    #                candidates at news-classification time but still carry
    #                useful context; trader scores them low on Gate 5).
    # UNVALIDATED  = interrogation attempted but no peer responded — this
    #                is the degraded-pipeline case and is explicitly NOT in
    #                this set. If the interrogation listener dies, new news
    #                signals land here and stop promoting, triggering both
    #                the fault detector's STUCK_SIGNALS gate and the
    #                watchdog's pipeline-stall alert.
    PROMOTABLE_INTERROGATION_STATUSES = ('VALIDATED', 'CORROBORATED', 'SKIPPED')

    def promote_validated_signals(self, cycle_id=None):
        """The last link in the validation chain.

        Transitions QUEUED signals to VALIDATED if they have all required
        stamps AND their interrogation_status is a promotable value (not
        UNVALIDATED, which means the peer-corroboration broadcast got no
        response). Trader reads only VALIDATED signals.

        For every promotion, writes a PROMOTED row to signal_decisions.
        For every still-QUEUED signal, writes a STUCK row that distinguishes
        "missing stamp X" from "stamp present but non-promotable value"
        (currently only applies to interrogation_status=UNVALIDATED) so the
        fault detector can name the exact bottleneck.

        Returns (promoted_count, stuck_count).
        """
        stamp_cols    = self.REQUIRED_STAMPS
        ok_statuses   = self.PROMOTABLE_INTERROGATION_STATUSES
        placeholders  = ','.join('?' * len(ok_statuses))

        # 1) Find candidates BEFORE the update so we know which IDs were promoted.
        with self.conn() as c:
            promotable = c.execute(
                f"SELECT id, ticker FROM signals "
                f"WHERE status='QUEUED' "
                f"AND interrogation_status IN ({placeholders}) "
                f"AND sentiment_evaluated_at IS NOT NULL "
                f"AND macro_regime_at_validation IS NOT NULL "
                f"AND market_state_at_validation IS NOT NULL "
                f"AND validator_stamped_at IS NOT NULL",
                ok_statuses,
            ).fetchall()
            promoted_ids = [(r[0], r[1]) for r in promotable]

            # 2) Promote them in one UPDATE.
            if promoted_ids:
                ids_placeholders = ','.join('?' * len(promoted_ids))
                c.execute(
                    f"UPDATE signals SET status='VALIDATED', updated_at=? "
                    f"WHERE id IN ({ids_placeholders})",
                    (self.now(), *[p[0] for p in promoted_ids])
                )

            # 3) Snapshot still-QUEUED rows with their stamp status for STUCK logging.
            stuck_cols = ','.join(['id', 'ticker', 'created_at', *stamp_cols])
            stuck_rows = c.execute(
                f"SELECT {stuck_cols} FROM signals WHERE status='QUEUED'"
            ).fetchall()

        # 4) Write per-signal PROMOTED rows.
        for sid, ticker in promoted_ids:
            self.log_signal_decision(
                agent='promoter', action='PROMOTED',
                ticker=ticker, signal_id=sid,
                value='QUEUED→VALIDATED',
                reason='all required stamps present, interrogation promotable',
                cycle_id=cycle_id,
            )

        # 5) Write per-signal STUCK rows.
        # Two shapes of "stuck":
        #   (a) a stamp is NULL — upstream agent hasn't touched this signal
        #       yet. reason: "missing: <column>, ..."
        #   (b) all stamps present but interrogation_status is a non-promotable
        #       value (e.g. UNVALIDATED). reason names the exact blocker so
        #       the fault detector doesn't misattribute this to a stamp miss.
        # Freshly-QUEUED signals with zero progress ("new" rather than "stuck")
        # are skipped to keep signal_decisions from being swamped by arrivals.
        interrogation_col_idx = stamp_cols.index('interrogation_status')
        stuck_logged = 0
        for row in stuck_rows:
            sid       = row[0]
            ticker    = row[1]
            created   = row[2]
            stamps    = row[3:]
            missing   = [stamp_cols[i] for i, v in enumerate(stamps) if v is None]
            present   = len(stamp_cols) - len(missing)
            if present == 0:
                continue

            reason_bits = []
            value_summary = None
            if missing:
                reason_bits.append(f"missing: {','.join(missing)}")
                value_summary = f"missing={len(missing)}/{len(stamp_cols)}"

            # Stamp-present-but-not-promotable cases. Today only one —
            # interrogation_status=UNVALIDATED / ABSTAINED / other. The
            # check is written to extend to any future promotability rule.
            interrog_value = stamps[interrogation_col_idx]
            if interrog_value is not None and interrog_value not in ok_statuses:
                reason_bits.append(
                    f"interrogation_status={interrog_value!r} not in "
                    f"promotable set {list(ok_statuses)}"
                )
                if value_summary is None:
                    value_summary = f"interrogation={interrog_value}"

            if not reason_bits:
                # All stamps present and promotable, but the signal wasn't
                # in the promoted set for some other reason we don't know
                # about. Fall through — don't pretend we understand why.
                continue

            self.log_signal_decision(
                agent='promoter', action='STUCK',
                ticker=ticker, signal_id=sid,
                value=value_summary,
                reason=f"{' | '.join(reason_bits)} (created={created})",
                cycle_id=cycle_id,
            )
            stuck_logged += 1

        return len(promoted_ids), stuck_logged

    def get_stuck_signals(self, min_age_minutes=120):
        """Return QUEUED signals older than `min_age_minutes` that never got
        all required stamps. Each row is a dict with 'ticker', 'id', 'age_minutes',
        'missing_stamps' (list). Used by fault detection to surface which
        upstream agent is the bottleneck."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        stamp_cols = self.REQUIRED_STAMPS
        cutoff = (_dt.now(_tz.utc) - _td(minutes=min_age_minutes)).isoformat()[:19]
        cols = ','.join(['id', 'ticker', 'created_at', *stamp_cols])
        with self.conn() as c:
            rows = c.execute(
                f"SELECT {cols} FROM signals "
                f"WHERE status='QUEUED' AND created_at < ?",
                (cutoff,)
            ).fetchall()
        result = []
        now = _dt.now(_tz.utc)
        for row in rows:
            created_str = row[2]
            try:
                created = _dt.fromisoformat(created_str.replace(' ', 'T').split('.')[0])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=_tz.utc)
                age_min = int((now - created).total_seconds() / 60)
            except Exception:
                age_min = -1
            missing = [stamp_cols[i] for i, v in enumerate(row[3:]) if v is None]
            result.append({
                'id':             row[0],
                'ticker':         row[1],
                'age_minutes':    age_min,
                'missing_stamps': missing,
            })
        return result

    def log_signal_decision(self, agent, action, ticker=None, signal_id=None,
                            value=None, reason=None, cycle_id=None, meta=None):
        """Append a row to signal_decisions. Every stamp/promotion/skip ends
        here so we can replay the validation chain for any signal or cycle.
        Non-fatal — any insert failure is logged at DEBUG and swallowed, so a
        decision-log hiccup never breaks the pipeline.

        cycle_id falls back to os.environ['SYNTHOS_CYCLE_ID'] so the daemon
        can set it once per enrichment cycle and every subprocess picks it up
        without threading the ID through every call site."""
        if cycle_id is None:
            cycle_id = os.environ.get('SYNTHOS_CYCLE_ID')
        try:
            meta_json = json.dumps(meta) if meta is not None else None
            with self.conn() as c:
                c.execute(
                    "INSERT INTO signal_decisions "
                    "(ts, cycle_id, agent, action, ticker, signal_id, value, reason, meta) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.now(), cycle_id, agent, action, ticker, signal_id,
                     value, reason, meta_json)
                )
        except Exception as e:
            log.debug(f"log_signal_decision failed (non-fatal): {e}")

    # ── VALIDATED POOL CAP ─────────────────────────────────────────────────
    # Tier-weighted quotas prevent a low-quality signal flood from crowding
    # out the trader's time on high-conviction trades AND cap per-trader
    # runtime at roughly N signals * ~0.5s/signal ≈ 50s of evaluation work.
    # 60+25+10+5 = 100 (the user-chosen global cap). Tier 1 gets the most
    # slots because those are the highest-conviction signals (political
    # trades, SEC filings); tier 4 gets a few slots so occasional weak
    # signals still have a path to execution but can't flood the pool.
    VALIDATED_TIER_QUOTAS = {1: 60, 2: 25, 3: 10, 4: 5}

    def get_validated_signals(self, tier_quotas=None):
        """Signals that have passed the full validation chain and are ready
        for the trader. Trader reads ONLY from this view — the unvalidated
        QUEUED pool is invisible.

        Applies tier-weighted quotas (VALIDATED_TIER_QUOTAS) so a flood of
        low-tier signals can't dilute the trader's evaluation budget away
        from high-conviction trades. Pass tier_quotas={} to disable the
        cap (returns the full unfiltered pool — useful for portal views
        and tests)."""
        quotas = self.VALIDATED_TIER_QUOTAS if tier_quotas is None else tier_quotas
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM signals
                WHERE status='VALIDATED'
                ORDER BY source_tier ASC, created_at ASC
            """).fetchall()

        # No cap requested, or no signals at all — fast path
        if not quotas or not rows:
            return [dict(r) for r in rows]

        # Apply per-tier quotas. Input is already sorted (tier ASC, then
        # oldest first within a tier) so taking the first N per tier yields
        # the highest-conviction, oldest-acting signals in each tier.
        kept = []
        used = {t: 0 for t in quotas}
        dropped = {t: 0 for t in quotas}
        unknown_dropped = 0
        for r in rows:
            tier = r['source_tier']
            if tier not in quotas:
                # Unknown tier — don't trust it into the pool. Logged below.
                unknown_dropped += 1
                continue
            if used[tier] < quotas[tier]:
                kept.append(dict(r))
                used[tier] += 1
            else:
                dropped[tier] += 1

        # Only log when the cap actually kicked in. Normal runs with a
        # small validated pool will produce zero extra log lines.
        if any(dropped.values()) or unknown_dropped:
            drop_summary = ' '.join(f"t{t}={dropped[t]}" for t in sorted(dropped))
            log.info(
                f"[SIGNAL_POOL] {len(rows)} validated available, capped to "
                f"{len(kept)} via tier quotas "
                f"(quotas: {quotas}, dropped: {drop_summary}"
                + (f", unknown_tier={unknown_dropped}" if unknown_dropped else "")
                + ")"
            )
        return kept

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

    def annotate_signal_pulse(self, signal_id, tier, summary):
        """
        The Pulse writes its pre-trade sentiment finding to the signal record.
        Appends to corroboration_note so The Daily's existing note is preserved.
        The Trader reads this note in analyze_signal_with_claude().
        """
        note = f"[PULSE Tier {tier}] {summary}"
        with self.conn() as c:
            c.execute("""
                UPDATE signals SET
                    corroboration_note = CASE
                        WHEN corroboration_note IS NULL OR corroboration_note = ''
                        THEN ?
                        ELSE corroboration_note || ' | ' || ?
                    END,
                    updated_at = ?
                WHERE id = ?
            """, (note, note, self.now(), signal_id))
        log.info(f"Signal {signal_id} annotated by Pulse: Tier {tier} — {summary[:60]}")

    # ── MEMBER WEIGHTS ─────────────────────────────────────────────────────

    def get_member_weight(self, congress_member):
        """
        Return weight record for a congress member.
        Returns dict with weight, win_count, loss_count.
        Default weight is 1.0 for unknown members.
        """
        if not congress_member:
            return {'weight': 1.0, 'win_count': 0, 'loss_count': 0}
        with self.conn() as c:
            row = c.execute(
                "SELECT weight, win_count, loss_count FROM member_weights WHERE congress_member=?",
                (congress_member,)
            ).fetchone()
        return dict(row) if row else {'weight': 1.0, 'win_count': 0, 'loss_count': 0}

    def update_member_weight_after_trade(self, congress_member, pnl_dollar):
        """
        Update win/loss counts and recompute weight after a trade closes.
        Weight formula: clamp(win_count / total * 2, 0.5, 1.5)
        Requires >= 5 trades before adjusting from 1.0.
        Called by Bolt after every close_position().
        """
        if not congress_member:
            return
        with self.conn() as c:
            row = c.execute(
                "SELECT win_count, loss_count FROM member_weights WHERE congress_member=?",
                (congress_member,)
            ).fetchone()
            if row:
                wins   = row['win_count']   + (1 if pnl_dollar > 0 else 0)
                losses = row['loss_count']  + (1 if pnl_dollar < 0 else 0)
            else:
                wins   = 1 if pnl_dollar > 0 else 0
                losses = 1 if pnl_dollar < 0 else 0

            total  = wins + losses
            weight = 1.0 if total < 5 else max(0.5, min(1.5, (wins / total) * 2))

            c.execute("""
                INSERT INTO member_weights
                    (congress_member, win_count, loss_count, weight, last_updated)
                VALUES (?,?,?,?,?)
                ON CONFLICT(congress_member) DO UPDATE SET
                    win_count    = excluded.win_count,
                    loss_count   = excluded.loss_count,
                    weight       = excluded.weight,
                    last_updated = excluded.last_updated
            """, (congress_member, wins, losses, round(weight, 4), self.now()))
        log.info(f"Member weight: {congress_member} {wins}W/{losses}L → {weight:.3f}")

    # ── NEWS FEED ──────────────────────────────────────────────────────────

    def write_news_feed_entry(self, congress_member, ticker, signal_score,
                               sentiment_score, raw_headline, metadata, source):
        """
        Write a signal to the news_feed table for portal display.
        Called by Scout for every signal processed — QUEUE, WATCH, and DISCARD alike.
        """
        with self.conn() as c:
            c.execute("""
                INSERT INTO news_feed
                    (timestamp, congress_member, ticker, signal_score, sentiment_score,
                     raw_headline, metadata, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                self.now(), congress_member, ticker, signal_score, sentiment_score,
                raw_headline,
                json.dumps(metadata) if metadata else None,
                source, self.now()
            ))

    def get_news_feed(self, limit=50):
        """Return recent news feed entries for portal display, newest first."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM news_feed ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_news_headlines(self, category=None, limit=100, min_floor=30):
        """Return display-only news headlines (source='NEWS') for the News panel.

        Same 30-article minimum floor as Intel page. Optionally filters by
        category ('Breaking', 'Markets', 'US', 'Global').
        """
        with self.conn() as c:
            if category and category != 'all':
                rows = c.execute("""
                    SELECT raw_headline, metadata, created_at FROM news_feed
                    WHERE source='NEWS'
                      AND json_extract(metadata,'$.category')=?
                    ORDER BY created_at DESC LIMIT ?
                """, (category, max(limit, min_floor))).fetchall()
            else:
                rows = c.execute("""
                    SELECT raw_headline, metadata, created_at FROM news_feed
                    WHERE source='NEWS'
                    ORDER BY created_at DESC LIMIT ?
                """, (max(limit, min_floor),)).fetchall()

            result = []
            seen: set = set()
            for r in rows:
                meta = {}
                try:
                    meta = json.loads(r['metadata'] or '{}')
                except Exception:
                    pass
                headline = r['raw_headline'] or ''
                key = headline.lower()[:60]
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    'headline':  headline,
                    'source':    meta.get('source', 'Unknown'),
                    'category':  meta.get('category', 'Breaking'),
                    'link':      meta.get('link', ''),
                    'pub_date':  meta.get('pub_date', ''),
                    'staleness': meta.get('staleness', 'fresh'),
                    'created_at': r['created_at'] or '',
                    'summary':   meta.get('summary', ''),
                    'image':     meta.get('image_url') or meta.get('image'),
                    'image_url':  meta.get('image_url') or meta.get('image'),
                    'symbols':   meta.get('symbols', []),
                    'provider':  meta.get('provider', 'rss'),
                })
            return result

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



    # ── Support Tickets ───────────────────────────────────────────────────────

    def create_ticket(self, category, subject, message, beta_test_id=None, priority='normal'):
        """Create a support ticket with initial message. Returns ticket_id."""
        import secrets
        ticket_id = 'TKT-' + secrets.token_hex(4).upper()
        now = self.now()
        with self.conn() as c:
            c.execute(
                "INSERT INTO support_tickets "
                "(ticket_id, category, subject, status, priority, beta_test_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
                (ticket_id, category, subject, priority, beta_test_id, now, now)
            )
            c.execute(
                "INSERT INTO support_messages (ticket_id, sender, message, created_at) "
                "VALUES (?, 'customer', ?, ?)",
                (ticket_id, message, now)
            )
        self.log_event("SUPPORT_TICKET_CREATED", agent="portal",
                       details=f"ticket={ticket_id} category={category}")
        return ticket_id

    def get_tickets(self, status=None, category=None, limit=50):
        """Get support tickets with latest message preview."""
        with self.conn() as c:
            query = "SELECT * FROM support_tickets"
            params = []
            clauses = []
            if status:
                clauses.append("status=?")
                params.append(status)
            if category:
                clauses.append("category=?")
                params.append(category)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            tickets = [dict(r) for r in c.execute(query, params).fetchall()]
            # Attach latest message preview
            for t in tickets:
                msg = c.execute(
                    "SELECT sender, message, created_at FROM support_messages "
                    "WHERE ticket_id=? ORDER BY created_at DESC LIMIT 1",
                    (t['ticket_id'],)
                ).fetchone()
                t['last_message'] = dict(msg) if msg else None
                t['message_count'] = c.execute(
                    "SELECT COUNT(*) FROM support_messages WHERE ticket_id=?",
                    (t['ticket_id'],)
                ).fetchone()[0]
            return tickets

    def get_ticket_messages(self, ticket_id):
        """Get all messages for a ticket in chronological order."""
        with self.conn() as c:
            ticket = c.execute(
                "SELECT * FROM support_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if not ticket:
                return None, []
            messages = c.execute(
                "SELECT * FROM support_messages WHERE ticket_id=? ORDER BY created_at ASC",
                (ticket_id,)
            ).fetchall()
            return dict(ticket), [dict(m) for m in messages]

    def add_ticket_message(self, ticket_id, sender, message):
        """Add a message to an existing ticket thread."""
        now = self.now()
        with self.conn() as c:
            c.execute(
                "INSERT INTO support_messages (ticket_id, sender, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_id, sender, message, now)
            )
            c.execute(
                "UPDATE support_tickets SET updated_at=? WHERE ticket_id=?",
                (now, ticket_id)
            )
        return True

    def update_ticket_status(self, ticket_id, status):
        """Update ticket status (open/in_progress/resolved/closed)."""
        now = self.now()
        with self.conn() as c:
            resolved_at = now if status in ('resolved', 'closed') else None
            c.execute(
                "UPDATE support_tickets SET status=?, updated_at=?, resolved_at=COALESCE(?, resolved_at) "
                "WHERE ticket_id=?",
                (status, now, resolved_at, ticket_id)
            )
        return True

    def get_open_ticket_count(self):
        """Count open tickets for badge display."""
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE status IN ('open','in_progress')"
            ).fetchone()[0]

    # ── Per-Customer Settings ─────────────────────────────────────────────────

    def get_setting(self, key, default=None):
        """Get a single customer setting. Returns default if not set."""
        with self.conn() as c:
            row = c.execute(
                "SELECT value FROM customer_settings WHERE key=?", (key,)
            ).fetchone()
            return row['value'] if row else default

    def set_setting(self, key, value):
        """Set a customer setting (upsert)."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO customer_settings (key, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), self.now())
            )

    def get_all_settings(self):
        """Get all customer settings as a dict."""
        with self.conn() as c:
            rows = c.execute("SELECT key, value FROM customer_settings").fetchall()
            return {r['key']: r['value'] for r in rows}

    def get_settings_with_defaults(self, global_defaults=None):
        """Get merged settings: customer DB overrides global defaults.
        
        Args:
            global_defaults: dict of {key: value} from global .env
        Returns:
            dict with customer settings overriding global defaults
        """
        defaults = dict(global_defaults or {})
        customer = self.get_all_settings()
        defaults.update(customer)
        return defaults

    def get_queued_signals(self):
        """All signals ready for the trader to act on.

        The interrogation_status='SKIPPED' tag is a news-agent internal
        bookkeeping flag (set on display-only / WATCH-routed news signals
        in retail_news_agent.py). It is NOT a trader filter. The trader's
        own gates 4–6 (liquidity, spread, event risk, score, entry) are
        responsible for rejecting unsuitable signals. A prior version
        filtered SKIPPED here to reduce sentiment-agent scan load — but
        sentiment uses screening_requests, not this method.
        """
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


    def cross_validate_signals(self, hours_back=96):
        """
        Scan QUEUED signals for cross-validation patterns:
        1. Same ticker from 2+ signals → corroborated, promote if different sources
        2. Sector cluster: 3+ different tickers in same sector, MEDIUM+ conf, 2+ sources

        Staleness decay (relative to newest signal in group):
          0-8h: 1.0x | 8-24h: 0.8x | 24-48h: 0.6x | 48-96h: 0.4x

        Returns: {tickers_corroborated: [str], sector_clusters: [str]}
        """
        from collections import defaultdict
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime('%Y-%m-%d %H:%M:%S')
        now_str = self.now()

        # Extract sub-source from 'Alpaca News (benzinga)' format
        import re as _re
        def _subsource(s):
            src_str = s.get('source') or ''
            m = _re.search(r'\(([^)]+)\)', src_str)
            return m.group(1) if m else src_str

        with self.conn() as c:
            rows = c.execute("""
                SELECT id, ticker, sector, source, confidence,
                       corroboration_note, interrogation_status, created_at
                FROM signals
                WHERE status = 'QUEUED' AND created_at > ?
                ORDER BY created_at DESC
            """, (cutoff,)).fetchall()

        signals = [dict(r) for r in rows]
        if not signals:
            return {"tickers_corroborated": [], "sector_clusters": []}

        # ── Helper: staleness decay weight ──
        def _decay_weight(created_at_str, newest_str):
            try:
                # Parse naive timestamps
                created = datetime.strptime(created_at_str[:19], '%Y-%m-%d %H:%M:%S')
                newest  = datetime.strptime(newest_str[:19], '%Y-%m-%d %H:%M:%S')
                age_hours = (newest - created).total_seconds() / 3600
            except (ValueError, TypeError):
                return 0.4
            if age_hours <= 8:   return 1.0
            if age_hours <= 24:  return 0.8
            if age_hours <= 48:  return 0.6
            return 0.4

        # ── Group by ticker ──
        by_ticker = defaultdict(list)
        for s in signals:
            by_ticker[s['ticker']].append(s)

        tickers_corroborated = []
        ids_to_update = {}  # id → {corroborated, interrogation_status, note_append}

        for ticker, group in by_ticker.items():
            if len(group) < 2:
                continue

            sources = set(_subsource(s) for s in group if s.get('source'))
            newest_ts = group[0]['created_at']  # already ordered DESC
            multi_source = len(sources) >= 2

            for s in group:
                decay = _decay_weight(s['created_at'], newest_ts)
                source_list = ', '.join(sorted(sources))
                note = f"[CROSS-VALIDATED: {len(group)} signals ({decay:.1f}x), sources: {source_list}]"

                # Don't re-annotate if already cross-validated this run
                existing_note = s.get('corroboration_note') or ''
                if '[CROSS-VALIDATED' in existing_note:
                    continue

                update = {'corroborated': 1, 'note_append': note}

                # Promote interrogation_status only if different sources and
                # current status is below CORROBORATED
                current_status = s.get('interrogation_status') or 'UNVALIDATED'
                if multi_source and current_status in ('UNVALIDATED', ''):
                    update['interrogation_status'] = 'CORROBORATED'

                ids_to_update[s['id']] = update

            tickers_corroborated.append(ticker)

        # ── Sector clusters ──
        by_sector = defaultdict(list)
        for s in signals:
            sector = s.get('sector') or ''
            if sector:
                by_sector[sector].append(s)

        sector_clusters = []

        for sector, group in by_sector.items():
            # Unique tickers in this sector
            tickers_in_sector = set(s['ticker'] for s in group)
            if len(tickers_in_sector) < 3:
                continue

            # Quality gate: all must be MEDIUM+
            quality_signals = [s for s in group
                               if (s.get('confidence') or '').upper() in ('MEDIUM', 'HIGH')]
            quality_tickers = set(s['ticker'] for s in quality_signals)
            if len(quality_tickers) < 3:
                continue

            # Source diversity gate: at least 2 different sources
            quality_sources = set(_subsource(s) for s in quality_signals if s.get('source'))
            # Require 2+ sub-sources, OR 4+ tickers if single sub-source
            if len(quality_sources) < 2 and len(quality_tickers) < 4:
                continue

            sector_clusters.append(sector)
            newest_ts = group[0]['created_at']

            for s in quality_signals:
                existing_note = s.get('corroboration_note') or ''
                if '[SECTOR_CLUSTER' in existing_note:
                    continue

                sid = s['id']
                note = f"[SECTOR_CLUSTER: {len(quality_tickers)} tickers in {sector}]"

                if sid in ids_to_update:
                    ids_to_update[sid]['note_append'] += ' | ' + note
                else:
                    update = {'corroborated': 1, 'note_append': note}
                    current_status = s.get('interrogation_status') or 'UNVALIDATED'
                    if current_status in ('UNVALIDATED', ''):
                        update['interrogation_status'] = 'CORROBORATED'
                    ids_to_update[sid] = update

        # ── Apply updates ──
        if ids_to_update:
            with self.conn() as c:
                for sid, upd in ids_to_update.items():
                    note = upd['note_append']
                    new_status = upd.get('interrogation_status')

                    if new_status:
                        c.execute("""
                            UPDATE signals SET
                                corroborated = 1,
                                interrogation_status = ?,
                                corroboration_note = CASE
                                    WHEN corroboration_note IS NULL OR corroboration_note = ''
                                    THEN ?
                                    ELSE corroboration_note || ' | ' || ?
                                END,
                                updated_at = ?
                            WHERE id = ?
                        """, (new_status, note, note, now_str, sid))
                    else:
                        c.execute("""
                            UPDATE signals SET
                                corroborated = 1,
                                corroboration_note = CASE
                                    WHEN corroboration_note IS NULL OR corroboration_note = ''
                                    THEN ?
                                    ELSE corroboration_note || ' | ' || ?
                                END,
                                updated_at = ?
                            WHERE id = ?
                        """, (note, note, now_str, sid))

            log.info(f"[CROSS-VAL] Updated {len(ids_to_update)} signals — "
                     f"tickers: {tickers_corroborated}, sectors: {sector_clusters}")

        return {"tickers_corroborated": tickers_corroborated,
                "sector_clusters": sector_clusters}

    def discard_signal(self, signal_id, reason=None):
        with self.conn() as c:
            c.execute("""
                UPDATE signals SET status='DISCARDED', updated_at=?,
                corroboration_note=COALESCE(?, corroboration_note)
                WHERE id=?
            """, (self.now(), reason, signal_id))

    def get_signal_by_id(self, signal_id):
        """Return a single signal row as dict, or None if not found."""
        if not signal_id:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM signals WHERE id=?", (signal_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── LEDGER ─────────────────────────────────────────────────────────────

    def add_ledger_entry(self, entry_type, description, amount, balance, position_id=None):
        with self.conn() as c:
            c.execute("""
                INSERT INTO ledger
                    (date, type, description, amount, balance, position_id, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (datetime.now(timezone.utc).strftime('%Y-%m-%d'), entry_type,
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
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')
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

    def get_watching_signals(self, limit=100, min_floor=30):
        """Return signals for the Intelligence page from news_feed.

        Always returns at least min_floor articles — fresh signals first,
        stale articles padded in to fill the floor. Field names are mapped
        to what the portal's renderIntelGrid expects.
        """
        with self.conn() as c:
            def _fetch(routing_filter, n):
                if routing_filter == "fresh":
                    rows = c.execute("""
                        SELECT ticker, congress_member, signal_score,
                               sentiment_score, raw_headline, metadata,
                               source, timestamp, created_at
                        FROM news_feed
                        WHERE COALESCE(json_extract(metadata,'$.routing'),'?') != 'STALE'
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (n,)).fetchall()
                else:
                    rows = c.execute("""
                        SELECT ticker, congress_member, signal_score,
                               sentiment_score, raw_headline, metadata,
                               source, timestamp, created_at
                        FROM news_feed
                        WHERE json_extract(metadata,'$.routing') = 'STALE'
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (n,)).fetchall()
                return [dict(r) for r in rows]

            fresh = _fetch("fresh", limit)
            result = list(fresh)
            if len(result) < min_floor:
                result.extend(_fetch("stale", min_floor - len(result)))

            # Map field names and flatten metadata for portal consumption
            out = []
            for r in result:
                meta = {}
                try:
                    meta = json.loads(r.get('metadata') or '{}')
                except Exception:
                    pass
                out.append({
                    "ticker":       r.get("ticker") or "?",
                    "politician":   r.get("congress_member") or "",
                    "confidence":   r.get("signal_score") or "NOISE",
                    "sentiment_score": r.get("sentiment_score"),
                    "headline":     r.get("raw_headline") or "",
                    "disc_date":    (r.get("timestamp") or r.get("created_at") or "")[:10],
                    "created_at":   r.get("created_at") or "",
                    "staleness":    meta.get("staleness") or "unknown",
                    "sector":       meta.get("sec_etf") or meta.get("source") or "",
                    "amount_range": meta.get("source") or "",
                    "corroborated": meta.get("routing") in ("QUEUE", "WATCH"),
                    "corroboration_note": meta.get("corroboration_note") or "",
                    "is_spousal":   bool(meta.get("is_spousal")),
                    "source":       r.get("source") or "",
                    "is_stale":     meta.get("routing") == "STALE",
                    "image":        meta.get("image_url") or meta.get("image"),
                    "image_url":    meta.get("image_url") or meta.get("image"),
                })
            return out

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

    # ── SECTOR SCREENING ──────────────────────────────────────────────────

    def write_screening_run(self, run_id, sector, etf, etf_5yr_return, candidates):
        """
        Write a full set of screening candidates for one screener run.
        candidates: list of dicts with keys:
            ticker, company, etf_weight_pct
        All signal fields start as 'pending'; Scout and Pulse fill them in.
        """
        now = self.now()
        with self.conn() as c:
            for cd in candidates:
                c.execute("""
                    INSERT INTO sector_screening
                        (run_id, sector, etf, etf_5yr_return, ticker, company,
                         etf_weight_pct, news_signal, sentiment_signal,
                         congressional_flag, momentum_score, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, sector, etf, etf_5yr_return,
                    cd['ticker'], cd.get('company', ''),
                    cd.get('etf_weight_pct', 0.0),
                    'pending', 'pending', 'none',
                    # momentum_score persisted 2026-04-21 — it's the
                    # per-ticker 0-1 score from calc_momentum_score.
                    # Primary filter column for candidate_generator.
                    cd.get('momentum_score'),
                    'considering', now,
                ))
            # Issue screening requests for Scout and Pulse
            for cd in candidates:
                for req_type in ('news', 'sentiment'):
                    c.execute("""
                        INSERT INTO screening_requests
                            (run_id, requested_by, ticker, request_type, status, created_at)
                        VALUES (?,?,?,?,?,?)
                    """, (run_id, 'sector_screener', cd['ticker'], req_type, 'pending', now))

    def get_pending_screening_requests(self, request_type):
        """Return pending requests of a given type (news or sentiment)."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM screening_requests
                WHERE request_type=? AND status='pending'
                ORDER BY created_at ASC
            """, (request_type,)).fetchall()
            return [dict(r) for r in rows]

    def fulfill_screening_request(self, run_id, ticker, request_type,
                                  signal, score, headline=None, notes=None):
        """
        Called by Scout (request_type='news') or Pulse (request_type='sentiment')
        to write results back into sector_screening and mark the request fulfilled.
        """
        now = self.now()
        with self.conn() as c:
            if request_type == 'news':
                c.execute("""
                    UPDATE sector_screening
                    SET news_signal=?, news_score=?, news_headline=?
                    WHERE run_id=? AND ticker=?
                """, (signal, score, headline, run_id, ticker))
            elif request_type == 'sentiment':
                c.execute("""
                    UPDATE sector_screening
                    SET sentiment_signal=?, sentiment_score=?, notes=?
                    WHERE run_id=? AND ticker=?
                """, (signal, score, notes, run_id, ticker))
            # Recompute combined_score and update status
            row = c.execute("""
                SELECT news_score, sentiment_score, etf_weight_pct
                FROM sector_screening WHERE run_id=? AND ticker=?
            """, (run_id, ticker)).fetchone()
            if row:
                ns = row['news_score'] or 0.0
                ss = row['sentiment_score'] or 0.0
                wt = min((row['etf_weight_pct'] or 0.0) / 25.0, 1.0)  # normalise weight
                combined = round(ns * 0.40 + ss * 0.40 + wt * 0.20, 4)
                c.execute("""
                    UPDATE sector_screening SET combined_score=?
                    WHERE run_id=? AND ticker=?
                """, (combined, run_id, ticker))
            # Mark request fulfilled
            c.execute("""
                UPDATE screening_requests
                SET status='fulfilled', fulfilled_at=?
                WHERE run_id=? AND ticker=? AND request_type=?
            """, (now, run_id, ticker, request_type))

    def flag_congressional_screening(self, ticker, flag):
        """
        Called by Bolt when it spots a congressional signal for a ticker
        that is currently under sector screening consideration.
        flag: 'recent_buy' | 'recent_sell' | 'none'
        """
        with self.conn() as c:
            # Only update the most recent run's row for this ticker
            c.execute("""
                UPDATE sector_screening SET congressional_flag=?
                WHERE ticker=? AND id=(
                    SELECT id FROM sector_screening WHERE ticker=?
                    ORDER BY created_at DESC LIMIT 1
                )
            """, (flag, ticker, ticker))

    def get_latest_screening_run(self, sector=None):
        """Return candidates from the most recent screener run.

        If sector is None, returns candidates for ALL sectors in the latest
        run (one row per ticker across all 11 sectors).
        If sector is given, restricts to that sector.
        """
        with self.conn() as c:
            row = c.execute("""
                SELECT run_id FROM sector_screening
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()
            if not row:
                return []
            run_id = row['run_id']
            if sector:
                rows = c.execute("""
                    SELECT * FROM sector_screening
                    WHERE run_id=? AND sector=?
                    ORDER BY combined_score DESC, etf_weight_pct DESC
                """, (run_id, sector)).fetchall()
            else:
                rows = c.execute("""
                    SELECT * FROM sector_screening WHERE run_id=?
                    ORDER BY combined_score DESC, etf_weight_pct DESC
                """, (run_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_sector_screening_summary(self):
        """Per-sector summary of the latest screener run. Used by the dashboard
        dropdown — each row has (sector, etf, etf_5yr_return, top_ticker,
        top_score, candidate_count). Sorted so the highest-scoring sector's
        top candidate surfaces first (dashboard default selection)."""
        with self.conn() as c:
            row = c.execute("""
                SELECT run_id FROM sector_screening
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()
            if not row:
                return []
            run_id = row['run_id']
            rows = c.execute("""
                SELECT sector, etf, etf_5yr_return,
                       COUNT(*) AS candidate_count,
                       MAX(combined_score) AS top_score
                FROM sector_screening
                WHERE run_id=?
                GROUP BY sector, etf, etf_5yr_return
                ORDER BY top_score DESC
            """, (run_id,)).fetchall()
            summary = []
            for r in rows:
                d = dict(r)
                # Pick up the top-scoring ticker for this sector
                top = c.execute("""
                    SELECT ticker FROM sector_screening
                    WHERE run_id=? AND sector=?
                    ORDER BY combined_score DESC LIMIT 1
                """, (run_id, d['sector'])).fetchone()
                d['top_ticker'] = top['ticker'] if top else None
                d['run_id'] = run_id
                summary.append(d)
            return summary

    def get_screening_score(self, ticker):
        """Return the most recent screening data for a ticker, or None."""
        with self.conn() as c:
            row = c.execute("""
                SELECT combined_score, news_signal, sentiment_signal,
                       congressional_flag, sector
                FROM sector_screening
                WHERE ticker = ?
                ORDER BY created_at DESC LIMIT 1
            """, (ticker,)).fetchone()
            return dict(row) if row else None

    # ── ADMIN ALERTS ───────────────────────────────────────────────────────
    # Anything that means "the system has a problem, not something the
    # customer can fix" writes here. Customers never see these; the admin
    # portal surfaces them and the admin resolves.

    def add_admin_alert(self, category, severity, title, body='',
                        source_agent=None, source_customer_id=None,
                        code=None, meta=None):
        """Insert an admin-visible alert. Returns the new alert ID."""
        import json as _json
        meta_str = _json.dumps(meta) if meta and not isinstance(meta, str) else meta
        with self.conn() as c:
            c.execute("""
                INSERT INTO admin_alerts
                    (category, severity, source_agent, source_customer_id, code,
                     title, body, meta, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (category, severity, source_agent, source_customer_id, code,
                  title, body, meta_str, self.now()))
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_admin_alerts(self, limit=100, unresolved_only=True, severity=None):
        """Fetch admin alerts. Newest first. unresolved_only filters by
        resolved=0 for the default admin dashboard view."""
        import json as _json
        sql = "SELECT * FROM admin_alerts WHERE 1=1"
        params = []
        if unresolved_only:
            sql += " AND resolved = 0"
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get('meta'):
                try:
                    d['meta'] = _json.loads(d['meta'])
                except Exception:
                    pass
            result.append(d)
        return result

    def resolve_admin_alert(self, alert_id):
        """Mark an admin alert as resolved."""
        with self.conn() as c:
            c.execute("UPDATE admin_alerts SET resolved=1, resolved_at=? WHERE id=?",
                      (self.now(), alert_id))

    def count_admin_alerts(self, unresolved_only=True):
        """Fast count for the admin badge."""
        sql = "SELECT COUNT(*) FROM admin_alerts"
        if unresolved_only:
            sql += " WHERE resolved = 0"
        with self.conn() as c:
            return c.execute(sql).fetchone()[0]

    # ── ATTRIBUTION FLAGS (news-agent quality audit, 2026-04-21) ──────────
    # News agent writes one row each time ticker attribution for an Alpaca
    # article looks suspicious. Reasons:
    #   'untradable'    — all tagged symbols non-tradable / crypto (Fix A — ENFORCED, signal dropped)
    #   'remap_differs' — re-ranker would pick a different ticker than symbols[0] (Fix C — SHADOW)
    #   'conflict'      — 2+ tagged symbols tie on headline score
    #   'no_match'      — zero tagged symbols match the headline
    # 90-day retention via migration. Close-session digest summarises each
    # day's counts into the `notifications` table for portal review.

    def add_attribution_flag(self, headline, alpaca_symbols, reason,
                             chosen_ticker=None, would_choose=None,
                             tie_candidates=None, best_score=None,
                             article_url=None):
        """Insert an attribution-flag row. Returns the new row id.

        alpaca_symbols: list of ticker symbols as Alpaca tagged the article.
        tie_candidates: list of (symbol, score) tuples for 'conflict' rows.
        Both are JSON-encoded at write time; callers pass Python lists.
        """
        import json as _json
        with self.conn() as c:
            c.execute("""
                INSERT INTO signal_attribution_flags
                    (created_at, headline, article_url, alpaca_symbols,
                     chosen_ticker, would_choose, reason, tie_candidates,
                     best_score)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (self.now(), headline[:500] if headline else '',
                  article_url, _json.dumps(list(alpaca_symbols or [])),
                  chosen_ticker, would_choose, reason,
                  _json.dumps(tie_candidates) if tie_candidates else None,
                  best_score))
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_attribution_flag_counts(self, since_hours=24):
        """Return dict of {reason: count} for flags created in the last
        `since_hours`. Used by close-session digest and portal."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT reason, COUNT(*) AS cnt FROM signal_attribution_flags "
                "WHERE created_at >= datetime('now', ?) "
                "GROUP BY reason",
                (f'-{int(since_hours)} hours',)
            ).fetchall()
        return {r['reason']: r['cnt'] for r in rows}

    def get_attribution_flags(self, reason=None, limit=50, unresolved_only=True):
        """Fetch recent attribution flags for review. Newest first."""
        import json as _json
        sql = "SELECT * FROM signal_attribution_flags WHERE 1=1"
        params = []
        if unresolved_only:
            sql += " AND resolved = 0"
        if reason:
            sql += " AND reason = ?"
            params.append(reason)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for k in ('alpaca_symbols', 'tie_candidates'):
                if d.get(k):
                    try:
                        d[k] = _json.loads(d[k])
                    except Exception:
                        pass
            result.append(d)
        return result

    # ── TICKER → SECTOR CACHE ──────────────────────────────────────────────

    def get_ticker_sector(self, ticker):
        """Return the cached (sector, industry, source) tuple for a ticker, or None."""
        with self.conn() as c:
            row = c.execute("""
                SELECT sector, industry, source, confidence, updated_at
                FROM ticker_sectors WHERE ticker=?
            """, (ticker.upper().strip(),)).fetchone()
            return dict(row) if row else None

    def set_ticker_sector(self, ticker, sector, industry=None,
                          source='manual', confidence='high'):
        """Upsert a ticker → sector mapping."""
        with self.conn() as c:
            c.execute("""
                INSERT INTO ticker_sectors (ticker, sector, industry, source,
                                            confidence, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET
                    sector=excluded.sector,
                    industry=excluded.industry,
                    source=excluded.source,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
            """, (ticker.upper().strip(), sector, industry, source,
                  confidence, self.now()))

    # ── INCREMENTAL-FETCH CURSORS ──────────────────────────────────────────

    def get_fetch_cursor(self, source_name):
        """Return the cursor value for a source, or None if not yet set.
        `source_name` is an arbitrary string (e.g. 'alpaca_news_main')."""
        with self.conn() as c:
            row = c.execute(
                "SELECT cursor_value, articles_seen, updated_at "
                "FROM fetch_cursors WHERE source_name=?",
                (source_name,)
            ).fetchone()
            return dict(row) if row else None

    def set_fetch_cursor(self, source_name, cursor_value, articles_seen=None):
        """Upsert a cursor. If articles_seen is given, adds to the running total."""
        with self.conn() as c:
            existing = c.execute(
                "SELECT articles_seen FROM fetch_cursors WHERE source_name=?",
                (source_name,)
            ).fetchone()
            current_seen = (existing['articles_seen'] if existing else 0)
            new_seen = current_seen + (articles_seen or 0)
            c.execute("""
                INSERT INTO fetch_cursors (source_name, cursor_value, articles_seen, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(source_name) DO UPDATE SET
                    cursor_value=excluded.cursor_value,
                    articles_seen=excluded.articles_seen,
                    updated_at=excluded.updated_at
            """, (source_name, cursor_value, new_seen, self.now()))

    def get_tickers_needing_sector(self, limit=200):
        """Return distinct tickers from positions + recent signals that have no
        sector set (or have 'Unknown') and no row in ticker_sectors. Used by
        retail_sector_backfill_agent."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT DISTINCT ticker FROM (
                    SELECT ticker FROM positions
                     WHERE status='OPEN'
                       AND (sector IS NULL OR sector='' OR sector='Unknown')
                    UNION
                    SELECT ticker FROM signals
                     WHERE status IN ('QUEUED','EVALUATED','ACTED_ON')
                       AND (sector IS NULL OR sector='' OR sector='Unknown')
                )
                WHERE ticker NOT IN (SELECT ticker FROM ticker_sectors)
                LIMIT ?
            """, (limit,)).fetchall()
            return [r['ticker'] for r in rows]

    # ── SYSTEM LOG & HEARTBEAT ─────────────────────────────────────────────

    # ── API CALL TRACKING ───────────────────────────────────────────────

    def log_api_call(self, agent, endpoint, method='GET', service='alpaca',
                     customer_id=None, status_code=None):
        """Record an external API call for rate limit monitoring."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO api_calls (timestamp, agent, service, endpoint, method, customer_id, status_code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.now(), agent, service, endpoint, method, customer_id, status_code))
        except Exception:
            pass  # never let tracking break an agent

    def get_api_call_rate(self, window_seconds=60):
        """Return the count of API calls in the last N seconds. Used by the
        dashboard gauge to surface proximity to Alpaca's 200/min rate limit."""
        try:
            with self.conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) FROM api_calls "
                    "WHERE timestamp >= datetime('now', ?)",
                    (f'-{window_seconds} seconds',)
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    def get_api_call_peak_rate(self, window_seconds=60, lookback_hours=24):
        """Return the peak calls-per-window observed over the past lookback_hours.
        Used to surface the worst case rate over the day (not just right now)."""
        try:
            with self.conn() as c:
                # Bucket timestamps into window_seconds buckets and find max count
                rows = c.execute("""
                    SELECT COUNT(*) as cnt
                    FROM api_calls
                    WHERE timestamp >= datetime('now', ?)
                    GROUP BY strftime('%s', timestamp) / ?
                    ORDER BY cnt DESC
                    LIMIT 1
                """, (f'-{lookback_hours} hours', window_seconds)).fetchone()
                return rows[0] if rows else 0
        except Exception:
            return 0

    def get_api_call_counts(self, date_str=None):
        """Get API call counts for a given date (default today). Returns dict with totals and per-agent breakdown.

        Audit Round 6 — rewritten to use a range predicate
        `timestamp >= day AND timestamp < day+1` so SQLite can use
        `idx_api_calls_ts`. The previous `date(timestamp) = ?` call
        forced a full-table scan because the function wraps the
        indexed column. This is a hot path (portal dashboard fetches
        it on every refresh)."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # Day boundaries as ISO prefixes so SQLite string compares work
        day_start = f"{date_str} 00:00:00"
        day_end   = f"{date_str} 23:59:59"
        try:
            with self.conn() as c:
                total = c.execute(
                    "SELECT COUNT(*) FROM api_calls WHERE timestamp >= ? AND timestamp <= ?",
                    (day_start, day_end)).fetchone()[0]
                by_agent = c.execute(
                    "SELECT agent, COUNT(*) FROM api_calls "
                    "WHERE timestamp >= ? AND timestamp <= ? "
                    "GROUP BY agent ORDER BY COUNT(*) DESC",
                    (day_start, day_end)).fetchall()
                by_service = c.execute(
                    "SELECT service, COUNT(*) FROM api_calls "
                    "WHERE timestamp >= ? AND timestamp <= ? "
                    "GROUP BY service ORDER BY COUNT(*) DESC",
                    (day_start, day_end)).fetchall()
                recent = c.execute(
                    "SELECT timestamp, agent, service, endpoint, method FROM api_calls "
                    "WHERE timestamp >= ? AND timestamp <= ? "
                    "ORDER BY id DESC LIMIT 10",
                    (day_start, day_end)).fetchall()
                return {
                    'date': date_str,
                    'total': total,
                    'by_agent': [{'agent': r[0], 'count': r[1]} for r in by_agent],
                    'by_service': [{'service': r[0], 'count': r[1]} for r in by_service],
                    'recent': [{'timestamp': r[0], 'agent': r[1], 'service': r[2],
                                'endpoint': r[3], 'method': r[4]} for r in recent],
                }
        except Exception:
            return {'date': date_str, 'total': 0, 'by_agent': [], 'by_service': [], 'recent': []}

    def get_api_call_history(self, days=5):
        """Get daily API call totals for the last N market days.

        Audit Round 9.3 — adds a WHERE timestamp >= ? bound so SQLite can
        use idx_api_calls_ts instead of a full-table scan. The previous
        query had no WHERE predicate at all. Mirrors the Round 6.2 fix
        applied to get_api_call_counts()."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
        try:
            with self.conn() as c:
                rows = c.execute(
                    "SELECT date(timestamp) as d, COUNT(*) FROM api_calls "
                    "WHERE timestamp >= ? "
                    "GROUP BY d ORDER BY d DESC LIMIT ?",
                    (since, days)).fetchall()
                return [{'date': r[0], 'total': r[1]} for r in rows]
        except Exception:
            return []

    def cleanup_api_calls(self, keep_days=30):
        """Purge API call records older than keep_days."""
        try:
            with self.conn() as c:
                c.execute(
                    "DELETE FROM api_calls WHERE date(timestamp) < date('now', ?)",
                    (f'-{keep_days} days',))
        except Exception as _e:
            log.debug(f"cleanup_api_calls suppressed: {_e}")

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


    def has_event_today(self, event_type, date_str=None):
        """Check if a specific event type already occurred today. For idempotency guards."""
        if not date_str:
            date_str = self.now()[:10]
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM system_log WHERE event=? AND timestamp LIKE ?",
                (event_type, f"{date_str}%")
            ).fetchone()
            return row is not None

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
                       reasoning='', session='',
                       queue_origin='market', status='PENDING_APPROVAL'):
        """
        Insert or replace a pending approval entry.
        Deduplicates by signal_id — re-queuing the same signal id
        replaces the existing row only if it is still
        PENDING_APPROVAL or QUEUED_FOR_OPEN (i.e. not yet acted on).

        queue_origin = 'market' (default) | 'overnight'
        status       = 'PENDING_APPROVAL' | 'QUEUED_FOR_OPEN'
          MANAGED/SUPERVISED + overnight → ('overnight', 'PENDING_APPROVAL')
          AUTOMATIC + overnight          → ('overnight', 'QUEUED_FOR_OPEN')
          Anything during market hours   → ('market',   'PENDING_APPROVAL')
        """
        if status not in ('PENDING_APPROVAL', 'QUEUED_FOR_OPEN'):
            raise ValueError(f"queue_approval: invalid initial status {status!r}")
        if queue_origin not in ('market', 'overnight'):
            raise ValueError(f"queue_approval: invalid queue_origin {queue_origin!r}")

        now = self.now()
        with self.conn() as c:
            c.execute("""
                INSERT INTO pending_approvals (
                    id, ticker, company, sector, politician,
                    confidence, staleness, headline,
                    price, shares, max_trade, trail_amt, trail_pct,
                    vol_label, reasoning, session,
                    status, queued_at, queue_origin
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                ON CONFLICT(id) DO UPDATE SET
                    ticker          = excluded.ticker,
                    company         = excluded.company,
                    sector          = excluded.sector,
                    politician      = excluded.politician,
                    confidence      = excluded.confidence,
                    staleness       = excluded.staleness,
                    headline        = excluded.headline,
                    price           = excluded.price,
                    shares          = excluded.shares,
                    max_trade       = excluded.max_trade,
                    trail_amt       = excluded.trail_amt,
                    trail_pct       = excluded.trail_pct,
                    vol_label       = excluded.vol_label,
                    reasoning       = excluded.reasoning,
                    session         = excluded.session,
                    status          = excluded.status,
                    queued_at       = excluded.queued_at,
                    queue_origin    = excluded.queue_origin,
                    decided_at      = NULL,
                    decided_by      = NULL,
                    executed_at     = NULL,
                    decision_note   = NULL,
                    reevaluated_at  = NULL,
                    cancelled_reason= NULL
                WHERE pending_approvals.status IN ('PENDING_APPROVAL','QUEUED_FOR_OPEN')
            """, (
                str(signal_id), ticker, company, sector, politician,
                confidence, staleness, headline,
                price, shares, max_trade, trail_amt, trail_pct,
                vol_label, reasoning, session,
                status, now, queue_origin,
            ))
        log.info(f"[DB] Approval queued: {ticker} id={signal_id} "
                 f"status={status} origin={queue_origin}")

    # ── OVERNIGHT QUEUE HELPERS ────────────────────────────────────────
    def get_overnight_queue(self, origin='overnight'):
        """Return all rows awaiting execution/approval that originated
        outside market hours. Used by the pre-open re-evaluation step
        and the dashboard 'pending' card. Sorted oldest first so the
        re-eval cycle processes in stable order."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM pending_approvals "
                "WHERE queue_origin=? "
                "AND status IN ('QUEUED_FOR_OPEN','PENDING_APPROVAL') "
                "ORDER BY queued_at ASC",
                (origin,)
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_reevaluated(self, signal_id):
        """Stamp a row as having been re-evaluated by the pre-open step.
        Does not change status — that's the caller's job based on whether
        the re-eval passed (approve) or failed (cancel_protective)."""
        with self.conn() as c:
            c.execute(
                "UPDATE pending_approvals SET reevaluated_at=? WHERE id=?",
                (self.now(), str(signal_id))
            )

    def cancel_protective(self, signal_id, reason: str):
        """Flip a row to CANCELLED_PROTECTIVE with a human-readable reason
        the portal surfaces on the trade-history overlay. Also sets
        reevaluated_at if not already populated so audit trail is
        complete. Returns True if a row was updated."""
        now = self.now()
        with self.conn() as c:
            result = c.execute("""
                UPDATE pending_approvals
                SET status           = 'CANCELLED_PROTECTIVE',
                    cancelled_reason = ?,
                    reevaluated_at   = COALESCE(reevaluated_at, ?),
                    decided_at       = ?,
                    decided_by       = 'pre_open_reeval'
                WHERE id = ?
                  AND status IN ('QUEUED_FOR_OPEN','PENDING_APPROVAL','APPROVED')
            """, (reason, now, now, str(signal_id)))
            updated = result.rowcount > 0
        if updated:
            log.info(f"[DB] CANCELLED_PROTECTIVE: id={signal_id} reason={reason[:80]}")
        else:
            log.warning(
                f"[DB] cancel_protective: no actionable row for id={signal_id}"
            )
        return updated

    def get_cancelled_protective(self, since_days: int = 14):
        """Return recent CANCELLED_PROTECTIVE rows for the trade-history
        overlay. Default window 14 days — covers a typical review
        period without dragging in years of history."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=since_days)
        ).strftime('%Y-%m-%d %H:%M:%S')
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM pending_approvals "
                "WHERE status='CANCELLED_PROTECTIVE' AND queued_at > ? "
                "ORDER BY queued_at DESC",
                (cutoff,)
            ).fetchall()
            return [dict(r) for r in rows]

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
        Update approval status. Accepts APPROVED, REJECTED, or PENDING_APPROVAL.
        Cannot modify already-EXECUTED rows.
        Returns True if a row was updated, False if not found or wrong state.
        """
        if status not in ('APPROVED', 'REJECTED', 'PENDING_APPROVAL'):
            raise ValueError(f"Invalid status for decision: {status}")
        now = self.now()
        with self.conn() as c:
            # Clear decision fields when revoking back to PENDING_APPROVAL
            if status == 'PENDING_APPROVAL':
                result = c.execute("""
                    UPDATE pending_approvals
                    SET status        = 'PENDING_APPROVAL',
                        decided_at    = NULL,
                        decided_by    = NULL,
                        decision_note = NULL
                    WHERE id = ? AND status IN ('APPROVED', 'REJECTED')
                """, (str(signal_id),))
            else:
                result = c.execute("""
                    UPDATE pending_approvals
                    SET status        = ?,
                        decided_at    = ?,
                        decided_by    = ?,
                        decision_note = ?
                    WHERE id = ? AND status IN ('PENDING_APPROVAL', 'REJECTED', 'APPROVED')
                    AND status != 'EXECUTED'
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
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
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

    # ── NOTIFICATIONS ─────────────────────────────────────────────────────

    # Categories considered "high-signal" — surfaced in the dashboard widget
    # and the bell dropdown. All other categories go to the /notifications
    # full-page archive only (routine 'system' / 'daily' status pings).
    # NOTE: 'alert' is intentionally absent — system-health / validator /
    # fault / bias alerts now flow to admin_alerts (admin-only). The
    # customer-facing notification stream is trades, account milestones,
    # approvals, and support — not meta-commentary on their trading.
    WIDGET_CATEGORIES = ('trade', 'account', 'approval')

    def add_notification(self, category, title, body='', meta=None,
                         dedup_key=None, dedup_window_minutes=None):
        """Insert a notification. Returns the new ID, or the existing ID if a
        duplicate was suppressed via dedup_key.

        dedup_key           — if set, suppresses inserts that collide on this key.
                              Typical values: 'account_ready_bootstrap',
                              'session_complete:open:2026-04-16'.
        dedup_window_minutes — if None, dedup is global (fire once ever).
                              If set, dedup only within the trailing window.
        """
        import json as _json
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        meta_str = _json.dumps(meta) if meta else None
        with self.conn() as c:
            if dedup_key:
                sql = "SELECT id FROM notifications WHERE dedup_key=?"
                params = [dedup_key]
                if dedup_window_minutes:
                    cutoff = (datetime.now(timezone.utc)
                              - timedelta(minutes=dedup_window_minutes)
                              ).strftime('%Y-%m-%d %H:%M:%S')
                    sql += " AND created_at >= ?"
                    params.append(cutoff)
                sql += " ORDER BY created_at DESC LIMIT 1"
                existing = c.execute(sql, params).fetchone()
                if existing:
                    return existing['id']  # suppressed — caller should treat as success

            c.execute(
                "INSERT INTO notifications (category, title, body, created_at, meta, dedup_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (category, title, body, now, meta_str, dedup_key)
            )
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_notifications(self, limit=50, unread_only=False, category=None,
                          widget_only=False, offset=0):
        """Fetch notifications, newest first.

        widget_only — restrict to WIDGET_CATEGORIES (dashboard + bell dropdown).
        offset      — pagination support for the /notifications full page.
        """
        import json as _json
        sql = "SELECT * FROM notifications WHERE 1=1"
        params = []
        if unread_only:
            sql += " AND is_read = 0"
        if category:
            sql += " AND category = ?"
            params.append(category)
        elif widget_only:
            placeholders = ','.join('?' * len(self.WIDGET_CATEGORIES))
            sql += f" AND category IN ({placeholders})"
            params.extend(self.WIDGET_CATEGORIES)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get('meta'):
                try:
                    d['meta'] = _json.loads(d['meta'])
                except Exception:
                    pass
            result.append(d)
        return result

    def count_notifications(self, unread_only=False, category=None, widget_only=False):
        """Total row count matching the same filters as get_notifications —
        used by the full-page view for pagination."""
        sql = "SELECT COUNT(*) FROM notifications WHERE 1=1"
        params = []
        if unread_only:
            sql += " AND is_read = 0"
        if category:
            sql += " AND category = ?"
            params.append(category)
        elif widget_only:
            placeholders = ','.join('?' * len(self.WIDGET_CATEGORIES))
            sql += f" AND category IN ({placeholders})"
            params.extend(self.WIDGET_CATEGORIES)
        with self.conn() as c:
            return c.execute(sql, params).fetchone()[0]

    def get_unread_count(self, widget_only=False):
        """Fast unread notification count.
        widget_only — restrict to WIDGET_CATEGORIES (what the bell actually shows)."""
        sql = "SELECT COUNT(*) FROM notifications WHERE is_read = 0"
        params = []
        if widget_only:
            placeholders = ','.join('?' * len(self.WIDGET_CATEGORIES))
            sql += f" AND category IN ({placeholders})"
            params.extend(self.WIDGET_CATEGORIES)
        with self.conn() as c:
            return c.execute(sql, params).fetchone()[0]

    def mark_notification_read(self, notification_id):
        """Mark a single notification as read."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with self.conn() as c:
            c.execute(
                "UPDATE notifications SET is_read = 1, read_at = ? WHERE id = ?",
                (now, notification_id)
            )

    def mark_all_notifications_read(self, category=None):
        """Mark all notifications as read. Optionally filter by category."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with self.conn() as c:
            if category:
                c.execute(
                    "UPDATE notifications SET is_read = 1, read_at = ? "
                    "WHERE is_read = 0 AND category = ?",
                    (now, category)
                )
            else:
                c.execute(
                    "UPDATE notifications SET is_read = 1, read_at = ? WHERE is_read = 0",
                    (now,)
                )

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
        cutoff_180 = (datetime.now(timezone.utc) - timedelta(days=180)).strftime('%Y-%m-%d %H:%M:%S')
        cutoff_30  = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

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
            r5 = c.execute(
                "DELETE FROM news_feed WHERE created_at < ?", (cutoff_30,)
            )

        log.info(
            f"Cleanup: removed {r1.rowcount} discarded signals, "
            f"{r2.rowcount} scan log entries, "
            f"{r3.rowcount + r4.rowcount} old system log entries, "
            f"{r5.rowcount} news feed entries (30d retention)"
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


_CUSTOMERS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'customers')

def get_customer_db(customer_id: str) -> 'DB':
    """
    Return a DB instance scoped to a specific customer.
    DB lives at data/customers/<customer_id>/signals.db.
    Directory is created on first call if it doesn't exist.
    Each agent process gets its own instance — no cross-customer sharing.
    """
    if not customer_id:
        raise ValueError("customer_id must not be empty")
    customer_dir = os.path.join(_CUSTOMERS_DIR, customer_id)
    os.makedirs(customer_dir, exist_ok=True)
    return DB(path=os.path.join(customer_dir, 'signals.db'))


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
