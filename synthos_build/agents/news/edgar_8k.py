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
v1 reads from EDGAR full-text search hits only.  Each search hit
includes the items array, ticker(s), and filer metadata.  We synthesize
a headline from item code descriptions; the news_agent's 22-gate
pipeline handles dedup against Alpaca news.

Optional body fetch (Stage 4 C, 2026-04-28)
-------------------------------------------
When `fetch_body=True` is passed, we ALSO fetch the filing's primary
HTML doc and extract a brief excerpt of the actual disclosure text
near the relevant item header.  Tradeoffs:

  * Pro: gate-7 sentiment gets real content to score (vs always
         NEUTRAL on the synthetic header-only headline).
  * Pro: trader's signal_attribution / display layer gets a glimpse
         of what the filing actually says.
  * Con: doubles EDGAR HTTP volume per 8-K (search call + body call).
         At default 8 RPS budget this absorbs comfortably (~50s of
         cycle time for a 200-filing pull).
  * Con: HTML parsing is brittle — SEC 8-Ks come in multiple formats
         (HTML, iXBRL-wrapped HTML, occasional PDF).  v1 strips tags
         via regex and finds the item header; if extraction fails or
         returns nothing useful, we FALL BACK to the synthetic
         headline so 8-K ingestion never silently breaks.

Item-code filter
----------------
Configurable via env `EDGAR_8K_ITEMS` (comma-separated). Default set
is the 4 items the backlog spec calls out as highest signal-to-noise.

