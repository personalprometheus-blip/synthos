# DATABASE SCHEMA — CANONICAL

**Date:** 2026-03-29
**Version:** v1.2 (retail) / v2.0 (company)
**Authority:** This document is the single source of truth for all Synthos database schema definitions.
**Supersedes:** Schema sections in SYNTHOS_TECHNICAL_ARCHITECTURE.md §2.3 and §3.3

---

## 1. PURPOSE

### Role of databases in Synthos

Synthos uses two SQLite databases, one per node type:

| Database | File | Node | Manages |
|----------|------|------|---------|
| `signals.db` | `${DATA_DIR}/signals.db` | Retail Pi | Trading signals, positions, portfolio, market scans, approvals |
| `company.db` | `${COMPANY_DATA_DIR}/company.db` | Company Pi | Customer records, heartbeats, suggestions, deploy watches, scheduling |

These are independent databases. Neither node queries the other's database directly. Data passes between nodes via the heartbeat HTTP endpoint (retail → company) and via Watchdog's optional db_helpers import when nodes share hardware.

### Scope

- `signals.db` is the authoritative state store for all retail Pi trading activity
- `company.db` is the authoritative state store for all company Pi operational activity
- Both use SQLite with WAL journal mode
- Both enforce foreign keys via `PRAGMA foreign_keys=ON`
- No shared tables exist between the two databases

---

## 2. DATA OWNERSHIP MODEL

### 2.1 signals.db — Retail Pi

**Access layer:** `src/database.py` — `DB` class. All retail agents must use this class. No agent opens raw `sqlite3` connections for writes.

**Write agents and their primary tables:**

| Agent | Primary write targets |
|-------|-----------------------|
| `agent1_trader.py` (ExecutionAgent) | `positions` (open/close), `portfolio`, `ledger`, `outcomes`, `handshakes`, `pending_approvals`, `system_log` |
| `agent2_research.py` (DisclosureResearchAgent) | `signals` (upsert), `config` (last-fetch timestamp) |
| `agent3_sentiment.py` (MarketSentimentAgent) | `scan_log`, `urgent_flags`, `system_log` |
| `portal.py` | `pending_approvals` (status updates), `system_log` |
| `heartbeat.py` | `system_log` |
| `database.py` (schema init) | All tables (CREATE IF NOT EXISTS on boot) |

**Read agents:**

| Agent | Primary read sources |
|-------|----------------------|
| `agent1_trader.py` | `signals`, `positions`, `pending_approvals`, `portfolio`, `urgent_flags`, `handshakes`, `member_weights` |
| `agent2_research.py` | `signals` (dedup check), `config` (last timestamp) |
| `agent3_sentiment.py` | `positions`, `signals` |
| `portal.py` | All tables (status display) |
| `health_check.py` | All tables (integrity check) |
| `watchdog.py` | `signals.db` path check (integrity only — watchdog reads deploy watches from company.db) |

**Concurrency model:**
Agent lock file (`.agent_lock`) coordinates write access. Priority order: agent1 > agent3 > agent4 > agent2 > portal. Portal backs off in 5 seconds; agents wait up to 10 minutes. Lock is file-based, not DB-based. WAL mode allows concurrent reads during writes.

### 2.2 company.db — Company Pi

**Access layer:** `utils/db_helpers.py` — `DB` class. All company agents must use this class for writes. Exceptions documented below.

**Write agents and their primary tables:**

| Agent | Write path | Primary write targets |
|-------|------------|-----------------------|
| `sentinel.py` | `db_helpers.heartbeat_write()` (direct, 5s timeout) | `customers`, `heartbeats`; `silence_alerts` (direct write) |
| `sentinel.py` | `db_helpers.post_suggestion()` (via slot) | `suggestions` |
| `blueprint.py` | `db_helpers.post_suggestion()`, `db_helpers.post_deploy_watch()` (via slot) | `suggestions`, `deploy_watches` |
| `patches.py` | `db_helpers.post_suggestion()` (via slot) | `suggestions` |
| `librarian.py` | `db_helpers.post_suggestion()` (via slot) | `suggestions` |
| `vault.py` | `db_helpers.post_suggestion()` (via slot) | `suggestions`; raw connection for `keys` table |
| `fidget.py` | `db_helpers.log_api_call()` (direct write) | `api_usage`, `token_ledger` |
| `scoop.py` | Raw `sqlite3` connection (direct, 10s timeout) | `scoop_queue` — reads and status updates |
| `timekeeper.py` | Raw `sqlite3` connection (direct, 10s timeout) | `work_requests` — slot grants |
| `strongbox.py` | No DB writes (no db_helpers import found) | `backup_log` — NOT YET WIRED |
| `watchdog.py` (retail) | `db_helpers.post_suggestion()` (via slot, optional) | `suggestions`; reads `deploy_watches` |

**Concurrency model:**
Timekeeper slot system (`work_requests` table). Agents call `db.slot()` before writes; Timekeeper grants/denies based on priority and current state. Sentinel and Fidget use direct writes (short timeouts, must not block HTTP request threads). Scoop and Timekeeper use raw connections — they are the infrastructure agents, not consumers of the slot system.

**Timekeeper relationship:**
Timekeeper reads `work_requests` and updates row status (PENDING → GRANTED → EXECUTING → COMPLETE). It does not use the slot system itself — it IS the slot system. Agents do not call Timekeeper directly; they write to `work_requests` and poll for GRANTED status.

---

## 3. TABLE DEFINITIONS

### 3.1 signals.db Tables (Retail Pi)

---

### Table: portfolio

