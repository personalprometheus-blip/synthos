# DOCUMENT_AUTHORITY_STACK.md
## Synthos — Document Authority Reconciliation
**Generated:** 2026-03-29
**Purpose:** Define which document is authoritative for each class of decision, with explicit conflict resolution rules.

---

## 1. AUTHORITY TIERS

### Tier 1 — Runtime Authority (what the code must match)
These documents govern runtime behavior. A conflict between code and Tier 1 doc is a code bug OR a doc that has fallen behind code.

| Document | Domain | Version | Staleness Risk |
|----------|--------|---------|----------------|
| SYSTEM_MANIFEST.md | File registry, node definitions, env schema, upgrade rules | v4.0 | MEDIUM — v1.2 schema additions not reflected |
| BLUEPRINT_SAFETY_CONTRACT.md | All code change procedures | — | LOW — stable |
| SUGGESTIONS_JSON_SPEC.md | Suggestion pipeline structure | — | HIGH — describes JSON file; actual impl is DB table |
| POST_DEPLOY_WATCH_SPEC.md | Post-deploy monitoring structure | — | HIGH — describes JSON file; actual impl is DB table |

### Tier 2 — Behavioral/Operational Authority
These documents govern how agents should behave. A conflict with code is ambiguous — could be code bug or stale doc.

| Document | Domain | Version | Staleness Risk |
|----------|--------|---------|----------------|
| SYNTHOS_OPERATIONS_SPEC.md | Weekly cadence, deployment pipeline, maturity gate | v3.0 | LOW-MEDIUM |
| SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md | Dynamic paths, license, company Pi restore | — | LOW |
| BLUEPRINT_WORKFLOW_SPEC_ADDENDUM_1.md | Blueprint execution procedure | — | LOW |
| TOOL_DEPENDENCY_ARCHITECTURE.md | Tool classification, execution contracts | — | MEDIUM — missing company agents |
| SYNTHOS_ADDENDUM_2_WEB_ACCESS.md | Web access layer | — | HIGH — speculative/not implemented |

### Tier 3 — Implementation Safety Authority
These documents constrain implementation choices. They must be consulted before any code changes.

| Document | Domain |
|----------|--------|
| BLUEPRINT_SAFETY_CONTRACT.md | Deployment rules, staging, truncation guard, rollback |
| SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md | Dynamic paths (§1), no hardcoded /home/pi/ |

### Tier 4 — Structural/Design Authority
These documents describe the system's architecture and schema. They are authoritative for what SHOULD exist, but must be validated against what DOES exist.

| Document | Domain | Version | Staleness Risk |
|----------|--------|---------|----------------|
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | Node topology, DB schema, dependency graph | v3.0 | CRITICAL — DB schema severely outdated |
| SYNTHOS_INSTALLER_ARCHITECTURE.md | Installer state model | — | LOW-MEDIUM |
| INSTALLER_STATE_MACHINE.md | Installer state transitions | — | LOW |

### Tier 5 — Historical/Audit Records
These documents record decisions and prior state. They are authoritative for WHY something is the way it is, but not for what is currently true.

| Document | Domain |
|----------|--------|
| SYNTHOS_GROUND_TRUTH.md | System extraction as of 2026-03-27 (v1.1 baseline) |
| HEARTBEAT_RESOLUTION.md | Decision record: heartbeat receiver deprecation |
| MANIFEST_PATCH.md | Patch list applied to SYSTEM_MANIFEST |
| NEXT_BUILD_SEQUENCE.md | Ordered build plan (partially complete) |
| synthos_design_brief.md | Pre-v1.0 origin document |

### Tier 6 — Planning/Speculative (not yet authoritative)
These documents describe intended future state. They must not be treated as current truth.

| Document | Domain | Status |
|----------|--------|--------|
| AGENT_ENHANCEMENT_PLAN.md | v3.1 retail agent changes | planning |
| AUDITINGAGENT_SPECIFICATION_v1.md | Future auditing agent | not implemented |
| EXECUTIONAGENT_SPECIFICATION_v2.md | Future execution agent v2 | not implemented |
| RESEARCHAGENTS_SPECIFICATION_v2.md | Future research agent v2 | not implemented |
| SYNTHOS_ADDENDUM_2_WEB_ACCESS.md | Cloud portal layer | not implemented |

