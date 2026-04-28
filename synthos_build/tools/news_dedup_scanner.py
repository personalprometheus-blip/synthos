#!/usr/bin/env python3
"""
news_dedup_scanner.py
=====================

Periodic auditor that watches the shared news_feed for duplicate-leak
signs. Belt-and-suspenders companion to the write-time dedup in
Database.write_news_feed_entry() and the UNIQUE INDEX backstop.

Three categories
----------------
1. HARD_DUP — same (ticker, raw_headline) appears 2+ times within 24h.
   Should never happen with Patch B in place; if it does, write-time
   dedup leaked. Logged at WARNING.
2. CROSS_SOURCE_CORROBORATION — same raw_headline appears under
   distinct `metadata.source` values within 12h. Legitimate (multiple
   outlets covering the same story) but useful to know about: today
   each source counts as a separate row, which inflates corroboration
   counters. Logged at INFO.
3. PARAPHRASE_SUSPECT — Jaccard similarity > 0.80 across two distinct
   raw_headlines for the same ticker within 12h. Logged at INFO.
   No auto-deletion; reviewer decides.

Output
------
* Stdout: ASCII summary table.
* If --post is passed, posts findings to the cmd portal at pi4b
  (auditor.db) so they show up in /api/logs-audit.
* Exit code 0 if no HARD_DUP findings, 1 otherwise — useful for cron
  failure email if hard dups start re-appearing.

Usage
-----
  # Manual:
  python3 tools/news_dedup_scanner.py
  python3 tools/news_dedup_scanner.py --hours 6
  python3 tools/news_dedup_scanner.py --post

  # Cron (hourly):
  0 * * * * cd /home/.../synthos_build && python3 tools/news_dedup_scanner.py --post
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

_DIR   = os.path.dirname(os.path.abspath(__file__))
_BUILD = os.path.dirname(_DIR)
_USER  = os.path.join(_BUILD, 'user')
SHARED_DB = os.path.join(_USER, 'signals.db')

# ── Helpers ──────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    if not text:
        return ''
    return re.sub(r'[^a-z0-9 ]', '', text.lower()).strip()

def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard. Same simple shape as retail_news_agent uses."""
    if not a or not b:
        return 0.0
    ta = set(_normalize(a).split())
    tb = set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def _meta_source(meta_json):
    if not meta_json:
        return None
    try:
        m = json.loads(meta_json)
        return m.get('source')
    except Exception:
        return None


# ── Scanners ─────────────────────────────────────────────────────────────

