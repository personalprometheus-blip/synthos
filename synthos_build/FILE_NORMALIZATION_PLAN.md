# FILE_NORMALIZATION_PLAN.md
## Synthos — File and Naming Normalization Plan
**Generated:** 2026-03-29
**Rule:** No files are renamed or moved by this document. This is a plan only. Each item requires explicit instruction to execute.

---

## 1. DISPOSITION CATEGORIES

- **RENAME NOW** — High-impact inconsistency; rename as soon as possible
- **MOVE NOW** — File is in wrong location; move without changing content
- **ALIAS TEMPORARILY** — Keep old name working while new name is introduced
- **DEPRECATE** — Mark as deprecated; remove in next major cleanup
- **LEAVE AS-IS, DOCUMENT** — Inconsistency is acceptable; document the reason
- **DELETE** — File should be removed (stale artifact)

---

## 2. FILE MOVES

| File | From | To | Disposition | Reason |
|------|------|----|-------------|--------|
| strongbox.py | synthos_build/ | synthos-company/agents/ | MOVE NOW | Company_node agent; not retail code |
| seed_backlog.py | synthos_build/ | synthos-company/ | MOVE NOW | Seeds company pipeline; belongs with company node |
| synthos_monitor.py (copy) | synthos_build/ | Remove copy | DELETE | Authoritative copy is synthos-company/synthos_monitor.py; build copy adds confusion |

---

## 3. FILE DELETIONS (stale artifacts)

| File | Location | Disposition | Reason |
|------|----------|-------------|--------|
| data/company.db | synthos_build/data/ | DELETE | Company DB in retail build; stale 4KB file |
| data/signals.db | synthos_build/data/ | DELETE | Empty (no tables); runtime DB is at synthos_build/signals.db (flat) |

---

## 4. FILES TO CREATE

| File | Location | Disposition | Reason |
|------|----------|-------------|--------|
| license_validator.py | synthos_build/ | CREATE — needs implementation | Missing critical security gate (CL-001) |
| config/priorities.json | synthos-company/config/ | CREATE or REMOVE REFERENCE | Referenced in arch doc; does not exist |
| allowed_outbound_ips.json template | synthos_build/config/ or user/ | CREATE template | Referenced in SYSTEM_MANIFEST v4.0; no file |

---

## 5. FILE RENAMES

| Current Name | Proposed Name | Disposition | Reason |
|--------------|--------------|-------------|--------|
| heartbeat.py | db_heartbeat.py | RENAME NOW (low risk) | Distinguishes it from synthos_heartbeat.py (monitor POST); both named "heartbeat.*" causes confusion |
| synthos_heartbeat.py | monitor_heartbeat.py | RENAME NOW (paired with above) | Clarifies: POSTs to monitor node |

**Note on heartbeat rename:** validate_03b.py checks for `heartbeat.py` by name and imports `from heartbeat import write_heartbeat`. Any rename requires updating validate_03b.py, all agents that import heartbeat, and the file registry in SYSTEM_MANIFEST.

---

## 6. AGENT NAMING CONVENTION (canonical going forward)

Decision required. Three options:

**Option A — File-based only (recommended)**
- All documentation references files by filename: agent1_trader.py, agent2_research.py, agent3_sentiment.py
- Informal aliases (Bolt/Scout/Pulse) permitted in source file headers as "also known as"
- Formal names (ExecutionAgent etc.) used only in Tier 6 spec docs and future agent contracts
- Reason: Files don't change; names drift.

**Option B — Formal names canonical**
- SYSTEM_MANIFEST v4.0 already does this
- Requires updating MASTER_STATUS, GROUND_TRUTH, all source file headers
- Higher maintenance burden

**Option C — Keep all three (status quo)**
- Accepted confusion; works for experienced contributors
- Fails for onboarding; docs become unreliable

**Recommendation: Option A.** Declare this in the new GROUND_TRUTH. Retire all use of ExecutionAgent/DisclosureResearchAgent/MarketSentimentAgent from operational docs; retain in Tier 6 specs only.

---

## 7. DOCUMENT NAMING CONVENTION

### Current Issues

| Pattern | Examples | Problem |
|---------|----------|---------|
| ALL_CAPS_UNDERSCORES.md | SYSTEM_MANIFEST.md, CONFLICT_LEDGER.md | OK — consistent |
| ALL_CAPS_ADDENDUM_N.md | SYNTHOS_OPERATIONS_SPEC_ADDENDUM_1.md | Verbose; addendum number should be in the name |
| Version suffixes (_v1, _v2) | AUDITINGAGENT_SPECIFICATION_v1.md | Acceptable for planning docs; not for operational docs |
| SYNTHOS_ prefix | On most docs | Redundant within this repo |
| lowercase | synthos_design_brief.md | Inconsistent with convention |

### Normalization Rules (apply to new documents; existing docs renamed only on explicit instruction)

1. Operational docs: ALL_CAPS_UNDERSCORES.md — no SYNTHOS_ prefix required (already in the Synthos repo)
2. Addenda: {DOC_NAME}_ADDENDUM_{N}.md — keep existing pattern; do not change
3. Spec docs for unimplemented features: {SUBJECT}_SPECIFICATION_v{N}.md — acceptable for Tier 6
4. Project tracking docs: mixed case acceptable (MASTER_STATUS.md, etc.)
5. No version suffixes on Tier 1-4 docs — doc version tracked inside the file header

### Rename Candidates (on explicit instruction only)

| Current | Proposed | Disposition |
|---------|----------|-------------|
| synthos_design_brief.md | DESIGN_BRIEF.md | RENAME — inconsistent casing |
| MANIFEST_PATCH.md | MANIFEST_PATCH_APPLIED.md | LEAVE AS-IS — historical record; rename would lose git history clarity |

---

## 8. SCHEMA/ARTIFACT NAMING

| Artifact | Current State | Proposed Canonical |
|----------|--------------|-------------------|
| Suggestion pipeline store | `suggestions.json` AND `company.db.suggestions` table | `company.db.suggestions` only — retire JSON |
| Post-deploy watch store | `post_deploy_watch.json` AND `company.db.deploy_watches` table | `company.db.deploy_watches` only — retire JSON |
| Runtime DB | `signals.db` at PROJECT_DIR flat | `signals.db` — leave as-is; document that dev=flat, deployed=data/ |

---

## 9. ENV VARIABLE NAMING

| Current | Status | Proposed |
|---------|--------|---------|
| MONITOR_URL | canonical | keep |
| MONITOR_TOKEN | canonical | keep |
| HEARTBEAT_URL | deprecated (never shipped) | remove from all docs |
| HEARTBEAT_TOKEN | deprecated (never shipped) | remove from all docs |
| HEARTBEAT_PORT | in env_writer.py still | clarify: this is SENTINEL port 5004, not heartbeat receiver |
| COMPANY_DATA_DIR | not currently an env var (hardcoded) | INTRODUCE as env var to fix CL-005 |

---

## 10. MANIFEST REGISTRY NAMING

The SYSTEM_MANIFEST FILE_STATUS section should use these dispositions (standardized):

| Disposition | Meaning |
|-------------|---------|
| active | confirmed present and in use |
| active-undocumented | present and running but not in a spec |
| misplaced | present but in wrong location |
| deprecated | exists in docs, never built or retired |
| missing | referenced but not found |
| speculative | planned but not implemented |
| superseded | replaced by newer file/approach |
