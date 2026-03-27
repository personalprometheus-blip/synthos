"""
install_company.py — Synthos Company Node Installer
Synthos · v1.1

Entry point for first-time setup and safe rerun/repair of the company
operations Pi. Runs as a CLI tool — no web wizard, no browser required.
Internal deployment only.

USAGE:
    python3 install_company.py            # first install or resume
    python3 install_company.py --repair   # re-run INSTALLING + VERIFYING
    python3 install_company.py --status   # print current install state

DESIGN RULES ENFORCED:
  - No hardcoded paths — all paths derived from this file's location
  - company.env is NEVER overwritten without a timestamped backup
  - No retail-style onboarding flow — no wizard, no license activation
  - COMPANY_MODE=true always written — disables license checks on all agents
  - DB schema bootstrapped directly by installer via db_helpers.py import
  - Idempotent — safe to re-run

STATE MACHINE:
    UNINITIALIZED → PREFLIGHT → COLLECTING → INSTALLING → VERIFYING → COMPLETE
                                                        ↘ DEGRADED (on failure)

EXIT CODES:
    0 — success or already complete
    1 — preflight failure
    2 — install or verification failure (DEGRADED state)
    3 — operator cancelled
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any, Optional

# ── PATH RESOLUTION ───────────────────────────────────────────────────────────
# SYNTHOS_HOME is the parent of this file — which is synthos-company/
# All other paths derive from here.

SYNTHOS_HOME: Path = Path(__file__).resolve().parent
AGENTS_DIR:   Path = SYNTHOS_HOME / "agents"
UTILS_DIR:    Path = SYNTHOS_HOME / "utils"
DATA_DIR:     Path = SYNTHOS_HOME / "data"
LOG_DIR:      Path = SYNTHOS_HOME / "logs"
BACKUP_DIR:   Path = DATA_DIR / "backup"
CONFIG_DIR:   Path = SYNTHOS_HOME / "config"
ENV_PATH:     Path = SYNTHOS_HOME / "company.env"
DB_PATH:      Path = DATA_DIR / "company.db"
SENTINEL_PATH: Path = SYNTHOS_HOME / ".install_complete"
PROGRESS_PATH: Path = SYNTHOS_HOME / ".install_progress.json"

# Bootstrap common helpers
_COMMON_DIR = SYNTHOS_HOME / "installers" / "common"
if str(_COMMON_DIR.parent.parent) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR.parent.parent))

from installers.common.preflight import run_preflight
from installers.common.progress import ProgressManager
from installers.common.env_writer import write_env, build_company_env

# ── CONSTANTS ─────────────────────────────────────────────────────────────────

SYNTHOS_VERSION = "1.1"

REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "python-dotenv",
    "anthropic",
    "sendgrid",
]

REQUIRED_AGENT_FILES = [
    "patches.py",
    "blueprint.py",
    "sentinel.py",
    "fidget.py",
    "librarian.py",
    "scoop.py",
    "vault.py",
    "timekeeper.py",
]

REQUIRED_UTIL_FILES = [
    "db_helpers.py",
]

# Files that must never be overwritten on rerun
PROTECTED_PATHS = [
    ENV_PATH,
    DB_PATH,
    BACKUP_DIR,
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
log = logging.getLogger("install_company")


def _log(message: str, level: str = "info") -> None:
    getattr(log, level, log.info)(message)


# ── DIRECTORY CREATION ────────────────────────────────────────────────────────

def create_directories() -> None:
    """Create all required company node directories."""
    dirs = [
        AGENTS_DIR,
        UTILS_DIR,
        DATA_DIR,
        BACKUP_DIR,
        LOG_DIR,
        CONFIG_DIR,
        SYNTHOS_HOME / "installers" / "common",
    ]
    for d in dirs:
        if d in PROTECTED_PATHS and d.exists():
            _log(f"  → Skipping protected dir (exists): {d.relative_to(SYNTHOS_HOME)}")
            continue
        d.mkdir(parents=True, exist_ok=True)
        _log(f"  ✓ {d.relative_to(SYNTHOS_HOME)}")


# ── PACKAGE INSTALLATION ──────────────────────────────────────────────────────

def install_packages() -> bool:
    """Install required packages. Returns True if all succeeded or already present."""
    import platform
    is_linux = platform.system() == "Linux"
    all_ok = True

    for pkg in REQUIRED_PACKAGES:
        _log(f"  Installing {pkg}...")
        cmd = [sys.executable, "-m", "pip", "install", pkg, "-q"]
        if is_linux:
            cmd.append("--break-system-packages")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                _log(f"  ✓ {pkg}")
            else:
                combined = (result.stdout + result.stderr).lower()
                if "already satisfied" in combined or "already installed" in combined:
                    _log(f"  ✓ {pkg} (already installed)")
                else:
                    _log(f"  ✗ {pkg}: {result.stderr[:200]}", "error")
                    all_ok = False
        except subprocess.TimeoutExpired:
            _log(f"  ✗ {pkg}: timed out after 120s", "error")
            all_ok = False
        except Exception as exc:
            _log(f"  ✗ {pkg}: {exc}", "error")
            all_ok = False

    return all_ok


# ── DATABASE INITIALIZATION ───────────────────────────────────────────────────

def init_company_db() -> bool:
    """
    Initialize company.db schema by importing db_helpers.py.
    If company.db already exists, schema bootstrap is idempotent (safe).
    Never overwrites existing data.
    """
    db_helpers_path = UTILS_DIR / "db_helpers.py"
    if not db_helpers_path.exists():
        _log("  ✗ utils/db_helpers.py not found — cannot initialize DB", "error")
        return False

    try:
        # Set environment so db_helpers resolves paths correctly
        os.environ["SYNTHOS_HOME"] = str(SYNTHOS_HOME)

        spec = importlib.util.spec_from_file_location("db_helpers", db_helpers_path)
        db_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(db_mod)

        # bootstrap_schema() is idempotent — safe on existing DB
        if hasattr(db_mod, "bootstrap_schema"):
            db_mod.bootstrap_schema()
            _log("  ✓ company.db schema bootstrapped")
        elif hasattr(db_mod, "DB"):
            db_mod.DB()  # DB.__init__ calls bootstrap_schema
            _log("  ✓ company.db initialized via DB()")
        else:
            _log("  ⚠ db_helpers.py loaded but no bootstrap entry point found", "warning")
            _log("    company.db will be initialized on first agent run")

        return True
    except Exception as exc:
        _log(f"  ✗ DB initialization failed: {exc}", "error")
        log.exception("DB init exception")
        return False


# ── CONFIG FILE STUBS ─────────────────────────────────────────────────────────

def write_config_stubs() -> None:
    """Write default config stubs if not already present."""
    allowed_ips = CONFIG_DIR / "allowed_ips.json"
    if not allowed_ips.exists():
        allowed_ips.write_text(json.dumps({
            "allowed_ips": [],
            "mode": "testing",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "note": "Add your home IP and dev Pi IP before enabling IP allowlisting",
        }, indent=2))
        _log("  ✓ config/allowed_ips.json (stub)")

    agent_policies = CONFIG_DIR / "agent_policies.json"
    if not agent_policies.exists():
        agent_policies.write_text(json.dumps({
            "market_hours": ["sentinel", "scoop", "vault"],
            "off_hours":    ["patches", "blueprint", "librarian", "fidget"],
            "always_on":    ["timekeeper"],
            "note": "Agents listed under market_hours run with elevated priority during trading hours",
        }, indent=2))
        _log("  ✓ config/agent_policies.json (stub)")

    market_calendar = CONFIG_DIR / "market_calendar.json"
    if not market_calendar.exists():
        market_calendar.write_text(json.dumps({
            "timezone":    "US/Eastern",
            "open":        "09:30",
            "close":       "16:00",
            "trading_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        }, indent=2))
        _log("  ✓ config/market_calendar.json (stub)")


# ── CRON REGISTRATION ─────────────────────────────────────────────────────────

def register_cron() -> bool:
    """
    Write cron entries for company agents.
    All paths are resolved from SYNTHOS_HOME — no hardcoding.
    Company agents run on off-hours schedule per agent_policies.json.
    """
    if not shutil.which("crontab"):
        _log("  ⚠ crontab not found — add cron entries manually", "warning")
        return False

    py = sys.executable

    def agent(name: str) -> str:
        return str(AGENTS_DIR / name)

    def logf(name: str) -> str:
        stem = name.replace(".py", "")
        return str(LOG_DIR / f"{stem}.log")

    new_entries = "\n".join([
        f"# SYNTHOS COMPANY — generated by install_company.py at {datetime.now().isoformat()}",

        # Always-on: Timekeeper starts at boot
        f"@reboot sleep 30 && {py} {agent('timekeeper.py')} >> {logf('timekeeper')} 2>&1 &",

        # Sentinel — runs every 15 minutes during market hours
        f"*/15 9-16 * * 1-5  {py} {agent('sentinel.py')} >> {logf('sentinel')} 2>&1",

        # Scoop — runs every 10 minutes (delivery queue drain)
        f"*/10 * * * *       {py} {agent('scoop.py')} >> {logf('scoop')} 2>&1",

        # Vault — hourly key compliance check
        f"0 * * * *          {py} {agent('vault.py')} >> {logf('vault')} 2>&1",

        # Patches (Bug Finder) — daily at 6pm ET (off-hours)
        f"0 18 * * 1-5       {py} {agent('patches.py')} >> {logf('patches')} 2>&1",

        # Blueprint (Engineer) — weekly, Friday 6pm ET
        f"0 18 * * 5         {py} {agent('blueprint.py')} >> {logf('blueprint')} 2>&1",

        # Fidget (token monitor) — daily at 8am ET
        f"0 8 * * *          {py} {agent('fidget.py')} >> {logf('fidget')} 2>&1",

        # Librarian — weekly Sunday 9am ET
        f"0 9 * * 0          {py} {agent('librarian.py')} >> {logf('librarian')} 2>&1",

        "",
    ])

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing_content = existing.stdout if existing.returncode == 0 else ""

        clean_lines = [
            line for line in existing_content.splitlines()
            if "SYNTHOS COMPANY" not in line
            and str(SYNTHOS_HOME) not in line
        ]
        clean_existing = "\n".join(clean_lines).strip()
        final_crontab = (clean_existing + "\n" + new_entries).strip() + "\n"

        proc = subprocess.run(
            ["crontab", "-"], input=final_crontab, text=True, capture_output=True
        )
        if proc.returncode == 0:
            _log("  ✓ Cron schedule registered")
            return True
        else:
            _log(f"  ✗ Cron error: {proc.stderr[:200]}", "error")
            return False
    except Exception as exc:
        _log(f"  ✗ Cron setup failed: {exc}", "error")
        return False


# ── TIMEZONE ──────────────────────────────────────────────────────────────────

def set_timezone() -> None:
    import platform
    if platform.system() != "Linux":
        _log("  → Timezone: skipped (not Linux)")
        return
    try:
        result = subprocess.run(
            ["sudo", "timedatectl", "set-timezone", "America/New_York"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            _log("  ✓ Timezone set to America/New_York (ET)")
        else:
            _log("  ⚠ Could not set timezone — run: sudo timedatectl set-timezone America/New_York",
                 "warning")
    except Exception as exc:
        _log(f"  ⚠ Timezone: {exc}", "warning")


# ── VERIFICATION ──────────────────────────────────────────────────────────────

def verify_installation() -> tuple[bool, list[str]]:
    """
    Run all post-install verification checks.
    Returns (passed: bool, failed_checks: list[str]).
    """
    failures: list[str] = []

    # 1. company.env present with required keys
    if not ENV_PATH.exists():
        failures.append("company.env not found")
    else:
        required_keys = [
            "COMPANY_MODE",
            "SENDGRID_API_KEY",
            "SENDGRID_FROM",
            "OPERATOR_EMAIL",
            "KEY_SIGNING_SECRET",
            "DATABASE_PATH",
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
                failures.append(f"company.env missing keys: {', '.join(missing)}")

            # COMPANY_MODE must be true
            if "COMPANY_MODE" in present:
                for line in env_text.splitlines():
                    if line.strip().startswith("COMPANY_MODE="):
                        val = line.split("=", 1)[1].strip().lower()
                        if val != "true":
                            failures.append(f"COMPANY_MODE must be 'true', got '{val}'")
        except OSError as exc:
            failures.append(f"company.env unreadable: {exc}")

    # 2. company.db exists
    if not DB_PATH.exists():
        failures.append("data/company.db not found")

    # 3. Required agent files present
    missing_agents = [f for f in REQUIRED_AGENT_FILES if not (AGENTS_DIR / f).exists()]
    if missing_agents:
        failures.append(f"Missing agent files: {', '.join(missing_agents)}")

    # 4. Required util files present
    missing_utils = [f for f in REQUIRED_UTIL_FILES if not (UTILS_DIR / f).exists()]
    if missing_utils:
        failures.append(f"Missing util files: {', '.join(missing_utils)}")

    # 5. Required packages importable
    import importlib
    unimportable = []
    pkg_map = {
        "flask": "flask",
        "requests": "requests",
        "python-dotenv": "dotenv",
        "anthropic": "anthropic",
        "sendgrid": "sendgrid",
    }
    for pip_name, import_name in pkg_map.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            unimportable.append(pip_name)
    if unimportable:
        failures.append(f"Packages not importable: {', '.join(unimportable)}")

    # 6. Cron entries written
    if shutil.which("crontab"):
        try:
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if "SYNTHOS COMPANY" not in result.stdout:
                failures.append("Company cron entries not found in crontab")
        except Exception as exc:
            failures.append(f"Could not read crontab: {exc}")

    passed = len(failures) == 0
    return passed, failures


# ── SENTINEL ──────────────────────────────────────────────────────────────────

def write_sentinel() -> None:
    content = {
        "version":      SYNTHOS_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "synthos_home": str(SYNTHOS_HOME),
        "node_type":    "company",
    }
    SENTINEL_PATH.write_text(json.dumps(content, indent=2), encoding="utf-8")
    log.info("Sentinel written: %s", SENTINEL_PATH)


# ── CONFIG COLLECTION (CLI) ───────────────────────────────────────────────────

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt operator for a value. Uses getpass for secret fields."""
    if default:
        display = f"{label} [{default}]: "
    else:
        display = f"{label}: "

    if secret:
        val = getpass(display).strip()
    else:
        val = input(display).strip()

    return val if val else default


