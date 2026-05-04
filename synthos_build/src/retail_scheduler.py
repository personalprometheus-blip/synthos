"""
retail_scheduler.py — Synthos Execution Scheduler
Synthos v3.0

Called by systemd timers at scheduled trading sessions.
Runs the correct agent pipeline for every active customer in parallel.

Sessions:
    open       9:30am ET  — Full pipeline → Trade Logic (open)
    midday    12:30pm ET  — Sentiment → Macro → State → Validator → Trade (midday)
    close      3:30pm ET  — Validator → Trade Logic (close)
    news       hourly     — News agent only (market or overnight based on time)
    sentiment  30-min     — Market Sentiment only
    validate   on-demand  — Full validation stack (macro → state → bias → fault → validator)
    fault      on-demand  — System health check only

Design notes:
  - Each session fires once via systemd timer and exits cleanly
  - Per-customer subprocesses run in parallel; scheduler waits for all to finish
  - A lock file prevents overlapping runs of the same session
  - Kill switch halts trading only — retail_trade_logic_agent.py checks it at
    Gate 1. All other agents (news, sentiment, screener, heartbeat) continue
    running so market data collection and monitor visibility are unaffected.
  - Run history is written to logs/scheduler_history.json for the admin portal
  - If no customers exist yet, falls back to single-tenant (legacy / dev mode)

Usage:
    python3 retail_scheduler.py --session open
    python3 retail_scheduler.py --session midday
    python3 retail_scheduler.py --session close
    python3 retail_scheduler.py --session news
    python3 retail_scheduler.py --session sentiment
    python3 retail_scheduler.py --status          # show recent run history
    python3 retail_scheduler.py --session open --dry-run
"""

import os
import sys
import json
import fcntl
import argparse
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ── PATHS ──────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT_DIR   = _SCRIPT_DIR.parent
_AGENTS_DIR = _ROOT_DIR / 'agents'
_LOG_DIR    = _ROOT_DIR / 'logs'
_LOCK_DIR   = _ROOT_DIR / 'data'

sys.path.insert(0, str(_SCRIPT_DIR))
load_dotenv(_ROOT_DIR / 'user' / '.env')

# R10-7 / D6: is_market_hours() is now canonical in retail_shared — the
# local version used to differ by a single second at the close boundary
# (inclusive vs exclusive end).
from retail_shared import is_market_hours as _is_market_hours_shared

ET               = ZoneInfo('America/New_York')
KILL_SWITCH_FILE = _ROOT_DIR / '.kill_switch'
HISTORY_FILE     = _LOG_DIR / 'scheduler_history.json'

# ── LOGGING ────────────────────────────────────────────────────────────────────
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s scheduler: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    # FileHandler only.  Cron invokes this script with `>> scheduler.log
    # 2>&1` which already captures stdout+stderr into the same file, so
    # a StreamHandler(sys.stdout) here would duplicate every log line.
    # Uncaught tracebacks / raw prints still reach the log via the cron
    # redirect — we're just not routing deliberate log records twice.
    handlers=[
        logging.FileHandler(_LOG_DIR / 'scheduler.log'),
    ],
)
log = logging.getLogger('scheduler')

# ── SESSION PIPELINES ──────────────────────────────────────────────────────────
# Each entry: (agent_script, extra_args, timeout_seconds)
# Agents run sequentially within a session — order matters.
# Within each agent step, all customer subprocesses run in parallel.

# Agents that need per-customer execution (separate run per active customer).
# Everything else is "shared" — runs once, output visible to all customers.
_PER_CUSTOMER_AGENTS = {'retail_trade_logic_agent.py'}  # Only trade logic runs per-customer; news/sentiment/screener run once and write to shared DB

