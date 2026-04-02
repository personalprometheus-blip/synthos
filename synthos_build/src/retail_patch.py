"""
retail_patch.py — Non-Volatile Update System for Synthos (retail node)
Synthos · Patch Manager v2.0

Safely updates agent and source files without touching:
  - signals.db     (trade history, positions, outcomes)
  - .env           (API keys)
  - logs/          (agent output history)
  - backups/       (database backups)

HOW IT WORKS:
  1. Backs up signals.db before anything changes
  2. Validates the new file (syntax check for .py files)
  3. Backs up the current file being replaced
  4. Replaces the file
  5. Runs a quick smoke test
  6. Rolls back automatically if anything fails

FILE LAYOUT:
  synthos_build/
    src/       ← source + runtime files (this file lives here)
    agents/    ← trading agents

USAGE:
  # Update a single file (auto-detects src/ vs agents/):
  python3 retail_patch.py --file retail_trade_logic_agent.py --source /path/to/new/file.py

  # Update multiple files from a directory:
  python3 retail_patch.py --dir /path/to/update/folder

  # Preview what would change without applying:
  python3 retail_patch.py --dir /path/to/update/folder --dry-run

  # Roll back to previous version:
  python3 retail_patch.py --rollback retail_trade_logic_agent.py

  # Show patch history:
  python3 retail_patch.py --history

  # Health check all three nodes:
  python3 retail_patch.py --check-nodes

  # Check GitHub for updates:
  python3 retail_patch.py --check-remote [--dry-run]
"""

SYNTHOS_VERSION = "2.0"

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

# ── PATHS ─────────────────────────────────────────────────────────────────
# retail_patch.py lives in synthos_build/src/
_SRC_DIR   = os.path.dirname(os.path.abspath(__file__))
_BUILD_DIR = os.path.dirname(_SRC_DIR)            # synthos_build/
_AGENT_DIR = os.path.join(_BUILD_DIR, 'agents')   # synthos_build/agents/

load_dotenv(os.path.join(_BUILD_DIR, 'user', '.env'))

PATCH_DIR  = os.path.join(_BUILD_DIR, '.patches')   # hidden patch history
DB_PATH    = os.path.join(_BUILD_DIR, 'user', 'signals.db')
BACKUP_DIR = os.path.join(_BUILD_DIR, 'backups')
LOG_DIR    = os.path.join(_BUILD_DIR, 'logs')
NODE_HEALTH_LOG = os.path.join(LOG_DIR, 'node_health.log')

# ── FILE MAP — filename → subdirectory relative to synthos_build/ ──────────
#
# Used for:
#   • Resolving the correct on-disk path for each patchable file
#   • Building the correct GitHub raw URL path
#
# src/   → runtime + infrastructure files
# agents/ → trading agent files

PATCHABLE_FILE_MAP = {
    # ── Trading agents ────────────────────────────────────────────────────
    'retail_trade_logic_agent.py':      'agents',
    'retail_news_agent.py':             'agents',
    'retail_market_sentiment_agent.py': 'agents',
    'retail_sector_screener.py':        'agents',

    # ── Source / runtime ──────────────────────────────────────────────────
    'retail_patch.py':                  'src',
    'retail_portal.py':                 'src',
    'retail_boot_sequence.py':          'src',
    'retail_health_check.py':           'src',
    'retail_heartbeat.py':              'src',
    'retail_shutdown.py':               'src',
    'retail_watchdog.py':               'src',
    'retail_scheduler.py':              'src',
    'retail_sync.py':                   'src',
    'retail_database.py':               'src',
    'retail_interrogation_listener.py': 'src',
    'synthos_monitor.py':               'src',
    'auth.py':                          'src',
    'database.py':                      'src',
    'uninstall.py':                     'src',
    'seed_backlog.py':                  'src',

    # ── Backup system ─────────────────────────────────────────────────────
    'retail_backup.py':                 'src',
    'strongbox.py':                     'src',

    # ── Company node (co-deployed on Pi 4B) ───────────────────────────────
    'company_server.py':                'src',
    'scoop.py':                         'src',
    'install_company.py':               'src',

    # ── Shell scripts ─────────────────────────────────────────────────────
    'first_run.sh':                     'src',
    'qpush.sh':                         'src',
    'qpull.sh':                         'src',
}