### Tier 7 — Project Tracking (not architectural authority)
These documents track work state. They are useful for project management but not for determining system truth.

| Document |
|----------|
| SYNTHOS_MASTER_STATUS.md |
| SYNTHOS_TODO_COMBINED.md |

---

## 2. CONFLICT RESOLUTION RULES

When two documents conflict, apply these rules in order:

1. **Repo reality beats all docs** — if the code/files disagree with a document, investigate whether the doc is stale or the code is wrong. Do not silently accept the doc.

2. **Lower tier loses to higher tier** — Tier 4 (architecture) loses to Tier 1 (manifest) when they conflict about what files exist or what env vars are required.

3. **Newer version beats older** — SYSTEM_MANIFEST v4.0 beats GROUND_TRUTH (which captures v1.1 state) for current file registry.

4. **GROUND_TRUTH is NOT current truth** — Its name implies it is, but it documents v1.1 state (2026-03-27). Anything added in v1.2 is not in GROUND_TRUTH. Use it only for auditing pre-v1.2 decisions.

5. **When spec says JSON and code says DB** — the code is correct (migration has happened); the spec is stale. SUGGESTIONS_JSON_SPEC and POST_DEPLOY_WATCH_SPEC describe superseded implementations.

6. **When a Tier 6 doc is registered in Tier 1 (SYSTEM_MANIFEST)** — do not treat registration as implementation. SYNTHOS_ADDENDUM_2_WEB_ACCESS.md is registered in the manifest but has zero implementation. Note the gap; do not assume the feature exists.

---

## 3. RECOMMENDED CANONICAL AUTHORITY ORDER

For resolving any specific question, consult sources in this order:

| Question | Primary Source | Secondary Source | Warning |
|----------|---------------|-----------------|---------|
| Does file X exist? | `find` the repo | SYSTEM_MANIFEST v4.0 | Manifest may reference deprecated files |
| What are the env vars? | SYSTEM_MANIFEST v4.0 §ENV_SCHEMA | company.env / user/.env actual files | v1.2 additions not in manifest |
| What DB tables exist? | `PRAGMA table_info` on signals.db | database.py `_create_tables()` | TECHNICAL_ARCH schema is OUTDATED |
| What node runs what? | SYSTEM_MANIFEST v4.0 §NODE_DEFINITIONS | TECHNICAL_ARCH v3.0 | Generally consistent |
| What did the decision about X? | HEARTBEAT_RESOLUTION.md / GROUND_TRUTH | MASTER_STATUS | Check timestamp |
| What code changes are safe? | BLUEPRINT_SAFETY_CONTRACT.md | OPERATIONS_SPEC v3.0 | Non-negotiable |
| What is next build priority? | NEXT_BUILD_SEQUENCE.md | SYNTHOS_TODO_COMBINED.md | Partially stale |
| What does the suggestion pipeline look like? | db_helpers.py `post_suggestion()` | SUGGESTIONS_JSON_SPEC.md | Spec is superseded |

---

## 4. DOCUMENTS THAT MUST BE UPDATED BEFORE NEW GROUND TRUTH

| Document | Why |
|----------|-----|
| SYNTHOS_TECHNICAL_ARCHITECTURE.md | DB schema is severely outdated; v1.2 tables absent; sentinel port conflict unresolved |
| SYNTHOS_GROUND_TRUTH.md | Still shows system_version 1.1; v1.2 changes not reflected; some resolved INC still use old data |
| SUGGESTIONS_JSON_SPEC.md | Describes JSON file pipeline; actual implementation is DB-backed |
| POST_DEPLOY_WATCH_SPEC.md | Same — JSON spec, DB reality |
| SYSTEM_MANIFEST.md | install.py still listed as active; v1.2 env vars not fully reflected |

---

## 5. DOCUMENTS THAT SHOULD NOT BE MODIFIED

| Document | Reason |
|----------|--------|
| HEARTBEAT_RESOLUTION.md | Decision record; historical authority |
| MANIFEST_PATCH.md | Applied patch record; historical |
| BLUEPRINT_SAFETY_CONTRACT.md | Non-negotiable safety rules; must not drift |
| synthos_design_brief.md | Origin document; historical context only |
