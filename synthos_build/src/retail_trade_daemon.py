#!/usr/bin/env python3
"""
retail_trade_daemon.py — Continuous Trade Execution Daemon
==========================================================
Phase 1 of TRADER_RESTRUCTURE_PLAN.md.

Splits the trader out of retail_market_daemon.py so trade decisions
run on market time (seconds) instead of intel cadence (30 min).

During market hours (9:30-16:00 ET weekdays), wakes every CYCLE_INTERVAL_SEC
seconds, checks the halt flag, and dispatches the trader for all active
customers. No new logic — same 13-gate trader, same 3-parallel subprocess
pool, same per-process 240s timeout, same bolt_decisions.log target.

WHAT CHANGES from v1:
- Trader evaluations happen ~every 30s instead of every 30min
- Intraday pullbacks become visible to the trader
- Halt check runs per cycle, not only per enrichment tick

WHAT STAYS THE SAME (Phase 1 is a pure refactor):
- 13 gates and their logic
- 0.75 Gate 5 composite threshold
- Signal source (VALIDATED signals from market_daemon's enrichment)
- Stop placement, sizing, entry criteria
- bolt_decisions.log writes
- MAX_TRADE_PARALLEL=3, TRADE_INDIVIDUAL_TIMEOUT_SEC=240

All trader-behavior changes (candidate generator, window calculator,
ATR stops, news reshape, gate rebalance) land in Phase 2+.

Enrichment pipeline (news, sentiment, macro, market_state, bias,
validator, screener) stays in retail_market_daemon.py and runs on its
30-min cadence. Approval notifications also stay in market_daemon.

Runs under its own pidfile + heartbeat so the watchdog can monitor it
independently of market_daemon. Systemd registration in install_retail.py.
"""

import os
import sys
import time
import signal
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_DIR / 'src'))

# Reuse market_daemon's helpers rather than duplicate. Import-safe because
# retail_market_daemon.py registers its SIGTERM/SIGINT handlers inside its
# own main(), not at module level.
from retail_market_daemon import (  # noqa: E402
    run_trade_all_customers,
    kill_switch_active,
    now_et,
    is_market_hours,
    past_market_close,
    is_weekday,
    send_retail_heartbeat,
)

ET = ZoneInfo("America/New_York")

# Cycle target per TRADER_RESTRUCTURE_PLAN. Measured cycle time from live
# scheduler.log shows 1.6-15s typical for 6-customer fleet with
# parallelism=3. 30s sleep gives ample headroom with room for growth.
CYCLE_INTERVAL_SEC = 30

HEARTBEAT_FILE = _ROOT_DIR / '.trade_daemon_heartbeat'
PID_FILE = _ROOT_DIR / '.trade_daemon.pid'
MAX_CRASH_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s trade_daemon: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('trade_daemon')


# ── Shutdown handling (this daemon's own handlers, not inherited) ──
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    log.info(f"Received signal {signum} — shutting down gracefully")
    _shutdown_requested = True


