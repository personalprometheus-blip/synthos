"""
retail_backup.py — Retail Pi Backup Creator (v2)
================================================
Creates two encrypted backup streams from the retail Pi and POSTs them to
the Company Pi's /receive_backup endpoint, where Strongbox stages them
and uploads to Cloudflare R2.

Streams (3-stream split, per BACKUP_ENCRYPT_AND_SPLIT_PLAN 2026-04-18):
    customer  — data/auth.db + data/customers/   (customer PII, isolated blast radius)
    retail    — user/.env + user/signals.db + user/agreements/ (operator config + admin trading)

Each stream:
    1. Files copied to a working dir (canonical layout matching manifest)
    2. SHA-256 computed over content (sorted by path), used in manifest.json
    3. manifest.json written at tar root per BACKUP_MANIFEST_CONTRACT v1.0
    4. Tarball built (manifest first, then sorted content)
    5. Fernet-encrypted with BACKUP_ENCRYPTION_KEY → .tar.gz.enc
    6. Round-trip self-verify: decrypt, recompute checksum, compare to manifest
    7. POSTed to company Pi with form fields {pi_id, stream}

.env keys required (synthos_build/user/.env):
    COMPANY_URL              http://<company-pi-ip>:5050
    SECRET_TOKEN             shared token (same as company_server)
    PI_ID                    unique identifier for this retail Pi
    BACKUP_ENCRYPTION_KEY    base64 Fernet key — MUST match pi4b's key

Usage:
    python3 retail_backup.py              # build, encrypt, verify, upload both streams
    python3 retail_backup.py --dry-run    # build + encrypt + verify, log plan, no upload
    python3 retail_backup.py --local      # save .enc files to backups/staging/, no upload
    python3 retail_backup.py --stream customer  # only one stream (testing)
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
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


# ── PATHS ──────────────────────────────────────────────────────────────────────
_SRC_DIR    = Path(__file__).resolve().parent      # synthos_build/src/
_BUILD_DIR  = _SRC_DIR.parent                       # synthos_build/
_DATA_DIR   = _BUILD_DIR / "data"                   # auth.db, customers/
_USER_DIR   = _BUILD_DIR / "user"                   # .env, signals.db, agreements/
_LOG_DIR    = _BUILD_DIR / "logs"
_BACKUP_DIR = _BUILD_DIR / "backups" / "staging"    # local .enc copies (7d retention)

load_dotenv(_USER_DIR / ".env", override=True)


# ── CONFIG ─────────────────────────────────────────────────────────────────────
COMPANY_URL          = os.environ.get("COMPANY_URL", "").rstrip("/")
SECRET_TOKEN         = os.environ.get("SECRET_TOKEN", "")
PI_ID                = os.environ.get("PI_ID", "synthos-pi-1")
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
UPLOAD_TIMEOUT       = 120          # seconds; encrypted blob is bigger and Pi can be slow
LOCAL_RETENTION_DAYS = 7

MANIFEST_VERSION = "1.0"
SYNTHOS_VERSION  = "3.0"
NODE_TYPE        = "process"        # forward-looking v2 vocabulary; pi_id retains legacy name

STREAMS = ("customer", "retail")


# ── LOGGING ────────────────────────────────────────────────────────────────────
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s retail_backup: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(_LOG_DIR / "retail_backup.log")],
)
log = logging.getLogger("retail_backup")


# ── CONTENTS DEFINITIONS (per stream, per BACKUP_MANIFEST_CONTRACT v1.0) ──────
#
# Each entry:
#   src_abs      — absolute source path on this Pi
#   arcname      — canonical path inside tar (relative to tar root)
#   dest         — restore path inside SYNTHOS_HOME on the target node
#   type         — "file" | "directory"
#   permissions  — octal string (e.g. "0600")
#   required     — True if absence aborts restore
#   merge_strategy — "replace" | "merge"

def _stream_contents(stream: str) -> list[dict]:
    """Return contents[] entries for a given stream, filtering out missing optionals."""
    if stream == "customer":
        candidates = [
            {
                "src_abs": _DATA_DIR / "auth.db",
                "src": "data/auth.db",
                "dest": "data/auth.db",
                "type": "file",
                "permissions": "0600",
                "required": True,
                "merge_strategy": "replace",
            },
            {
                "src_abs": _DATA_DIR / "customers",
                "src": "data/customers",
                "dest": "data/customers/",
                "type": "directory",
                "permissions": "0755",
                "required": True,
                "merge_strategy": "replace",
            },
        ]
    elif stream == "retail":
        candidates = [
            {
                "src_abs": _USER_DIR / ".env",
                "src": "user/.env",
                "dest": "user/.env",
                "type": "file",
                "permissions": "0600",
                "required": True,
                "merge_strategy": "replace",
            },
            {
                "src_abs": _USER_DIR / "signals.db",
                "src": "user/signals.db",
                "dest": "user/signals.db",
                "type": "file",
                "permissions": "0644",
                "required": True,
                "merge_strategy": "replace",
            },
            {
                "src_abs": _USER_DIR / "agreements",
                "src": "user/agreements",
                "dest": "user/agreements/",
                "type": "directory",
                "permissions": "0755",
                "required": False,
                "merge_strategy": "merge",
            },
        ]
    else:
        raise ValueError(f"unknown stream: {stream!r}")

    # Filter out missing entries (allowed for required=False; abort for required=True)
    present = []
    for c in candidates:
        if c["src_abs"].exists():
            present.append(c)
        elif c["required"]:
            raise FileNotFoundError(
                f"Required source missing for stream {stream!r}: {c['src_abs']}"
            )
        else:
            log.info("[%s] optional source missing: %s — skipping", stream, c["src_abs"])
    return present


# ── HELPERS ───────────────────────────────────────────────────────────────────

_NOISE_DIRS = {"__pycache__", ".ruff_cache", ".git", ".pytest_cache", ".mypy_cache"}


def _is_noise(rel_path: Path) -> bool:
    """Skip recreatable artifacts."""
    parts = rel_path.parts
    if any(p in _NOISE_DIRS for p in parts):
        return True
    name = rel_path.name
    return name.endswith((".pyc", ".pyo", ".swp", ".swo", "~"))


def _walk_files(root: Path) -> list[Path]:
    """Walk root; return relative POSIX paths of regular files, sorted, noise excluded."""
    if not root.exists():
        return []
    if root.is_file():
        return [root.relative_to(root.parent)]
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _is_noise(rel):
            continue
        out.append(rel)
    out.sort()
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fernet() -> Fernet:
    if not BACKUP_ENCRYPTION_KEY:
        raise EnvironmentError(
            "BACKUP_ENCRYPTION_KEY missing from .env — required for v2 encrypt-on-source. "
            "Copy from pi4b's company.env (must match exactly)."
        )
    try:
        return Fernet(BACKUP_ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise EnvironmentError(
            f"BACKUP_ENCRYPTION_KEY is not a valid Fernet key: {exc}"
        ) from exc


# ── ARCHIVE BUILDER (v2 with manifest.json) ───────────────────────────────────

def _stage_stream(stream: str, work_dir: Path) -> tuple[list[dict], list[Path]]:
    """
    Copy stream contents into work_dir under canonical arcnames.
    Returns (manifest_contents_entries, list_of_tar_root_relative_paths_sorted).
    """
    entries = _stream_contents(stream)
    rel_paths: list[Path] = []

    for entry in entries:
        abs_src: Path = entry["src_abs"]
        arc: str = entry["src"]
        dest_in_work = work_dir / arc

        if entry["type"] == "file":
            dest_in_work.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_src, dest_in_work)
            rel_paths.append(Path(arc))
        elif entry["type"] == "directory":
            for p in abs_src.rglob("*"):
                if not p.is_file():
                    continue
                rel_in_dir = p.relative_to(abs_src)
                if _is_noise(rel_in_dir):
                    continue
                dest_file = dest_in_work / rel_in_dir
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest_file)
                rel_paths.append(Path(arc) / rel_in_dir)
            # If the directory exists but has no files, still add an empty marker dir
            if not dest_in_work.exists():
                dest_in_work.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"unknown entry type: {entry['type']!r}")

    rel_paths.sort()

    # Build manifest contents[] (without src_abs)
    manifest_entries = []
    for entry in entries:
        m = {k: v for k, v in entry.items() if k != "src_abs"}
        manifest_entries.append(m)

    return manifest_entries, rel_paths


def _content_checksum(work_dir: Path, rel_paths: list[Path]) -> tuple[str, int]:
    """SHA-256 over file content in sorted-rel-path order. Returns (hex, total_bytes)."""
    h = hashlib.sha256()
    total = 0
    for rel in rel_paths:
        p = work_dir / rel
        size = p.stat().st_size
        total += size
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return h.hexdigest(), total


def _write_manifest(work_dir: Path, stream: str, manifest_contents: list[dict],
                    checksum: str, decrypted_size: int, date_str: str) -> Path:
    """Write manifest.json into work_dir at root."""
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "synthos_version":  SYNTHOS_VERSION,
        "node_type":        NODE_TYPE,
        "stream":           stream,
        "pi_id":            PI_ID,
        "created_at":       _now_iso_z(),
        "date":             date_str,
        "checksum_sha256":  checksum,
        "size_bytes_decrypted": decrypted_size,
        "encryption":       {"algorithm": "fernet", "key_id": "primary"},
        "contents":         manifest_contents,
    }
    out = work_dir / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False))
    return out


def _build_tarball(work_dir: Path, rel_paths: list[Path], out_path: Path) -> Path:
    """Write tar.gz with manifest.json first, then content files in sorted order."""
    with tarfile.open(out_path, "w:gz") as tar:
        # manifest.json MUST be first member so installer can extract it cheaply
        tar.add(work_dir / "manifest.json", arcname="manifest.json")
        for rel in rel_paths:
            tar.add(work_dir / rel, arcname=str(rel))
    return out_path


def _verify_tar_content_checksum(tar_path: Path, expected: str) -> bool:
    """Re-compute content checksum from a tar.gz and compare to expected."""
    h = hashlib.sha256()
    with tarfile.open(tar_path, "r:gz") as tar:
        members = sorted(
            (m for m in tar.getmembers()
             if m.isfile() and m.name != "manifest.json"),
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


def _encrypt_and_roundtrip(plaintext_path: Path, expected_content_checksum: str,
                           tmp_dir: Path) -> tuple[Path, str]:
    """
    Fernet-encrypt plaintext_path → .enc. Round-trip-verify by decrypting in
    memory, recomputing content checksum from the decrypted tar.gz, comparing
    to expected_content_checksum. Aborts on mismatch.

    Returns (encrypted_path, sha256_of_ciphertext).
    """
    f = _fernet()
    plaintext = plaintext_path.read_bytes()
    ciphertext = f.encrypt(plaintext)

    enc_path = tmp_dir / (plaintext_path.name + ".enc")
    enc_path.write_bytes(ciphertext)
    cipher_checksum = _sha256_bytes(ciphertext)

    # Round-trip: decrypt the ciphertext we just produced; write to a temp tar; verify.
    decrypted = f.decrypt(ciphertext)
    if decrypted != plaintext:
        raise RuntimeError("round-trip BYTE MISMATCH after encrypt+decrypt — aborting")

    rt_tar = tmp_dir / "roundtrip_check.tar.gz"
    rt_tar.write_bytes(decrypted)
    if not _verify_tar_content_checksum(rt_tar, expected_content_checksum):
        raise RuntimeError(
            "round-trip CONTENT CHECKSUM mismatch — manifest's checksum_sha256 "
            "does not match decrypted tar contents. Aborting."
        )
    rt_tar.unlink()

    log.info("[%s] round-trip verify OK (cipher sha256 %s...)",
             plaintext_path.stem, cipher_checksum[:16])
    return enc_path, cipher_checksum


def build_stream_encrypted(stream: str, tmp_dir: Path) -> tuple[Path, dict]:
    """
    End-to-end build for a single stream.
    Returns (encrypted_archive_path, info_dict).
    info_dict has: stream, plaintext_path, plaintext_size, decrypted_content_checksum,
                    encrypted_size, encrypted_checksum, customer_count (if customer)
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    work_dir = tmp_dir / f"{stream}_root"
    work_dir.mkdir()

    # 1. Stage files into canonical layout
    manifest_contents, rel_paths = _stage_stream(stream, work_dir)

    # 2. Compute content checksum + total decrypted size
    content_checksum, total_bytes = _content_checksum(work_dir, rel_paths)

    # 3. Write manifest.json
    _write_manifest(work_dir, stream, manifest_contents, content_checksum,
                    total_bytes, date_str)

    # 4. Build .tar.gz (manifest first, then content sorted)
    plaintext_name = f"synthos_backup_{stream}_{PI_ID}_{date_str}.tar.gz"
    plaintext_path = tmp_dir / plaintext_name
    _build_tarball(work_dir, rel_paths, plaintext_path)
    plaintext_size = plaintext_path.stat().st_size

    # Self-check: reading back the tar matches expected checksum (sanity, before encrypt)
    if not _verify_tar_content_checksum(plaintext_path, content_checksum):
        raise RuntimeError(
            f"Self-check FAIL: tar content checksum != manifest checksum for stream {stream}"
        )

    # 5. Encrypt + round-trip verify
    enc_path, enc_checksum = _encrypt_and_roundtrip(plaintext_path, content_checksum, tmp_dir)
    enc_size = enc_path.stat().st_size

    customer_count = None
    if stream == "customer":
        customers_dir = _DATA_DIR / "customers"
        if customers_dir.exists():
            customer_count = sum(1 for _ in customers_dir.iterdir() if _.is_dir())

    info = {
        "stream":            stream,
        "plaintext_path":    plaintext_path,
        "plaintext_size":    plaintext_size,
        "decrypted_content_checksum": content_checksum,
        "decrypted_total_bytes":      total_bytes,
        "encrypted_path":    enc_path,
        "encrypted_size":    enc_size,
        "encrypted_checksum": enc_checksum,
        "manifest_contents": manifest_contents,
        "customer_count":    customer_count,
        "date":              date_str,
    }
    log.info("[%s] built %s — plaintext %.1f KB → encrypted %.1f KB (%d files)",
             stream, plaintext_name, plaintext_size / 1024, enc_size / 1024, len(rel_paths))
    return enc_path, info


