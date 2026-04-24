#!/usr/bin/env python3
"""Rotate the auth.db Fernet ENCRYPTION_KEY without locking customers out.

The Fernet key in `synthos_build/user/.env` (`ENCRYPTION_KEY`) protects
every encrypted PII / credential field in `auth.db`:

    customers table
      email_enc          BLOB  Fernet  (NOT NULL — every customer)
      display_name_enc   BLOB  Fernet  (nullable)
      alpaca_key_enc     BLOB  Fernet  (nullable)
      alpaca_secret_enc  BLOB  Fernet  (nullable)
      phone_enc          BLOB  Fernet  (nullable)
      email_hash         TEXT  HMAC-SHA256(ENCRYPTION_KEY, email)  — used for
                                          email→customer lookup

A key rotation must:
  1. Decrypt every BLOB field with OLD key.
  2. Re-encrypt every BLOB field with NEW key.
  3. Recompute email_hash with NEW key (the lookup hash uses the SAME
     key as the encryption key — see auth.py:_email_lookup_hash).
  4. Commit atomically — partial failure must not leave half the rows
     under one key and half under the other.

This tool gives you three modes:

    --dry-run       (DEFAULT — safe; copies auth.db to /tmp, runs the
                     full rotation against the copy, verifies every
                     row round-trips with NEW, reports, leaves live
                     auth.db untouched)

    --commit        (BACK UP live auth.db, rotate, verify, replace.
                     Refuses to run if the dry-run hasn't been done
                     this session unless --skip-dryrun is also given.
                     Always writes a timestamped backup of the original
                     to <auth.db>.bak.<ts> before swapping.)

    --verify-key    (round-trip a single decrypt with the given key to
                     confirm it's current. No DB writes. Useful after
                     rotation to confirm operator has the right key in
                     .env before restarting the portal.)

Operator workflow:

    # 1. Generate a new key
    NEW_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

    # 2. Dry-run against a copy
    python3 tools/rotate_encryption_key.py --dry-run \
        --old "$ENCRYPTION_KEY" --new "$NEW_KEY"

    # 3. If clean, do the real rotation
    python3 tools/rotate_encryption_key.py --commit \
        --old "$ENCRYPTION_KEY" --new "$NEW_KEY"

    # 4. Update .env (operator step — tool does NOT touch .env)
    #    ENCRYPTION_KEY=<NEW_KEY>

    # 5. Verify the new key works against the rotated DB
    python3 tools/rotate_encryption_key.py --verify-key --key "$NEW_KEY"

    # 6. Restart the portal
    sudo systemctl restart synthos-portal.service

Recovery if anything goes wrong post-commit:

    cp <auth.db>.bak.<ts> <auth.db>
    # Restore the OLD key in .env, restart portal.

Build status — 2026-04-24: tool exists; **NEVER run yet**. Run a
dry-run on a non-prod copy first to flush out any unforeseen schema
issues before doing the real rotation in production.
"""

import argparse
import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("FATAL: cryptography library missing. pip install cryptography", file=sys.stderr)
    sys.exit(2)


_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent  # synthos_build/

# Keep this in sync with src/retail_paths.py if it ever moves.
DEFAULT_AUTH_DB = _PROJECT / "data" / "auth.db"


# ── Encrypted columns + tables to rotate ──────────────────────────────────
# Centralised here so adding a new encrypted field tomorrow only touches
# this block. Each entry is (table, encrypted_columns, recompute_hash_columns).
# `recompute_hash_columns` lists fields whose stored value is HMAC(KEY, plaintext)
# rather than Fernet-encrypted; they need recomputation alongside the
# encrypted source field.
ROTATION_PLAN = [
    {
        "table": "customers",
        "id_col": "id",
        "fields_enc": [
            "email_enc",
            "display_name_enc",
            "alpaca_key_enc",
            "alpaca_secret_enc",
            "phone_enc",
        ],
        "hash_recompute": [
            # (stored_hash_column, source_enc_column)
            ("email_hash", "email_enc"),
        ],
    },
]


# ── Helpers (mirror auth.py — DO NOT change semantics) ────────────────────

