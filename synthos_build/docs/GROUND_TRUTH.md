# SYNTHOS — GROUND TRUTH

**Version:** 1.0
**Date:** 2026-03-29
**Status:** AUTHORITATIVE — supersedes all prior fragmented architecture, manifest, and operations descriptions
**Baseline:** Post-normalization sprint, pre-deployment pipeline
**Phase:** Phase 3 complete → Phase 4 declared

---

## 1. SYSTEM PURPOSE

### What Synthos is

Synthos is a distributed, offline-capable algorithmic trading assistant deployed on Raspberry Pi hardware. It monitors United States Congressional trading disclosures, scores signals using multi-agent analysis, and executes paper trades via the Alpaca API.

The system operates across two physically distinct node types: retail nodes (customer-facing, run the trading agents) and a company node (company-operated, runs infrastructure, monitoring, and key management). Each node type runs its own software repository.

### What problem it solves

Congressional members are required to disclose securities transactions within 45 days. These disclosures are public record. Synthos automates the workflow of fetching disclosures, scoring their signal quality, managing position entry and exit, and reporting portfolio state to the owner of each retail Pi — without requiring the owner to monitor markets or parse disclosure filings manually.

### Scope of the current baseline

The current baseline is a **paper-trading system only**. `TRADING_MODE=PAPER` is enforced. The system is in supervised paper-trading mode. No real capital is deployed. No live trading occurs. The transition to live trading requires explicit human decision by the project lead (Phase 6 gate) and is not automatic.

The current baseline does NOT include:
- Retail license enforcement (deferred — see §9)
- Boot-time company integrity gate (deferred — see §9)
- Tiered backup implementation (deferred — see §7)
- A deployed deployment pipeline (Phase 5, not started)
- A live `update-staging` branch (SYS-B07, open)

---

## 2. TRUST DOMAIN MODEL

Synthos uses a two-domain trust architecture. The company/internal domain is the authority domain. The retail/customer domain is the validated domain.

### Company Domain

**Identity:** A node running with `COMPANY_MODE=true` in `company.env`. This flag is verified by the installer and is the canonical discriminator between company and retail behavior.

**Integrity gate definition:**
The company node uses an internal integrity gate model, fully specified in `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`. The canonical gate checks are:

| Check | Description |
|-------|-------------|
| MODE CHECK | `COMPANY_MODE=true` in `company.env` |
| SECRET PRESENCE | Required keys present and non-empty in `company.env` |
| CRITICAL FILE CHECK | All required agent and utility files present |
| DATABASE INTEGRITY | `company.db` exists and passes `PRAGMA integrity_check` |
| CONFIGURATION SANITY | Required config values structurally present |

**Current enforcement state:**
The installer (`install_company.py`) enforces a partial subset of the canonical gate: MODE CHECK, SENDGRID/email keys, KEY_SIGNING_SECRET, DATABASE_PATH, and all required agent files. `ANTHROPIC_API_KEY` and `MONITOR_TOKEN` are in the canonical set but not currently enforced by the installer. `PRAGMA integrity_check` is not currently run. Full gate enforcement is deferred to pre-release security hardening (Phase 6 gate condition).

**No boot-time gate exists:** There is no `boot_company.py` or equivalent process that evaluates the integrity gate before starting company agents. Current company startup relies on the installer having succeeded at setup time. This gap is known and accepted for the current phase.

**Explicit dependency rules — what the company domain requires:**
- Zero dependency on `license_validator.py`
- Zero dependency on any retail license system
- Zero dependency on any retail node being reachable
- Zero dependency on Vault being pre-operational (Vault is a company agent)
- Zero dependency on internet connectivity

### Retail Domain

**Identity:** A node running without `COMPANY_MODE=true`. Retail Pis are customer-owned Raspberry Pi 2W units.

**Current state — no entitlement enforcement:**
Retail license validation is `DEFERRED_FROM_CURRENT_BASELINE`. In the current release:
- `license_validator.py` is not built and is not a required artifact
- The installer (`install_retail.py`) does not require `license_validator.py` in `REQUIRED_CORE_FILES`
- `LICENSE_KEY` is collected during setup and written to `.env` but is not validated by any running process
- `boot_sequence.py` has no license gate and this is the correct current state, not a defect
- Retail Pis operate without entitlement enforcement

**What is intentionally not enforced (current baseline):**
- Boot-time license validation
- Per-session agent license checks
- Key cache verification
- Online key registry lookup
- Pi ID binding enforcement

