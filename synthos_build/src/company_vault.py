"""
company_vault.py — Synthos License Key Manager
===============================================
Runs on the Company Pi alongside company_server.py.
Manages license keys for retail Pi nodes — issuing, validating, and revoking
them via a lightweight Flask API on port 5011.

Retail Pis call POST /api/validate at boot (via retail_boot_sequence.py) to
confirm their license is active before starting trading sessions.

Routes:
    GET  /health                  — unauthenticated liveness check
    POST /api/validate            — validate a license key (X-Token auth)
    POST /api/issue               — issue a new license key (X-Token auth)
    POST /api/revoke              — revoke a license key (X-Token auth)
    GET  /api/keys                — list all keys (X-Token auth)

Authentication:
    All non-health routes require the X-Token header to match SECRET_TOKEN.
    This is the same token used by company_server.py — no additional credential.

.env required:
    SECRET_TOKEN=some_random_string   # shared with company_server

.env optional:
    VAULT_PORT=5011                   # Flask listen port (default 5011)
    VAULT_DB_PATH=data/vault.db       # SQLite path override
    VAULT_LOG=logs/vault.log          # log file path (default stdout only)
    COMPANY_DB_PATH=data/company.db   # not used directly, kept for consistency

Usage:
    python3 company_vault.py
    # or daemonized:
    nohup python3 company_vault.py >> logs/vault.log 2>&1 &
"""

import json
import logging
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_TOKEN = os.getenv("SECRET_TOKEN") or os.getenv("COMPANY_TOKEN", "")
VAULT_PORT   = int(os.getenv("VAULT_PORT", 5011))

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.getenv("VAULT_DB_PATH", os.path.join(DATA_DIR, "vault.db"))

_BUILD_DIR = os.path.dirname(_HERE)
LOG_DIR    = os.path.join(_BUILD_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

VAULT_LOG = os.getenv("VAULT_LOG", "")

# ── Logging ───────────────────────────────────────────────────────────────────
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if VAULT_LOG:
    os.makedirs(os.path.dirname(os.path.abspath(VAULT_LOG)), exist_ok=True)
    _log_handlers.append(logging.FileHandler(VAULT_LOG))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VAULT] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger("vault")

# ── Database ──────────────────────────────────────────────────────────────────
@contextmanager
def _db_conn():
    """Thread-safe SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create vault schema. Idempotent — safe to call on every startup."""
    with _db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS license_keys (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                key                 TEXT NOT NULL UNIQUE,
                customer_id         TEXT NOT NULL DEFAULT '',
                customer_name       TEXT NOT NULL DEFAULT '',
                pi_id               TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active','revoked','expired')),
                issued_at           TEXT NOT NULL,
                expires_at          TEXT,
                last_validated_at   TEXT,
                validation_count    INTEGER NOT NULL DEFAULT 0,
                notes               TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS validation_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT NOT NULL,
                pi_id       TEXT NOT NULL DEFAULT '',
                result      TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '',
                logged_at   TEXT NOT NULL
            );
        """)
    log.info("Vault DB ready at %s", DB_PATH)


# ── Auth ──────────────────────────────────────────────────────────────────────
def _token_required(f):
    """Decorator: require X-Token header matching SECRET_TOKEN."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Token", "")
        if not SECRET_TOKEN:
            log.warning("SECRET_TOKEN not set — all auth checks will fail.")
        if not secrets.compare_digest(token, SECRET_TOKEN):
            log.warning("Rejected request to %s — bad token", request.path)
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Key generation ────────────────────────────────────────────────────────────
def _generate_key() -> str:
    """Generate a cryptographically random license key in XXXX-XXXX-XXXX-XXXX format."""
    raw = secrets.token_hex(8).upper()
    return "-".join(raw[i:i+4] for i in range(0, 16, 4))


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    """Unauthenticated liveness probe."""
    return jsonify({"ok": True, "service": "company_vault", "port": VAULT_PORT}), 200


@app.route("/api/validate", methods=["POST"])
@_token_required
def api_validate():
    """
    Validate a license key.

    Body (JSON): { "key": "XXXX-XXXX-XXXX-XXXX", "pi_id": "synthos-pi-1" }
    Returns:     { "ok": true|false, "status": "active"|..., "customer_id": ..., "error": ... }

    Increments validation_count and records last_validated_at on success.
    All validation attempts (pass/fail) are written to validation_log.
    """
    body = request.get_json(silent=True) or {}
    key    = (body.get("key") or "").strip().upper()
    pi_id  = (body.get("pi_id") or "").strip()

    if not key:
        return jsonify({"ok": False, "error": "key is required"}), 400

    now = datetime.now(timezone.utc).isoformat()

    with _db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM license_keys WHERE key = ?", (key,)
        ).fetchone()

        if not row:
            _log_validation(conn, key, pi_id, "rejected", "key not found", now)
            log.warning("Validation rejected: key not found pi_id=%s", pi_id)
            return jsonify({"ok": False, "status": "unknown", "error": "Key not found"}), 404

        if row["status"] != "active":
            _log_validation(conn, key, pi_id, "rejected", f"status={row['status']}", now)
            log.warning("Validation rejected: key status=%s pi_id=%s", row["status"], pi_id)
            return jsonify({
                "ok": False,
                "status": row["status"],
                "error": f"Key is {row['status']}",
            }), 403

        # Check expiry if set
        if row["expires_at"]:
            if now > row["expires_at"]:
                conn.execute(
                    "UPDATE license_keys SET status='expired' WHERE key=?", (key,)
                )
                _log_validation(conn, key, pi_id, "rejected", "expired", now)
                log.warning("Validation rejected: key expired pi_id=%s", pi_id)
                return jsonify({"ok": False, "status": "expired", "error": "Key has expired"}), 403

        # Success — update stats
        conn.execute(
            """UPDATE license_keys
               SET last_validated_at=?, validation_count=validation_count+1, pi_id=?
               WHERE key=?""",
            (now, pi_id, key),
        )
        _log_validation(conn, key, pi_id, "ok", "valid", now)

    log.info("License validated ok key=%.9s... pi_id=%s", key, pi_id)
    return jsonify({
        "ok":          True,
        "status":      "active",
        "customer_id": row["customer_id"],
        "customer_name": row["customer_name"],
        "expires_at":  row["expires_at"],
    }), 200


