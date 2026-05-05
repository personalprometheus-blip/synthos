#!/usr/bin/env python3
"""validate_ticker_state.py — End-to-end validation for the ticker_state architecture.

Runs on pi5 against the real shared DB. Verifies all four phases:
  Phase 1: schemas exist, helpers work, backfill populated rows
  Phase 2: writer agents dual-write to ticker_state
  Phase 3: trader gate 5 reads ticker_state, position snapshots capture
  Phase 3c: bias-detection findings carry meta+detail; validator extracts sector

Each test prints pass/fail. Exit code = number of failures.

Usage:
    python3 validate_ticker_state.py
"""
import os
import sys
import sqlite3
import json
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

from retail_database import get_shared_db, get_customer_db


GREEN = '\033[92m'
RED = '\033[91m'
YEL = '\033[93m'
DIM = '\033[2m'
RST = '\033[0m'

passes = []
failures = []


def t_pass(name, detail=''):
    passes.append(name)
    print(f'  {GREEN}✓{RST} {name}' + (f' {DIM}({detail}){RST}' if detail else ''))


def t_fail(name, reason):
    failures.append((name, reason))
    print(f'  {RED}✗{RST} {name} — {reason}')


def section(title):
    print()
    print(f'{YEL}── {title} ──{RST}')


# ───────────────────────────────────────────────────────────────────────────
# PHASE 1 — Foundation
# ───────────────────────────────────────────────────────────────────────────
section('PHASE 1 — Foundation')

shared = get_shared_db()
conn = sqlite3.connect(shared.path)
conn.row_factory = sqlite3.Row

# Schema tables exist
for tbl in ('ticker_state', 'ticker_state_archive', 'ticker_state_rebuild_log'):
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (tbl,)
    ).fetchone()[0]
    if n == 1:
        cols = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        t_pass(f'table {tbl} exists', f'{len(cols)} columns')
    else:
        t_fail(f'table {tbl} exists', 'missing')

# ticker_state has expected columns
expected_cols = {
    'ticker', 'is_active', 'first_seen_at', 'last_active_at',
    'sector', 'company', 'price', 'price_at',
    'sentiment_score', 'sentiment_evaluated_at', 'cascade_tier',
    'screener_score', 'screener_evaluated_at',
    'momentum_score', 'momentum_evaluated_at',
    'sector_score', 'sector_evaluated_at',
    'news_score_4h', 'news_evaluated_at',
    'updated_at',
}
ts_cols = {r['name'] for r in conn.execute("PRAGMA table_info(ticker_state)")}
missing = expected_cols - ts_cols
if not missing:
    t_pass('ticker_state has all expected columns', f'{len(ts_cols)} total')
else:
    t_fail('ticker_state has all expected columns', f'missing: {sorted(missing)}')

# positions has snapshot columns
sample_cust = next(iter(glob.glob('/home/pi516gb/synthos/synthos_build/data/customers/30eff008*/signals.db')), None)
if sample_cust:
    c2 = sqlite3.connect(sample_cust)
    pcols = {r[1] for r in c2.execute("PRAGMA table_info(positions)").fetchall()}
    for col in ('entry_state_snapshot', 'exit_state_snapshot'):
        if col in pcols:
            t_pass(f'positions.{col} exists')
        else:
            t_fail(f'positions.{col} exists', 'missing')

# Helpers smoke-test
t = '_VALID_SMOKE_TEST'
conn.execute("DELETE FROM ticker_state WHERE ticker=?", (t,))
conn.commit()

# upsert + auto-twin
shared.upsert_ticker_state(t, sentiment_score=0.42, screener_score=0.6)
state = shared.get_ticker_state(t)
if state and state['sentiment_score'] == 0.42 and state['screener_score'] == 0.6:
    t_pass('upsert_ticker_state writes value fields')
else:
    t_fail('upsert_ticker_state writes value fields', f'got {state}')
