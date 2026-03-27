"""
vault.py — Vault > Control Agent
Synthos Company Pi | /home/pi/synthos-company/agents/vault.py

Role:
  License key management, compliance tracking, customer status transitions,
  and encrypted customer backup management.

  Vault is accountable for:
    - Every key that exists, its state, and who holds it
    - Customer status lifecycle (ACTIVE → INACTIVE → ARCHIVED)
    - Encrypted daily backups of all retail Pi data to cloud storage
    - Flagging compliance issues to the project lead

  Vault does not send emails. It writes to suggestions.json and triggers
  Scoop for any customer-facing communication.

  Backup encryption: AES-256 via Fernet (cryptography library).
  The encryption key is held by the project lead, not stored on any Pi.
  Vault encrypts before upload. Only the project lead can decrypt for restore.

Key format:
  synthos-<pi_id>-<timestamp>-<signature>
  Example: synthos-retail-pi-01-1704067200-abc123def456

Schedule:
  Daily backup:   2am ET (off-hours, low system load)
  Compliance scan: 6am ET (before market open)
  INACTIVE check:  Triggered by Sentinel or daily at midnight

USAGE:
  python3 vault.py --generate-key <pi_id>           # generate new license key
  python3 vault.py --revoke-key <pi_id>             # revoke key for Pi
  python3 vault.py --validate-key <key>             # validate a key string
  python3 vault.py --backup-now                     # run backup immediately
  python3 vault.py --backup-status                  # show backup health
  python3 vault.py --compliance-scan                # run compliance check
  python3 vault.py --list-keys                      # list all keys
"""

import os
import sys
import json
import uuid
import hmac
import time
import shutil
import hashlib
import logging
import argparse
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Paths resolved dynamically — works for any username or install directory.
# Override with SYNTHOS_BASE_DIR / SYNTHOS_RETAIL_DIR env vars if needed.

import sys as _sys
import os.path as _osp
_AGENTS_DIR = _osp.dirname(_osp.abspath(__file__))
_COMPANY_DIR = _osp.dirname(_AGENTS_DIR)
if _osp.join(_COMPANY_DIR, "utils") not in _sys.path:
    _sys.path.insert(0, _osp.join(_COMPANY_DIR, "utils"))

from synthos_paths import (
    BASE_DIR, DATA_DIR, LOGS_DIR, DB_PATH,
    SUGGESTIONS_FILE, SCOOP_TRIGGER, ENV_PATH,
    BACKUP_STAGING, RETAIL_DIR,
)

LOG_FILE   = LOGS_DIR / "control_agent.log"
BACKUP_LOG = DATA_DIR / "backup_log.json"

# Retail Pi root — used as backup source. Resolved via synthos_paths.RETAIL_DIR.
# Override with SYNTHOS_RETAIL_DIR env var if retail is installed elsewhere.
RETAIL_BASE = RETAIL_DIR.parent if RETAIL_DIR else BASE_DIR.parent

ET = ZoneInfo("America/New_York")

load_dotenv(ENV_PATH, override=True)

# Key signing secret — used to generate/verify license key signatures
KEY_SIGNING_SECRET = os.environ.get("KEY_SIGNING_SECRET", "")

# Cloud storage config (Cloudflare R2 via rclone or boto3)
# R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY from .env
R2_BUCKET       = os.environ.get("R2_BUCKET", "")
R2_ENDPOINT     = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY   = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY   = os.environ.get("R2_SECRET_KEY", "")

# Encryption key path — project lead provides this file for backup/restore
ENCRYPTION_KEY_PATH = Path(os.environ.get(
    "VAULT_ENCRYPTION_KEY_PATH",
    str(BASE_DIR / "config" / "vault.key")
))

# Customer lifecycle thresholds
INACTIVE_DAYS = 45
ARCHIVE_DAYS  = 365   # after INACTIVE

# Backup retention
BACKUP_RETENTION_DAYS = 30

SYNTHOS_VERSION = "1.0"

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s vault: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("vault")


# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT UNIQUE NOT NULL,
                pi_id       TEXT NOT NULL,
                issued_at   DATETIME NOT NULL,
                expires_at  DATETIME,
                status      TEXT NOT NULL DEFAULT 'ACTIVE',
                issued_by   TEXT DEFAULT 'Vault',
                notes       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                pi_id                TEXT UNIQUE NOT NULL,
                license_key          TEXT,
                customer_name        TEXT,
                email                TEXT,
                status               TEXT DEFAULT 'ACTIVE',
                created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_heartbeat       DATETIME,
                payment_status       TEXT DEFAULT 'PAID',
                github_fork_access   BOOLEAN DEFAULT 1,
                mail_alerts_enabled  BOOLEAN DEFAULT 1,
                archived_at          DATETIME
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_trail (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                agent       TEXT NOT NULL,
                action      TEXT NOT NULL,
                target      TEXT,
                details     TEXT,
                outcome     TEXT
            )
        """)
        conn.commit()
    log.info("Schema verified")


# ── TIME HELPERS ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


# ── KEY MANAGEMENT ────────────────────────────────────────────────────────────

def _sign_key(pi_id: str, timestamp: int) -> str:
    """Generate HMAC signature for a license key."""
    if not KEY_SIGNING_SECRET:
        log.warning("KEY_SIGNING_SECRET not set — keys will use weak signatures")
        secret = "synthos-default-insecure"
    else:
        secret = KEY_SIGNING_SECRET

    payload = f"{pi_id}-{timestamp}"
    sig = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    return sig


def generate_key(pi_id: str, expires_at: datetime = None,
                 notes: str = "") -> str:
    """
    Generate a new license key for a retail Pi.
    Stores in database and returns the key string.
    Revokes any existing active key for this Pi first.
    """
    if not pi_id or not pi_id.strip():
        raise ValueError("pi_id is required")

    pi_id     = pi_id.strip().lower()
    timestamp = int(now_utc().timestamp())
    sig       = _sign_key(pi_id, timestamp)
    key       = f"synthos-{pi_id}-{timestamp}-{sig}"

    with get_db() as conn:
        # Revoke any existing active keys for this Pi
        conn.execute("""
            UPDATE keys SET status='SUPERSEDED'
            WHERE pi_id=? AND status='ACTIVE'
        """, (pi_id,))

        # Insert new key
        conn.execute("""
            INSERT INTO keys (key, pi_id, issued_at, expires_at, status, notes)
            VALUES (?, ?, ?, ?, 'ACTIVE', ?)
        """, (key, pi_id, now_iso(),
              expires_at.isoformat() if expires_at else None,
              notes))

        # Ensure customer record exists
        conn.execute("""
            INSERT OR IGNORE INTO customers (pi_id, license_key, status)
            VALUES (?, ?, 'ACTIVE')
        """, (pi_id, key))
        conn.execute("""
            UPDATE customers SET license_key=? WHERE pi_id=?
        """, (key, pi_id))

        conn.commit()

    _audit("generate_key", pi_id, f"Key issued: {key[:30]}...", "SUCCESS")
    log.info(f"Key generated for {pi_id}: {key[:30]}...")
    return key


def revoke_key(pi_id: str, reason: str = "Manual revocation") -> bool:
    """
    Revoke all active keys for a Pi.
    The Pi will still run but loses update access on Company Pi side.
    """
    with get_db() as conn:
        rows = conn.execute("""
            UPDATE keys SET status='REVOKED', notes=?
            WHERE pi_id=? AND status='ACTIVE'
        """, (reason, pi_id))
        conn.commit()
        affected = rows.rowcount

    if affected:
        _audit("revoke_key", pi_id, reason, "SUCCESS")
        log.info(f"Revoked {affected} key(s) for {pi_id}: {reason}")
        _alert_project_lead(
            level="WARN",
            title=f"Key revoked: {pi_id}",
            message=f"License key for {pi_id} has been revoked. Reason: {reason}",
        )
        return True
    else:
        log.warning(f"No active keys found to revoke for {pi_id}")
        return False


def validate_key(key: str) -> dict:
    """
    Validate a license key.
    Returns dict with valid, pi_id, status, reason.
    """
    if not key or not key.startswith("synthos-"):
        return {"valid": False, "reason": "Invalid key format"}

    parts = key.split("-")
    if len(parts) < 4:
        return {"valid": False, "reason": "Malformed key"}

    with get_db() as conn:
        row = conn.execute("""
            SELECT pi_id, status, expires_at, issued_at
            FROM keys WHERE key=?
        """, (key,)).fetchone()

    if not row:
        return {"valid": False, "reason": "Key not found in registry"}

    if row["status"] != "ACTIVE":
        return {
            "valid":  False,
            "pi_id":  row["pi_id"],
            "reason": f"Key is {row['status']}",
        }

    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.replace(tzinfo=timezone.utc) < now_utc():
            return {
                "valid":  False,
                "pi_id":  row["pi_id"],
                "reason": "Key has expired",
            }

    return {
        "valid":      True,
        "pi_id":      row["pi_id"],
        "status":     row["status"],
        "issued_at":  row["issued_at"],
        "expires_at": row["expires_at"],
    }


def list_keys() -> list:
    """Return all keys with their status."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT key, pi_id, issued_at, expires_at, status, notes
            FROM keys ORDER BY issued_at DESC
        """).fetchall()]


# ── ENCRYPTED BACKUPS ─────────────────────────────────────────────────────────

def _load_encryption_key() -> bytes:
    """
    Load the Fernet encryption key from the key file.
    The project lead provides this file. Vault cannot generate it.
    If the file is missing, backups cannot be encrypted — abort.
    """
    if not ENCRYPTION_KEY_PATH.exists():
        raise FileNotFoundError(
            f"Encryption key not found at {ENCRYPTION_KEY_PATH}. "
            f"Project lead must provide vault.key before backups can run. "
            f"Generate with: python3 -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\" > {ENCRYPTION_KEY_PATH}"
        )
    key = ENCRYPTION_KEY_PATH.read_bytes().strip()
    return key


def _encrypt_file(src_path: Path, dst_path: Path) -> None:
    """Encrypt a file using Fernet (AES-256). Destination is .enc file."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ImportError(
            "cryptography package required for backups. "
            "Install with: pip install cryptography --break-system-packages"
        )

    key     = _load_encryption_key()
    fernet  = Fernet(key)
    data    = src_path.read_bytes()
    enc     = fernet.encrypt(data)
    dst_path.write_bytes(enc)