def _log_validation(conn, key: str, pi_id: str, result: str, detail: str, now: str):
    conn.execute(
        "INSERT INTO validation_log (key, pi_id, result, detail, logged_at) VALUES (?,?,?,?,?)",
        (key, pi_id, result, detail, now),
    )


@app.route("/api/issue", methods=["POST"])
@_token_required
def api_issue():
    """
    Issue a new license key.

    Body (JSON): {
        "customer_id":   "cust_abc123",    (required)
        "customer_name": "Jane Smith",     (optional)
        "pi_id":         "synthos-pi-1",   (optional — may be set later)
        "expires_at":    "2027-01-01T00:00:00+00:00",  (optional — omit for perpetual)
        "notes":         "Early adopter",  (optional)
    }
    Returns: { "ok": true, "key": "XXXX-XXXX-XXXX-XXXX", "customer_id": ... }
    """
    body        = request.get_json(silent=True) or {}
    customer_id = (body.get("customer_id") or "").strip()
    if not customer_id:
        return jsonify({"ok": False, "error": "customer_id is required"}), 400

    customer_name = (body.get("customer_name") or "").strip()
    pi_id         = (body.get("pi_id") or "").strip()
    expires_at    = (body.get("expires_at") or "").strip() or None
    notes         = (body.get("notes") or "").strip()
    now           = datetime.now(timezone.utc).isoformat()

    new_key = _generate_key()

    with _db_conn() as conn:
        conn.execute(
            """INSERT INTO license_keys
               (key, customer_id, customer_name, pi_id, status, issued_at, expires_at, notes)
               VALUES (?,?,?,?,'active',?,?,?)""",
            (new_key, customer_id, customer_name, pi_id, now, expires_at, notes),
        )

    log.info("Key issued key=%.9s... customer_id=%s", new_key, customer_id)
    return jsonify({
        "ok":          True,
        "key":         new_key,
        "customer_id": customer_id,
        "issued_at":   now,
        "expires_at":  expires_at,
    }), 201


@app.route("/api/revoke", methods=["POST"])
@_token_required
def api_revoke():
    """
    Revoke a license key.

    Body (JSON): { "key": "XXXX-XXXX-XXXX-XXXX", "reason": "..." }
    Returns:     { "ok": true, "key": "...", "status": "revoked" }
    """
    body   = request.get_json(silent=True) or {}
    key    = (body.get("key") or "").strip().upper()
    reason = (body.get("reason") or "").strip()

    if not key:
        return jsonify({"ok": False, "error": "key is required"}), 400

    with _db_conn() as conn:
        row = conn.execute(
            "SELECT id, status FROM license_keys WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Key not found"}), 404
        if row["status"] == "revoked":
            return jsonify({"ok": True, "key": key, "status": "revoked", "note": "already revoked"}), 200

        notes_append = f" | revoked: {reason}" if reason else " | revoked"
        conn.execute(
            "UPDATE license_keys SET status='revoked', notes=notes||? WHERE key=?",
            (notes_append, key),
        )

    log.info("Key revoked key=%.9s... reason=%s", key, reason or "(none)")
    return jsonify({"ok": True, "key": key, "status": "revoked"}), 200


@app.route("/api/keys", methods=["GET"])
@_token_required
def api_keys():
    """
    List all license keys.

    Query params:
        status=active|revoked|expired   (optional filter)
        customer_id=...                 (optional filter)
    Returns: { "ok": true, "count": N, "keys": [...] }
    """
    status_filter      = request.args.get("status", "").strip().lower() or None
    customer_id_filter = request.args.get("customer_id", "").strip() or None

    query  = "SELECT * FROM license_keys WHERE 1=1"
    params: list = []

    if status_filter:
        query  += " AND status=?"
        params.append(status_filter)
    if customer_id_filter:
        query  += " AND customer_id=?"
        params.append(customer_id_filter)

    query += " ORDER BY issued_at DESC"

    with _db_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    keys = [dict(r) for r in rows]
    log.info("Keys listed count=%d status_filter=%s", len(keys), status_filter)
    return jsonify({"ok": True, "count": len(keys), "keys": keys}), 200


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SECRET_TOKEN:
        log.warning("SECRET_TOKEN is not set — all authenticated routes will return 401.")

    init_db()

    print(f"[Vault] License key manager running on port {VAULT_PORT}")
    print(f"[Vault] Database: {DB_PATH}")
    app.run(host="0.0.0.0", port=VAULT_PORT, debug=False)
