#!/usr/bin/env python3
"""
customer_activity_report.py — Cross-customer trading activity report
====================================================================

V1 of the operator-facing report tool described in the 2026-04-28
session.  Answers: "what is the bot doing for each customer, how is
it doing it, is something wrong?"

Data sources (STORED only — no live Alpaca calls)
--------------------------------------------------
  * positions       (open + closed trades)
  * outcomes        (exit-reason histograms)
  * signals         (shared DB — what was evaluated this period)
  * signal_decisions (shared DB — gate-by-gate verdicts)
  * scan_log        (per-customer agent run telemetry)
  * system_log      (heartbeat with portfolio_value snapshots)

Inputs
------
  start_date:  YYYY-MM-DD (inclusive)
  end_date:    YYYY-MM-DD (inclusive)
  customers:   list of customer IDs, OR ["all"], OR ["owner"]
  options:     {"include_aggregate": bool}

Output
------
A structured dict with three top-level keys:
  customers   — list, one entry per customer in scope
  aggregate   — fleet-level rollups (only if multi/all)
  meta        — generated_at, range, customer_count

The render layer (CLI text or HTML page) consumes this dict; the
report engine itself produces no UI strings.

Section coverage (V1)
---------------------
A. Activity      — trades opened/closed, $ volume, net P&L, equity start/end
C. Positions     — closed trades table, top winner/loser, sectors traded
E. Fleet behavior — most-traded tickers, most-traded sectors, signal clusters

Deferred to V2 (B/D/F)
----------------------
B. Decision flow      — signal source mix, gate verdict histogram
D. Operational health — trader fire count vs expected, gate CAUTION/NO_GO
F. Concerning patterns — concentration, loss streaks, idle customers

Usage (CLI)
-----------
  python3 tools/customer_activity_report.py --start 2026-04-23 --end 2026-04-28
  python3 tools/customer_activity_report.py --start 2026-04-28 --end 2026-04-28 \\
                                            --customer 30eff008
  python3 tools/customer_activity_report.py --start 2026-04-21 --end 2026-04-28 \\
                                            --customer all --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Paths ────────────────────────────────────────────────────────────────
_DIR    = os.path.dirname(os.path.abspath(__file__))
_BUILD  = os.path.dirname(_DIR)
_DATA   = os.path.join(_BUILD, "data")
_USER   = os.path.join(_BUILD, "user")
CUSTOMERS_DIR = os.path.join(_DATA, "customers")
SHARED_DB     = os.path.join(_USER, "signals.db")


# ── Helpers ──────────────────────────────────────────────────────────────

def _customer_db_path(cid: str) -> str:
    return os.path.join(CUSTOMERS_DIR, cid, "signals.db")


def _list_all_customer_ids() -> list[str]:
    out: list[str] = []
    for p in sorted(glob.glob(os.path.join(CUSTOMERS_DIR, "*", "signals.db"))):
        cid = os.path.basename(os.path.dirname(p))
        if cid != "default":
            out.append(cid)
    return out


def _resolve_scope(customers: list[str]) -> list[str]:
    """Expand 'all' / 'owner' aliases into concrete customer IDs."""
    if not customers:
        return []
    out: list[str] = []
    for c in customers:
        if c == "all":
            out.extend(_list_all_customer_ids())
        elif c == "owner":
            owner = os.environ.get("OWNER_CUSTOMER_ID", "").strip()
            if owner:
                out.append(owner)
        else:
            out.append(c)
    # Dedup preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _customer_display_name(cid: str) -> str:
    """Try auth.db lookup; fall back to short cid8."""
    try:
        auth_db = os.path.join(_USER, "auth.db")
        if os.path.exists(auth_db):
            db = sqlite3.connect(auth_db)
            row = db.execute(
                "SELECT display_name, email FROM customers WHERE id=? LIMIT 1",
                (cid,)
            ).fetchone()
            db.close()
            if row:
                return row[0] or row[1] or cid[:8]
    except Exception:
        pass
    return cid[:8]


def _connect(path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _safe_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Handle 'Z' suffix and missing T
        s2 = s.strip().replace("Z", "").replace(" ", "T")[:26]
        return datetime.fromisoformat(s2)
    except Exception:
        return None


# ── Per-customer report ──────────────────────────────────────────────────

def _build_customer_report(cid: str, start: str, end: str) -> dict:
    """Generate one customer's report block.  Returns {} on missing DB."""
    path = _customer_db_path(cid)
    db = _connect(path)
    if db is None:
        return {}

    name = _customer_display_name(cid)

    # Pull-once (range-bounded) helpers
    range_lo = f"{start} 00:00:00"
    range_hi = f"{end} 23:59:59"

    # ── Section A: Activity ──────────────────────────────────────────────
    # Trades opened in range
    opened = db.execute(
        """SELECT id, ticker, sector, entry_price, shares, opened_at, status,
                  trail_stop_pct, vol_bucket
           FROM positions
           WHERE opened_at BETWEEN ? AND ?
           ORDER BY opened_at""",
        (range_lo, range_hi)
    ).fetchall()
    opened_count    = len(opened)
    opened_volume   = sum((r["entry_price"] or 0) * (r["shares"] or 0) for r in opened)

    # Trades closed in range
    closed = db.execute(
        """SELECT id, ticker, sector, entry_price, current_price, shares, pnl,
                  opened_at, closed_at, exit_reason, vol_bucket
           FROM positions
           WHERE status='CLOSED' AND closed_at BETWEEN ? AND ?
           ORDER BY closed_at""",
        (range_lo, range_hi)
    ).fetchall()
    closed_count    = len(closed)
    closed_volume   = sum((r["current_price"] or 0) * (r["shares"] or 0) for r in closed)
    realized_pnl    = sum((r["pnl"] or 0) for r in closed)
    wins            = sum(1 for r in closed if (r["pnl"] or 0) > 0)
    losses          = sum(1 for r in closed if (r["pnl"] or 0) < 0)

    # Equity bookmarks (portfolio_value at start and end of range)
    equity_at_start = db.execute(
        """SELECT portfolio_value FROM system_log
           WHERE portfolio_value IS NOT NULL AND portfolio_value > 0
             AND timestamp >= ?
           ORDER BY timestamp ASC LIMIT 1""", (range_lo,)
    ).fetchone()
    equity_at_end = db.execute(
        """SELECT portfolio_value FROM system_log
           WHERE portfolio_value IS NOT NULL AND portfolio_value > 0
             AND timestamp <= ?
           ORDER BY timestamp DESC LIMIT 1""", (range_hi,)
    ).fetchone()
    eq_start = float(equity_at_start[0]) if equity_at_start else None
    eq_end   = float(equity_at_end[0])   if equity_at_end   else None
    equity_change = (eq_end - eq_start) if (eq_start is not None and eq_end is not None) else None
    equity_change_pct = (equity_change / eq_start * 100.0) if (equity_change is not None and eq_start) else None

    # Open positions snapshot at end of range
    open_now = db.execute(
        """SELECT ticker, sector, entry_price, current_price, shares, pnl, opened_at
           FROM positions
           WHERE status='OPEN'""",
    ).fetchall()
    open_count = len(open_now)
    open_value = sum((r["current_price"] or r["entry_price"] or 0) * (r["shares"] or 0) for r in open_now)

    # ── Section C: Positions detail ──────────────────────────────────────
    closed_rows = []
    for r in closed:
        cost = (r["entry_price"] or 0) * (r["shares"] or 0)
        ret_pct = ((r["pnl"] or 0) / cost * 100.0) if cost else 0.0
        opened_dt = _safe_dt(r["opened_at"])
        closed_dt = _safe_dt(r["closed_at"])
        days_held = ((closed_dt - opened_dt).total_seconds() / 86400.0) if (opened_dt and closed_dt) else None
        closed_rows.append({
            "ticker":      r["ticker"],
            "sector":      r["sector"],
            "shares":      r["shares"],
            "entry":       r["entry_price"],
            "exit":        r["current_price"],
            "pnl":         round(r["pnl"] or 0, 2),
            "ret_pct":     round(ret_pct, 2),
            "days_held":   round(days_held, 2) if days_held is not None else None,
            "exit_reason": r["exit_reason"],
            "opened_at":   r["opened_at"],
            "closed_at":   r["closed_at"],
        })

    # Top winner / loser
    top_win  = max(closed_rows, key=lambda r: r["pnl"]) if closed_rows else None
    top_loss = min(closed_rows, key=lambda r: r["pnl"]) if closed_rows else None

    # Sectors traded (closed in range)
    sector_pnl = Counter()
    sector_count = Counter()
    for r in closed_rows:
        s = r["sector"] or "Unknown"
        sector_pnl[s] += r["pnl"]
        sector_count[s] += 1
    sectors_traded = [
        {"sector": s, "trades": n, "pnl": round(sector_pnl[s], 2)}
        for s, n in sector_count.most_common()
    ]

    db.close()

    return {
        "customer_id":   cid,
        "display_name":  name,
        "activity": {
            "opened_count":      opened_count,
            "opened_volume":     round(opened_volume, 2),
            "closed_count":      closed_count,
            "closed_volume":     round(closed_volume, 2),
            "realized_pnl":      round(realized_pnl, 2),
            "wins":              wins,
            "losses":            losses,
            "win_rate_pct":      round(wins / max(closed_count, 1) * 100.0, 1),
            "equity_start":      round(eq_start, 2) if eq_start is not None else None,
            "equity_end":        round(eq_end,   2) if eq_end   is not None else None,
            "equity_change":     round(equity_change, 2) if equity_change is not None else None,
            "equity_change_pct": round(equity_change_pct, 2) if equity_change_pct is not None else None,
            "open_count":        open_count,
            "open_value":        round(open_value, 2),
        },
        "positions": {
            "closed":    closed_rows,
            "top_win":   top_win,
            "top_loss":  top_loss,
            "sectors":   sectors_traded,
        },
    }


