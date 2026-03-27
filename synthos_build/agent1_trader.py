"""
agent1_trader.py — The Trader (Execution Agent)
Synthos · Agent 1

Runs:
  9:30 AM ET  --session=open
  12:30 PM ET --session=midday
  3:30 PM ET  --session=close

Responsibilities:
  - Read validated signals from The Daily (via signals.db)
  - Check urgent flags from The Pulse
  - Apply all trading rules (position sizing, ATR stops, sector caps)
  - Execute paper or live trades via Alpaca API
  - Manage open positions (trailing stop updates, profit-taking)
  - Reconcile positions against Alpaca on session start
  - Auto-sweep monthly tax on last trading day of month

Usage:
  python3 agent1_trader.py --session=open
  python3 agent1_trader.py --session=midday
  python3 agent1_trader.py --session=close
"""

import os
import sys
import time
import logging
import argparse
import requests
# smtplib / email imports removed -- Gmail send path is commented out pending
# command portal transport toggle. Uncomment these and the Gmail block in
# _direct_send_fallback() when ready to activate.
# import smtplib
# from email.mime.text import MIMEText
# from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from calendar import monthrange
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from database import get_db, acquire_agent_lock, release_agent_lock

# ── CONFIG ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ALPACA_API_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET     = os.environ.get('ALPACA_SECRET_KEY', '')
ALPACA_BASE_URL   = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
TRADING_MODE      = os.environ.get('TRADING_MODE', 'PAPER')
ET                = ZoneInfo("America/New_York")
CLAUDE_MODEL      = "claude-sonnet-4-20250514"
MAX_RETRIES       = 3

# ── OPERATING MODE ────────────────────────────────────────────────────────
# SUPERVISED: Claude queues proposals, user approves each one before execution
#             This is the default per framing section 4.1
# AUTONOMOUS: Pre-authorized rules execute without per-trade approval
#             Requires unlock key — see framing section 4.2
OPERATING_MODE   = os.environ.get('OPERATING_MODE', 'SUPERVISED').upper()
AUTONOMOUS_KEY   = os.environ.get('AUTONOMOUS_UNLOCK_KEY', '')
KILL_SWITCH_FILE = os.path.join(os.path.dirname(__file__), '.kill_switch')

# ── NOTIFICATION CONFIG ───────────────────────────────────────────────────
# SendGrid for protective exit email notifications (framing section 5.3)
SENDGRID_API_KEY  = os.environ.get('SENDGRID_API_KEY', '')
ALERT_FROM        = os.environ.get('ALERT_FROM', '')
USER_EMAIL        = os.environ.get('USER_EMAIL', '')       # recipient for trade alerts

# Safety check — refuse to run if config is ambiguous
if TRADING_MODE not in ('PAPER', 'LIVE'):
    print(f"ERROR: Invalid TRADING_MODE '{TRADING_MODE}'. Must be PAPER or LIVE.")
    sys.exit(1)
if TRADING_MODE == 'LIVE' and 'paper' in ALPACA_BASE_URL:
    print("ERROR: TRADING_MODE=LIVE but ALPACA_BASE_URL points to paper endpoint.")
    sys.exit(1)

# Validate autonomous mode — requires unlock key (framing 4.2)
if OPERATING_MODE == 'AUTONOMOUS' and not AUTONOMOUS_KEY:
    print("ERROR: OPERATING_MODE=AUTONOMOUS requires AUTONOMOUS_UNLOCK_KEY in .env")
    print("Contact Synthos support to complete the onboarding process.")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('agent1_trader')


# ── KILL SWITCH ───────────────────────────────────────────────────────────

def kill_switch_active():
    """
    Check if the kill switch has been triggered.
    Kill switch is set by writing a file at KILL_SWITCH_FILE.
    The portal writes this file when the user hits the kill button.
    Framing section 4.3 / C1 / C5.
    """
    return os.path.exists(KILL_SWITCH_FILE)

def clear_kill_switch():
    """Remove kill switch file — called when user re-enables the system."""
    try:
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)
            log.info("Kill switch cleared")
    except Exception as e:
        log.error(f"Could not clear kill switch: {e}")


# ── SUPERVISED MODE QUEUE ─────────────────────────────────────────────────
# DB-backed. pending_approvals table is the single source of truth.
# JSON file is no longer written or read by the trader.

def queue_for_approval(signal, decision_data):
    """
    In supervised mode: instead of executing, queue the proposed trade
    for user review. Portal reads from DB and presents it.
    Framing section 4.1 / C2 / C3.
    """
    from database import get_db
    try:
        get_db().queue_approval(
            signal_id  = signal['id'],
            ticker     = signal['ticker'],
            company    = signal.get('company', ''),
            sector     = signal.get('sector', ''),
            politician = signal.get('politician', ''),
            confidence = signal.get('confidence', ''),
            staleness  = signal.get('staleness', ''),
            headline   = signal.get('headline', ''),
            price      = decision_data.get('price'),
            shares     = decision_data.get('shares'),
            max_trade  = decision_data.get('max_trade'),
            trail_amt  = decision_data.get('trail_amt'),
            trail_pct  = decision_data.get('trail_pct'),
            vol_label  = decision_data.get('vol_label'),
            reasoning  = decision_data.get('reasoning', ''),
            session    = decision_data.get('session', ''),
        )
        log.info(
            f"[SUPERVISED] Trade queued for approval [DB]: "
            f"{signal['ticker']} ${decision_data.get('max_trade', 0):.2f}"
        )
    except Exception as e:
        log.error(f"queue_for_approval DB error: {e}")
        raise

def get_approved_trades():
    """Return trades the user has approved via the portal."""
    from database import get_db
    try:
        return get_db().get_pending_approvals(status_filter=['APPROVED'])
    except Exception as e:
        log.error(f"get_approved_trades DB error: {e}")
        return []

