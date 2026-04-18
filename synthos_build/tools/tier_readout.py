#!/usr/bin/env python3
"""Per-tier behavior snapshot for the calibration experiment.

Reads every customer's signals.db and produces a markdown table of:

  - open positions, deployed $, sector diversity
  - closed trades since EXPERIMENT_START, realized PnL, win rate
  - avg trade dollar size
  - top skip reasons from recent TRADE_DECISION entries — the actual
    calibration data ("why was this gate rejecting?")
  - exit reasons on closed trades

Read-only. Safe to run any time, any number of times. Fleet list is
discovered dynamically via _fleet.iter_customers() — adding a new
customer doesn't require editing this script.

Run:
    cd ~/synthos/synthos_build && python3 tools/tier_readout.py
"""
import sys
import json
import collections
from datetime import datetime, timezone
from pathlib import Path

# Make synthos_build importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from _fleet import iter_customers, project_root

from src.retail_database import DB


def _row(cdb, name, tier):
    with cdb.conn() as c:
        settings_rows = c.execute(
            "SELECT key, value FROM customer_settings"
        ).fetchall()
        settings  = dict(settings_rows)
        start_iso = settings.get('EXPERIMENT_START')

        opens = c.execute(
            "SELECT ticker, sector, entry_price, shares, pnl "
            "FROM positions WHERE status='OPEN'"
        ).fetchall()

        if start_iso:
            closed = c.execute(
                "SELECT ticker, entry_price, shares, pnl, exit_reason "
                "FROM positions WHERE status='CLOSED' AND closed_at >= ?",
                (start_iso,),
            ).fetchall()
            decisions = c.execute(
                "SELECT details FROM system_log "
                "WHERE event='TRADE_DECISION' AND timestamp >= ? "
                "ORDER BY id DESC LIMIT 500",
                (start_iso,),
            ).fetchall()
        else:
            closed    = []
            decisions = []

    open_count      = len(opens)
    open_deployed   = sum((p[2] or 0) * (p[3] or 0) for p in opens)
    sectors_open    = {p[1] for p in opens if p[1]}
    closed_count    = len(closed)
    closed_deployed = sum((p[1] or 0) * (p[2] or 0) for p in closed)
    closed_pnl      = sum(p[3] or 0 for p in closed)
    closed_winners  = sum(1 for p in closed if (p[3] or 0) > 0)
    exit_reasons    = collections.Counter(p[4] or 'unknown' for p in closed)

    avg_trade_usd = 0.0
    total_trades  = closed_count + open_count
    if total_trades:
        avg_trade_usd = (open_deployed + closed_deployed) / total_trades

    # First failing gate per rejected signal — points at which knob is
    # most commonly blocking each tier's trades.
    skips = collections.Counter()
    for (det,) in decisions:
        try:
            d = json.loads(det)
        except Exception:
            continue
        if d.get('decision') == 'SKIP':
            for g in d.get('gates', []):
                res = str(g.get('result'))
                if res in ('False', 'SKIP'):
                    skips[str(g.get('gate'))] += 1
                    break

    return {
        'name':            name,
        'tier':            tier,
        'equity':          settings.get('_ALPACA_EQUITY', '—'),
        'min_confidence':  settings.get('MIN_CONFIDENCE', '—'),
        'max_position':    settings.get('MAX_POSITION_PCT', '—'),
        'open_count':      open_count,
        'open_deployed':   open_deployed,
        'sectors_open':    len(sectors_open),
        'closed_count':    closed_count,
        'closed_pnl':      closed_pnl,
        'closed_winners':  closed_winners,
        'avg_trade_usd':   avg_trade_usd,
        'total_trades':    total_trades,
        'skips':           dict(skips.most_common(5)),
        'exits':           dict(exit_reasons.most_common(3)),
        'experiment_id':   settings.get('EXPERIMENT_ID', '—'),
        'start':           start_iso or '—',
    }


def main() -> int:
    root = project_root()
    rows = []
    for cid, name, tier in iter_customers():
        path = root / 'data' / 'customers' / cid / 'signals.db'
        try:
            cdb = DB(path=str(path))
            rows.append(_row(cdb, name, tier))
        except Exception as e:
            print(f"{name}: ERROR {e}")

    rows.sort(key=lambda r: (r['tier'], r['name']))

    exp_id = next((r['experiment_id'] for r in rows if r['experiment_id'] != '—'), '—')
    start  = next((r['start']         for r in rows if r['start']         != '—'), '—')
    print(f"# Tier calibration readout  —  {datetime.now(timezone.utc).isoformat()[:19]}Z")
    print(f"Experiment: `{exp_id}`  ·  started `{start}`\n")

    # Per-customer snapshot
    print("## Per-customer snapshot\n")
    print("| Tier | Customer | Equity | MinConf | MaxPos | Open | Deployed | Sectors | Closed | PnL | WinRate | AvgTrade$ |")
    print("|------|----------|--------|---------|--------|------|----------|---------|--------|-----|---------|-----------|")
    for r in rows:
        try:
            eq   = float(r['equity'])
            eq_s = f"${eq:,.0f}"
        except Exception:
            eq_s = str(r['equity'])
        wr = "—"
        if r['closed_count']:
            wr = f"{(r['closed_winners'] * 100.0 / r['closed_count']):.0f}% ({r['closed_winners']}/{r['closed_count']})"
        print(
            f"| {r['tier']} | {r['name']} | {eq_s} | {r['min_confidence']} | "
            f"{r['max_position']} | {r['open_count']} | ${r['open_deployed']:,.0f} | "
            f"{r['sectors_open']} | {r['closed_count']} | ${r['closed_pnl']:+,.2f} | "
            f"{wr} | ${r['avg_trade_usd']:,.0f} |"
        )

    # Per-tier aggregate
    print("\n## Tier aggregates\n")
    per_tier: dict = {}
    for r in rows:
        agg = per_tier.setdefault(r['tier'], {
            'customers': 0, 'open': 0, 'deployed': 0.0, 'closed': 0,
            'pnl': 0.0, 'winners': 0, 'total_trades': 0,
        })
        agg['customers']    += 1
        agg['open']         += r['open_count']
        agg['deployed']     += r['open_deployed']
        agg['closed']       += r['closed_count']
        agg['pnl']          += r['closed_pnl']
        agg['winners']      += r['closed_winners']
        agg['total_trades'] += r['total_trades']

    print("| Tier | Customers | Total trades | Open | Deployed | Closed | PnL | WinRate |")
    print("|------|-----------|--------------|------|----------|--------|-----|---------|")
    for t in sorted(per_tier.keys()):
        a = per_tier[t]
        wr = "—"
        if a['closed']:
            wr = f"{(a['winners'] * 100.0 / a['closed']):.0f}%"
        print(
            f"| {t} | {a['customers']} | {a['total_trades']} | {a['open']} | "
            f"${a['deployed']:,.0f} | {a['closed']} | ${a['pnl']:+,.2f} | {wr} |"
        )

    # Skip breakdown
    print("\n## Top skip reasons  (first failing gate per rejected signal)\n")
    for r in rows:
        if not r['skips']:
            continue
        print(f"**{r['tier']} {r['name']}**")
        for gate, n in r['skips'].items():
            print(f"  - {gate}: {n}")
        print()

    # Exit reasons
    if any(r['exits'] for r in rows):
        print("## Exit reasons on closed trades\n")
        for r in rows:
            if not r['exits']:
                continue
            print(f"**{r['tier']} {r['name']}**")
            for reason, n in r['exits'].items():
                print(f"  - {reason}: {n}")
            print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
