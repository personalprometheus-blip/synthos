#!/usr/bin/env python3
"""
validate_03b.py — Phase 03B Approval Queue Validation Script
Synthos

Run from the synthos project directory on the Pi:
    python3 validate_03b.py

Covers:
  1. File presence checks
  2. DB schema — pending_approvals table and columns
  3. DB method interface checks
  4. Legacy JSON detection and divergence analysis
  5. Approval lifecycle simulation (write → approve → execute → expire)
  6. Active queue state report
  7. Portal route smoke check (local only)

Output: copy and paste full output back for review.
"""

import os
import sys
import json
import sqlite3
import traceback
from datetime import datetime, timedelta

_TESTS_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(_TESTS_DIR)   # synthos_build/
CORE_DIR    = os.path.join(PROJECT_DIR, 'src')
AGENTS_DIR  = os.path.join(PROJECT_DIR, 'agents')
sys.path.insert(0, CORE_DIR)

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

def safe(fn, label):
    try:
        return fn()
    except Exception as e:
        p(label, False, f"{type(e).__name__}: {e}")
        return None


# ══════════════════════════════════════════════════════════
# 1. FILE PRESENCE
# ══════════════════════════════════════════════════════════
section("1. FILE PRESENCE")

required_core = [
    "database.py", "heartbeat.py",
    "portal.py", "health_check.py", "boot_sequence.py",
    "synthos_monitor.py",
]
required_agents = [
    "trade_logic_agent.py", "news_agent.py", "market_sentiment_agent.py",
]
for f in required_core:
    path = os.path.join(CORE_DIR, f)
    p(f"File exists: {f}", os.path.exists(path))
for f in required_agents:
    path = os.path.join(AGENTS_DIR, f)
    p(f"File exists: {f}", os.path.exists(path))

json_path = os.path.join(PROJECT_DIR, ".pending_approvals.json")
json_exists = os.path.exists(json_path)
p("No legacy .pending_approvals.json (clean)", not json_exists,
  "Not present — no migration needed" if not json_exists else "Present — will compare against DB state below")


# ══════════════════════════════════════════════════════════
# 2. DATABASE SCHEMA
# ══════════════════════════════════════════════════════════
section("2. DATABASE SCHEMA")

db_path = os.path.join(PROJECT_DIR, "signals.db")
p("signals.db exists", os.path.exists(db_path))

conn = None
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        p("DB connection", True)
    except Exception as e:
        p("DB connection", False, str(e))

if conn:
    # Table exists
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    p("pending_approvals table exists", "pending_approvals" in tables)

    if "pending_approvals" in tables:
        # Column check
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pending_approvals)"
        ).fetchall()}
        expected_cols = {
            "id", "ticker", "company", "sector", "politician",
            "confidence", "staleness", "headline", "price", "shares",
            "max_trade", "trail_amt", "trail_pct", "vol_label",
            "reasoning", "session", "status", "queued_at",
            "decided_at", "decided_by", "executed_at", "decision_note",
        }
        missing_cols = expected_cols - cols
        p("All expected columns present",
          len(missing_cols) == 0,
          f"Missing: {missing_cols}" if missing_cols else f"Columns: {sorted(cols)}")

        # Indexes
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        p("idx_approvals_status index", "idx_approvals_status" in indexes)
        p("idx_approvals_queued index",  "idx_approvals_queued" in indexes)

    # Required tables
    required_tables = [
        "portfolio", "positions", "ledger", "signals",
        "handshakes", "scan_log", "system_log", "outcomes",
        "urgent_flags", "pending_approvals",
    ]
    missing_tables = [t for t in required_tables if t not in tables]
    p("All required tables present",
      len(missing_tables) == 0,
      f"Missing: {missing_tables}" if missing_tables else f"{len(required_tables)} tables OK")


# ══════════════════════════════════════════════════════════
# 3. DB METHOD INTERFACE
# ══════════════════════════════════════════════════════════
section("3. DB METHOD INTERFACE")

db = None
try:
    from database import get_db, DB
    db = get_db()
    p("database module imports", True)
except Exception as e:
    p("database module imports", False, str(e))

