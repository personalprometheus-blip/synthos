# CONFLICT_LEDGER.md
## Synthos — Formal Conflict and Gap Register
**Generated:** 2026-03-29
**Severity scale:** CRITICAL (blocks validation or causes silent failures) | HIGH (causes incorrect behavior or doc divergence) | MEDIUM (inconsistency, not immediately dangerous) | LOW (cosmetic or future-only)

---

## CRITICAL CONFLICTS

### CL-001 — license_validator.py does not exist
- **Severity:** CRITICAL
- **Affected:** boot_sequence.py (calls it), TECHNICAL_ARCH §dependency_graph, OPERATIONS_SPEC, GROUND_TRUTH file registry
- **Description:** `license_validator.py` is documented as a SECURITY gate called at every boot by boot_sequence.py. It validates the license key on startup before agents are allowed to run. It is NOT PRESENT anywhere in the repository or on disk.
- **Operational impact:** Either (a) boot_sequence.py silently skips license validation because the file doesn't exist, or (b) boot fails at license step. In either case, the security contract stated in the architecture is not enforced.
- **Canonical resolution:** Determine if boot_sequence.py handles the missing file gracefully or crashes. If license validation is genuinely deferred (per v1.0 decision), strike the file from the architecture as a current active file and mark as "not yet implemented."
- **Blocks validation:** YES — validate_03b.py does not check for it; but Phase 01 pre-boot validation would fail.
- **Human confirmation needed:** YES — project lead must confirm whether license validation is currently active.

### CL-002 — suggestions.json vs company.db.suggestions: two implementations, both active
- **Severity:** CRITICAL
- **Affected:** vault.py, sentinel.py, librarian.py, watchdog.py (write to JSON); patches.py, blueprint.py, fidget.py (write via db_helpers to DB); SUGGESTIONS_JSON_SPEC.md (spec describes JSON)
- **Description:** The suggestions pipeline has been partially migrated from a JSON file to company.db.suggestions table. However, vault.py, sentinel.py, librarian.py, and watchdog.py still write directly to `suggestions.json` by reading/writing JSON. patches.py and blueprint.py write via `db_helpers.post_suggestion()` to the DB. migrate_agents.py exists to handle this migration but its application state is unknown.
- **Operational impact:** Suggestions from different agents go to different stores. Blueprint reads from one store; project lead portal reads from another. Critical alerts from sentinel/vault may never reach Blueprint.
- **Canonical resolution:** All agents must write via `db_helpers.post_suggestion()`. The JSON file must be retired. `migrate_agents.py` must be run or its changes manually applied to vault.py, sentinel.py, librarian.py, watchdog.py.
- **Blocks validation:** YES — any validation of the suggestion pipeline will give false results.
- **Human confirmation needed:** Confirm whether migrate_agents.py has been run.

### CL-003 — SYNTHOS_TECHNICAL_ARCHITECTURE.md DB schema is severely outdated
- **Severity:** CRITICAL
- **Affected:** TECHNICAL_ARCH v3.0 §2.3 (retail DB schema)
- **Description:** The architecture document shows a retail DB schema with tables: signals, positions, trades, agent_status, license, config. The actual database.py (v1.2) has: signals, positions, ledger, portfolio, system_log, heartbeats, outcomes, handshakes, scan_log, urgent_flags, pending_approvals, member_weights, news_feed. The tables `trades`, `agent_status`, `license`, `config` do not exist. Ten tables added in v1.1/v1.2 are completely absent from the architecture doc.
- **Operational impact:** Any developer reading the architecture doc to understand the schema will implement against the wrong schema. Any tool that validates schema against the doc will report false failures.
- **Canonical resolution:** Update TECHNICAL_ARCH §2.3 to reflect actual database.py schema. Source of truth: `database.py _create_tables()` and `_run_migrations()`.
- **Blocks validation:** YES — V-D01 through V-D06 in validation matrix.

### CL-004 — post_deploy_watch.json vs company.db.deploy_watches: split implementation
- **Severity:** CRITICAL
- **Affected:** watchdog.py (reads POST_DEPLOY_FILE JSON), patches.py (calls db.get_active_deploy_watches()), SUGGESTIONS_JSON_SPEC / POST_DEPLOY_WATCH_SPEC
- **Description:** watchdog.py reads post_deploy_watch.json at `COMPANY_DATA_DIR / "post_deploy_watch.json"`. patches.py uses `_db.get_active_deploy_watches()` (DB method). These two agents are reading from different stores for the same data.
- **Operational impact:** Blueprint writes deploy watches to DB (via db_helpers.post_deploy_watch()). watchdog.py will never see those watches because it reads from a JSON file that no agent writes to anymore.
- **Canonical resolution:** Migrate watchdog.py to use db_helpers. The JSON path in watchdog.py is dead code.
- **Blocks validation:** YES — post-deploy watch monitoring is silently broken.

