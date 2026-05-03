"""
retail_restore.py — Retail Pi Restore Tool (v2)
=================================================
Counterpart to retail_backup.py. Pulls v2 backups from R2 (or local file or
company-pi proxy), validates the manifest, and restores files into the
retail/process node's filesystem.

This tool is designed for two flows:
  1. Operator-driven recovery on an existing pi5: `python3 retail_restore.py
     --stream customer --pi-id synthos-pi-retail --apply` to overwrite local data.
  2. Bootstrap during install (called by v2 installer's restore phase, but
     can also be invoked directly).

R2 access modes:
  --source via-r2          direct boto3 download using R2 creds in user/.env
  --source via-company     POST to company Pi /restore_backup; recommended path
                           on a fresh node that doesn't have R2 creds yet
  --source file:/path      decrypt + restore from a local .tar.gz.enc

.env keys required (synthos_build/user/.env):
  BACKUP_ENCRYPTION_KEY    REQUIRED for any restore
  COMPANY_URL + SECRET_TOKEN  REQUIRED for --source via-company
  R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME
                           REQUIRED for --source via-r2

Usage:
  # List available backups for a stream
  python3 retail_restore.py --list --stream customer

  # Dry-run restore (download, decrypt, validate manifest, print plan)
  python3 retail_restore.py --stream customer --pi-id synthos-pi-retail

  # Apply restore (overwrite local files; refuses without --apply)
  python3 retail_restore.py --stream customer --pi-id synthos-pi-retail --apply

  # Restore from local .enc file
  python3 retail_restore.py --source file:/path/to/backup.tar.gz.enc --apply

  # Restore via company Pi proxy
  python3 retail_restore.py --source via-company --stream retail \\
      --pi-id synthos-pi-retail --apply
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import tarfile
import hashlib
import argparse
import logging
import tempfile
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


# ── PATHS ──────────────────────────────────────────────────────────────────────
_SRC_DIR    = Path(__file__).resolve().parent
_BUILD_DIR  = _SRC_DIR.parent
_USER_DIR   = _BUILD_DIR / "user"
_LOG_DIR    = _BUILD_DIR / "logs"

load_dotenv(_USER_DIR / ".env", override=True)

COMPANY_URL          = os.environ.get("COMPANY_URL", "").rstrip("/")
SECRET_TOKEN         = os.environ.get("SECRET_TOKEN", "")
PI_ID                = os.environ.get("PI_ID", "synthos-pi-1")
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY", "")

R2_BUCKET     = os.environ.get("R2_BUCKET_NAME", "synthos-backups")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")

VALID_STREAMS = ("company", "customer", "retail")
DOWNLOAD_TIMEOUT = 300


# ── LOGGING ────────────────────────────────────────────────────────────────────
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s retail_restore: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(_LOG_DIR / "retail_restore.log")],
)
log = logging.getLogger("retail_restore")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    if not BACKUP_ENCRYPTION_KEY:
        raise EnvironmentError("BACKUP_ENCRYPTION_KEY missing — cannot decrypt")
    try:
        return Fernet(BACKUP_ENCRYPTION_KEY.encode())
    except Exception as e:
        raise EnvironmentError(f"BACKUP_ENCRYPTION_KEY invalid: {e}") from e


def _r2_client():
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY]):
        raise EnvironmentError("R2 credentials missing in .env")
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def _validate_manifest(manifest: dict, expected_stream: str | None) -> None:
    if manifest.get("manifest_version", "").split(".")[0] != "1":
        raise ValueError(f"Unsupported manifest_version: {manifest.get('manifest_version')!r}")
    if expected_stream and manifest.get("stream") != expected_stream:
        raise ValueError(
            f"Manifest stream mismatch: expected {expected_stream!r}, "
            f"got {manifest.get('stream')!r}"
        )
    for key in ("manifest_version", "node_type", "pi_id", "created_at",
                "checksum_sha256", "size_bytes_decrypted", "encryption", "contents"):
        if key not in manifest:
            raise ValueError(f"Manifest missing required field: {key}")


def _verify_content_checksum(tar_path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with tarfile.open(tar_path, "r:gz") as tar:
        members = sorted(
            (m for m in tar.getmembers() if m.isfile() and m.name != "manifest.json"),
            key=lambda m: m.name,
        )
        for m in members:
            f = tar.extractfile(m)
            if f is None:
                continue
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest() == expected


# ── SOURCES ───────────────────────────────────────────────────────────────────

def _fetch_via_r2(stream: str, pi_id: str, date: str, dest: Path) -> dict:
    """Download from R2 directly. Returns the {key, size, last_modified}."""
    client = _r2_client()
    if date == "latest":
        prefix = f"{stream}/{pi_id}/"
        keys = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append((obj["Key"], obj["LastModified"], obj["Size"]))
        if not keys:
            raise FileNotFoundError(f"No backups at prefix {prefix}")
        keys.sort(key=lambda kv: kv[1])
        object_key, last_mod, size = keys[-1]
    else:
        object_key = f"{stream}/{pi_id}/{date}/synthos_backup_{stream}_{pi_id}_{date}.tar.gz.enc"
        try:
            head = client.head_object(Bucket=R2_BUCKET, Key=object_key)
            size = head["ContentLength"]
            last_mod = head["LastModified"]
        except Exception as e:
            raise FileNotFoundError(f"R2 object not found: {object_key} ({e})")

    log.info("Downloading s3://%s/%s (%.1f KB)", R2_BUCKET, object_key, size / 1024)
    client.download_file(R2_BUCKET, object_key, str(dest))
    return {"key": object_key, "size": size, "last_modified": str(last_mod)}


def _fetch_via_company(stream: str, pi_id: str, date: str, dest: Path) -> dict:
    """POST to company Pi /restore_backup."""
    if not COMPANY_URL or not SECRET_TOKEN:
        raise EnvironmentError("COMPANY_URL or SECRET_TOKEN missing in .env")
    url = f"{COMPANY_URL}/restore_backup"
    log.info("Requesting from %s (stream=%s, pi_id=%s, date=%s)", url, stream, pi_id, date)
    resp = requests.post(
        url,
        data={"stream": stream, "pi_id": pi_id, "date": date},
        headers={"X-Token": SECRET_TOKEN},
        timeout=DOWNLOAD_TIMEOUT,
        stream=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"company /restore_backup HTTP {resp.status_code}: {resp.text[:300]}")
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)
    return {
        "key": resp.headers.get("X-R2-Key", ""),
        "size": dest.stat().st_size,
        "last_modified": resp.headers.get("X-Backup-Date", ""),
        "manifest_version": resp.headers.get("X-Manifest-Version", ""),
    }


def _fetch_via_file(file_path: Path, dest: Path) -> dict:
    if not file_path.exists():
        raise FileNotFoundError(f"local file not found: {file_path}")
    shutil.copy2(file_path, dest)
    return {"key": str(file_path), "size": dest.stat().st_size, "last_modified": ""}


# ── LIST ──────────────────────────────────────────────────────────────────────

def cmd_list(stream: str | None) -> None:
    client = _r2_client()
    streams = (stream,) if stream else VALID_STREAMS
    for s in streams:
        prefix = f"{s}/"
        try:
            paginator = client.get_paginator("list_objects_v2")
            entries = []
            for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    entries.append((obj["Key"], obj["Size"], obj["LastModified"]))
            entries.sort(key=lambda x: x[2])
            print(f"\n=== {s.upper()} ({len(entries)} objects) ===")
            for k, sz, dt in entries[-30:]:
                print(f"  {dt.isoformat()[:19]}  {sz:>12,} B  {k}")
            if len(entries) > 30:
                print(f"  ...and {len(entries) - 30} older")
        except Exception as e:
            print(f"[{s}] error: {e}")


# ── DECRYPT + EXTRACT ─────────────────────────────────────────────────────────

def decrypt_to_tar(enc_path: Path, out_dir: Path) -> Path:
    """Decrypt .enc → .tar.gz in out_dir. Returns tar path."""
    f = _fernet()
    plaintext = f.decrypt(enc_path.read_bytes())
    tar_path = out_dir / "decrypted.tar.gz"
    tar_path.write_bytes(plaintext)
    return tar_path


def read_manifest(tar_path: Path) -> dict:
    with tarfile.open(tar_path, "r:gz") as tar:
        if "manifest.json" not in tar.getnames():
            raise ValueError("Archive has no manifest.json (legacy v1 backup?)")
        member = tar.getmember("manifest.json")
        return json.loads(tar.extractfile(member).read())


def extract_per_manifest(tar_path: Path, manifest: dict, target_home: Path,
                        apply: bool = False) -> dict:
    """
    Extract files from tar to target_home according to manifest.contents[].
    Honors merge_strategy and permissions. If apply=False, just dry-run.

    Returns a dict of {planned: [...], skipped: [...]} with action descriptions.
    """
    plan = {"planned": [], "skipped": [], "errors": []}
    contents = manifest.get("contents", [])

    with tarfile.open(tar_path, "r:gz") as tar:
        all_names = set(tar.getnames())

        for entry in contents:
            src = entry["src"].rstrip("/")
            dest_rel = entry["dest"].rstrip("/")
            etype = entry["type"]
            required = entry.get("required", False)
            merge = entry.get("merge_strategy", "replace")
            perms = entry.get("permissions")

            target_path = target_home / dest_rel

            # Find members under this src
            if etype == "file":
                if src not in all_names:
                    if required:
                        plan["errors"].append(f"REQUIRED file missing in tar: {src}")
                    else:
                        plan["skipped"].append(f"optional file absent: {src}")
                    continue
                action = f"FILE  {src} → {target_path} (perms={perms})"
                plan["planned"].append(action)
                if apply:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    member = tar.getmember(src)
                    tar.extract(member, path=str(target_home), set_attrs=False, filter="data")
                    extracted = target_home / src
                    if extracted != target_path:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(extracted), str(target_path))
                    if perms:
                        target_path.chmod(int(perms, 8))
            elif etype == "directory":
                # Members under src/
                src_prefix = src + "/"
                children = [n for n in all_names if n == src or n.startswith(src_prefix)]
                if not children:
                    if required:
                        plan["errors"].append(f"REQUIRED directory missing in tar: {src}/")
                    else:
                        plan["skipped"].append(f"optional dir absent: {src}/")
                    continue
                action = f"DIR   {src}/ → {target_path}/ ({merge}, {len(children)} entries)"
                plan["planned"].append(action)
                if apply:
                    if merge == "replace" and target_path.exists():
                        if target_path.is_dir():
                            shutil.rmtree(target_path)
                        else:
                            target_path.unlink()
                    target_path.mkdir(parents=True, exist_ok=True)
                    for n in children:
                        m = tar.getmember(n)
                        if m.isdir():
                            (target_home / n).mkdir(parents=True, exist_ok=True)
                            continue
                        tar.extract(m, path=str(target_home), set_attrs=False, filter="data")
                    if perms:
                        target_path.chmod(int(perms, 8))
            else:
                plan["errors"].append(f"unknown entry type: {etype} (src={src})")

    return plan


# ── MAIN COMMANDS ─────────────────────────────────────────────────────────────

def cmd_restore(source: str, stream: str | None, pi_id: str | None,
                date: str, apply: bool, target_home: Path) -> int:
    """End-to-end restore. Returns exit code."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        enc_local = tmp_path / "backup.tar.gz.enc"

        # 1. Fetch
        try:
            if source.startswith("file:"):
                fetch_meta = _fetch_via_file(Path(source[5:]), enc_local)
            elif source == "via-r2":
                if not stream or not pi_id:
                    log.error("--stream and --pi-id required for via-r2")
                    return 2
                fetch_meta = _fetch_via_r2(stream, pi_id, date, enc_local)
            elif source == "via-company":
                if not stream or not pi_id:
                    log.error("--stream and --pi-id required for via-company")
                    return 2
                fetch_meta = _fetch_via_company(stream, pi_id, date, enc_local)
            else:
                log.error("unknown --source: %s", source)
                return 2
        except Exception as e:
            log.error("fetch failed: %s", e)
            return 3

        log.info("Fetched: %s (%.1f KB)", fetch_meta.get("key"), fetch_meta.get("size", 0)/1024)

        # 2. Decrypt
        try:
            tar_path = decrypt_to_tar(enc_local, tmp_path)
        except InvalidToken:
            log.error("decrypt FAILED — wrong BACKUP_ENCRYPTION_KEY?")
            return 4

        # 3. Read + validate manifest
        try:
            manifest = read_manifest(tar_path)
        except ValueError as e:
            log.error("manifest error: %s", e)
            return 5

        try:
            _validate_manifest(manifest, expected_stream=stream)
        except ValueError as e:
            log.error("manifest validation: %s", e)
            return 5

        log.info("Manifest OK — version=%s, stream=%s, pi_id=%s, date=%s, "
                 "node_type=%s, %d content entries",
                 manifest["manifest_version"], manifest.get("stream"),
                 manifest["pi_id"], manifest.get("date") or manifest["created_at"][:10],
                 manifest["node_type"], len(manifest["contents"]))

        # 4. Verify checksum
        if not _verify_content_checksum(tar_path, manifest["checksum_sha256"]):
            log.error("CHECKSUM FAIL — archive content does not match manifest")
            return 6
        log.info("Content checksum verified (%s...)", manifest["checksum_sha256"][:16])

        # 5. Plan or apply
        plan = extract_per_manifest(tar_path, manifest, target_home, apply=apply)
        if plan["errors"]:
            log.error("Restore PLAN has errors:")
            for e in plan["errors"]:
                log.error("  - %s", e)
            return 7

        verb = "EXTRACTED" if apply else "WOULD EXTRACT"
        log.info("--- Restore plan ---")
        for action in plan["planned"]:
            log.info("  %s %s", verb, action)
        for skipped in plan["skipped"]:
            log.info("  (skipped) %s", skipped)

        if not apply:
            log.info("Dry-run complete. Re-run with --apply to actually overwrite files at %s",
                     target_home)
            return 0

        log.info("=== Restore complete: %d entries written to %s ===",
                 len(plan["planned"]), target_home)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="retail_restore.py v2 — Retail Pi Restore")
    parser.add_argument("--list", action="store_true",
                        help="List available backups in R2 (exits)")
    parser.add_argument("--source", default="via-r2",
                        help="Source: via-r2 | via-company | file:/path  (default via-r2)")
    parser.add_argument("--stream", choices=VALID_STREAMS,
                        help="Backup stream")
    parser.add_argument("--pi-id", help="Source pi_id (default: env PI_ID)")
    parser.add_argument("--date", default="latest",
                        help="YYYY-MM-DD or 'latest' (default latest)")
    parser.add_argument("--target", default=str(_BUILD_DIR),
                        help=f"Restore destination (default: {_BUILD_DIR})")
    parser.add_argument("--apply", action="store_true",
                        help="Actually extract files (otherwise dry-run only)")
    args = parser.parse_args()

    if args.list:
        try:
            cmd_list(args.stream)
        except Exception as e:
            log.error("list failed: %s", e)
            sys.exit(1)
        return

    pi_id = args.pi_id or PI_ID
    target = Path(args.target).resolve()
    if not args.source.startswith("file:") and not args.stream:
        log.error("--stream is required unless --source=file:...")
        sys.exit(2)

    rc = cmd_restore(args.source, args.stream, pi_id, args.date, args.apply, target)
    sys.exit(rc)


if __name__ == "__main__":
    main()