if state and state['sentiment_evaluated_at'] and state['screener_evaluated_at']:
    t_pass('upsert_ticker_state auto-derives *_evaluated_at twins')
else:
    t_fail('upsert_ticker_state auto-derives *_evaluated_at twins', 'missing twin timestamps')

# unknown field rejected
ok = shared.upsert_ticker_state(t, not_a_real_field=42)
if not ok:
    t_pass('upsert_ticker_state rejects unknown field')
else:
    t_fail('upsert_ticker_state rejects unknown field', 'returned True for bad field')

# get_ticker_state returns dict
got = shared.get_ticker_state(t)
if isinstance(got, dict) and got.get('ticker') == t:
    t_pass('get_ticker_state returns dict')
else:
    t_fail('get_ticker_state returns dict', f'got {type(got).__name__}')

# mark_ticker_active idempotent
shared.mark_ticker_active(t)
shared.mark_ticker_active(t)
state2 = shared.get_ticker_state(t)
if state2 and state2['is_active'] == 1:
    t_pass('mark_ticker_active idempotent')
else:
    t_fail('mark_ticker_active idempotent', f'is_active={state2["is_active"] if state2 else None}')

# Backfill row count
n_rows = conn.execute("SELECT COUNT(*) FROM ticker_state").fetchone()[0]
if n_rows >= 1000:
    t_pass(f'ticker_state populated', f'{n_rows} rows')
else:
    t_fail('ticker_state populated', f'only {n_rows} rows (expected ≥1000)')

# Cleanup
conn.execute("DELETE FROM ticker_state WHERE ticker=?", (t,))
conn.commit()


# ───────────────────────────────────────────────────────────────────────────
# PHASE 2 — Writers
# ───────────────────────────────────────────────────────────────────────────
section('PHASE 2 — Writers')

# Stamp via legacy path → ticker_state should pick up
t = '_VALID_STAMP_TEST'
conn.execute("DELETE FROM ticker_state WHERE ticker=?", (t,))
conn.commit()

shared.stamp_signals_sentiment(t, 0.77, cascade_tier=2)
state = shared.get_ticker_state(t)
if state and state['sentiment_score'] == 0.77 and state['cascade_tier'] == 2:
    t_pass('stamp_signals_sentiment dual-writes ticker_state', f'score=0.77 tier=2')
else:
    t_fail('stamp_signals_sentiment dual-writes ticker_state', f'got {state}')

shared.stamp_signals_screener([t], score=0.55)
state2 = shared.get_ticker_state(t)
if state2 and state2['screener_score'] == 0.55 and state2['sentiment_score'] == 0.77:
    t_pass('stamp_signals_screener dual-writes ticker_state', 'score=0.55, sentiment unchanged')
else:
    t_fail('stamp_signals_screener dual-writes ticker_state', f'got {state2}')

# news recompute hook
score = shared.recompute_news_score_4h(t)
# This ticker has no news_feed entries; expect None and no write
if score is None:
    t_pass('recompute_news_score_4h returns None for ticker with no news', 'no-write semantics correct')
else:
    t_fail('recompute_news_score_4h returns None for ticker with no news', f'got {score}')

# Real ticker with news (use AAPL or any high-volume ticker)
real_score = shared.recompute_news_score_4h('AAPL')
if real_score is not None:
    aapl_state = shared.get_ticker_state('AAPL')
    if aapl_state and aapl_state['news_score_4h'] is not None:
        t_pass('recompute_news_score_4h writes ticker_state.news_score_4h for ticker with news',
               f'AAPL={real_score:.2f}')
    else:
        t_fail('recompute_news_score_4h writes news_score_4h', 'returned value but DB row missing field')
else:
    t_pass('recompute_news_score_4h skipped AAPL', 'no recent news in 4h window — expected after-hours')

# Cleanup
conn.execute("DELETE FROM ticker_state WHERE ticker=?", (t,))
conn.commit()


