# Synthos — Complete Documentation Index
**Last Updated:** 2026-04-15  
**Source of Truth:** Git repo + live system state

---

## Quick Navigation

- **[Core System](#core-system-documentation)** — README, STATUS, PROJECT_STATUS, CLAUDE
- **[Architecture & Technical](#architecture--technical-specs)** — Technical architecture, design, topology
- **[Agent Specifications](#agent-specifications)** — Individual agent specs (v1–v3)
- **[Governance & Operations](#governance--operations)** — Safety contracts, operations specs, deployment
- **[Database & Schema](#database--schema)** — Canonical schema, normalization
- **[Installation & Deployment](#installation--deployment)** — Installer architecture, deployment pipeline
- **[Validation & Testing](#validation--testing)** — Validation reports, blockers, conflict ledger
- **[Security](#security)** — Security hardening, trust gates, integrity
- **[API References](#api-references)** — Alpaca API, contract specs
- **[Milestones & Planning](#milestones--planning)** — Future roadmap, deferred work
- **[Archive](#archive)** — Historical and superseded documents

---

## Core System Documentation

### Main Repository Level
| File | Path | Purpose | Status |
|------|------|---------|--------|
| **README.md** | `synthos_build/README.md` | Project overview, node architecture, phase summary | ✅ Current |
| **CLAUDE.md** | `synthos_build/CLAUDE.md` | AI agent context, critical issues, conventions | ⚠️ Needs update (Pi 2W refs) |
| **STATUS.md** | `synthos_build/STATUS.md` | Retail node operational health | ✅ Current |
| **PROJECT_STATUS.md** | `synthos_build/PROJECT_STATUS.md` | Master cross-project tracker, phase overview | ✅ Current (as of 2026-04-05) |
| **SECURITY.md** | `synthos_build/SECURITY.md` | Security policies and guidelines | TBD |

### Company Repository Level
| File | Path | Purpose | Status |
|------|------|---------|--------|
| **README.md** | `synthos-company/README.md` | Company node overview | TBD |
| **STATUS.md** | `synthos-company/STATUS.md` | Company node operational health | TBD |
| **CLAUDE.md** | `synthos-company/CLAUDE.md` | Company node context for AI agents | ⚠️ Needs update |

---

## Architecture & Technical Specs

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **SYNTHOS_TECHNICAL_ARCHITECTURE.md** | `synthos_build/docs/specs/` | Complete system architecture (agents, DBs, connections) | ⚠️ Schema refs stale |
| **SYSTEM_TOPOLOGY.md** | `synthos_build/SYSTEM_TOPOLOGY.md` | Physical Pi nodes, networks, interfaces | ✅ Current |
| **SYNTHOS_OPERATIONS_SPEC.md** | `synthos_build/docs/governance/` | Operations procedures, startup sequence, monitoring | ✅ Current |
| **SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md** | `synthos_build/docs/governance/` | Operational addendum | ✅ Current |
| **TOOL_DEPENDENCY_ARCHITECTURE.md** | `synthos_build/docs/specs/` | Agent classification, dependencies, retired agents | ⚠️ login_server refs |
| **SYSTEM_MANIFEST.md** | `synthos_build/docs/specs/` | Core directories, services, entry points | ⚠️ Minor updates needed |
| **architecture.md** | `synthos_build/docs/architecture.md` | High-level architectural overview | TBD |

---

## Agent Specifications

### v3 Specifications (Current)
| Agent | File | Path | Status |
|-------|------|------|--------|
| **ExecutionAgent (Bolt)** | `EXECUTIONAGENT_SPECIFICATION_v3.md` | `synthos_build/docs/specs/` | ✅ Current |
| **ResearchAgent (Scout)** | `RESEARCHAGENTS_SPECIFICATION_v2.md` | `synthos_build/docs/specs/` | ✅ Current |
| **SentimentAgent (Pulse)** | `MARKETSENTIMENTAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **AuditingAgent** | `AUDITINGAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **BiasAgent** | `BIASAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **DisclosureResearchAgent** | `DISCLOSURERESEARCHAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **DispatcherAgent** | `DISPATCHERAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **PositioningFlowAgent** | `POSITIONINGFLOWAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **SocialRumorAgent** | `SOCIALRUMORAGENT_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |
| **MergedValidatorStack** | `MERGEDVALIDATORSTACK_SPECIFICATION_v3.md` | `synthos_build/` | ✅ Current |

### v2 Specifications (Archive)
| Agent | File | Path |
|-------|------|------|
| **ExecutionAgent** | `EXECUTIONAGENT_SPECIFICATION_v2.md` | `synthos_build/docs/specs/` |
| **ResearchAgents** | `RESEARCHAGENTS_SPECIFICATION_v2.md` | `synthos_build/docs/specs/` |

### v1 Specifications (Archive)
| Agent | File | Path |
|-------|------|------|
| **AuditingAgent** | `AUDITINGAGENT_SPECIFICATION_v1.md` | `synthos_build/docs/specs/` |
| **Agent API Contract** | `AGENT_API_CONTRACT_v1.md` | `synthos_build/` |

---

## Governance & Operations

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **BLUEPRINT_SAFETY_CONTRACT.md** | `synthos_build/docs/governance/` | Safety constraints for all agents, hard limits | ✅ Current |
| **BLUEPRINT_WORKFLOW_SPEC_ADDENDUM_1.md** | `synthos_build/docs/governance/` | Workflow safety addendum | ✅ Current |
| **COMPANY_INTEGRITY_GATE_SPEC.md** | `synthos_build/docs/governance/` | Company node boot-time integrity checks | ✅ Current |
| **SUGGESTIONS_JSON_SPEC.md** | `synthos_build/docs/governance/` | Suggestions pipeline format | 🔴 SUPERSEDED |
| **POST_DEPLOY_WATCH_SPEC.md** | `synthos_build/docs/governance/` | Post-deploy monitoring spec | 🔴 SUPERSEDED |
| **FRIDAY_PUSH_RUNBOOK.md** | `synthos_build/docs/governance/` | Weekly deployment procedure | ✅ Current (needs Pi 5 update) |
| **SYNTHOS_ADDENDUM_2_WEB_ACCESS.md** | `synthos_build/docs/specs/` | Web access and portal architecture | ✅ Current |

---

## Database & Schema

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **DATABASE_SCHEMA_CANONICAL.md** | `synthos_build/docs/specs/` | Authoritative schema: signals.db (v1.2) + company.db (v2.0) | ✅ Current |
| **DB_SCHEMA_NORMALIZATION_NOTE.md** | `synthos_build/docs/validation/` | Schema normalization process notes | ✅ Reference |

---

## Installation & Deployment

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **SYNTHOS_INSTALLER_ARCHITECTURE.md** | `synthos_build/docs/specs/` | Installer design, required keys, validation | ✅ Current |
| **INSTALLER_STATE_MACHINE.md** | `synthos_build/docs/specs/` | Installer state transitions and logic | ✅ Reference |
| **AGENT_ENHANCEMENT_PLAN.md** | `synthos_build/docs/specs/` | Planned agent improvements | 📋 Future work |
| **NEXT_BUILD_SEQUENCE.md** | `synthos_build/NEXT_BUILD_SEQUENCE.md` | Next phase build steps | Archive |

---

## Validation & Testing

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **SYSTEM_VALIDATION_REPORT.md** | `synthos_build/docs/validation/` | Master validation report, all critical blockers | ✅ Reference |
| **STATIC_VALIDATION_REPORT.md** | `synthos_build/docs/validation/` | Static code/config validation results | ✅ Reference |
| **VALIDATION_MATRIX.md** | `synthos_build/docs/specs/` | Testing matrix, coverage checklist | ✅ Reference |
| **CONFLICT_LEDGER.md** | `synthos_build/docs/validation/` | All 26 architectural conflicts logged and resolved | ✅ Complete |
| **BLOCKER_REFRESH_REPORT.md** | `synthos_build/docs/validation/` | Current blocker status | ✅ Reference |
| **POST_DEFERRAL_VALIDATION_REPORT.md** | `synthos_build/docs/validation/` | Validation after Phase 3 deferrals | ✅ Reference |
| **RETAIL_LICENSE_DEFERRAL_NOTE.md** | `synthos_build/docs/validation/` | License validation deferral rationale | ✅ Reference |
| **SYS-B03_REMEDIATION_NOTE.md** | `synthos_build/docs/validation/` | SYS-B03 fix notes | ✅ Reference |
| **SYS-B04_REMEDIATION_NOTE.md** | `synthos_build/docs/validation/` | SYS-B04 fix notes | ✅ Reference |

---

## Security

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **SECURITY.md** | `synthos_build/SECURITY.md` | Security policies | TBD |
| **api_security.md** | `synthos_build/api_security.md` | API security guidelines | TBD |
| **STRONGBOX_AUDIT.md** | `synthos_build/docs/validation/` | Vault/strongbox security audit | ✅ Reference |
| **STRONGBOX_WIRING_NOTE.md** | `synthos_build/docs/validation/` | Strongbox integration notes | ✅ Reference |
| **TRUST_GATE_ALIGNMENT_NOTE.md** | `synthos_build/docs/validation/` | Trust gate setup verification | ✅ Reference |

---

## API References

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **alpaca_api_reference.md** | `synthos_build/config/alpaca_api_reference.md` | Alpaca Trading API reference | ✅ Reference |

---

## Ground Truth & Definitions

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **GROUND_TRUTH.md** | `synthos_build/docs/GROUND_TRUTH.md` | Authoritative system definition as of Phase 4 | ✅ Current |
| **SYNTHOS_GROUND_TRUTH.md** | `synthos_build/docs/validation/` | Ground truth validation report | ✅ Reference |
| **GROUND_TRUTH_READINESS.md** | `synthos_build/docs/validation/` | Ground truth readiness checklist | ✅ Reference |

---

## Milestones & Planning

| File | Path | Purpose | Status |
|------|------|---------|--------|
| **milestones.md** | `synthos_build/docs/milestones.md` | Future roadmap, deferred work (license gate, etc) | ✅ Current |
| **REBASELINE_EXEC_SUMMARY.md** | `synthos_build/docs/validation/` | Phase 3 completion summary | ✅ Reference |
| **REPO_REALITY.md** | `synthos_build/docs/validation/` | Reality check against documented state | ✅ Reference |

---

## Archive

All files below are historical reference or superseded. Kept for context.

| File | Path | Status |
|------|------|--------|
| **synthos_design_brief.md** | `synthos_build/docs/specs/` | Original design brief (v1) | Archive |
| **synthos_design_brief (1).md** | `synthos_build/` | Design brief variant | Archive |
| **synthos_framing_v1.1.md** | `synthos_build/` | Project framing (v1.1) | Archive |
| **DISTRIBUTED_INTELLIGENCE_NETWORK_BRIEF.md** | `synthos_build/` | Historical multi-node brief | Archive |
| **HEARTBEAT_RESOLUTION.md** | `synthos_build/archive/` | Heartbeat architecture resolution | Archive |
| **MANIFEST_PATCH.md** | `synthos_build/archive/` | Manifest patch notes | Archive |
| **NEXT_BUILD_SEQUENCE.md** | `synthos_build/archive/` | Previous build sequence | Archive |
| **SYNTHOS_MASTER_STATUS.md** | `synthos_build/archive/` | Historical master status | Archive |
| **SYNTHOS_TODO_COMBINED.md** | `synthos_build/archive/` | Combined TODO (superseded by PROJECT_STATUS) | Archive |
| **SUGGESTIONS_JSON_SPEC.md** | `synthos_build/docs/governance/` | (Superseded by db_helpers.post_suggestion) | Archive |
| **POST_DEPLOY_WATCH_SPEC.md** | `synthos_build/docs/governance/` | (Superseded by db_helpers.get_active_deploy_watches) | Archive |

---

## Documentation by Category

### For New AI Agents or Developers Starting Here
1. **Start:** [README.md](synthos_build/README.md) (overview + node architecture)
2. **Then:** [CLAUDE.md](synthos_build/CLAUDE.md) (critical issues + conventions)
3. **Then:** [STATUS.md](synthos_build/STATUS.md) (current operational health)
4. **Then:** [PROJECT_STATUS.md](synthos_build/PROJECT_STATUS.md) (cross-project phase overview)
5. **Deep Dive:** [SYNTHOS_TECHNICAL_ARCHITECTURE.md](synthos_build/docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md) (complete system)

### For Operational / DevOps
- [SYNTHOS_OPERATIONS_SPEC.md](synthos_build/docs/governance/SYNTHOS_OPERATIONS_SPEC.md)
- [FRIDAY_PUSH_RUNBOOK.md](synthos_build/docs/governance/FRIDAY_PUSH_RUNBOOK.md)
- [DATABASE_SCHEMA_CANONICAL.md](synthos_build/docs/specs/DATABASE_SCHEMA_CANONICAL.md)
- [SYSTEM_MANIFEST.md](synthos_build/docs/specs/SYSTEM_MANIFEST.md)
- [SYSTEM_TOPOLOGY.md](synthos_build/SYSTEM_TOPOLOGY.md)

### For Security / Compliance
- [BLUEPRINT_SAFETY_CONTRACT.md](synthos_build/docs/governance/BLUEPRINT_SAFETY_CONTRACT.md)
- [COMPANY_INTEGRITY_GATE_SPEC.md](synthos_build/docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md)
- [SECURITY.md](synthos_build/SECURITY.md)
- [STRONGBOX_AUDIT.md](synthos_build/docs/validation/STRONGBOX_AUDIT.md)

### For Testing / Validation
- [SYSTEM_VALIDATION_REPORT.md](synthos_build/docs/validation/SYSTEM_VALIDATION_REPORT.md)
- [VALIDATION_MATRIX.md](synthos_build/docs/specs/VALIDATION_MATRIX.md)
- [CONFLICT_LEDGER.md](synthos_build/docs/validation/CONFLICT_LEDGER.md)

### For Agent Development
- **v3 Specs:** All `*_SPECIFICATION_v3.md` files in `synthos_build/`
- [AGENT_API_CONTRACT_v1.md](synthos_build/AGENT_API_CONTRACT_v1.md)
- [TOOL_DEPENDENCY_ARCHITECTURE.md](synthos_build/docs/specs/TOOL_DEPENDENCY_ARCHITECTURE.md)

---

## Current Documentation Gaps & Stale Content

### Priority 1 — Update before Pi 5 build
- [ ] CLAUDE.md (both repos) — References Pi 2W retail role, process_node, old phases
- [ ] SYNTHOS_TECHNICAL_ARCHITECTURE.md — Schema section (now in DATABASE_SCHEMA_CANONICAL.md)
- [ ] SYSTEM_MANIFEST.md — Minor install path updates

### Priority 2 — Update during/after Pi 5 build
- [ ] FRIDAY_PUSH_RUNBOOK.md — Update for Pi 5 as retail target
- [ ] DATABASE_SCHEMA_CANONICAL.md — Verify deployed schema matches
- [ ] TOOL_DEPENDENCY_ARCHITECTURE.md — Mark login_server agents as retired

### Priority 3 — Archive before Phase 6
- [ ] login_server/ directory in synthos-company
- [ ] Remove all references to synthos-process repo
- [ ] Archive any remaining SYNTHOS_TODO_* files

---

## File Organization

```
synthos/
├── synthos_build/
│   ├── README.md                                    # Main project README
│   ├── CLAUDE.md                                    # AI context
│   ├── STATUS.md                                    # Retail node status
│   ├── PROJECT_STATUS.md                            # Cross-project master tracker
│   ├── SECURITY.md                                  # Security policies
│   ├── SYSTEM_TOPOLOGY.md                           # Node architecture
│   ├── config/
│   │   └── alpaca_api_reference.md
│   ├── docs/
│   │   ├── architecture.md
│   │   ├── GROUND_TRUTH.md
│   │   ├── milestones.md
│   │   ├── governance/
│   │   │   ├── BLUEPRINT_SAFETY_CONTRACT.md
│   │   │   ├── BLUEPRINT_WORKFLOW_SPEC_ADDENDUM_1.md
│   │   │   ├── COMPANY_INTEGRITY_GATE_SPEC.md
│   │   │   ├── FRIDAY_PUSH_RUNBOOK.md
│   │   │   ├── SYNTHOS_OPERATIONS_SPEC.md
│   │   │   ├── SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md
│   │   │   ├── SUGGESTIONS_JSON_SPEC.md (SUPERSEDED)
│   │   │   └── POST_DEPLOY_WATCH_SPEC.md (SUPERSEDED)
│   │   ├── specs/
│   │   │   ├── AGENT_ENHANCEMENT_PLAN.md
│   │   │   ├── DATABASE_SCHEMA_CANONICAL.md
│   │   │   ├── EXECUTIONAGENT_SPECIFICATION_v2.md
│   │   │   ├── INSTALLER_STATE_MACHINE.md
│   │   │   ├── RESEARCHAGENTS_SPECIFICATION_v2.md
│   │   │   ├── SYNTHOS_ADDENDUM_2_WEB_ACCESS.md
│   │   │   ├── SYNTHOS_INSTALLER_ARCHITECTURE.md
│   │   │   ├── SYNTHOS_TECHNICAL_ARCHITECTURE.md
│   │   │   ├── SYSTEM_MANIFEST.md
│   │   │   ├── TOOL_DEPENDENCY_ARCHITECTURE.md
│   │   │   ├── VALIDATION_MATRIX.md
│   │   │   └── [agent v3 specs]
│   │   └── validation/
│   │       ├── BLOCKER_REFRESH_REPORT.md
│   │       ├── CONFLICT_LEDGER.md
│   │       ├── DOCUMENT_AUTHORITY_STACK.md
│   │       ├── FILE_NORMALIZATION_PLAN.md
│   │       ├── GROUND_TRUTH_READINESS.md
│   │       ├── POST_DEFERRAL_VALIDATION_REPORT.md
│   │       ├── REBASELINE_EXEC_SUMMARY.md
│   │       ├── REPO_REALITY.md
│   │       ├── RETAIL_LICENSE_DEFERRAL_NOTE.md
│   │       ├── RETAIL_LICENSE_DEFERRAL_VERIFICATION.md
│   │       ├── STATIC_VALIDATION_REPORT.md
│   │       ├── STRONGBOX_AUDIT.md
│   │       ├── STRONGBOX_WIRING_NOTE.md
│   │       ├── STRONGBOX_WIRING_VERIFICATION.md
│   │       ├── SYSTEM_VALIDATION_REPORT.md
│   │       ├── SYS-B03_REMEDIATION_NOTE.md
│   │       ├── SYS-B03_VERIFICATION.md
│   │       ├── SYS-B04_REMEDIATION_NOTE.md
│   │       ├── SYS-B04_VERIFICATION.md
│   │       ├── SYNTHOS_GROUND_TRUTH.md
│   │       ├── TRUST_GATE_ALIGNMENT_NOTE.md
│   │       ├── VALIDATION_MATRIX.md
│   │       └── [agent v3 specs]
│   ├── archive/
│   │   ├── HEARTBEAT_RESOLUTION.md
│   │   ├── MANIFEST_PATCH.md
│   │   ├── NEXT_BUILD_SEQUENCE.md
│   │   ├── SYNTHOS_MASTER_STATUS.md
│   │   └── SYNTHOS_TODO_COMBINED.md
│   ├── src/                                         # Source code
│   └── tests/                                       # Validation tests
│
├── synthos-company/
│   ├── README.md
│   ├── CLAUDE.md
│   ├── STATUS.md
│   └── [documentation/ or docs/ — mirrors main repo structure]
│
└── DOCUMENTATION_INDEX.md ← YOU ARE HERE
```

---

## How to Use This Index

- **Need to understand the system?** Start with [README.md](synthos_build/README.md) → [CLAUDE.md](synthos_build/CLAUDE.md) → [SYNTHOS_TECHNICAL_ARCHITECTURE.md](synthos_build/docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md)
- **Need to deploy or run a command?** See [SYNTHOS_OPERATIONS_SPEC.md](synthos_build/docs/governance/SYNTHOS_OPERATIONS_SPEC.md) and [FRIDAY_PUSH_RUNBOOK.md](synthos_build/docs/governance/FRIDAY_PUSH_RUNBOOK.md)
- **Need agent specs?** Find your agent in [Agent Specifications](#agent-specifications) section (use v3 unless otherwise noted)
- **Need to verify something?** See [Validation & Testing](#validation--testing)
- **Looking for something specific?** Use Ctrl+F to search this index

---

## Version Notes

This index was generated on **2026-04-15** from:
- Git history across synthos + synthos-company repos
- Current file system state in `/Users/patrickmcguire/synthos/`
- All markdown and documentation files discovered via recursive search

**Note:** Some documents may reference outdated Pi node designations (Pi 2W, Pi 3, etc). These will be updated during Pi 5 commissioning. Refer to [PROJECT_STATUS.md](synthos_build/PROJECT_STATUS.md) for the canonical node map.
