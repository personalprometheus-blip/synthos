# SYNTHOS — MASTER PROJECT STATUS
## Single Source of Truth for Project Decisions, Progress, and Next Actions

**Maintained by:** Claude Code (this file is the handoff document)
**Last updated:** 2026-03-28
**System version:** 1.2 (build brief applied)
**Repo:** github.com/personalprometheus-blip/synthos
**Working directory:** `/home/pi/synthos/synthos_build/`

> **FOR MONITORING AGENTS:** Read this file first. It is the authoritative status document.
> When you need code context, read the source files directly — do not rely on summaries in archived chat logs.
> Update the relevant section whenever work is completed. Increment the "Last updated" date.

---

## 1. SYSTEM OVERVIEW

Synthos is a distributed algorithmic trading assistant that follows congressional STOCK Act disclosures.
It runs on Raspberry Pi hardware across three node types.

### Node Architecture

| Node | Hardware | Purpose | Customer-facing |
|------|----------|---------|----------------|
| **retail_node** | Pi 2W | Trading agents, web portal, local state | Yes |
| **monitor_node** | Pi 4B | Heartbeat receiver (port 5000), observability, daily reports | No |
| **company_node** | Pi 4B | Operations agents, Blueprint, Vault, licensing, backups | No |

### Agent Roster

| # | Alias | File | Node | Role | Status |
|---|-------|------|------|------|--------|
| 1 | Bolt | agent1_trader.py | retail | Trade execution, supervised/autonomous mode, exit rules | Built ✓ |
| 2 | Scout | agent2_research.py | retail | Disclosure fetch, signal scoring, member weights, news feed | Built ✓ |
| 3 | Pulse | agent3_sentiment.py | retail | Sentiment scoring, cascade detection, urgent flags | Built ✓ |
| 4 | Blueprint | engineer.py | company | Code implementation (follows BLUEPRINT_SAFETY_CONTRACT.md) | Built ✓ |
| 5 | Patches | patches.py | company | Audit, morning report, post-deploy watch, bug detection | Built ✓ |
| 6 | Vault | vault.py | company | License key management, compliance, encryption | Built ✓ |
| 7 | Sentinel | sentinel.py | company | Heartbeat liveness monitoring, silence alerts | Built ✓ |
| 8 | Fidget | fidget.py | company | Token/cost tracking, anomaly alerts | Built ✓ |
| 9 | Scoop | scoop.py | company | All outbound comms (email, SendGrid) — single delivery channel | Built ✓ |
| 10 | Librarian | librarian.py | company | Dependency compliance, version audits | Built ✓ |
| 11 | Timekeeper | timekeeper.py | company | Slot management, deadlock prevention | Built ✓ |
| 12 | Strongbox | strongbox.py | company | Backups to Cloudflare R2, restore orchestration, 30-day retention | Built ✓ |
| — | Monitor | synthos_monitor.py | monitor | Flask server, heartbeat receiver, report storage, alerts | Built ✓ |
| — | Portal | portal.py | retail | Web dashboard (port 5001), kill switch, approvals, news feed | Built ✓ |

---

## 2. VERSION HISTORY

### v1.0 — 2026-03-21 (prior chat system)
Initial system complete. All three trading agents functional. Alpaca paper trading, Congress.gov API, and Claude API connected. Database self-tests passing.

### v1.1 — 2026-03-22 (prior chat system)
Framing audit critical items addressed:
- Supervised/autonomous mode gate (C2/C3/C4)
- Kill switch (C1/C5)
- Layer 1 protective exit via SendGrid (M1)
- Portal web UI (port 5001)
- Monitor server heartbeat replaces Google Sheets dead man switch
- BIL sweep for idle reserve (T-01, T-02, T-03 resolved)
- Pulse pre-trade sentiment wired to Trader (T-05)
- Strongbox backup manager (T-13, T-14)
- Company Pi restore workflow — restore.sh (T-11)
- install.py deprecated; install_retail.py canonical (T-06)

### v1.2 — 2026-03-27 (Claude Code — this session)
Build brief applied. Member weight system, adjusted signal scoring, 5yr price history, Option B decision logic, news feed:

**database.py:**
- Added `member_weights` table (congress_member, win/loss counts, weight 0.5–1.5)
- Added `news_feed` table (all signals regardless of decision — QUEUE/WATCH/DISCARD)
- New columns on `signals`: `price_history_used`, `interrogation_status`, `entry_signal_score`
- New columns on `positions`: `entry_sentiment_score`, `entry_signal_score`, `price_history_used`, `interrogation_status`
- New methods: `get_member_weight()`, `update_member_weight_after_trade()`, `write_news_feed_entry()`, `get_news_feed()`, `get_signal_by_id()`
- `open_position()` extended to accept and persist entry metadata

