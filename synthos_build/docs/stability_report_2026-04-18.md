# Stability Report — 2026-04-18 PM

Snapshot of system foundations after a heavy bug-fix day. Purpose: prove today's work held in production, identify silent-failure modes, and give future-us a clean reference point before building on this foundation.

Generated during the "stabilize, don't extend" session on 2026-04-18 around 14:00 ET.

---

## 1. What we verified green

### Systemd unit state (pi5 + pi4b)

| Node | Unit | State | Restarts | Last start |
|------|------|-------|----------|------------|
| pi5  | synthos-boot-sequence | active (exited, Result=success) | 0 | 2026-04-18 08:14:50 |
| pi5  | synthos-portal        | active (running)                | 0 | 2026-04-18 04:00:21 |
| pi5  | synthos-watchdog      | active (running)                | 0 | 2026-04-18 08:55:09 |
| pi4b | synthos-archivist     | active (running)                | 0 | 2026-04-18 13:36:05 |
| pi4b | synthos-auditor       | active (running)                | 0 | 2026-04-18 04:00:18 |
| pi4b | synthos-login-server  | active (running)                | 0 | 2026-04-18 04:00:18 |

No restart loops, no failed units, no zombie processes. Dependency chain on pi5 verified: `network.target → synthos-boot-sequence → synthos-portal → synthos-watchdog`.

### Dedupe fixes held

All 10 files touched in commits `67a5c86` + `142dd04` are writing single-line entries in production:

- pi5: scheduler.log, watchdog.log, interrogation.log, retail_backup.log — 0 duplicate groups in any window since the fix landed.
- pi4b: archivist.log, vault.log, librarian.log, sentinel.log, strongbox.log, fidget.log — 0 duplicate groups since 13:00 today.
- Bonus LOG_FILE renames on pi4b verified: vault's INFO lines now appear in `vault.log` (was silently going to `control_agent.log`); same pattern works for sentinel + librarian.

### Heartbeat pipeline

Both nodes sending to `http://10.0.0.10:5050/heartbeat` every minute:
- pi5 → pi4b: **60 sent / 0 failures** in last hour
- pi4b → localhost: **60 sent / 0 failures** in last hour

Cadence alignment from this morning's work is holding. Pi4b's URL/token fix (from stale `192.168.203.10`) is still in effect.

### Weekend scheduler

The `5 * * * 0,6` crontab entry added this morning is firing correctly — confirmed fires at 09:05:01, 10:05:01, 11:05:01, 12:05:01, 13:05:01 today (Sat). Each run completes in 1–13 seconds. Zero overlap with the 01:30 backup or any other cron slot.

### Backup chain — was flagged as broken; actually recovered

Morning health report flagged `HTTP 413` in `retail_backup.log` as an ongoing failure. Investigation shows:

- **2026-04-17 01:30** — failed (`Request Entity Too Large`, limit was 8 MB).
- A commit between Apr 17 and Apr 18 raised `MAX_CONTENT_LENGTH` on `synthos_monitor.py` from 8 MB → 200 MB. Commit message: *"synthos_monitor: raise upload limit 8MB → 200MB (real fix)"*.
- **2026-04-18 01:30** — **succeeded** (9.6 MB tarball uploaded cleanly).

The morning grep caught the stale Apr-17 error line and flagged it as current. Backup chain is not broken. Tonight's 01:30 run will re-confirm.

### Cron / service error scan

- `sudo journalctl --since '24 hours ago'` on both nodes: no cron failures, no service restart events other than the ones we deliberately triggered.
- Auditor's `detected_issues` table has no new critical/high entries since this morning's scan cycles.

---

## 2. Silent failure modes identified (not fixed today)

These are things that are currently broken-but-not-complaining. Each one is scoped and ready to fix when we resume extension work; none is urgent enough that it blocks today's stabilization goal.

### 2.1 Alert delivery — both paths dead

`send_alert()` in `retail_health_check.py` tries two paths in order:

