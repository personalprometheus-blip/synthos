# SYNTHOS TECHNICAL ARCHITECTURE
## System-Wide Design and Agent Integration Model

**Document Version:** 3.0
**Date:** March 2026
**Supersedes:** v2.0 (SYNTHOS_TECHNICAL_ARCHITECTURE_1_.md)
**Audience:** Engineers, AI agents building/maintaining the system
**Scope:** Retail customer deployments + company infrastructure + web access layer

---

## CHANGE LOG (v2.0 → v3.0)

| Change | Detail |
|--------|--------|
| Agent renaming | Bolt → ExecutionAgent, Scout → DisclosureResearchAgent, Pulse → MarketSentimentAgent |
| First-run assumption removal | All agent logic now explicitly handles prior-state scenarios |
| Agent integration contract | New Part 2.5 — standard interface all agents must implement |
| IP allowlist (production) | New Part 4.4 — Pi outbound restriction to approved news feed IPs |
| Web access layer reference | Points to SYNTHOS_ADDENDUM_2_WEB_ACCESS.md for full spec |

---

## EXECUTIVE SUMMARY

Synthos is a distributed system with three tiers:

| Tier | Hardware | Purpose | Autonomy |
|------|----------|---------|----------|
| **Retail** | Pi 2W | Customer trading agents | Completely standalone |
| **Company** | Pi 4B | Operations, monitoring, experiments | Internal only |
| **Web Access** | Cloud-hosted | End-user portal delivery via domain tunnel | See Addendum 2 |

**Key principle:** Retail Pis are *self-contained forever*. Company Pi is *optional infrastructure*. Customers connect through the web portal. Pis are never directly reachable from the public internet.

---

## PART 1: SYSTEM ARCHITECTURE OVERVIEW

### 1.1 High-Level System Diagram

```
                    CUSTOMER ENVIRONMENT
                    ====================

    ┌─────────────────────────────────────────┐
    │  Retail Pi 2W (retail-pi-01)            │
    │  ─────────────────────────────────────  │
    │  • ExecutionAgent  (agent1_trader.py)   │
    │  • DisclosureResearchAgent              │
    │              (agent2_research.py)       │
    │  • MarketSentimentAgent                 │
    │              (agent3_sentiment.py)      │
    │  • Portal (localhost:5001)              │
    │  • SQLite (signals.db)                  │
    │  • License key validator                │
    │  ─────────────────────────────────────  │
    │  Runs: Always (offline-first)           │
    │  Updates: Git pull (optional)           │
    │  Dependency: None on Company Pi         │
    │  Network: Outbound only — approved      │
    │           IPs only (see Part 4.4)       │
    └──────────┬──────────────────────────────┘
               │
               ├─→ [OPTIONAL] ─→ Session-end heartbeat POST
               │                  to Company Pi
               │                  (customer can disable)
               │
               └─→ Alpaca API (paper/live trades)


                    COMPANY ENVIRONMENT
                    ===================

    ┌─────────────────────────────────────────┐
    │  Pi 4B (admin-pi-4b)                    │
    │  ─────────────────────────────────────  │
    │  COMPANY OPERATIONS:                    │
    │  • Patches   (bug detection, triage)    │
    │  • Blueprint (code improvement)         │
    │  • Sentinel  (customer health)          │
    │  • Fidget    (cost efficiency)          │
    │  • Librarian (security, deps)           │
    │  • Scoop     (all outbound comms)       │
    │  • Vault     (keys, licensing)          │
    │  • Timekeeper (resource scheduling)     │
    │  • Strongbox  (backup management)       │
    │                                         │
    │  DATA:                                  │
    │  • SQLite (company.db)                  │
    │  • Schema: customers, heartbeats,       │
    │    api_usage, bug_reports, keys, logs   │
    │                                         │
    │  SERVICES:                              │
    │  • Command Interface (5002)             │
    │  • Installer Delivery (5003)            │
    │  • Heartbeat Receiver (5004)            │
    │  ─────────────────────────────────────  │
    │  Runs: 24/7                             │
    └─────────────────────────────────────────┘
               │
               └─→ Cloudflare Tunnel
                   (exposes 5003 for installer delivery)


                    WEB ACCESS LAYER
                    ================

    ┌─────────────────────────────────────────┐
    │  Cloud-hosted web server                │
    │  ─────────────────────────────────────  │
    │  • Login portal (your domain)           │
    │  • Tunnels session to customer's Pi     │
    │  • Employee accounts (admin access)     │
    │  • End-user accounts (portal proxy)     │
    │  • User provisioning service            │
    │  ─────────────────────────────────────  │
    │  Full spec: SYNTHOS_ADDENDUM_2_WEB.md   │
    └─────────────────────────────────────────┘


                    EXTERNAL SERVICES
                    =================

    • Alpaca (paper/live trading API)
    • Congress.gov (disclosure data)
    • Anthropic (Claude API)
    • Cloudflare (tunnel for installer delivery)
    • Approved news feed providers (see §4.4)
    [SendGrid routed exclusively through Scoop on Company Pi]
    [GitHub — operator/engineer access only, not customer-facing]
```

