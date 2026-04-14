#!/usr/bin/env python3
"""
retail_market_daemon.py — Market Hours Trading Daemon
=====================================================
Replaces cron-based trading schedule with a single daemon process that owns
market hours. Runs continuously from 9:10 AM to 4:00 PM ET.

Architecture:
    1. Pre-market prep (9:15): screener → news → sentiment → price poll → trade
    2. Market open (9:30): continuous trade evaluation loop
    3. Enrichment every 30m: screener + sentiment + news interleaved
    4. Price poller: runs every cycle (~2s, keeps live_prices table fresh)
    5. Market close (4:00): final close-session trade evaluation, then exit

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
ENRICHMENT_INTERVAL_MIN = 30    # run sentiment+news every N minutes
HEARTBEAT_FILE = _ROOT_DIR / '.market_daemon_heartbeat'
MAX_CRASH_RETRIES = 3
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
            capture_output=True, text=True, timeout=120,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error(f"[TRADE] Timeout for {customer_id[:8]}")
        return False
    except Exception as e:
        log.error(f"[TRADE] Error for {customer_id[:8]}: {e}")
        return False


def run_trade_all_customers(session='open'):
    """Run trade logic for all active customers sequentially."""
    write_agent_running('retail_trade_logic_agent.py', session)
    customers = get_active_customers()
    if not customers:
        log.warning("[TRADE] No active customers found")
        return 0, 0

    log.info(f"[TRADE] Evaluating {len(customers)} customer(s) — session={session}")
    t0 = time.monotonic()
    ok = 0
    fail = 0

    for cid in customers:
        if _shutdown_requested or kill_switch_active():
            log.info("[TRADE] Shutdown/kill switch — stopping customer loop")
            break
        if run_trade_for_customer(cid, session):
            ok += 1
        else:
            fail += 1

    elapsed = time.monotonic() - t0
    log.info(f"[TRADE] Complete: {ok}/{len(customers)} ok, {fail} failed in {elapsed:.1f}s")
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


def run_screener():
    """Run sector screener once."""
    log.info("[SCREENER] Starting")
    write_agent_running('retail_sector_screener.py')
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(_ROOT_DIR / 'agents' / 'retail_sector_screener.py')],
            capture_output=True, text=True, timeout=300,
            cwd=str(_ROOT_DIR / 'agents'),
        )
        if result.returncode == 0:
            log.info("[SCREENER] Complete")
        return result.returncode == 0
    except Exception as e:
        log.error(f"[SCREENER] Error: {e}")
        return False


# ── Main Daemon Loop ──

def run_premarket_prep():
    """9:15 AM: Sequential prep block — screener → news → sentiment → trade."""
    log.info("=" * 60)
    log.info("PRE-MARKET PREP — screener → news → sentiment → trade")
    log.info("=" * 60)

    run_screener()
    if _shutdown_requested:
        return
    run_news(session='market')
    if _shutdown_requested:
        return
    run_sentiment()
    if _shutdown_requested:
        return
    run_price_poller()
    run_trade_all_customers(session='open')
    clear_agent_running()


def run_market_loop():
    """
    9:30 AM - 4:00 PM: Continuous trade evaluation with periodic enrichment.

    Loop:
        1. Run trade agent for all customers
        2. Check if it's time for enrichment (every ENRICHMENT_INTERVAL_MIN)
        3. If yes: run sentiment + news
        4. Repeat immediately
    """
    log.info("=" * 60)
    log.info("MARKET HOURS — continuous evaluation loop")
    log.info("=" * 60)

    cycle = 0
    last_enrichment = time.monotonic()
    enrichment_interval = ENRICHMENT_INTERVAL_MIN * 60  # seconds

    while not _shutdown_requested and not past_market_close():
        cycle += 1
        t_cycle = now_et().strftime('%H:%M:%S')

        # Kill switch only blocks trade execution — enrichment continues
        if kill_switch_active():
            log.info(f"[CYCLE {cycle}] {t_cycle} — kill switch active, skipping trade evaluation")
            ok, fail = 0, 0
        else:
            # Trade evaluation for all customers
            ok, fail = run_trade_all_customers(session='open')

        write_heartbeat(status="OK" if not kill_switch_active() else "KILL_SWITCH",
                        cycle=cycle, customers=ok + fail)

        # Check if enrichment is due (runs regardless of kill switch)
        since_enrichment = time.monotonic() - last_enrichment
        if since_enrichment >= enrichment_interval:
            log.info(f"[ENRICHMENT] {since_enrichment/60:.0f}m since last — running screener + sentiment + news")
            run_screener()
            if not _shutdown_requested:
                run_sentiment()
            if not _shutdown_requested:
                session = 'market' if is_market_hours() else 'overnight'
                run_news(session=session)
            last_enrichment = time.monotonic()

        # Update live prices every cycle (fast, ~2s)
        run_price_poller()

        clear_agent_running()
        send_retail_heartbeat('market_daemon', 'OK')

        # Brief pause between cycles to avoid hammering
        # Adaptive: 5s with few customers, shorter as list grows
        customers_count = ok + fail
        pause = max(2, min(10, 30 // max(customers_count, 1)))
        log.info(f"[CYCLE {cycle}] {t_cycle} — {ok} ok, {fail} fail — "
                 f"next enrichment in {(enrichment_interval - (time.monotonic() - last_enrichment))/60:.0f}m — "
                 f"pause {pause}s")

        time.sleep(pause)

    log.info(f"Market loop ended — cycle={cycle} shutdown={_shutdown_requested} "
             f"past_close={past_market_close()}")


def run_close_session():
    """4:00 PM: Final evaluation with close session parameters."""
    log.info("=" * 60)
    log.info("MARKET CLOSE — final evaluation")
    log.info("=" * 60)

    if not kill_switch_active():
        run_trade_all_customers(session='close')
    else:
        log.info("[CLOSE] Kill switch active — skipping close session trades")
    clear_agent_running()


def main():
    """Main entry point. Waits for pre-market, then runs through market day."""
    log.info("=" * 60)
    log.info(f"MARKET DAEMON starting — pid={os.getpid()}")
    log.info(f"  Pre-market: {PREMARKET_START_HOUR}:{PREMARKET_START_MIN:02d} ET")
    log.info(f"  Market: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - "
             f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
    log.info(f"  Enrichment interval: {ENRICHMENT_INTERVAL_MIN}m")
    log.info(f"  Owner: {OWNER_CUSTOMER_ID[:12] if OWNER_CUSTOMER_ID else 'not set'}...")
    log.info("=" * 60)
    send_retail_heartbeat('market_daemon', 'STARTING')

    if not is_weekday():
        log.info("Weekend — exiting")
        return

    write_heartbeat(status="STARTING")

    # Wait for pre-market if started early
    while not _shutdown_requested:
        t = now_et()
        if t.hour > MARKET_CLOSE_HOUR or (t.hour == MARKET_CLOSE_HOUR and t.minute >= MARKET_CLOSE_MIN):
            log.info("Past market close — exiting")
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

    # Phase 1: Pre-market prep
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

    clear_agent_running()
    write_heartbeat(status="STOPPED")
    send_retail_heartbeat('market_daemon', 'STOPPED')
    log.info("Market daemon shutting down — day complete")


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
                write_heartbeat(status="DEAD")
                send_retail_heartbeat('market_daemon', 'DEAD')