**Purpose:** Single-row store for current portfolio capital state. Updated on every trade entry, exit, and monthly tax sweep.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key |
| `cash` | REAL | YES | Current available cash |
| `realized_gains` | REAL | YES | Cumulative realized gains since last tax sweep; default 0.0 |
| `tax_withdrawn` | REAL | YES | Total tax swept to date; default 0.0 |
| `month_start` | REAL | YES | Portfolio value at start of current month (for gain calculation) |
| `updated_at` | TEXT | YES | ISO timestamp of last update |

**Primary key:** `id`
**Interacting agents:** agent1_trader.py (read/write), portal.py (read)

---

### Table: positions

**Purpose:** All trade positions — open, closed, and flagged for reconciliation. Never deleted; closed positions are retained for audit and outcome tracking.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | TEXT | YES | Primary key; format `pos_{TICKER}_{YYYYMMDDHHMMSS}` |
| `ticker` | TEXT | YES | Stock ticker symbol |
| `company` | TEXT | NO | Company name |
| `sector` | TEXT | NO | Sector classification |
| `entry_price` | REAL | YES | Price per share at entry |
| `current_price` | REAL | NO | Last known market price (mark-to-market) |
| `shares` | REAL | YES | Number of shares held |
| `trail_stop_amt` | REAL | YES | ATR-based trailing stop dollar amount |
| `trail_stop_pct` | REAL | YES | Trailing stop percentage (display) |
| `vol_bucket` | TEXT | NO | `Low Vol` / `Mid Vol` / `High Vol` |
| `pnl` | REAL | YES | Current unrealized (OPEN) or realized (CLOSED) P&L; default 0.0 |
| `status` | TEXT | YES | `OPEN` / `CLOSED` / `RECONCILE_NEEDED`; default `OPEN` |
| `opened_at` | TEXT | YES | ISO timestamp of position open |
| `closed_at` | TEXT | NO | ISO timestamp of position close |
| `exit_reason` | TEXT | NO | `TRAILING_STOP` / `PROFIT_TAKE` / `MANUAL` |
| `signal_id` | INTEGER | NO | FK → signals.id |
| `entry_sentiment_score` | REAL | NO | Sentiment score at entry time (v1.2 migration) |
| `entry_signal_score` | TEXT | NO | Signal score label at entry time (v1.2 migration) |
| `price_history_used` | TEXT | NO | Whether price history was available at entry (v1.2 migration) |
| `interrogation_status` | TEXT | NO | Interrogation agent result at entry time (v1.2 migration) |

**Primary key:** `id`
**Indexes:** `idx_positions_status` on `status`
**Interacting agents:** agent1_trader.py (read/write), agent3_sentiment.py (read), portal.py (read)

---

### Table: signals

**Purpose:** All trading signals derived from Congressional disclosure filings. Lifecycle: PENDING → WATCHING / QUEUED → ACTED_ON / DISCARDED / EXPIRED / INTERRUPTED.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `ticker` | TEXT | YES | Stock ticker |
| `company` | TEXT | NO | Company name |
| `sector` | TEXT | NO | Sector |
| `source` | TEXT | YES | Data source identifier |
| `source_tier` | INTEGER | YES | 1=Official 2=Wire 3=Press 4=Opinion |
| `headline` | TEXT | NO | Raw disclosure headline |
| `politician` | TEXT | NO | Congressional member name |
| `tx_date` | TEXT | NO | Transaction date from disclosure |
| `disc_date` | TEXT | NO | Disclosure filing date |
| `amount_range` | TEXT | NO | Dollar range from disclosure |
| `confidence` | TEXT | YES | `HIGH` / `MEDIUM` / `LOW` / `NOISE` |
| `staleness` | TEXT | NO | `Fresh` / `Aging` / `Stale` / `Expired` |
| `corroborated` | INTEGER | YES | 0/1 boolean; default 0 |
| `corroboration_note` | TEXT | NO | Notes on corroboration |
| `status` | TEXT | YES | `PENDING` / `WATCHING` / `QUEUED` / `ACTED_ON` / `DISCARDED` / `EXPIRED` / `INTERRUPTED`; default `PENDING` |
| `is_amended` | INTEGER | YES | 0/1; default 0 |
| `is_spousal` | INTEGER | YES | 0/1; default 0 |
| `needs_reeval` | INTEGER | YES | 0/1 flag for re-evaluation queue; default 0 (v1.1 migration) |
| `expires_at` | TEXT | NO | Expiry timestamp (tier-based: T1=30d, T2=7d, T3=2d, T4=1d) |
| `discard_delete_at` | TEXT | NO | Soft-delete timestamp for discarded signals (+30 days) |
| `created_at` | TEXT | YES | ISO timestamp |
| `updated_at` | TEXT | YES | ISO timestamp |
| `price_history_used` | TEXT | NO | Whether price history was consulted (v1.2 migration) |
| `interrogation_status` | TEXT | NO | Interrogation agent outcome (v1.2 migration) |
| `entry_signal_score` | TEXT | NO | Signal score at time of trade entry (v1.2 migration) |

**Primary key:** `id`
**Indexes:** `idx_signals_status` on `status`; `idx_signals_ticker` on `ticker`
**Deduplication:** On insert, checks for existing signal with same `ticker` + `tx_date` not in DISCARDED/EXPIRED status. Duplicate is skipped; existing `id` returned.
**Interacting agents:** agent1_trader.py (read/update status), agent2_research.py (upsert), agent3_sentiment.py (read), portal.py (read)

---

### Table: ledger