SESSION_PIPELINES = {
    'open': [
        # Data collection
        # NOTE: sector_screener moved to prep-only (once daily) as of v2.0.
        # Sector momentum is a multi-week signal — no value refreshing intraday.
        ('retail_market_sentiment_agent.py', [],                   600),
        ('retail_news_agent.py',             ['--session=market'], 420),
        # Analysis & classification
        ('retail_macro_regime_agent.py',     [],                   300),
        ('retail_market_state_agent.py',     [],                   180),
        # Per-customer checks
        ('retail_bias_detection_agent.py',   [],                   180),
        # System health & validation
        ('retail_fault_detection_agent.py',  [],                   120),
        ('retail_validator_stack_agent.py',  [],                   180),
        # Trade execution
        ('retail_trade_logic_agent.py',      ['--session=open'],   300),
    ],
    'midday': [
        ('retail_market_sentiment_agent.py', [],                   600),
        ('retail_macro_regime_agent.py',     [],                   300),
        ('retail_market_state_agent.py',     [],                   180),
        ('retail_bias_detection_agent.py',   [],                   180),
        ('retail_fault_detection_agent.py',  [],                   120),
        ('retail_validator_stack_agent.py',  [],                   180),
        ('retail_trade_logic_agent.py',      ['--session=midday'], 300),
    ],
    'close': [
        ('retail_fault_detection_agent.py',  [],                   120),
        ('retail_validator_stack_agent.py',  [],                   180),
        ('retail_trade_logic_agent.py',      ['--session=close'],  300),
    ],
    'news': [
        # Session arg resolved at runtime based on market hours
        ('retail_news_agent.py',             None,                 420),
    ],
    'sentiment': [
        ('retail_market_sentiment_agent.py', [],                   600),
    ],
    'overnight': [
        ('retail_news_agent.py',             ['--session=overnight'], 420),
    ],
    'prep': [
        # Sunday evening prep — builds Monday context.
        # Full pipeline: data → analysis → checks → validation.
        # Sector screener sweeps ALL 11 S&P sectors here (once per day).
        ('retail_sector_screener.py',        [],                     900),
        ('retail_news_agent.py',             ['--session=overnight'], 420),
        ('retail_market_sentiment_agent.py', [],                     600),
        ('retail_macro_regime_agent.py',     [],                     300),
        ('retail_market_state_agent.py',     [],                     180),
        ('retail_bias_detection_agent.py',   [],                     180),
        ('retail_fault_detection_agent.py',  [],                     120),
        ('retail_validator_stack_agent.py',  [],                     180),
    ],
    'trade': [
        # Hourly trader evaluation + execution.
        # Session type (open/midday/close) auto-resolved based on time of day.
        ('retail_trade_logic_agent.py',      None,                 300),
    ],
    'fault': [
        # Manual / on-demand system health check
        ('retail_fault_detection_agent.py',  [],                   120),
    ],
    'validate': [
        # On-demand full validation stack
        ('retail_macro_regime_agent.py',     [],                   300),
        ('retail_market_state_agent.py',     [],                   180),
        ('retail_bias_detection_agent.py',   [],                   180),
        ('retail_fault_detection_agent.py',  [],                   120),
        ('retail_validator_stack_agent.py',  [],                   180),
    ],
}

# Maximum history entries to retain in scheduler_history.json
HISTORY_MAX = 200


