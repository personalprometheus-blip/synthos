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
    # Mega-cap tech — 2026-04-21 seed + 2026-05-01 expansion based on
    # observed [TICKER_REJECT] no_match patterns. Headlines often reference
    # the CEO or a major product line instead of the corporate name; aliases
    # let the resolver still anchor the article to its primary ticker.
    'GOOGL': ['google', 'alphabet', 'sundar pichai', 'pichai'],
    'GOOG':  ['google', 'alphabet', 'sundar pichai', 'pichai'],
    'META':  ['facebook', 'meta', 'instagram', 'whatsapp',
              'zuckerberg', 'mark zuckerberg'],
    'AAPL':  ['apple', 'tim cook', 'iphone', 'ipad', 'macbook', 'apple watch'],
    'MSFT':  ['microsoft', 'azure', 'nadella', 'satya nadella'],
    'AMZN':  ['amazon', 'aws', 'bezos', 'jeff bezos', 'jassy', 'andy jassy', 'kindle'],
    'NVDA':  ['nvidia', 'jensen huang', 'cuda', 'geforce'],
    'AMD':   ['amd', 'lisa su'],
    'INTC':  ['intel', 'pat gelsinger'],
    'NFLX':  ['netflix'],
    'TSLA':  ['tesla', 'musk', 'elon musk', 'cybertruck', 'roadster',
              'model y', 'model 3', 'model s', 'model x'],
    # Holdings + finance
    'BRK.A': ['berkshire', 'warren buffett'],
    'BRK.B': ['berkshire', 'warren buffett'],
    'JPM':   ['jpmorgan', 'jp morgan', 'chase', 'jamie dimon', 'dimon'],
    'GS':    ['goldman', 'goldman sachs'],
    'BAC':   ['bank of america', 'bofa'],
    'COF':   ['capital one'],
    'AXP':   ['american express', 'amex'],
    # Telecom + consumer
    'T':     ['at&t'],
    'VZ':    ['verizon'],
    'KO':    ['coca-cola', 'coke'],
    'WMT':   ['walmart', 'wal-mart'],
    'HD':    ['home depot'],
    'UBER':  ['uber'],
    # Industrial + energy
    'F':     ['ford motor'],
    'GM':    ['general motors'],
    'BA':    ['boeing'],
    'CAT':   ['caterpillar'],
    'XOM':   ['exxon', 'exxonmobil'],
    'CVX':   ['chevron'],
    'SLB':   ['schlumberger'],
    'APD':   ['air products'],
    'NEM':   ['newmont'],
    'NEE':   ['nextera', 'nextera energy'],
    # Healthcare
    'UNH':   ['unitedhealth'],
    'PFE':   ['pfizer'],
    'MRK':   ['merck'],
    'LLY':   ['lilly', 'eli lilly'],
    'JNJ':   ['johnson & johnson', 'j&j'],
    # Mid-cap names appearing in 2026-05-01 wrong_ticker / remap flags —
    # adding them gives the multi-symbol re-ranker a name signal to anchor
    # the right ticker when an article mentions several companies.
    'IHRT':  ['iheartmedia', 'iheart'],
    'SNDK':  ['sandisk'],
    'FIVN':  ['five9'],
    'VEEV':  ['veeva', 'veeva systems'],
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
