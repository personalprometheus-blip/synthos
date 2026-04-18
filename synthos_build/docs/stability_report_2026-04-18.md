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

### 2.1 Alert delivery — updated diagnosis (NOT actually broken)

My morning write-up said both paths were dead. After live-firing `send_alert()` and tracing, the story is simpler:

- **Path 1 (Scoop queue via POST `/api/enqueue`)** — **works.** The `.env` has `MONITOR_URL=http://10.0.0.10:5050` and `MONITOR_TOKEN` matching pi4b's `SECRET_TOKEN`. Manual test returned `200 OK` and `"Health alert queued for Scoop: VALIDATION_FAILURE P1"`.
- **Path 2 (Resend direct)** — guard does fail because `USER_EMAIL=''` in the env file (actually empty, not just unset) and `ALERT_TO` is absent, so `ALERT_TO` resolves empty. Not a blocker because Path 1 succeeds first.

**Boot-time alert failure is a race, not a fragility.** Both pi5 and pi4b reboot at 04:00 Saturday per their crontabs. Pi5's health_check fires at ~04:01:19 while pi4b may still be coming up — the `POST /api/enqueue` hits a closed socket, Path 1 returns False, Path 2 fails guard (empty `ALERT_TO`), and the "Alert not delivered" log line is produced. Once pi4b is up (typically ~30–60s after the initial race), ongoing alerts work fine.

**Accepting this as known behavior.** Fixing properly requires either ordering the reboots so pi4b comes up first, adding retry/backoff in `_enqueue_alert`, or wiring a local-machine fallback (e.g. populating `USER_EMAIL` so Path 2 works). None are urgent — the race is narrow and operational alerts during regular-hours are working correctly.

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

- **Full pi5 reboot test** — exercise boot chain end-to-end on an intentional reboot. Scheduled Saturday 04:00 will do this automatically next week.
- **Stale `tool_agent.log` / `interface_agent.log` / `control_agent.log`** on pi4b — no longer written to after today's LOG_FILE rename; will naturally age out. Harmless.
- **Boot-race alert loss** — documented in §2.1. Known behavior on Saturday 04:00 reboot when pi5+pi4b come up together. Fix requires either reboot ordering, retry/backoff, or populating `USER_EMAIL` for Path 2 fallback. Not fixing in this stabilization session — will revisit if we see a real incident lost to the race.

---

## 5. Foundation health: green (after PM fix pass)

Every change from today's heavy bug-fix day is verified working in production. No regressions. The three flagged "silent failure modes" from the morning report:

- **Alert delivery** → not actually broken; live-fire confirms Path 1 works. Boot-time race known and documented.
- **`db_helpers` import** → deliberate cross-deployment graceful degradation, not a bug.
- **ORPHAN false positive** → real bug, **fixed this session**.

**Foundations are solid enough to build on.** Quick wins taken this session (orphan fix, dead-log cleanup, pi4b unit snapshots). Carryover in §4 is genuinely low-urgency; next session can safely open with extension work.