### 1.2 Data Flow

**Scenario A: Customer trading (offline-capable)**
```
1. Retail Pi agents run on schedule
2. DisclosureResearchAgent fetches Congress.gov disclosures
3. MarketSentimentAgent scores market context
4. ExecutionAgent calls Claude, decides action
5. Trade executes on Alpaca account
6. Result written to local signals.db
7. Portal displays to customer
[Company Pi never involved]
```

**Scenario B: Customer accesses portal through web**
```
1. Customer navigates to your domain (e.g. app.synthos.com)
2. Login authenticates against web-hosted user store
3. Session proxies through to customer's retail Pi portal
4. Customer interacts with their Pi portal via the web session
5. All Pi traffic remains behind IP restrictions (see §4.4)
[Full specification: SYNTHOS_ADDENDUM_2_WEB.md]
```

**Scenario C: New customer installation**
```
1. Customer powers on Pi 2W, connects to network
2. Navigates to https://your-tunnel.com/install
3. Enters license key (from command interface) — key collected and stored; validation deferred
4. Installer script pulls current synthos from GitHub
5. Runs 7-step setup wizard (key validation — DEFERRED_FROM_CURRENT_BASELINE; not enforced in current release)
6. Pi reboots, agents start
7. Agents check for existing state before initializing (see §2.5)
```

**Scenario D: Bug fix / code update**
```
1. Patches identifies issue in bug logs
2. Blueprint implements fix on update-staging
3. Project lead approves Friday push
4. Blueprint merges to main
5. Customer Pi pulls update (automatic or manual)
6. New agent code in /core/ loads
7. Agents resume from existing state — not fresh init (see §2.5)
[User settings in /user/ untouched]
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
${SYNTHOS_HOME}/
│
├── core/                          # Company-managed (updatable)
│   ├── agent1_trader.py           # ExecutionAgent
│   ├── agent2_research.py         # DisclosureResearchAgent
│   ├── agent3_sentiment.py        # MarketSentimentAgent
│   ├── database.py                # SQLite helpers, schema
│   ├── portal.py                  # Local web UI (5001)
│   ├── synthos_heartbeat.py       # Session-end POST to Company Pi
│   ├── license_validator.py       # DEFERRED_FROM_CURRENT_BASELINE — not yet built; future retail entitlement gate
│   ├── boot_sequence.py           # Start agents in order
│   ├── watchdog.py                # Restart crashed agents
│   ├── health_check.py            # Verify system health
│   ├── shutdown.py                # Graceful pre-maintenance shutdown
│   ├── cleanup.py                 # Nightly database maintenance
│   ├── patch.py                   # Safe file replacement with backup
│   ├── install.py                 # Guided installer with web UI
│   ├── sync.py                    # Dev sync utility
│   └── uninstall.py               # Full system removal
│
├── user/                          # Customer-owned (chmod 444)
│   ├── .env                       # API keys, trading settings, mode
│   ├── settings.json              # Portal preferences, thresholds
│   └── agreements/                # Legal documents (read-only)
│
├── data/
│   ├── signals.db                 # SQLite: signals, positions, trades
│   ├── license_cache.json         # DEFERRED_FROM_CURRENT_BASELINE — future; written by license_validator.py when built
│   └── backup/                    # Daily backup of signals.db
│
└── logs/
    ├── trader.log
    ├── research.log
    ├── sentiment.log
    ├── heartbeat.log
    ├── system.log
    └── health.log
```

