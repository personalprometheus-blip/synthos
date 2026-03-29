# STRONGBOX WIRING NOTE

**Date:** 2026-03-29
**Type:** Change record
**Scope:** Minimal wiring fix — installer verification + nightly scheduling only

---

## Files Changed

| File | Change |
|------|--------|
| `synthos-company/install_company.py` | Added `strongbox.py` to `REQUIRED_AGENT_FILES`; added nightly cron entry in `register_cron()` |
| `synthos-company/CLAUDE.md` | Corrected stale strongbox reference (two lines) |

## What Was Added

**install_company.py — REQUIRED_AGENT_FILES:**
```python
"strongbox.py",
```
Installer verification phase will now confirm strongbox.py is present before declaring COMPLETE.

**install_company.py — register_cron():**
```
0 23 * * *   python3 .../agents/strongbox.py >> .../logs/strongbox.log 2>&1
```
Strongbox will be invoked nightly at 11:00 PM local time. Uses the identical pattern already used for all other company agents. No new scheduling mechanism introduced.

**CLAUDE.md:**
- Agent roster table: removed `(NEEDS MOVE from retail repo)` — file is correctly placed
- Known Open Issues: replaced the stale "must be moved" entry with an accurate status note

## What Was Intentionally Not Changed

- `strongbox.py` itself — no behavior, logic, or configuration modified
- Backup cycle, encryption, R2 upload, retention period — untouched
- No other sections of install_company.py modified
- No other lines of CLAUDE.md modified

## Explicit Statement

This pass wires strongbox into the installer and scheduler only. The backup behavior of strongbox.py — including its daily full backup model, Fernet encryption, Cloudflare R2 upload, and 30-day retention — was not modified and does not yet align with `docs/specs/BACKUP_STRATEGY_INITIAL.md`. That alignment is tracked as future work in `docs/milestones.md`.