def collect_config() -> dict[str, str]:
    """
    Collect company node configuration via CLI prompts.
    No secrets are stored in progress file.
    Returns config dict.
    """
    print()
    print("─" * 50)
    print("  Company Node Configuration")
    print("─" * 50)
    print("  Press Enter to accept defaults shown in [brackets].")
    print()

    config: dict[str, str] = {}

    print("  — SendGrid (Scoop delivery) —")
    config["sendgrid_key"]   = _prompt("  SendGrid API Key", secret=True)
    config["sendgrid_from"]  = _prompt("  From address (verified sender)", "alerts@synthos.com")
    config["operator_email"] = _prompt("  Operator email (internal alert recipient)")

    print()
    print("  — License Key Infrastructure —")
    config["key_signing_secret"] = _prompt(
        "  KEY_SIGNING_SECRET (HMAC seed — never printed after this)", secret=True
    )
    config["vault_url"] = _prompt("  Vault URL (optional — self-reference for retail Pis)", "")

    print()
    print("  — Service Ports —")
    config["command_port"]   = _prompt("  Command interface port", "5002")
    config["installer_port"] = _prompt("  Installer delivery port", "5003")
    config["heartbeat_port"] = _prompt("  Heartbeat receiver port", "5004")

    print()
    print("  — Scheduler —")
    config["scheduler_timeout"]   = _prompt("  Scheduler timeout (seconds)", "120")
    config["market_hours_start"]  = _prompt("  Market open (HHMM)", "0930")
    config["market_hours_end"]    = _prompt("  Market close (HHMM)", "1600")
    config["market_timezone"]     = _prompt("  Market timezone", "US/Eastern")

    print()
    print("  — GitHub (optional) —")
    config["github_token"] = _prompt("  GitHub token (for pushing customer forks)", secret=True)
    config["github_repo"]  = _prompt("  GitHub repo (e.g. yourorg/synthos)", "")

    print()
    return config