### 2.3 Database Schema (Retail Pi)

**Authoritative schema:** `docs/specs/DATABASE_SCHEMA_CANONICAL.md` §3.1

The pre-v3.1 inline schema definition in this section was stale and materially incorrect (wrong field names, phantom tables `trades` / `agent_status` / `license` / `config`, missing tables `portfolio` / `ledger` / `outcomes` / `handshakes` / `scan_log` / `urgent_flags` / `pending_approvals` / `member_weights` / `news_feed`). It has been replaced with this reference.

**Actual database:** `${DATA_DIR}/signals.db` (SQLite, WAL mode)
**Access layer:** `src/database.py` — `DB` class
**Schema version:** v1.2 (includes v1.1 and v1.2 migration columns)
**Tables:** portfolio, positions, signals, ledger, outcomes, handshakes, scan_log, system_log, urgent_flags, pending_approvals, member_weights, news_feed

See `docs/specs/DATABASE_SCHEMA_CANONICAL.md` for full table definitions, field types, indexes, access patterns, and known limitations.

### 2.4 Agents (Retail Pi)

> **Naming rule:** Nicknames (Bolt, Scout, Pulse) are retired. Agent names reflect function.
> File names (`agent1_trader.py` etc.) remain unchanged — canonical names are display identifiers only.

---

**ExecutionAgent** (`agent1_trader.py`)
- **Function:** Executes trades on the Alpaca API based on signals produced by DisclosureResearchAgent and flags produced by MarketSentimentAgent
- **Schedule:** 9:30am, 12:30pm, 3:30pm ET (market hours)
- **Input:**
  - `signals` table: approved or queued signals
  - `positions` table: open positions for exit decisions
  - `.kill_switch` file: checked at session start; halts all activity if present
  - `.pending_approvals.json`: trade proposals awaiting portal approval (supervised mode)
- **Process:**
  1. Check kill switch — abort immediately if present
  2. Check license validity via `license_validator.py`
  3. Load operating mode from `.env` (SUPERVISED or AUTONOMOUS)
  4. Query `signals` table for status=PENDING signals; do not re-create if already present
  5. In SUPERVISED mode: propose trades, write to `.pending_approvals.json`, wait for portal approval
  6. In AUTONOMOUS mode: verify AUTONOMOUS_UNLOCK_KEY, execute per pre-defined rules
  7. Execute approved signals on Alpaca; write outcome to `trades` and `signals` tables
  8. Check open positions against urgent flags from MarketSentimentAgent; execute protective exits if flagged
  9. Send session-end heartbeat via `synthos_heartbeat.py`
- **State continuity:** On any run, reads existing signals and positions from the database. Does not re-initialize or overwrite records that were created in prior sessions. Trade history, open positions, and signal scores from prior runs are inputs to the current session, not discarded data.
- **Output:** Trade confirmations, updated signal statuses, log entries
- **Controls:**
  - Supervised mode: portal approval required before execution
  - Autonomous mode: pre-defined rules, requires unlock key
  - Kill switch: halts immediately, logged
  - Protective exit: triggered by MarketSentimentAgent urgent flags
- **Communication rule:** Does NOT send emails or alerts directly. Urgent conditions are flagged to the database; Scoop on the Company Pi handles delivery.
- **Failure modes:**
  - Alpaca API down → queue signals, retry next window
  - Network failure → exponential backoff
  - Invalid license → skip execution, log warning, portal alert

---

**DisclosureResearchAgent** (`agent2_research.py`)
- **Function:** Fetches Congressional trading disclosures from Congress.gov and scores them as signals for ExecutionAgent
- **Schedule:** Hourly during market hours (8am–8pm ET)
- **Input:**
  - Congress.gov API (disclosures endpoint)
  - `signals` table: checked for already-processed disclosures (deduplication)
  - `signals` table: `needs_reeval` flag — up to 10 WATCH signals re-evaluated per run
