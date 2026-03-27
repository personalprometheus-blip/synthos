# SYNTHOS TECHNICAL ARCHITECTURE
## Experimental Deployment Model

**Document Version:** 1.0  
**Date:** March 2026  
**Audience:** Engineers, AI agents building/maintaining the system  
**Scope:** Retail customer deployments + company infrastructure  

---

## EXECUTIVE SUMMARY

Synthos is a distributed system with three tiers:

| Tier | Hardware | Purpose | Autonomy |
|------|----------|---------|----------|
| **Retail** | Pi 2W | Customer trading agents | Completely standalone |
| **Company** | Pi 4B | Operations, monitoring, experiments | Internal only |
| **Integration** | Cloudflare tunnel | Installer delivery, optional heartbeat | Customer → Company bridge |

**Key principle:** Retail Pis are *self-contained forever*. Company Pi is *optional infrastructure*. Customer can disconnect and still trade indefinitely.

---

## PART 1: SYSTEM ARCHITECTURE OVERVIEW

### 1.1 High-Level System Diagram

```
                    CUSTOMER ENVIRONMENT
                    ====================

    ┌─────────────────────────────────┐
    │  Retail Pi 2W (retail-pi-01)   │
    │  ─────────────────────────────  │
    │  • Agent 1: Trader              │
    │  • Agent 2: Research            │
    │  • Agent 3: Sentiment           │
    │  • Portal (localhost:5001)      │
    │  • SQLite (signals.db)          │
    │  • License key validator        │
    │  ─────────────────────────────  │
    │  Runs: Always (offline-first)   │
    │  Updates: Git pull (optional)   │
    │  Dependency: None on Company Pi │
    └──────────┬──────────────────────┘
               │
               ├─→ [OPTIONAL] ─→ Heartbeat POST
               │                  to Company Pi
               │                  (customer can disable)
               │
               └─→ Alpaca API (paper trades)


                    COMPANY ENVIRONMENT
                    ===================

    ┌─────────────────────────────────────────┐
    │  Pi 4B (admin-pi-4b)                    │
    │  ─────────────────────────────────────  │
    │  TRADING AGENTS: (None — reserved)      │
    │                                         │
    │  COMPANY OPERATIONS:                    │
    │  • Mail Agent (SendGrid)                │
    │  • Interface Agent (heartbeats)         │
    │  • Control Agent (keys, flags)          │
    │  • Bug Finder (log analysis, crashes)   │
    │  • Engineer (code improvements)         │
    │  • Tool Agent (dependency manager)      │
    │  • Scheduler (resource coordinator)     │
    │                                         │
    │  DATA:                                  │
    │  • SQLite (company.db)                  │
    │  • Schema: customers, heartbeats,       │
    │    api_usage, bug_reports, keys, logs   │
    │                                         │
    │  SERVICES:                              │
    │  • Command Interface (5002)             │
    │  • Installer Delivery (5003)            │
    │  (heartbeat receiver: monitor_node:5000) │
    │  ─────────────────────────────────────  │
    │  Runs: 24/7                             │
    │  Scheduler priority:                    │
    │    Market hours: Interface + Mail       │
    │    Off-hours: Bug Finder + Engineer     │
    └─────────────────────────────────────────┘
               │
               └─→ Cloudflare Tunnel
                   (exposes 5003 for installer)


                    EXTERNAL SERVICES
                    =================

    • Alpaca (paper trading API)
    • Congress.gov (disclosure data)
    • Anthropic (Claude API)
    • SendGrid (email alerts)
    • GitHub (code hosting + customer forks)
    • Cloudflare (tunnel for installer delivery)
```

### 1.2 Data Flow

**Scenario A: Customer trading (offline-capable)**
```
1. Retail Pi agents run on schedule
2. Research agent fetches Congress.gov disclosures
3. Sentiment agent scores market context
4. Trader agent calls Claude, decides action
5. Trade executes on Alpaca paper account
6. Result written to local signals.db
7. Portal displays to customer
[Company Pi never involved]
```

**Scenario B: Customer wants alerts (optional)**
```
1. Heartbeat.py on Retail Pi runs hourly
2. POSTs: {pi_id, portfolio_value, agent_status, timestamp}
3. Company Pi Interface agent receives, logs
4. Mail agent composes alert email
5. SendGrid delivers to customer
[Customer can disable by removing MAIL_SERVER env var]
```

**Scenario C: New customer installation**
```
1. Customer powers on Pi 2W, connects to network
2. Navigates to https://your-tunnel.com/install
3. Enters license key (from your command page)
4. Clicks "Download & Install"
5. Installer script pulls current synthos from GitHub
6. Validates key, extracts to /home/pi/synthos/
7. Runs setup wizard (portal credentials, API keys)
8. Pi reboots, agents start
```

**Scenario D: Bug fix / code update (experimental phase)**
```
1. Engineer agent identifies issue in bug logs
2. Creates fix in company GitHub repo (company branch)
3. Bug Finder agent tests fix against customer log
4. Control agent generates deployment approval
5. Engineer pushes to customer fork branch
6. Customer (or cron job) does `git pull origin update-staging`
7. New code in /home/pi/synthos/core/ takes effect
8. Agents restart automatically
[User settings in /home/pi/synthos/user/ untouched]
```

---

## PART 2: RETAIL PI ARCHITECTURE (Customer-Facing)

### 2.1 Hardware & OS

**Target Device:** Raspberry Pi 2W (low cost, low power, sufficient for 3 agents)

**OS:** Raspberry Pi OS Lite (minimal surface area, fast boot)

**Assumptions:**
- 512MB RAM (3 agents + portal = ~250MB typical)
- 32GB microSD (logs + DB grow slowly with ~5 trades/day)
- WiFi or Ethernet (always-on expected)
- Power: continuous supply (can tolerate brief outages)

### 2.2 Directory Structure

```
/home/pi/synthos/
│
├── core/                          # Company-managed (updatable)
│   ├── agent1_trader.py           # Trade execution, supervised/autonomous
│   ├── agent2_research.py         # Disclosure fetching, signal scoring
│   ├── agent3_sentiment.py        # Market sentiment, cascade detection
│   ├── scheduler.py               # (Retail: minimal, just startup orchestration)
│   ├── database.py                # SQLite helpers, schema
│   ├── portal.py                  # Web UI (5001)
│   ├── heartbeat.py               # Optional: POST to Company Pi
│   ├── license_validator.py       # Check key on startup, periodic
│   ├── boot_sequence.py           # Start agents in order
│   ├── watchdog.py                # Restart crashed agents
│   ├── health_check.py            # Verify system health
│   └── utils/                     # Shared libraries
│       ├── api_client.py          # Alpaca, Congress.gov, Anthropic
│       ├── config.py              # Load from .env
│       └── logging.py             # Structured logs
│
├── user/                          # Customer-owned (immutable, chmod 444)
│   ├── .env                       # API keys, trading settings, mode
│   ├── settings.json              # Portal preferences, thresholds
│   └── agreements/                # Legal documents (read-only)
│       ├── framing.txt            # Legal framing
│       ├── operating_agreement.txt
│       └── beta_agreement.txt
│
├── data/
│   ├── signals.db                 # SQLite: signals, positions, trades
│   └── backup/                    # Daily backup of signals.db
│
├── logs/
│   ├── trader.log
│   ├── research.log
│   ├── sentiment.log
│   ├── heartbeat.log
│   ├── system.log
│   └── health.log
│
└── .git/                          # Git repo (for updates)
    └── (branches: main, update-staging, backup)
```