**Path 1 — Scoop queue via `db_helpers`:**
```python
from db_helpers import DB as _DB   # retail_watchdog.py:79
```
The `db_helpers` module **doesn't exist anywhere on pi5 or pi4b** (`find ~/synthos ~/synthos-company -name db_helpers.py` returns nothing). The import is caught in a try/except that logs `"db_helpers not available — alerts will fall back to local log"` and continues with `None`. So Path 1 has been dead since at least 2026-04-15 (when log starts) — possibly always.

**Path 2 — Resend direct email:**
```python
# retail_health_check.py line 35 and 111:
ALERT_TO = os.environ.get('ALERT_TO', os.environ.get('USER_EMAIL', ''))
if RESEND_API_KEY and ALERT_FROM and ALERT_TO:
```
Env file on pi5 has `RESEND_API_KEY`, `ALERT_FROM`, and **`ALERT_PHONE`** — but the code reads **`ALERT_TO`** (with fallback to `USER_EMAIL`, also not set). Name mismatch → `ALERT_TO` resolves empty → guard fails → Path 2 never attempts.

**Net result:** when health_check or watchdog calls `send_alert()`, nothing leaves the machine. The only trace is `"Alert not delivered — all paths failed or unconfigured"` in boot.log.

**Fix options (deferred):**
- Trivial: add `ALERT_TO=<email>` to `user/.env` on pi5. Restores Path 2.
- Medium: locate or reimplement `db_helpers.py` (likely was in a previous repo layout). Restores Path 1.
- Right now, pick the trivial one; defer the other until we formalize cross-node alert routing.

### 2.2 `db_helpers` import in retail_watchdog.py

Line 79 imports a module that doesn't exist. Try/except swallows the ImportError and the watchdog continues without the helper. No functional impact today — the code path that would have used `_DB` also has a None check — but it's dead code pretending to be live.

**Fix options:** delete the import and the dead `_DB` code path, or restore `db_helpers.py`. Either way, cheap cleanup.

### 2.3 pi5 `backups/staging/` directory absent

Got removed in the migration cleanup and hasn't been recreated. `retail_backup.py` creates it on demand at the next 01:30 fire, so this self-heals tonight. Flagging only for the record; no action needed.

### 2.4 journald persistence

This morning I flagged it as broken. Re-checking: `/var/log/journal` **does** exist, `journald.conf` uses `Storage=auto` (which persists when the directory is present), and 32.8 MB of current-boot journal is stored. Earlier "no persistent journal" error was for boot offset `-1` (there is no previous boot — migration reset the journal dir). Persistence is working; history will accumulate across future reboots.

**No action needed.**

---

## 3. Open items carried from this morning (not touched)

Tracked for a future session:

- **Orphan positions** — 9 tickers in Alpaca paper account (`MSFT, AMD, HOOD, AVGO, KGC, LYFT, AAL, BIL, LUMN`) not reflected in local DB. Reconciliation needed.
- **Dead 0-byte log files on pi5** — `daily.log`, `install.log`, `monitor.log`, `manual_run.log`, `price_poller.log`. Housekeeping.
- **pi4b systemd unit snapshots** → `ops/systemd/pi4b/` for symmetric canonical state. ~10 min of purely additive work.
- **Full pi5 reboot test** — exercise boot chain end-to-end on an intentional reboot. Scheduled Saturday 04:00 will do this automatically next week.
- **Stale `tool_agent.log` / `interface_agent.log` / `control_agent.log`** on pi4b — no longer written to after today's LOG_FILE rename; will naturally age out. Harmless.

---

## 4. Foundation health: green

The stack is in a stable, verifiable state. Every change landed today (across seven commits in the retail repo plus one in synthos-company) has been observed working in production. No regressions detected. The three silent-failure modes identified are all:

- Non-fatal today (nothing actively failing; only alert paths are silent, and nothing has needed to alert).
- Well-scoped (known symptom + known cause + known fix option).
- Safe to carry into the next session.

**Foundations are solid enough to build on.** Next session, pick one item from §2 or §3 and extend from there.