- **Process:**
  1. Fetch new disclosures from Congress.gov since last recorded timestamp
  2. Deduplicate: skip any disclosure already present in `signals` by disclosure ID
  3. Score each new disclosure (HIGH/MEDIUM/LOW)
  4. Upsert into `signals` table with status=PENDING
  5. Flag cascade signals (same member, multiple tickers in short window)
  6. Re-evaluate up to 10 signals with `needs_reeval=1`; update scores in place
  7. Send session-end heartbeat
- **State continuity:** Tracks last-fetched disclosure timestamp in `config` table. On any run — including post-update runs — reads this timestamp first and fetches only records newer than it. Never repopulates signals that already exist.
- **Output:** New signals written to database, re-scored WATCH signals updated
- **Controls:**
  - Confidence threshold (adjustable in portal: 60–95%)
  - Max signals per run (prevent database bloat)
- **Communication rule:** Does NOT send alerts. Signal data is written to database only.
- **Failure modes:**
  - Congress.gov down → skip run, retry next hour, log warning
  - API key invalid → portal alert "Congress API misconfigured"

---

**MarketSentimentAgent** (`agent3_sentiment.py`)
- **Function:** Monitors market sentiment and open positions; generates urgent exit flags when conditions turn adverse
- **Schedule:** Every 30 min during market hours
- **Input:**
  - Approved news feeds (see §4.4 for IP allowlist)
  - `positions` table: open positions to evaluate
  - `signals` table: cascading signal context
- **Process:**
  1. Fetch current sentiment data from approved feeds only
  2. Query open positions from database
  3. For each open position: score sentiment context (positive/neutral/adverse)
  4. Detect cascade signals (same member, same ticker, different transaction direction)
  5. If adverse: write urgent flag to `signals` table with status=URGENT
  6. Update sentiment scores in database for ExecutionAgent to consume on its next run
  7. Send session-end heartbeat
- **State continuity:** Reads open positions as they exist in the database at run time. Does not assume this is the first time a position has been evaluated. Prior sentiment scores are context for this session, not discarded.
- **Output:** Urgent exit flags, updated sentiment scores
- **Controls:**
  - Sentiment threshold (portal-adjustable: when to flag as adverse)
  - Cascade detection sensitivity
- **Communication rule:** Does NOT send alerts. Urgent flags written to database; ExecutionAgent acts on them; Scoop delivers notification via Company Pi.
- **Failure modes:**
  - Approved feeds unreachable → use cached sentiment from last 4 hours
  - No open positions → skip sentiment scoring, log as success

---

### 2.5 Agent Integration Contract

This section defines the standard interface every retail agent must implement. It exists to ensure that new agents — or updated versions of existing agents — can be dropped into the system with minimal configuration. An agent that fully implements this contract requires no changes to boot_sequence.py, watchdog.py, portal.py, or the database schema.

#### 2.5.1 Required Methods

Every agent must implement the following:

```python
class SynthosAgent:

    def __init__(self):
        """
        Read configuration from .env and settings.json.
        Do NOT fetch data, open database connections, or execute logic here.
        Init must be safe to call at any time without side effects.
        """
        self.agent_name = "descriptive_agent_name"  # Used in logs + DB
        self.agent_file = Path(__file__).resolve()   # Never hardcode path

    def check_preconditions(self) -> bool:
        """
        Verify the agent is safe to run:
        - Kill switch not present
        - License valid (via license_validator.py)
        - Required API keys present in .env
        - Database accessible
        Returns True if all conditions met; False to abort cleanly.
        Logs reason for any False return.
        """
        pass

    def load_existing_state(self) -> dict:
        """
        Read current state from the database before doing anything.
        Returns a dict describing what already exists:
        - open positions
        - pending signals
        - last run timestamp
        - any prior session flags
        This method enforces the principle that agents NEVER assume
        the database is empty or that this is the first run.
        """
        pass

    def run_session(self, state: dict) -> dict:
        """
        Execute the agent's core logic for this session.
        Receives the state dict from load_existing_state().
        Must:
        - Act on existing data, not replace it
        - Write outputs to database via database.py only
        - Never send alerts directly (write flags; let Scoop deliver)
        - Return a summary dict for logging
        """
        pass

    def send_heartbeat(self, session_summary: dict):
        """
        POST session-end heartbeat to Company Pi via synthos_heartbeat.py.
        Include: agent_name, timestamp, status, session_summary.
        Must be called at end of every session, including failed sessions.
        """
        pass

    def handle_failure(self, error: Exception):
        """
        Log the failure. Write error to agent_status table.
        Do NOT crash silently. Do NOT corrupt existing database state.
        Call send_heartbeat() with error status before exiting.
        """
        pass
```

