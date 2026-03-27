"""
agent2_research.py — The Daily (Research Agent)
Synthos · Agent 2

Runs:
  - Every hour during market hours (9am-4pm ET weekdays)
  - Every 4 hours overnight

Responsibilities:
  - Fetch congressional disclosures from free APIs
  - Validate signals against tier rules
  - Corroborate Tier 2/3 signals before queuing
  - Queue validated HIGH confidence signals for the Trader
  - Re-evaluate queued signals that may have been contradicted
  - Expire stale signals

Strict scope: political and legislative signals ONLY.
No market data, no sentiment — that is The Pulse's job.

Usage:
  python3 agent2_research.py
  python3 agent2_research.py --session=overnight
"""

import os
import sys
import json
import time
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
ANTHROPIC_API_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
CONGRESS_API_KEY   = os.environ.get('CONGRESS_API_KEY', '')
CLAUDE_MODEL       = "claude-sonnet-4-20250514"
ET                 = ZoneInfo("America/New_York")
MAX_RETRIES        = 3
REQUEST_TIMEOUT    = 10   # seconds per HTTP request

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('agent2_research')


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


def call_claude(prompt, max_tokens=700):
    """Call Claude API with retry. Returns text or None."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=30,
            )
            if r.status_code == 429:
                wait = 2 ** (attempt + 2)
                log.warning(f"Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            return "".join(b.get("text", "") for b in data.get("content", []))
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 2)
    log.error(f"Claude API failed after {MAX_RETRIES} attempts: {last_error}")
    return None


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

    # Recent bill actions
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
    import json
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


# ── SIGNAL VALIDATION ─────────────────────────────────────────────────────

def validate_signal_with_claude(signal, db):
    """
    Ask Claude to validate a signal and determine if it should be
    queued for the trader, watched, or discarded.
    Returns: ("QUEUE" | "WATCH" | "DISCARD", confidence, summary)
    """
    tier_label = {1:"OFFICIAL", 2:"WIRE", 3:"PRESS", 4:"OPINION"}.get(signal.get("source_tier", 2), "WIRE")

    # Build learning context from recent outcomes
    outcomes = db.get_recent_outcomes(limit=5)
    outcome_context = ""
    if outcomes:
        lines = [f"  - {o['ticker']} {o['verdict']} {o.get('pnl_pct',0):+.1f}% ({o.get('staleness','?')} T{o.get('signal_tier','?')})" for o in outcomes]
        outcome_context = "RECENT TRADE OUTCOMES (learning context):\n" + "\n".join(lines)

    prompt = f"""You are The Daily — the Research Agent for Synthos, a conservative congressional trade following system.

STRICT SCOPE: You analyze political and legislative signals ONLY.
You do NOT analyze market sentiment, price action, or volume.
You do NOT execute trades. Your output goes to the Trader agent.

SIGNAL TO VALIDATE:
Source: {signal.get('source')} (Tier {signal.get('source_tier')} — {tier_label})
Headline: "{signal.get('headline', '')}"
Subhead: "{signal.get('subhead', '')}"
Politician: {signal.get('politician', 'Unknown')}
Ticker: {signal.get('ticker', 'Unknown')}
Sector: {signal.get('sector', 'Unknown')}
Transaction date: {signal.get('tx_date', 'Unknown')}
Disclosure date: {signal.get('disc_date', 'Unknown')}
Staleness: {signal.get('staleness', 'Unknown')}
Corroborated: {signal.get('corroborated', False)}
Corroboration note: {signal.get('corroboration_note', 'N/A')}

TIER RULES:
- Tier 1 (Official: Congress.gov, SEC, Federal Register) → auto-HIGH, queue immediately
- Tier 2 (Wire: Reuters, AP) → HIGH only if corroborated by official source
- Tier 3 (Press: Politico, The Hill) → MEDIUM, needs Tier 1 or 2 backup
- Tier 4 (Opinion/Social) → NOISE, always discard

{outcome_context}

Analyze (be concise):
1. Source credibility — factual or sensational?
2. Political mechanism — plausible path from this news to the ticker?
3. Manipulation check — could this be coordinated price movement?
4. Confidence — HIGH / MEDIUM / LOW / NOISE
5. Decision — QUEUE / WATCH / DISCARD