PATCHABLE_FILES = set(PATCHABLE_FILE_MAP.keys())

# Files that can NEVER be overwritten by the patcher
PROTECTED_FILES = {
    '.env',
    'credentials.json',
    'signals.db',
    '.kill_switch',
    '.pending_approvals.json',
    '.install_progress.json',
    '.company_install_complete',
}

# Directories that are never touched
PROTECTED_DIRS = {
    'logs',
    'backups',
    '.patches',
    'user',
}


# ── LOGGING ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s patch: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('patch')


# ── PATH RESOLUTION ───────────────────────────────────────────────────────

def resolve_local_path(filename: str) -> str:
    """Return the full on-disk path for a patchable file."""
    subdir = PATCHABLE_FILE_MAP.get(filename, 'src')
    return os.path.join(_BUILD_DIR, subdir, filename)


def github_subpath(filename: str) -> str:
    """
    Return the path within the repo for building a GitHub raw URL.
    e.g. 'retail_trade_logic_agent.py' → 'synthos_build/agents/retail_trade_logic_agent.py'
    """
    subdir = PATCHABLE_FILE_MAP.get(filename, 'src')
    return f"synthos_build/{subdir}/{filename}"


# ── HELPERS ───────────────────────────────────────────────────────────────

def now_str():
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def file_hash(path: str) -> str:
    """SHA-256 hash of a file for change detection."""
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_dirs():
    os.makedirs(PATCH_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def validate_python(path: str) -> tuple[bool, str | None]:
    """Syntax check a Python file before applying it."""
    with open(path, 'r') as f:
        source = f.read()
    try:
        ast.parse(source)
        return True, None
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"


def backup_database() -> str | None:
    """Backup signals.db before any patch operation."""
    if not os.path.exists(DB_PATH):
        log.info("No signals.db found — skipping DB backup (cold start)")
        return None

    try:
        conn   = sqlite3.connect(DB_PATH, timeout=10)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] != 'ok':
            log.error(f"Database integrity check FAILED before patch: {result[0]}")
            log.error("Fix the database before applying patches")
            return None
    except Exception as e:
        log.error(f"Could not check database integrity: {e}")
        return None

    ts     = now_str()
    backup = os.path.join(BACKUP_DIR, f'signals_pre_patch_{ts}.db')
    shutil.copy2(DB_PATH, backup)
    log.info(f"Database backed up: {os.path.basename(backup)}")
    return backup


def backup_file(filename: str) -> str | None:
    """Backup a code file before overwriting."""
    src = resolve_local_path(filename)
    if not os.path.exists(src):
        return None
    ts   = now_str()
    dest = os.path.join(PATCH_DIR, f'{filename}.{ts}.bak')
    shutil.copy2(src, dest)
    log.info(f"Code backup: {os.path.basename(dest)}")
    return dest


def restore_file(filename: str, backup_path: str):
    """Restore a file from its backup."""
    dest = resolve_local_path(filename)
    shutil.copy2(backup_path, dest)
    log.info(f"Restored: {filename} ← {os.path.basename(backup_path)}")


def log_patch_event(filename, action, old_hash, new_hash, backup_path, success, notes=""):
    """Write patch history to a simple log file."""
    log_file = os.path.join(PATCH_DIR, 'patch_history.log')
    entry = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"{action:10} | {filename:40} | "
        f"{'OK' if success else 'FAIL':4} | "
        f"old={old_hash[:8] if old_hash else 'new':8} | "
        f"new={new_hash[:8] if new_hash else 'none':8} | "
        f"backup={os.path.basename(backup_path) if backup_path else 'none'} | "
        f"{notes}\n"
    )
    with open(log_file, 'a') as f:
        f.write(entry)


