#!/usr/bin/env python3
"""
retail_market_daemon.py — Market Hours Trading Daemon
=====================================================
Replaces cron-based trading schedule with a single daemon process that owns
market hours. Runs continuously from 9:10 AM to 4:00 PM ET.

Architecture:
    1. Pre-market prep (9:15): news → screener → sentiment → macro_regime →
       market_state → bias_detection → fault_detection → validator_stack →
       price poll → trade
    2. Market open (9:30): first trade evaluation (signals from prep)
    3. Every 30 min: full enrichment pipeline → validator → trade eval
    4. Every 10 min: lightweight reconciliation + exit checks only (no new signal eval)
    5. Every 60 sec: price poller (keeps live_prices fresh for portal)
    6. Market close (4:00): final close-session trade evaluation, then exit

    The trade agent only runs full signal evaluation after enrichment brings
    new data. Between enrichments, only reconciliation and exit checks run.
    This eliminates wasted cycles while keeping position monitoring responsive.

Kill switch behavior:
    - Kill switch ONLY stops trade execution
    - News, sentiment, screener, price poller continue running
    - Market data stays fresh even when trading is halted

Started by cron:
    10 9 * * 1-5  cd /home/pi516gb/synthos/synthos_build && python3 src/retail_market_daemon.py >> logs/market_daemon.log 2>&1

Replaces these scheduler cron entries during market hours:
    - retail_trade_logic_agent.py (was every 20 min)
    - retail_news_agent.py (was hourly)
    - retail_market_sentiment_agent.py (was every 30 min)
    - retail_sector_screener.py (was in open session only)
    - retail_price_poller.py (was every 1 min via cron)

Keep these cron entries active (not replaced):
    - retail_heartbeat.py (1 min — node heartbeat to monitor)
    - retail_backup.py (1:30 AM nightly)
    - retail_shutdown.py (Sat 3:55 PM)
    - rebuild_default_template.py (3 AM nightly)
    - retail_boot_sequence.py (@reboot)
    - retail_watchdog.py (@reboot)

Stopped by:
    - Self-exit: after 4:00 PM ET
    - SIGTERM: from cron, systemd, or manual kill
    - Kill switch: trade agent skipped, enrichment continues

Safety:
    - Writes .market_daemon_heartbeat file every cycle (watchdog monitors)
    - Writes .agent_running file per agent (portal wave animation)
    - Sends retail heartbeat to monitor every cycle
    - Self-restarts on uncaught exception (max 3 retries)
    - Kill switch checked every iteration (trade-only scope)
"""

import os
import sys
import time
import signal
import logging
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

# ── Path Setup ──
_SRC_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _SRC_DIR.parent
sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv
load_dotenv(_ROOT_DIR / 'user' / '.env')

ET = ZoneInfo("America/New_York")

# ── Config ──
PREMARKET_START_HOUR = 9
PREMARKET_START_MIN = 15
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 0
ENRICHMENT_INTERVAL_MIN = 30    # run enrichment (screener+sentiment+news) then trade
# Pre-open re-evaluation: an overnight-queued decision older than this
# gets CANCELLED_PROTECTIVE rather than executed. 18h covers a Friday-
# close → Monday-open window comfortably; anything older is almost
# certainly stale intent that the user would want re-examined.
PRE_OPEN_REEVAL_MAX_AGE_HOURS = 18
APPROVAL_CHECK_TIMES    = [(9, 30), (12, 0), (15, 30)]  # ET times to nudge managed-mode customers
RECON_INTERVAL_MIN      = 10    # lightweight reconciliation + exit checks between enrichments
PRICE_POLL_INTERVAL_SEC = 60    # price poller between major cycles
HEARTBEAT_FILE = _ROOT_DIR / '.market_daemon_heartbeat'
MAX_CRASH_RETRIES = 3
# OWNER_CUSTOMER_ID loaded for startup info only
OWNER_CUSTOMER_ID = os.environ.get('OWNER_CUSTOMER_ID', '')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s daemon: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('market_daemon')

# ── Graceful Shutdown ──
_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    log.info(f"Received signal {signum} — shutting down gracefully")
    _shutdown_requested = True

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ── Helpers ──

def now_et():
    return datetime.now(ET)


def is_weekday():
    return now_et().weekday() < 5


def is_premarket():
    """Between 9:15 and 9:30 ET."""
    t = now_et()
    start = t.replace(hour=PREMARKET_START_HOUR, minute=PREMARKET_START_MIN, second=0)
    market_open = t.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0)
    return start <= t < market_open


def is_market_hours():
    """Between 9:30 and 16:00 ET."""
    t = now_et()
    open_time = t.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0)
    close_time = t.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return open_time <= t < close_time


def past_market_close():
    t = now_et()
    close_time = t.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return t >= close_time


def kill_switch_active():
    kill_file = _ROOT_DIR / '.kill_switch'
    return kill_file.exists()


PID_FILE = _ROOT_DIR / '.market_daemon.pid'

def acquire_pidlock():
    """Prevent multiple daemon instances. Returns True if lock acquired."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            log.error(f"Another daemon is running (pid={old_pid}) — exiting")
            return False
        except (ProcessLookupError, ValueError):
            log.info(f"Stale pidfile found (pid not running) — taking over")
        except PermissionError:
            log.error(f"Another daemon is running (pid={old_pid}, permission denied) — exiting")
            return False
    PID_FILE.write_text(str(os.getpid()))
    return True

def release_pidlock():
    """Remove pidfile on shutdown."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def write_heartbeat(status="OK", cycle=0, customers=0):
    """Write heartbeat file for watchdog monitoring."""
    try:
        HEARTBEAT_FILE.write_text(json.dumps({
            'status': status,
            'timestamp': now_et().isoformat(),
            'cycle': cycle,
            'customers': customers,
            'pid': os.getpid(),
        }))
    except Exception:
        pass