**Purpose:** Immutable financial transaction log. One row per capital event (deposit, trade entry, trade exit, tax sweep). Never updated after insert.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `date` | TEXT | YES | Date of transaction |
| `type` | TEXT | YES | `DEPOSIT` / `ENTRY` / `EXIT` / `TAX` / `BIL` |
| `description` | TEXT | NO | Human-readable description |
| `amount` | REAL | YES | Signed amount (negative for outflows) |
| `balance` | REAL | YES | Portfolio cash balance after transaction |
| `position_id` | TEXT | NO | FK → positions.id (for ENTRY/EXIT rows) |
| `created_at` | TEXT | YES | ISO timestamp |

**Primary key:** `id`
**Interacting agents:** agent1_trader.py (write), portal.py (read)

---

### Table: outcomes

**Purpose:** Closed trade results for the learning loop. One row per closed position. Written at position close time.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `position_id` | TEXT | YES | FK → positions.id |
| `ticker` | TEXT | YES | Stock ticker |
| `entry_price` | REAL | YES | Entry price per share |
| `exit_price` | REAL | YES | Exit price per share |
| `shares` | REAL | YES | Shares held |
| `hold_days` | INTEGER | NO | Days position was held |
| `pnl_pct` | REAL | NO | P&L as percentage |
| `pnl_dollar` | REAL | NO | P&L in dollars |
| `signal_tier` | INTEGER | NO | Source tier of originating signal |
| `staleness` | TEXT | NO | Signal staleness at time of entry |
| `vol_bucket` | TEXT | NO | Volatility bucket |
| `exit_reason` | TEXT | NO | `TRAILING_STOP` / `PROFIT_TAKE` / `MANUAL` |
| `verdict` | TEXT | NO | `WIN` / `LOSS` |
| `lesson` | TEXT | NO | Claude-generated lesson text (v1.1 migration) |
| `created_at` | TEXT | YES | ISO timestamp |

**Primary key:** `id`
**Interacting agents:** agent1_trader.py (write), portal.py (read)

---

### Table: handshakes

**Purpose:** Tracks signal hand-off from DisclosureResearchAgent ("The Daily") to ExecutionAgent ("The Trader"). One row per queued signal.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `signal_id` | INTEGER | YES | FK → signals.id |
| `ticker` | TEXT | YES | Stock ticker |
| `from_agent` | TEXT | YES | Source agent name (e.g. "The Daily") |
| `to_agent` | TEXT | YES | Destination agent name (e.g. "The Trader") |
| `queued_at` | TEXT | YES | ISO timestamp when handshake was created |
| `acknowledged_at` | TEXT | NO | ISO timestamp when acknowledged |
| `ack` | INTEGER | YES | 0/1 acknowledgment flag; default 0 |

**Primary key:** `id`
**Interacting agents:** agent2_research.py (write), agent1_trader.py (read/ack)

---

### Table: scan_log

**Purpose:** Results from MarketSentimentAgent's 30-minute scan cycle. Capped by cleanup.py to prevent unbounded growth.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `ticker` | TEXT | YES | Stock ticker scanned |
| `put_call_ratio` | REAL | NO | Options put/call ratio |
| `put_call_avg30d` | REAL | NO | 30-day average put/call ratio |
| `insider_net` | TEXT | NO | Net insider transaction direction |
| `volume_vs_avg` | TEXT | NO | Volume vs. average characterization |
| `seller_dominance` | TEXT | NO | Seller pressure indicator |
| `cascade_detected` | INTEGER | YES | 0/1; default 0 |
| `tier` | INTEGER | YES | 1=Critical 2=Elevated 3=Neutral 4=Quiet |
| `event_summary` | TEXT | NO | Human-readable scan summary |
| `scanned_at` | TEXT | YES | ISO timestamp |

**Primary key:** `id`
**Indexes:** `idx_scan_log_scanned` on `scanned_at`
**Interacting agents:** agent3_sentiment.py (write), portal.py (read)

---

### Table: system_log

**Purpose:** Operational event log — heartbeats, shutdowns, reboots, and notable system events. Retained for portal display and health monitoring.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `timestamp` | TEXT | YES | ISO timestamp of event |
| `event` | TEXT | YES | Event type: `HEARTBEAT` / `SHUTDOWN` / `REBOOT_OK` / etc. |
| `agent` | TEXT | NO | Agent name that generated the event |
| `details` | TEXT | NO | Free-form detail text |
| `portfolio_value` | REAL | NO | Portfolio value at event time (heartbeat rows) |

**Primary key:** `id`
**Indexes:** `idx_system_log_event` on `event`
**Interacting agents:** heartbeat.py (write), agent1_trader.py (write), portal.py (read), health_check.py (read)

---

### Table: urgent_flags

**Purpose:** Cascade alerts that bypass normal session schedule. Written by MarketSentimentAgent; read by ExecutionAgent to trigger protective exits.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `ticker` | TEXT | YES | Stock ticker flagged |
| `detected_at` | TEXT | YES | ISO timestamp of detection |
| `tier` | INTEGER | YES | Alert tier; default 1 |
| `acknowledged` | INTEGER | YES | 0/1; default 0 |
| `acknowledged_at` | TEXT | NO | ISO timestamp of acknowledgment |
| `scan_log_id` | INTEGER | NO | FK → scan_log.id |
| `label` | TEXT | NO | Alert label (v1.1 migration) |

**Primary key:** `id`
**Indexes:** `idx_urgent_flags_ack` on `acknowledged`
**Interacting agents:** agent3_sentiment.py (write), agent1_trader.py (read/ack)

---

### Table: pending_approvals