if db:
    for method in [
        "queue_approval", "get_pending_approvals",
        "update_approval_status", "mark_approval_executed",
        "expire_stale_approvals",
    ]:
        p(f"DB method: {method}", hasattr(db, method))

    # Heartbeat module
    try:
        from heartbeat import write_heartbeat
        import inspect
        sig = inspect.signature(write_heartbeat)
        has_agent = "agent_name" in sig.parameters
        has_status = "status" in sig.parameters
        p("heartbeat.write_heartbeat signature",
          has_agent and has_status,
          f"params: {list(sig.parameters.keys())}")
    except Exception as e:
        p("heartbeat.write_heartbeat signature", False, str(e))


# ══════════════════════════════════════════════════════════
# 4. LEGACY JSON DETECTION & DIVERGENCE
# ══════════════════════════════════════════════════════════
section("4. LEGACY JSON DETECTION & DIVERGENCE")

json_items = []
if json_exists:
    try:
        with open(json_path) as f:
            json_items = json.load(f)
        print(f"{INFO} .pending_approvals.json has {len(json_items)} entries")
        for item in json_items:
            status = item.get("status", "?")
            ticker = item.get("ticker", "?")
            sig_id = item.get("id", "?")
            print(f"       JSON entry: id={sig_id} ticker={ticker} status={status}")
    except Exception as e:
        print(f"{WARN} Could not read JSON file: {e}")

    # Compare JSON entries against DB
    if db and json_items:
        pending_json = [i for i in json_items if i.get("status") == "PENDING_APPROVAL"]
        p("JSON has PENDING_APPROVAL items that need migration",
          len(pending_json) > 0,
          f"{len(pending_json)} items need importing to DB" if pending_json
          else "No pending items — JSON is historical only")

        if pending_json:
            print(f"\n{WARN} ACTION REQUIRED: {len(pending_json)} JSON item(s) not yet in DB.")
            print(f"       Run the migration block at the bottom of this script")
            print(f"       or queue them manually via the portal.")

        # Check if any JSON IDs already exist in DB
        if conn and "pending_approvals" in tables:
            for item in json_items:
                sig_id = str(item.get("id", ""))
                row = conn.execute(
                    "SELECT id, status FROM pending_approvals WHERE id=?",
                    (sig_id,)
                ).fetchone()
                if row:
                    print(f"{INFO} id={sig_id}: in DB as status={row['status']}")
                else:
                    print(f"{WARN} id={sig_id}: in JSON but NOT in DB (status={item.get('status')})")
else:
    p("No legacy JSON file present", True, "No migration needed")


# ══════════════════════════════════════════════════════════
# 5. APPROVAL LIFECYCLE SIMULATION
# ══════════════════════════════════════════════════════════
section("5. APPROVAL LIFECYCLE SIMULATION (write-only test IDs)")

TEST_ID = "_validate_03b_test_001"