AGENT_RUNNING_FILE = _ROOT_DIR / '.agent_running'

def write_agent_running(agent_name, session=''):
    """Write .agent_running file so the portal wave animation knows which agent is active."""
    try:
        AGENT_RUNNING_FILE.write_text(json.dumps({
            'agent': agent_name,
            'session': session,
            'started': now_et().isoformat(),
            'source': 'market_daemon',
        }))
    except Exception:
        pass

def clear_agent_running():
    """Clear .agent_running file when no agent is active."""
    try:
        if AGENT_RUNNING_FILE.exists():
            AGENT_RUNNING_FILE.unlink()
    except Exception:
        pass


def send_retail_heartbeat(agent_name='market_daemon', status='OK'):
    """Send heartbeat to monitor via retail_heartbeat module."""
    try:
        from retail_heartbeat import write_heartbeat as _hb
        _hb(agent_name=agent_name, status=status)
    except Exception as e:
        log.debug(f"Retail heartbeat failed: {e}")


def get_active_customers():
    """Return list of active customer IDs from auth.db."""
    try:
        import auth
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active')]
    except Exception as e:
        log.error(f"Could not list customers: {e}")
        return []


# ── Agent Runners ──

def run_news(session='overnight'):
    """Run news agent once (shared, not per-customer)."""
    log.info(f"[NEWS] Starting ({session})")
    write_agent_running('retail_news_agent.py', session)
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_news_agent.py'),
             f'--session={session}'],
            capture_output=True, text=True, timeout=420,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[NEWS] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[NEWS] Exit code {result.returncode} in {elapsed:.1f}s")
            if result.stderr:
                log.warning(f"[NEWS] stderr: {result.stderr[-200:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error(f"[NEWS] Timeout after 420s")
        return False
    except Exception as e:
        log.error(f"[NEWS] Error: {e}")
        return False


def run_sentiment():
    """Run sentiment agent once (shared, not per-customer)."""
    log.info("[SENTIMENT] Starting")
    write_agent_running('retail_market_sentiment_agent.py')
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_market_sentiment_agent.py')],
            capture_output=True, text=True, timeout=600,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[SENTIMENT] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[SENTIMENT] Exit code {result.returncode} in {elapsed:.1f}s")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("[SENTIMENT] Timeout after 600s")
        return False
    except Exception as e:
        log.error(f"[SENTIMENT] Error: {e}")
        return False


def run_trade_for_customer(customer_id, session='open'):
    """Run trade logic agent for a single customer."""
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_trade_logic_agent.py'),
             f'--session={session}',
             f'--customer-id={customer_id}'],
            capture_output=True, text=True, timeout=300,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error(f"[TRADE] Timeout for {customer_id[:8]}")
        return False
    except Exception as e:
        log.error(f"[TRADE] Error for {customer_id[:8]}: {e}")
        return False


MAX_TRADE_PARALLEL = 3   # max concurrent trade agent subprocesses


def _read_validator_verdict(customer_id):
    """Read validator verdict from customer DB. Returns (verdict, restrictions) or (None, [])."""
    try:
        import sqlite3
        db_path = _ROOT_DIR / 'data' / 'customers' / customer_id / 'signals.db'
        if not db_path.exists():
            return None, []
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT value FROM customer_settings WHERE key='_VALIDATOR_VERDICT'"
        ).fetchone()
        verdict = row[0] if row else None
        row2 = conn.execute(
            "SELECT value FROM customer_settings WHERE key='_VALIDATOR_RESTRICTIONS'"
        ).fetchone()
        restrictions = json.loads(row2[0]) if row2 else []
        conn.close()
        return verdict, restrictions
    except Exception:
        return None, []


TRADE_INDIVIDUAL_TIMEOUT_SEC = 240   # kill any trader subprocess stuck longer than this


