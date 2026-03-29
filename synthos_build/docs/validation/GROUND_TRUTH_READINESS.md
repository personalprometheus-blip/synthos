# GROUND_TRUTH_READINESS.md
## Synthos — Gate Conditions for Writing a New Ground Truth
**Generated:** 2026-03-29
**Purpose:** Define exactly what must be true before SYNTHOS_GROUND_TRUTH.md can be updated to v1.2 / declared current.

---

## CURRENT STATE

The existing SYNTHOS_GROUND_TRUTH.md captures system state as of 2026-03-27 (v1.1 baseline).
It is **not current**. The following have changed since it was written:
- v1.2 feature additions (member_weights, news_feed, interrogation, Option B, 5yr price history)
- SYSTEM_MANIFEST v4.0 agent renaming
- SYNTHOS_TECHNICAL_ARCHITECTURE v3.0 structural revisions
- validate_02.py and validate_03b.py added and repaired
- synthos_monitor.py added to retail repo
- patches.py bug fixes (dry-run, timezone, continuous mode)
- suggestions pipeline partial migration to DB

---

## SECTION 1: WHAT MUST BE RECONCILED FIRST

These conflicts make a ground truth premature. They must be resolved before writing one.

| Item | Why it Blocks Ground Truth | Conflict Ref |
|------|---------------------------|-------------|
| CL-001: license_validator.py status | Cannot declare a canonical security model while the documented security gate has unknown status | CL-001 |
| CL-002: suggestions dual-store | Ground truth must declare ONE canonical suggestions implementation; two implementations mean any declared truth is wrong for half the system | CL-002 |
| CL-003: DB schema drift | Ground truth contains a DB schema. Writing it now with the wrong schema makes the document wrong on day one | CL-003 |
| CL-004: post_deploy_watch dual-store | Same problem as CL-002 | CL-004 |
| CL-007: strongbox.py misplaced | Ground truth must correctly state which node runs each agent | CL-007 |

---

## SECTION 2: WHAT CAN REMAIN UNRESOLVED BUT DOCUMENTED

These items do not have to be fixed before writing the ground truth. They should be noted as open issues in the new ground truth document.

| Item | How to Handle in Ground Truth |
|------|------------------------------|
| CL-011: update-staging branch | Note as "pipeline intends update-staging; not yet created" |
| CL-013: signals.db path (flat vs data/) | Document both: dev=flat, deployed=data/ |
| CL-015: seed_backlog.py misplaced | Document current location; note as MEDIUM priority move |
| CL-016: blueprint.py env file | Note as open inconsistency; note conflict ID |
| CL-017: ADDENDUM_2 speculative | Register as "speculative — not yet implemented" |
| CL-020: allowed_ips.json naming | Document both files with explicit direction (inbound vs outbound) |
| CL-021 through CL-026 (LOW items) | List in open INC section |
| T-10: first_run.sh path | List as open T-10 |
| INC-007, INC-008, INC-009 | Carry forward as open |

---

## SECTION 3: UNACCEPTABLE AMBIGUITY (cannot leave in ground truth)

The new ground truth MUST definitively answer these questions. Any ambiguity here defeats the purpose of having a ground truth.

| Question | Why Unacceptable to Leave Ambiguous |
|----------|-------------------------------------|
| Is license_validator.py required, deferred, or absent? | It's cited as a security gate. "Unknown" is not an acceptable state for a security component. |
| Which suggestions store is canonical: JSON file or company.db table? | Every agent that writes suggestions is either correct or broken depending on this answer. |
| Which post_deploy_watch store is canonical? | Watchdog rollback may be silently broken. |
| What DB tables does the retail Pi actually have? | The ground truth schema section must match reality, not the architecture doc. |
| Is strongbox running on the company Pi? If so, from where? | Agent roster integrity. |
| What is the canonical version number for this release? | v1.2 or 3.0 — decide and document the relationship. |

---