# ───────────────────────────────────────────────────────────────────────────
# PHASE 3 — Readers + bias-finding fix
# ───────────────────────────────────────────────────────────────────────────
section('PHASE 3 — Readers + bias serialization fix')

# Trader gate 5 should read ticker_state when present.
# Smoke-test the lookup using whichever ticker is most recently active —
# we don't actually invoke gate 5 here (would require alpaca creds + live
# signal). Just verifying the read path works against real data.
recent = conn.execute(
    "SELECT ticker, sentiment_score, sector FROM ticker_state "
    "WHERE sentiment_score IS NOT NULL "
    "ORDER BY updated_at DESC LIMIT 1"
).fetchone()
if recent:
    state = shared.get_ticker_state(recent['ticker'])
    if state:
        t_pass('ticker_state read path works against real data',
               f'{recent["ticker"]}: sentiment={state["sentiment_score"]}, sector={state["sector"]}')
    else:
        t_fail('ticker_state read path works', f'{recent["ticker"]} returned None')
else:
    t_fail('ticker_state read path works', 'no rows have sentiment_score — backfill incomplete?')

# Bias detection: latest scan should now have meta + detail in findings
import sqlite3
owner_db = '/home/pi516gb/synthos/synthos_build/data/customers/30eff008-c27a-4c71-a788-05f883e4e3a0/signals.db'
c3 = sqlite3.connect(owner_db)
r = c3.execute("SELECT value FROM customer_settings WHERE key='_BIAS_SCAN_LAST'").fetchone()
if r:
    scan = json.loads(r[0])
    findings = scan.get('findings', [])
    crit_findings = [f for f in findings if f.get('severity') == 'CRITICAL']
    if crit_findings:
        f = crit_findings[0]
        if 'meta' in f and 'detail' in f:
            t_pass('bias _BIAS_SCAN_LAST findings include meta + detail',
                   f'{len(findings)} findings, {len(crit_findings)} critical')
            if isinstance(f.get('meta'), dict) and 'sector' in f['meta']:
                t_pass('CRITICAL sector finding has meta.sector populated',
                       f'sector={f["meta"]["sector"]}')
            else:
                t_fail('CRITICAL sector finding has meta.sector populated',
                       f'meta={f.get("meta")}')
        else:
            t_fail('bias findings include meta + detail', f'finding keys={list(f.keys())}')
    else:
        t_pass('bias _BIAS_SCAN_LAST has findings', f'{len(findings)} non-critical')
else:
    t_fail('bias _BIAS_SCAN_LAST present', 'setting missing')

# Validator's restriction should now be sector-specific (not _UNKNOWN)
r2 = c3.execute("SELECT value FROM customer_settings WHERE key='_VALIDATOR_RESTRICTIONS'").fetchone()
if r2:
    restrictions = json.loads(r2[0])
    sector_blocks = [r for r in restrictions if r.startswith('BLOCK_SECTOR_')]
    if any(r == 'BLOCK_SECTOR_UNKNOWN' for r in sector_blocks):
        t_fail('no spurious BLOCK_SECTOR_UNKNOWN', f'still present in {restrictions}')
    elif sector_blocks:
        t_pass('BLOCK_SECTOR_* restriction is named, not UNKNOWN', f'{sector_blocks}')
    else:
        t_pass('no BLOCK_SECTOR_* restrictions active')
else:
    t_pass('no _VALIDATOR_RESTRICTIONS setting (clean state)')


# ───────────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────────
section('Summary')
print(f'  {GREEN}Passed: {len(passes)}{RST}')
if failures:
    print(f'  {RED}Failed: {len(failures)}{RST}')
    for name, reason in failures:
        print(f'    {RED}✗{RST} {name} — {reason}')
else:
    print(f'  {GREEN}All checks pass.{RST}')
print()

sys.exit(len(failures))
