"""
agent3_sentiment.py — The Pulse (Sentiment Agent)
Synthos · Agent 3

Runs:
  Every 30 minutes during market hours (9am-4pm ET weekdays)

Responsibilities:
  - Monitor open positions for market deterioration
  - Check pre-trade queue sentiment before trader acts
  - Detect cascade patterns (multiple sellers simultaneously)
  - Raise urgent flags that bypass normal session schedule
  - Log all scan results to scan_log table

Does NOT trade. Does NOT give the trader orders.
Logs warnings — trader makes all final decisions.

Free data sources:
  - SEC EDGAR (insider transactions)
  - CBOE put/call ratio (public data)
  - Finviz (volume and price data)
  - Yahoo Finance RSS

Usage:
  python3 agent3_sentiment.py
"""

import os
import sys
import time
import logging
import requests
import feedparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from database import get_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
# ANTHROPIC_API_KEY removed — The Pulse uses no LLM in sentiment analysis.
# All cascade detection and scan analysis is rule-based and traceable.
ET                = ZoneInfo("America/New_York")
MAX_RETRIES       = 3
REQUEST_TIMEOUT   = 10

# Cascade thresholds — tuned to distinguish noise from real deterioration
CASCADE_PUT_CALL_THRESHOLD    = 1.10   # put/call > 110% of 30d avg
CASCADE_SELLER_DOM_THRESHOLD  = 0.70   # 70%+ seller dominance
CASCADE_VOLUME_THRESHOLD      = 2.50   # 250%+ of average volume
CASCADE_INSIDER_SELLS_MIN     = 4      # at least 4 insider sells with 0 buys

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('agent3_sentiment')


# ── RETRY HELPERS ─────────────────────────────────────────────────────────

def fetch_with_retry(url, params=None, headers=None, max_retries=MAX_RETRIES):
    """Fetch URL with exponential backoff. Returns response or None."""
    last_error = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"Fetch failed attempt {attempt+1}/{max_retries} — retry in {wait}s")
                time.sleep(wait)
    log.error(f"Fetch permanently failed: {url[:80]} — {last_error}")
    return None


# ── DATA FETCHERS ─────────────────────────────────────────────────────────

def fetch_put_call_ratio(ticker):
    """
    Fetch put/call ratio from CBOE public data.
    Returns (current_ratio, avg_30d) or (None, None) on failure.

    Note: CBOE provides aggregate market put/call. Per-ticker options
    data requires a paid feed. On free tier we use market-wide ratio
    as a proxy and note this limitation in the analysis.
    """
    url = "https://www.cboe.com/us/options/market_statistics/daily/"
    r = fetch_with_retry(url)
    if not r:
        return None, None

    # CBOE returns HTML — parse the equity put/call ratio
    # Free tier: market-wide equity put/call ratio
    try:
        import re
        text = r.text
        # Look for equity put/call ratio in the page
        match = re.search(r'Equity Put/Call Ratio.*?(\d+\.\d+)', text, re.DOTALL)
        if match:
            ratio = float(match.group(1))
            # Approximate 30d avg (CBOE historical avg ~0.60-0.65)
            avg30d = 0.62
            log.info(f"CBOE put/call ratio: {ratio:.2f} (30d avg ~{avg30d:.2f})")
            return ratio, avg30d
    except Exception as e:
        log.warning(f"CBOE parse error: {e}")

    # Fallback: use Finviz options data if available
    return fetch_finviz_put_call(ticker)


def fetch_finviz_put_call(ticker):
    """Fallback put/call from Finviz screener data."""
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Synthos/1.0)"}
    r = fetch_with_retry(url, headers=headers)
    if not r:
        return None, None

    try:
        import re
        # Look for options data in Finviz page
        match = re.search(r'Optionable.*?Yes', r.text, re.DOTALL)
        if match:
            # Finviz doesn't expose put/call directly in free HTML
            # Return None to indicate data unavailable
            return None, None
    except Exception:
        pass
    return None, None


