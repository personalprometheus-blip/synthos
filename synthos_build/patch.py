"""
patch.py — Non-Volatile Update System for Synthos
Synthos · Patch Manager

Safely updates agent code files without touching:
  - signals.db     (trade history, positions, outcomes)
  - .env           (API keys)
  - credentials.json (Google service account)
  - logs/          (agent output history)
  - backups/       (database backups)

HOW IT WORKS:
  1. Backs up signals.db before anything changes
  2. Validates the new file (syntax check for .py files)
  3. Backs up the current file being replaced
  4. Replaces the file
  5. Runs a quick smoke test
  6. Rolls back automatically if anything fails

USAGE:
  # Update a single file:
  python3 patch.py --file agent1_trader.py --source /path/to/new/agent1_trader.py

  # Update multiple files from a directory:
  python3 patch.py --dir /path/to/update/folder

  # Preview what would change without applying:
  python3 patch.py --dir /path/to/update/folder --dry-run

  # Roll back to previous version:
  python3 patch.py --rollback agent1_trader.py

  # Show patch history:
  python3 patch.py --history
"""


SYNTHOS_VERSION = "1.0"  # Synthos system version

import os
import sys
import ast
import shutil
import hashlib
import argparse
import sqlite3
import logging
import subprocess
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── CONFIG ────────────────────────────────────────────────────────────────
PROJECT_DIR  = os.path.dirname(os.path.abspath(__file__))
PATCH_DIR    = os.path.join(PROJECT_DIR, '.patches')   # hidden patch history
DB_PATH      = os.path.join(PROJECT_DIR, 'signals.db')
BACKUP_DIR   = os.path.join(PROJECT_DIR, 'backups')

# Files that can NEVER be overwritten by the patcher
PROTECTED_FILES = {
    '.env',
    'credentials.json',
    'signals.db',
    '.kill_switch',
    '.pending_approvals.json',
    '.install_progress.json',
}

# Directories that are never touched
PROTECTED_DIRS = {
    'logs',
    'backups',
    '.patches',
}

# Files the patcher is allowed to update
PATCHABLE_FILES = {
    'agent1_trader.py',
    'agent2_research.py',
    'agent3_sentiment.py',
    'database.py',
    'cleanup.py',
    'heartbeat.py',
    'health_check.py',
    'shutdown.py',
    'patch.py',
    'boot_sequence.py',
    'portal.py',
    'synthos_monitor.py',
    'generate_unlock_key.py',
    'daily_digest.py',
    'uninstall.py',
    'qpush.sh',
    'qpull.sh',
    'watchdog.py',
    'install.py',
    'sync.py',
    'first_run.sh',
    'README.md',
    'VERSION_MANIFEST.txt',
    'deadman_switch.md',
    'api_security.md',
    'pi_maintenance.md',
    'beta_agreement.md',
    'legal_documents.md',
}

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s patch: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('patch')


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_str():
    return datetime.now().strftime('%Y%m%d_%H%M%S')

def file_hash(path):
    """SHA256 hash of a file for change detection."""
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

def ensure_dirs():
    os.makedirs(PATCH_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

def validate_python(path):
    """Syntax check a Python file before applying it."""
    with open(path, 'r') as f:
        source = f.read()
    try:
        ast.parse(source)
        return True, None
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"

def backup_database():
    """Backup signals.db before any patch operation."""
    if not os.path.exists(DB_PATH):
        log.info("No signals.db found — skipping DB backup (cold start)")
        return None

    # Integrity check first
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] != 'ok':
            log.error(f"Database integrity check FAILED before patch: {result[0]}")
            log.error("Fix the database before applying patches")
            return None
    except Exception as e:
        log.error(f"Could not check database integrity: {e}")
        return None

    ts      = now_str()
    backup  = os.path.join(BACKUP_DIR, f'signals_pre_patch_{ts}.db')
    shutil.copy2(DB_PATH, backup)
    log.info(f"Database backed up: {os.path.basename(backup)}")
    return backup

def backup_file(filename):
    """Backup a code file before overwriting."""
    src  = os.path.join(PROJECT_DIR, filename)
    if not os.path.exists(src):
        return None

    ts   = now_str()
    dest = os.path.join(PATCH_DIR, f'{filename}.{ts}.bak')
    shutil.copy2(src, dest)
    log.info(f"Code backup: {os.path.basename(dest)}")
    return dest