# ── Pidlock ──
def acquire_pidlock():
    """Prevent multiple trade_daemon instances. Returns True if lock acquired."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            log.error(f"Another trade_daemon is running (pid={old_pid}) — exiting")
            return False
        except (ProcessLookupError, ValueError):
            log.info("Stale pidfile found (pid not running) — taking over")
        except PermissionError:
            log.error(f"Another daemon running (pid={old_pid}, permission denied) — exiting")
            return False
    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pidlock():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


# ── Heartbeat ──
def write_heartbeat(status="OK", cycle=0, last_dispatch_sec=0):
    try:
        HEARTBEAT_FILE.write_text(json.dumps({
            'status': status,
            'timestamp': now_et().isoformat(),
            'cycle': cycle,
            'last_dispatch_sec': last_dispatch_sec,
            'pid': os.getpid(),
        }))
    except Exception:
        pass


# ── Main loop ──
def wait_for_market_open():
    """Sleep until market open (9:30 ET) or exit on shutdown/close.
    Returns True if market opened, False if exiting for other reasons."""
    while not _shutdown_requested:
        if is_market_hours():
            return True
        if past_market_close() or not is_weekday():
            return False
        time.sleep(30)
    return False


def trade_loop():
    """Main continuous loop during market hours."""
    log.info(
        f"[LOOP] Entering trading loop — cycle interval {CYCLE_INTERVAL_SEC}s"
    )
    cycle = 0
    last_dispatch_sec = 0.0

    while not _shutdown_requested and not past_market_close():
        cycle += 1
        t_cycle = now_et().strftime('%H:%M:%S')

        if kill_switch_active():
            log.info(f"[CYCLE {cycle}] {t_cycle} — kill switch active, skipping dispatch")
            write_heartbeat(status="KILL_SWITCH", cycle=cycle,
                            last_dispatch_sec=last_dispatch_sec)
        else:
            t0 = time.monotonic()
            log.info(f"[CYCLE {cycle}] {t_cycle} — dispatching trader")
            try:
                ok, fail = run_trade_all_customers(session='open')
                last_dispatch_sec = time.monotonic() - t0
                log.info(
                    f"[CYCLE {cycle}] dispatch complete: {ok} ok, {fail} fail "
                    f"in {last_dispatch_sec:.1f}s"
                )
                write_heartbeat(status="OK", cycle=cycle,
                                last_dispatch_sec=last_dispatch_sec)
                send_retail_heartbeat('trade_daemon', 'OK')
            except Exception as e:
                last_dispatch_sec = time.monotonic() - t0
                log.error(f"[CYCLE {cycle}] dispatch raised: {e}", exc_info=True)
                write_heartbeat(status="ERROR", cycle=cycle,
                                last_dispatch_sec=last_dispatch_sec)
                send_retail_heartbeat('trade_daemon', f'ERROR: {e}')

        # Sleep until next cycle. Short granularity so shutdown + past-close
        # checks stay responsive even if CYCLE_INTERVAL_SEC grows later.
        slept = 0
        while slept < CYCLE_INTERVAL_SEC and not _shutdown_requested and not past_market_close():
            time.sleep(min(5, CYCLE_INTERVAL_SEC - slept))
            slept += 5

    log.info(
        f"[LOOP] Exit — cycle={cycle} shutdown={_shutdown_requested} "
        f"past_close={past_market_close()}"
    )


def main():
    """Entry point — register signals, acquire lock, run market loop.

    Behavior by wall-clock time at startup:
      - Weekend / non-weekday           → exit immediately
      - Pre-market (before 9:30 ET)     → sleep until market open, then loop
      - Market hours (9:30-16:00 ET)    → enter loop immediately
      - Post-close (after 16:00 ET)     → exit immediately
    """
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    if not acquire_pidlock():
        return

    log.info("=" * 60)
    log.info(f"TRADE DAEMON starting — pid={os.getpid()}")
    log.info(f"  Cycle interval: {CYCLE_INTERVAL_SEC}s")
    log.info(f"  Market hours: 9:30-16:00 ET weekdays")
    log.info("=" * 60)
    send_retail_heartbeat('trade_daemon', 'STARTING')

    try:
        if not is_weekday():
            log.info("Not a weekday — exiting cleanly")
            return
        if past_market_close():
            log.info("Past market close — nothing to do, exiting")
            return
        if not is_market_hours():
            log.info("Pre-market — waiting for 9:30 ET open")
            if not wait_for_market_open():
                log.info("Market closed / shutdown while waiting — exiting")
                return

        trade_loop()
    finally:
        release_pidlock()
        write_heartbeat(status="STOPPED")
        send_retail_heartbeat('trade_daemon', 'STOPPED')
        log.info("Trade daemon stopped")


if __name__ == '__main__':
    # Crash-retry loop mirrors retail_market_daemon.py pattern. If we
    # crash mid-day the watchdog or cron respawns us.
    retries = 0
    while retries < MAX_CRASH_RETRIES:
        try:
            main()
            break
        except Exception as e:
            retries += 1
            log.error(f"Daemon crashed (retry {retries}/{MAX_CRASH_RETRIES}): {e}", exc_info=True)
            if retries < MAX_CRASH_RETRIES:
                time.sleep(15)
    else:
        log.error(f"Exceeded {MAX_CRASH_RETRIES} retries — giving up")
        sys.exit(1)
