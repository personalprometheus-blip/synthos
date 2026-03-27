"""
synthos_paths.py — Centralized Path Authority
Synthos Company Pi | utils/synthos_paths.py

Single source of truth for all filesystem paths used by company Pi agents.
No agent should hardcode /home/pi/ or compute BASE_DIR independently.

Resolution order for BASE_DIR:
  1. SYNTHOS_BASE_DIR env var  (explicit override — CI, testing, alt installs)
  2. File-relative resolution  (Path(__file__).resolve().parent.parent)
     Works for any username, any install directory.

Resolution order for RETAIL_DIR:
  1. SYNTHOS_RETAIL_DIR env var (explicit override)
  2. BASE_DIR.parent / "synthos" (standard sibling layout)
  Company agents log a warning if RETAIL_DIR doesn't exist — they can still
  run without it (Sentinel, Timekeeper, Scoop don't need the retail tree).

Exports:
  BASE_DIR     — synthos-company root
  DATA_DIR     — synthos-company/data/
  LOGS_DIR     — synthos-company/logs/
  CONFIG_DIR   — synthos-company/config/
  AGENTS_DIR   — synthos-company/agents/
  UTILS_DIR    — synthos-company/utils/
  DB_PATH      — synthos-company/data/company.db
  ENV_PATH     — synthos-company/.env
  RETAIL_DIR   — sibling synthos/ root (may not exist on all deployments)
  RETAIL_CORE  — synthos/core/ (retail Pi agent files)

Usage:
  from utils.synthos_paths import BASE_DIR, DATA_DIR, LOGS_DIR, DB_PATH

Validation:
  python3 utils/synthos_paths.py   — prints all resolved paths, flags missing dirs
"""

import os
import sys
import logging
from pathlib import Path

log = logging.getLogger("synthos_paths")

# ── RESOLUTION ────────────────────────────────────────────────────────────────

def _resolve_base_dir() -> Path:
    """
    Resolve the synthos-company root directory.

    Priority:
      1. SYNTHOS_BASE_DIR environment variable (absolute path, must exist)
      2. File-relative: this file is at utils/synthos_paths.py,
         so parent.parent is synthos-company/
    """
    env_override = os.environ.get("SYNTHOS_BASE_DIR", "").strip()
    if env_override:
        p = Path(env_override).resolve()
        if not p.exists():
            raise RuntimeError(
                f"SYNTHOS_BASE_DIR is set to '{env_override}' but that path does not exist. "
                f"Unset SYNTHOS_BASE_DIR or create the directory."
            )
        return p

    # File-relative: utils/synthos_paths.py → utils/ → synthos-company/
    resolved = Path(__file__).resolve().parent.parent
    return resolved


def _resolve_retail_dir(base_dir: Path) -> Path | None:
    """
    Resolve the retail Pi source directory (synthos/).

    Priority:
      1. SYNTHOS_RETAIL_DIR environment variable
      2. Sibling of BASE_DIR: base_dir.parent / "synthos"

    Returns None and logs a warning if not found.
    Company agents that don't need retail files can safely ignore None.
    """
    env_override = os.environ.get("SYNTHOS_RETAIL_DIR", "").strip()
    if env_override:
        p = Path(env_override).resolve()
        if not p.exists():
            log.warning(
                f"SYNTHOS_RETAIL_DIR is set to '{env_override}' but that path does not exist."
            )
            return p   # return the configured path even if missing — let callers decide
        return p

    candidate = base_dir.parent / "synthos"
    if not candidate.exists():
        log.warning(
            f"Retail directory not found at expected location: {candidate}. "
            f"Set SYNTHOS_RETAIL_DIR env var if installed elsewhere. "
            f"Company agents that don't access retail files can ignore this."
        )
    return candidate   # return candidate regardless — caller decides if None matters


# ── PATH CONSTANTS ────────────────────────────────────────────────────────────
#
# Resolved once at module load. All agents import these constants.
# Do not modify after import — treat as read-only.

try:
    BASE_DIR = _resolve_base_dir()
except RuntimeError as _e:
    # Re-raise with clear context so agents fail fast with useful error
    raise RuntimeError(
        f"synthos_paths: Cannot resolve BASE_DIR — {_e}"
    ) from _e