# ── MARKET HOURS ───────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """True if current ET time is within regular market hours (9:30am–4:00pm weekday).

    R10-7 / D6: delegates to `retail_shared.is_market_hours()` so all callers
    agree on the session boundary (exclusive at 16:00).
    """
    return _is_market_hours_shared()


def resolve_trade_args() -> list:
    """Auto-select trade session type based on time of day (ET)."""
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo("America/New_York")).hour
    if hour < 12:
        return ['--session=open']
    elif hour < 15:
        return ['--session=midday']
    else:
        return ['--session=close']


def resolve_news_args() -> list:
    """Decide whether to run the news agent in market or overnight mode."""
    return ['--session=market'] if is_market_hours() else ['--session=overnight']


# ── CUSTOMER LOADING ───────────────────────────────────────────────────────────

def get_active_customer_ids() -> list:
    """Return list of active customer UUIDs from auth.db."""
    try:
        import auth as _auth
        customers = _auth.list_customers()
        ids = [c['id'] for c in customers if c.get('is_active', 1)]
        log.info(f"Active customers: {len(ids)}")
        return ids
    except Exception as e:
        log.warning(f"Could not load customers from auth.db: {e}")
        return []


# ── AGENT EXECUTION ────────────────────────────────────────────────────────────

def run_agent_for_all_customers(
    script: str,
    extra_args: list,
    timeout: int,
    dry_run: bool = False,
) -> dict:
    """
    Spawn one subprocess per active customer, all in parallel.
    Returns {customer_id: 'ok' | 'failed' | 'timeout' | 'skipped'}.
    Falls back to a single run (no --customer-id) if no customers are found.
    """
    customer_ids = get_active_customer_ids()

    if dry_run:
        targets = customer_ids or ['__default__']
        log.info(f"[DRY RUN] Would run {script} for: {targets}")
        return {cid: 'skipped' for cid in targets}

    if not customer_ids:
        log.info(f"No customers found — running {script} in single-tenant mode")
        try:
            result = subprocess.run(
                [sys.executable, str(_AGENTS_DIR / script)] + extra_args,
                capture_output=True, text=True,
                timeout=timeout, cwd=str(_AGENTS_DIR),
            )
            outcome = 'ok' if result.returncode == 0 else 'failed'
            if outcome == 'failed':
                log.warning(f"{script} [single] stderr: {(result.stderr or '')[-300:]}")
            return {'__default__': outcome}
        except subprocess.TimeoutExpired:
            return {'__default__': 'timeout'}
        except Exception as e:
            log.error(f"{script} [single] launch error: {e}")
            return {'__default__': 'failed'}

    procs: dict = {}
    for cid in customer_ids:
        cmd = (
            [sys.executable, str(_AGENTS_DIR / script)]
            + extra_args
            + ['--customer-id', cid]
        )
        try:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(_AGENTS_DIR),
            )
            procs[cid] = p
            log.debug(f"Launched {script} for {cid[:8]} (pid={p.pid})")
        except Exception as e:
            log.error(f"Failed to launch {script} for {cid[:8]}: {e}")
            procs[cid] = None

    results: dict = {}
    for cid, proc in procs.items():
        if proc is None:
            results[cid] = 'failed'
            continue
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            if proc.returncode == 0:
                results[cid] = 'ok'
            else:
                err = (stderr or stdout or '')[-300:]
                log.warning(f"{script} failed for {cid[:8]}: {err[:120]}")
                results[cid] = 'failed'
        except subprocess.TimeoutExpired:
            proc.kill()
            log.warning(f"{script} timed out for customer {cid[:8]} after {timeout}s")
            results[cid] = 'timeout'

    return results


# ── SESSION ORCHESTRATION ──────────────────────────────────────────────────────

def run_session(session: str, dry_run: bool = False) -> bool:
    """
    Execute the full agent pipeline for a given session.
    Returns True if every agent step succeeded for every customer.
    """
    pipeline = SESSION_PIPELINES.get(session)
    if not pipeline:
        log.error(f"Unknown session: '{session}'. "
                  f"Valid: {', '.join(SESSION_PIPELINES)}")
        return False

    # Note: kill switch is NOT checked here. The trade logic agent (Gate 1)
    # handles its own kill switch check. Other agents (news, sentiment, screener)
    # do not have account access and continue running so that heartbeats and
    # market data collection are unaffected when trading is halted.
    if KILL_SWITCH_FILE.exists():
        log.warning(
            f"Kill switch is active — session '{session}' will run but "
            f"retail_trade_logic_agent.py will halt at Gate 1"
        )

    now = datetime.now(ET)
    started_at = datetime.now(timezone.utc).isoformat()
    log.info(
        f"{'[DRY RUN] ' if dry_run else ''}"
        f"=== Session '{session}' starting — {now.strftime('%Y-%m-%d %H:%M ET')} ==="
    )

    step_names = [s for s, _, _ in pipeline]
    log.info(f"Pipeline: {' → '.join(step_names)}")

    session_results = {}   # script → {customer_id: outcome}
    session_ok = True

    # 2026-05-04 — Tier 5 of distributed-trader migration: skip the trader
    # entry in any pipeline when DISPATCH_MODE=distributed. Other pipeline
    # stages (news, sentiment, validator, etc.) still run normally — they
    # produce signals on the shared DB that the dispatcher feeds to the
    # retail trader server via HTTP work packets.
    _dispatch_mode = os.environ.get('DISPATCH_MODE', 'daemon').lower()

    for script, args, timeout in pipeline:
        if _dispatch_mode == 'distributed' and 'trade_logic_agent' in script:
            log.info(
                f"[SCHEDULER] DISPATCH_MODE=distributed — skipping {script} "
                f"in {session} pipeline (synthos_dispatcher owns trader execution)"
            )
            continue
        # Resolve dynamic args (e.g. news agent market vs overnight)
        # Resolve dynamic args based on which agent needs them
        if args is None:
            if 'news_agent' in script:
                effective_args = resolve_news_args()
            elif 'trade_logic' in script:
                effective_args = resolve_trade_args()
            else:
                effective_args = []
        else:
            effective_args = args

        log.info(
            f"Step: {script}"
            + (f" {' '.join(effective_args)}" if effective_args else "")
        )
        # Write status file for portal wave animation
        try:
            import json as _json
            (_ROOT_DIR / '.agent_running').write_text(_json.dumps({
                'agent': script.replace('retail_', '').replace('_agent.py', '').replace('.py', '').replace('_', ' ').title(),
                'started': datetime.now(timezone.utc).isoformat(),
                'session': session,
            }))
        except Exception:
            pass
        t0 = time.monotonic()
        if script in _PER_CUSTOMER_AGENTS:
            # Per-customer: spawn one process per active customer
            results = run_agent_for_all_customers(
                script, effective_args, timeout, dry_run=dry_run
            )
        else:
            # Shared: run once in single-tenant mode (market data is universal)
            import subprocess as _sp
            log.info(f"  [shared] Running {script} once (not per-customer)")
            try:
                if dry_run:
                    results = {'__shared__': 'skipped'}
                else:
                    r = _sp.run(
                        [sys.executable, str(_AGENTS_DIR / script)] + effective_args,
                        capture_output=True, text=True,
                        timeout=timeout, cwd=str(_AGENTS_DIR),
                    )
                    outcome = 'ok' if r.returncode == 0 else 'failed'
                    if outcome == 'failed':
                        log.warning(f"  [shared] {script} stderr: {(r.stderr or '')[-300:]}")
                    results = {'__shared__': outcome}
            except _sp.TimeoutExpired:
                results = {'__shared__': 'timeout'}
            except Exception as e:
                log.error(f"  [shared] {script} error: {e}")
                results = {'__shared__': 'failed'}
        elapsed = time.monotonic() - t0

        ok      = sum(1 for v in results.values() if v == 'ok')
        failed  = sum(1 for v in results.values() if v == 'failed')
        timedout = sum(1 for v in results.values() if v == 'timeout')
        skipped = sum(1 for v in results.values() if v == 'skipped')
        total   = len(results)

        log.info(
            f"{script} — {elapsed:.1f}s — "
            f"{ok}/{total} ok"
            + (f", {failed} failed" if failed else "")
            + (f", {timedout} timeout" if timedout else "")
            + (f", {skipped} skipped" if skipped else "")
        )

        session_results[script] = results
        if failed or timedout:
            session_ok = False
            # Do NOT abort pipeline — later agents may still be able to run.
            # Trade Logic should execute even if Sentiment had errors.
            log.warning(f"Failures in {script} — continuing pipeline")

    # Clear agent running status
    try:
        (_ROOT_DIR / '.agent_running').unlink(missing_ok=True)
    except Exception:
        pass

    status = 'ok' if session_ok else 'partial'
    log.info(
        f"=== Session '{session}' complete — "
        f"{'OK' if session_ok else 'PARTIAL FAILURES'} ==="
    )

    _record_history(session, started_at, status, session_results)
    return session_ok


# ── RUN HISTORY ────────────────────────────────────────────────────────────────

def _record_history(session: str, started_at: str, status: str, results: dict):
    """Append a run record to scheduler_history.json (admin portal reads this)."""
    try:
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except Exception:
                history = []

        entry = {
            'session':    session,
            'started_at': started_at,
            'finished_at': datetime.now(timezone.utc).isoformat(),
            'status':     status,
            'steps':      {
                script: {
                    'ok':      sum(1 for v in r.values() if v == 'ok'),
                    'failed':  sum(1 for v in r.values() if v == 'failed'),
                    'timeout': sum(1 for v in r.values() if v == 'timeout'),
                    'customers': r,
                }
                for script, r in results.items()
            },
        }
        history.insert(0, entry)
        history = history[:HISTORY_MAX]
        HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except Exception as e:
        log.warning(f"Could not write run history: {e}")


def show_status():
    """Print recent run history to stdout."""
    if not HISTORY_FILE.exists():
        print("No run history yet.")
        return

    try:
        history = json.loads(HISTORY_FILE.read_text())
    except Exception as e:
        print(f"Could not read history: {e}")
        return

    print(f"\n{'='*60}")
    print(f"SYNTHOS SCHEDULER — Last {min(10, len(history))} runs")
    print(f"{'='*60}")
    for entry in history[:10]:
        icon   = '✓' if entry['status'] == 'ok' else '⚠'
        ts     = entry['started_at'][:16].replace('T', ' ')
        steps  = list(entry.get('steps', {}).keys())
        print(f"  {icon}  {ts}  [{entry['session']:10}]  {entry['status']:8}  "
              f"pipeline: {' → '.join(s.replace('retail_','').replace('_agent.py','') for s in steps)}")
    print(f"{'='*60}\n")


# ── OVERLAP PROTECTION ─────────────────────────────────────────────────────────

class SessionLock:
    """
    File-based lock to prevent two instances of the same session from running
    simultaneously. Uses fcntl for atomic locking — safe across processes.

    Stale-lock note (R10-11 triage): `fcntl.flock` is a POSIX advisory lock
    tied to the file descriptor. The Linux kernel releases it automatically
    when the holding process dies — even on SIGKILL, crash, or power loss
    — because the fd is torn down. The lock FILE may linger on disk, but
    the next acquire() opens a fresh fd and flock() succeeds immediately
    because no process holds the lock. No stale-lock cleanup needed here.
    """
    def __init__(self, session: str):
        _LOCK_DIR.mkdir(exist_ok=True)
        self._path = _LOCK_DIR / f'.scheduler_{session}.lock'
        self._fd   = None

    def acquire(self, timeout: int = 10) -> bool:
        """Try to acquire the lock. Returns False if already held after timeout."""
        self._fd = open(self._path, 'w')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd.write(str(os.getpid()))
                self._fd.flush()
                return True
            except BlockingIOError:
                time.sleep(0.5)
        return False

    def release(self):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
                self._path.unlink(missing_ok=True)
            except Exception:
                pass
        self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


# ── ENTRY POINT ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('scheduler', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

    parser = argparse.ArgumentParser(
        description='Synthos Retail Scheduler — executes agent pipelines per session'
    )
    parser.add_argument(
        '--session',
        choices=list(SESSION_PIPELINES.keys()),
        help='Trading session to execute',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print what would run without executing agents',
    )
    parser.add_argument(
        '--status', action='store_true',
        help='Show recent run history and exit',
    )
    args = parser.parse_args()

    if args.status:
        show_status()
        sys.exit(0)

    if not args.session:
        parser.print_help()
        sys.exit(1)

    lock = SessionLock(args.session)
    if not lock.acquire(timeout=10):
        log.warning(
            f"Session '{args.session}' is already running — "
            f"skipping this invocation (systemd timer overlap)"
        )
        sys.exit(0)

    try:
        ok = run_session(args.session, dry_run=args.dry_run)
        sys.exit(0 if ok else 1)
    finally:
        lock.release()
