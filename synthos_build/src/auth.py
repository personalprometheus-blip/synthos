"""
auth.py — Customer account management for Synthos.

Handles customer accounts, password hashing, and encryption of PII and
Alpaca API credentials. Uses a separate auth.db isolated from trading data.

Encryption key setup (run once, add output to .env as ENCRYPTION_KEY):
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Admin account setup (.env):
  ADMIN_EMAIL=you@example.com
  ADMIN_PASSWORD=your-strong-password
  ADMIN_NAME=Your Name

Called from retail_portal.py on startup:
  auth.init_auth_db()
  auth.ensure_admin_account()
"""

import os
import sqlite3
import hashlib
import hmac as _hmac
import secrets
import uuid
import logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from cryptography.fernet import Fernet
from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # src/
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)                  # synthos_build/

load_dotenv(os.path.join(_ROOT_DIR, 'user', '.env'))

log = logging.getLogger('auth')

AUTH_DB_PATH  = os.path.join(_ROOT_DIR, 'data', 'auth.db')
CUSTOMERS_DIR = os.path.join(_ROOT_DIR, 'data', 'customers')

# Shared invite code for public signup (set in .env)
SIGNUP_ACCESS_CODE = os.environ.get('SIGNUP_ACCESS_CODE', '')


# ── ENCRYPTION ─────────────────────────────────────────────────────────────
# Fernet symmetric encryption. Key must be set in .env as ENCRYPTION_KEY.
# All PII (email, display name) and Alpaca credentials are encrypted at rest.

def _get_fernet() -> Fernet:
    key = os.environ.get('ENCRYPTION_KEY', '')
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set in .env — cannot encrypt customer data. "
            "Generate with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_field(value: str) -> bytes:
    """Encrypt a string for storage. Returns empty bytes for empty input."""
    if not value:
        return b''
    return _get_fernet().encrypt(value.encode())


def decrypt_field(value: bytes) -> str:
    """Decrypt a stored field. Returns empty string for empty input."""
    if not value:
        return ''
    return _get_fernet().decrypt(bytes(value)).decode()


# ── PASSWORD HASHING ────────────────────────────────────────────────────────
# PBKDF2-HMAC-SHA256. 480,000 iterations per OWASP 2023 recommendation.
# Salt is generated fresh per password — stored as "salt_hex:dk_hex".

_PBKDF2_ITERATIONS = 480_000


def hash_password(password: str) -> str:
    """Hash a password for storage. Returns 'salt_hex:dk_hex'."""
    salt = secrets.token_bytes(32)
    dk   = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, _PBKDF2_ITERATIONS)
    return salt.hex() + ':' + dk.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time password verification."""
    try:
        salt_hex, dk_hex = stored_hash.split(':')
        salt  = bytes.fromhex(salt_hex)
        dk    = bytes.fromhex(dk_hex)
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, _PBKDF2_ITERATIONS)
        return _hmac.compare_digest(check, dk)
    except Exception:
        return False


# ── EMAIL LOOKUP HASH ───────────────────────────────────────────────────────
# Email is stored encrypted (for display/notifications) and also as an
# HMAC-SHA256 keyed hash (for DB lookup without storing plaintext).

def _email_lookup_hash(email: str) -> str:
    key = os.environ.get('ENCRYPTION_KEY', '').encode()
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set in .env — cannot hash email for lookup. "
            "Generate with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return _hmac.new(key, email.lower().strip().encode(), hashlib.sha256).hexdigest()


# ── AUTH DATABASE ───────────────────────────────────────────────────────────

_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id                  TEXT    PRIMARY KEY,        -- UUID
    email_hash          TEXT    NOT NULL UNIQUE,    -- HMAC-SHA256 of email, for lookup
    email_enc           BLOB    NOT NULL,           -- Fernet-encrypted email
    display_name_enc    BLOB,                       -- Fernet-encrypted display name
    password_hash       TEXT    NOT NULL,           -- PBKDF2-HMAC-SHA256
    alpaca_key_enc      BLOB,                       -- Fernet-encrypted Alpaca API key
    alpaca_secret_enc   BLOB,                       -- Fernet-encrypted Alpaca secret
    role                TEXT    NOT NULL DEFAULT 'customer',  -- 'customer' or 'admin'
    is_active           INTEGER NOT NULL DEFAULT 1,
    operating_mode      TEXT    NOT NULL DEFAULT 'MANAGED',
    created_at          TEXT    NOT NULL,
    last_login          TEXT
);
"""