Output dict (matches the gate-pipeline `item` shape)
----------------------------------------------------
{
    "headline":     "TSLA 8-K: Item 2.02 — Earnings Results"  # synth
                  # or with body_excerpt:
                  # "TSLA 8-K Item 2.02: Q3 revenue of $25.0B, up 8% YoY..."
    "ticker":       "TSLA",
    "source":       "edgar_8k",
    "source_tier":  2,
    "tx_date":      "2026-04-25",
    "disc_date":    "2026-04-25",
    "all_symbols":  ["TSLA"],
    "metadata":     {... item codes, accession, filer name,
                     body_excerpt (when fetch_body=True), ...},
}
"""
from __future__ import annotations

import html as html_lib
import logging
import os
import re
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


def _headline_for_8k(ticker: str, items: list[str], filer_name: str = "") -> str:
    parts = []
    for code in items:
        desc = ITEM_DESCRIPTIONS.get(code, f"Item {code}")
        parts.append(f"Item {code} ({desc})")
    summary = "; ".join(parts) if parts else "(no recognized items)"
    # Include filer name so headlines reliably clear gate1's MIN_WORD_COUNT
    # (8). The shortest possible summary 'Item 1.03 (Bankruptcy or Receivership)'
    # alone with '{ticker} 8-K filed:' prefix is only 7 tokens — adding the
    # filer name pushes it past the floor for every filing.
    name_str = f" by {filer_name}" if filer_name else ""
    return f"{ticker} 8-K filed{name_str}: {summary}"


# ── Body extraction (Stage 4 C, 2026-04-28) ──────────────────────────────

_TAG_RE       = re.compile(r"<[^>]+>", re.DOTALL)
_SCRIPT_RE    = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE     = re.compile(r"<style[^>]*>.*?</style>",   re.IGNORECASE | re.DOTALL)
_WS_RE        = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Best-effort HTML → plain text.  No BeautifulSoup dependency.
    Strips <script> and <style> blocks first so we don't include their
    content as 'text', then strips remaining tags, decodes entities,
    normalizes whitespace.  Returns empty string on falsy input."""
    if not html:
        return ""
    text = _SCRIPT_RE.sub("", html)
    text = _STYLE_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def extract_8k_excerpt(html: str, item_code: str,
                       max_chars: int = 280) -> str:
    """Pull a brief excerpt from an 8-K filing body, anchored on the
    'Item X.XX' header that matches our filter.

    Strategy: strip HTML to plain text, find first occurrence of
    'Item <code>', return next `max_chars` characters of disclosure
    text.  Falls back to first `max_chars` of body if the item header
    isn't found (some 8-K formats wrap items in tables that this
    text-only path doesn't cleanly anchor).

    Returns empty string on any failure — caller is expected to fall
    back to the synthetic header-only headline.

    Stage 4 C v1 — opt-in via fetch_body=True in fetch_8k_signals().
    """
    text = _strip_html(html)
    if not text:
        return ""
    # Match 'Item 2.02', 'Item 2.02.', 'Item 2.02 -', 'Item 2.02:'
    pattern = re.compile(
        r"Item\s+" + re.escape(item_code) + r"\.?\s*[\.\-:]?\s*",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        start = match.end()
        return text[start:start + max_chars].strip()
    # Fallback — return first max_chars of overall body.  Most 8-Ks have
    # the item header somewhere visible, but if extraction missed it
    # (e.g., header inside an image, in a table cell that survived as
    # 'Item 2 . 02' with extra whitespace), the lead paragraph is still
    # better than returning nothing.
    return text[:max_chars].strip()


def fetch_8k_signals(client, since_days: int = 2,
                     max_filings: int = 200,
                     fetch_body: bool = False,
                     body_excerpt_chars: int = 280) -> list[dict]:
    """Search EDGAR for recent 8-Ks, filter by item code, emit pipeline items.

    Args:
        client:              EdgarClient instance.
        since_days:          Look-back window.
        max_filings:         Max search hits to consider. 8-K volume is
                             ~3-5k per business day across all tickers,
                             so 200 over a 2-day window covers most
                             named issuers.
        fetch_body:          Stage 4 C, opt-in via env EDGAR_8K_BODY_FETCH.
                             When True, fetches each filing's primary
                             HTML doc and substitutes a body-excerpt
                             headline.  Falls back to the synthetic
                             header-only headline on any failure.
        body_excerpt_chars:  Max length of body excerpt (default 280).

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
    body_fetched   = 0
    body_extracted = 0
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

        # Default headline: synthetic, header-only.
        synthetic_headline = _headline_for_8k(ticker, relevant,
                                              filer_name=hit.get("filer_name") or "")
        headline           = synthetic_headline
        body_excerpt: str  = ""

        # Optional body fetch + excerpt extraction.
        if fetch_body:
            body_url = hit.get("primary_doc_url")
            if body_url:
                body_fetched += 1
                try:
                    html = client.fetch_url(body_url)
                except Exception as e:
                    log.debug(f"8-K body fetch raised for "
                              f"{hit.get('accession')}: {e}")
                    html = None
                if html:
                    excerpt = extract_8k_excerpt(
                        html, relevant[0],
                        max_chars=body_excerpt_chars,
                    )
                    if excerpt:
                        body_excerpt = excerpt
                        body_extracted += 1
                        # Keep the synthetic prefix for context, then add the
                        # body excerpt so dedup against Benzinga still has a
                        # chance to match on entity/event tokens.
                        headline = (
                            f"{ticker} 8-K Item {relevant[0]}: "
                            f"{excerpt[:body_excerpt_chars - 40]}"
                        )

        # Build amount_range field as the count of relevant items, since
        # an 8-K has no monetary amount per se. The gate pipeline tolerates
        # this field being arbitrary text.
        amount_field = (f"{len(relevant)} item" + ("s" if len(relevant) > 1 else ""))

        items_out.append({
            "headline":     headline,
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
                # Body fetch trace.  Empty when fetch_body=False or
                # extraction failed.
                "body_excerpt":      body_excerpt,
                "synthetic_headline": synthetic_headline,
            },
        })
    if fetch_body:
        log.info(f"8-K fetch: {len(hits)} filings → {len(items_out)} signal "
                 f"items (item filter: {sorted(interesting)}, "
                 f"body fetched: {body_fetched}, "
                 f"body extracted: {body_extracted})")
    else:
        log.info(f"8-K fetch: {len(hits)} filings → {len(items_out)} signal "
                 f"items (item filter: {sorted(interesting)})")
    return items_out