Future retail entitlement work is tracked in `docs/milestones.md` (Retail Entitlement / License System). The LICENSE_KEY is preserved in `.env` for forward-compatibility.

**Validation direction:** Company validates retail. Retail never validates company. This direction is fixed and non-negotiable.

---

## 3. SYSTEM ARCHITECTURE

### Node structure

| Node | Hardware | Repo | Role |
|------|----------|------|------|
| `retail_node` | Raspberry Pi 2W | `synthos` (this repo) | Trading agents, portal, signals.db |
| `company_node` | Raspberry Pi 4B (or equivalent Linux host) | `synthos-company` | Operations agents, licensing, backups, monitoring |
| `monitor_node` | Same Pi 4B as company_node | `synthos` / `synthos_monitor.py` | Heartbeat receiver on port 5000 |

The retail node is fully standalone after installation. It does not require a persistent connection to the company node. The heartbeat POST at session end is optional — the retail Pi continues to operate if the company Pi is unreachable.

### Runtime environment

- OS: Raspberry Pi OS Lite (Debian-based); `aarch64`, `armv7l`, `armv6l`; non-Pi Linux hosts permitted with operator confirmation
- Python: 3.9+ minimum, `python3` interpreter
- Databases: SQLite with WAL journal mode (signals.db on retail, company.db on company)
- No username hardcoding: all paths derived dynamically from the running script's location
- Network: required at install; optional at runtime (outbound-only on retail, to approved IPs only)
- Outbound traffic (retail): enforced by iptables rules written at install time; approved IP list in `config/allowed_outbound_ips.json`

### Agent-based system model

All agents run as scheduled processes (cron) or persistent background processes. There is no central process manager or supervisor process (other than `watchdog.py` on the retail node and the OS cron daemon). Agents are stateless across sessions — they read current state from the database at each run and do not hold in-memory state between sessions.

**Communication rule:** No retail agent sends email or external alerts directly. Urgent conditions are written to the database; Scoop on the company Pi handles all outbound delivery.

---

### 3.1 Retail Node Agents

#### ExecutionAgent (`agent1_trader.py`)

**Role:** Primary trade execution engine.

**Responsibilities:**
- Runs 3×/day during market hours (9:30am, 12:30pm, 3:30pm ET)
- Checks kill switch at session start — halts immediately if `.kill_switch` present
- Loads operating mode (SUPERVISED or AUTONOMOUS) from `.env`
- In SUPERVISED mode: proposes trades, writes to `pending_approvals` table, waits for portal approval
- In AUTONOMOUS mode: executes on pre-defined rules with AUTONOMOUS_UNLOCK_KEY verification
- Reads PENDING/QUEUED signals from `signals` table
- Executes approved trades via Alpaca API
- Opens and closes positions; updates `portfolio`, `ledger`, `outcomes`
- Checks open positions against urgent flags from MarketSentimentAgent; executes protective exits if flagged
- Writes session-end heartbeat via `synthos_heartbeat.py`
- Reads state from database on every run — never re-initializes existing records

**Key interactions:** signals table (read), positions table (read/write), portfolio (read/write), pending_approvals (write), urgent_flags (read/ack), handshakes (read/ack), member_weights (read/update), synthos_heartbeat.py (call on session end)

---

#### DisclosureResearchAgent (`agent2_research.py`)

**Role:** Signal sourcing from Congressional disclosure filings.

**Responsibilities:**
- Fetches Congressional trade disclosures from official sources
- Deduplicates signals: same ticker + tx_date = same disclosure; skips if already in signals table
- Scores signals by source tier (T1=Official T2=Wire T3=Press T4=Opinion), confidence, and staleness
- Tier 4 signals are immediately discarded on insert
- Re-evaluates up to 10 WATCH-status signals per run when `needs_reeval` flag is set
- Tracks last-fetched timestamp to avoid reprocessing
- Writes all signals to `signals` table and raw events to `news_feed` table
- Writes session-end heartbeat via `synthos_heartbeat.py`
- Does NOT send alerts

**Key interactions:** signals table (upsert), news_feed (write), synthos_heartbeat.py (call on session end)

---

#### MarketSentimentAgent (`agent3_sentiment.py`)

**Role:** Market surveillance and cascade detection.

**Responsibilities:**
- Runs every 30 minutes via cron
- Scans open positions for adverse sentiment (put/call ratios, insider activity, volume anomalies)
- Writes scan results to `scan_log` table
- Writes `urgent_flags` when cascade detected
- Urgent flags bypass normal session schedule and trigger protective exits in ExecutionAgent
- Updates sentiment scores in database for ExecutionAgent to consume on its next run
- Does NOT send alerts