#### 2.5.2 Database Write Rules

- All writes go through `database.py` helpers — never raw `sqlite3` connections
- Agents read existing state first, then write only what is new or updated
- No agent may DROP, TRUNCATE, or DELETE records unless explicitly implementing a cleanup role (e.g., cleanup.py)
- Upsert pattern: use `INSERT OR REPLACE` / `INSERT OR IGNORE` with unique constraints — never bulk-insert without deduplication

#### 2.5.3 Scheduling Registration

Agents are scheduled via crontab entries written at install time. A new agent registers by adding a cron entry following this pattern:

```bash
# Pattern: run at specified times, log to agent-specific log file
MM HH * * * python3 ${CORE_DIR}/agent_new.py >> ${LOG_DIR}/agent_new.log 2>&1
```

The agent is also registered in `boot_sequence.py` health check list and in `watchdog.py` process list. These registrations require a one-time addition at integration time — no other files need modification.

#### 2.5.4 Portal Visibility

For an agent to appear in the portal's system status page, it must write to the `agent_status` table on every session completion:

```python
database.update_agent_status(
    agent_name=self.agent_name,
    status="SUCCESS" | "ERROR",
    last_run=datetime.utcnow(),
    error_message=None | str(error)
)
```

No portal code changes are needed. The portal reads `agent_status` dynamically.

#### 2.5.5 Dropping In a Replacement Agent

To replace an existing agent with an updated version or entirely new logic:

1. New agent file implements all methods in §2.5.1
2. File is placed at the existing agent path (e.g., `${CORE_DIR}/agent1_trader.py`)
3. No changes to boot_sequence.py, watchdog.py, database.py, or portal.py
4. Cron entry remains unchanged (agent file name is the reference)
5. On next scheduled run, new logic executes — database state from prior agent is fully intact
6. If rollback needed: `patch.py` restores prior version from `.known_good/` snapshot

---

## PART 3: COMPANY PI ARCHITECTURE (Internal Operations)

### 3.1 Hardware & OS

**Target Device:** Raspberry Pi 4B (8GB RAM recommended)
**OS:** Raspberry Pi OS Lite
**Always-on:** 24/7, power backup recommended

### 3.2 Directory Structure

```
${SYNTHOS_HOME}/    # company node
│
├── agents/
│   ├── patches.py
│   ├── blueprint.py
│   ├── sentinel.py
│   ├── fidget.py
│   ├── librarian.py
│   ├── scoop.py
│   ├── vault.py
│   ├── timekeeper.py
│   └── strongbox.py
│
├── utils/
│   └── db_helpers.py
│
├── data/
│   └── company.db
│
├── config/
│   ├── agent_policies.json
│   ├── market_calendar.json
│   ├── priorities.json
│   └── allowed_ips.json        # approved news feed IPs + internal IPs
│
└── logs/
    └── [per-agent log files]
```

### 3.3 Database Schema (Company Pi)

**Authoritative schema:** `docs/specs/DATABASE_SCHEMA_CANONICAL.md` §3.2

The pre-v3.1 reference ("See SYSTEM_MANIFEST for full schema") was incorrect — SYSTEM_MANIFEST does not contain a company.db schema. The schema is defined in `synthos-company/utils/db_helpers.py` `_bootstrap_inline()`.

**Actual database:** `${COMPANY_DATA_DIR}/company.db` (SQLite, WAL mode)
**Access layer:** `utils/db_helpers.py` — `DB` class
**Schema version:** v2.0 (per schema_version table)
**Tables:** customers, heartbeats, keys, audit_trail, suggestions, scoop_queue, deploy_watches, work_requests, api_usage, token_ledger, backup_log, silence_alerts, schema_version

