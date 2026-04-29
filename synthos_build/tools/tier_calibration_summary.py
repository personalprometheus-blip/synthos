#!/usr/bin/env python3
"""
tier_calibration_summary.py — Per-preset characterization of trading
behavior during the April 2026 tier-calibration lockdown window.

Reads each customer's signals.db, groups by PRESET_NAME (conservative /
moderate / aggressive / custom), and reports:
  * customers per preset
  * positions opened / closed during the window
  * win/loss split (with the standard caveat: the score distribution
    + signal pipeline both shifted mid-window, so absolute hit-rate
    numbers are partially contaminated)
  * average dollars per trade
  * average entry signal score
  * top sectors per customer

Window defaults to 2026-04-15 → 2026-04-29; override via env vars
W_START / W_END (ISO timestamps).

Run on whichever host has the customer signals.db files (pi5):
    cd ~/synthos/synthos_build
    python3 tools/tier_calibration_summary.py
"""
import os
import sys
import sqlite3
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'src'))

W_START = os.environ.get('W_START', '2026-04-15 00:00:00')
W_END   = os.environ.get('W_END',   '2026-04-29 00:00:00')


def fetch_customer(cid: str, customers_dir: str):
    p = os.path.join(customers_dir, cid, 'signals.db')
    if not os.path.exists(p):
        return None
    with sqlite3.connect(p) as c:
        c.row_factory = sqlite3.Row
        s = dict(
            (r['key'], r['value'])
            for r in c.execute("SELECT key, value FROM customer_settings").fetchall()
        )
        opened = c.execute(
            """SELECT COUNT(*) AS n,
                      AVG(entry_price * shares)             AS avg_dollars,
                      AVG(CAST(entry_signal_score AS REAL)) AS avg_score
                 FROM positions
                WHERE opened_at >= ? AND opened_at < ?""",
            (W_START, W_END)
        ).fetchone()
        closed = c.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                      SUM(pnl) AS total_pl
                 FROM positions
                WHERE closed_at IS NOT NULL
                  AND closed_at >= ? AND closed_at < ?""",
            (W_START, W_END)
        ).fetchone()
        sectors = c.execute(
            """SELECT sector, COUNT(*) AS n FROM positions
                WHERE opened_at >= ? AND opened_at < ?
                GROUP BY sector ORDER BY n DESC LIMIT 3""",
            (W_START, W_END)
        ).fetchall()
        sec_str = ', '.join(f"{r['sector']}:{r['n']}" for r in sectors) if sectors else '—'
        return {
            'cid':       cid[:8],
            'preset':    s.get('PRESET_NAME', '?'),
            'min_c':     s.get('MIN_CONFIDENCE', '?'),
            'max_p':     s.get('MAX_POSITIONS', '?'),
            'max_pct':   s.get('MAX_POSITION_PCT', '?'),
            'frozen':    'Y' if s.get('EXPERIMENT_FREEZE', 'false').lower() == 'true' else 'N',
            'opened':    opened['n'] or 0,
            'avg_d':     opened['avg_dollars'],
            'avg_s':     opened['avg_score'],
            'closed':    closed['n'] or 0,
            'wins':      closed['wins'] or 0,
            'losses':    closed['losses'] or 0,
            'total_pl':  closed['total_pl'] or 0,
            'sectors':   sec_str,
        }


def main():
    customers_dir = os.path.join(_HERE, '..', 'data', 'customers')
    rows = []
    for cid in os.listdir(customers_dir):
        if cid == 'default':
            continue
        r = fetch_customer(cid, customers_dir)
        if r:
            rows.append(r)

    rows.sort(key=lambda r: (str(r['preset']), str(r['min_c'])))

    print(f"Window: {W_START} → {W_END}")
    print('=' * 138)
    hdr = (
        f"{'cid':10}{'preset':14}{'frozen':7}{'min_c':8}{'max_p':7}{'pos%':7}"
        f"{'opened':>8}{'avg_$':>10}{'avg_sc':>8}"
        f"{'closed':>8}{'wins':>6}{'loss':>6}{'pl_$':>10}  sectors"
    )
    print(hdr)
    print('-' * 138)
    for r in rows:
        avg_d = f"${r['avg_d']:.0f}" if r['avg_d'] else '—'
        avg_s = f"{r['avg_s']:.2f}" if r['avg_s'] else '—'
        pl    = f"${r['total_pl']:.0f}" if r['total_pl'] else '—'
        print(
            f"{r['cid']:10}{str(r['preset']):14}{r['frozen']:7}"
            f"{str(r['min_c']):8}{str(r['max_p']):7}{str(r['max_pct']):7}"
            f"{r['opened']:>8}{avg_d:>10}{avg_s:>8}"
            f"{r['closed']:>8}{r['wins']:>6}{r['losses']:>6}{pl:>10}  {r['sectors']}"
        )

    # Group by preset
    by = defaultdict(lambda: {
        'custs': 0, 'opened': 0, 'closed': 0,
        'wins': 0, 'losses': 0, 'pl': 0.0,
        'sum_d': 0.0, 'sum_s': 0.0, 'score_n': 0
    })
    for r in rows:
        p = str(r['preset'])
        by[p]['custs']  += 1
        by[p]['opened'] += r['opened']
        by[p]['closed'] += r['closed']
        by[p]['wins']   += r['wins']
        by[p]['losses'] += r['losses']
        by[p]['pl']     += r['total_pl']
        if r['avg_d'] and r['opened']:
            by[p]['sum_d'] += r['avg_d'] * r['opened']
        if r['avg_s'] and r['opened']:
            by[p]['sum_s']   += r['avg_s'] * r['opened']
            by[p]['score_n'] += r['opened']

    print()
    print('=' * 105)
    print('SUMMARY BY PRESET (tier-calibration window)')
    print('=' * 105)
    print(
        f"{'preset':14}{'custs':>7}{'opened':>9}{'closed':>9}"
        f"{'wins':>6}{'loss':>6}{'hit%':>8}"
        f"{'avg_$/trade':>14}{'avg_score':>11}{'total_pl':>12}"
    )
    print('-' * 105)
    for p in sorted(by.keys()):
        d = by[p]
        decided = d['wins'] + d['losses']
        hit = (d['wins'] / decided * 100) if decided else 0.0
        avg_d = (d['sum_d'] / d['opened']) if d['opened'] else 0.0
        avg_s = (d['sum_s'] / d['score_n']) if d['score_n'] else 0.0
        print(
            f"{p:14}{d['custs']:>7}{d['opened']:>9}{d['closed']:>9}"
            f"{d['wins']:>6}{d['losses']:>6}{hit:>7.1f}%"
            f"{avg_d:>13.0f}$ {avg_s:>10.3f}{d['pl']:>11.0f}$"
        )

    print()
    print('NOTE: hit-rate and total_pl are not clean comparisons across presets.')
    print('      Signal pipeline + Gate 5 scoring changed multiple times during')
    print('      the window. Trust the activity columns (opened, closed,')
    print('      avg_$/trade, avg_score) for tier characterization. Do NOT')
    print('      tune from win-rate or P&L.')


if __name__ == '__main__':
    main()