DATA_DIR   = BASE_DIR / "data"
LOGS_DIR   = BASE_DIR / "logs"
CONFIG_DIR = BASE_DIR / "config"
AGENTS_DIR = BASE_DIR / "agents"
UTILS_DIR  = BASE_DIR / "utils"

DB_PATH  = DATA_DIR / "company.db"
ENV_PATH = BASE_DIR / ".env"

# Retail Pi paths — may be None-equivalent if not installed
RETAIL_DIR  = _resolve_retail_dir(BASE_DIR)
RETAIL_CORE = RETAIL_DIR / "core" if RETAIL_DIR else None


# ── DERIVED PATHS (commonly used by agents) ───────────────────────────────────
#
# These follow directly from the constants above.
# Listed here for discoverability — agents can also construct them directly.

SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
SCOOP_TRIGGER    = DATA_DIR / "scoop_trigger.json"
MORNING_REPORT   = DATA_DIR / "morning_report.json"
MANIFEST_FILE    = CONFIG_DIR / "package_manifest.json"
BACKUP_STAGING   = DATA_DIR / "backup_staging"
BACKUP_LOG_FILE  = DATA_DIR / "backup_log.json"


# ── DIRECTORY BOOTSTRAP ───────────────────────────────────────────────────────

def ensure_directories() -> None:
    """
    Create required directories if they don't exist.
    Safe to call on every agent startup.
    Does not create RETAIL_DIR — that belongs to the retail installer.
    """
    for d in (DATA_DIR, LOGS_DIR, CONFIG_DIR, AGENTS_DIR, UTILS_DIR, BACKUP_STAGING):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(f"Could not create directory {d}: {e}")


# ── VALIDATION ────────────────────────────────────────────────────────────────

def validate(verbose: bool = True) -> bool:
    """
    Validate all resolved paths. Returns True if critical paths are present.
    Logs warnings for optional paths that are missing.

    Called by: python3 utils/synthos_paths.py (CLI validation)
    Also callable by agents during startup for self-diagnostics.
    """
    ok = True

    critical = {
        "BASE_DIR":  BASE_DIR,
        "DATA_DIR":  DATA_DIR,
        "LOGS_DIR":  LOGS_DIR,
        "CONFIG_DIR": CONFIG_DIR,
    }

    optional = {
        "DB_PATH":      DB_PATH,
        "ENV_PATH":     ENV_PATH,
        "RETAIL_DIR":   RETAIL_DIR,
        "RETAIL_CORE":  RETAIL_CORE,
        "MANIFEST_FILE": MANIFEST_FILE,
    }

    if verbose:
        print(f"\n{'─' * 56}")
        print(f"SYNTHOS PATH RESOLUTION")
        print(f"{'─' * 56}")
        print(f"  SYNTHOS_BASE_DIR env : {os.environ.get('SYNTHOS_BASE_DIR', '(not set)')}")
        print(f"  SYNTHOS_RETAIL_DIR env: {os.environ.get('SYNTHOS_RETAIL_DIR', '(not set)')}")
        print(f"  Resolution source    : {Path(__file__).resolve()}")
        print(f"{'─' * 56}")
        print(f"  CRITICAL PATHS:")

    for name, path in critical.items():
        exists = path.exists() if path else False
        if not exists:
            ok = False
            if verbose:
                print(f"    ✗ {name:20} MISSING  → {path}")
            else:
                log.error(f"Critical path missing: {name} = {path}")
        else:
            if verbose:
                print(f"    ✓ {name:20} {path}")

    if verbose:
        print(f"  OPTIONAL PATHS:")

    for name, path in optional.items():
        if path is None:
            if verbose:
                print(f"    — {name:20} None (not configured)")
            continue
        exists = path.exists()
        icon   = "✓" if exists else "—"
        label  = "" if exists else " (not yet created)"
        if verbose:
            print(f"    {icon} {name:20} {path}{label}")

    if verbose:
        print(f"{'─' * 56}")
        print(f"  Status: {'OK' if ok else 'MISSING CRITICAL PATHS — check install'}\n")

    return ok


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Configure basic logging for standalone run
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s"
    )

    ok = validate(verbose=True)
    sys.exit(0 if ok else 1)
