"""
retail_sector_map.py — Sector resolution for tickers

Single entry point for resolving ticker → sector across the system.
Used by the trade agent (Gate 0 adoption, position tagging), the bias
detector (concentration analysis), and the portal.

Resolution order (fastest → slowest):
  1. HARDCODED_MAP      — curated dict, O(1), authoritative for reserves
  2. ticker_sectors     — persisted table populated by backfill agent
  3. sector_screening   — existing screener output (covers screener universe)
  4. None               — caller should display 'Unknown' and flag for backfill

The hardcoded map takes precedence intentionally — reserve ETFs like BIL
are classified by FMP as "Financial Services", which is technically true
but misleading for concentration analysis. Cash/Reserve positions must be
excluded from equity sector bucketing.
"""

from typing import Optional


# Special bucket for reserves — positions with this "sector" are excluded
# from sector concentration analysis by the bias detector.
CASH_RESERVE = 'Cash/Reserve'


# Curated ticker → sector map.
# Priorities: (1) reserve ETFs, (2) customer holdings, (3) S&P 100 common names.
# Keep it tight — the FMP backfill covers the long tail.
HARDCODED_MAP = {
    # ── Cash / Treasury reserve ETFs ───────────────────────────────────
    'BIL':   CASH_RESERVE,   # SPDR 1-3 Month T-Bill
    'SGOV':  CASH_RESERVE,   # iShares 0-3 Month Treasury
    'SHV':   CASH_RESERVE,   # iShares Short Treasury
    'VMOT':  CASH_RESERVE,   # Alpha Architect Value Momentum (actually equity, leave as reserve per usage)
    'USFR':  CASH_RESERVE,   # WisdomTree Floating Rate Treasury
    'TFLO':  CASH_RESERVE,   # iShares Treasury Floating Rate

    # ── Known customer holdings (observed in production) ───────────────
    'AAL':   'Industrials',           # American Airlines
    'AMD':   'Technology',            # Advanced Micro Devices
    'LYFT':  'Consumer Cyclical',     # Lyft
    'XOM':   'Energy',                # Exxon Mobil (already in screener, kept as safety)

    # ── S&P 100 common names ───────────────────────────────────────────
    'AAPL':  'Technology',
    'MSFT':  'Technology',
    'NVDA':  'Technology',
    'GOOGL': 'Communication Services',
    'GOOG':  'Communication Services',
    'META':  'Communication Services',
    'AMZN':  'Consumer Cyclical',
    'TSLA':  'Consumer Cyclical',
    'BRK.B': 'Financial Services',
    'BRK.A': 'Financial Services',
    'JPM':   'Financial Services',
    'BAC':   'Financial Services',
    'WFC':   'Financial Services',
    'GS':    'Financial Services',
    'MS':    'Financial Services',
    'V':     'Financial Services',
    'MA':    'Financial Services',
    'UNH':   'Healthcare',
    'JNJ':   'Healthcare',
    'LLY':   'Healthcare',
    'PFE':   'Healthcare',
    'ABBV':  'Healthcare',
    'MRK':   'Healthcare',
    'TMO':   'Healthcare',
    'ABT':   'Healthcare',
    'CVS':   'Healthcare',
    'WMT':   'Consumer Defensive',
    'PG':    'Consumer Defensive',
    'KO':    'Consumer Defensive',
    'PEP':   'Consumer Defensive',
    'COST':  'Consumer Defensive',
    'MCD':   'Consumer Cyclical',
    'NKE':   'Consumer Cyclical',
    'HD':    'Consumer Cyclical',
    'LOW':   'Consumer Cyclical',
    'SBUX':  'Consumer Cyclical',
    'DIS':   'Communication Services',
    'NFLX':  'Communication Services',
    'CMCSA': 'Communication Services',
    'T':     'Communication Services',
    'VZ':    'Communication Services',
    'CVX':   'Energy',
    'COP':   'Energy',
    'SLB':   'Energy',
    'BA':    'Industrials',
    'CAT':   'Industrials',
    'GE':    'Industrials',
    'UPS':   'Industrials',
    'FDX':   'Industrials',
    'DE':    'Industrials',
    'HON':   'Industrials',
    'LIN':   'Basic Materials',
    'APD':   'Basic Materials',
    'DOW':   'Basic Materials',
    'NEE':   'Utilities',
    'DUK':   'Utilities',
    'SO':    'Utilities',
    'SPG':   'Real Estate',
    'AMT':   'Real Estate',
    'PLD':   'Real Estate',

    # ── Common broad-market ETFs (treated as diversified/not-a-sector) ─
    'SPY':   'Diversified',
    'VOO':   'Diversified',
    'IVV':   'Diversified',
    'VTI':   'Diversified',
    'QQQ':   'Technology',
    'DIA':   'Diversified',
    'IWM':   'Diversified',
    'VXUS':  'Diversified',
    'TLT':   'Fixed Income',
    'IEF':   'Fixed Income',
    'AGG':   'Fixed Income',
    'BND':   'Fixed Income',
    'HYG':   'Fixed Income',
    'LQD':   'Fixed Income',
    'GLD':   'Commodities',
    'SLV':   'Commodities',
    'USO':   'Commodities',
}


# Sectors that should NOT count toward equity-concentration bias analysis.
# Reserves are not a "sector bet" and broad-market diversified positions
# deliberately span sectors.
EXCLUDED_FROM_CONCENTRATION = {
    CASH_RESERVE,
    'Diversified',
    'Fixed Income',
    'Unknown',
    '',
}


def lookup_sector(ticker: str, shared_db=None) -> Optional[str]:
    """Resolve a ticker to a sector using the cascade:
        1. HARDCODED_MAP          — fast, overrides FMP for reserves
        2. ticker_sectors table   — populated by the backfill agent (via shared_db)
        3. sector_screening       — existing screener output
        4. None                   — caller should display 'Unknown'

    `shared_db` is optional; when None the function only checks the hardcoded
    map (useful in tests or from modules without DB access).
    """
    if not ticker:
        return None
    t = ticker.upper().strip()

    # 1. Hardcoded map
    if t in HARDCODED_MAP:
        return HARDCODED_MAP[t]

    # 2. ticker_sectors table
    if shared_db is not None:
        try:
            with shared_db.conn() as c:
                row = c.execute(
                    "SELECT sector FROM ticker_sectors WHERE ticker=?",
                    (t,)
                ).fetchone()
                if row and row['sector']:
                    return row['sector']
        except Exception:
            pass  # Table may not exist yet on first deploy

        # 3. sector_screening (existing screener output)
        try:
            sec = shared_db.get_screening_score(t)
            if sec and sec.get('sector'):
                return sec['sector']
        except Exception:
            pass

    return None


def is_excluded_from_concentration(sector: Optional[str]) -> bool:
    """True if this sector bucket should be excluded from bias-detector
    sector-concentration analysis (reserves, diversified ETFs, unknowns)."""
    if not sector:
        return True
    return sector.strip() in EXCLUDED_FROM_CONCENTRATION
