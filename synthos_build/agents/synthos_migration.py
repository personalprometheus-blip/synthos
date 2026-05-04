#!/usr/bin/env python3
"""
synthos_migration.py — Per-customer dispatch-mode CLI.

Created 2026-05-04 as Tier 7 of the distributed-trader migration.

The sanctioned tool for moving customers between the daemon and
distributed trader paths. Reads / writes the _DISPATCH_MODE setting
in each customer's signals.db; both market_daemon and synthos_dispatcher
honor the resolved mode on every cycle (see src/dispatch_mode.py).

USAGE:

  # Show every active customer + their effective mode
  python3 synthos_migration.py status

  # Migrate one customer to the distributed dispatcher
  python3 synthos_migration.py enable <customer_id>

  # Roll a customer back to daemon mode (instant — next cycle picks it up)
  python3 synthos_migration.py disable <customer_id>

  # Bulk migrate every active customer to distributed (use with care)
  python3 synthos_migration.py enable-all --confirm

  # Bulk roll back all customers to daemon
  python3 synthos_migration.py disable-all --confirm

  # Reset a customer to env-default (clears the per-customer setting)
  python3 synthos_migration.py reset <customer_id>

EXIT CODES:
  0 — success
  1 — operator error (invalid customer id, missing --confirm, etc.)
  2 — runtime failure (DB unreachable, etc.)

THE CUTOVER PATTERN:
  1. python3 synthos_migration.py status                    # baseline
  2. python3 synthos_migration.py enable <one customer id>  # one
  3. monitor for ≥1 market session
  4. on success: enable next customer; on failure: disable + investigate
  5. iterate until status shows all on distributed
  6. (later) decommission daemon trader path entirely

ALL OPERATIONS ARE LOGGED to the customer's signals.db `events` table
via cust_db.log_event for audit purposes.
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
from pathlib import Path

# ── PATH SETUP ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "agents"))

from dotenv import load_dotenv
load_dotenv(str(_ROOT / "user" / ".env"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migration")


# ── HELPERS ───────────────────────────────────────────────────────────────

def _load_active_customers() -> list[str]:
    from retail_shared import get_active_customers
    return get_active_customers() or []


def _log_audit(customer_id: str, action: str, detail: str) -> None:
    """Write an audit row to the customer's events table. Best-effort."""
    try:
        from retail_database import get_customer_db
        cust_db = get_customer_db(customer_id)
        cust_db.log_event(
            "DISPATCH_MODE_CHANGE",
            agent="synthos_migration",
            details=f"{action}: {detail}",
        )
    except Exception as e:
        log.debug(f"[{customer_id[:8]}] audit log failed: {e}")


# ── COMMANDS ──────────────────────────────────────────────────────────────

def cmd_status(args) -> int:
    """Show every active customer + their effective dispatch mode."""
    from dispatch_mode import (
        env_default, resolve_customer_dispatch_mode, SETTING_KEY,
    )
    from retail_database import get_customer_db

    env = env_default()
    cids = _load_active_customers()
    if not cids:
        print("(no active customers found)")
        return 0

    print(f"env DISPATCH_MODE default: {env}")
    print()
    print(f"{'CUSTOMER_ID':40}  {'MODE':12}  SOURCE")
    print(f"{'-'*40}  {'-'*12}  {'-'*30}")
    daemon_n = distributed_n = 0
    for cid in cids:
        try:
            db = get_customer_db(cid)
            raw = db.get_setting(SETTING_KEY)
        except Exception as e:
            print(f"{cid:40}  {'?':12}  ERROR: {e}")
            continue
        effective = resolve_customer_dispatch_mode(cid)
        if raw and str(raw).lower() in ('daemon', 'distributed'):
            source = f"per-customer setting ({SETTING_KEY}={raw})"
        else:
            source = "env default"
        print(f"{cid:40}  {effective:12}  {source}")
        if effective == 'daemon':
            daemon_n += 1
        else:
            distributed_n += 1

    print()
    print(f"summary: {daemon_n} daemon, {distributed_n} distributed, {len(cids)} total")
    return 0


