# Synthos Documentation — Quick Reference Card
**Keep this open while working on Synthos.**

---

## 🚀 Start Here

| Need | Go To |
|------|-------|
| **System overview** | `synthos_build/README.md` |
| **Critical issues/conventions** | `synthos_build/CLAUDE.md` |
| **Current operational status** | `synthos_build/STATUS.md` |
| **Cross-project progress** | `synthos_build/PROJECT_STATUS.md` |
| **Complete architecture** | `synthos_build/docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md` |

---

## 📋 Find Anything Quickly

### By Task
| Task | Document |
|------|----------|
| Deploy code (Friday push) | `docs/governance/FRIDAY_PUSH_RUNBOOK.md` |
| Start/restart agents | `docs/governance/SYNTHOS_OPERATIONS_SPEC.md` |
| Check database schema | `docs/specs/DATABASE_SCHEMA_CANONICAL.md` |
| Review system architecture | `docs/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md` |
| Understand an agent behavior | `docs/specs/*_SPECIFICATION_v3.md` |
| Check what's broken | `docs/validation/SYSTEM_VALIDATION_REPORT.md` |
| Review decisions made | `docs/validation/CONFLICT_LEDGER.md` |

### By Node
| Node | Documents |
|------|-----------|
| **Pi 5 (retail)** | README.md, STATUS.md, SYSTEM_TOPOLOGY.md |
| **Pi 4B (company)** | synthos-company/README.md, SYSTEM_MANIFEST.md |
| **Pi 2W (monitor)** | PROJECT_STATUS.md (Addendum section) |

### By Topic
| Topic | Document |
|-------|----------|
| **Safety constraints** | `docs/governance/BLUEPRINT_SAFETY_CONTRACT.md` |
| **Boot-time integrity checks** | `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` |
| **Portal architecture** | `docs/specs/SYNTHOS_ADDENDUM_2_WEB_ACCESS.md` |
| **Installer design** | `docs/specs/SYNTHOS_INSTALLER_ARCHITECTURE.md` |
| **All resolved blockers** | `docs/validation/CONFLICT_LEDGER.md` |
| **Deferred work (Phase 6+)** | `docs/milestones.md` |

---

## 🔴 Common Issues & Fixes

| Problem | See This First |
|---------|---|
| "Unknown agent X" | Check `TOOL_DEPENDENCY_ARCHITECTURE.md` §Agent Classification |
| "Database connection failed" | Check `DATABASE_SCHEMA_CANONICAL.md` + verify `.env` on Pi |
| "Portal won't start" | Check `SYNTHOS_OPERATIONS_SPEC.md` §Portal Startup |
| "Validator reports conflict X" | Check `CONFLICT_LEDGER.md` for how it was resolved |
| "Why can't we trade live?" | Read `PROJECT_STATUS.md` §Phase 6 requirements |
| "License validator missing?" | See `CLAUDE.md` §Critical Known Issues #1 |

---

## 📊 Status Quick Check

| Status | File | Last Updated |
|--------|------|--------------|
| **Master tracker** | PROJECT_STATUS.md | 2026-04-05 |
| **Retail node** | STATUS.md | 2026-04 |
| **Company node** | synthos-company/STATUS.md | TBD |
| **Validation** | SYSTEM_VALIDATION_REPORT.md | 2026-04 |
| **Ground truth** | GROUND_TRUTH.md | 2026-03-29 |

---

## 🗂️ File Locations Cheat Sheet

