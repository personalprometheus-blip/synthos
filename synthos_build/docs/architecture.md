# ARCHITECTURE

**Last Updated:** 2026-03-29
**Full spec:** docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md
**System manifest:** docs/specs/SYSTEM_MANIFEST.md

---

## Node Topology

```
retail_node (Pi 2W)          company_node (Pi 4B)        monitor_node (Pi 4B, same)
─────────────────────        ────────────────────        ──────────────────────────
agent1_trader.py             agents/blueprint.py         synthos_monitor.py
agent2_research.py           agents/sentinel.py          └─ port 5000
agent3_sentiment.py          agents/vault.py             └─ receives heartbeats
portal.py (port 5001)        agents/patches.py
watchdog.py                  agents/librarian.py
heartbeat.py → signals.db    agents/fidget.py
synthos_heartbeat.py ────────► MONITOR_URL:5000          scoop.py (email delivery)
interrogation_listener.py    agents/timekeeper.py        strongbox.py (MISPLACED —
  └─ UDP 5556/5557           db_helpers.py               should be here, not in src/)
                             company.db
```

---

## Data Stores

| Store | Location | Owner | Status |
|-------|----------|-------|--------|
| signals.db | src/ (dev flat) / data/ (deployed) | retail_node | Active |
| company.db | synthos-company/data/ | company_node | Active |
| suggestions (JSON) | synthos-company/data/suggestions.json | LEGACY | Split — being migrated |
| suggestions (DB) | company.db.suggestions | CANONICAL | Active |
| deploy_watches (JSON) | synthos-company/data/post_deploy_watch.json | LEGACY | Split — broken |
| deploy_watches (DB) | company.db.deploy_watches | CANONICAL | Active |

**CRITICAL:** suggestions and deploy_watch stores are split between JSON and DB. Four agents still write to JSON; blueprint reads DB only. See docs/validation/CONFLICT_LEDGER.md CL-002 and CL-004.

---

## Agent Roster

### Retail Node Agents (src/)
| File | Informal Name | Role | Schedule |
|------|--------------|------|----------|
| agent1_trader.py | Bolt / ExecutionAgent | Signal scoring, trade execution | Cron |
| agent2_research.py | Scout / ResearchAgent | Congressional disclosure research | Cron |
| agent3_sentiment.py | Pulse / SentimentAgent | Market sentiment scoring | Cron |

### Company Node Agents (synthos-company/agents/)
| File | Role | Schedule |
|------|------|----------|
| blueprint.py | Deploy approved suggestions via staging | Scheduled |
| sentinel.py | Receive retail Pi heartbeats (port 5004) | Persistent |
| vault.py | License key management | On-demand |
| patches.py | Continuous code auditing | Continuous |
| librarian.py | Documentation management | Scheduled |
| fidget.py | Feedback processing | Scheduled |
| scoop.py | Sole outbound email/comms sender | On-demand |
| timekeeper.py | DB slot management | Persistent |
| strongbox.py | **MISPLACED — in src/, should be here** | Scheduled |

---

## Boot Sequence (retail_node)

```
@reboot → boot_sequence.py
  1. Network connectivity
  2. .env keys present (ANTHROPIC_API_KEY, ALPACA_API_KEY, TRADING_MODE)
  3. Required agent files present
  4. signals.db integrity check
  5. health_check.py (NON-FATAL — never halts boot)
  6. Start watchdog.py (background)
  7. Start portal.py (background)
  8. Start synthos_monitor.py (background, skips if absent — should not be on retail)
  9. Start interrogation_listener.py (background, non-fatal)
  10. Initial data seed (agent2, only if signals table empty)
  11. Seed suggestions backlog (company mode only — should not be in retail boot)
```

**Known issues:** No license gate at step 2–3. Health check always passes regardless of result. Step 8 and 11 should not be in retail boot sequence.

---

## Update / Rollback Architecture

```
Normal patch: patch.py --file <file> --source <new_file>
  → Backs up signals.db
  → Validates .py syntax
  → Backs up current file to .patches/
  → Replaces file
  → Smoke test
  → Auto-rollback on failure

Known-good snapshot: watchdog.py --snapshot
  → Covers: agent1/2/3, database.py, heartbeat.py, cleanup.py
  → Does NOT cover: portal.py, watchdog.py, boot_sequence.py, patch.py

Post-deploy rollback (BROKEN — CL-004):
  → watchdog reads post_deploy_watch.json
  → blueprint writes to company.db.deploy_watches
  → These are never synchronized → trigger never fires
```

---

## Key Path Rules

- **No hardcoded `/home/pi/` paths** (SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md §1)
- All retail paths derive from `Path(__file__).resolve().parent`
- Company agents use `_HERE = Path(__file__).resolve().parent` or synthos_paths.py
- **EXCEPTION (violation):** `watchdog.py:64` hardcodes `COMPANY_DATA_DIR = Path("/home/pi/synthos-company/data")` — must be fixed

---

## Communications Policy

**Rule (ADDENDUM_1 §4):** scoop.py is the ONLY permitted outbound email sender.

**Violations:**
- `src/boot_sequence.py:125` — direct smtplib send (boot failure SMS)
- `src/health_check.py:111` — SendGrid fallback when Scoop enqueue fails
- `src/agent1_trader.py:260` — SendGrid fallback when Scoop fails

---

## Version Note

Feature version: v1.2 (code reality)
Doc version: v1.1 (architecture docs still reflect v1.1 schema)
This gap is CL-003. See docs/validation/CONFLICT_LEDGER.md.
