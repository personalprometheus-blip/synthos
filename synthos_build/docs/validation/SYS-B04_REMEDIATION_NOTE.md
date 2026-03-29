# SYS-B04 REMEDIATION NOTE

**Blocker:** SYS-B04 — Suggestions pipeline split
**Date:** 2026-03-29
**Status:** RESOLVED

---

## Problem

`blueprint.py` reads suggestions exclusively from `company.db.suggestions` via `db_helpers`.
Four agents were writing suggestions directly to `suggestions.json` (file-backed store).
Suggestions from those agents were silently dropped — blueprint never saw them.

---

## Files Changed

| File | Repo | Change |
|------|------|--------|
| `agents/vault.py` | synthos-company | Replaced direct JSON write with `db_helpers.post_suggestion()` |
| `agents/sentinel.py` | synthos-company | Replaced direct JSON write with `db_helpers.post_suggestion()` |
| `agents/librarian.py` | synthos-company | Replaced direct JSON write with `db_helpers.post_suggestion()` |
| `src/watchdog.py` | synthos (retail) | Replaced direct JSON write with `db_helpers.post_suggestion()` (with fallback to local log when db_helpers unavailable) |

---

## Old Behavior

Each agent independently constructed a suggestion dict and appended it to
`COMPANY_DATA_DIR/suggestions.json` via direct file I/O. `blueprint.py` never
reads from this file — it reads from `company.db.suggestions` only. Suggestions
written to JSON were permanently invisible to the deployment pipeline.

---

## New Behavior

All four agents call `db_helpers.post_suggestion()` wrapped in `_db.slot()` for
write coordination. Suggestions land in `company.db.suggestions` — the same
store blueprint reads from.

**watchdog.py** retains a graceful fallback: if `db_helpers` is unavailable
(separate-Pi deployment where `utils/` is not on the path), it logs the alert
locally and continues. This fallback is clearly marked in the code and does not
write to `suggestions.json`.

---

## Agents Migrated

- Vault (vault.py) — compliance and key alerts
- Sentinel (sentinel.py) — Pi silence alerts
- Librarian (librarian.py) — CVE and package suggestions
- Watchdog (watchdog.py) — crash, health, and rollback alerts

---

## Legacy JSON Path

No authoritative writes to `suggestions.json` remain in any of the four agents.

The `SUGGESTIONS_FILE` variable in `watchdog.py` has been removed (replaced with
a comment). The `suggestions.json` file in `data/` may still exist on disk from
prior runs but is no longer written by any migrated agent. It is gitignored and
can be deleted safely.

---

## What Still Needs Verification

- Confirm `db_helpers.post_suggestion()` contract matches payload fields sent by each agent (agent field, category, title, description, risk_level — spot-check against `company.db.suggestions` schema)
- Confirm `blueprint.py` query path reads from `company.db.suggestions` (expected: already does — this was the canonical read path before migration)
- Run a test suggestion through each agent in dry-run or test mode and confirm it appears in `company.db.suggestions`
- Confirm `suggestions.json` is no longer growing on the company Pi
