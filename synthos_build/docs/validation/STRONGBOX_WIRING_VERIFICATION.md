# STRONGBOX WIRING VERIFICATION

**Date:** 2026-03-29
**Verifies:** Changes described in STRONGBOX_WIRING_NOTE.md

---

## 1. INSTALLER VERIFICATION STATUS

**Check:** Is `strongbox.py` in `REQUIRED_AGENT_FILES` in `install_company.py`?

```python
REQUIRED_AGENT_FILES = [
    "patches.py",
    "blueprint.py",
    "sentinel.py",
    "fidget.py",
    "librarian.py",
    "scoop.py",
    "vault.py",
    "timekeeper.py",
    "strongbox.py",    ← ADDED
]
```

**Confirmed: YES**

The installer's verification phase (which checks `AGENTS_DIR / f` for each name in this list) will now include strongbox.py. An install run on a system where strongbox.py is absent will correctly report failure.

---

## 2. CRON REGISTRATION STATUS

**Check:** Does `register_cron()` in `install_company.py` write a nightly entry for strongbox?

```python
# Strongbox (Backup Manager) — nightly 11pm ET
f"0 23 * * *         {py} {agent('strongbox.py')} >> {logf('strongbox')} 2>&1",
```

**Confirmed: YES**

- Schedule: `0 23 * * *` — 11:00 PM local time nightly
- Pattern: identical to all other company agent entries (no new mechanism)
- Log destination: `{LOG_DIR}/strongbox.log` via the same `logf()` helper used throughout

Note: The live crontab will not reflect this entry until `install_company.py` is run again or the cron is manually re-registered. The change is in the installer; the running system requires a re-run to pick it up.

---

## 3. STALE DOC REFERENCE STATUS

**Check:** Is the stale strongbox reference in `synthos-company/CLAUDE.md` corrected?

Before:
```
| strongbox.py | Automated backups | — (NEEDS MOVE from retail repo) |
```
```
- strongbox.py is currently in the retail repo (src/) — must be moved here (Step 4 of normalization)
```

After:
```
| strongbox.py | Automated backups | — |
```
```
- strongbox.py is correctly placed in agents/ (Step 4 complete); wired into installer
  and nightly cron (0 23 * * *); backup model not yet aligned to BACKUP_STRATEGY_INITIAL.md
  — tracked in milestones.md
```

**Confirmed: YES**

No other lines in CLAUDE.md were modified.

---

## 4. WHAT REMAINS OPEN

| Item | Status |
|------|--------|
| strongbox.py backup behavior | Unchanged — still implements daily full backup, Fernet encryption, Cloudflare R2 upload, 30-day retention |
| Alignment with BACKUP_STRATEGY_INITIAL.md | NOT resolved — model mismatch (daily full vs. monthly baseline + nightly incremental, R2 vs. local, 30-day vs. 6-month retention) |
| Live crontab | Not updated — requires installer re-run to register the new entry |
| Timekeeper integration | Not implemented — strongbox does not use the request/grant model |
| Backup policy migration | Tracked in `docs/milestones.md` as future phase work |

Wiring strongbox into the installer and scheduler does not resolve the backup-policy alignment gap. That gap is intentionally deferred.

---

## 5. FINAL STATUS

**STRONGBOX_WIRING_STATUS: RESOLVED**

Installer verification and cron wiring are both complete in `install_company.py`. The stale CLAUDE.md reference is corrected. No strongbox behavior was modified.

Backup-policy alignment (tiered model, local storage, 6-month retention) remains open and is explicitly not claimed as resolved here.
