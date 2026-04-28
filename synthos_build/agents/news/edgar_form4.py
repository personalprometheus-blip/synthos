"""
agents/news/edgar_form4.py — Form 4 (insider transactions) parser + ingestion
=============================================================================

Form 4 = corporate insider (officer / director / 10% owner) reports a
transaction in their company's securities.  Filed within 2 business days
of the transaction. Best real-time public-disclosure insider signal.

Schema (XBRL-like XML)
----------------------
The primary doc is XML.  Key paths:
  * <issuerCik>, <issuerName>, <issuerTradingSymbol>
  * <reportingOwner>/<reportingOwnerId>/<rptOwnerName>
  * <reportingOwner>/<reportingOwnerRelationship>/<isDirector|isOfficer|...>
  * <reportingOwner>/<reportingOwnerRelationship>/<officerTitle>
  * <nonDerivativeTable>/<nonDerivativeTransaction>/...
      transactionCoding/transactionCode (P=open-market buy, S=open-market sell,
                                          A=grant, F=tax withheld, M=option exercise, ...)
      transactionAmounts/transactionShares/value
      transactionAmounts/transactionPricePerShare/value
      transactionAmounts/transactionAcquiredDisposedCode/value (A=acquired, D=disposed)
  * <derivativeTable>/<derivativeTransaction>/... (options)

Filter
------
We keep open-market transactions (codes P and S) over a configurable
USD threshold.  Skip A (grants/awards), F (tax withholdings), G (gifts),
J (other) — these aren't price-discoveryful insider signals.

Output dict (matches the gate-pipeline `item` shape)
----------------------------------------------------
{
    "headline":     "<Filer Title> <Filer Name> bought $X of TICKER",
    "ticker":       "AAPL",
    "source":       "edgar_form4",
    "source_tier":  1,
    "politician":   "<Filer Name>",   # piggybacks the existing congress-style member field
    "tx_date":      "2026-04-25",
    "disc_date":    "2026-04-26",
    "amount_range": "$1.5M",
    "all_symbols":  ["AAPL"],
    "is_amended":   False,
    "metadata":     {... raw filing details ...},
}
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional


log = logging.getLogger("edgar_form4")


# ── Configurable thresholds (env-overridable) ───────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Minimum dollar value of the transaction to surface as a signal.
# Default: $50k — cuts noise from small token sales by random VPs but
# preserves real CEO/CFO/Director moves.
MIN_TX_VALUE_USD = _env_float("EDGAR_FORM4_MIN_USD", 50_000.0)

# Codes worth surfacing.  P=open-market buy, S=open-market sell.
# A (award/grant), F (tax withholding), M (option exercise), G (gift),
# C (conversion), W (will/inheritance), J (other) are all skipped — they
# don't carry "insider conviction" signal.
INTERESTING_CODES = frozenset({"P", "S"})


# ── XML helpers ─────────────────────────────────────────────────────────────

def _text(elem: Optional[ET.Element], path: str, default: str = "") -> str:
    if elem is None:
        return default
    found = elem.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _value_text(elem: Optional[ET.Element], path: str, default: str = "") -> str:
    """Form 4 nests many fields as <field><value>X</value></field>; this
    grabs <field>/<value> directly."""
    return _text(elem, path + "/value", default)


def _to_float(s: str) -> Optional[float]:
    s = (s or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_amount(value_usd: float) -> str:
    """Compact USD bucket: '$1.5M', '$320K'."""
    if value_usd >= 1_000_000:
        return f"${value_usd / 1_000_000:.1f}M"
    if value_usd >= 1_000:
        return f"${value_usd / 1_000:.0f}K"
    return f"${value_usd:.0f}"


def _format_headline(filer_name: str, title: str, tx_code: str,
                     tx_action: str, ticker: str, value_usd: float) -> str:
    verb_map = {"P": "bought", "S": "sold"}
    verb     = verb_map.get(tx_code, "transacted")
    title    = title.strip().rstrip(",") or ""
    title_part = f" ({title})" if title else ""
    amount     = _format_amount(value_usd)
    # "Form 4: CEO Tim Cook sold $5.2M of AAPL"
    return f"Form 4: {filer_name}{title_part} {verb} {amount} of {ticker}"


# ── Core parser ─────────────────────────────────────────────────────────────

def parse_form4(xml_text: str, filed_date: str = "") -> list[dict]:
    """Parse a Form 4 XML body into zero or more pipeline items.

    Returns one item per non-derivative transaction whose code is in
    INTERESTING_CODES and whose computed USD value clears MIN_TX_VALUE_USD.

    Args:
        xml_text:    The raw XML string from the filing's primary doc.
        filed_date:  YYYY-MM-DD of the filing (from the search hit), used
                     as the disc_date if the XML's periodOfReport differs.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning(f"form4 XML parse failed: {e}")
        return []

    issuer        = root.find("issuer")
    issuer_ticker = _text(issuer, "issuerTradingSymbol")
    issuer_name   = _text(issuer, "issuerName")
    if not issuer_ticker:
        return []  # No ticker — can't attribute a signal

    # Detect amendment via the canonical SEC marker (documentType).
    # 4/A is an amended Form 4. The earlier heuristic (schemaVersion +
    # name match) was unreliable; this is the actual schema field.
    doc_type    = _text(root, "documentType")
    is_amended  = doc_type.upper() in ("4/A", "4-A") or doc_type.endswith("/A")

    # Reporting owner — usually one, occasionally multiple
    owners = root.findall("reportingOwner")
    if not owners:
        return []

    # Take the first owner (multi-owner filings are rare; we'd over-count
    # by emitting one signal per owner-tx pair).  We attach all names in
    # metadata for trace-back.
    owner       = owners[0]
    owner_name  = _text(owner, "reportingOwnerId/rptOwnerName")
    relationship = owner.find("reportingOwnerRelationship")
    is_director  = _text(relationship, "isDirector").lower() in ("1", "true")
    is_officer   = _text(relationship, "isOfficer").lower()  in ("1", "true")
    is_ten_pct   = _text(relationship, "isTenPercentOwner").lower() in ("1", "true")
    title_str    = _text(relationship, "officerTitle")

    # Best-effort role label for the headline
    if is_officer and title_str:
        role = title_str
    elif is_director:
        role = "Director"
    elif is_ten_pct:
        role = "10% Owner"
    else:
        role = ""

    # Period of report (the actual transaction date, often = filed - 1d)
    period = _text(root, "periodOfReport")

    # First pass: harvest option-exercise transactions from the derivative
    # table. M = option/SAR exercise (acquired shares from option grant).
    # We use this to detect "exercise + tax-cover sell" patterns: when a
    # non-derivative S transaction on the SAME DATE has share count
    # <= matching M shares, that S is overwhelmingly likely the
    # tax-withholding sale that follows automated option-exercise plans
    # — NOT discretionary insider conviction. Skipping those reduces the
    # false-positive rate of "CEO sold!" headlines that are really
    # "CEO's automated 10b5-1 plan exercised options and sold to cover
    # taxes."
    option_exercises_by_date: dict[str, float] = {}
    deriv_table = root.find("derivativeTable")
    if deriv_table is not None:
        for dx in deriv_table.findall("derivativeTransaction"):
            d_code = _text(dx, "transactionCoding/transactionCode")
            if d_code != "M":
                continue
            d_shares = _to_float(_value_text(dx, "transactionAmounts/transactionShares"))
            d_date   = _value_text(dx, "transactionDate")
            if d_shares is None or d_shares <= 0 or not d_date:
                continue
            option_exercises_by_date[d_date] = (
                option_exercises_by_date.get(d_date, 0.0) + d_shares
            )

    # Walk non-derivative transactions
    out: list[dict] = []
    nd_table = root.find("nonDerivativeTable")
    if nd_table is None:
        return out

    for tx in nd_table.findall("nonDerivativeTransaction"):
        # Note: transactionCode is direct text in the SEC schema, while
        # transactionShares / transactionPricePerShare are nested under
        # <value>. Mixed schema — keep these distinct.
        code   = _text(tx, "transactionCoding/transactionCode")
        if code not in INTERESTING_CODES:
            continue
        shares = _to_float(_value_text(tx, "transactionAmounts/transactionShares"))
        ppx    = _to_float(_value_text(tx, "transactionAmounts/transactionPricePerShare"))
        ad     = _value_text(tx, "transactionAmounts/transactionAcquiredDisposedCode")
        tx_date = _value_text(tx, "transactionDate")
        if shares is None or ppx is None or shares <= 0 or ppx <= 0:
            continue
        value_usd = shares * ppx
        if value_usd < MIN_TX_VALUE_USD:
            continue

        # Tax-cover detection (Fix B 2026-04-28). If this is an open-market
        # sell AND the same filing reports an option exercise on the same
        # date with shares >= our sale shares, treat as automated tax-
        # cover and skip emission.  We err conservative: even when M
        # shares match S shares exactly (split-evenly tax-cover), we
        # skip — the net signal of "exec exercised + sold proceeds for
        # taxes" is not the same as discretionary conviction.
        if code == "S":
            same_day_m = option_exercises_by_date.get(tx_date, 0.0)
            if same_day_m > 0 and same_day_m >= shares:
                log.debug(
                    f"form4 skip tax-cover: {issuer_ticker} on {tx_date} — "
                    f"M={same_day_m:.0f} >= S={shares:.0f}"
                )
                continue

        # Sanity: A (acquired) should pair with code P (buy);
        # D (disposed) with S (sell).  Mismatches happen for amended
        # filings — we trust the code field.
        action_label = "buy" if code == "P" else "sell"

        headline = _format_headline(owner_name, role, code, action_label,
                                    issuer_ticker, value_usd)
        out.append({
            "headline":     headline,
            "ticker":       issuer_ticker.upper(),
            "source":       "edgar_form4",
            "source_tier":  1,
            "politician":   owner_name,
            "tx_date":      tx_date or period,
            "disc_date":    filed_date or period,
            "amount_range": _format_amount(value_usd),
            "all_symbols":  [issuer_ticker.upper()],
            "is_amended":   is_amended,
            "is_spousal":   False,
            "metadata": {
                "issuer_name":   issuer_name,
                "filer_role":    role,
                "is_director":   is_director,
                "is_officer":    is_officer,
                "is_ten_pct":    is_ten_pct,
                "tx_code":       code,
                "tx_shares":     shares,
                "tx_price":      ppx,
                "tx_value_usd":  round(value_usd, 2),
                "acquired_disposed": ad,
                "doc_type":      doc_type,
                # Option-exercise context — non-zero when this filing also
                # reports a same-date option exercise.  For S codes this
                # transaction has already passed the tax-cover skip
                # (option_exercise_shares < sale_shares); the field is
                # carried so downstream gates can still derate "exercise
                # + sell-more" patterns if they want.
                "same_day_option_exercise_shares": option_exercises_by_date.get(tx_date, 0.0),
                "image_url":     "",
            },
        })
    return out


