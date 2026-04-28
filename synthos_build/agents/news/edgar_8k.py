"""
agents/news/edgar_8k.py — 8-K item-code filtered ingestion
===========================================================

8-K is the SEC's "current report" form — companies file when something
material happens. Items code what kind of event, e.g.:
  * Item 2.02 — Results of Operations and Financial Condition (earnings)
  * Item 5.02 — Departure / Election of Directors or Certain Officers
  * Item 1.03 — Bankruptcy or Receivership
  * Item 8.01 — Other Events (catch-all; pre-announcements, strategic shifts)
  * Item 1.01 — Material Definitive Agreement (M&A signing, big partnerships)
  * Item 2.05 — Costs Associated with Exit or Disposal Activities (layoffs)
  * Item 7.01 — Regulation FD Disclosure (selective-disclosure remedies)

Strategy
--------
v1 reads from EDGAR full-text search hits only (no filing-body fetch).
Each search hit includes the items array, ticker(s), and filer metadata.
We synthesize a headline from item code descriptions; the news_agent's
22-gate pipeline handles dedup against Alpaca news (Gate 8 NOVELTY's
Jaccard already catches headlines that Benzinga published earlier).

This keeps EDGAR rate-limit usage low (one search call per cycle vs
N filing fetches) and shifts the "is this novel?" decision to the
existing pipeline rather than reinventing it here.

Item-code filter
----------------
Configurable via env `EDGAR_8K_ITEMS` (comma-separated). Default set
is the 4 items the backlog spec calls out as highest signal-to-noise.

Output dict (matches the gate-pipeline `item` shape)
----------------------------------------------------
{
    "headline":     "TSLA 8-K: Item 2.02 — Earnings Results",
    "ticker":       "TSLA",
    "source":       "edgar_8k",
    "source_tier":  2,
    "tx_date":      "2026-04-25",
    "disc_date":    "2026-04-25",
    "all_symbols":  ["TSLA"],
    "metadata":     {... item codes, accession, filer name ...},
}
"""
from __future__ import annotations

import logging
import os
from typing import Optional


log = logging.getLogger("edgar_8k")


ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.02": "Earnings Results",
    "2.05": "Exit / Disposal Costs",
    "5.02": "Officer Change",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
}


def _interesting_items() -> frozenset[str]:
    """Return the set of 8-K item codes we surface as signals.
    Override via env var EDGAR_8K_ITEMS (comma-separated)."""
    raw = os.environ.get("EDGAR_8K_ITEMS", "").strip()
    if not raw:
        return frozenset({"1.03", "2.02", "5.02", "8.01"})
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _headline_for_8k(ticker: str, items: list[str]) -> str:
    parts = []
    for code in items:
        desc = ITEM_DESCRIPTIONS.get(code, f"Item {code}")
        parts.append(f"Item {code} — {desc}")
    summary = "; ".join(parts) if parts else "(no recognized items)"
    return f"{ticker} 8-K: {summary}"


def fetch_8k_signals(client, since_days: int = 2,
                     max_filings: int = 200) -> list[dict]:
    """Search EDGAR for recent 8-Ks, filter by item code, emit pipeline items.

    Args:
        client:        EdgarClient instance.
        since_days:    Look-back window.
        max_filings:   Max search hits to consider. 8-K volume is ~3-5k
                       per business day across all tickers, so 200 over
                       a 2-day window covers most named issuers.

    Returns: list of pipeline items, one per filing whose item codes
    intersect the configured INTERESTING_ITEMS set.  Empty list on
    EDGAR failure or no relevant filings.
    """
    interesting = _interesting_items()
    if not interesting:
        log.info("8-K item filter empty (env-disabled) — skipping fetch")
        return []

    hits = client.search_filings(form_type="8-K", since_days=since_days,
                                 max_results=max_filings)
    if not hits:
        log.info("8-K search returned 0 hits")
        return []

    items_out: list[dict] = []
    for hit in hits:
        raw = hit.get("raw_hit") or {}
        src = raw.get("_source") or {}
        item_codes = src.get("items") or []
        if not isinstance(item_codes, list):
            continue
        relevant = [c for c in item_codes if c in interesting]
        if not relevant:
            continue

        tickers = hit.get("tickers") or []
        if not tickers:
            # Some 8-K filings don't carry a ticker in the search hit
            # (private subsidiaries, recent IPOs).  Skip — the gate
            # pipeline needs a ticker to attribute a signal.
            continue
        ticker = tickers[0].upper()

        # Build amount_range field as the count of relevant items, since
        # an 8-K has no monetary amount per se. The gate pipeline tolerates
        # this field being arbitrary text.
        amount_field = (f"{len(relevant)} item" + ("s" if len(relevant) > 1 else ""))

        items_out.append({
            "headline":     _headline_for_8k(ticker, relevant),
            "ticker":       ticker,
            "source":       "edgar_8k",
            "source_tier":  2,
            "tx_date":      hit.get("filed_date", ""),
            "disc_date":    hit.get("filed_date", ""),
            "amount_range": amount_field,
            "all_symbols":  [ticker],
            "is_amended":   "/A" in (hit.get("form") or ""),
            "is_spousal":   False,
            "metadata": {
                "accession":    hit.get("accession"),
                "filer_name":   hit.get("filer_name"),
                "filer_cik":    hit.get("filer_cik"),
                "items":        relevant,
                "all_items":    item_codes,   # before filter, for trace
                "primary_doc":  hit.get("primary_doc"),
                "primary_doc_url": hit.get("primary_doc_url"),
                "image_url":    "",
            },
        })
    log.info(f"8-K fetch: {len(hits)} filings → {len(items_out)} signal items "
             f"(item filter: {sorted(interesting)})")
    return items_out
