# Pi5 — SD → NVMe Migration Playbook

One-shot playbook for cloning the retail stack off the SD card onto
the attached NVMe and making NVMe the boot device.

## Current state (confirmed 2026-04-18 recon)

```
mmcblk0     119.1G  SD card (current root)
├─mmcblk0p1   512M  /boot/firmware  (FAT32)
└─mmcblk0p2 118.6G  /               (ext4)
nvme0n1     238.5G  Patriot P300    (empty — migration target)
```

- Debian 13 (trixie), kernel 6.12.47
- EEPROM: `2025/11/05` release, `BOOT_ORDER=0xf416` (SD first — we
  will change to `0xf461` for NVMe first)
- Services currently running: `synthos-portal`, `synthos-watchdog`
- Project on disk: 245 MB; actively-written DBs total ~100 MB
- Market is closed; next trading session is Monday 9:30 ET

## Strategy

**Clone, swap boot source, physically pull SD.** The user-approved
plan:

1. Clone SD → NVMe with `rpi-clone` (rsync-based, DB-safe with
   services stopped; only copies used blocks so it's fast and the
   NVMe ext4 fills to the clone's actual size, not the SD's
   partition size).
2. Update NVMe's `/etc/fstab` and `/boot/firmware/cmdline.txt` so the
   cloned system references NVMe UUIDs, not the SD's.
3. Flip `BOOT_ORDER` in EEPROM to `0xf461` (NVMe first, USB, SD,
   restart).
4. Power down. Pull SD. Power on.

**Rollback:** two routes depending on failure mode — see §9.

## 1. Pre-flight (non-destructive)

All of this runs while services are still up; none of it changes
anything permanent.

```bash
# 1a. Final safety backup — a clean tarball of the entire stack,
# stored OFF the pi5 on the workstation. This is a last-resort
# restore source.
mkdir -p ~/pi5_migration_safety
ssh -J pi4b pi516gb@10.0.0.11 "sudo tar czf - --exclude=/home/pi516gb/synthos/backups/staging \
                                           --exclude=/home/pi516gb/synthos/synthos_build/logs/archive \
                                           --exclude=/home/pi516gb/synthos/synthos_build/.known_good \
                                           /home/pi516gb/synthos" \
  > ~/pi5_migration_safety/pi5_synthos_pre_migration_$(date -u +%Y%m%dT%H%M%SZ).tar.gz

# 1b. Rolling backup tarballs in backups/staging/ — pull those too.
rsync -avz -e "ssh -J pi4b" pi516gb@10.0.0.11:/home/pi516gb/synthos/synthos_build/backups/staging/ \
       ~/pi5_migration_safety/backups_staging/

# 1c. NVMe physical state
ssh -J pi4b pi516gb@10.0.0.11 "sudo smartctl -a /dev/nvme0n1 || \
                               sudo apt install -y smartmontools && sudo smartctl -a /dev/nvme0n1"
# Expect: no reallocated sectors, no media errors, temp < 70°C.

# 1d. Confirm rpi-clone is available; install if not.
ssh -J pi4b pi516gb@10.0.0.11 "which rpi-clone || \
  (sudo apt install -y git rsync && \
   cd /tmp && git clone https://github.com/billw2/rpi-clone.git && \
   sudo cp rpi-clone/rpi-clone rpi-clone/rpi-clone-setup /usr/local/sbin/)"

# 1e. DB integrity baseline — every signals.db + auth.db pre-clone.
ssh -J pi4b pi516gb@10.0.0.11 "cd ~/synthos/synthos_build && \
  for db in data/auth.db user/signals.db data/customers/*/signals.db; do
    echo -n \"\$db: \"
    sqlite3 \"\$db\" 'PRAGMA integrity_check;' | head -1
  done"
# Expect: every line ends in 'ok'.
```

## 2. Quiesce services (stop writers)

Critical: rsync during live DB writes can produce a corrupt DB copy.
Stop all writers first. Confirm nothing holds a write lock on any DB.

```bash
ssh -J pi4b pi516gb@10.0.0.11 "
  sudo systemctl stop synthos-portal synthos-watchdog
  sudo systemctl stop cron                      # stop per-minute heartbeat cron
  sleep 3
  sudo lsof /home/pi516gb/synthos/synthos_build/data/auth.db \
           /home/pi516gb/synthos/synthos_build/user/signals.db \
           /home/pi516gb/synthos/synthos_build/data/customers/*/signals.db 2>&1 | head -10
"
# Expect: no output (no process holding any DB file open).
```