def backup_customer_pi(pi_id: str) -> dict:
    """
    Create an encrypted backup of a retail Pi's data.

    What gets backed up:
      - data/signals.db        (trading history — most important)
      - user/.env              (API keys, settings — encrypted with Fernet)
      - user/settings.json     (portal preferences)
      - logs/*.log             (last 7 days, compressed)

    What does NOT get backed up:
      - user/agreements/       (static legal docs, not customer-specific)
      - data/backup/           (don't backup the backup)
      - .known_good/           (watchdog snapshot, rebuilt automatically)

    Returns: dict with success, path, size_kb, error
    """
    retail_dir = RETAIL_BASE / f"synthos-{pi_id}" if (
        RETAIL_BASE / f"synthos-{pi_id}").exists() else RETAIL_BASE / "synthos"

    if not retail_dir.exists():
        # Try to find the retail Pi via SSH (if available)
        log.warning(f"Retail Pi directory not found locally for {pi_id} — skipping")
        return {"success": False, "error": f"Retail dir not found: {retail_dir}"}

    BACKUP_STAGING.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    tar_name = f"synthos_backup_{pi_id}_{ts}.tar.gz"
    enc_name = tar_name + ".enc"
    tar_path = BACKUP_STAGING / tar_name
    enc_path = BACKUP_STAGING / enc_name

    try:
        # Build list of files to include
        include = []

        db_path = retail_dir / "data" / "signals.db"
        if db_path.exists():
            include.append(db_path)

        env_path = retail_dir / "user" / ".env"
        if env_path.exists():
            include.append(env_path)

        settings_path = retail_dir / "user" / "settings.json"
        if settings_path.exists():
            include.append(settings_path)

        # Include last 7 days of logs (compressed)
        log_dir  = retail_dir / "logs"
        cutoff   = now_utc() - timedelta(days=7)
        for log_path in log_dir.glob("*.log"):
            if log_path.stat().st_mtime > cutoff.timestamp():
                include.append(log_path)

        if not include:
            return {"success": False, "error": "No files found to back up"}

        # Create tarball
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in include:
                tar.add(f, arcname=f.relative_to(retail_dir))

        tar_size_kb = round(tar_path.stat().st_size / 1024, 1)

        # Encrypt
        _encrypt_file(tar_path, enc_path)
        enc_size_kb = round(enc_path.stat().st_size / 1024, 1)

        # Upload to R2
        upload_ok = _upload_to_r2(enc_path, pi_id, enc_name)

        # Cleanup staging
        tar_path.unlink(missing_ok=True)

        result = {
            "success":    upload_ok,
            "pi_id":      pi_id,
            "timestamp":  now_iso(),
            "tar_size_kb": tar_size_kb,
            "enc_size_kb": enc_size_kb,
            "remote_path": f"backup/{pi_id}/{datetime.now().strftime('%Y-%m-%d')}/{enc_name}",
            "files_included": len(include),
        }

        if not upload_ok:
            result["error"] = "Encrypted backup created but upload failed — check R2 config"
            enc_path.unlink(missing_ok=True)

        _audit("backup", pi_id, f"Backup {'uploaded' if upload_ok else 'FAILED'}: {enc_name}", 
               "SUCCESS" if upload_ok else "FAILED")
        return result

    except FileNotFoundError as e:
        log.error(f"Encryption key missing for backup of {pi_id}: {e}")
        tar_path.unlink(missing_ok=True)
        return {"success": False, "error": str(e)}
    except Exception as e:
        log.error(f"Backup failed for {pi_id}: {e}")
        for p in [tar_path, enc_path]:
            p.unlink(missing_ok=True)
        return {"success": False, "error": str(e)}