# ── MAIN INSTALL FLOW ─────────────────────────────────────────────────────────

def run_full_install(config: dict) -> bool:
    """
    Execute INSTALLING and VERIFYING phases.
    Returns True on full success (COMPLETE), False on failure (DEGRADED).
    """
    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()

    print()
    print("── INSTALLING ──────────────────────────────────────")
    progress.transition("INSTALLING")

    # 1. Directories
    print("Creating directories...")
    create_directories()

    # 2. Write env
    print("Writing company.env...")
    try:
        env_content = build_company_env(config, str(DB_PATH))
        write_env(ENV_PATH, env_content)
        _log("  ✓ company.env written")
    except Exception as exc:
        _log(f"  ✗ Failed to write company.env: {exc}", "error")
        progress.transition("DEGRADED")
        progress.set("degraded_reason", f"env write failed: {exc}")
        return False

    # 3. Install packages
    print("Installing Python packages...")
    if not install_packages():
        _log("  ⚠ Some packages may have failed — check logs", "warning")

    # 4. Init DB
    print("Initializing company database...")
    if not init_company_db():
        _log("  ✗ DB initialization failed", "error")
        progress.transition("DEGRADED")
        progress.set("degraded_reason", "DB init failed")
        return False

    # 5. Config stubs
    print("Writing config stubs...")
    write_config_stubs()

    # 6. Cron
    print("Registering cron schedule...")
    register_cron()

    # 7. Timezone
    print("Setting timezone...")
    set_timezone()

    progress.set("install_complete", True)

    # 8. Verify
    print()
    print("── VERIFYING ───────────────────────────────────────")
    progress.transition("VERIFYING")

    passed, failures = verify_installation()

    if passed:
        print()
        print("── COMPLETE ────────────────────────────────────────")
        progress.transition("COMPLETE")
        write_sentinel()
        _log("✓ Company node installation complete.")
        _log(f"  Sentinel: {SENTINEL_PATH}")
        print()
        print("  Next steps:")
        print(f"  1. Review config/allowed_ips.json and add your IP addresses")
        print(f"  2. Run: python3 agents/seed_backlog.py  (bootstrap agent queue)")
        print(f"  3. Reboot to start agent cron schedule")
        print(f"  4. Set up Cloudflare tunnel: bash setup_tunnel.sh")
        print()
        return True
    else:
        print()
        print("── DEGRADED ────────────────────────────────────────")
        progress.transition("DEGRADED")
        for f in failures:
            _log(f"  ✗ {f}", "error")
        progress.set("degraded_reason", failures)
        _log("Installation DEGRADED. Check logs/install.log and run --repair.", "error")
        return False