def restore_file(filename, backup_path):
    """Restore a file from its backup."""
    dest = os.path.join(PROJECT_DIR, filename)
    shutil.copy2(backup_path, dest)
    log.info(f"Restored: {filename} ← {os.path.basename(backup_path)}")

def log_patch_event(filename, action, old_hash, new_hash, backup_path, success, notes=""):
    """Write patch history to a simple log file."""
    log_file = os.path.join(PATCH_DIR, 'patch_history.log')
    entry = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"{action:10} | {filename:30} | "
        f"{'OK' if success else 'FAIL':4} | "
        f"old={old_hash[:8] if old_hash else 'new':8} | "
        f"new={new_hash[:8] if new_hash else 'none':8} | "
        f"backup={os.path.basename(backup_path) if backup_path else 'none'} | "
        f"{notes}\n"
    )
    with open(log_file, 'a') as f:
        f.write(entry)

def smoke_test():
    """
    Quick smoke test after patching — verifies database module still works.
    Returns True if clean.
    """
    try:
        result = os.popen(f'cd {PROJECT_DIR} && python3 -c "from database import DB; db = DB(); print(db.integrity_check())" 2>&1').read().strip()
        if 'True' in result:
            log.info("Smoke test: database module OK")
            return True
        else:
            log.error(f"Smoke test failed: {result}")
            return False
    except Exception as e:
        log.error(f"Smoke test error: {e}")
        return False


# ── PATCH OPERATIONS ──────────────────────────────────────────────────────

def patch_file(source_path, filename, dry_run=False):
    """
    Safely patch a single file.
    Returns True on success.
    """
    dest_path = os.path.join(PROJECT_DIR, filename)

    # Safety checks
    if filename in PROTECTED_FILES:
        log.error(f"BLOCKED: {filename} is a protected file — patcher will never touch it")
        return False

    if filename not in PATCHABLE_FILES:
        log.warning(f"WARNING: {filename} is not in the known patchable files list")
        response = input(f"  Patch {filename} anyway? (yes/no): ").strip().lower()
        if response != 'yes':
            log.info(f"Skipped: {filename}")
            return False

    if not os.path.exists(source_path):
        log.error(f"Source file not found: {source_path}")
        return False

    # Validate Python syntax before doing anything
    if filename.endswith('.py'):
        valid, error = validate_python(source_path)
        if not valid:
            log.error(f"SYNTAX ERROR in {filename} — patch aborted: {error}")
            return False
        log.info(f"Syntax check: {filename} — CLEAN")

    # Check if file actually changed
    old_hash = file_hash(dest_path) if os.path.exists(dest_path) else None
    new_hash = file_hash(source_path)

    if old_hash == new_hash:
        log.info(f"No change: {filename} is identical to current version")
        return True

    if dry_run:
        log.info(f"[DRY RUN] Would patch: {filename}")
        if old_hash:
            log.info(f"  Current hash: {old_hash[:16]}...")
        log.info(f"  New hash:     {new_hash[:16]}...")
        return True

    # Backup current file
    backup_path = backup_file(filename)

    # Apply the patch
    try:
        shutil.copy2(source_path, dest_path)
        log.info(f"Patched: {filename}")
        log_patch_event(filename, 'PATCH', old_hash, new_hash, backup_path, True)
        return True
    except Exception as e:
        log.error(f"Failed to patch {filename}: {e}")
        # Restore from backup
        if backup_path:
            restore_file(filename, backup_path)
            log.info(f"Auto-rolled back: {filename}")
        log_patch_event(filename, 'PATCH', old_hash, new_hash, backup_path, False, str(e))
        return False