**Key interactions:** positions table (read), signals table (read), scan_log (write), urgent_flags (write)

---

#### Portal (`portal.py`)

**Role:** Local web interface for the retail Pi owner.

**Responsibilities:**
- Serves on port 5001
- Kill switch control
- Trade approval queue (SUPERVISED mode)
- Portfolio and position display
- Signal and news feed display
- Agent status display
- Settings adjustment (confidence threshold, max signals per run)
- Not accessible directly from the public internet (web portal proxy layer handles external access)

---

#### Watchdog (`watchdog.py`)

**Role:** Process monitor and crash recovery.

**Responsibilities:**
- Monitors all three trading agents for crashes
- Auto-restarts crashed agents (maximum 3 attempts per agent)
- Maintains a known-good snapshot in `.known_good/`
- Can restore from known-good snapshot if rollback is triggered
- Reads deploy watches from `company.db` via `db_helpers.get_active_deploy_watches()` when company utils are available (same-Pi deployment or shared mount)
- Posts alerts to `company.db` via `db_helpers.post_suggestion()` when available
- Falls back to local log gracefully when company Pi is unreachable

---

### 3.2 Company Node Agents

#### Blueprint (`blueprint.py`)

**Role:** Autonomous code improvement and deployment.

**Responsibilities:**
- Processes approved suggestions from the `suggestions` table
- Implements approved changes in the sandbox branch during Mon–Thu build window
- Posts suggestions via `db_helpers.post_suggestion()` within a Timekeeper slot
- After Friday push: calls `db_helpers.post_deploy_watch()` to register a 48-hour monitoring window
- Does not touch production code or live DB during the build window

---

#### Sentinel (`sentinel.py`)

**Role:** Customer Pi health monitor.

**Responsibilities:**
- Runs the HTTP heartbeat endpoint on port 5004
- Receives heartbeat POSTs from retail Pis (HMAC-signed with `MONITOR_TOKEN`)
- Validates HMAC signature; rejects unsigned or tampered payloads with 401
- Writes accepted heartbeats directly to `customers` and `heartbeats` tables (direct write, 5-second timeout — must never block HTTP request thread)
- Detects Pi silence (no heartbeat within expected window)
- Files silence alerts via `db_helpers.post_suggestion()` within a Timekeeper slot; deduplicates via `silence_alerts` table
- Posts Scoop events for customer-facing notifications

---

#### Scoop (`scoop.py`)

**Role:** Sole outbound communication channel.

**Responsibilities:**
- The only agent permitted to send external email
- Polls `scoop_queue` table every 30 seconds for pending events
- Delivers email via SendGrid
- Handles retry logic (status: pending → retry → sent / failed)
- Consumes events written by all other agents via `post_scoop_event()` or `post_scoop_event_direct()`
- Delivers morning report written by Patches to project lead at 8am ET
- Uses raw SQLite connection directly (it is the queue consumer, not a producer subject to slot rules)

**Critical rule:** No other agent may send external email. All outbound notifications route through Scoop.

---

#### Patches (`patches.py`)

**Role:** Continuous system audit and morning report author.

**Responsibilities:**
- Runs daily: light pass every few hours, deep pass at 6am ET
- Deep pass includes DB integrity check, API reachability, log pattern analysis, crash report triage
- Writes the weekly morning digest (delivered by Scoop at 8am Friday)
- Posts bug reports and suggestions via `db_helpers.post_suggestion()` within a Timekeeper slot
- Monitors post-deployment watchdog windows (reads `deploy_watches` via `db_helpers.get_active_deploy_watches()`)
- Accountable for knowing what is wrong before the project lead does

---

#### Strongbox (`strongbox.py`)

**Role:** Backup manager.

**Responsibilities:**
- Runs nightly at 11pm ET (cron `0 23 * * *`, registered by `install_company.py`)
- Verified as present in `REQUIRED_AGENT_FILES` by installer
- Current implementation: daily full backup to Cloudflare R2 with Fernet encryption (651-line Python agent)
- Does NOT currently import `db_helpers` or write to `backup_log` table — this wiring is deferred
- Canonical target backup policy: tiered model (monthly baseline + nightly incremental + 6-month retention) defined in `docs/specs/BACKUP_STRATEGY_INITIAL.md` — **NOT YET IMPLEMENTED**

See §7 for full backup state detail.

---

#### Timekeeper (`timekeeper.py`)

**Role:** Database write coordinator.

