"""
install_monitor.py — Synthos Monitor Node Installer
Synthos · v3.0

First-time setup and safe rerun/repair for the Monitor Node (any Pi).
Runs a local web wizard on port 8082, writes user/.env, registers
a cron job to start synthos_monitor.py on boot.

The Monitor receives heartbeats from all retail Pis, shows a live
dashboard, and sends Resend silence alerts when a Pi goes quiet.

USAGE:
    python3 install_monitor.py             # first install or resume
    python3 install_monitor.py --repair    # re-run INSTALLING + VERIFYING
    python3 install_monitor.py --status    # print current install state

STATE MACHINE:
    UNINITIALIZED → PREFLIGHT → COLLECTING → INSTALLING → VERIFYING → COMPLETE
                                                         ↘ DEGRADED (on failure)

EXIT CODES:
    0 — success or already complete
    1 — preflight failure
    2 — install or verification failure (DEGRADED)
    3 — operator cancelled

REQUIRED FILES (src/):
    synthos_monitor.py  — Flask heartbeat dashboard + silence alerting (port 5000)

POST-INSTALL:
    Add to each retail Pi .env:
        MONITOR_URL=http://<monitor-ip>:<port>
        MONITOR_TOKEN=<SECRET_TOKEN from this install>
        PI_ID=<unique name e.g. synthos-pi-1>

    Add Resend key for silence email alerts:
        RESEND_API_KEY=re_...   (add to user/.env manually or via portal)
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
ENV_PATH:      Path = USER_DIR / ".env"
SENTINEL_PATH: Path = SYNTHOS_HOME / ".monitor_install_complete"
PROGRESS_PATH: Path = SYNTHOS_HOME / ".monitor_install_progress.json"

_COMMON_DIR = SYNTHOS_HOME / "installers" / "common"
if str(_COMMON_DIR.parent.parent) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR.parent.parent))

from installers.common.env_writer import write_env, build_monitor_env

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
SYNTHOS_VERSION = "3.0"
INSTALLER_PORT  = 8082

REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "python-dotenv",
]

REQUIRED_CORE_FILES = [
    "synthos_monitor.py",
]

PROTECTED_PATHS = [
    ENV_PATH,
]

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "install_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("install_monitor")

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

    if sys.version_info < (3, 9):
        issues.append(f"Python 3.9+ required (found {sys.version})")

    for fname in REQUIRED_CORE_FILES:
        if not (CORE_DIR / fname).exists():
            issues.append(f"Missing required file: src/{fname}")

    try:
        usage = shutil.disk_usage(SYNTHOS_HOME)
        free_mb = usage.free // (1024 * 1024)
        if free_mb < 100:
            issues.append(f"Low disk space: {free_mb}MB free (need 100MB)")
    except Exception as e:
        log.warning(f"Disk check failed: {e}")

    return issues


# ── DIRECTORY CREATION ────────────────────────────────────────────────────────
def create_directories() -> None:
    dirs = [USER_DIR, DATA_DIR, LOG_DIR]
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


# ── CRON REGISTRATION ─────────────────────────────────────────────────────────
def register_cron(config: dict) -> bool:
    """Register @reboot cron entry to start synthos_monitor.py."""
    if not shutil.which("crontab"):
        _log_ui("  ⚠ crontab not found — add cron entry manually", "warning")
        return False

    monitor_script = str(CORE_DIR / "synthos_monitor.py")
    monitor_log    = str(LOG_DIR / "synthos_monitor.log")

    new_entry = "\n".join([
        f"# SYNTHOS MONITOR NODE — generated by install_monitor.py at {datetime.now().isoformat()}",
        f"@reboot sleep 20 && {sys.executable} {monitor_script} >> {monitor_log} 2>&1 &",
        "",
    ])

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing_content = existing.stdout if existing.returncode == 0 else ""

        clean_lines = [
            line for line in existing_content.splitlines()
            if "SYNTHOS MONITOR NODE" not in line
            and "synthos_monitor" not in line
        ]
        clean_existing = "\n".join(clean_lines).strip()
        final_crontab  = (clean_existing + "\n" + new_entry).strip() + "\n"

        proc = subprocess.run(
            ["crontab", "-"], input=final_crontab, text=True, capture_output=True
        )
        if proc.returncode == 0:
            _log_ui("  ✓ Cron registered (synthos_monitor on @reboot)")
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

    if not ENV_PATH.exists():
        issues.append(".env file not found")
    else:
        content = ENV_PATH.read_text()
        for key in ("SECRET_TOKEN", "PORT"):
            if key not in content:
                issues.append(f".env missing {key}")

    for fname in REQUIRED_CORE_FILES:
        if not (CORE_DIR / fname).exists():
            issues.append(f"Source file missing: src/{fname}")

    return (len(issues) == 0), issues


# ── INSTALL ORCHESTRATOR ──────────────────────────────────────────────────────
def run_install(config: dict) -> bool:
    _log_ui("=== Monitor Node Install Starting ===")
    _save_progress("INSTALLING", config)

    _log_ui("Step 1/4 — Creating directories…")
    create_directories()

    _log_ui("Step 2/4 — Installing Python packages…")
    if not install_packages():
        _log_ui("Package install had errors — continuing (some may already be installed)", "warning")

    _log_ui("Step 3/4 — Writing .env…")
    try:
        env_content = build_monitor_env(config)
        write_env(ENV_PATH, env_content)
        _log_ui("  ✓ user/.env written (chmod 600)")
    except Exception as exc:
        _log_ui(f"  ✗ Failed to write .env: {exc}", "error")
        _save_progress("DEGRADED")
        return False

    _log_ui("Step 4/4 — Registering cron…")
    register_cron(config)

    _log_ui("Verifying installation…")
    ok, issues = verify_install(config)
    if ok:
        SENTINEL_PATH.write_text(
            json.dumps({"installed_at": datetime.now(timezone.utc).isoformat(),
                        "version": SYNTHOS_VERSION})
        )
        _save_progress("COMPLETE")
        _log_ui("=== Monitor Node Install Complete ✓ ===")
        port = config.get("port", 5000)
        _log_ui(f"Dashboard: http://0.0.0.0:{port}/")
        _log_ui(f"Token:     {config.get('secret_token','<see user/.env>')}")
        _log_ui("Next: add MONITOR_URL and MONITOR_TOKEN to each retail Pi .env")
        return True
    else:
        for issue in issues:
            _log_ui(f"  ✗ {issue}", "error")
        _save_progress("DEGRADED")
        _log_ui("=== Install completed with issues (DEGRADED) ===", "error")
        return False


# ── WEB WIZARD ────────────────────────────────────────────────────────────────
_WIZARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.14);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.4);--dim:rgba(255,255,255,0.18);
  --teal:#00f5d4;--pink:#ff4b6e;--amber:#ffb347;--purple:#7b61ff;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.header{border-bottom:1px solid var(--border);padding:0 28px;height:56px;display:flex;
  align-items:center;gap:12px;background:rgba(8,11,18,0.95)}
.wordmark{font-family:var(--mono);font-size:1rem;font-weight:600;letter-spacing:0.15em;
  color:var(--teal);text-shadow:0 0 20px rgba(0,245,212,0.4)}
.badge{font-size:9px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  padding:3px 8px;border-radius:99px;border:1px solid rgba(123,97,255,0.3);
  background:rgba(123,97,255,0.1);color:#a78bfa}
.page{max-width:660px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:6px}
.subtitle{font-size:13px;color:var(--muted);margin-bottom:28px;line-height:1.5}
.section{border:1px solid var(--border);border-radius:14px;background:var(--surface);
  margin-bottom:16px;overflow:hidden}
.section-head{padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px}
.section-num{width:22px;height:22px;border-radius:50%;background:rgba(0,245,212,0.12);
  border:1px solid rgba(0,245,212,0.3);color:var(--teal);font-size:10px;font-weight:700;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.section-title{font-size:13px;font-weight:600}
.section-body{padding:18px}
.field{margin-bottom:16px}
.field:last-child{margin-bottom:0}
label{display:block;font-size:11px;font-weight:600;letter-spacing:0.05em;
  text-transform:uppercase;color:var(--muted);margin-bottom:6px}
input[type=text],input[type=email],input[type=number]{
  width:100%;padding:10px 12px;border-radius:8px;
  border:1px solid var(--border2);background:var(--surface2);
  color:var(--text);font-size:13px;font-family:var(--sans);outline:none;transition:border 0.15s}
input:focus{border-color:rgba(0,245,212,0.4)}
input[readonly]{opacity:0.6;cursor:default}
.hint{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.4}
.token-row{display:flex;gap:8px}
.token-row input{flex:1;font-family:var(--mono);font-size:11px}
.copy-btn{padding:0 14px;border-radius:8px;border:1px solid rgba(0,245,212,0.25);
  background:rgba(0,245,212,0.06);color:var(--teal);font-size:11px;font-weight:600;
  cursor:pointer;font-family:var(--sans);white-space:nowrap;transition:all 0.15s}
.copy-btn:hover{background:rgba(0,245,212,0.12)}
.info-box{background:rgba(0,245,212,0.04);border:1px solid rgba(0,245,212,0.15);
  border-radius:8px;padding:12px 14px;font-size:12px;color:var(--muted);line-height:1.6;margin-top:4px}
.info-box code{font-family:var(--mono);font-size:10px;color:var(--teal)}
.warn-box{background:rgba(255,179,71,0.06);border:1px solid rgba(255,179,71,0.2);
  border-radius:8px;padding:12px 14px;font-size:12px;color:rgba(255,179,71,0.8);line-height:1.6;margin-bottom:16px}
.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
.btn-primary{padding:10px 28px;border-radius:9px;border:none;
  background:linear-gradient(135deg,rgba(0,245,212,0.9),rgba(0,200,180,0.9));
  color:#040a0d;font-size:13px;font-weight:700;cursor:pointer;font-family:var(--sans);transition:opacity 0.15s}
.btn-primary:hover{opacity:0.88}
.btn-secondary{padding:10px 20px;border-radius:9px;
  border:1px solid var(--border2);background:transparent;
  color:var(--muted);font-size:13px;font-weight:600;cursor:pointer;font-family:var(--sans)}
.review-table{width:100%;border-collapse:collapse;font-size:12px}
.review-table td{padding:8px 0;border-bottom:1px solid var(--border);vertical-align:top}
.review-table td:first-child{color:var(--muted);width:45%;padding-right:12px}
.review-table tr:last-child td{border-bottom:none}
.review-table code{font-family:var(--mono);font-size:11px;color:var(--teal)}
.step-progress{display:flex;align-items:center;gap:6px;margin-bottom:28px}
.step-dot{width:8px;height:8px;border-radius:50%}
.step-dot.active{background:var(--teal);box-shadow:0 0 8px rgba(0,245,212,0.5)}
.step-dot.done{background:rgba(0,245,212,0.4)}
.step-dot.future{background:var(--border2)}
.step-label{font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
  color:var(--muted);margin-left:4px}
/* Log */
.log-wrap{border:1px solid var(--border);border-radius:10px;background:var(--surface);
  padding:14px;max-height:300px;overflow-y:auto;margin-top:12px}
.log-line{font-family:var(--mono);font-size:11px;line-height:1.7;white-space:pre-wrap}
.log-line.error{color:var(--pink)}
.log-line.warning{color:var(--amber)}
.log-line.info{color:rgba(255,255,255,0.55)}
.status-icon{font-size:36px;margin-bottom:8px}
"""