End your response with exactly one line:
DECISION: QUEUE
or
DECISION: WATCH
or
DECISION: DISCARD"""

    response = call_claude(prompt)
    if not response:
        return "WATCH", "MEDIUM", "Claude unavailable — defaulting to WATCH"

    # Extract decision
    decision = "WATCH"
    for line in response.strip().split('\n'):
        if line.startswith("DECISION:"):
            d = line.replace("DECISION:", "").strip().upper()
            if d in ("QUEUE", "WATCH", "DISCARD"):
                decision = d
            break

    # Extract confidence
    confidence = "MEDIUM"
    for conf in ("HIGH", "MEDIUM", "LOW", "NOISE"):
        if conf in response.upper():
            confidence = conf
            break

    return decision, confidence, response[:500]


# ── TICKER EXTRACTION ─────────────────────────────────────────────────────

# Known congressional trading sectors mapped to representative tickers
# On Pi this would use a proper NLP entity extractor — simplified here
SECTOR_TICKER_MAP = {
    "defense":     ["LMT", "RTX", "NOC", "GD", "BA", "KTOS", "LHX"],
    "technology":  ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMD", "INTC"],
    "healthcare":  ["LLY", "JNJ", "UNH", "PFE", "ABBV", "MRK"],
    "energy":      ["XOM", "CVX", "NEE", "SO", "DUK"],
    "financials":  ["JPM", "BAC", "WFC", "GS", "KRE"],
    "materials":   ["MP", "ALB", "FCX", "NEM"],
    "industrials": ["DE", "CAT", "GE", "HON"],
}

def extract_ticker_from_headline(headline, existing_ticker=None):
    """
    Returns existing ticker if provided, otherwise tries to infer from headline.
    On Pi this would call an NLP service — simplified keyword matching here.
    """
    if existing_ticker and existing_ticker.upper() not in ("", "UNKNOWN"):
        return existing_ticker.upper()

    headline_lower = headline.lower()
    for sector, tickers in SECTOR_TICKER_MAP.items():
        if sector in headline_lower:
            return tickers[0]   # return most representative ticker for sector

    # Check for direct ticker mentions
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
    log.info(f"The Daily starting — session={session} time={now.strftime('%H:%M ET')}")

    db.log_event("AGENT_START", agent="The Daily", details=f"session={session}")
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
    new_signals = 0
    queued      = 0
    discarded   = 0
    skipped     = 0

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

        # Detect edge cases
        is_amended  = bool(item.get("is_amended")) or any(w in headline.lower() for w in ["amend", "corrected", "revised"])
        is_spousal  = bool(item.get("is_spousal")) or any(w in (item.get("politician","")).lower() for w in ["spouse", "joint", "dependent"])

        # Staleness
        staleness, discount = get_staleness(
            item.get("tx_date", ""),
            item.get("disc_date", datetime.now().strftime('%Y-%m-%d')),
        )

        # Tier 1 items auto-pass — no Claude call needed
        source_tier = item.get("source_tier", 2)
        if source_tier == 1:
            sig_id = db.upsert_signal(
                ticker=ticker,
                company=item.get("company"),
                sector=item.get("sector"),
                source=item.get("source"),
                source_tier=1,
                headline=headline,
                politician=item.get("politician"),
                tx_date=item.get("tx_date"),
                disc_date=item.get("disc_date"),
                amount_range=str(item.get("amount", "")),
                confidence="HIGH",
                staleness=staleness,
                corroborated=True,
                is_amended=is_amended,
                is_spousal=is_spousal,
            )
            if sig_id:
                db.queue_signal_for_trader(sig_id)
                queued += 1
                new_signals += 1
            continue

        # Tier 4 always discarded — no Claude call
        if source_tier == 4:
            db.upsert_signal(
                ticker=ticker,
                source=item.get("source"),
                source_tier=4,
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

        # Tier 2/3 — validate with Claude
        item["staleness"]  = staleness
        item["ticker"]     = ticker
        item["is_amended"] = is_amended
        item["is_spousal"] = is_spousal

        decision, confidence, summary = validate_signal_with_claude(item, db)

        sig_id = db.upsert_signal(
            ticker=ticker,
            company=item.get("company"),
            sector=item.get("sector"),
            source=item.get("source"),
            source_tier=source_tier,
            headline=headline,
            politician=item.get("politician"),
            tx_date=item.get("tx_date"),
            disc_date=item.get("disc_date"),
            confidence=confidence,
            staleness=staleness,
            corroboration_note=item.get("corroboration_note"),
            is_amended=is_amended,
            is_spousal=is_spousal,
        )

        if sig_id:
            new_signals += 1
            if decision == "QUEUE":
                db.queue_signal_for_trader(sig_id)
                queued += 1
            elif decision == "DISCARD":
                db.discard_signal(sig_id, reason=f"Claude: {decision}")
                discarded += 1
            else:
                # WATCH — flag for re-evaluation on next run
                try:
                    with db.conn() as c:
                        c.execute("UPDATE signals SET needs_reeval=1, updated_at=? WHERE id=?",
                                  (db.now(), sig_id))
                except Exception:
                    pass

    # ── STEP 4: Re-evaluate signals that were previously WATCHING
    # Pull Tier 2/3 PENDING signals and re-run Claude analysis with fresh context
    # This catches signals that were WATCH last run but may have new corroboration
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
            decision, confidence, summary = validate_signal_with_claude(sig, db)
            reeval_count += 1

            with db.conn() as c:
                c.execute("UPDATE signals SET needs_reeval=0, updated_at=? WHERE id=?",
                          (db.now(), sig['id']))

            if decision == 'QUEUE':
                db.queue_signal_for_trader(sig['id'])
                queued += 1
                log.info(f"Re-eval promoted to queue: {sig['ticker']}")
            elif decision == 'DISCARD':
                db.discard_signal(sig['id'], reason="Re-eval: DISCARD")
                discarded += 1
                log.info(f"Re-eval discarded: {sig['ticker']}")
            # WATCH again — stays pending, will be checked next run

            time.sleep(1)  # rate limit buffer

    except Exception as e:
        log.warning(f"Re-evaluation step failed: {e}")

    portfolio = db.get_portfolio()
    log.info(
        f"Run complete — new={new_signals} queued={queued} "
        f"discarded={discarded} skipped={skipped} reeval={reeval_count} "
        f"portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("agent2_research", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="The Daily",
        details=f"new={new_signals} queued={queued} discarded={discarded}",
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
    parser = argparse.ArgumentParser(description='Synthos — The Daily (Research Agent)')
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