**Responsibilities:**
- Manages the `work_requests` table in company.db
- Polls PENDING work requests and grants/denies based on priority and concurrency rules
- Prevents write conflicts between company agents
- All company agents (except those on the direct-write path) must obtain a slot from Timekeeper before any DB write
- Does NOT use the slot system itself — it IS the slot system
- Uses raw SQLite connection directly

---

#### Vault (`vault.py`)

**Role:** License key management and compliance.

**Responsibilities:**
- Issues, tracks, and revokes license keys in the `keys` table
- Distributes `allowed_outbound_ips.json` updates to retail Pis
- Posts suggestions and alerts via `db_helpers.post_suggestion()` within a Timekeeper slot
- License enforcement (boot-time validation) is DEFERRED_FROM_CURRENT_BASELINE — Vault manages the key data, but `license_validator.py` is not yet built and is not called by any retail agent

---

#### Fidget (`fidget.py`)

**Role:** API cost monitoring.

**Responsibilities:**
- Tracks all external API calls by all company agents
- Writes to `api_usage` and `token_ledger` tables via `db_helpers.log_api_call()` (direct write)
- Produces cost reports and flags anomalies
- Posts cost-related suggestions via `db_helpers.post_suggestion()`

---

#### Librarian (`librarian.py`)

**Role:** Security and dependency compliance.

**Responsibilities:**
- Audits retail Pi iptables rules against `allowed_outbound_ips.json`
- Scans for CVEs in dependencies
- Posts compliance findings as suggestions via `db_helpers.post_suggestion()` within a Timekeeper slot

---

## 4. DATA MODEL

**Authoritative schema reference:** `docs/specs/DATABASE_SCHEMA_CANONICAL.md`

This section summarizes ownership and purpose. Do not duplicate schema details here — read the canonical document for all table definitions, field types, indexes, and access patterns.

### signals.db (Retail Pi)

**Purpose:** Complete state store for all retail Pi trading activity — signals lifecycle, trade positions, portfolio value, market scans, and system health.

**File:** `${DATA_DIR}/signals.db`
**Access layer:** `src/database.py` — `DB` class. All retail agents use this class. No direct `sqlite3` connections from agent code.
**Schema version:** v1.2 (12 tables; v1.1 and v1.2 migration columns applied at every startup via `_run_migrations()`)
**Tables (summary):** portfolio, positions, signals, ledger, outcomes, handshakes, scan_log, system_log, urgent_flags, pending_approvals, member_weights, news_feed

**Concurrency model:** File-based agent lock (`.agent_lock`). Write priority: ExecutionAgent > MarketSentimentAgent > DisclosureResearchAgent > portal. Portal backs off in 5 seconds; agents wait up to 10 minutes. WAL mode supports concurrent reads during writes.

### company.db (Company Pi)

**Purpose:** Complete state store for company Pi operational activity — customer registry, heartbeats, suggestions, deploy watches, outbound message queue, and scheduling.

**File:** `${COMPANY_DATA_DIR}/company.db`
**Access layer:** `utils/db_helpers.py` — `DB` class. All company agents use this class. Exceptions: Scoop and Timekeeper use raw connections (they are infrastructure, not producers subject to slot rules). Sentinel, Fidget, and audit trail use the direct-write path (5-second timeout, must not block HTTP threads).
**Schema version:** v2.0 (13 tables)
**Tables (summary):** customers, heartbeats, keys, audit_trail, suggestions, scoop_queue, deploy_watches, work_requests, api_usage, token_ledger, backup_log, silence_alerts, schema_version

**Concurrency model:** Timekeeper slot system (`work_requests` table). Agents request a slot, Timekeeper grants it, agent writes within the slot, slot is released. Direct-write path bypasses slots for time-sensitive operations (heartbeat receipt, audit, API logging).

---

## 5. CONTROL FLOWS

### 5.1 Suggestion Pipeline

```
Any company agent detects a finding
  │
  ├─ Requests Timekeeper slot (work_requests INSERT → poll for GRANTED)
  │
  ├─ Calls db_helpers.post_suggestion() — writes to suggestions table
  │   └─ Built-in deduplication: same agent + same title prefix + within 24h → skipped
  │
  ├─ Releases slot
  │
  └─ Scoop polls scoop_queue for pending events → delivers email to project lead

Project lead reviews in portal or email
  │
  └─ Approves / rejects suggestion → status updated in suggestions table
       └─ Blueprint picks up approved suggestions during Mon-Thu build window
```

**Critical rule:** All suggestion writes must go through `db_helpers.post_suggestion()`. No agent writes directly to a suggestions file or table via raw SQL. `SUGGESTIONS_JSON_SPEC.md` is superseded — suggestions.json is retired.

