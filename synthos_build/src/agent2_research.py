"""
agent2_research.py — Scout (Research Agent)
Synthos · Agent 2

Runs:
  - Every hour during market hours (9am-4pm ET weekdays)
  - Every 4 hours overnight

Responsibilities:
  - Fetch congressional disclosures from free APIs
  - Score base signal (HIGH/MEDIUM/LOW) with existing tier rules
  - Apply per-member reliability weight → adjusted score
  - Pull 1yr price history for ticker + industry/sector ETF
  - Write all signals to news_feed table for portal display
  - Announce for peer interrogation (UDP broadcast, 30s wait)
  - Post metadata to company Pi if COMPANY_SUBSCRIPTION=true
  - Queue validated signals for Bolt

Strict scope: political and legislative signals ONLY.
No sentiment analysis — that is The Pulse's job.

Usage:
  python3 agent2_research.py
  python3 agent2_research.py --session=overnight
"""

import os
import sys
import json
import time
import socket
import logging
import argparse
import requests
import feedparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from database import get_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
# ANTHROPIC_API_KEY removed — Scout uses no LLM in signal classification.
# All decisions are rule-based and traceable. See classify_signal().
CONGRESS_API_KEY    = os.environ.get('CONGRESS_API_KEY', '')
ALPACA_API_KEY      = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY   = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_DATA_URL     = "https://data.alpaca.markets"
ET                  = ZoneInfo("America/New_York")
MAX_RETRIES         = 3
REQUEST_TIMEOUT     = 10   # seconds per HTTP request

# Company subscription — fire-and-forget metadata POST to company Pi
COMPANY_SUBSCRIPTION = os.environ.get('COMPANY_SUBSCRIPTION', 'true').lower() == 'true'
MONITOR_URL          = os.environ.get('MONITOR_URL', '').rstrip('/')
MONITOR_TOKEN        = os.environ.get('MONITOR_TOKEN', '')

# Signal threshold — near-zero floor to filter only truly bad adjusted scores
MIN_SIGNAL_THRESHOLD = float(os.environ.get('MIN_SIGNAL_THRESHOLD', '0.1'))

# UDP interrogation broadcast
INTERROGATION_PORT    = 5556
INTERROGATION_TIMEOUT = 30   # seconds to wait for peer response

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('agent2_research')


# ── INDUSTRY / SECTOR ETF MAP ─────────────────────────────────────────────
# Maps sector names to (industry_ETF, sector_ETF) for price history context.
SECTOR_ETF_MAP = {
    "technology":           ("QQQ",  "XLK"),
    "defense":              ("ITA",  "XLI"),
    "healthcare":           ("IBB",  "XLV"),
    "energy":               ("XOP",  "XLE"),
    "financials":           ("KRE",  "XLF"),
    "materials":            ("PICK", "XLB"),
    "industrials":          ("XLI",  "XLI"),
    "consumer staples":     ("XLP",  "XLP"),
    "consumer discretionary": ("XLY", "XLY"),
    "real estate":          ("XLRE", "XLRE"),
    "utilities":            ("XLU",  "XLU"),
    "communication":        ("XLC",  "XLC"),
}

# Confidence numeric mapping for member weight calculation
CONFIDENCE_NUMERIC = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "NOISE": 0.0}

# Known congressional trading sectors mapped to representative tickers
SECTOR_TICKER_MAP = {
    "defense":     ["LMT", "RTX", "NOC", "GD", "BA", "KTOS", "LHX"],
    "technology":  ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMD", "INTC"],
    "healthcare":  ["LLY", "JNJ", "UNH", "PFE", "ABBV", "MRK"],
    "energy":      ["XOM", "CVX", "NEE", "SO", "DUK"],
    "financials":  ["JPM", "BAC", "WFC", "GS", "KRE"],
    "materials":   ["MP", "ALB", "FCX", "NEM"],
    "industrials": ["DE", "CAT", "GE", "HON"],
}


# ── RETRY HELPERS ─────────────────────────────────────────────────────────

def fetch_with_retry(url, params=None, headers=None, max_retries=MAX_RETRIES):
    """Fetch a URL with exponential backoff. Returns response or None."""
    last_error = None
    for attempt in range(max_retries):
        try:
            r = requests.get(
                url, params=params, headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"Fetch failed ({url[:60]}...) attempt {attempt+1}/{max_retries} — retrying in {wait}s: {e}")
                time.sleep(wait)
    log.error(f"Fetch permanently failed after {max_retries} attempts: {url[:80]} — {last_error}")
    return None


# ── SIGNAL DECISION LOG ───────────────────────────────────────────────────

