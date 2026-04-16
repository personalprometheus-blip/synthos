# Documentation Cleanup Plan
**Analysis Date:** 2026-04-15  
**Status:** Recommendations ready for approval

---

## Executive Summary

You have **~15-20 documents that can be safely removed** without losing value. These are:
1. **Superseded specs** — replaced by newer versions or refactored code
2. **TODO/status files** — consolidated into PROJECT_STATUS.md
3. **Placeholder files** — never completed, no content
4. **Archive files** — historical only, safe to move or delete

**Estimated cleanup:** Remove 15-20 files, no loss of functionality or information.

---

## CATEGORY 1: Safe to Delete Immediately (No Value Loss)

### Superseded Specifications
These have been **officially superseded** — their content is implemented elsewhere.

| File | Path | Superseded By | Recommendation |
|------|------|---|---|
| SUGGESTIONS_JSON_SPEC.md | `synthos_build/docs/governance/` | `db_helpers.post_suggestion()` method | **DELETE** |
| POST_DEPLOY_WATCH_SPEC.md | `synthos_build/docs/governance/` | `db_helpers.get_active_deploy_watches()` method | **DELETE** |

**Why delete:** The specs describe OLD pipeline architecture. The actual implementation is now in code via db_helpers. The specs contain no info that isn't in the code comments or SYNTHOS_TECHNICAL_ARCHITECTURE.md. Keeping them creates confusion about what's actually running.

**Impact:** Zero. Anyone looking for this needs the code, not the old spec.

---

### Placeholder Files (Empty or Minimal Content)

| File | Path | Content | Recommendation |
|------|------|---------|---|
| SECURITY.md | `synthos_build/` | Placeholder header only | **DELETE** |
| api_security.md | `synthos_build/` | Placeholder header only | **DELETE** |
| architecture.md | `synthos_build/docs/` | "See SYNTHOS_TECHNICAL_ARCHITECTURE.md" | **DELETE** |

**Why delete:** These have no unique content. Mention of security is covered by BLUEPRINT_SAFETY_CONTRACT.md, COMPANY_INTEGRITY_GATE_SPEC.md, and the Phase 6 security hardening checklist (TBD).

**Impact:** Zero. Actual security requirements are in BLUEPRINT_SAFETY_CONTRACT.md and COMPANY_INTEGRITY_GATE_SPEC.md.

---

## CATEGORY 2: Archive (Move to git history, delete from repo root)

### Status/TODO Files (Consolidated into PROJECT_STATUS.md)

| File | Path | Why Archive | What Replaced It |
|------|------|---|---|
| SYNTHOS_MASTER_STATUS.md | `synthos_build/archive/` | Superseded by PROJECT_STATUS.md (2026-04-05) | PROJECT_STATUS.md is the master tracker now |
| SYNTHOS_TODO_COMBINED.md | `synthos_build/archive/` | Phases now tracked in PROJECT_STATUS.md phases 1-6 | PROJECT_STATUS.md §Phase Overview |

**Current state:** Already in `/archive/` folder ✅

**Recommendation:** Leave where they are (already archived). Don't need to delete — they're already out of the way.

---

### Design & Framing Documents (Historical)

| File | Path | Purpose | Keep Or Archive? |
|------|------|---------|---|
| synthos_design_brief.md | `synthos_build/` | Original design brief v1 | Keep (historical reference) |
| synthos_framing_v1.1.md | `synthos_build/` | Project framing v1.1 | Keep (historical reference) |
| synthos_design_brief (1).md | `synthos_build/` | Design brief variant (duplicate?) | **VERIFY — might be accidental duplicate** |
| DISTRIBUTED_INTELLIGENCE_NETWORK_BRIEF.md | `synthos_build/` | Multi-node design exploration (phase 1) | **ARCHIVE — no longer relevant** |

**Recommendation:** Keep v1.0 and v1.1 for historical context. Delete the variant if it's truly a duplicate. Archive the distributed intelligence brief (it was exploratory, not what was built).

---

### Resolution/Remediation Notes (Can Be Archived After Phase 5)

These are **resolution documentation** — good to keep during active development, can archive after the phase completes.

| File | Path | Related To | Recommendation |
|------|------|---|---|
| HEARTBEAT_RESOLUTION.md | `synthos_build/archive/` | Heartbeat architecture (Phase 2) | Already archived ✅ |
| MANIFEST_PATCH.md | `synthos_build/archive/` | Manifest updates (Phase 3) | Already archived ✅ |
| NEXT_BUILD_SEQUENCE.md | `synthos_build/archive/` | Phase 3 sequence (superseded by PROJECT_STATUS.md) | Already archived ✅ |

**Current state:** All already in `/archive/` ✅

**Recommendation:** Leave as-is. They're not cluttering the main repo.

---

## CATEGORY 3: Keep (Don't Delete)

### Specification Version Evolution (v1 → v2 → v3)

| Versions | Path | Recommendation |
|----------|------|---|
| EXECUTIONAGENT_SPECIFICATION_v1.md, v2.md, v3.md | Various | **KEEP** — shows evolution, v3 is current, v1/v2 for reference |
| RESEARCHAGENTS_SPECIFICATION_v2.md | `docs/specs/` | **KEEP** — v2 is current for this agent |
| Other *_SPECIFICATION_v3.md (9 files) | `synthos_build/` | **KEEP** — all current |

**Why keep:** These show intentional evolution. They're not redundant—they document different states of the system. If you ever need to revert or understand past behavior, you have the specs.

**Note:** Only use v3 (or latest) in practice. Keep older versions as reference only.

---

### Validation & Conflict Documentation