def mark_approval_executed(signal_id):
    """Transition an approved trade to EXECUTED status. Row is preserved for audit."""
    from database import get_db
    try:
        get_db().mark_approval_executed(signal_id)
    except Exception as e:
        log.error(f"mark_approval_executed DB error: {e}")


# ── PROTECTIVE EXIT EMAIL ─────────────────────────────────────────────────

def _enqueue_p0_alert(subject: str, body: str, event_type: str,
                      related_ticker: str = None,
                      related_signal_id: str = None) -> bool:
    """
    POST a P0 alert to the company Pi Scoop queue via /api/enqueue.
    Returns True if enqueue succeeded.
    Caller must implement fallback if this returns False.
    """
    monitor_url   = os.environ.get('MONITOR_URL', '').rstrip('/')
    monitor_token = os.environ.get('MONITOR_TOKEN', '')
    pi_id         = os.environ.get('PI_ID', 'synthos-pi')

    if not monitor_url:
        log.debug("[ENQUEUE] MONITOR_URL not set — enqueue skipped")
        return False

    payload = {
        "event_type":        event_type,
        "priority":          0,
        "subject":           subject,
        "body":              body,
        "source_agent":      "agent1_trader",
        "pi_id":             pi_id,
        "audience":          "customer",
        "related_ticker":    related_ticker,
        "related_signal_id": str(related_signal_id) if related_signal_id else None,
        "payload":           {
            "ticker":     related_ticker,
            "signal_id":  related_signal_id,
            "pi_id":      pi_id,
        },
    }

    try:
        r = requests.post(
            f"{monitor_url}/api/enqueue",
            json=payload,
            headers={
                "X-Token":      monitor_token,
                "Content-Type": "application/json",
            },
            timeout=3,   # short — P0 can't wait long
        )
        if r.status_code == 200:
            log.info(f"[ENQUEUE] P0 {event_type} queued for Scoop (id={r.json().get('id','?')[:8]})")
            return True
        else:
            log.warning(
                f"[ENQUEUE] Company Pi returned {r.status_code} — "
                f"falling back to direct send"
            )
            return False
    except requests.exceptions.Timeout:
        log.warning("[ENQUEUE] Enqueue timed out (3s) — falling back to direct send")
        return False
    except requests.exceptions.ConnectionError:
        log.warning(f"[ENQUEUE] Company Pi unreachable at {monitor_url} — falling back to direct send")
        return False
    except Exception as e:
        log.warning(f"[ENQUEUE] Unexpected error: {e} — falling back to direct send")
        return False


def _direct_send_fallback(subject: str, body: str, reason: str = "enqueue_failed") -> bool:
    """
    P0-ONLY direct email fallback when Scoop enqueue fails.
    Uses SendGrid if configured. Logs the fallback reason.
    Gmail SMTP path placeholder — toggle via command portal when configured.
    """
    log.warning(f"[FALLBACK] Direct send triggered — reason: {reason}")

    # Log to system_log for audit trail
    try:
        from database import get_db
        get_db().log_event(
            "P0_DIRECT_SEND_FALLBACK",
            agent="agent1_trader",
            details=f"reason={reason} subject={subject[:80]}"
        )
    except Exception:
        pass

    # ── SendGrid path (primary) ────────────────────────────────────────────
    if SENDGRID_API_KEY and USER_EMAIL:
        try:
            import sendgrid as _sg
            from sendgrid.helpers.mail import Mail
            msg = Mail(
                from_email=ALERT_FROM or 'alerts@synthos.local',
                to_emails=USER_EMAIL,
                subject=subject,
                plain_text_content=body,
            )
            sg = _sg.SendGridAPIClient(api_key=SENDGRID_API_KEY)
            sg.client.mail.send.post(request_body=msg.get())
            log.info(f"[FALLBACK] SendGrid direct send succeeded → {USER_EMAIL}")
            return True
        except ImportError:
            log.warning("[FALLBACK] sendgrid package not installed")
        except Exception as e:
            log.error(f"[FALLBACK] SendGrid send failed: {e}")

    # ── Gmail SMTP path (secondary — uncomment when configured) ───────────
    # GMAIL_USER = os.environ.get('GMAIL_USER', '')
    # GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
    # if GMAIL_USER and GMAIL_APP_PASSWORD and USER_EMAIL:
    #     try:
    #         import smtplib
    #         from email.mime.text import MIMEText
    #         msg = MIMEText(body)
    #         msg['Subject'] = subject
    #         msg['From']    = GMAIL_USER
    #         msg['To']      = USER_EMAIL
    #         with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
    #             s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    #             s.send_message(msg)
    #         log.info(f"[FALLBACK] Gmail direct send succeeded → {USER_EMAIL}")
    #         return True
    #     except Exception as e:
    #         log.error(f"[FALLBACK] Gmail send failed: {e}")

    log.error("[FALLBACK] All send paths exhausted — P0 alert not delivered")
    return False


def send_protective_exit_email(ticker, reason, reasoning, entry_price,
                                exit_price, shares, pnl_dollar):
    """
    P0 alert — Layer 1 protective exit notification.
    Framing section 5.3 / M1.

    Primary path:  POST to company Pi /api/enqueue → Scoop delivers.
    Fallback path: Direct send (SendGrid, then Gmail if configured).
    Fallback is P0-only and only triggers if enqueue fails.
    """
    pnl_sign  = "+" if pnl_dollar >= 0 else ""
    direction = "profit" if pnl_dollar >= 0 else "loss"

    subject = f"[Synthos] Protective Exit — {ticker} ({pnl_sign}${abs(pnl_dollar):.2f})"
    body = f"""Synthos executed a Layer 1 protective exit on your behalf.

TRADE SUMMARY
─────────────────────────────────────
Ticker:      {ticker}
Exit reason: {reason}
Entry price: ${entry_price:.2f}
Exit price:  ${exit_price:.2f}
Shares:      {shares:.4f}
P&L:         {pnl_sign}${abs(pnl_dollar):.2f} ({direction})

REASONING
─────────────────────────────────────
{reasoning}

─────────────────────────────────────
This exit was executed automatically under your pre-authorized
protective exit ruleset. No action is required.

Synthos
"""
    # Step 1 — try Scoop enqueue (preferred path)
    enqueued = _enqueue_p0_alert(
        subject          = subject,
        body             = body,
        event_type       = "PROTECTIVE_EXIT_TRIGGERED",
        related_ticker   = ticker,
    )

    if enqueued:
        return True

    # Step 2 — P0 fallback: direct send (enqueue failed)
    return _direct_send_fallback(
        subject = subject,
        body    = body,
        reason  = "enqueue_failed_protective_exit",
    )

