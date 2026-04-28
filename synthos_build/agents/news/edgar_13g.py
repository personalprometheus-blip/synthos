"""
agents/news/edgar_13g.py — 13G passive-crossing ingestion (Stage 3)
====================================================================

13G is filed when an investor crosses 5% ownership PASSIVELY (no intent
to influence).  Companion to 13D.  Filed under either Schedule 13G
(initial) or 13G/A (amendment).

Why 13G is lower-signal than 13D
--------------------------------
By definition, 13G is the passive form.  An institutional holder who
crosses 5% but has no intent to actively shape company strategy files
13G instead of 13D.  Vanguard, BlackRock, State Street all file
hundreds of 13Gs per quarter — that's pure index-fund signal.

But: when a *known activist* (per the registry) files 13G instead of
13D, that's an interesting signal.  Could mean:
  * They're accumulating quietly before declaring activist intent
  * They've actually decided this position is passive (rare)
  * They're using 13G as a stepping stone — common pattern is 13G first,
    then convert to 13D later when ready to publicly campaign

Reusing the activist registry
-----------------------------
Same JSON file as 13D (synthos_build/data/activists.json).  An empty
registry means 13G fetching produces zero signals — same safe default
as 13D.

Tier 2, not 1, because:
  * Form is by definition passive — even a known activist filing 13G
    is signaling "I'm not (currently) activist on this name"
  * Higher false-positive rate than 13D (legitimate passive accumulation
    by an activist working on multiple positions)

Output dict
-----------
Same shape as 13D output, with `source='edgar_13g'`, `source_tier=2`.
"""
from __future__ import annotations

import logging


log = logging.getLogger("edgar_13g")


def _build_headline(activist_info: dict, ticker: str, is_amended: bool) -> str:
    name       = activist_info.get("name", "Unknown")
    principals = activist_info.get("principals") or []
    principal  = principals[0] if principals else ""
    suffix     = f" ({principal})" if principal else ""
    form       = "13G/A" if is_amended else "13G"
    verb       = "amended passive ≥5% position" if is_amended else "filed passive ≥5% position"
    return f"{form}: {name}{suffix} {verb} in {ticker}"


def fetch_13g_signals(client, registry, since_days: int = 7,
                     max_filings: int = 100) -> list[dict]:
    """Search EDGAR for recent 13G / 13G/A filings, classify by filer
    CIK against the activist registry, emit pipeline items.

    Args:
        client:        EdgarClient instance.
        registry:      ActivistRegistry instance (lazy-loaded).
        since_days:    Look-back window. 13G volume is much higher
                       than 13D — most are index funds.  Default 7 days.
        max_filings:   Cap on raw search hits.

    Returns: list of pipeline items.  Empty if registry is empty,
    EDGAR fails, or no matching filers in the window.
    """
    if len(registry) == 0:
        log.info("13G fetch skipped — activist registry is empty. "
                 "Populate synthos_build/data/activists.json before enabling.")
        return []

    # Fix D (2026-04-28): try fallback '13G' if 'SC 13G' returns 0 — some
    # EDGAR query paths normalize differently.  See edgar_13d.py for the
    # parallel pattern.
    hits = client.search_filings(form_type="SC 13G", since_days=since_days,
                                 max_results=max_filings)
    if not hits:
        log.info("13G 'SC 13G' search returned 0 — trying fallback '13G'")
        hits = client.search_filings(form_type="13G", since_days=since_days,
                                     max_results=max_filings)
    if not hits:
        log.info("13G search (both query forms) returned 0 hits")
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
            skipped_no_ticker += 1
            continue
        ticker = tickers[0].upper()

        form_str = (hit.get("form") or "").upper()
        is_amended = "/A" in form_str

        items_out.append({
            "headline":     _build_headline(info, ticker, is_amended),
            "ticker":       ticker,
            "source":       "edgar_13g",
            "source_tier":  2,
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
                "form":           "13G/A" if is_amended else "13G",
            },
        })

    log.info(
        f"13G fetch: {len(hits)} raw filings → {len(items_out)} signal items "
        f"(unknown filer: {skipped_unknown}, no-ticker: {skipped_no_ticker}, "
        f"registry size: {len(registry)})"
    )
    return items_out