def run_trade_all_customers(session='open'):
    """Run trade logic for all active customers in parallel (up to MAX_TRADE_PARALLEL).

    Each subprocess gets its own TRADE_INDIVIDUAL_TIMEOUT_SEC deadline; any
    trader that exceeds it is killed and counted as a failure, so one hung
    customer cannot stall the entire dispatch pool. Previously the main
    polling loop had no per-process deadline — a hang would pin all slots
    until external intervention, causing the 1-hour DISPATCH stalls we saw
    on 2026-04-17.
    """
    import subprocess as _sp

    write_agent_running('retail_trade_logic_agent.py', session)
    customers = get_active_customers()
    if not customers:
        log.warning("[TRADE] No active customers found")
        return 0, 0

    # Pre-filter: log validator verdicts for visibility
    verdicts = {cid: _read_validator_verdict(cid)[0] for cid in customers}
    v_counts: dict = {}
    for v in verdicts.values():
        v_counts[v or 'UNKNOWN'] = v_counts.get(v or 'UNKNOWN', 0) + 1
    v_summary = ', '.join(f"{cnt} {vrd}" for vrd, cnt in sorted(v_counts.items()))
    log.info(f"[DISPATCH] {len(customers)} customers: {v_summary}")

    t0            = time.monotonic()
    ok            = 0
    fail          = 0
    timeout_count = 0

    pending        = list(customers)
    active: dict   = {}   # {customer_id: Popen}
    proc_started: dict = {}  # {customer_id: monotonic start ts}

    def _retire(cid: str, proc, status: str, note: str = "") -> None:
        """Close streams, count the result, and drop from active.
        status is 'ok' | 'fail' | 'timeout'."""
        nonlocal ok, fail, timeout_count
        try:
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
        except Exception:
            pass
        if status == 'ok':
            ok += 1
        else:
            fail += 1
            if status == 'timeout':
                timeout_count += 1
        if note:
            log.warning(f"[TRADE] {cid[:8]} {status}: {note}")
        active.pop(cid, None)
        proc_started.pop(cid, None)

    while (pending or active) and not _shutdown_requested:
        # Launch up to MAX_TRADE_PARALLEL
        while pending and len(active) < MAX_TRADE_PARALLEL:
            cid = pending.pop(0)
            if _shutdown_requested:
                log.info("[DISPATCH] Shutdown requested — stopping launches")
                pending.clear()
                break
            # NOTE (halt v2): admin halt no longer halts dispatch here.
            # Each trader subprocess checks the halt state at its own entry
            # point and exits cleanly if halted. This preserves heartbeats,
            # scheduler ticks, and observability during admin maintenance.
            # See docs/specs/HALT_AGENT_REWRITE.md.
            try:
                cmd = [
                    sys.executable,
                    str(_ROOT_DIR / 'agents' / 'retail_trade_logic_agent.py'),
                    f'--session={session}',
                    f'--customer-id={cid}',
                ]
                p = _sp.Popen(
                    cmd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                    text=True, cwd=str(_ROOT_DIR / 'agents'),
                )
                active[cid]       = p
                proc_started[cid] = time.monotonic()
                vrd = verdicts.get(cid, '?')
                log.debug(f"[DISPATCH] Launched {cid[:8]} (pid={p.pid}, verdict={vrd})")
            except Exception as e:
                log.error(f"[DISPATCH] Failed to launch {cid[:8]}: {e}")
                fail += 1

        # Poll + enforce per-process deadline in one pass.
        now_mono = time.monotonic()
        for cid in list(active.keys()):
            proc = active[cid]
            ret  = proc.poll()
            if ret is not None:
                if ret == 0:
                    _retire(cid, proc, 'ok')
                else:
                    stderr = ''
                    try:
                        stderr = (proc.stderr.read() or '')[-200:]
                    except Exception:
                        pass
                    _retire(cid, proc, 'fail', note=f"exit={ret} {stderr[:100]}")
            elif now_mono - proc_started[cid] > TRADE_INDIVIDUAL_TIMEOUT_SEC:
                # Individual deadline exceeded — this is the fix for the
                # 1-hour DISPATCH hangs. Kill, reap, and move on so the pool
                # stays responsive.
                log.error(
                    f"[TRADE] {cid[:8]} exceeded {TRADE_INDIVIDUAL_TIMEOUT_SEC}s — killing"
                )
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                _retire(cid, proc, 'timeout',
                        note=f"killed after {TRADE_INDIVIDUAL_TIMEOUT_SEC}s")

        # Short sleep when we have no launches to do, so we don't spin.
        if active and (not pending or len(active) >= MAX_TRADE_PARALLEL):
            time.sleep(1)

    # Final sweep: anything still alive (typically because shutdown was
    # requested mid-loop) gets a short grace period then a hard kill.
    for cid in list(active.keys()):
        proc = active[cid]
        try:
            proc.wait(timeout=30)
            if proc.returncode == 0:
                _retire(cid, proc, 'ok')
            else:
                _retire(cid, proc, 'fail', note=f"exit={proc.returncode} (shutdown sweep)")
        except _sp.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            _retire(cid, proc, 'timeout', note="killed in shutdown sweep")

    elapsed = time.monotonic() - t0
    log.info(
        f"[TRADE] Complete: {ok}/{len(customers)} ok, {fail} failed"
        + (f", {timeout_count} timeout" if timeout_count else "")
        + f" in {elapsed:.1f}s (parallel={MAX_TRADE_PARALLEL})"
    )
    return ok, fail


def run_price_poller():
    """Run price poller once (updates live_prices table for portal)."""
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_price_poller.py')],
            capture_output=True, text=True, timeout=60,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        if result.returncode != 0:
            log.warning(f"[POLLER] Exit code {result.returncode}")
        return result.returncode == 0
    except Exception as e:
        log.error(f"[POLLER] Error: {e}")
        return False


def run_fault_detection():
    """Run fault detection agent — system health scan."""
    log.info("[FAULT DETECTION] Starting")
    write_agent_running('retail_fault_detection_agent.py')
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_fault_detection_agent.py')],
            capture_output=True, text=True, timeout=120,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        if result.returncode == 0:
            log.info("[FAULT DETECTION] Complete")
        else:
            log.warning(f"[FAULT DETECTION] Non-zero exit: {result.returncode}")
            if result.stderr:
                log.warning(f"[FAULT DETECTION] stderr: {result.stderr[-500:]}")
        return result.returncode == 0
    except Exception as e:
        log.error(f"[FAULT DETECTION] Error: {e}")
        return False


def run_screener():
    """Run sector screener once — sweeps all 11 S&P sectors.
    Only called from pre-market prep (once per day). Sector momentum is a
    multi-week signal; no value refreshing intraday."""
    log.info("[SCREENER] Starting — sweeping all 11 sectors")
    write_agent_running('retail_sector_screener.py')
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_sector_screener.py')],
            capture_output=True, text=True, timeout=900,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        if result.returncode == 0:
            log.info("[SCREENER] Complete")
        return result.returncode == 0
    except Exception as e:
        log.error(f"[SCREENER] Error: {e}")
        return False


