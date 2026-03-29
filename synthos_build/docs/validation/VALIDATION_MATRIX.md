# VALIDATION_MATRIX.md
## Synthos — Validation Path Before New Ground Truth
**Generated:** 2026-03-29
**Format:** Each item: ID | Category | Condition | How to Verify | Evidence | Blocker | Auto-verifiable?

---

## CATEGORY A — Documentation Integrity

| ID | Condition to Pass | How to Verify | Evidence File | Blocker | Claude Code? |
|----|------------------|---------------|---------------|---------|-------------|
| V-A01 | SYSTEM_MANIFEST v4.0 FILE_STATUS has no "active" entry for install.py | grep SYSTEM_MANIFEST.md for "install.py" + status | SYSTEM_MANIFEST.md | HIGH | YES |
| V-A02 | TECHNICAL_ARCH v3.0 contains zero active (non-deprecated) references to HEARTBEAT_URL | `grep HEARTBEAT_URL SYNTHOS_TECHNICAL_ARCHITECTURE.md` returns 0 results outside deprecation notice | SYNTHOS_TECHNICAL_ARCHITECTURE.md | HIGH | YES |
| V-A03 | TECHNICAL_ARCH v3.0 contains zero active references to heartbeat_receiver.py as a running service | grep for heartbeat_receiver in arch doc | SYNTHOS_TECHNICAL_ARCHITECTURE.md | HIGH | YES |
| V-A04 | TECHNICAL_ARCH DB schema matches actual database.py _create_tables() output | Diff TECHNICAL_ARCH table list against database.py | SYNTHOS_TECHNICAL_ARCHITECTURE.md + database.py | CRITICAL | YES |
| V-A05 | Company DB schema (14 tables) is documented somewhere | Search all .md files for api_usage, audit_trail, deploy_watches, scoop_queue | Any .md in synthos_build/ | HIGH | YES |
| V-A06 | SUGGESTIONS_JSON_SPEC.md is marked as superseded or updated to reflect DB implementation | Check header/status of SUGGESTIONS_JSON_SPEC.md | SUGGESTIONS_JSON_SPEC.md | HIGH | YES |
| V-A07 | POST_DEPLOY_WATCH_SPEC.md is marked as superseded or updated to reflect DB implementation | Check header/status of POST_DEPLOY_WATCH_SPEC.md | POST_DEPLOY_WATCH_SPEC.md | HIGH | YES |
| V-A08 | SYNTHOS_ADDENDUM_2_WEB_ACCESS.md is marked speculative/not-implemented in manifest | Check SYSTEM_MANIFEST documentation registry entry | SYSTEM_MANIFEST.md | MEDIUM | YES |
| V-A09 | GROUND_TRUTH is updated to system_version 1.2 or marked as v1.1 baseline (not current) | Check system_version field in GROUND_TRUTH | SYNTHOS_GROUND_TRUTH.md | HIGH | YES |
| V-A10 | Version number relationship (1.x feature vs 3.x/4.x doc) is explained in one document | grep for version relationship explanation | SYSTEM_MANIFEST.md or GROUND_TRUTH | MEDIUM | YES |

---

## CATEGORY B — File Registry Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-B01 | license_validator.py status is declared (missing/not-yet-built/deferred) in SYSTEM_MANIFEST | grep SYSTEM_MANIFEST for license_validator | SYSTEM_MANIFEST.md | CRITICAL | YES |
| V-B02 | strongbox.py is absent from synthos_build/ | `ls synthos_build/strongbox.py` returns not found | repo | HIGH | YES |
| V-B03 | strongbox.py is present in synthos-company/agents/ | `ls synthos-company/agents/strongbox.py` | synthos-company | HIGH | YES |
| V-B04 | synthos_monitor.py copy in synthos_build/ is intentional and documented, OR removed | Decision record in SYSTEM_MANIFEST or removal | SYSTEM_MANIFEST.md | MEDIUM | YES |
| V-B05 | All files listed as "active" in SYSTEM_MANIFEST v4.0 exist on disk | Diff SYSTEM_MANIFEST active list against `find` output | SYSTEM_MANIFEST.md | CRITICAL | YES |
| V-B06 | seed_backlog.py location is declared in SYSTEM_MANIFEST with correct node assignment | grep SYSTEM_MANIFEST for seed_backlog | SYSTEM_MANIFEST.md | MEDIUM | YES |
| V-B07 | synthos_test.py is either registered in SYSTEM_MANIFEST or labeled undocumented | Check manifest | SYSTEM_MANIFEST.md | LOW | YES |
| V-B08 | migrate_agents.py is either registered in SYSTEM_MANIFEST or labeled undocumented | Check manifest | SYSTEM_MANIFEST.md | LOW | YES |
| V-B09 | No file labeled "deprecated" in SYSTEM_MANIFEST exists as an active running process | Cross-ref FILE_STATUS with `pgrep` | SYSTEM_MANIFEST.md + ps output | HIGH | YES |
| V-B10 | install_retail.py is the only installer listed as active (install.py is deprecated) | Check SYSTEM_MANIFEST FILE_STATUS | SYSTEM_MANIFEST.md | HIGH | YES |