class SignalDecisionLog:
    """
    Records the classification decision for every processed signal.
    Human-readable + machine-readable output for regulatory audit.

    # FLAG — LOG WRITE LOCATION: Currently written via db.log_event() to
    # system_log table. A dedicated signal_decisions table is recommended
    # for regulatory export and volume management. Tracked as future work.
    """

    def __init__(self, ticker, source, source_tier):
        self.ticker      = ticker
        self.source      = source
        self.source_tier = source_tier
        self.steps       = []
        self.decision    = None
        self.confidence  = None
        self.reason      = None
        self.ts          = datetime.now(ET).isoformat()

    def step(self, name, value, note=""):
        self.steps.append({"name": name, "value": str(value), "note": note})
        return self

    def decide(self, decision, confidence, reason):
        self.decision   = decision
        self.confidence = confidence
        self.reason     = reason
        return self

    def to_human(self):
        tier_label = {1: "OFFICIAL", 2: "WIRE", 3: "PRESS", 4: "OPINION"}.get(
            self.source_tier, "UNKNOWN"
        )
        lines = [
            f"SIGNAL CLASSIFICATION — {self.ticker} @ {self.ts}",
            f"  Source    : {self.source} (Tier {self.source_tier} — {tier_label})",
        ]
        for s in self.steps:
            note = f"  [{s['note']}]" if s["note"] else ""
            lines.append(f"  {s['name']:<24}: {s['value']}{note}")
        lines.append(f"  {'DECISION':<24}: {self.decision} | confidence={self.confidence}")
        lines.append(f"  {'REASON':<24}: {self.reason}")
        return "\n".join(lines)

    def to_machine(self):
        return {
            "ts":          self.ts,
            "ticker":      self.ticker,
            "source":      self.source,
            "source_tier": self.source_tier,
            "steps":       self.steps,
            "decision":    self.decision,
            "confidence":  self.confidence,
            "reason":      self.reason,
        }

    def commit(self, db):
        log.info(f"[SIGNAL_DECISION]\n{self.to_human()}")
        try:
            db.log_event(
                "SIGNAL_CLASSIFIED",
                agent="Scout",
                details=json.dumps(self.to_machine()),
            )
        except Exception as e:
            log.debug(f"SignalDecisionLog.commit failed (non-fatal): {e}")


# ── STALENESS CALCULATION ─────────────────────────────────────────────────

def get_staleness(tx_date_str, disc_date_str):
    """Returns staleness label and position discount based on disclosure delay."""
    try:
        tx   = datetime.strptime(tx_date_str,   '%Y-%m-%d')
        disc = datetime.strptime(disc_date_str, '%Y-%m-%d')
        days = (disc - tx).days
    except Exception:
        return "Unknown", 0.0

    if days <= 3:  return "Fresh",   0.0
    if days <= 7:  return "Aging",   0.15
    if days <= 14: return "Stale",   0.30
    return "Expired", 0.50


# ── MEMBER WEIGHT ─────────────────────────────────────────────────────────

def apply_member_weight(base_confidence, member_weight):
    """
    Apply member reliability weight to base confidence score.

    Returns (adjusted_text, adjusted_numeric):
      adjusted_text    — HIGH / MEDIUM / LOW / NOISE (for Bolt's decision rules)
      adjusted_numeric — 0.0–1.5 float (for MIN_SIGNAL_THRESHOLD check)

    Weight floor 0.5, ceiling 1.5. Requires 5+ trades before weight changes from 1.0.
    """
    base_numeric   = CONFIDENCE_NUMERIC.get(base_confidence, 0.0)
    adj_numeric    = round(base_numeric * member_weight, 4)

    if   adj_numeric >= 0.85: adj_text = "HIGH"
    elif adj_numeric >= 0.45: adj_text = "MEDIUM"
    elif adj_numeric >= 0.10: adj_text = "LOW"
    else:                     adj_text = "NOISE"

    return adj_text, adj_numeric


# ── PRICE HISTORY ─────────────────────────────────────────────────────────

