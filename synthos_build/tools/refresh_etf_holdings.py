#!/usr/bin/env python3
"""
refresh_etf_holdings.py — Quarterly review of SECTOR_CONFIG.

Fetches each SPDR sector ETF's daily holdings from SSGA, diffs against
the current hardcoded SECTOR_CONFIG in retail_sector_screener.py, and
prints a report.

Propose-only by default. Does NOT auto-write the dict — outputs a
ready-to-paste Python block that the operator can review and copy in.

Sources:
  PRIMARY:    SSGA daily holdings xlsx — public URL per fund. Stable
              path /library-content/products/fund-data/etfs/us/.
  SANITY:     iShares parallel-sector ETF top-name overlap (scaffolded
              but disabled by default — requires numeric product IDs
              in URL paths that need manual lookup; see ISHARES_SECTORS
              constant below to enable).
  TERTIARY:   FMP /etf-holdings — placeholder. Requires Ultimate tier
              ($149/mo). Not currently implemented; structure is
              ready when/if the user signs up.

Failure mode: any SSGA fetch failure flags that sector as UNAVAILABLE.
The current SECTOR_CONFIG values for failed sectors are LEFT IN PLACE
(do not substitute from iShares — that's a different fund). Loud
failure is the safer default.

Run quarterly:
    cd ~/synthos/synthos_build
    python3 tools/refresh_etf_holdings.py
Exit code 1 if any source fails (so cron can alert).
"""
import io
import os
import sys
import zipfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'agents'))

# ── CONFIG ────────────────────────────────────────────────────────────────
# SSGA holdings xlsx URL pattern. Stable for years; if SSGA ever moves
# this path, the fetch will 404 and the script will fail-loud — exactly
# what we want.
SPDR_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-{ticker}.xlsx"
)

# Sectors we screen. Keys must match SECTOR_CONFIG in
# retail_sector_screener.py exactly.
SPDR_SECTORS = {
    "Energy":                  "xle",
    "Technology":              "xlk",
    "Financials":              "xlf",
    "Healthcare":              "xlv",
    "Industrials":             "xli",
    "Consumer Discretionary":  "xly",
    "Consumer Staples":        "xlp",
    "Basic Materials":         "xlb",
    "Utilities":               "xlu",
    "Real Estate":             "xlre",
    "Communication Services":  "xlc",
}

# iShares parallel-sector tickers. NOT used today — left as a TODO.
# To enable, fill in the numeric product IDs from each fund's iShares
# page URL (e.g. /products/239507/...) and uncomment the cross-check
# in main(). Different fund family, different methodology — only the
# top-name *identity* should be compared, never weights.
ISHARES_SECTORS = {
    "Energy":                  ("IYE", None),  # TODO: product_id
    "Technology":              ("IYW", None),
    "Financials":              ("IYF", None),
    "Healthcare":              ("IYH", None),
    "Industrials":             ("IYJ", None),
    "Consumer Discretionary":  ("IYC", None),
    "Consumer Staples":        ("IYK", None),
    "Basic Materials":         ("IYM", None),
    "Utilities":               ("IDU", None),
    "Real Estate":             ("IYR", None),
    "Communication Services":  ("IYZ", None),
}

TOP_N        = 10
HTTP_TIMEOUT = 30
WEIGHT_DRIFT_TOL = 0.5  # % — minor rebalances under this aren't reported


# ── XLSX PARSING (stdlib only — no openpyxl dependency) ───────────────────
_NS = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def _parse_xlsx(file_data: bytes):
    """Parse the first worksheet of an xlsx blob into a list of rows
    (each row a list of strings). Resolves shared-string indices."""
    with zipfile.ZipFile(io.BytesIO(file_data)) as z:
        # Shared strings table (may be absent if the workbook inlines all values).
        ss = []
        if 'xl/sharedStrings.xml' in z.namelist():
            with z.open('xl/sharedStrings.xml') as f:
                root = ET.parse(f).getroot()
                for si in root.findall('main:si', _NS):
                    # Concatenate any rich-text runs inside this shared string.
                    parts = [t.text or '' for t in si.findall('.//main:t', _NS)]
                    ss.append(''.join(parts))
        # First worksheet.
        sheet_path = 'xl/worksheets/sheet1.xml'
        if sheet_path not in z.namelist():
            # Some workbooks use a different sheet ordering — pick whatever's present.
            for n in z.namelist():
                if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'):
                    sheet_path = n
                    break
        with z.open(sheet_path) as f:
            root = ET.parse(f).getroot()
            rows = []
            for row in root.findall('.//main:row', _NS):
                cells = []
                for c in row.findall('main:c', _NS):
                    v = c.find('main:v', _NS)
                    if v is None:
                        cells.append('')
                        continue
                    val = v.text or ''
                    if c.get('t') == 's':
                        idx = int(val)
                        cells.append(ss[idx] if 0 <= idx < len(ss) else val)
                    else:
                        cells.append(val)
                rows.append(cells)
            return rows


