# SYS-B03 VERIFICATION

**Date:** 2026-03-29
**Verifier:** Static code inspection

---

## SYS-B03 STATUS

**RESOLVED**

---

## FILES CHECKED

| File | Repo |
|------|------|
| `src/watchdog.py` | synthos (retail) |
| `utils/db_helpers.py` | synthos-company |
| `agents/blueprint.py` | synthos-company |

---

## LEGACY JSON WATCH PATH REMOVED OR DISABLED

| Item | Status |
|------|--------|
| `POST_DEPLOY_FILE` variable | Removed — replaced with explanatory comment at line 65 |
| JSON fallback read (`elif POST_DEPLOY_FILE.exists()`) | Removed from `check_post_deploy_rollback()` |
| `post_deploy_watch.json` on disk | May still exist from prior runs; gitignored; no agent writes to it |

Only remaining reference to `post_deploy_watch` in watchdog.py is the comment at line 65.

---

## DB WATCH PATH CONFIRMED

| Component | Call | Location |
|-----------|------|----------|
| Read | `_db.get_active_deploy_watches()` | `watchdog.py:581` |
| Write | `db.post_deploy_watch(...)` | `blueprint.py:605` |
| Read helper | `DB.get_active_deploy_watches()` | `db_helpers.py:642` |
| Write helper | `DB.post_deploy_watch()` | `db_helpers.py:614` |

Both blueprint (writer) and watchdog (reader) now reference `company.db.deploy_watches`.

---

## ROLLBACK TRIGGER DATA SOURCE

```
blueprint.py:605  → db_helpers.post_deploy_watch() → company.db.deploy_watches
watchdog.py:581   → db_helpers.get_active_deploy_watches() ← company.db.deploy_watches
```

The split is closed. Blueprint writes and watchdog reads from the same store.

If `_db` is unavailable at watchdog runtime, `check_post_deploy_rollback()` logs a warning
and returns `False`. No JSON fallback. No silent split.

---

## REMAINING RISKS

| Risk | Severity | Notes |
|------|----------|-------|
| Rollback trigger only fires in post-trading mode | INFO | Intentional — `is_post_trading()` guard unchanged. No risk. |
| `_db` unavailable on separate-Pi deployment | LOW | Trigger silently skips with warning log. Acceptable — stale JSON was worse. |
| Schema field alignment not runtime-tested | LOW | `deployed_at`, `watch_duration_hours`, `rollback_trigger` expected in `deploy_watches` rows — not verified against live DB. Spot-check recommended. |
| `post_deploy_watch.json` stale file on disk | INFO | Harmless. Delete manually if desired. |

---

## NEXT BLOCKER RECOMMENDATION

SYS-B03 is resolved. Remaining active blockers from the normalization sprint:

**SYS-B01 / SYS-B02** — license_validator.py missing; no boot-time license gate
- These require a **human decision** (Step 6): build license_validator.py now or formally defer
- No code work can proceed on these without project lead direction

**Normalization sprint code work is complete** (Steps 1–4 done). Remaining steps are:
- Step 5: Document company.db schema (PRAGMA table_info) — doc work
- Step 6: Human decision on license_validator.py