**Purpose:** Supervised-mode trade approval queue. Full lifecycle is preserved — rows are never deleted. Active queue is filtered by `status = 'PENDING_APPROVAL'`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | TEXT | YES | Primary key (signal id, e.g. "42" or "sig_AAPL_...") |
| `ticker` | TEXT | YES | Stock ticker |
| `company` | TEXT | NO | Company name |
| `sector` | TEXT | NO | Sector |
| `politician` | TEXT | NO | Congressional member |
| `confidence` | TEXT | NO | `HIGH` / `MEDIUM` / `LOW` |
| `staleness` | TEXT | NO | `Fresh` / `Aging` / `Stale` / `Expired` |
| `headline` | TEXT | NO | Disclosure headline |
| `price` | REAL | NO | Proposed entry price |
| `shares` | REAL | NO | Proposed share count |
| `max_trade` | REAL | NO | Maximum trade size |
| `trail_amt` | REAL | NO | Trailing stop dollar amount |
| `trail_pct` | REAL | NO | Trailing stop percentage |
| `vol_label` | TEXT | NO | Volatility label |
| `reasoning` | TEXT | NO | Agent reasoning text |
| `session` | TEXT | NO | Session identifier |
| `status` | TEXT | YES | `PENDING_APPROVAL` / `APPROVED` / `REJECTED` / `EXECUTED` / `EXPIRED`; default `PENDING_APPROVAL` |
| `queued_at` | TEXT | YES | ISO timestamp |
| `decided_at` | TEXT | NO | ISO timestamp of approval/rejection |
| `decided_by` | TEXT | NO | `portal` or agent name |
| `executed_at` | TEXT | NO | ISO timestamp of execution |
| `decision_note` | TEXT | NO | Free-form note from approver |

**Primary key:** `id`
**Indexes:** `idx_approvals_status` on `status`; `idx_approvals_queued` on `queued_at`
**Interacting agents:** agent1_trader.py (write, status update), portal.py (read, approve/reject)

---

### Table: member_weights

**Purpose:** Per-Congressional-member signal reliability scores. Updated after each trade closes via the learning loop.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `congress_member` | TEXT | YES | Primary key — member name |
| `win_count` | INTEGER | YES | Winning trades on this member's signals; default 0 |
| `loss_count` | INTEGER | YES | Losing trades; default 0 |
| `weight` | REAL | YES | Reliability multiplier; floor 0.5, ceiling 1.5; default 1.0 |
| `last_updated` | TEXT | NO | ISO timestamp |

**Primary key:** `congress_member`
**Interacting agents:** agent1_trader.py (read/update), portal.py (read)

---

### Table: news_feed

**Purpose:** All signals seen by DisclosureResearchAgent (Scout/Daily), including low-confidence and discarded entries, before ExecutionAgent (Bolt/Trader) acts. Displayed in portal `/news` page. Cleared after 30 days by cleanup.py.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `timestamp` | TEXT | YES | ISO timestamp of event |
| `congress_member` | TEXT | NO | Congressional member |
| `ticker` | TEXT | NO | Stock ticker |
| `signal_score` | TEXT | NO | Adjusted score: `HIGH` / `MEDIUM` / `LOW` / `NOISE` |
| `sentiment_score` | REAL | NO | Sentiment score at time of filing |
| `raw_headline` | TEXT | NO | Raw disclosure text |
| `metadata` | TEXT | NO | JSON blob of additional fields |
| `source` | TEXT | NO | `CONGRESS` / `RSS` |
| `created_at` | TEXT | YES | ISO timestamp |

**Primary key:** `id`
**Indexes:** `idx_news_feed_created` on `created_at`; `idx_news_feed_ticker` on `ticker`
**Interacting agents:** agent2_research.py (write), portal.py (read)

---

### 3.2 company.db Tables (Company Pi)

---

### Table: customers

**Purpose:** Registry of all retail Pi units. Auto-registered on first heartbeat receipt. Tracks license, contact, and operational status.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `pi_id` | TEXT | YES | Unique Pi identifier (UNIQUE) |
| `license_key` | TEXT | NO | License key (stored; validation deferred — DEFERRED_FROM_CURRENT_BASELINE) |
| `customer_name` | TEXT | NO | Customer name |
| `email` | TEXT | NO | Contact email |
| `status` | TEXT | YES | `ACTIVE` / `INACTIVE` / `SUSPENDED`; default `ACTIVE` |
| `created_at` | DATETIME | YES | Row creation timestamp; default CURRENT_TIMESTAMP |
| `last_heartbeat` | DATETIME | NO | Timestamp of most recent heartbeat |
| `payment_status` | TEXT | YES | `PAID` / `OVERDUE`; default `PAID` |
| `github_fork_access` | INTEGER | YES | 0/1; default 1 |
| `mail_alerts_enabled` | INTEGER | YES | 0/1; default 1 |
| `archived_at` | DATETIME | NO | Soft-delete timestamp |
| `notes` | TEXT | NO | Operator notes |

**Primary key:** `id`
**Indexes:** `idx_customers_status` on `status`; unique on `pi_id`
**Interacting agents:** sentinel.py (write — auto-register + heartbeat update), vault.py (read/update), scoop.py (read for customer-facing notifications)

---

### Table: heartbeats

**Purpose:** Time-series record of all heartbeat payloads received from retail Pis. Retained for trend analysis and silence detection.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `pi_id` | TEXT | YES | Reporting Pi identifier |
| `timestamp` | DATETIME | YES | Timestamp from heartbeat payload |
| `portfolio_value` | REAL | NO | Portfolio value at time of heartbeat |
| `agent_statuses` | TEXT | NO | JSON blob of per-agent status |
| `uptime_seconds` | INTEGER | NO | Pi uptime in seconds |
| `received_at` | DATETIME | YES | Server-side receipt time; default CURRENT_TIMESTAMP |

**Primary key:** `id`
**Indexes:** `idx_heartbeats_pi_time` on `(pi_id, timestamp)`
**Interacting agents:** sentinel.py (write — direct path, 5s timeout)

---

