"""
agents/news/edgar_form144.py — Form 144 (proposed sale) ingestion (Stage 3)
============================================================================

Form 144 is an INTENT to sell restricted shares — filed by insiders who
plan to sell more than 5,000 shares OR $50,000 worth of stock in any
3-month period.  Under Rule 144 of the Securities Act.

Form 144 vs Form 4
------------------
Form 144 = "I plan to sell" (filed BEFORE the sale)
Form 4   = "I sold" (filed AFTER the sale, within 2 business days)

Same insider, same stock, same trade: Form 144 fires first (sometimes
days earlier).  Tradeoffs:

  * Pro:  earlier signal — see the intent before the print
  * Con:  weaker signal — most modern 144s are 10b5-1 plan executions
          where the decision was made months earlier; the timing is
          mechanical, not a current-information move
  * Con:  some 144s never result in actual sales (the insider files
          the notice but ultimately doesn't sell)

Net effect: lower tier than Form 4 (source_tier=2 vs 1).

Filter
------
Two-stage:

  1. USD threshold (default $50k, env-tunable EDGAR_FORM144_MIN_USD).
     Cuts noise from small token sales.

  2. Relationship filter — only surface filings where the relationship
     to issuer is one of {Officer, Director, 10% Owner}.  Skips
     "Affiliate", "Other", trust filings — those are typically family
     trusts and estate planning, not insider-conviction signals.

Schema notes (XML)
------------------
Form 144 was electronified around 2022.  XML schema (typical):
  * <edgarSubmission><headerData> — filer info
  * <formData><issuerInfo><issuerName>
  * <formData><issuerInfo><issuerTradingSymbol>  (sometimes empty —
                              we fall back to the search-hit tickers)
  * <formData><securitiesInformation><securitiesClassTitle>
  * <formData><securitiesInformation><securitiesAmount>
  * <formData><securitiesInformation><aggregateMarketValue>
  * <formData><securitiesInformation><approximateDateOfSale>
  * <formData><securitiesInformation><relationshipToIssuer>

The parser is permissive — if the SEC schema differs in production
from these assumed paths, the parser falls through to an empty result
and logs a warning rather than throwing.  That's a known Stage 4
polish task: validate against live filings and tighten parsing.

Output dict (matches the gate-pipeline `item` shape)
----------------------------------------------------
{
    "headline":     "Form 144: <Filer> (<Role>) plans to sell $X of TICKER",
    "ticker":       "TICKER",
    "source":       "edgar_form144",
    "source_tier":  2,
    "politician":   "<Filer Name>",
    "tx_date":      "2026-04-25",   # approximate sale date
    "disc_date":    "2026-04-25",   # filing date
    "amount_range": "$1.5M",
    "all_symbols":  ["TICKER"],
    "metadata":     {... raw filing details ...},
}
"""
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from typing import Optional


log = logging.getLogger("edgar_form144")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Configurable threshold (default $50k mirrors Form 4)
MIN_TX_VALUE_USD = _env_float("EDGAR_FORM144_MIN_USD", 50_000.0)

# Relationships we consider insider-conviction signals.  Lowercased
# for case-insensitive match against the form's relationship field.
INSIDER_RELATIONSHIPS = frozenset({
    "officer", "director", "officer/director",
    "10% owner", "ten percent owner",
    "officer & director",
})


def _text(elem: Optional[ET.Element], path: str, default: str = "") -> str:
    if elem is None:
        return default
    found = elem.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _to_float(s: str) -> Optional[float]:
    s = (s or "").replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_amount(value_usd: float) -> str:
    if value_usd >= 1_000_000:
        return f"${value_usd / 1_000_000:.1f}M"
    if value_usd >= 1_000:
        return f"${value_usd / 1_000:.0f}K"
    return f"${value_usd:.0f}"


def _normalize_relationship(raw: str) -> str:
    return (raw or "").lower().strip()