## 3. Clone

`rpi-clone` handles partitioning, filesystem creation, rsync, fstab
UUID rewrite, and cmdline.txt root-device rewrite. It's the standard
Pi tool for this.

```bash
# Dry run first — rpi-clone -v is verbose; reviewing before commit.
ssh -J pi4b pi516gb@10.0.0.11 "sudo rpi-clone -f -v nvme0n1"
# Flags:
#   -f     : force unattended (don't prompt for each confirmation)
#   -v     : verbose
#   nvme0n1: destination device (target — NO partition suffix)
# rpi-clone will:
#   - partition nvme0n1 to mirror mmcblk0's layout
#   - mkfs.vfat on p1, mkfs.ext4 on p2
#   - mount both under /mnt/clone
#   - rsync everything
#   - rewrite /mnt/clone/etc/fstab to use the new PARTUUID values
#   - rewrite /mnt/clone/boot/firmware/cmdline.txt root= to the new
#     PARTUUID of nvme0n1p2
#   - unmount and optionally sync filesystems
```

**Expected runtime:** ~3–6 minutes for ~5 GB of used space over
NVMe PCIe x1 (Pi5's nvme bus).

## 4. Sanity-check the clone before committing to it

```bash
ssh -J pi4b pi516gb@10.0.0.11 "
  sudo mkdir -p /mnt/clone_check
  sudo mount /dev/nvme0n1p2 /mnt/clone_check

  echo '=== fstab on clone ==='
  cat /mnt/clone_check/etc/fstab | grep -v '^#' | grep -v '^\$'

  echo '=== cmdline.txt on clone ==='
  sudo mkdir -p /mnt/clone_check_boot
  sudo mount /dev/nvme0n1p1 /mnt/clone_check_boot
  cat /mnt/clone_check_boot/cmdline.txt

  echo '=== compare synthos trees (should be identical after clone) ==='
  sudo diff -rq /home/pi516gb/synthos /mnt/clone_check/home/pi516gb/synthos | head -20 || echo 'identical'

  echo '=== DB integrity on clone ==='
  for db in /mnt/clone_check/home/pi516gb/synthos/synthos_build/data/auth.db \
           /mnt/clone_check/home/pi516gb/synthos/synthos_build/user/signals.db \
           /mnt/clone_check/home/pi516gb/synthos/synthos_build/data/customers/*/signals.db; do
    echo -n \"\$(basename \$(dirname \$db))/\$(basename \$db): \"
    sqlite3 \"\$db\" 'PRAGMA integrity_check;' | head -1
  done

  sudo umount /mnt/clone_check_boot /mnt/clone_check
  sudo rmdir /mnt/clone_check_boot /mnt/clone_check
"
# Expected:
#   - fstab references /dev/nvme0n1p1 (or its PARTUUID) and
#     /dev/nvme0n1p2 — NOT mmcblk0 paths
#   - cmdline.txt 'root=' references nvme0n1p2 PARTUUID
#   - diff is empty (or only runtime state like logs/watchdog.pid)
#   - every DB reports 'ok'
```

If any check fails — **do not proceed**. Re-run rpi-clone or fix the
specific issue.

## 5. Set EEPROM boot order

```bash
ssh -J pi4b pi516gb@10.0.0.11 "
  # Save current config.
  sudo rpi-eeprom-config > /tmp/eeprom_before.txt

  # Write new config — change BOOT_ORDER=0xf416 → 0xf461.
  sudo rpi-eeprom-config --edit-config /tmp/eeprom_before.txt > /tmp/eeprom_after.txt
  # (interactive; change the one line, save)

  # Or scriptable:
  sudo sed 's/BOOT_ORDER=0xf416/BOOT_ORDER=0xf461/' /tmp/eeprom_before.txt \
       | sudo tee /tmp/eeprom_after.txt
  sudo rpi-eeprom-config --apply /tmp/eeprom_after.txt
"
# 0xf461 = NVMe (6), USB (4), SD (1), restart (f) — tried in that order.
```

Nothing visible has happened yet — EEPROM updates apply on next boot.

## 6. Restart services on the SD system (still running from SD)

Services were stopped in §2 to quiesce DBs for the clone. Now that
the clone is committed and EEPROM is updated, bring them back up on
the SD system so the user can verify live behavior before cutover.

```bash
ssh -J pi4b pi516gb@10.0.0.11 "
  sudo systemctl start cron synthos-watchdog synthos-portal
  sleep 3
  systemctl is-active synthos-portal synthos-watchdog cron
  curl -sS -o /dev/null -w 'login=%{http_code}\\n' http://localhost:5001/login
"
# Expect: active / active / active, login=200.
```

## 7. Cutover

```bash
# Final clean shutdown.
ssh -J pi4b pi516gb@10.0.0.11 "sudo shutdown -h now"
```

**Physical steps (user-hands):**

1. Wait for the Pi5's activity LED to go dark (≥15 s of no blinking).
2. Unplug power.
3. Remove the SD card. **Store it safely — it is your cold rollback.**
4. Double-check the NVMe HAT is seated.
5. Replug power.
6. Wait ~60 s for boot (NVMe boot is faster than SD; expect <30 s to
   SSH readiness).

## 8. Post-cutover verification

```bash
# Should SSH via the same IP — no network config change.
ssh -J pi4b pi516gb@10.0.0.11 "
  echo '=== ROOT DEVICE (must be nvme0n1p2) ==='
  findmnt /

  echo '=== LSBLK (no mmcblk should appear) ==='
  lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE

  echo '=== SERVICES ==='
  systemctl is-active synthos-portal synthos-watchdog cron

  echo '=== PORTAL RESPONDS ==='
  curl -sS -o /dev/null -w 'login=%{http_code}\\n' http://localhost:5001/login

  echo '=== DB INTEGRITY POST-MIGRATION ==='
  cd ~/synthos/synthos_build
  for db in data/auth.db user/signals.db data/customers/*/signals.db; do
    echo -n \"\$db: \"
    sqlite3 \"\$db\" 'PRAGMA integrity_check;' | head -1
  done

  echo '=== TOOLS SMOKE ==='
  python3 tools/verify_schema.py 2>&1 | tail -5
"
```

**Green-light criteria (all must be true):**

- `findmnt /` → source is `/dev/nvme0n1p2`
- no `mmcblk*` anywhere in `lsblk`
- three services active
- portal returns HTTP 200 on `/login`
- every DB reports `ok` from integrity check
- `verify_schema.py` reports `OK` fleet-wide

## 9. Rollback

### If Pi5 doesn't boot at all

Pull power, reinsert SD, power on. With the SD card physically
present, the EEPROM `BOOT_ORDER=0xf461` falls through NVMe (if NVMe
is unreachable or the clone is broken) → USB → SD, and boots SD
with the original system intact. Nothing on the SD was touched
during cloning.

### If Pi5 boots NVMe but services fail / data looks wrong

Two options:

**(a) Debug on NVMe.** SSH in, diagnose, fix. The SD is safely
stored as a backup; no rush.

**(b) Full rollback to SD.**

```bash
# From the running (but broken) NVMe system, or from a recovery shell:
sudo rpi-eeprom-config > /tmp/eeprom.txt
sudo sed -i 's/BOOT_ORDER=0xf461/BOOT_ORDER=0xf416/' /tmp/eeprom.txt
sudo rpi-eeprom-config --apply /tmp/eeprom.txt
sudo shutdown -h now
```

Then physically: reinsert SD. Power on. Boots SD.

If the NVMe system is too broken to run `rpi-eeprom-config`:

1. Power off.
2. Disconnect NVMe HAT (or just reinsert SD — with SD present, the
   EEPROM will boot whichever is reachable via fall-through).
3. Power on. SD boots.
4. From the SD system, change `BOOT_ORDER` back to `0xf416` so the
   system doesn't try NVMe again on reboot.

## 10. Post-migration un-defer list

Once §8 green-light criteria are met **and** the system has survived
one clean reboot:

- `docs/backlog.md` → `TIER-CALIBRATION-EXPERIMENT` entry condition
  #1 is now satisfied. Move to un-defer after confirming conditions
  #2–4.
- `docs/backlog.md` → `OVERNIGHT-QUEUE` deferred pieces entry
  condition #2 (pi5 accessible post-migration) is satisfied. Pick
  up when the tier experiment's clean-pause window allows.

Strike through both with commit SHAs in `backlog.md` when they land.
