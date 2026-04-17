#!/usr/bin/env python3
"""
retail_dry_run.py — Pre-Market Diagnostic & Stress Test
========================================================
Simulates a compressed market day by running the full agent pipeline
through all sessions (prep → open → midday → close) in sequence.

Exercises the actual subprocess execution path — same as the daemon and
scheduler would during a real trading day — but with no time-of-day gating.

Trade logic agent runs in its existing MANAGED mode, so decisions are
queued for approval rather than executed. If a customer is in AUTOMATIC
mode, the agent still evaluates signals but Alpaca will reject market
orders outside market hours (harmless failure, logged).

Usage:
    python3 retail_dry_run.py                   # Full diagnostic
    python3 retail_dry_run.py --session open     # Single session only
    python3 retail_dry_run.py --skip-trade       # Pipeline only, no trade agent
    python3 retail_dry_run.py --parallel         # Test parallel trade dispatch
    python3 retail_dry_run.py --quick            # Validate + open only (fast check)

Output:
    Console report with per-step timing, pass/fail, API call counts,
    customer verdicts, and overall health assessment.
"""

import os, sys, time, json, subprocess, sqlite3, signal
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Paths ──
_ROOT = Path(__file__).resolve().parent.parent
_AGENTS = _ROOT / 'agents'
_SRC = _ROOT / 'src'
_DATA = _ROOT / 'data'

sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv
load_dotenv(_ROOT / 'user' / '.env')

ET = ZoneInfo("America/New_York")
OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')

# ── Colors ──
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
DIM    = '\033[2m'
BOLD   = '\033[1m'
RESET  = '\033[0m'


# ── Session Definitions (mirrors scheduler SESSION_PIPELINES) ──

# (script, args, timeout_sec, label)
PIPELINE_PREP = [
    # Screener sweeps all 11 sectors here; takes ~5-10 min depending on Alpaca latency.
    ('retail_sector_screener.py',        [],                      900, 'Sector Screener'),
    ('retail_news_agent.py',             ['--session=overnight'],  420, 'News Agent'),
    ('retail_market_sentiment_agent.py', [],                      600, 'Sentiment Agent'),
    ('retail_macro_regime_agent.py',     [],                      300, 'Macro Regime'),
    ('retail_market_state_agent.py',     [],                      180, 'Market State'),
    ('retail_bias_detection_agent.py',   [],                      180, 'Bias Detection'),
    ('retail_fault_detection_agent.py',  [],                      120, 'Fault Detection'),
    ('retail_validator_stack_agent.py',  [],                      180, 'Validator Stack'),
]

PIPELINE_OPEN = [
    # Screener runs in prep only (once daily); open session uses its latest output.
    ('retail_market_sentiment_agent.py', [],                      600, 'Sentiment Agent'),
    ('retail_news_agent.py',             ['--session=market'],    420, 'News Agent'),
    ('retail_macro_regime_agent.py',     [],                      300, 'Macro Regime'),
    ('retail_market_state_agent.py',     [],                      180, 'Market State'),
    ('retail_bias_detection_agent.py',   [],                      180, 'Bias Detection'),
    ('retail_fault_detection_agent.py',  [],                      120, 'Fault Detection'),
    ('retail_validator_stack_agent.py',  [],                      180, 'Validator Stack'),
]

PIPELINE_MIDDAY = [
    ('retail_market_sentiment_agent.py', [],                      600, 'Sentiment Agent'),
    ('retail_macro_regime_agent.py',     [],                      300, 'Macro Regime'),
    ('retail_market_state_agent.py',     [],                      180, 'Market State'),
    ('retail_bias_detection_agent.py',   [],                      180, 'Bias Detection'),
    ('retail_fault_detection_agent.py',  [],                      120, 'Fault Detection'),
    ('retail_validator_stack_agent.py',  [],                      180, 'Validator Stack'),
]

PIPELINE_CLOSE = [
    ('retail_fault_detection_agent.py',  [],                      120, 'Fault Detection'),
    ('retail_validator_stack_agent.py',  [],                      180, 'Validator Stack'),
]

# Trade agent step (appended when not --skip-trade)
TRADE_STEP = lambda session: ('retail_trade_logic_agent.py', [f'--session={session}'], 300, 'Trade Logic')

SESSIONS = {
    'prep':   PIPELINE_PREP,
    'open':   PIPELINE_OPEN,
    'midday': PIPELINE_MIDDAY,
    'close':  PIPELINE_CLOSE,
}


def get_active_customers():
    """Return list of active customer IDs."""
    try:
        import auth
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active')]
    except Exception as e:
        print(f"  {RED}Could not list customers: {e}{RESET}")
        return []


