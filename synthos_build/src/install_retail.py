"""
install_retail.py — Synthos Retail Node Installer
Synthos · v1.1

Entry point for first-time setup and safe rerun/repair of a retail
customer Pi. Runs a local web wizard on port 8080.

USAGE:
    python3 install_retail.py            # first install or resume
    python3 install_retail.py --repair   # re-run INSTALLING + VERIFYING
    python3 install_retail.py --status   # print current install state

DESIGN RULES ENFORCED:
  - No hardcoded paths — all paths derived from this file's location
  - user/.env is NEVER overwritten without a timestamped backup
  - data/signals.db is NEVER touched if it exists
  - .known_good/, user/agreements/, consent_log.jsonl are NEVER touched
  - Installer is idempotent — safe to re-run
  - Re-run on COMPLETE system: prints status, exits 0 without changes
  - License key collected and written to .env; validation deferred to boot

STATE MACHINE:
    UNINITIALIZED → PREFLIGHT → COLLECTING → INSTALLING → VERIFYING → COMPLETE
                                                        ↘ DEGRADED (on failure)

EXIT CODES:
    0 — success or already complete
    1 — preflight failure (system cannot run installer)
    2 — install or verification failure (DEGRADED state)
    3 — operator cancelled
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# ── PATH RESOLUTION ───────────────────────────────────────────────────────────
# SYNTHOS_HOME is the build root (parent of src/ where this file lives).
# This is the ONLY path resolution in this file. All others derive from it.

SYNTHOS_HOME: Path = Path(__file__).resolve().parent.parent
CORE_DIR:     Path = SYNTHOS_HOME / "src"
AGENTS_DIR:   Path = SYNTHOS_HOME / "agents"
USER_DIR:     Path = SYNTHOS_HOME / "user"
DATA_DIR:     Path = SYNTHOS_HOME / "data"
LOG_DIR:      Path = SYNTHOS_HOME / "logs"
BACKUP_DIR:   Path = DATA_DIR / "backup"
CRASH_DIR:    Path = LOG_DIR / "crash_reports"
SNAPSHOT_DIR: Path = SYNTHOS_HOME / ".known_good"
ENV_PATH:     Path = USER_DIR / ".env"
DB_PATH:      Path = CORE_DIR / "signals.db"
SENTINEL_PATH: Path = SYNTHOS_HOME / ".install_complete"
PROGRESS_PATH: Path = SYNTHOS_HOME / ".install_progress.json"

# Bootstrap common helpers from the installers/common path relative to this file
_COMMON_DIR = SYNTHOS_HOME / "installers" / "common"
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR.parent.parent))

from installers.common.preflight import run_preflight
from installers.common.progress import ProgressManager
from installers.common.env_writer import write_env, build_retail_env

# ── CONSTANTS ─────────────────────────────────────────────────────────────────

SYNTHOS_VERSION = "1.1"
INSTALLER_PORT  = 8080

REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "python-dotenv",
    "anthropic",
    "feedparser",
    "psutil",
]

REQUIRED_AGENT_FILES = [
    "retail_trade_logic_agent.py",
    "retail_news_agent.py",
    "retail_market_sentiment_agent.py",
]

REQUIRED_CORE_FILES = [
    "retail_database.py",
    "retail_boot_sequence.py",
    "retail_watchdog.py",
    "retail_health_check.py",
    "retail_shutdown.py",
    # cleanup.py — not yet built; runs via cron for nightly DB maintenance;
    # absence is non-fatal at install time (boot_sequence.py also omits it).
    # Future implementation tracked in docs/milestones.md.
    "synthos_heartbeat.py",
    "retail_portal.py",
    "retail_patch.py",
    "retail_sync.py",
    # license_validator.py — DEFERRED_FROM_CURRENT_BASELINE
    # Retail entitlement validation is not implemented in the current release.
    # Remove from required files so installer can reach COMPLETE without it.
    # Future implementation tracked in docs/milestones.md (Retail Entitlement).
    "uninstall.py",
]

# Files that must never be overwritten or deleted during rerun
PROTECTED_PATHS = [
    ENV_PATH,                          # user/.env
    USER_DIR / "settings.json",        # user/settings.json
    USER_DIR / "agreements",           # user/agreements/
    DB_PATH,                           # src/signals.db
    BACKUP_DIR,                        # data/backup/
    SNAPSHOT_DIR,                      # .known_good/
    SYNTHOS_HOME / "consent_log.jsonl",# consent_log.jsonl
]

# ── LOGGING ───────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "install.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("install_retail")

# ── GLOBAL INSTALLER STATE ────────────────────────────────────────────────────
# Shared between HTTP handler and install thread.
# Protected by GIL — no explicit lock needed for simple flag/dict reads.

_state: dict[str, Any] = {
    "config":          {},
    "test_results":    {},
    "disclaimer":      False,
    "install_started": False,
    "install_done":    False,
    "install_error":   None,
    "log_lines":       [],
}


def _log_ui(message: str, level: str = "info") -> None:
    """Log to both file and the web UI log buffer."""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": message, "level": level}
    _state["log_lines"].append(entry)
    getattr(log, level, log.info)(message)


# ── DIRECTORY CREATION ────────────────────────────────────────────────────────

def create_directories() -> None:
    """Create all required directories. Never touches protected paths."""
    dirs = [
        CORE_DIR,
        USER_DIR,
        USER_DIR / "agreements",
        DATA_DIR,
        BACKUP_DIR,
        LOG_DIR,
        CRASH_DIR,
        SNAPSHOT_DIR,
    ]
    for d in dirs:
        if d in PROTECTED_PATHS and d.exists():
            _log_ui(f"  → Skipping protected dir (exists): {d.relative_to(SYNTHOS_HOME)}")
            continue
        d.mkdir(parents=True, exist_ok=True)
        _log_ui(f"  ✓ {d.relative_to(SYNTHOS_HOME)}")


# ── PACKAGE INSTALLATION ──────────────────────────────────────────────────────

def install_packages() -> bool:
    """
    Install required Python packages via pip.
    Returns True if all packages installed or already present.
    Uses --break-system-packages on Linux (Pi OS requirement).
    """
    import platform
    is_linux = platform.system() == "Linux"
    all_ok = True

    for pkg in REQUIRED_PACKAGES:
        _log_ui(f"  Installing {pkg}...")
        cmd = [sys.executable, "-m", "pip", "install", pkg, "-q"]
        if is_linux:
            cmd.append("--break-system-packages")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                _log_ui(f"  ✓ {pkg}")
            else:
                # pip returns non-zero even for "already satisfied" on some versions
                combined = (result.stdout + result.stderr).lower()
                if "already satisfied" in combined or "already installed" in combined:
                    _log_ui(f"  ✓ {pkg} (already installed)")
                else:
                    _log_ui(f"  ✗ {pkg}: {result.stderr[:200]}", "error")
                    all_ok = False
        except subprocess.TimeoutExpired:
            _log_ui(f"  ✗ {pkg}: timed out after 120s", "error")
            all_ok = False
        except Exception as exc:
            _log_ui(f"  ✗ {pkg}: {exc}", "error")
            all_ok = False

    return all_ok


# ── DATABASE BOOTSTRAP ────────────────────────────────────────────────────────

def bootstrap_database() -> bool:
    """
    Initialize signals.db schema by importing retail_database.py from src/.
    If signals.db already exists, this is a no-op (migrations run on import).
    Never overwrites existing data.
    """
    if DB_PATH.exists():
        _log_ui(f"  → signals.db exists — skipping bootstrap (protected)")
        return True

    db_module = CORE_DIR / "retail_database.py"
    if not db_module.exists():
        _log_ui("  ✗ src/retail_database.py not found — cannot bootstrap DB", "error")
        return False

    try:
        # Import database.py dynamically from src/
        import importlib.util
        spec = importlib.util.spec_from_file_location("database", db_module)
        db_mod = importlib.util.module_from_spec(spec)
        # Set DB path via environment before import
        os.environ["SYNTHOS_HOME"] = str(SYNTHOS_HOME)
        spec.loader.exec_module(db_mod)
        # database.py initializes schema on import via get_db() or similar
        if hasattr(db_mod, "get_db"):
            db_mod.get_db()
        _log_ui("  ✓ signals.db schema bootstrapped")
        return True
    except Exception as exc:
        _log_ui(f"  ✗ DB bootstrap failed: {exc}", "error")
        log.exception("DB bootstrap exception")
        return False


# ── CRON REGISTRATION ─────────────────────────────────────────────────────────

def register_cron() -> bool:
    """
    Write cron entries for this retail node.
    Uses resolved absolute paths — no hardcoded /home/pi anywhere.
    Removes any existing Synthos entries before writing new ones.
    """
    if not shutil.which("crontab"):
        _log_ui("  ⚠ crontab not found — add cron entries manually", "warning")
        return False

    py           = sys.executable
    boot_script  = str(CORE_DIR / "retail_boot_sequence.py")
    watchdog     = str(CORE_DIR / "retail_watchdog.py")
    portal       = str(CORE_DIR / "retail_portal.py")
    shutdown_scr = str(CORE_DIR / "retail_shutdown.py")
    boot_log     = str(LOG_DIR / "boot.log")

    bolt  = str(AGENTS_DIR / "retail_trade_logic_agent.py")
    pulse = str(AGENTS_DIR / "retail_market_sentiment_agent.py")
    scout = str(AGENTS_DIR / "retail_news_agent.py")

    def logf(name: str) -> str:
        return str(LOG_DIR / f"{name}.log")

    new_entries = "\n".join([
        f"# SYNTHOS RETAIL — generated by install_retail.py at {datetime.now().isoformat()}",
        "",
        "# Boot services",
        f"@reboot sleep 60 && {py} {boot_script} >> {boot_log} 2>&1",
        f"@reboot sleep 90 && {py} {watchdog} &",
        f"@reboot sleep 90 && {py} {portal} &",
        "",
        "# RETAIL AGENTS — market hours schedule (all times America/New_York)",
        "# Bolt (Trader) — :30 each half-hour, market hours (leads each cycle)",
        f"30 9-15 * * 1-5    {py} {bolt} >> {logf('trade_logic_agent')} 2>&1",
        "# Pulse (Sentiment) — :00 each half-hour, market hours (30 min after Bolt)",
        f"0 10-16 * * 1-5    {py} {pulse} >> {logf('market_sentiment_agent')} 2>&1",
        "# Scout (News) — :05 hourly market hours; overnight every 4h",
        f"5 9-15 * * 1-5     {py} {scout} >> {logf('news_agent')} 2>&1",
        f"5 1,5,21 * * 1-5   {py} {scout} --session=overnight >> {logf('news_agent')} 2>&1",
        "",
        "# Saturday maintenance",
        f"55 3 * * 6  {py} {shutdown_scr}",
        f"0  4 * * 6  sudo reboot",
        "",
    ])

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing_content = existing.stdout if existing.returncode == 0 else ""

        # Remove all existing Synthos lines
        clean_lines = [
            line for line in existing_content.splitlines()
            if "SYNTHOS" not in line
            and str(SYNTHOS_HOME) not in line
            and "boot_sequence" not in line
            and "watchdog" not in line.lower()
            and "shutdown" not in line.lower()
            and "portal" not in line
            and "sudo reboot" not in line
        ]
        clean_existing = "\n".join(clean_lines).strip()

        final_crontab = (clean_existing + "\n" + new_entries).strip() + "\n"

        proc = subprocess.run(
            ["crontab", "-"], input=final_crontab, text=True, capture_output=True
        )
        if proc.returncode == 0:
            _log_ui("  ✓ Cron schedule registered")
            return True
        else:
            _log_ui(f"  ✗ Cron error: {proc.stderr[:200]}", "error")
            return False
    except Exception as exc:
        _log_ui(f"  ✗ Cron setup failed: {exc}", "error")
        return False


# ── TIMEZONE ──────────────────────────────────────────────────────────────────

def set_timezone() -> None:
    """Set system timezone to America/New_York. Skips gracefully on non-Linux."""
    import platform
    if platform.system() != "Linux":
        _log_ui("  → Timezone: skipped (not Linux)")
        return
    try:
        result = subprocess.run(
            ["sudo", "timedatectl", "set-timezone", "America/New_York"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            _log_ui("  ✓ Timezone set to America/New_York (ET)")
        else:
            _log_ui(
                "  ⚠ Could not set timezone — run manually: "
                "sudo timedatectl set-timezone America/New_York",
                "warning",
            )
    except Exception as exc:
        _log_ui(f"  ⚠ Timezone: {exc}", "warning")


# ── HEALTH CHECK ──────────────────────────────────────────────────────────────

def run_health_check() -> bool:
    """
    Run retail_health_check.py as a subprocess.
    Returns True if exit code is 0.
    """
    hc_path = CORE_DIR / "retail_health_check.py"
    if not hc_path.exists():
        _log_ui("  ✗ retail_health_check.py not found in src/", "error")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(hc_path)],
            capture_output=True, text=True, timeout=120,
            cwd=str(SYNTHOS_HOME),
            env={**os.environ, "SYNTHOS_HOME": str(SYNTHOS_HOME)},
        )
        if result.returncode == 0:
            _log_ui("  ✓ retail_health_check.py passed")
            return True
        else:
            _log_ui(f"  ✗ retail_health_check.py failed (exit {result.returncode})", "error")
            if result.stdout:
                _log_ui(f"    stdout: {result.stdout[-500:]}", "error")
            if result.stderr:
                _log_ui(f"    stderr: {result.stderr[-500:]}", "error")
            return False
    except subprocess.TimeoutExpired:
        _log_ui("  ✗ retail_health_check.py timed out after 120s", "error")
        return False
    except Exception as exc:
        _log_ui(f"  ✗ retail_health_check.py exception: {exc}", "error")
        return False


# ── VERIFICATION ──────────────────────────────────────────────────────────────

def verify_installation() -> tuple[bool, list[str]]:
    """
    Run all post-install verification checks.
    Returns (passed: bool, failed_checks: list[str]).
    """
    failures: list[str] = []

    # 1. .env present and has required keys
    if not ENV_PATH.exists():
        failures.append(".env not found")
    else:
        required_keys = [
            "ANTHROPIC_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
            "CONGRESS_API_KEY", "OPERATING_MODE", "OWNER_NAME",
            "PORTAL_SECRET_KEY",
            # LICENSE_KEY — DEFERRED_FROM_CURRENT_BASELINE
            # Key is still collected during setup and written to .env for future use.
            # Not required for verification until license_validator.py is implemented.
        ]
        try:
            env_text = ENV_PATH.read_text(encoding="utf-8")
            present = {
                line.split("=")[0].strip()
                for line in env_text.splitlines()
                if "=" in line and not line.strip().startswith("#")
            }
            missing = [k for k in required_keys if k not in present]
            if missing:
                failures.append(f".env missing keys: {', '.join(missing)}")
        except OSError as exc:
            failures.append(f".env unreadable: {exc}")

    # 2. signals.db exists
    if not DB_PATH.exists():
        failures.append("data/signals.db not found")

    # 3. Required core files present
    missing_files = [f for f in REQUIRED_CORE_FILES if not (CORE_DIR / f).exists()]
    missing_files += [f for f in REQUIRED_AGENT_FILES if not (AGENTS_DIR / f).exists()]
    if missing_files:
        failures.append(f"Missing core files: {', '.join(missing_files)}")

    # 4. Required packages importable
    import importlib
    unimportable = []
    pkg_import_map = {
        "flask": "flask",
        "requests": "requests",
        "python-dotenv": "dotenv",
        "anthropic": "anthropic",
    }
    for pip_name, import_name in pkg_import_map.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            unimportable.append(pip_name)
    if unimportable:
        failures.append(f"Packages not importable: {', '.join(unimportable)}")

    # 5. Cron entries written
    if shutil.which("crontab"):
        try:
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            crontab = result.stdout
            if "boot_sequence" not in crontab:
                failures.append("boot_sequence cron entry missing")
        except Exception as exc:
            failures.append(f"Could not read crontab: {exc}")

    # 6. health_check
    if not run_health_check():
        failures.append("retail_health_check.py failed")

    passed = len(failures) == 0
    return passed, failures


# ── SENTINEL ──────────────────────────────────────────────────────────────────

def write_sentinel(pi_id: str) -> None:
    """Write .install_complete sentinel with install metadata."""
    content = {
        "version":      SYNTHOS_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "synthos_home": str(SYNTHOS_HOME),
        "pi_id":        pi_id,
        "node_type":    "retail",
    }
    SENTINEL_PATH.write_text(json.dumps(content, indent=2), encoding="utf-8")
    log.info("Sentinel written: %s", SENTINEL_PATH)


# ── MAIN INSTALL FLOW ─────────────────────────────────────────────────────────

def run_full_install(config: dict) -> bool:
    """
    Execute the INSTALLING and VERIFYING phases.
    Returns True on full success (COMPLETE), False on failure (DEGRADED).
    """
    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()

    _log_ui("── INSTALLING ──────────────────────────────────────")
    progress.transition("INSTALLING")

    # 1. Directories
    _log_ui("Creating directories...")
    create_directories()

    # 2. Write .env
    _log_ui("Writing environment configuration...")
    try:
        # Preserve existing PORTAL_SECRET_KEY if repair mode loaded it; otherwise generate
        secret_key = config.pop("_portal_secret_key", None) or secrets.token_hex(32)
        env_content = build_retail_env(config, secret_key)
        write_env(ENV_PATH, env_content)
        _log_ui("  ✓ user/.env written")
    except Exception as exc:
        _log_ui(f"  ✗ Failed to write .env: {exc}", "error")
        progress.transition("DEGRADED")
        progress.set("degraded_reason", f".env write failed: {exc}")
        return False

    # 3. Install packages
    _log_ui("Installing Python packages...")
    if not install_packages():
        _log_ui("  ✗ Package installation had failures — continuing (some may already be installed)", "warning")

    # 4. Bootstrap database
    _log_ui("Bootstrapping database...")
    if not bootstrap_database():
        _log_ui("  ✗ Database bootstrap failed", "error")
        progress.transition("DEGRADED")
        progress.set("degraded_reason", "DB bootstrap failed")
        return False

    # 5. Register cron
    _log_ui("Registering cron schedule...")
    register_cron()

    # 6. Timezone
    _log_ui("Setting timezone...")
    set_timezone()

    progress.set("install_complete", True)

    # 7. Verify
    _log_ui("── VERIFYING ───────────────────────────────────────")
    progress.transition("VERIFYING")

    passed, failures = verify_installation()

    if passed:
        _log_ui("── COMPLETE ────────────────────────────────────────")
        progress.transition("COMPLETE")
        write_sentinel(config.get("pi_id", "synthos-pi-1"))
        _log_ui("✓ Installation complete. Reboot to start Synthos.")
        _log_ui(f"  Sentinel: {SENTINEL_PATH}")
        return True
    else:
        _log_ui("── DEGRADED ────────────────────────────────────────")
        progress.transition("DEGRADED")
        for f in failures:
            _log_ui(f"  ✗ {f}", "error")
        progress.set("degraded_reason", failures)
        _log_ui("Installation is in DEGRADED state. Check logs and run --repair.", "error")
        return False


# ── API CONNECTION TESTS ──────────────────────────────────────────────────────

def test_anthropic(key: str) -> tuple[bool, str]:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.models.list()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)[:100]


def test_alpaca(key: str, secret: str, base_url: str) -> tuple[bool, str]:
    try:
        import requests
        resp = requests.get(
            f"{base_url.rstrip('/')}/v2/account",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)[:100]


def test_congress(key: str) -> tuple[bool, str]:
    try:
        import requests
        resp = requests.get(
            "https://api.congress.gov/v3/bill?limit=1",
            params={"api_key": key},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "ok"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)[:100]


# ── WEB WIZARD ────────────────────────────────────────────────────────────────

HTML_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f7; color: #1d1d1f; padding: 20px; }
  .card { background: white; border-radius: 12px; padding: 24px; max-width: 520px;
          margin: 0 auto; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
  .card-title { font-size: 20px; font-weight: 600; margin-bottom: 6px; }
  .card-sub { font-size: 13px; color: #666; margin-bottom: 20px; }
  label { font-size: 12px; font-weight: 500; color: #555; display: block;
          margin-top: 14px; margin-bottom: 4px; }
  input, select { width: 100%; padding: 9px 12px; border: 1px solid #ddd;
                  border-radius: 8px; font-size: 13px; }
  input:focus, select:focus { outline: none; border-color: #0071e3; }
  .btn { background: #0071e3; color: white; border: none; border-radius: 8px;
         padding: 11px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
         width: 100%; margin-top: 20px; }
  .btn:hover { background: #005bb5; }
  .btn-secondary { background: #f5f5f7; color: #1d1d1f; border: 1px solid #ddd; }
  .note { background: #f0f6ff; border-left: 3px solid #0071e3; padding: 10px 14px;
          font-size: 12px; color: #444; margin: 14px 0; border-radius: 0 8px 8px 0; }
  .warn { background: #fff8e1; border-left-color: #f5a623; color: #7a5200; }
  .error { background: #fff0f0; border-left-color: #e53935; color: #b71c1c; }
  .step { font-size: 11px; color: #999; margin-bottom: 16px; }
  .check { color: #34a853; font-weight: 600; }
  .fail  { color: #ea4335; font-weight: 600; }
  pre { background: #1d1d1f; color: #e0e0e0; padding: 12px; border-radius: 8px;
        font-size: 11px; overflow-y: auto; max-height: 320px; white-space: pre-wrap; }
</style>
"""