# ── Ingestion entry point ───────────────────────────────────────────────────

def fetch_form4_signals(client, since_days: int = 2,
                        max_filings: int = 100) -> list[dict]:
    """Search EDGAR for recent Form 4 filings, fetch and parse each, return
    a flat list of pipeline-shaped items.

    Args:
        client:        EdgarClient instance (caller-owned, env-configured)
        since_days:    Look-back window
        max_filings:   Cap on how many Form 4 filings to *fetch* (each
                       can yield 0..N transactions). At ~thousands/month
                       across all tickers, 100 over a 2-day window pulls
                       roughly the recent flow.

    Returns: list of items, each ready to feed the news_agent gate
    pipeline. Empty list if EDGAR is unreachable or returns nothing.
    """
    hits = client.search_filings(form_type="4", since_days=since_days,
                                 max_results=max_filings)
    if not hits:
        log.info("form4 search returned 0 hits")
        return []

    items: list[dict] = []
    for hit in hits:
        url  = hit.get("primary_doc_url")
        if not url or not url.endswith(".xml"):
            # Skip non-XML primary docs (older filings sometimes are HTML)
            continue
        body = client.fetch_url(url)
        if not body:
            continue
        try:
            parsed = parse_form4(body, filed_date=hit.get("filed_date", ""))
        except Exception as e:
            log.warning(f"form4 parse error on {url[:80]}: {e}")
            continue
        items.extend(parsed)
    log.info(f"form4 fetch: {len(hits)} filings → {len(items)} signal items "
             f"(threshold ${MIN_TX_VALUE_USD:,.0f})")
    return items