### CL-005 — watchdog.py hardcodes /home/pi/synthos-company/data
- **Severity:** CRITICAL
- **Affected:** watchdog.py line 64: `COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")`
- **Description:** watchdog.py writes critical crash alerts and suggestions to a hardcoded absolute path. If deployed on a non-pi user system (different username), writes silently fail. This violates ADDENDUM 1 §1 (no hardcoded /home/pi/).
- **Operational impact:** On any non-standard deployment, crash alerts from watchdog never reach the company Pi. Silent failure.
- **Canonical resolution:** Replace with dynamic path. Use environment variable `COMPANY_DATA_DIR` or derive from a relative path anchor. Since watchdog runs on the retail Pi, it needs a configured path to the company node — this should be an env var.
- **Blocks validation:** YES — blocks path integrity check V-C02.

---

## HIGH CONFLICTS

### CL-006 — Sentinel reuses port 5004 (documented as deprecated heartbeat port)
- **Severity:** HIGH
- **Affected:** sentinel.py (PORT=5004), HEARTBEAT_RESOLUTION.md, TECHNICAL_ARCH v3.0 (iptables example still routes :5004 to company Pi)
- **Description:** HEARTBEAT_RESOLUTION.md formally deprecated port 5004 as "heartbeat_receiver.py — never built." However, sentinel.py actively uses port 5004 as its own Flask server. The TECHNICAL_ARCH iptables example (`-A OUTPUT -d <COMPANY_PI_IP> -p tcp --dport 5004 -j ACCEPT`) intended to block this port but now would block sentinel traffic.
- **Operational impact:** If iptables rules from the architecture doc are applied, sentinel becomes unreachable. Retail Pi cannot reach sentinel for IP validation queries.
- **Canonical resolution:** (a) Document sentinel's use of 5004 explicitly; (b) update the iptables example in TECHNICAL_ARCH to permit :5004 to company Pi with a note that it's sentinel (not heartbeat); (c) update HEARTBEAT_RESOLUTION.md footnote.

### CL-007 — strongbox.py is in synthos_build/ (retail repo) but is a company_node agent
- **Severity:** HIGH
- **Affected:** synthos_build/strongbox.py, MASTER_STATUS §agent_roster, TECHNICAL_ARCH company dir tree
- **Description:** Strongbox is documented as company_node Agent 12. It does not belong in the retail build. It is currently in synthos_build/ but should be in synthos-company/agents/. The company node does not have strongbox.py in its agents/ directory.
- **Operational impact:** (a) Strongbox is not running on the company Pi (it has no copy in agents/). (b) The retail repo incorrectly contains company-only code. (c) Any retail Pi git pull would receive strongbox.py unnecessarily.
- **Canonical resolution:** Move strongbox.py to synthos-company/agents/ and remove from synthos_build/. Verify it's not imported anywhere in retail code.

### CL-008 — Agent naming: three incompatible systems (informal / formal / file-based)
- **Severity:** HIGH
- **Affected:** All documentation, all source file docstrings, all agents on both nodes
- **Description:** Bolt/Scout/Pulse (informal), ExecutionAgent/DisclosureResearchAgent/MarketSentimentAgent (formal v4.0), agent1_trader/agent2_research/agent3_sentiment (file-based). MASTER_STATUS uses informal. TECHNICAL_ARCH and MANIFEST use formal. Source file headers use a mix.
- **Operational impact:** Documentation is confusing. Onboarding is ambiguous. Validators and log grep patterns may use any of the three systems. No authoritative guide to which name is canonical.
- **Canonical resolution:** Decide: formal names are for user-facing documentation; file names are for code references; informal aliases are deprecated and should be removed from new documentation (but can remain in source file docstrings as "also known as"). Update GROUND_TRUTH and MASTER_STATUS.

### CL-009 — install.py listed as active in SYSTEM_MANIFEST v4.0 FILE_STATUS
- **Severity:** HIGH
- **Affected:** SYSTEM_MANIFEST v4.0 §FILE_STATUS, GROUND_TRUTH §deprecated_files
- **Description:** SYSTEM_MANIFEST v4.0 still lists `install.py` in its file registry as an active file (the v4.0 agent renaming section refers to install.py implicitly). GROUND_TRUTH correctly marks it deprecated. The actual file does not exist — only `install_retail.py` exists.
- **Canonical resolution:** Update SYSTEM_MANIFEST FILE_STATUS to mark install.py as deprecated and reference install_retail.py as canonical.

