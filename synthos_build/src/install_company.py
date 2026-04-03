"""
install_company.py — Synthos Company Node Installer
Synthos · v3.0

First-time setup and safe rerun/repair for the Company Pi (Pi 4B).
Runs a local web wizard on port 8081, writes user/.env, registers
cron jobs to start company_server.py and scoop.py on boot.

USAGE:
    python3 install_company.py             # first install or resume
    python3 install_company.py --repair    # re-run INSTALLING + VERIFYING
    python3 install_company.py --status    # print current install state

STATE MACHINE:
    UNINITIALIZED → PREFLIGHT → COLLECTING → INSTALLING → VERIFYING → COMPLETE
                                                         ↘ DEGRADED (on failure)

EXIT CODES:
    0 — success or already complete
    1 — preflight failure
    2 — install or verification failure (DEGRADED)
    3 — operator cancelled

REQUIRED FILES (src/):
    company_server.py   — Flask API + ops dashboard (port 5010)
    scoop.py            — Queue drain daemon (Resend dispatch)
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
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── PATH RESOLUTION ───────────────────────────────────────────────────────────
SYNTHOS_HOME:  Path = Path(__file__).resolve().parent.parent
CORE_DIR:      Path = SYNTHOS_HOME / "src"
USER_DIR:      Path = SYNTHOS_HOME / "user"
DATA_DIR:      Path = SYNTHOS_HOME / "data"
LOG_DIR:       Path = SYNTHOS_HOME / "logs"
BACKUP_DIR:    Path = DATA_DIR / "backup"
ENV_PATH:      Path = USER_DIR / ".env"
DB_PATH:       Path = DATA_DIR / "company.db"
SENTINEL_PATH: Path = SYNTHOS_HOME / ".company_install_complete"
PROGRESS_PATH: Path = SYNTHOS_HOME / ".company_install_progress.json"

_COMMON_DIR = SYNTHOS_HOME / "installers" / "common"
if str(_COMMON_DIR.parent.parent) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR.parent.parent))

from installers.common.env_writer import write_env, build_company_env

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
SYNTHOS_VERSION = "3.0"
INSTALLER_PORT  = 8081

REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "python-dotenv",
]

REQUIRED_CORE_FILES = [
    "company_server.py",
    "scoop.py",
]

PROTECTED_PATHS = [
    ENV_PATH,
    BACKUP_DIR,
    DB_PATH,
]

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "install_company.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("install_company")

# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
_state: dict[str, Any] = {
    "phase":           "UNINITIALIZED",
    "config":          {},
    "install_started": False,
    "install_done":    False,
    "install_error":   None,
    "log_lines":       [],
}


def _log_ui(message: str, level: str = "info") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    _state["log_lines"].append({"ts": ts, "msg": message, "level": level})
    getattr(log, level, log.info)(message)


# ── PROGRESS ──────────────────────────────────────────────────────────────────
def _save_progress(phase: str, config: dict | None = None) -> None:
    _state["phase"] = phase
    try:
        payload: dict = {"phase": phase, "ts": datetime.now(timezone.utc).isoformat()}
        if config:
            payload["config_keys"] = list(config.keys())
        PROGRESS_PATH.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        log.warning(f"Could not save progress: {e}")


def _load_progress() -> dict:
    try:
        if PROGRESS_PATH.exists():
            return json.loads(PROGRESS_PATH.read_text())
    except Exception:
        pass
    return {"phase": "UNINITIALIZED"}


# ── PREFLIGHT ─────────────────────────────────────────────────────────────────
def run_preflight() -> list[str]:
    """Return list of blocking issues. Empty list = all clear."""
    issues = []

    # Python version
    if sys.version_info < (3, 9):
        issues.append(f"Python 3.9+ required (found {sys.version})")

    # Required source files
    for fname in REQUIRED_CORE_FILES:
        fpath = CORE_DIR / fname
        if not fpath.exists():
            issues.append(f"Missing required file: src/{fname}")

    # Disk space — need at least 200 MB
    try:
        usage = shutil.disk_usage(SYNTHOS_HOME)
        free_mb = usage.free // (1024 * 1024)
        if free_mb < 200:
            issues.append(f"Low disk space: {free_mb}MB free (need 200MB)")
    except Exception as e:
        log.warning(f"Disk check failed: {e}")

    return issues


# ── DIRECTORY CREATION ────────────────────────────────────────────────────────
def create_directories() -> None:
    dirs = [USER_DIR, DATA_DIR, BACKUP_DIR, LOG_DIR]
    for d in dirs:
        if d in PROTECTED_PATHS and d.exists():
            _log_ui(f"  → Skipping protected dir (exists): {d.relative_to(SYNTHOS_HOME)}")
            continue
        d.mkdir(parents=True, exist_ok=True)
        _log_ui(f"  ✓ {d.relative_to(SYNTHOS_HOME)}")


# ── PACKAGE INSTALLATION ──────────────────────────────────────────────────────
def install_packages() -> bool:
    import platform
    is_linux = platform.system() == "Linux"
    all_ok = True
    for pkg in REQUIRED_PACKAGES:
        _log_ui(f"  Installing {pkg}…")
        cmd = [sys.executable, "-m", "pip", "install", pkg, "-q"]
        if is_linux:
            cmd.append("--break-system-packages")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                _log_ui(f"  ✓ {pkg}")
            else:
                combined = (result.stdout + result.stderr).lower()
                if "already satisfied" in combined or "already installed" in combined:
                    _log_ui(f"  ✓ {pkg} (already installed)")
                else:
                    _log_ui(f"  ✗ {pkg}: {result.stderr[:200]}", "error")
                    all_ok = False
        except subprocess.TimeoutExpired:
            _log_ui(f"  ✗ {pkg}: timed out", "error")
            all_ok = False
        except Exception as exc:
            _log_ui(f"  ✗ {pkg}: {exc}", "error")
            all_ok = False
    return all_ok


# ── DATABASE INIT ─────────────────────────────────────────────────────────────
def bootstrap_database(db_path: Path) -> bool:
    """
    Call init_db() from company_server.py to create company.db schema.
    Safe to call on existing DB — init_db() is idempotent.
    """
    server_module = CORE_DIR / "company_server.py"
    if not server_module.exists():
        _log_ui("  ✗ src/company_server.py not found", "error")
        return False
    try:
        import importlib.util
        os.environ["COMPANY_DB_PATH"] = str(db_path)
        spec = importlib.util.spec_from_file_location("company_server", server_module)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.init_db()
        _log_ui(f"  ✓ company.db schema initialized: {db_path}")
        return True
    except Exception as exc:
        _log_ui(f"  ✗ DB init failed: {exc}", "error")
        log.exception("DB init exception")
        return False


# ── CRON REGISTRATION ─────────────────────────────────────────────────────────
def register_cron(config: dict) -> bool:
    """
    Register @reboot cron entries to start company_server.py and scoop.py.
    Removes any previous Company Node Synthos entries before writing.
    """
    if not shutil.which("crontab"):
        _log_ui("  ⚠ crontab not found — add cron entries manually", "warning")
        return False

    server_script = str(CORE_DIR / "company_server.py")
    scoop_script  = str(CORE_DIR / "scoop.py")
    server_log    = str(LOG_DIR / "company_server.log")
    scoop_log     = str(LOG_DIR / "scoop.log")

    new_entries = "\n".join([
        f"# SYNTHOS COMPANY NODE — generated by install_company.py at {datetime.now().isoformat()}",
        f"@reboot sleep 30 && {sys.executable} {server_script} >> {server_log} 2>&1 &",
        f"@reboot sleep 45 && {sys.executable} {scoop_script}  >> {scoop_log}  2>&1 &",
        "",
    ])

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing_content = existing.stdout if existing.returncode == 0 else ""

        clean_lines = [
            line for line in existing_content.splitlines()
            if "SYNTHOS COMPANY NODE" not in line
            and "company_server" not in line
            and "scoop.py" not in line
        ]
        clean_existing = "\n".join(clean_lines).strip()
        final_crontab  = (clean_existing + "\n" + new_entries).strip() + "\n"

        proc = subprocess.run(
            ["crontab", "-"], input=final_crontab, text=True, capture_output=True
        )
        if proc.returncode == 0:
            _log_ui("  ✓ Cron schedule registered (company_server + scoop on @reboot)")
            return True
        else:
            _log_ui(f"  ✗ Cron error: {proc.stderr[:200]}", "error")
            return False
    except Exception as exc:
        _log_ui(f"  ✗ Cron registration failed: {exc}", "error")
        return False


# ── VERIFICATION ──────────────────────────────────────────────────────────────
def verify_install(config: dict) -> tuple[bool, list[str]]:
    issues = []

    # .env written
    if not ENV_PATH.exists():
        issues.append(".env file not found")
    else:
        content = ENV_PATH.read_text()
        for key in ("SECRET_TOKEN", "PORT"):
            if key not in content:
                issues.append(f".env missing {key}")
        # RESEND_API_KEY intentionally blank on fresh install — restored from backup

    # company.db exists
    db_path = Path(config.get("company_db_path", str(DB_PATH)))
    if not db_path.exists():
        issues.append(f"company.db not found at {db_path}")

    # Required source files still present
    for fname in REQUIRED_CORE_FILES:
        if not (CORE_DIR / fname).exists():
            issues.append(f"Source file missing: src/{fname}")

    return (len(issues) == 0), issues


# ── INSTALL ORCHESTRATOR ──────────────────────────────────────────────────────
def run_install(config: dict) -> bool:
    _log_ui("=== Company Node Install Starting ===")
    _save_progress("INSTALLING", config)

    _log_ui("Step 1/5 — Creating directories…")
    create_directories()

    _log_ui("Step 2/5 — Installing Python packages…")
    if not install_packages():
        _log_ui("Package install had errors — continuing (some may already be installed)", "warning")

    _log_ui("Step 3/5 — Writing .env…")
    try:
        db_path = config.get("company_db_path") or str(DB_PATH)
        config["company_db_path"] = db_path
        env_content = build_company_env(config)
        write_env(ENV_PATH, env_content)
        _log_ui(f"  ✓ user/.env written (chmod 600)")
    except Exception as exc:
        _log_ui(f"  ✗ Failed to write .env: {exc}", "error")
        _save_progress("DEGRADED")
        return False

    _log_ui("Step 4/5 — Initializing company.db…")
    if not bootstrap_database(Path(db_path)):
        _log_ui("  ⚠ DB init failed — server will retry on first start", "warning")

    _log_ui("Step 5/5 — Registering cron…")
    register_cron(config)

    _log_ui("Verifying installation…")
    ok, issues = verify_install(config)
    if ok:
        SENTINEL_PATH.write_text(
            json.dumps({"installed_at": datetime.now(timezone.utc).isoformat(),
                        "version": SYNTHOS_VERSION})
        )
        _save_progress("COMPLETE")
        _log_ui("=== Company Node Install Complete ✓ ===")
        _log_ui(f"Dashboard: http://0.0.0.0:{config.get('port', 5010)}/console?token=<SECRET_TOKEN>")
        return True
    else:
        for issue in issues:
            _log_ui(f"  ✗ {issue}", "error")
        _save_progress("DEGRADED")
        _log_ui("=== Install completed with issues (DEGRADED) ===", "error")
        return False


# ── WEB WIZARD ────────────────────────────────────────────────────────────────
WIZARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos Company Node Setup</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.14);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.4);--dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;--pink:#ff4b6e;--amber:#ffb347;--purple:#7b61ff;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}

.header{
  border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;gap:12px;
  background:rgba(8,11,18,0.95);
}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;
          letter-spacing:0.15em;color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
       padding:3px 8px;border-radius:99px;
       border:1px solid rgba(123,97,255,0.3);background:rgba(123,97,255,0.1);color:#a78bfa}

.page{max-width:680px;margin:0 auto;padding:32px 20px 60px}

h1{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:6px}
.subtitle{font-size:13px;color:var(--muted);margin-bottom:28px;line-height:1.5}

/* SECTION */
.section{
  border:1px solid var(--border);border-radius:14px;
  background:var(--surface);margin-bottom:16px;overflow:hidden;
}
.section-head{
  padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;
}
.section-num{
  width:22px;height:22px;border-radius:6px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:800;
  background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.2);color:var(--teal);
}
.section-title{font-size:12px;font-weight:700;letter-spacing:0.04em}
.section-sub{font-size:11px;color:var(--muted);margin-left:auto}
.section-body{padding:16px 18px;display:flex;flex-direction:column;gap:12px}

/* FIELD */
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:0.04em;text-transform:uppercase}
.field input,.field select{
  background:var(--surface2);border:1px solid var(--border2);
  color:var(--text);font-family:var(--sans);font-size:13px;
  padding:9px 12px;border-radius:8px;outline:none;
  transition:border-color 0.15s;
}
.field input:focus,.field select:focus{border-color:rgba(0,245,212,0.4)}
.field input::placeholder{color:var(--dim)}
.field .hint{font-size:10px;color:var(--dim);margin-top:1px}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}

/* GENERATED TOKEN */
.gen-row{display:flex;gap:8px}
.gen-row input{flex:1}
.gen-btn{
  padding:9px 14px;border-radius:8px;font-size:11px;font-weight:700;
  border:1px solid rgba(0,245,212,0.3);background:rgba(0,245,212,0.08);
  color:var(--teal);cursor:pointer;font-family:var(--sans);
  transition:all 0.15s;white-space:nowrap;
}
.gen-btn:hover{background:rgba(0,245,212,0.14)}

/* TOGGLE */
.toggle-row{display:flex;align-items:center;gap:10px}
.toggle-row label{font-size:12px;color:var(--text);font-weight:500;cursor:pointer;flex:1}
.toggle{position:relative;width:36px;height:20px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-slider{
  position:absolute;inset:0;border-radius:99px;cursor:pointer;
  background:rgba(255,255,255,0.1);border:1px solid var(--border2);
  transition:all 0.2s;
}
.toggle-slider::after{
  content:'';position:absolute;top:2px;left:2px;
  width:14px;height:14px;border-radius:50%;
  background:rgba(255,255,255,0.4);transition:all 0.2s;
}
.toggle input:checked + .toggle-slider{background:rgba(0,245,212,0.25);border-color:rgba(0,245,212,0.4)}
.toggle input:checked + .toggle-slider::after{transform:translateX(16px);background:var(--teal)}

/* STATUS BANNER */
.preflight-ok{
  padding:12px 16px;border-radius:10px;margin-bottom:20px;
  background:rgba(0,245,212,0.06);border:1px solid rgba(0,245,212,0.2);
  font-size:12px;color:var(--teal);display:flex;align-items:center;gap:8px;
}
.preflight-fail{
  padding:12px 16px;border-radius:10px;margin-bottom:20px;
  background:rgba(255,75,110,0.06);border:1px solid rgba(255,75,110,0.2);
  font-size:12px;color:var(--pink);
}
.preflight-fail ul{margin-top:6px;padding-left:18px}

/* INSTALL BTN */
.install-btn{
  width:100%;padding:14px;border-radius:12px;font-size:14px;font-weight:700;
  letter-spacing:0.04em;border:none;cursor:pointer;font-family:var(--sans);
  background:linear-gradient(135deg,rgba(0,245,212,0.2),rgba(0,245,212,0.08));
  border:1px solid rgba(0,245,212,0.3);color:var(--teal);
  transition:all 0.2s;margin-top:8px;
}
.install-btn:hover{background:linear-gradient(135deg,rgba(0,245,212,0.3),rgba(0,245,212,0.12));
  box-shadow:0 0 20px rgba(0,245,212,0.15)}
.install-btn:disabled{opacity:0.4;cursor:not-allowed}

/* LOG PANEL */
#log-panel{
  border:1px solid var(--border);border-radius:14px;background:var(--surface);
  margin-top:20px;overflow:hidden;display:none;
}
.log-head{padding:12px 16px;border-bottom:1px solid var(--border);
          font-size:10px;font-weight:700;letter-spacing:0.08em;
          text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px}
.log-body{padding:14px 16px;max-height:320px;overflow-y:auto;font-family:var(--mono);font-size:11px;line-height:1.7}
.log-line{margin-bottom:1px}
.log-line.error{color:var(--pink)}
.log-line.warning{color:var(--amber)}
.log-line.info{color:rgba(255,255,255,0.7)}
.log-spin{width:8px;height:8px;border-radius:50%;border:2px solid rgba(0,245,212,0.3);
          border-top-color:var(--teal);animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* COMPLETE PANEL */
#complete-panel{
  padding:24px;border:1px solid rgba(0,245,212,0.2);border-radius:14px;
  background:rgba(0,245,212,0.05);text-align:center;margin-top:20px;display:none;
}
#complete-panel h2{font-size:18px;color:var(--teal);margin-bottom:8px}
#complete-panel p{font-size:13px;color:var(--muted);line-height:1.6}
.complete-detail{
  margin-top:16px;padding:12px 16px;border-radius:10px;
  background:var(--surface2);border:1px solid var(--border);
  font-family:var(--mono);font-size:11px;color:var(--text);text-align:left;
}

/* ERROR PANEL */
#error-panel{
  padding:20px;border:1px solid rgba(255,75,110,0.2);border-radius:14px;
  background:rgba(255,75,110,0.05);margin-top:20px;display:none;
}
#error-panel h3{color:var(--pink);margin-bottom:8px}
#error-panel p{font-size:12px;color:var(--muted)}
</style>
</head>
<body>

<div class="header">
  <span class="wordmark">SYNTHOS</span>
  <span class="badge">Company Node Setup</span>
</div>

<div class="page">
  <h1>Company Node Setup</h1>
  <p class="subtitle">
    This wizard configures the Pi 4B as a Synthos Company Node —
    running <code style="font-family:var(--mono);font-size:11px;color:var(--teal)">company_server.py</code> (Scoop queue API, port 5010)
    and <code style="font-family:var(--mono);font-size:11px;color:var(--teal)">scoop.py</code> (email dispatch daemon).<br><br>
    Estimated setup time: 2–3 minutes.
  </p>

  <div id="preflight-banner"></div>

  <form id="wizard" onsubmit="return false">

    <!-- SECTION 1: Node Identity -->
    <div class="section">
      <div class="section-head">
        <div class="section-num">1</div>
        <div class="section-title">Node Identity</div>
        <div class="section-sub">auth token + port</div>
      </div>
      <div class="section-body">
        <div class="field">
          <label>Secret Token</label>
          <div class="gen-row">
            <input type="text" id="secret_token" name="secret_token"
                   placeholder="shared secret — same on all retail Pis" autocomplete="off">
            <button type="button" class="gen-btn" onclick="genToken()">Generate</button>
          </div>
          <div class="hint">
            Must match SECRET_TOKEN / MONITOR_TOKEN on retail Pis.
            Also set COMPANY_URL=http://&lt;this-pi-ip&gt;:5010 on retail Pis.
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Port</label>
            <input type="number" id="port" name="port" value="5010" min="1024" max="65535">
            <div class="hint">Company server listens on this port</div>
          </div>
          <div class="field">
            <label>DB Path (optional)</label>
            <input type="text" id="company_db_path" name="company_db_path"
                   placeholder="data/company.db">
            <div class="hint">Leave blank for default</div>
          </div>
        </div>
      </div>
    </div>

    <!-- SECTION 2: Ops Contact -->
    <div class="section">
      <div class="section-head">
        <div class="section-num">2</div>
        <div class="section-title">Ops Contact</div>
        <div class="section-sub">where critical alerts are routed</div>
      </div>
      <div class="section-body">
        <div class="field">
          <label>Ops Email</label>
          <input type="email" id="ops_email" name="ops_email" placeholder="ops@yourco.com">
          <div class="hint">Used for operational alerts. Resend API key and alert addresses are restored from backup after install.</div>
        </div>
      </div>
    </div>

    <!-- SECTION 3: Scoop Settings -->
    <div class="section">
      <div class="section-head">
        <div class="section-num">3</div>
        <div class="section-title">Scoop Settings</div>
        <div class="section-sub">queue drain behaviour</div>
      </div>
      <div class="section-body">
        <div class="field-row">
          <div class="field">
            <label>Poll Interval (s)</label>
            <input type="number" id="scoop_poll_s" name="scoop_poll_s" value="5" min="1" max="60">
            <div class="hint">How often Scoop checks for new items</div>
          </div>
          <div class="field">
            <label>Max Attempts</label>
            <input type="number" id="scoop_max_attempts" name="scoop_max_attempts" value="3" min="1" max="10">
            <div class="hint">Before marking item failed</div>
          </div>
        </div>
        <div class="field">
          <label>Retry Delay (s)</label>
          <input type="number" id="scoop_retry_delay_s" name="scoop_retry_delay_s" value="60" min="10" max="3600">
          <div class="hint">Minimum seconds between retry attempts per item</div>
        </div>
        <div class="toggle-row">
          <label for="scoop_dry_run">Dry Run Mode — log dispatch without sending emails</label>
          <label class="toggle">
            <input type="checkbox" id="scoop_dry_run" name="scoop_dry_run">
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <button type="button" class="install-btn" id="install-btn" onclick="startInstall()">
      Install Company Node
    </button>
  </form>

  <div id="log-panel">
    <div class="log-head">
      <div class="log-spin" id="log-spin"></div>
      <span id="log-status">Installing…</span>
    </div>
    <div class="log-body" id="log-body"></div>
  </div>

  <div id="complete-panel">
    <h2>✓ Company Node Ready</h2>
    <p>company_server.py and scoop.py are installed and will start automatically on reboot.</p>
    <div class="complete-detail" id="complete-detail"></div>
    <p style="margin-top:16px;font-size:12px;color:var(--muted)">
      Start now without rebooting:
    </p>
    <div class="complete-detail" id="start-cmds" style="margin-top:8px"></div>
  </div>

  <div id="error-panel">
    <h3>Install encountered issues</h3>
    <p>Check the log above for details. You can retry after fixing the issues.</p>
  </div>
</div>

<script>
// Generate token on load if empty
window.addEventListener('DOMContentLoaded', () => {
  const t = document.getElementById('secret_token');
  if (!t.value) genToken();
  checkPreflight();
});

function genToken() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let s = '';
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  document.getElementById('secret_token').value = s;
}

async function checkPreflight() {
  try {
    const r = await fetch('/preflight');
    const j = await r.json();
    const banner = document.getElementById('preflight-banner');
    if (j.ok) {
      banner.innerHTML = '<div class="preflight-ok">✓ Preflight checks passed — ready to install</div>';
    } else {
      const items = j.issues.map(i => `<li>${i}</li>`).join('');
      banner.innerHTML = `<div class="preflight-fail"><strong>Preflight issues found:</strong><ul>${items}</ul></div>`;
      document.getElementById('install-btn').disabled = true;
    }
  } catch(e) {
    console.error('Preflight check failed:', e);
  }
}

function collect() {
  const f = id => document.getElementById(id)?.value?.trim() || '';
  const chk = id => document.getElementById(id)?.checked || false;
  return {
    secret_token:        f('secret_token'),
    port:                f('port') || '5010',
    company_db_path:     f('company_db_path'),
    ops_email:           f('ops_email'),
    scoop_poll_s:        f('scoop_poll_s') || '5',
    scoop_max_attempts:  f('scoop_max_attempts') || '3',
    scoop_retry_delay_s: f('scoop_retry_delay_s') || '60',
    scoop_dry_run:       chk('scoop_dry_run') ? 'true' : 'false',
  };
}

function validate(config) {
  if (!config.secret_token) return 'Secret Token is required';
  return null;
}

async function startInstall() {
  const config = collect();
  const err = validate(config);
  if (err) { alert(err); return; }

  document.getElementById('install-btn').disabled = true;
  document.getElementById('wizard').style.opacity = '0.5';
  document.getElementById('wizard').style.pointerEvents = 'none';

  const logPanel = document.getElementById('log-panel');
  logPanel.style.display = 'block';

  try {
    await fetch('/install', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config),
    });
  } catch(e) {}

  pollLog();
}

let _logOffset = 0;
let _polling   = false;
let _pollTimer = null;

function pollLog() {
  if (_polling) return;
  _polling = true;
  _pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/log?offset=${_logOffset}`);
      const j = await r.json();
      if (j.lines?.length) {
        const body = document.getElementById('log-body');
        for (const line of j.lines) {
          const div = document.createElement('div');
          div.className = 'log-line ' + (line.level || 'info');
          div.textContent = `[${line.ts}] ${line.msg}`;
          body.appendChild(div);
        }
        body.scrollTop = body.scrollHeight;
        _logOffset += j.lines.length;
      }
      if (j.done) {
        clearInterval(_pollTimer);
        _polling = false;
        document.getElementById('log-spin').style.display = 'none';
        if (j.ok) {
          document.getElementById('log-status').textContent = 'Complete ✓';
          showComplete(j);
        } else {
          document.getElementById('log-status').textContent = 'Install failed';
          document.getElementById('error-panel').style.display = 'block';
        }
      }
    } catch(e) {}
  }, 800);
}

function showComplete(j) {
  const panel = document.getElementById('complete-panel');
  panel.style.display = 'block';
  const detail = document.getElementById('complete-detail');
  detail.innerHTML = [
    `Company DB:   ${j.db_path || 'data/company.db'}`,
    `Dashboard:    http://&lt;this-ip&gt;:${j.port || 5010}/console?token=&lt;SECRET_TOKEN&gt;`,
    `Scoop dry run: ${j.dry_run || 'false'}`,
  ].join('<br>');
  const cmds = document.getElementById('start-cmds');
  cmds.innerHTML = [
    `python3 src/company_server.py &`,
    `python3 src/scoop.py &`,
  ].join('<br>');
}
</script>
</body>
</html>"""