### 5.2 Deployment Pipeline

```
Mon–Thu: Build Window
  Blueprint processes approved suggestions in sandbox branch
  Patches reviews implementations, flags concerns
  Librarian checks new dependencies
  No production changes; no live DB access

Thursday EOD:
  Changes staged to update-staging branch
  [NOTE: update-staging branch is not yet created — SYS-B07 STILL_OPEN]

Friday after market close (4pm ET):
  1. Project lead reviews Pending Changes package in command portal
  2. Project lead approves or rejects each change
  3. Approved changes merge to main
  4. Pi cron jobs pull the update
  5. Watchdog activates heightened monitoring (48 hours)
  6. Blueprint posts deploy_watch record via db_helpers.post_deploy_watch()

Saturday–Sunday: Correction Window
  Watchdog monitors all Pis continuously
  Patches watches for regressions, crash patterns
  Blueprint on standby for hot-fixes
  Sunday morning deadline: unresolvable regressions → full rollback
```

**Current state:** The deployment pipeline is defined but not yet executable. `update-staging` branch does not exist (SYS-B07). End-to-end pipeline test has not been performed (Phase 5, not started).

### 5.3 Rollback Mechanism

```
Watchdog (retail):
  Monitors active deploy_watches from company.db
  If rollback_trigger condition is met:
    → Restores from .known_good/ snapshot
    → Posts alert to company.db via db_helpers.post_suggestion()

Manual rollback (project lead):
  Sunday morning deadline — if regression unresolved → git reset to prior known-good tag
  Watchdog .known_good/ snapshot restored to all affected Pis
```

**Post-deploy watch:** `deploy_watches` table records the deployed suggestion ID, affected files, watch conditions, rollback trigger, and 48-hour expiry. Watchdog and Patches read this table to know what to watch for.

### 5.4 Monitoring and Alert Flow

```
Retail Pi:
  Agent runs → writes session-end heartbeat via synthos_heartbeat.py
    → HTTP POST to company Pi port 5004 (HMAC-signed with MONITOR_TOKEN)
    → Sentinel validates signature → writes heartbeats + customers tables (direct write)

  Agent detects urgent condition → writes urgent_flag to signals.db
    → ExecutionAgent reads on next session → executes protective exit

Company Pi:
  Sentinel monitors heartbeat age per Pi
  If Pi goes silent → silence_alert_needed() → post_suggestion() → Scoop delivers email

  Patches daily pass → writes morning report findings to suggestions table
  Scoop delivers morning report email at 8am ET

  Patches/Watchdog monitor active deploy_watches
  If rollback trigger → alert via post_suggestion()
```

### 5.5 Backup Flow (Current State)

```
Nightly at 11pm ET (cron 0 23 * * *):
  Strongbox runs → daily full backup to Cloudflare R2 with Fernet encryption
  [backup_log table NOT populated — db_helpers wiring not yet implemented]

Canonical policy target (NOT YET IMPLEMENTED):
  See docs/specs/BACKUP_STRATEGY_INITIAL.md
  Monthly full baseline + nightly incremental chain + 6-month retention
```

See §7 for full backup state detail.

---

## 6. INSTALLATION AND BOOT

### 6.1 Retail Node Installer (`install_retail.py`)

**Entry point:** `python3 install_retail.py`
**Modes:** normal install/resume; `--repair` (re-runs INSTALLING + VERIFYING); `--status`
**Web wizard:** port 8080 during install

**State machine:**
```
UNINITIALIZED → PREFLIGHT → COLLECTING → INSTALLING → VERIFYING → COMPLETE
                                                    ↘ DEGRADED (on failure)
```

**PREFLIGHT:** Verifies Python version, network reachability, disk space, system packages.

**COLLECTING:** Web wizard collects: Alpaca API keys, Anthropic API key, SendGrid credentials, Alpaca account number, PI_ID, `LICENSE_KEY` (collected and written to `.env`; not validated), trading mode, STARTING_CAPITAL, alert phone/email.

**INSTALLING:** Installs Python packages, creates directory structure, writes `.env` from collected values, bootstraps `signals.db` schema.