### Table: keys

**Purpose:** License key records managed by Vault. Key issuance, status tracking, and expiry. Enforcement deferred — see RETAIL_LICENSE_DEFERRAL_NOTE.md.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `key` | TEXT | YES | Key value (UNIQUE) |
| `pi_id` | TEXT | YES | Associated Pi identifier |
| `issued_at` | DATETIME | YES | Issue timestamp; default CURRENT_TIMESTAMP |
| `expires_at` | DATETIME | NO | Expiry timestamp |
| `status` | TEXT | YES | `ACTIVE` / `REVOKED` / `EXPIRED` / `SUPERSEDED`; default `ACTIVE` |
| `issued_by` | TEXT | YES | Issuing agent; default `Vault` |
| `notes` | TEXT | NO | Operator notes |

**Primary key:** `id`
**Indexes:** unique on `key`
**Interacting agents:** vault.py (write via raw connection; read)
**Note:** Key enforcement (boot-time validation) is `DEFERRED_FROM_CURRENT_BASELINE`. Table structure is preserved for forward-compatibility.

---

### Table: audit_trail

**Purpose:** Immutable audit log for all significant company Pi agent actions. Append-only; never updated or deleted.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `timestamp` | DATETIME | YES | Event timestamp; default CURRENT_TIMESTAMP |
| `agent` | TEXT | YES | Agent that performed the action |
| `action` | TEXT | YES | Action identifier |
| `target` | TEXT | NO | Target of the action (Pi ID, suggestion ID, etc.) |
| `details` | TEXT | NO | Free-form detail text |
| `outcome` | TEXT | YES | `SUCCESS` / `FAILURE` / `SKIPPED`; default `SUCCESS` |

**Primary key:** `id`
**Interacting agents:** All company agents via `db.audit()` (direct write, 5s timeout, must never block)

---

### Table: suggestions