def _page(step: str, body: str, progress_dots: str = "") -> str:
    return (
        f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Synthos Monitor Setup</title>"
        f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
        f"&family=JetBrains+Mono:wght@400;600&display=swap' rel='stylesheet'>"
        f"<style>{_WIZARD_CSS}</style></head><body>"
        f"<div class='header'><span class='wordmark'>SYNTHOS</span>"
        f"<span class='badge'>Monitor Setup</span></div>"
        f"<div class='page'>{progress_dots}{body}</div></body></html>"
    )


def _dots(active: int, total: int = 3) -> str:
    dots = ""
    for i in range(total):
        cls = "active" if i == active else ("done" if i < active else "future")
        dots += f"<div class='step-dot {cls}'></div>"
    label = f"Step {active + 1} of {total}" if active < total else "Complete"
    return f"<div class='step-progress'>{dots}<span class='step-label'>{label}</span></div>"


def page_welcome(token: str) -> str:
    return _page("welcome", f"""
<h1>Monitor Node Setup</h1>
<p class='subtitle'>This wizard sets up <strong>synthos_monitor.py</strong> — the heartbeat receiver
and silence alerting service for all your Synthos retail Pis.</p>
<div class='warn-box'>
  Run this on the Pi dedicated to monitoring. It does not run trading agents — it only
  watches other Pis and alerts you when one goes silent.
</div>
<div class='section'>
  <div class='section-head'><div class='section-num'>1</div>
    <div class='section-title'>What this installs</div></div>
  <div class='section-body'>
    <div class='info-box'>
      <strong style='color:var(--text)'>synthos_monitor.py</strong> — starts on boot (port 5000)<br>
      Receives <code>POST /heartbeat</code> from each retail Pi every session<br>
      Serves a live dashboard at <code>http://&lt;this-ip&gt;:5000/</code><br>
      Sends Resend silence alerts if a Pi misses 3 heartbeats<br><br>
      <strong style='color:var(--text)'>After install, add to each retail Pi .env:</strong><br>
      <code>MONITOR_URL=http://&lt;this-ip&gt;:5000</code><br>
      <code>MONITOR_TOKEN=&lt;token shown on next screen&gt;</code><br>
      <code>PI_ID=synthos-pi-1</code>
    </div>
  </div>
</div>
<div class='actions'>
  <form method='POST' action='/start'>
    <input type='hidden' name='token' value='{token}'>
    <button class='btn-primary' type='submit'>Begin Setup →</button>
  </form>
</div>
""")


