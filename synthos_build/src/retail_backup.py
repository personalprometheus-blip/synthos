"""
retail_backup.py — Retail Pi Backup Creator
============================================
Creates a compressed archive of all critical retail Pi data and POSTs it
to the Company Pi's /receive_backup endpoint for Strongbox to pick up.

What gets archived:
    data/auth.db              — customer authentication DB (PII, hashed passwords, encrypted Alpaca creds)
    data/customers/           — per-customer signals.db directories (trading history)
    user/signals.db           — default/admin trading DB (if exists)
    user/.env                 — API keys and configuration

Security:
    Archive is plain .tar.gz (NOT encrypted here — Strongbox encrypts it with
    BACKUP_ENCRYPTION_KEY before uploading to R2, so transit to company Pi
    is the only unencrypted leg. If COMPANY_URL is internal network, this is
    acceptable. For external transit, set up mTLS or a VPN.)

.env keys required:
    COMPANY_URL       — http://<company-pi-ip>:5010
    SECRET_TOKEN      — shared token (same as company_server SECRET_TOKEN)
    PI_ID             — unique identifier for this retail Pi

Usage:
    python3 retail_backup.py              # run backup and upload
    python3 retail_backup.py --dry-run    # create archive but don't upload
    python3 retail_backup.py --local      # create archive in backups/ only, no upload
"""

import os
import sys
import tarfile
import hashlib
import argparse
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── PATHS ──────────────────────────────────────────────────────────────────────
_SRC_DIR   = Path(__file__).resolve().parent   # synthos_build/src/
_BUILD_DIR = _SRC_DIR.parent                   # synthos_build/
_DATA_DIR  = _BUILD_DIR / "data"               # auth.db, customers/
_USER_DIR  = _BUILD_DIR / "user"               # .env, signals.db
_LOG_DIR   = _BUILD_DIR / "logs"
_BACKUP_DIR = _BUILD_DIR / "backups" / "staging"   # local staging output

load_dotenv(_USER_DIR / ".env", override=True)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
COMPANY_URL   = os.environ.get("COMPANY_URL", "").rstrip("/")
SECRET_TOKEN  = os.environ.get("SECRET_TOKEN", "")
PI_ID         = os.environ.get("PI_ID", "synthos-pi-1")
UPLOAD_TIMEOUT = 60   # seconds
LOCAL_RETENTION_DAYS = 7   # delete local copies older than this

# ── LOGGING ────────────────────────────────────────────────────────────────────
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s retail_backup: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    # FileHandler only.  Cron invokes this script with `>> backup.log
    # 2>&1` which already captures stdout+stderr into the same file, so
    # a StreamHandler(sys.stdout) here would duplicate every log line.
    # Uncaught tracebacks still reach the log via the cron redirect.
    handlers=[
        logging.FileHandler(_LOG_DIR / "retail_backup.log"),
    ],
)
log = logging.getLogger("retail_backup")