@contextmanager
def _auth_conn():
    c = sqlite3.connect(AUTH_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_auth_db():
    """Initialize auth.db schema and customers directory. Safe to call on every startup."""
    os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
    os.makedirs(CUSTOMERS_DIR, exist_ok=True)
    with _auth_conn() as c:
        c.executescript(_AUTH_SCHEMA)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                code        TEXT    PRIMARY KEY,
                created_at  TEXT    NOT NULL,
                created_by  TEXT    NOT NULL DEFAULT 'admin',
                used_at     TEXT,
                used_by     TEXT,
                is_used     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pending_signups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                email           TEXT    NOT NULL UNIQUE,
                phone           TEXT    NOT NULL,
                password_hash   TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
                customer_id     TEXT,            -- filled on approval
                created_at      TEXT    NOT NULL,
                reviewed_at     TEXT,
                reviewed_by     TEXT
            );
            -- Pending email changes (Phase 2.5 MED-3 — added 2026-04-25).
            -- /api/account/change-email creates a row here, sends a verify
            -- link to the NEW address + an alert to the OLD address.
            -- The actual email change to customers.email_hash/email_enc
            -- happens only when /verify-email-change/<token> is hit.
            -- 1h TTL. Single-use (consumed_at populated on success).
            CREATE TABLE IF NOT EXISTS pending_email_changes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT    NOT NULL,
                old_email_hash  TEXT    NOT NULL,   -- snapshot for safety / cleanup queries
                new_email_hash  TEXT    NOT NULL,   -- collision check at create + apply time
                new_email_enc   BLOB    NOT NULL,   -- Fernet-encrypted new email
                token           TEXT    NOT NULL UNIQUE,
                expires_at      TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                consumed_at     TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            CREATE INDEX IF NOT EXISTS idx_pec_customer  ON pending_email_changes(customer_id);
            CREATE INDEX IF NOT EXISTS idx_pec_token     ON pending_email_changes(token);
            CREATE INDEX IF NOT EXISTS idx_pec_expires   ON pending_email_changes(expires_at);

            -- Login attempts (Phase 2.5 MEDIUM-7 — added 2026-04-25).
            -- Persistent log of every login attempt (success + failure)
            -- with email_hash + IP + user-agent. Used for:
            --   • per-account lockout (10 failures/15min on same email_hash
            --     → block, regardless of source IP — defeats IP rotation
            --     by attackers)
            --   • per-IP lockout (20 failures/5min from same IP → block,
            --     regardless of email — defeats credential-stuffing across
            --     many target accounts)
            --   • triage / forensics — review user-agent + IP history when
            --     a customer reports a compromise
            -- Auto-pruned to last 7 days at write time for bounded growth.
            CREATE TABLE IF NOT EXISTS login_attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email_hash    TEXT,                       -- nullable: blank-email submissions
                ip            TEXT NOT NULL,
                user_agent    TEXT,
                success       INTEGER NOT NULL DEFAULT 0, -- 0=fail, 1=success
                attempted_at  TEXT NOT NULL               -- UTC ISO
            );
            CREATE INDEX IF NOT EXISTS idx_la_email_attempt ON login_attempts(email_hash, attempted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_la_ip_attempt    ON login_attempts(ip, attempted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_la_attempted_at  ON login_attempts(attempted_at);
        """)
    _migrate_auth_db()
    _migrate_pending_signups()
    # Restrict permissions — auth.db contains encrypted PII and password hashes
    os.chmod(AUTH_DB_PATH, 0o600)
    log.info("Auth DB initialized")


def _migrate_pending_signups():
    """Add ToS-acceptance columns to pending_signups. Idempotent."""
    new_columns = [
        ("tos_accepted_at", "TEXT"),
        ("tos_version",     "TEXT"),
        ("tos_ip",          "TEXT"),
        ("tos_user_agent",  "TEXT"),
    ]
    with _auth_conn() as c:
        for col_name, col_def in new_columns:
            try:
                c.execute(f"ALTER TABLE pending_signups ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass  # Column already exists


def _migrate_auth_db():
    """
    Safely add new columns to existing auth.db. Idempotent — safe to call on every startup.
    SQLite does not support ADD COLUMN IF NOT EXISTS, so we catch the duplicate-column error.

    v3.0 additions (subscription + verification pipeline):
      email_verified        — gate: must be 1 for portal access
      email_verify_token    — single-use token for /setup-account link
      email_verify_sent_at  — token issue time, used for 48h expiry
      stripe_customer_id    — links account to Stripe
      subscription_id       — Stripe subscription ID
      subscription_status   — inactive | active | trialing | past_due | cancelled
      subscription_ends_at  — current period end from Stripe
      grace_period_ends_at  — past_due grace cutoff (7 days from payment failure)
      pricing_tier          — early_adopter | standard
      pricing_locked_at     — timestamp when tier was locked in

    v3.1 additions (terms of service gate):
      tos_accepted_at       — UTC ISO timestamp when customer accepted ToS
      tos_version           — version string of accepted ToS (e.g. '1.0')
    """
    new_columns = [
        ("email_verified",       "INTEGER NOT NULL DEFAULT 0"),
        ("email_verify_token",   "TEXT"),
        ("password_reset_token", "TEXT"),
        ("password_reset_expires", "TEXT"),
        ("email_verify_sent_at", "TEXT"),
        ("stripe_customer_id",   "TEXT"),
        ("subscription_id",      "TEXT"),
        ("subscription_status",  "TEXT NOT NULL DEFAULT 'inactive'"),
        ("subscription_ends_at", "TEXT"),
        ("grace_period_ends_at", "TEXT"),
        ("pricing_tier",         "TEXT NOT NULL DEFAULT 'standard'"),
        ("pricing_locked_at",    "TEXT"),
        ("tos_accepted_at",      "TEXT"),
        ("tos_version",          "TEXT"),
            ("state",              "ALTER TABLE customers ADD COLUMN state TEXT"),
            ("zip_code",           "ALTER TABLE customers ADD COLUMN zip_code TEXT"),
        ("phone_enc",            "BLOB"),
    ]
    with _auth_conn() as c:
        for col_name, col_def in new_columns:
            try:
                c.execute(f"ALTER TABLE customers ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass  # Column already exists — expected on all runs after first


# ── ACCOUNT MANAGEMENT ──────────────────────────────────────────────────────

def create_customer(email: str, password: str, display_name: str = '',
                    role: str = 'customer', auto_activate: bool = False,
                    pricing_tier: str = 'standard') -> str:
    """
    Create a new customer account. Returns the customer ID (UUID).
    Raises ValueError if the email is already registered.

    auto_activate=True (used for admin-created accounts and the admin account itself):
      Sets email_verified=1 and subscription_status='active' so the account bypasses
      the email verification and subscription gate. Use for internally-provisioned accounts.

    auto_activate=False (default, used for Stripe-webhook-created accounts via
      create_unverified_customer()):
      Account is inactive until the customer completes /setup-account flow.

    pricing_tier: 'standard' (default) or 'early_adopter'. Locked in at creation
      when auto_activate=True; left NULL for Stripe-flow accounts until activation.
    """
    email       = email.lower().strip()
    customer_id = str(uuid.uuid4())
    email_hash  = _email_lookup_hash(email)
    now         = datetime.now(timezone.utc).isoformat()

    email_verified      = 1 if auto_activate else 0
    subscription_status = 'active' if auto_activate else 'inactive'
    pricing_locked_at   = now if auto_activate else None

    with _auth_conn() as c:
        existing = c.execute(
            "SELECT id FROM customers WHERE email_hash = ?", (email_hash,)
        ).fetchone()
        if existing:
            raise ValueError("An account already exists for this email address")

        c.execute(
            """INSERT INTO customers
               (id, email_hash, email_enc, display_name_enc, password_hash, role,
                email_verified, subscription_status, pricing_tier,
                pricing_locked_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                customer_id,
                email_hash,
                encrypt_field(email),
                encrypt_field(display_name) if display_name else b'',
                hash_password(password),
                role,
                email_verified,
                subscription_status,
                pricing_tier,
                pricing_locked_at,
                now,
            )
        )

    # Create per-customer data directory (holds their signals.db)
    os.makedirs(os.path.join(CUSTOMERS_DIR, customer_id), exist_ok=True)
    log.info(f"Created customer {customer_id} (role={role} tier={pricing_tier} auto_activate={auto_activate})")
    return customer_id


def get_customer_by_email(email: str):
    """Look up an active customer by email. Returns sqlite3.Row or None."""
    email_hash = _email_lookup_hash(email.lower().strip())
    with _auth_conn() as c:
        return c.execute(
            "SELECT * FROM customers WHERE email_hash = ? AND is_active = 1",
            (email_hash,)
        ).fetchone()


def get_customer_by_id(customer_id: str):
    """Look up an active customer by ID. Returns sqlite3.Row or None."""
    with _auth_conn() as c:
        return c.execute(
            "SELECT * FROM customers WHERE id = ? AND is_active = 1",
            (customer_id,)
        ).fetchone()


def record_login(customer_id: str):
    """Update last_login timestamp for a customer."""
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET last_login = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), customer_id)
        )


# ── LOGIN RATE LIMITING (Phase 2.5 MEDIUM-7 — added 2026-04-25) ─────────────
# Persistent per-account + per-IP failure tracking. Defeats two attack classes
# the in-memory limiter alone cannot:
#   • IP rotation: attacker brute-forces single account from many IPs.
#     Per-account check counts failures by email_hash regardless of source IP.
#   • Credential stuffing: attacker tries many email/password pairs from one
#     IP. Per-IP check counts failures by IP regardless of email target.
# In-memory _login_attempts in retail_portal.py stays as a coarse first gate
# (cheap, no DB hit on the per-IP burst case). DB layer is checked AFTER and
# adds the persistent + per-account dimensions.
#
# Tunables (env-overridable):
#   LOGIN_ACCOUNT_LOCKOUT_MAX     — default 10
#   LOGIN_ACCOUNT_LOCKOUT_WINDOW  — seconds, default 900 (15 min)
#   LOGIN_IP_LOCKOUT_MAX          — default 20
#   LOGIN_IP_LOCKOUT_WINDOW       — seconds, default 300 (5 min)
#   LOGIN_ATTEMPT_RETENTION_DAYS  — default 7