### CL-010 — Version number fragmentation (1.1 / 1.2 / 3.0 / 4.0 coexist)
- **Severity:** HIGH
- **Affected:** GROUND_TRUTH (1.1), MASTER_STATUS (1.2), SYSTEM_MANIFEST (3.0/4.0), TECHNICAL_ARCH (3.0), OPERATIONS_SPEC (3.0)
- **Description:** There is no single authoritative version number for the system. Two different versioning schemes coexist — "feature version" (1.1, 1.2) and "document version" (3.0, 4.0) — without an explicit statement of their relationship.
- **Canonical resolution:** Define: feature version (1.x) tracks code capability milestones; manifest/doc version (3.x/4.x) tracks document revision cycles. State this explicitly in SYSTEM_MANIFEST. The new ground truth should declare one canonical version for each tier.

### CL-011 — update-staging git branch referenced but does not exist
- **Severity:** HIGH
- **Affected:** OPERATIONS_SPEC v3.0 §deployment_pipeline, MASTER_STATUS
- **Description:** The deployment pipeline mandates a `update-staging` branch for Blueprint to build against before Friday push. Only `main` branch exists in the repo (plus `remotes/origin/Main` case-variant).
- **Canonical resolution:** Either create the branch or update the operations spec to describe the actual workflow. `remotes/origin/Main` vs `remotes/origin/main` is also a case inconsistency that should be resolved.

### CL-012 — Company DB schema (company.db) not documented anywhere
- **Severity:** HIGH
- **Affected:** TECHNICAL_ARCH v3.0 (only documents retail DB), SYSTEM_MANIFEST v4.0
- **Description:** company.db has 14 tables: api_usage, audit_trail, backup_log, customers, deploy_watches, heartbeats, keys, schema_version, scoop_queue, silence_alerts, suggestions, token_ledger, work_requests, sqlite_sequence. None of these are described in any architecture document.
- **Canonical resolution:** Add company DB schema section to TECHNICAL_ARCH v3.0 (or a new COMPANY_DB_SCHEMA.md). This is needed before Blueprint can safely work against the company DB.

---

## MEDIUM CONFLICTS

### CL-013 — data/signals.db is empty; runtime DB is at synthos_build/signals.db (flat)
- **Severity:** MEDIUM
- **Affected:** architecture docs (say data/signals.db), validate_02.py (uses PROJECT_DIR/signals.db)
- **Description:** Arch docs show signals.db at `${SYNTHOS_HOME}/data/signals.db`. validate_02.py and runtime code use `os.path.join(PROJECT_DIR, "signals.db")` — flat, not in data/. The `data/signals.db` file exists but has zero tables.
- **Canonical resolution:** Decide canonical DB path and remove the stale empty file. During dev: flat path is convenient. For deployment: data/ path matches architecture. Make them consistent.

### CL-014 — data/company.db exists in retail build (wrong location)
- **Severity:** MEDIUM
- **Affected:** synthos_build/data/company.db
- **Description:** A 4KB company.db file exists in the retail build directory. It should not be there. .gitignore has `*.db` so it shouldn't be tracked, but it exists on disk.
- **Canonical resolution:** Delete synthos_build/data/company.db. Confirm it's not being used by anything in synthos_build/.

### CL-015 — seed_backlog.py is in retail build but seeds company pipeline
- **Severity:** MEDIUM
- **Affected:** synthos_build/seed_backlog.py, boot_sequence.py step 10
- **Description:** seed_backlog.py populates the company suggestions pipeline. It lives in synthos_build/ (retail repo). boot_sequence.py step 10 calls it on company node boot. This means the company Pi is running retail repo code to seed its own pipeline — the code lives in the wrong repo.
- **Canonical resolution:** seed_backlog.py should be in synthos-company/. boot_sequence.py should be updated to reference the correct path.

### CL-016 — blueprint.py loads .env; other company agents load company.env
- **Severity:** MEDIUM
- **Affected:** blueprint.py line: `load_dotenv(BASE_DIR / ".env")`, vs patches.py/vault.py/others: `load_dotenv(BASE_DIR / "company.env")`
- **Description:** Inconsistent env file loading across company agents. Blueprint will silently miss keys that are in company.env.
- **Canonical resolution:** All company agents should load the same env file. Standardize on `company.env`.

### CL-017 — SYNTHOS_ADDENDUM_2_WEB_ACCESS.md registered in manifest as active spec
- **Severity:** MEDIUM
- **Affected:** SYSTEM_MANIFEST v4.0 §documentation_registry
- **Description:** Web access layer is listed as a registered specification. It has zero implementation. A developer reading the manifest would assume it exists.
- **Canonical resolution:** Add status: "speculative — not yet implemented" to its manifest registration.

### CL-018 — config/priorities.json referenced in architecture but not on disk
- **Severity:** MEDIUM
- **Affected:** TECHNICAL_ARCH v3.0 company directory tree
- **Description:** The architecture shows `config/priorities.json` in the company Pi directory structure. The file does not exist. Only agent_policies.json, allowed_ips.json, market_calendar.json are present.
- **Canonical resolution:** Either create priorities.json or remove the reference from the architecture.