**Permission model:**
```bash
# On first setup:
chmod -R 755 /home/pi/synthos/core/
chmod -R 444 /home/pi/synthos/user/        # Read-only
chmod 600 /home/pi/synthos/user/.env       # Sensitive
chmod -R 755 /home/pi/synthos/data/
chmod -R 755 /home/pi/synthos/logs/
```

Even if an agent crashes and becomes corrupted, it cannot modify `/user/`.

### 2.3 Database Schema (Retail Pi)

**SQLite: signals.db**

```sql
-- Core trading data
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    congress_member TEXT,
    ticker TEXT,
    transaction_type TEXT,  -- BUY/SELL
    signal_score TEXT,      -- HIGH/MEDIUM/LOW
    agent_decision TEXT,    -- MIRROR/WATCH/SKIP
    status TEXT,            -- PENDING/APPROVED/EXECUTED/SKIPPED
    created_at DATETIME
);

CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    ticker TEXT UNIQUE,
    shares REAL,
    entry_price REAL,
    entry_date DATETIME,
    portfolio_value REAL,
    status TEXT             -- OPEN/CLOSED
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER,
    ticker TEXT,
    action TEXT,           -- BUY/SELL
    shares REAL,
    price REAL,
    executed_at DATETIME,
    profit_loss REAL,
    status TEXT,           -- EXECUTED/PENDING/FAILED
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE agent_status (
    id INTEGER PRIMARY KEY,
    agent_name TEXT,
    last_run DATETIME,
    status TEXT,           -- RUNNING/SUCCESS/ERROR
    error_message TEXT,
    uptime_seconds INTEGER
);

-- License tracking (local)
CREATE TABLE license (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,
    issued_date DATETIME,
    status TEXT,           -- VALID/REVOKED
    expires_at DATETIME    -- NULL = forever
);

-- Customer configuration
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT,
    set_by TEXT,           -- USER/SYSTEM
    updated_at DATETIME
);
```

### 2.4 Agents (Retail Pi)

**Agent 1: Trader**
- **Schedule:** 9:30am, 12:30pm, 3:30pm ET (market hours)
- **Input:** Approved signals from queue (supervised mode) or highest-confidence signals (autonomous)
- **Process:**
  1. Read pending signals from database
  2. Call Claude API for trade decision (or use pre-authorized rules if autonomous)
  3. Execute on Alpaca if approved
  4. Log outcome to signals.db
- **Output:** Trade confirmation + log entry
- **Controls:**
  - Supervised mode: requires user portal approval before execution
  - Autonomous mode: executes per pre-defined rules (requires unlock key)
  - Kill switch: single portal button halts execution immediately
- **Failure modes:**
  - Alpaca API down → queue signals, retry next window
  - Network failure → retry with exponential backoff
  - Invalid key/license → skip execution, log warning

**Agent 2: Research (The Daily)**
- **Schedule:** Hourly during market hours (8am–8pm ET)
- **Input:** Congress.gov API
- **Process:**
  1. Fetch new Congressional disclosures
  2. Score each for signal quality (HIGH/MEDIUM/LOW)
  3. Write to signals.db with timestamp
  4. Flag cascade signals (same person, different ticker)
- **Output:** New signals in database
- **Controls:**
  - Confidence threshold (adjustable in portal: 60–95%)
  - Max signals per run (prevent database bloat)
- **Failure modes:**
  - Congress.gov down → skip run, retry next hour
  - API key invalid → portal alert "Congress API misconfigured"

**Agent 3: Sentiment (The Pulse)**
- **Schedule:** Every 30 min during market hours
- **Input:** Market data (via news API), open positions
- **Process:**
  1. Fetch market sentiment (news, social, volatility)
  2. Check open positions for adverse cascade signals
  3. Flag protective exits (if market turns against position)
  4. Update signal status in database
- **Output:** Exit recommendations, updated sentiment scores
- **Controls:**
  - Sentiment threshold (when to warn)
  - Cascade detection sensitivity
- **Failure modes:**
  - News API down → use cached sentiment from last 4 hours
  - No open positions → skip, log as success

**Scheduler (Retail Pi)**
- Minimal role: just orchestrates startup
- Each agent is self-scheduled (built-in cron via crontab or APScheduler)
- Watchdog monitors agent health, restarts if crashed
- **No resource requests/grants** — agents run independently, SQLite handles locking

### 2.5 Portal (Retail Pi)

**URL:** `http://retail-pi-01.local:5001`

**Pages:**
1. **Dashboard:** Portfolio value, open positions, P&L
2. **Signals Queue:** Pending signals (supervised mode only)
   - Approve/Reject buttons
   - View trade rationale from Claude
3. **Trade History:** Past trades, results, agent logs
4. **Settings:**
   - Trading mode (supervised/autonomous)
   - Confidence threshold, position size
   - API key updates (encrypted storage)
   - Email alerts (on/off for heartbeat)
5. **Kill Switch:** Red button, halts all agents immediately
6. **System Status:** Agent uptime, last run, error logs
7. **License:** Display key, expiration (if applicable)

**Technology:**
- Python Flask (lightweight)
- JavaScript frontend (vanilla, no heavy frameworks)
- WebSocket for live updates (optional, fallback to polling)

### 2.6 License Validation (Retail Pi)

**License key format:**
```
synthos-[pi_id]-[timestamp]-[signature]
Example: synthos-retail-pi-01-1704067200-abc123def456
```

**Validation flow:**
1. **On first boot:** license_validator.py reads license from database or .env
2. **Every 24 hours:** Verify key is still in company registry (if online)
   - If offline: use cached status from last 30 days
   - If online but key revoked: agents continue but log warning
3. **Before major operation:** Trader confirms license before executing trades
4. **On payment expiration:**
   - Key remains valid forever (one-time purchase model)
   - Portal continues working
   - Heartbeat/mail alerts stop if customer disables
   - GitHub fork access revoked (on company side, not Pi)

**Failure mode:** Key lookup fails → agents run with "unlicensed warning" but don't stop

### 2.7 Optional Heartbeat (Retail Pi → Company Pi)

**heartbeat.py** (runs hourly during market hours)