def page_token(token: str, errors: list | None = None) -> str:
    err_html = ""
    if errors:
        err_html = "".join(
            f"<p style='color:var(--pink);font-size:12px;margin-bottom:8px'>⚠ {e}</p>"
            for e in errors
        )
    return _page("token", f"""
{err_html}
<h1>Token &amp; Alert Contact</h1>
<p class='subtitle'>Step 1 of 3 — Your SECRET_TOKEN is the shared key used by all retail Pis
to authenticate heartbeats. Keep it safe.</p>
<form method='POST' action='/token'>
  <div class='section'>
    <div class='section-head'><div class='section-num'>1</div>
      <div class='section-title'>Secret Token</div></div>
    <div class='section-body'>
      <div class='field'>
        <label>Secret Token</label>
        <div class='token-row'>
          <input type='text' id='secret_token' name='secret_token'
            value='{token}' readonly>
          <button type='button' class='copy-btn'
            onclick="navigator.clipboard.writeText(document.getElementById('secret_token').value);this.textContent='Copied!'">
            Copy
          </button>
        </div>
        <p class='hint'>Auto-generated. Copy this — you'll paste it into each retail Pi .env as <code style='font-family:monospace'>MONITOR_TOKEN</code>.</p>
      </div>
    </div>
  </div>
  <div class='section'>
    <div class='section-head'><div class='section-num'>2</div>
      <div class='section-title'>Alert Contact</div></div>
    <div class='section-body'>
      <div class='field'>
        <label>Ops Email (alerts sent here)</label>
        <input type='email' name='alert_to' placeholder='you@yourdomain.com' required>
        <p class='hint'>Where silence alerts are sent when a Pi goes quiet. Add <code style='font-family:monospace'>RESEND_API_KEY</code> to .env post-install to activate email.</p>
      </div>
    </div>
  </div>
  <div class='actions'>
    <a href='/' class='btn-secondary'>Back</a>
    <button class='btn-primary' type='submit'>Next →</button>
  </div>
</form>
""", _dots(0))