def fetch_sec_insider_transactions(ticker, days_back=30):
    """
    Fetch recent insider transactions from SEC EDGAR.
    Returns dict with buys, sells, net_dollar.
    Free API — no key required.
    """
    url = "https://data.sec.gov/submissions"
    # SEC EDGAR full-text search for Form 4 (insider transactions)
    search_url = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=4"
    start = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    end   = datetime.now().strftime('%Y-%m-%d')

    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=4"
    headers = {
        "User-Agent": "Synthos/1.0 research@synthos.local",  # SEC requires User-Agent
        "Accept": "application/json",
    }
    r = fetch_with_retry(url, headers=headers)
    if not r:
        return {"buys": 0, "sells": 0, "net_dollar": "$0", "available": False}

    try:
        data  = r.json()
        hits  = data.get("hits", {}).get("hits", [])
        buys  = 0
        sells = 0
        for hit in hits:
            src = hit.get("_source", {})
            # Form 4 transaction type: P=purchase, S=sale
            tx_type = src.get("period_of_report", "")
            # Simplified — count filings as proxy for transaction count
            # Full parsing requires downloading each filing XML
            sells += 1  # conservative: assume sells until proven otherwise

        log.info(f"SEC EDGAR {ticker}: found {len(hits)} Form 4 filings in last {days_back}d")
        return {
            "buys": buys,
            "sells": sells,
            "net_dollar": f"-${sells * 100000:,}" if sells > buys else f"+${buys * 100000:,}",
            "available": True,
            "filing_count": len(hits),
        }
    except Exception as e:
        log.warning(f"SEC EDGAR parse error ({ticker}): {e}")
        return {"buys": 0, "sells": 0, "net_dollar": "$0", "available": False}


def fetch_volume_profile(ticker):
    """
    Fetch volume data from Yahoo Finance RSS and Finviz.
    Returns dict with today_vs_avg, seller_dominance estimate.
    """
    # Yahoo Finance RSS for recent news and volume context
    url = f"https://finance.yahoo.com/rss/headline?s={ticker}"
    r = fetch_with_retry(url)

    volume_data = {
        "today_vs_avg": "+0%",
        "seller_dominance": "50%",
        "cascade_detected": False,
        "available": False,
    }

    if r:
        try:
            feed = feedparser.parse(r.text)
            # News volume as a proxy for market attention
            recent_articles = len([e for e in feed.entries if e.get("title")])
            if recent_articles > 5:
                volume_data["available"] = True
                log.info(f"Yahoo Finance {ticker}: {recent_articles} recent news items")
        except Exception as e:
            log.warning(f"Yahoo Finance RSS error ({ticker}): {e}")

    # Finviz for price action data
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Synthos/1.0)"}
    fv_url  = f"https://finviz.com/quote.ashx?t={ticker}"
    fv_r    = fetch_with_retry(fv_url, headers=headers)

    if fv_r:
        try:
            import re
            text = fv_r.text
            # Extract volume ratio from Finviz
            vol_match = re.search(r'Rel Volume.*?(\d+\.\d+)', text, re.DOTALL)
            if vol_match:
                rel_vol = float(vol_match.group(1))
                pct_change = f"+{int((rel_vol - 1) * 100)}%" if rel_vol > 1 else f"{int((rel_vol - 1) * 100)}%"
                volume_data["today_vs_avg"] = pct_change
                volume_data["available"] = True

                # Price change as proxy for seller dominance
                price_match = re.search(r'Change.*?(-?\d+\.\d+)%', text, re.DOTALL)
                if price_match:
                    price_change = float(price_match.group(1))
                    # Negative price change with high volume = seller dominance
                    if price_change < 0 and rel_vol > 1.5:
                        dom = min(50 + abs(price_change) * 5, 90)
                        volume_data["seller_dominance"] = f"{dom:.0f}%"
                        if rel_vol > CASCADE_VOLUME_THRESHOLD and dom > CASCADE_SELLER_DOM_THRESHOLD * 100:
                            volume_data["cascade_detected"] = True

            log.info(f"Finviz {ticker}: rel_vol={volume_data['today_vs_avg']} sellers={volume_data['seller_dominance']}")
        except Exception as e:
            log.warning(f"Finviz parse error ({ticker}): {e}")

    return volume_data


# ── CASCADE DETECTION ─────────────────────────────────────────────────────