def _alpaca_bars(ticker, days):
    """
    Fetch daily OHLCV bars from Alpaca Data API for the past `days` days.
    Returns list of bar dicts or empty list on failure.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []

    end   = datetime.now(ET).strftime('%Y-%m-%dT00:00:00Z')
    start = (datetime.now(ET) - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')

    url = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/bars"
    params = {
        "timeframe": "1Day",
        "start":     start,
        "end":       end,
        "limit":     min(days, 365),   # API max per page
        "feed":      "iex",            # free tier
    }
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception as e:
        log.debug(f"Alpaca bars fetch failed ({ticker}): {e}")
    return []


def _summarise_bars(bars):
    """
    Compute simple stats from a list of bar dicts.
    Returns dict with last_close, change_pct_1yr, high_52w, low_52w, avg_vol.
    Returns None if bars are empty.
    """
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    vols   = [b.get("v", 0) for b in bars]
    last   = closes[-1]
    first  = closes[0]
    high   = max(b["h"] for b in bars)
    low    = min(b["l"] for b in bars)
    avg_v  = round(sum(vols) / len(vols), 0) if vols else 0
    chg    = round((last - first) / first * 100, 2) if first else 0.0
    return {
        "last_close":     round(last, 2),
        "change_pct_1yr": chg,
        "high_52w":       round(high, 2),
        "low_52w":        round(low, 2),
        "avg_volume":     int(avg_v),
        "bars_available": len(bars),
    }


def fetch_price_history_1yr(ticker, industry_etf, sector_etf):
    """
    Pull 1yr daily bars for ticker + industry ETF + sector ETF.
    Returns (summary_dict, tickers_pulled_list).
    Summary is suitable for inclusion in interrogation announcement.
    Caller is responsible for deleting the summary from memory after use.
    """
    tickers_pulled = []
    summaries = {}

    for sym in {ticker, industry_etf, sector_etf}:
        if not sym:
            continue
        bars = _alpaca_bars(sym, days=365)
        s    = _summarise_bars(bars)
        if s:
            summaries[sym] = s
            tickers_pulled.append(sym)
            log.info(f"[PRICE HISTORY] {sym}: {s['bars_available']} bars, "
                     f"{s['change_pct_1yr']:+.1f}% 1yr")
        else:
            log.debug(f"[PRICE HISTORY] No bars for {sym}")

    return summaries, tickers_pulled


def identify_industry_etf(ticker, sector):
    """
    Map a ticker/sector to (industry_ETF, sector_ETF).
    Returns ('', '') if sector is unknown.
    """
    sector_lower = (sector or "").lower()
    for key, (ind_etf, sec_etf) in SECTOR_ETF_MAP.items():
        if key in sector_lower:
            return ind_etf, sec_etf
    # Fallback: look up ticker in SECTOR_TICKER_MAP
    for sec_name, tickers in SECTOR_TICKER_MAP.items():
        if ticker.upper() in tickers:
            etfs = SECTOR_ETF_MAP.get(sec_name.lower(), ('', ''))
            return etfs
    return '', ''


# ── INTERROGATION BROADCAST ───────────────────────────────────────────────

def announce_for_interrogation(signal_id, ticker, price_summary_json):
    """
    Broadcast HAS_DATA_FOR_INTERROGATION on the local network via UDP.
    Waits up to INTERROGATION_TIMEOUT seconds for a peer response.

    Returns True if a peer acknowledged (signal should be marked VALIDATED),
    False if no response (mark UNVALIDATED and proceed anyway).

    Peer logic (corroboration side) is NOT implemented here — Scout only
    announces and listens. If no peer is present, UNVALIDATED is expected.
    """
    message = json.dumps({
        "event":        "HAS_DATA_FOR_INTERROGATION",
        "signal_id":    signal_id,
        "ticker":       ticker,
        "price_summary": price_summary_json,
    }).encode("utf-8")

    response_received = False

    try:
        # Broadcast socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(INTERROGATION_TIMEOUT)

        # Bind a reply port
        reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reply_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reply_sock.settimeout(INTERROGATION_TIMEOUT)
        try:
            reply_sock.bind(('', INTERROGATION_PORT + 1))
        except OSError:
            # Port in use — skip reply listener, will get UNVALIDATED
            pass

        sock.sendto(message, ('<broadcast>', INTERROGATION_PORT))
        log.info(f"[INTERROGATION] Announced {ticker} (signal {signal_id}) — waiting {INTERROGATION_TIMEOUT}s for peer")

        # Wait for a peer acknowledgement
        deadline = time.time() + INTERROGATION_TIMEOUT
        while time.time() < deadline:
            try:
                data, addr = reply_sock.recvfrom(4096)
                reply = json.loads(data.decode("utf-8"))
                if (reply.get("event") == "INTERROGATION_ACK"
                        and str(reply.get("signal_id")) == str(signal_id)):
                    log.info(f"[INTERROGATION] Peer {addr[0]} acknowledged {ticker} — VALIDATED")
                    response_received = True
                    break
            except socket.timeout:
                break
            except Exception:
                break

    except Exception as e:
        log.debug(f"[INTERROGATION] Socket error (non-fatal): {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        try:
            reply_sock.close()
        except Exception:
            pass

    if not response_received:
        log.info(f"[INTERROGATION] No peer response for {ticker} — marking UNVALIDATED")

    return response_received


# ── COMPANY SUBSCRIPTION POST ─────────────────────────────────────────────

def post_to_company_pi(ticker, signal_id, congress_member, adjusted_score,
                       headline, price_summary, interrogation_status):
    """
    Fire-and-forget POST of signal metadata to company Pi news intake.
    Only called when COMPANY_SUBSCRIPTION=true and MONITOR_URL is set.
    Failures are non-fatal and logged at DEBUG level.
    """
    if not COMPANY_SUBSCRIPTION or not MONITOR_URL:
        return

    payload = {
        "event":                "SCOUT_SIGNAL",
        "ticker":               ticker,
        "signal_id":            signal_id,
        "congress_member":      congress_member,
        "adjusted_score":       adjusted_score,
        "headline":             headline,
        "price_summary":        price_summary,
        "interrogation_status": interrogation_status,
        "timestamp":            datetime.now(ET).isoformat(),
    }
    try:
        requests.post(
            f"{MONITOR_URL}/api/news-feed",
            json=payload,
            headers={"X-Token": MONITOR_TOKEN},
            timeout=3,
        )
        log.debug(f"[COMPANY] Metadata posted for {ticker} signal {signal_id}")
    except Exception as e:
        log.debug(f"[COMPANY] Post failed (non-fatal): {e}")


# ── SOURCE FETCHERS ───────────────────────────────────────────────────────

def fetch_capitol_trades():
    """
    Fetch recent congressional trades from Capitol Trades free tier.
    Returns list of disclosure dicts.
    """
    url = "https://capitoltrades.com/api/trades"
    params = {"pageSize": 50, "page": 1}
    r = fetch_with_retry(url, params=params)
    if not r:
        return []

    try:
        data = r.json()
        trades = data.get("data", [])
        results = []
        for t in trades:
            results.append({
                "ticker":     t.get("issuer", {}).get("tickerSymbol", ""),
                "company":    t.get("issuer", {}).get("name", ""),
                "politician": f"{t.get('politician', {}).get('name', '')} ({t.get('politician', {}).get('party', '')}·{t.get('politician', {}).get('chamber', '')})",
                "tx_date":    t.get("txDate", ""),
                "disc_date":  t.get("pubDate", ""),
                "tx_type":    t.get("txType", ""),
                "amount":     t.get("value", ""),
                "source":     "Capitol Trades API",
                "source_tier": 1,
                "source_url":  "https://capitoltrades.com",
            })
        log.info(f"Capitol Trades: fetched {len(results)} disclosures")
        return results
    except Exception as e:
        log.error(f"Capitol Trades parse error: {e}")
        return []


def fetch_congress_gov_activity():
    """
    Fetch recent committee activity and bill advancement from Congress.gov API.
    Returns list of signal dicts.
    """
    if not CONGRESS_API_KEY:
        log.warning("No CONGRESS_API_KEY set — skipping Congress.gov fetch")
        return []

    results = []

    url = "https://api.congress.gov/v3/bill"
    params = {
        "api_key": CONGRESS_API_KEY,
        "format":  "json",
        "limit":   20,
        "sort":    "updateDate+desc",
    }
    r = fetch_with_retry(url, params=params)
    if r:
        try:
            bills = r.json().get("bills", [])
            for bill in bills:
                title = bill.get("title", "")
                if not title:
                    continue
                results.append({
                    "headline":    title,
                    "source":      "Congress.gov API",
                    "source_tier": 1,
                    "source_url":  "https://api.congress.gov",
                    "politician":  bill.get("sponsors", [{}])[0].get("fullName", "Unknown") if bill.get("sponsors") else "Multiple sponsors",
                    "tx_date":     bill.get("introducedDate", ""),
                    "disc_date":   bill.get("updateDate", "")[:10] if bill.get("updateDate") else "",
                    "raw":         bill,
                })
        except Exception as e:
            log.error(f"Congress.gov parse error: {e}")

    log.info(f"Congress.gov: fetched {len(results)} bill actions")
    return results


def fetch_federal_register():
    """
    Fetch recent procurement and regulatory notices from Federal Register.
    Free API — no key required.
    """
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "per_page": 20,
        "order":    "newest",
        "fields[]": ["title", "abstract", "agencies", "publication_date", "html_url"],
        "conditions[type][]": ["Notice", "Proposed Rule"],
    }
    r = fetch_with_retry(url, params=params)
    if not r:
        return []

    try:
        docs = r.json().get("results", [])
        results = []
        for doc in docs:
            results.append({
                "headline":    doc.get("title", ""),
                "subhead":     (doc.get("abstract", "") or "")[:120],
                "source":      "Federal Register API",
                "source_tier": 1,
                "source_url":  doc.get("html_url", "https://www.federalregister.gov/api/v1"),
                "politician":  ", ".join(a.get("name", "") for a in doc.get("agencies", [])[:2]),
                "disc_date":   doc.get("publication_date", ""),
                "tx_date":     doc.get("publication_date", ""),
            })
        log.info(f"Federal Register: fetched {len(results)} notices")
        return results
    except Exception as e:
        log.error(f"Federal Register parse error: {e}")
        return []


def get_rss_feeds():
    """
    Return RSS feed list. Reads from RSS_FEEDS_JSON env var if set,
    otherwise uses built-in defaults. Portal can inject custom feeds.
    Format: [[name, url, tier], ...]
    """
    rss_json = os.environ.get('RSS_FEEDS_JSON', '')
    if rss_json:
        try:
            custom = json.loads(rss_json)
            if isinstance(custom, list) and custom:
                log.info(f"Using {len(custom)} custom RSS feeds from RSS_FEEDS_JSON")
                return custom
        except Exception as e:
            log.warning(f"RSS_FEEDS_JSON parse error — using defaults: {e}")

    return [
        # Tier 2 — Wire
        ["Reuters RSS",          "https://feeds.reuters.com/reuters/politicsNews", 2],
        ["Associated Press RSS", "https://apnews.com/rss",                         2],
        # Tier 3 — Press
        ["Politico RSS",         "https://www.politico.com/rss/politicopicks.xml", 3],
        ["The Hill RSS",         "https://thehill.com/feed",                       3],
        ["Roll Call RSS",        "https://rollcall.com/feed",                      3],
        ["Bloomberg RSS",        "https://feeds.bloomberg.com/politics/news.rss",  3],
    ]


def fetch_rss_feeds():
    """
    Fetch and parse RSS feeds from wire and press sources.
    Returns list of signal dicts with tier assigned by source.
    """
    feeds = get_rss_feeds()

    results = []
    for source_name, url, tier in feeds:
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                log.warning(f"RSS parse issue: {source_name}")
                continue
            for entry in feed.entries[:5]:  # last 5 per feed
                title = entry.get("title", "").strip()
                if not title:
                    continue
                results.append({
                    "headline":    title,
                    "subhead":     entry.get("summary", "")[:120].strip(),
                    "source":      source_name,
                    "source_tier": tier,
                    "source_url":  entry.get("link", url),
                    "politician":  "",
                    "disc_date":   datetime.now().strftime('%Y-%m-%d'),
                    "tx_date":     datetime.now().strftime('%Y-%m-%d'),
                })
            log.info(f"{source_name}: fetched {min(5, len(feed.entries))} articles")
        except Exception as e:
            log.error(f"RSS fetch failed ({source_name}): {e}")

    return results


# ── SIGNAL CLASSIFICATION ─────────────────────────────────────────────────

def _check_tier1_corroboration(ticker, db):
    """
    Returns True if a non-expired Tier 1 signal for this ticker exists.
    Used by classify_signal() to elevate Tier 2/3 signals.
    """
    try:
        with db.conn() as c:
            row = c.execute("""
                SELECT id FROM signals
                WHERE ticker = ?
                  AND source_tier = 1
                  AND (expires_at IS NULL OR expires_at > ?)
                LIMIT 1
            """, (ticker, db.now())).fetchone()
        return row is not None
    except Exception:
        return False


def classify_signal(signal, db):
    """
    Deterministic rule-based signal classification.
    Replaces: validate_signal_with_claude()

    Tier rules:
      Tier 1 (Official) → auto-HIGH / QUEUE  — handled in run() before this call
      Tier 4 (Opinion)  → auto-NOISE / DISCARD — handled in run() before this call
      Tier 2 (Wire)     → HIGH if Tier 1 corroborated; MEDIUM/WATCH otherwise
      Tier 3 (Press)    → MEDIUM always; QUEUE only with Tier 1 corroboration

    Returns: (decision, confidence, reason)
    All steps logged to SignalDecisionLog.
    """
    sdl = SignalDecisionLog(
        ticker      = signal.get("ticker", "UNKNOWN"),
        source      = signal.get("source", ""),
        source_tier = signal.get("source_tier", 2),
    )

    source_tier = signal.get("source_tier", 2)
    staleness   = signal.get("staleness", "Unknown")
    is_spousal  = signal.get("is_spousal", False)
    is_amended  = signal.get("is_amended", False)
    ticker      = signal.get("ticker")

    sdl.step("staleness",  staleness)
    sdl.step("is_spousal", is_spousal)
    sdl.step("is_amended", is_amended)

    # ── Hard discard conditions ────────────────────────────────────────────
    if not ticker:
        sdl.decide("DISCARD", "NOISE", "No ticker resolved for this signal")
        sdl.commit(db)
        return sdl.decision, sdl.confidence, sdl.reason

    if is_spousal:
        sdl.decide("DISCARD", "LOW",
                   "Spousal/dependent filing — reduced legislative signal quality")
        sdl.commit(db)
        return sdl.decision, sdl.confidence, sdl.reason

    if staleness == "Expired":
        sdl.decide("DISCARD", "LOW",
                   "Signal expired (>14 days between transaction and disclosure)")
        sdl.commit(db)
        return sdl.decision, sdl.confidence, sdl.reason

    # ── Corroboration check ────────────────────────────────────────────────
    corroborated = _check_tier1_corroboration(ticker, db)
    sdl.step("tier1_corroboration", corroborated,
             note="Tier 1 official signal exists for ticker" if corroborated else "No Tier 1 signal found")

    # ── Tier 2: Wire sources (Reuters, AP) ────────────────────────────────
    if source_tier == 2:
        if corroborated:
            sdl.decide("QUEUE", "HIGH",
                       "Tier 2 wire + Tier 1 official corroboration confirmed")
        elif staleness in ("Fresh", "Aging"):
            sdl.decide("WATCH", "MEDIUM",
                       "Tier 2 wire — no Tier 1 corroboration yet; watching")
        else:
            sdl.decide("DISCARD", "LOW",
                       "Tier 2 wire — stale (>7 days) and uncorroborated")
        sdl.commit(db)
        return sdl.decision, sdl.confidence, sdl.reason

    # ── Tier 3: Press sources (Politico, The Hill, Bloomberg) ─────────────
    if source_tier == 3:
        if corroborated and staleness in ("Fresh", "Aging"):
            sdl.decide("QUEUE", "MEDIUM",
                       "Tier 3 press + Tier 1 backup — within freshness window")
        elif staleness in ("Fresh", "Aging"):
            sdl.decide("WATCH", "MEDIUM",
                       "Tier 3 press — monitoring for Tier 1 corroboration")
        else:
            sdl.decide("DISCARD", "LOW",
                       "Tier 3 press — stale or outside freshness window")
        sdl.commit(db)
        return sdl.decision, sdl.confidence, sdl.reason

    # ── Fallback for unrecognised tier ─────────────────────────────────────
    sdl.decide("WATCH", "LOW", f"Unrecognised source tier {source_tier} — defaulting to WATCH")
    sdl.commit(db)
    return sdl.decision, sdl.confidence, sdl.reason


# ── TICKER EXTRACTION ─────────────────────────────────────────────────────

def extract_ticker_from_headline(headline, existing_ticker=None):
    """
    Returns existing ticker if provided, otherwise tries to infer from headline.
    """
    if existing_ticker and existing_ticker.upper() not in ("", "UNKNOWN"):
        return existing_ticker.upper()

    headline_lower = headline.lower()
    for sector, tickers in SECTOR_TICKER_MAP.items():
        if sector in headline_lower:
            return tickers[0]

    import re
    matches = re.findall(r'\b([A-Z]{2,5})\b', headline)
    for m in matches:
        for tickers in SECTOR_TICKER_MAP.values():
            if m in tickers:
                return m

    return None


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run(session="market"):
    db  = get_db()
    now = datetime.now(ET)
    log.info(f"Scout starting — session={session} time={now.strftime('%H:%M ET')}")

    db.log_event("AGENT_START", agent="Scout", details=f"session={session}")
    db.log_heartbeat("agent2_research", "RUNNING")

    # ── STEP 1: Expire stale signals from previous runs
    db.expire_old_signals()

    # ── STEP 2: Fetch all sources
    all_raw = []

    disclosures = fetch_capitol_trades()
    all_raw.extend(disclosures)

    if session == "market":
        # Full scan during market hours
        congress = fetch_congress_gov_activity()
        all_raw.extend(congress)

        fed_reg = fetch_federal_register()
        all_raw.extend(fed_reg)

        rss = fetch_rss_feeds()
        all_raw.extend(rss)
    else:
        # Overnight: disclosures + congress only (save API calls)
        congress = fetch_congress_gov_activity()
        all_raw.extend(congress)

    log.info(f"Fetched {len(all_raw)} raw items across all sources")

    # ── STEP 3: Process each item
    new_signals  = 0
    queued       = 0
    discarded    = 0
    skipped      = 0
    below_thresh = 0

    for item in all_raw:
        headline = item.get("headline", "").strip()
        if not headline or len(headline) < 10:
            skipped += 1
            continue

        ticker = extract_ticker_from_headline(
            headline, existing_ticker=item.get("ticker")
        )
        if not ticker:
            log.debug(f"No ticker found for: {headline[:60]}")
            skipped += 1
            continue

        is_amended  = bool(item.get("is_amended")) or any(w in headline.lower() for w in ["amend", "corrected", "revised"])
        is_spousal  = bool(item.get("is_spousal")) or any(w in (item.get("politician","")).lower() for w in ["spouse", "joint", "dependent"])

        staleness, discount = get_staleness(
            item.get("tx_date", ""),
            item.get("disc_date", datetime.now().strftime('%Y-%m-%d')),
        )

        congress_member = item.get("politician", "")
        source_tier     = item.get("source_tier", 2)
        source_label    = "CONGRESS" if source_tier == 1 else "RSS"

        # ── Determine base confidence ──────────────────────────────────
        if source_tier == 1:
            base_confidence = "HIGH"
            signal_decision = "QUEUE"
            signal_reason   = "Tier 1 official source — auto-HIGH"
        elif source_tier == 4:
            base_confidence = "NOISE"
            signal_decision = "DISCARD"
            signal_reason   = "Tier 4 opinion source — auto-DISCARD"
        else:
            item["staleness"]  = staleness
            item["ticker"]     = ticker
            item["is_amended"] = is_amended
            item["is_spousal"] = is_spousal
            signal_decision, base_confidence, signal_reason = classify_signal(item, db)

        # ── Apply member weight → adjusted score ──────────────────────
        member_data      = db.get_member_weight(congress_member)
        member_weight    = member_data['weight']
        adj_text, adj_numeric = apply_member_weight(base_confidence, member_weight)

        log.info(
            f"{ticker} base={base_confidence} weight={member_weight:.2f} "
            f"adj={adj_text}({adj_numeric:.3f}) decision={signal_decision}"
        )

        # ── Identify sector + ETFs ─────────────────────────────────────
        sector       = item.get("sector", "")
        ind_etf, sec_etf = identify_industry_etf(ticker, sector)

        # ── Write to news_feed (all signals, regardless of decision) ───
        try:
            db.write_news_feed_entry(
                congress_member = congress_member,
                ticker          = ticker,
                signal_score    = adj_text,
                sentiment_score = None,   # Pulse hasn't run yet; will annotate later
                raw_headline    = headline,
                metadata        = {
                    "source":         item.get("source"),
                    "source_tier":    source_tier,
                    "staleness":      staleness,
                    "base_confidence": base_confidence,
                    "member_weight":   member_weight,
                    "adj_numeric":     adj_numeric,
                    "is_amended":      is_amended,
                    "is_spousal":      is_spousal,
                    "ind_etf":         ind_etf,
                    "sec_etf":         sec_etf,
                },
                source = source_label,
            )
        except Exception as e:
            log.warning(f"news_feed write failed (non-fatal): {e}")

        # ── Discard path ───────────────────────────────────────────────
        if signal_decision == "DISCARD" or adj_text == "NOISE":
            db.upsert_signal(
                ticker=ticker,
                source=item.get("source"),
                source_tier=source_tier,
                headline=headline,
                confidence="NOISE",
                staleness=staleness,
                tx_date=item.get("tx_date"),
                disc_date=item.get("disc_date"),
                is_amended=is_amended,
                is_spousal=is_spousal,
            )
            discarded += 1
            continue

        # ── Threshold check ────────────────────────────────────────────
        if adj_numeric < MIN_SIGNAL_THRESHOLD:
            log.info(f"{ticker} below MIN_SIGNAL_THRESHOLD ({adj_numeric:.3f} < {MIN_SIGNAL_THRESHOLD}) — dropping")
            below_thresh += 1
            continue

        # ── Pull 1yr price history ─────────────────────────────────────
        price_summary, tickers_pulled = fetch_price_history_1yr(ticker, ind_etf, sec_etf)
        price_history_used = ",".join(tickers_pulled) if tickers_pulled else ""

        # ── Write signal to DB ─────────────────────────────────────────
        sig_id = db.upsert_signal(
            ticker=ticker,
            company=item.get("company"),
            sector=sector,
            source=item.get("source"),
            source_tier=source_tier,
            headline=headline,
            politician=congress_member,
            tx_date=item.get("tx_date"),
            disc_date=item.get("disc_date"),
            amount_range=str(item.get("amount", "")),
            confidence=adj_text,          # store adjusted confidence
            staleness=staleness,
            corroborated=(source_tier == 1),
            corroboration_note=item.get("corroboration_note"),
            is_amended=is_amended,
            is_spousal=is_spousal,
        )

        if not sig_id:
            continue

        new_signals += 1

        # Annotate with adjusted score and price history tickers
        try:
            with db.conn() as c:
                c.execute("""
                    UPDATE signals
                    SET entry_signal_score  = ?,
                        price_history_used  = ?,
                        updated_at          = ?
                    WHERE id = ?
                """, (adj_text, price_history_used, db.now(), sig_id))
        except Exception as e:
            log.warning(f"Signal annotation failed (non-fatal): {e}")

        # ── Announce for interrogation ─────────────────────────────────
        price_summary_for_announce = {
            k: v for k, v in price_summary.items()
        } if price_summary else {}

        validated = announce_for_interrogation(sig_id, ticker, price_summary_for_announce)
        interrogation_status = "VALIDATED" if validated else "UNVALIDATED"

        # Delete price history from memory
        del price_summary
        del price_summary_for_announce

        # Write interrogation status to signal
        try:
            with db.conn() as c:
                c.execute("""
                    UPDATE signals SET interrogation_status = ?, updated_at = ? WHERE id = ?
                """, (interrogation_status, db.now(), sig_id))
        except Exception as e:
            log.warning(f"interrogation_status write failed (non-fatal): {e}")

        # ── Post to company Pi ─────────────────────────────────────────
        post_to_company_pi(
            ticker               = ticker,
            signal_id            = sig_id,
            congress_member      = congress_member,
            adjusted_score       = adj_text,
            headline             = headline,
            price_summary        = None,   # already deleted above
            interrogation_status = interrogation_status,
        )

        # ── Pass to Bolt ───────────────────────────────────────────────
        # All signals above threshold go to Bolt; Bolt applies Option B rules
        # to decide MIRROR / WATCH / SKIP based on adjusted score + pulse data.
        if signal_decision in ("QUEUE", "WATCH"):
            db.queue_signal_for_trader(sig_id)
            queued += 1
            log.info(f"Queued for Bolt: {ticker} adj={adj_text} {interrogation_status}")
        else:
            db.discard_signal(sig_id, reason=signal_reason)
            discarded += 1

    # ── STEP 4: Re-evaluate signals that were previously WATCHING
    reeval_count = 0
    try:
        with db.conn() as c:
            watching = c.execute("""
                SELECT * FROM signals
                WHERE status IN ('PENDING','WATCHING')
                AND source_tier IN (2, 3)
                AND needs_reeval = 1
                AND expires_at > ?
                ORDER BY created_at ASC
                LIMIT 10
            """, (db.now(),)).fetchall()
            watching = [dict(r) for r in watching]

        for sig in watching:
            log.info(f"Re-evaluating WATCH signal: {sig['ticker']} T{sig['source_tier']} (id={sig['id']})")
            decision, confidence, reason = classify_signal(sig, db)
            reeval_count += 1

            with db.conn() as c:
                c.execute("UPDATE signals SET needs_reeval=0, updated_at=? WHERE id=?",
                          (db.now(), sig['id']))

            if decision == 'QUEUE':
                db.queue_signal_for_trader(sig['id'])
                queued += 1
                log.info(f"Re-eval promoted to queue: {sig['ticker']}")
            elif decision == 'DISCARD':
                db.discard_signal(sig['id'], reason=reason)
                discarded += 1
                log.info(f"Re-eval discarded: {sig['ticker']}")

    except Exception as e:
        log.warning(f"Re-evaluation step failed: {e}")

    portfolio = db.get_portfolio()
    log.info(
        f"Run complete — new={new_signals} queued={queued} "
        f"discarded={discarded} below_threshold={below_thresh} "
        f"skipped={skipped} reeval={reeval_count} "
        f"portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("agent2_research", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="Scout",
        details=f"new={new_signals} queued={queued} discarded={discarded} below_threshold={below_thresh}",
        portfolio_value=portfolio['cash'],
    )

    # Post heartbeat to monitor server
    try:
        from heartbeat import write_heartbeat
        write_heartbeat(agent_name="agent2_research", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — Scout (Research Agent)')
    parser.add_argument('--session', choices=['market','overnight'], default='market',
                        help='market=full scan, overnight=disclosures+congress only')
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — check .env file")
        sys.exit(1)

    acquire_agent_lock("agent2_research.py")
    try:
        run(session=args.session)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
