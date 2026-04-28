"""
agents/news/cross_source_dedup.py — Stage 4 E
==============================================

Pre-pipeline pass that clusters items reporting the same event across
different sources, picks one primary per cluster, and enriches the
primary's metadata with corroboration info.

Why this is needed
------------------
With EDGAR ingestion enabled alongside Alpaca news, the same event
flows from multiple sources:

  Apple Q3 earnings:
    * 8-K Item 2.02 (edgar_8k)
    * Benzinga "Apple Reports Q3 EPS Beat" (alpaca_news)
    * Bloomberg recap aggregated by Alpaca (alpaca_news)

Without dedup, each runs through the 22-gate pipeline independently
→ triple-counted in signal volume, sentiment, and confirmation
counters. The signal is real; the count of it is inflated.

Existing dedup is insufficient
------------------------------
* Gate 8 (NOVELTY): Jaccard threshold is calibrated for SAME-source
  repeats (a Benzinga repost of its own article). Cross-source
  paraphrases SEC "Form 4: Cook sold $1.5M of AAPL" vs Benzinga
  "Apple CEO Sells $1.5M Worth of Stock" Jaccard ~0.20-0.30, well
  under the 0.55 cutoff.
* DB UNIQUE INDEX on news_feed: (ticker, raw_headline, date) only
  blocks IDENTICAL headlines.

How this works
--------------
1. Bucket items by ticker (cross-ticker dedup is by definition wrong).
2. Within each ticker bucket, cluster items where:
   - filing/published timestamps are within `time_window_minutes`
   - headline Jaccard similarity ≥ `jaccard_threshold`
   - sources are different (same-source repeats stay for gate 8 to
     handle)
3. For each cluster, pick a primary:
   - highest source_tier (1 beats 2/3 — SEC primary docs win)
   - ties broken by SOURCE_PREFERENCE order
   - within same source: earliest disc_date
4. Merge cluster info into primary metadata:
     corroboration_count: int
     corroborating_sources: list[str]
     corroborating_headlines: list[str]
5. Items without tickers pass through unchanged.

What this does NOT do
---------------------
* Does NOT promote Gate 12 (SKIP_CONFIRMATION). That gate is the
  natural consumer of corroboration_count, but using the data is a
  separate decision after we've watched the dedup output for a week.
  Gate 12 stays SKIP for now.
* Does NOT modify the original items beyond metadata enrichment.
  The primary keeps its headline, ticker, all original fields.
* Does NOT cross-cluster between tickers. An 8-K affecting AAPL and
  an article affecting MSFT stay independent even with similar text.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional


log = logging.getLogger("cross_source_dedup")


# Source preference within same source_tier — lower index = higher
# precedence. SEC primary documents beat news-aggregator coverage of
# the same event, even at the same tier number.
SOURCE_PREFERENCE = [
    "edgar_form4",      # Direct insider tx, SEC primary, tier 1
    "edgar_13d",        # Activist position, SEC primary, tier 1
    "edgar_form144",    # Insider intent, SEC primary, tier 2
    "edgar_8k",         # Corporate disclosure, SEC primary, tier 2
    "edgar_13g",        # Passive crossing, SEC primary, tier 2
    # All Alpaca / Benzinga / wire sources rank below SEC primaries
    # at the same tier.  They land in DEFAULT_PRIORITY below.
]
DEFAULT_PRIORITY = len(SOURCE_PREFERENCE) + 1


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Configurable thresholds.  Conservative defaults — wide time window
# (events are often hours apart on aggregator delay), Jaccard at 0.40
# (looser than gate-8's 0.55 to catch cross-source paraphrases).
DEFAULT_TIME_WINDOW_MINUTES = _env_int  ("CROSS_SOURCE_DEDUP_WINDOW_MIN", 60)
DEFAULT_JACCARD_THRESHOLD   = _env_float("CROSS_SOURCE_JACCARD",         0.40)


# ── Helpers ──────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase + strip non-alphanumeric, mirrors the news_agent's own
    normalization in fetch_and_store_alpaca_display_news."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity. Same shape as news_agent uses."""
    if not a or not b:
        return 0.0
    ta = set(_normalize(a).split())
    tb = set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _parse_iso_date(s: str) -> Optional[datetime]:
    """Lenient parse — handles YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, ISO with
    timezone, or empty.  Returns None on parse failure."""
    if not s:
        return None
    # Strip timezone for easier comparison; we only need ordering / window
    s = s.strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 6], fmt)  # buffer for fractional secs
        except ValueError:
            continue
    return None


def _item_timestamp(item: dict) -> Optional[datetime]:
    """Best-effort timestamp for time-window grouping.  Falls back from
    explicit timestamp → disc_date → tx_date → None."""
    for field in ("timestamp", "created_at", "disc_date", "tx_date"):
        ts = _parse_iso_date(item.get(field, ""))
        if ts is not None:
            return ts
    return None


def _within_time_window(a: dict, b: dict, window_minutes: int) -> bool:
    """Two items are time-window candidates if both timestamps parse and
    differ by ≤ window_minutes.  If either timestamp is missing, fall
    open (treat as candidates) — we'd rather over-cluster than miss
    cross-source dups when timestamps are sparse."""
    ta = _item_timestamp(a)
    tb = _item_timestamp(b)
    if ta is None or tb is None:
        return True
    delta_minutes = abs((ta - tb).total_seconds()) / 60.0
    return delta_minutes <= window_minutes


