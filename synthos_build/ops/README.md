# synthos_build/ops/

Canonical, version-controlled snapshots of per-node operational state:

- **`systemd/<node>/`** — every custom systemd unit running on that node. The live unit lives at `/etc/systemd/system/<name>.service`; copies here are the source of truth for what that file should contain. After editing a file here, deploy to the node, then `sudo systemctl daemon-reload`.
- **`crontab.<node>.txt`** — `crontab -l` captured for each node. Not auto-applied; if these diverge from the live crontab it's a signal that either the repo is stale or someone edited the live crontab out-of-band.
- **`logrotate/<node>/`** — drop-in logrotate config files, one per `/etc/logrotate.d/<name>` the node installs. Applied daily by the system's `logrotate.timer` (enabled by default on Debian). Deploy: `scp` to node, then `sudo cp` into `/etc/logrotate.d/`.

## Nodes

| Node | Hostname | Role | Units tracked |
|------|----------|------|---------------|
| pi5 | SentinelRetail (10.0.0.11) | Retail stack | `synthos-boot-sequence`, `synthos-portal`, `synthos-watchdog` |
| pi4b | (10.0.0.10) | Company/monitor stack | _(to be captured)_ |

## Boot chain on pi5

```
network.target
    └─ synthos-boot-sequence.service   (oneshot, Type=oneshot)
          ├─ Wanted by: synthos-portal.service
          └─ Before=: synthos-portal.service, synthos-watchdog.service

synthos-portal.service   (simple, Restart=always)
    └─ synthos-watchdog.service   (simple, Restart=always, After=synthos-portal)
```

Boot-sequence is a soft dep (`Wants=`) of the portal, not a hard dep (`Requires=`). That means a failed boot-sequence check does not block the portal from starting — it only logs the failure. This matches the pre-systemd philosophy of "let agents run and fail gracefully rather than halt on boot errors."

## Deploying a unit file change

```bash
# From the repo, with node_name = pi5 or pi4b:
rsync -avz ops/systemd/<node_name>/<unit>.service <node>:/tmp/
ssh <node> "sudo cp /tmp/<unit>.service /etc/systemd/system/ \
            && sudo systemctl daemon-reload \
            && sudo systemctl restart <unit>"
```

## Restoring the crontab from snapshot

```bash
# From a machine with SSH to <node>:
scp ops/crontab.<node>.txt <node>:/tmp/
ssh <node> "crontab /tmp/crontab.<node>.txt && crontab -l"
```

## History