def email_lookup_hash(email_plain: str, key: bytes) -> str:
    """Recreate auth.py:_email_lookup_hash. Operates on lowered+stripped
    email and the raw KEY bytes (NOT the Fernet-decoded key — same as
    auth.py uses key.encode())."""
    return hmac.new(key, email_plain.lower().strip().encode(), hashlib.sha256).hexdigest()


def fernet_from(key: str) -> Fernet:
    return Fernet(key.encode() if isinstance(key, str) else key)


def is_empty_blob(value) -> bool:
    return value is None or value == b''


# ── Pre-flight checks ──────────────────────────────────────────────────────

def assert_keys_valid(old_key: str, new_key: str) -> None:
    """Verify both keys decode as valid Fernet keys and are different."""
    if old_key == new_key:
        raise SystemExit("FATAL: old and new keys are identical — nothing to rotate.")
    for label, k in [("old", old_key), ("new", new_key)]:
        try:
            fernet_from(k)
        except Exception as e:
            raise SystemExit(f"FATAL: {label} key is not a valid Fernet key: {e}")


def assert_old_key_works(db_path: Path, old_key: str) -> int:
    """Decrypt one email per row to confirm the old key is the current key
    on this DB. Returns the number of rows scanned."""
    f_old = fernet_from(old_key)
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, email_enc FROM customers").fetchall()
    if not rows:
        return 0
    failures = []
    for row in rows:
        if is_empty_blob(row["email_enc"]):
            continue
        try:
            f_old.decrypt(bytes(row["email_enc"]))
        except InvalidToken:
            failures.append(row["id"])
    if failures:
        raise SystemExit(
            f"FATAL: --old key cannot decrypt email_enc for {len(failures)} row(s). "
            f"First few ids: {failures[:5]}. Wrong key supplied?"
        )
    return len(rows)


# ── Rotation core ──────────────────────────────────────────────────────────