# ── PORTFOLIO TIERS ───────────────────────────────────────────────────────
PORTFOLIO_TIERS = [
    {"threshold": 0,      "max_deployed": 0.30, "max_positions": 3,  "label": "Seed"   },
    {"threshold": 1000,   "max_deployed": 0.35, "max_positions": 5,  "label": "Early"  },
    {"threshold": 5000,   "max_deployed": 0.40, "max_positions": 8,  "label": "Growth" },
    {"threshold": 20000,  "max_deployed": 0.45, "max_positions": 10, "label": "Scaled" },
    {"threshold": 50000,  "max_deployed": 0.50, "max_positions": 12, "label": "Mature" },
]

VOLATILITY_BUCKETS = {
    "low":  {"multiplier": 1.5,  "label": "Low Vol",
             "sectors": ["Utilities","Industrials","Consumer Staples","Real Estate"]},
    "mid":  {"multiplier": 1.1,  "label": "Mid Vol",
             "sectors": ["Defense","Financials","Healthcare","Materials","Energy"]},
    "high": {"multiplier": 0.85, "label": "High Vol",
             "sectors": ["Technology","Consumer Discretionary","Communication"]},
}

STALENESS_DISCOUNTS = {"Fresh": 0.0, "Aging": 0.15, "Stale": 0.30, "Expired": 0.50}

GAIN_TAX_PCT       = 0.10
# ── CAPITAL DEPLOYMENT LIMITS ─────────────────────────────────────────────
# VALIDATION MODE: 20% tradeable / 80% reserve.
# Intentionally conservative — allows trade execution testing without
# deploying serious capital. Flip to 80/20 once execution is validated.
#
# TODO (post-validation): flip to TRADEABLE_PCT=0.80 / IDLE_RESERVE_PCT=0.20
# TODO (post-validation): implement idle reserve → BIL sweep
#   - Buy BIL (T-bill ETF) with IDLE_RESERVE_PCT of portfolio cash
#   - Sync on every session after cash reconcile from Alpaca
#   - Log ledger entry type 'BIL' on buy/sell
#   - Exclude BIL from position count and P&L tracking
IDLE_RESERVE_PCT   = 0.80
TRADEABLE_PCT      = 0.20
MAX_POSITION_PCT   = float(os.environ.get('MAX_POSITION_PCT', '0.10'))
MAX_SECTOR_CAP_PCT = float(os.environ.get('MAX_SECTOR_PCT', '25'))
MONTHLY_INFRA_COST = 20.0

# Portal-configurable filters
MIN_CONFIDENCE     = os.environ.get('MIN_CONFIDENCE', 'MEDIUM').upper()   # HIGH / MEDIUM / LOW
MAX_STALENESS      = os.environ.get('MAX_STALENESS', 'Aging')             # Fresh / Aging / Stale / Expired
CLOSE_SESSION_MODE = os.environ.get('CLOSE_SESSION_MODE', 'conservative') # conservative / normal
SPOUSAL_WEIGHT     = os.environ.get('SPOUSAL_WEIGHT', 'reduced')          # reduced / skip / equal

CONFIDENCE_ORDER   = ['HIGH', 'MEDIUM', 'LOW', 'NOISE']

PROFIT_RULES = [
    {"gain_pct": 0.08, "sell_pct": 0.33, "label": "8% — sell ⅓"},
    {"gain_pct": 0.15, "sell_pct": 0.50, "label": "15% — sell ½"},
    {"gain_pct": 0.25, "sell_pct": 0.75, "label": "25% — sell ¾"},
]

# ── HELPERS ───────────────────────────────────────────────────────────────

def get_portfolio_tier(total_value):
    tier = PORTFOLIO_TIERS[0]
    for t in PORTFOLIO_TIERS:
        if total_value >= t["threshold"]:
            tier = t
    return tier


def get_volatility_bucket(sector):
    sector = (sector or "").strip()
    for key, bucket in VOLATILITY_BUCKETS.items():
        if sector in bucket["sectors"]:
            return key, bucket
    return "mid", VOLATILITY_BUCKETS["mid"]


def calculate_trail_stop(atr, price, sector):
    _, bucket = get_volatility_bucket(sector)
    amt = round(atr * bucket["multiplier"], 2)
    pct = round((amt / price) * 100, 2)
    return amt, pct, bucket["label"]


def get_position_size_pct(tier_num, confidence, sector_concentration_pct):
    """Signal quality sizing — stronger signals get more capital."""
    pct = MAX_POSITION_PCT
    if tier_num == 1 and confidence == "HIGH":   pct = 0.12
    elif tier_num == 2 and confidence == "HIGH": pct = 0.10
    elif confidence == "MEDIUM":                 pct = 0.07
    elif confidence == "LOW":                    pct = 0.05
    # Sector concentration penalty
    if sector_concentration_pct > MAX_SECTOR_CAP_PCT * 0.8:
        pct = min(pct, 0.05)
    return pct


def is_last_trading_day_of_month():
    """Check if today is the last weekday of the month (approximate)."""
    today   = datetime.now(ET).date()
    _, days = monthrange(today.year, today.month)
    last    = date(today.year, today.month, days)
    # Walk back from last day to find last weekday
    while last.weekday() > 4:
        last -= timedelta(days=1)
    return today == last