def run_macro_regime():
    """Run macro regime classifier — shared agent, writes to master DB."""
    log.info("[MACRO REGIME] Starting")
    write_agent_running('Macro Regime')
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_macro_regime_agent.py')],
            capture_output=True, text=True, timeout=300,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[MACRO REGIME] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[MACRO REGIME] Exit code {result.returncode} in {elapsed:.1f}s")
            if result.stderr:
                log.warning(f"[MACRO REGIME] stderr: {result.stderr[-300:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("[MACRO REGIME] Timeout after 300s")
        return False
    except Exception as e:
        log.error(f"[MACRO REGIME] Error: {e}")
        return False


def run_market_state():
    """Run market state synthesizer — shared agent, writes to master DB."""
    log.info("[MARKET STATE] Starting")
    write_agent_running('Market State')
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_market_state_agent.py')],
            capture_output=True, text=True, timeout=180,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[MARKET STATE] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[MARKET STATE] Exit code {result.returncode} in {elapsed:.1f}s")
            if result.stderr:
                log.warning(f"[MARKET STATE] stderr: {result.stderr[-300:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("[MARKET STATE] Timeout after 180s")
        return False
    except Exception as e:
        log.error(f"[MARKET STATE] Error: {e}")
        return False


def run_bias_detection():
    """Run bias detection agent — handles all customers internally."""
    log.info("[BIAS DETECTION] Starting")
    write_agent_running('Bias Detection')
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_bias_detection_agent.py')],
            capture_output=True, text=True, timeout=180,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[BIAS DETECTION] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[BIAS DETECTION] Exit code {result.returncode} in {elapsed:.1f}s")
            if result.stderr:
                log.warning(f"[BIAS DETECTION] stderr: {result.stderr[-300:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("[BIAS DETECTION] Timeout after 180s")
        return False
    except Exception as e:
        log.error(f"[BIAS DETECTION] Error: {e}")
        return False


def run_validator_stack():
    """Run validator stack — pre-trade gatekeeper, handles all customers internally."""
    log.info("[VALIDATOR STACK] Starting")
    write_agent_running('Validator Stack')
    t0 = time.monotonic()
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_validator_stack_agent.py')],
            capture_output=True, text=True, timeout=180,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            log.info(f"[VALIDATOR STACK] Complete in {elapsed:.1f}s")
        else:
            log.warning(f"[VALIDATOR STACK] Exit code {result.returncode} in {elapsed:.1f}s")
            if result.stderr:
                log.warning(f"[VALIDATOR STACK] stderr: {result.stderr[-300:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("[VALIDATOR STACK] Timeout after 180s")
        return False
    except Exception as e:
        log.error(f"[VALIDATOR STACK] Error: {e}")
        return False


def promote_validated_signals():
    """The last link in the validation chain.

    Runs ONCE per enrichment cycle, after validator_stack completes and
    before trader dispatch. Stamps validator completion on all QUEUED
    signals, then promotes any signal that has all required stamps
    (news + sentiment + macro + market_state + validator) to VALIDATED.
    Per-signal PROMOTED rows and STUCK (with missing-stamps diagnostic)
    rows are written to signal_decisions so the fault detector and portal
    can explain exactly why any given signal did or did not pass.
    Trader reads only VALIDATED signals.
    """
    log.info("[PROMOTER] Stamping validator + promoting signals")
    try:
        from retail_database import get_customer_db
        if not OWNER_CUSTOMER_ID:
            log.error("[PROMOTER] OWNER_CUSTOMER_ID not set — skipping")
            return 0
        master = get_customer_db(OWNER_CUSTOMER_ID)
        stamped = master.stamp_signals_validator('OK')
        promoted, stuck = master.promote_validated_signals()
        log.info(f"[PROMOTER] validator-stamped {stamped}, "
                 f"promoted {promoted} signal(s) QUEUED → VALIDATED, "
                 f"{stuck} still stuck at QUEUED (missing stamps — see signal_decisions)")
        return promoted
    except Exception as e:
        log.error(f"[PROMOTER] Error: {e}")
        return 0


def run_pre_open_reeval():
    """Walk every active customer's pending_approvals rows that were
    queued overnight (queue_origin='overnight') and decide whether each
    one still makes sense now that the market is about to open.

    pending_approvals is per-customer (matches managed-mode approvals).
    This function iterates the same customer list the trader dispatch
    uses, so every fleet member gets the same treatment with one pass.

    Runs as the FIRST step of the market-open pipeline — before the
    normal enrichment cycle — so any orders we approve land in the
    APPROVED pool that the existing managed-mode executor picks up on
    the next trader dispatch.

    Decision rules (v1, intentionally conservative):
      - Queued row older than PRE_OPEN_REEVAL_MAX_AGE_HOURS → CANCELLED
        ("too stale — original gate state no longer representative")
      - Otherwise → APPROVED (ready for executor)
    Later versions can re-run gate4/5/6 for BUYs and gate10 for SELLs
    for a stricter check; v1 establishes the pipeline + cancel path
    with a small, explainable rule set.

    Returns (total_approved, total_cancelled) across all customers.
    """
    from retail_database import get_customer_db
    customers = get_active_customers()
    if not customers:
        log.info("[REEVAL] No active customers — skipping")
        return 0, 0

    total_approved = 0
    total_cancelled = 0
    total_touched = 0
    now_utc = datetime.now(ZoneInfo("UTC"))
    max_age_hours = PRE_OPEN_REEVAL_MAX_AGE_HOURS

    for cid in customers:
        try:
            cdb   = get_customer_db(cid)
            queue = cdb.get_overnight_queue()
        except Exception as e:
            log.warning(f"[REEVAL] {cid[:8]}: read overnight queue failed: {e}")
            continue
        if not queue:
            continue

        total_touched += len(queue)
        approved = 0
        cancelled = 0
        for row in queue:
            sid    = row.get('id')
            ticker = row.get('ticker', '?')
            try:
                cdb.mark_reevaluated(sid)
            except Exception as e:
                log.debug(f"[REEVAL] {cid[:8]}: mark_reevaluated({sid}) failed: {e}")

            # Age check — stale queued rows get an explicit cancel
            # rather than a silent out-of-date execute.
            queued_at_str = row.get('queued_at') or ''
            try:
                qdt = datetime.fromisoformat(queued_at_str.replace('Z', ''))
                if qdt.tzinfo is None:
                    qdt = qdt.replace(tzinfo=ZoneInfo("UTC"))
                age_hours = (now_utc - qdt).total_seconds() / 3600
            except Exception:
                age_hours = 0.0  # unparseable → assume fresh, let it through

            if age_hours > max_age_hours:
                reason = (f"queued {age_hours:.1f}h ago "
                          f"(>{max_age_hours}h max age) — gate state stale")
                try:
                    if cdb.cancel_protective(sid, reason):
                        cancelled += 1
                except Exception as e:
                    log.warning(f"[REEVAL] {cid[:8]}: cancel_protective "
                                f"failed for {sid}: {e}")
                continue

            # Otherwise → APPROVED for the managed-mode executor.
            try:
                cdb.update_approval_status(
                    sid, 'APPROVED',
                    decided_by='pre_open_reeval',
                    decision_note=(f"overnight queue re-evaluated at "
                                   f"{cdb.now()} (age {age_hours:.1f}h)"),
                )
                approved += 1
            except Exception as e:
                log.warning(f"[REEVAL] {cid[:8]}: approve failed for "
                            f"{sid}: {e}")

        if approved or cancelled:
            log.info(f"[REEVAL] {cid[:8]}: approved {approved}, "
                     f"cancelled {cancelled} of {len(queue)} row(s)")
        total_approved  += approved
        total_cancelled += cancelled

    log.info(f"[REEVAL] Complete — approved {total_approved}, "
             f"cancelled {total_cancelled} of {total_touched} queued row(s) "
             f"across {len(customers)} customer(s)")
    return total_approved, total_cancelled


def _begin_cycle(label='enrichment'):
    """Mint a new cycle_id, export it via env var (inherited by subprocess
    agents), and log the cycle start. Returns the cycle_id string."""
    cycle_id = uuid.uuid4().hex[:8]
    os.environ['SYNTHOS_CYCLE_ID'] = cycle_id
    log.info(f"[CYCLE {cycle_id}] {label} start — validation stamps tagged with this id")
    return cycle_id


def _end_cycle(cycle_id):
    """Clear the env var so no stray stamp outside a cycle picks it up."""
    os.environ.pop('SYNTHOS_CYCLE_ID', None)
    log.info(f"[CYCLE {cycle_id}] end")


# ── Main Daemon Loop ──

def run_premarket_prep():
    """9:15 AM: Sequential prep block — full enrichment pipeline → trade."""
    cycle_id = _begin_cycle(label='premarket')
    log.info("=" * 60)
    log.info("PRE-MARKET PREP — reeval → news → screener → sentiment → macro → state → bias → fault → validator → trade")
    log.info("=" * 60)

    # Phase 0: Pre-open re-evaluation of overnight-queued orders.
    # Runs first so APPROVED rows are visible to the managed-mode
    # executor in the trader dispatch that comes at the end of prep.
    run_pre_open_reeval()
    if _shutdown_requested:
        return

    # Phase 1: Data collection
    run_news(session='market')
    if _shutdown_requested:
        return
    run_screener()
    if _shutdown_requested:
        return
    run_sentiment()
    if _shutdown_requested:
        return

    # Phase 2: Analysis & classification
    run_macro_regime()
    if _shutdown_requested:
        return
    run_market_state()
    if _shutdown_requested:
        return

    # Phase 3: Per-customer checks
    run_bias_detection()
    if _shutdown_requested:
        return

    # Phase 4: System health & validation
    run_fault_detection()
    if _shutdown_requested:
        return
    run_validator_stack()
    if _shutdown_requested:
        return

    # Phase 4b: The last link in the validation chain.
    # Stamp validator completion + promote any QUEUED signal that has all
    # required stamps (news + sentiment + macro + market_state + validator).
    # Trader reads ONLY promoted (VALIDATED) signals in the next phase.
    promote_validated_signals()
    if _shutdown_requested:
        return

    # Phase 5: Price poll + trade
    run_price_poller()
    run_trade_all_customers(session='open')
    clear_agent_running()
    _end_cycle(cycle_id)


def run_market_loop():
    """
    9:30 AM - 4:00 PM: Event-driven trading with periodic enrichment.

    Schedule:
        Every 30 min:  enrichment (news → sentiment) → trade
        Every 10 min:  lightweight reconciliation + exit checks only
        Every 60 sec:  price poller (keeps live_prices fresh for portal)
        Once daily:    sector screener (pre-market prep only)

    The trade agent only runs full signal evaluation after enrichment
    brings new data. Between enrichments, only reconciliation and exit
    condition checks run — no wasted signal re-evaluation.
    """
    log.info("=" * 60)
    log.info("MARKET HOURS — event-driven loop")
    log.info("=" * 60)

    cycle = 0
    last_enrichment = time.monotonic()
    last_recon = time.monotonic()
    last_price_poll = time.monotonic()
    enrichment_interval = ENRICHMENT_INTERVAL_MIN * 60
    recon_interval = RECON_INTERVAL_MIN * 60

    # First trade evaluation right at market open (pre-market prep already ran enrichment).
    # Halt v2: dispatch always runs; each trader subprocess checks halt state at entry
    # and exits cleanly if admin/customer halt is active.
    run_trade_all_customers(session='open')
    clear_agent_running()

    while not _shutdown_requested and not past_market_close():
        cycle += 1
        t_cycle = now_et().strftime('%H:%M:%S')
        now_mono = time.monotonic()

        since_enrichment = now_mono - last_enrichment
        since_recon = now_mono - last_recon
        since_price = now_mono - last_price_poll

        # ── ENRICHMENT CYCLE (every 30 min) ──
        # Full pipeline: data → analysis → checks → validation → trade
        if since_enrichment >= enrichment_interval:
            cycle_id = _begin_cycle(label='enrichment')
            log.info(f"[ENRICHMENT] {since_enrichment/60:.0f}m — full pipeline")
            # Data collection
            # NOTE: sector screener is pre-market prep only (once daily).
            # Sector momentum doesn't move intraday, no value refreshing here.
            run_news(session='market')
            if not _shutdown_requested:
                run_sentiment()
            # Analysis & classification
            if not _shutdown_requested:
                run_macro_regime()
            if not _shutdown_requested:
                run_market_state()
            # Per-customer checks
            if not _shutdown_requested:
                run_bias_detection()
            # System health & validation
            if not _shutdown_requested:
                run_fault_detection()
            if not _shutdown_requested:
                run_validator_stack()
            # Last link: stamp validator + promote fully-validated signals
            if not _shutdown_requested:
                promote_validated_signals()
            # Trade execution (validator verdict gates decisions).
            # Halt v2: subprocesses handle their own halt-skip; dispatch runs.
            if not _shutdown_requested:
                run_trade_all_customers(session='open')
            last_enrichment = time.monotonic()
            last_recon = time.monotonic()  # enrichment includes reconciliation
            clear_agent_running()
            send_retail_heartbeat('market_daemon', 'OK')
            _end_cycle(cycle_id)

        # ── RECONCILIATION CYCLE (every 10 min between enrichments) ──
        # Lightweight: catches trailing stop fills, exit conditions, approved trades.
        # Halt v2: subprocesses handle their own halt-skip; dispatch runs.
        elif since_recon >= recon_interval:
            log.info(f"[RECON] {since_recon/60:.0f}m — reconciliation + exit checks")
            run_trade_all_customers(session='open')
            clear_agent_running()
            last_recon = time.monotonic()
            send_retail_heartbeat('market_daemon', 'OK')

        # ── PRICE POLL (every 60 sec) ──
        if since_price >= PRICE_POLL_INTERVAL_SEC:
            run_price_poller()
            last_price_poll = time.monotonic()

        write_heartbeat(status="OK" if not kill_switch_active() else "KILL_SWITCH",
                        cycle=cycle, customers=0)

        # Approval notifications (3x/day for managed-mode customers)
        check_approval_notifications()

        # Next enrichment / recon countdown
        next_enrichment = max(0, enrichment_interval - (time.monotonic() - last_enrichment))
        next_recon = max(0, recon_interval - (time.monotonic() - last_recon))
        next_event = min(next_enrichment, next_recon, PRICE_POLL_INTERVAL_SEC)

        log.info(f"[IDLE {cycle}] {t_cycle} — next enrichment {next_enrichment/60:.0f}m, "
                 f"next recon {next_recon/60:.0f}m — sleeping {min(next_event, 30):.0f}s")

        # Sleep until next event (cap at 30s for responsiveness)
        time.sleep(min(next_event, 30))

    log.info(f"Market loop ended — cycle={cycle} shutdown={_shutdown_requested} "
             f"past_close={past_market_close()}")


def check_approval_notifications():
    """
    At 9:30, 12:00, 15:30 ET — check managed-mode customers for pending approvals.
    If they have pending trades and aren't logged in, send text (preferred) or email.
    Max 3 notifications per day per customer.
    """
    t = now_et()
    current_check = (t.hour, t.minute)

    # Only run if we're within 2 minutes of a check time
    matched = False
    for check_h, check_m in APPROVAL_CHECK_TIMES:
        if t.hour == check_h and abs(t.minute - check_m) <= 2:
            matched = True
            break
    if not matched:
        return

    try:
        import auth
        from retail_database import get_customer_db
    except ImportError:
        return

    today_str = t.strftime('%Y-%m-%d')
    customers = get_active_customers()

    for cid in customers:
        try:
            db = get_customer_db(cid)
            mode = auth.get_operating_mode(cid)
            if mode != 'MANAGED':
                continue

            # Check for pending approvals
            pending = db.get_pending_approvals(status_filter=['PENDING_APPROVAL'])
            if not pending:
                continue

            # Check if already notified today (max 3)
            notif_key = f'_APPROVAL_NOTIF_{today_str}'
            sent_today = int(db.get_setting(notif_key) or 0)
            if sent_today >= 3:
                continue

            # Check if customer is currently logged in (session activity)
            # Skip notification if they're active — they can see it themselves
            try:
                last_activity = db.get_setting('_LAST_PORTAL_ACTIVITY')
                if last_activity:
                    from datetime import datetime as _dt
                    last = _dt.fromisoformat(last_activity)
                    idle_min = (t.replace(tzinfo=None) - last.replace(tzinfo=None)).total_seconds() / 60
                    if idle_min < 15:
                        continue  # active in last 15 min — skip
            except Exception:
                pass

            # Get customer contact info
            customer = auth.get_customer_by_id(cid) if hasattr(auth, 'get_customer_by_id') else None
            if not customer:
                try:
                    custs = auth.list_customers()
                    customer = next((c for c in custs if c['id'] == cid), None)
                except Exception:
                    continue
            if not customer:
                continue

            phone = customer.get('phone', '')
            email = customer.get('email', '')
            pref = (db.get_setting('NOTIFICATION_PREFERENCE') or 'text').lower()

            count = len(pending)
            tickers = ', '.join(p.get('ticker', '?') for p in pending[:3])
            msg = f"Synthos: {count} trade{'s' if count > 1 else ''} waiting for approval ({tickers}). Log in to review."

            sent = False
            # Prefer text if phone available and preference allows
            if pref in ('text', 'both') and phone:
                sent = _send_sms(phone, msg)
            # Fall back to email, or use if preferred
            if (not sent or pref == 'both') and email:
                sent = _send_email(email, f"[Synthos] {count} trade{'s' if count > 1 else ''} pending approval", msg)

            if sent:
                db.set_setting(notif_key, str(sent_today + 1))
                log.info(f"[APPROVAL NOTIFY] {cid[:8]}: {count} pending, notified via {'text' if phone and pref != 'email' else 'email'}")

        except Exception as _e:
            log.debug(f"[APPROVAL NOTIFY] {cid[:8]} error: {_e}")


def _send_sms(phone, message):
    """Send SMS via email-to-SMS gateway or Resend."""
    carrier_gw = os.environ.get('CARRIER_GATEWAY', '')
    resend_key = os.environ.get('RESEND_API_KEY', '')
    alert_from = os.environ.get('ALERT_FROM', '')

    if not resend_key or not alert_from:
        return False

    # Clean phone number
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        return False

    # If carrier gateway configured, send as email-to-SMS
    if carrier_gw:
        to_addr = f"{digits}@{carrier_gw}"
    else:
        # No gateway — fall back to email
        return False

    try:
        import requests as _req
        _req.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
            json={'from': alert_from, 'to': [to_addr], 'subject': '', 'text': message},
            timeout=10)
        return True
    except Exception:
        return False


def _send_email(email, subject, body):
    """Send email via Resend."""
    resend_key = os.environ.get('RESEND_API_KEY', '')
    alert_from = os.environ.get('ALERT_FROM', '')
    if not resend_key or not alert_from or not email:
        return False
    try:
        import requests as _req
        _req.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
            json={'from': alert_from, 'to': [email], 'subject': subject, 'text': body},
            timeout=10)
        return True
    except Exception:
        return False


def run_close_session():
    """4:00 PM: Final evaluation with close session parameters.
    Halt v2: always dispatch; trader subprocesses skip individually if halted."""
    log.info("=" * 60)
    log.info("MARKET CLOSE — final evaluation")
    log.info("=" * 60)

    run_trade_all_customers(session='close')
    clear_agent_running()

    # Post-close: backfill exit performance data, then run optimizer
    run_exit_backfill()
    run_trail_optimizer()


def run_exit_backfill():
    """Backfill post-exit prices for the trailing stop optimizer."""
    customers = get_active_customers()
    if not customers:
        return
    log.info(f"[BACKFILL] Checking {len(customers)} customer(s) for exit performance backfill")
    try:
        import auth
        from retail_database import get_customer_db
    except ImportError:
        return

    for cid in customers:
        try:
            db = get_customer_db(cid)
            rows = db.get_exit_performance_needing_backfill(min_days=5)
            if not rows:
                continue

            # Get Alpaca creds for price lookups
            ak, sk = auth.get_alpaca_credentials(cid)
            if not ak:
                continue

            import requests as _req
            base_url = os.environ.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')
            headers = {'APCA-API-KEY-ID': ak, 'APCA-API-SECRET-KEY': sk}
            filled = 0

            for row in rows:
                try:
                    ticker = row['ticker']
                    exit_ts = row['exit_timestamp']

                    # Fetch bars for 15 days after exit
                    r = _req.get(
                        f"{base_url}/v2/stocks/{ticker}/bars",
                        headers=headers,
                        params={'timeframe': '1Day', 'start': exit_ts[:10], 'limit': 15},
                        timeout=8,
                    )
                    if not r.ok:
                        continue
                    bars = (r.json().get('bars') or [])
                    if len(bars) < 2:
                        continue

                    closes = [b['c'] for b in bars]
                    highs = [b['h'] for b in bars]
                    lows = [b['l'] for b in bars]

                    price_5d = closes[min(5, len(closes) - 1)] if len(closes) > 1 else None
                    price_10d = closes[min(10, len(closes) - 1)] if len(closes) > 5 else None
                    peak = max(highs) if highs else None
                    trough = min(lows) if lows else None

                    db.backfill_exit_performance(
                        row_id=row['id'],
                        price_5d=price_5d,
                        price_10d=price_10d,
                        peak_during_hold=peak,
                        trough_during_hold=trough,
                    )
                    filled += 1
                except Exception as _e:
                    log.debug(f"[BACKFILL] {row.get('ticker','?')}: {_e}")

            if filled:
                log.info(f"[BACKFILL] {cid[:8]}: backfilled {filled}/{len(rows)} exits")
        except Exception as _e:
            log.debug(f"[BACKFILL] {cid[:8]} error: {_e}")


def run_trail_optimizer():
    """Run trailing stop optimizer for each customer after backfill."""
    customers = get_active_customers()
    if not customers:
        return
    log.info(f"[OPTIMIZER] Checking {len(customers)} customer(s)")
    try:
        import auth
        from retail_database import get_customer_db
        from trailing_stop_optimizer import run_optimization
    except ImportError as e:
        log.warning(f"[OPTIMIZER] Import error: {e}")
        return

    for cid in customers:
        try:
            db = get_customer_db(cid)
            # Check weekly cooldown
            last_run = db.get_setting('_OPTIMIZER_LAST_RUN')
            if last_run:
                from datetime import datetime as _dt
                try:
                    days_since = (_dt.now() - _dt.fromisoformat(last_run)).days
                    if days_since < 7:
                        continue
                except Exception:
                    pass

            result = run_optimization(db, min_sample=20, max_adj=0.20)
            if result and result.get('adjustments'):
                db.set_setting('_OPTIMIZER_LAST_RUN', now_et().isoformat())
                log.info(f"[OPTIMIZER] {cid[:8]}: {len(result['adjustments'])} adjustments applied")
            else:
                log.info(f"[OPTIMIZER] {cid[:8]}: no adjustments needed")
        except Exception as _e:
            log.debug(f"[OPTIMIZER] {cid[:8]} error: {_e}")


def run_overnight_cycle():
    """One-shot enrichment pass for off-hours / weekend runs.

    Fetches news incrementally via the cursor-based path, runs the full
    validation chain (sentiment → macro → market_state → fault →
    validator → promoter), and exits. Does NOT dispatch traders — the
    overnight-queue gate inside AlpacaClient would queue any submits
    anyway, so traders would produce no useful execution and just
    consume the dispatch budget.

    Intended to be cron-invoked hourly 24/7 (see docs/overnight_queue_plan.md
    Phase 4.1) — during market hours main() picks the intraday path
    instead, so this function only runs on off-hours entries.
    """
    cycle_id = _begin_cycle(label='overnight')
    log.info("=" * 60)
    log.info("OVERNIGHT CYCLE — news → sentiment → macro → state → fault → validator → promoter (no trader)")
    log.info("=" * 60)
    try:
        run_news(session='overnight')
        if _shutdown_requested: return
        run_sentiment()
        if _shutdown_requested: return
        run_macro_regime()
        if _shutdown_requested: return
        run_market_state()
        if _shutdown_requested: return
        run_fault_detection()
        if _shutdown_requested: return
        run_validator_stack()
        if _shutdown_requested: return
        promote_validated_signals()
    except Exception as e:
        log.error(f"[OVERNIGHT] Cycle error: {e}", exc_info=True)
    finally:
        clear_agent_running()
        _end_cycle(cycle_id)


def _run_intraday_pipeline():
    """The existing wait-for-open → premarket-prep → market-loop →
    close-session flow. Extracted from main() so main() can pick
    intraday vs overnight based on the current time."""
    write_heartbeat(status="STARTING")

    # Wait for pre-market if started early
    while not _shutdown_requested:
        t = now_et()
        if t.hour > MARKET_CLOSE_HOUR or (t.hour == MARKET_CLOSE_HOUR and t.minute >= MARKET_CLOSE_MIN):
            log.info("Past market close — switching to overnight cycle")
            run_overnight_cycle()
            return
        if t.hour > PREMARKET_START_HOUR or \
           (t.hour == PREMARKET_START_HOUR and t.minute >= PREMARKET_START_MIN):
            break
        wait_min = ((PREMARKET_START_HOUR - t.hour) * 60 +
                    (PREMARKET_START_MIN - t.minute))
        log.info(f"Waiting {wait_min}m for pre-market ({PREMARKET_START_HOUR}:{PREMARKET_START_MIN:02d})")
        time.sleep(min(60, wait_min * 60))

    if _shutdown_requested:
        return

    # Phase 1: Pre-market prep (includes pre-open re-evaluation as Phase 0)
    if is_premarket() or (now_et().hour == PREMARKET_START_HOUR and
                          now_et().minute >= PREMARKET_START_MIN):
        run_premarket_prep()

    if _shutdown_requested:
        return

    # Wait for market open if we finished prep early
    while not _shutdown_requested and not is_market_hours() and not past_market_close():
        remaining = (now_et().replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN) -
                     now_et()).total_seconds()
        if remaining <= 0:
            break
        log.info(f"Waiting {remaining/60:.0f}m for market open")
        time.sleep(min(30, max(1, remaining)))

    if _shutdown_requested:
        return

    # Phase 2: Market hours continuous loop
    if is_market_hours():
        run_market_loop()

    # Phase 3: Close session
    if not _shutdown_requested:
        run_close_session()


def main():
    """Entry point. Dispatches to intraday pipeline during the trading-
    hours runway (weekday pre-market through close) or the one-shot
    overnight cycle otherwise. Cron invokes hourly 24/7 — same script,
    different path based on wall-clock time. See
    docs/overnight_queue_plan.md Phase 4."""
    if not acquire_pidlock():
        return

    log.info("=" * 60)
    log.info(f"MARKET DAEMON starting — pid={os.getpid()}")
    log.info(f"  Pre-market: {PREMARKET_START_HOUR}:{PREMARKET_START_MIN:02d} ET")
    log.info(f"  Market: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - "
             f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
    log.info(f"  Enrichment interval: {ENRICHMENT_INTERVAL_MIN}m")
    log.info(f"  Owner: {OWNER_CUSTOMER_ID[:12] if OWNER_CUSTOMER_ID else 'not set'}...")
    log.info("=" * 60)
    send_retail_heartbeat('market_daemon', 'STARTING')

    try:
        t = now_et()
        weekday         = is_weekday()
        before_premkt   = weekday and (
            t.hour < PREMARKET_START_HOUR or
            (t.hour == PREMARKET_START_HOUR and t.minute < PREMARKET_START_MIN)
        )
        in_trading_day  = weekday and not past_market_close() and not before_premkt

        if in_trading_day:
            log.info(f"[MODE] intraday — current ET time {t.strftime('%H:%M')}")
            _run_intraday_pipeline()
        else:
            # Weekend, before pre-market, or after close. Run one-shot
            # overnight cycle so news keeps flowing and the validation
            # chain advances against whatever fresh signals arrive.
            reason = (
                "weekend" if not weekday else
                "before pre-market" if before_premkt else
                "past market close"
            )
            log.info(f"[MODE] overnight ({reason}) — current ET time {t.strftime('%H:%M')}")
            run_overnight_cycle()
    finally:
        clear_agent_running()
        release_pidlock()
        write_heartbeat(status="STOPPED")
        send_retail_heartbeat('market_daemon', 'STOPPED')
        log.info("Market daemon shutting down")


if __name__ == '__main__':
    retries = 0
    while retries < MAX_CRASH_RETRIES:
        try:
            main()
            break  # Clean exit
        except Exception as e:
            retries += 1
            clear_agent_running()
            log.error(f"DAEMON CRASH #{retries}: {e}", exc_info=True)
            write_heartbeat(status=f"CRASHED_{retries}")
            send_retail_heartbeat('market_daemon', f'CRASHED_{retries}')
            if retries < MAX_CRASH_RETRIES:
                log.info(f"Restarting in 30s (attempt {retries + 1}/{MAX_CRASH_RETRIES})")
                time.sleep(30)
            else:
                log.error("Max retries reached — daemon exiting")
                release_pidlock()
                write_heartbeat(status="DEAD")
                send_retail_heartbeat('market_daemon', 'DEAD')
