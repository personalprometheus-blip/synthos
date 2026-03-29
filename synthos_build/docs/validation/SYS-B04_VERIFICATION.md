# SYS-B04 VERIFICATION

**Date:** 2026-03-29
**Verifier:** Static code inspection

---

## SYS-B04 STATUS

**RESOLVED**

---

## FILES CHECKED

| File | Repo |
|------|------|
| `agents/vault.py` | synthos-company |
| `agents/sentinel.py` | synthos-company |
| `agents/librarian.py` | synthos-company |
| `src/watchdog.py` | synthos (retail) |

---

## DIRECT JSON WRITE PATHS REMOVED

| Agent | Old write path | Status |
|-------|---------------|--------|
| vault.py | `SUGGESTIONS_FILE` (from synthos_paths) | Dead import removed |
| sentinel.py | `SUGGESTIONS_FILE` (from synthos_paths) | Dead import removed |
| librarian.py | `SUGGESTIONS_FILE` (from synthos_paths) | Dead import removed |
| watchdog.py | `SUGGESTIONS_FILE = COMPANY_DATA_DIR / "suggestions.json"` | Variable removed; replaced with comment |

No authoritative writes to `suggestions.json` remain in any of the four agents.
The only remaining `suggestions.json` reference in watchdog.py is a comment
marking where the variable was removed.

---

## DB HELPER PATHS ADDED

| Agent | DB write call | Slot priority |
|-------|--------------|--------------|
| vault.py:749–758 | `_db.slot("Vault", "post_suggestion", priority=5)` → `_db.post_suggestion(...)` | 5 |
| sentinel.py:318–328 | `_db.slot("Sentinel", "post_suggestion", priority=4)` → `_db.post_suggestion(...)` | 4 |
| librarian.py:587–597 | `_db.slot("Librarian", "post_suggestion", priority=3)` → `_db.post_suggestion(...)` | 3 |
| watchdog.py:652–662 | `_db.slot("Watchdog", "post_suggestion", priority=10)` → `_db.post_suggestion(...)` | 10 |

All four use `_db.slot()` for write coordination with timekeeper.

**watchdog.py fallback:** if `db_helpers` is not importable (separate-Pi deployment),
`alert_company_pi()` logs locally and returns. It does NOT fall back to writing
`suggestions.json`. The fallback is log-only.

---

## PIPELINE CONFIRMATION

- `blueprint.py` reads from `company.db.suggestions` — this was already the canonical read path
- All four agents now write to `company.db.suggestions` via `db_helpers.post_suggestion()`
- The split is closed: blueprint can now see suggestions from vault, sentinel, librarian, and watchdog

---

## REMAINING RISKS

| Risk | Severity | Notes |
|------|----------|-------|
| `db_helpers` not on path (watchdog separate-Pi) | LOW | Fallback is log-only, not JSON write. Alert is lost but not silently misdirected. |
| `company.db.suggestions` schema field mismatch | LOW | Not verified by runtime test — spot-check recommended on next agent run |
| Stale `suggestions.json` file on disk from prior runs | INFO | Gitignored, safe to delete. No agent writes to it. |

---

## NEXT BLOCKER RECOMMENDATION

SYS-B04 is resolved. The next active blocker is:

**SYS-B03** — Post-deploy rollback trigger broken
- watchdog.py reads `post_deploy_watch.json`
- blueprint.py writes to `company.db.deploy_watches`
- Rollback trigger never fires

**Note:** Per the session summary, the watchdog.py migration for post-deploy watch
(Step 2 of the normalization sprint) was also already applied in the previous session.
Verify SYS-B03 status before beginning remediation work.