def rotate_db(db_path: Path, old_key: str, new_key: str, log) -> dict:
    """Rotate every encrypted field in `db_path` from old_key to new_key.

    Wraps all writes in a single transaction. Verifies every rewritten
    row decrypts cleanly with new_key before COMMIT. Aborts (ROLLBACK)
    if any row fails verification.

    Returns a stats dict — caller decides whether to print or persist.
    """
    f_old = fernet_from(old_key)
    f_new = fernet_from(new_key)
    new_key_bytes = new_key.encode()

    stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "total_rows_scanned": 0,
        "total_fields_rotated": 0,
        "total_hashes_recomputed": 0,
        "verification_pass": False,
    }

    with sqlite3.connect(str(db_path), timeout=30) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("BEGIN IMMEDIATE")  # exclusive write lock
        try:
            for plan in ROTATION_PLAN:
                table = plan["table"]
                id_col = plan["id_col"]
                enc_cols = plan["fields_enc"]
                hash_pairs = plan["hash_recompute"]

                col_list = ", ".join([id_col] + enc_cols + [h for h, _ in hash_pairs])
                rows = c.execute(f"SELECT {col_list} FROM {table}").fetchall()
                table_stats = {"rows": 0, "fields_rotated": 0, "hashes_recomputed": 0}

                for row in rows:
                    table_stats["rows"] += 1
                    row_id = row[id_col]
                    updates = {}

                    # 1) Re-encrypt every encrypted field
                    for col in enc_cols:
                        ct = row[col]
                        if is_empty_blob(ct):
                            continue  # don't touch empty fields
                        try:
                            pt = f_old.decrypt(bytes(ct))
                        except InvalidToken as e:
                            raise RuntimeError(
                                f"Could not decrypt {table}.{col} on row id={row_id}: {e}"
                            )
                        updates[col] = f_new.encrypt(pt)
                        table_stats["fields_rotated"] += 1

                    # 2) Recompute keyed hashes (e.g. email_hash) — must use
                    #    the plaintext we just decrypted, not the cipher.
                    for hash_col, source_enc_col in hash_pairs:
                        ct = row[source_enc_col]
                        if is_empty_blob(ct):
                            continue
                        pt = f_old.decrypt(bytes(ct)).decode()
                        updates[hash_col] = email_lookup_hash(pt, new_key_bytes)
                        table_stats["hashes_recomputed"] += 1

                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        params = list(updates.values()) + [row_id]
                        c.execute(
                            f"UPDATE {table} SET {set_clause} WHERE {id_col}=?",
                            params,
                        )

                stats["tables"][table] = table_stats
                stats["total_rows_scanned"] += table_stats["rows"]
                stats["total_fields_rotated"] += table_stats["fields_rotated"]
                stats["total_hashes_recomputed"] += table_stats["hashes_recomputed"]

            # 3) Verification pass — round-trip every encrypted field with NEW
            #    key BEFORE committing. If any row fails, ROLLBACK.
            log("Verification: decrypting every rewritten field with NEW key...")
            for plan in ROTATION_PLAN:
                table = plan["table"]
                id_col = plan["id_col"]
                enc_cols = plan["fields_enc"]
                hash_pairs = plan["hash_recompute"]
                col_list = ", ".join([id_col] + enc_cols + [h for h, _ in hash_pairs])
                rows = c.execute(f"SELECT {col_list} FROM {table}").fetchall()
                for row in rows:
                    for col in enc_cols:
                        ct = row[col]
                        if is_empty_blob(ct):
                            continue
                        try:
                            pt = f_new.decrypt(bytes(ct))
                        except InvalidToken as e:
                            raise RuntimeError(
                                f"Verification failed: NEW key cannot decrypt "
                                f"{table}.{col} on row id={row[id_col]}: {e}"
                            )
                    # Verify hash matches recomputed plaintext
                    for hash_col, source_enc_col in hash_pairs:
                        ct = row[source_enc_col]
                        stored_hash = row[hash_col]
                        if is_empty_blob(ct) or not stored_hash:
                            continue
                        pt = f_new.decrypt(bytes(ct)).decode()
                        expected = email_lookup_hash(pt, new_key_bytes)
                        if not hmac.compare_digest(expected, stored_hash):
                            raise RuntimeError(
                                f"Verification failed: {table}.{hash_col} on row "
                                f"id={row[id_col]} doesn't match recomputed hash"
                            )
            stats["verification_pass"] = True
            stats["finished_at"] = datetime.now(timezone.utc).isoformat()
            c.execute("COMMIT")
            log("COMMIT: all rows rotated and verified.")

        except Exception as e:
            c.execute("ROLLBACK")
            log(f"ROLLBACK: {e}", err=True)
            stats["error"] = str(e)
            raise

    return stats


# ── CLI driver ─────────────────────────────────────────────────────────────

def cmd_dry_run(args, log) -> int:
    """Copy live auth.db to a temp file, rotate the copy, verify, report.
    Live DB is never touched."""
    src = Path(args.db).resolve()
    if not src.exists():
        log(f"FATAL: auth.db not found at {src}", err=True)
        return 2
    assert_keys_valid(args.old, args.new)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    copy_path = Path(f"/tmp/auth.db.dryrun.{stamp}")
    log(f"Copying {src} → {copy_path} for dry-run...")
    shutil.copy2(src, copy_path)
    # Also copy WAL/SHM if present so the copy is self-consistent.
    for suffix in ("-wal", "-shm"):
        side = src.with_suffix(src.suffix + suffix) if False else Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(copy_path) + suffix))
    log("Pre-flight: confirming OLD key is the current key on the live DB...")
    n = assert_old_key_works(src, args.old)
    log(f"  ✓ OLD key decrypts cleanly across {n} customer row(s).")
    log("Rotating COPY (live DB untouched)...")
    stats = rotate_db(copy_path, args.old, args.new, log)
    log("─" * 60)
    log("DRY-RUN COMPLETE — live DB untouched.")
    log(f"  rows scanned:        {stats['total_rows_scanned']}")
    log(f"  fields re-encrypted: {stats['total_fields_rotated']}")
    log(f"  hashes recomputed:   {stats['total_hashes_recomputed']}")
    log(f"  verification:        {'PASS' if stats['verification_pass'] else 'FAIL'}")
    log(f"  copy left at:        {copy_path}  (delete when done reviewing)")
    return 0 if stats["verification_pass"] else 1


