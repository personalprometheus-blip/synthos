#!/usr/bin/env python3
"""Self-contained smoke test for rotate_encryption_key.py.

Builds a tiny in-memory-style auth.db copy in /tmp, populates it with
3 fake customer rows that mirror the real schema (email_enc, display_name_enc,
alpaca_key_enc, alpaca_secret_enc, phone_enc, email_hash), runs the
rotation tool's `rotate_db()` against it, then asserts:

  1. Every encrypted field decrypts cleanly with NEW key.
  2. Every encrypted field FAILS to decrypt with OLD key.
  3. email_hash equals HMAC(NEW_KEY, plaintext_email).
  4. The original plaintext is preserved (round-trip identity).
  5. Empty fields stay empty.

Usage:
    python3 tools/rotate_encryption_key_test.py
    # exit 0 = all assertions pass
    # exit 1 = something failed
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from cryptography.fernet import Fernet, InvalidToken

from rotate_encryption_key import (
    email_lookup_hash,
    fernet_from,
    rotate_db,
)


def _build_fixture_db(path: Path, key: str) -> list:
    """Create a fresh auth.db-shaped DB at `path`, return the inserted rows
    as plaintext for later assertion."""
    f = fernet_from(key)
    key_bytes = key.encode()
    rows = [
        # (id, email, display_name, alpaca_key, alpaca_secret, phone)
        ("c1", "alice@example.com", "Alice A.", "AKEY1", "ASECRET1", "+15555550101"),
        ("c2", "bob@example.com",   "",         "",      "",         ""),  # empties
        ("c3", "carol@example.com", "Carol C.", "AKEY3", "ASECRET3", ""),
    ]
    with sqlite3.connect(str(path)) as c:
        c.execute("""
            CREATE TABLE customers (
                id                  TEXT PRIMARY KEY,
                email_hash          TEXT NOT NULL UNIQUE,
                email_enc           BLOB NOT NULL,
                display_name_enc    BLOB,
                alpaca_key_enc      BLOB,
                alpaca_secret_enc   BLOB,
                phone_enc           BLOB
            )
        """)
        for r_id, email, name, akey, asecret, phone in rows:
            c.execute(
                "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r_id,
                    email_lookup_hash(email, key_bytes),
                    f.encrypt(email.encode()),
                    f.encrypt(name.encode()) if name else b"",
                    f.encrypt(akey.encode()) if akey else b"",
                    f.encrypt(asecret.encode()) if asecret else b"",
                    f.encrypt(phone.encode()) if phone else b"",
                ),
            )
        c.commit()
    return rows


def main() -> int:
    OLD = Fernet.generate_key().decode()
    NEW = Fernet.generate_key().decode()
    assert OLD != NEW, "Fernet.generate_key() must return distinct values"

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "auth_test.db"
        truth = _build_fixture_db(db, OLD)

        log_lines = []
        def log(msg, err=False):
            log_lines.append(("ERR " if err else "    ") + msg)

        stats = rotate_db(db, OLD, NEW, log)

        # ── Assertion 1: rotate_db verification passed
        assert stats["verification_pass"], f"Internal verification failed: {stats}"
        # ── Assertion 2: stats counts match fixture
        assert stats["total_rows_scanned"] == 3, f"Expected 3 rows, got {stats['total_rows_scanned']}"
        # 3 rows × 5 enc cols = 15 max. c1 has all 5, c2 has 1 (email only), c3 has 4
        # (no phone). Total non-empty enc fields = 5 + 1 + 4 = 10.
        assert stats["total_fields_rotated"] == 10, (
            f"Expected 10 fields rotated, got {stats['total_fields_rotated']}"
        )
        # Each row has 1 hash to recompute (email).
        assert stats["total_hashes_recomputed"] == 3, (
            f"Expected 3 hashes recomputed, got {stats['total_hashes_recomputed']}"
        )

        # ── Assertion 3-5: round-trip every row with NEW key, check OLD now fails
        f_old = fernet_from(OLD)
        f_new = fernet_from(NEW)
        new_key_bytes = NEW.encode()

        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            actual = c.execute(
                "SELECT id, email_hash, email_enc, display_name_enc, "
                "alpaca_key_enc, alpaca_secret_enc, phone_enc FROM customers"
            ).fetchall()

        for row, fixture in zip(actual, truth):
            r_id, email, name, akey, asecret, phone = fixture
            plain_map = {
                "email_enc": email,
                "display_name_enc": name,
                "alpaca_key_enc": akey,
                "alpaca_secret_enc": asecret,
                "phone_enc": phone,
            }
            for col, expected_plain in plain_map.items():
                ct = row[col]
                if not expected_plain:
                    assert (ct is None or ct == b""), (
                        f"{r_id}.{col} should be empty but isn't: {ct!r}"
                    )
                    continue
                # 3a — NEW key MUST decrypt
                try:
                    pt = f_new.decrypt(bytes(ct)).decode()
                except InvalidToken:
                    print(f"FAIL: NEW key cannot decrypt {r_id}.{col}", file=sys.stderr)
                    return 1
                assert pt == expected_plain, (
                    f"{r_id}.{col}: round-trip altered plaintext "
                    f"({expected_plain!r} → {pt!r})"
                )
                # 3b — OLD key MUST FAIL (rotation actually happened)
                try:
                    f_old.decrypt(bytes(ct))
                    print(f"FAIL: OLD key still decrypts {r_id}.{col} — rotation didn't run", file=sys.stderr)
                    return 1
                except InvalidToken:
                    pass  # expected

            # 3c — email_hash recomputed under NEW key
            expected_hash = email_lookup_hash(email, new_key_bytes)
            assert row["email_hash"] == expected_hash, (
                f"{r_id}.email_hash mismatch: stored {row['email_hash']!r} "
                f"vs expected {expected_hash!r}"
            )

    print("OK — all 5 assertion classes passed:")
    print("  1. rotate_db internal verification: PASS")
    print("  2. row & field counts match fixture")
    print("  3. NEW key decrypts every encrypted field round-trip")
    print("  4. OLD key fails on every rotated field (rotation actually applied)")
    print("  5. email_hash recomputed with NEW key matches HMAC(NEW, email)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