def page_network(config: dict, errors: list | None = None) -> str:
    err_html = ""
    if errors:
        err_html = "".join(
            f"<p style='color:var(--pink);font-size:12px;margin-bottom:8px'>⚠ {e}</p>"
            for e in errors
        )
    return _page("network", f"""
{err_html}
<h1>Network &amp; Integrations</h1>
<p class='subtitle'>Step 2 of 3 — Configure the monitor port and optional Company Pi integration.</p>
<form method='POST' action='/network'>
  <input type='hidden' name='secret_token' value="{config.get('secret_token','')}">
  <input type='hidden' name='alert_to' value="{config.get('alert_to','')}">
  <div class='section'>
    <div class='section-head'><div class='section-num'>1</div>
      <div class='section-title'>Monitor Port</div></div>
    <div class='section-body'>
      <div class='field'>
        <label>Port</label>
        <input type='number' name='port' value="{config.get('port', '5000')}"
          min='1024' max='65535' required>
        <p class='hint'>Default 5000. Retail Pis POST heartbeats here. Must be reachable from all retail Pis on your network.</p>
      </div>
    </div>
  </div>
  <div class='section'>
    <div class='section-head'><div class='section-num'>2</div>
      <div class='section-title'>Company Pi Integration (optional)</div></div>
    <div class='section-body'>
      <div class='field'>
        <label>Company Server URL</label>
        <input type='text' name='company_url'
          value="{config.get('company_url', '')}"
          placeholder='http://192.168.1.xx:5010'>
        <p class='hint'>If set, the monitor will forward events to the Company Pi for the ops dashboard. Leave blank to skip.</p>
      </div>
    </div>
  </div>
  <div class='actions'>
    <a href='/token-back' class='btn-secondary'>Back</a>
    <button class='btn-primary' type='submit'>Review →</button>
  </div>
</form>
""", _dots(1))


