#!/usr/bin/env python3
"""
uninstall.py — Synthos Clean Uninstaller
Synthos Resurgens LLC

Removes all Synthos/Quorum files from the Pi and restores it to a clean state.
Safe to run at any time. Always prompts before deleting anything.

Usage:
  python3 uninstall.py              # interactive — prompts for confirmation
  python3 uninstall.py --full       # remove everything including backups
  python3 uninstall.py --dry-run    # show what would be removed, touch nothing
  python3 uninstall.py --force      # no prompts (for automated testing only)

What it removes:
  - /home/pi/synthos/     (all agent files, logs, DB)
  - /home/pi/quorum/      (legacy name — if present)
  - Crontab entries for synthos/quorum
  - 'install' and 'synthos' system commands
  - Optionally: /home/pi/backups/ (database backups)

What it preserves:
  - Python packages (pip uninstall is slow and may break other things)
  - Your Alpaca account (we never touch that)
  - Your Anthropic credits (we never touch those)
  - All other files on the Pi
"""

import os
import sys
import shutil
import subprocess
import argparse
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────

INSTALL_DIRS = [
    '/home/pi/synthos',
    '/home/pi/quorum',          # legacy — remove if present
]

BACKUP_DIRS = [
    '/home/pi/backups',
    '/home/pi/synthos_backups',
    '/home/pi/quorum_backups',
]

SYSTEM_COMMANDS = [
    '/usr/local/bin/install',
    '/usr/local/bin/synthos',
    '/usr/local/bin/quorum',
]

# Cron patterns to remove
CRON_PATTERNS = [
    'synthos',
    'quorum',
    'trade_logic_agent',
    'news_agent',
    'market_sentiment_agent',
    'heartbeat.py',
    'cleanup.py',
    'shutdown.py',
    'boot_sequence.py',
    'health_check.py',
    'daily_digest.py',
    'portal.py',
    'watchdog.py',
]

# ── HELPERS ───────────────────────────────────────────────────────────────

def header(msg):
    print(f"\n{'='*55}")
    print(f"  {msg}")
    print(f"{'='*55}")

def step(msg):
    print(f"\n  → {msg}")

def ok(msg):
    print(f"    ✓ {msg}")

def skip(msg):
    print(f"    · {msg}")

def warn(msg):
    print(f"    ⚠ {msg}")

def err(msg):
    print(f"    ✗ {msg}")

def confirm(prompt, force=False):
    if force:
        return True
    resp = input(f"\n  {prompt} [y/N]: ").strip().lower()
    return resp == 'y'

def get_dir_size_mb(path):
    try:
        result = subprocess.run(['du', '-sm', path], capture_output=True, text=True)
        return result.stdout.split('\t')[0] if result.returncode == 0 else '?'
    except Exception:
        return '?'

# ── UNINSTALL STEPS ───────────────────────────────────────────────────────

def remove_cron_entries(dry_run=False):
    """Remove all Synthos/Quorum cron entries."""
    step("Removing cron entries")
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if result.returncode != 0:
            skip("No crontab found")
            return

        original_lines = result.stdout.splitlines(keepends=True)
        kept_lines = []
        removed = 0

        for line in original_lines:
            should_remove = any(p in line for p in CRON_PATTERNS)
            if should_remove:
                if dry_run:
                    ok(f"[DRY RUN] Would remove: {line.strip()[:70]}")
                else:
                    ok(f"Removed: {line.strip()[:70]}")
                removed += 1
            else:
                kept_lines.append(line)

        if removed == 0:
            skip("No Synthos cron entries found")
            return

        if not dry_run:
            new_crontab = ''.join(kept_lines)
            proc = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE, text=True)
            proc.communicate(new_crontab)
            if proc.returncode == 0:
                ok(f"Crontab updated — removed {removed} entries")
            else:
                err("Failed to update crontab")

    except Exception as e:
        err(f"Cron removal error: {e}")


def remove_system_commands(dry_run=False):
    """Remove install/synthos/quorum system commands."""
    step("Removing system commands")
    found = False
    for cmd_path in SYSTEM_COMMANDS:
        if os.path.exists(cmd_path):
            found = True
            if dry_run:
                ok(f"[DRY RUN] Would remove: {cmd_path}")
            else:
                try:
                    os.remove(cmd_path)
                    ok(f"Removed: {cmd_path}")
                except PermissionError:
                    try:
                        subprocess.run(['sudo', 'rm', cmd_path], check=True)
                        ok(f"Removed (sudo): {cmd_path}")
                    except Exception as e:
                        err(f"Could not remove {cmd_path}: {e}")
    if not found:
        skip("No system commands found")


def backup_database(install_dir, dry_run=False):
    """
    Before deleting, offer to save the signals.db trade history.
    This is the only thing that might have sentimental/analytical value.
    """
    db_path = os.path.join(install_dir, 'signals.db')
    if not os.path.exists(db_path):
        return

    size_kb = round(os.path.getsize(db_path) / 1024, 1)
    print(f"\n  Your trade history database is {size_kb}KB.")
    print(f"  It contains all signals, positions, and outcomes from your paper trading.")

    if dry_run:
        ok("[DRY RUN] Would offer to save signals.db to ~/signals_backup.db")
        return

    if confirm("Save trade history to ~/signals_backup.db before deleting?"):
        dest = os.path.expanduser('~/signals_backup.db')
        try:
            shutil.copy2(db_path, dest)
            ok(f"Trade history saved to: {dest}")
        except Exception as e:
            err(f"Could not save database: {e}")
    else:
        ok("Trade history will be deleted with the install directory")


