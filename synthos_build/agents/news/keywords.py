"""Pure-data keyword dictionaries, term sets, and sector maps for the
news agent. Extracted from `retail_news_agent.py` 2026-04-24 as
phase-0 of the C9 module split (see backlog).

These are read-only constants — no logic, no I/O, no dependencies
beyond builtins. The news agent re-imports every name here verbatim
so external callers and internal references continue to work without
modification.

Adding new keywords: extend the relevant set/tuple here. Do not move
classifier logic into this module — that belongs in a future
`classifiers.py` once C8 (gate-pipeline refactor) lands and golden-file
fixtures are in place.
"""


# ── KEYWORD DICTIONARIES ──────────────────────────────────────────────────

_POSITIVE = frozenset({
    'beat', 'beats', 'exceeded', 'exceeds', 'surpassed', 'surpasses',
    'growth', 'expanding', 'expansion', 'upgrade', 'upgraded', 'approved',
    'approval', 'awarded', 'wins', 'won', 'partnership', 'bullish',
    'recovery', 'recovers', 'strong', 'strength', 'gains', 'record',
    'outperform', 'raised', 'raises', 'increased', 'increases', 'boost',
    'boosted', 'profitable', 'profit', 'optimistic', 'upbeat', 'momentum',
    'accelerating', 'breakout', 'advancing', 'progress', 'breakthrough',
    'contract', 'deal', 'launch', 'launched', 'dividend', 'buyback',
    'acquisition', 'merger', 'positive', 'upside', 'rebound', 'rally',
})

_NEGATIVE = frozenset({
    'missed', 'miss', 'below', 'declined', 'decline', 'declining',
    'loss', 'losses', 'layoffs', 'layoff', 'downgrade', 'downgraded',
    'investigation', 'lawsuit', 'sanction', 'sanctions', 'bearish',
    'concern', 'concerns', 'weak', 'weakness', 'fell', 'fall', 'falling',
    'cut', 'cuts', 'reduce', 'reduces', 'suspend', 'suspends', 'halted',
    'halt', 'recall', 'warning', 'default', 'bankruptcy', 'bankrupt',
    'crisis', 'crash', 'downside', 'failure', 'failed', 'disappointing',
    'disappoint', 'headwinds', 'headwind', 'negative', 'selloff',
    'plunged', 'plunge', 'collapse', 'collapsed', 'probe', 'fine',
    'penalty', 'charged', 'indicted', 'delisting', 'downward',
})

_UNCERTAINTY = frozenset({
    'may', 'might', 'could', 'possible', 'possibly', 'potential',
    'potentially', 'uncertain', 'uncertainty', 'unclear', 'unknown',
    'pending', 'conditional', 'tentative', 'alleged', 'reportedly',
    'rumored', 'expected', 'anticipated', 'likely', 'unlikely',
    'if', 'whether', 'contingent', 'provisional', 'unconfirmed',
})

_MACRO_TERMS = (
    'federal reserve', 'interest rate', 'inflation', 'gdp', 'unemployment',
    'fomc', 'central bank', 'monetary policy', 'rate hike', 'rate cut',
    'yield curve', 'treasury', 'deficit', 'fiscal', 'jobs report',
    'payroll', 'recession', 'stagflation', 'quantitative easing',
)

_EARNINGS_TERMS = (
    'earnings', 'revenue', ' eps ', 'guidance', 'quarterly results',
    'net income', 'operating income', 'full year', 'annual results',
    'beat estimates', 'missed estimates', 'first quarter', 'second quarter',
    'third quarter', 'fourth quarter',
)

_GEOPOLITICAL_TERMS = (
    'sanctions', 'tariff', 'election', 'diplomacy', 'geopolitical',
    'military', 'invasion', 'nato', 'trade war', 'embargo', 'ceasefire',
    'escalation', 'missile', 'nuclear',
)