**VERIFYING:** Checks that all files in `REQUIRED_CORE_FILES` are present. Checks required `.env` keys: `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `TRADING_MODE`. Does NOT check `LICENSE_KEY` for validity (DEFERRED_FROM_CURRENT_BASELINE).

**Current REQUIRED_CORE_FILES:**
`agent1_trader.py`, `agent2_research.py`, `agent3_sentiment.py`, `database.py`, `boot_sequence.py`, `watchdog.py`, `health_check.py`, `shutdown.py`, `cleanup.py`, `synthos_heartbeat.py`, `portal.py`, `patch.py`, `sync.py`, `uninstall.py`

**What is NOT enforced by install_retail.py:**
- `license_validator.py` presence (DEFERRED)
- `LICENSE_KEY` validity (key is collected, not validated)
- DB integrity check (existence only)

**Idempotency rules:** `user/.env` is never overwritten without a timestamped backup. `data/signals.db` is never touched if it already exists. `.known_good/`, `user/agreements/`, `consent_log.jsonl` are never touched.

### 6.2 Company Node Installer (`install_company.py`)

**Enforces:**
- `COMPANY_MODE=true` in `company.env` (presence and value)
- Required secrets: `SENDGRID_API_KEY`, `SENDGRID_FROM`, `OPERATOR_EMAIL`, `KEY_SIGNING_SECRET`, `DATABASE_PATH`
- All agent files present (superset of canonical minimum): `blueprint.py`, `sentinel.py`, `scoop.py`, `patches.py`, `fidget.py`, `librarian.py`, `vault.py`, `timekeeper.py`, `strongbox.py` + `utils/db_helpers.py`
- `company.db` existence (PRAGMA integrity_check not currently run)
- Registers all company agent cron entries including Strongbox (`0 23 * * *`)

**What is NOT enforced by install_company.py:**
- `ANTHROPIC_API_KEY` and `MONITOR_TOKEN` (in canonical spec but currently missing from installer check — alignment gap)
- `MONITOR_URL` and `PI_ID` presence (deferred)
- DB PRAGMA integrity_check (deferred to boot-time gate)

### 6.3 Retail Boot Sequence (`boot_sequence.py`)

Runs once after every Pi reboot via `@reboot sleep 60 && python3 boot_sequence.py`.

**Boot order:**
1. Wait for network (timeout 90s; continues on timeout)
2. Verify `.env` exists and has `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `TRADING_MODE`
3. Verify all `REQUIRED_FILES` are present
4. Verify database (cold-start safe — absent DB passes, existing DB checked for basic accessibility)
5. Run `health_check.py`
6. Start watchdog in background
7. Write boot heartbeat
8. Log boot complete — cron takes over for agent scheduling

**What boot does NOT enforce:**
- License validation (no gate, no license_validator.py call — DEFERRED)
- Integrity gate evaluation (no equivalent of COMPANY_INTEGRITY_GATE_SPEC.md for retail)

**Known open issue:** `boot_sequence.py` uses `smtplib` directly for boot SMS alerts (SYS-B08). This violates the policy that all outbound communication routes through Scoop. This is an open HIGH item; it does not block current operation.

### 6.4 Company Startup

There is no `boot_company.py`. Company agents are started by individual cron entries registered at install time. The company node startup is:

```
On reboot:
  Cron entries start individual agents:
    - sentinel.py (port 5004, persistent)
    - scoop.py (persistent poller)
    - timekeeper.py (persistent scheduler)
    - patches.py (scheduled: continuous or daily deep)
    - blueprint.py (scheduled: Mon-Thu build window)
    - vault.py (scheduled)
    - fidget.py (scheduled)
    - librarian.py (scheduled)
    - strongbox.py (nightly 11pm ET)
```

No pre-start integrity gate evaluation occurs. The installer having succeeded at setup time is the implicit guarantee of environment correctness.

---

## 7. BACKUP SYSTEM

### 7.1 Current implementation state

Strongbox (`strongbox.py`) is:
- Correctly placed at `synthos-company/agents/strongbox.py`
- Listed in installer `REQUIRED_AGENT_FILES` (checked at install verify time)
- Registered in cron at `0 23 * * *` (nightly 11pm ET, written by `install_company.py`)
- 651 lines of Python implementing daily full backup to Cloudflare R2 with Fernet encryption

Strongbox does NOT currently:
- Import `db_helpers` or call `db.log_backup()` (backup_log table is not populated)
- Implement the tiered model (monthly baseline + nightly incremental)
- Enforce retention or cleanup

### 7.2 Canonical policy

The target backup policy is defined in `docs/specs/BACKUP_STRATEGY_INITIAL.md`. That document governs:

| Policy element | Target |
|----------------|--------|
| Baseline snapshots | Monthly full backup |
| Incremental chain | Nightly within 11pm–midnight window |
| Retention | Full baselines for 6 months; incrementals until superseded |
| Chain cleanup | Prior incremental chain deleted when new baseline created |
| Scope | company.db, .env, config/ contents |
| Redundancy | Local only (off-device, cloud, RAID deferred) |