**Purpose:** Improvement and alert suggestions posted by all company agents. Reviewed by operator via portal or email. Lifecycle: `pending` → `approved` / `rejected` → implementation tracking.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | TEXT | YES | Primary key; UUID |
| `timestamp` | DATETIME | YES | Suggestion creation timestamp |
| `agent` | TEXT | YES | Posting agent name |
| `category` | TEXT | YES | Category tag |
| `title` | TEXT | YES | Short title (max 80 chars in practice) |
| `description` | TEXT | YES | Full description |
| `risk_level` | TEXT | YES | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`; default `LOW` |
| `affected_component` | TEXT | NO | Component affected |
| `affected_customers` | INTEGER | NO | Count of affected customers |
| `tokens_saved_per_week` | INTEGER | NO | Estimated token savings |
| `execution_time_saved` | TEXT | NO | Estimated time savings |
| `estimated_improvement` | TEXT | NO | Improvement description |
| `effort` | TEXT | NO | Effort estimate |
| `complexity` | TEXT | NO | `SIMPLE` / `MODERATE` / `COMPLEX` |
| `approver_needed` | TEXT | YES | Approver identifier; default `you` |
| `trial_run_recommended` | INTEGER | YES | 0/1; default 0 |
| `breaking_changes` | INTEGER | YES | 0/1; default 0 |
| `rollback_difficulty` | TEXT | YES | `EASY` / `MODERATE` / `HARD`; default `EASY` |
| `root_cause` | TEXT | NO | Root cause analysis |
| `solution_approach` | TEXT | NO | Proposed solution |
| `alternative_approaches` | TEXT | NO | Alternatives considered |
| `dependencies` | TEXT | NO | Dependencies |
| `metrics_to_track` | TEXT | NO | JSON array of metric names |
| `status` | TEXT | YES | `pending` / `approved` / `rejected` / `implemented`; default `pending` |
| `status_updated_at` | DATETIME | YES | Timestamp of last status change |
| `approver_notes` | TEXT | NO | Notes from approver |
| `implementation_status` | TEXT | NO | Implementation progress notes |
| `implementation_notes` | TEXT | NO | Implementation detail notes |
| `has_disagreement` | INTEGER | YES | 0/1; default 0 |
| `disagreement_details` | TEXT | NO | Details of disagreement |

**Primary key:** `id`
**Indexes:** `idx_suggestions_status`; `idx_suggestions_agent`; `idx_suggestions_risk`
**Deduplication:** Built into `db_helpers.post_suggestion()` — same agent + same title prefix (60 chars) within `dedupe_hours` (default 24h) is silently skipped.
**Interacting agents:** All company agents (write via `db.post_suggestion()` within slot); portal (read); scoop.py (read for digest delivery)

---

### Table: scoop_queue

**Purpose:** Outbound notification delivery queue. All company agent notifications are written here; Scoop is the sole consumer and sender.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | TEXT | YES | Primary key; UUID |
| `created_at` | DATETIME | YES | Queue entry timestamp; default CURRENT_TIMESTAMP |
| `event_type` | TEXT | YES | Event category identifier |
| `audience` | TEXT | YES | `internal` / `customer`; default `internal` |
| `pi_id` | TEXT | NO | Target Pi ID (for customer-audience events) |
| `payload` | TEXT | YES | JSON blob of event data |
| `status` | TEXT | YES | `pending` / `retry` / `sent` / `failed`; default `pending` |
| `retry_count` | INTEGER | YES | Retry attempt count; default 0 |
| `last_attempt` | DATETIME | NO | Timestamp of last delivery attempt |
| `sent_at` | DATETIME | NO | Timestamp of successful delivery |
| `error_msg` | TEXT | NO | Last error message |

**Primary key:** `id`
**Indexes:** `idx_scoop_status` on `(status, created_at)`; `idx_scoop_pi` on `(pi_id, status)`
**Interacting agents:** All company agents (write via `db.post_scoop_event()` or `post_scoop_event_direct()`); scoop.py (read/update via raw connection — Scoop is the queue consumer)

---

### Table: deploy_watches

**Purpose:** Post-deployment monitoring records. Blueprint posts a watch after each Friday deployment; Watchdog and Patches monitor for rollback trigger conditions during the watch window.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | TEXT | YES | Primary key; UUID |
| `suggestion_id` | TEXT | YES | FK → suggestions.id (which suggestion was deployed) |
| `created_at` | DATETIME | YES | Watch creation timestamp; default CURRENT_TIMESTAMP |
| `deployed_at` | DATETIME | YES | Actual deployment timestamp |
| `watch_duration_hours` | INTEGER | YES | Watch window duration; default 48 |
| `expires_at` | DATETIME | YES | Watch expiry timestamp |
| `affected_files` | TEXT | YES | JSON array of affected file paths |
| `watch_for` | TEXT | YES | JSON array of conditions to watch for |
| `rollback_trigger` | TEXT | NO | Condition string that triggers rollback |
| `status` | TEXT | YES | `active` / `expired` / `triggered`; default `active` |
| `triggered_at` | DATETIME | NO | Timestamp of trigger event |
| `triggered_by` | TEXT | NO | Agent or event that triggered rollback |
| `rollback_executed` | INTEGER | YES | 0/1; default 0 |
| `notes` | TEXT | NO | Operator notes |

**Primary key:** `id`
**Indexes:** `idx_deploy_watches` on `(status, expires_at)`
**Interacting agents:** blueprint.py (write via `db.post_deploy_watch()` within slot); watchdog.py (read via `db.get_active_deploy_watches()`); patches.py (read)

---

### Table: work_requests

**Purpose:** Timekeeper slot coordination. Agents insert rows to request write access; Timekeeper polls and grants/denies based on priority and current state.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `agent_name` | TEXT | YES | Requesting agent name |
| `request_time` | DATETIME | YES | Request timestamp; default CURRENT_TIMESTAMP |
| `duration_requested` | INTEGER | YES | Requested slot duration in seconds |
| `priority` | INTEGER | YES | Priority number (lower = higher priority) |
| `task_type` | TEXT | YES | Task description string |
| `access_type` | TEXT | YES | `WRITE` / `READ`; default `WRITE` |
| `status` | TEXT | YES | `PENDING` / `GRANTED` / `EXECUTING` / `COMPLETE` / `TIMED_OUT` / `PREEMPTED` / `FAILED`; default `PENDING` |
| `scheduled_start` | DATETIME | NO | Scheduled start time (Timekeeper-assigned) |
| `actual_start` | DATETIME | NO | Actual execution start time |
| `actual_end` | DATETIME | NO | Actual execution end time |
| `grant_expires_at` | DATETIME | NO | Grant expiry time |
| `notes` | TEXT | NO | Notes |

**Primary key:** `id`
**Indexes:** `idx_work_requests_status` on `status`
**Interacting agents:** All agents that write (insert request, poll for GRANTED, update to EXECUTING); timekeeper.py (polls all PENDING rows, grants/denies, uses raw connection)

---

### Table: api_usage

**Purpose:** API call tracking across all company agents. Written after every external API call (Anthropic, Alpaca, external services). Used by Fidget for cost analysis and reporting.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `pi_id` | TEXT | NO | Pi identifier (defaults to "company") |
| `agent_name` | TEXT | YES | Calling agent name |
| `api_provider` | TEXT | YES | Provider identifier (e.g. "anthropic", "alpaca") |
| `operation` | TEXT | NO | Operation type |
| `token_count` | INTEGER | YES | Total tokens (for LLM calls); default 0 |
| `call_count` | INTEGER | YES | Number of API calls; default 1 |
| `cost_estimate` | REAL | YES | Estimated cost in USD; default 0.0 |
| `timestamp` | DATETIME | YES | Call timestamp; default CURRENT_TIMESTAMP |

**Primary key:** `id`
**Indexes:** `idx_api_usage_time` on `(agent_name, timestamp)`
**Interacting agents:** fidget.py (write via `db.log_api_call()` — direct write); any agent that imports Fidget

---

### Table: token_ledger

**Purpose:** Granular Anthropic token usage tracking with input/output breakdown. Written in parallel with api_usage for Anthropic calls only. Used for monthly cost reporting.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `agent_name` | TEXT | YES | Calling agent name |
| `timestamp` | DATETIME | YES | Call timestamp; default CURRENT_TIMESTAMP |
| `tokens_used` | INTEGER | YES | Total tokens (input + output) |
| `tokens_input` | INTEGER | YES | Input tokens; default 0 |
| `tokens_output` | INTEGER | YES | Output tokens; default 0 |
| `model` | TEXT | NO | Model identifier |
| `operation` | TEXT | NO | Operation description |
| `cost_estimate` | REAL | YES | Estimated cost in USD; default 0.0 |
| `month` | TEXT | YES | `YYYY-MM` month key for aggregation |

**Primary key:** `id`
**Indexes:** `idx_token_ledger_month` on `(month, agent_name)`
**Interacting agents:** fidget.py (write via `db.log_api_call()` — direct write, Anthropic calls only)

---

### Table: backup_log

**Purpose:** Strongbox backup run results. One row per backup attempt.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | INTEGER | YES | Primary key, autoincrement |
| `pi_id` | TEXT | YES | Pi identifier for which backup was run |
| `timestamp` | DATETIME | YES | Backup run timestamp; default CURRENT_TIMESTAMP |
| `status` | TEXT | YES | `success` / `failed` / `partial` |
| `size_kb` | REAL | NO | Backup size in kilobytes |
| `remote_path` | TEXT | NO | Remote storage path (Cloudflare R2) |
| `files_included` | INTEGER | NO | Number of files backed up |
| `error_msg` | TEXT | NO | Error message on failure |

**Primary key:** `id`
**Indexes:** `idx_backup_log` on `(pi_id, timestamp)`
**Interacting agents:** strongbox.py (write via `db.log_backup()`) — **NOTE: Strongbox does not currently import db_helpers; this wiring is DEFERRED per STRONGBOX_AUDIT.md. Table exists but is not populated in current baseline.**

---

### Table: silence_alerts

**Purpose:** Deduplication record for Sentinel's Pi silence alerts. Prevents repeated alerts for the same Pi within the cooldown window. One row per Pi.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `pi_id` | TEXT | YES | Primary key — Pi identifier |
| `last_alerted` | DATETIME | YES | Timestamp of most recent alert |
| `alert_count` | INTEGER | YES | Total alerts sent for this Pi; default 1 |

**Primary key:** `pi_id`
**Interacting agents:** sentinel.py (read/write via direct connection, 5s timeout)

---

### Table: schema_version

**Purpose:** Migration tracking. Records applied schema versions.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `version` | INTEGER | YES | Primary key — version number |
| `applied_at` | DATETIME | YES | Application timestamp; default CURRENT_TIMESTAMP |
| `description` | TEXT | NO | Version description |

**Primary key:** `version`
**Current value:** version 2 ("v2.0") inserted at bootstrap.

---

## 4. ACCESS PATTERNS

### 4.1 signals.db — Retail Pi access patterns

**Write path:**
All writes go through `database.py` helpers — never raw `sqlite3` connections from agents. The `DB.conn()` context manager handles connection lifecycle, lock check, WAL settings, commit, and rollback.

**Lock model:**
File-based agent lock (`.agent_lock`) coordinates write priority. The lock is checked on every `DB.conn()` call. It is NOT checked during `_init_schema()` (schema bootstrap). Priority callers write the lock file at session start; backoff callers (portal, heartbeat) bail after 5 seconds. The lock is advisory — it does not prevent a lower-priority caller from proceeding after timeout.

**Read path:**
All reads go through `DB.conn()` — same WAL connection. WAL mode allows concurrent reads during a write. No special read coordination is required.

**Stale lock handling:**
Locks older than 10 minutes are cleared automatically. An agent that finds its own lock clears it and proceeds.

**No strict transactional snapshot:**
There is no row-level locking or snapshot isolation beyond SQLite's standard WAL guarantees. Concurrent access is mediated by the file lock, not by SQLite transactions.

### 4.2 company.db — Company Pi access patterns

**Write path (slotted):**
All writes except those on the direct-write path must be performed within `db.slot()`. The slot context manager inserts a `work_requests` row, polls for GRANTED status (Timekeeper grants it), executes work, then releases the slot. Agents must not write to any table outside a slot (except via the direct-write path below).

**Direct-write path (no slot):**
Three categories of writes bypass the slot system:
1. **Sentinel heartbeat path** — `heartbeat_write()`, `post_scoop_event_direct()`, `silence_alert_needed()`, `clear_silence_alert()`: use `_direct_connect()` (5-second timeout). These must never block an HTTP request thread.
2. **Audit trail** — `db.audit()`: uses `_direct_connect()`. Audit records must never fail silently or block.
3. **API usage / token ledger** — `db.log_api_call()`: uses `_direct_connect()`. Must not block agent execution.

**Scoop and Timekeeper:**
Both use raw `sqlite3` connections directly (not the `DB` class):
- Scoop reads and updates `scoop_queue` directly — it is the queue consumer, not a producer that needs slot coordination.
- Timekeeper reads `work_requests` and writes status updates directly — it IS the slot manager, not a consumer of it.

**Read path:**
All reads are performed without a slot. WAL mode supports concurrent readers alongside a writer. `db.query()` is the generic read interface; specific helpers provide structured reads (`get_pending_suggestions()`, `get_active_deploy_watches()`, etc.).

**No strict transactional snapshot:**
Same caveat as signals.db: no row-level locking beyond WAL. Coordination is slot-based (sequenced writes via Timekeeper) rather than lock-based.

---

## 5. CONSISTENCY NORMALIZATION

### 5.1 Inconsistencies found and resolved

| Location | Old claim | Actual state | Resolution |
|----------|-----------|--------------|------------|
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `signals` table: fields `congress_member`, `transaction_type`, `agent_decision`, `status` (PENDING/APPROVED/EXECUTED/SKIPPED) | Actual fields are `ticker`, `source`, `source_tier`, `confidence`, `politician`, `tx_date`, `disc_date`, `amount_range`, `staleness`, etc. | §2.3 replaced with reference to this document |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `positions` table: `ticker UNIQUE`, `portfolio_value` field | Actual: `id` is PK (not unique ticker), no `portfolio_value` column | Same — replaced with reference |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `trades` table exists | No `trades` table in `database.py` schema. Trade outcomes are in `outcomes` + `ledger`. | Same — replaced with reference |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `agent_status` table exists | No `agent_status` table in `database.py` SCHEMA constant. Not in CREATE TABLE IF NOT EXISTS list. | Same — replaced with reference |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `license` table exists | No `license` table in `database.py` schema. License validation is DEFERRED_FROM_CURRENT_BASELINE. | Same — replaced with reference |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 | `config` table exists | No `config` table in `database.py` SCHEMA constant. References to `config` table in agent descriptions may reflect planned or removed functionality. | Same — replaced with reference |
| `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §3.3 | "No changes from v2.0. See SYSTEM_MANIFEST for full schema." | SYSTEM_MANIFEST does not contain a full schema. Company schema is defined in db_helpers.py inline bootstrap. | §3.3 replaced with reference to this document |
| `SYSTEM_MANIFEST.md` | `company.db` referenced but schema not defined | Full schema is in db_helpers.py | Note added pointing to this document |
| `agent1_trader.py` agent description in §2.4 | "Check license validity via `license_validator.py`" (step 2) | `license_validator.py` does not exist; DEFERRED_FROM_CURRENT_BASELINE | Architecture description is stale — not changed here (out of scope for schema normalization) |

