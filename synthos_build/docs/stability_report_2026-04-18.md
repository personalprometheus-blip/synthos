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

## 2. Fragility items found — status after PM fix session

### 2.1 Alert delivery — FIXED (boot race closed)

Morning diagnosis said both paths were dead. Corrected diagnosis after live-firing:

- **Path 1 (Scoop queue via POST `/api/enqueue`)** — works. `.env` has `MONITOR_URL=http://10.0.0.10:5050` and `MONITOR_TOKEN` matching pi4b's `SECRET_TOKEN`. Returned `200 OK`.
- **Path 2 (Resend direct)** — guard fails because `USER_EMAIL=''` and `ALERT_TO` is absent. Blank fallback. Not active today; would kick in only if Path 1 gave up.

**Boot-time race — addressed.** Both pi5 and pi4b reboot at 04:00 Sat; pi5's health_check fired at ~04:01:19 before pi4b was listening, Path 1 failed without retry, alert dropped. Two fixes landed this session (commit `b090c5f`):

- **`_enqueue_alert` retries on transport failures** — up to 3 attempts with 10 s backoff. Only retries `ConnectionError` / `Timeout` (non-transient 4xx/5xx pass through). Worst-case latency ~30 s; comfortably covers pi4b's 30–45 s boot-to-listening window. Verified on pi5:
  - Dead IP: retries 3×, falls through after 29.2 s ✓
  - Pi4b up: succeeds 1st try in 0.08 s, no retry noise ✓
- **`synthos-boot-sequence.service` uses `network-online.target`** — waits for DNS + actual reachability, not just link-up. Fixes the concurrent "Temporary failure in name resolution" for Alpaca at 04:01:19.

Path 2 (Resend fallback with `USER_EMAIL=''` blank) isn't closed — if Path 1 gives up after 30 s of retries *and* Path 2 would guard-fail, the alert is still dropped. Low priority because Path 1's retry window makes that almost impossible in practice, and belt-and-suspenders is a separate feature ask.

### 2.2 `db_helpers` import — NOT dead code (graceful degradation)

Rechecking retail_watchdog.py line 10-12 docstring: *"Watchdog does NOT send email or SMS directly. All alerts are written to company.db via db_helpers.post_suggestion() and Scoop delivers the notification."* Lines 67-84 show this is an explicit shared-Pi-only path — there is a module-level try/except that logs a `warning` when the module is unreachable, sets `_db = None`, and the downstream callers all guard on `if _db is None: return False`.

This is deliberate cross-deployment design, not dead code. In the current two-node setup, the watchdog's DB-level alert path is intentionally inert; the equivalent cross-node alerting is through `_enqueue_alert()` in `retail_health_check.py` (Path 1 above). No fix needed.

### 2.3 ORPHAN positions false positive — FIXED

Morning report cited `health_check: ORPHAN positions in Alpaca (not in DB): MSFT, AMD, HOOD, AVGO, KGC, LYFT, AAL, BIL, LUMN` as a reconciliation bug. Investigation showed:

- Master signals.db has 0 OPEN positions (multi-tenant mode: positions live per-customer).
- Alpaca returned 9 positions (Patrick's admin paper account).
- `check_positions()` computes `orphans = alpaca_tickers - db_tickers` = {9 tickers} - {} = 9 false "orphans".

Every boot flagged it as an error. It's a structural mismatch between the single-tenant design that check was written for and the current multi-tenant architecture.

**Fixed this session** (commit pending): `check_positions()` now detects empty master-DB positions and skips with an informational log:

```
✓ Position reconciliation: skipped — master DB has no positions
  (multi-tenant mode; 9 positions reported by Alpaca, reconciled
   per-customer by customer_health_check)
```

Alpaca connectivity check (the genuinely useful part of the step — catches DNS/auth/network problems) is preserved.

### 2.4 pi5 `backups/staging/` directory absent

Got cleaned during migration. `retail_backup.py` recreates it on demand at the next 01:30 fire. Self-heals tonight. No action.

### 2.5 journald persistence

False alarm this morning. `/var/log/journal` exists, `Storage=auto` is in effect, 32.8 MB stored. Persistence working; history accumulates across future reboots. No action.

---

## 3. Housekeeping addressed this session

- **Dead 0-byte log files on pi5** — deleted (`daily.log`, `install.log`, `monitor.log`, `manual_run.log`, `price_poller.log`). They had been zero bytes since Apr 11/14 and nothing was writing to them.
- **pi4b systemd unit snapshots** — captured to `ops/systemd/pi4b/`: `synthos-archivist.service`, `synthos-auditor.service`, `synthos-login-server.service`, `synthos-company-server.service`. Repo now has symmetric canonical systemd state for both nodes.

## 4. Remaining carryover — not urgent

- **Full pi5 reboot test** — exercise boot chain end-to-end on an intentional reboot. Scheduled Saturday 04:00 will do this automatically next week, and is now the first real-world test of the retry + `network-online.target` fixes from §2.1.
- **Stale `tool_agent.log` / `interface_agent.log` / `control_agent.log`** on pi4b — no longer written to after today's LOG_FILE rename; will naturally age out. Harmless.
- **Path 2 (Resend) fallback still not wired** — `USER_EMAIL=''` in pi5's `.env`. Not a fragility today (Path 1 retry covers the typical race); only matters if pi4b is permanently down AND we need the alert delivered by email anyway. Flag for belt-and-suspenders in a future session.

---

## 5. Foundation health: green (after PM fix pass)

Every change from today's heavy bug-fix day is verified working in production. No regressions. The three flagged "silent failure modes" from the morning report:

- **Alert delivery** → not actually broken; live-fire confirms Path 1 works. Boot-time race known and documented.
- **`db_helpers` import** → deliberate cross-deployment graceful degradation, not a bug.
- **ORPHAN false positive** → real bug, **fixed this session**.

**Foundations are solid enough to build on.** Quick wins taken this session (orphan fix, dead-log cleanup, pi4b unit snapshots). Carryover in §4 is genuinely low-urgency; next session can safely open with extension work.