def smoke_test() -> bool:
    """
    Quick smoke test after patching — verifies the database module still imports.
    Returns True if clean.
    """
    try:
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "."); '
             'from retail_database import DB; db = DB(); print(db.integrity_check())'],
            cwd=_SRC_DIR,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if 'True' in result.stdout:
            log.info("Smoke test: database module OK")
            return True
        else:
            log.error(f"Smoke test failed: {result.stderr or result.stdout}")
            return False
    except Exception as e:
        log.error(f"Smoke test error: {e}")
        return False


# ── PATCH OPERATIONS ──────────────────────────────────────────────────────

def patch_file(source_path: str, filename: str, dry_run: bool = False) -> bool:
    """
    Safely patch a single file.
    Returns True on success.
    """
    dest_path = resolve_local_path(filename)

    if filename in PROTECTED_FILES:
        log.error(f"BLOCKED: {filename} is a protected file — patcher will never touch it")
        return False

    if filename not in PATCHABLE_FILES:
        log.warning(f"WARNING: {filename} is not in the patchable files list")
        response = input(f"  Patch {filename} anyway? (yes/no): ").strip().lower()
        if response != 'yes':
            log.info(f"Skipped: {filename}")
            return False

    if not os.path.exists(source_path):
        log.error(f"Source file not found: {source_path}")
        return False

    if filename.endswith('.py'):
        valid, error = validate_python(source_path)
        if not valid:
            log.error(f"SYNTAX ERROR in {filename} — patch aborted: {error}")
            return False
        log.info(f"Syntax check: {filename} — CLEAN")

    old_hash = file_hash(dest_path) if os.path.exists(dest_path) else None
    new_hash = file_hash(source_path)

    if old_hash == new_hash:
        log.info(f"No change: {filename} is identical to current version")
        return True

    if dry_run:
        subdir = PATCHABLE_FILE_MAP.get(filename, 'src')
        log.info(f"[DRY RUN] Would patch: {subdir}/{filename}")
        if old_hash:
            log.info(f"  Current hash: {old_hash[:16]}...")
        log.info(f"  New hash:     {new_hash[:16]}...")
        return True

    backup_path = backup_file(filename)

    try:
        # Ensure the destination directory exists
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(source_path, dest_path)
        log.info(f"Patched: {filename}")
        log_patch_event(filename, 'PATCH', old_hash, new_hash, backup_path, True)
        return True
    except Exception as e:
        log.error(f"Failed to patch {filename}: {e}")
        if backup_path:
            restore_file(filename, backup_path)
            log.info(f"Auto-rolled back: {filename}")
        log_patch_event(filename, 'PATCH', old_hash, new_hash, backup_path, False, str(e))
        return False


def patch_directory(source_dir: str, dry_run: bool = False) -> bool:
    """
    Patch all eligible files found in a source directory.
    Searches both source_dir root and any src/ or agents/ subdirs.
    """
    source_path = Path(source_dir)
    if not source_path.exists():
        log.error(f"Source directory not found: {source_dir}")
        return False

    # Collect candidates from root + src/ + agents/ subdirs
    candidates = []
    search_dirs = [source_path]
    for sub in ('src', 'agents', 'synthos_build/src', 'synthos_build/agents'):
        d = source_path / sub
        if d.exists():
            search_dirs.append(d)

    seen = set()
    for d in search_dirs:
        for f in d.iterdir():
            if f.name in PATCHABLE_FILES and f.name not in seen:
                candidates.append(f)
                seen.add(f.name)

    if not candidates:
        log.info(f"No patchable files found in {source_dir}")
        log.info(f"Patchable set: {', '.join(sorted(PATCHABLE_FILES))}")
        return False

    log.info(f"Found {len(candidates)} file(s) to patch: {', '.join(f.name for f in candidates)}")

    if dry_run:
        log.info("[DRY RUN] No changes will be applied")

    if not dry_run:
        db_backup = backup_database()
        if db_backup is None and os.path.exists(DB_PATH):
            log.error("Database backup failed — aborting patch")
            return False

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

    if success_count > 0:
        if not smoke_test():
            log.error("Smoke test FAILED — rolling back all patches")
            for fname in rollback_files:
                latest_backup = get_latest_backup(fname)
                if latest_backup:
                    restore_file(fname, latest_backup)
                    log_patch_event(fname, 'ROLLBACK', None, None, latest_backup, True,
                                    "smoke test failed")
            log.info("Rollback complete — system restored to previous version")
            return False

    return fail_count == 0