def remove_install_dirs(dry_run=False, full=False, force=False):
    """Remove the main install directories."""
    for install_dir in INSTALL_DIRS:
        if not os.path.exists(install_dir):
            skip(f"Not found: {install_dir}")
            continue

        size_mb = get_dir_size_mb(install_dir)
        step(f"Removing {install_dir} ({size_mb}MB)")

        # Offer to save DB first
        backup_database(install_dir, dry_run=dry_run)

        if dry_run:
            ok(f"[DRY RUN] Would remove: {install_dir}/")
            # List contents
            for item in sorted(os.listdir(install_dir))[:15]:
                ok(f"  {item}")
            return

        try:
            shutil.rmtree(install_dir)
            ok(f"Removed: {install_dir}/")
        except Exception as e:
            err(f"Could not remove {install_dir}: {e}")
            warn("Try: sudo python3 uninstall.py")


def remove_backup_dirs(dry_run=False):
    """Remove backup directories — only in --full mode."""
    for backup_dir in BACKUP_DIRS:
        if not os.path.exists(backup_dir):
            continue
        size_mb = get_dir_size_mb(backup_dir)
        step(f"Removing backups {backup_dir} ({size_mb}MB)")
        if dry_run:
            ok(f"[DRY RUN] Would remove: {backup_dir}/")
        else:
            try:
                shutil.rmtree(backup_dir)
                ok(f"Removed: {backup_dir}/")
            except Exception as e:
                err(f"Could not remove {backup_dir}: {e}")


def kill_running_processes(dry_run=False):
    """Kill any running Synthos/Quorum processes."""
    step("Stopping running processes")
    patterns = ['trade_logic_agent', 'news_agent', 'market_sentiment_agent',
                'watchdog.py', 'portal.py', 'heartbeat.py', 'daily_digest.py',
                'boot_sequence.py']
    found = False
    for pattern in patterns:
        try:
            result = subprocess.run(
                ['pgrep', '-f', pattern],
                capture_output=True, text=True
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                found = True
                for pid in pids:
                    if dry_run:
                        ok(f"[DRY RUN] Would kill PID {pid} ({pattern})")
                    else:
                        try:
                            subprocess.run(['kill', pid], check=True)
                            ok(f"Stopped PID {pid} ({pattern})")
                        except Exception:
                            try:
                                subprocess.run(['sudo', 'kill', '-9', pid])
                                ok(f"Force-stopped PID {pid} ({pattern})")
                            except Exception:
                                pass
        except Exception:
            pass
    if not found:
        skip("No running Synthos processes found")


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Synthos Uninstaller — removes all Synthos/Quorum files from this Pi'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be removed without touching anything')
    parser.add_argument('--full', action='store_true',
                        help='Also remove backup databases (irreversible)')
    parser.add_argument('--force', action='store_true',
                        help='Skip all confirmation prompts (use carefully)')
    args = parser.parse_args()

    header("SYNTHOS UNINSTALLER")
    print(f"  Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode:    {'DRY RUN — no changes will be made' if args.dry_run else 'LIVE'}")
    print(f"  Full:    {'Yes — backups will also be removed' if args.full else 'No — backups preserved'}")

    if not args.dry_run:
        print("""
  This will remove:
    • All Synthos/Quorum files and logs
    • Cron schedule entries
    • System commands (install, synthos, quorum)
    • Your signals.db trade history (you can save it first)

  This will NOT remove:
    • Python packages
    • Your Alpaca account or API keys
    • Your Anthropic account or credits
    • Any other files on this Pi
        """)

        if not args.force:
            if not confirm("Proceed with uninstall?", force=False):
                print("\n  Aborted — nothing changed.\n")
                sys.exit(0)

    print()

    # Step 1: Kill running processes
    kill_running_processes(dry_run=args.dry_run)

    # Step 2: Remove cron entries
    remove_cron_entries(dry_run=args.dry_run)

    # Step 3: Remove system commands
    remove_system_commands(dry_run=args.dry_run)

    # Step 4: Remove install directories (with optional DB save)
    remove_install_dirs(dry_run=args.dry_run, full=args.full, force=args.force)

    # Step 5: Remove backups if --full
    if args.full:
        if args.dry_run or confirm("Also remove all database backups? (irreversible)", force=args.force):
            remove_backup_dirs(dry_run=args.dry_run)

    # Done
    header("UNINSTALL COMPLETE")
    if args.dry_run:
        print("  DRY RUN complete — nothing was changed.")
        print("  Run without --dry-run to actually uninstall.")
    else:
        print("  Synthos has been removed from this Pi.")
        print()
        print("  To reinstall fresh:")
        print("    git clone https://github.com/personalprometheus-blip/synthos")
        print("    cd synthos && bash first_run.sh")
        print("    install")
        print()
        if os.path.exists(os.path.expanduser('~/signals_backup.db')):
            print("  Your trade history was saved to: ~/signals_backup.db")
    print()


if __name__ == '__main__':
    main()
