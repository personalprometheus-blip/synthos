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