LOGIN_ACCOUNT_LOCKOUT_MAX    = int(os.environ.get('LOGIN_ACCOUNT_LOCKOUT_MAX',    '10'))
LOGIN_ACCOUNT_LOCKOUT_WINDOW = int(os.environ.get('LOGIN_ACCOUNT_LOCKOUT_WINDOW', '900'))
LOGIN_IP_LOCKOUT_MAX         = int(os.environ.get('LOGIN_IP_LOCKOUT_MAX',         '20'))
LOGIN_IP_LOCKOUT_WINDOW      = int(os.environ.get('LOGIN_IP_LOCKOUT_WINDOW',      '300'))
LOGIN_ATTEMPT_RETENTION_DAYS = int(os.environ.get('LOGIN_ATTEMPT_RETENTION_DAYS', '7'))


def record_login_attempt(email: str, ip: str, user_agent: str, success: bool) -> None:
    """Persist a single login attempt. Auto-prunes attempts older than
    LOGIN_ATTEMPT_RETENTION_DAYS at the same write to keep table bounded.

    `email` may be empty / None (blank-email POSTs are still recorded
    with NULL email_hash so per-IP lockout still tracks them).
    """
    email_hash = _email_lookup_hash(email) if email else None
    now_iso    = datetime.now(timezone.utc).isoformat()
    cutoff     = (datetime.now(timezone.utc) - timedelta(days=LOGIN_ATTEMPT_RETENTION_DAYS)).isoformat()
    try:
        with _auth_conn() as c:
            c.execute(
                "INSERT INTO login_attempts (email_hash, ip, user_agent, success, attempted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email_hash, ip[:64], (user_agent or '')[:255], 1 if success else 0, now_iso)
            )
            c.execute(
                "DELETE FROM login_attempts WHERE attempted_at < ?",
                (cutoff,)
            )
    except Exception as e:
        log.warning(f"record_login_attempt failed: {e}")


def is_account_locked(email: str) -> tuple:
    """Return (locked: bool, retry_after_seconds: int|None) based on the
    per-account failure count. Counts FAILED attempts only (success=0)
    in the LOGIN_ACCOUNT_LOCKOUT_WINDOW for this email_hash.

    Caller-friendly: if locked, the second return value is the number of
    seconds until the OLDEST counted failure ages out — i.e., when the
    customer can try again.
    """
    if not email:
        return (False, None)
    email_hash = _email_lookup_hash(email)
    cutoff_dt  = datetime.now(timezone.utc) - timedelta(seconds=LOGIN_ACCOUNT_LOCKOUT_WINDOW)
    cutoff_iso = cutoff_dt.isoformat()
    try:
        with _auth_conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n, MIN(attempted_at) AS oldest FROM login_attempts "
                "WHERE email_hash = ? AND success = 0 AND attempted_at >= ?",
                (email_hash, cutoff_iso)
            ).fetchone()
    except Exception as e:
        log.warning(f"is_account_locked failed: {e}")
        return (False, None)
    n = row['n'] or 0
    if n < LOGIN_ACCOUNT_LOCKOUT_MAX:
        return (False, None)
    try:
        oldest = datetime.fromisoformat(row['oldest'])
        retry_at = oldest + timedelta(seconds=LOGIN_ACCOUNT_LOCKOUT_WINDOW)
        retry_after = int((retry_at - datetime.now(timezone.utc)).total_seconds())
        retry_after = max(0, retry_after)
    except Exception:
        retry_after = LOGIN_ACCOUNT_LOCKOUT_WINDOW
    return (True, retry_after)


def is_ip_locked(ip: str) -> tuple:
    """Return (locked: bool, retry_after_seconds: int|None) based on the
    per-IP failure count. Counts FAILED attempts only in the
    LOGIN_IP_LOCKOUT_WINDOW for this IP."""
    if not ip:
        return (False, None)
    cutoff_dt  = datetime.now(timezone.utc) - timedelta(seconds=LOGIN_IP_LOCKOUT_WINDOW)
    cutoff_iso = cutoff_dt.isoformat()
    try:
        with _auth_conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n, MIN(attempted_at) AS oldest FROM login_attempts "
                "WHERE ip = ? AND success = 0 AND attempted_at >= ?",
                (ip, cutoff_iso)
            ).fetchone()
    except Exception as e:
        log.warning(f"is_ip_locked failed: {e}")
        return (False, None)
    n = row['n'] or 0
    if n < LOGIN_IP_LOCKOUT_MAX:
        return (False, None)
    try:
        oldest = datetime.fromisoformat(row['oldest'])
        retry_at = oldest + timedelta(seconds=LOGIN_IP_LOCKOUT_WINDOW)
        retry_after = int((retry_at - datetime.now(timezone.utc)).total_seconds())
        retry_after = max(0, retry_after)
    except Exception:
        retry_after = LOGIN_IP_LOCKOUT_WINDOW
    return (True, retry_after)


def mark_tos_accepted(customer_id: str, version: str) -> None:
    """
    Record ToS acceptance in auth.db.
    Sets tos_accepted_at (UTC ISO timestamp) and tos_version for the customer.
    Idempotent — safe to call again if version changes in the future.
    """
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET tos_accepted_at = ?, tos_version = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), version, customer_id)
        )


def get_display_name_by_id(customer_id: str) -> str:
    """Look up and decrypt display name for a customer ID."""
    try:
        with _auth_conn() as c:
            row = c.execute("SELECT display_name_enc FROM customers WHERE id=?", (customer_id,)).fetchone()
            if row and row['display_name_enc']:
                return decrypt_field(row['display_name_enc']) or customer_id[:8]
    except Exception:
        pass
    return customer_id[:8]


def get_display_name(customer) -> str:
    """Decrypt and return display name from a customer Row. Falls back to 'Customer'."""
    try:
        return decrypt_field(customer['display_name_enc']) or 'Customer'
    except Exception:
        return 'Customer'


def get_email(customer) -> str:
    """Decrypt and return email from a customer Row."""
    try:
        return decrypt_field(customer['email_enc'])
    except Exception:
        return ''


# ── ALPACA CREDENTIALS ──────────────────────────────────────────────────────

def get_alpaca_credentials(customer_id: str) -> tuple:
    """
    Returns (alpaca_key, alpaca_secret) decrypted for a customer.
    Returns ('', '') if not set.
    """
    with _auth_conn() as c:
        row = c.execute(
            "SELECT alpaca_key_enc, alpaca_secret_enc FROM customers WHERE id = ?",
            (customer_id,)
        ).fetchone()
    if not row:
        return ('', '')
    key    = decrypt_field(row['alpaca_key_enc'])    if row['alpaca_key_enc']    else ''
    secret = decrypt_field(row['alpaca_secret_enc']) if row['alpaca_secret_enc'] else ''
    return (key, secret)


def set_alpaca_credentials(customer_id: str, alpaca_key: str, alpaca_secret: str):
    """Store encrypted Alpaca credentials for a customer."""
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET alpaca_key_enc = ?, alpaca_secret_enc = ? WHERE id = ?",
            (encrypt_field(alpaca_key), encrypt_field(alpaca_secret), customer_id)
        )
    log.info(f"Alpaca credentials updated for customer {customer_id}")


# ── ADMIN BOOTSTRAP ─────────────────────────────────────────────────────────