# ── UPLOAD ─────────────────────────────────────────────────────────────────────

def upload_stream(encrypted_path: Path, stream: str) -> bool:
    """POST encrypted .tar.gz.enc to company Pi /receive_backup with stream field."""
    if not COMPANY_URL:
        log.error("COMPANY_URL not set — cannot upload")
        return False
    if not SECRET_TOKEN:
        log.error("SECRET_TOKEN not set — cannot authenticate")
        return False

    url = f"{COMPANY_URL}/receive_backup"
    log.info("[%s] uploading %s to %s", stream, encrypted_path.name, url)

    try:
        with open(encrypted_path, "rb") as fh:
            resp = requests.post(
                url,
                data={"pi_id": PI_ID, "stream": stream},
                files={"archive": (encrypted_path.name, fh, "application/octet-stream")},
                headers={"X-Token": SECRET_TOKEN},
                timeout=UPLOAD_TIMEOUT,
            )
        if resp.status_code == 200:
            log.info("[%s] upload OK — %s", stream, resp.json().get("staged", ""))
            return True
        log.error("[%s] upload FAIL — HTTP %d: %s", stream, resp.status_code, resp.text[:200])
        return False
    except requests.Timeout:
        log.error("[%s] upload TIMEOUT after %ds — company Pi unreachable?",
                  stream, UPLOAD_TIMEOUT)
        return False
    except Exception as exc:
        log.error("[%s] upload error: %s", stream, exc)
        return False