```python
POST /heartbeat

{
    "pi_id": "retail-pi-01",
    "timestamp": "2026-03-23T14:30:00Z",
    "portfolio_value": 50000.00,
    "agents": {
        "trader": {"status": "SUCCESS", "last_run": "2026-03-23T12:30:00Z"},
        "research": {"status": "SUCCESS", "last_run": "2026-03-23T14:00:00Z"},
        "sentiment": {"status": "SUCCESS", "last_run": "2026-03-23T14:15:00Z"}
    },
    "license_key": "synthos-retail-pi-01-...",
    "uptime_seconds": 864000
}
```

**Sends to:** monitor_node at `MONITOR_URL` (env var, defaults to None for offline mode)

**Customer can disable:** Remove `MONITOR_URL` from .env → heartbeat still runs (no-op)

---

## PART 3: COMPANY PI ARCHITECTURE (Internal Operations)

### 3.1 Hardware & OS

**Target Device:** Raspberry Pi 4B (8GB RAM recommended)

**OS:** Raspberry Pi OS Lite

**Always-on:** 24/7, power backup recommended

### 3.2 Directory Structure

```
/home/pi/synthos-company/
│
├── agents/                        # Company operations
│   ├── mail_agent.py              # Email via SendGrid
│   ├── interface_agent.py         # Heartbeat receiver, customer data
│   ├── control_agent.py           # Key generation, issue flagging
│   ├── bug_finder.py              # Log analysis, crash detection
│   ├── engineer.py                # Code improvements, optimization
│   ├── tool_agent.py              # Dependency manager
│   ├── scheduler.py               # Resource coordinator (critical)
│   └── token_monitor.py           # API usage tracking
│
├── services/
│   ├── command_interface.py       # Flask app (port 5002)
│   ├── installer_service.py       # Flask app (port 5003)
│   # heartbeat_receiver.py — DEPRECATED (never built); heartbeat received by synthos_monitor.py on monitor_node:5000
│   └── config_manager.py
│
├── data/
│   ├── company.db                 # All company data
│   └── backup/
│
├── logs/
│   ├── mail_agent.log
│   ├── interface_agent.log
│   ├── control_agent.log
│   ├── bug_finder.log
│   ├── engineer.log
│   ├── tool_agent.log
│   ├── scheduler.log
│   └── token_monitor.log
│
├── utils/
│   ├── scheduler_core.py          # Request/grant logic
│   ├── db_guardian.py             # Lock management, conflict detection
│   ├── api_client.py              # Anthropic, GitHub, SendGrid
│   └── logging.py
│
├── config/
│   ├── agent_policies.json        # Who runs when
│   ├── market_calendar.json       # Trading hours
│   └── priorities.json            # Task urgency ranking
│
└── .git/
    └── (repos: company-core, customer-forks...)
```

### 3.3 Database Schema (Company Pi)

**SQLite: company.db**

```sql
-- Customer registry
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    pi_id TEXT UNIQUE,              -- retail-pi-01
    license_key TEXT UNIQUE,
    customer_name TEXT,
    email TEXT,
    status TEXT,                   -- ACTIVE/INACTIVE/ARCHIVED
    created_at DATETIME,
    last_heartbeat DATETIME,
    payment_status TEXT,           -- PAID/EXPIRED/CANCELLED
    github_fork_access BOOLEAN,    -- Can pull updates?
    mail_alerts_enabled BOOLEAN,
    archived_at DATETIME           -- When 45 days expired
);

-- Heartbeat log (for monitoring, analytics)
CREATE TABLE heartbeats (
    id INTEGER PRIMARY KEY,
    pi_id TEXT,
    timestamp DATETIME,
    portfolio_value REAL,
    agent_statuses JSON,           -- {"trader": "SUCCESS", ...}
    uptime_seconds INTEGER,
    FOREIGN KEY(pi_id) REFERENCES customers(pi_id)
);

-- API usage tracking
CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY,
    pi_id TEXT,
    api_provider TEXT,             -- anthropic/alpaca/congress/sendgrid
    token_count INTEGER,           -- For Anthropic
    call_count INTEGER,
    timestamp DATETIME,
    FOREIGN KEY(pi_id) REFERENCES customers(pi_id)
);

-- Bug reports (from bug_finder agent)
CREATE TABLE bug_reports (
    id INTEGER PRIMARY KEY,
    pi_id TEXT,
    severity TEXT,                 -- CRITICAL/HIGH/MEDIUM/LOW
    agent_name TEXT,
    error_message TEXT,
    stack_trace TEXT,
    log_snippet TEXT,
    first_seen DATETIME,
    last_seen DATETIME,
    status TEXT,                   -- NEW/INVESTIGATING/FIXED/DISMISSED
    FOREIGN KEY(pi_id) REFERENCES customers(pi_id)
);

-- License key inventory (company-side)
CREATE TABLE keys (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,
    pi_id TEXT,
    issued_at DATETIME,
    expires_at DATETIME,           -- NULL = forever
    status TEXT,                   -- ACTIVE/REVOKED/EXPIRED
    FOREIGN KEY(pi_id) REFERENCES customers(pi_id)
);

-- Agent work requests (for scheduler)
CREATE TABLE work_requests (
    id INTEGER PRIMARY KEY,
    agent_name TEXT,
    request_time DATETIME,
    duration_requested INTEGER,    -- seconds
    priority INTEGER,              -- 1=critical, 10=low
    task_type TEXT,               -- scan_logs, optimize_code, etc
    status TEXT,                   -- PENDING/GRANTED/EXECUTING/COMPLETE
    scheduled_start DATETIME,
    actual_start DATETIME,
    actual_end DATETIME
);

-- Token usage per agent (for optimization)
CREATE TABLE token_ledger (
    id INTEGER PRIMARY KEY,
    agent_name TEXT,
    timestamp DATETIME,
    tokens_used INTEGER,
    operation TEXT,                -- which API call
    cost_estimate REAL,
    month TEXT                     -- YYYY-MM for billing
);

-- Control flags (for engineer/bug_finder to action)
CREATE TABLE control_flags (
    id INTEGER PRIMARY KEY,
    flag_type TEXT,               -- CODE_REVIEW_NEEDED, PERFORMANCE_ISSUE, etc
    pi_id TEXT,
    description TEXT,
    severity TEXT,
    flagged_at DATETIME,
    resolved_at DATETIME,
    FOREIGN KEY(pi_id) REFERENCES customers(pi_id)
);
```

### 3.4 Agents (Company Pi)

**Agent: Mail Agent**
- **Role:** Compose and send emails via SendGrid
- **Triggers:**
  - Heartbeat received from retail Pi
  - Bug report raised by Bug Finder
  - Key generated by Control Agent
  - Trade executed (forwarded from customer Pi if alerts enabled)
- **Output:** Email sent, logged to database
- **Failure mode:** SendGrid down → queue emails, retry every 5 min
- **Concurrency:** No database conflicts (read-only, write log entry)

