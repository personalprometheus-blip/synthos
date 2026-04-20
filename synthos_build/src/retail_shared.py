"""
retail_shared.py — Canonical shared helpers used across multiple agents.

Phase C / D6 of post-audit cleanup (2026-04-20). Previously these helpers
were duplicated verbatim across retail_market_daemon, retail_portal,
retail_trade_logic_agent, and retail_dry_run. Any one of them diverging
silently would cause inconsistent behavior. Centralising here means there
is one place to change and one place to test.

Intentionally NOT consolidated here (still local per-file):
  - now_et()         — diverged return types (datetime object vs formatted
                       string); callers have different expectations
  - fetch_with_retry() — news agent version has stateful circuit-breaker
                       globals; sentiment agent version is simpler. Same
                       pattern, but extracting the state would require a
                       shared circuit-breaker object and is not worth it
                       until a caller actually needs to diverge.

Import pattern:
    from retail_shared import kill_switch_active, get_active_customers, is_market_hours
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger('retail_shared')

# Synthos root — two levels up from synthos_build/src/retail_shared.py
_ROOT_DIR = Path(__file__).resolve().parent.parent

_ET = ZoneInfo("America/New_York")

# Market session constants (ET)
_MARKET_OPEN_HOUR  = 9
_MARKET_OPEN_MIN   = 30
_MARKET_CLOSE_HOUR = 16
_MARKET_CLOSE_MIN  = 0


# ── Kill switch ────────────────────────────────────────────────────────────

def kill_switch_active() -> bool:
    """Return True if the admin kill-switch file exists.

    The kill switch is a plain file at <root>/.kill_switch. An admin or
    emergency SSH session can create it with `touch .kill_switch` to halt
    trade execution without a DB write. Market daemon checks it in its main
    loop; trader checks it at the top of run().

    Previously duplicated in:
      retail_market_daemon  (Path-based check)
      retail_portal         (os.path.exists)
      retail_trade_logic_agent (os.path.exists)
    All were functionally identical.
    """
    return (_ROOT_DIR / '.kill_switch').exists()


# ── Active customers ───────────────────────────────────────────────────────

def get_active_customers() -> list[str]:
    """Return list of active customer IDs from auth.db.

    Reads the customer registry and filters for is_active=True. Returns an
    empty list on any failure so callers can degrade gracefully rather than
    crash.

    Previously duplicated verbatim in:
      retail_market_daemon  (canonical source)
      retail_dry_run        (identical copy)
      retail_window_calculator (delegated to market_daemon, now points here)
    """
    try:
        import auth  # noqa: PLC0415 — auth is on sys.path in all call contexts
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active')]
    except Exception as e:
        log.error(f"get_active_customers failed: {e}")
        return []


# ── Market hours ───────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """Return True if the US regular session is currently open (9:30-16:00 ET,
    weekdays only).

    Does NOT check exchange holiday calendar — signals queued on a market
    holiday get CANCELLED_PROTECTIVE at the re-eval max-age threshold, so
    the failure mode is benign. Holiday awareness is tracked as a future TODO.

    Previously duplicated (with minor style differences) in:
      retail_market_daemon      — identical logic
      retail_scheduler          — identical logic, different style
      retail_watchdog           — identical logic
      retail_trade_logic_agent  — named is_market_hours_utc_now() for
                                   backward-compat; same implementation
    """
    now = datetime.now(_ET)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    open_time  = now.replace(hour=_MARKET_OPEN_HOUR,  minute=_MARKET_OPEN_MIN,
                             second=0, microsecond=0)
    close_time = now.replace(hour=_MARKET_CLOSE_HOUR, minute=_MARKET_CLOSE_MIN,
                             second=0, microsecond=0)
    return open_time <= now < close_time
