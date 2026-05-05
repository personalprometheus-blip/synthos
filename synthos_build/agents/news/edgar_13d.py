"""
agents/news/edgar_13d.py — 13D activist filing ingestion (Stage 2)
==================================================================

13D is filed when an investor crosses 5% ownership *with intent to
influence* (vs the simpler 13G for passive crossings).  When a known
activist files 13D, the target stock routinely moves 5-15% on
disclosure — that's the signal we want.

Volume is very low.  EDGAR returns ~5-20 raw 13D hits/week.  ~95% are
foundations, family offices, and employee benefit plans that just
don't qualify for 13G — those carry no activist signal.  The filter
is the activist registry (agents/news/activist_registry.py): if the
filer CIK isn't known, we skip.

13D vs 13D/A
------------
13D     — initial filing, the moment a position crosses 5% with intent.
13D/A   — amendment.  Often more market-moving than the initial: the
          activist has gone public with demands (board nominations,
          spin-offs, replacement-CEO calls).  We surface both, with
          is_amended=True for /A.

Form    — 13D parsing
---------
Unlike Form 4 (XML), 13D is filed as HTML or PDF.  Headlines vary
widely.  v1 reads only EDGAR search-hit metadata (filer, ticker,
filing date) and constructs a synthetic headline.  Body fetch is a
Stage 4 polish task once we see live volume.

Output dict (matches the gate-pipeline `item` shape)
----------------------------------------------------
{
    "headline":     "13D: Pershing Square (Bill Ackman) filed activist position in TICKER",
    "ticker":       "TICKER",
    "source":       "edgar_13d",
    "source_tier":  1,
    "politician":   "Pershing Square Capital Management",
    "tx_date":      "2026-04-25",
    "disc_date":    "2026-04-25",
    "amount_range": "≥5%",
    "all_symbols":  ["TICKER"],
    "is_amended":   False,
    "metadata":     {... activist tier, principals, accession ...},
}
"""
from __future__ import annotations

import logging
from typing import Optional


log = logging.getLogger("edgar_13d")


def _build_headline(activist_info: dict, ticker: str, is_amended: bool) -> str:
    name       = activist_info.get("name", "Unknown")
    principals = activist_info.get("principals") or []
    principal  = principals[0] if principals else ""
    suffix     = f" ({principal})" if principal else ""
    form       = "13D/A" if is_amended else "13D"
    verb       = "amended activist position" if is_amended else "filed activist position"
    return f"{form}: {name}{suffix} {verb} in {ticker}"


def fetch_13d_signals(
    client,
    registry,
    since_days: int = 7,
    max_filings: int = 50,
) -> list[dict]:
    """Search EDGAR for recent 13D / 13D/A filings, classify by filer CIK,
    emit pipeline items only for filings whose filer is in the activist
    registry.

    Args:
        client:        EdgarClient instance (rate-limited, UA-enforced).
        registry:      ActivistRegistry instance (lazy-loaded).
        since_days:    Look-back window.  13D volume is low; 7-day default
                       catches typical week's flow without overlap.
        max_filings:   Cap on raw search hits.  ~5-20/week real-world.

    Returns: list of pipeline items, one per matched filing.  Empty list
    if EDGAR is unreachable, registry is empty, or no known activists
    filed in the window.
    """
    if len(registry) == 0:
        log.info("13D fetch skipped — activist registry is empty. "
                 "Populate synthos_build/data/activists.json before enabling.")
        return []

    # 2026-05-04: empirical test against efts.sec.gov/LATEST/search-index
    # showed the canonical form name is 'SCHEDULE 13D' (returns hits) NOT
    # 'SC 13D' (returns 0). Earlier 'SC 13D' / '13D' fallbacks were both
    # broken queries — the EDGAR full-text index uses the long-form name.
    # Try SCHEDULE 13D first (covers 13D + 13D/A combined), then the older
    # 'SC 13D' query as legacy fallback in case EDGAR ever changes back.
    hits = client.search_filings(form_type="SCHEDULE 13D", since_days=since_days,
                                 max_results=max_filings)
    if not hits:
        log.info("13D 'SCHEDULE 13D' returned 0 — trying legacy 'SC 13D'")
        hits = client.search_filings(form_type="SC 13D", since_days=since_days,
                                     max_results=max_filings)
    if not hits:
        log.info("13D search returned 0 hits across all query forms")
        return []

    items_out: list[dict] = []
    skipped_unknown = 0
    skipped_no_ticker = 0

    for hit in hits:
        cik = hit.get("filer_cik") or ""
        info = registry.lookup(cik)
        if info is None:
            skipped_unknown += 1
            continue

        tickers = hit.get("tickers") or []
        if not tickers:
            # 13D filings sometimes lack a ticker in the search hit if the
            # target is a recent IPO or a private subsidiary.  Skip — the
            # gate pipeline needs a ticker to attribute a signal.
            skipped_no_ticker += 1
            continue
        ticker = tickers[0].upper()

        # Form field on the hit is "SC 13D" or "SC 13D/A" (or just "13D").
        form_str = (hit.get("form") or "").upper()
        is_amended = "/A" in form_str

        items_out.append({
            "headline":     _build_headline(info, ticker, is_amended),
            "ticker":       ticker,
            "source":       "edgar_13d",
            "source_tier":  1,
            "politician":   info["name"],
            "tx_date":      hit.get("filed_date", ""),
            "disc_date":    hit.get("filed_date", ""),
            "amount_range": "≥5%",
            "all_symbols":  [ticker],
            "is_amended":   is_amended,
            "is_spousal":   False,
            "metadata": {
                "accession":      hit.get("accession"),
                "filer_cik":      info["cik"],
                "filer_name":     info["name"],
                "principals":     info.get("principals", []),
                "activist_tier":  info.get("tier"),
                "activist_notes": info.get("notes", ""),
                "primary_doc_url": hit.get("primary_doc_url"),
                "image_url":      "",
            },
        })

    log.info(
        f"13D fetch: {len(hits)} raw filings → {len(items_out)} signal items "
        f"(unknown filer: {skipped_unknown}, no-ticker: {skipped_no_ticker}, "
        f"registry size: {len(registry)})"
    )
    return items_out