**Agent: Interface Agent**
- **Role:** Receive and process heartbeats from retail Pis
- **Triggers:** HTTP POST from heartbeat.py
- **Process:**
  1. Validate pi_id + token
  2. Parse heartbeat JSON
  3. Write to `heartbeats` table
  4. Check if silent >4 hours during market hours → trigger alert
  5. Update `customers.last_heartbeat`
- **Output:** Heartbeat logged, alerts triggered if needed
- **Failure mode:** Database locked → return 503, client retries
- **Concurrency:** Protected by database transaction (Scheduler grants time)

**Agent: Control Agent**
- **Role:** Key management, issue flagging, compliance tracking
- **Capabilities:**
  - Generate new license keys (via command interface)
  - Revoke keys (on payment expiration)
  - Flag issues for engineer review
  - Generate reports for manual audit
- **Triggers:** Manual command from command interface, or automatic (payment expiration)
- **Output:** Keys stored in database, flags set, reports generated
- **Concurrency:** Write operations serialized by Scheduler

**Agent: Bug Finder**
- **Role:** Analyze logs from all retail Pis, detect patterns
- **Process:**
  1. Collect logs from each customer Pi (via heartbeat metadata or SSH)
  2. Parse for ERROR/EXCEPTION patterns
  3. Correlate similar errors across multiple Pis
  4. Raise bug report with severity
  5. Suggest fix approach to Engineer
- **Schedule:** Daily full scan (off-hours), hourly CRITICAL check (anytime)
- **Output:** Bug reports in database, notifications to Engineer
- **Concurrency:** Read-heavy, Scheduler grants long windows off-hours

**Agent: Engineer**
- **Role:** Code improvements, performance optimization, technical debt
- **Capabilities:**
  - Review code suggestions from Tool Agent
  - Implement fixes from Bug Finder recommendations
  - Profile agent performance, suggest optimizations
  - Refactor complex functions
  - Update dependencies (via Tool Agent)
- **Triggers:** Bug reports, performance metrics, manual review queue
- **Output:** Code commits, pull requests, performance reports
- **Concurrency:** Works on company-core repo, doesn't touch customer data

**Agent: Tool Agent**
- **Role:** Dependency manager, utility library maintenance
- **Capabilities:**
  - Track installed packages on retail Pis (from heartbeat metadata)
  - Check for security updates
  - Suggest missing utilities (e.g., "all Pis have git, but missing pip caching")
  - Recommend third-party libraries to Engineer
- **Process:**
  1. Audit /home/pi/synthos/utils/ for outdated code
  2. Check pip packages for CVEs
  3. Suggest consolidation (remove duplicated code)
  4. Flag unused imports/functions
- **Output:** Recommendations to Engineer, update manifest
- **Concurrency:** Read-only (except manifest write)

**Agent: Token Monitor**
- **Role:** Track API spend across all agents
- **Process:**
  1. Log every API call (Anthropic, Congress.gov, Alpaca, SendGrid)
  2. Calculate cost (tokens × rate)
  3. Aggregate by agent per day/month
  4. Flag anomalies (sudden spike, unexpected usage)
  5. Suggest optimizations to Engineer
- **Schedule:** Hourly summary, daily report
- **Output:** Token ledger, alerts if unusual spend
- **Example alert:** "Trader agent used 50k tokens in 1 hour (10x normal). Possible infinite loop?"
- **Concurrency:** Append-only log, no conflicts

**Agent: Scheduler (Critical)**
- **Role:** Resource coordinator, prevents database bottlenecks
- **Architecture:**
  ```
  Agent submits: WorkRequest {
      agent_name: "bug_finder",
      duration_requested: 300,  // seconds
      priority: 2,              // 1=critical, 10=low
      task_type: "scan_logs"
  }
  
  Scheduler decides:
    Current time: 14:30 (market hours)
    Current load: Trader running, Interface waiting
    Decision: Queue bug_finder, grant 10 min slot at 15:00
  
  Result: WorkRequest { status: "GRANTED", scheduled_start: 15:00 }
  ```
- **Decision logic:**
  - **Market hours (9:30am–4pm ET):**
    - Critical: Interface Agent, Mail Agent (customer-facing)
    - High: Trader, Research, Sentiment (if running customer agent code here)
    - Medium: Token Monitor (logging only, minimal DB)
    - Low: Bug Finder, Engineer, Tool Agent (defer/queue)
  - **Off-hours (4pm–9:30am ET):**
    - Priority rebalances: Bug Finder, Engineer get long time slots
    - Still protect: Interface Agent (catches late heartbeats)
  - **Conflict avoidance:**
    - Max 1 writer to company.db at a time
    - Readers can run parallel if no writer active
    - Timeout after 2 min of lock wait → fail gracefully
- **Concurrency:** Single source of truth, no conflicts
- **Failure mode:** Scheduler crashes → agents continue (but may conflict, logged as error)

### 3.5 Services (Company Pi)

**Service: Command Interface (port 5002)**

User-facing operational dashboard:

```
http://admin-pi-4b.local:5002

Pages:
1. Dashboard
   - Active customers: list with last heartbeat
   - System health: Scheduler status, DB size
   - Alert summary: Critical issues in past 24h

2. Generate License Key
   - Input: pi_id (retail-pi-01), expiration (default: never)
   - Output: Key string, QR code
   - Store in database, ready to email/display

3. View Bug Reports
   - Filter by: severity, agent, customer
   - Status: NEW, INVESTIGATING, FIXED
   - Actions: Mark as fixed, dismiss, reassign to Engineer

4. Monitor Token Usage
   - Chart: tokens/day over past 30 days
   - Breakdown: by agent, by API provider
   - Alerts: usage anomalies

5. Customer Ledger
   - List all retail Pis (active + archived)
   - Last heartbeat, portfolio value
   - Payment status, GitHub fork access
   - Actions: Disable fork, send alert, archive

6. Scheduler Status
   - Current queue of work requests
   - Running agents, estimated completion time
   - Lock contention warnings

7. Manual Actions
   - Revoke license key
   - Force bug scan on specific Pi
   - Send test email
```

**Service: Installer Service (port 5003, exposed via Cloudflare tunnel)**

```
GET /install

Response: HTML page with:
  - Input field: "Enter your license key"
  - Button: "Download & Install"
  - Link: "Or run in terminal"

POST /install
Params: key=<license_key>
Response: Bash script that:
  1. Validates key via Control Agent
  2. git clone from company repo
  3. Extracts to /home/pi/synthos/
  4. Runs setup wizard
  5. Returns: Success or error message

Bash option:
  curl -X POST https://your-tunnel.com/install \
    -d "key=synthos-retail-pi-01-..." \
    -H "Content-Type: application/json" | bash
```

**Heartbeat Receiver — DEPRECATED on company_node**

> `heartbeat_receiver.py` (port 5004) was never implemented. The authoritative heartbeat receiver
> is `synthos_monitor.py` on the **monitor_node** at port 5000. Retail Pis POST to `MONITOR_URL`.
> See `HEARTBEAT_RESOLUTION.md` for the full decision record.

### 3.6 Database Locking & Guardians