# ── LOCAL STAGING + RETENTION ─────────────────────────────────────────────────

def save_local_copy(encrypted_path: Path) -> Path:
    """Copy .enc to backups/staging/. Returns the local copy path."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BACKUP_DIR / encrypted_path.name
    shutil.copy2(encrypted_path, dest)
    log.info("Local copy saved: %s", dest)
    return dest


def cleanup_old_local_copies(retention_days: int = LOCAL_RETENTION_DAYS) -> int:
    """Delete local backup copies (.enc and legacy .tar.gz) older than retention_days."""
    if not _BACKUP_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for f in _BACKUP_DIR.glob("synthos_backup_*"):
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

def run(dry_run: bool = False, local_only: bool = False,
        only_stream: str | None = None) -> bool:
    """
    Build, encrypt, verify each stream; save locally; upload (unless dry/local).
    Returns True if all streams succeeded end-to-end.
    """
    mode_tag = " [DRY RUN]" if dry_run else " [LOCAL]" if local_only else ""
    streams = (only_stream,) if only_stream else STREAMS
    log.info("=== Retail backup v2 started (pi_id=%s, streams=%s%s) ===",
             PI_ID, ",".join(streams), mode_tag)

    # Validate key early — fail fast if misconfigured
    try:
        _fernet()
    except EnvironmentError as exc:
        log.error("Backup aborted: %s", exc)
        return False

    all_ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            for stream in streams:
                if stream not in STREAMS:
                    log.error("unknown stream %r — skipping", stream)
                    all_ok = False
                    continue
                try:
                    enc_path, info = build_stream_encrypted(stream, tmp_path)
                except FileNotFoundError as exc:
                    log.error("[%s] required source missing: %s", stream, exc)
                    all_ok = False
                    continue
                except Exception as exc:
                    log.error("[%s] build failed: %s", stream, exc, exc_info=True)
                    all_ok = False
                    continue

                if dry_run:
                    log.info("[%s][DRY RUN] would upload %s (%.1f KB) to %s",
                             stream, enc_path.name, info["encrypted_size"] / 1024,
                             COMPANY_URL or "(COMPANY_URL not set)")
                    continue

                # Save local copy before upload
                save_local_copy(enc_path)

                if local_only:
                    continue

                if not upload_stream(enc_path, stream):
                    all_ok = False

            cleanup_old_local_copies()
    except Exception as exc:
        log.error("Unexpected error: %s", exc, exc_info=True)
        return False

    log.info("=== Retail backup v2 complete (overall_ok=%s) ===", all_ok)
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="retail_backup.py v2 — Retail Pi Backup Creator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build + encrypt + verify but don't upload or save locally")
    parser.add_argument("--local", action="store_true",
                        help="Build + encrypt + verify + save locally; no upload")
    parser.add_argument("--stream", choices=STREAMS,
                        help="Run only one stream (testing)")
    args = parser.parse_args()

    ok = run(dry_run=args.dry_run, local_only=args.local, only_stream=args.stream)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('backup', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

    main()