def patch_directory(source_dir, dry_run=False):
    """
    Patch all eligible files from a source directory.
    """
    source_path = Path(source_dir)
    if not source_path.exists():
        log.error(f"Source directory not found: {source_dir}")
        return False

    # Find patchable files in source directory
    candidates = []
    for f in source_path.iterdir():
        if f.name in PATCHABLE_FILES:
            candidates.append(f)

    if not candidates:
        log.info(f"No patchable files found in {source_dir}")
        log.info(f"Looking for: {', '.join(sorted(PATCHABLE_FILES))}")
        return False

    log.info(f"Found {len(candidates)} file(s) to patch: {', '.join(f.name for f in candidates)}")

    if dry_run:
        log.info("[DRY RUN] No changes will be applied")

    # Backup database before any changes
    if not dry_run:
        db_backup = backup_database()
        if db_backup is None and os.path.exists(DB_PATH):
            log.error("Database backup failed — aborting patch")
            return False

    # Track results
    success_count = 0
    fail_count    = 0
    rollback_files = []

    for f in candidates:
        result = patch_file(str(f), f.name, dry_run=dry_run)
        if result:
            success_count += 1
            if not dry_run:
                rollback_files.append(f.name)
        else:
            fail_count += 1

    if dry_run:
        log.info(f"[DRY RUN] Would patch {success_count} file(s)")
        return True

    log.info(f"Patch complete: {success_count} succeeded, {fail_count} failed")

    # Smoke test after all patches applied
    if success_count > 0:
        if not smoke_test():
            log.error("Smoke test FAILED — rolling back all patches")
            for fname in rollback_files:
                latest_backup = get_latest_backup(fname)
                if latest_backup:
                    restore_file(fname, latest_backup)
                    log_patch_event(fname, 'ROLLBACK', None, None, latest_backup, True, "smoke test failed")
            log.info("Rollback complete — system restored to previous version")
            return False

    return fail_count == 0


def get_latest_backup(filename):
    """Find the most recent backup of a file."""
    patch_path = Path(PATCH_DIR)
    if not patch_path.exists():
        return None
    backups = sorted(patch_path.glob(f'{filename}.*.bak'), reverse=True)
    return str(backups[0]) if backups else None


def rollback_file(filename):
    """Roll back a single file to its most recent backup."""
    if filename in PROTECTED_FILES:
        log.error(f"BLOCKED: {filename} is protected")
        return False

    backup_path = get_latest_backup(filename)
    if not backup_path:
        log.error(f"No backup found for {filename}")
        return False

    dest = os.path.join(PROJECT_DIR, filename)
    old_hash = file_hash(dest) if os.path.exists(dest) else None

    restore_file(filename, backup_path)
    new_hash = file_hash(dest)
    log_patch_event(filename, 'ROLLBACK', old_hash, new_hash, backup_path, True, "manual rollback")
    log.info(f"Rolled back: {filename}")
    return True


def show_history(filename=None):
    """Show patch history."""
    log_file = os.path.join(PATCH_DIR, 'patch_history.log')
    if not os.path.exists(log_file):
        log.info("No patch history found")
        return

    print("\n" + "="*80)
    print("SYNTHOS PATCH HISTORY")
    print("="*80)

    with open(log_file, 'r') as f:
        lines = f.readlines()

    if filename:
        lines = [l for l in lines if filename in l]

    if not lines:
        print(f"No history for {filename}" if filename else "No history")
        return

    for line in lines[-50:]:   # last 50 entries
        parts = line.strip().split(' | ')
        if len(parts) >= 4:
            ts, action, fname, status = parts[0], parts[1].strip(), parts[2].strip(), parts[3].strip()
            status_str = "✓" if status == "OK" else "✗"
            print(f"  {status_str} {ts}  {action:10}  {fname:30}  {status}")

    print("="*80 + "\n")


def show_protected():
    """Show which files are protected and which are patchable."""
    print("\n" + "="*60)
    print("SYNTHOS FILE PROTECTION STATUS")
    print("="*60)
    print("\nPROTECTED (never modified by patcher):")
    for f in sorted(PROTECTED_FILES):
        exists = "✓" if os.path.exists(os.path.join(PROJECT_DIR, f)) else "○"
        print(f"  {exists} {f}")
    print("\n  Protected directories: " + ", ".join(sorted(PROTECTED_DIRS)))

    print("\nPATCHABLE (can be updated):")
    for f in sorted(PATCHABLE_FILES):
        exists = "✓" if os.path.exists(os.path.join(PROJECT_DIR, f)) else "○"
        h = file_hash(os.path.join(PROJECT_DIR, f))[:12] if os.path.exists(os.path.join(PROJECT_DIR, f)) else "not found"
        print(f"  {exists} {f:35} {h}...")

    print("="*60 + "\n")




# ── REMOTE UPDATE (GITHUB) ────────────────────────────────────────────────

GITHUB_REPO     = "personalprometheus-blip/synthos"
GITHUB_BRANCH   = "main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"

