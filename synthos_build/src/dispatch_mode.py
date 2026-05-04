"""
dispatch_mode.py — Per-customer dispatch mode resolver.

Created 2026-05-04 as Tier 7 of the distributed-trader migration.

PROBLEM SOLVED:
The DISPATCH_MODE env var is process-wide — flipping it migrates ALL
customers at once. That's the wrong granularity for a careful rollout:
we want to put one customer on the distributed path, watch them for a
few sessions, then add another, etc.

This module gives every customer their own dispatch mode setting that
overrides the env var. Both market_daemon (the legacy fan-out) and
synthos_dispatcher (the new orchestrator) consult this resolver to
decide whether a given customer belongs to them on this cycle.

RESOLUTION ORDER (highest precedence first):
  1. Per-customer setting `_DISPATCH_MODE` in customer_settings table.
     Set explicitly via synthos_migration.py CLI. Values: 'daemon' or
     'distributed'. Anything else is treated as unset.
  2. Process env var DISPATCH_MODE. Defaults to 'daemon' for backward
     compatibility — the safe value that preserves today's behavior.

CONTRACT:
  Both daemon and dispatcher MUST honor the resolution. If both run
  the same customer on the same cycle, trades double-fire.

  The migration CLI is the only sanctioned path to flip the per-
  customer setting. Hand-editing customer_settings.db works but
  bypasses validation + audit logging.

USAGE:
  from dispatch_mode import resolve_customer_dispatch_mode

  # In market_daemon — filter to customers it owns:
  daemon_cids = [c for c in active_customers
                 if resolve_customer_dispatch_mode(c) == 'daemon']

  # In dispatcher — filter to customers it owns:
  distributed_cids = [c for c in active_customers
                      if resolve_customer_dispatch_mode(c) == 'distributed']

  # Combined check: every active customer goes through exactly one path.
"""

from __future__ import annotations
import logging
import os

log = logging.getLogger(__name__)

VALID_MODES = ('daemon', 'distributed')
DEFAULT_MODE = 'daemon'   # safe default — preserves pre-migration behavior
SETTING_KEY  = '_DISPATCH_MODE'


def env_default() -> str:
    """The DISPATCH_MODE env var, normalized + validated. Falls back to
    DEFAULT_MODE on any unrecognized value."""
    val = (os.environ.get('DISPATCH_MODE') or '').lower().strip()
    return val if val in VALID_MODES else DEFAULT_MODE


def resolve_customer_dispatch_mode(customer_id: str) -> str:
    """Return 'daemon' or 'distributed' for this customer.

    Order of precedence: per-customer setting overrides env var. Reads
    are best-effort — any DB failure falls back to the env default so
    we never block dispatch on a transient SQLite hiccup.
    """
    if not customer_id:
        return env_default()
    try:
        from retail_database import get_customer_db
        db = get_customer_db(customer_id)
        raw = db.get_setting(SETTING_KEY)
    except Exception as e:
        log.debug(f"[{customer_id[:8]}] dispatch-mode read failed ({e}) — using env default")
        return env_default()
    if raw is None:
        return env_default()
    val = str(raw).lower().strip()
    return val if val in VALID_MODES else env_default()


def set_customer_dispatch_mode(customer_id: str, mode: str) -> None:
    """Persist the per-customer setting. Validates mode against
    VALID_MODES. Raises ValueError on invalid input — the CLI catches
    it and surfaces a useful error to the operator."""
    if mode not in VALID_MODES:
        raise ValueError(
            f"invalid dispatch mode {mode!r} — must be one of {VALID_MODES}"
        )
    from retail_database import get_customer_db
    db = get_customer_db(customer_id)
    db.set_setting(SETTING_KEY, mode)


def clear_customer_dispatch_mode(customer_id: str) -> None:
    """Remove the per-customer setting so the env var default applies
    again. Useful for rolling back a customer who was migrated and is
    misbehaving — `clear` instantly returns them to whatever the env
    default is."""
    from retail_database import get_customer_db
    db = get_customer_db(customer_id)
    # set_setting(key, None) deletes in most SQLite-backed DBs but
    # behavior varies — write empty string and let the resolver treat
    # it as unset (it falls back since '' is not in VALID_MODES).
    db.set_setting(SETTING_KEY, '')


def filter_customers_by_mode(
    customer_ids: list[str], mode: str,
) -> list[str]:
    """Return the subset of customer_ids whose effective dispatch mode
    matches `mode`. Used by both market_daemon (mode='daemon') and
    synthos_dispatcher (mode='distributed') to filter their cycle's
    customer list."""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}")
    return [c for c in customer_ids if resolve_customer_dispatch_mode(c) == mode]