def get_latest_backup(filename: str) -> str | None:
    """Find the most recent backup of a file."""
    patch_path = Path(PATCH_DIR)
    if not patch_path.exists():
        return None
    backups = sorted(patch_path.glob(f'{filename}.*.bak'), reverse=True)
    return str(backups[0]) if backups else None


def rollback_file(filename: str) -> bool:
    """Roll back a single file to its most recent backup."""
    if filename in PROTECTED_FILES:
        log.error(f"BLOCKED: {filename} is protected")
        return False

    backup_path = get_latest_backup(filename)
    if not backup_path:
        log.error(f"No backup found for {filename}")
        return False

    dest     = resolve_local_path(filename)
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

    print("\n" + "=" * 90)
    print("SYNTHOS PATCH HISTORY")
    print("=" * 90)

    with open(log_file, 'r') as f:
        lines = f.readlines()

    if filename:
        lines = [l for l in lines if filename in l]

    if not lines:
        print(f"No history for {filename}" if filename else "No history")
        return

    for line in lines[-50:]:
        parts = line.strip().split(' | ')
        if len(parts) >= 4:
            ts, action, fname, status = parts[0], parts[1].strip(), parts[2].strip(), parts[3].strip()
            status_str = "✓" if status == "OK" else "✗"
            print(f"  {status_str} {ts}  {action:10}  {fname:45}  {status}")

    print("=" * 90 + "\n")


def show_status():
    """Show which files are protected and which are patchable, with on-disk presence."""
    print("\n" + "=" * 70)
    print("SYNTHOS FILE PROTECTION STATUS")
    print("=" * 70)

    print("\nPROTECTED (never modified by patcher):")
    for f in sorted(PROTECTED_FILES):
        # Check in both src/ and build root
        exists = any(
            os.path.exists(os.path.join(d, f))
            for d in [_SRC_DIR, _BUILD_DIR, os.path.join(_BUILD_DIR, 'user')]
        )
        print(f"  {'✓' if exists else '○'} {f}")
    print(f"\n  Protected directories: {', '.join(sorted(PROTECTED_DIRS))}")

    print("\nPATCHABLE (can be updated):")
    for f in sorted(PATCHABLE_FILE_MAP.keys()):
        subdir = PATCHABLE_FILE_MAP[f]
        path   = resolve_local_path(f)
        exists = os.path.exists(path)
        h      = file_hash(path)[:12] if exists else "not found"
        print(f"  {'✓' if exists else '○'} {subdir:7} {f:45} {h}...")

    print("=" * 70 + "\n")


# ── CROSS-NODE HEALTH CHECK ───────────────────────────────────────────────

# Retail agents that are expected to heartbeat into signals.db.
# Maps agent_name (as stored in system_log) → display label
RETAIL_AGENTS = {
    'trade_logic_agent':      'Trade Logic',
    'news_agent':             'News',
    'market_sentiment_agent': 'Market Sentiment',
}

# Stale threshold in seconds for agent heartbeat warnings
_HB_WARN_S  = 2 * 3600   # > 2 h  → warn (yellow)
_HB_DEAD_S  = 6 * 3600   # > 6 h  → dead (red)


def _fmt_age(seconds: float) -> str:
    """Human-readable age string from seconds."""
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _http_get(url: str, headers: dict | None = None, timeout: int = 6) -> tuple[dict | None, str | None]:
    """
    Simple HTTP GET returning (json_dict, error_str).
    No external dependencies — uses urllib only.
    """
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json_loads(r.read().decode('utf-8')), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as exc:
        return None, str(exc)