# ── ARCHIVE ─────────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def create_retail_archive(tmp_dir: Path) -> Path:
    """
    Bundle all critical retail Pi data into a .tar.gz archive.
    Returns the path to the created archive.
    Raises FileNotFoundError if no data files found at all.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = tmp_dir / f"synthos_backup_{PI_ID}_{date_str}.tar.gz"

    included = []
    with tarfile.open(archive_path, "w:gz") as tar:

        # 1. data/auth.db — customer authentication (most critical)
        auth_db = _DATA_DIR / "auth.db"
        if auth_db.exists():
            tar.add(str(auth_db), arcname="data/auth.db")
            included.append("data/auth.db")
        else:
            log.warning("data/auth.db not found — skipping (not yet initialised?)")

        # 2. data/customers/ — per-customer signals.db directories
        customers_dir = _DATA_DIR / "customers"
        if customers_dir.exists():
            cust_count = sum(1 for _ in customers_dir.iterdir() if _.is_dir())
            if cust_count > 0:
                tar.add(str(customers_dir), arcname="data/customers")
                included.append(f"data/customers/ ({cust_count} account(s))")
        else:
            log.info("data/customers/ not found — no customer accounts yet")

        # 3. user/signals.db — default/admin trading DB
        signals_db = _USER_DIR / "signals.db"
        if signals_db.exists():
            tar.add(str(signals_db), arcname="user/signals.db")
            included.append("user/signals.db")

        # 4. user/.env — API keys and config (encrypted at rest on R2)
        env_file = _USER_DIR / ".env"
        if env_file.exists():
            tar.add(str(env_file), arcname="user/.env")
            included.append("user/.env")

    if not included:
        archive_path.unlink(missing_ok=True)
        raise FileNotFoundError(
            "No data files found to archive — has the portal been run at least once?"
        )

    size_kb = archive_path.stat().st_size / 1024
    log.info("Archive created: %s (%.1f KB) — %s",
             archive_path.name, size_kb, ", ".join(included))
    return archive_path


# ── UPLOAD ─────────────────────────────────────────────────────────────────────

def upload_archive(archive_path: Path) -> bool:
    """
    POST the archive to the company Pi's /receive_backup endpoint.
    Returns True on success.
    """
    if not COMPANY_URL:
        log.error("COMPANY_URL not set in .env — cannot upload backup")
        return False
    if not SECRET_TOKEN:
        log.error("SECRET_TOKEN not set in .env — cannot authenticate to company Pi")
        return False

    url = f"{COMPANY_URL}/receive_backup"
    log.info("Uploading to %s ...", url)

    try:
        with open(archive_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"pi_id": PI_ID},
                files={"archive": (archive_path.name, fh, "application/gzip")},
                headers={"X-Token": SECRET_TOKEN},
                timeout=UPLOAD_TIMEOUT,
            )
        if resp.status_code == 200:
            log.info("Upload successful — %s", resp.json().get("staged", ""))
            return True
        else:
            log.error("Upload failed — HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
    except requests.Timeout:
        log.error("Upload timed out after %ds — company Pi may be offline", UPLOAD_TIMEOUT)
        return False
    except Exception as exc:
        log.error("Upload error: %s", exc)
        return False


def save_local_copy(archive_path: Path) -> Path:
    """Save a local copy of the archive in backups/staging/ with date-stamped name."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BACKUP_DIR / archive_path.name
    import shutil
    shutil.copy2(str(archive_path), str(dest))
    log.info("Local copy saved: %s", dest)
    return dest


def cleanup_old_local_copies(retention_days: int = LOCAL_RETENTION_DAYS) -> int:
    """Delete local backup copies older than retention_days. Returns count removed."""
    if not _BACKUP_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for f in _BACKUP_DIR.glob("synthos_backup_*.tar.gz*"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                log.info("Local retention: removed %s (mtime %s)",
                         f.name, mtime.isoformat()[:19])
                removed += 1
        except OSError as e:
            log.warning("Local retention: failed to remove %s: %s", f.name, e)
    if removed:
        log.info("Local retention: %d archive(s) older than %dd removed",
                 removed, retention_days)
    return removed


# ── MAIN ───────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, local_only: bool = False) -> bool:
    """
    Create archive and upload (or stage locally).
    Returns True if fully successful.
    """
    log.info("=== Retail backup started (pi_id=%s%s) ===",
             PI_ID, " [DRY RUN]" if dry_run else " [LOCAL]" if local_only else "")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive  = create_retail_archive(tmp_path)

            checksum = _sha256(archive)
            log.info("Archive SHA-256: %s", checksum)

            if dry_run:
                log.info("[DRY RUN] Would upload %s to %s/receive_backup",
                         archive.name, COMPANY_URL or "(COMPANY_URL not set)")
                log.info("=== Retail backup dry-run complete ===")
                return True

            local = save_local_copy(archive)
            cleanup_old_local_copies()

            if local_only:
                log.info("=== Retail backup complete — local only: %s ===", local)
                return True

            ok = upload_archive(archive)
            if ok:
                log.info("=== Retail backup complete — uploaded + local copy saved ===")
            else:
                log.warning(
                    "=== Retail backup PARTIAL — upload failed, local copy at %s ===",
                    local
                )
            return ok

    except FileNotFoundError as exc:
        log.error("Backup aborted: %s", exc)
        return False
    except Exception as exc:
        log.error("Unexpected error: %s", exc, exc_info=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="retail_backup.py — Retail Pi Backup Creator"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Create archive and log plan but don't upload")
    parser.add_argument("--local", action="store_true",
                        help="Save archive locally in backups/staging/ but don't upload")
    args = parser.parse_args()

    ok = run(dry_run=args.dry_run, local_only=args.local)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