def get_github_token():
    """Read GitHub token from .env — never hardcoded, never committed."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, '.env'), override=True)
    return os.environ.get('GITHUB_TOKEN', '')

def github_request(url):
    """Make an authenticated GitHub request using token from .env."""
    token = get_github_token()
    req   = urllib.request.Request(url)
    if token:
        req.add_header('Authorization', f'token {token}')
    req.add_header('Accept', 'application/vnd.github.v3.raw')
    return urllib.request.urlopen(req, timeout=15)


def get_local_version():
    for fname in ["database.py", "patch.py"]:
        fpath = os.path.join(PROJECT_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, "r") as f:
                for line in f:
                    if line.startswith("SYNTHOS_VERSION"):
                        val = line.split("=")[1].split("#")[0].strip()
                        return val.strip('"').strip("'")
    return "unknown"


def get_remote_version():
    url = f"{GITHUB_RAW_BASE}/VERSION_MANIFEST.txt"
    try:
        with github_request(url) as r:
            text = r.read().decode("utf-8")
        for line in text.split("\n"):
            if line.startswith("System Version:"):
                return line.split(":")[1].strip(), text
        return None, text
    except Exception as e:
        log.error(f"Could not fetch remote version: {e}")
        return None, None


def download_file_from_github(filename):
    url = f"{GITHUB_RAW_BASE}/{filename}"
    try:
        with github_request(url) as r:
            return r.read().decode("utf-8"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def check_remote(dry_run=False):
    """
    Hash-based remote update — compares each file against GitHub.
    Updates any file whose hash differs, regardless of version number.
    """
    log.info(f"Checking GitHub for updates — {GITHUB_REPO}@{GITHUB_BRANCH}")

    # Verify GitHub is reachable
    _, manifest = get_remote_version()
    if not manifest:
        log.error("Could not reach GitHub — check internet connection")
        return False

    # Scan each file for changes
    changed   = []
    unchanged = []
    missing   = []

    for filename in sorted(PATCHABLE_FILES):
        local_path = os.path.join(PROJECT_DIR, filename)
        remote_content, error = download_file_from_github(filename)
        if error:
            log.warning(f"  Could not fetch {filename}: {error}")
            missing.append(filename)
            continue

        remote_hash = hashlib.sha256(remote_content.encode('utf-8')).hexdigest()

        if os.path.exists(local_path):
            local_hash = file_hash(local_path)
            if local_hash == remote_hash:
                unchanged.append(filename)
                continue

        changed.append((filename, remote_content))

    log.info(f"Scanned {len(changed) + len(unchanged) + len(missing)} files:")
    log.info(f"  Changed:     {len(changed)}")
    log.info(f"  Unchanged:   {len(unchanged)}")
    log.info(f"  Unreachable: {len(missing)}")
    if unchanged:
        log.info(f"  Up to date:  {', '.join(unchanged)}")

    if not changed:
        log.info("✓ Already up to date — all files match GitHub")
        return True

    log.info(f"  Will update: {', '.join(f for f, _ in changed)}")

    if dry_run:
        log.info("[DRY RUN] No changes applied")
        return True

    # Backup database before applying changes
    db_backup = backup_database()
    if db_backup is None and os.path.exists(DB_PATH):
        log.error("Database backup failed — aborting")
        return False

    success_count = 0
    fail_count    = 0
    updated_files = []
    patch_self    = None

    def apply_update(filename, remote_content):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(remote_content)
            tmp_path = tmp.name
        try:
            return patch_file(tmp_path, filename, dry_run=False)
        finally:
            os.unlink(tmp_path)

    # Pass 1 — all files except patch.py
    for filename, remote_content in changed:
        if filename == 'patch.py':
            patch_self = remote_content
            continue
        result = apply_update(filename, remote_content)
        if result:
            success_count += 1
            updated_files.append(filename)
        else:
            fail_count += 1

    # Pass 2 — patch.py last
    if patch_self is not None:
        log.info("Processing patch.py last (self-update)")
        result = apply_update('patch.py', patch_self)
        if result:
            success_count += 1
            updated_files.append('patch.py')
            log.info("⚡ patch.py updated — changes take effect on next run")
        else:
            fail_count += 1

    log.info(f"Update complete — {success_count} updated, {fail_count} failed, {len(unchanged)} already current")
    if updated_files:
        log.info(f"  Updated: {', '.join(updated_files)}")

    # Update VERSION_MANIFEST
    manifest_content, err = download_file_from_github("VERSION_MANIFEST.txt")
    if manifest_content:
        with open(os.path.join(PROJECT_DIR, "VERSION_MANIFEST.txt"), "w") as f:
            f.write(manifest_content)

    # Smoke test
    if success_count > 0:
        if not smoke_test():
            log.error("Smoke test failed — rolling back")
            for fname in updated_files:
                latest = get_latest_backup(fname)
                if latest:
                    restore_file(fname, latest)
            return False

    return fail_count == 0


def push_to_github(commit_message=None):
    if not commit_message:
        commit_message = f"Synthos v{get_local_version()} — update"
    try:
        files_to_add = [f for f in PATCHABLE_FILES
                        if os.path.exists(os.path.join(PROJECT_DIR, f))]
        files_to_add += [".gitignore", "VERSION_MANIFEST.txt"]
        subprocess.run(["git", "add"] + files_to_add,
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=PROJECT_DIR, capture_output=True, text=True)
        if not status.stdout.strip():
            log.info("Nothing to push — no changes detected")
            return True
        subprocess.run(["git", "commit", "-m", commit_message],
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        result = subprocess.run(["git", "push", "origin", "main"],
                                cwd=PROJECT_DIR, capture_output=True, text=True)
        if result.returncode == 0:
            log.info(f"Pushed: {commit_message}")
            return True
        else:
            log.error(f"Push failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"Push error: {e}")
        return False

# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Synthos Patch Manager — safely update code without touching trade data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 patch.py --file agent1_trader.py --source ~/downloads/agent1_trader.py
  python3 patch.py --dir ~/downloads/synthos-update/
  python3 patch.py --dir ~/downloads/synthos-update/ --dry-run
  python3 patch.py --rollback agent1_trader.py
  python3 patch.py --history
  python3 patch.py --status
        """
    )

    parser.add_argument('--file',     help='Filename to patch (e.g. agent1_trader.py)')
    parser.add_argument('--source',   help='Path to the new version of the file')
    parser.add_argument('--dir',      help='Directory containing updated files')
    parser.add_argument('--dry-run',  action='store_true', help='Preview changes without applying')
    parser.add_argument('--rollback', metavar='FILE', help='Roll back FILE to previous version')
    parser.add_argument('--history',  action='store_true', help='Show patch history')
    parser.add_argument('--status',        action='store_true', help='Show file protection status')
    parser.add_argument('--check-remote',  action='store_true', help='Check GitHub for updates and apply')
    parser.add_argument('--push',          metavar='MSG', nargs='?', const='auto', help='Push to GitHub (Developer only)')
    parser.add_argument('--version',       action='store_true', help='Show local and remote version')

    args = parser.parse_args()
    ensure_dirs()

    if args.version:
        local  = get_local_version()
        remote, _ = get_remote_version()
        print(f"\nLocal:  v{local}")
        print(f"Remote: v{remote or 'unreachable'}")
        print(f"Status: {'Up to date ✓' if local == remote else 'Update available — run --check-remote'}")
        print()

    elif args.check_remote:
        success = check_remote(dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    elif args.push is not None:
        msg = None if args.push == 'auto' else args.push
        success = push_to_github(commit_message=msg)
        sys.exit(0 if success else 1)

    elif args.status:
        show_protected()

    elif args.history:
        show_history(args.file)

    elif args.rollback:
        success = rollback_file(args.rollback)
        sys.exit(0 if success else 1)

    elif args.file and args.source:
        db_backup = backup_database()
        success   = patch_file(args.source, args.file, dry_run=args.dry_run)
        if success and not args.dry_run:
            smoke_test()
        sys.exit(0 if success else 1)

    elif args.dir:
        success = patch_directory(args.dir, dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    else:
        parser.print_help()
        print("\nQuick reference:")
        print("  Update one file:   python3 patch.py --file agent1_trader.py --source /path/to/new/agent1_trader.py")
        print("  Update from dir:   python3 patch.py --dir /path/to/updates/")
        print("  Preview changes:   python3 patch.py --dir /path/to/updates/ --dry-run")
        print("  Roll back file:    python3 patch.py --rollback agent1_trader.py")
        print("  See history:       python3 patch.py --history")
        print("  Check protection:  python3 patch.py --status")
        print("  Check for updates: python3 patch.py --check-remote")
        print("  Preview update:    python3 patch.py --check-remote --dry-run")
        print("  Push to GitHub:    python3 patch.py --push \'v1.1 — description\'")
        print("  Check version:     python3 patch.py --version")