def page_review(config: dict) -> str:
    company_url = config.get("company_url") or "—  (not configured)"
    return _page("review", f"""
<h1>Review &amp; Install</h1>
<p class='subtitle'>Step 3 of 3 — Confirm your settings then click Install.</p>
<div class='section'>
  <div class='section-head'><div class='section-num'>✓</div>
    <div class='section-title'>Configuration summary</div></div>
  <div class='section-body'>
    <table class='review-table'>
      <tr><td>Secret Token</td><td><code>{"*" * 8 + config.get("secret_token","")[-6:]}</code></td></tr>
      <tr><td>Alert email</td><td>{config.get("alert_to","—")}</td></tr>
      <tr><td>Monitor port</td><td>{config.get("port", 5000)}</td></tr>
      <tr><td>Company URL</td><td>{company_url}</td></tr>
      <tr><td>Alert from</td><td>alerts@yourdomain.com <span style='color:var(--muted)'>(set in .env after install)</span></td></tr>
      <tr><td>Resend API key</td><td><span style='color:var(--muted)'>— add to .env after install</span></td></tr>
    </table>
  </div>
</div>
<div class='warn-box'>
  This will write <code>user/.env</code>, install Python packages, and register a cron job.
  Existing .env is backed up before overwrite.
</div>
<form method='POST' action='/install'>
  <input type='hidden' name='secret_token' value="{config.get('secret_token','')}">
  <input type='hidden' name='alert_to' value="{config.get('alert_to','')}">
  <input type='hidden' name='port' value="{config.get('port', 5000)}">
  <input type='hidden' name='company_url' value="{config.get('company_url','')}">
  <div class='actions'>
    <a href='/network-back' class='btn-secondary'>Back</a>
    <button class='btn-primary' type='submit'>Install Monitor Node →</button>
  </div>
</form>
""", _dots(2))


def page_installing() -> str:
    return _page("installing", """
<h1>Installing…</h1>
<p class='subtitle'>Setting up the Monitor Node. This usually takes under a minute.</p>
<div id='log-wrap' class='log-wrap'>
  <div id='log-inner'><span class='log-line info'>Starting install…</span></div>
</div>
<div id='status-area' style='margin-top:20px'></div>
<script>
async function poll(){
  const r=await fetch('/api/log').then(x=>x.json());
  const inner=document.getElementById('log-inner');
  inner.innerHTML=r.lines.map(l=>
    `<div class="log-line ${l.level}">[${l.ts}] ${l.msg}</div>`
  ).join('');
  const wrap=document.getElementById('log-wrap');
  wrap.scrollTop=wrap.scrollHeight;
  if(r.done){
    document.getElementById('status-area').innerHTML=r.ok
      ?`<div style='text-align:center'>
          <div class='status-icon'>✅</div>
          <h2 style='margin-bottom:8px'>Monitor Node Ready</h2>
          <p style='color:var(--muted);font-size:13px;margin-bottom:20px'>
            Dashboard: <code style='font-family:monospace;color:var(--teal)'>
            http://&lt;this-ip&gt;:${r.port}/</code>
          </p>
          <div class='info-box' style='text-align:left;margin-bottom:20px'>
            <strong style='color:var(--text)'>Add to each retail Pi .env:</strong><br>
            <code>MONITOR_URL=http://&lt;this-ip&gt;:${r.port}</code><br>
            <code>MONITOR_TOKEN=${r.token}</code><br>
            <code>PI_ID=synthos-pi-1</code><br><br>
            <strong style='color:var(--text)'>To enable silence alerts:</strong><br>
            Add <code>RESEND_API_KEY=re_...</code> and <code>ALERT_FROM=alerts@yourdomain.com</code>
            to <code>user/.env</code> — no restart needed (reads on alert trigger).
          </div>
        </div>`
      :`<div style='text-align:center;padding:20px'>
          <div class='status-icon'>⚠️</div>
          <h2 style='margin-bottom:8px;color:var(--pink)'>Install completed with issues</h2>
          <p style='color:var(--muted);font-size:13px'>Check the log above. Run with --repair to retry.</p>
        </div>`;
  } else {
    setTimeout(poll, 1200);
  }
}
poll();
</script>
""")