def _check_retail_node() -> list[str]:
    """
    Audit the local retail node. Returns a list of formatted status lines.
    Checks: file presence, Python syntax, DB integrity, agent heartbeats,
    and portal liveness.
    """
    lines = []
    TICK, WARN, DEAD = "✓", "!", "✗"

    # ── File presence + syntax ────────────────────────────────────────────
    missing  = []
    bad_syn  = []
    present  = 0
    for fname, subdir in PATCHABLE_FILE_MAP.items():
        path = resolve_local_path(fname)
        if not os.path.exists(path):
            missing.append(f"{subdir}/{fname}")
            continue
        present += 1
        if fname.endswith('.py'):
            ok, err = validate_python(path)
            if not ok:
                bad_syn.append(f"{fname}: {err}")

    total = len(PATCHABLE_FILE_MAP)
    file_icon = TICK if not missing else DEAD
    lines.append(f"  {file_icon} Files: {present}/{total} present"
                 + (f"  — missing: {', '.join(missing)}" if missing else ""))
    for e in bad_syn:
        lines.append(f"  {DEAD} Syntax error — {e}")

    # ── Database integrity ────────────────────────────────────────────────
    if os.path.exists(DB_PATH):
        try:
            size_mb = os.path.getsize(DB_PATH) / 1_048_576
            conn    = sqlite3.connect(DB_PATH, timeout=5)
            result  = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            ok_str  = result[0] if result else "no result"
            icon    = TICK if ok_str == "ok" else DEAD
            lines.append(f"  {icon} Database: {ok_str} (signals.db {size_mb:.1f} MB)")
        except Exception as exc:
            lines.append(f"  {DEAD} Database: could not check — {exc}")
    else:
        lines.append(f"  {WARN} Database: signals.db not found (cold start or wrong path)")

    # ── Agent heartbeats ──────────────────────────────────────────────────
    lines.append("  Agent heartbeats (from signals.db):")
    now_ts = datetime.utcnow()
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.row_factory = sqlite3.Row
            for agent_key, label in RETAIL_AGENTS.items():
                row = conn.execute("""
                    SELECT timestamp FROM system_log
                    WHERE event='HEARTBEAT' AND agent=?
                    ORDER BY timestamp DESC LIMIT 1
                """, (agent_key,)).fetchone()
                if not row:
                    lines.append(f"    {WARN}  {label:<22} never seen (not yet deployed?)")
                    continue
                try:
                    hb_ts   = datetime.fromisoformat(str(row['timestamp']).replace('Z', ''))
                    age_s   = (now_ts - hb_ts).total_seconds()
                    age_str = _fmt_age(age_s)
                    if age_s > _HB_DEAD_S:
                        icon = DEAD
                        note = "DEAD"
                    elif age_s > _HB_WARN_S:
                        icon = WARN
                        note = "stale"
                    else:
                        icon = TICK
                        note = ""
                    lines.append(f"    {icon}  {label:<22} last seen {age_str} ago"
                                 + (f"  ({note})" if note else ""))
                except Exception as e:
                    lines.append(f"    {WARN}  {label:<22} timestamp parse error: {e}")
            conn.close()
        except Exception as exc:
            lines.append(f"    {WARN} Could not read heartbeats: {exc}")
    else:
        lines.append(f"    {WARN} Skipped — signals.db not found")

    # ── Portal liveness ───────────────────────────────────────────────────
    portal_port = os.environ.get('PORTAL_PORT', '5001')
    portal_url  = f"http://localhost:{portal_port}/health"
    data, err   = _http_get(portal_url, timeout=3)
    if err:
        lines.append(f"  {WARN} Portal ({portal_url}): {err}")
    else:
        lines.append(f"  {TICK} Portal (:{portal_port}): responding")

    return lines