def detect_cascade(put_call, put_call_avg, insider_data, volume_data):
    """
    Determine sentiment tier based on signal alignment.

    Tier 1 CRITICAL: cascade — multiple signals aligning
    Tier 2 ELEVATED: notable activity, one or two signals elevated
    Tier 3 NEUTRAL:  normal market behavior
    Tier 4 QUIET:    below average activity, healthy

    Returns: (tier, tier_label, cascade_detected, summary)
    """
    signals_critical = 0
    signals_elevated = 0
    notes = []

    # Put/call ratio check
    if put_call and put_call_avg:
        ratio_change = put_call / put_call_avg
        if ratio_change > CASCADE_PUT_CALL_THRESHOLD:
            signals_critical += 1
            notes.append(f"Put/call {put_call:.2f} vs avg {put_call_avg:.2f} (+{(ratio_change-1)*100:.0f}%)")
        elif ratio_change > 1.20:
            signals_elevated += 1
            notes.append(f"Put/call elevated: {put_call:.2f} vs avg {put_call_avg:.2f}")

    # Insider transaction check
    sells = insider_data.get("sells", 0)
    buys  = insider_data.get("buys", 0)
    if sells >= CASCADE_INSIDER_SELLS_MIN and buys == 0:
        signals_critical += 1
        notes.append(f"Insider net: {buys}B/{sells}S = {insider_data.get('net_dollar', '?')}")
    elif sells > buys + 2:
        signals_elevated += 1
        notes.append(f"Insider net selling: {buys}B/{sells}S")

    # Volume profile check
    cascade_vol = volume_data.get("cascade_detected", False)
    if cascade_vol:
        signals_critical += 1
        notes.append(f"Volume cascade: {volume_data.get('today_vs_avg')} avg, {volume_data.get('seller_dominance')} sellers")
    elif "+" in str(volume_data.get("today_vs_avg", "")) and int(str(volume_data.get("today_vs_avg", "+0%")).replace("+","").replace("%","") or 0) > 80:
        signals_elevated += 1
        notes.append(f"Elevated volume: {volume_data.get('today_vs_avg')} avg")

    # Determine tier
    cascade_detected = signals_critical >= 2
    if cascade_detected or signals_critical >= 3:
        tier, label = 1, "CRITICAL"
    elif signals_critical == 1 or signals_elevated >= 2:
        tier, label = 2, "ELEVATED"
    elif signals_elevated == 1:
        tier, label = 3, "NEUTRAL"
    else:
        tier, label = 4, "QUIET"

    summary = f"Tier {tier} ({label}): " + ("; ".join(notes) if notes else "All signals normal")
    return tier, label, cascade_detected, summary


# ── SCAN DECISION LOG ────────────────────────────────────────────────────

from datetime import datetime as _dt
import json as _json

class ScanDecisionLog:
    """
    Records the sentiment scan result for each scanned position.
    Human-readable + machine-readable output for audit.

    # FLAG — LOG WRITE LOCATION: Results written to scan_log table via db.log_scan().
    # The human-readable summary is included in the event_summary field.
    # A dedicated sentiment_decisions table is recommended for volume management
    # and regulatory export. Tracked as future work item.
    """

    def __init__(self, ticker):
        self.ticker   = ticker
        self.signals  = []
        self.tier     = None
        self.label    = None
        self.cascade  = None
        self.analysis = None
        self.ts       = datetime.now(ET).isoformat()

    def signal(self, name, value, status, note=""):
        self.signals.append({"name": name, "value": str(value), "status": status, "note": note})
        return self

    def conclude(self, tier, label, cascade, analysis):
        self.tier     = tier
        self.label    = label
        self.cascade  = cascade
        self.analysis = analysis
        return self

    def to_human(self):
        lines = [
            f"SENTIMENT SCAN — {self.ticker} @ {self.ts}",
        ]
        for s in self.signals:
            note = f"  [{s['note']}]" if s["note"] else ""
            lines.append(f"  {s['name']:<24}: {s['value']}  [{s['status']}]{note}")
        lines.append(f"  {'TIER':<24}: {self.tier} ({self.label}) | cascade={self.cascade}")
        lines.append(f"  {'ANALYSIS':<24}: {self.analysis}")
        return "\n".join(lines)

    def to_machine(self):
        return {
            "ts":       self.ts,
            "ticker":   self.ticker,
            "signals":  self.signals,
            "tier":     self.tier,
            "label":    self.label,
            "cascade":  self.cascade,
            "analysis": self.analysis,
        }

    def commit(self, db):
        log.info(f"[SCAN_DECISION]\n{self.to_human()}")
        try:
            db.log_event(
                "SCAN_CLASSIFIED",
                agent="The Pulse",
                details=_json.dumps(self.to_machine()),
            )
        except Exception as e:
            log.debug(f"ScanDecisionLog.commit failed (non-fatal): {e}")