def get_customer_name(cid):
    """Get display name for a customer ID."""
    try:
        import auth
        customers = auth.list_customers()
        for c in customers:
            if c['id'] == cid:
                return c.get('display_name', cid[:8])
    except Exception:
        pass
    return cid[:8]


def read_validator_verdict(cid):
    """Read validator verdict from customer DB."""
    try:
        db_path = _DATA / 'customers' / cid / 'signals.db'
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


def read_market_state():
    """Read current market state from shared DB."""
    try:
        db_path = _DATA / 'customers' / OWNER_CID / 'signals.db'
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT value FROM customer_settings WHERE key='_MARKET_STATE'"
        ).fetchone()
        state = row[0] if row else None
        conn.close()
        return state
    except Exception:
        return None


def read_macro_regime():
    """Read current macro regime from shared DB."""
    try:
        db_path = _DATA / 'customers' / OWNER_CID / 'signals.db'
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT value FROM customer_settings WHERE key='_MACRO_REGIME'"
        ).fetchone()
        regime = row[0] if row else None
        conn.close()
        return regime
    except Exception:
        return None


def get_api_call_count():
    """Get today's API call count from shared DB. Uses UTC date to match
    the DB's UTC timestamps — using ET date here drops the current run's
    rows when ET/UTC dates straddle midnight."""
    try:
        from datetime import timezone
        db_path = _DATA / 'customers' / OWNER_CID / 'signals.db'
        conn = sqlite3.connect(str(db_path), timeout=5)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE date(timestamp) = ?", (today,)
        ).fetchone()
        count = row[0] if row else 0
        conn.close()
        return count
    except Exception:
        return 0


def get_api_breakdown():
    """Get today's API call breakdown by agent (UTC date to match DB)."""
    try:
        from datetime import timezone
        db_path = _DATA / 'customers' / OWNER_CID / 'signals.db'
        conn = sqlite3.connect(str(db_path), timeout=5)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        rows = conn.execute(
            "SELECT agent, COUNT(*) FROM api_calls WHERE date(timestamp) = ? GROUP BY agent ORDER BY COUNT(*) DESC",
            (today,)
        ).fetchall()
        conn.close()
        return dict(rows)
    except Exception:
        return {}


def run_agent(script, args, timeout, label, per_customer_ids=None):
    """
    Run an agent subprocess. Returns dict with timing and results.
    If per_customer_ids is provided, runs once per customer (parallel).
    """
    result = {
        'label': label,
        'script': script,
        'elapsed': 0,
        'status': 'unknown',
        'customers': {},
        'stderr_tail': '',
    }

    t0 = time.monotonic()

    if per_customer_ids:
        # Parallel dispatch (same as daemon's run_trade_all_customers)
        MAX_PARALLEL = 3
        pending = list(per_customer_ids)
        active = {}  # cid → Popen
        done = {}    # cid → 'ok'|'failed'|'timeout'

        while pending or active:
            # Launch
            while pending and len(active) < MAX_PARALLEL:
                cid = pending.pop(0)
                cmd = [sys.executable, str(_AGENTS / script)] + args + [f'--customer-id={cid}']
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, cwd=str(_AGENTS))
                    active[cid] = (p, time.monotonic())
                except Exception as e:
                    done[cid] = 'launch_failed'

            # Poll
            finished = []
            for cid, (proc, start_t) in active.items():
                ret = proc.poll()
                if ret is not None:
                    finished.append(cid)
                    if ret == 0:
                        done[cid] = 'ok'
                    else:
                        tail = (proc.stderr.read() or '')[-200:]
                        done[cid] = 'failed'
                        result['stderr_tail'] += f"\n[{cid[:8]}] {tail}"
                elif time.monotonic() - start_t > timeout:
                    proc.kill()
                    finished.append(cid)
                    done[cid] = 'timeout'

            for cid in finished:
                del active[cid]

            if active:
                time.sleep(0.5)

        result['customers'] = done
        ok = sum(1 for v in done.values() if v == 'ok')
        total = len(done)
        result['status'] = 'ok' if ok == total else ('partial' if ok > 0 else 'failed')

    else:
        # Shared agent — run once
        try:
            r = subprocess.run(
                [sys.executable, str(_AGENTS / script)] + args,
                capture_output=True, text=True, timeout=timeout,
                cwd=str(_AGENTS),
            )
            if r.returncode == 0:
                result['status'] = 'ok'
            else:
                result['status'] = 'failed'
                result['stderr_tail'] = (r.stderr or '')[-300:]
        except subprocess.TimeoutExpired:
            result['status'] = 'timeout'
        except Exception as e:
            result['status'] = 'error'
            result['stderr_tail'] = str(e)

    result['elapsed'] = time.monotonic() - t0
    return result