See `docs/specs/DATABASE_SCHEMA_CANONICAL.md` for full table definitions, field types, indexes, Timekeeper slot access model, and known limitations.

### 3.4 Company Agents

**Naming rule:** Company agent names are proper nouns (Patches, Blueprint, Sentinel, etc.) and have not changed. These names are canonical.

Agent responsibilities, schedules, failure modes, and concurrency rules are unchanged from v2.0. See SYNTHOS_TECHNICAL_ARCHITECTURE_1_.md §3.4 for full detail.

### 3.5 Services (Company Pi)

Services on ports 5002 (Command Interface), 5003 (Installer Delivery), and 5004 (Heartbeat Receiver) are unchanged from v2.0.

---

## PART 4: SECURITY & NETWORK MODEL

### 4.1 Transport Security

All HTTP traffic between retail Pis and Company Pi uses HTTPS via Cloudflare tunnel (TLS 1.3). No additional application-layer encryption is required for transport.

For direct Pi-to-Pi communication (if implemented in future), use mutual TLS with certificates issued by Vault.

### 4.2 Payload Signing

Heartbeat payloads are HMAC-signed with `MONITOR_TOKEN`. Sentinel validates the signature before writing to the database. Unsigned or tampered payloads are rejected (401) and logged. Repeated failures trigger a Patches alert.

### 4.3 Company Pi IP Isolation

The Company Pi heartbeat receiver and Vault validation endpoint enforce an IP allowlist stored in `config/allowed_ips.json`. POSTs from unknown IPs return 403 and are logged. Patches is alerted on repeated unknown IP attempts.

This list governs inbound access to the Company Pi's services. It is separate from the retail Pi outbound allowlist in §4.4.

### 4.4 Retail Pi Outbound IP Allowlist (News Feed Restriction)

**Purpose:** Retail Pis must only connect to a defined list of approved external IPs. This prevents unauthorized data exfiltration, unexpected API costs, and lateral attack surface. No public internet access is permitted directly to a Pi except through the approved list.

**Enforcement:** `allowed_outbound_ips.json` is stored in `${SYNTHOS_HOME}/config/` on each retail Pi. A network filtering layer (iptables rules written at install time) enforces this list at the OS level, not just the application level.

```json
{
  "version": "1.0",
  "description": "Approved outbound IPs for retail Pi. All other outbound blocked.",
  "mode": "production",
  "updated_at": "2026-03-28",
  "approved_feeds": [
    {
      "provider": "Alpaca Markets",
      "purpose": "Trade execution + market data",
      "ips": ["ALPACA_IP_RANGES"],
      "ports": [443]
    },
    {
      "provider": "Congress.gov",
      "purpose": "Disclosure data",
      "ips": ["CONGRESS_GOV_IP"],
      "ports": [443]
    },
    {
      "provider": "Anthropic",
      "purpose": "Claude API",
      "ips": ["ANTHROPIC_IP_RANGES"],
      "ports": [443]
    },
    {
      "provider": "[Approved News Feed 1]",
      "purpose": "Sentiment data",
      "ips": ["FEED_1_IP"],
      "ports": [443]
    }
  ],
  "approved_internal": [
    {
      "target": "Company Pi heartbeat receiver",
      "purpose": "Session-end heartbeat POST",
      "ips": ["COMPANY_PI_IP"],
      "ports": [5004]
    }
  ],
  "blocked_by_default": "all other outbound traffic"
}
```

**iptables enforcement (written at install time):**

```bash
# Flush existing rules
iptables -F OUTPUT

# Allow loopback
iptables -A OUTPUT -o lo -j ACCEPT

# Allow established connections back in
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow each approved IP/port combination
# (installer writes these dynamically from allowed_outbound_ips.json)
iptables -A OUTPUT -d <ALPACA_IP> -p tcp --dport 443 -j ACCEPT
iptables -A OUTPUT -d <CONGRESS_IP> -p tcp --dport 443 -j ACCEPT
iptables -A OUTPUT -d <ANTHROPIC_IP> -p tcp --dport 443 -j ACCEPT
iptables -A OUTPUT -d <COMPANY_PI_IP> -p tcp --dport 5004 -j ACCEPT

# Block everything else
iptables -A OUTPUT -j DROP

# Persist rules across reboot
iptables-save > /etc/iptables/rules.v4
```