def cmd_enable(args) -> int:
    """Flip a single customer to distributed mode."""
    from dispatch_mode import set_customer_dispatch_mode, resolve_customer_dispatch_mode
    cid = args.customer_id
    if not cid:
        log.error("missing customer_id")
        return 1
    before = resolve_customer_dispatch_mode(cid)
    try:
        set_customer_dispatch_mode(cid, 'distributed')
    except Exception as e:
        log.error(f"set failed: {e}")
        return 2
    after = resolve_customer_dispatch_mode(cid)
    _log_audit(cid, "ENABLE_DISTRIBUTED", f"{before} → {after}")
    print(f"[{cid[:8]}] {before} → {after}")
    return 0


def cmd_disable(args) -> int:
    """Flip a single customer back to daemon mode."""
    from dispatch_mode import set_customer_dispatch_mode, resolve_customer_dispatch_mode
    cid = args.customer_id
    if not cid:
        log.error("missing customer_id")
        return 1
    before = resolve_customer_dispatch_mode(cid)
    try:
        set_customer_dispatch_mode(cid, 'daemon')
    except Exception as e:
        log.error(f"set failed: {e}")
        return 2
    after = resolve_customer_dispatch_mode(cid)
    _log_audit(cid, "DISABLE_DISTRIBUTED", f"{before} → {after}")
    print(f"[{cid[:8]}] {before} → {after}")
    return 0


def cmd_reset(args) -> int:
    """Clear the per-customer setting; env var default applies again."""
    from dispatch_mode import clear_customer_dispatch_mode, resolve_customer_dispatch_mode
    cid = args.customer_id
    if not cid:
        log.error("missing customer_id")
        return 1
    before = resolve_customer_dispatch_mode(cid)
    try:
        clear_customer_dispatch_mode(cid)
    except Exception as e:
        log.error(f"clear failed: {e}")
        return 2
    after = resolve_customer_dispatch_mode(cid)
    _log_audit(cid, "RESET_TO_ENV_DEFAULT", f"{before} → {after}")
    print(f"[{cid[:8]}] {before} → {after} (cleared, env default applies)")
    return 0


def cmd_enable_all(args) -> int:
    return _bulk(args, target='distributed', label='ENABLE_DISTRIBUTED_ALL')


def cmd_disable_all(args) -> int:
    return _bulk(args, target='daemon', label='DISABLE_DISTRIBUTED_ALL')


def _bulk(args, target: str, label: str) -> int:
    if not args.confirm:
        log.error(
            "bulk operation requires --confirm flag. This will affect ALL "
            "active customers. If you really mean it, re-run with --confirm."
        )
        return 1
    from dispatch_mode import set_customer_dispatch_mode
    cids = _load_active_customers()
    if not cids:
        print("(no active customers)")
        return 0
    ok = fail = 0
    for cid in cids:
        try:
            set_customer_dispatch_mode(cid, target)
            _log_audit(cid, label, f"bulk → {target}")
            ok += 1
        except Exception as e:
            log.warning(f"[{cid[:8]}] bulk set failed: {e}")
            fail += 1
    print(f"bulk {label}: {ok} ok, {fail} fail of {len(cids)} customers → {target}")
    return 0 if fail == 0 else 2


# ── ARG PARSING ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='synthos_migration.py',
        description='Per-customer dispatch-mode management for distributed-trader migration',
        epilog='See docstring for cutover pattern recommendations.',
    )
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('status', help='show current mode for every active customer')

    p_en = sub.add_parser('enable', help='flip ONE customer to distributed mode')
    p_en.add_argument('customer_id')

    p_di = sub.add_parser('disable', help='flip ONE customer back to daemon mode')
    p_di.add_argument('customer_id')

    p_re = sub.add_parser('reset', help='clear per-customer override; env default applies')
    p_re.add_argument('customer_id')

    p_ea = sub.add_parser('enable-all', help='flip ALL active customers to distributed (requires --confirm)')
    p_ea.add_argument('--confirm', action='store_true')

    p_da = sub.add_parser('disable-all', help='flip ALL active customers back to daemon (requires --confirm)')
    p_da.add_argument('--confirm', action='store_true')
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    handlers = {
        'status':       cmd_status,
        'enable':       cmd_enable,
        'disable':      cmd_disable,
        'reset':        cmd_reset,
        'enable-all':   cmd_enable_all,
        'disable-all':  cmd_disable_all,
    }
    return handlers[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
