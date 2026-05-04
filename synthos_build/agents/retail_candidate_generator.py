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

from retail_database import get_customer_db, get_shared_db  # noqa: E402

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s candidate_gen: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('candidate_gen')

_OWNER_CID = os.environ.get('OWNER_CUSTOMER_ID', '30eff008-c27a-4c71-a788-05f883e4e3a0')

# 2026-04-21 — filter changed from combined_score (news+sent+weight) to
# momentum_score (ret_3m + SMA + volume) because:
#   - combined_score maxes around 0.66 and misses the core "sector-driven"
#     signal (zero weight on sector momentum itself)
#   - momentum_score directly measures what sector_screener is designed
#     to surface — confirmed uptrend with structure
# See docs/audit/AUDIT_2026-04-21*.md for the trace that drove this.
#
# MIN_MOMENTUM_SCORE = 0.45 is deliberately permissive for initial rollout
# (paper-only, low risk). At this threshold ~76 tickers pass, but the
# MAX_PER_SECTOR=3 + MAX_TOTAL=30 caps squeeze emission to ~30 real
# candidates. Observed today: 10 at 0.90+, 13 at 0.75-0.89, 6 at
# 0.60-0.74, 1 at 0.45-0.59. All 11 sectors represented.
MIN_MOMENTUM_SCORE  = 0.45
# Legacy name kept for any backward-compat callers; unused internally now.
MIN_CANDIDATE_SCORE = MIN_MOMENTUM_SCORE
MAX_PER_SECTOR      = 3       # top-N per sector to emit as candidates
MAX_TOTAL           = 30      # global cap per run


def _shared_db():
    """Shared market-intel DB.
    2026-04-27: was previously get_customer_db(OWNER_CUSTOMER_ID).  See
    retail_database.get_shared_db() for the architectural rationale."""
    return get_shared_db()


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
    MAX_PER_SECTOR per sector, filtered + ranked by momentum_score.

    2026-04-21: filter moved from combined_score to momentum_score. The
    combined_score column is still populated (by news + sentiment agents
    fulfilling screening_requests) and remains available for portal
    display + informational context, but it no longer gates emission.
    """
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT sector, ticker, momentum_score, combined_score, congressional_flag
            FROM (
                SELECT sector, ticker, momentum_score, combined_score, congressional_flag,
                       ROW_NUMBER() OVER (
                           PARTITION BY sector
                           ORDER BY momentum_score DESC
                       ) AS sector_rank
                FROM sector_screening
                WHERE run_id = ?
                  AND momentum_score >= ?
                  AND ticker IS NOT NULL
            )
            WHERE sector_rank <= ?
            ORDER BY momentum_score DESC
            LIMIT ?
            """,
            (run_id, MIN_MOMENTUM_SCORE, MAX_PER_SECTOR, MAX_TOTAL),
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

    # Audit Round 5 — filter out un-tradable tickers before emission.
    # Uses Alpaca's /v2/assets cache (refreshed daily by market_daemon).
    # Unknown tickers (no cache row) pass through — block-on-None would
    # be too aggressive until the first cache refresh has happened.
    try:
        from retail_tradable_cache import is_tradable  # noqa: E402
        _before = len(candidates)
        candidates = [c for c in candidates
                      if is_tradable(db, c['ticker']) is not False]
        _dropped = _before - len(candidates)
        if _dropped:
            log.info(f"Tradable-cache filtered {_dropped} un-tradable ticker(s)")
    except Exception as _e:
        log.debug(f"tradable filter skipped: {_e}")

    emitted = 0
    updated = 0
    for c in candidates:
        try:
            # momentum_score is the driver (what we selected on). Pass it
            # as the "score" on add_candidate_signal so entry_signal_score
            # reflects the actual selection reason (price-action momentum,
            # not news+sentiment mix). combined_score (may be None if
            # news/sentiment haven't fulfilled screening_requests yet)
            # shows up in the headline when present as supplementary info.
            mom = float(c['momentum_score']) if c.get('momentum_score') is not None else 0.0
            cs  = c.get('combined_score')
            cs_tag = f", news+sent={cs:.2f}" if cs is not None else ""
            new_id = db.add_candidate_signal(
                ticker=c['ticker'],
                combined_score=mom,  # primary selection score (name kept for API compat)
                sector=c['sector'],
                headline=(f"sector-driven candidate "
                          f"({c['sector']}, momentum={mom:.2f}{cs_tag}"
                          f"{', '+c['congressional_flag'] if c['congressional_flag'] and c['congressional_flag']!='none' else ''})"),
            )
            if new_id is None:
                updated += 1
            else:
                emitted += 1
                log.info(
                    f"  + {c['ticker']:6s} sector={c['sector']:20s} "
                    f"momentum={mom:.3f} flag={c.get('congressional_flag','-')}"
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
    # 2026-05-04 — MQTT heartbeat (Tier 4 of distributed-trader migration).
    # Publishes to process/heartbeat/<node>/<agent>. No-op if broker is
    # unreachable; cleanup auto-registered via atexit. Strictly additive
    # to existing retail_heartbeat.py / node_heartbeat.py mechanisms.
    try:
        from heartbeat import register_telemetry as _register_telemetry
        _register_telemetry('candidate_generator', long_running=False)
    except Exception as _hb_e:
        # Silent: telemetry must never block an agent from starting.
        pass

    t0 = datetime.now(ET)
    summary = run()
    dt = (datetime.now(ET) - t0).total_seconds()
    log.info(
        f"[CANDIDATE GEN] considered={summary['considered']} "
        f"emitted={summary['emitted']} updated={summary['updated']} "
        f"run_id={(summary['run_id'] or '-')[:8]} in {dt:.1f}s"
    )