```
synthos_build/
├── README.md                                      # Start here
├── CLAUDE.md                                      # Critical context
├── STATUS.md                                      # Operational health
├── PROJECT_STATUS.md                              # Master tracker
├── SYSTEM_TOPOLOGY.md                             # Node map
├── docs/
│   ├── GROUND_TRUTH.md                            # Authoritative definition
│   ├── milestones.md                              # Deferred work
│   ├── governance/
│   │   ├── BLUEPRINT_SAFETY_CONTRACT.md           # Agent constraints
│   │   ├── COMPANY_INTEGRITY_GATE_SPEC.md        # Boot checks
│   │   ├── SYNTHOS_OPERATIONS_SPEC.md             # Procedures
│   │   └── FRIDAY_PUSH_RUNBOOK.md                 # Deploy
│   ├── specs/
│   │   ├── SYNTHOS_TECHNICAL_ARCHITECTURE.md      # Full system
│   │   ├── DATABASE_SCHEMA_CANONICAL.md           # DB schemas
│   │   ├── SYSTEM_MANIFEST.md                     # Manifest
│   │   ├── SYNTHOS_INSTALLER_ARCHITECTURE.md      # Installer
│   │   └── *_SPECIFICATION_v3.md                  # Agent specs
│   └── validation/
│       ├── SYSTEM_VALIDATION_REPORT.md            # Blockers
│       ├── CONFLICT_LEDGER.md                     # Decisions
│       └── [many more validation reports]
└── archive/                                       # Historical
```

---

## ⚡ Common Commands

| Need | Command |
|------|---------|
| Check system status | SSH to Pi 4B, run `systemctl status synthos-*` |
| Deploy update | Run `FRIDAY_PUSH_RUNBOOK.md` steps on Mac |
| View portal | Open `app.synth-cloud.com` in browser |
| Monitor alerts | SSH to Pi 2W (monitor node), check logs |
| Check DB | SSH to Pi (any node), `sqlite3 signals.db ".schema"` |
| Read logs | `journalctl -u synthos-portal.service -n 50` |

---

## 🎯 What's Currently Happening (Phase 5)

- ✅ Deployment pipeline proven & working
- ⏳ Pi 5 hardware pending (will be retail_node)
- ⏳ Pi 5 build validation in progress
- 🔴 Phase 6 (Live Trading Gate) not started

**Next major deliverable:** Pi 5 comes online → run validation plan from PROJECT_STATUS.md

---

## 📚 Document Types & How to Use Them

| Type | Use When | Example |
|------|----------|---------|
| **SPECIFICATION_v3.md** | Understanding what an agent does | When reading agent code |
| **_REPORT.md** | Checking what was tested/validated | Before claiming something works |
| **_SPEC.md** | Implementing a feature | When building new infrastructure |
| **_VERIFICATION.md** | Confirming a fix works | After applying a fix |
| **RUNBOOK.md** | Executing a procedure | When deploying code |
| **.md (root level)** | Getting project context | Starting a new task |

---

## 🔗 Cross-Document Navigation

Key internal links that matter:

- README.md → links to STATUS.md + PROJECT_STATUS.md
- PROJECT_STATUS.md → references CLAUDE.md + all phase docs
- CLAUDE.md → points to critical issues in SYSTEM_VALIDATION_REPORT.md
- SYNTHOS_TECHNICAL_ARCHITECTURE.md → references DATABASE_SCHEMA_CANONICAL.md
- GROUND_TRUTH.md → authoritative source for any ambiguity
- CONFLICT_LEDGER.md → explains why architecture looks the way it does

---

## ✅ Maintenance Checklist

After any code change:
- [ ] Does SYNTHOS_TECHNICAL_ARCHITECTURE.md still match?
- [ ] Does STATUS.md need a "Last Updated" bump?
- [ ] Should PROJECT_STATUS.md phase progress change?
- [ ] Are there new blockers or resolved conflicts?

---

## 📞 Help Finding Something?

Use this index to find it:

1. **DOCUMENTATION_INDEX.md** — Complete catalog of all docs
2. **DOCUMENTATION_STATUS.md** — Status of each doc (current/stale/archive)
3. **This file** — Quick cheat sheet for common needs

---

## Legend

- ✅ Current / Up-to-date
- ⚠️ Needs attention / Has known stale content
- 🔴 Superseded / Do not use
- 📦 Archive / Historical reference only
- 🔲 Placeholder / Incomplete
- ⏳ Pending / Will be completed in next phase

---

**Last Updated:** 2026-04-15  
**Questions?** Check DOCUMENTATION_INDEX.md or DOCUMENTATION_STATUS.md for full details.