# ── HTTP HANDLER ──────────────────────────────────────────────────────────────
class WizardHandler(BaseHTTPRequestHandler):
    """Single-threaded wizard HTTP handler."""

    def log_message(self, fmt, *args):  # suppress default access log
        pass

    def _send(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str) -> None:
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_post(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    # ── GET ────────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", ""):
            token = secrets.token_hex(32)
            self._send(page_welcome(token))

        elif path == "/token-back":
            token = _state["config"].get("secret_token") or secrets.token_hex(32)
            self._send(page_token(token))

        elif path == "/network-back":
            self._send(page_network(_state["config"]))

        elif path == "/api/log":
            self._json({
                "lines": _state["log_lines"],
                "done":  _state["install_done"],
                "ok":    _state["install_error"] is None and _state["install_done"],
                "port":  _state["config"].get("port", 5000),
                "token": _state["config"].get("secret_token", ""),
            })

        else:
            self._send("<h1>Not found</h1>", 404)

    # ── POST ───────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._parse_post()

        if path == "/start":
            token = data.get("token") or secrets.token_hex(32)
            self._send(page_token(token))

        elif path == "/token":
            errors = []
            secret_token = data.get("secret_token", "").strip()
            alert_to     = data.get("alert_to", "").strip()
            if len(secret_token) < 16:
                errors.append("Token must be at least 16 characters")
            if not alert_to or "@" not in alert_to:
                errors.append("A valid alert email address is required")
            if errors:
                self._send(page_token(secret_token, errors))
                return
            _state["config"].update({
                "secret_token": secret_token,
                "alert_to":     alert_to,
            })
            self._send(page_network(_state["config"]))

        elif path == "/network":
            errors = []
            try:
                port = int(data.get("port", 5000))
                if not (1024 <= port <= 65535):
                    errors.append("Port must be between 1024 and 65535")
            except ValueError:
                errors.append("Port must be a number")
                port = 5000

            config_update = dict(_state["config"])
            config_update.update({
                "port":        port,
                "company_url": data.get("company_url", "").strip(),
            })
            if errors:
                self._send(page_network(config_update, errors))
                return
            _state["config"] = config_update
            self._send(page_review(_state["config"]))

        elif path == "/install":
            if _state["install_started"]:
                self._redirect("/installing")
                return
            _state["install_started"] = True
            config = {
                "secret_token": data.get("secret_token", "").strip(),
                "alert_to":     data.get("alert_to", "").strip(),
                "port":         int(data.get("port", 5000)),
                "company_url":  data.get("company_url", "").strip(),
            }
            _state["config"] = config

            def _run():
                try:
                    ok = run_install(config)
                    _state["install_done"]  = True
                    _state["install_error"] = None if ok else "Install had errors"
                except Exception as exc:
                    _state["install_done"]  = True
                    _state["install_error"] = str(exc)
                    log.exception("Install thread exception")

            threading.Thread(target=_run, daemon=True).start()
            self._send(page_installing())

        else:
            self._send("<h1>Not found</h1>", 404)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Synthos Monitor Node Installer")
    parser.add_argument("--repair", action="store_true",
                        help="Force re-run of INSTALLING + VERIFYING phases")
    parser.add_argument("--status", action="store_true",
                        help="Print current install state and exit")
    args = parser.parse_args()

    progress = _load_progress()

    if args.status:
        phase = progress.get("phase", "UNINITIALIZED")
        print(f"Monitor install phase: {phase}")
        if SENTINEL_PATH.exists():
            info = json.loads(SENTINEL_PATH.read_text())
            print(f"Installed at: {info.get('installed_at','unknown')}")
        return 0

    if progress.get("phase") == "COMPLETE" and not args.repair:
        print("Monitor Node already installed. Use --repair to re-run.")
        return 0

    # Preflight
    issues = run_preflight()
    if issues:
        print("Preflight failed:")
        for i in issues:
            print(f"  ✗ {i}")
        return 1

    _save_progress("PREFLIGHT")
    print(f"\nSynthos Monitor Node Installer v{SYNTHOS_VERSION}")
    print(f"Open http://localhost:{INSTALLER_PORT} in your browser to continue.\n")

    server = HTTPServer(("0.0.0.0", INSTALLER_PORT), WizardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nInstaller cancelled.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
