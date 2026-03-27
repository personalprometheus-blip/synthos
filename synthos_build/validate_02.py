#!/usr/bin/env python3
"""
validate_02.py — Phase 02 System Exposure Layer Validation
Synthos

Run from the synthos project directory on the Pi:
    python3 validate_02.py

Covers four portal surfaces:
  1. Status Tab     — live agent activity, portfolio, kill switch, heartbeat
  2. Research Tab   — Scout signals (agent2), watchlist, confidence distribution
  3. Market Context — Pulse scans (agent3), scan_log, urgent flags
  4. Activity Tab   — signal/trade/outcome timeline, system_log events

Each section tests both the DB layer directly AND the portal API endpoint.
Portal must be running on localhost:5001 for API checks.
"""

import os
import sys
import json
import sqlite3
import traceback
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [WARN]"
INFO = "  [INFO]"
SEP  = "-" * 60

results = []

def p(label, ok, detail=""):
    icon = PASS if ok else FAIL
    line = f"{icon} {label}"
    if detail:
        line += f"\n         {detail}"
    print(line)
    results.append((label, ok, detail))
    return ok

def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def api_get(path, timeout=6):
    """GET a portal API endpoint. Returns (ok, status_code, data, error)."""
    try:
        import requests as _req
        r = _req.get(f"http://localhost:5001{path}", timeout=timeout)
        if r.status_code == 200:
            return True, 200, r.json(), None
        elif r.status_code == 302:
            return False, 302, None, "Redirect — portal requires login (PORTAL_PASSWORD set)"
        else:
            return False, r.status_code, None, r.text[:100]
    except Exception as e:
        return False, 0, None, str(e)


# ══════════════════════════════════════════════════════════
# SETUP — DB CONNECTION
# ══════════════════════════════════════════════════════════
section("SETUP")

db = None
conn = None

try:
    from database import get_db
    db = get_db()
    p("database module loads", True)
except Exception as e:
    p("database module loads", False, str(e))

db_path = os.path.join(PROJECT_DIR, "signals.db")
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        p("Direct DB connection", True)
    except Exception as e:
        p("Direct DB connection", False, str(e))
else:
    p("signals.db exists", False, "File not found")

# Portal reachability
ok, code, data, err = api_get("/api/status")
portal_up = ok
p("Portal reachable at localhost:5001",
  portal_up or code == 302,
  f"HTTP {code}" if not ok else "responding")

if code == 302:
    print(f"\n{WARN} Portal has PORTAL_PASSWORD set.")
    print(f"       API checks will report 302 redirects.")
    print(f"       To enable API validation: temporarily unset PORTAL_PASSWORD in .env")
    print(f"       and restart portal, OR verify manually in browser.")


# ══════════════════════════════════════════════════════════
# 1. STATUS TAB
# ══════════════════════════════════════════════════════════
section("1. STATUS TAB — Agent Activity, Portfolio, Kill Switch")

# 1a. Portfolio state
if db:
    try:
        portfolio = db.get_portfolio()
        cash = portfolio.get('cash', 0)
        gains = portfolio.get('realized_gains', 0)
        p("Portfolio row exists", portfolio is not None,
          f"cash=${cash:.2f} realized_gains=${gains:.2f}")
    except Exception as e:
        p("Portfolio row readable", False, str(e))

# 1b. Open positions
if db:
    try:
        positions = db.get_open_positions()
        p("get_open_positions returns", True,
          f"{len(positions)} open position(s)")
        if positions:
            for pos in positions[:3]:
                print(f"       {pos['ticker']} {pos['shares']:.4f} shares "
                      f"@ ${pos['entry_price']:.2f} status={pos['status']}")
    except Exception as e:
        p("get_open_positions", False, str(e))

# 1c. Last heartbeat per agent
if db:
    for agent in ['agent1_trader', 'agent2_research', 'agent3_sentiment']:
        try:
            hb = db.get_last_heartbeat(agent)
            if hb:
                age_mins = round(
                    (datetime.now() -
                     datetime.strptime(hb['timestamp'][:19], '%Y-%m-%d %H:%M:%S')
                    ).total_seconds() / 60
                )
                p(f"Heartbeat: {agent}",
                  True,
                  f"last={hb['timestamp']} ({age_mins}m ago) status={hb['details']}")
            else:
                p(f"Heartbeat: {agent}", False,
                  "No heartbeat recorded — agent has not run yet")
        except Exception as e:
            p(f"Heartbeat: {agent}", False, str(e))