# ── Aggregate / fleet-level report ───────────────────────────────────────

def _build_aggregate_report(customer_reports: list[dict]) -> dict:
    """Cross-customer rollups.  Inputs: a list of per-customer reports."""
    # Most-traded tickers (by # customers who traded them in range)
    ticker_customers: dict[str, set[str]] = defaultdict(set)
    ticker_pnl: dict[str, float]          = defaultdict(float)
    ticker_trade_count: Counter           = Counter()

    sector_customers: dict[str, set[str]] = defaultdict(set)
    sector_pnl: dict[str, float]          = defaultdict(float)
    sector_trade_count: Counter           = Counter()

    total_opened = total_closed = 0
    total_realized_pnl = 0.0

    for cr in customer_reports:
        if not cr:
            continue
        cid = cr.get("customer_id")
        a   = cr.get("activity", {})
        total_opened       += a.get("opened_count", 0)
        total_closed       += a.get("closed_count", 0)
        total_realized_pnl += a.get("realized_pnl",  0.0)
        for tr in cr.get("positions", {}).get("closed", []):
            t = tr.get("ticker") or "?"
            s = tr.get("sector") or "Unknown"
            ticker_customers[t].add(cid)
            ticker_pnl[t]   += tr.get("pnl") or 0
            ticker_trade_count[t] += 1
            sector_customers[s].add(cid)
            sector_pnl[s]   += tr.get("pnl") or 0
            sector_trade_count[s] += 1

    # Top 20 most-traded tickers across customers
    top_tickers = sorted(
        ticker_customers.items(),
        key=lambda kv: (-len(kv[1]), -ticker_trade_count[kv[0]])
    )[:20]
    top_tickers_out = [
        {
            "ticker":   t,
            "customers": len(custs),
            "trades":   ticker_trade_count[t],
            "pnl":      round(ticker_pnl[t], 2),
        }
        for t, custs in top_tickers
    ]

    # Top sectors
    top_sectors = sorted(
        sector_customers.items(),
        key=lambda kv: (-len(kv[1]), -sector_trade_count[kv[0]])
    )
    top_sectors_out = [
        {
            "sector":    s,
            "customers": len(custs),
            "trades":    sector_trade_count[s],
            "pnl":       round(sector_pnl[s], 2),
        }
        for s, custs in top_sectors
    ]

    # Signal-cluster: tickers traded by ≥3 customers
    signal_clusters = [t for t in top_tickers_out if t["customers"] >= 3]

    return {
        "fleet_totals": {
            "trades_opened":   total_opened,
            "trades_closed":   total_closed,
            "realized_pnl":    round(total_realized_pnl, 2),
            "active_customers": sum(1 for cr in customer_reports
                                   if cr and (cr["activity"]["opened_count"] +
                                              cr["activity"]["closed_count"]) > 0),
        },
        "top_tickers":     top_tickers_out,
        "top_sectors":     top_sectors_out,
        "signal_clusters": signal_clusters,
    }


