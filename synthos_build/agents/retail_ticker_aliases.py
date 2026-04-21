#!/usr/bin/env python3
"""
retail_ticker_aliases.py — Ticker ↔ name helpers for news attribution.

Used by retail_news_agent.py during Alpaca article ingestion to validate
that the tagged ticker (symbols[]) actually matches the article headline.

Contents:
  TICKER_ALIASES         — informal names not present in Alpaca's asset
                           "name" field (e.g. "Google" for Alphabet).
  NAME_TOKEN_BLACKLIST   — generic tokens that alone shouldn't count as
                           a match (e.g. "bank" matches too many names).
  strip_suffixes(name)   — drop Inc/Corp/ETF/etc. from a company name.
  tokens_for_name(name)  — tokenize stripped name, filter to ≥4-char
                           non-blacklisted tokens for match scoring.

Design notes (2026-04-21):
  - Seeded with ~30 mega-cap aliases where headline usage diverges from
    the Alpaca "name" field. Grow this table as the [TICKER_REJECT]
    shadow log exposes new cases.
  - Blacklist keeps weak single-token matches from leaking through. A
    headline like "Bank Of America Launches X" still matches BAC via the
    alias "bank of america"; but a headline saying "Banks Face Pressure"
    won't accidentally attribute to every BA/BAC/BK/BKR whose name
    contains "bank".
  - Deliberately NOT a full company database — Alpaca's /v2/assets
    "name" field covers the other 99% of tickers. This module only
    handles the edge cases.
"""

from __future__ import annotations

import re


# ── ALIASES ────────────────────────────────────────────────────────────────
# Ticker → list of informal names/variants seen in headlines. All lowercase.
# Add entries when [TICKER_REJECT] / [TICKER_FLAG:NOMATCH] logs show legit
# articles being flagged because the headline uses an informal name.
TICKER_ALIASES: dict[str, list[str]] = {
    'GOOGL': ['google', 'alphabet'],
    'GOOG':  ['google', 'alphabet'],
    'META':  ['facebook', 'meta', 'instagram', 'whatsapp'],
    'BRK.A': ['berkshire'],
    'BRK.B': ['berkshire'],
    'JPM':   ['jpmorgan', 'jp morgan', 'chase'],
    'GS':    ['goldman'],
    'BAC':   ['bank of america', 'bofa'],
    'T':     ['at&t'],
    'VZ':    ['verizon'],
    'KO':    ['coca-cola', 'coke'],
    'WMT':   ['walmart', 'wal-mart'],
    'HD':    ['home depot'],
    'NFLX':  ['netflix'],
    'NVDA':  ['nvidia'],
    'AMD':   ['amd'],
    'INTC':  ['intel'],
    'MSFT':  ['microsoft'],
    'AAPL':  ['apple'],
    'AMZN':  ['amazon'],
    'TSLA':  ['tesla'],
    'F':     ['ford motor'],
    'GM':    ['general motors'],
    'BA':    ['boeing'],
    'CAT':   ['caterpillar'],
    'XOM':   ['exxon'],
    'CVX':   ['chevron'],
    'UNH':   ['unitedhealth'],
    'PFE':   ['pfizer'],
    'MRK':   ['merck'],
    'LLY':   ['lilly', 'eli lilly'],
    'JNJ':   ['johnson & johnson', 'j&j'],
}


# ── NAME-TOKEN BLACKLIST ───────────────────────────────────────────────────
# Tokens that appear in too many company names to be useful alone. A single
# blacklisted token match is dropped by tokens_for_name(); multi-token names
# still work because the non-blacklisted tokens remain.
#
# Example: "Bank of New York Mellon" → tokens become ['york', 'mellon']
# (bank dropped). "Citigroup Inc" → ['citigroup']. "American Airlines" →
# ['airlines'] (american dropped — too many companies).
NAME_TOKEN_BLACKLIST: set[str] = {
    'american', 'united', 'international', 'global', 'national',
    'bank', 'banks', 'banking',
    'energy', 'resources', 'industries', 'products', 'services',
    'trust', 'ishares', 'invesco', 'vanguard', 'spdr',
    'capital', 'holdings', 'holding', 'group', 'partners',
    'technology', 'technologies', 'systems',
    'financial', 'finance', 'fund', 'trust',
    'corporation', 'company', 'companies',
    'common', 'stock', 'shares', 'class',
    'ordinary', 'preferred', 'series', 'depositary',
}


# ── SUFFIX STRIPPING ──────────────────────────────────────────────────────
# Trailing entity descriptors that aren't part of the headline-usable name.
# Order matters: longer variants first so "Inc." doesn't leave a ".".
_SUFFIX_PATTERNS = [
    re.compile(r'\b(inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|plc|'
               r'n\.?v\.?|ag|s\.?a\.?|s\.?p\.?a\.?|llc|l\.p\.?|lp|'
               r'holdings?|group|trust|fund|etf)\b', re.I),
    re.compile(r'[.,&]'),
    re.compile(r'\s+'),
]


def strip_suffixes(name: str) -> str:
    """Return company name with corporate suffixes and punctuation removed."""
    if not name:
        return ''
    s = name.strip()
    # Remove entity suffixes (may leave double-spaces — collapsed below)
    s = _SUFFIX_PATTERNS[0].sub(' ', s)
    # Strip punctuation
    s = _SUFFIX_PATTERNS[1].sub(' ', s)
    # Collapse whitespace
    s = _SUFFIX_PATTERNS[2].sub(' ', s).strip()
    return s


def tokens_for_name(name: str) -> list[str]:
    """
    Return distinctive tokens from a company name suitable for headline matching.

    Drops tokens that are too short (<4 chars), blacklisted generics, or
    obvious stop-words. Output is lowercased.

    Examples:
      "Bank Of New York Mellon"    → ['york', 'mellon']
      "American Airlines Group"     → ['airlines']
      "Citigroup Inc."              → ['citigroup']
      "Apple Inc. Common Stock"     → ['apple']
      "Broadcom Inc. Common Stock"  → ['broadcom']
    """
    stripped = strip_suffixes(name).lower()
    if not stripped:
        return []
    tokens = []
    for t in stripped.split():
        if len(t) < 4:
            continue
        if t in NAME_TOKEN_BLACKLIST:
            continue
        tokens.append(t)
    return tokens


def aliases_for(ticker: str) -> list[str]:
    """Return the alias list for a ticker (empty list if none)."""
    return TICKER_ALIASES.get((ticker or '').upper(), [])