**Problem:** Keygen holds exclusive lock while other agents wait.

**Solution: db_guardian.py**

```python
class DBGuardian:
    def request_access(agent_name, access_type, duration_sec):
        """
        access_type: READ | WRITE
        duration_sec: max time needed
        
        Returns: Grant or Queue
        """
        if access_type == WRITE:
            # Max 1 writer; check if any active
            if writer_active:
                return QUEUE(agent_name, priority)
            else:
                return GRANT(agent_name, duration_sec)
        else:
            # Many readers OK if no writer pending
            if high_priority_writer_waiting:
                return QUEUE(agent_name)
            else:
                return GRANT(agent_name)
    
    def release_access(agent_name):
        """Mark agent done; grant next in queue"""
        remove_grant(agent_name)
        next_agent = queue.pop(priority_order)
        GRANT(next_agent)
```

**In practice:**

```python
# In bug_finder.py (read-only)
guardian.request_access("bug_finder", READ, 300)
# → Immediate grant if no writer active

# In control_agent.py (write operation)
guardian.request_access("control_agent", WRITE, 30)
# → Waits if bug_finder is reading, or queues if interface agent has grant
```

**Timeout protection:**

```python
# If guardian can't grant within 120 seconds:
# Agent logs: "DB access timeout, skipping operation"
# Scheduler: marks work request as FAILED
# No cascading locks
```

---

## PART 4: UPDATE & DEPLOYMENT FLOW

### 4.1 Code Organization (Git)

```
GitHub Repo: personalprometheus-blip/synthos

Branches:
  main
    ├── Latest stable retail release
    ├── Includes all files in /core/ and /user/
    └── Customers fork this branch

  company-core
    ├── Company operations agents (mail, scheduler, etc)
    ├── Installer service
    └── Not accessible to customers

  update-staging
    ├── Testing branch for bug fixes
    ├── Engineer pushes fixes here
    └── Bug Finder validates before merge to main
```

### 4.2 Update Flow (Experimental Phase)

**Scenario: Bug found in Trader agent**

```
1. Bug Finder detects crash pattern:
   "Trader exits when Congress.gov returns 429"
   
2. Raises bug report:
   - Severity: HIGH
   - Agent: trader
   - Frequency: 3 Pis affected
   
3. Engineer reviews:
   - Adds exponential backoff retry
   - Tests locally
   - Commits to update-staging
   
4. Control Agent generates patch:
   - Creates summary: "Fix rate-limit handling"
   - Signs commit
   
5. Customer option A (automatic):
   - Pi runs cron: git fetch && git merge origin/update-staging
   - New code in /core/ loads
   - Agents restart
   - Old code backed up
   
6. Customer option B (manual):
   - Portal shows: "Update available: Fix rate-limit handling"
   - Customer clicks "Install"
   - Same flow as A
   
7. Validation:
   - Bug Finder monitors Pis post-update
   - No new crash reports → mark as FIXED
   - New crash → revert automatically
```

### 4.3 What Cannot Be Updated

**Protected (immutable):**
- `/user/.env` (customer API keys)
- `/user/settings.json` (customer preferences)
- `/user/agreements/` (legal documents)
- `/data/signals.db` (customer trades)
- `database.py` schema (once deployed)

**Why:** Ensures customer can always access their data, settings survive updates, legal agreements never change mid-deployment.

### 4.4 Rollback Strategy

**If update breaks system:**

1. Watchdog detects crash (agent fails 3x in 5 min)
2. Triggers automatic rollback:
   ```bash
   git reset --hard HEAD~1
   systemctl restart synthos
   ```
3. Logs: "Rollback triggered after update failure"
4. Notifies Company Pi via heartbeat: "ROLLBACK_EXECUTED"
5. Engineer alerted to investigate update

---

## PART 5: PAYMENT & LICENSING MODEL

### 5.1 License Key Lifecycle

**Issue:**
- Control Agent generates key
- Contains: pi_id + timestamp + signature
- Expires: never (one-time purchase)
- Stored: Company Pi registry + Retail Pi database

**Active:**
- Retail Pi validates on startup
- Validates every 24 hours (if online)
- Portal displays: "Licensed to [customer]"
- Agents run normally

**Non-payment (after 45 days):**
- Control Agent marks status as EXPIRED in company registry
- Retail Pi behavior: **unchanged**
  - Agents keep running
  - Portal accessible
  - Database grows normally
  - Trades execute as usual
- Company side:
  - GitHub fork access revoked (automatic)
  - Heartbeat alerts disabled (email stops)
  - Customer flagged as "inactive"
  - Pi archived after retention period (default: 1 year)

### 5.2 Customer Disconnection Path

**Customer wants to leave:**
1. Stops paying or opts out
2. Company Pi removes from active customer list
3. Retail Pi continues indefinitely (no changes)
4. Customer can manually delete heartbeat URL from .env
5. Portal still works, trades still execute
6. Over time: software becomes outdated (no updates), but functional

### 5.3 Archival Process

**After 45 days of inactivity:**

```sql
UPDATE customers
SET status = 'INACTIVE'
WHERE last_heartbeat < NOW() - INTERVAL '45 days'

# Later, after 1-year retention:
UPDATE customers
SET status = 'ARCHIVED', archived_at = NOW()
WHERE status = 'INACTIVE' AND status_change_date < NOW() - INTERVAL '365 days'

# Archive to separate table for auditing:
INSERT INTO archived_customers SELECT * FROM customers WHERE status = 'ARCHIVED'
DELETE FROM customers WHERE status = 'ARCHIVED'
```

**Data retention:**
- Archived customer heartbeats kept in ledger for 2 years (regulatory)
- Bug reports tied to archived customer kept for 1 year
- API usage logs kept for 1 year (billing audit)

---

## PART 6: FAILURE MODES & RESILIENCE

### 6.1 Retail Pi Failures

| Failure | Impact | Detection | Recovery |
|---------|--------|-----------|----------|
| **Trader agent crashes** | Pending trades not executed | Watchdog detects no heartbeat | Auto-restart within 2 min |
| **Congress.gov API down** | No new signals fetched | Research agent logs error | Retry next hour, use cached data |
| **Database corruption** | Can't read/write trades | Boot sequence fails | Restore from daily backup |
| **Network outage** | Heartbeat can't post | Heartbeat times out | Queues locally, sends when online |
| **SQLite locked** | All agents blocked | Timeout after 30 sec | Agents skip cycle, retry next window |
| **Insufficient disk space** | Database can't grow | Check at boot | Delete old logs (keep 90 days) |
| **Anthropic API key invalid** | Trader can't call Claude | API returns 401 | Log error, portal shows alert |
| **License key expired** | Trader refuses to run | License check fails | Portal shows "unlicensed", agent skips |
| **Portal offline** | Customer can't monitor | No HTTP response | Watchdog logs; agents still run |

**Recovery without Company Pi:** All local. Retail Pi is self-healing.

### 6.2 Company Pi Failures