# 1d. Kill switch state
kill_path = os.path.join(PROJECT_DIR, '.kill_switch')
kill_active = os.path.exists(kill_path)
p("Kill switch state readable", True,
  f"{'ACTIVE ⛔' if kill_active else 'clear ✓'}")

# 1e. Agent lock state
lock_path = os.path.join(PROJECT_DIR, '.agent_lock')
if os.path.exists(lock_path):
    try:
        parts = open(lock_path).read().strip().split('\n')
        agent = parts[0] if parts else 'unknown'
        lock_time = float(parts[2]) if len(parts) > 2 else 0
        import time as _t
        age = int(_t.time() - lock_time)
        p("Agent lock file", True,
          f"{agent} has held lock for {age}s {'(STALE — >10min)' if age > 600 else ''}")
    except Exception as e:
        p("Agent lock file", False, str(e))
else:
    p("Agent lock state", True, "No lock held — no agent currently running")

# 1f. Portfolio history
if db:
    try:
        history = db.get_portfolio_history(days=30)
        p("Portfolio history readable", True,
          f"{len(history)} data point(s) in last 30 days")
    except Exception as e:
        p("Portfolio history", False, str(e))

# 1g. API: /api/status
if portal_up:
    ok, code, data, err = api_get("/api/status")
    if ok and data:
        p("/api/status returns valid shape",
          all(k in data for k in ['portfolio_value', 'cash', 'open_positions',
                                   'kill_switch', 'operating_mode']),
          f"portfolio=${data.get('portfolio_value',0):.2f} "
          f"positions={data.get('open_positions',0)} "
          f"mode={data.get('operating_mode','?')} "
          f"kill={data.get('kill_switch',False)}")
    else:
        p("/api/status", False, f"HTTP {code}: {err}")

# 1h. API: /api/system-health
if portal_up:
    ok, code, data, err = api_get("/api/system-health")
    if ok and data:
        monitor_status = data.get('monitor', {}).get('status', '?')
        claude_status  = data.get('claude_api', {}).get('status', '?')
        uptime         = data.get('uptime', '?')
        p("/api/system-health returns valid shape", True,
          f"monitor={monitor_status} claude={claude_status} uptime={uptime}")
    else:
        p("/api/system-health", False, f"HTTP {code}: {err}")


# ══════════════════════════════════════════════════════════
# 2. RESEARCH TAB — Scout Signals (agent2_research)
# ══════════════════════════════════════════════════════════
section("2. RESEARCH TAB — Scout Signals (agent2_research)")

# 2a. Signals table row count
if conn:
    try:
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        p("signals table has rows", total > 0,
          f"{total} total signal(s) in DB")
    except Exception as e:
        p("signals table readable", False, str(e))

# 2b. Signal status distribution
if conn:
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM signals GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
        print(f"{INFO} Signal status distribution:")
        for r in rows:
            print(f"       {r['status']:<20} {r['cnt']}")
    except Exception as e:
        print(f"{WARN} Could not read signal distribution: {e}")

# 2c. Confidence distribution
if conn:
    try:
        rows = conn.execute(
            "SELECT confidence, COUNT(*) as cnt FROM signals GROUP BY confidence ORDER BY cnt DESC"
        ).fetchall()
        print(f"{INFO} Signal confidence distribution:")
        for r in rows:
            print(f"       {r['confidence']:<10} {r['cnt']}")
    except Exception as e:
        print(f"{WARN} Could not read confidence distribution: {e}")

# 2d. Most recent signals
if conn:
    try:
        recent = conn.execute("""
            SELECT ticker, politician, confidence, staleness, status, created_at
            FROM signals
            ORDER BY created_at DESC LIMIT 5
        """).fetchall()
        p("Recent signals readable", len(recent) > 0,
          f"{len(recent)} most recent signal(s):")
        for r in recent:
            print(f"       {r['ticker']:<6} {r['confidence']:<8} "
                  f"{r['staleness']:<10} {r['status']:<15} {r['created_at'][:16]}")
    except Exception as e:
        p("Recent signals", False, str(e))