# ── STATUS CHECK ──────────────────────────────────────────────────────────────

def print_status() -> None:
    print()
    print("=" * 50)
    print("  SYNTHOS COMPANY — Install Status")
    print("=" * 50)
    print(f"  SYNTHOS_HOME : {SYNTHOS_HOME}")

    if SENTINEL_PATH.exists():
        sentinel = json.loads(SENTINEL_PATH.read_text())
        print(f"  State        : COMPLETE")
        print(f"  Installed at : {sentinel.get('installed_at','?')}")
    elif PROGRESS_PATH.exists():
        prog = json.loads(PROGRESS_PATH.read_text())
        print(f"  State        : {prog.get('state','UNKNOWN')}")
        if "degraded_reason" in prog:
            print(f"  Degraded     : {prog['degraded_reason']}")
    else:
        print(f"  State        : UNINITIALIZED")

    print(f"  company.env  : {ENV_PATH.exists()}")
    print(f"  company.db   : {DB_PATH.exists()}")
    print()


# ── REPAIR MODE ───────────────────────────────────────────────────────────────

def repair_mode() -> int:
    """Re-run INSTALLING + VERIFYING without collecting config again."""
    print()
    print("=" * 50)
    print("  SYNTHOS COMPANY — Repair Mode")
    print("=" * 50)

    if not ENV_PATH.exists():
        print("  ✗ company.env not found — cannot repair without configuration.")
        print("  Run install_company.py (without --repair) to reconfigure.")
        return 1

    config: dict[str, str] = {}
    try:
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip()
    except OSError as exc:
        print(f"  ✗ Could not read company.env: {exc}")
        return 1

    print("  Running repair: INSTALLING → VERIFYING")

    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()
    if progress.state in ("DEGRADED", "COMPLETE"):
        progress.transition("INSTALLING")

    success = run_full_install(config)
    return 0 if success else 2


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthos Company Node Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repair", action="store_true",
                        help="Re-run install/verify without recollecting config")
    parser.add_argument("--status", action="store_true",
                        help="Print current install state and exit")
    args = parser.parse_args()

    log.info("=" * 55)
    log.info("SYNTHOS v%s — COMPANY INSTALLER", SYNTHOS_VERSION)
    log.info("SYNTHOS_HOME: %s", SYNTHOS_HOME)
    log.info("=" * 55)

    if args.status:
        print_status()
        return 0

    if args.repair:
        return repair_mode()

    # Already complete
    if SENTINEL_PATH.exists():
        print()
        print("=" * 50)
        print("  Synthos company node is already installed.")
        sentinel = json.loads(SENTINEL_PATH.read_text())
        print(f"  Installed at: {sentinel.get('installed_at','?')}")
        print()
        print("  To repair:   python3 install_company.py --repair")
        print("  To check:    python3 install_company.py --status")
        print()
        return 0

    print()
    print("=" * 55)
    print(f"  SYNTHOS v{SYNTHOS_VERSION} — COMPANY NODE INSTALLER")
    print(f"  SYNTHOS_HOME: {SYNTHOS_HOME}")
    print("=" * 55)

    # Preflight
    preflight = run_preflight()
    print(preflight.report())
    print()

    if not preflight.passed:
        log.error("Preflight failed — cannot continue")
        return 1

    progress = ProgressManager(SYNTHOS_HOME)
    progress.load()

    # If config was already collected (prior partial run), skip collection
    if progress.get("config_complete") and not ENV_PATH.exists():
        log.info("Resuming from prior session (config already collected)")
        # We cannot recover secrets from progress file (intentionally not stored)
        # Operator must re-enter secrets on resume after interruption
        print("  Prior session detected but secrets cannot be recovered from progress file.")
        print("  Re-entering config collection.\n")

    progress.transition("COLLECTING")

    try:
        config = collect_config()
    except (KeyboardInterrupt, EOFError):
        print("\n  Installer cancelled by operator.")
        log.info("Installer cancelled by operator")
        return 3

    # Confirm before proceeding
    print()
    print("─" * 50)
    print("  Configuration collected. Review:")
    print(f"  Operator email : {config.get('operator_email','—')}")
    print(f"  SendGrid from  : {config.get('sendgrid_from','—')}")
    print(f"  Command port   : {config.get('command_port','—')}")
    print(f"  Heartbeat port : {config.get('heartbeat_port','—')}")
    print(f"  KEY_SIGNING_SECRET : {'set' if config.get('key_signing_secret') else '⚠ NOT SET'}")
    print("─" * 50)

    try:
        confirm = input("  Proceed with installation? (yes/no): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.")
        return 3

    if confirm != "yes":
        print("  Installation cancelled.")
        log.info("Operator declined to proceed")
        return 3

    progress.set("config_complete", True)

    success = run_full_install(config)
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