def scan_hard_dups(db, since_hours: int):
    """Same (ticker, raw_headline) inserted 2+ times within `since_hours`.
    With Patch B's write-time dedup + UNIQUE index this should be empty."""
    rows = db.execute(
        f"""SELECT ticker, raw_headline, COUNT(*) AS c,
                   MIN(created_at) AS first_seen, MAX(created_at) AS last_seen,
                   GROUP_CONCAT(id) AS ids
            FROM news_feed
            WHERE created_at >= datetime('now', '-{int(since_hours)} hours')
              AND raw_headline IS NOT NULL AND raw_headline != ''
            GROUP BY ticker, raw_headline
            HAVING c > 1
            ORDER BY c DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def scan_cross_source(db, since_hours: int):
    """Same raw_headline, distinct metadata.source. Legitimate
    corroboration but inflates row count if naively summed."""
    rows = db.execute(
        f"""SELECT id, ticker, raw_headline, metadata, created_at
            FROM news_feed
            WHERE created_at >= datetime('now', '-{int(since_hours)} hours')
              AND raw_headline IS NOT NULL AND raw_headline != ''"""
    ).fetchall()
    bucket = defaultdict(list)
    for r in rows:
        key = (r['ticker'], r['raw_headline'])
        bucket[key].append({
            'id':         r['id'],
            'source':     _meta_source(r['metadata']),
            'created_at': r['created_at'],
        })
    findings = []
    for (ticker, headline), entries in bucket.items():
        sources = {e['source'] for e in entries if e['source']}
        if len(sources) > 1:
            findings.append({
                'ticker':   ticker,
                'headline': headline[:80],
                'sources':  sorted(sources),
                'rows':     len(entries),
                'first':    min(e['created_at'] for e in entries),
                'last':     max(e['created_at'] for e in entries),
            })
    findings.sort(key=lambda f: -f['rows'])
    return findings


def scan_paraphrase(db, since_hours: int, threshold: float = 0.80):
    """Pairs of distinct headlines with Jaccard > threshold for same
    ticker within `since_hours`. Heuristic — flags reposts with minor
    edits (e.g. "Stock A surges X%" vs "Stock A jumps X%")."""
    rows = db.execute(
        f"""SELECT id, ticker, raw_headline, created_at
            FROM news_feed
            WHERE created_at >= datetime('now', '-{int(since_hours)} hours')
              AND raw_headline IS NOT NULL AND raw_headline != ''
              AND ticker IS NOT NULL AND ticker != ''"""
    ).fetchall()
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r['ticker']].append((r['id'], r['raw_headline'], r['created_at']))
    findings = []
    for ticker, items in by_ticker.items():
        seen = set()
        for i, (id_a, ha, ta) in enumerate(items):
            for id_b, hb, tb in items[i+1:]:
                if ha == hb:
                    continue  # exact dups handled by hard-dup scan
                if (id_a, id_b) in seen or (id_b, id_a) in seen:
                    continue
                sim = _jaccard(ha, hb)
                if sim >= threshold:
                    findings.append({
                        'ticker':       ticker,
                        'jaccard':      round(sim, 3),
                        'headline_a':   ha[:80],
                        'headline_b':   hb[:80],
                        'id_a':         id_a,
                        'id_b':         id_b,
                        'created_a':    ta,
                        'created_b':    tb,
                    })
                    seen.add((id_a, id_b))
    findings.sort(key=lambda f: -f['jaccard'])
    return findings


# ── Output ───────────────────────────────────────────────────────────────

def print_section(title, findings, formatter):
    print(f"== {title} ({len(findings)}) ==")
    if not findings:
        print("  (none)")
        print()
        return
    for f in findings[:15]:
        print("  " + formatter(f))
    if len(findings) > 15:
        print(f"  ... {len(findings) - 15} more")
    print()

def fmt_hard_dup(f):
    return (f"{f['c']:>3}x  {f['ticker']:6}  first={f['first_seen'][:16]}  "
            f"last={f['last_seen'][:16]}  {(f['raw_headline'] or '')[:55]}")

def fmt_cross_source(f):
    return (f"{f['rows']:>3}r  {f['ticker']:6}  "
            f"sources={','.join(f['sources']):20.20}  {f['headline']}")

def fmt_paraphrase(f):
    return (f"J={f['jaccard']:.2f}  {f['ticker']:6}  "
            f"a:{f['headline_a'][:35]}  b:{f['headline_b'][:35]}")


# ── Cmd portal post ──────────────────────────────────────────────────────

def post_to_cmd_portal(payload):
    """POST findings to the auditor on pi4b. Best-effort; failures are
    logged but don't change exit code."""
    try:
        import requests
        token = os.environ.get('MONITOR_TOKEN', os.environ.get('SECRET_TOKEN', ''))
        if not token:
            print("[POST] MONITOR_TOKEN/SECRET_TOKEN not set — skipping post")
            return
        url = os.environ.get('CMD_PORTAL_URL', 'http://pi4b:5002') + '/api/auditor/news-dedup'
        r = requests.post(url, json=payload, timeout=5,
                          headers={'Authorization': f'Bearer {token}'})
        if r.status_code == 200:
            print(f"[POST] forwarded {sum(len(v) for v in payload.values() if isinstance(v, list))} findings to {url}")
        else:
            print(f"[POST] {url} returned HTTP {r.status_code}")
    except Exception as e:
        print(f"[POST] failed: {e}")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--hours', type=int, default=24,
                        help='Window in hours for hard-dup scan (default 24)')
    parser.add_argument('--cross-hours', type=int, default=12,
                        help='Window for cross-source + paraphrase scans (default 12)')
    parser.add_argument('--jaccard', type=float, default=0.80,
                        help='Paraphrase Jaccard threshold (default 0.80)')
    parser.add_argument('--post', action='store_true',
                        help='Post findings to cmd portal /api/auditor/news-dedup')
    args = parser.parse_args()

    if not os.path.exists(SHARED_DB):
        print(f"ERROR: shared DB does not exist at {SHARED_DB}", file=sys.stderr)
        return 2

    db = sqlite3.connect(SHARED_DB, timeout=30)
    db.row_factory = sqlite3.Row

    hard       = scan_hard_dups   (db, args.hours)
    cross      = scan_cross_source(db, args.cross_hours)
    paraphrase = scan_paraphrase  (db, args.cross_hours, args.jaccard)

    total = db.execute(
        f"SELECT COUNT(*) FROM news_feed WHERE created_at >= datetime('now', '-{args.hours} hours')"
    ).fetchone()[0]

    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f"== news_dedup_scanner @ {ts} ==")
    print(f"  shared DB    : {SHARED_DB}")
    print(f"  rows in last {args.hours}h: {total}")
    print()
    print_section(f"HARD_DUP (last {args.hours}h)",       hard,       fmt_hard_dup)
    print_section(f"CROSS_SOURCE_CORROBORATION (last {args.cross_hours}h)", cross, fmt_cross_source)
    print_section(f"PARAPHRASE_SUSPECT  Jaccard>={args.jaccard} (last {args.cross_hours}h)",
                  paraphrase, fmt_paraphrase)

    db.close()

    if args.post:
        post_to_cmd_portal({
            'scanned_at': ts,
            'window_hours_hard': args.hours,
            'window_hours_cross': args.cross_hours,
            'rows_scanned': total,
            'hard_dup_count': len(hard),
            'cross_source_count': len(cross),
            'paraphrase_count': len(paraphrase),
            'hard_dup': hard[:50],
            'cross_source': cross[:50],
            'paraphrase': paraphrase[:50],
        })

    # Exit non-zero only on HARD_DUP — that's the bug-detector signal.
    return 1 if hard else 0


if __name__ == '__main__':
    sys.exit(main())