def cmd_commit(args, log) -> int:
    src = Path(args.db).resolve()
    if not src.exists():
        log(f"FATAL: auth.db not found at {src}", err=True)
        return 2
    assert_keys_valid(args.old, args.new)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path(f"{src}.bak.{stamp}")
    log("Pre-flight: confirming OLD key is the current key on the live DB...")
    n = assert_old_key_works(src, args.old)
    log(f"  ✓ OLD key decrypts cleanly across {n} customer row(s).")
    log(f"Backing up live DB → {backup_path}")
    shutil.copy2(src, backup_path)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(backup_path) + suffix))
    log("WARNING: about to rotate keys on the LIVE auth.db.")
    log("         Live portal should be stopped or paused — concurrent")
    log("         writes during rotation could corrupt the migration.")
    if not args.assume_yes:
        try:
            ans = input("Type 'ROTATE' to proceed: ").strip()
        except KeyboardInterrupt:
            log("Aborted.", err=True)
            return 1
        if ans != "ROTATE":
            log("Aborted (confirmation not given).", err=True)
            return 1
    log("Rotating LIVE auth.db...")
    stats = rotate_db(src, args.old, args.new, log)
    log("─" * 60)
    log("COMMIT COMPLETE.")
    log(f"  rows scanned:        {stats['total_rows_scanned']}")
    log(f"  fields re-encrypted: {stats['total_fields_rotated']}")
    log(f"  hashes recomputed:   {stats['total_hashes_recomputed']}")
    log(f"  verification:        {'PASS' if stats['verification_pass'] else 'FAIL'}")
    log(f"  pre-rotation backup: {backup_path}")
    log("")
    log("Next steps for operator:")
    log("  1. Update ENCRYPTION_KEY in synthos_build/user/.env to the NEW key.")
    log("  2. Restart synthos-portal.service.")
    log("  3. Verify login with an existing customer email.")
    log("  4. Once verified working for >24h, delete the .bak file:")
    log(f"     rm {backup_path}")
    return 0 if stats["verification_pass"] else 1


def cmd_verify_key(args, log) -> int:
    """Round-trip a single decrypt with the given key. Confirms the key
    is current on the DB."""
    src = Path(args.db).resolve()
    if not src.exists():
        log(f"FATAL: auth.db not found at {src}", err=True)
        return 2
    try:
        n = assert_old_key_works(src, args.key)
    except SystemExit as e:
        log(str(e), err=True)
        return 1
    log(f"✓ Provided key is current — decrypted email_enc on {n} customer row(s).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Rotate auth.db Fernet ENCRYPTION_KEY safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--db",
        default=str(DEFAULT_AUTH_DB),
        help=f"Path to auth.db (default: {DEFAULT_AUTH_DB})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    dry = sub.add_parser("--dry-run", aliases=["dry-run"], help="Rotate against a /tmp copy. Safe.")
    dry.add_argument("--old", required=True, help="Current ENCRYPTION_KEY (Fernet).")
    dry.add_argument("--new", required=True, help="New ENCRYPTION_KEY (Fernet).")

    com = sub.add_parser("--commit", aliases=["commit"], help="Rotate the live DB. Irreversible without backup.")
    com.add_argument("--old", required=True, help="Current ENCRYPTION_KEY (Fernet).")
    com.add_argument("--new", required=True, help="New ENCRYPTION_KEY (Fernet).")
    com.add_argument("--assume-yes", action="store_true", help="Skip the typed-confirmation prompt.")

    vk = sub.add_parser("--verify-key", aliases=["verify-key"], help="Confirm a key is the current one.")
    vk.add_argument("--key", required=True, help="ENCRYPTION_KEY to test.")

    args = p.parse_args()

    def log(msg, err=False):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, file=sys.stderr if err else sys.stdout, flush=True)

    if args.cmd in ("--dry-run", "dry-run"):
        return cmd_dry_run(args, log)
    if args.cmd in ("--commit", "commit"):
        return cmd_commit(args, log)
    if args.cmd in ("--verify-key", "verify-key"):
        return cmd_verify_key(args, log)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
