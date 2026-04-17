"""
interrogation_listener.py — Signal Interrogation ACK Responder
Synthos · Retail Node

Listens on UDP port 5556 for HAS_DATA_FOR_INTERROGATION broadcasts from Scout
(agent2_research.py). Runs cross-validation checks and sends INTERROGATION_ACK
back to Scout on port 5557 if the signal passes.

Protocol (defined by Scout):
  Scout broadcasts on port 5556:
    { "event": "HAS_DATA_FOR_INTERROGATION",
      "signal_id": "<id>", "ticker": "<TICKER>",
      "price_summary": "<json_string>" }

  This listener replies to sender on port 5557:
    { "event": "INTERROGATION_ACK", "signal_id": "<id>",
      "ticker": "<TICKER>", "validator": "<hostname>" }

  ACK sent  → Scout marks signal VALIDATED  → eligible for MIRROR under Option B
  No ACK    → Scout marks signal UNVALIDATED → forced to WATCH under Option B

Validation checks (all must pass for ACK to be sent):
  1. Ticker format — 1–6 alphanumeric characters, no spaces
  2. Not a duplicate — no QUEUED/WATCHING signal for same ticker in last 6h
  3. Not rate-blocked — no more than 3 ACKs for same ticker in last 1h

Phase 1 (single Pi): runs on the same device as Scout — provides automated
sanity checking that costs nothing. From Scout's perspective it is a peer.

Phase 2 (multiple Pis): each retail Pi runs this listener. Cross-Pi
corroboration strengthens automatically as devices are added — no protocol
changes required on either side.

Startup:
  boot_sequence.py starts this as a background process on @reboot.
  Or run manually: python3 interrogation_listener.py

Ports:
  5556 — inbound  (receive HAS_DATA_FOR_INTERROGATION)
  5557 — outbound (send INTERROGATION_ACK to Scout)
"""

import os
import sys
import time
import json
import socket
import logging
import signal as _signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

# ── PATH RESOLUTION ───────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / '.env')

# ── CONFIG ────────────────────────────────────────────────────────────────
try:
    LISTEN_PORT  = int(os.environ.get('INTERROGATION_PORT', 5556))
except (ValueError, TypeError):
    LISTEN_PORT  = 5556
ACK_PORT         = LISTEN_PORT + 1          # Scout listens here for ACK
RECV_TIMEOUT     = 1.0                      # seconds per recv — tight loop
try:
    DUPLICATE_WINDOW = int(os.environ.get('INTERROGATION_DUPLICATE_WINDOW_HOURS', 6))
except (ValueError, TypeError):
    DUPLICATE_WINDOW = 6
try:
    RATE_LIMIT_HOUR  = int(os.environ.get('INTERROGATION_RATE_LIMIT_PER_HOUR', 3))
except (ValueError, TypeError):
    RATE_LIMIT_HOUR  = 3

# Heartbeat cadence — fault detection's GATE1_LIVENESS uses the default
# HEARTBEAT_STALE_MINUTES=45 threshold, so 60s cadence gives us 45× headroom
# and keeps DB writes light (one row per minute in system_log).
HEARTBEAT_INTERVAL_SEC = 60

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s interrogation: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_DIR / 'interrogation.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('interrogation_listener')

# ── RATE LIMITER ──────────────────────────────────────────────────────────
# In-process rate limit: track ACK timestamps per ticker.
# Prevents runaway ACKs if Scout somehow broadcasts repeatedly.
_ack_history = defaultdict(list)   # ticker → [timestamp, ...]


