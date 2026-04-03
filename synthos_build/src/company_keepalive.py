"""
company_keepalive.py — Company Pi Keep-Alive Daemon
====================================================
Runs on the Company Pi as a lightweight background process.
Prevents the Pi from going idle, keeps network connections warm, and emits
a periodic liveness ping to company_server's /health endpoint.

Behavior:
    - Pings GET http://localhost:{COMPANY_PORT}/health every KEEPALIVE_INTERVAL_S
    - Performs a small disk read/write cycle to keep storage active
    - Logs activity at DEBUG level; logs warnings on connection failure
    - Does NOT alert on failure — company_sentinel.py owns alerting

.env optional:
    KEEPALIVE_INTERVAL_S  — seconds between pings (default 60)
    COMPANY_PORT          — company_server port (default 5010)
    KEEPALIVE_LOG         — log file path (stdout only if unset)

Usage:
    python3 company_keepalive.py
    # or daemonized:
    nohup python3 company_keepalive.py >> logs/keepalive.log 2>&1 &
"""

import logging
import os
import signal
import sys
import time
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
INTERVAL_S    = int(os.getenv("KEEPALIVE_INTERVAL_S", 60))
COMPANY_PORT  = int(os.getenv("COMPANY_PORT", 5010))
KEEPALIVE_LOG = os.getenv("KEEPALIVE_LOG", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE       = os.path.dirname(os.path.abspath(__file__))
_BUILD_DIR  = os.path.dirname(_HERE)

# Scratch file used for the disk I/O keepalive touch.
_TOUCH_PATH = os.path.join(_BUILD_DIR, "logs", ".keepalive_touch")

# ── Logging ───────────────────────────────────────────────────────────────────
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if KEEPALIVE_LOG:
    os.makedirs(os.path.dirname(os.path.abspath(KEEPALIVE_LOG)), exist_ok=True)
    _log_handlers.append(logging.FileHandler(KEEPALIVE_LOG))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [KEEPALIVE] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger("keepalive")

# ── Shutdown flag ─────────────────────────────────────────────────────────────
_shutdown = False

def _handle_signal(signum: int, frame) -> None:
    global _shutdown
    log.info("Signal %s received — shutting down.", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── Keepalive actions ─────────────────────────────────────────────────────────

def ping_server() -> None:
    """GET /health on the local company_server to confirm it is reachable."""
    url = f"http://localhost:{COMPANY_PORT}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            log.debug("ping /health → HTTP %s", resp.status)
    except urllib.error.URLError as exc:
        log.warning("ping /health failed: %s", exc.reason)
    except Exception as exc:
        log.warning("ping /health error: %s", exc)


def touch_disk() -> None:
    """Write and read a tiny sentinel file to keep disk I/O warm."""
    try:
        os.makedirs(os.path.dirname(_TOUCH_PATH), exist_ok=True)
        ts = str(time.time())
        with open(_TOUCH_PATH, "w") as fh:
            fh.write(ts)
        with open(_TOUCH_PATH, "r") as fh:
            fh.read()
        log.debug("disk touch OK (ts=%s)", ts)
    except Exception as exc:
        log.warning("disk touch failed: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info(
        "Keepalive starting — interval=%ds port=%d",
        INTERVAL_S,
        COMPANY_PORT,
    )

    while not _shutdown:
        try:
            ping_server()
            touch_disk()
        except Exception as exc:
            log.warning("Unexpected error in keepalive cycle: %s", exc, exc_info=True)

        # Sleep in short increments so SIGTERM is handled promptly.
        for _ in range(INTERVAL_S):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Keepalive stopped.")


if __name__ == "__main__":
    main()
