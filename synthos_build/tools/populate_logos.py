#!/usr/bin/env python3
"""Populate ticker_logos table from data/ticker_domains.csv + Clearbit.

Phase B (2026-04-27). One-shot tool — run after deploying the schema
migration. Idempotent: re-running upserts and only re-fetches PENDING /
NOT_FOUND_RETRY rows.

Usage:
    python3 synthos_build/tools/populate_logos.py
    python3 synthos_build/tools/populate_logos.py --refresh-failed   # retry NOT_FOUND
    python3 synthos_build/tools/populate_logos.py --dry-run

Targets the SHARED user/signals.db (logos are universal; same Apple
logo for every customer). Skips per-customer DBs.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# Resolve project root, add src to path
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / 'src'))

import requests  # noqa: E402

from retail_database import get_shared_db  # noqa: E402

CLEARBIT_URL = "https://logo.clearbit.com/{domain}"
USER_AGENT = "Synthos-LogoFetch/1.0 (+https://synth-cloud.com; contact@synth-cloud.com)"
TIMEOUT_SECS = 10
SLEEP_BETWEEN_FETCHES = 0.5  # be polite — Clearbit is free

CSV_PATH = _ROOT / 'data' / 'ticker_domains.csv'


def shared_db():
    """Return the shared market-intel DB (user/signals.db). Same
    instance the portal and every agent uses via get_shared_db()."""
    return get_shared_db()


def load_csv(path: Path) -> list[tuple[str, str]]:
    """Parse CSV → [(ticker, domain), ...]. Skips comment lines."""
    rows = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith('#'):
                continue
            if row[0].strip() == 'ticker':
                continue  # header
            if len(row) < 2:
                continue
            ticker = row[0].strip().upper()
            domain = row[1].strip().lower()
            if ticker and domain:
                rows.append((ticker, domain))
    return rows


def fetch_logo(domain: str) -> tuple[bytes | None, int]:
    """Fetch PNG bytes from Clearbit. Returns (bytes_or_None, http_status).
    Treats 4xx as definitive NOT_FOUND; transient errors raise."""
    url = CLEARBIT_URL.format(domain=domain)
    try:
        r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT_SECS)
    except requests.RequestException as e:
        print(f"  [transient] {domain}: {e}")
        return None, 0
    if r.status_code == 200 and r.content:
        return r.content, 200
    return None, r.status_code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh-failed', action='store_true',
                    help="Re-attempt fetching tickers marked NOT_FOUND")
    ap.add_argument('--dry-run', action='store_true',
                    help="Don't write to DB or hit network")
    args = ap.parse_args()

    db = shared_db()
    print(f"Target DB: {db.path}")

    csv_rows = load_csv(CSV_PATH)
    print(f"CSV rows : {len(csv_rows)}")

    if args.dry_run:
        for ticker, domain in csv_rows[:10]:
            print(f"  would fetch {ticker:8} {domain}")
        if len(csv_rows) > 10:
            print(f"  ... and {len(csv_rows)-10} more")
        return

    # Seed PENDING rows for any (ticker, domain) not yet in the table.
    # Status PENDING is the "queued, will fetch in this run" marker.
    seeded = 0
    skipped_existing = 0
    for ticker, domain in csv_rows:
        existing_png, existing_status = db.get_ticker_logo(ticker)
        if existing_status == 'OK':
            skipped_existing += 1
            continue
        if existing_status == 'NOT_FOUND' and not args.refresh_failed:
            skipped_existing += 1
            continue
        # Mark pending (or refresh)
        db.set_ticker_logo(ticker, png_bytes=None, domain=domain, status='PENDING')
        seeded += 1
    print(f"Seeded   : {seeded} pending  ·  skipped {skipped_existing} already-OK/NOT_FOUND")

    # Drain PENDING rows
    pending = db.get_pending_logo_tickers()
    print(f"Fetching : {len(pending)} ...")

    ok = nf = err = 0
    for i, row in enumerate(pending, 1):
        ticker, domain = row['ticker'], row['domain']
        if not domain:
            db.set_ticker_logo(ticker, None, status='NOT_FOUND')
            nf += 1
            continue

        png, status = fetch_logo(domain)
        if png:
            db.set_ticker_logo(ticker, png, domain=domain, status='OK')
            ok += 1
            tag = "OK"
        elif status in (404, 422):
            db.set_ticker_logo(ticker, None, domain=domain, status='NOT_FOUND')
            nf += 1
            tag = f"NF({status})"
        else:
            err += 1
            tag = f"ERR({status})"

        size = len(png) if png else 0
        print(f"  [{i:3}/{len(pending)}] {ticker:8} {domain:32} {tag:10} {size}B")

        if i < len(pending):
            time.sleep(SLEEP_BETWEEN_FETCHES)

    print()
    print(f"Done.  OK={ok}  NOT_FOUND={nf}  ERROR={err}")
    print(f"Total bytes cached: {sum_logo_bytes(db)} bytes "
          f"({sum_logo_bytes(db)/1024:.1f} KB)")


def sum_logo_bytes(db) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT SUM(LENGTH(logo_png)) AS n FROM ticker_logos WHERE status='OK'"
        ).fetchone()
        return int(row['n'] or 0)


if __name__ == '__main__':
    main()
