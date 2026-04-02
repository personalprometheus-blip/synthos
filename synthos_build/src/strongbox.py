"""
strongbox.py — Agent 12: Backup Manager
Company Pi only. Run via cron daily at 2am ET.

Responsibilities:
  - Compress, encrypt, and upload company.db to Cloudflare R2
  - Process staged retail Pi backup archives and upload to R2
  - Enforce 30-day rolling retention on R2
  - Verify backup integrity via checksum spot-check
  - Report health to data/backup_status.json
  - Alert Scoop via scoop_trigger.json on CRITICAL failures

Does NOT:
  - Send email or SMS directly (Scoop owns delivery)
  - Generate or revoke license keys (company_vault.py owns this)
  - Make compliance decisions (company_vault.py owns this)

Retail Pi backup staging:
  Retail Pis deposit compressed archives to
  .backup_staging/<pi_id>/<filename>.tar.gz via the company Pi's
  /receive_backup endpoint (company_server.py).
  Strongbox picks them up at 2am and uploads to R2.

  T-14 NOTE: Session-end backup triggering (getting retail Pis to deposit
  immediately after market close) is deferred. The daily 2am schedule is
  sufficient for Phase 1. When implemented, synthos_heartbeat.py will
  include a backup payload on its POST to the company Pi endpoint.

R2 key format:
  backup/{pi_id}/{YYYY-MM-DD}/synthos_backup_{pi_id}_{YYYY-MM-DD}.tar.gz.enc

Usage:
  python3 strongbox.py              # run full daily cycle
  python3 strongbox.py --status     # print last backup status and exit
  python3 strongbox.py --verify     # spot-check most recent backup per Pi
  python3 strongbox.py --restore <pi_id> [--date YYYY-MM-DD]
  python3 strongbox.py --dry-run    # plan without uploading or deleting

.env keys (company Pi only):
  R2_ACCOUNT_ID          Cloudflare account ID
  R2_ACCESS_KEY_ID       R2 API token (read + write on backup bucket)
  R2_SECRET_ACCESS_KEY   R2 API secret
  R2_BUCKET_NAME         R2 bucket name (default: synthos-backups)
  BACKUP_ENCRYPTION_KEY  Base64 Fernet key — project lead holds master copy
  COMPANY_DB             Override path to company.db (optional)
"""

import os
import sys
import json
import tarfile
import hashlib
import logging
import argparse
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


# ── PATH RESOLUTION ───────────────────────────────────────────────────────────
# strongbox.py lives in synthos_build/src/
_SRC_DIR     = Path(__file__).resolve().parent          # synthos_build/src/
_BUILD_DIR   = _SRC_DIR.parent                          # synthos_build/
DATA_DIR     = _SRC_DIR / "data"                        # company node data (matches company_server.py)
LOG_DIR      = _BUILD_DIR / "logs"                      # synthos_build/logs/
USER_DIR     = _BUILD_DIR / "user"                      # synthos_build/user/
STAGING_DIR  = _BUILD_DIR / ".backup_staging"           # synthos_build/.backup_staging/

BACKUP_STATUS_FILE = DATA_DIR / "backup_status.json"

load_dotenv(USER_DIR / ".env", override=True)

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
RETENTION_DAYS     = 30
R2_BUCKET          = os.environ.get("R2_BUCKET_NAME", "synthos-backups")
R2_ACCOUNT_ID      = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY      = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY      = os.environ.get("R2_SECRET_ACCESS_KEY", "")
ENCRYPTION_KEY_B64 = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
# Hours before a missing or outdated backup triggers a CRITICAL alert.
# Override with STRONGBOX_STALE_HOURS in .env (default: 48h).
STALE_HOURS        = int(os.environ.get("STRONGBOX_STALE_HOURS", "48"))