def _upload_to_r2(file_path: Path, pi_id: str, filename: str) -> bool:
    """
    Upload encrypted backup to Cloudflare R2.
    Uses boto3 with R2's S3-compatible API.
    Returns True on success.
    """
    if not all([R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY]):
        log.warning(
            "R2 credentials not configured — backup not uploaded. "
            "Set R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY in .env"
        )
        return False

    try:
        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
        )

        date_prefix = datetime.now().strftime("%Y-%m-%d")
        key         = f"backup/{pi_id}/{date_prefix}/{filename}"

        s3.upload_file(str(file_path), R2_BUCKET, key)
        log.info(f"Uploaded backup to R2: s3://{R2_BUCKET}/{key}")
        return True

    except ImportError:
        log.error("boto3 not installed — install with: pip install boto3 --break-system-packages")
        return False
    except Exception as e:
        log.error(f"R2 upload failed for {pi_id}: {e}")
        return False


def run_all_backups() -> None:
    """
    Run backups for all active customers.
    Called daily at 2am ET.
    """
    log.info("Starting daily backup run")

    with get_db() as conn:
        customers = conn.execute("""
            SELECT pi_id FROM customers WHERE status='ACTIVE'
        """).fetchall()

    results    = []
    failed_pis = []

    for customer in customers:
        pi_id  = customer["pi_id"]
        result = backup_customer_pi(pi_id)
        results.append(result)
        if not result["success"]:
            failed_pis.append(pi_id)
            log.error(f"Backup failed for {pi_id}: {result.get('error', 'unknown')}")

    # Update backup log
    backup_record = {
        "timestamp":   now_iso(),
        "total":       len(results),
        "succeeded":   len(results) - len(failed_pis),
        "failed":      len(failed_pis),
        "failed_pis":  failed_pis,
    }

    try:
        if BACKUP_LOG.exists():
            log_data = json.loads(BACKUP_LOG.read_text())
        else:
            log_data = {"runs": []}
        log_data["runs"].append(backup_record)
        log_data["runs"] = log_data["runs"][-90:]   # keep 90 days of run history
        BACKUP_LOG.write_text(json.dumps(log_data, indent=2))
    except Exception as e:
        log.warning(f"Could not update backup log: {e}")

    if failed_pis:
        _alert_project_lead(
            level="CRITICAL",
            title=f"Backup failed for {len(failed_pis)} Pi(s)",
            message=(
                f"Daily backup failed for: {', '.join(failed_pis)}. "
                f"Customer data is unprotected. Check R2 credentials and Pi connectivity."
            ),
        )

    log.info(
        f"Backup run complete: {len(results) - len(failed_pis)}/{len(results)} succeeded"
    )