### CL-019 — heartbeat.py and synthos_heartbeat.py: confusingly similar names, different roles
- **Severity:** MEDIUM
- **Affected:** anyone reading the file tree; validate_03b.py checks for heartbeat.py
- **Description:** heartbeat.py writes heartbeat records to the local DB (called by agents). synthos_heartbeat.py POSTs session-end heartbeat to the monitor node. Different purposes, similar names, no spec doc distinguishing them.
- **Canonical resolution:** Documented distinction in a file header cross-reference or rename one. No rename action taken here — flagged as normalization candidate.

### CL-020 — allowed_ips.json (inbound) vs allowed_outbound_ips.json (outbound): naming collision
- **Severity:** MEDIUM
- **Affected:** synthos-company/config/allowed_ips.json, SYSTEM_MANIFEST v4.0 (references allowed_outbound_ips.json)
- **Description:** The company Pi has `allowed_ips.json` for inbound IP allowlisting. The retail Pi architecture references a separate `allowed_outbound_ips.json` for outbound feed restriction. The names are easily confused.
- **Canonical resolution:** No rename yet. Document explicitly: `allowed_ips.json` = company Pi inbound; `allowed_outbound_ips.json` = retail Pi outbound (not yet implemented).

---

## LOW CONFLICTS

### CL-021 — uninstall.py references /home/pi/quorum (legacy project name)
- **Severity:** LOW
- **Affected:** uninstall.py lines 39-46
- **Description:** Project had a prior name "quorum". uninstall.py still removes /home/pi/quorum path as legacy cleanup. Not dangerous, just historical noise.

### CL-022 — agent1_trader.py references .improvement_backlog.json and .audit_latest.json
- **Severity:** LOW
- **Affected:** agent1_trader.py lines 2112, 2119
- **Description:** References to legacy artifact files that no longer exist or are no longer part of the architecture.

### CL-023 — INC-007: TOOL_DEPENDENCY_ARCHITECTURE.md hardcodes /home/pi/ in log paths
- **Severity:** LOW
- **Affected:** TOOL_DEPENDENCY_ARCHITECTURE.md
- **Description:** Open since 2026-03-27.

### CL-024 — INC-008: INSTALLER_STATE_MACHINE.md hardcodes paths
- **Severity:** LOW
- **Affected:** INSTALLER_STATE_MACHINE.md
- **Description:** Open since 2026-03-27.

### CL-025 — INC-009: TDA missing company agent classifications
- **Severity:** LOW
- **Affected:** TOOL_DEPENDENCY_ARCHITECTURE.md
- **Description:** Company agents not classified. Open since 2026-03-27.

### CL-026 — remotes/origin/Main vs remotes/origin/main: case inconsistency
- **Severity:** LOW
- **Affected:** git remote refs
- **Description:** `remotes/origin/Main` and `remotes/origin/main` both exist. Standard convention is lowercase `main`.

---

## CONFLICT SUMMARY TABLE

| ID | Severity | Blocks Validation | Migration Required | Human Confirm |
|----|----------|------------------|--------------------|--------------|
| CL-001 | CRITICAL | YES | NO | YES |
| CL-002 | CRITICAL | YES | YES | YES |
| CL-003 | CRITICAL | YES | NO | NO |
| CL-004 | CRITICAL | YES | YES | NO |
| CL-005 | CRITICAL | YES | NO | NO |
| CL-006 | HIGH | NO | NO | NO |
| CL-007 | HIGH | NO | YES | NO |
| CL-008 | HIGH | NO | NO | NO |
| CL-009 | HIGH | NO | NO | NO |
| CL-010 | HIGH | NO | NO | NO |
| CL-011 | HIGH | YES | NO | YES |
| CL-012 | HIGH | YES | NO | NO |
| CL-013 | MEDIUM | NO | NO | NO |
| CL-014 | MEDIUM | NO | NO | NO |
| CL-015 | MEDIUM | NO | NO | NO |
| CL-016 | MEDIUM | YES | NO | NO |
| CL-017 | MEDIUM | NO | NO | NO |
| CL-018 | MEDIUM | NO | NO | NO |
| CL-019 | MEDIUM | NO | NO | NO |
| CL-020 | MEDIUM | NO | NO | NO |
| CL-021 | LOW | NO | NO | NO |
| CL-022 | LOW | NO | NO | NO |
| CL-023 | LOW | NO | NO | NO |
| CL-024 | LOW | NO | NO | NO |
| CL-025 | LOW | NO | NO | NO |
| CL-026 | LOW | NO | NO | NO |

**Count by severity:** CRITICAL: 5 | HIGH: 7 | MEDIUM: 8 | LOW: 6 | Total: 26