### 7.3 Mismatch between current and target

| Dimension | Current (Strongbox actual) | Target (BACKUP_STRATEGY_INITIAL.md) |
|-----------|-----------------------------|--------------------------------------|
| Frequency | Daily full | Monthly full + nightly incremental |
| Scope | Full R2 upload with Fernet | company.db, .env, config/ (defined scope) |
| Retention | Not defined in current code | 6 months full; incremental until superseded |
| Cleanup | Not implemented | Automated on new baseline |
| backup_log | Not written | Should be written to company.db |

This mismatch is a known, accepted gap. Aligning Strongbox to the canonical policy is tracked in `docs/milestones.md` (Backup System Evolution). It does not block the current phase.

---

## 8. VALIDATION MODEL

### 8.1 How validation is performed

Synthos uses a layered validation approach:

**Static validation** (`docs/validation/STATIC_VALIDATION_REPORT.md`): File-by-file review of code and configuration against documented requirements. Identifies what is present, absent, or inconsistent without running the system.

**System validation** (`docs/validation/SYSTEM_VALIDATION_REPORT.md`): End-to-end review of the system as a whole — architecture coherence, agent interactions, control flow correctness. Identifies blockers that prevent the system from operating coherently. Originally identified 9 blockers (SYS-B01 through SYS-B09).

**Blocker tracking** (`docs/validation/BLOCKER_REFRESH_REPORT.md`): Maintained truth table of all identified blockers with current status: RESOLVED / RESOLVED_PENDING_DEPLOYMENT / DEFERRED_FROM_CURRENT_BASELINE / STILL_OPEN.

**Post-deferral validation** (`docs/validation/POST_DEFERRAL_VALIDATION_REPORT.md`): Confirms that the retail license deferral removed all remaining critical blockers and that the baseline is internally consistent.

**DB schema normalization** (`docs/validation/DB_SCHEMA_NORMALIZATION_NOTE.md`): Records what inconsistencies were found, what was normalized, and what was intentionally left unchanged in the schema normalization step.

**Automated tests:** `tests/validate_02.py` (22/22 passing — portal surface), `tests/validate_03b.py` (44/44 passing — approval queue).

### 8.2 Current blocker state

| ID | Status | Description |
|----|--------|-------------|
| SYS-B01 | DEFERRED | Retail license_validator.py — formally deferred from current baseline |
| SYS-B02 | DEFERRED | Retail boot license gate — absent state is correct current state |
| SYS-B03 | RESOLVED | Suggestions pipeline split — db_helpers.post_suggestion() migration complete |
| SYS-B04 | RESOLVED | Deploy watch read — db_helpers.get_active_deploy_watches() migration complete |
| SYS-B05 | RESOLVED | watchdog.py COMPANY_DATA_DIR hardcode — now reads from env var |
| SYS-B06 | STILL_OPEN | Installer core/ vs flat layout mismatch — HIGH, non-critical |
| SYS-B07 | STILL_OPEN | update-staging branch absent — HIGH, non-critical, blocks Phase 5 |
| SYS-B08 | STILL_OPEN | boot_sequence.py smtplib direct use — HIGH, non-critical, policy violation |
| SYS-B09 | RESOLVED_PENDING_DEPLOYMENT | Strongbox wiring — code correct; live crontab needs install --repair re-run |

**CRITICAL_BLOCKERS_REMAIN: NO** (as of 2026-03-29)

### 8.3 Relationship to Ground Truth

This Ground Truth document is the output of the Phase 4 validation gate. It is the synthesis and consolidation of all prior validation work. Future validation reports must:
- Reference this document as the baseline
- Flag deviations from this document as defects
- Update this document when system state changes (see §10)

---

## 9. DEFERRED WORK

### 9.1 Retail License Validation System

**Classification:** DEFERRED_FROM_CURRENT_BASELINE / FUTURE_RETAIL_ENTITLEMENT_WORK
**Decision date:** 2026-03-29
**Authoritative decision record:** `docs/validation/RETAIL_LICENSE_DEFERRAL_NOTE.md`

Items deferred:
- Build `license_validator.py` with HMAC validation, Pi ID binding, online registry check
- Wire boot-time entitlement gate into `boot_sequence.py`
- Define behavior for invalid, expired, revoked, and offline keys
- Cache model and grace period implementation
- Add `license_validator.py` back to `REQUIRED_CORE_FILES` when built
- Add `LICENSE_KEY` back to installer verification required_keys when validator is wired
- Vault: stricter post-trading license enforcement, key rotation, rate limiting
- End-to-end retail entitlement flow validation