def get_operating_mode(customer_id: str) -> str:
    """Return the operating mode for a customer: 'MANAGED' or 'AUTOMATIC'."""
    with _auth_conn() as c:
        row = c.execute(
            "SELECT operating_mode FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
    return row['operating_mode'] if row else 'MANAGED'


def set_operating_mode(customer_id: str, mode: str):
    """Set operating mode for a customer. Mode must be 'MANAGED' or 'AUTOMATIC'."""
    if mode not in ('MANAGED', 'AUTOMATIC'):
        raise ValueError(f"Invalid mode: {mode}")
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET operating_mode = ? WHERE id = ?",
            (mode, customer_id)
        )


def _migrate_trading_mode():
    """Add trading_mode column to customers if missing."""
    with _auth_conn() as c:
        cols = {r[1] for r in c.execute('PRAGMA table_info(customers)').fetchall()}
        if 'trading_mode' not in cols:
            c.execute("ALTER TABLE customers ADD COLUMN trading_mode TEXT NOT NULL DEFAULT 'PAPER'")
            log.info('Migration: added trading_mode column to customers')

try:
    _migrate_trading_mode()
except Exception as _e:
    log.debug(f'trading_mode migration: {_e}')


def get_trading_mode(customer_id: str) -> str:
    """Return the trading mode for a customer: 'PAPER' or 'LIVE'."""
    with _auth_conn() as c:
        row = c.execute(
            "SELECT trading_mode FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
    return row['trading_mode'] if row and row['trading_mode'] else 'PAPER'


def set_trading_mode(customer_id: str, mode: str):
    """Set trading mode for a customer. Mode must be 'PAPER' or 'LIVE'."""
    if mode not in ('PAPER', 'LIVE'):
        raise ValueError(f"Invalid trading mode: {mode}")
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET trading_mode = ? WHERE id = ?",
            (mode, customer_id)
        )
    log.info(f'Trading mode set: {customer_id[:8]} -> {mode}')


def customer_count() -> int:
    """Return count of active customer accounts."""
    with _auth_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM customers WHERE is_active = 1"
        ).fetchone()[0]


def list_customers() -> list:
    """
    Return all customers with decrypted display info for admin listing.
    Never returns password hashes or raw encrypted blobs.
    """
    with _auth_conn() as c:
        rows = c.execute(
            """SELECT id, email_enc, display_name_enc, role, is_active,
                      operating_mode, trading_mode, created_at, last_login,
                      CASE WHEN alpaca_key_enc IS NOT NULL AND length(alpaca_key_enc) > 0
                           THEN 1 ELSE 0 END AS has_alpaca,
                      email_verified, subscription_status, pricing_tier,
                      subscription_ends_at, stripe_customer_id
               FROM customers
               ORDER BY created_at DESC"""
        ).fetchall()

    result = []
    for row in rows:
        result.append({
            'id':                  row['id'],
            'email':               decrypt_field(row['email_enc'])        if row['email_enc']        else '',
            'display_name':        decrypt_field(row['display_name_enc']) if row['display_name_enc'] else '',
            'role':                row['role'],
            'is_active':           bool(row['is_active']),
            'operating_mode':      row['operating_mode'],
            'trading_mode':        row['trading_mode'] if 'trading_mode' in row.keys() else 'PAPER',
            'created_at':          row['created_at'],
            'last_login':          row['last_login'],
            'has_alpaca':          bool(row['has_alpaca']),
            # v3.0 subscription + verification fields
            'email_verified':      bool(row['email_verified'])      if 'email_verified'      in row.keys() else True,
            'subscription_status': row['subscription_status']       if 'subscription_status' in row.keys() else 'active',
            'pricing_tier':        row['pricing_tier']              if 'pricing_tier'        in row.keys() else 'standard',
            'subscription_ends_at':row['subscription_ends_at']      if 'subscription_ends_at'in row.keys() else None,
            'stripe_customer_id':  row['stripe_customer_id']        if 'stripe_customer_id'  in row.keys() else None,
        })
    return result


# ── SIGNUP MANAGEMENT ─────────────────────────────────────────────────────

def create_pending_signup(name: str, email: str, phone: str, password: str,
                          state: str = '', zip_code: str = '',
                          tos_accepted: bool = False, tos_version: str = '',
                          tos_ip: str = '', tos_user_agent: str = '') -> int:
    """
    Create a pending signup. Stores password hash (not plaintext).
    Returns the signup row ID. Raises ValueError if email already registered, pending,
    or if ToS was not accepted.

    ToS acceptance (tos_accepted=True) is required — user must check the checkbox on
    the signup form. tos_version/tos_ip/tos_user_agent are recorded for audit trail.
    On approve_signup(), these values are copied to the customer row (so the "I agreed"
    timestamp reflects when the user checked the box, not when admin clicked approve).
    """
    if not tos_accepted:
        raise ValueError("You must accept the Terms of Service to sign up.")

    email = email.lower().strip()
    now   = datetime.now(timezone.utc).isoformat()

    # Check if email already exists as a customer
    email_hash = _email_lookup_hash(email)
    with _auth_conn() as c:
        existing = c.execute(
            "SELECT id FROM customers WHERE email_hash = ?", (email_hash,)
        ).fetchone()
        if existing:
            raise ValueError("An account already exists for this email address")

        # Check if already pending
        existing_signup = c.execute(
            "SELECT id, status FROM pending_signups WHERE email = ?", (email,)
        ).fetchone()
        if existing_signup:
            if existing_signup['status'] == 'PENDING':
                raise ValueError("A signup request for this email is already pending")
            elif existing_signup['status'] == 'APPROVED':
                raise ValueError("This email has already been approved")
            # If REJECTED, allow re-signup by updating the row
            c.execute(
                "UPDATE pending_signups SET name=?, phone=?, password_hash=?, state=?, zip_code=?, "
                "status='PENDING', created_at=?, reviewed_at=NULL, reviewed_by=NULL, "
                "tos_accepted_at=?, tos_version=?, tos_ip=?, tos_user_agent=? "
                "WHERE id=?",
                (name, phone, hash_password(password), state, zip_code, now,
                 now, tos_version, tos_ip, tos_user_agent, existing_signup['id'])
            )
            log.info(f"Re-submitted rejected signup: {email} (ToS v{tos_version})")
            return existing_signup['id']

        c.execute(
            "INSERT INTO pending_signups "
            "(name, email, phone, password_hash, state, zip_code, created_at, "
            " tos_accepted_at, tos_version, tos_ip, tos_user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, email, phone, hash_password(password), state, zip_code, now,
             now, tos_version, tos_ip, tos_user_agent)
        )
        signup_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    log.info(f"New pending signup #{signup_id}: {email} (ToS v{tos_version} accepted)")
    return signup_id