COMPANY_DB    = Path(os.environ.get("COMPANY_DB", str(DATA_DIR / "company.db")))
COMPANY_PI_ID = "company-pi"

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s strongbox: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "strongbox.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("strongbox")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2."""
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY]):
        raise EnvironmentError(
            "R2 credentials missing — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY in .env"
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def _fernet() -> Fernet:
    """Return a Fernet instance using BACKUP_ENCRYPTION_KEY from .env."""
    if not ENCRYPTION_KEY_B64:
        raise EnvironmentError(
            "BACKUP_ENCRYPTION_KEY not set in .env — "
            "generate one with: python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(ENCRYPTION_KEY_B64.encode())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _r2_object_key(pi_id: str, date_str: str) -> str:
    """S3 object key (no bucket prefix)."""
    return f"{pi_id}/{date_str}/synthos_backup_{pi_id}_{date_str}.tar.gz.enc"


def _load_status() -> dict:
    if BACKUP_STATUS_FILE.exists():
        try:
            return json.loads(BACKUP_STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_status(status: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_STATUS_FILE.write_text(json.dumps(status, indent=2))


def _alert_scoop(alert_type: str, pi_id: str, message: str) -> None:
    """
    Insert a P0 CRITICAL alert directly into scoop_queue in company.db so that
    the Scoop daemon picks it up and dispatches via Resend.

    Strongbox never sends email directly — all delivery is Scoop's job.
    All backup alerts are P0 (critical) so they bypass MIN_PRIORITY filtering
    and are dispatched immediately on the next Scoop tick.
    """
    import uuid as _uuid
    import sqlite3 as _sqlite3

    subject = f"[Synthos] CRITICAL: {alert_type.replace('_', ' ').title()} — {pi_id}"
    body    = (
        f"Strongbox backup alert\n\n"
        f"Type:    {alert_type}\n"
        f"Pi:      {pi_id}\n"
        f"Message: {message}\n"
        f"Time:    {_now_utc().isoformat()}\n\n"
        f"Check strongbox.log for details."
    )
    payload = json.dumps({"alert_type": alert_type, "pi_id": pi_id})
    now     = _now_utc().isoformat()
    item_id = str(_uuid.uuid4())

    try:
        conn = _sqlite3.connect(str(COMPANY_DB), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO scoop_queue
               (id, event_type, priority, subject, body,
                source_agent, pi_id, payload, status,
                queued_at, dispatch_attempts)
               VALUES (?, ?, 0, ?, ?, 'strongbox', ?, ?, 'pending', ?, 0)""",
            (item_id, alert_type, subject, body, pi_id, payload, now),
        )
        conn.commit()
        conn.close()
        log.warning("Scoop alert queued (P0): [%s] %s — %s", alert_type, pi_id, message)
    except Exception as exc:
        # Last-resort: if we can't reach the DB, at least it's in the log
        log.error("Failed to queue Scoop alert (DB unavailable): %s — %s", alert_type, exc)


# ── CORE OPERATIONS ──────────────────────────────────────────────────────────

def _create_company_archive(tmp_dir: Path) -> Path:
    """
    Create a compressed tar archive of company.db and return its path.
    The archive is NOT encrypted here — encryption happens in _encrypt_archive.

    For single-Pi deployments where retail data is co-located, also includes
    data/auth.db and data/customers/ if present.
    """
    if not COMPANY_DB.exists():
        raise FileNotFoundError(f"company.db not found at {COMPANY_DB}")

    date_str = _now_utc().strftime("%Y-%m-%d")
    archive_path = tmp_dir / f"synthos_backup_{COMPANY_PI_ID}_{date_str}.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(COMPANY_DB, arcname="data/company.db")
        # user/ — contains .env (API keys, COMPANY_MODE flag) and settings.json.
        # Required by restore.sh step c: "Restores .env from encrypted backup."
        if USER_DIR.exists():
            tar.add(USER_DIR, arcname="user")
        # data/auth.db — customer auth database
        auth_db = _BUILD_DIR / "data" / "auth.db"
        if auth_db.exists():
            tar.add(str(auth_db), arcname="data/auth.db")
        # data/customers/ — per-customer signals.db directories
        customers_dir = _BUILD_DIR / "data" / "customers"
        if customers_dir.exists() and any(customers_dir.iterdir()):
            tar.add(str(customers_dir), arcname="data/customers")

    log.info("Company archive created: %s (%.1f KB)",
             archive_path.name, archive_path.stat().st_size / 1024)
    return archive_path


def _create_retail_archive_list() -> list[tuple[str, Path]]:
    """
    Return a list of (pi_id, archive_path) tuples from the staging directory.
    Each retail Pi deposits a single archive to .backup_staging/<pi_id>/.
    """
    results = []
    if not STAGING_DIR.exists():
        return results

    for pi_dir in sorted(STAGING_DIR.iterdir()):
        if not pi_dir.is_dir():
            continue
        archives = sorted(pi_dir.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
        if archives:
            # Take the most recent archive for this Pi
            results.append((pi_dir.name, archives[-1]))
            if len(archives) > 1:
                log.warning("Multiple archives for %s — using most recent, "
                            "removing stale: %s", pi_dir.name,
                            [a.name for a in archives[:-1]])
                for stale in archives[:-1]:
                    stale.unlink()
    return results


def _encrypt_archive(archive_path: Path, tmp_dir: Path) -> tuple[Path, str]:
    """
    Encrypt archive_path with Fernet. Returns (encrypted_path, sha256_of_encrypted).
    """
    f = _fernet()
    plaintext = archive_path.read_bytes()
    ciphertext = f.encrypt(plaintext)

    enc_path = tmp_dir / (archive_path.name + ".enc")
    enc_path.write_bytes(ciphertext)

    checksum = hashlib.sha256(ciphertext).hexdigest()
    log.info("Encrypted: %s → %s (sha256: %s...)",
             archive_path.name, enc_path.name, checksum[:16])
    return enc_path, checksum


def _upload_to_r2(
    enc_path: Path,
    pi_id: str,
    date_str: str,
    dry_run: bool = False,
) -> str:
    """
    Upload encrypted archive to R2. Returns the R2 object key.
    """
    object_key = _r2_object_key(pi_id, date_str)

    if dry_run:
        log.info("[DRY RUN] Would upload %s → s3://%s/%s",
                 enc_path.name, R2_BUCKET, object_key)
        return object_key

    client = _r2_client()
    client.upload_file(str(enc_path), R2_BUCKET, object_key)
    log.info("Uploaded → s3://%s/%s", R2_BUCKET, object_key)
    return object_key


def _backup_single(pi_id: str, archive_path: Path, dry_run: bool = False) -> dict:
    """
    Encrypt and upload one archive. Returns a status record.
    """
    date_str = _now_utc().strftime("%Y-%m-%d")
    status = {
        "pi_id":        pi_id,
        "date":         date_str,
        "last_backup":  None,
        "r2_key":       None,
        "checksum":     None,
        "size_bytes":   None,
        "outcome":      "failed",
        "error":        None,
    }

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            enc_path, checksum = _encrypt_archive(archive_path, tmp_path)
            object_key = _upload_to_r2(enc_path, pi_id, date_str, dry_run)

            status.update({
                "last_backup": _now_utc().isoformat(),
                "r2_key":      object_key,
                "checksum":    checksum,
                "size_bytes":  enc_path.stat().st_size,
                "outcome":     "success" if not dry_run else "dry_run",
            })
            log.info("[%s] Backup %s — %.1f KB",
                     pi_id, status["outcome"], (status["size_bytes"] or 0) / 1024)

    except EnvironmentError as e:
        status["error"] = str(e)
        log.error("[%s] Config error: %s", pi_id, e)
        _alert_scoop("backup_config_error", pi_id, str(e))

    except (ClientError, NoCredentialsError) as e:
        status["error"] = str(e)
        log.error("[%s] R2 upload failed: %s", pi_id, e)
        _alert_scoop("backup_upload_failed", pi_id, str(e))

    except Exception as e:
        status["error"] = str(e)
        log.error("[%s] Unexpected error: %s", pi_id, e, exc_info=True)
        _alert_scoop("backup_failed", pi_id, str(e))

    return status


# ── RETENTION ─────────────────────────────────────────────────────────────────

def enforce_retention(dry_run: bool = False) -> None:
    """
    Delete R2 objects older than RETENTION_DAYS days for all Pi IDs.
    Lists all objects under backup/ prefix and removes stale ones.
    """
    log.info("Enforcing %d-day retention...", RETENTION_DAYS)
    cutoff = _now_utc() - timedelta(days=RETENTION_DAYS)

    try:
        client = _r2_client()
        paginator = client.get_paginator("list_objects_v2")
        deleted = 0

        for page in paginator.paginate(Bucket=R2_BUCKET):
            for obj in page.get("Contents", []):
                last_mod = obj["LastModified"]
                if last_mod.tzinfo is None:
                    last_mod = last_mod.replace(tzinfo=timezone.utc)
                if last_mod < cutoff:
                    if dry_run:
                        log.info("[DRY RUN] Would delete: %s", obj["Key"])
                    else:
                        client.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                        log.info("Deleted stale object: %s", obj["Key"])
                    deleted += 1

        log.info("Retention: %d object(s) %s.",
                 deleted, "would be removed" if dry_run else "removed")

    except (ClientError, NoCredentialsError) as e:
        log.error("Retention enforcement failed: %s", e)
        _alert_scoop("retention_failed", "all", str(e))


# ── VERIFICATION ──────────────────────────────────────────────────────────────

def verify_backups(status: dict) -> None:
    """
    For each Pi with a known R2 key, download and spot-check the most recent
    backup: decrypt it, open the tar.gz, confirm key files are present.
    Logs PASS/FAIL per Pi; alerts Scoop on failure.
    """
    log.info("Verifying recent backups...")

    if not status:
        log.info("No backups recorded in backup_status.json — nothing to verify.")
        return

    try:
        client = _r2_client()
        f = _fernet()
    except EnvironmentError as e:
        log.error("Verify aborted — config error: %s", e)
        return

    for pi_id, record in status.items():
        if record.get("outcome") not in ("success",):
            log.info("[%s] Skipping verify — last backup outcome: %s",
                     pi_id, record.get("outcome"))
            continue

        object_key = record.get("r2_key")
        if not object_key:
            log.warning("[%s] No R2 key recorded — skipping verify.", pi_id)
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                dl_path = tmp_path / "backup.tar.gz.enc"

                client.download_file(R2_BUCKET, object_key, str(dl_path))

                # Verify checksum matches what we recorded at upload time
                actual_checksum = hashlib.sha256(dl_path.read_bytes()).hexdigest()
                expected = record.get("checksum", "")
                if expected and actual_checksum != expected:
                    raise ValueError(
                        f"Checksum mismatch — expected {expected[:16]}..., "
                        f"got {actual_checksum[:16]}..."
                    )

                # Decrypt
                plaintext = f.decrypt(dl_path.read_bytes())
                dec_path = tmp_path / "backup.tar.gz"
                dec_path.write_bytes(plaintext)

                # Confirm archive is readable and contains expected files
                with tarfile.open(dec_path, "r:gz") as tar:
                    members = tar.getnames()

                expected_files = (
                    ["data/company.db", "user/.env"]  if pi_id == COMPANY_PI_ID
                    else ["data/auth.db"]             # retail Pis must have auth.db
                )
                missing = [f for f in expected_files if not any(
                    m.endswith(f) for m in members
                )]
                if missing:
                    raise ValueError(
                        f"Archive missing expected file(s): {missing}. "
                        f"Found: {members[:10]}"
                    )

                log.info("[%s] VERIFY PASS — checksum OK, archive readable, "
                         "key files present.", pi_id)

        except InvalidToken:
            msg = "Decryption failed — archive may be corrupt or key mismatch"
            log.error("[%s] VERIFY FAIL — %s", pi_id, msg)
            _alert_scoop("backup_verify_failed", pi_id, msg)

        except (ClientError, NoCredentialsError) as e:
            log.error("[%s] VERIFY FAIL — R2 download error: %s", pi_id, e)
            _alert_scoop("backup_verify_failed", pi_id, str(e))

        except Exception as e:
            log.error("[%s] VERIFY FAIL — %s", pi_id, e)
            _alert_scoop("backup_verify_failed", pi_id, str(e))


# ── RESTORE ───────────────────────────────────────────────────────────────────

def restore_backup(pi_id: str, date_str: str | None = None) -> None:
    """
    Download and decrypt a backup from R2 into data/restore_staging/<pi_id>/.
    The project lead manually extracts and deploys from that path.

    Per spec §7.6: Strongbox handles steps 1–2 of the restore process.
    The project lead performs decryption verification and Pi re-flash (steps 3–7).
    """
    if date_str is None:
        date_str = _now_utc().strftime("%Y-%m-%d")

    object_key = _r2_object_key(pi_id, date_str)
    restore_dir = DATA_DIR / "restore_staging" / pi_id
    restore_dir.mkdir(parents=True, exist_ok=True)
    output_path = restore_dir / f"synthos_backup_{pi_id}_{date_str}.tar.gz"

    log.info("Restore: downloading s3://%s/%s", R2_BUCKET, object_key)
    try:
        client = _r2_client()
        enc_path = restore_dir / f"synthos_backup_{pi_id}_{date_str}.tar.gz.enc"
        client.download_file(R2_BUCKET, object_key, str(enc_path))
        log.info("Downloaded: %s (%.1f KB)",
                 enc_path.name, enc_path.stat().st_size / 1024)

        f = _fernet()
        plaintext = f.decrypt(enc_path.read_bytes())
        output_path.write_bytes(plaintext)
        enc_path.unlink()

        log.info("Decrypted backup written to: %s", output_path)
        log.info(
            "Next steps (project lead):\n"
            "  1. Flash fresh Pi OS Lite\n"
            "  2. Run install_retail.py\n"
            "  3. Extract: tar -xzf %s -C /home/pi/synthos/\n"
            "  4. Reboot — agents resume from last known state",
            output_path,
        )

    except (ClientError, NoCredentialsError) as e:
        log.error("Restore failed — R2 error: %s", e)
        sys.exit(1)
    except InvalidToken:
        log.error("Restore failed — decryption error. Wrong BACKUP_ENCRYPTION_KEY?")
        sys.exit(1)
    except Exception as e:
        log.error("Restore failed: %s", e, exc_info=True)
        sys.exit(1)


# ── HEALTH REPORTING ─────────────────────────────────────────────────────────

def _check_staleness(status: dict) -> None:
    """
    For each Pi in status, alert if last_backup is older than STALE_HOURS.
    """
    threshold = _now_utc() - timedelta(hours=STALE_HOURS)
    for pi_id, record in status.items():
        last = record.get("last_backup")
        if last is None:
            msg = f"No backup on record — Pi has never been backed up."
            log.warning("[%s] %s", pi_id, msg)
            _alert_scoop("backup_missing", pi_id, msg)
            continue

        last_dt = datetime.fromisoformat(last)
        if last_dt < threshold:
            age_h = (_now_utc() - last_dt).total_seconds() / 3600
            msg = f"Last backup {age_h:.1f}h ago — exceeds {STALE_HOURS}h threshold."
            log.warning("[%s] CRITICAL — %s", pi_id, msg)
            _alert_scoop("backup_stale", pi_id, msg)


def print_status() -> None:
    """Print a human-readable summary of backup_status.json."""
    status = _load_status()
    if not status:
        print("No backup status recorded yet.")
        return

    print(f"\n{'Pi ID':<30} {'Last Backup (UTC)':<28} {'Outcome':<12} {'Size'}")
    print("-" * 85)
    for pi_id, r in sorted(status.items()):
        last = r.get("last_backup", "never")[:19] if r.get("last_backup") else "never"
        outcome = r.get("outcome", "unknown")
        size = f"{r['size_bytes'] / 1024:.1f} KB" if r.get("size_bytes") else "—"
        print(f"{pi_id:<30} {last:<28} {outcome:<12} {size}")
    print()


# ── MAIN CYCLE ────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    """Full daily backup cycle."""
    log.info("=== Strongbox daily run started%s ===",
             " [DRY RUN]" if dry_run else "")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    status = _load_status()

    # 1. Back up company.db
    log.info("--- Backing up company Pi ---")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _create_company_archive(Path(tmp))
            record = _backup_single(COMPANY_PI_ID, archive, dry_run)
            status[COMPANY_PI_ID] = record
    except FileNotFoundError as e:
        log.error("Company DB backup skipped: %s", e)
        status[COMPANY_PI_ID] = {
            "pi_id": COMPANY_PI_ID,
            "outcome": "failed",
            "error": str(e),
            "last_backup": status.get(COMPANY_PI_ID, {}).get("last_backup"),
        }

    # 2. Process staged retail Pi backups
    staged = _create_retail_archive_list()
    if staged:
        log.info("--- Processing %d staged retail Pi backup(s) ---", len(staged))
        for pi_id, archive_path in staged:
            log.info("[%s] Processing: %s", pi_id, archive_path.name)
            record = _backup_single(pi_id, archive_path, dry_run)
            status[pi_id] = record
            if record["outcome"] == "success":
                archive_path.unlink()
                log.info("[%s] Staging archive removed.", pi_id)
    else:
        log.info("No staged retail Pi backups found.")

    # 3. Enforce retention
    enforce_retention(dry_run)

    # 4. Stale backup check — alert Scoop on any Pi overdue
    _check_staleness(status)

    # 5. Persist status
    if not dry_run:
        _save_status(status)
        log.info("Backup status written to %s", BACKUP_STATUS_FILE)

    log.info("=== Strongbox daily run complete ===")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strongbox — Synthos Backup Manager (Agent 12)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan without uploading or deleting")
    parser.add_argument("--status", action="store_true",
                        help="Print last backup status and exit")
    parser.add_argument("--verify", action="store_true",
                        help="Spot-check most recent backup per Pi and exit")
    parser.add_argument("--restore", metavar="PI_ID",
                        help="Download and decrypt backup for PI_ID")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="Backup date for --restore (default: today)")

    args = parser.parse_args()

    if args.status:
        print_status()
        sys.exit(0)

    if args.verify:
        verify_backups(_load_status())
        sys.exit(0)

    if args.restore:
        restore_backup(args.restore, args.date)
        sys.exit(0)

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