### 5.2 Naming conventions — confirmed as-is

| Convention | Retail (signals.db) | Company (company.db) |
|------------|--------------------|-----------------------|
| Table names | snake_case, plural nouns | snake_case, plural nouns |
| Field names | snake_case | snake_case |
| Timestamps | TEXT (ISO string) in older tables; DATETIME in newer tables | DATETIME throughout |
| Boolean fields | INTEGER 0/1 | INTEGER 0/1 |
| UUID primary keys | TEXT (signals, pending_approvals in company) | TEXT (suggestions, scoop_queue, deploy_watches) |
| Auto-increment PKs | INTEGER PRIMARY KEY AUTOINCREMENT | INTEGER PRIMARY KEY AUTOINCREMENT |

**Timestamp type inconsistency (signals.db):** Some tables use `TEXT NOT NULL` for timestamps (portfolio, positions, signals, ledger, outcomes, handshakes, scan_log) while system_log uses `TEXT NOT NULL` with field named `timestamp`. This is the existing convention in database.py — it is not changed here. All values are ISO strings; the type label difference has no runtime effect in SQLite.

### 5.3 Tables in architecture docs with no code basis

The following tables appeared in the pre-normalization architecture doc schema but have **no corresponding CREATE TABLE in database.py**:

| Table | Status |
|-------|--------|
| `trades` | Not present in actual schema — functionality split across `outcomes` (closed trade results) and `ledger` (financial transactions) |
| `agent_status` | Not present in actual schema — agent status is written to `system_log` with `event='HEARTBEAT'`; portal derives status from log reads |
| `license` | Not present — DEFERRED_FROM_CURRENT_BASELINE |
| `config` | Not present in schema constant — `agent2_research.py` description references config table for last-fetch timestamp; this may be written directly or may be an outdated reference. Out of scope for schema normalization — requires separate verification |