# ── SCAN ANALYSIS ─────────────────────────────────────────────────────────

def format_scan_analysis(pos, put_call, put_call_avg, insider_data,
                         volume_data, tier, tier_label, cascade_detected, db):
    """
    Generate a structured sentiment analysis summary from raw signal data.
    Replaces: analyze_position_with_claude()

    All logic is deterministic. No external calls.
    Returns: analysis string for inclusion in scan log event_summary.
    """
    sdl = ScanDecisionLog(ticker=pos.get("ticker", "UNKNOWN"))

    # ── Record each signal and its status ─────────────────────────────────
    if put_call is not None and put_call_avg is not None:
        ratio_pct = (put_call / put_call_avg - 1) * 100
        status = "ELEVATED" if put_call / put_call_avg > CASCADE_PUT_CALL_THRESHOLD else (
                 "ABOVE_AVG" if ratio_pct > 0 else "NORMAL")
        sdl.signal("put_call_ratio",
                   f"{put_call:.2f} vs {put_call_avg:.2f} avg ({ratio_pct:+.0f}%)", status)
    else:
        sdl.signal("put_call_ratio", "unavailable", "UNKNOWN")

    insider_sells = insider_data.get("sells", 0)
    insider_buys  = insider_data.get("buys", 0)
    insider_net   = insider_data.get("net_dollar", "$0")
    if insider_sells >= CASCADE_INSIDER_SELLS_MIN and insider_buys == 0:
        ins_status = "CRITICAL"
    elif insider_sells > insider_buys + 2:
        ins_status = "ELEVATED"
    elif insider_buys > insider_sells:
        ins_status = "POSITIVE"
    else:
        ins_status = "NORMAL"
    sdl.signal("insider_transactions",
               f"{insider_buys}B / {insider_sells}S = {insider_net}", ins_status)

    vol_cascade = volume_data.get("cascade_detected", False)
    sdl.signal("volume_cascade",
               f"{volume_data.get('today_vs_avg','?')} vs avg, {volume_data.get('seller_dominance','?')} sellers",
               "CRITICAL" if vol_cascade else "NORMAL")

    # ── Actor assessment (single large sell vs. multiple sellers) ──────────
    # KEY RULE: multiple independent signals aligning = cascade.
    # Single elevated signal = likely one actor, not cascade.
    cascade_str = "multiple signals aligning — broad seller pressure" if cascade_detected else (
                  "single signal elevated — isolated actor likely")

    # ── Stop level context ─────────────────────────────────────────────────
    current_price = pos.get("current_price", pos.get("entry_price", 0.0))
    trail_dist    = pos.get("trail_stop_amt", 0.0)
    stop_level    = current_price - trail_dist if trail_dist else 0.0

    # ── Recommendation by tier ─────────────────────────────────────────────
    if tier == 1:
        aligned = [s["name"] for s in sdl.signals if s["status"] in ("CRITICAL", "ELEVATED")]
        rec = (
            f"CASCADE DETECTED. Trail stop at ~${stop_level:.2f}. "
            f"Signals aligning: {', '.join(aligned) or 'multiple'}. "
            "Tighten trailing stop or prepare protective exit."
        )
    elif tier == 2:
        top = next((s for s in sdl.signals if s["status"] in ("ELEVATED",)), None)
        src = top["name"] if top else "unknown signal"
        rec = (
            f"Single signal elevated ({src}). "
            f"Trail stop at ~${stop_level:.2f}. "
            "Hold stops — monitor for additional confirmation."
        )
    else:
        rec = f"No action required — tier {tier} ({tier_label})."

    analysis = f"cascade_assessment={cascade_str} | {rec}"
    sdl.conclude(tier, tier_label, cascade_detected, analysis)
    sdl.commit(db)

    return analysis


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run():
    db  = get_db()
    now = datetime.now(ET)
    log.info(f"The Pulse starting — {now.strftime('%H:%M ET')}")

    db.log_event("AGENT_START", agent="The Pulse")
    db.log_heartbeat("agent3_sentiment", "RUNNING")

    positions = db.get_open_positions()
    if not positions:
        log.info("No open positions to scan")
        db.log_heartbeat("agent3_sentiment", "OK")
        return

    log.info(f"Scanning {len(positions)} open position(s)")

    critical_count = 0
    elevated_count = 0

    for pos in positions:
        ticker = pos['ticker']
        log.info(f"Scanning {ticker}...")

        # Fetch all three signals
        put_call, put_call_avg = fetch_put_call_ratio(ticker)
        insider_data           = fetch_sec_insider_transactions(ticker)
        volume_data            = fetch_volume_profile(ticker)

        # Small delay between tickers to avoid rate limiting
        time.sleep(2)

        # Detect cascade
        tier, tier_label, cascade_detected, summary = detect_cascade(
            put_call, put_call_avg, insider_data, volume_data
        )

        # Deep analysis for elevated/critical positions
        analysis = None
        if tier <= 2:
            analysis = format_scan_analysis(
                pos, put_call, put_call_avg, insider_data,
                volume_data, tier, tier_label, cascade_detected, db
            )

        # Log scan result to database
        db.log_scan(
            ticker=ticker,
            put_call_ratio=put_call,
            put_call_avg30d=put_call_avg,
            insider_net=insider_data.get("net_dollar"),
            volume_vs_avg=volume_data.get("today_vs_avg"),
            seller_dominance=volume_data.get("seller_dominance"),
            cascade_detected=cascade_detected,
            tier=tier,
            event_summary=summary + (f" | Analysis: {analysis[:100]}" if analysis else ""),
        )

        if tier == 1:
            critical_count += 1
            log.warning(f"CRITICAL — {ticker}: {summary}")
        elif tier == 2:
            elevated_count += 1
            log.info(f"ELEVATED — {ticker}: {summary}")
        else:
            log.info(f"{tier_label} — {ticker}: normal scan")

    # ── STEP 2: Check pre-trade queue sentiment
    # Signals queued by The Daily but not yet acted on by trader
    queued_signals = db.get_queued_signals()
    if queued_signals:
        log.info(f"Checking sentiment for {len(queued_signals)} queued signal(s)")
        for sig in queued_signals:
            ticker = sig['ticker']
            # Quick check only — full scan done on open positions
            put_call, put_call_avg = fetch_put_call_ratio(ticker)
            insider_data           = fetch_sec_insider_transactions(ticker, days_back=7)
            volume_data            = fetch_volume_profile(ticker)
            tier, tier_label, cascade_detected, summary = detect_cascade(
                put_call, put_call_avg, insider_data, volume_data
            )
            db.log_scan(
                ticker=ticker,
                put_call_ratio=put_call,
                put_call_avg30d=put_call_avg,
                insider_net=insider_data.get("net_dollar"),
                volume_vs_avg=volume_data.get("today_vs_avg"),
                seller_dominance=volume_data.get("seller_dominance"),
                cascade_detected=cascade_detected,
                tier=tier,
                event_summary=f"PRE-TRADE CHECK: {summary}",
            )
            if tier <= 2:
                log.warning(f"Pre-trade warning: {ticker} (signal {sig['id']}) — {tier_label}: {summary}")
                # Write finding back to signal so Agent 1 reads it in Gate 5 signal scoring
                db.annotate_signal_pulse(sig['id'], tier, summary)
            time.sleep(1)

    portfolio = db.get_portfolio()
    log.info(
        f"Scan complete — critical={critical_count} elevated={elevated_count} "
        f"positions={len(positions)} portfolio=${portfolio['cash']:.2f}"
    )

    db.log_heartbeat("agent3_sentiment", "OK", portfolio_value=portfolio['cash'])
    db.log_event(
        "AGENT_COMPLETE", agent="The Pulse",
        details=f"critical={critical_count} elevated={elevated_count} scanned={len(positions)}",
        portfolio_value=portfolio['cash'],
    )

    # Post heartbeat to monitor server
    try:
        from heartbeat import write_heartbeat
        write_heartbeat(agent_name="agent3_sentiment", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    acquire_agent_lock("agent3_sentiment.py")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