def _check_monitor_node() -> list[str]:
    """
    Check the monitor node via HTTP GET {MONITOR_URL}/health.
    Returns a list of formatted status lines.
    """
    lines  = []
    TICK, WARN, DEAD = "✓", "!", "✗"
    url    = os.environ.get('MONITOR_URL', '').rstrip('/')
    token  = os.environ.get('MONITOR_TOKEN', '')

    if not url:
        lines.append(f"  {WARN} MONITOR_URL not set — skipping monitor node check")
        return lines

    data, err = _http_get(f"{url}/health", timeout=6)
    if err:
        lines.append(f"  {DEAD} Unreachable ({url}/health): {err}")
        return lines

    status    = data.get("status", "unknown")
    pi_count  = data.get("pi_count", 0)
    pis       = data.get("pis", [])

    icon = TICK if status == "ok" else DEAD
    lines.append(f"  {icon} Monitor online — {pi_count} Pi(s) in registry")

    for pi in pis:
        age_s   = pi.get("age_secs", 0)
        label   = pi.get("label") or pi.get("pi_id", "unknown")
        st      = pi.get("status", "unknown")
        age_str = _fmt_age(age_s)
        if age_s > _HB_DEAD_S:
            icon = DEAD
        elif age_s > _HB_WARN_S:
            icon = WARN
        else:
            icon = TICK
        lines.append(f"    {icon}  {label:<22} {st:<10} last hb {age_str} ago")

    if not pis:
        lines.append(f"    {WARN} No Pis currently registered")

    return lines


def _check_company_node() -> list[str]:
    """
    Check the company node via HTTP GET {COMPANY_URL}/health.
    Returns a list of formatted status lines.
    """
    lines = []
    TICK, WARN, DEAD = "✓", "!", "✗"
    url   = os.environ.get('COMPANY_URL', '').rstrip('/')

    if not url:
        lines.append(f"  {WARN} COMPANY_URL not set — skipping company node check")
        return lines

    data, err = _http_get(f"{url}/health", timeout=6)
    if err:
        lines.append(f"  {DEAD} Unreachable ({url}/health): {err}")
        return lines

    ok      = data.get("ok", False)
    queue   = data.get("queue", {})
    pending = queue.get("PENDING", 0)
    failed  = queue.get("FAILED", 0)
    sent    = queue.get("SENT", 0)
    skipped = queue.get("SKIPPED", 0)

    icon = TICK if ok else DEAD
    lines.append(f"  {icon} Company node online — Scoop queue: "
                 f"{pending} pending, {failed} failed, {sent} sent, {skipped} skipped")

    if pending > 0:
        lines.append(f"  {WARN} {pending} item(s) waiting in dispatch queue")
    if failed > 0:
        lines.append(f"  {DEAD} {failed} item(s) in FAILED state — manual retry may be needed")

    return lines


def check_all_nodes() -> bool:
    """
    Print a full health report across all three Synthos nodes:
      - Retail node (local): files, syntax, DB, agent heartbeats, portal
      - Monitor node (MONITOR_URL): Pi registry, heartbeat ages
      - Company node (COMPANY_URL): Scoop queue counts

    Output is written to both stdout and NODE_HEALTH_LOG (append mode).
    Returns True if no critical issues found.
    """
    import json as _json   # local import — avoid polluting module namespace

    sep    = "═" * 62
    now_et = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    issues = 0

    # Collect all output lines so we can write to both stdout and log file
    out_lines: list[str] = []

    def _emit(line: str = ""):
        out_lines.append(line)
        print(line)

    _emit(f"\n{sep}")
    _emit(f"  SYNTHOS NODE HEALTH — {now_et}")
    _emit(sep)

    # ── Retail node ───────────────────────────────────────────────────────
    _emit("\n  RETAIL NODE (local)")
    for line in _check_retail_node():
        if line.strip().startswith("✗"):
            issues += 1
        _emit(line)

    # ── Monitor node ──────────────────────────────────────────────────────
    monitor_url = os.environ.get('MONITOR_URL', '(not set)')
    _emit(f"\n  MONITOR NODE ({monitor_url})")
    for line in _check_monitor_node():
        if line.strip().startswith("✗"):
            issues += 1
        _emit(line)

    # ── Company node ──────────────────────────────────────────────────────
    company_url = os.environ.get('COMPANY_URL', '(not set)')
    _emit(f"\n  COMPANY NODE ({company_url})")
    for line in _check_company_node():
        if line.strip().startswith("✗"):
            issues += 1
        _emit(line)

    # ── Summary ───────────────────────────────────────────────────────────
    _emit(f"\n{sep}")
    if issues == 0:
        _emit("  ✓ All nodes healthy — no critical issues")
    else:
        _emit(f"  ✗ {issues} critical issue(s) found — review output above")
    _emit(f"{sep}\n")

    # ── Write to log file ─────────────────────────────────────────────────
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(NODE_HEALTH_LOG, 'a', encoding='utf-8') as lf:
            lf.write('\n'.join(out_lines) + '\n')
    except Exception as log_err:
        print(f"[patch] Warning: could not write node health log: {log_err}")

    return issues == 0