def parse_form144(xml_text: str, ticker_from_hit: str = "",
                  filed_date: str = "") -> list[dict]:
    """Parse a Form 144 XML body.  Returns 0 or 1 item (Form 144 is
    one filing per intent-to-sell event).

    Args:
        xml_text:         The raw XML.
        ticker_from_hit:  Fallback ticker from EDGAR search hit (some
                          Form 144 XMLs have an empty issuerTradingSymbol).
        filed_date:       YYYY-MM-DD from the search hit.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning(f"form144 XML parse failed: {e}")
        return []

    # Form 144 nests issuer info under formData; older forms may use
    # different paths. Try the modern path first.
    issuer = root.find(".//issuerInfo") or root.find("issuerInfo")
    issuer_name   = _text(issuer, "issuerName")
    issuer_ticker = _text(issuer, "issuerTradingSymbol")
    if not issuer_ticker and ticker_from_hit:
        issuer_ticker = ticker_from_hit
    if not issuer_ticker:
        return []  # No ticker — can't attribute

    # Filer information
    filer_name = (
        _text(root, ".//filerInfo/filerName")
        or _text(root, ".//headerData/filerName")
        or _text(root, ".//filer/name")
        or "Unknown filer"
    )

    # Securities-to-be-sold info
    sec = root.find(".//securitiesInformation") or root.find(".//securitiesToBeSold")
    if sec is None:
        return []

    aggregate_value = _to_float(_text(sec, "aggregateMarketValue"))
    if aggregate_value is None:
        # Fall back to shares × price if present
        shares = _to_float(_text(sec, "securitiesAmount"))
        ppx    = _to_float(_text(sec, "approximatePricePerShare"))
        if shares is not None and ppx is not None:
            aggregate_value = shares * ppx
    if aggregate_value is None or aggregate_value < MIN_TX_VALUE_USD:
        return []

    relationship_raw = _text(sec, "relationshipToIssuer") or _text(root, ".//relationshipToIssuer")
    relationship_norm = _normalize_relationship(relationship_raw)
    # Relationship can contain multiple roles separated by '/' or ',';
    # match if ANY component is in our insider set.
    parts = [p.strip() for p in relationship_norm.replace(",", "/").split("/") if p.strip()]
    if not any(p in INSIDER_RELATIONSHIPS or
               any(insider == p for insider in INSIDER_RELATIONSHIPS)
               for p in parts):
        # Not an insider relationship — skip
        return []

    approx_sale = _text(sec, "approximateDateOfSale")
    securities_class = _text(sec, "securitiesClassTitle") or "Common Stock"

    role_label = relationship_raw or "Insider"

    headline = (
        f"Form 144: {filer_name} ({role_label}) plans to sell "
        f"{_format_amount(aggregate_value)} of {issuer_ticker.upper()}"
    )

    return [{
        "headline":     headline,
        "ticker":       issuer_ticker.upper(),
        "source":       "edgar_form144",
        "source_tier":  2,
        "politician":   filer_name,
        "tx_date":      approx_sale or filed_date,
        "disc_date":    filed_date or approx_sale,
        "amount_range": _format_amount(aggregate_value),
        "all_symbols":  [issuer_ticker.upper()],
        "is_amended":   False,
        "is_spousal":   False,
        "metadata": {
            "issuer_name":      issuer_name,
            "filer_role":       role_label,
            "relationship":     relationship_raw,
            "securities_class": securities_class,
            "aggregate_value_usd": round(aggregate_value, 2),
            "approximate_sale_date": approx_sale,
            "image_url":        "",
        },
    }]


def fetch_form144_signals(client, since_days: int = 2,
                          max_filings: int = 100) -> list[dict]:
    """Search EDGAR for recent Form 144 filings, fetch + parse, return
    pipeline items that pass the threshold + relationship filters.

    Args:
        client:        EdgarClient instance.
        since_days:    Look-back window.
        max_filings:   Cap on raw search hits.  Form 144 volume is
                       higher than Form 4 — a 2-day window can pull
                       hundreds across all tickers.  Most will be
                       filtered out by the threshold + relationship
                       checks.

    Returns: list of pipeline items.  Empty if EDGAR fails or no
    filings clear the filters.
    """
    hits = client.search_filings(form_type="144", since_days=since_days,
                                 max_results=max_filings)
    if not hits:
        log.info("form144 search returned 0 hits")
        return []

    items: list[dict] = []
    for hit in hits:
        url = hit.get("primary_doc_url")
        if not url or not url.endswith(".xml"):
            continue
        body = client.fetch_url(url)
        if not body:
            continue
        ticker_hint = ""
        tk = hit.get("tickers") or []
        if tk:
            ticker_hint = tk[0]
        try:
            parsed = parse_form144(body,
                                   ticker_from_hit=ticker_hint,
                                   filed_date=hit.get("filed_date", ""))
        except Exception as e:
            log.warning(f"form144 parse error on {url[:80]}: {e}")
            continue
        items.extend(parsed)
    log.info(f"form144 fetch: {len(hits)} filings → {len(items)} signal items "
             f"(threshold ${MIN_TX_VALUE_USD:,.0f}, "
             f"relationship: {sorted(INSIDER_RELATIONSHIPS)})")
    return items