_REGULATORY_TERMS = (
    ' sec ', ' doj ', ' ftc ', ' fda ', ' epa ', 'legislation', 'compliance',
    'antitrust', 'investigation', 'enforcement action', 'class action',
    'subpoena', 'consent decree', 'indictment',
)

_PRIMARY_SOURCE_SIGNALS = frozenset({
    'filing', 'press release', 'official statement', 'transcript',
    'form 4', 'sec filing', '8-k', '10-k', '10-q', 'annual report',
    'confirmed by', 'announced by', 'sec.gov', 'alpaca news',
})

_OPINION_SIGNALS = frozenset({
    'opinion', 'analysis', 'commentary', 'editorial', 'column',
    'perspective', 'viewpoint', 'argues', 'believes', 'thinks',
    'according to analysts', 'analysts say', 'experts say',
})

_MARKET_STRUCTURE_TERMS = (
    'circuit breaker', 'market maker', 'high frequency trading', 'hft',
    'liquidity', 'market mechanics', 'order flow', 'dark pool',
    'market structure', 'exchange halt', 'trading halt', 'market open',
    'market close', 'settlement', 'clearing',
)


# ── SECTOR / ETF MAPS ─────────────────────────────────────────────────────

SECTOR_ETF_MAP = {
    "technology":             ("QQQ",  "XLK"),
    "defense":                ("ITA",  "XLI"),
    "healthcare":             ("IBB",  "XLV"),
    "energy":                 ("XOP",  "XLE"),
    "financials":             ("KRE",  "XLF"),
    "materials":              ("PICK", "XLB"),
    "industrials":            ("XLI",  "XLI"),
    "consumer staples":       ("XLP",  "XLP"),
    "consumer discretionary": ("XLY",  "XLY"),
    "real estate":            ("XLRE", "XLRE"),
    "utilities":              ("XLU",  "XLU"),
    "communication":          ("XLC",  "XLC"),
}

CONFIDENCE_NUMERIC = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "NOISE": 0.0}

SECTOR_TICKER_MAP = {
    "defense":     ["LMT", "RTX", "NOC", "GD", "BA", "KTOS", "LHX"],
    "technology":  ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMD", "INTC"],
    "healthcare":  ["LLY", "JNJ", "UNH", "PFE", "ABBV", "MRK"],
    "energy":      ["XOM", "CVX", "NEE", "SO", "DUK"],
    "financials":  ["JPM", "BAC", "WFC", "GS", "KRE"],
    "materials":   ["MP", "ALB", "FCX", "NEM"],
    "industrials": ["DE", "CAT", "GE", "HON"],
}


# ── ALPACA SOURCE → TIER MAP ──────────────────────────────────────────────
# Benzinga is the primary content provider via Alpaca's news feed.
# Logic that uses this (e.g. _alpaca_news_tier) lives in retail_news_agent.py
# — only the data lives here.

_ALPACA_SOURCE_TIERS = {
    "benzinga":  2,   # wire-level financial news
    "reuters":   2,
    "ap":        2,
    "dow jones": 2,
    "wsj":       2,
    "ft":        2,
    "bloomberg": 2,
}


# ── PUBLIC EXPORT LIST ────────────────────────────────────────────────────
# Every name above is intended to be re-imported by retail_news_agent.py.
# Callers may also `from agents.news.keywords import <name>` if running
# with synthos_build/ on sys.path.
__all__ = [
    "_POSITIVE",
    "_NEGATIVE",
    "_UNCERTAINTY",
    "_MACRO_TERMS",
    "_EARNINGS_TERMS",
    "_GEOPOLITICAL_TERMS",
    "_REGULATORY_TERMS",
    "_PRIMARY_SOURCE_SIGNALS",
    "_OPINION_SIGNALS",
    "_MARKET_STRUCTURE_TERMS",
    "SECTOR_ETF_MAP",
    "CONFIDENCE_NUMERIC",
    "SECTOR_TICKER_MAP",
    "_ALPACA_SOURCE_TIERS",
]