**Full task list:** `docs/milestones.md` — Retail Entitlement / License System

### 9.2 Backup System Full Alignment

**Classification:** Future implementation
**Canonical policy:** `docs/specs/BACKUP_STRATEGY_INITIAL.md`

Items deferred:
- Monthly baseline snapshot generation
- Nightly incremental chain implementation
- Baseline-linked cleanup automation
- 6-month retention enforcement
- Backup health reporting (backup_log table wiring for Strongbox)
- Restore path verification from baseline + incremental chain

**Full task list:** `docs/milestones.md` — Backup System Evolution

### 9.3 Company Boot-Time Integrity Gate

**Classification:** Pre-release security hardening (Phase 6 gate condition)
**Spec:** `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md`

Items deferred:
- `boot_company.py` or equivalent — evaluates full integrity gate before starting any company agent
- Installer alignment with canonical secret set (add `ANTHROPIC_API_KEY`, `MONITOR_TOKEN`)
- PRAGMA integrity_check in installer DB verification
- Enforce `MONITOR_URL` and `PI_ID` at install time
- Verify company startup trust path under normal and break-glass modes

### 9.4 Network and Cloud Backup

**Classification:** Future evaluation only
- Off-device backup (cloud target, Cloudflare R2 as formal target)
- RAID / NAS storage
- Encrypted backup archive
- Backup integrity verification (spot-check after write)

### 9.5 Remaining Open Blockers (HIGH, non-critical)

| Blocker | Description | Blocks |
|---------|-------------|--------|
| SYS-B06 | Installer core/ vs flat layout mismatch | Deployment pipeline correctness |
| SYS-B07 | update-staging branch absent | Phase 5 deployment pipeline |
| SYS-B08 | boot_sequence.py uses smtplib directly | Policy compliance (all alerts through Scoop) |

### 9.6 Secondary Normalization Items

These items were identified as required before Phase 4 but are carried forward as open documentation tasks:
- Mark `SUGGESTIONS_JSON_SPEC.md` as SUPERSEDED (suggestions.json retired; DB is authoritative)
- Mark `POST_DEPLOY_WATCH_SPEC.md` as SUPERSEDED (post_deploy_watch.json retired; DB is authoritative)
- Update `SYSTEM_MANIFEST.md` v1.2 env var additions
- CL-009: Company agents not classified in `TOOL_DEPENDENCY_ARCHITECTURE.md`

---

## 10. SOURCE OF TRUTH DECLARATION

**This document is the authoritative definition of the Synthos system.**

It reflects the system as it exists as of 2026-03-29, post-normalization sprint, with all critical blockers resolved or formally deferred.

It supersedes:
- Prior fragmented schema descriptions in `SYNTHOS_TECHNICAL_ARCHITECTURE.md` (replaced by `DATABASE_SCHEMA_CANONICAL.md`)
- All pre-normalization architecture, manifest, and operations sections where they contradict this document
- Any implicit or emergent system definitions derived from reading individual agent files in isolation

**All future changes must follow this process:**

1. **Update this document first** — describe what is changing and why
2. **Update implementation** — code, config, or installer changes
3. **Update canonical references** — `DATABASE_SCHEMA_CANONICAL.md` if schema changes; `SYSTEM_MANIFEST.md` if file registry changes; relevant spec docs if architecture changes
4. **Revalidate** — confirm the change does not introduce new contradictions
5. **Commit** — commit all document updates and implementation changes together

**No change may bypass this process.** A change that updates code without updating Ground Truth creates drift and invalidates future validation.

**Ownership:** Project lead. No agent may unilaterally declare Ground Truth changes. Ground Truth updates require human review and explicit approval.

---

*Synthesized from: `docs/specs/DATABASE_SCHEMA_CANONICAL.md`, `docs/specs/SYSTEM_MANIFEST.md` (v4.0), `docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md` (v3.1), `docs/governance/SYNTHOS_OPERATIONS_SPEC.md` (v3.0), `docs/governance/SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md` (v1.0), `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` (v1.1), `docs/specs/BACKUP_STRATEGY_INITIAL.md` (v1.0), `STATUS.md`, `PROJECT_STATUS.md`, `docs/validation/BLOCKER_REFRESH_REPORT.md`, `docs/validation/POST_DEFERRAL_VALIDATION_REPORT.md`, `docs/validation/DB_SCHEMA_NORMALIZATION_NOTE.md`*