| Failure | Impact | Detection | Recovery |
|---------|--------|-----------|----------|
| **Database locked** | All agents blocked | Multiple timeout errors | Scheduler timeout force-unlocks after 2 min |
| **Mail service down** | Alerts not sent | SendGrid returns error | Queue, retry exponentially |
| **Scheduler crashes** | No resource coordination | Process not running | Crontab restarts it, agents continue (risky) |
| **Heartbeat receiver down** | Can't receive from retail Pis | Retail Pi connection refused | Retry, eventually times out; retail Pi unaffected |
| **Disk full** | Can't write logs | Write fails | Oldest log files auto-deleted |
| **Network partition** | Can't reach Alpaca/Anthropic | API timeouts | Retry with backoff, don't affect retail Pis |

**Operator response:**
- Monitor command interface dashboard
- Watch Scheduler queue for stuck requests
- Restart services if needed: `systemctl restart synthos-company`

### 6.3 Distributed Failures (Retail ↔ Company)

| Failure | Retail Behavior | Company Behavior |
|---------|-----------------|------------------|
| **Tunnel down (Cloudflare)** | New installs fail | Existing Pis unaffected |
| **Customer network down** | All agents local, work offline | Last heartbeat recorded, no new data |
| **Auth token invalid** | Heartbeat rejected 401 | Logged, customer flagged |
| **Epidemic bug** (same bug affects 10+ Pis) | Each Pi fails independently | Bug Finder alerts, Engineer creates patch |

---

## PART 7: SCHEDULER (DETAILED)

### 7.1 Request/Grant Model

Each agent must request time from Scheduler before accessing company.db.

```python
# Mail Agent (every heartbeat)
scheduler.request_access(
    agent="mail_agent",
    task="send_alert_email",
    duration_sec=5,
    priority=2  # HIGH
)
# → Immediate grant (write is fast, low contention)

# Bug Finder (daily scan)
scheduler.request_access(
    agent="bug_finder",
    task="scan_all_logs",
    duration_sec=600,  # 10 minutes
    priority=7  # LOW (can wait)
)
# → Queued if Interface Agent is reading
# → Granted at night (18:00)
```

### 7.2 Priority Matrix

```
MARKET HOURS (9:30am–4pm ET):
  Priority 1: Interface Agent (receive heartbeats)
  Priority 2: Mail Agent (send alerts)
  Priority 3: Token Monitor (logging only, append)
  Priority 4: Control Agent (key generation on-demand)
  Priority 5: Bug Finder (queued, no interrupts)
  Priority 6: Engineer (queued)
  Priority 7: Tool Agent (queued)

OFF-HOURS (4pm–9:30am ET):
  Priority 1: Interface Agent (still receiving, catch-up)
  Priority 2: Bug Finder (long scans, 30 min windows)
  Priority 3: Engineer (optimize code, 30 min windows)
  Priority 4: Tool Agent (update dependencies)
  Priority 5: Mail Agent (batch alerts)
```

### 7.3 Lock Acquisition

```
Scheduler state machine:

  WorkRequest arrives
    ↓
  Is it a READ or WRITE?
    ├─ READ: Check if writer active
    │   ├─ No → GRANT immediately
    │   └─ Yes → QUEUE
    ├─ WRITE: Check if any active grants
    │   ├─ No → GRANT immediately
    │   └─ Yes → QUEUE (wait for releases)
    ↓
  EXECUTING: Agent runs, Scheduler monitors
    ├─ Agent completes → COMPLETE, release lock
    ├─ Timeout (2 min) → TIMEOUT, force release, log error
    └─ Error during → ERROR, release lock
    ↓
  Grant next QUEUED request by priority
```

### 7.4 Deadlock Prevention

**Rule:** No nested requests. If Agent A holds a lock and requests another, it deadlocks.

**Solution:** Agents must pre-request all resources.

```python
# BAD: Nested requests
scheduler.request_access("agent1", WRITE, 30)
# ... inside transaction ...
scheduler.request_access("agent1", WRITE, 10)  # DEADLOCK

# GOOD: Pre-request
scheduler.request_access("agent1", WRITE, 40)  # 30 + 10
# ... do both operations ...
scheduler.release_access("agent1")
```

---

## PART 8: OPERATIONAL PROCEDURES

### 8.1 Adding a New Customer

**Step 1: Generate License Key**
- Use command interface page 2
- Input: pi_id (retail-pi-01), email
- Output: Key string
- Store: Automatically in company.db

**Step 2: Customer Receives Key**
- Email or manual transfer
- Customer enters at installer

**Step 3: Installer Runs**
- Validates key via Control Agent
- Clones main branch from GitHub
- Sets up /home/pi/synthos/ with company-managed code
- Runs setup wizard (portal, API keys)
- Creates customer database, agents start

**Step 4: Heartbeat Starts**
- After boot, heartbeat.py runs hourly
- Interface Agent receives, logs
- Mail Agent sends welcome email (optional)
- Dashboard shows new customer: retail-pi-01

### 8.2 Monitoring Operational Health

**Daily:**
- Check command interface dashboard
- Look for: red alerts, failed work requests, stuck scheduler queue
- If scheduler stuck > 5 min: manual restart

**Weekly:**
- Review bug reports: are patterns emerging?
- Check token usage: any spikes?
- Monitor customer heartbeats: are all active Pis alive?

**Monthly:**
- Review Engineer's optimization suggestions
- Plan major code refactors
- Archive inactive customers (>45 days silent)

### 8.3 Disaster Recovery

**If Company Pi database corrupted:**
1. Stop all agents: `systemctl stop synthos-company`
2. Restore from backup: `cp company.db.backup company.db`
3. Restart: `systemctl start synthos-company`
4. Retail Pis unaffected (they have local databases)
5. Lost data: customer heartbeats since last backup (usually <24h)

**If Scheduler broken:**
1. Kill Scheduler: `pkill -f scheduler.py`
2. Agents continue (may conflict on DB, logged)
3. Fix Scheduler code
4. Restart: `systemctl start synthos-company`
5. Review conflict logs to detect issues