# ── HTTP SERVER ───────────────────────────────────────────────────────────────
class WizardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def _send(self, body: str, status: int = 200, ct: str = "text/html"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict, status: int = 200):
        self._send(json.dumps(obj), status, "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path in ("/", "/wizard"):
            self._send(WIZARD_HTML)

        elif path == "/preflight":
            issues = run_preflight()
            self._json({"ok": len(issues) == 0, "issues": issues})

        elif path == "/log":
            qs     = parse_qs(parsed.query)
            offset = int(qs.get("offset", ["0"])[0])
            lines  = _state["log_lines"][offset:]
            done   = _state["install_done"] or bool(_state["install_error"])
            resp: dict = {"lines": lines, "done": done, "ok": _state.get("ok", False)}
            if done:
                config = _state.get("config", {})
                resp["port"]     = config.get("port", "5010")
                resp["db_path"]  = config.get("company_db_path", str(DB_PATH))
                resp["dry_run"]  = config.get("scoop_dry_run", "false")
            self._json(resp)

        elif path == "/status":
            prog = _load_progress()
            self._json({
                "phase":   prog.get("phase", "UNINITIALIZED"),
                "sentinel": SENTINEL_PATH.exists(),
            })

        else:
            self._send("Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/install":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                config = json.loads(body)
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return

            if _state["install_started"]:
                self._json({"error": "install already in progress"}, 409)
                return

            _state["install_started"] = True
            _state["install_done"]    = False
            _state["install_error"]   = None
            _state["config"]          = config

            def _run():
                ok = run_install(config)
                _state["install_done"] = True
                _state["ok"]           = ok
                if not ok:
                    _state["install_error"] = "See log for details"

            threading.Thread(target=_run, daemon=True).start()
            self._json({"ok": True, "msg": "install started"})

        else:
            self._send("Not found", 404)


def _open_browser(port: int) -> None:
    time.sleep(1.2)
    url = f"http://localhost:{port}"
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url])
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", url])
    except Exception:
        pass


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthos Company Node Installer"
    )
    parser.add_argument("--repair",  action="store_true",
                        help="Re-run install even if already complete")
    parser.add_argument("--status",  action="store_true",
                        help="Print current install state and exit")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't attempt to open browser automatically")
    args = parser.parse_args()

    if args.status:
        prog = _load_progress()
        phase = prog.get("phase", "UNINITIALIZED")
        sentinel = "✓" if SENTINEL_PATH.exists() else "✗"
        print(f"\nSynthos Company Node Install Status")
        print(f"  Phase:    {phase}")
        print(f"  Complete: {sentinel}")
        print(f"  Env:      {'✓' if ENV_PATH.exists() else '✗'}")
        print(f"  DB:       {'✓' if DB_PATH.exists() else '✗'}")
        print()
        return 0

    if SENTINEL_PATH.exists() and not args.repair:
        log.info("Company Node already installed. Use --repair to re-run.")
        log.info(f"Status: {SENTINEL_PATH}")
        return 0

    # Preflight check before starting server
    issues = run_preflight()
    if issues:
        log.error("Preflight failed:")
        for i in issues:
            log.error(f"  ✗ {i}")
        # Don't exit — wizard will show issues and let operator fix them
        _save_progress("PREFLIGHT_FAILED")

    _save_progress("PREFLIGHT")

    server = HTTPServer(("0.0.0.0", INSTALLER_PORT), WizardHandler)
    log.info(f"Company Node Setup Wizard running on port {INSTALLER_PORT}")
    log.info(f"Open: http://localhost:{INSTALLER_PORT}")
    log.info("Press Ctrl+C to stop")

    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(INSTALLER_PORT,), daemon=True).start()

    try:
        while True:
            server.handle_request()
            if _state["install_done"]:
                time.sleep(2)  # let browser poll complete state
                break
    except KeyboardInterrupt:
        log.info("Installer stopped by operator")
        _save_progress("CANCELLED")
        return 3

    return 0 if _state.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