# ── Public entry point ───────────────────────────────────────────────────

def run_report(start: str, end: str, customers: list[str],
               include_aggregate: bool = True) -> dict:
    """Generate the full report.  Pure data — no UI."""
    scope = _resolve_scope(customers)
    customer_reports = []
    for cid in scope:
        rep = _build_customer_report(cid, start, end)
        if rep:
            customer_reports.append(rep)

    out = {
        "meta": {
            "generated_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "start":          start,
            "end":            end,
            "scope_input":    customers,
            "scope_resolved": scope,
            "customer_count": len(customer_reports),
        },
        "customers": customer_reports,
    }
    if include_aggregate and len(customer_reports) > 1:
        out["aggregate"] = _build_aggregate_report(customer_reports)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────

def _format_text(report: dict) -> str:
    """Render the report as plain text — mirror what the cmd portal HTML
    will show.  Useful for terminal use + email digests."""
    lines: list[str] = []
    m = report["meta"]
    lines.append("=" * 78)
    lines.append(f"CUSTOMER ACTIVITY REPORT  {m['start']} → {m['end']}")
    lines.append(f"Generated {m['generated_at']}  ·  customers: {m['customer_count']}")
    lines.append("=" * 78)
    lines.append("")

    for cr in report.get("customers", []):
        a = cr["activity"]
        lines.append(f"── {cr['display_name']}  ({cr['customer_id'][:8]})")
        lines.append("")
        eq_str = ""
        if a.get("equity_start") is not None and a.get("equity_end") is not None:
            sign = "+" if (a["equity_change"] or 0) >= 0 else ""
            eq_str = (f"  equity  ${a['equity_start']:,.2f} → ${a['equity_end']:,.2f}  "
                      f"({sign}${a['equity_change']:,.2f}, "
                      f"{sign}{a['equity_change_pct']:.2f}%)")
        else:
            eq_str = "  equity  (no portfolio_value snapshots in range)"
        lines.append(eq_str)
        lines.append(f"  trades  opened {a['opened_count']:3} ($"
                     f"{a['opened_volume']:>10,.0f})   "
                     f"closed {a['closed_count']:3} ($"
                     f"{a['closed_volume']:>10,.0f})")
        sign = "+" if a["realized_pnl"] >= 0 else "-"
        lines.append(f"  realized P&L  {sign}${abs(a['realized_pnl']):>10,.2f}   "
                     f"win rate {a['win_rate_pct']:.1f}% "
                     f"({a['wins']}W / {a['losses']}L)")
        lines.append(f"  open at end   {a['open_count']:3} positions ($"
                     f"{a['open_value']:>10,.0f})")
        lines.append("")
        if cr["positions"]["top_win"]:
            t = cr["positions"]["top_win"]
            lines.append(f"  best:   {t['ticker']:6} +${t['pnl']:>7,.2f}  ({t['ret_pct']:.1f}%)  "
                         f"{t['exit_reason'] or '?'}")
        if cr["positions"]["top_loss"]:
            t = cr["positions"]["top_loss"]
            sign = "+" if t["pnl"] >= 0 else "-"
            lines.append(f"  worst:  {t['ticker']:6} {sign}${abs(t['pnl']):>7,.2f}  ({t['ret_pct']:.1f}%)  "
                         f"{t['exit_reason'] or '?'}")
        if cr["positions"]["sectors"]:
            lines.append("")
            lines.append(f"  sectors traded:")
            for s in cr["positions"]["sectors"][:6]:
                sign = "+" if s["pnl"] >= 0 else "-"
                lines.append(f"    {s['sector']:30}  {s['trades']:>3} trades  "
                             f"{sign}${abs(s['pnl']):>7,.2f}")
        lines.append("")
        lines.append("")

    if "aggregate" in report:
        agg = report["aggregate"]
        lines.append("=" * 78)
        lines.append("FLEET TOTALS")
        lines.append("=" * 78)
        ft = agg["fleet_totals"]
        sign = "+" if ft["realized_pnl"] >= 0 else "-"
        lines.append(f"  active customers: {ft['active_customers']}   "
                     f"opened {ft['trades_opened']}   closed {ft['trades_closed']}   "
                     f"net P&L {sign}${abs(ft['realized_pnl']):>10,.2f}")
        lines.append("")
        if agg.get("signal_clusters"):
            lines.append(f"  ⚠ tickers traded by ≥3 customers (signal cluster):")
            for t in agg["signal_clusters"]:
                sign = "+" if t["pnl"] >= 0 else "-"
                lines.append(f"    {t['ticker']:6} {t['customers']:>2} customers  "
                             f"{t['trades']:>3} trades  {sign}${abs(t['pnl']):>7,.2f}")
            lines.append("")
        if agg["top_tickers"]:
            lines.append(f"  Top tickers across fleet:")
            lines.append(f"    {'ticker':6} {'cust':>4} {'trades':>6} {'pnl':>10}")
            for t in agg["top_tickers"][:10]:
                sign = "+" if t["pnl"] >= 0 else "-"
                lines.append(f"    {t['ticker']:6} {t['customers']:>4} {t['trades']:>6} "
                             f"{sign}${abs(t['pnl']):>8,.2f}")
            lines.append("")
        if agg["top_sectors"]:
            lines.append(f"  Top sectors across fleet:")
            for s in agg["top_sectors"][:6]:
                sign = "+" if s["pnl"] >= 0 else "-"
                lines.append(f"    {s['sector']:30}  {s['customers']:>2} customers  "
                             f"{s['trades']:>3} trades  {sign}${abs(s['pnl']):>7,.2f}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--start", required=True,
                        help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end",   required=True,
                        help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--customer", action="append", default=None,
                        help='Customer ID, "all", or "owner". Repeatable.')
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of text")
    args = parser.parse_args()

    customers = args.customer if args.customer else ["all"]
    report = run_report(args.start, args.end, customers)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