def list_pending_signups(status_filter: str = None) -> list:
    """
    List pending signups. If status_filter is provided, only return that status.
    """
    with _auth_conn() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM pending_signups WHERE status = ? ORDER BY created_at DESC",
                (status_filter.upper(),)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM pending_signups ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def approve_signup(signup_id: int, reviewed_by: str = 'admin') -> dict:
    """
    Approve a pending signup: creates the customer account with auto_activate=True,
    provisions the customer directory & signals.db. Returns customer info dict.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _auth_conn() as c:
        row = c.execute(
            "SELECT * FROM pending_signups WHERE id = ? AND status = 'PENDING'",
            (signup_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Signup #{signup_id} not found or not pending")
        row = dict(row)

    # Create the customer account using existing create_customer,
    # but we need to pass the already-hashed password, so we do it manually.
    customer_id = str(uuid.uuid4())
    email       = row['email'].lower().strip()
    email_hash  = _email_lookup_hash(email)

    with _auth_conn() as c:
        # Check for existing customer with this email (race condition guard)
        existing = c.execute(
            "SELECT id FROM customers WHERE email_hash = ?", (email_hash,)
        ).fetchone()
        if existing:
            raise ValueError("An account already exists for this email address")

        c.execute(
            """INSERT INTO customers
               (id, email_hash, email_enc, display_name_enc, phone_enc,
                password_hash, role, email_verified, subscription_status,
                pricing_tier, pricing_locked_at, created_at,
                state, zip_code, tos_accepted_at, tos_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                customer_id,
                email_hash,
                encrypt_field(email),
                encrypt_field(row['name']) if row['name'] else b'',
                encrypt_field(row['phone']) if row['phone'] else b'',
                row['password_hash'],   # Already hashed during signup
                'customer',
                1,                      # email_verified = true (admin-approved)
                'active',               # active subscription (trial)
                'early_adopter',        # trial users get early_adopter pricing
                now,                    # pricing_locked_at
                now,                    # created_at
                row.get('state', ''),   # from signup form
                row.get('zip_code', ''),# from signup form
                # ToS values carried over from pending_signups row (captured when
                # the user checked the box on the signup form). Falls back to the
                # approval timestamp only for legacy rows created before ToS was
                # required at signup — those should not exist post-2026-04-23.
                row.get('tos_accepted_at') or now,
                row.get('tos_version')     or '1.0',
            )
        )

        c.execute(
            "UPDATE pending_signups SET status='APPROVED', customer_id=?, "
            "reviewed_at=?, reviewed_by=? WHERE id=?",
            (customer_id, now, reviewed_by, signup_id)
        )

    # Create per-customer data directory and initialize DB from default template
    customer_dir = os.path.join(CUSTOMERS_DIR, customer_id)
    os.makedirs(customer_dir, exist_ok=True)

    # Copy default template DB to give new customer a clean schema + default settings
    default_db = os.path.join(CUSTOMERS_DIR, 'default', 'signals.db')
    customer_db = os.path.join(customer_dir, 'signals.db')
    if os.path.exists(default_db) and not os.path.exists(customer_db):
        import shutil
        shutil.copy2(default_db, customer_db)
        log.info(f"Customer DB initialized from default template: {customer_db}")
    elif not os.path.exists(customer_db):
        # Fallback: create DB via retail_database module
        try:
            from retail_database import get_customer_db
            get_customer_db(customer_id)  # triggers schema creation
            log.info(f"Customer DB created via schema bootstrap: {customer_db}")
        except Exception as e:
            log.warning(f"Could not initialize customer DB: {e}")

    log.info(f"Approved signup #{signup_id}: {email} -> customer {customer_id}")
    return {
        'customer_id': customer_id,
        'email':       email,
        'name':        row['name'],
        'phone':       row['phone'],
    }


