"""
retail_daily_master.py — End-of-day audit rollup.

Phase 5.c of TRADER_RESTRUCTURE_PLAN. Collects the day's activity across
all active customers into a single Markdown file at
`logs/daily_master/YYYY-MM-DD.md`. Intended as an auditable daily
artifact: what the system did, what it skipped, and why.

Sections:
  - Summary stats (customers, opens, closes, realized P&L, etc.)
  - Enrichment (candidates emitted, macro regime)
  - Trades opened (with entry score, source)
  - Trades closed (with P&L and exit reason)
  - Cooling-off roster
  - Upcoming earnings for tickers we're watching

Run via `market_daemon.run_close_session()` at 4 PM ET, or manually:
  python3 src/retail_daily_master.py [--date YYYY-MM-DD]

Idempotent — writes to the same path multiple times just overwrites.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger('daily_master')

_ROOT_DIR = Path(__file__).resolve().parent.parent
_LOGS_DIR = _ROOT_DIR / 'logs' / 'daily_master'
_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')


def _get_active_customers():
    """Mirrors retail_market_daemon.get_active_customers — avoids importing
    the daemon to keep module load cheap when called from CLI."""
    try:
        import auth  # noqa: E402
        customers = auth.list_customers()
        return [c['id'] for c in customers if c.get('is_active')]
    except Exception as e:
        log.warning(f"auth.list_customers failed: {e}")
        return [_OWNER_CID]


def _opens_today(db, day_iso: str) -> list[dict]:
    """Positions opened on `day_iso` (YYYY-MM-DD) for this customer."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT ticker, sector, entry_price, shares, opened_at, "
            "entry_signal_score, entry_sentiment_score, interrogation_status "
            "FROM positions "
            "WHERE opened_at >= ? AND opened_at < datetime(?, '+1 day') "
            "ORDER BY opened_at",
            (day_iso, day_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def _closes_today(db, day_iso: str) -> list[dict]:
    """Positions closed on `day_iso` for this customer."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT ticker, entry_price, current_price as exit_price, "
            "shares, pnl, closed_at, exit_reason "
            "FROM positions "
            "WHERE closed_at >= ? AND closed_at < datetime(?, '+1 day') "
            "ORDER BY closed_at",
            (day_iso, day_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def _candidates_today(shared_db, day_iso: str) -> list[dict]:
    """Candidate signals emitted today (master/shared DB only)."""
    with shared_db.conn() as c:
        rows = c.execute(
            "SELECT ticker, sector, entry_signal_score, created_at "
            "FROM signals "
            "WHERE source='candidate' AND created_at >= ? "
            "AND created_at < datetime(?, '+1 day') "
            "ORDER BY created_at",
            (day_iso, day_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def _cooling_off_roster(shared_db) -> list[dict]:
    """Current cooling-off entries."""
    with shared_db.conn() as c:
        rows = c.execute(
            "SELECT ticker, cool_until, reason, pnl_pct FROM cooling_off "
            "WHERE cool_until > datetime('now') "
            "ORDER BY cool_until"
        ).fetchall()
    return [dict(r) for r in rows]


def _upcoming_earnings(shared_db, days_ahead: int = 5) -> list[dict]:
    """Earnings_cache rows with next_earnings within `days_ahead` biz days."""
    today = datetime.now(timezone.utc).date()
    with shared_db.conn() as c:
        rows = c.execute(
            "SELECT ticker, next_earnings FROM earnings_cache "
            "WHERE next_earnings IS NOT NULL "
            "AND next_earnings >= ? "
            "AND next_earnings <= date(?, ?) "
            "ORDER BY next_earnings, ticker",
            (today.isoformat(), today.isoformat(), f'+{days_ahead} days'),
        ).fetchall()
    return [dict(r) for r in rows]


def _end_of_day_portfolio(db) -> dict:
    """Current portfolio snapshot."""
    try:
        portfolio = db.get_portfolio()
        positions = db.get_open_positions()
        deployed = sum(float(p.get('entry_price') or 0) * float(p.get('shares') or 0)
                       for p in positions)
        return {
            'cash':            float(portfolio.get('cash') or 0),
            'realized_gains':  float(portfolio.get('realized_gains') or 0),
            'positions':       len(positions),
            'deployed':        deployed,
        }
    except Exception as e:
        log.debug(f"portfolio snapshot failed: {e}")
        return {'cash': 0, 'realized_gains': 0, 'positions': 0, 'deployed': 0}


# ── Markdown rendering ──────────────────────────────────────────────────

def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(none)_\n"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    out = []
    out.append("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    out.append(sep)
    for row in rows:
        out.append("| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + " |")
    return "\n".join(out) + "\n"


def render_report(day_iso: str) -> str:
    """Build the full Markdown report as a string."""
    sys.path.insert(0, str(_ROOT_DIR / 'src'))
    from retail_database import get_customer_db  # noqa: E402

    shared_db = get_customer_db(_OWNER_CID)
    customers = _get_active_customers()

    # Aggregate per-customer data
    all_opens = []
    all_closes = []
    portfolios = {}
    for cid in customers:
        try:
            db = get_customer_db(cid)
            opens = _opens_today(db, day_iso)
            closes = _closes_today(db, day_iso)
            portfolios[cid] = _end_of_day_portfolio(db)
            for r in opens:
                r['customer_id'] = cid
            for r in closes:
                r['customer_id'] = cid
            all_opens.extend(opens)
            all_closes.extend(closes)
        except Exception as e:
            log.warning(f"aggregation failed for {cid[:8]}: {e}")

    candidates = _candidates_today(shared_db, day_iso)
    cooling = _cooling_off_roster(shared_db)
    earnings = _upcoming_earnings(shared_db, days_ahead=5)

    # Summary counts
    wins = [c for c in all_closes if (c.get('pnl') or 0) >= 0]
    losses = [c for c in all_closes if (c.get('pnl') or 0) < 0]
    realized = sum(float(c.get('pnl') or 0) for c in all_closes)
    total_deployed = sum(p['deployed'] for p in portfolios.values())
    total_cash = sum(p['cash'] for p in portfolios.values())
    total_positions = sum(p['positions'] for p in portfolios.values())

    # Render
    out = []
    out.append(f"# Daily Master — {day_iso}\n")
    out.append(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n")

    out.append("## Summary\n")
    out.append(f"- **Active customers**: {len(customers)}")
    out.append(f"- **Trades opened**: {len(all_opens)}")
    out.append(f"- **Trades closed**: {len(all_closes)} ({len(wins)} W / {len(losses)} L)")
    out.append(f"- **Realized P&L today**: ${realized:+,.2f}")
    out.append(f"- **End-of-day positions**: {total_positions}")
    out.append(f"- **Deployed capital**: ${total_deployed:,.2f}")
    out.append(f"- **Cash**: ${total_cash:,.2f}")
    out.append(f"- **Cooling-off active**: {len(cooling)} ticker(s)")
    out.append(f"- **Candidates emitted today**: {len(candidates)}\n")

    out.append("## Trades Opened\n")
    out.append(_fmt_table(
        ['Time', 'Customer', 'Ticker', 'Sector', 'Shares', 'Entry', 'Score'],
        [[(r['opened_at'] or '')[11:19], r['customer_id'][:8],
          r['ticker'], (r.get('sector') or '-')[:20],
          f"{float(r['shares']):.2f}", f"${float(r['entry_price']):.2f}",
          f"{float(r.get('entry_signal_score') or 0):.3f}"]
         for r in all_opens]
    ))

    out.append("## Trades Closed\n")
    out.append(_fmt_table(
        ['Time', 'Customer', 'Ticker', 'Entry', 'Exit', 'P&L $', 'Reason'],
        [[(r['closed_at'] or '')[11:19], r['customer_id'][:8],
          r['ticker'], f"${float(r['entry_price']):.2f}",
          f"${float(r.get('exit_price') or 0):.2f}",
          f"{float(r.get('pnl') or 0):+.2f}",
          (r.get('exit_reason') or '-')[:30]]
         for r in all_closes]
    ))

    out.append("## Candidate Signals Emitted\n")
    out.append(_fmt_table(
        ['Time', 'Ticker', 'Sector', 'Score'],
        [[(r['created_at'] or '')[11:19], r['ticker'],
          (r.get('sector') or '-')[:20],
          f"{float(r.get('entry_signal_score') or 0):.3f}"]
         for r in candidates]
    ))

    out.append("## Cooling-off Roster\n")
    out.append(_fmt_table(
        ['Ticker', 'Cool Until', 'Reason', 'P&L %'],
        [[r['ticker'], r['cool_until'][:16], (r.get('reason') or '-')[:40],
          (f"{r['pnl_pct']:+.2f}%" if r.get('pnl_pct') is not None else '-')]
         for r in cooling]
    ))

    out.append("## Upcoming Earnings (next 5 biz days)\n")
    out.append(_fmt_table(
        ['Date', 'Ticker'],
        [[r['next_earnings'], r['ticker']] for r in earnings]
    ))

    return "\n".join(out)


def generate(day_iso: str | None = None) -> str:
    """Write the daily master log for `day_iso` (defaults to today ET).
    Returns the path written."""
    if not day_iso:
        day_iso = date.today().isoformat()
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _LOGS_DIR / f"{day_iso}.md"
    report = render_report(day_iso)
    out_path.write_text(report)
    log.info(f"[DAILY MASTER] wrote {out_path} ({len(report)} bytes)")
    return str(out_path)


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='YYYY-MM-DD (default: today)')
    args = ap.parse_args()
    path = generate(args.date)
    print(path)


if __name__ == '__main__':
    main()