- **2026-04-18 (AM)** — pi5 migrated retail_boot_sequence.py from `@reboot` cron to `synthos-boot-sequence.service`. Removed two now-redundant `@reboot` entries (boot_sequence + watchdog). Commit chain: `5cdee51` (boot_sequence systemd-aware) → `7b52281`.
- **2026-04-18 (AM)** — pi4b cron: moved `company_vault.py --backup-now` from 02:00 to 01:45 to eliminate second-by-second collision with `company_strongbox.py` at 02:00 (both write to R2; future risk if vault's integration ever completes). Added `/etc/logrotate.d/synthos-company` for daily rotation of all `synthos-company/logs/*.log` with 30-day retention. Note: two logs (`archivist.log`, `auditor.log`) were initially owned by root because `StandardOutput=append:` in their systemd units opens the file as root before the service drops to `User=pi`; chowning to pi once is sufficient because systemd writes through the pre-opened FD and logrotate's `copytruncate` preserves that FD.
- **2026-04-18 (AM)** — fixed pi4b heartbeat target. `company.env` had `MONITOR_URL=http://192.168.203.10:5000` (stale subnet from pre-current network topology) and `MONITOR_TOKEN=synthos-default-token` (install-time default). Both heartbeats had been silently failing every 5 minutes for an unknown period. Changed to `MONITOR_URL=http://10.0.0.10:5050` (pi4b's own command portal, same endpoint pi5 successfully posts to) and `MONITOR_TOKEN` to match the existing `SECRET_TOKEN` on the node. First post-fix heartbeat at 08:27:14 reported OK.
- **2026-04-18 (AM)** — aligned pi4b heartbeat cadence from 5min to 1min (matching pi5). Updated `node_heartbeat.py` `_detect_agents()` to also recognize retail long-running agents (`retail_portal`, `retail_watchdog`, `retail_interrogation_listener`, `retail_market_daemon`, `retail_price_poller`). Previously pi5 reported only `['node_heartbeat']` because the `known` dict contained only company-side script names; post-fix pi5 reports its real running agents. Note: pi4b has an independent copy of `node_heartbeat.py` in the `synthos-company` repo — the fix landed here (retail repo) only, so if pi4b's copy is ever refreshed it should merge this change.
- **2026-04-18 (PM)** — enabled persistent systemd-journal on pi5. RaspberryPiOS ships `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with `Storage=volatile` to protect the SD card; that's obsolete post-NVMe-migration. Added `/etc/systemd/journald.conf.d/90-nvme-persistent.conf` (captured in `ops/systemd/pi5/journald.conf.d/`) with `Storage=persistent`. `/etc/`-scope drop-ins override `/usr/lib/`-scope. Created `/var/log/journal/<machine-id>/`, restarted journald, flushed volatile content to disk. Future reboot history is preserved. Pi4b still runs the RPi default because it's still on microSD.
- **2026-04-18 (AM)** — added pi5 weekend overnight-scheduler cron (`5 * * * 0,6`), matching the weekday `:05` pattern but running 24 hours/day on Sat+Sun. Previously weekends had zero scheduler fires; now news/sentiment enrichment runs hourly all weekend so Monday open doesn't have a stale signal pool. Verified: scheduler runtime is 1–13s per fire, so no overlap with 01:30 backup, 03:00 rebuild, 03:55 Saturday shutdown, or 04:00 Saturday reboot. Cron-timing cheatsheet added above for future work.

## Known issues — cosmetic, deferred

- **Duplicate log lines** across at least `scheduler.log` and `strongbox.log` (and likely more). Root cause: each script's Python logging is configured with both a `FileHandler` to its own log file and a `StreamHandler` to stdout, **plus** the cron entry redirects stdout `>>` to the same log. Result: FileHandler writes each line, stdout-via-cron writes the same line again. Either drop the `StreamHandler` from the Python config OR drop the cron-side `>> log 2>&1`. Cosmetic (just doubles log volume); fix coordinates across retail + synthos-company repos so deferred.
- **Vault `--backup-now` no-op on pi4b** — scheduled daily but the script can't find retail customer data (it's on pi5, not pi4b). Leaves the cron in place so future integration work gets scheduling for free; revisit when backup architecture is formalized.

## Pi5 cron timing cheatsheet

Minute offsets are chosen so nothing starts at the exact same second as
anything else, and so the scheduler never begins within a few minutes
of the backup or the weekly reboot. Read this before adding a new job.

| Minute | What fires | Days | Notes |
|--------|-----------|------|-------|
| `* * * * *` | `node_heartbeat.py` | every | every minute, trivial |
| `5 0-8,16-23 * * 1-5` | `retail_scheduler.py --session overnight` | weekdays | overnight window only; market_daemon owns 9–15 |
| `5 * * * 0,6` | `retail_scheduler.py --session overnight` | weekends | all 24 hours; market is closed so no conflict with market_daemon |
| `10 9 * * 1-5` | `retail_market_daemon.py` | weekdays | 9:10 ET trading-day start |
| `21 * * * 1-5` | `retail_heartbeat.py --session trade` | weekdays | **:21 deliberately**: avoids :05 scheduler and :10 market_daemon overlap windows |
| `30 1 * * *` | `retail_backup.py` | every | nightly 01:30 |
| `0 3 * * *` | `rebuild_default_template.py` | every | 03:00, runs <1s |
| `55 3 * * 6` | `retail_shutdown.py` | Saturday | graceful pre-reboot stop |
| `0 4 * * 6` | `sudo reboot` | Saturday | weekly maintenance reboot |
| `0 20 * * 0` | `retail_scheduler.py --session prep` | Sunday | weekly Monday-prep (distinct from overnight) |
| `0 0 * * 0` | `rotate_logs.py` | Sunday | weekly log rotation |

If adding a new hourly cron, avoid these conflict zones:
- **Minute :00** — rebuild_template fires at 03:00; reboot at 04:00 on Sat
- **Minute :05** — overnight scheduler (all days now)
- **Minute :10** — market_daemon at 09:10 weekdays
- **Minute :21** — retail_heartbeat hourly weekdays
- **Minute :30** — nightly backup at 01:30

Pick an unused offset (e.g. :15, :40, :45) for new hourly jobs.

## Heartbeat cadence — 1 minute on both nodes

- **pi5** → `http://10.0.0.10:5050/heartbeat` every minute. Cross-node; primary failure-detection signal during trading hours.
- **pi4b** → `http://10.0.0.10:5050/heartbeat` every minute. Self-loop; measures "is pi4b's command-portal process accepting POSTs?".

The two signals mean different things (cross-node reachability vs self-liveness) but the cadences are aligned at 1min for consistency and simpler ops reasoning. Total write volume: 2880 heartbeat records/day across the pair — trivial load on the monitor DB.

## Config files NOT tracked in this directory

- `user/.env` (pi5) and `synthos-company/company.env` (pi4b) hold secrets (API keys, shared tokens, encryption keys). They are `.gitignore`'d. When either node is rebuilt, these files must be restored from a separate secrets backup. Keys that should be set:
  - `MONITOR_URL` — both nodes point at `http://10.0.0.10:5050`
  - `MONITOR_TOKEN` — both nodes use the same shared secret (matches `SECRET_TOKEN` on the monitor side, which is pi4b)
  - `ENCRYPTION_KEY`, `PORTAL_SECRET_KEY`, `ANTHROPIC_API_KEY`, etc.