def reject_signup(signup_id: int, reviewed_by: str = 'admin'):
    """Reject a pending signup."""
    now = datetime.now(timezone.utc).isoformat()
    with _auth_conn() as c:
        row = c.execute(
            "SELECT * FROM pending_signups WHERE id = ? AND status = 'PENDING'",
            (signup_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Signup #{signup_id} not found or not pending")
        c.execute(
            "UPDATE pending_signups SET status='REJECTED', reviewed_at=?, reviewed_by=? WHERE id=?",
            (now, reviewed_by, signup_id)
        )
    log.info(f"Rejected signup #{signup_id}")




def create_password_reset_token(email: str) -> str:
    """Generate a password reset token for the given email. Returns token or None if email not found."""
    email_hash = _email_lookup_hash(email)
    with _auth_conn() as c:
        row = c.execute("SELECT id FROM customers WHERE email_hash = ?", (email_hash,)).fetchone()
        if not row:
            return None  # Don't reveal whether email exists
        customer_id = row['id']
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        c.execute(
            "UPDATE customers SET password_reset_token=?, password_reset_expires=? WHERE id=?",
            (token, expires, customer_id))
        return token


def verify_reset_token(token: str):
    """Verify a reset token. Returns customer row if valid, None if expired/invalid."""
    with _auth_conn() as c:
        row = c.execute(
            "SELECT id, password_reset_expires FROM customers WHERE password_reset_token=?",
            (token,)).fetchone()
        if not row:
            return None
        expires = row['password_reset_expires']
        if not expires or datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            # Expired — clear it
            c.execute("UPDATE customers SET password_reset_token=NULL, password_reset_expires=NULL WHERE id=?",
                      (row['id'],))
            return None
        return row


def reset_password(token: str, new_password: str) -> bool:
    """Reset password using a valid token. Returns True if successful."""
    with _auth_conn() as c:
        row = c.execute(
            "SELECT id, password_reset_expires FROM customers WHERE password_reset_token=?",
            (token,)).fetchone()
        if not row:
            return False
        expires = row['password_reset_expires']
        if not expires or datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            c.execute("UPDATE customers SET password_reset_token=NULL, password_reset_expires=NULL WHERE id=?",
                      (row['id'],))
            return False
        new_hash = hash_password(new_password)
        c.execute(
            "UPDATE customers SET password_hash=?, password_reset_token=NULL, password_reset_expires=NULL WHERE id=?",
            (new_hash, row['id']))
        log.info(f"Password reset completed for customer {row['id']}")
        return True


def generate_signup_verify_token(signup_id: int) -> str:
    """Generate a one-time email verification token for a pending signup."""
    import secrets
    token = secrets.token_urlsafe(32)
    with _auth_conn() as c:
        c.execute(
            "UPDATE pending_signups SET email_verify_token=? WHERE id=?",
            (token, signup_id)
        )
    return token


def verify_signup_email(token: str) -> dict:
    """
    Verify a signup email using the token from the verification link.
    Returns {signup_id, name, email} on success.
    Raises ValueError on invalid/expired/already-used token.
    """
    from datetime import datetime, timedelta, timezone
    with _auth_conn() as c:
        row = c.execute(
            "SELECT id, name, email, status, email_verified, created_at "
            "FROM pending_signups WHERE email_verify_token=?",
            (token,)
        ).fetchone()

        if not row:
            raise ValueError("Invalid or expired verification link")

        signup_id, name, email, status, already_verified, created_at = row

        if already_verified:
            return {"signup_id": signup_id, "name": name, "email": email, "already": True}

        if status not in ("PENDING",):
            raise ValueError("This signup has already been processed")

        # Check 48-hour expiry
        try:
            created = datetime.fromisoformat(created_at)
            if (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds() > 48 * 3600:
                raise ValueError("Verification link has expired (48 hour limit)")
        except (ValueError, TypeError) as e:
            if "expired" in str(e):
                raise
            pass  # If date parsing fails, allow verification

        now_iso = datetime.now(timezone.utc).isoformat()
        c.execute(
            "UPDATE pending_signups SET email_verified=1, email_verified_at=?, "
            "email_verify_token=NULL WHERE id=?",
            (now_iso, signup_id)
        )
    # ── End of write transaction — the `with` block exited and committed above. ──
    # Auto-approve MUST live outside the `with` block: approve_signup() opens its
    # own _auth_conn() to write the new customer row, and SQLite only allows one
    # writer at a time. Calling it while the outer context still held the write
    # lock raised "database is locked" → the pending_signup stayed PENDING with no
    # customer row, even though email verification had already succeeded on disk.
    # Invite code was pre-validated at signup; email verify is the final gate.
    try:
        approval = approve_signup(signup_id, reviewed_by="invite_auto")
        return {"signup_id": signup_id, "name": name, "email": email, **approval}
    except Exception as _ae:
        log.warning(f"Auto-approve after email verify failed: {_ae}")
        return {"signup_id": signup_id, "name": name, "email": email}



def verify_signup_access_code(code: str) -> bool:
    """
    Check if the provided code is valid.
    Accepts the static SIGNUP_ACCESS_CODE OR a valid one-time invite code.
    One-time codes are consumed (marked used) on successful verification.
    """
    code = code.strip()
    if not code:
        return False
    # Check static code first
    if SIGNUP_ACCESS_CODE and _hmac.compare_digest(code, SIGNUP_ACCESS_CODE.strip()):
        return True
    # Check one-time invite codes
    with _auth_conn() as c:
        row = c.execute(
            "SELECT code FROM invite_codes WHERE code = ? AND is_used = 0", (code,)
        ).fetchone()
        if row:
            c.execute(
                "UPDATE invite_codes SET is_used = 1, used_at = ? WHERE code = ?",
                (datetime.now(timezone.utc).isoformat(), code)
            )
            log.info(f"One-time invite code consumed: {code[:8]}...")
            return True
    return False


def generate_invite_code(created_by: str = "admin") -> str:
    """Generate a one-time invite code. Returns the code string."""
    code = "SYN-" + secrets.token_hex(4).upper()
    now = datetime.now(timezone.utc).isoformat()
    with _auth_conn() as c:
        c.execute(
            "INSERT INTO invite_codes (code, created_at, created_by) VALUES (?, ?, ?)",
            (code, now, created_by)
        )
    log.info(f"Generated invite code: {code} by {created_by}")
    return code


def list_invite_codes() -> list:
    """List all invite codes with usage status."""
    with _auth_conn() as c:
        rows = c.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_customer(customer_id: str):
    """Soft-delete a customer account (sets is_active=0). Does not delete data."""
    with _auth_conn() as c:
        c.execute("UPDATE customers SET is_active = 0 WHERE id = ?", (customer_id,))
    log.info(f"Customer deactivated: {customer_id}")


def update_customer_name(customer_id: str, display_name: str):
    """Update encrypted display name for a customer."""
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET display_name_enc = ? WHERE id = ?",
            (encrypt_field(display_name), customer_id)
        )


def update_password(customer_id: str, current_password: str, new_password: str) -> None:
    """Change a customer's password after verifying the current one."""
    with _auth_conn() as c:
        row = c.execute(
            "SELECT password_hash FROM customers WHERE id = ? AND is_active = 1",
            (customer_id,)
        ).fetchone()
        if not row:
            raise ValueError("Account not found")
        if not verify_password(current_password, row['password_hash']):
            raise ValueError("Current password is incorrect")
        c.execute(
            "UPDATE customers SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), customer_id)
        )
    log.info(f"Password updated for customer {customer_id}")


def update_email(customer_id: str, current_password: str, new_email: str) -> None:
    """Direct email change. PRESERVED for backward compatibility but the
    portal route /api/account/change-email now uses initiate_email_change
    (Phase 2.5 MED-2/MED-3) which sends a verification link to the new
    address and an alert to the old address. Direct callers (admin
    tooling, migrations) can still use this."""
    new_email  = new_email.lower().strip()
    new_hash   = _email_lookup_hash(new_email)
    new_enc    = encrypt_field(new_email)
    with _auth_conn() as c:
        row = c.execute(
            "SELECT password_hash FROM customers WHERE id = ? AND is_active = 1",
            (customer_id,)
        ).fetchone()
        if not row:
            raise ValueError("Account not found")
        if not verify_password(current_password, row['password_hash']):
            raise ValueError("Current password is incorrect")
        conflict = c.execute(
            "SELECT id FROM customers WHERE email_hash = ? AND id != ?",
            (new_hash, customer_id)
        ).fetchone()
        if conflict:
            raise ValueError("Email address is already in use")
        c.execute(
            "UPDATE customers SET email_hash = ?, email_enc = ? WHERE id = ?",
            (new_hash, new_enc, customer_id)
        )
    log.info(f"Email updated for customer {customer_id}")


# ── EMAIL CHANGE — TWO-STEP VERIFY FLOW (Phase 2.5 MED-2 + MED-3) ────────────
# 1. initiate_email_change() — verifies password, validates new email is
#    not already taken, creates pending_email_changes row with 1h TTL,
#    returns (token, old_email, new_email) so the portal can send the
#    verification email to the new address and the alert email to the old.
# 2. confirm_email_change(token) — validates token+expiry+single-use,
#    re-checks the new email isn't already taken (race protection between
#    initiate and confirm), commits the change to customers.

EMAIL_CHANGE_TTL_MINUTES = 60   # 1h verification window

def initiate_email_change(customer_id: str, current_password: str,
                          new_email: str) -> dict:
    """Step 1 of secure email-change flow. Verifies password, checks new
    email is not taken, creates a pending_email_changes row with a
    1h-TTL verification token. Does NOT change customers.email_* yet —
    that happens in confirm_email_change after the verification link
    is clicked. Pre-existing pending changes for this customer are
    superseded (only the most recent token is valid).

    Returns: {'token': str, 'old_email': str, 'new_email': str}
    Raises:  ValueError on bad password, taken email, or same-email no-op.
    """
    new_email = new_email.lower().strip()
    new_hash  = _email_lookup_hash(new_email)
    new_enc   = encrypt_field(new_email)

    with _auth_conn() as c:
        row = c.execute(
            "SELECT password_hash, email_hash, email_enc FROM customers "
            "WHERE id = ? AND is_active = 1",
            (customer_id,)
        ).fetchone()
        if not row:
            raise ValueError("Account not found")
        if not verify_password(current_password, row['password_hash']):
            raise ValueError("Current password is incorrect")
        if row['email_hash'] == new_hash:
            raise ValueError("New email is the same as the current email")
        # Collision check against existing customers AND any other
        # in-flight pending changes (different customer pre-claiming
        # the same email).
        conflict = c.execute(
            "SELECT id FROM customers WHERE email_hash = ? AND id != ?",
            (new_hash, customer_id)
        ).fetchone()
        if conflict:
            raise ValueError("Email address is already in use")
        pending_conflict = c.execute(
            "SELECT id FROM pending_email_changes "
            "WHERE new_email_hash = ? AND customer_id != ? "
            "AND consumed_at IS NULL "
            "AND expires_at > ?",
            (new_hash, customer_id,
             datetime.now(timezone.utc).isoformat())
        ).fetchone()
        if pending_conflict:
            raise ValueError("Email address is already in use")
        # Supersede prior pending changes for this customer
        c.execute(
            "DELETE FROM pending_email_changes "
            "WHERE customer_id = ? AND consumed_at IS NULL",
            (customer_id,)
        )
        token = secrets.token_urlsafe(32)
        now   = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=EMAIL_CHANGE_TTL_MINUTES)).isoformat()
        c.execute(
            """INSERT INTO pending_email_changes
                 (customer_id, old_email_hash, new_email_hash, new_email_enc,
                  token, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, row['email_hash'], new_hash, new_enc,
             token, expires, now.isoformat())
        )
    old_email = decrypt_field(row['email_enc']) if row['email_enc'] else ''
    log.info(f"Email-change initiated for customer {customer_id} "
             f"(old→{new_email[:3]}***)")
    return {'token': token, 'old_email': old_email, 'new_email': new_email}


def confirm_email_change(token: str) -> str:
    """Step 2 of secure email-change flow. Validates token+expiry,
    commits the change to customers.email_hash/email_enc, marks the
    pending row as consumed.

    Returns: customer_id on success
    Raises:  ValueError on invalid/expired/already-consumed token, or
             if the email got taken between initiate and confirm.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with _auth_conn() as c:
        row = c.execute(
            "SELECT id, customer_id, new_email_hash, new_email_enc, expires_at, consumed_at "
            "FROM pending_email_changes WHERE token = ?",
            (token,)
        ).fetchone()
        if not row:
            raise ValueError("This verification link is invalid.")
        if row['consumed_at']:
            raise ValueError("This verification link has already been used.")
        if row['expires_at'] < now_iso:
            raise ValueError("This verification link has expired. "
                             "Request a new email change from your account settings.")
        # Race protection: re-check that the new email isn't already
        # claimed by someone else (initiate→confirm window can be ≤1h).
        conflict = c.execute(
            "SELECT id FROM customers WHERE email_hash = ? AND id != ?",
            (row['new_email_hash'], row['customer_id'])
        ).fetchone()
        if conflict:
            raise ValueError("Email address was claimed by another account "
                             "during the verification window.")
        c.execute(
            "UPDATE customers SET email_hash = ?, email_enc = ? WHERE id = ?",
            (row['new_email_hash'], row['new_email_enc'], row['customer_id'])
        )
        c.execute(
            "UPDATE pending_email_changes SET consumed_at = ? WHERE id = ?",
            (now_iso, row['id'])
        )
    log.info(f"Email-change confirmed for customer {row['customer_id']}")
    return row['customer_id']


def ensure_admin_account():
    """
    Create the default admin account from .env if no accounts exist yet.
    Reads ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_NAME from environment.
    Safe to call on every startup — no-op if accounts already exist.
    """
    if customer_count() > 0:
        return

    admin_email    = os.environ.get('ADMIN_EMAIL', '')
    admin_password = os.environ.get('ADMIN_PASSWORD', '')
    admin_name     = os.environ.get('ADMIN_NAME', 'Admin')

    if not admin_email or not admin_password:
        log.warning(
            "No customer accounts exist and ADMIN_EMAIL/ADMIN_PASSWORD are not set. "
            "The portal will fall back to PORTAL_PASSWORD if set, otherwise access is blocked. "
            "Add ADMIN_EMAIL and ADMIN_PASSWORD to .env to create the admin account."
        )
        return

    try:
        create_customer(admin_email, admin_password, display_name=admin_name,
                        role='admin', auto_activate=True)
        log.info(f"Admin account created: {admin_email}")
    except ValueError:
        pass  # Already exists


# ── SUBSCRIPTION + VERIFICATION PIPELINE ────────────────────────────────────
# Functions for the customer acquisition flow:
#   Stripe payment → create_unverified_customer() → generate_verify_token()
#   → email setup link → /setup-account/<token> → activate_account()
#   → customer can log in → is_access_allowed() gates every login
#
# Admin-created accounts (create_customer with auto_activate=True) bypass this
# entire flow — email_verified=1 and subscription_status='active' from creation.


def generate_verify_token(customer_id: str) -> str:
    """
    Generate and store a single-use email verification / password-setup token.
    Token is URL-safe, 32 bytes (43 chars base64). Expires after 48 hours.
    Used in the link sent to /setup-account/<token>.
    """
    token = secrets.token_urlsafe(32)
    with _auth_conn() as c:
        c.execute(
            "UPDATE customers SET email_verify_token = ?, email_verify_sent_at = ? WHERE id = ?",
            (token, datetime.now(timezone.utc).isoformat(), customer_id)
        )
    log.info(f"Verification token generated for customer {customer_id}")
    return token


def consume_verify_token(token: str):
    """
    Look up and validate a setup/verification token.
    Returns the customer sqlite3.Row if valid, None if not found or expired.
    Does NOT activate the account — caller calls activate_account() after password is set.
    Token is consumed (cleared) only by activate_account().
    Tokens expire after 48 hours.
    """
    with _auth_conn() as c:
        row = c.execute(
            "SELECT * FROM customers WHERE email_verify_token = ? AND is_active = 1",
            (token,)
        ).fetchone()
    if not row:
        return None
    sent_at = row['email_verify_sent_at'] if 'email_verify_sent_at' in row.keys() else None
    if sent_at:
        try:
            sent = datetime.fromisoformat(sent_at)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - sent).total_seconds() > 48 * 3600:
                log.warning(f"Expired verify token used for customer {row['id']}")
                return None
        except Exception:
            pass
    return row