# 2e. WATCHING signals (portal watchlist)
if db:
    try:
        watching = db.get_watching_signals(limit=10)
        p("get_watching_signals returns", True,
          f"{len(watching)} signal(s) in watchlist")
        if watching:
            for s in watching[:3]:
                print(f"       {s['ticker']:<6} {s['confidence']:<8} "
                      f"{s['staleness']:<10} {s.get('headline','')[:50]}")
    except Exception as e:
        p("get_watching_signals", False, str(e))

# 2f. Last agent2 run
if conn:
    try:
        last_run = conn.execute("""
            SELECT timestamp, details FROM system_log
            WHERE agent='agent2_research' OR agent='The Daily'
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        p("agent2_research has run",
          last_run is not None,
          f"last={last_run['timestamp'][:16]} details={last_run['details'][:60]}"
          if last_run else "No record of agent2 run in system_log")
    except Exception as e:
        p("agent2 last run", False, str(e))

# 2g. API: /api/watchlist
if portal_up:
    ok, code, data, err = api_get("/api/watchlist")
    if ok and data:
        signals = data.get('signals', [])
        p("/api/watchlist returns valid shape", True,
          f"{len(signals)} signal(s) in portal watchlist")
    else:
        p("/api/watchlist", False, f"HTTP {code}: {err}")


# ══════════════════════════════════════════════════════════
# 3. MARKET CONTEXT — Pulse Scans (agent3_sentiment)
# ══════════════════════════════════════════════════════════
section("3. MARKET CONTEXT — Pulse Scans (agent3_sentiment)")

# 3a. scan_log row count
if conn:
    try:
        total = conn.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
        p("scan_log has rows", total > 0,
          f"{total} total scan(s) recorded")
    except Exception as e:
        p("scan_log readable", False, str(e))

# 3b. Recent scans
if conn:
    try:
        scans = conn.execute("""
            SELECT ticker, tier, cascade_detected, event_summary, scanned_at
            FROM scan_log
            ORDER BY scanned_at DESC LIMIT 5
        """).fetchall()
        p("Recent scans readable", len(scans) > 0,
          f"{len(scans)} most recent scan(s):")
        for s in scans:
            cascade = "CASCADE ⚠" if s['cascade_detected'] else "clean"
            print(f"       {s['ticker']:<6} T{s['tier']} {cascade:<12} {s['scanned_at'][:16]}")
            if s['event_summary']:
                print(f"              {s['event_summary'][:70]}")
    except Exception as e:
        p("Recent scans", False, str(e))

# 3c. Tier distribution
if conn:
    try:
        rows = conn.execute("""
            SELECT tier, COUNT(*) as cnt FROM scan_log GROUP BY tier ORDER BY tier
        """).fetchall()
        if rows:
            print(f"{INFO} Scan tier distribution (1=Critical, 4=Quiet):")
            tier_labels = {1: "Critical", 2: "Elevated", 3: "Neutral", 4: "Quiet"}
            for r in rows:
                print(f"       Tier {r['tier']} ({tier_labels.get(r['tier'],'?'):<10}) {r['cnt']}")
    except Exception as e:
        print(f"{WARN} Could not read scan tier distribution: {e}")

# 3d. Urgent flags
if db:
    try:
        flags = db.get_urgent_flags()
        p("get_urgent_flags returns", True,
          f"{len(flags)} active urgent flag(s)")
        if flags:
            for f in flags:
                print(f"       {f['ticker']:<6} T{f['tier']} detected={f['detected_at'][:16]}")
    except Exception as e:
        p("get_urgent_flags", False, str(e))

# 3e. Cascade events
if conn:
    try:
        cascades = conn.execute("""
            SELECT ticker, tier, event_summary, scanned_at
            FROM scan_log WHERE cascade_detected=1
            ORDER BY scanned_at DESC LIMIT 5
        """).fetchall()
        p("Cascade events readable", True,
          f"{len(cascades)} cascade event(s) in history")
        for c in cascades[:3]:
            print(f"       {c['ticker']:<6} T{c['tier']} {c['scanned_at'][:16]}")
    except Exception as e:
        p("Cascade events", False, str(e))

# 3f. Last agent3 run
if conn:
    try:
        last_run = conn.execute("""
            SELECT timestamp, details FROM system_log
            WHERE agent='agent3_sentiment' OR agent='The Pulse'
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        p("agent3_sentiment has run",
          last_run is not None,
          f"last={last_run['timestamp'][:16]} details={last_run['details'][:60]}"
          if last_run else "No record of agent3 run in system_log")
    except Exception as e:
        p("agent3 last run", False, str(e))


# ══════════════════════════════════════════════════════════
# 4. ACTIVITY TAB — Signal/Trade/Outcome Timeline
# ══════════════════════════════════════════════════════════
section("4. ACTIVITY TAB — Signal / Trade / Outcome Timeline")

# 4a. system_log event distribution
if conn:
    try:
        events = conn.execute("""
            SELECT event, COUNT(*) as cnt FROM system_log
            GROUP BY event ORDER BY cnt DESC LIMIT 12
        """).fetchall()
        p("system_log has events", len(events) > 0,
          f"{sum(r['cnt'] for r in events)} total events across {len(events)} event types")
        print(f"{INFO} Top system_log events:")
        for r in events:
            print(f"       {r['event']:<30} {r['cnt']}")
    except Exception as e:
        p("system_log readable", False, str(e))

# 4b. Recent system events (last 10)
if conn:
    try:
        recent = conn.execute("""
            SELECT timestamp, event, agent, details FROM system_log
            ORDER BY timestamp DESC LIMIT 10
        """).fetchall()
        p("Recent system events readable", len(recent) > 0,
          f"{len(recent)} most recent event(s):")
        for r in recent:
            detail = (r['details'] or '')[:50]
            print(f"       {r['timestamp'][:16]}  {r['event']:<25} "
                  f"{(r['agent'] or ''):<20} {detail}")
    except Exception as e:
        p("Recent system events", False, str(e))

# 4c. Outcomes / closed trades
if db:
    try:
        outcomes = db.get_recent_outcomes(limit=10)
        p("get_recent_outcomes returns", True,
          f"{len(outcomes)} closed trade outcome(s)")
        if outcomes:
            wins   = sum(1 for o in outcomes if o.get('verdict') == 'WIN')
            losses = sum(1 for o in outcomes if o.get('verdict') == 'LOSS')
            total_pnl = sum(o.get('pnl_dollar', 0) for o in outcomes)
            print(f"       Wins: {wins}  Losses: {losses}  "
                  f"Total P&L: ${total_pnl:+.2f}")
            for o in outcomes[:3]:
                print(f"       {o['ticker']:<6} {o.get('verdict','?'):<5} "
                      f"${o.get('pnl_dollar',0):+.2f} "
                      f"{o.get('exit_reason','?'):<20} {o.get('created_at','')[:16]}")
    except Exception as e:
        p("get_recent_outcomes", False, str(e))

# 4d. ACTED_ON signals (signals that led to trades)
if conn:
    try:
        acted = conn.execute("""
            SELECT ticker, confidence, staleness, created_at, updated_at
            FROM signals WHERE status='ACTED_ON'
            ORDER BY updated_at DESC LIMIT 5
        """).fetchall()
        p("ACTED_ON signals readable", True,
          f"{len(acted)} signal(s) with ACTED_ON status")
        for s in acted:
            print(f"       {s['ticker']:<6} {s['confidence']:<8} {s['staleness']:<10} "
                  f"acted={s['updated_at'][:16]}")
    except Exception as e:
        p("ACTED_ON signals", False, str(e))

# 4e. Handshakes (agent2 → agent1 signal passing)
if conn:
    try:
        total_hs = conn.execute("SELECT COUNT(*) FROM handshakes").fetchone()[0]
        acked    = conn.execute(
            "SELECT COUNT(*) FROM handshakes WHERE ack=1"
        ).fetchone()[0]
        p("Handshakes table readable", True,
          f"{total_hs} total, {acked} acknowledged")
    except Exception as e:
        p("Handshakes readable", False, str(e))

# 4f. API: /api/portfolio-history
if portal_up:
    ok, code, data, err = api_get("/api/portfolio-history?days=30")
    if ok and data:
        hist = data.get('history', [])
        p("/api/portfolio-history returns valid shape", True,
          f"{len(hist)} data point(s) for last 30 days")
        if hist:
            print(f"       First: {hist[0].get('date','?')} "
                  f"${hist[0].get('value',0):.2f}  →  "
                  f"Last: {hist[-1].get('date','?')} "
                  f"${hist[-1].get('value',0):.2f}")
    else:
        p("/api/portfolio-history", False, f"HTTP {code}: {err}")


# ══════════════════════════════════════════════════════════
# 5. CROSS-SURFACE COHERENCE
# ══════════════════════════════════════════════════════════
section("5. CROSS-SURFACE COHERENCE")

# 5a. Signals in DB match portal watchlist count
if db and portal_up:
    try:
        db_watching = db.get_watching_signals(limit=100)
        ok, code, api_data, err = api_get("/api/watchlist")
        if ok:
            api_watching = api_data.get('signals', [])
            p("DB watchlist count matches API",
              len(db_watching) == len(api_watching),
              f"DB={len(db_watching)} API={len(api_watching)}")
        else:
            print(f"{INFO} Skipping watchlist coherence check — portal unavailable")
    except Exception as e:
        p("Watchlist DB/API coherence", False, str(e))

# 5b. Portfolio value coherence: DB vs API
if db and portal_up:
    try:
        portfolio = db.get_portfolio()
        positions = db.get_open_positions()
        db_total  = round(
            portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions), 2
        )
        ok, code, api_data, err = api_get("/api/status")
        if ok:
            api_total = api_data.get('portfolio_value', None)
            match = api_total is not None and abs(db_total - api_total) < 1.0
            p("Portfolio value: DB matches API",
              match,
              f"DB=${db_total:.2f} API=${api_total:.2f if api_total else '?'}")
        else:
            print(f"{INFO} Skipping portfolio coherence check — portal unavailable")
    except Exception as e:
        p("Portfolio DB/API coherence", False, str(e))