# ── SSGA FETCH ────────────────────────────────────────────────────────────
def fetch_ssga_holdings(etf_ticker: str):
    """Fetch + parse SSGA daily holdings for one SPDR sector ETF.
    Returns (ok: bool, holdings: list[dict], error_msg: str)."""
    url = SPDR_HOLDINGS_URL.format(ticker=etf_ticker.lower())
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': ('Mozilla/5.0 (X11; Linux aarch64) '
                           'synthos-screener-refresh/1.0'),
            'Accept': '*/*',
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
    except Exception as e:
        return False, [], f"HTTP fetch failed: {e}"

    try:
        rows = _parse_xlsx(data)
    except Exception as e:
        return False, [], f"xlsx parse failed: {e}"

    # Locate header row — SSGA typically prefixes with metadata rows
    # (Fund:, As of:, blank lines) before the table header. Find the row
    # that contains both 'ticker' and 'weight' substrings.
    header_idx = None
    for i, row in enumerate(rows):
        lower = [(c or '').strip().lower() for c in row]
        if any('ticker' in c for c in lower) and any('weight' in c for c in lower):
            header_idx = i
            break
    if header_idx is None:
        return False, [], "could not find table header in xlsx (no row with both 'ticker' and 'weight')"

    header = [(c or '').strip().lower() for c in rows[header_idx]]

    def col_index(needle: str):
        for j, h in enumerate(header):
            if needle in h:
                return j
        return -1

    ticker_col = col_index('ticker')
    name_col   = col_index('name')
    weight_col = col_index('weight')
    if -1 in (ticker_col, name_col, weight_col):
        return False, [], f"missing required columns in header: {header}"

    holdings = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(ticker_col, name_col, weight_col):
            continue
        tkr = (row[ticker_col] or '').strip().upper()
        nm  = (row[name_col]   or '').strip()
        raw_w = (row[weight_col] or '0').replace(',', '').replace('%', '').strip()
        try:
            weight = float(raw_w)
        except ValueError:
            continue
        if not tkr or not nm or weight <= 0:
            continue
        holdings.append({
            'ticker':         tkr,
            'company':        nm,
            'etf_weight_pct': round(weight, 2),
        })

    if not holdings:
        return False, [], "header parsed but no holdings rows extracted"

    holdings.sort(key=lambda h: -h['etf_weight_pct'])
    return True, holdings[:TOP_N], ""


# ── DIFF AGAINST CURRENT SECTOR_CONFIG ───────────────────────────────────
def load_current_config():
    try:
        from retail_sector_screener import SECTOR_CONFIG  # type: ignore
        return SECTOR_CONFIG
    except Exception as e:
        print(f"WARNING: could not import SECTOR_CONFIG: {e}", file=sys.stderr)
        return {}


def diff_holdings(current, proposed):
    """current/proposed are lists of {ticker, company, etf_weight_pct}.
    Returns added/removed/weight_changed."""
    cur_map  = {h['ticker']: h for h in current}
    prop_map = {h['ticker']: h for h in proposed}
    added            = [prop_map[t] for t in prop_map if t not in cur_map]
    removed          = [cur_map[t]  for t in cur_map  if t not in prop_map]
    weight_changed   = []
    for t in prop_map:
        if t in cur_map:
            old_w = cur_map[t].get('etf_weight_pct', 0)
            new_w = prop_map[t].get('etf_weight_pct', 0)
            if abs(old_w - new_w) > WEIGHT_DRIFT_TOL:
                weight_changed.append({
                    'ticker': t, 'old': old_w, 'new': new_w,
                    'delta': round(new_w - old_w, 2),
                })
    return {'added': added, 'removed': removed, 'weight_changed': weight_changed}


# ── REPORT ────────────────────────────────────────────────────────────────
def main():
    config = load_current_config()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print('=' * 78)
    print(f"SECTOR_CONFIG quarterly refresh — {now}")
    print('=' * 78)
    print()

    any_failures = False
    any_drift    = False
    successful_proposals = {}  # sector → list of holdings

    for sector, etf in SPDR_SECTORS.items():
        print(f"# {sector}  ({etf.upper()})")
        ok, proposed, err = fetch_ssga_holdings(etf)
        if not ok:
            print(f"  ❌ SSGA fetch FAILED: {err}")
            print(f"     → SECTOR_CONFIG['{sector}'] left untouched (fail-loud).")
            any_failures = True
            print()
            continue

        current = (config.get(sector, {}) or {}).get('holdings', [])
        d = diff_holdings(current, proposed)

        if not d['added'] and not d['removed'] and not d['weight_changed']:
            print(f"  ✅ Clean — {len(proposed)} holdings match current within tolerance")
        else:
            any_drift = True
            print(f"  ⚠️  Drift detected:")
            if d['added']:
                print(f"     ADDED:       {', '.join(h['ticker'] for h in d['added'])}")
            if d['removed']:
                print(f"     REMOVED:     {', '.join(h['ticker'] for h in d['removed'])}")
            if d['weight_changed']:
                lines = [f"{c['ticker']} {c['old']:.1f}→{c['new']:.1f}% (Δ{c['delta']:+.1f})"
                         for c in d['weight_changed']]
                print(f"     WEIGHT Δ:    {', '.join(lines)}")
        successful_proposals[sector] = proposed
        print()

    print('-' * 78)
    if any_failures:
        print('⚠️  One or more SSGA fetches failed.')
        print('    Failed sectors retain their current SECTOR_CONFIG values (fail-loud).')
        print('    Investigate: SSGA URL changes, network issues, xlsx format drift.')
        print()

    if any_drift:
        print('Proposed SECTOR_CONFIG block — review and paste into')
        print('synthos_build/agents/retail_sector_screener.py if it looks right:')
        print()
        print('SECTOR_CONFIG = {')
        for sector, holdings in successful_proposals.items():
            etf_upper = SPDR_SECTORS[sector].upper()
            print(f'    "{sector}": {{')
            print(f'        "etf": "{etf_upper}",')
            print(f'        "holdings": [')
            for h in holdings:
                tkr = h['ticker']
                co  = h['company'].replace('"', "'")
                w   = h['etf_weight_pct']
                print(f'            {{"ticker": "{tkr}", "company": "{co}", "etf_weight_pct": {w:>5.2f}}},')
            print(f'        ],')
            print(f'    }},')
        print('}')
    elif not any_failures:
        print('No changes needed — SECTOR_CONFIG matches all SSGA sources.')

    return 1 if any_failures else 0


if __name__ == '__main__':
    sys.exit(main())