**Managing the allowlist:**
- Updates to `allowed_outbound_ips.json` require a corresponding iptables update and a Pi restart
- Vault is responsible for distributing approved IP list updates to retail Pis via the patch mechanism
- Librarian audits the iptables ruleset against `allowed_outbound_ips.json` on its regular security scan
- Any mismatch (ruleset more permissive than the JSON) is flagged as CRITICAL by Librarian

**Connecting through the web portal:** End users do not connect directly to the Pi. The web portal (see SYNTHOS_ADDENDUM_2_WEB.md) proxies the connection. The Pi only ever communicates with the approved IP list — it never accepts inbound connections from the public internet.

### 4.5 SSH Access

SSH access to retail Pis is restricted to the operator's known IPs. Configured at install time. Customer-facing access to the Pi is exclusively through the portal, not SSH.

---

## PART 5: UPDATE & DEPLOYMENT FLOW

*(Unchanged from v2.0 except for agent name references updated throughout.)*

**Scenario: Bug found in ExecutionAgent**

```
1. Patches detects crash pattern in trader.log
2. Blueprint implements fix on update-staging
3. Patches validates, project lead approves Friday push
4. Blueprint merges to main
5. Customer Pi pulls update
6. New agent1_trader.py code loads
7. ExecutionAgent reads existing signals.db state on first run —
   no re-initialization, no data loss (see §2.5 contract)
8. Watchdog activates heightened monitoring (48h)
```

**Protected files — never updated:**
- `${USER_DIR}/.env`
- `${USER_DIR}/settings.json`
- `${USER_DIR}/agreements/`
- `${DATA_DIR}/signals.db`

---

## PART 6: FAILURE MODES & RESILIENCE

### 6.1 Retail Pi Failures

| Failure | Impact | Detection | Recovery |
|---------|--------|-----------|----------|
| **ExecutionAgent crashes** | Pending trades not executed | Watchdog detects process absence | Auto-restart within 2 min |
| **Congress.gov API down** | No new signals | DisclosureResearchAgent logs error | Retry next hour |
| **Database corruption** | Can't read/write trades | Boot sequence fails | Restore from daily backup |
| **Network outage** | Heartbeat can't POST | Heartbeat times out | Queues locally, sends when online |
| **Approved feed unreachable** | No sentiment data | MarketSentimentAgent logs | Use cached 4h window |
| **Unapproved IP attempted** | iptables drops packet | OS-level block, no log entry needed | Expected behavior |
| **Anthropic API key invalid** | ExecutionAgent can't call Claude | API returns 401 | Log error, portal alert |

### 6.2 Company Pi Failures

*(Unchanged from v2.0 — see prior version for full table.)*

---

## PART 7: SCHEDULER (TIMEKEEPER) DETAIL

*(No changes from v2.0. Timekeeper remains the canonical name for the resource coordinator.)*

---

## PART 8: DISASTER RECOVERY

*(Unchanged from v2.0. Strongbox manages all backup and restore.)*

---

## PART 9: WEB ACCESS LAYER

The web access layer — including login portal, user provisioning, employee vs. end-user separation, and Pi session tunneling — is fully specified in:

**SYNTHOS_ADDENDUM_2_WEB_ACCESS.md**

Summary of scope:
- Domain-hosted login portal
- Session proxying to customer Pi portal
- Two user classes: Company Employees, End Users
- User provisioning, credential management, session security
- Pis are never directly addressable from the public internet

---

## PART 10: AGENT MANAGEMENT FRAMEWORK

### 10.1 Managers, Not Servants

*(Unchanged from v2.0. All company agents are managers with defined accountability. See prior version §11.1–11.7 for full framework.)*

---

## END OF DOCUMENT

**Version:** 3.0
**Last Updated:** March 2026
**Supersedes:** SYNTHOS_TECHNICAL_ARCHITECTURE_1_.md v2.0