def _source_priority(source: str) -> int:
    """Lower priority value = higher precedence."""
    try:
        return SOURCE_PREFERENCE.index(source)
    except ValueError:
        return DEFAULT_PRIORITY


def _pick_primary(cluster: list[dict]) -> dict:
    """Sort cluster by:
       1. Lowest source_tier (1 beats 2/3)
       2. Lowest SOURCE_PREFERENCE index
       3. Earliest disc_date / fallback timestamp (string compare since
          ISO YYYY-MM-DD sorts correctly)
    Returns the first item.  Cluster is non-empty by precondition."""
    def sort_key(item: dict):
        tier = item.get("source_tier", 99)
        src  = item.get("source", "")
        prio = _source_priority(src)
        when = item.get("disc_date") or item.get("tx_date") or ""
        return (tier, prio, when)
    return sorted(cluster, key=sort_key)[0]


# ── Clustering ───────────────────────────────────────────────────────────

def _cluster_for_ticker(items_for_ticker: list[dict],
                        time_window_minutes: int,
                        jaccard_threshold: float) -> list[list[dict]]:
    """Given a list of items all sharing one ticker, return a list of
    clusters.  Each item belongs to exactly one cluster.  Singletons
    are valid clusters (length 1)."""
    clusters: list[list[dict]] = []
    used: set[int] = set()

    for i, item_a in enumerate(items_for_ticker):
        if i in used:
            continue
        cluster = [item_a]
        used.add(i)
        for j in range(i + 1, len(items_for_ticker)):
            if j in used:
                continue
            item_b = items_for_ticker[j]
            # Same-source items don't cluster — that's gate 8's job
            if item_a.get("source") == item_b.get("source"):
                continue
            if not _within_time_window(item_a, item_b, time_window_minutes):
                continue
            sim = _jaccard(item_a.get("headline", ""),
                           item_b.get("headline", ""))
            if sim >= jaccard_threshold:
                cluster.append(item_b)
                used.add(j)
        clusters.append(cluster)
    return clusters


# ── Public API ───────────────────────────────────────────────────────────

def cluster_and_pick_primary(
    items: list[dict],
    time_window_minutes: Optional[int] = None,
    jaccard_threshold: Optional[float] = None,
) -> list[dict]:
    """Main entry point.  Returns a deduped list where each cross-source
    cluster is represented by one primary, with corroboration info in
    metadata.

    Args:
        items: raw items as produced by fetch functions
               (Alpaca, EDGAR Form 4, 8-K, 13D, Form 144, 13G)
        time_window_minutes: optional override; defaults to env var
        jaccard_threshold:   optional override; defaults to env var

    Returns: list with len() ≤ len(items).  Items without tickers pass
    through unchanged.  Single-source items pass through unchanged.
    """
    if not items:
        return []
    window = time_window_minutes if time_window_minutes is not None \
                                  else DEFAULT_TIME_WINDOW_MINUTES
    threshold = jaccard_threshold if jaccard_threshold is not None \
                                   else DEFAULT_JACCARD_THRESHOLD

    # Bucket by ticker; tickerless items pass through as-is.
    tickerless: list[dict] = []
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        ticker = (it.get("ticker") or "").upper()
        if ticker:
            by_ticker[ticker].append(it)
        else:
            tickerless.append(it)

    deduped: list[dict] = []
    merge_count = 0
    for ticker, ticker_items in by_ticker.items():
        clusters = _cluster_for_ticker(ticker_items, window, threshold)
        for cluster in clusters:
            if len(cluster) == 1:
                # Singleton — pass through as-is, but record a
                # corroboration_count of 1 so all items have the field
                # downstream (cleaner gate-12 logic later).
                primary = cluster[0]
                _ensure_metadata(primary)
                primary["metadata"].setdefault("corroboration_count", 1)
                primary["metadata"].setdefault("corroborating_sources", [])
                primary["metadata"].setdefault("corroborating_headlines", [])
                deduped.append(primary)
                continue

            primary = _pick_primary(cluster)
            secondaries = [it for it in cluster if it is not primary]
            _ensure_metadata(primary)
            primary["metadata"]["corroboration_count"] = len(cluster)
            primary["metadata"]["corroborating_sources"] = sorted({
                s.get("source", "") for s in secondaries if s.get("source")
            })
            primary["metadata"]["corroborating_headlines"] = [
                s.get("headline", "") for s in secondaries
            ]
            deduped.append(primary)
            merge_count += len(secondaries)
            log.info(
                f"[X-DEDUP] merged {len(secondaries)} secondaries into "
                f"primary {primary.get('source')} for {ticker}: "
                f"{(primary.get('headline') or '')[:60]!r}"
            )

    if tickerless:
        for it in tickerless:
            _ensure_metadata(it)
            it["metadata"].setdefault("corroboration_count", 1)
            it["metadata"].setdefault("corroborating_sources", [])
            it["metadata"].setdefault("corroborating_headlines", [])
        deduped.extend(tickerless)

    log.info(
        f"[X-DEDUP] {len(items)} items → {len(deduped)} primaries "
        f"({merge_count} merged into corroborators) "
        f"[window={window}m jaccard={threshold:.2f}]"
    )
    return deduped


def _ensure_metadata(item: dict) -> None:
    """Coerce item['metadata'] to a dict in place — items from various
    fetchers may have None / missing."""
    if not isinstance(item.get("metadata"), dict):
        item["metadata"] = {}