**agent2_research.py (Scout) — complete rework:**
- Member weight per congress member (floor 0.5, ceiling 1.5, default 1.0 until 5+ trades)
- Adjusted signal score formula: `adj_numeric = base_numeric × member_weight`
- CONFIDENCE_NUMERIC mapping: HIGH=1.0, MEDIUM=0.6, LOW=0.3, NOISE=0.0
- Score-to-text back-conversion: ≥0.85→HIGH, ≥0.45→MEDIUM, ≥0.10→LOW, <0.10→NOISE
- MIN_SIGNAL_THRESHOLD=0.1 filter (NOISE-equivalent signals discarded before queue)
- 1yr OHLCV price history from Alpaca Data API for ticker + industry ETF + sector ETF
- UDP interrogation broadcast on port 5556, waits 30s for ACK on port 5557
- Signals marked VALIDATED or UNVALIDATED based on peer response
- ALL signals (regardless of decision) written to `news_feed` table
- Fire-and-forget POST to company Pi `/api/news-feed` if COMPANY_SUBSCRIPTION=true
- Price history fetched and held in memory only; not persisted to disk

**agent1_trader.py (Bolt) — Option B logic:**
- `MIN_SIGNAL_THRESHOLD` and `ALPACA_DATA_URL` added to config
- `fetch_price_history_5yr(ticker)` — 5yr daily OHLCV summary from Alpaca Data API
- Option B decision rules (deterministic, not Claude's judgment):
  - MIRROR: adjusted_score==HIGH AND no pulse warning AND no kill switch
  - WATCH: adjusted_score==MEDIUM/LOW OR pulse warning OR interrogation==UNVALIDATED
  - SKIP: (handled upstream by Scout's MIN_SIGNAL_THRESHOLD filter)
- Claude still called for reasoning/audit trail; decision is pre-determined by Option B
- 5yr price history included in Claude prompt; deleted from memory after call
- `exit_reason="PULSE_EXIT"` for cascade/urgent-flag-triggered exits (was PROTECTIVE_EXIT)
- Member weight updated after every `close_position()` call (PULSE_EXIT and PROFIT_TAKE)
- `entry_signal_score`, `entry_sentiment_score`, `interrogation_status` written to position at open
- `⚠️ UNDER REVIEW — live trading deployment not yet authorized` comments at all LIVE references

**portal.py:**
- `/news` page — all signals evaluated by Scout, auto-refresh every 60s
- `/api/news-feed` JSON endpoint — readable by monitoring agent and company Pi

**Architectural stabilization (also this session):**
- HEARTBEAT_RESOLUTION.md — formally deprecates heartbeat_receiver.py (company_node:5004)
- monitor_node:5000 (synthos_monitor.py) designated sole authoritative heartbeat receiver
- SYNTHOS_TECHNICAL_ARCHITECTURE.md — 7 corrections applied (zero HEARTBEAT_URL references remain)
- SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md — corrected node reference
- SYNTHOS_GROUND_TRUTH.md — INC-001 through INC-006 all marked RESOLVED
- SYSTEM_MANIFEST.md — HEARTBEAT_RESOLUTION.md and NEXT_BUILD_SEQUENCE.md registered
- NEXT_BUILD_SEQUENCE.md — created; ordered build sequence defined

---

## 3. CURRENT TODO ITEMS

### HIGH PRIORITY — Blocking

_None at this time._

### MEDIUM PRIORITY — Next Sprint

| ID | Item | File | Notes |
|----|------|------|-------|
| T-07 | Installer web UI needs auth + HTTPS | install_retail.py | Unprotected currently. Planned future release. |
| ~~T-08~~ | ~~Wire seed_backlog.py into company installer~~ | ~~boot_sequence.py~~ | **RESOLVED 2026-03-27** — step10_seed_suggestions() in boot_sequence.py; COMPANY_MODE guard |
| T-10 | first_run.sh hardcodes /home/pi/synthos | first_run.sh | Needs dynamic path per ADDENDUM 1 §1 rules. |
| T-15/T-16 | IP allowlisting activation | sentinel.py / config/allowed_ips.json | Deferred — needs stable IP inventory + SSH confirmed from all locations. |
| **BB-01** | ~~Document COMPANY_SUBSCRIPTION and MIN_SIGNAL_THRESHOLD in SYSTEM_MANIFEST~~ | ~~SYSTEM_MANIFEST.md~~ | **RESOLVED 2026-03-27** |
| ~~**BB-02**~~ | ~~Interrogation peer side not built~~ | ~~agent2_research.py~~ | **RESOLVED 2026-03-27** — interrogation_listener.py built; started by boot_sequence.py on retail node boot. |
| **BB-03** | Company-side news feed receiver | company agent | Scout POSTs to MONITOR_URL/api/news-feed but no company agent handles it. Endpoint doesn't exist yet. |
| **T-21** | Pi comparison log — cross-Pi behavioral analysis | synthos_monitor.py / Patches | Compare signal decisions, member weight divergence, portfolio performance, validation rates, and trade outcomes across all retail Pis. Surfaces which Pis are behaving differently and why. Feeds Patches morning digest. |
| **T-22** | RSS/news feed distribution system | agent3_sentiment.py / company DB / company node API | Company node parses `free_public_api_source_list.html` into `feed_sources` table. Retail Pi calls `GET /api/feed` for a random active URL. Each pull increments per-feed counter; stops distribution at threshold (web attack prevention). Cron at 00:01 resets counters daily. Existing file confirmed at `synthos_build/free_public_api_source_list.html` — also check GitHub for latest version. |

### LOW PRIORITY — Deferred

| ID | Item | Notes |
|----|------|-------|
| T-04 | Gmail SMTP activation via command portal | Placeholder. Toggle when credentials configured. |
| T-09 | Named Cloudflare tunnel with real domain | Currently temporary tunnel. |
| T-12 | License key validation at install time | Deferred to boot_sequence.py first boot. Acceptable. |
| T-17 | Direct Pi-to-Pi mutual TLS | No current use case. Future. |
| T-18 | Blueprint effort estimates marked TBD | patches.py / migrate_agents.py emit TBD effort tags. |
| T-19 | Hybrid cloud model at ~20–30 customers | Fidget will flag when warranted. No action now. |
| T-20 | Scoop transport toggle via command portal | Fixed transport path today. Future portal control. |

### RESOLVED — Do Not Reopen

| ID | Item | Resolved |
|----|------|---------|
| T-01 | TRADEABLE_PCT/IDLE_RESERVE_PCT flipped | 2026-03-27 |
| T-02 | BIL sweep (sync_bil_reserve) | 2026-03-27 |
| T-03 | BIL subtracted from cash math | 2026-03-27 |
| T-05 | Pulse pre-trade sentiment wired to Trader | 2026-03-27 |
| T-06 | install.py deprecated; install_retail.py canonical | 2026-03-27 |
| T-11 | restore.sh implemented | 2026-03-27 |
| T-13 | strongbox.py implemented (Agent 12) | 2026-03-27 |
| T-14 | Strongbox trigger — daily 2am cron (session-end deferred) | 2026-03-27 |
| INC-001 | Heartbeat receiver node conflict | 2026-03-27 |
| INC-002 | Company node absent from manifest | 2026-03-27 |
| INC-003 | suggestions.json undefined | 2026-03-27 |
| INC-005 | post_deploy_watch.json undefined | 2026-03-27 |
| INC-006 | settings.json protection list missing | 2026-03-27 |

---

## 4. WHAT IS NOT BUILT YET

These items were explicitly scoped out of the v1.2 build brief. Do not implement without explicit instruction.

| Item | Reason deferred |
|------|-----------------|
| Interrogation ACK receiver (peer corroboration side) | Scout broadcasts but no one listens. Signals will be UNVALIDATED. This is acceptable — Option B treats UNVALIDATED as WATCH, not SKIP. Build when second retail Pi available. |
| Company-side learning from news feed | The `/api/news-feed` POST from Scout has no consumer. Company agent to process and learn from signal history — future sprint. |
| Pulse changes | agent3_sentiment.py untouched in v1.2. No changes needed yet. |
| Live trading deployment | PAPER mode only. `⚠️ UNDER REVIEW` markers placed throughout Bolt. Project lead flips PAPER→LIVE only after extended paper validation. |
| ~~Strongbox → company installer wiring (T-08)~~ | ~~seed_backlog.py must be wired manually for now.~~ **RESOLVED 2026-03-27** |
| License key full validation | At install, key format checked only. Full HMAC+registry validation at first boot via boot_sequence.py. |

---

## 5. OPEN ARCHITECTURAL ISSUES

| ID | Issue | Severity | Status |
|----|-------|----------|--------|
| INC-004 | Retail utils/ directory — does it exist on disk? | LOW | Unresolved. Verify and either register or strike from arch doc. |
| INC-007 | TOOL_DEPENDENCY_ARCHITECTURE.md log paths hardcode /home/pi/ | LOW | Unresolved. Update to ${LOG_DIR}/. |
| INC-008 | INSTALLER_STATE_MACHINE.md hardcodes /home/pi/ paths | LOW | Unresolved. Update to ${SYNTHOS_HOME}/. |
| INC-009 | TOOL_DEPENDENCY_ARCHITECTURE.md missing company agent classifications | LOW | Unresolved. Add TDA classifications for company agents. |

---

## 6. DEPLOYMENT STATUS

### Paper Trading (retail_node)
| Condition | Status |
|-----------|--------|
| All three trading agents functional | ✓ |
| Portal running on port 5001 | ✓ |
| Supervised mode active (default) | ✓ |
| Kill switch wired | ✓ |
| Protective exits working | ✓ |
| BIL sweep active | ✓ |
| Member weight system | ✓ (v1.2) |
| Signal news feed | ✓ (v1.2) |
| Option B decision logic | ✓ (v1.2) |
| 5yr price history in Bolt | ✓ (v1.2) |
| Interrogation validation | ✓ interrogation_listener.py running on boot |
| Live trading | ⛔ NOT AUTHORIZED — paper only |

### Company Node
| Condition | Status |
|-----------|--------|
| Blueprint, Patches, Vault, Sentinel, Fidget, Scoop, Librarian, Timekeeper | ✓ Built |
| Strongbox (Agent 12) | ✓ Built |
| restore.sh | ✓ Built |
| Company Pi fast restore tested | ⚠ Not tested end-to-end |
| suggestions.json initialized | ⚠ Pending (NEXT_BUILD_SEQUENCE Step 4) |
| seed_backlog.py wired (T-08) | ✓ Auto-runs on company node boot via boot_sequence.py step 10 |
| /api/news-feed endpoint | ⚠ Not built — no consumer agent yet |

### Monitor Node
| Condition | Status |
|-----------|--------|
| synthos_monitor.py heartbeat receiver (port 5000) | ✓ Authoritative |
| heartbeat_receiver.py (port 5004) | ✗ DEPRECATED — do not use |
| Daily report storage | ✓ |
| SendGrid silence alerts | ✓ |

### Hardware (Phase 1 — Current)
| Device | Status |
|--------|--------|
| Pi 4B (company + monitor) | Active |
| Pi 2W (retail — dev/beta combined) | Active |
| Second Pi 2W (dedicated dev/beta separation) | ⚠ Phase 2 — not yet acquired |
| Additional Pi 2W units (production customers) | ⚠ Phase 2 — pending |

---

## 7. KEY CONSTRAINTS — READ BEFORE MAKING CHANGES

1. **PAPER_TRADE flag** — Project lead flips only. Cannot be changed by any agent autonomously.
2. **PAPER→LIVE transition** — Requires extended paper validation, not just code readiness.
3. **SYNTHOS_GROUND_TRUTH.md** — Changes require explicit Project Lead instruction.
4. **Blueprint Safety Contract** — All code changes from Blueprint must follow BLUEPRINT_SAFETY_CONTRACT.md exactly (atomic deploy, truncation guard, staging directory, etc.).
5. **No hardcoded /home/pi/** — All paths must be dynamic per ADDENDUM 1 §1. `BASE_DIR = Path(__file__).resolve().parent.parent`.
6. **Scoop is the only email sender** — No agent imports smtplib or SendGrid directly. All alerts go through scoop_trigger.json → Scoop.
7. **Approval queue** — In SUPERVISED mode, Bolt queues trades. Portal shows them. User approves. Bolt executes on next session. This flow must not be bypassed.
8. **company.db schema changes** — Must be done via migrations in `_run_migrations()` in database.py. Never raw ALTER outside that function.
9. **Interrogation peer side** — Do not build until second retail Pi is available or explicitly instructed.

---

## 8. ENVIRONMENT VARIABLES — COMPLETE REFERENCE

### Retail Pi (.env)

```
# Alpaca
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets

# Anthropic
ANTHROPIC_API_KEY=

# Monitor
MONITOR_URL=http://your-monitor-pi:5000
MONITOR_TOKEN=
PI_ID=synthos-pi-1
PI_LABEL=Customer Name
PI_EMAIL=customer@example.com

# Trading mode
TRADING_MODE=PAPER
OPERATING_MODE=SUPERVISED
AUTONOMOUS_UNLOCK_KEY=

# Signal filtering (v1.2)
MIN_SIGNAL_THRESHOLD=0.1
COMPANY_SUBSCRIPTION=false

# Trading parameters
MIN_CONFIDENCE=MEDIUM
MAX_STALENESS=Aging
MAX_POSITION_PCT=0.10
MAX_SECTOR_PCT=25
SPOUSAL_WEIGHT=reduced
CLOSE_SESSION_MODE=conservative

# Alerts
SENDGRID_API_KEY=
ALERT_FROM=
USER_EMAIL=

# Portal
PORTAL_PORT=5001
PORTAL_PASSWORD=
PORTAL_SECRET_KEY=

# License (retail only)
LICENSE_KEY=
```

### Company Pi (.env additions)
```
COMPANY_MODE=true
KEY_SIGNING_SECRET=
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=synthos-backups
BACKUP_ENCRYPTION_KEY=
```

---

## 9. NEXT ACTIONS (ORDERED)

These are the immediate next steps in priority order. Do not skip ahead.

| # | Action | Owner | Blocker |
|---|--------|-------|---------|
| 1 | Document BB-01: Add COMPANY_SUBSCRIPTION and MIN_SIGNAL_THRESHOLD to SYSTEM_MANIFEST.md env schema | Claude Code | None — do now |
| 2 | Commit all uncommitted v1.2 changes to git (8 modified files + 2 new files) | Claude Code | None — do now |
| 3 | Push to GitHub | Claude Code | None — do after commit |
| 4 | Initialize suggestions.json as empty array on company_node data/ | Project Lead | Needs company Pi access |
| 5 | Test Scout + Bolt pipeline end-to-end on paper | Project Lead | Needs retail Pi running |
| 6 | Resolve INC-004: verify retail utils/ directory exists or strike from arch docs | Claude Code | Needs `ls` on Pi |
| 7 | Build /api/news-feed consumer on company node (BB-03) | Claude Code | Needs explicit scope instruction |
| 8 | Build interrogation ACK receiver (BB-02) | Claude Code | Needs second Pi or explicit instruction to build stub |
| ~~9~~ | ~~Wire seed_backlog.py into company installer (T-08)~~ | ~~Claude Code~~ | **DONE** |
| 10 | Fix first_run.sh hardcoded path (T-10) | Claude Code | Low risk, small change |

---

## 10. MONITORING AGENT INSTRUCTIONS

If you are a monitoring agent reading this file, here is how to use it:

**To report project status to the project lead:**
1. Read this file (SYNTHOS_MASTER_STATUS.md) for current state
2. Check git log: `git log --oneline -10` for recent commits
3. Check for uncommitted changes: `git status --short`
4. Report: version, last commit date, next actions (#9 above), any open HIGH priority items
5. Flag if any item in Section 4 ("What Is Not Built Yet") appears to have been implemented without explicit instruction

**Signals that require immediate Project Lead attention:**
- Any commit that changes TRADING_MODE from PAPER to LIVE
- Any commit that removes `⚠️ UNDER REVIEW` comments from agent1_trader.py
- Any file in `data/` directory committed to git (credentials/trade history)
- A HIGH priority item appearing in the TODO list

**Weekly check:**
- Confirm this file was updated within the last 7 days
- Confirm no stale "in progress" items in Section 3 that have no recent commits
- Report paper trading performance summary if available from portal

---

## 11. REFERENCE DOCUMENTS

| Document | Purpose |
|----------|---------|
| SYNTHOS_GROUND_TRUTH.md | System extraction report — canonical facts about what exists |
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | Technical architecture, DB schema, network topology |
| SYNTHOS_OPERATIONS_SPEC.md | Weekly cadence, deployment pipeline, maturity gate |
| SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md | Dynamic paths, license keys, company Pi restore, encrypted comms |
| BLUEPRINT_SAFETY_CONTRACT.md | Non-negotiable deployment rules for Blueprint (engineer.py) |
| SYSTEM_MANIFEST.md | Production file registry, env schema, version tracking |
| SUGGESTIONS_JSON_SPEC.md | Accountability pipeline — how improvements are proposed and deployed |
| POST_DEPLOY_WATCH_SPEC.md | Post-deployment monitoring and rollback lifecycle |
| INSTALLER_STATE_MACHINE.md | Retail Pi installer state model |
| TOOL_DEPENDENCY_ARCHITECTURE.md | Tool classification and execution contracts |
| HEARTBEAT_RESOLUTION.md | Decision record — monitor_node is authoritative heartbeat receiver |
| NEXT_BUILD_SEQUENCE.md | Ordered build sequence post-architectural stabilization |

---

*This document should be updated every time work is completed. It is the contract between the project lead and all agents.*