---

## 6. KNOWN LIMITATIONS

### 6.1 No strict transactional snapshot

Neither database uses row-level locking or SERIALIZABLE isolation. Both rely on:
- WAL mode for concurrent read safety
- Advisory coordination (file lock for signals.db; Timekeeper slots for company.db)

This means a reader can observe partial write state if it reads between two writes in the same logical transaction. This is an accepted design constraint — the system is single-writer in practice (agents run sequentially by schedule), and the lock/slot systems provide sufficient serialization for normal operation.

### 6.2 Timekeeper slot system is polling-based

Agents poll `work_requests` every 5 seconds for GRANTED status. Under load (many agents requesting slots simultaneously), grant latency can reach 30 minutes before timeout. This is documented behavior, not a defect.

### 6.3 signals.db v1.1/v1.2 migration columns

The v1.1 and v1.2 migration columns (`lesson`, `needs_reeval`, `label`, `price_history_used`, `interrogation_status`, `entry_signal_score`, `entry_sentiment_score`) are added via `ALTER TABLE` at every startup. They appear as NULL for rows created before the migration was applied. This is expected and has no functional impact.

### 6.4 `config` table reference in agent2_research.py description

The architecture document §2.4 references a `config` table for last-fetch timestamp tracking by DisclosureResearchAgent. This table is not defined in `database.py` SCHEMA. The actual mechanism for last-fetch timestamp tracking requires verification in `agent2_research.py` source. This is out of scope for schema normalization — it is flagged here as an unresolved ambiguity.

### 6.5 Strongbox backup_log not yet populated

`backup_log` table exists in company.db schema. Strongbox does not currently import db_helpers or call `db.log_backup()`. The table is present but unpopulated in the current baseline. Future wiring is tracked in `docs/milestones.md` (Backup System Evolution).

### 6.6 Direct connection exceptions in scoop.py and timekeeper.py

Both agents bypass `db_helpers.DB` and use raw `sqlite3.connect()` directly. This is intentional — Scoop is the queue consumer (not a producer subject to slot rules), and Timekeeper is the slot manager itself. These are not policy violations.

---

## 7. SOURCE OF TRUTH DECLARATION

**This document is the authoritative database schema definition for Synthos.**

It supersedes all schema definitions in:
- `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §2.3 (Retail Pi schema)
- `SYNTHOS_TECHNICAL_ARCHITECTURE.md` §3.3 (Company Pi schema)
- Any schema descriptions in `SYSTEM_MANIFEST.md`
- Any inline schema descriptions in agent architecture documents

The ground truth for the schema is always `database.py` (SCHEMA constant + `_run_migrations()`) for signals.db, and `db_helpers.py` (`_bootstrap_inline()`) for company.db. This document reflects those sources as of 2026-03-29.

**All future schema changes must:**
1. Update `database.py` (signals.db) or `db_helpers.py` + `company_schema.sql` (company.db) first
2. Update this document to match
3. Update `_run_migrations()` if the change requires a migration on live databases
4. Revalidate any documentation that references affected tables

---

*Schema extracted from: `synthos_build/src/database.py` (SCHEMA constant, `_run_migrations()`), `synthos-company/utils/db_helpers.py` (`_bootstrap_inline()`, method signatures). Cross-referenced against agent source files to confirm actual table usage.*
