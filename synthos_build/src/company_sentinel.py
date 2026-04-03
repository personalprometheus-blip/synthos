"""
company_sentinel.py — Company Pi Health Watchdog
=================================================
Runs on the Company Pi as a background daemon.
Monitors the health of co-resident services and the system itself, then
enqueues CRITICAL alerts via company_server when anything looks wrong.

Services monitored:
    - company_server.py  — GET /health on COMPANY_PORT (default 5010)
    - scoop.py           — process name check via ps
    - strongbox.py       — log recency check (alert if silent > 26 hours)
    - disk space         — alert if free space < 500 MB
    - company.db         — existence and readability check

On any failure a P0 (CRITICAL) event is POSTed to:
    POST http://localhost:{COMPANY_PORT}/api/enqueue
with header X-Token: SECRET_TOKEN, so Scoop dispatches it immediately.

Cooldown: the same service will not re-alert within 30 minutes.

.env required:
    SECRET_TOKEN      — shared token for company_server auth

.env optional:
    COMPANY_PORT      — company_server port (default 5010)
    SENTINEL_POLL_S   — seconds between full health sweeps (default 120)
    SENTINEL_LOG      — log file path (default logs/sentinel.log)

Usage:
    python3 company_sentinel.py
    # or daemonized:
    nohup python3 company_sentinel.py >> logs/sentinel.log 2>&1 &
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_TOKEN  = os.getenv("SECRET_TOKEN", "")
COMPANY_PORT  = int(os.getenv("COMPANY_PORT", 5010))
POLL_S        = int(os.getenv("SENTINEL_POLL_S", 120))
SENTINEL_LOG  = os.getenv("SENTINEL_LOG", "")

# Alert cooldown: don't re-alert the same service within this many seconds.
COOLDOWN_S    = 30 * 60   # 30 minutes

# Strongbox is nightly — alert if its log is silent for longer than this.
STRONGBOX_MAX_SILENCE_S = 26 * 3600  # 26 hours

# Minimum free disk space before alerting (bytes).
DISK_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
DB_PATH  = os.getenv("COMPANY_DB_PATH", os.path.join(DATA_DIR, "company.db"))

# Strongbox writes to logs/strongbox.log (relative to synthos_build root).
_BUILD_DIR    = os.path.dirname(_HERE)
LOG_DIR       = os.path.join(_BUILD_DIR, "logs")
STRONGBOX_LOG = os.path.join(LOG_DIR, "strongbox.log")

# ── Logging ───────────────────────────────────────────────────────────────────
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if SENTINEL_LOG:
    os.makedirs(os.path.dirname(os.path.abspath(SENTINEL_LOG)), exist_ok=True)
    _log_handlers.append(logging.FileHandler(SENTINEL_LOG))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SENTINEL] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=_log_handlers,
)
log = logging.getLogger("sentinel")

# ── Shutdown flag ─────────────────────────────────────────────────────────────
_shutdown = False

def _handle_signal(signum: int, frame) -> None:
    global _shutdown
    log.info("Signal %s received — shutting down.", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── Cooldown tracker ──────────────────────────────────────────────────────────
# Maps service name → unix timestamp of last alert sent.
_last_alerted: dict[str, float] = {}


def _in_cooldown(service: str) -> bool:
    last = _last_alerted.get(service, 0.0)
    return (time.monotonic() - last) < COOLDOWN_S


def _mark_alerted(service: str) -> None:
    _last_alerted[service] = time.monotonic()


# ── Alert dispatch ────────────────────────────────────────────────────────────

def _enqueue_alert(service: str, detail: str) -> None:
    """POST a P0 CRITICAL alert to company_server /api/enqueue."""
    if _in_cooldown(service):
        log.debug("Cooldown active for %s — skipping alert.", service)
        return

    subject = f"[SENTINEL] CRITICAL — {service} unhealthy"
    body    = (
        f"Company Pi sentinel detected a problem with {service}.\n\n"
        f"Detail: {detail}\n\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
    )

    payload = json.dumps({
        "event_type": "sentinel_alert",
        "priority":   0,
        "subject":    subject,
        "body":       body,
        "service":    service,
    }).encode("utf-8")

    url = f"http://localhost:{COMPANY_PORT}/api/enqueue"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Token":      SECRET_TOKEN,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        log.info("Alert enqueued for %s (HTTP %s).", service, status)
        _mark_alerted(service)
    except Exception as exc:
        log.error("Failed to enqueue alert for %s: %s", service, exc)


# ── Health checks ─────────────────────────────────────────────────────────────

def check_company_server() -> None:
    """GET /health on the local company_server."""
    service = "company_server"
    url     = f"http://localhost:{COMPANY_PORT}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                log.debug("%s: /health OK (HTTP 200).", service)
                return
            detail = f"/health returned HTTP {resp.status}"
    except urllib.error.URLError as exc:
        detail = f"Connection error: {exc.reason}"
    except Exception as exc:
        detail = str(exc)

    log.warning("%s unhealthy: %s", service, detail)
    _enqueue_alert(service, detail)


def check_scoop() -> None:
    """Confirm scoop.py appears in the running process list."""
    service = "scoop"
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "scoop.py" in result.stdout:
            log.debug("%s: process found.", service)
            return
        detail = "scoop.py not found in ps aux output"
    except Exception as exc:
        detail = f"ps check failed: {exc}"

    log.warning("%s unhealthy: %s", service, detail)
    _enqueue_alert(service, detail)


def check_strongbox() -> None:
    """Check that strongbox.log was written within the last 26 hours."""
    service = "strongbox"
    if not os.path.exists(STRONGBOX_LOG):
        detail = f"Log file not found: {STRONGBOX_LOG}"
        log.warning("%s unhealthy: %s", service, detail)
        _enqueue_alert(service, detail)
        return

    try:
        mtime   = os.path.getmtime(STRONGBOX_LOG)
        age_s   = time.time() - mtime
        if age_s <= STRONGBOX_MAX_SILENCE_S:
            log.debug("%s: log last modified %.1f hours ago — OK.", service, age_s / 3600)
            return
        detail = (
            f"Log last modified {age_s / 3600:.1f} hours ago "
            f"(threshold: {STRONGBOX_MAX_SILENCE_S / 3600:.0f} hours)"
        )
    except Exception as exc:
        detail = f"Could not stat log: {exc}"

    log.warning("%s unhealthy: %s", service, detail)
    _enqueue_alert(service, detail)


def check_disk() -> None:
    """Alert if free disk space on the root partition drops below 500 MB."""
    service = "disk"
    try:
        usage = shutil.disk_usage("/")
        if usage.free >= DISK_MIN_FREE_BYTES:
            log.debug(
                "disk: %.1f MB free — OK.",
                usage.free / (1024 * 1024),
            )
            return
        detail = (
            f"Only {usage.free / (1024 * 1024):.1f} MB free "
            f"(threshold: {DISK_MIN_FREE_BYTES / (1024 * 1024):.0f} MB)"
        )
    except Exception as exc:
        detail = f"disk_usage check failed: {exc}"

    log.warning("disk unhealthy: %s", detail)
    _enqueue_alert(service, detail)


def check_company_db() -> None:
    """Verify company.db exists and is readable."""
    service = "company_db"
    if not os.path.exists(DB_PATH):
        detail = f"Database file not found: {DB_PATH}"
        log.warning("%s unhealthy: %s", service, detail)
        _enqueue_alert(service, detail)
        return

    try:
        with open(DB_PATH, "rb") as fh:
            fh.read(16)  # read the SQLite header bytes
        log.debug("%s: exists and is readable.", service)
    except Exception as exc:
        detail = f"Cannot read database: {exc}"
        log.warning("%s unhealthy: %s", service, detail)
        _enqueue_alert(service, detail)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_checks() -> None:
    """Run all health checks in sequence."""
    log.info("Running health sweep…")
    check_company_server()
    check_scoop()
    check_strongbox()
    check_disk()
    check_company_db()
    log.info("Health sweep complete.")


def main() -> None:
    log.info(
        "Sentinel starting — poll=%ds cooldown=%dm port=%d",
        POLL_S,
        COOLDOWN_S // 60,
        COMPANY_PORT,
    )

    if not SECRET_TOKEN:
        log.warning("SECRET_TOKEN is not set — alerts will be rejected by company_server.")

    while not _shutdown:
        try:
            run_checks()
        except Exception as exc:
            log.error("Unexpected error during health sweep: %s", exc, exc_info=True)

        # Sleep in short increments so SIGTERM is handled promptly.
        for _ in range(POLL_S):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Sentinel stopped.")


if __name__ == "__main__":
    main()