# 5c. Pending approvals: DB matches API
if db and portal_up:
    try:
        db_pending = db.get_pending_approvals(status_filter=['PENDING_APPROVAL'])
        ok, code, api_data, err = api_get("/api/approvals")
        if ok:
            api_pending = [r for r in api_data if r.get('status') == 'PENDING_APPROVAL']
            p("Pending approvals: DB matches API",
              len(db_pending) == len(api_pending),
              f"DB={len(db_pending)} API={len(api_pending)}")
    except Exception as e:
        p("Pending approvals coherence", False, str(e))

# 5d. Urgent flags: DB matches portal status
if db and portal_up:
    try:
        db_flags = db.get_urgent_flags()
        ok, code, api_data, err = api_get("/api/status")
        if ok:
            api_flags = api_data.get('urgent_flags', None)
            p("Urgent flags: DB matches API",
              api_flags is not None and api_flags == len(db_flags),
              f"DB={len(db_flags)} API={api_flags}")
    except Exception as e:
        p("Urgent flags coherence", False, str(e))


# ══════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════
section("SUMMARY")

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total  = len(results)

print(f"\n  Passed: {passed}/{total}")

if failed:
    print(f"  Failed: {failed}/{total}")
    print(f"\n  Failed checks:")
    for label, ok, detail in results:
        if not ok:
            print(f"    {FAIL} {label}")
            if detail:
                print(f"           {detail}")

