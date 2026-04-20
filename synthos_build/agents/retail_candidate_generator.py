#!/usr/bin/env python3
"""
retail_candidate_generator.py — Emit sector-driven candidate signals.

Phase 3b of TRADER_RESTRUCTURE_PLAN. Replaces "news is the trigger" with
"sector momentum generates candidates; news modifies them." Reads the
sector screener's output (sector_screening table) and writes candidate
signals for tickers with strong sector scores.

PHASE 3b SCOPE — DELIBERATELY LIMITED:

  * Read sector_screening rows where combined_score >= MIN_CANDIDATE_SCORE
  * One candidate per ticker per day (dedup in add_candidate_signal())
  * status='WATCHING' — trader doesn't consume in 3b; 3c promotes to
    VALIDATED after wiring gate/window checks.
  * Writes into master signals.db (shared across customers).

PHASE 3c WILL ADD:
  * Validator-stack GO/CAUTION filter per customer
  * Not-already-held filter per customer (reads positions table)
  * Liquidity floor filter (Alpaca avg_volume_30d)
  * Rel-strength × regime-match ranking
  * Candidate promotion to VALIDATED status

RUN CONTEXT:
  Invoked by retail_market_daemon each enrichment tick (~30 min).
  Subprocess-style (consistent with other agents). Cheap — pure DB query
  + write, no external API calls.
"""

import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_DIR / 'src'))

from retail_database import get_customer_db  # noqa: E402

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s candidate_gen: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('candidate_gen')

_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')

MIN_CANDIDATE_SCORE = 0.70    # sector_screening.combined_score threshold
MAX_PER_SECTOR      = 3       # top-N per sector to emit as candidates
MAX_TOTAL           = 30      # global cap per run


def _shared_db():
    """Master/owner customer DB — same pattern as other agents."""
    return get_customer_db(_OWNER_CID)


def _latest_screener_run(db) -> str | None:
    """Return the most recent run_id from sector_screening, or None."""
    with db.conn() as c:
        row = c.execute(
            "SELECT run_id FROM sector_screening "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row['run_id'] if row else None


def _fetch_candidates(db, run_id: str) -> list:
    """
    Pull top-ranking rows from the latest screener run. Grouped by sector,
    MAX_PER_SECTOR per sector, filtered by combined_score threshold.
    """
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT sector, ticker, combined_score, congressional_flag
            FROM (
                SELECT sector, ticker, combined_score, congressional_flag,
                       ROW_NUMBER() OVER (
                           PARTITION BY sector
                           ORDER BY combined_score DESC
                       ) AS sector_rank
                FROM sector_screening
                WHERE run_id = ?
                  AND combined_score >= ?
                  AND ticker IS NOT NULL
            )
            WHERE sector_rank <= ?
            ORDER BY combined_score DESC
            LIMIT ?
            """,
            (run_id, MIN_CANDIDATE_SCORE, MAX_PER_SECTOR, MAX_TOTAL),
        ).fetchall()
    return [dict(r) for r in rows]


def run() -> dict:
    """
    Emit candidate signals for the latest screener output. Returns a
    summary dict: {run_id, considered, emitted, updated, skipped_dup}.
    """
    db = _shared_db()
    run_id = _latest_screener_run(db)
    if not run_id:
        log.info("No sector_screening runs found — nothing to emit")
        return {'run_id': None, 'considered': 0, 'emitted': 0,
                'updated': 0, 'skipped_dup': 0}

    candidates = _fetch_candidates(db, run_id)
    log.info(f"Latest screener run {run_id[:8]} returned {len(candidates)} qualifying rows")

    emitted = 0
    updated = 0
    for c in candidates:
        try:
            new_id = db.add_candidate_signal(
                ticker=c['ticker'],
                combined_score=float(c['combined_score']),
                sector=c['sector'],
                headline=(f"sector-driven candidate "
                          f"({c['sector']}, score={c['combined_score']:.2f}"
                          f"{', '+c['congressional_flag'] if c['congressional_flag'] and c['congressional_flag']!='none' else ''})"),
            )
            if new_id is None:
                updated += 1
            else:
                emitted += 1
                log.info(
                    f"  + {c['ticker']:6s} sector={c['sector']:20s} "
                    f"score={c['combined_score']:.3f} flag={c.get('congressional_flag','-')}"
                )
        except Exception as e:
            log.warning(f"Failed to emit candidate {c.get('ticker')}: {e}")

    return {
        'run_id':      run_id,
        'considered':  len(candidates),
        'emitted':     emitted,
        'updated':     updated,
        'skipped_dup': 0,   # dedup handled inside add_candidate_signal
    }


if __name__ == '__main__':
    t0 = datetime.now(ET)
    summary = run()
    dt = (datetime.now(ET) - t0).total_seconds()
    log.info(
        f"[CANDIDATE GEN] considered={summary['considered']} "
        f"emitted={summary['emitted']} updated={summary['updated']} "
        f"run_id={(summary['run_id'] or '-')[:8]} in {dt:.1f}s"
    )