**If Cloudflare tunnel down:**
1. New customer installations fail (they can't reach /install)
2. Existing Pis: unaffected
3. Operator: re-establish tunnel, OR direct customers to manual setup
4. Manual setup: `curl <IP>:5003/install?key=...`

---

## PART 9: TESTING & EXPERIMENTAL ITERATIONS

### 9.1 Testing Framework

**For Retail Pi changes:**
```bash
# Test locally on a spare Pi 2W
pi@test-pi:~/synthos$ python3 -m pytest tests/
pi@test-pi:~/synthos$ bash run_agent_locally.sh trader

# If OK: push to update-staging branch
# Bug Finder validates after auto-deployment
```

**For Company Pi changes:**
```bash
# Test on Company Pi with staging database
pi@admin-pi:~/synthos-company$ sqlite3 company.db.staging < schema.sql
pi@admin-pi:~/synthos-company$ python3 -m pytest tests/

# Deploy to main database if tests pass
```

### 9.2 Experimentation Process

**Hypothesis:** "Moving sentiment analysis to an off-hours window cuts Trader API spend by 20%"

**Test:**
1. Create branch: `experiment/sentiment-offhours`
2. Modify Sentiment agent schedule
3. Deploy to 2–3 test Pis (not production)
4. Token Monitor tracks usage for 1 week
5. Review: did spend decrease?
6. If yes: merge to main. If no: archive branch.

### 9.3 Removing Unsuccessful Agents

**If an agent doesn't prove useful:**
1. Engineer marks as "experimental" in code
2. Control Agent flags: "Low signal, consider deprecation"
3. Operator decides: disable during off-hours, track metrics
4. If no improvement in 1 month: remove from main branch
5. Existing retail Pis keep old version (backwards compatible)
6. New retail Pis don't get the agent code

---

## PART 10: GLOSSARY & DEFINITIONS

| Term | Definition |
|------|-----------|
| **Retail Pi** | Customer's Pi 2W running trading agents (Trader, Research, Sentiment) |
| **Company Pi** | Your Pi 4B running operations (Mail, Interface, Control, Bug Finder, Engineer, Tool, Scheduler) |
| **Heartbeat** | Hourly POST from Retail Pi → Company Pi with status data |
| **License Key** | Forever-valid token issued to customer at purchase |
| **Work Request** | Agent's application to access company.db with duration and priority |
| **Scheduler Grant** | Scheduler's approval to Agent to access database |
| **Market Hours** | 9:30am–4pm ET (NYSE trading window) |
| **Inactive Customer** | No heartbeat for 45+ days |
| **Archived Customer** | Inactive for 365+ days, data moved to archive table |
| **Update Staging** | Git branch used for testing fixes before merge to main |
| **Offline-Capable** | Retail Pi functions without Company Pi |
| **db_guardian** | Concurrency control mechanism for SQLite |

---

## APPENDIX A: CONFIGURATION REFERENCE

### Retail Pi (.env)

```bash
# APIs (customer-provided)
ANTHROPIC_API_KEY=sk-ant-v0xxxx
ALPACA_API_KEY=PKxxxxx
ALPACA_SECRET_KEY=xxxxxxx
CONGRESS_API_KEY=xxxxxxx

# Trading settings
TRADING_MODE=SUPERVISED  # or AUTONOMOUS
POSITION_SIZE=5000       # dollars per trade
CONFIDENCE_THRESHOLD=75  # percent

# Optional: heartbeat to monitor_node
MONITOR_URL=http://monitor-pi.local:5000
MONITOR_TOKEN=abc123def456xyz
PI_ID=retail-pi-01
PI_LABEL=Customer Name
PI_EMAIL=customer@example.com

# Portal
PORTAL_PASSWORD=          # Leave blank for open access on LAN
```

### Company Pi (.env)

```bash
# Database
DATABASE_PATH=/home/pi/synthos-company/data/company.db

# APIs
SENDGRID_API_KEY=SG.xxxxxxxxxxxx
SENDGRID_FROM=alerts@yourcompany.com

# Services
COMMAND_PORT=5002
INSTALLER_PORT=5003
# HEARTBEAT_PORT=5004 — DEPRECATED; heartbeat_receiver.py was never built; see HEARTBEAT_RESOLUTION.md

# Scheduler
SCHEDULER_TIMEOUT_SEC=120
MARKET_HOURS_START=0930
MARKET_HOURS_END=1600
MARKET_TIMEZONE=US/Eastern

# GitHub (for pushing updates)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx
GITHUB_REPO=personalprometheus-blip/synthos
```

---

## APPENDIX B: CRITICAL TIMINGS

| Event | Frequency | Duration | Window |
|-------|-----------|----------|--------|
| Trader execution | 3x daily | 2 min | 9:30am, 12:30pm, 3:30pm ET |
| Research agent | Hourly | 1 min | 8am–8pm ET |
| Sentiment agent | Every 30 min | 1 min | 8am–8pm ET |
| Heartbeat | Hourly | 5 sec | Market hours; 4-hourly off-hours |
| Bug Finder scan | Daily | 10–30 min | 6pm ET (off-hours) |
| Engineer optimization | Weekly | 30 min | Friday 6pm ET |
| Token Monitor report | Daily | 2 min | 8am ET |
| Database backup | Daily | 5 min | 2am ET (off-hours) |

---

## APPENDIX C: SECURITY CONSIDERATIONS

**Threats & Mitigations:**

| Threat | Mitigation |
|--------|-----------|
| Customer Pi hacked | Attacker gets access to local trades, not other customers (isolated DBs) |
| License key leaked | Key is forever-valid but only activates that specific pi_id |
| Company Pi breached | Attacker gets heartbeat logs, bug reports, not customer API keys (stored locally) |
| Anthropic API key exposed | Attacker can make trades via compromised Retail Pi, but only to that Pi's Alpaca account |
| Database SQL injection | Use parameterized queries (all DB calls) |
| Man-in-the-middle (heartbeat) | Validate MONITOR_TOKEN before logging; use HTTPS tunnel |

**Best practices:**
- All .env files: `chmod 600` (read-only by owner)
- Database backups: encrypted, stored off-site
- API keys: rotated quarterly
- Audit logs: immutable, kept 2 years

---

## END OF DOCUMENT

**Version:** 1.0  
**Last Updated:** March 2026  
**Next Review:** June 2026

**Questions?** Escalate architectural decisions to the project lead before implementation.

---

## PART 11: MANAGEMENT & ACCOUNTABILITY FRAMEWORK

### 11.1 Core Principle: Managers, Not Servants

All agents (Patches, Blueprint, Sentinel, Fidget, Librarian, Scoop, Vault, Timekeeper) are **managers** with defined responsibilities and accountability. They are not task executors—they are decision-makers who must:

1. **Watch each other** — Catch problems before they reach you
2. **Hold each other accountable** — Challenge decisions, raise concerns, escalate when needed
3. **Own outcomes** — Not just "I ran the code," but "I stand behind this decision"
4. **Communicate honestly** — Tell you what they actually think, even if it's uncomfortable

**Your role:** Final decision-maker. Not micro-manager. Not task assigner. Leader who hears competing perspectives and decides.

---

### 11.2 Manager Roles & Accountability

| Manager | Primary Watch | Accountability | Authority |
|---------|---------------|-----------------|-----------|
| **Patches** | System health, code stability, bugs | "Did we ship broken code?" | Halt production if critical flaw detected |
| **Blueprint** | Code quality, performance, technical debt | "Is this maintainable?" | Reject implementation that's sloppy |
| **Sentinel** | Customer health, heartbeat liveness | "Are our customers alive?" | Escalate if >3 Pis silent |
| **Fidget** | Cost efficiency, token waste | "Are we hemorrhaging money?" | Flag unexpected spend |
| **Librarian** | Security, dependencies, compliance | "Are we vulnerable?" | Require security updates before ship |
| **Vault** | License compliance, customer status | "Are we following rules?" | Block unlicensed operations |
| **Scoop** | Customer communication, alerts | "Are messages reaching customers?" | Queue and retry if delivery fails |
| **Timekeeper** | System resources, scheduling fairness | "Is the system overloaded?" | Refuse requests that would deadlock |

---

### 11.3 Decision Escalation: When Managers Disagree

**Scenario:** Patches discovers a critical edge case in Blueprint's fix. Blueprint says the risk is acceptable. You need to decide.

**The Process:**

**Step 1: Patches raises concern (forcefully)**

```json
{
  "id": "uuid",
  "agent": "Patches",
  "timestamp": "2026-03-24T14:30:00Z",
  "category": "critical_decision",
  "title": "REJECT: Blueprint's fix has critical edge case",
  
  "message_to_leadership": "Blueprint's fix for issue X is technically sound but introduces a vulnerability in scenario Y. This is a deal-breaker. I recommend HOLDING the fix until the edge case is handled. The original bug is bad, but shipping known-broken code is worse.",
  
  "technical_details": {
    "original_bug": "Trader crashes on Congress.gov rate limit",
    "blueprint_fix": "Add exponential backoff retry",
    "edge_case": "If Alpaca API is also slow, retry queue grows unbounded → memory leak",
    "reproduction": "High Congressional activity (>100 disclosures/hour) + Alpaca latency spikes (both rare, but possible)",
    "impact_if_triggered": "Pi runs out of memory, all agents crash, customer loses trading",
    "likelihood_per_day": "0.5% (rare, but not impossible)"
  },
  
  "recommendation": "HOLD. Don't ship until edge case is bounded.",
  "severity": "CRITICAL",
  "status": "pending_your_decision",
  "approver_needed": "you"
}
```

**Step 2: Blueprint responds (defending their position)**

```json
{
  "id": "uuid",
  "agent": "Blueprint",
  "timestamp": "2026-03-24T14:45:00Z",
  "category": "critical_decision",
  "title": "COUNTER: Accept edge case risk, ship with mitigation",
  
  "message_to_leadership": "Patches is correct about the edge case. I've analyzed it. Here's why I still recommend shipping: (1) The edge case requires two rare events simultaneously (0.5% per day). (2) The original bug happens 3x per week, affecting real customers NOW. (3) I propose shipping with a safeguard: memory limit on retry queue. If queue exceeds 1000 items, oldest items are dropped (acceptable loss). This bounds the leak.",
  
  "technical_details": {
    "original_bug_frequency": "3 times per week",
    "original_bug_impact": "Customer can't trade for 5 minutes",
    "edge_case_frequency": "0.5% per day = ~1.8 times per year",
    "edge_case_impact": "Pi crashes, requires manual restart",
    "proposed_mitigation": "Queue size limit (1000 items) + monitoring + auto-alert if queue grows",
    "mitigation_cost": "1 hour coding, 2 hours testing"
  },
  
  "decision_matrix": "Wait for perfect fix (1–2 weeks) vs. ship with known mitigation (48 hours). Customers suffering now vs. hypothetical crash once a year.",
  
  "recommendation": "SHIP WITH MITIGATION. Monitor closely. Revert if edge case triggers.",
  "severity": "HIGH",
  "status": "pending_your_decision",
  "approver_needed": "you"
}
```

**Step 3: You (the leader) decide**

You see both perspectives. You make the call and own it:

```json
{
  "decision": "SHIP WITH MITIGATION",
  "reasoning": "Original bug affects 3 customers actively. Edge case is rare + mitigated. Risk is acceptable.",
  "conditions": [
    "Implement queue size limit (1000 items) as proposed",
    "Monitor queue depth for 48 hours post-deployment",
    "Auto-alert if queue exceeds 800 items",
    "Auto-revert if edge case is triggered",
    "Schedule round 2 fix for next sprint"
  ],
  "accountability": "Blueprint owns implementation. Patches owns monitoring. Both accountable for results."
}
```

---

### 11.4 Peer Accountability in Action

**Example: Sentinel notices customers are silent, Vault says it's expected.**

Sentinel challenges Vault (their job):
```
"Vault, I'm seeing 7 Pis with no heartbeat for >4 hours during market hours. 
You said this was batch maintenance. Can you confirm completion? 
If not, we need to investigate now."
```

Vault admits gap, escalates:
```
"You're right to push. Maintenance is complete on 6/7 Pis. 
One Pi (retail-pi-03) is still silent. Escalating to Patches."
```

Patches analyzes and reports:
```
"Found it: retail-pi-03 has intermittent power shutdowns. 
Hardware issue, not code. Recommending customer outreach 
(Scoop) for hardware replacement."
```

**The result:** No silent failures. Clear communication. You wake up informed.

---

### 11.5 Escalation Rules: When Disagreements Go To You

| Situation | Escalate? |
|-----------|-----------|
| Blueprint wants to ship, Patches says "critical flaw" | **YES** (risk decision is yours) |
| Fidget flags unusual spend, Blueprint says "expected" | **YES** (trust but verify) |
| Librarian finds CVE, Blueprint says "can wait" | **YES** (security is non-negotiable) |
| Patches finds bug, Blueprint fixes it, both agree ready | **NO** (ship immediately) |
| Fidget flags minor inefficiency, Blueprint says "low priority" | **NO** (engineering owns prioritization) |

---

### 11.6 Accountability Record: Suggestions.json as Permanent Audit Trail

Every manager speaks on the record:

```json
{
  "agent": "Patches",
  "message_to_leadership": "I, Patches, flag this as risky. If we ship and it breaks, I flagged it. If we hold and it costs customers, I recommended holding.",
  "status": "pending_your_decision"
}
```

When you approve:

```json
{
  "decision": "SHIP WITH MITIGATION",
  "decision_maker": "you",
  "accountability": "I reviewed both sides. I decided to ship. I own this decision and its consequences."
}
```

This creates an **immutable record.** If something fails, you trace back to understand the thinking, not assign blame.

---

### 11.7 Failure Modes When Accountability Breaks

| Failure | Warning Sign | Fix |
|---------|--------------|-----|
| **Silent failures** | No suggestions for days, sudden crash | Demand communication. Make escalation safe. |
| **Blame culture** | Managers pointing fingers | Reframe: "We're a team. What did we miss?" |
| **Rubber stamp** | Every suggestion auto-approved, no disagreements | Managers are being too cautious. Show it's safe to have opinions. |
| **Escalation fatigue** | Too many decisions dumped on you | Empower managers to resolve disagreements without you. |

---

## END OF DOCUMENT

**Version:** 1.1  
**Last Updated:** March 24, 2026  

**Critical Note:** Part 11 is foundational. All agents are managers. They watch each other, hold each other accountable, and escalate to you when the team disagrees. Your job: decide quickly and own the consequences.