# helper used inside check_all_nodes — import json at module level below
import json as _json_mod

def json_loads(s: str) -> dict:
    return _json_mod.loads(s)


# ── GITHUB REMOTE UPDATE ──────────────────────────────────────────────────

GITHUB_REPO     = "personalprometheus-blip/synthos"
GITHUB_BRANCH   = "main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"


def get_github_token() -> str:
    """Read GitHub token from .env — never hardcoded, never committed."""
    load_dotenv(os.path.join(_BUILD_DIR, 'user', '.env'), override=True)
    return os.environ.get('GITHUB_TOKEN', '')


def github_request(url: str):
    """Make an authenticated GitHub request using token from .env."""
    token = get_github_token()
    req   = urllib.request.Request(url)
    if token:
        req.add_header('Authorization', f'token {token}')
    req.add_header('Accept', 'application/vnd.github.v3.raw')
    return urllib.request.urlopen(req, timeout=15)


def get_local_version() -> str:
    for fname in ['retail_patch.py', 'retail_database.py']:
        fpath = resolve_local_path(fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                for line in f:
                    if line.startswith('SYNTHOS_VERSION'):
                        val = line.split('=')[1].split('#')[0].strip()
                        return val.strip('"').strip("'")
    return 'unknown'


def download_file_from_github(filename: str) -> tuple[str | None, str | None]:
    """Download a file from GitHub using its repo-relative path."""
    subpath = github_subpath(filename)
    url     = f"{GITHUB_RAW_BASE}/{subpath}"
    try:
        with github_request(url) as r:
            return r.read().decode('utf-8'), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} ({url})"
    except Exception as e:
        return None, str(e)