if db:
    # Clean up any prior test run
    if conn:
        conn.execute("DELETE FROM pending_approvals WHERE id LIKE '_validate_03b%'")
        conn.commit()

    # 5a. Queue
    try:
        db.queue_approval(
            signal_id  = TEST_ID,
            ticker     = "TEST",
            company    = "Validation Corp",
            confidence = "HIGH",
            staleness  = "Fresh",
            reasoning  = "Phase 03B validation test entry",
            session    = "open",
        )
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (TEST_ID,)
        ).fetchone()
        p("queue_approval writes row",
          row is not None and row["status"] == "PENDING_APPROVAL",
          f"status={row['status'] if row else 'NOT FOUND'}")
    except Exception as e:
        p("queue_approval writes row", False, traceback.format_exc(limit=2))
        row = None

    # 5b. get_pending_approvals filter
    try:
        pending = db.get_pending_approvals(status_filter=["PENDING_APPROVAL"])
        test_in_pending = any(str(r.get("id")) == TEST_ID for r in pending)
        p("get_pending_approvals returns test row", test_in_pending,
          f"{len(pending)} PENDING_APPROVAL rows total")
    except Exception as e:
        p("get_pending_approvals filter", False, str(e))

    # 5c. Approve
    try:
        updated = db.update_approval_status(TEST_ID, "APPROVED", decided_by="validator")
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (TEST_ID,)
        ).fetchone()
        p("update_approval_status → APPROVED",
          updated and row and row["status"] == "APPROVED",
          f"decided_by={row['decided_by'] if row else '?'} decided_at={row['decided_at'] if row else '?'}")
    except Exception as e:
        p("update_approval_status → APPROVED", False, str(e))

    # 5d. Mark executed
    try:
        db.mark_approval_executed(TEST_ID)
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (TEST_ID,)
        ).fetchone()
        p("mark_approval_executed → EXECUTED",
          row and row["status"] == "EXECUTED",
          f"executed_at={row['executed_at'] if row else '?'}")
        p("Executed row still exists (not deleted)",
          row is not None)
    except Exception as e:
        p("mark_approval_executed → EXECUTED", False, str(e))

    # 5e. Reject path (separate test row)
    REJECT_ID = "_validate_03b_test_002"
    try:
        db.queue_approval(signal_id=REJECT_ID, ticker="TREJ", confidence="LOW",
                          reasoning="reject path test")
        db.update_approval_status(REJECT_ID, "REJECTED", decided_by="validator")
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (REJECT_ID,)
        ).fetchone()
        p("update_approval_status → REJECTED",
          row and row["status"] == "REJECTED",
          f"row preserved: {row is not None}")
    except Exception as e:
        p("Reject path", False, str(e))

    # 5f. Expiry path
    EXPIRE_ID = "_validate_03b_test_003"
    try:
        db.queue_approval(signal_id=EXPIRE_ID, ticker="TEXP",
                          confidence="MEDIUM", reasoning="expiry test")
        # Back-date queued_at to 49 hours ago
        old_time = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE pending_approvals SET queued_at=? WHERE id=?",
            (old_time, EXPIRE_ID)
        )
        conn.commit()
        expired_count = db.expire_stale_approvals(max_age_hours=48)
        row = conn.execute(
            "SELECT * FROM pending_approvals WHERE id=?", (EXPIRE_ID,)
        ).fetchone()
        p("expire_stale_approvals → EXPIRED",
          row and row["status"] == "EXPIRED",
          f"expired_count={expired_count} status={row['status'] if row else '?'}")
        p("Expired row still exists (not deleted)",
          row is not None)
    except Exception as e:
        p("Expiry path", False, str(e))

    # 5g. Active queue excludes non-actionable statuses
    try:
        active = db.get_pending_approvals(status_filter=["PENDING_APPROVAL"])
        executed_in_active = any(
            str(r.get("id")) == TEST_ID for r in active
        )
        rejected_in_active = any(
            str(r.get("id")) == REJECT_ID for r in active
        )
        expired_in_active = any(
            str(r.get("id")) == EXPIRE_ID for r in active
        )
        p("Active queue excludes EXECUTED rows", not executed_in_active)
        p("Active queue excludes REJECTED rows", not rejected_in_active)
        p("Active queue excludes EXPIRED rows",  not expired_in_active)
    except Exception as e:
        p("Active queue filter", False, str(e))

    # 5h. Duplicate/re-queue safety
    try:
        DUPE_ID = "_validate_03b_test_004"
        db.queue_approval(signal_id=DUPE_ID, ticker="TDUPE",
                          confidence="HIGH", reasoning="first queue")
        db.queue_approval(signal_id=DUPE_ID, ticker="TDUPE",
                          confidence="HIGH", reasoning="second queue — should replace")
        rows = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE id=?", (DUPE_ID,)
        ).fetchone()[0]
        p("Duplicate queue_approval does not create two rows", rows == 1,
          f"row count: {rows}")
    except Exception as e:
        p("Duplicate queue safety", False, str(e))

    # 5i. Already-decided row cannot be re-approved
    try:
        result = db.update_approval_status(REJECT_ID, "APPROVED", decided_by="validator")
        row = conn.execute(
            "SELECT status FROM pending_approvals WHERE id=?", (REJECT_ID,)
        ).fetchone()
        p("Already-REJECTED row cannot be re-approved",
          not result and row and row["status"] == "REJECTED",
          f"update returned {result}, status={row['status'] if row else '?'}")
    except Exception as e:
        p("Already-decided row protection", False, str(e))

    # Cleanup test rows
    conn.execute("DELETE FROM pending_approvals WHERE id LIKE '_validate_03b%'")
    conn.commit()
    print(f"\n{INFO} Test rows cleaned up")