# ── HEARTBEAT WRITER ──────────────────────────────────────────────────────
def _post_heartbeat(accepted: int, rejected: int) -> None:
    """Write a HEARTBEAT row to the owner customer DB (where fault detection
    reads from). Silent on failure — heartbeat is a diagnostic signal, not
    a business rule, so a brief DB hiccup shouldn't crash the listener."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from retail_database import get_db, get_customer_db
        owner_id = os.environ.get('OWNER_CUSTOMER_ID', '')
        target = get_customer_db(owner_id) if owner_id else get_db()
        target.log_heartbeat(
            "interrogation_listener",
            "OK",
            portfolio_value=None,
        )
        log.debug(f"[HB] posted — accepted={accepted} rejected={rejected}")
    except Exception as e:
        log.debug(f"[HB] write failed (non-fatal): {e}")


def _rate_ok(ticker):
    """Return True if this ticker is under the per-hour ACK rate limit."""
    now   = time.monotonic()
    cutoff = now - 3600
    _ack_history[ticker] = [t for t in _ack_history[ticker] if t > cutoff]
    if len(_ack_history[ticker]) >= RATE_LIMIT_HOUR:
        return False
    _ack_history[ticker].append(now)
    return True


# ── VALIDATION ────────────────────────────────────────────────────────────

def validate(ticker, signal_id, price_summary):
    """
    Run all validation checks. Returns (ok: bool, reason: str).
    ok=True  → send ACK.
    ok=False → drop silently (Scout will mark UNVALIDATED).
    """
    # ── Check 1: ticker format ────────────────────────────────────────────
    clean = ticker.replace('.', '').replace('-', '').replace('/', '')
    if not clean or not clean.isalpha() or len(ticker) > 6:
        return False, f"ticker format invalid: {ticker!r}"

    # ── Check 2: rate limit ───────────────────────────────────────────────
    if not _rate_ok(ticker):
        return False, f"{ticker} rate-limited ({RATE_LIMIT_HOUR} ACKs/hr max)"

    # ── Check 3: no duplicate active signal in DB ─────────────────────────
    try:
        sys.path.insert(0, str(BASE_DIR))
        from retail_database import get_db
        db = get_db()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=DUPLICATE_WINDOW)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')
        with db.conn() as c:
            row = c.execute("""
                SELECT id FROM signals
                WHERE ticker = ?
                  AND status IN ('QUEUED', 'WATCHING')
                  AND created_at > ?
                LIMIT 1
            """, (ticker, cutoff)).fetchone()
        if row:
            existing_id = dict(row).get('id', '?')
            return False, (
                f"duplicate active signal for {ticker} "
                f"(existing id={str(existing_id)[:12]}, "
                f"window={DUPLICATE_WINDOW}h)"
            )
    except Exception as e:
        # DB unavailable — pass through rather than block valid signals
        log.warning(f"[CHECK-3] DB check failed (non-fatal): {e} — allowing through")

    # ── Check 4: price summary plausibility (if provided) ─────────────────
    if price_summary:
        try:
            summary = json.loads(price_summary) if isinstance(price_summary, str) else price_summary
            last_close = summary.get('last_close', 0)
            if last_close is not None and float(last_close) <= 0:
                return False, f"{ticker} price_summary shows last_close={last_close} (invalid)"
        except Exception:
            pass  # unparseable summary is not a blocker

    return True, "ok"


# ── MAIN LOOP ─────────────────────────────────────────────────────────────

def run():
    log.info(f"Interrogation listener starting — port {LISTEN_PORT}")
    log.info(f"Duplicate window: {DUPLICATE_WINDOW}h | Rate limit: {RATE_LIMIT_HOUR} ACKs/hr/ticker")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(RECV_TIMEOUT)

    try:
        sock.bind(('', LISTEN_PORT))
        log.info(f"Bound to UDP port {LISTEN_PORT} — listening")
    except OSError as e:
        log.error(f"Could not bind to port {LISTEN_PORT}: {e}")
        log.error("Is another instance already running? Check: pgrep -f interrogation_listener")
        sys.exit(1)

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        log.info(f"Received signal {sig} — shutting down")
        running = False

    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT,  _shutdown)

    accepted = 0
    rejected = 0
    last_heartbeat = 0.0

    # Emit one heartbeat at startup so fault detection sees us immediately
    # rather than waiting a full HEARTBEAT_INTERVAL_SEC after boot.
    _post_heartbeat(accepted, rejected)
    last_heartbeat = time.monotonic()

    while running:
        # Heartbeat cadence check — fires between recvfrom calls so a quiet
        # UDP channel doesn't mean a "dead" agent to the fault detector.
        if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            _post_heartbeat(accepted, rejected)
            last_heartbeat = time.monotonic()

        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as e:
            if running:
                log.warning(f"recv error: {e}")
            continue

        # Parse payload
        try:
            msg = json.loads(data.decode('utf-8'))
        except Exception:
            continue

        if msg.get('event') != 'HAS_DATA_FOR_INTERROGATION':
            continue

        signal_id     = str(msg.get('signal_id', ''))
        ticker        = str(msg.get('ticker', '')).upper().strip()
        price_summary = msg.get('price_summary')
        sender_ip     = addr[0]

        log.info(
            f"[IN]  {ticker} signal_id={signal_id[:12]} from {sender_ip}"
        )

        ok, reason = validate(ticker, signal_id, price_summary)

        if ok:
            ack_payload = json.dumps({
                "event":     "INTERROGATION_ACK",
                "signal_id": signal_id,
                "ticker":    ticker,
                "validator": socket.gethostname(),
            }).encode('utf-8')
            try:
                ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                ack_sock.sendto(ack_payload, (sender_ip, ACK_PORT))
                ack_sock.close()
                accepted += 1
                log.info(
                    f"[ACK] {ticker} → {sender_ip}:{ACK_PORT} "
                    f"(accepted={accepted} rejected={rejected})"
                )
            except Exception as e:
                log.warning(f"[ACK] Failed to send ACK for {ticker}: {e}")
        else:
            rejected += 1
            log.info(
                f"[NO-ACK] {ticker} — {reason} "
                f"(accepted={accepted} rejected={rejected})"
            )

    sock.close()
    log.info(
        f"Listener stopped — session totals: "
        f"{accepted} accepted, {rejected} rejected"
    )


if __name__ == '__main__':
    run()