def check_remote(dry_run: bool = False) -> bool:
    """
    Hash-based remote update — compares each patchable file against GitHub.
    Updates any file whose hash differs from the remote version.
    """
    log.info(f"Checking GitHub for updates — {GITHUB_REPO}@{GITHUB_BRANCH}")

    # Verify GitHub connectivity with a lightweight probe
    probe_file = 'retail_patch.py'
    _, err = download_file_from_github(probe_file)
    if err:
        log.error(f"Could not reach GitHub ({err}) — check internet connection")
        return False

    changed   = []
    unchanged = []
    missing   = []

    for filename in sorted(PATCHABLE_FILE_MAP.keys()):
        local_path     = resolve_local_path(filename)
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

    if not changed:
        log.info("✓ Already up to date — all files match GitHub")
        return True

    log.info(f"  Will update: {', '.join(f for f, _ in changed)}")

    if dry_run:
        log.info("[DRY RUN] No changes applied")
        return True

    db_backup = backup_database()
    if db_backup is None and os.path.exists(DB_PATH):
        log.error("Database backup failed — aborting")
        return False

    success_count = 0
    fail_count    = 0
    updated_files = []
    patch_self    = None   # retail_patch.py updated last

    def apply_update(fname, content):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            return patch_file(tmp_path, fname, dry_run=False)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # Pass 1 — all files except retail_patch.py (self-update last)
    for filename, remote_content in changed:
        if filename == 'retail_patch.py':
            patch_self = remote_content
            continue
        result = apply_update(filename, remote_content)
        if result:
            success_count += 1
            updated_files.append(filename)
        else:
            fail_count += 1

    # Pass 2 — self-update
    if patch_self is not None:
        log.info("Applying self-update: retail_patch.py")
        result = apply_update('retail_patch.py', patch_self)
        if result:
            success_count += 1
            updated_files.append('retail_patch.py')
            log.info("⚡ retail_patch.py updated — changes take effect on next run")
        else:
            fail_count += 1

    log.info(f"Update complete — {success_count} updated, {fail_count} failed, "
             f"{len(unchanged)} already current")
    if updated_files:
        log.info(f"  Updated: {', '.join(updated_files)}")

    if success_count > 0 and not smoke_test():
        log.error("Smoke test failed — rolling back")
        for fname in updated_files:
            latest = get_latest_backup(fname)
            if latest:
                restore_file(fname, latest)
        return False

    return fail_count == 0


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Synthos Patch Manager v2 — safely update code without touching trade data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 retail_patch.py --file retail_trade_logic_agent.py --source ~/downloads/retail_trade_logic_agent.py
  python3 retail_patch.py --dir ~/downloads/synthos-update/
  python3 retail_patch.py --dir ~/downloads/synthos-update/ --dry-run
  python3 retail_patch.py --rollback retail_trade_logic_agent.py
  python3 retail_patch.py --history
  python3 retail_patch.py --status
  python3 retail_patch.py --check-remote
  python3 retail_patch.py --check-remote --dry-run
        """
    )

    parser.add_argument('--file',         help='Filename to patch (e.g. retail_trade_logic_agent.py)')
    parser.add_argument('--source',       help='Path to the new version of the file')
    parser.add_argument('--dir',          help='Directory containing updated files')
    parser.add_argument('--dry-run',      action='store_true', help='Preview changes without applying')
    parser.add_argument('--rollback',     metavar='FILE',      help='Roll back FILE to previous version')
    parser.add_argument('--history',      action='store_true', help='Show patch history')
    parser.add_argument('--status',       action='store_true', help='Show file protection / patchable status')
    parser.add_argument('--check-remote', action='store_true', help='Fetch updates from GitHub and apply')
    parser.add_argument('--check-nodes',  action='store_true', help='Health check all three nodes (retail, monitor, company)')
    parser.add_argument('--version',      action='store_true', help='Show local version')

    args = parser.parse_args()
    ensure_dirs()

    if args.version:
        local = get_local_version()
        print(f"\nLocal version:  v{local}")
        print(f"GitHub repo:    {GITHUB_REPO}@{GITHUB_BRANCH}")
        print()

    elif args.check_nodes:
        success = check_all_nodes()
        sys.exit(0 if success else 1)

    elif args.check_remote:
        success = check_remote(dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    elif args.status:
        show_status()

    elif args.history:
        show_history(args.file)

    elif args.rollback:
        success = rollback_file(args.rollback)
        sys.exit(0 if success else 1)

    elif args.file and args.source:
        ensure_dirs()
        backup_database()
        success = patch_file(args.source, args.file, dry_run=args.dry_run)
        if success and not args.dry_run:
            smoke_test()
        sys.exit(0 if success else 1)

    elif args.dir:
        success = patch_directory(args.dir, dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    else:
        parser.print_help()
        print("\nQuick reference:")
        print("  Patch one file:    python3 retail_patch.py --file retail_trade_logic_agent.py --source /path/to/file")
        print("  Patch from dir:    python3 retail_patch.py --dir /path/to/updates/")
        print("  Preview changes:   python3 retail_patch.py --dir /path/to/updates/ --dry-run")
        print("  Roll back file:    python3 retail_patch.py --rollback retail_trade_logic_agent.py")
        print("  See history:       python3 retail_patch.py --history")
        print("  Check protection:  python3 retail_patch.py --status")
        print("  Node health:       python3 retail_patch.py --check-nodes")
        print("  GitHub update:     python3 retail_patch.py --check-remote")
        print("  Preview update:    python3 retail_patch.py --check-remote --dry-run")
        print()