def backup_status() -> dict:
    """Return current backup health summary."""
    if not BACKUP_LOG.exists():
        return {"status": "no_runs", "message": "No backup runs recorded yet"}

    try:
        log_data = json.loads(BACKUP_LOG.read_text())
        runs     = log_data.get("runs", [])
        if not runs:
            return {"status": "no_runs", "message": "No backup runs recorded"}

        last_run = runs[-1]
        last_ts  = datetime.fromisoformat(last_run["timestamp"].replace("Z", "+00:00"))
        age_hours = (now_utc() - last_ts).total_seconds() / 3600

        status = "healthy"
        if age_hours > 48:
            status = "stale"
        if last_run["failed"] > 0:
            status = "degraded"

        return {
            "status":       status,
            "last_run":     last_run["timestamp"],
            "age_hours":    round(age_hours, 1),
            "last_result":  last_run,
            "total_runs":   len(runs),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── COMPLIANCE SCAN ───────────────────────────────────────────────────────────

def run_compliance_scan() -> list[dict]:
    """
    Daily compliance check — runs at 6am ET before market open.
    Checks:
      - Keys about to expire
      - Customers overdue for INACTIVE transition
      - Backup staleness
      - Unusual key activity
    Returns list of findings.
    """
    findings = []
    now      = now_utc()

    with get_db() as conn:
        # Keys expiring within 7 days
        expiring = conn.execute("""
            SELECT key, pi_id, expires_at FROM keys
            WHERE status='ACTIVE'
            AND expires_at IS NOT NULL
            AND expires_at < ?
        """, ((now + timedelta(days=7)).isoformat(),)).fetchall()

        for row in expiring:
            findings.append({
                "type":    "key_expiring",
                "pi_id":   row["pi_id"],
                "message": f"Key for {row['pi_id']} expires {row['expires_at']}",
                "severity": "WARN",
            })

        # Customers overdue for INACTIVE (already handled by Sentinel, but Vault
        # maintains the compliance record)
        inactive_cutoff = (now - timedelta(days=INACTIVE_DAYS)).isoformat()
        overdue = conn.execute("""
            SELECT pi_id, last_heartbeat FROM customers
            WHERE status='ACTIVE' AND last_heartbeat < ?
        """, (inactive_cutoff,)).fetchall()

        for row in overdue:
            findings.append({
                "type":     "inactive_overdue",
                "pi_id":    row["pi_id"],
                "message":  f"{row['pi_id']} silent since {row['last_heartbeat']} — should be INACTIVE",
                "severity": "WARN",
            })

    # Backup staleness
    bs = backup_status()
    if bs["status"] == "stale":
        findings.append({
            "type":     "backup_stale",
            "pi_id":    "all",
            "message":  f"Last backup was {bs.get('age_hours', '?')}h ago — over 48h threshold",
            "severity": "CRITICAL",
        })
    elif bs["status"] == "degraded":
        findings.append({
            "type":     "backup_failed",
            "pi_id":    str(bs.get("last_result", {}).get("failed_pis", [])),
            "message":  f"Last backup run had failures: {bs.get('last_result', {})}",
            "severity": "HIGH",
        })

    # Alert on any CRITICAL findings
    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    for finding in criticals:
        _alert_project_lead(
            level="CRITICAL",
            title=f"Compliance: {finding['type']}",
            message=finding["message"],
        )

    log.info(f"Compliance scan: {len(findings)} findings ({len(criticals)} critical)")
    return findings


# ── CUSTOMER ARCHIVAL ─────────────────────────────────────────────────────────

def run_archival_check() -> None:
    """
    Transition INACTIVE customers to ARCHIVED after ARCHIVE_DAYS.
    Called daily at midnight.
    """
    archive_cutoff = (now_utc() - timedelta(days=ARCHIVE_DAYS)).isoformat()

    with get_db() as conn:
        to_archive = conn.execute("""
            SELECT pi_id, last_heartbeat FROM customers
            WHERE status='INACTIVE'
            AND (archived_at IS NULL OR archived_at < ?)
        """, (archive_cutoff,)).fetchall()

        for row in to_archive:
            conn.execute("""
                UPDATE customers SET status='ARCHIVED', archived_at=?
                WHERE pi_id=?
            """, (now_iso(), row["pi_id"]))

            # Revoke key on archive
            conn.execute("""
                UPDATE keys SET status='ARCHIVED'
                WHERE pi_id=? AND status IN ('ACTIVE', 'SUPERSEDED')
            """, (row["pi_id"],))

            _audit("archive_customer", row["pi_id"],
                   f"Archived after {ARCHIVE_DAYS} days inactive", "SUCCESS")
            log.info(f"Archived customer: {row['pi_id']}")

        if to_archive:
            conn.commit()


# ── AUDIT TRAIL ───────────────────────────────────────────────────────────────

def _audit(action: str, target: str, details: str, outcome: str) -> None:
    """Write immutable audit record. Every Vault action is logged here."""
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO audit_trail (agent, action, target, details, outcome)
                VALUES ('Vault', ?, ?, ?, ?)
            """, (action, target, details, outcome))
            conn.commit()
    except Exception as e:
        log.error(f"Audit write failed: {e} — action={action} target={target}")


# ── ALERTS ────────────────────────────────────────────────────────────────────

def _alert_project_lead(level: str, title: str, message: str) -> None:
    """Write alert to suggestions.json for project lead review."""
    try:
        if SUGGESTIONS_FILE.exists():
            data = json.loads(SUGGESTIONS_FILE.read_text())
        else:
            data = {"version": "1.0", "suggestions": []}

        # Deduplicate by title prefix
        prefix = title[:60]
        existing_titles = {
            s.get("title", "")[:60]
            for s in data.get("suggestions", [])
            if s.get("status") in ("pending", "approved")
        }
        if prefix in existing_titles:
            return

        suggestion = {
            "id":        str(uuid.uuid4()),
            "timestamp": now_iso(),
            "agent":     "Vault",
            "category":  "security",
            "title":     title,
            "description": message,
            "impact": {
                "tokens_saved_per_week":    None,
                "execution_time_saved":     None,
                "risk_level":               level,
                "affected_component":       "Vault / Key Management",
                "affected_customers_count": 1,
                "estimated_improvement":    "Compliance risk resolved",
            },
            "implementation": {
                "effort":                "Manual review required",
                "complexity":           "SIMPLE",
                "approver_needed":      "you",
                "trial_run_recommended": False,
                "breaking_changes":     False,
                "rollback_difficulty":  "EASY",
            },
            "details": {
                "root_cause":           message,
                "solution_approach":    "Review and take appropriate action",
                "alternative_approaches": [],
                "dependencies":         [],
                "metrics_to_track":     ["Issue resolved"],
            },
            "status":            "pending",
            "status_updated_at": now_iso(),
            "approver_notes":    None,
            "implementation_status": None,
            "implementation_notes":  None,
        }

        data["suggestions"].append(suggestion)
        data["last_updated"] = now_iso()

        if SUGGESTIONS_FILE.exists():
            shutil.copy2(SUGGESTIONS_FILE, SUGGESTIONS_FILE.with_suffix(".json.backup"))
        SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2))

    except Exception as e:
        log.error(f"Failed to write alert: {e}")


def _trigger_scoop(event_type: str, payload: dict) -> None:
    """Write event to scoop_trigger.json."""
    try:
        if SCOOP_TRIGGER.exists():
            events = json.loads(SCOOP_TRIGGER.read_text())
        else:
            events = []
        events.append({
            "id":         str(uuid.uuid4()),
            "type":       event_type,
            "payload":    payload,
            "created_at": now_iso(),
            "status":     "pending",
        })
        SCOOP_TRIGGER.write_text(json.dumps(events, indent=2))
    except Exception as e:
        log.warning(f"Could not trigger Scoop: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_generate_key(pi_id: str) -> None:
    key = generate_key(pi_id)
    print(f"\nKey generated for {pi_id}:")
    print(f"  {key}")
    print(f"\nStore this key securely. Send to customer for Pi setup.")
    print(f"Key is now active in company.db.\n")


def cli_revoke_key(pi_id: str) -> None:
    reason = input(f"Reason for revoking key for {pi_id}: ").strip()
    if not reason:
        reason = "Manual revocation via CLI"
    ok = revoke_key(pi_id, reason)
    print(f"{'Revoked' if ok else 'No active key found'} for {pi_id}")


def cli_validate_key(key: str) -> None:
    result = validate_key(key)
    print(f"\nKey validation result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print()


def cli_list_keys() -> None:
    keys = list_keys()
    print(f"\n{'=' * 70}")
    print(f"VAULT KEY REGISTRY — {len(keys)} keys")
    print(f"{'=' * 70}")
    for k in keys:
        icon = "✓" if k["status"] == "ACTIVE" else "✗"
        print(
            f"  {icon} {k['pi_id']:20} {k['status']:12} "
            f"issued={k['issued_at'][:10]} "
            f"{'expires=' + k['expires_at'][:10] if k['expires_at'] else 'no expiry'}"
        )
    print(f"{'=' * 70}\n")


def cli_backup_status() -> None:
    status = backup_status()
    print(f"\nBackup status: {status['status'].upper()}")
    for k, v in status.items():
        if k != "status":
            print(f"  {k}: {v}")
    print()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vault — Control Agent")
    parser.add_argument("--generate-key",    metavar="PI_ID",
                        help="Generate new license key for Pi")
    parser.add_argument("--revoke-key",      metavar="PI_ID",
                        help="Revoke license key for Pi")
    parser.add_argument("--validate-key",    metavar="KEY",
                        help="Validate a license key string")
    parser.add_argument("--backup-now",      action="store_true",
                        help="Run backup for all active customers now")
    parser.add_argument("--backup-status",   action="store_true",
                        help="Show backup health summary")
    parser.add_argument("--compliance-scan", action="store_true",
                        help="Run compliance check")
    parser.add_argument("--list-keys",       action="store_true",
                        help="List all license keys")
    parser.add_argument("--archival-check",  action="store_true",
                        help="Run customer archival check")
    args = parser.parse_args()

    ensure_schema()

    if args.generate_key:
        cli_generate_key(args.generate_key)
    elif args.revoke_key:
        cli_revoke_key(args.revoke_key)
    elif args.validate_key:
        cli_validate_key(args.validate_key)
    elif args.backup_now:
        run_all_backups()
    elif args.backup_status:
        cli_backup_status()
    elif args.compliance_scan:
        findings = run_compliance_scan()
        for f in findings:
            print(f"  [{f['severity']}] {f['type']}: {f['message']}")
        if not findings:
            print("  ✓ No compliance issues found")
    elif args.list_keys:
        cli_list_keys()
    elif args.archival_check:
        run_archival_check()
    else:
        parser.print_help()
