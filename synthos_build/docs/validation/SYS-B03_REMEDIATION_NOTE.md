# SYS-B03 REMEDIATION NOTE

**Blocker:** SYS-B03 — Post-deploy rollback trigger broken
**Date:** 2026-03-29
**Status:** RESOLVED

---

## Problem

`blueprint.py` writes deploy watches to `company.db.deploy_watches` via `db_helpers.post_deploy_watch()`.
`watchdog.py` read deploy watches from `post_deploy_watch.json` (file-backed store).
These stores were never synchronized. The automated rollback trigger could never fire.

Prior state (before this session's prior work): JSON-only read path.
State at start of this remediation: DB was primary, JSON was still a live fallback.
The fallback preserved the split — if `_db` was unavailable for any reason, watchdog
would silently revert to the stale JSON store and evaluate stale or absent watches.

---

## Files Changed

| File | Repo | Change |
|------|------|--------|
| `src/watchdog.py` | synthos (retail) | Removed JSON fallback from `check_post_deploy_rollback()`; `company.db.deploy_watches` is now sole authoritative source |

---

## Old Trigger Path

```
blueprint.py → db_helpers.post_deploy_watch() → company.db.deploy_watches
watchdog.py  → post_deploy_watch.json (STALE / NEVER WRITTEN BY BLUEPRINT)
```
Trigger never fires.

---

## New Trigger Path

```
blueprint.py → db_helpers.post_deploy_watch() → company.db.deploy_watches
watchdog.py  → db_helpers.get_active_deploy_watches() → company.db.deploy_watches
```
Blueprint and watchdog now share the same store.

---

## Legacy JSON Watch Logic

`POST_DEPLOY_FILE` variable removed from `watchdog.py` (replaced with explanatory comment).
The JSON fallback `elif POST_DEPLOY_FILE.exists(): watches = json.loads(...)` has been removed.

If `_db` is unavailable (separate-Pi deployment, db_helpers not on path), `check_post_deploy_rollback()`
now logs a warning and returns `False` — it does NOT fall back to reading `post_deploy_watch.json`.
This is the correct behavior: a stale JSON file is worse than a silent skip.

The `post_deploy_watch.json` file may still exist on disk from prior runs. It is gitignored
and no agent writes to it. It can be deleted safely.

---

## Helper Added or Changed

No new helper added. `db_helpers.get_active_deploy_watches()` already existed:

```python
def get_active_deploy_watches(self) -> list[dict]:
    # Returns rows from deploy_watches WHERE status = 'active'
    # and expires_at > now (or expires_at IS NULL)
```

---

## What Still Needs Runtime Verification

- Confirm `blueprint.py` calls `db_helpers.post_deploy_watch()` after a deploy completes (expected: it does — this was the canonical write path before this fix)
- Confirm `company.db.deploy_watches` schema fields match what `check_post_deploy_rollback()` reads: `deployed_at`, `watch_duration_hours`, `rollback_trigger`
- Confirm rollback trigger evaluates correctly on a test watch row (cannot verify statically — requires a post-trading mode deploy cycle)
- Rollback trigger only fires in post-trading mode (`is_post_trading()` check at line 573) — this is intentional and unchanged