def status_icon(status):
    if status == 'ok':
        return f'{GREEN}PASS{RESET}'
    elif status == 'partial':
        return f'{YELLOW}PARTIAL{RESET}'
    elif status == 'timeout':
        return f'{RED}TIMEOUT{RESET}'
    else:
        return f'{RED}FAIL{RESET}'


def run_session_test(session_name, pipeline, customers, skip_trade=False, use_parallel=True):
    """Run a complete session and return results."""
    print(f"\n{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  SESSION: {session_name.upper()}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 60}{RESET}")

    api_before = get_api_call_count()
    session_t0 = time.monotonic()
    results = []

    # Run enrichment pipeline
    for script, args, timeout, label in pipeline:
        print(f"\n  {DIM}Running{RESET} {label} {DIM}({script}){RESET}", end='', flush=True)
        r = run_agent(script, args, timeout, label)
        results.append(r)
        icon = status_icon(r['status'])
        print(f"\r  [{icon}] {label:<22s} {r['elapsed']:6.1f}s")
        if r['stderr_tail'] and r['status'] != 'ok':
            for line in r['stderr_tail'].strip().split('\n')[-3:]:
                print(f"        {RED}{line.strip()[:100]}{RESET}")

    # Trade agent (per-customer)
    if not skip_trade:
        trade_session = session_name if session_name in ('open', 'midday', 'close') else 'open'
        script, args, timeout, label = TRADE_STEP(trade_session)
        print(f"\n  {DIM}Running{RESET} {label} {DIM}({len(customers)} customers, parallel={use_parallel}){RESET}", end='', flush=True)

        if use_parallel and customers:
            r = run_agent(script, args, timeout, label, per_customer_ids=customers)
        elif customers:
            # Sequential fallback
            r = {'label': label, 'script': script, 'elapsed': 0, 'status': 'ok',
                 'customers': {}, 'stderr_tail': ''}
            seq_t0 = time.monotonic()
            for cid in customers:
                cr = run_agent(script, args + [f'--customer-id={cid}'], timeout, label)
                r['customers'][cid] = cr['status']
                if cr['status'] != 'ok':
                    r['stderr_tail'] += cr['stderr_tail']
            r['elapsed'] = time.monotonic() - seq_t0
            ok = sum(1 for v in r['customers'].values() if v == 'ok')
            r['status'] = 'ok' if ok == len(customers) else ('partial' if ok > 0 else 'failed')
        else:
            r = {'label': label, 'elapsed': 0, 'status': 'skipped',
                 'customers': {}, 'stderr_tail': 'No active customers'}

        results.append(r)
        icon = status_icon(r['status'])
        print(f"\r  [{icon}] {label:<22s} {r['elapsed']:6.1f}s")

        # Per-customer detail
        for cid, outcome in r.get('customers', {}).items():
            name = get_customer_name(cid)
            v, restrictions = read_validator_verdict(cid)
            v_color = GREEN if v == 'GO' else (YELLOW if v == 'CAUTION' else RED)
            c_icon = f'{GREEN}ok{RESET}' if outcome == 'ok' else f'{RED}{outcome}{RESET}'
            print(f"        {name:<16s} [{c_icon}]  verdict={v_color}{v or '?'}{RESET}"
                  + (f"  restrictions={restrictions}" if restrictions else ""))

        if r['stderr_tail'] and r['status'] != 'ok':
            for line in r['stderr_tail'].strip().split('\n')[-3:]:
                print(f"        {RED}{line.strip()[:100]}{RESET}")

    session_elapsed = time.monotonic() - session_t0
    api_after = get_api_call_count()
    api_delta = api_after - api_before

    print(f"\n  {DIM}Session {session_name}: {session_elapsed:.1f}s total, "
          f"{api_delta} API calls{RESET}")

    return {
        'session': session_name,
        'elapsed': session_elapsed,
        'api_calls': api_delta,
        'steps': results,
        'all_ok': all(r['status'] in ('ok', 'skipped') for r in results),
    }