# ══════════════════════════════════════════════════════════
# 6. ACTIVE QUEUE STATE REPORT
# ══════════════════════════════════════════════════════════
section("6. ACTIVE QUEUE STATE (live DB)")

if conn and "pending_approvals" in tables:
    all_rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM pending_approvals GROUP BY status"
    ).fetchall()
    if all_rows:
        print(f"{INFO} pending_approvals row counts by status:")
        for r in all_rows:
            print(f"       {r['status']:<20} {r['cnt']}")
    else:
        print(f"{INFO} pending_approvals table is empty (expected on fresh system)")

    pending_rows = conn.execute(
        "SELECT id, ticker, confidence, queued_at FROM pending_approvals "
        "WHERE status='PENDING_APPROVAL' ORDER BY queued_at ASC"
    ).fetchall()
    if pending_rows:
        print(f"\n{INFO} Current PENDING_APPROVAL entries ({len(pending_rows)}):")
        for r in pending_rows:
            print(f"       id={r['id']} ticker={r['ticker']} "
                  f"conf={r['confidence']} queued={r['queued_at']}")
    else:
        print(f"\n{INFO} No PENDING_APPROVAL entries in DB currently")


# ══════════════════════════════════════════════════════════
# 7. CODE PATH VERIFICATION (no JSON in active paths)
# ══════════════════════════════════════════════════════════
section("7. CODE PATH — NO JSON IN ACTIVE WORKFLOW")

for fname, checks in {
    "trade_logic_agent.py": [
        ("No PENDING_APPROVALS_FILE constant",
         lambda s: "PENDING_APPROVALS_FILE" not in s),
        ("No save_pending_approvals function",
         lambda s: "def save_pending_approvals" not in s),
        ("No load_pending_approvals function",
         lambda s: "def load_pending_approvals" not in s),
        ("Uses DB queue_approval",
         lambda s: "queue_approval" in s),
    ],
    "portal.py": [
        ("No save_pending_approvals function",
         lambda s: "def save_pending_approvals" not in s),
        ("No .pending_approvals.json path constant",
         lambda s: ".pending_approvals.json" not in s),
        ("Uses update_approval_status",
         lambda s: "update_approval_status" in s),
    ],
}.items():
    _agent_files = {'trade_logic_agent.py', 'news_agent.py', 'market_sentiment_agent.py'}
    fpath = os.path.join(AGENTS_DIR if fname in _agent_files else CORE_DIR, fname)
    if os.path.exists(fpath):
        src = open(fpath).read()
        for label, check_fn in checks:
            p(f"{fname}: {label}", check_fn(src))
    else:
        p(f"{fname}: readable", False, "File not found")


# ══════════════════════════════════════════════════════════
# 8. PORTAL ROUTE SMOKE CHECK
# ══════════════════════════════════════════════════════════
section("8. PORTAL ROUTE SMOKE CHECK (local)")

try:
    import requests as _req
    portal_url = "http://localhost:5001"
    r = _req.get(f"{portal_url}/api/approvals", timeout=4)
    if r.status_code == 200:
        data = r.json()
        p("/api/approvals returns 200", True,
          f"returned {len(data)} rows")
    elif r.status_code == 302:
        p("/api/approvals returns 200", False,
          "Got 302 redirect — portal requires login (PORTAL_PASSWORD set)")
    else:
        p("/api/approvals returns 200", False,
          f"Status {r.status_code}: {r.text[:80]}")
except Exception as e:
    p("/api/approvals reachable", False,
      f"Portal not running or unreachable: {e}")


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

print()
if failed == 0:
    print("  ✓ PHASE 03B VALIDATION PASSED — safe to proceed")
elif failed <= 2:
    print("  ⚠ MINOR ISSUES — review failed checks before proceeding")
else:
    print("  ✗ VALIDATION FAILED — do not proceed until failures resolved")
print()

if conn:
    conn.close()