# ── ALPACA CLIENT ─────────────────────────────────────────────────────────

class AlpacaClient:
    def __init__(self):
        self.base_url = ALPACA_BASE_URL.rstrip('/')
        self.headers  = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
            "Content-Type":        "application/json",
        }

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}{endpoint}"
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                r = getattr(requests, method)(
                    url, headers=self.headers, timeout=15, **kwargs
                )
                r.raise_for_status()
                return r.json() if r.text else {}
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
        log.error(f"Alpaca {method.upper()} {endpoint} failed: {last_error}")
        return None

    def get_account(self):
        return self._request("get", "/v2/account")

    def get_positions(self):
        return self._request("get", "/v2/positions") or []

    def get_position(self, ticker):
        return self._request("get", f"/v2/positions/{ticker}")

    def get_latest_price(self, ticker):
        r = self._request("get", f"/v2/stocks/{ticker}/quotes/latest")
        if r and "quote" in r:
            return float(r["quote"].get("ap", 0) or r["quote"].get("bp", 0))
        return None

    def get_atr(self, ticker, period=14):
        """Fetch 14-day ATR from Alpaca bars data."""
        end   = datetime.now(ET).strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (datetime.now(ET) - timedelta(days=period+5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        r = self._request("get", f"/v2/stocks/{ticker}/bars",
                          params={"timeframe":"1Day","start":start,"end":end,"limit":period+5})
        if not r or "bars" not in r:
            return None
        bars = r["bars"]
        if len(bars) < 2:
            return None
        # True Range = max(High-Low, abs(High-PrevClose), abs(Low-PrevClose))
        trs = []
        for i in range(1, len(bars)):
            h  = bars[i]["h"]
            l  = bars[i]["l"]
            pc = bars[i-1]["c"]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        return round(sum(trs[-period:]) / min(len(trs), period), 2)

    def submit_order(self, ticker, qty, side, order_type="market",
                     trail_price=None, trail_percent=None):
        """
        Submit an order to Alpaca.
        For entries: market order with fractional qty.
        For trail stops: trailing_stop order type.
        """
        if TRADING_MODE == "PAPER":
            log.info(f"[PAPER] Would {side} {qty} shares of {ticker}")

        payload = {
            "symbol":        ticker,
            "qty":           str(qty),
            "side":          side,
            "type":          order_type,
            "time_in_force": "day",
        }
        if order_type == "trailing_stop":
            if trail_price:
                payload["trail_price"] = str(trail_price)
            elif trail_percent:
                payload["trail_percent"] = str(trail_percent)

        result = self._request("post", "/v2/orders", json=payload)
        if result:
            log.info(f"Order submitted: {side} {qty} {ticker} — id={result.get('id','?')}")
        return result

    def cancel_order(self, order_id):
        return self._request("delete", f"/v2/orders/{order_id}")

    def close_position(self, ticker):
        """Liquidate entire position."""
        return self._request("delete", f"/v2/positions/{ticker}")


# ── CLAUDE HELPERS ────────────────────────────────────────────────────────

def call_claude(prompt, max_tokens=1000):
    headers = {
        "Content-Type":    "application/json",
        "x-api-key":       ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model":     CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages":  [{"role": "user", "content": prompt}],
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
            return "".join(b.get("text","") for b in data.get("content",[]))
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 2)
    log.error(f"Claude API failed: {last_error}")
    return None


def build_learning_context(db):
    """Build recent outcome context to include in Claude prompts."""
    outcomes = db.get_recent_outcomes(limit=8)
    if not outcomes:
        return ""
    lines = []
    for o in outcomes:
        lines.append(
            f"  {o['ticker']} {o.get('verdict','?')} "
            f"{o.get('pnl_pct',0):+.1f}% "
            f"({o.get('staleness','?')} T{o.get('signal_tier','?')} {o.get('vol_bucket','?')}) "
            f"— {o.get('lesson','')[:60]}"
        )
    return "RECENT OUTCOMES (learning):\n" + "\n".join(lines)


def analyze_signal_with_claude(signal, portfolio, positions, session, db, alpaca):
    """
    Ask Claude to analyze a queued signal and decide: MIRROR / WATCH / SKIP.
    Returns (decision, reasoning)
    """
    total_value    = portfolio['cash'] + sum(
        p['entry_price'] * p['shares'] for p in positions
    )
    tradeable      = portfolio['cash'] * TRADEABLE_PCT
    tier_rules     = get_portfolio_tier(total_value)
    deployed_value = sum(p['entry_price'] * p['shares'] for p in positions)
    deployed_pct   = deployed_value / tradeable if tradeable > 0 else 0

    # Sector concentration
    sig_sector     = signal.get('sector', '')
    sector_value   = sum(
        p['entry_price'] * p['shares']
        for p in positions if p.get('sector') == sig_sector
    )
    sector_pct     = (sector_value / total_value * 100) if total_value > 0 else 0

    # Position sizing
    pos_pct        = get_position_size_pct(
        signal.get('source_tier', 2),
        signal.get('confidence', 'MEDIUM'),
        sector_pct,
    )
    staleness_disc = STALENESS_DISCOUNTS.get(signal.get('staleness','Fresh'), 0)
    adj_pos_pct    = round(pos_pct * (1 - staleness_disc), 4)
    max_trade      = round(tradeable * adj_pos_pct, 2)

    # Get current price and ATR from Alpaca
    ticker = signal['ticker']
    price  = alpaca.get_latest_price(ticker)
    atr    = alpaca.get_atr(ticker)

    if not price:
        log.warning(f"Could not get price for {ticker} — skipping")
        return "SKIP", "Price data unavailable"

    if not atr:
        atr = price * 0.02   # fallback: 2% of price as rough ATR estimate

    trail_amt, trail_pct, vol_label = calculate_trail_stop(atr, price, sig_sector)
    shares = round(max_trade / price, 4)

    # Urgent flags from The Pulse
    urgent_flags = db.get_urgent_flags()
    urgent_tickers = {f['ticker'] for f in urgent_flags}
    pulse_warning = f"⚠ URGENT PULSE FLAG on {ticker}" if ticker in urgent_tickers else ""

    learning = build_learning_context(db)
    is_cold  = len(positions) == 0 and portfolio.get('realized_gains', 0) == 0

    session_guidance = {
        "open":   "Morning session — new disclosures most actionable. Fresh signals preferred.",
        "midday": "Midday check — verify signal still holds, no major reversals.",
        "close":  "⚠ PRE-CLOSE: Be conservative. Avoid overnight positions unless signal is exceptional.",
    }.get(session, "")

    prompt = f"""You are the Trader — Agent 1 for Synthos, a conservative congressional trade following system.

MISSION: Prove congressional STOCK Act disclosures generate consistent positive returns.
Capital preservation first. More wins than losses is the goal, not big wins.

PORTFOLIO ({tier_rules['label']} tier):
  Total: ${total_value:.2f} | Tradeable: ${tradeable:.2f} | Deployed: {deployed_pct*100:.1f}% (max {tier_rules['max_deployed']*100:.0f}%)
  Open positions: {len(positions)}/{tier_rules['max_positions']} | Month gains: ${portfolio.get('realized_gains',0):.2f}
  {'🚀 COLD START MODE: First run — only Tier 1 HIGH confidence, max 1 position.' if is_cold else ''}

SESSION: {session.upper()} — {session_guidance}

SIGNAL:
  Ticker: {ticker} | Source: {signal.get('source')} (Tier {signal.get('source_tier')}) | Confidence: {signal.get('confidence')}
  Politician: {signal.get('politician','?')} | Staleness: {signal.get('staleness','?')} (discount: {staleness_disc*100:.0f}%)
  Headline: "{signal.get('headline','')}"

PROPOSED TRADE:
  Position size: ${max_trade:.2f} ({adj_pos_pct*100:.1f}% of tradeable after staleness discount)
  Shares: {shares:.4f} @ ${price:.2f}
  Trailing stop: ${trail_amt:.2f} ({trail_pct:.1f}%) — {vol_label} (ATR x {VOLATILITY_BUCKETS.get("mid",{}).get("multiplier",1.1)})
  Sector exposure: {sector_pct:.1f}% in {sig_sector} {'⚠ approaching cap' if sector_pct > MAX_SECTOR_CAP_PCT * 0.8 else ''}

{pulse_warning}

{learning}

PROFIT RULES (for awareness): sell ⅓ at 8% gain, ½ at 15%, ¾ at 25%.

Analyze: signal quality, timing, session fit, sector, pulse warnings, cold start rules if applicable.

End with exactly:
DECISION: MIRROR
or
DECISION: WATCH
or
DECISION: SKIP"""

    response = call_claude(prompt, max_tokens=1000)
    if not response:
        return "SKIP", "Claude unavailable"

    decision = "WATCH"
    for line in response.strip().split('\n'):
        if line.startswith("DECISION:"):
            d = line.replace("DECISION:", "").strip().upper()
            if d in ("MIRROR", "WATCH", "SKIP"):
                decision = d
            break

    return decision, response, price, atr, shares, max_trade, trail_amt, trail_pct, vol_label


# ── POSITION MANAGEMENT ───────────────────────────────────────────────────

def check_profit_taking(pos, current_price):
    """Check if position has hit a profit-taking threshold."""
    entry   = pos['entry_price']
    gain_pct = (current_price - entry) / entry
    triggered = [r for r in PROFIT_RULES if gain_pct >= r["gain_pct"]]
    return triggered[-1] if triggered else None


def reconcile_with_alpaca(db, alpaca):
    """
    Compare DB open positions against Alpaca.
    Flag orphans (in Alpaca but not DB) and ghosts (in DB but not Alpaca).
    Also syncs portfolio.cash from Alpaca's actual account balance — DB cash
    is not authoritative; Alpaca is the source of truth for cash.
    """
    try:
        alpaca_positions = alpaca.get_positions()
        if alpaca_positions is None:
            log.warning("Could not fetch Alpaca positions for reconciliation")
            return

        # ── CASH SYNC ─────────────────────────────────────────────────────
        # Pull actual cash from Alpaca and update DB — overrides cold-start
        # seed value (STARTING_CAPITAL). Without this, all position sizing
        # is calculated against a phantom balance.
        # TODO: once BIL sweep is implemented, subtract BIL position value
        #       from cash before storing so tradeable math stays correct.
        try:
            account = alpaca.get_account()
            if account:
                alpaca_cash = float(account.get('cash', 0))
                if alpaca_cash > 0:
                    db.update_portfolio(cash=alpaca_cash)
                    log.info(f"[CASH SYNC] Portfolio cash updated from Alpaca: ${alpaca_cash:.2f}")
                else:
                    log.warning("[CASH SYNC] Alpaca returned $0 cash — skipping update")
        except Exception as e:
            log.warning(f"[CASH SYNC] Could not sync cash from Alpaca (non-fatal): {e}")

        alpaca_tickers = {p['symbol'] for p in alpaca_positions}
        db_tickers     = db.get_open_tickers()

        orphans = alpaca_tickers - db_tickers
        ghosts  = db_tickers - alpaca_tickers

        for ticker in orphans:
            log.critical(f"ORPHAN POSITION: {ticker} in Alpaca but not in DB — human review required")
            db.log_event("ORPHAN_POSITION", agent="The Trader",
                         details=f"Ticker {ticker} in Alpaca but not in DB")

        for ticker in ghosts:
            log.critical(f"GHOST POSITION: {ticker} in DB but not in Alpaca — human review required")
            # Find position id and flag it
            positions = db.get_open_positions()
            for pos in positions:
                if pos['ticker'] == ticker:
                    db.flag_orphan(pos['id'])

        if orphans or ghosts:
            log.critical(f"Reconciliation found issues — {len(orphans)} orphans, {len(ghosts)} ghosts. HALTING new trades this session.")
            return False

        # Update prices for all open positions
        for ap in alpaca_positions:
            ticker = ap['symbol']
            current_price = float(ap.get('current_price', 0))
            if current_price:
                positions = db.get_open_positions()
                for pos in positions:
                    if pos['ticker'] == ticker:
                        db.update_position_price(pos['id'], current_price)

        log.info(f"Reconciliation clean — {len(alpaca_positions)} Alpaca positions match DB")
        return True

    except Exception as e:
        log.error(f"Reconciliation error: {e}")
        return True  # Don't halt on reconciliation errors in paper mode


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────

def run(session="open"):
    db      = get_db()
    alpaca  = AlpacaClient()
    now     = datetime.now(ET)

    log.info(f"The Trader starting — session={session} mode={TRADING_MODE} operating={OPERATING_MODE} time={now.strftime('%H:%M ET')}")
    db.log_event("AGENT_START", agent="The Trader", details=f"session={session} mode={TRADING_MODE} operating={OPERATING_MODE}")
    db.log_heartbeat("agent1_trader", "RUNNING")

    # ── KILL SWITCH CHECK (framing 4.3 / C1 / C5)
    if kill_switch_active():
        log.warning("KILL SWITCH ACTIVE — halting all agent activity")
        db.log_event("KILL_SWITCH_HALT", agent="The Trader",
                     details="Kill switch file present — session aborted")
        try:
            from heartbeat import write_heartbeat
            write_heartbeat(agent_name="agent1_trader", status="KILL_SWITCH_ACTIVE")
        except Exception:
            pass
        sys.exit(0)

    # ── EXPIRE STALE APPROVALS
    # Clean up any PENDING_APPROVAL entries older than 48h before this session.
    try:
        db.expire_stale_approvals(max_age_hours=48)
    except Exception as e:
        log.warning(f"expire_stale_approvals failed (non-fatal): {e}")

    # ── STEP 1: Account check
    account = alpaca.get_account()
    if not account:
        log.error("Cannot connect to Alpaca — aborting session")
        db.log_event("ALPACA_UNREACHABLE", agent="The Trader")
        sys.exit(1)
    log.info(f"Alpaca connected — cash: ${float(account.get('cash',0)):.2f} ({TRADING_MODE})")

    # ── STEP 2: Reconcile positions
    reconcile_ok = reconcile_with_alpaca(db, alpaca)

    # ── STEP 3: Check urgent flags from The Pulse
    urgent_flags = db.get_urgent_flags()
    if urgent_flags:
        for flag in urgent_flags:
            log.warning(f"URGENT FLAG: {flag['ticker']} cascade detected at {flag['detected_at']}")

    # ── STEP 3b: Layer 1 protective exits for urgent-flagged open positions
    # Framing section 5.3 — executes without per-trade approval, user notified by email
    if urgent_flags and reconcile_ok:
        flagged_tickers = {f['ticker'] for f in urgent_flags}
        positions_now   = db.get_open_positions()
        for pos in positions_now:
            if pos['ticker'] in flagged_tickers:
                current_price = pos.get('current_price') or pos['entry_price']
                flag_info     = next((f for f in urgent_flags if f['ticker'] == pos['ticker']), {})
                reason        = f"CASCADE DETECTED — Tier {flag_info.get('tier', 1)} urgent flag"
                reasoning     = (
                    f"The Pulse detected a cascade signal on {pos['ticker']}. "
                    f"Put/call ratio, insider flow, or volume anomalies triggered "
                    f"a Tier {flag_info.get('tier', 1)} alert at {flag_info.get('detected_at', 'unknown')}. "
                    f"Layer 1 protective exit executed per pre-authorized ruleset."
                )
                log.warning(f"LAYER 1 EXIT: Closing {pos['ticker']} — {reason}")
                order = alpaca.close_position(pos['ticker'])
                if order is not None:
                    pnl = db.close_position(pos['id'], current_price, exit_reason="PROTECTIVE_EXIT")
                    db.acknowledge_urgent_flag(flag_info['id'])
                    db.log_event(
                        "PROTECTIVE_EXIT", agent="The Trader",
                        details=f"{pos['ticker']} — {reason}",
                        portfolio_value=current_price * pos['shares'],
                    )
                    # Send immediate email notification (framing 5.3 / M1)
                    send_protective_exit_email(
                        ticker=pos['ticker'],
                        reason=reason,
                        reasoning=reasoning,
                        entry_price=pos['entry_price'],
                        exit_price=current_price,
                        shares=pos['shares'],
                        pnl_dollar=pnl,
                    )
                    log.info(f"Layer 1 exit complete: {pos['ticker']} P&L=${pnl:+.2f}")

    # ── STEP 4: Get current state
    portfolio  = db.get_portfolio()
    positions  = db.get_open_positions()
    total_value = portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions)
    tradeable  = portfolio['cash'] * TRADEABLE_PCT
    tier_rules = get_portfolio_tier(total_value)
    deployed   = sum(p['entry_price'] * p['shares'] for p in positions)
    deployed_pct = deployed / tradeable if tradeable > 0 else 0

    log.info(
        f"Portfolio: ${total_value:.2f} ({tier_rules['label']}) | "
        f"Cash: ${portfolio['cash']:.2f} | Deployed: {deployed_pct*100:.1f}% | "
        f"Positions: {len(positions)}/{tier_rules['max_positions']}"
    )

    # ── STEP 5: In supervised mode — execute any trades the user approved via portal
    if OPERATING_MODE == 'SUPERVISED':
        approved = get_approved_trades()
        if approved:
            log.info(f"[SUPERVISED] Executing {len(approved)} user-approved trade(s)")
        for approval in approved:
            try:
                ticker  = approval['ticker']
                shares  = float(approval['shares'])
                price   = float(approval['price'])
                trail_amt = float(approval['trail_amt'])
                trail_pct = float(approval['trail_pct'])
                vol_label = approval['vol_label']
                sig_id    = approval['id']

                order = alpaca.submit_order(ticker=ticker, qty=shares, side="buy")
                if order:
                    db.open_position(
                        ticker=ticker,
                        company=approval.get('company'),
                        sector=approval.get('sector'),
                        entry_price=price,
                        shares=shares,
                        trail_stop_amt=trail_amt,
                        trail_stop_pct=trail_pct,
                        vol_bucket=vol_label,
                        signal_id=sig_id,
                    )
                    alpaca.submit_order(
                        ticker=ticker, qty=shares, side="sell",
                        order_type="trailing_stop", trail_price=trail_amt,
                    )
                    db.acknowledge_signal(sig_id)
                    mark_approval_executed(sig_id)
                    log.info(f"[SUPERVISED] Approved trade executed: BUY {shares:.4f} {ticker} @ ${price:.2f}")
                else:
                    log.error(f"[SUPERVISED] Order failed for approved trade: {ticker}")
            except Exception as e:
                log.error(f"[SUPERVISED] Error executing approved trade: {e}")

    # ── STEP 6: Check profit-taking on open positions
    for pos in positions:
        current_price = pos.get('current_price') or pos['entry_price']
        rule = check_profit_taking(pos, current_price)
        if rule:
            sell_shares = round(pos['shares'] * rule['sell_pct'], 4)
            log.info(f"PROFIT TAKE: {pos['ticker']} hit {rule['label']} — selling {sell_shares:.4f} shares")
            if reconcile_ok:
                result = alpaca.submit_order(
                    ticker=pos['ticker'], qty=sell_shares, side="sell"
                )
                if result:
                    proceeds = sell_shares * current_price
                    remaining = pos['shares'] - sell_shares
                    db.close_position(pos['id'], current_price, exit_reason="PROFIT_TAKE")
                    if remaining > 0.0001:
                        # Reopen with remaining shares
                        _, _, vol_label = calculate_trail_stop(
                            pos['trail_stop_amt'] / 1.1, current_price, pos.get('sector','')
                        )
                        db.open_position(
                            ticker=pos['ticker'], company=pos.get('company'),
                            sector=pos.get('sector'), entry_price=current_price,
                            shares=remaining,
                            trail_stop_amt=pos['trail_stop_amt'],
                            trail_stop_pct=pos['trail_stop_pct'],
                            vol_bucket=pos.get('vol_bucket'), signal_id=pos.get('signal_id'),
                        )
                    pnl = round((current_price - pos['entry_price']) * sell_shares, 2)
                    pnl_sign = "+" if pnl >= 0 else ""
                    log.info(f"Partial exit complete: sold {sell_shares:.4f} @ ${current_price:.2f} P&L={pnl_sign}${pnl:.2f}")

    # ── STEP 7: Process queued signals
    if not reconcile_ok:
        log.warning("Reconciliation issues detected — skipping new entries this session")
    else:
        can_deploy = (
            deployed_pct < tier_rules['max_deployed'] and
            len(positions) < tier_rules['max_positions']
        )

        if not can_deploy:
            log.info(f"Max deployment/positions reached — no new entries this session")
        else:
            signals = db.get_queued_signals()
            log.info(f"Processing {len(signals)} queued signal(s)")

            for signal in signals:
                # Re-check deployment limits per signal
                positions  = db.get_open_positions()
                deployed   = sum(p['entry_price'] * p['shares'] for p in positions)
                deployed_pct = deployed / tradeable if tradeable > 0 else 0

                if (deployed_pct >= tier_rules['max_deployed'] or
                        len(positions) >= tier_rules['max_positions']):
                    log.info("Deployment limit reached mid-session — stopping")
                    break

                # ── PORTAL SETTINGS FILTERS ───────────────────────────────
                sig_confidence = signal.get('confidence', 'LOW').upper()
                sig_staleness  = signal.get('staleness', 'Fresh')
                sig_spousal    = bool(signal.get('is_spousal', 0))

                # Confidence filter
                min_idx = CONFIDENCE_ORDER.index(MIN_CONFIDENCE) if MIN_CONFIDENCE in CONFIDENCE_ORDER else 1
                sig_idx = CONFIDENCE_ORDER.index(sig_confidence) if sig_confidence in CONFIDENCE_ORDER else 2
                if sig_idx > min_idx:
                    log.info(f"Signal {signal['ticker']} skipped — confidence {sig_confidence} below threshold {MIN_CONFIDENCE}")
                    continue

                # Staleness filter
                staleness_order = ['Fresh', 'Aging', 'Stale', 'Expired']
                max_stale_idx = staleness_order.index(MAX_STALENESS) if MAX_STALENESS in staleness_order else 1
                sig_stale_idx = staleness_order.index(sig_staleness) if sig_staleness in staleness_order else 0
                if sig_stale_idx > max_stale_idx:
                    log.info(f"Signal {signal['ticker']} skipped — staleness {sig_staleness} beyond cutoff {MAX_STALENESS}")
                    continue

                # Spousal filter
                if sig_spousal and SPOUSAL_WEIGHT == 'skip':
                    log.info(f"Signal {signal['ticker']} skipped — spousal trade (SPOUSAL_WEIGHT=skip)")
                    continue

                # Close session conservatism
                if session == 'close' and CLOSE_SESSION_MODE == 'conservative':
                    if sig_confidence != 'HIGH':
                        log.info(f"Signal {signal['ticker']} skipped — close session conservative mode requires HIGH confidence")
                        continue

                log.info(f"Analyzing signal: {signal['ticker']} T{signal['source_tier']} {signal['confidence']}")

                result = analyze_signal_with_claude(
                    signal, portfolio, positions, session, db, alpaca
                )

                if isinstance(result, tuple) and len(result) == 9:
                    decision, reasoning, price, atr, shares, max_trade, trail_amt, trail_pct, vol_label = result
                else:
                    decision, reasoning = result[0], result[1]
                    log.warning(f"Unexpected result format for {signal['ticker']} — skipping")
                    continue

                log.info(f"Decision: {decision} — {signal['ticker']}")

                if decision == "MIRROR":
                    decision_data = {
                        "price":     price,
                        "shares":    shares,
                        "max_trade": max_trade,
                        "trail_amt": trail_amt,
                        "trail_pct": trail_pct,
                        "vol_label": vol_label,
                        "reasoning": reasoning,
                        "session":   session,
                    }

                    if OPERATING_MODE == 'SUPERVISED':
                        # ── SUPERVISED MODE (framing 4.1 / C2 / C3)
                        # Queue for user approval — do not execute yet
                        queue_for_approval(signal, decision_data)
                        log.info(f"[SUPERVISED] {signal['ticker']} queued — awaiting portal approval")

                    else:
                        # ── AUTONOMOUS MODE (framing 4.2)
                        # Execute immediately per pre-authorized rules
                        order = alpaca.submit_order(
                            ticker=signal['ticker'], qty=shares, side="buy"
                        )
                        if order:
                            _, _, vol_label = calculate_trail_stop(
                                atr, price, signal.get('sector','')
                            )
                            pos_id = db.open_position(
                                ticker=signal['ticker'],
                                company=signal.get('company'),
                                sector=signal.get('sector'),
                                entry_price=price,
                                shares=shares,
                                trail_stop_amt=trail_amt,
                                trail_stop_pct=trail_pct,
                                vol_bucket=vol_label,
                                signal_id=signal['id'],
                            )
                            alpaca.submit_order(
                                ticker=signal['ticker'],
                                qty=shares,
                                side="sell",
                                order_type="trailing_stop",
                                trail_price=trail_amt,
                            )
                            db.acknowledge_signal(signal['id'])
                            log.info(
                                f"TRADE EXECUTED: BUY {shares:.4f} {signal['ticker']} "
                                f"@ ${price:.2f} — stop ${trail_amt:.2f} ({vol_label})"
                            )
                        else:
                            log.error(f"Order failed for {signal['ticker']}")

                elif decision == "SKIP":
                    db.discard_signal(signal['id'], reason="Trader: SKIP")
                    log.info(f"Signal {signal['id']} discarded by trader")

                # WATCH: signal stays in queue, re-evaluated next session
                time.sleep(2)  # Rate limit buffer between Claude calls

    # ── STEP 7: Monthly tax sweep
    if is_last_trading_day_of_month() and session == "close":
        portfolio = db.get_portfolio()
        positions = db.get_open_positions()
        unrealized = sum(p.get('pnl', 0) for p in positions)
        total_gains = portfolio['realized_gains'] + unrealized
        if total_gains > 0:
            tax = round(total_gains * GAIN_TAX_PCT, 2)
            log.info(f"Month-end tax sweep: ${tax:.2f} (10% of ${total_gains:.2f} gains)")
            db.sweep_monthly_tax(tax)

    # ── DONE
    portfolio = db.get_portfolio()
    positions = db.get_open_positions()
    total_value = portfolio['cash'] + sum(p['entry_price'] * p['shares'] for p in positions)

    log.info(
        f"Session complete — portfolio=${total_value:.2f} "
        f"positions={len(positions)} cash=${portfolio['cash']:.2f}"
    )
    db.log_heartbeat("agent1_trader", "OK", portfolio_value=total_value)
    db.log_event(
        "AGENT_COMPLETE", agent="The Trader",
        details=f"session={session} positions={len(positions)}",
        portfolio_value=total_value,
    )

    # Send heartbeat to monitor server
    try:
        from heartbeat import write_heartbeat
        write_heartbeat(agent_name="agent1_trader", status="OK")
    except Exception as e:
        log.warning(f"Heartbeat post failed: {e}")

    # On close session — send daily report to monitor server
    if session == "close":
        try:
            monitor_url   = os.environ.get('MONITOR_URL', '')
            monitor_token = os.environ.get('MONITOR_TOKEN', '')
            pi_id         = os.environ.get('PI_ID', 'synthos-pi')
            if monitor_url:
                outcomes_today = db.get_recent_outcomes(limit=20)
                today_str = datetime.now(ET).strftime('%Y-%m-%d')
                today_outcomes = [o for o in outcomes_today
                                  if o.get('created_at','').startswith(today_str)]
                wins   = sum(1 for o in today_outcomes if o.get('verdict') == 'WIN')
                losses = sum(1 for o in today_outcomes if o.get('verdict') == 'LOSS')
                realized = round(sum(o.get('pnl_dollar', 0) for o in today_outcomes), 2)
                report = {
                    "pi_id":           pi_id,
                    "date":            today_str,
                    "portfolio_value": round(total_value, 2),
                    "realized_pnl":    realized,
                    "open_positions":  len(positions),
                    "trades_today":    len(today_outcomes),
                    "wins":            wins,
                    "losses":          losses,
                    "summary":         f"{len(today_outcomes)} trades today — {wins}W/{losses}L — portfolio ${total_value:.2f}",
                }
                requests.post(
                    f"{monitor_url.rstrip('/')}/report",
                    json=report,
                    headers={"X-Token": monitor_token},
                    timeout=10,
                )
                log.info(f"Daily report posted to monitor: {report['summary']}")
        except Exception as e:
            log.warning(f"Daily report POST failed: {e}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Synthos — The Trader (Execution Agent)')
    parser.add_argument('--session', choices=['open','midday','close'], default='open',
                        help='Trading session: open=9:30am, midday=12:30pm, close=3:30pm')
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — check .env file")
        sys.exit(1)
    if not ALPACA_API_KEY:
        log.error("ALPACA_API_KEY not set — check .env file")
        sys.exit(1)

    acquire_agent_lock("agent1_trader.py")
    try:
        run(session=args.session)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_agent_lock()