def print_summary(session_results, total_elapsed, customers):
    """Print the final diagnostic report."""
    print(f"\n\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  DRY RUN DIAGNOSTIC REPORT{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"  Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}")
    print(f"  Total elapsed: {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
    print(f"  Customers: {len(customers)}")

    # Session summary
    print(f"\n  {BOLD}Sessions:{RESET}")
    total_api = 0
    all_passed = True
    for sr in session_results:
        icon = f'{GREEN}PASS{RESET}' if sr['all_ok'] else f'{RED}FAIL{RESET}'
        print(f"    [{icon}] {sr['session']:<10s}  {sr['elapsed']:6.1f}s  "
              f"{sr['api_calls']:3d} API calls")
        total_api += sr['api_calls']
        if not sr['all_ok']:
            all_passed = False
            for step in sr['steps']:
                if step['status'] not in ('ok', 'skipped'):
                    print(f"           {RED}^ {step['label']}: {step['status']}{RESET}")

    # API usage
    total_today = get_api_call_count()
    print(f"\n  {BOLD}API Usage:{RESET}")
    print(f"    This run:  {total_api} calls")
    print(f"    Today:     {total_today} / 1,000")
    pct = total_today / 10
    bar_len = 30
    filled = int(pct / 100 * bar_len)
    bar_color = GREEN if pct < 65 else (YELLOW if pct < 85 else RED)
    print(f"    [{bar_color}{'█' * filled}{'░' * (bar_len - filled)}{RESET}] {pct:.0f}%")

    breakdown = get_api_breakdown()
    if breakdown:
        print(f"    By agent: {', '.join(f'{a}: {c}' for a, c in breakdown.items())}")

    # Market context
    print(f"\n  {BOLD}Market Context:{RESET}")
    regime = read_macro_regime()
    state = read_market_state()
    print(f"    Macro regime:  {regime or 'not set'}")
    print(f"    Market state:  {state or 'not set'}")

    # Customer verdicts
    print(f"\n  {BOLD}Customer Verdicts:{RESET}")
    for cid in customers:
        name = get_customer_name(cid)
        v, restrictions = read_validator_verdict(cid)
        v_color = GREEN if v == 'GO' else (YELLOW if v == 'CAUTION' else RED)
        print(f"    {name:<16s}  {v_color}{v or 'UNKNOWN'}{RESET}"
              + (f"  {DIM}{restrictions}{RESET}" if restrictions else ""))

    # Failed steps detail
    failures = []
    for sr in session_results:
        for step in sr['steps']:
            if step['status'] not in ('ok', 'skipped'):
                failures.append((sr['session'], step))

    if failures:
        print(f"\n  {BOLD}{RED}Failures ({len(failures)}):{RESET}")
        for sess, step in failures:
            print(f"    [{sess}] {step['label']}: {step['status']}")
            if step['stderr_tail']:
                for line in step['stderr_tail'].strip().split('\n')[-3:]:
                    print(f"      {DIM}{line.strip()[:120]}{RESET}")

    # Health assessment
    print(f"\n  {BOLD}Assessment:{RESET}")
    if all_passed:
        print(f"    {GREEN}{BOLD}ALL CLEAR{RESET} — pipeline healthy, ready for market open")
    else:
        print(f"    {RED}{BOLD}ISSUES FOUND{RESET} — review failures above before market open")

    print(f"\n{'=' * 60}\n")
    return all_passed


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Synthos Pre-Market Dry Run Diagnostic')
    parser.add_argument('--session', choices=['prep', 'open', 'midday', 'close'],
                        help='Run a single session instead of full day')
    parser.add_argument('--skip-trade', action='store_true',
                        help='Skip trade agent (enrichment pipeline only)')
    parser.add_argument('--parallel', action='store_true', default=True,
                        help='Use parallel trade dispatch (default: True)')
    parser.add_argument('--sequential', action='store_true',
                        help='Force sequential trade dispatch')
    parser.add_argument('--quick', action='store_true',
                        help='Quick check: validate + open only')
    args = parser.parse_args()

    use_parallel = not args.sequential

    print(f"\n{BOLD}{CYAN}SYNTHOS DRY RUN DIAGNOSTIC{RESET}")
    print(f"{DIM}{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}{RESET}")
    print(f"{DIM}Testing full pipeline outside market hours{RESET}")
    print(f"{DIM}Trade agent runs in evaluation mode (no real orders){RESET}\n")

    # Discover customers
    customers = get_active_customers()
    print(f"  Active customers: {len(customers)}")
    for cid in customers:
        name = get_customer_name(cid)
        print(f"    {name} ({cid[:12]}...)")

    total_t0 = time.monotonic()
    session_results = []

    if args.session:
        # Single session
        sessions_to_run = [args.session]
    elif args.quick:
        sessions_to_run = ['open']  # validate is just a subset of open's enrichment
    else:
        # Full day simulation: prep → open → midday → close
        sessions_to_run = ['prep', 'open', 'midday', 'close']

    for sess in sessions_to_run:
        pipeline = SESSIONS.get(sess, [])
        sr = run_session_test(sess, pipeline, customers,
                              skip_trade=args.skip_trade,
                              use_parallel=use_parallel)
        session_results.append(sr)

    total_elapsed = time.monotonic() - total_t0
    passed = print_summary(session_results, total_elapsed, customers)
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