# Surface-level summary
surfaces = {
    "Status Tab":       [l for l,o,_ in results if any(x in l for x in
                         ['Portfolio','Heartbeat','Kill switch','Agent lock',
                          '/api/status','/api/system-health','history'])],
    "Research Tab":     [l for l,o,_ in results if any(x in l for x in
                         ['signals table','Recent signals','watching','agent2',
                          '/api/watchlist','confidence','Signal status'])],
    "Market Context":   [l for l,o,_ in results if any(x in l for x in
                         ['scan_log','Scan','Cascade','urgent','agent3','Pulse'])],
    "Activity Tab":     [l for l,o,_ in results if any(x in l for x in
                         ['system_log','outcomes','ACTED_ON','Handshake',
                          '/api/portfolio-history','timeline','event'])],
}

print(f"\n  Surface pass rates:")
for surface, labels in surfaces.items():
    surface_results = [(l,o) for l,o,_ in results if l in labels]
    if surface_results:
        sp = sum(1 for _,o in surface_results if o)
        st = len(surface_results)
        icon = "✓" if sp == st else "⚠" if sp > st // 2 else "✗"
        print(f"    {icon} {surface:<20} {sp}/{st}")

print()
if failed == 0:
    print("  ✓ PHASE 02 VALIDATION PASSED — all surfaces reporting correctly")
elif failed <= 3:
    print("  ⚠ MINOR ISSUES — review failed checks, likely data gaps not code bugs")
else:
    print("  ✗ VALIDATION ISSUES — review failures before marking Phase 02 complete")
print()

if conn:
    conn.close()