## SECTION 4: CONFIRM FROM REPO, NOT DOCS

These facts must be extracted from the live system, not from documentation.

| Fact | How to Extract |
|------|---------------|
| Actual retail DB tables and columns | `PRAGMA table_info` on signals.db |
| Actual company DB tables and columns | `PRAGMA table_info` on company.db |
| Actual file list on retail Pi | `find ${SYNTHOS_HOME} -type f` |
| Actual running processes | `pgrep -a -f python3` |
| Which agents are writing to JSON vs DB | grep all company agents for SUGGESTIONS_FILE vs db_helpers |
| Whether license_validator.py is called and handles missing file | Read boot_sequence.py license step |
| Whether update-staging branch exists | `git branch -a` |
| Whether COMPANY_DATA_DIR is hardcoded | grep watchdog.py |

---

## SECTION 5: CONFIRM FROM DOCS, NOT REPO

These must come from authoritative documents, not inferred from code.

| Fact | Source Document |
|------|----------------|
| Deployment pipeline (Friday push, rollback rules) | OPERATIONS_SPEC v3.0 — do not infer from code |
| Protected files (must never be overwritten) | BLUEPRINT_SAFETY_CONTRACT.md |
| Paper/Live trading gate (who can flip it) | OPERATIONS_SPEC v3.0 + MASTER_STATUS |
| License key validation model | OPERATIONS_SPEC_ADDENDUM_1.md |
| Suggestion approval workflow | SUGGESTIONS_JSON_SPEC + db_helpers.py together |
| Post-deploy watch lifecycle | POST_DEPLOY_WATCH_SPEC + db_helpers.py together |

---

## SECTION 6: WHAT MUST BE EXCLUDED FROM NEW GROUND TRUTH (speculative)

These items MUST NOT appear as factual claims in the new ground truth.

| Item | Why to Exclude |
|------|---------------|
| Web access layer (ADDENDUM_2) | Not implemented; speculative |
| allowed_outbound_ips.json enforcement via iptables | Template exists in arch doc; not deployed anywhere |
| Interrogation peer ACK receiver (second retail Pi) | Explicitly deferred; does not exist |
| update-staging deployment branch | Does not exist |
| AGENT_ENHANCEMENT_PLAN v3.1 features | Planning only |
| EXECUTIONAGENT_SPECIFICATION_v2, RESEARCHAGENTS_SPECIFICATION_v2, AUDITINGAGENT_SPECIFICATION_v1 content | Not implemented |
| Phase 2 hardware (second Pi 2W, multiple customer Pis) | Not acquired |

---

## READINESS CHECKLIST

Before writing the new SYNTHOS_GROUND_TRUTH.md v1.2:

```
BLOCKING (must be done first):
  [ ] CL-001: Confirm license_validator.py status with project lead
  [ ] CL-002: Complete suggestions migration (all agents → db_helpers)
  [ ] CL-003: Update TECHNICAL_ARCH DB schema to match reality
  [ ] CL-004: Migrate watchdog.py to read deploy_watches from DB
  [ ] CL-007: Move strongbox.py to synthos-company/agents/; verify running

NON-BLOCKING (document as open issues):
  [ ] CL-011 through CL-026 noted as open INC items

EXTRACTIONS REQUIRED:
  [ ] Re-run `PRAGMA table_info` on both DBs
  [ ] Confirm all active processes
  [ ] Confirm suggestions store state on company Pi

PROHIBITED CONTENT:
  [ ] No web access layer facts
  [ ] No iptables facts (not deployed)
  [ ] No Phase 2 hardware facts
  [ ] No v3.1 spec facts
```

---

## ESTIMATED READINESS STATE: NOT READY

**Blocking items outstanding:** 5
**Items requiring human confirmation:** 2 (CL-001, CL-011)
**Estimated work before ready:** CL-002 and CL-004 require code changes (migrate agents to DB); CL-003 requires doc update; CL-007 requires file move.