def activate_account(customer_id: str, password: str):
    """
    Set password, mark email verified, activate subscription, lock in pricing tier.
    Called when customer submits the password-setup form at /setup-account/<token>.
    Sets subscription_status='active' — Stripe webhook will update this to the
    correct Stripe-driven status once the payment flow is wired.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _auth_conn() as c:
        c.execute(
            """UPDATE customers SET
               password_hash       = ?,
               email_verified      = 1,
               email_verify_token  = NULL,
               subscription_status = 'active',
               pricing_locked_at   = COALESCE(pricing_locked_at, ?)
               WHERE id = ?""",
            (hash_password(password), now, customer_id)
        )
    log.info(f"Account activated: {customer_id}")


def is_access_allowed(customer_id: str, role: str) -> tuple:
    """
    Determine if a customer is allowed to access the portal.
    Returns (allowed: bool, reason: str).

    Gate logic:
      - Admin role: always allowed
      - Customer: email_verified=1 AND subscription_status in (active, trialing)
      - past_due within grace period: allowed with warning banner
      - past_due past grace, inactive, cancelled: denied

    Reasons returned:
      'admin'        — admin role, always allowed
      'active'       — subscription active
      'trialing'     — in trial period
      'grace_period' — past_due but within 7-day grace window
      'unverified'   — email not yet verified (must complete /setup-account)
      'past_due'     — payment failed, grace period expired
      'inactive'     — never activated (pre-Stripe or manually deactivated)
      'cancelled'    — subscription cancelled
      'not_found'    — customer ID not in DB
    """
    if role == 'admin':
        return (True, 'admin')

    with _auth_conn() as c:
        row = c.execute(
            """SELECT email_verified, subscription_status, grace_period_ends_at
               FROM customers WHERE id = ? AND is_active = 1""",
            (customer_id,)
        ).fetchone()

    if not row:
        return (False, 'not_found')

    # Check email verification first
    email_verified = row['email_verified'] if 'email_verified' in row.keys() else 0
    if not email_verified:
        return (False, 'unverified')

    status = (row['subscription_status'] if 'subscription_status' in row.keys() else 'inactive') or 'inactive'

    if status == 'active':
        return (True, 'active')

    if status == 'trialing':
        return (True, 'trialing')

    if status == 'past_due':
        grace = row['grace_period_ends_at'] if 'grace_period_ends_at' in row.keys() else None
        if grace:
            try:
                grace_dt = datetime.fromisoformat(grace)
                if grace_dt.tzinfo is None:
                    grace_dt = grace_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < grace_dt:
                    return (True, 'grace_period')
            except Exception:
                pass
        return (False, 'past_due')

    return (False, status)  # inactive, cancelled, or unknown


def update_subscription(customer_id: str, stripe_customer_id: str,
                        subscription_id: str, status: str, ends_at: str = None):
    """
    Update subscription status from a Stripe webhook event.
    Called by the portal's /webhook/stripe handler (to be wired when Stripe is integrated).
    """
    with _auth_conn() as c:
        c.execute(
            """UPDATE customers SET
               stripe_customer_id  = ?,
               subscription_id     = ?,
               subscription_status = ?,
               subscription_ends_at = ?
               WHERE id = ?""",
            (stripe_customer_id, subscription_id, status, ends_at, customer_id)
        )
    log.info(f"Subscription updated: {customer_id} status={status}")


def mark_grace_period(customer_id: str, days: int = 7):
    """
    Set a grace period after payment failure.
    Called by portal /webhook/stripe on invoice.payment_failed.
    Customer retains portal access for `days` days with a warning banner,
    then is locked out until payment is resolved.
    """
    from datetime import timedelta
    ends_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with _auth_conn() as c:
        c.execute(
            """UPDATE customers SET
               subscription_status  = 'past_due',
               grace_period_ends_at = ?
               WHERE id = ?""",
            (ends_at, customer_id)
        )
    log.info(f"Grace period set for {customer_id}: expires {ends_at}")


def get_customer_by_stripe_id(stripe_customer_id: str):
    """Look up a customer by Stripe customer ID. Returns sqlite3.Row or None."""
    with _auth_conn() as c:
        return c.execute(
            "SELECT * FROM customers WHERE stripe_customer_id = ? AND is_active = 1",
            (stripe_customer_id,)
        ).fetchone()


def _write_owner_id_to_env(customer_id: str) -> None:
    """
    Write OWNER_CUSTOMER_ID=<uuid> to user/.env, adding or updating the line.
    Also updates the live process environment so callers don't need to reload.
    """
    env_path = os.path.join(_ROOT_DIR, 'user', '.env')
    if not os.path.exists(env_path):
        log.warning("user/.env not found — cannot persist OWNER_CUSTOMER_ID")
        return
    try:
        with open(env_path, 'r') as fh:
            lines = fh.readlines()

        new_line = f"OWNER_CUSTOMER_ID={customer_id}\n"
        found, updated = False, []
        for line in lines:
            if line.startswith('OWNER_CUSTOMER_ID='):
                updated.append(new_line)
                found = True
            else:
                updated.append(line)
        if not found:
            # Append after OWNER_EMAIL line if present, else at end
            inserted = False
            for i, line in enumerate(updated):
                if line.startswith('OWNER_EMAIL=') or line.startswith('OWNER_PASSWORD='):
                    updated.insert(i + 1, new_line)
                    inserted = True
                    break
            if not inserted:
                updated.append(new_line)

        with open(env_path, 'w') as fh:
            fh.writelines(updated)

        os.environ['OWNER_CUSTOMER_ID'] = customer_id
        log.info("OWNER_CUSTOMER_ID written to user/.env")
    except Exception as exc:
        log.warning(f"Could not write OWNER_CUSTOMER_ID to user/.env: {exc}")


def ensure_owner_customer() -> str | None:
    """
    Create or verify the system owner's customer account from .env.

    The owner is the human who purchased and runs this Synthos node.  They get
    a role='customer' account with auto_activate=True so they trade under their
    own account while the admin account is used for portal management.

    Reads from .env:
        OWNER_EMAIL        — owner's login email (required)
        OWNER_PASSWORD     — owner's portal password (required)
        OWNER_NAME         — display name shown in portal (optional)
        OWNER_CUSTOMER_ID  — already-created UUID (skip creation if set + valid)

    Writes back to .env:
        OWNER_CUSTOMER_ID  — set on first creation and on every recovery

    Returns the customer_id string, or None if OWNER_EMAIL/OWNER_PASSWORD are
    not configured in .env.

    Safe to call on every portal startup — idempotent.
    """
    owner_email    = os.environ.get('OWNER_EMAIL', '').strip()
    owner_password = os.environ.get('OWNER_PASSWORD', '').strip()
    owner_name     = os.environ.get('OWNER_NAME', 'Owner').strip()
    owner_tier     = os.environ.get('OWNER_PRICING_TIER', 'standard').strip()
    existing_id    = os.environ.get('OWNER_CUSTOMER_ID', '').strip()

    if owner_tier not in ('standard', 'early_adopter'):
        log.warning(f"OWNER_PRICING_TIER '{owner_tier}' invalid — defaulting to 'standard'")
        owner_tier = 'standard'

    if not owner_email or not owner_password:
        log.info(
            "OWNER_EMAIL or OWNER_PASSWORD not set — "
            "skipping owner customer account creation"
        )
        return None

    # If OWNER_CUSTOMER_ID is already set, verify the account still exists
    if existing_id:
        with _auth_conn() as c:
            row = c.execute(
                "SELECT id FROM customers WHERE id = ? AND is_active = 1",
                (existing_id,)
            ).fetchone()
        if row:
            log.info(f"Owner customer verified: {existing_id}")
            return existing_id
        else:
            log.warning(
                f"OWNER_CUSTOMER_ID {existing_id} not found in DB — "
                "recreating owner account"
            )

    # Create the owner customer account (or recover the existing one by email)
    try:
        customer_id = create_customer(
            owner_email, owner_password,
            display_name=owner_name,
            role='customer',
            auto_activate=True,
            pricing_tier=owner_tier,
        )
        log.info(f"Owner customer account created: {customer_id} ({owner_email})")
    except ValueError:
        # Account already exists — look it up by email
        existing = get_customer_by_email(owner_email)
        if existing:
            customer_id = existing['id']
            log.info(f"Owner customer already exists: {customer_id}")
        else:
            log.error("Owner customer creation failed and email lookup returned nothing")
            return None

    # Ensure the customer data directory exists
    cust_dir = os.path.join(CUSTOMERS_DIR, customer_id)
    os.makedirs(cust_dir, exist_ok=True)

    # Persist the ID back to .env so future restarts skip creation
    _write_owner_id_to_env(customer_id)
    return customer_id


def create_unverified_customer(email: str, stripe_customer_id: str,
                                pricing_tier: str = 'standard') -> str:
    """
    Create an account triggered by a Stripe webhook (checkout.session.completed).
    Account starts unverified with no usable password and subscription_status='inactive'.
    Caller must immediately call generate_verify_token() and email the /setup-account link.
    Stripe webhook will update subscription_status to 'active' on invoice.payment_succeeded.

    Returns the new customer ID (UUID).
    Raises ValueError if email already registered.
    """
    email        = email.lower().strip()
    customer_id  = str(uuid.uuid4())
    email_hash   = _email_lookup_hash(email)
    now          = datetime.now(timezone.utc).isoformat()

    with _auth_conn() as c:
        existing = c.execute(
            "SELECT id FROM customers WHERE email_hash = ?", (email_hash,)
        ).fetchone()
        if existing:
            raise ValueError("An account already exists for this email address")

        c.execute(
            """INSERT INTO customers
               (id, email_hash, email_enc, password_hash, role,
                stripe_customer_id, subscription_status, pricing_tier,
                email_verified, is_active, created_at)
               VALUES (?, ?, ?, ?, 'customer', ?, 'inactive', ?, 0, 1, ?)""",
            (
                customer_id,
                email_hash,
                encrypt_field(email),
                hash_password(secrets.token_hex(32)),  # unusable temp password
                stripe_customer_id,
                pricing_tier,
                now,
            )
        )

    os.makedirs(os.path.join(CUSTOMERS_DIR, customer_id), exist_ok=True)
    log.info(f"Unverified customer created via Stripe: {customer_id} tier={pricing_tier}")
    return customer_id
