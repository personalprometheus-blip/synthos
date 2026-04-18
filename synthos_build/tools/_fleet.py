"""Shared helper for the diagnostic tools in this folder.

Discovers the current fleet (customer_id → display_name + tier) at
runtime rather than hardcoding the list in every tool. Always reflects
what's actually on disk, so adding a new customer doesn't require
editing each script.

Import from a tool like:

    from _fleet import iter_customers
    for cid, name, tier in iter_customers():
        ...
"""
from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

# Resolve synthos_build/ regardless of cwd the tool was launched from.
_HERE         = Path(__file__).resolve().parent
_PROJECT_DIR  = _HERE.parent
_CUSTOMERS_DIR = _PROJECT_DIR / 'data' / 'customers'
_AUTH_DB_PATH  = _PROJECT_DIR / 'data' / 'auth.db'


def _customer_dirs():
    """All per-customer signals.db locations under data/customers/,
    excluding the 'default' template directory."""
    if not _CUSTOMERS_DIR.exists():
        return []
    out = []
    for entry in sorted(_CUSTOMERS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == 'default':
            continue
        if (entry / 'signals.db').exists():
            out.append(entry)
    return out


def _auth_names():
    """Best-effort name lookup via the auth module's decryption. If
    auth isn't importable (module missing encryption key, wrong cwd),
    return an empty dict — callers fall back to short-id labels."""
    out = {}
    sys.path.insert(0, str(_PROJECT_DIR / 'src'))
    try:
        import auth as _auth
        with _auth._auth_conn() as c:
            for row in c.execute(
                "SELECT id, display_name_enc FROM customers"
            ).fetchall():
                try:
                    out[row['id']] = (
                        _auth.decrypt_field(row['display_name_enc'])
                        or row['id'][:8]
                    )
                except Exception:
                    out[row['id']] = row['id'][:8]
    except Exception:
        pass
    return out


def _tier_for(customer_id: str) -> str:
    """Read TIER from the customer's customer_settings table. Returns
    '-' if no tier tag is set (pre-experiment customer)."""
    db_path = _CUSTOMERS_DIR / customer_id / 'signals.db'
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT value FROM customer_settings WHERE key='TIER'"
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else '-'
    except Exception:
        return '-'


def iter_customers():
    """Yield (customer_id, display_name, tier) for every on-disk
    customer — excluding 'default'. Sorted by tier then name so
    readouts are deterministic across runs.
    """
    names = _auth_names()
    rows = []
    for d in _customer_dirs():
        cid = d.name
        name = names.get(cid, cid[:8])
        rows.append((cid, name, _tier_for(cid)))
    rows.sort(key=lambda r: (r[2], r[1]))
    return rows


def project_root() -> Path:
    """synthos_build/ as a Path. Tools may import DB / agents relative
    to this without hardcoding paths."""
    return _PROJECT_DIR