def _render_page(title: str, body: str, step: str = "") -> bytes:
    step_html = f'<div class="step">{step}</div>' if step else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<title>Synthos Installer</title>{HTML_STYLE}</head>
<body><div class="card">
<div class="card-title">Synthos v{SYNTHOS_VERSION} — Setup</div>
{step_html}
{body}
</div></body></html>"""
    return html.encode("utf-8")


class WizardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args) -> None:  # suppress default HTTP logs
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str) -> None:
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _read_post(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    # ── PAGES ─────────────────────────────────────────────────────────────────

    def page_welcome(self) -> bytes:
        body = """
        <div class="card-sub">Welcome. This wizard will configure your Synthos trading node.
        It takes about 5 minutes.</div>
        <div class="note">Make sure you have your API keys ready:<br>
        Anthropic, Alpaca, and Congress.gov</div>
        <form method="POST" action="/personal">
          <button class="btn" type="submit">Begin Setup →</button>
        </form>"""
        return _render_page("Welcome", body, "Step 0 of 7 — Welcome")

    def page_personal(self) -> bytes:
        cfg = _state["config"]
        body = f"""
        <div class="card-sub">Tell us about yourself.</div>
        <form method="POST" action="/personal">
          <label>Your full name</label>
          <input name="owner_name" value="{cfg.get('owner_name', '')}" required>
          <label>Your email address</label>
          <input name="owner_email" type="email" value="{cfg.get('owner_email', '')}" required>
          <label>Pi node ID (e.g. synthos-pi-1)</label>
          <input name="pi_id" value="{cfg.get('pi_id', 'synthos-pi-1')}">
          <button class="btn" type="submit">Next →</button>
        </form>"""
        return _render_page("About You", body, "Step 1 of 7 — Identity")

    def page_api_keys(self) -> bytes:
        cfg = _state["config"]
        tr = _state["test_results"]

        def status(key: str) -> str:
            if key not in tr:
                return ""
            return ' <span class="check">✓</span>' if tr[key] else ' <span class="fail">✗</span>'

        body = f"""
        <div class="card-sub">Enter your API keys. They will be tested before saving.</div>
        <form method="POST" action="/api-keys">
          <label>Anthropic API Key{status('anthropic')}</label>
          <input name="anthropic_key" value="{cfg.get('anthropic_key', '')}" placeholder="sk-ant-..." required>
          <label>Alpaca API Key{status('alpaca')}</label>
          <input name="alpaca_key" value="{cfg.get('alpaca_key', '')}" placeholder="PK..." required>
          <label>Alpaca Secret Key</label>
          <input name="alpaca_secret" value="{cfg.get('alpaca_secret', '')}" type="password" required>
          <label>Alpaca Base URL</label>
          <input name="alpaca_base_url" value="{cfg.get('alpaca_base_url', 'https://paper-api.alpaca.markets')}">
          <label>Trading Mode</label>
          <select name="trading_mode">
            <option value="PAPER" {"selected" if cfg.get("trading_mode","PAPER")=="PAPER" else ""}>PAPER (recommended)</option>
            <option value="LIVE" {"selected" if cfg.get("trading_mode")=="LIVE" else ""}>LIVE</option>
          </select>
          <label>Congress.gov API Key{status('congress')}</label>
          <input name="congress_key" value="{cfg.get('congress_key', '')}" required>
          <label>License Key</label>
          <input name="license_key" value="{cfg.get('license_key', '')}" placeholder="synthos-pi-01-..." required>
          <div class="note">Keys will be tested live. You will see results before proceeding.</div>
          <button class="btn" type="submit">Test Keys →</button>
        </form>"""
        return _render_page("API Keys", body, "Step 2 of 7 — Keys")

    def page_alerts(self) -> bytes:
        cfg = _state["config"]
        body = f"""
        <div class="card-sub">Optional: configure alerts and the monitor server.</div>
        <form method="POST" action="/alerts">
          <label>Monitor Server URL (optional)</label>
          <input name="monitor_url" value="{cfg.get('monitor_url', '')}" placeholder="http://your-monitor-pi:5000">
          <label>Monitor Token</label>
          <input name="monitor_token" value="{cfg.get('monitor_token', 'changeme')}">
          <label>Resend API Key (optional — protective exit emails)</label>
          <input name="resend_key" value="{cfg.get('resend_key', '')}">
          <label>Alert From Address</label>
          <input name="alert_from" value="{cfg.get('alert_from', '')}" placeholder="alerts@yourdomain.com">
          <label>Your Email (alert recipient)</label>
          <input name="user_email" value="{cfg.get('user_email', '')}" placeholder="you@example.com">
          <label>Gmail User (crash SMS alerts, optional)</label>
          <input name="gmail_user" value="{cfg.get('gmail_user', '')}" placeholder="youraddress@gmail.com">
          <label>Gmail App Password</label>
          <input name="gmail_app_password" value="{cfg.get('gmail_app_password', '')}" type="password">
          <label>Alert Phone (10-digit)</label>
          <input name="alert_phone" value="{cfg.get('alert_phone', '')}" placeholder="8005551234">
          <button class="btn" type="submit">Next →</button>
        </form>"""
        return _render_page("Alerts", body, "Step 3 of 7 — Alerts")

    def page_portal(self) -> bytes:
        cfg = _state["config"]
        body = f"""
        <div class="card-sub">Configure the web portal (available on your local network).</div>
        <form method="POST" action="/portal">
          <label>Portal Password (leave blank for open LAN access)</label>
          <input name="portal_password" value="{cfg.get('portal_password', '')}" type="password">
          <label>Portal Port</label>
          <input name="portal_port" value="{cfg.get('portal_port', '5001')}">
          <div class="note">Portal runs at http://&lt;your-pi&gt;.local:{cfg.get("portal_port","5001")}</div>
          <button class="btn" type="submit">Next →</button>
        </form>"""
        return _render_page("Portal", body, "Step 4 of 7 — Portal")

    def page_capital(self) -> bytes:
        cfg = _state["config"]
        body = f"""
        <div class="card-sub">Set your starting capital and operating mode.</div>
        <form method="POST" action="/capital">
          <label>Starting Capital (USD)</label>
          <input name="starting_capital" type="number" min="10" max="100000"
                 value="{cfg.get('starting_capital', '1000')}" required>
          <label>Operating Mode</label>
          <select name="operating_mode">
            <option value="SUPERVISED" {"selected" if cfg.get("operating_mode","SUPERVISED")=="SUPERVISED" else ""}>
              SUPERVISED — you approve all trades</option>
            <option value="AUTONOMOUS" {"selected" if cfg.get("operating_mode")=="AUTONOMOUS" else ""}>
              AUTONOMOUS — requires unlock key</option>
          </select>
          <div class="note warn">In SUPERVISED mode, all trades queue for your approval via the portal.
          AUTONOMOUS mode requires a separate unlock key from Synthos.</div>
          <button class="btn" type="submit">Next →</button>
        </form>"""
        return _render_page("Capital", body, "Step 5 of 7 — Capital")

    def page_disclaimer(self) -> bytes:
        body = """
        <div class="card-sub">Read and accept before proceeding.</div>
        <div class="note warn" style="line-height:1.7; font-size:12px">
          <strong>Synthos is experimental software.</strong><br><br>
          Trading involves significant risk of financial loss.
          Past performance does not guarantee future results.
          Synthos is not a licensed financial advisor.<br><br>
          By continuing, you confirm:<br>
          • You are using paper trading (simulated) unless you explicitly enabled LIVE mode<br>
          • You accept full responsibility for all trading decisions and outcomes<br>
          • You understand this software is provided as-is with no warranty<br>
          • You have read and agreed to the Synthos operating agreement
        </div>
        <form method="POST" action="/disclaimer">
          <input type="hidden" name="disclaimer_accepted" value="yes">
          <button class="btn" type="submit">I Accept — Continue to Install →</button>
        </form>
        <form method="POST" action="/">
          <button class="btn btn-secondary" type="submit" style="margin-top:8px">← Back</button>
        </form>"""
        return _render_page("Disclaimer", body, "Step 6 of 7 — Agreement")

    def page_review(self) -> bytes:
        cfg = _state["config"]
        tr = _state["test_results"]

        def check(key: str) -> str:
            if key not in tr:
                return ""
            return '<span class="check">✓</span>' if tr[key] else '<span class="fail">✗ NOT SET</span>'

        def mask(val: str) -> str:
            if not val:
                return "— not set"
            return val[:4] + "••••" if len(val) > 4 else "••••"

        body = f"""
        <div class="card-sub">Confirm your configuration before installing.</div>
        <table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:16px">
          <tr><td style="padding:5px 0;color:#555;width:160px">Owner name</td>
              <td>{cfg.get('owner_name','—')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Email</td>
              <td>{cfg.get('owner_email','—')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Pi ID</td>
              <td>{cfg.get('pi_id','—')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Anthropic key</td>
              <td>{mask(cfg.get('anthropic_key',''))} {check('anthropic')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Alpaca key</td>
              <td>{mask(cfg.get('alpaca_key',''))} {check('alpaca')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Congress.gov key</td>
              <td>{mask(cfg.get('congress_key',''))} {check('congress')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">License key</td>
              <td>{mask(cfg.get('license_key',''))}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Mode</td>
              <td>{cfg.get('operating_mode','SUPERVISED')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Capital</td>
              <td>${cfg.get('starting_capital','—')}</td></tr>
          <tr><td style="padding:5px 0;color:#555">Monitor URL</td>
              <td>{cfg.get('monitor_url','—') or '— (set later)'}</td></tr>
        </table>
        <div class="note warn">Do not close this tab during installation. Takes 2–5 minutes.</div>
        <form method="POST" action="/run-install">
          <button class="btn" type="submit">Install Now →</button>
        </form>"""
        return _render_page("Review", body, "Step 7 of 7 — Install")

    def page_installing(self) -> bytes:
        lines = _state.get("log_lines", [])[-40:]
        log_html = "\n".join(
            f'<span style="color:{"#ff6b6b" if l["level"]=="error" else "#ccc"}">'
            f'{l["ts"]}  {l["msg"]}</span>'
            for l in lines
        )
        if _state["install_done"]:
            if _state["install_error"]:
                status = f'<div class="note error">✗ Installation failed: {_state["install_error"]}<br>Check logs/install.log</div>'
                action = '<a href="/"><button class="btn btn-secondary">← Start Over</button></a>'
            else:
                status = '<div class="note">✓ Installation complete. Reboot your Pi to start Synthos.</div>'
                action = ''
        else:
            status = '<div class="note">Installation in progress — do not close this tab.</div>'
            action = '<script>setTimeout(()=>location.reload(),3000)</script>'

        body = f"""
        {status}
        <pre>{log_html or "(waiting for output...)"}</pre>
        {action}"""
        return _render_page("Installing...", body, "Installing")

    # ── ROUTING ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        routes = {
            "/":           self.page_welcome,
            "/personal":   self.page_personal,
            "/api-keys":   self.page_api_keys,
            "/alerts":     self.page_alerts,
            "/portal":     self.page_portal,
            "/capital":    self.page_capital,
            "/disclaimer": self.page_disclaimer,
            "/review":     self.page_review,
            "/installing": self.page_installing,
        }
        handler = routes.get(path, self.page_welcome)
        self._send(handler())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self._read_post()

        if path == "/personal":
            _state["config"].update({
                "owner_name":  data.get("owner_name", "").strip(),
                "owner_email": data.get("owner_email", "").strip(),
                "pi_id":       data.get("pi_id", "synthos-pi-1").strip(),
            })
            self._redirect("/api-keys")

        elif path == "/api-keys":
            _state["config"].update({
                "anthropic_key":  data.get("anthropic_key", "").strip(),
                "alpaca_key":     data.get("alpaca_key", "").strip(),
                "alpaca_secret":  data.get("alpaca_secret", "").strip(),
                "alpaca_base_url": data.get("alpaca_base_url",
                                            "https://paper-api.alpaca.markets").strip(),
                "trading_mode":   data.get("trading_mode", "PAPER"),
                "congress_key":   data.get("congress_key", "").strip(),
                "license_key":    data.get("license_key", "").strip(),
            })
            # Run live tests
            cfg = _state["config"]
            ok_a, _ = test_anthropic(cfg["anthropic_key"])
            ok_b, _ = test_alpaca(cfg["alpaca_key"], cfg["alpaca_secret"], cfg["alpaca_base_url"])
            ok_c, _ = test_congress(cfg["congress_key"])
            _state["test_results"] = {
                "anthropic": ok_a,
                "alpaca":    ok_b,
                "congress":  ok_c,
            }
            self._redirect("/api-keys")

        elif path == "/alerts":
            _state["config"].update({
                "monitor_url":        data.get("monitor_url", "").strip(),
                "monitor_token":      data.get("monitor_token", "changeme").strip(),
                "resend_key":         data.get("resend_key", "").strip(),
                "alert_from":         data.get("alert_from", "").strip(),
                "user_email":         data.get("user_email", "").strip(),
                "gmail_user":         data.get("gmail_user", "").strip(),
                "gmail_app_password": data.get("gmail_app_password", "").strip(),
                "alert_phone":        data.get("alert_phone", "").strip(),
            })
            self._redirect("/portal")

        elif path == "/portal":
            _state["config"].update({
                "portal_password": data.get("portal_password", "").strip(),
                "portal_port":     data.get("portal_port", "5001").strip(),
            })
            self._redirect("/capital")

        elif path == "/capital":
            try:
                cap = float(data.get("starting_capital", "1000"))
                cap = max(10, min(100000, cap))
            except (ValueError, TypeError):
                cap = 1000
            _state["config"].update({
                "starting_capital": str(int(cap)),
                "operating_mode":   data.get("operating_mode", "SUPERVISED"),
            })
            self._redirect("/disclaimer")

        elif path == "/disclaimer":
            if data.get("disclaimer_accepted") == "yes":
                _state["disclaimer"] = True
                progress = ProgressManager(SYNTHOS_HOME)
                progress.load()
                progress.set("disclaimer_accepted", True)
                progress.transition("COLLECTING")
                self._redirect("/review")
            else:
                self._redirect("/disclaimer")

        elif path == "/run-install":
            if _state["install_started"]:
                log.warning("Duplicate /run-install ignored")
                self._redirect("/installing")
                return

            if not _state["disclaimer"]:
                self._redirect("/disclaimer")
                return

            _state["install_started"] = True

            def _install_thread() -> None:
                try:
                    result = run_full_install(_state["config"])
                    _state["install_done"] = True
                    if not result:
                        _state["install_error"] = "Verification failed — see logs/install.log"
                except Exception as exc:
                    log.exception("Unexpected install error")
                    _state["install_done"] = True
                    _state["install_error"] = str(exc)

            t = threading.Thread(target=_install_thread, daemon=True)
            t.start()
            self._redirect("/installing")

        else:
            self._redirect("/")


# ── STATUS CHECK ──────────────────────────────────────────────────────────────

def print_status() -> None:
    """Print current install state to stdout."""
    print()
    print("=" * 50)
    print(f"  SYNTHOS RETAIL — Install Status")
    print("=" * 50)
    print(f"  SYNTHOS_HOME : {SYNTHOS_HOME}")

    if SENTINEL_PATH.exists():
        sentinel = json.loads(SENTINEL_PATH.read_text())
        print(f"  State        : COMPLETE")
        print(f"  Installed at : {sentinel.get('installed_at','?')}")
        print(f"  Pi ID        : {sentinel.get('pi_id','?')}")
    elif PROGRESS_PATH.exists():
        prog = json.loads(PROGRESS_PATH.read_text())
        print(f"  State        : {prog.get('state','UNKNOWN')}")
        if "degraded_reason" in prog:
            print(f"  Degraded     : {prog['degraded_reason']}")
    else:
        print(f"  State        : UNINITIALIZED")

    print(f"  .env exists  : {ENV_PATH.exists()}")
    print(f"  DB exists    : {DB_PATH.exists()}")
    print()


# ── REPAIR MODE ───────────────────────────────────────────────────────────────

def repair_mode() -> int:
    """
    Re-run INSTALLING + VERIFYING without repeating COLLECTING.
    Requires user/.env to already exist.
    """
    print()
    print("=" * 50)
    print("  SYNTHOS RETAIL — Repair Mode")
    print("=" * 50)

    if not ENV_PATH.exists():
        print("  ✗ user/.env not found — cannot repair without configuration.")
        print("  Run install_retail.py (without --repair) to reconfigure.")
        return 1

    # Load existing config from .env for package/cron steps.
    # Map raw env var names → wizard key names so build_retail_env preserves all values.
    _env_to_wizard = {
        "ANTHROPIC_API_KEY":    "anthropic_key",
        "ALPACA_API_KEY":       "alpaca_key",
        "ALPACA_SECRET_KEY":    "alpaca_secret",
        "ALPACA_BASE_URL":      "alpaca_base_url",
        "TRADING_MODE":         "trading_mode",
        "CONGRESS_API_KEY":     "congress_key",
        "OPERATING_MODE":       "operating_mode",
        "AUTONOMOUS_UNLOCK_KEY":"autonomous_unlock_key",
        "LICENSE_KEY":          "license_key",
        "PI_ID":                "pi_id",
        "PI_LABEL":             "pi_label",
        "PI_EMAIL":             "pi_email",
        "OWNER_NAME":           "owner_name",
        "OWNER_EMAIL":          "owner_email",
        "STARTING_CAPITAL":     "starting_capital",
        "SUPPORT_EMAIL":        "support_email",
        "PORTAL_PORT":          "portal_port",
        "PORTAL_PASSWORD":      "portal_password",
        "PORTAL_SECRET_KEY":    "_portal_secret_key",
        "MONITOR_URL":          "monitor_url",
        "MONITOR_TOKEN":        "monitor_token",
        "RESEND_API_KEY":       "resend_key",
        "ALERT_FROM":           "alert_from",
        "USER_EMAIL":           "user_email",
        "GMAIL_USER":           "gmail_user",
        "GMAIL_APP_PASSWORD":   "gmail_app_password",
        "ALERT_PHONE":          "alert_phone",
        "CARRIER_GATEWAY":      "carrier_gateway",
        "GITHUB_TOKEN":         "github_token",
    }
    config: dict[str, str] = {}
    try:
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                k = k.strip()
                wizard_key = _env_to_wizard.get(k, k)
                config[wizard_key] = v.strip()
    except OSError as exc:
        print(f"  ✗ Could not read .env: {exc}")
        return 1

    print("  Running repair: INSTALLING → VERIFYING")
    print()

    # Remove degraded state so run_full_install doesn't short-circuit
    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()
    if progress.state in ("DEGRADED", "COMPLETE"):
        progress.transition("INSTALLING")

    success = run_full_install(config)
    return 0 if success else 2


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthos Retail Node Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repair", action="store_true",
                        help="Re-run install/verify phases without recollecting config")
    parser.add_argument("--status", action="store_true",
                        help="Print current install state and exit")
    args = parser.parse_args()

    log.info("=" * 55)
    log.info("SYNTHOS v%s — RETAIL INSTALLER", SYNTHOS_VERSION)
    log.info("SYNTHOS_HOME: %s", SYNTHOS_HOME)
    log.info("=" * 55)

    if args.status:
        print_status()
        return 0

    if args.repair:
        return repair_mode()

    # Check for already-complete install
    if SENTINEL_PATH.exists():
        print()
        print("=" * 50)
        print("  Synthos retail node is already installed.")
        sentinel = json.loads(SENTINEL_PATH.read_text())
        print(f"  Installed at: {sentinel.get('installed_at','?')}")
        print(f"  Pi ID:        {sentinel.get('pi_id','?')}")
        print()
        print("  To repair:   python3 install_retail.py --repair")
        print("  To check:    python3 install_retail.py --status")
        print()
        return 0

    # Preflight
    print()
    print("=" * 55)
    print(f"  SYNTHOS v{SYNTHOS_VERSION} — RETAIL INSTALLER")
    print("=" * 55)

    preflight = run_preflight()
    print(preflight.report())
    print()

    if not preflight.passed:
        log.error("Preflight failed — cannot continue")
        return 1

    if preflight.warnings:
        print("  ⚠  Warnings above are non-fatal but should be reviewed.\n")

    # Resume or start progress
    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()

    if progress.get("disclaimer_accepted") and not ENV_PATH.exists():
        # Previous run got through disclaimer — skip wizard, go straight to install
        log.info("Resuming from prior session (disclaimer already accepted)")
        config = progress.get("collected_config", {})
        if not config:
            log.warning("Disclaimer accepted but no config found — restarting wizard")
        else:
            success = run_full_install(config)
            return 0 if success else 2

    # Launch web wizard
    try:
        server = HTTPServer(("0.0.0.0", INSTALLER_PORT), WizardHandler)
    except OSError as exc:
        log.error("Cannot start web server on port %d: %s", INSTALLER_PORT, exc)
        print(f"\n  ✗ Cannot start installer on port {INSTALLER_PORT}: {exc}")
        print(f"  Is another process using that port? Try: sudo lsof -i :{INSTALLER_PORT}")
        return 1

    import socket
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "your-pi"

    progress.transition("COLLECTING")

    print(f"\n  Installer web UI running.")
    print(f"  Open a browser on the same network and go to:")
    print(f"")
    print(f"      http://{hostname}.local:{INSTALLER_PORT}")
    print(f"      http://localhost:{INSTALLER_PORT}  (if accessing Pi directly)")
    print(f"")
    print(f"  Press Ctrl+C to stop.\n")
    log.info("Web wizard listening on port %d", INSTALLER_PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Installer stopped by operator.")
        log.info("Installer stopped by operator (Ctrl+C)")
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