---

## CATEGORY C — Path Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-C01 | first_run.sh hardcoded path is fixed or T-10 is explicitly deferred with justification | grep first_run.sh for /home/pi | first_run.sh | MEDIUM | YES |
| V-C02 | watchdog.py does not hardcode /home/pi/synthos-company/data | grep watchdog.py for hardcoded company path | watchdog.py | CRITICAL | YES |
| V-C03 | All company agents (patches, blueprint, sentinel, vault, fidget, librarian, scoop, timekeeper) use dynamic path resolution | Check each agent for BASE_DIR or synthos_paths import | All company agent .py files | HIGH | YES |
| V-C04 | blueprint.py loads company.env (not .env) | grep blueprint.py for load_dotenv | blueprint.py | MEDIUM | YES |
| V-C05 | No functional /home/pi/ hardcoding exists in retail agents (comments/docstrings excluded) | grep retail .py files for /home/pi excluding comments | agent*.py, database.py, portal.py | HIGH | YES |
| V-C06 | The deployed `core/` path relationship to `synthos_build/` flat layout is documented | Check for explanation in SYSTEM_MANIFEST or TECHNICAL_ARCH | SYSTEM_MANIFEST.md | MEDIUM | YES — just needs doc |

---

## CATEGORY D — Architecture Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-D01 | Actual signals.db tables match what TECHNICAL_ARCH claims | Compare PRAGMA table_info output vs TECHNICAL_ARCH §2.3 | database.py + TECHNICAL_ARCH | CRITICAL | YES |
| V-D02 | Actual company.db tables are documented | Check for company DB schema in any .md | company.db + all .md files | HIGH | YES |
| V-D03 | Sentinel port 5004 usage is documented (not just heartbeat deprecation) | grep arch docs for sentinel + 5004 | TECHNICAL_ARCH + HEARTBEAT_RESOLUTION | HIGH | YES |
| V-D04 | Node topology (retail / company / monitor) is consistent across MANIFEST, TECHNICAL_ARCH, and OPERATIONS_SPEC | Cross-read all three node definition sections | Three docs | HIGH | YES |
| V-D05 | agent_status table is NOT referenced in any spec as a required table (it doesn't exist) | grep all docs for agent_status | all .md | MEDIUM | YES |
| V-D06 | trades table is NOT referenced as required (replaced by outcomes + ledger) | grep all docs for "trades table" | all .md | MEDIUM | YES |
| V-D07 | v1.2 features (member_weights, news_feed, interrogation) appear in at least one architecture doc | grep TECHNICAL_ARCH for member_weight | TECHNICAL_ARCH | HIGH | YES |

---

## CATEGORY E — Runtime/Tool Classification Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-E01 | TOOL_DEPENDENCY_ARCHITECTURE.md classifies all company agents | grep TDA for Blueprint, Vault, Sentinel, Fidget, Librarian, Scoop, Timekeeper, Strongbox | TOOL_DEPENDENCY_ARCHITECTURE.md | MEDIUM | YES |
| V-E02 | All agents listed as "Runtime" in manifest are confirmed running processes | `pgrep -a -f agent` vs manifest Runtime list | SYSTEM_MANIFEST.md + ps | HIGH | YES — partial |
| V-E03 | Scoop is the only agent with outbound email capability | grep all .py files for smtplib, sendgrid outside scoop.py | all agent .py files | HIGH | YES |

---

## CATEGORY F — Schema/Artifact Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-F01 | All company agents that write suggestions use db_helpers.post_suggestion() (none write to JSON directly) | grep vault.py, sentinel.py, librarian.py, watchdog.py for SUGGESTIONS_FILE or json.dump | agent .py files | CRITICAL | YES |
| V-F02 | watchdog.py reads post_deploy_watch from DB (not JSON file) | grep watchdog.py for POST_DEPLOY_FILE | watchdog.py | CRITICAL | YES |
| V-F03 | data/company.db is absent from synthos_build/data/ | `ls synthos_build/data/company.db` | repo | MEDIUM | YES |
| V-F04 | data/signals.db in synthos_build/data/ is absent or explained | `ls synthos_build/data/signals.db` + table count | repo | MEDIUM | YES |
| V-F05 | suggestions.json (seed file) is clearly labeled as seed/not-live in its directory or manifest | Check suggestions.json header or manifest entry | synthos_build/data/suggestions.json | MEDIUM | YES |
| V-F06 | pending_approvals table has all expected columns (validate_03b.py check) | Run validate_03b.py | signals.db | HIGH | YES |

---

## CATEGORY G — Deployment Model Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-G01 | update-staging branch exists OR OPERATIONS_SPEC documents actual deployment workflow | `git branch -a` + check OPERATIONS_SPEC | git + OPERATIONS_SPEC | HIGH | YES — partial |
| V-G02 | Friday push process is executable as documented (branch exists, approvals exist) | Manual review of deployment pipeline | OPERATIONS_SPEC v3.0 | HIGH | Human |
| V-G03 | Protected files (.env, signals.db, settings.json) are in .gitignore | cat .gitignore | .gitignore | HIGH | YES |
| V-G04 | PAPER_TRADE flag is confirmed set to PAPER in all .env files | grep .env files for TRADING_MODE | user/.env or company.env | CRITICAL | Human |

---

## CATEGORY H — Installer Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-H01 | install_retail.py uses no hardcoded /home/pi/ | grep install_retail.py for /home/pi | install_retail.py | HIGH | YES |
| V-H02 | install_company.py configures COMMAND_PORT, INSTALLER_PORT with documentation of what runs on them | Review env_writer.py port assignments | installers/common/env_writer.py | MEDIUM | YES |
| V-H03 | boot_sequence.py step 10 (seed_backlog) only runs when COMPANY_MODE=true | grep boot_sequence for COMPANY_MODE guard | boot_sequence.py | HIGH | YES |

---

## CATEGORY I — Update/Rollback Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-I01 | patch.py exists and implements .known_good/ snapshot logic | `ls patch.py` + read header | patch.py | HIGH | YES |
| V-I02 | watchdog.py rollback trigger reads from DB (not JSON) | V-F02 covers this | watchdog.py | CRITICAL | YES |
| V-I03 | BLUEPRINT_SAFETY_CONTRACT.md staging sequence is followable (staging dir exists or is created) | Check if .blueprint_staging/ is created by blueprint.py | blueprint.py | HIGH | YES |

---

## CATEGORY J — Repo Naming Integrity

| ID | Condition to Pass | How to Verify | Evidence | Blocker | Claude Code? |
|----|------------------|---------------|----------|---------|-------------|
| V-J01 | remotes/origin/Main and remotes/origin/main inconsistency is resolved | `git remote show origin` | git | LOW | YES |
| V-J02 | All .md files use consistent naming convention (no mixed case outliers) | `ls *.md` in synthos_build/ | repo | LOW | YES |
| V-J03 | No document references a renamed agent by its old informal alias as the primary name in a Tier 1-4 doc | grep all Tier 1-4 docs for "Bolt\|Scout\|Pulse" | SYSTEM_MANIFEST, TECHNICAL_ARCH, OPERATIONS_SPEC, GROUND_TRUTH | MEDIUM | YES |

---

## VALIDATION RUN ORDER

Run these in order. Later validations depend on earlier ones passing.

```
Phase 1 (can run now, no code changes needed):
  V-A02, V-A03        — heartbeat refs clean
  V-B05               — all manifest-active files exist
  V-C02               — watchdog hardcoded path
  V-D01               — DB schema match
  V-F01, V-F02        — suggestions/watch store consistency
  V-E03               — Scoop is only email sender

Phase 2 (after document updates):
  V-A01, V-A04-A10    — doc integrity
  V-B01-B10           — registry integrity
  V-C01, V-C03-C06    — path integrity
  V-D02-D07           — architecture integrity
  V-G01, V-G03        — deployment integrity
  V-H01-H03           — installer integrity

Phase 3 (human confirmation required):
  CL-001 (license_validator decision)
  V-G02, V-G04        — deployment + PAPER mode
  V-C04               — blueprint.py env file
```