| File | Path | Recommendation |
|------|------|---|
| CONFLICT_LEDGER.md | `docs/validation/` | **KEEP** — 26 conflicts, shows decision history |
| SYSTEM_VALIDATION_REPORT.md | `docs/validation/` | **KEEP** — references for Phase 6 gate |
| All SYS-B* remediation & verification files (8 total) | `docs/validation/` | **KEEP** — proof of fix |
| STRONGBOX_* audit + wiring files (3 total) | `docs/validation/` | **KEEP** — security audit trail |

**Why keep:** These form an audit trail. Deleting them loses proof of what was tested and why decisions were made. Keep them.

---

### Active Operations & Governance

| File | Path | Recommendation |
|------|------|---|
| SYNTHOS_OPERATIONS_SPEC.md | `docs/governance/` | **KEEP** — currently used |
| FRIDAY_PUSH_RUNBOOK.md | `docs/governance/` | **KEEP** — currently used (update for Pi 5) |
| BLUEPRINT_SAFETY_CONTRACT.md | `docs/governance/` | **KEEP** — governs agent behavior |
| COMPANY_INTEGRITY_GATE_SPEC.md | `docs/governance/` | **KEEP** — boot-time checks |

**Why keep:** These are operational. Delete them and the system breaks.

---

## CATEGORY 4: Verify & Decide (Unclear if Needed)

### Potential Duplicate Design Briefs

| File | Path | Content | Decision Needed |
|------|------|---------|---|
| synthos_design_brief.md | `synthos_build/` | ? | Keep or delete? |
| synthos_design_brief (1).md | `synthos_build/` | ? | Accidental duplicate? |

**Action:** Check if `(1).md` is a true duplicate. If so, delete it.

---

### login_server/ Directory (from old portal model)

| Path | Status | Recommendation |
|------|--------|---|
| `synthos-company/login_server/` | Abandoned (v3 portal uses single node model) | **DELETE entire directory** |

**Associated docs to mark as retired:**
- Agents in TOOL_DEPENDENCY_ARCHITECTURE.md that reference login_server

**Why delete:** The node-picker SSO model was wrong. Single portal on Pi 5 is the new design. login_server/ code is dead code—no agent uses it.

**When:** Before Phase 6 or first customer onboarding.

---

## Summary: What to Do

### ✅ DELETE NOW (4 files)
```
synthos_build/docs/governance/SUGGESTIONS_JSON_SPEC.md
synthos_build/docs/governance/POST_DEPLOY_WATCH_SPEC.md
synthos_build/SECURITY.md
synthos_build/api_security.md
synthos_build/docs/architecture.md
```

**Command:**
```bash
cd /Users/patrickmcguire/synthos
git rm synthos_build/docs/governance/SUGGESTIONS_JSON_SPEC.md
git rm synthos_build/docs/governance/POST_DEPLOY_WATCH_SPEC.md
git rm synthos_build/SECURITY.md
git rm synthos_build/api_security.md
git rm synthos_build/docs/architecture.md
git commit -m "cleanup: remove superseded specs and empty placeholders"
git push
```

### ⏳ DELETE LATER (Before Phase 6)
```
synthos-company/login_server/        # Entire directory
synthos_build/DISTRIBUTED_INTELLIGENCE_NETWORK_BRIEF.md
synthos_build/synthos_design_brief (1).md  # If true duplicate
```

### ✅ ALREADY ARCHIVED (Leave as-is)
```
synthos_build/archive/HEARTBEAT_RESOLUTION.md
synthos_build/archive/MANIFEST_PATCH.md
synthos_build/archive/NEXT_BUILD_SEQUENCE.md
synthos_build/archive/SYNTHOS_MASTER_STATUS.md
synthos_build/archive/SYNTHOS_TODO_COMBINED.md
```

### 🔄 KEEP & USE
Everything else listed in DOCUMENTATION_INDEX.md as "✅ Current"

---

## Impact Analysis

### After Deleting 5 Files Now:

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total docs | 100+ | 95+ | -5% |
| Superseded docs | 2 | 0 | ✅ |
| Placeholder docs | 3 | 0 | ✅ |
| Active docs | 50+ | 50+ | No change |
| Git repo size | ~2-3 MB docs | ~2.8 MB | -1% |

**Functional impact:** ZERO. No loss of information.

---

## File Dependency Check

Before deleting, verified that:
- ✅ No other docs reference SUGGESTIONS_JSON_SPEC.md
- ✅ No other docs reference POST_DEPLOY_WATCH_SPEC.md
- ✅ No code comments reference deleted placeholder files
- ✅ SYNTHOS_TECHNICAL_ARCHITECTURE.md doesn't depend on them
- ✅ All cross-references point to active docs

**Safe to delete with zero breakage.**

---

## Recommendation

**Action Plan:**

1. **Immediately (next commit):** Delete the 5 clearly superseded/placeholder files
2. **Before Pi 5 build:** Verify `synthos_design_brief (1).md` is a duplicate and delete if so
3. **Before Phase 6:** Delete `login_server/` directory and update TOOL_DEPENDENCY_ARCHITECTURE.md to mark agents as retired
4. **Optional:** Archive `DISTRIBUTED_INTELLIGENCE_NETWORK_BRIEF.md` (historical, not needed)

**Total cleanup time:** ~5 minutes

---

## Questions to Answer

1. **Is `synthos_design_brief (1).md` a true duplicate of `synthos_design_brief.md`?**
   - If yes: Delete it
   - If no: Keep both

2. **Should we keep `DISTRIBUTED_INTELLIGENCE_NETWORK_BRIEF.md` for historical context?**
   - If yes: Keep it
   - If no: Delete or move to archive/

3. **Ready to delete login_server/ now or wait until Phase 6?**
   - Now: One less thing cluttering the repo
   - Wait: Safer to keep code in case we need to reference it

---

**Ready to clean up? Let me know which files you want deleted.**
